import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from agent import USGSWaterAgent
from water_agent.config import settings
from usgs_water_data import USGSServiceError, USGSValidationError, USGSNotFoundError
from water_agent.fetchers.samples_fetcher import SamplesError


@pytest.fixture(autouse=True)
def isolate_keepwarm():
    from water_agent.keepwarm import stop_keepwarm_loop, _WARMING

    stop_keepwarm_loop()
    _WARMING.clear()
    with patch("water_agent.config.settings.keepwarm_enabled", False):
        yield
    stop_keepwarm_loop()
    _WARMING.clear()


@pytest.fixture
def mock_context():
    ctx = MagicMock()
    ctx.reply = AsyncMock()
    return ctx


@pytest.mark.asyncio
async def test_deadline_timeout_slow_usgs_reply_and_log(mock_context, caplog):
    agent = USGSWaterAgent()
    from water_agent.keepwarm import last_samples_target

    async def slow_handler(context, request, params):
        last_samples_target.set(("USGS-01646500", ("00300",)))
        await asyncio.sleep(0.5)

    mock_warmup = MagicMock()
    with patch.object(agent, "_handle_query_water_data", slow_handler):
        with patch("water_agent.config.settings.request_deadline_seconds", 0.05):
            with patch("water_agent.keepwarm.request_warmup", mock_warmup):
                with caplog.at_level(logging.INFO):
                    await agent.run(mock_context, "test request", "query_water_data", None)

    # Exactly one reply sent
    assert mock_context.reply.call_count == 1
    reply_msg = mock_context.reply.call_args[0][0]
    assert "USGS is waking up or responding slowly right now" in reply_msg
    assert "Please ask again in a minute or two." in reply_msg

    # Summary log line has outcome=deadline
    summary_logs = [r.message for r in caplog.records if "outcome=" in r.message]
    assert len(summary_logs) >= 1
    assert "outcome=deadline" in summary_logs[-1]
    assert "elapsed_s=" in summary_logs[-1]

    # Warmup trigger called with the target recorded by fetcher
    assert mock_warmup.call_count == 1
    mock_warmup.assert_called_with("USGS-01646500", ("00300",))


@pytest.mark.asyncio
async def test_cancelled_error_propagates(mock_context):
    agent = USGSWaterAgent()

    async def cancelling_handler(context, request, params):
        raise asyncio.CancelledError()

    with patch.object(agent, "_handle_query_water_data", cancelling_handler):
        with pytest.raises(asyncio.CancelledError):
            await agent.run(mock_context, "test request", "query_water_data", None)


@pytest.mark.asyncio
async def test_error_replies_never_end_with_colon_space(mock_context):
    agent = USGSWaterAgent()

    async def empty_error_handler(context, request, params):
        raise USGSServiceError("")

    with patch.object(agent, "_handle_query_water_data", empty_error_handler):
        await agent.run(mock_context, "test request", "query_water_data", None)
    assert mock_context.reply.call_count == 1
    reply_msg = mock_context.reply.call_args[0][0]
    assert not reply_msg.strip().endswith(":")
    assert not reply_msg.endswith(": ")


@pytest.mark.asyncio
async def test_successful_request_summary_log(mock_context, caplog):
    agent = USGSWaterAgent()

    async def ok_handler(context, request, params):
        await context.reply("Data found!")

    with patch.object(agent, "_handle_query_water_data", ok_handler):
        with caplog.at_level(logging.INFO):
            await agent.run(mock_context, "test request", "query_water_data", None)

    assert mock_context.reply.call_count == 1
    summary_logs = [r.message for r in caplog.records if "outcome=" in r.message]
    assert len(summary_logs) >= 1
    assert "outcome=ok" in summary_logs[-1]


@pytest.mark.asyncio
async def test_read_timeout_does_not_retry():
    from water_agent.fetchers.samples_fetcher import USGSSamplesFetcher
    import httpx

    mock_client = MagicMock()
    mock_client.get = AsyncMock(side_effect=httpx.ReadTimeout("read timeout"))
    mock_client.is_closed = False

    fetcher = USGSSamplesFetcher(client=mock_client)

    with patch("water_agent.config.settings.max_attempts", 3):
        with pytest.raises(SamplesError) as exc_info:
            await fetcher._safe_get("https://example.com/test")

    assert mock_client.get.call_count == 1
    assert "ReadTimeout" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, httpx.ReadTimeout)


@pytest.mark.asyncio
async def test_503_retries_up_to_max_attempts():
    from water_agent.fetchers.samples_fetcher import USGSSamplesFetcher

    resp = MagicMock()
    resp.status_code = 503
    resp.text = "Service Unavailable"

    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=resp)
    mock_client.is_closed = False

    fetcher = USGSSamplesFetcher(client=mock_client)

    with patch("water_agent.config.settings.max_attempts", 2):
        with patch("asyncio.sleep", AsyncMock()):
            with pytest.raises(SamplesError) as exc_info:
                await fetcher._safe_get("https://example.com/test")

    assert mock_client.get.call_count == 2
    assert "503" in str(exc_info.value)


