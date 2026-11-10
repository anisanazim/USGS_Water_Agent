from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from usgs_water_data import PagedResult, USGSWaterDataFetcher
from water_agent.fetchers.samples_fetcher import DiscreteFetchResult, USGSSamplesFetcher
from water_agent.handlers.count import handle_parameter_count
from water_agent.handlers.observation import handle_observation
from water_agent.query_planner import QueryPlan
from water_agent.results import RequestContext
from water_agent.temporal import TemporalRequest


def _make_ctx_and_process():
    mock_process = AsyncMock()
    mock_process.__aenter__.return_value = mock_process
    mock_process.__aexit__.return_value = None

    mock_context = MagicMock()
    mock_context.begin_process.return_value = mock_process
    mock_context.reply = AsyncMock()
    return mock_context, mock_process


@pytest.mark.asyncio
async def test_observation_discrete_query_a_do_range():
    """Query a: DO between Jan 2020 and Jul 2023 -> 2020-01-01..2023-07-31, reply contains Interpreted."""
    mock_context, mock_process = _make_ctx_and_process()
    mock_fetcher = AsyncMock(spec=USGSSamplesFetcher)
    mock_fetcher.fetch_results = AsyncMock(
        return_value=DiscreteFetchResult(
            records=[
                {
                    "activity_date": "2022-06-15",
                    "activity_time": "10:00:00",
                    "result_value": 8.4,
                    "unit": "mg/l",
                    "pcode": "00300",
                    "characteristic_name": "Dissolved oxygen",
                }
            ],
            total_count=1,
            source_urls=["https://api.waterdata.usgs.gov/samples-data/results/narrow?mock"],
        )
    )

    plan = QueryPlan(
        intent="observation",
        modality="discrete",
        filters={
            "parameter_code": "00300",
            "monitoring_location_id": "USGS-02087500",
        },
        temporal=TemporalRequest(
            phrase="between January 2020 and July 2023",
            start="2020-01",
            end="2023-07",
        ),
    )
    ctx = RequestContext(request_id="test-obs-do")

    await handle_observation(
        context=mock_context,
        process=mock_process,
        plan=plan,
        ctx=ctx,
        samples_fetcher=mock_fetcher,
    )

    call_params = mock_fetcher.fetch_results.call_args[1]["params"]
    assert call_params["activityStartDateLower"] == "2020-01-01"
    assert call_params["activityStartDateUpper"] == "2023-07-31"

    reply_text = mock_context.reply.call_args[0][0]
    assert "Interpreted 'between January 2020 and July 2023' as 2020-01-01 to 2023-07-31." in reply_text

    meta = mock_process.create_artifact.call_args[1]["metadata"]
    assert meta["date_lower"] == "2020-01-01"
    assert meta["date_upper"] == "2023-07-31"
    assert meta["temporal"] == {
        "phrase": "between January 2020 and July 2023",
        "start": "2020-01-01",
        "end": "2023-07-31",
        "months": [],
        "label": "2020-01-01 to 2023-07-31",
    }


