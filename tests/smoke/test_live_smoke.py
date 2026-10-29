from __future__ import annotations

import pytest
import httpx
from ichatbio.agent_response import ResponseContext
from ichatbio.test_utils import InMemoryResponseChannel

from agent import USGSWaterAgent
from usgs_water_data import USGSWaterDataFetcher
from water_agent.fetchers.samples_fetcher import USGSSamplesFetcher


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_live_ogc_collections():
    """Live Smoke 1: Verify upstream USGS OGC API is online and returns valid collections schema."""
    url = "https://api.waterdata.usgs.gov/ogcapi/v0/collections"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url)
        assert resp.status_code == 200, f"OGC Collections API failed with status {resp.status_code}"
        payload = resp.json()
        assert "collections" in payload, "Missing 'collections' field in USGS OGC response"
        assert len(payload["collections"]) > 0


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_live_samples_count_api():
    """Live Smoke 2: Verify upstream USGS Samples API /results/count is responding and returns counts."""
    fetcher = USGSSamplesFetcher(timeout=15.0)
    try:
        # Query total phosphorus (00665) count at USGS-0423205010
        count = await fetcher.get_results_count({
            "monitoringLocationIdentifier": "USGS-0423205010",
            "usgsPCode": "00665",
        })
        assert isinstance(count, int)
        assert count > 0, "Expected non-zero record count for phosphorus at USGS-0423205010"
    finally:
        await fetcher.aclose()


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_live_samples_narrow_api():
    """Live Smoke 3: Verify upstream USGS Samples API /results/narrow returns valid records."""
    fetcher = USGSSamplesFetcher(timeout=20.0)
    try:
        params = {
            "monitoringLocationIdentifier": "USGS-0423205010",
            "usgsPCode": "00665",
        }
        fetch_res = await fetcher.fetch_results(params, profile="narrow")
        assert len(fetch_res.records) > 0, "Expected at least 1 record from /results/narrow for USGS-0423205010"
        records_dto = fetch_res.to_observation_records()
        assert len(records_dto) > 0
        assert records_dto[0].location_id == "USGS-0423205010"
        assert records_dto[0].pcode == "00665"
    finally:
        await fetcher.aclose()


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_live_continuous_streamflow():
    """Live Smoke 4: Verify upstream OGC Daily Continuous time-series returns readings for reference site."""
    fetcher = USGSWaterDataFetcher(timeout=15.0)
    try:
        # USGS-02087500 (Neuse River near Clayton, NC)
        filters = {
            "monitoring_location_id": "USGS-02087500",
            "parameter_code": "00060",
            "statistic_id": "00003",
        }
        paged_res = await fetcher.fetch_daily_values(filters=filters)
        readings = paged_res.to_daily_readings()
        assert len(readings) > 0, "Expected streamflow daily readings at USGS-02087500"
        assert readings[0].parameter_code == "00060"
    finally:
        await fetcher.aclose()


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_live_agent_e2e_query():
    """Live Smoke 5: Canonical live end-to-end agent query smoke test."""
    messages = []
    channel = InMemoryResponseChannel(messages)
    context = ResponseContext(channel)
    agent = USGSWaterAgent()

    try:
        query = "How many phosphorus records are at USGS-0423205010?"
        await agent.run(context, query, "query_water_data", None, None)

        direct_replies = [m for m in messages if type(m).__name__ == "DirectResponse"]
        assert len(direct_replies) > 0, "Agent failed to emit DirectResponse in live run"
        reply_text = direct_replies[0].text
        assert "12,337" in reply_text or "phosphorus" in reply_text.lower()
    finally:
        await agent.aclose()