def test_samples_fetcher_reads_settings():
    from water_agent.fetchers.samples_fetcher import USGSSamplesFetcher

    with patch("water_agent.config.settings.samples_timeout_seconds", 37.5):
        fetcher = USGSSamplesFetcher()
        assert fetcher.timeout == 37.5


@pytest.mark.asyncio
async def test_timeout_samples_error_triggers_deadline_reply(mock_context, caplog):
    agent = USGSWaterAgent()
    import httpx

    cause = httpx.ReadTimeout("read timeout")
    err = SamplesError("USGS did not respond in time (ReadTimeout)")
    err.__cause__ = cause

    async def timeout_handler(context, request, params):
        raise err

    mock_warmup = MagicMock()
    with patch.object(agent, "_handle_query_water_data", timeout_handler):
        with patch("agent._trigger_warmup", mock_warmup):
            with caplog.at_level(logging.INFO):
                await agent.run(mock_context, "test request", "query_water_data", None)

    assert mock_context.reply.call_count == 1
    reply_msg = mock_context.reply.call_args[0][0]
    assert "USGS is waking up or responding slowly right now" in reply_msg
    assert mock_warmup.call_count == 1

    summary_logs = [r.message for r in caplog.records if "outcome=" in r.message]
    assert len(summary_logs) >= 1
    assert "outcome=deadline" in summary_logs[-1]


@pytest.mark.asyncio
async def test_contextvar_set_by_fetch_and_readable_in_agent_run(mock_context):
    from water_agent.fetchers.samples_fetcher import USGSSamplesFetcher
    import httpx

    agent = USGSWaterAgent()

    mock_client = MagicMock()
    mock_client.get = AsyncMock(side_effect=httpx.ReadTimeout("timed out"))
    mock_client.is_closed = False

    fetcher = USGSSamplesFetcher(client=mock_client)

    async def handler_with_fetch(context, request, params):
        await fetcher.get_results_count({
            "monitoringLocationIdentifier": "USGS-0423205010",
            "usgsPCode": ["00400", "00403"],
        })

    mock_warmup = MagicMock()
    with patch.object(agent, "_handle_query_water_data", handler_with_fetch):
        with patch("water_agent.keepwarm.request_warmup", mock_warmup):
            await agent.run(mock_context, "test request", "query_water_data", None)

    assert mock_warmup.call_count == 1
    station_arg, pcodes_arg = mock_warmup.call_args[0]
    assert station_arg == "USGS-0423205010"
    assert set(pcodes_arg) == {"00400", "00403"}


@pytest.mark.asyncio
async def test_request_warmup_single_flight_and_removal():
    from water_agent.keepwarm import request_warmup, _WARMING

    mock_resp = MagicMock()
    mock_resp.status_code = 200

    async def slow_get(*args, **kwargs):
        await asyncio.sleep(0.1)
        return mock_resp

    with patch("httpx.AsyncClient.get", side_effect=slow_get):
        _WARMING.clear()
        request_warmup("USGS-0423205010", ("00400",))
        # Second call while first is in-flight
        request_warmup("USGS-0423205010", ("00400",))

        assert len(_WARMING) == 1
        task = _WARMING["USGS-0423205010"]

        await task
        # After done, entry is removed from _WARMING
        assert "USGS-0423205010" not in _WARMING


@pytest.mark.asyncio
async def test_request_warmup_cap():
    from water_agent.keepwarm import request_warmup, _WARMING

    mock_resp = MagicMock()
    mock_resp.status_code = 200

    async def slow_get(*args, **kwargs):
        await asyncio.sleep(0.2)
        return mock_resp

    with patch("httpx.AsyncClient.get", side_effect=slow_get):
        with patch("water_agent.config.settings.max_warmups", 2):
            _WARMING.clear()
            request_warmup("USGS-0001")
            request_warmup("USGS-0002")
            # Third call should hit the cap of 2
            request_warmup("USGS-0003")

            assert len(_WARMING) == 2
            assert "USGS-0003" not in _WARMING

            await asyncio.gather(*list(_WARMING.values()))
            assert len(_WARMING) == 0


@pytest.mark.asyncio
async def test_request_warmup_random_date_and_swallow_exceptions():
    from water_agent.keepwarm import request_warmup, _WARMING
    import httpx

    captured_params = []

    async def err_get(self, url, params=None, **kwargs):
        captured_params.append(params)
        raise httpx.ConnectError("network down")

    with patch.object(httpx.AsyncClient, "get", err_get):
        _WARMING.clear()
        dates = []
        for _ in range(10):
            request_warmup("USGS-0423205010", ("00400",))
            task = _WARMING.get("USGS-0423205010")
            assert task is not None
            # Does not raise even though client.get raised
            await task
            assert "USGS-0423205010" not in _WARMING

        assert len(captured_params) == 10
        for p in captured_params:
            start_date = p["activityStartDateLower"]
            dates.append(start_date)
            year = int(start_date.split("-")[0])
            assert 1990 <= year <= 2020

        # Pings use different random start dates
        assert len(set(dates)) > 1