@pytest.mark.asyncio
async def test_count_winter_2022_applies_bounds():
    """'winter 2022' count sends 2021-12-01 and 2022-02-28 to /results/count and sorted months in metadata."""
    mock_context, mock_process = _make_ctx_and_process()
    mock_fetcher = AsyncMock(spec=USGSSamplesFetcher)
    mock_fetcher.base_url = "https://api.waterdata.usgs.gov/samples-data"
    mock_fetcher._build_source_url = lambda url, p: f"{url}?mock"
    mock_fetcher._normalize_params = lambda p: p
    mock_fetcher.get_results_count = AsyncMock(return_value=42)

    plan = QueryPlan(
        intent="station_parameter_count",
        filters={
            "monitoring_location_id": "USGS-02087500",
            "parameter_code": "carbon",
        },
        temporal=TemporalRequest(phrase="winter 2022", season="winter", year=2022),
    )
    ctx = RequestContext(request_id="test-count-w22")

    await handle_parameter_count(
        context=mock_context,
        process=mock_process,
        plan=plan,
        ctx=ctx,
        request="How many carbon records at USGS-02087500 in winter 2022?",
        samples_fetcher=mock_fetcher,
    )

    call_params = mock_fetcher.get_results_count.call_args[0][0]
    assert call_params["activityStartDateLower"] == "2021-12-01"
    assert call_params["activityStartDateUpper"] == "2022-02-28"

    reply_text = mock_context.reply.call_args[0][0]
    assert "Interpreted 'winter 2022' as winter 2022 (2021-12-01 to 2022-02-28)." in reply_text

    meta = mock_process.create_artifact.call_args[1]["metadata"]
    assert meta["date_lower"] == "2021-12-01"
    assert meta["date_upper"] == "2022-02-28"
    assert meta["temporal"]["months"] == [1, 2, 12]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "temp_req,expected_label",
    [
        (TemporalRequest(phrase="during the month of March", month=3), "March"),
        (TemporalRequest(phrase="during the winter", season="winter"), "winter"),
    ],
)
async def test_unbounded_month_or_season_fails_closed_no_fetch(temp_req, expected_label):
    """'March' (no year) and 'winter' (no year) fail closed with P2 notice and zero fetches in count and observation."""
    # 1. Count handler
    mc_c, mp_c = _make_ctx_and_process()
    mf_c = AsyncMock(spec=USGSSamplesFetcher)
    plan_c = QueryPlan(
        intent="station_parameter_count",
        filters={"monitoring_location_id": "USGS-02087500", "parameter_code": "ph"},
        temporal=temp_req,
    )
    await handle_parameter_count(
        context=mc_c,
        process=mp_c,
        plan=plan_c,
        ctx=RequestContext(request_id="test-mask-count"),
        request=f"How many pH records {temp_req.phrase} at USGS-02087500?",
        samples_fetcher=mf_c,
    )
    reply_c = mc_c.reply.call_args[0][0]
    assert f"Filtering by {expected_label} across multiple years isn't supported yet." in reply_c
    assert not mf_c.get_results_count.called

    # 2. Discrete observation handler
    mc_d, mp_d = _make_ctx_and_process()
    mf_d = AsyncMock(spec=USGSSamplesFetcher)
    plan_d = QueryPlan(
        intent="observation",
        modality="discrete",
        filters={"monitoring_location_id": "USGS-02087500", "parameter_code": "00400"},
        temporal=temp_req,
    )
    await handle_observation(
        context=mc_d,
        process=mp_d,
        plan=plan_d,
        ctx=RequestContext(request_id="test-mask-obs-disc"),
        samples_fetcher=mf_d,
    )
    reply_d = mc_d.reply.call_args[0][0]
    assert f"Filtering by {expected_label} across multiple years isn't supported yet." in reply_d
    assert not mf_d.fetch_results.called

    # 3. Continuous daily observation handler
    mc_o, mp_o = _make_ctx_and_process()
    wf_o = AsyncMock(spec=USGSWaterDataFetcher)
    plan_o = QueryPlan(
        intent="observation",
        modality="continuous",
        filters={"monitoring_location_id": "USGS-02087500", "parameter_code": "00060"},
        temporal=temp_req,
    )
    await handle_observation(
        context=mc_o,
        process=mp_o,
        plan=plan_o,
        ctx=RequestContext(request_id="test-mask-obs-daily"),
        water_data_fetcher=wf_o,
    )
    reply_o = mc_o.reply.call_args[0][0]
    assert f"Filtering by {expected_label} across multiple years isn't supported yet." in reply_o
    assert not wf_o.fetch_daily_values.called


