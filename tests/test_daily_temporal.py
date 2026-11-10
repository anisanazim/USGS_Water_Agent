import pytest
from unittest.mock import AsyncMock, MagicMock

from usgs_water_data import PagedResult, USGSWaterDataFetcher
from water_agent.handlers.observation import handle_observation
from water_agent.query_planner import QueryPlan
from water_agent.results import RequestContext


@pytest.mark.asyncio
async def test_fetch_all_pages_truncation():
    """A fake 3-page response with max_records hit on page 2 sets truncated=True."""
    fetcher = USGSWaterDataFetcher()

    async def mock_request(url, params):
        if "page2" in url:
            return {
                "features": [{"id": f"feat_{i}"} for i in range(5, 10)],
                "links": [{"rel": "next", "href": "https://api.test/collections/daily/items?page3"}],
            }
        elif "page3" in url:
            return {
                "features": [{"id": f"feat_{i}"} for i in range(10, 15)],
                "links": [],
            }
        else:
            return {
                "features": [{"id": f"feat_{i}"} for i in range(5)],
                "links": [{"rel": "next", "href": "https://api.test/collections/daily/items?page2"}],
            }

    fetcher._request = mock_request

    # max_records = 10 reached on page 2 while next link to page 3 still exists
    res = await fetcher._fetch_all_pages("https://api.test/collections/daily/items", {}, max_records=10)
    assert len(res.features) == 10
    assert res.truncated is True

    # Not truncated when max_records > total available features (15)
    res_all = await fetcher._fetch_all_pages("https://api.test/collections/daily/items", {}, max_records=20)
    assert len(res_all.features) == 15
    assert res_all.truncated is False


@pytest.mark.asyncio
async def test_continuous_observation_bounded_branch():
    """Daily continuous query strips non-queryables, builds datetime interval, sets sortby='-time',
    and formats the bounded reply with first/last dates."""
    mock_process = AsyncMock()
    mock_process.__aenter__.return_value = mock_process
    mock_process.__aexit__.return_value = None

    mock_context = MagicMock()
    mock_context.begin_process.return_value = mock_process
    mock_context.reply = AsyncMock()

    mock_fetcher = AsyncMock(spec=USGSWaterDataFetcher)
    mock_fetcher.fetch_daily_values = AsyncMock(
        return_value=PagedResult(
            features=[
                {
                    "properties": {
                        "monitoring_location_id": "USGS-02087500",
                        "parameter_code": "00060",
                        "value": 150.0,
                        "unit_of_measure": "ft3/s",
                        "time": "2023-07-31",
                    }
                },
                {
                    "properties": {
                        "monitoring_location_id": "USGS-02087500",
                        "parameter_code": "00060",
                        "value": 120.0,
                        "unit_of_measure": "ft3/s",
                        "time": "2020-01-01",
                    }
                },
            ],
            source_urls=["https://api.test/daily?mocked"],
            truncated=False,
        )
    )

    plan = QueryPlan(
        intent="observation",
        modality="continuous",
        filters={
            "parameter_code": "00060",
            "monitoring_location_id": "USGS-02087500",
            "start_date": "2020-01-01",
            "end_date": "2023-07-31",
            "foo": "unknown_key",
        },
        semantic={"parameter": "streamflow"},
    )
    ctx = RequestContext(request_id="test-daily-bound")

    await handle_observation(
        context=mock_context,
        process=mock_process,
        plan=plan,
        ctx=ctx,
        water_data_fetcher=mock_fetcher,
    )

    # 1. Check fetch_daily_values parameters
    assert mock_fetcher.fetch_daily_values.called
    call_args = mock_fetcher.fetch_daily_values.call_args
    passed_filters = call_args[0][0]
    kwargs = call_args[1]

    # Non-queryables dropped
    assert "start_date" not in passed_filters
    assert "end_date" not in passed_filters
    assert "foo" not in passed_filters
    assert "time" not in passed_filters
    assert "datetime" not in passed_filters
    # Valid queryables kept
    assert passed_filters["parameter_code"] == "00060"
    assert passed_filters["monitoring_location_id"] == "USGS-02087500"

    # Datetime range and sortby
    assert kwargs.get("datetime") == "2020-01-01/2023-07-31"
    assert kwargs.get("sortby") == "-time"
    assert kwargs.get("limit") is None

    # 2. Check reply message
    assert mock_context.reply.called
    reply_text = mock_context.reply.call_args[0][0]
    assert "2020-01-01/2023-07-31" in reply_text
    assert "Found 2 daily value(s)" in reply_text
    assert "between 2020-01-01 and 2023-07-31" in reply_text
    assert "Most recent value: 150" in reply_text


@pytest.mark.asyncio
async def test_continuous_observation_truncated_reply_and_metadata():
    """When truncated=True, reply states results were capped and artifact metadata records it."""
    mock_process = AsyncMock()
    mock_process.__aenter__.return_value = mock_process
    mock_process.__aexit__.return_value = None

    mock_context = MagicMock()
    mock_context.begin_process.return_value = mock_process
    mock_context.reply = AsyncMock()

    mock_fetcher = AsyncMock(spec=USGSWaterDataFetcher)
    mock_fetcher.fetch_daily_values = AsyncMock(
        return_value=PagedResult(
            features=[
                {
                    "properties": {
                        "monitoring_location_id": "USGS-02087500",
                        "parameter_code": "00060",
                        "value": 150.0,
                        "unit_of_measure": "ft3/s",
                        "time": "2023-07-31",
                    }
                }
            ],
            source_urls=["https://api.test/daily?mocked"],
            truncated=True,
        )
    )

    plan = QueryPlan(
        intent="observation",
        modality="continuous",
        filters={
            "parameter_code": "00060",
            "monitoring_location_id": "USGS-02087500",
            "start_date": "2020-01-01",
        },
    )
    ctx = RequestContext(request_id="test-trunc")

    await handle_observation(
        context=mock_context,
        process=mock_process,
        plan=plan,
        ctx=ctx,
        water_data_fetcher=mock_fetcher,
    )

    # Reply must mention results were capped
    reply_text = mock_context.reply.call_args[0][0]
    assert "capped" in reply_text.lower()
    assert "narrower" in reply_text.lower()

    # Artifact metadata must contain truncated=True
    meta = mock_process.create_artifact.call_args[1]["metadata"]
    assert meta["truncated"] is True