@pytest.mark.asyncio
async def test_keepwarm_loop_runs_rounds_and_cancels_cleanly():
    from water_agent.keepwarm import start_keepwarm_loop, stop_keepwarm_loop, _KEEPWARM_TASK

    mock_warmup = MagicMock()
    with patch("water_agent.keepwarm.request_warmup", mock_warmup):
        with patch("water_agent.config.settings.keepwarm_enabled", True):
            with patch("water_agent.config.settings.keepwarm_interval_seconds", 0.05):
                with patch("water_agent.config.settings.keepwarm_targets", ["USGS-0423205010:00400,00403"]):
                    stop_keepwarm_loop()
                    task = start_keepwarm_loop()
                    assert task is not None
                    await asyncio.sleep(0.18)
                    stop_keepwarm_loop()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    assert task.cancelled() or task.done()

    # In 0.18s with 0.05s interval, should have run at least 2 rounds (round 0 at t=0, round 1 at t=0.05, round 2 at t=0.10, etc.)
    assert mock_warmup.call_count >= 2
    mock_warmup.assert_called_with("USGS-0423205010", ("00400", "00403"))


def test_keepwarm_disabled_runs_nothing():
    from water_agent.keepwarm import start_keepwarm_loop, stop_keepwarm_loop

    with patch("water_agent.config.settings.keepwarm_enabled", False):
        stop_keepwarm_loop()
        task = start_keepwarm_loop()
        assert task is None


def test_keepwarm_targets_parsing():
    from water_agent.keepwarm import parse_target_entry
    from water_agent.config import WaterAgentSettings

    # Helper parsing
    station, pcodes = parse_target_entry("USGS-0423205010:00400,00403")
    assert station == "USGS-0423205010"
    assert pcodes == ("00400", "00403")

    station2, pcodes2 = parse_target_entry("USGS-01646500")
    assert station2 == "USGS-01646500"
    assert pcodes2 == ()

    # Settings env parsing with semicolon-separated entries
    s = WaterAgentSettings(KEEPWARM_TARGETS="STA-1:00400;STA-2:00060,00065")
    assert s.keepwarm_targets == ["STA-1:00400", "STA-2:00060,00065"]


def test_format_family_name_helper():
    from water_agent.handlers.common import format_family_name

    assert format_family_name("dissolved_oxygen") == "dissolved oxygen"
    assert format_family_name("organic_carbon") == "organic carbon"
    assert format_family_name("turbidity_tss") == "turbidity/TSS"
    assert format_family_name("chlorophyll_a") == "chlorophyll a"
    assert format_family_name("ph") == "pH"
    assert format_family_name("calcium") == "calcium"
    assert format_family_name("total_nitrogen") == "total nitrogen"
    assert format_family_name("custom_param_name") == "custom param name"
    assert format_family_name(None) == ""


@pytest.mark.asyncio
async def test_observation_heading_and_family_display_name(mock_context):
    from water_agent.handlers.observation import handle_observation
    from water_agent.query_planner import QueryPlan
    from water_agent.results import RequestContext

    mock_samples_res = MagicMock()
    mock_samples_res.source_urls = ["https://example.com/narrow"]
    mock_samples_res.total_count = 15
    mock_samples_res.handle = None
    mock_samples_res.records = [
        {"activity_date": f"2022-01-{i:02d}", "result_value": 8.0 + i * 0.1, "pcode": "00300"}
        for i in range(1, 16)
    ]

    mock_samples_fetcher = MagicMock()
    mock_samples_fetcher.fetch_results = AsyncMock(return_value=mock_samples_res)

    plan = QueryPlan(
        intent="observation",
        filters={"monitoring_location_id": "USGS-01646500", "parameter_code": "dissolved_oxygen"},
    )
    ctx = RequestContext(request_id="test-obs")
    process = MagicMock()
    process.log = AsyncMock()
    process.create_artifact = AsyncMock()

    await handle_observation(
        mock_context,
        process,
        plan,
        ctx,
        samples_fetcher=mock_samples_fetcher,
    )

    assert mock_context.reply.called
    reply_text = mock_context.reply.call_args[0][0]

    # Heading check
    assert "Most recent 10 observations:" in reply_text
    assert "First 10 observations:" not in reply_text

    # Readable family name check (no underscores like dissolved_oxygen)
    assert "Dissolved oxygen" in reply_text
    assert "dissolved_oxygen" not in reply_text

    # Artifact description check
    assert process.create_artifact.called
    artifact_desc = process.create_artifact.call_args.kwargs["description"]
    assert "Dissolved oxygen" in artifact_desc
    assert "dissolved_oxygen" not in artifact_desc