@pytest.mark.asyncio
async def test_observation_daily_past_5_years_with_patched_today(monkeypatch):
    """Daily path: 'past 5 years' with today patched -> datetime '2021-11-10/2026-11-10'."""
    monkeypatch.setattr("water_agent.temporal.current_date", lambda: date(2026, 11, 10))

    mock_context, mock_process = _make_ctx_and_process()
    mock_fetcher = AsyncMock(spec=USGSWaterDataFetcher)
    mock_fetcher.fetch_daily_values = AsyncMock(
        return_value=PagedResult(
            features=[
                {
                    "properties": {
                        "monitoring_location_id": "USGS-02087500",
                        "parameter_code": "00060",
                        "value": 210.0,
                        "unit_of_measure": "ft3/s",
                        "time": "2026-11-09",
                    }
                }
            ],
            source_urls=["https://api.test/daily?mock"],
            truncated=False,
        )
    )

    plan = QueryPlan(
        intent="observation",
        modality="continuous",
        filters={
            "parameter_code": "00060",
            "monitoring_location_id": "USGS-02087500",
        },
        temporal=TemporalRequest(
            phrase="past 5 years",
            relative="past",
            unit="year",
            n=5,
        ),
        semantic={"parameter": "streamflow"},
    )
    ctx = RequestContext(request_id="test-daily-past5")

    await handle_observation(
        context=mock_context,
        process=mock_process,
        plan=plan,
        ctx=ctx,
        water_data_fetcher=mock_fetcher,
    )

    kwargs = mock_fetcher.fetch_daily_values.call_args[1]
    assert kwargs["datetime"] == "2021-11-10/2026-11-10"
    assert kwargs["sortby"] == "-time"

    reply_text = mock_context.reply.call_args[0][0]
    assert "Interpreted 'past 5 years' as past 5 years (2021-11-10 to 2026-11-10)." in reply_text

    meta = mock_process.create_artifact.call_args[1]["metadata"]
    assert meta["date_lower"] == "2021-11-10"
    assert meta["date_upper"] == "2026-11-10"
    assert meta["temporal"]["start"] == "2021-11-10"
    assert meta["temporal"]["end"] == "2026-11-10"


@pytest.mark.asyncio
async def test_legacy_dates_and_temporal_conflict_resolution():
    """Legacy start_date/end_date works; when both temporal and conflicting legacy exist, temporal wins."""
    # 1. Legacy start_date/end_date only on discrete path
    mc1, mp1 = _make_ctx_and_process()
    mf1 = AsyncMock(spec=USGSSamplesFetcher)
    mf1.fetch_results = AsyncMock(
        return_value=DiscreteFetchResult(
            records=[
                {
                    "activity_date": "2021-05-01",
                    "result_value": 10.0,
                    "unit": "mg/l",
                    "pcode": "00915",
                }
            ],
            total_count=1,
            source_urls=["https://api.test/narrow"],
        )
    )
    p1 = QueryPlan(
        intent="observation",
        modality="discrete",
        filters={
            "parameter_code": "00915",
            "monitoring_location_id": "USGS-02087500",
            "start_date": "2020-01-01",
            "end_date": "2023-07-31",
        },
    )
    await handle_observation(
        context=mc1,
        process=mp1,
        plan=p1,
        ctx=RequestContext(request_id="test-leg"),
        samples_fetcher=mf1,
    )
    assert mf1.fetch_results.call_args[1]["params"]["activityStartDateLower"] == "2020-01-01"
    assert mf1.fetch_results.call_args[1]["params"]["activityStartDateUpper"] == "2023-07-31"
    assert "Interpreted date filter as 2020-01-01 to 2023-07-31." in mc1.reply.call_args[0][0]

    # 2. Temporal + conflicting legacy filters -> temporal wins
    mc2, mp2 = _make_ctx_and_process()
    mf2 = AsyncMock(spec=USGSSamplesFetcher)
    mf2.fetch_results = AsyncMock(
        return_value=DiscreteFetchResult(records=[], total_count=0, source_urls=[])
    )
    p2 = QueryPlan(
        intent="observation",
        modality="discrete",
        filters={
            "parameter_code": "00915",
            "monitoring_location_id": "USGS-02087500",
            "start_date": "2018-01-01",
            "end_date": "2019-12-31",
        },
        temporal=TemporalRequest(phrase="in 2021", start="2021", end="2021"),
    )
    await handle_observation(
        context=mc2,
        process=mp2,
        plan=p2,
        ctx=RequestContext(request_id="test-conflict"),
        samples_fetcher=mf2,
    )
    params2 = mf2.fetch_results.call_args[1]["params"]
    assert params2["activityStartDateLower"] == "2021-01-01"
    assert params2["activityStartDateUpper"] == "2021-12-31"
    assert "Interpreted 'in 2021' as 2021-01-01 to 2021-12-31." in mc2.reply.call_args[0][0]


