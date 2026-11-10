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


@pytest.mark.asyncio
async def test_latest_n_with_fixed_limit_not_marked_truncated():
    """limit=5 returning 5 features with a next link must have truncated=False in result and artifact."""
    fetcher = USGSWaterDataFetcher()
    recorded_params = []

    async def mock_request(url, params):
        recorded_params.append(params)
        return {
            "features": [
                {
                    "properties": {
                        "monitoring_location_id": "USGS-02087500",
                        "parameter_code": "00060",
                        "value": 100.0 + i,
                        "unit_of_measure": "ft3/s",
                        "time": f"2023-07-0{5 - i}",
                    }
                }
                for i in range(5)
            ],
            "links": [{"rel": "next", "href": "https://api.test/collections/daily/items?offset=5"}],
        }

    fetcher._request = mock_request

    # 1. Direct fetch_daily_values with limit=5
    res = await fetcher.fetch_daily_values(
        {"monitoring_location_id": "USGS-02087500", "parameter_code": "00060"},
        limit=5,
        sortby="-time",
    )
    assert len(res.features) == 5
    assert res.truncated is False
    assert recorded_params[0]["parameter_code"] == "00060"

    # 2. Through handle_observation latest-N path (no dates -> limit=5)
    mock_process = AsyncMock()
    mock_process.__aenter__.return_value = mock_process
    mock_process.__aexit__.return_value = None

    mock_context = MagicMock()
    mock_context.begin_process.return_value = mock_process
    mock_context.reply = AsyncMock()

    plan = QueryPlan(
        intent="observation",
        modality="continuous",
        filters={
            "parameter_code": "00060",
            "monitoring_location_id": "USGS-02087500",
        },
        semantic={"parameter": "streamflow"},
    )
    ctx = RequestContext(request_id="test-latest-n")

    await handle_observation(
        context=mock_context,
        process=mock_process,
        plan=plan,
        ctx=ctx,
        water_data_fetcher=fetcher,
    )

    reply_text = mock_context.reply.call_args[0][0]
    assert "Most recent matching USGS Daily" in reply_text
    assert "capped" not in reply_text.lower()
    meta = mock_process.create_artifact.call_args[1]["metadata"]
    assert meta["truncated"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filters,expected_dt",
    [
        (
            {
                "parameter_code": "00060",
                "monitoring_location_id": "USGS-02087500",
                "start_date": "2020-01-01",
            },
            "2020-01-01/..",
        ),
        (
            {
                "parameter_code": "00060",
                "monitoring_location_id": "USGS-02087500",
                "end_date": "2023-07-31",
            },
            "../2023-07-31",
        ),
        (
            {
                "parameter_code": "00060",
                "monitoring_location_id": "USGS-02087500",
                "datetime": "2020-01-01/2023-07-31",
                "start_date": "2019-01-01",
            },
            "2020-01-01/2023-07-31",
        ),
    ],
)
async def test_daily_path_open_ended_and_explicit_valid_datetime(filters, expected_dt):
    """Daily path builds 'start/..' and '../end', honours valid explicit datetime, and passes parameter_code in OGC params."""
    fetcher = USGSWaterDataFetcher()
    recorded_http_params = []

    async def mock_request(url, params):
        recorded_http_params.append(params)
        return {
            "features": [
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
            "links": [],
        }

    fetcher._request = mock_request

    mock_process = AsyncMock()
    mock_process.__aenter__.return_value = mock_process
    mock_process.__aexit__.return_value = None

    mock_context = MagicMock()
    mock_context.begin_process.return_value = mock_process
    mock_context.reply = AsyncMock()

    plan = QueryPlan(
        intent="observation",
        modality="continuous",
        filters=filters,
        semantic={"parameter": "streamflow"},
    )
    ctx = RequestContext(request_id="test-daily-intervals")

    await handle_observation(
        context=mock_context,
        process=mock_process,
        plan=plan,
        ctx=ctx,
        water_data_fetcher=fetcher,
    )

    assert len(recorded_http_params) == 1
    http_params = recorded_http_params[0]
    assert http_params["parameter_code"] == "00060"
    assert http_params["monitoring_location_id"] == "USGS-02087500"
    assert http_params["datetime"] == expected_dt
    assert http_params["sortby"] == "-time"
    assert "start_date" not in http_params
    assert "end_date" not in http_params


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filters,expected_bad_token",
    [
        (
            {
                "parameter_code": "00060",
                "monitoring_location_id": "USGS-02087500",
                "start_date": "January 2020",
            },
            "January 2020",
        ),
        (
            {
                "parameter_code": "00060",
                "monitoring_location_id": "USGS-02087500",
                "end_date": "2023-02-30",
            },
            "2023-02-30",
        ),
        (
            {
                "parameter_code": "00060",
                "monitoring_location_id": "USGS-02087500",
                "datetime": "bad/2023-07-31",
            },
            "bad",
        ),
        (
            {
                "parameter_code": "00060",
                "monitoring_location_id": "USGS-02087500",
                "datetime": "January 2020",
            },
            "January 2020",
        ),
    ],
)
async def test_daily_path_invalid_date_or_explicit_datetime_fails_closed(filters, expected_bad_token):
    """Invalid start/end dates or malformed explicit datetime in daily path reply with error and make zero fetches."""
    mock_process = AsyncMock()
    mock_process.__aenter__.return_value = mock_process
    mock_process.__aexit__.return_value = None

    mock_context = MagicMock()
    mock_context.begin_process.return_value = mock_process
    mock_context.reply = AsyncMock()

    mock_fetcher = AsyncMock(spec=USGSWaterDataFetcher)

    plan = QueryPlan(
        intent="observation",
        modality="continuous",
        filters=filters,
        semantic={"parameter": "streamflow"},
    )
    ctx = RequestContext(request_id="test-daily-invalid-date")

    await handle_observation(
        context=mock_context,
        process=mock_process,
        plan=plan,
        ctx=ctx,
        water_data_fetcher=mock_fetcher,
    )

    assert mock_context.reply.called
    reply_msg = mock_context.reply.call_args[0][0]
    assert expected_bad_token in reply_msg
    assert "YYYY-MM-DD" in reply_msg
    assert not mock_fetcher.fetch_daily_values.called
