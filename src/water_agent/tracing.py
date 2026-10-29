from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextvars import ContextVar
from enum import Enum
from typing import Any, Callable, Coroutine, Optional, TypeVar

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

T = TypeVar("T")


def generate_trace_id() -> str:
    """Generate a 32-character hex trace ID (OpenTelemetry standard)."""
    return uuid.uuid4().hex


def generate_span_id() -> str:
    """Generate a 16-character hex span ID (OpenTelemetry standard)."""
    return uuid.uuid4().hex[:16]


class SpanStatus(str, Enum):
    OK = "ok"
    ERROR = "error"


class SpanEvent(BaseModel):
    name: str
    timestamp: float = Field(default_factory=time.time)
    attributes: dict[str, Any] = Field(default_factory=dict)


class Span(BaseModel):
    """Structured distributed tracing span compatible with OpenTelemetry conventions."""

    name: str
    trace_id: str
    span_id: str
    parent_span_id: Optional[str] = None
    start_time: float = Field(default_factory=time.time)
    end_time: Optional[float] = None
    duration_ms: Optional[float] = None
    status: SpanStatus = SpanStatus.OK
    status_message: Optional[str] = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    events: list[SpanEvent] = Field(default_factory=list)

    def set_attribute(self, key: str, value: Any) -> Span:
        self.attributes[key] = value
        return self

    def add_event(self, name: str, attributes: dict[str, Any] | None = None) -> Span:
        self.events.append(SpanEvent(name=name, attributes=attributes or {}))
        return self

    def set_error(self, exc: Exception | str) -> Span:
        self.status = SpanStatus.ERROR
        self.status_message = str(exc)
        self.set_attribute("error", True)
        self.set_attribute("error.message", str(exc))
        if isinstance(exc, Exception):
            self.set_attribute("error.type", type(exc).__name__)
        return self

    def finish(self) -> None:
        if self.end_time is None:
            self.end_time = time.time()
            self.duration_ms = round((self.end_time - self.start_time) * 1000, 2)

    def to_otel_dict(self) -> dict[str, Any]:
        """Export span in standard OpenTelemetry format."""
        return {
            "name": self.name,
            "context": {
                "trace_id": self.trace_id,
                "span_id": self.span_id,
            },
            "parent_id": self.parent_span_id,
            "start_time_unix_nano": int(self.start_time * 1e9),
            "end_time_unix_nano": int((self.end_time or time.time()) * 1e9),
            "duration_ms": self.duration_ms,
            "status": {"code": self.status.value, "message": self.status_message},
            "attributes": self.attributes,
            "events": [
                {
                    "name": ev.name,
                    "time_unix_nano": int(ev.timestamp * 1e9),
                    "attributes": ev.attributes,
                }
                for ev in self.events
            ],
        }


# Context-local storage for distributed trace propagation across async tasks
_CURRENT_TRACE_ID: ContextVar[str | None] = ContextVar("current_trace_id", default=None)
_CURRENT_SPAN: ContextVar[Span | None] = ContextVar("current_span", default=None)
_SPAN_COLLECTOR: ContextVar[list[Span] | None] = ContextVar("span_collector", default=None)


def get_current_trace_id() -> str | None:
    return _CURRENT_TRACE_ID.get()


def get_current_span() -> Span | None:
    return _CURRENT_SPAN.get()


class SpanContextManager:
    """Async and synchronous context manager for span lifecycles."""

    def __init__(
        self,
        name: str,
        attributes: dict[str, Any] | None = None,
        trace_id: str | None = None,
    ):
        self.name = name
        self.initial_attributes = attributes or {}
        self.specified_trace_id = trace_id
        self.span: Optional[Span] = None
        self._prev_span: Optional[Span] = None
        self._token_span = None
        self._token_trace = None

    def __enter__(self) -> Span:
        parent = _CURRENT_SPAN.get()
        active_trace = self.specified_trace_id or _CURRENT_TRACE_ID.get() or (parent.trace_id if parent else generate_trace_id())

        self.span = Span(
            name=self.name,
            trace_id=active_trace,
            span_id=generate_span_id(),
            parent_span_id=parent.span_id if parent else None,
            attributes=dict(self.initial_attributes),
        )

        self._prev_span = parent
        self._token_trace = _CURRENT_TRACE_ID.set(active_trace)
        self._token_span = _CURRENT_SPAN.set(self.span)
        return self.span

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self.span is not None:
            if exc_val is not None:
                self.span.set_error(exc_val)
            self.span.finish()

            collector = _SPAN_COLLECTOR.get()
            if collector is not None:
                collector.append(self.span)

        if self._token_span:
            _CURRENT_SPAN.reset(self._token_span)
        if self._token_trace:
            _CURRENT_TRACE_ID.reset(self._token_trace)

    async def __aenter__(self) -> Span:
        return self.__enter__()

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        self.__exit__(exc_type, exc_val, exc_tb)


def start_span(
    name: str,
    attributes: dict[str, Any] | None = None,
    trace_id: str | None = None,
) -> SpanContextManager:
    """Begin a tracing span with automatic parentage and trace propagation."""
    return SpanContextManager(name=name, attributes=attributes, trace_id=trace_id)


class TraceLogFilter(logging.Filter):
    """Logging filter that injects active trace_id and span_id into log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = get_current_trace_id() or "-"
        span = get_current_span()
        record.span_id = span.span_id if span else "-"
        return True