@pytest.mark.asyncio
async def test_temporal_error_replies_and_makes_zero_fetches(caplog):
    """When plan.temporal_error is set, handlers log detail at INFO, reply cleanly, and make zero upstream calls."""
    import logging

    # Count handler
    mc_c, mp_c = _make_ctx_and_process()
    mf_c = AsyncMock(spec=USGSSamplesFetcher)
    plan_c = QueryPlan(
        intent="station_parameter_count",
        filters={"monitoring_location_id": "USGS-02087500", "parameter_code": "phosphorus"},
        temporal_error="Month 13 is out of range; expected 1-12.",
    )
    with caplog.at_level(logging.INFO, logger="water_agent.temporal"):
        await handle_parameter_count(
            context=mc_c,
            process=mp_c,
            plan=plan_c,
            ctx=RequestContext(request_id="test-terr-count"),
            request="How many phosphorus records in month 13?",
            samples_fetcher=mf_c,
        )
    reply_c = mc_c.reply.call_args[0][0]
    assert reply_c == (
        "I couldn't understand the date period in your question. "
        "Try e.g. '2020-01-01 to 2023-07-31', 'March 2021', or 'winter 2022'."
    )
    assert "Month 13" not in reply_c
    assert any("Month 13" in r.message for r in caplog.records)
    assert not mf_c.get_results_count.called

    # Observation handler (continuous)
    mc_o, mp_o = _make_ctx_and_process()
    wf_o = AsyncMock(spec=USGSWaterDataFetcher)
    plan_o = QueryPlan(
        intent="observation",
        modality="continuous",
        filters={"monitoring_location_id": "USGS-02087500", "parameter_code": "00060"},
        temporal_error="Unrecognized season 'monsoon'.",
    )
    await handle_observation(
        context=mc_o,
        process=mp_o,
        plan=plan_o,
        ctx=RequestContext(request_id="test-terr-obs"),
        water_data_fetcher=wf_o,
    )
    reply_o = mc_o.reply.call_args[0][0]
    assert reply_o == (
        "I couldn't understand the date period in your question. "
        "Try e.g. '2020-01-01 to 2023-07-31', 'March 2021', or 'winter 2022'."
    )
    assert "monsoon" not in reply_o
    assert not wf_o.fetch_daily_values.called


@pytest.mark.asyncio
async def test_pydantic_validation_error_in_temporal_never_leaks_to_reply():
    """Bad temporal payload like {'month': [3]} must not leak pydantic ValidationError text into user reply."""
    from water_agent.query_planner import QueryPlanner

    plan = QueryPlanner._parse_plan(
        '{"intent": "observation", "modality": "continuous", '
        '"filters": {"parameter_code": "00060", "monitoring_location_id": "USGS-02087500"}, '
        '"temporal": {"month": [3]}}'
    )
    assert plan.temporal is None
    assert plan.temporal_error is not None

    mc, mp = _make_ctx_and_process()
    wf = AsyncMock(spec=USGSWaterDataFetcher)
    await handle_observation(
        context=mc,
        process=mp,
        plan=plan,
        ctx=RequestContext(request_id="test-pydantic-leak"),
        water_data_fetcher=wf,
    )

    reply_text = mc.reply.call_args[0][0]
    assert "I couldn't understand the date period in your question." in reply_text
    reply_lower = reply_text.lower()
    assert "validation error" not in reply_lower
    assert "pydantic" not in reply_lower
    assert "errors.pydantic.dev" not in reply_lower
    assert not wf.fetch_daily_values.called


