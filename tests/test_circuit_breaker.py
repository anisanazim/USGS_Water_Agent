from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from usgs_water_data import USGSNotFoundError, USGSServiceError, USGSValidationError, USGSWaterDataFetcher
from water_agent.circuit_breaker import CircuitBreaker, CircuitBreakerOpenError, CircuitState
from water_agent.fetchers.samples_fetcher import SamplesError, SamplesValidationError, USGSSamplesFetcher


@pytest.mark.asyncio
async def test_circuit_breaker_transitions_to_open():
    cb = CircuitBreaker("test_service", failure_threshold=3, recovery_timeout=0.1)
    call_count = 0

    async def failing_func():
        nonlocal call_count
        call_count += 1
        raise ConnectionError("Service unreachable")

    # First 3 attempts fail and are recorded
    for _ in range(3):
        with pytest.raises(ConnectionError):
            await cb.call(failing_func)

    assert cb.state == CircuitState.OPEN
    assert cb.failure_count == 3
    assert call_count == 3

    # 4th attempt must fail fast without calling the function
    with pytest.raises(CircuitBreakerOpenError) as exc_info:
        await cb.call(failing_func)

    assert "is OPEN" in str(exc_info.value)
    assert call_count == 3  # Did not invoke the failing function!


@pytest.mark.asyncio
async def test_circuit_breaker_half_open_and_recovery():
    cb = CircuitBreaker("test_service", failure_threshold=2, recovery_timeout=0.05)

    async def failing_func():
        raise ConnectionError("Outage")

    for _ in range(2):
        with pytest.raises(ConnectionError):
            await cb.call(failing_func)

    assert cb.state == CircuitState.OPEN

    # Sleep past recovery timeout
    await asyncio.sleep(0.06)
    assert cb.state == CircuitState.HALF_OPEN

    # Probe call succeeds
    async def healthy_func():
        return "success"

    result = await cb.call(healthy_func)
    assert result == "success"
    assert cb.state == CircuitState.CLOSED
    assert cb.failure_count == 0


@pytest.mark.asyncio
async def test_circuit_breaker_reopens_on_probe_failure():
    cb = CircuitBreaker("test_service", failure_threshold=2, recovery_timeout=0.05)

    async def failing_func():
        raise ConnectionError("Outage")

    for _ in range(2):
        with pytest.raises(ConnectionError):
            await cb.call(failing_func)

    await asyncio.sleep(0.06)
    assert cb.state == CircuitState.HALF_OPEN

    # Probe fails
    with pytest.raises(ConnectionError):
        await cb.call(failing_func)

    assert cb.state == CircuitState.OPEN


@pytest.mark.asyncio
async def test_circuit_breaker_ignores_excluded_exceptions():
    cb = CircuitBreaker(
        "test_service",
        failure_threshold=2,
        expected_exceptions=(Exception,),
        excluded_exceptions=(ValueError,),
    )

    async def validation_error_func():
        raise ValueError("Invalid client input")

    for _ in range(5):
        with pytest.raises(ValueError):
            await cb.call(validation_error_func)

    # Excluded exceptions must NOT increment failure count or open the circuit
    assert cb.failure_count == 0
    assert cb.state == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_water_data_fetcher_circuit_breaker():
    fetcher = USGSWaterDataFetcher()
    fetcher.circuit_breaker.failure_threshold = 2
    fetcher.circuit_breaker.recovery_timeout = 0.05

    # Mock _client.get to raise TransportError
    fetcher._client.get = AsyncMock(side_effect=httpx.ConnectError("USGS server down"))

    with patch("usgs_water_data.RETRY_BACKOFF_BASE_SECONDS", 0.001):
        with pytest.raises(USGSServiceError):
            await fetcher._request("https://api.waterdata.usgs.gov/ogcapi/v0/collections", {})

        with pytest.raises(USGSServiceError):
            await fetcher._request("https://api.waterdata.usgs.gov/ogcapi/v0/collections", {})

    assert fetcher.circuit_breaker.state == CircuitState.OPEN

    # Next call fails fast with CircuitBreakerOpenError
    with pytest.raises(CircuitBreakerOpenError):
        await fetcher._request("https://api.waterdata.usgs.gov/ogcapi/v0/collections", {})


@pytest.mark.asyncio
async def test_samples_fetcher_circuit_breaker():
    fetcher = USGSSamplesFetcher()
    fetcher.circuit_breaker.failure_threshold = 2
    fetcher.circuit_breaker.recovery_timeout = 0.05

    # Mock _client.get to return 503 Service Unavailable
    mock_503 = httpx.Response(status_code=503, text="Service Unavailable", request=httpx.Request("GET", "https://api.waterdata.usgs.gov"))
    fetcher._client.get = AsyncMock(return_value=mock_503)

    with patch("asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(SamplesError):
            await fetcher.get_results_count({"stateFips": "US:37"})

        with pytest.raises(SamplesError):
            await fetcher.get_results_count({"stateFips": "US:37"})

    assert fetcher.circuit_breaker.state == CircuitState.OPEN

    # Next call fails fast with CircuitBreakerOpenError
    with pytest.raises(CircuitBreakerOpenError):
        await fetcher.get_results_count({"stateFips": "US:37"})
