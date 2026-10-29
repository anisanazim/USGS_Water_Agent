from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent import USGSWaterAgent
from water_agent.health import HealthChecker, HealthStatus


@pytest.mark.asyncio
async def test_health_check_all_healthy(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake-key-12345678")

    mock_ogc_resp = MagicMock(status_code=200)
    mock_samples_resp = MagicMock(status_code=200)

    mock_water_fetcher = MagicMock()
    mock_water_fetcher.base_url = "https://api.waterdata.usgs.gov/ogcapi/v0"
    mock_water_fetcher._client = MagicMock()
    mock_water_fetcher._client.get = AsyncMock(return_value=mock_ogc_resp)

    mock_samples_fetcher = MagicMock()
    mock_samples_fetcher.base_url = "https://api.waterdata.usgs.gov/samples-data"
    mock_samples_fetcher._client = MagicMock()
    mock_samples_fetcher._client.get = AsyncMock(return_value=mock_samples_resp)

    checker = HealthChecker(
        samples_fetcher=mock_samples_fetcher,
        water_data_fetcher=mock_water_fetcher,
    )

    report = await checker.run_checks()
    assert report.status == HealthStatus.HEALTHY
    assert "metadata_catalog" in report.checks
    assert report.checks["metadata_catalog"].status == HealthStatus.HEALTHY
    assert "ogc_api" in report.checks
    assert report.checks["ogc_api"].status == HealthStatus.HEALTHY
    assert "samples_api" in report.checks
    assert report.checks["samples_api"].status == HealthStatus.HEALTHY
    assert "llm_config" in report.checks
    assert report.checks["llm_config"].status == HealthStatus.HEALTHY


@pytest.mark.asyncio
async def test_health_check_degraded_when_one_api_fails(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake-key-12345678")

    mock_ogc_resp = MagicMock(status_code=200)
    mock_water_fetcher = MagicMock()
    mock_water_fetcher.base_url = "https://api.waterdata.usgs.gov/ogcapi/v0"
    mock_water_fetcher._client = MagicMock()
    mock_water_fetcher._client.get = AsyncMock(return_value=mock_ogc_resp)

    # Samples API fails with network timeout exception
    mock_samples_fetcher = MagicMock()
    mock_samples_fetcher.base_url = "https://api.waterdata.usgs.gov/samples-data"
    mock_samples_fetcher._client = MagicMock()
    mock_samples_fetcher._client.get = AsyncMock(side_effect=TimeoutError("Connection timed out"))

    checker = HealthChecker(
        samples_fetcher=mock_samples_fetcher,
        water_data_fetcher=mock_water_fetcher,
    )

    report = await checker.run_checks()
    assert report.status == HealthStatus.DEGRADED
    assert report.checks["ogc_api"].status == HealthStatus.HEALTHY
    assert report.checks["samples_api"].status == HealthStatus.UNHEALTHY
    assert "timed out" in report.checks["samples_api"].message


@pytest.mark.asyncio
async def test_health_check_unhealthy_when_all_apis_fail(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake-key-12345678")

    mock_water_fetcher = MagicMock()
    mock_water_fetcher.base_url = "https://api.waterdata.usgs.gov/ogcapi/v0"
    mock_water_fetcher._client = MagicMock()
    mock_water_fetcher._client.get = AsyncMock(side_effect=Exception("OGC down"))

    mock_samples_fetcher = MagicMock()
    mock_samples_fetcher.base_url = "https://api.waterdata.usgs.gov/samples-data"
    mock_samples_fetcher._client = MagicMock()
    mock_samples_fetcher._client.get = AsyncMock(side_effect=Exception("Samples down"))

    checker = HealthChecker(
        samples_fetcher=mock_samples_fetcher,
        water_data_fetcher=mock_water_fetcher,
    )

    report = await checker.run_checks()
    assert report.status == HealthStatus.UNHEALTHY
    assert report.checks["ogc_api"].status == HealthStatus.UNHEALTHY
    assert report.checks["samples_api"].status == HealthStatus.UNHEALTHY


@pytest.mark.asyncio
async def test_agent_check_health_method(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake-key-12345678")

    mock_ogc_resp = MagicMock(status_code=200)
    mock_samples_resp = MagicMock(status_code=200)

    mock_water_fetcher = MagicMock()
    mock_water_fetcher.base_url = "https://api.waterdata.usgs.gov/ogcapi/v0"
    mock_water_fetcher._client = MagicMock()
    mock_water_fetcher._client.get = AsyncMock(return_value=mock_ogc_resp)

    mock_samples_fetcher = MagicMock()
    mock_samples_fetcher.base_url = "https://api.waterdata.usgs.gov/samples-data"
    mock_samples_fetcher._client = MagicMock()
    mock_samples_fetcher._client.get = AsyncMock(return_value=mock_samples_resp)

    agent = USGSWaterAgent(
        samples_fetcher=mock_samples_fetcher,
        water_data_fetcher=mock_water_fetcher,
    )

    report = await agent.check_health()
    assert report.status == HealthStatus.HEALTHY
    assert report.checks["metadata_catalog"].status == HealthStatus.HEALTHY