@pytest.mark.asyncio
async def test_spatial_handler_appends_temporal_note_without_failing():
    """Spatial discovery succeeds and appends date-period-not-applied note when temporal or temporal_error is set."""
    from water_agent.handlers.spatial import handle_spatial

    expected_note = "Note: the date period in your question isn't applied to station discovery/inventory."

    # 1. With temporal field on populated discrete discovery
    mc1, mp1 = _make_ctx_and_process()
    ctx1 = RequestContext(request_id="test-sp-temp")
    handle1 = ctx1.store.put(
        kind="stations",
        items={
            "stations": [{"id": "USGS-02087500", "name": "Neuse River", "site_type": "Stream"}],
            "modality": "discrete",
            "family": "calcium",
            "pcodes": ["00915"],
        },
        summary="1 station",
        sample=[],
        provenance={"modality": "discrete", "source_urls": ["https://test.url"]},
        count=1,
    )
    plan1 = QueryPlan(
        intent="spatial_discovery",
        modality="discrete",
        stations_ref=handle1.ref,
        filters={"parameter_code": "00915", "state_code": "37"},
        temporal=TemporalRequest(phrase="winter 2022", season="winter", year=2022),
    )
    await handle_spatial(mc1, mp1, plan1, ctx1)
    reply1 = mc1.reply.call_args[0][0]
    assert "Found **1** distinct monitoring locations" in reply1
    assert expected_note in reply1

    # 2. With temporal_error on 0-station discovery -> does not fail, appends note
    mc2, mp2 = _make_ctx_and_process()
    ctx2 = RequestContext(request_id="test-sp-terr")
    handle2 = ctx2.store.put(
        kind="stations",
        items={"stations": [], "modality": "continuous"},
        summary="0 stations",
        sample=[],
        provenance={"modality": "continuous"},
        count=0,
    )
    plan2 = QueryPlan(
        intent="spatial_discovery",
        modality="continuous",
        stations_ref=handle2.ref,
        filters={"parameter_code": "00060", "state_code": "37"},
        temporal_error="Unrecognized season 'monsoon'.",
    )
    await handle_spatial(mc2, mp2, plan2, ctx2)
    reply2 = mc2.reply.call_args[0][0]
    assert "I found no USGS monitoring locations" in reply2
    assert expected_note in reply2


@pytest.mark.asyncio
async def test_inventory_handler_appends_temporal_note_without_failing():
    """Inventory handler succeeds and appends date-period-not-applied note when temporal or temporal_error is set."""
    from water_agent.handlers.inventory import handle_inventory

    expected_note = "Note: the date period in your question isn't applied to station discovery/inventory."

    for kwargs in (
        {"temporal": TemporalRequest(phrase="in 2021", start="2021", end="2021")},
        {"temporal_error": "Month 13 is out of range"},
    ):
        mc, mp = _make_ctx_and_process()
        ctx = RequestContext(request_id="test-inv-temp")
        handle = ctx.store.put(
            kind="inventory",
            items={
                "monitoring_location_id": "USGS-02087500",
                "parameters": {
                    "00060": {
                        "name": "Streamflow",
                        "estimated_daily_records": 5000,
                        "begin": "2000-01-01",
                        "end": "2026-01-01",
                    }
                },
                "discrete_parameters": {},
                "discrete_total_observations": 0,
            },
            summary="inventory",
            sample=[],
            provenance={"source_urls": ["https://test.url"]},
            count=1,
        )
        plan = QueryPlan(
            intent="station_inventory",
            inventory_ref=handle.ref,
            filters={},
            **kwargs,
        )
        await handle_inventory(mc, mp, plan, ctx, request="What data is available at USGS-02087500?")
        reply = mc.reply.call_args[0][0]
        assert "Available data parameters for **USGS-02087500**" in reply
        assert expected_note in reply

