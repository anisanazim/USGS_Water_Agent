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
    assert reply_msg == "USGS has been unresponsive for several minutes. Please try again later."

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
    assert "USGS has been unresponsive for several minutes. Please try again later." in reply_msg
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

    # GBIF-style complete reply check (15 <= inline_items_max -> all 15 inlined, no partial heading)
    assert "Retrieved **all 15** records" in reply_text
    assert "This is the complete result; nothing was truncated." in reply_text
    assert "Most recent 10 observations:" not in reply_text
    assert "Continuous sensor data is also available" not in reply_text
    assert "| Date | Time | Value | Unit | Parameter Code |" not in reply_text
    assert "2022-01-01" in reply_text and "2022-01-15" in reply_text

    # Readable family name check (no underscores like dissolved_oxygen)
    assert "Dissolved oxygen" in reply_text
    assert "dissolved_oxygen" not in reply_text

    # Artifact description check
    assert process.create_artifact.called
    artifact_desc = process.create_artifact.call_args.kwargs["description"]
    assert "Dissolved oxygen" in artifact_desc
    assert "dissolved_oxygen" not in artifact_desc


@pytest.mark.asyncio
async def test_observation_reply_flags_incomplete_result(mock_context):
    from water_agent.fetchers.samples_fetcher import DiscreteFetchResult
    from water_agent.handlers.observation import handle_observation
    from water_agent.query_planner import QueryPlan
    from water_agent.results import RequestContext

    fetch_res = DiscreteFetchResult(
        records=[
            {"activity_date": f"2022-01-{i:02d}", "result_value": 8.0 + i * 0.1, "pcode": "00300"}
            for i in range(1, 6)
        ],
        total_count=9,
        source_urls=["https://example.com/narrow"],
        warning="Some date ranges could not be retrieved",
    )

    mock_samples_fetcher = MagicMock()
    mock_samples_fetcher.fetch_results = AsyncMock(return_value=fetch_res)

    plan = QueryPlan(
        intent="observation",
        filters={"monitoring_location_id": "USGS-01646500", "parameter_code": "00300"},
    )
    ctx = RequestContext(request_id="test-obs-incomplete")
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
    reply_data = mock_context.reply.call_args.kwargs["data"]

    assert "Retrieved **5 of 9** records" in reply_text
    assert "The result is incomplete: Some date ranges could not be retrieved." in reply_text
    assert "This is the complete result" not in reply_text
    assert reply_data["result"]["complete"] is False
    assert reply_data["result"]["items_inlined"] is False


@pytest.mark.asyncio
async def test_circuit_breaker_open_reply_and_warmup(mock_context, caplog):
    from water_agent.circuit_breaker import CircuitBreakerOpenError
    from water_agent.fetchers.samples_fetcher import USGSSamplesFetcher

    agent = USGSWaterAgent()
    fetcher = USGSSamplesFetcher(client=MagicMock(is_closed=False))

    async def open_cb_call(func, *args, **kwargs):
        raise CircuitBreakerOpenError("usgs_samples_api", 12.3)

    fetcher.circuit_breaker.call = open_cb_call

    async def handler_with_open_cb(context, request, params):
        await fetcher.get_results_count({
            "monitoringLocationIdentifier": "USGS-0423205010",
            "usgsPCode": ["00400", "00403"],
        })

    mock_warmup = MagicMock()
    with patch.object(agent, "_handle_query_water_data", handler_with_open_cb):
        with patch("water_agent.keepwarm.request_warmup", mock_warmup):
            with caplog.at_level(logging.INFO):
                await agent.run(mock_context, "test request", "query_water_data", None)

    assert mock_context.reply.call_count == 1
    reply_msg = mock_context.reply.call_args[0][0]
    assert reply_msg == "USGS is temporarily unavailable. Please try again in about 13 s."

    summary_logs = [r.message for r in caplog.records if "outcome=" in r.message]
    assert len(summary_logs) >= 1
    assert "outcome=deadline" in summary_logs[-1]

    warn_logs = [r for r in caplog.records if r.levelno == logging.WARNING and "Circuit breaker" in r.message]
    assert len(warn_logs) == 1
    assert warn_logs[0].exc_info is None

    error_logs = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(error_logs) == 0

    assert mock_warmup.call_count == 1
    mock_warmup.assert_called_with("USGS-0423205010", ("00400", "00403"))


@pytest.mark.asyncio
async def test_circuit_breaker_open_zero_retry_after_omits_suffix(mock_context):
    from water_agent.circuit_breaker import CircuitBreakerOpenError

    agent = USGSWaterAgent()

    async def handler_open_zero(context, request, params):
        raise CircuitBreakerOpenError("usgs_samples_api", 0.0)

    with patch.object(agent, "_handle_query_water_data", handler_open_zero):
        await agent.run(mock_context, "test request", "query_water_data", None)

    reply_msg = mock_context.reply.call_args[0][0]
    assert reply_msg == "USGS is temporarily unavailable. Please try again shortly."
    assert "try again in about" not in reply_msg


