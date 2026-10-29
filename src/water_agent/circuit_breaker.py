from __future__ import annotations

import asyncio
import logging
import time
from enum import Enum
from typing import Any, Callable, Coroutine, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreakerOpenError(Exception):
    """Raised when an operation is attempted while the circuit breaker is OPEN."""

    def __init__(self, service_name: str, retry_after: float):
        self.service_name = service_name
        self.retry_after = retry_after
        super().__init__(
            f"Circuit breaker for '{service_name}' is OPEN. "
            f"Failing fast to prevent cascading failures. Retry in {retry_after:.1f}s."
        )


class CircuitBreaker:
    """Async circuit breaker implementing the state machine:
    CLOSED -> OPEN -> HALF_OPEN -> CLOSED.
    
    Protects downstream infrastructure from cascading task queue exhaustion
    when remote upstream services (like USGS APIs) experience outages or heavy latency.
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        expected_exceptions: tuple[type[Exception], ...] = (Exception,),
        excluded_exceptions: tuple[type[Exception], ...] = (),
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.expected_exceptions = expected_exceptions
        self.excluded_exceptions = excluded_exceptions

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time: float | None = None
        self._opened_at: float | None = None
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        if self._state == CircuitState.OPEN and self._opened_at is not None:
            if time.monotonic() - self._opened_at >= self.recovery_timeout:
                return CircuitState.HALF_OPEN
        return self._state

    @property
    def failure_count(self) -> int:
        return self._failure_count

    def status_details(self) -> dict[str, Any]:
        st = self.state
        remaining = 0.0
        if st == CircuitState.OPEN and self._opened_at:
            remaining = max(0.0, self.recovery_timeout - (time.monotonic() - self._opened_at))
        return {
            "name": self.name,
            "state": st.value,
            "failure_count": self._failure_count,
            "failure_threshold": self.failure_threshold,
            "recovery_timeout_s": self.recovery_timeout,
            "seconds_until_probe": round(remaining, 1),
        }

    async def call(
        self, func: Callable[..., Coroutine[Any, Any, T]], *args: Any, **kwargs: Any
    ) -> T:
        """Execute an async coroutine protected by this circuit breaker."""
        async with self._lock:
            current_state = self.state

            if current_state == CircuitState.OPEN:
                retry_after = max(
                    0.0,
                    self.recovery_timeout - (time.monotonic() - (self._opened_at or 0.0)),
                )
                raise CircuitBreakerOpenError(self.name, retry_after)

            if current_state == CircuitState.HALF_OPEN:
                self._state = CircuitState.HALF_OPEN

        try:
            result = await func(*args, **kwargs)
        except self.excluded_exceptions:
            # Explicitly excluded exceptions (e.g. 4xx validation errors) do not trip the breaker
            raise
        except self.expected_exceptions as exc:
            await self._record_failure(exc)
            raise
        else:
            await self._record_success()
            return result

    async def _record_success(self) -> None:
        async with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                logger.info("Circuit breaker '%s' probe succeeded! Closing circuit.", self.name)
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._opened_at = None

    async def _record_failure(self, exc: Exception) -> None:
        async with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.monotonic()
            logger.warning(
                "Circuit breaker '%s' recorded failure (%d/%d): %s",
                self.name,
                self._failure_count,
                self.failure_threshold,
                exc,
            )

            if self._state == CircuitState.HALF_OPEN or self._failure_count >= self.failure_threshold:
                self._state = CircuitState.OPEN
                self._opened_at = time.monotonic()
                logger.error(
                    "Circuit breaker '%s' tripped to OPEN. Failing fast for %ss.",
                    self.name,
                    self.recovery_timeout,
                )

    def reset(self) -> None:
        """Manually reset the breaker to CLOSED state."""
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._opened_at = None
        self._last_failure_time = None