@pytest.mark.asyncio
async def test_keepwarm_loop_continues_after_round_exception(caplog):
    from water_agent.keepwarm import start_keepwarm_loop, stop_keepwarm_loop

    calls = 0

    def flaky_warmup(station, pcodes):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient warmup failure")

    with patch("water_agent.keepwarm.request_warmup", side_effect=flaky_warmup):
        with patch("water_agent.config.settings.keepwarm_enabled", True):
            with patch("water_agent.config.settings.keepwarm_interval_seconds", 0.04):
                with patch("water_agent.config.settings.keepwarm_targets", ["USGS-0423205010:00400"]):
                    with caplog.at_level(logging.WARNING):
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

    # Round 1 raised, then >= 2 more rounds succeeded
    assert calls >= 3
    assert any(
        r.levelno == logging.WARNING and "transient warmup failure" in r.message
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_chained_timeout_samples_error_triggers_deadline_reply_and_warmup(mock_context, caplog):
    import httpx
    from agent import _is_timeout
    from water_agent.keepwarm import last_samples_target

    agent = USGSWaterAgent()

    # Build SamplesError -> SamplesError -> ReadTimeout chain
    try:
        try:
            raise httpx.ReadTimeout("read timed out")
        except httpx.ReadTimeout as e1:
            raise SamplesError("inner failure") from e1
    except SamplesError as e2:
        outer_err = SamplesError("outer failure")
        outer_err.__cause__ = e2

    assert _is_timeout(outer_err) is True

    # Also verify depth > 5 returns False and __context__ works
    deep: BaseException = httpx.ReadTimeout("too deep")
    for i in range(6):
        wrapper = SamplesError(f"wrap-{i}")
        wrapper.__cause__ = deep
        deep = wrapper
    assert _is_timeout(deep) is False

    ctx_err = SamplesError("implicit context")
    ctx_err.__context__ = httpx.ConnectTimeout("connect timed out")
    assert _is_timeout(ctx_err) is True

    async def chained_timeout_handler(context, request, params):
        last_samples_target.set(("USGS-01646500", ("00665",)))
        raise outer_err

    mock_warmup = MagicMock()
    with patch.object(agent, "_handle_query_water_data", chained_timeout_handler):
        with patch("water_agent.keepwarm.request_warmup", mock_warmup):
            with caplog.at_level(logging.INFO):
                await agent.run(mock_context, "test request", "query_water_data", None)

    assert mock_context.reply.call_count == 1
    reply_msg = mock_context.reply.call_args[0][0]
    assert "USGS has been unresponsive for several minutes. Please try again later." in reply_msg

    summary_logs = [r.message for r in caplog.records if "outcome=" in r.message]
    assert len(summary_logs) >= 1
    assert "outcome=deadline" in summary_logs[-1]

    assert mock_warmup.call_count == 1
    mock_warmup.assert_called_with("USGS-01646500", ("00665",))


@pytest.mark.asyncio
async def test_heartbeat_emits_increasing_logs_and_none_for_fast_blocks():
    import re
    from water_agent.heartbeat import heartbeat

    # Slow block (~0.18 s) with 0.05 s interval -> 3 heartbeat logs with increasing elapsed values
    slow_process = MagicMock()
    slow_logs: list[str] = []

    async def record_slow_log(msg: str, **kwargs):
        slow_logs.append(msg)

    slow_process.log = AsyncMock(side_effect=record_slow_log)

    with patch("water_agent.config.settings.heartbeat_interval_seconds", 0.05):
        async with heartbeat(slow_process):
            await asyncio.sleep(0.18)

    assert len(slow_logs) == 3, f"Expected 3 heartbeat logs, got {len(slow_logs)}: {slow_logs}"
    elapsed_vals = []
    for msg in slow_logs:
        assert msg.startswith("Waiting for USGS to respond (")
        assert msg.endswith(" s)…")
        m = re.search(r"\(([0-9.]+) s\)", msg)
        assert m is not None
        elapsed_vals.append(float(m.group(1)))
    assert elapsed_vals[0] < elapsed_vals[1] < elapsed_vals[2]

    # Fast block (0.03 s) with 0.05 s interval -> 0 heartbeats
    fast_process = MagicMock()
    fast_process.log = AsyncMock()

    with patch("water_agent.config.settings.heartbeat_interval_seconds", 0.05):
        async with heartbeat(fast_process):
            await asyncio.sleep(0.03)

    assert fast_process.log.call_count == 0


@pytest.mark.asyncio
async def test_heartbeat_message_switches_at_60s():
    from water_agent.heartbeat import heartbeat

    process = MagicMock()
    logs: list[str] = []

    async def record_log(msg: str, **kwargs):
        logs.append(msg)

    process.log = AsyncMock(side_effect=record_log)

    # Simulate start=100.0, beat 1 at 120.0 (20s), beat 2 at 165.0 (65s)
    monotonic_vals = [100.0, 120.0, 165.0]
    idx = 0

    def fake_monotonic():
        nonlocal idx
        val = monotonic_vals[min(idx, len(monotonic_vals) - 1)]
        idx += 1
        return val

    with patch("water_agent.heartbeat._monotonic", side_effect=fake_monotonic):
        with patch("water_agent.config.settings.request_deadline_seconds", 300.0):
            with patch("water_agent.config.settings.heartbeat_interval_seconds", 0.03):
                async with heartbeat(process):
                    await asyncio.sleep(0.08)

    assert len(logs) >= 2
    assert logs[0] == "Waiting for USGS to respond (20 s)…"
    assert logs[1] == (
        "Still waiting for USGS (65 s). After a quiet period USGS can take a few minutes to "
        "wake up; I'll keep waiting up to 300 s."
    )


@pytest.mark.asyncio
async def test_heartbeat_task_cleaned_up_on_normal_error_and_cancel():
    from water_agent.heartbeat import heartbeat

    process = MagicMock()
    process.log = AsyncMock()
    baseline_tasks = asyncio.all_tasks()

    # 1. Normal exit
    with patch("water_agent.config.settings.heartbeat_interval_seconds", 0.05):
        async with heartbeat(process):
            await asyncio.sleep(0.02)
    assert asyncio.all_tasks() == baseline_tasks

    # 2. Exception raised inside block
    with patch("water_agent.config.settings.heartbeat_interval_seconds", 0.05):
        with pytest.raises(ValueError, match="handler boom"):
            async with heartbeat(process):
                await asyncio.sleep(0.02)
                raise ValueError("handler boom")
    assert asyncio.all_tasks() == baseline_tasks

    # 3. Outer task cancelled
    started = asyncio.Event()

    async def cancellable_block():
        with patch("water_agent.config.settings.heartbeat_interval_seconds", 0.05):
            async with heartbeat(process):
                started.set()
                await asyncio.sleep(10.0)

    outer = asyncio.create_task(cancellable_block())
    await started.wait()
    outer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await outer
    assert asyncio.all_tasks() == baseline_tasks


@pytest.mark.asyncio
async def test_heartbeat_process_log_failure_stops_quietly(caplog):
    from water_agent.heartbeat import heartbeat

    process = MagicMock()
    process.log = AsyncMock(side_effect=RuntimeError("log channel closed"))

    with caplog.at_level(logging.DEBUG, logger="water_agent.heartbeat"):
        with patch("water_agent.config.settings.heartbeat_interval_seconds", 0.03):
            async with heartbeat(process):
                await asyncio.sleep(0.12)

    # Stopped after the first failed log call and did not raise into handler
    assert process.log.call_count == 1
    assert any(
        r.levelno == logging.DEBUG and "log channel closed" in r.message
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_handle_query_water_data_uses_heartbeat_not_reply():
    from contextlib import asynccontextmanager
    from water_agent.query_planner import QueryPlan

    agent = USGSWaterAgent()
    process = MagicMock()
    process_logs: list[str] = []

    async def capture_log(msg: str, **kwargs):
        process_logs.append(msg)

    process.log = AsyncMock(side_effect=capture_log)

    @asynccontextmanager
    async def fake_begin_process(desc):
        yield process

    ctx = MagicMock()
    ctx.begin_process = fake_begin_process
    ctx.reply = AsyncMock()

    fake_plan = QueryPlan(
        intent="observation",
        filters={"monitoring_location_id": "USGS-01646500", "parameter_code": "00300"},
    )
    fake_planner = MagicMock()
    fake_planner.plan = AsyncMock(return_value=fake_plan)
    agent = USGSWaterAgent(query_planner=fake_planner)

    async def slow_obs_handler(context, proc, plan, req_ctx, **kwargs):
        await asyncio.sleep(0.14)
        await context.reply("Final answer")

    with patch("agent.handle_observation", side_effect=slow_obs_handler):
        with patch("water_agent.config.settings.heartbeat_interval_seconds", 0.04):
            await agent._handle_query_water_data(ctx, "show dissolved oxygen", None)

    heartbeat_logs = [m for m in process_logs if "Waiting for USGS to respond" in m]
    assert len(heartbeat_logs) >= 2
    assert ctx.reply.call_count == 1
    assert ctx.reply.call_args[0][0] == "Final answer"
