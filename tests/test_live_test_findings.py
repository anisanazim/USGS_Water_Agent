from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from usgs_water_data import PagedResult, USGSNotFoundError, USGSServiceError, USGSWaterDataFetcher
from water_agent.fetchers.samples_fetcher import DiscreteFetchResult, USGSSamplesFetcher
from water_agent.handlers.count import handle_parameter_count
from water_agent.handlers.inventory import handle_inventory
from water_agent.handlers.observation import handle_observation
from water_agent.handlers.spatial import handle_spatial
from water_agent.metadata.discrete_families import resolve_discrete_parameter
from water_agent.metadata.models import ParameterModality
from water_agent.query_planner import QueryPlan
from water_agent.results import RequestContext
from water_agent.temporal import TemporalRequest
from water_agent.tools.spatial_query import SpatialQueryTool


def _make_mocks():
    process = MagicMock()
    process.log = AsyncMock()
    process.create_artifact = AsyncMock()
    context = MagicMock()
    context.reply = AsyncMock()
    return context, process


# ---------------------------------------------------------------------------
# Item 5: Observation Routing (Daily vs Discrete)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_routing_00300_modality_continuous_uses_daily_only():
    context, process = _make_mocks()
    ctx = RequestContext(request_id="route-cont-00300")
    samples_fetcher = AsyncMock(spec=USGSSamplesFetcher)
    daily_fetcher = AsyncMock(spec=USGSWaterDataFetcher)
    daily_fetcher.fetch_daily_values = AsyncMock(
        return_value=PagedResult(
            features=[
                {
                    "properties": {
                        "monitoring_location_id": "USGS-02087500",
                        "parameter_code": "00300",
                        "statistic_id": "00003",
                        "value": 8.2,
                        "unit_of_measure": "mg/l",
                        "time": "2024-06-01",
                        "approval_status": "Approved",
                    }
                }
            ],
            source_urls=["https://example.test/daily"],
        )
    )
    plan = QueryPlan(
        intent="observation",
        modality=ParameterModality.CONTINUOUS,
        filters={"monitoring_location_id": "USGS-02087500", "parameter_code": "00300"},
        semantic={"parameter": "dissolved oxygen"},
    )
    await handle_observation(
        context, process, plan, ctx, samples_fetcher=samples_fetcher, water_data_fetcher=daily_fetcher
    )
    assert daily_fetcher.fetch_daily_values.called
    assert not samples_fetcher.fetch_results.called
    logged = " ".join(call.args[0] for call in process.log.call_args_list if call.args)
    assert "Daily OGC API" in logged
    assert "modality=continuous" in logged


@pytest.mark.asyncio
async def test_routing_00300_statistic_id_modality_none_uses_daily():
    context, process = _make_mocks()
    ctx = RequestContext(request_id="route-stat-00300")
    samples_fetcher = AsyncMock(spec=USGSSamplesFetcher)
    daily_fetcher = AsyncMock(spec=USGSWaterDataFetcher)
    daily_fetcher.fetch_daily_values = AsyncMock(
        return_value=PagedResult(
            features=[
                {
                    "properties": {
                        "monitoring_location_id": "USGS-02087500",
                        "parameter_code": "00300",
                        "statistic_id": "00003",
                        "value": 7.9,
                        "unit_of_measure": "mg/l",
                        "time": "2024-06-02",
                        "approval_status": "Approved",
                    }
                }
            ],
            source_urls=["https://example.test/daily"],
        )
    )
    plan = QueryPlan(
        intent="observation",
        modality=None,
        filters={
            "monitoring_location_id": "USGS-02087500",
            "parameter_code": "00300",
            "statistic_id": "00003",
        },
        semantic={"parameter": "dissolved oxygen"},
    )
    await handle_observation(
        context, process, plan, ctx, samples_fetcher=samples_fetcher, water_data_fetcher=daily_fetcher
    )
    assert daily_fetcher.fetch_daily_values.called
    assert not samples_fetcher.fetch_results.called
    logged = " ".join(call.args[0] for call in process.log.call_args_list if call.args)
    assert "statistic_id=00003" in logged


@pytest.mark.asyncio
@pytest.mark.parametrize("modality", [ParameterModality.DISCRETE, ParameterModality.MIXED, None])
async def test_routing_00300_discrete_mixed_none_no_statistic_uses_samples(modality):
    context, process = _make_mocks()
    ctx = RequestContext(request_id=f"route-samp-{modality}")
    samples_fetcher = AsyncMock(spec=USGSSamplesFetcher)
    samples_fetcher.fetch_results = AsyncMock(
        return_value=DiscreteFetchResult(
            records=[
                {
                    "activity_date": "2024-05-10",
                    "activity_time": "10:00:00",
                    "result_value": 8.4,
                    "unit": "mg/l",
                    "pcode": "00300",
                }
            ],
            total_count=1,
            source_urls=["https://example.test/samples"],
        )
    )
    daily_fetcher = AsyncMock(spec=USGSWaterDataFetcher)
    plan = QueryPlan(
        intent="observation",
        modality=modality,
        filters={"monitoring_location_id": "USGS-02087500", "parameter_code": "00300"},
    )
    await handle_observation(
        context, process, plan, ctx, samples_fetcher=samples_fetcher, water_data_fetcher=daily_fetcher
    )
    assert samples_fetcher.fetch_results.called
    assert not daily_fetcher.fetch_daily_values.called


@pytest.mark.asyncio
async def test_routing_00300_modality_discrete_wins_over_statistic_id():
    context, process = _make_mocks()
    ctx = RequestContext(request_id="route-disc-over-stat")
    samples_fetcher = AsyncMock(spec=USGSSamplesFetcher)
    samples_fetcher.fetch_results = AsyncMock(
        return_value=DiscreteFetchResult(
            records=[
                {
                    "activity_date": "2024-05-10",
                    "activity_time": "10:00:00",
                    "result_value": 8.4,
                    "unit": "mg/l",
                    "pcode": "00300",
                }
            ],
            total_count=1,
            source_urls=["https://example.test/samples"],
        )
    )
    daily_fetcher = AsyncMock(spec=USGSWaterDataFetcher)
    plan = QueryPlan(
        intent="observation",
        modality=ParameterModality.DISCRETE,
        filters={
            "monitoring_location_id": "USGS-02087500",
            "parameter_code": "00300",
            "statistic_id": "00001",
        },
    )
    await handle_observation(
        context, process, plan, ctx, samples_fetcher=samples_fetcher, water_data_fetcher=daily_fetcher
    )
    assert samples_fetcher.fetch_results.called
    assert not daily_fetcher.fetch_daily_values.called
    logged = " ".join(call.args[0] for call in process.log.call_args_list if call.args)
    assert "reason: modality=discrete" in logged
    assert "statistic_id" not in logged


@pytest.mark.asyncio
async def test_daily_continuous_zero_rows_no_fallback_no_artifact():
    context, process = _make_mocks()
    ctx = RequestContext(request_id="route-cont-zero")
    samples_fetcher = AsyncMock(spec=USGSSamplesFetcher)
    daily_fetcher = AsyncMock(spec=USGSWaterDataFetcher)
    daily_fetcher.fetch_daily_values = AsyncMock(
        return_value=PagedResult(features=[], source_urls=["https://example.test/daily"])
    )
    plan = QueryPlan(
        intent="observation",
        modality=ParameterModality.CONTINUOUS,
        filters={
            "monitoring_location_id": "USGS-02087500",
            "parameter_code": "00300",
            "statistic_id": "00003",
        },
        semantic={"parameter": "dissolved oxygen"},
        temporal=TemporalRequest(
            phrase="from 2025-12-01 to 2026-02-28",
            start="2025-12-01",
            end="2026-02-28",
        ),
    )
    await handle_observation(
        context, process, plan, ctx, samples_fetcher=samples_fetcher, water_data_fetcher=daily_fetcher
    )
    assert daily_fetcher.fetch_daily_values.called
    assert not samples_fetcher.fetch_results.called
    assert not process.create_artifact.called
    reply_text = context.reply.call_args[0][0]
    assert (
        "No daily (continuous sensor) values were found for dissolved oxygen at USGS-02087500 "
        "for 2025-12-01 to 2026-02-28." in reply_text
    )
    assert "Discrete sample measurements are a separate dataset; request them explicitly if needed." in reply_text


@pytest.mark.asyncio
async def test_discrete_notes_mention_separate_continuous_dataset():
    context, process = _make_mocks()
    ctx = RequestContext(request_id="disc-note")
    samples_fetcher = AsyncMock(spec=USGSSamplesFetcher)
    samples_fetcher.fetch_results = AsyncMock(
        return_value=DiscreteFetchResult(
            records=[
                {
                    "activity_date": "2024-05-10",
                    "activity_time": "10:00:00",
                    "result_value": 8.4,
                    "unit": "mg/l",
                    "pcode": "00300",
                }
            ],
            total_count=1,
            source_urls=["https://example.test/samples"],
        )
    )
    plan = QueryPlan(
        intent="observation",
        modality=ParameterModality.DISCRETE,
        filters={"monitoring_location_id": "USGS-02087500", "parameter_code": "00300"},
    )
    await handle_observation(context, process, plan, ctx, samples_fetcher=samples_fetcher)
    reply_text = context.reply.call_args[0][0]
    assert "This agent currently returns discrete samples only." not in reply_text
    assert (
        "Continuous sensor data (daily values) is a separate dataset and was not included in this answer; "
        "request daily values for this parameter and station to retrieve it." in reply_text
    )


# ---------------------------------------------------------------------------
# Item 8: Zero Results and Comma-Separated Code Lists
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_zero_result_discrete_family_lists_all_codes_and_source_url():
    context, process = _make_mocks()
    ctx = RequestContext(request_id="zero-nitrate")
    nitrate_codes = resolve_discrete_parameter("nitrate").pcodes
    assert len(nitrate_codes) > 1

    samples_fetcher = AsyncMock(spec=USGSSamplesFetcher)
    samples_fetcher.fetch_results = AsyncMock(
        return_value=DiscreteFetchResult(
            records=[],
            total_count=0,
            source_urls=["https://api.waterdata.usgs.gov/samples-data/results/narrow?mock=1"],
        )
    )
    plan = QueryPlan(
        intent="observation",
        modality=ParameterModality.DISCRETE,
        filters={"monitoring_location_id": "USGS-02087500", "parameter_code": "nitrate"},
        semantic={"parameter": "nitrate"},
        temporal=TemporalRequest(
            phrase="from 2025-12-01 to 2026-02-28",
            start="2025-12-01",
            end="2026-02-28",
        ),
    )
    await handle_observation(context, process, plan, ctx, samples_fetcher=samples_fetcher)
    assert not process.create_artifact.called
    reply_text = context.reply.call_args[0][0]
    assert "No nitrate samples found (codes searched:" in reply_text
    assert "at USGS-02087500 for 2025-12-01 to 2026-02-28." in reply_text
    for code in nitrate_codes:
        assert code in reply_text
    assert "https://api.waterdata.usgs.gov/samples-data/results/narrow?mock=1" in reply_text
    assert "Interpreted 'from 2025-12-01 to 2026-02-28' as 2025-12-01 to 2026-02-28." in reply_text


@pytest.mark.asyncio
async def test_comma_separated_known_codes_modality_none_goes_to_samples():
    context, process = _make_mocks()
    ctx = RequestContext(request_id="comma-known")
    samples_fetcher = AsyncMock(spec=USGSSamplesFetcher)
    samples_fetcher.fetch_results = AsyncMock(
        return_value=DiscreteFetchResult(
            records=[
                {
                    "activity_date": "2024-05-10",
                    "activity_time": "10:00:00",
                    "result_value": 1.2,
                    "unit": "mg/l",
                    "pcode": "00618",
                }
            ],
            total_count=1,
            source_urls=["https://example.test/samples"],
        )
    )
    daily_fetcher = AsyncMock(spec=USGSWaterDataFetcher)
    plan = QueryPlan(
        intent="observation",
        modality=None,
        filters={"monitoring_location_id": "USGS-02087500", "parameter_code": "00618,99137"},
    )
    await handle_observation(
        context, process, plan, ctx, samples_fetcher=samples_fetcher, water_data_fetcher=daily_fetcher
    )
    assert samples_fetcher.fetch_results.called
    assert not daily_fetcher.fetch_daily_values.called
    called_params = samples_fetcher.fetch_results.call_args.kwargs["params"]
    assert called_params["usgsPCode"] == ["00618", "99137"]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_pcode", ["00618,99999", "00618,invalid", "abc,def"])
async def test_comma_separated_unknown_code_replies_unrecognized(bad_pcode):
    context, process = _make_mocks()
    ctx = RequestContext(request_id="comma-unknown")
    samples_fetcher = AsyncMock(spec=USGSSamplesFetcher)
    daily_fetcher = AsyncMock(spec=USGSWaterDataFetcher)
    plan = QueryPlan(
        intent="observation",
        modality=None,
        filters={"monitoring_location_id": "USGS-02087500", "parameter_code": bad_pcode},
    )
    await handle_observation(
        context, process, plan, ctx, samples_fetcher=samples_fetcher, water_data_fetcher=daily_fetcher
    )
    assert not samples_fetcher.fetch_results.called
    assert not daily_fetcher.fetch_daily_values.called
    assert not process.create_artifact.called
    reply_text = context.reply.call_args[0][0]
    assert "unrecognized" in reply_text.lower()


# ---------------------------------------------------------------------------
# Item 9: Count activityMediaName=Water
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_count_params_include_activity_media_water():
    context, process = _make_mocks()
    ctx = RequestContext(request_id="count-media")
    mock_fetcher = AsyncMock()
    mock_fetcher.base_url = "https://api.waterdata.usgs.gov/samples-data"
    mock_fetcher._build_source_url = lambda url, p: f"{url}?mocked"
    mock_fetcher._normalize_params = lambda p: p
    mock_fetcher.get_results_count = AsyncMock(return_value=42)

    plan = QueryPlan(
        intent="station_parameter_count",
        filters={"monitoring_location_id": "USGS-0423205010", "parameter_code": "00665"},
    )
    await handle_parameter_count(
        context,
        process,
        plan,
        ctx,
        request="How many 00665 records at USGS-0423205010?",
        samples_fetcher=mock_fetcher,
    )
    assert mock_fetcher.get_results_count.called
    called_params = mock_fetcher.get_results_count.call_args[0][0]
    assert called_params["activityMediaName"] == "Water"


# ---------------------------------------------------------------------------
# Item 10: Spatial state_name / county_name storage & geography-only fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spatial_geography_only_semantic_uses_stored_state_and_county():
    mock_fetcher = MagicMock()
    mock_resolver = MagicMock()
    mock_discrete = AsyncMock()
    mock_discrete.find_stations = AsyncMock(
        return_value={
            "status": "resolved",
            "family": "calcium",
            "pcodes": ["00915"],
            "stations": [
                {"location_id": "USGS-02087500", "location_name": "Neuse River", "site_type": "Stream", "county_name": "Wake County"}
            ],
            "source_urls": ["https://example.test/locations"],
            "pcode_breakdown": [
                {"pcode": "00915", "descriptor": "Calcium, filtered", "unit": "mg/l", "station_count": 1}
            ],
            "mixed_modality": False,
            "summary": "1 station",
        }
    )

    tool = SpatialQueryTool(mock_fetcher, mock_resolver, discrete_tool=mock_discrete)
    res = await tool.find_stations("00915", "North Carolina", county_name="Wake County")
    assert res.state_name == "North Carolina"
    assert res.county_name == "Wake County"

    ctx = RequestContext(request_id="spatial-geo")
    handle = ctx.store.put(
        kind="stations",
        items=res.model_dump(),
        summary="1 station",
        sample=[],
        provenance={"modality": "discrete", "source_urls": ["https://example.test/locations"]},
        count=1,
    )
    plan = QueryPlan(
        intent="spatial_discovery",
        modality=ParameterModality.DISCRETE,
        stations_ref=handle.ref,
        filters={"parameter_code": "00915"},
        semantic={"parameter": "calcium", "geography": "Wake County, North Carolina"},
    )
    context, process = _make_mocks()
    await handle_spatial(context, process, plan, ctx)

    art_desc = process.create_artifact.call_args.kwargs["description"]
    assert "Wake County, North Carolina" in art_desc
    reply_text = context.reply.call_args[0][0]
    assert "Wake County, North Carolina" in reply_text


# ---------------------------------------------------------------------------
# Item 11: Daily Record Qualifier Preservation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_daily_feature_preserves_qualifier_in_artifact_and_dto():
    raw_feature = {
        "properties": {
            "monitoring_location_id": "USGS-02087500",
            "parameter_code": "00060",
            "statistic_id": "00003",
            "value": 150.0,
            "unit_of_measure": "ft3/s",
            "unit": "ft3/s",
            "time": "2024-01-15",
            "approval_status": "Approved",
            "qualifier": ["ICE"],
        }
    }
    paged = PagedResult(features=[raw_feature], source_urls=["https://example.test/daily"])
    dto_list = paged.to_daily_readings()
    assert len(dto_list) == 1
    assert dto_list[0].qualifier == ["ICE"]

    context, process = _make_mocks()
    ctx = RequestContext(request_id="daily-qual")
    daily_fetcher = AsyncMock(spec=USGSWaterDataFetcher)
    daily_fetcher.fetch_daily_values = AsyncMock(return_value=paged)
    plan = QueryPlan(
        intent="observation",
        modality=ParameterModality.CONTINUOUS,
        filters={"monitoring_location_id": "USGS-02087500", "parameter_code": "00060", "statistic_id": "00003"},
        semantic={"parameter": "streamflow"},
    )
    await handle_observation(context, process, plan, ctx, water_data_fetcher=daily_fetcher)
    assert process.create_artifact.called
    payload = json.loads(process.create_artifact.call_args.kwargs["content"].decode("utf-8"))
    assert payload["readings"][0]["qualifier"] == ["ICE"]


# ---------------------------------------------------------------------------
# Item 12: Inventory Zero-Chars Branch (No Artifact + Monitoring Location Check)
# ---------------------------------------------------------------------------


def _empty_inventory_ctx_and_plan(station_id: str = "USGS-99999999"):
    ctx = RequestContext(request_id="inv-zero")
    handle = ctx.store.put(
        kind="inventory",
        items={
            "monitoring_location_id": station_id,
            "parameters": {},
            "discrete_parameters": {},
            "discrete_total_observations": 0,
        },
        summary="empty inventory",
        sample=[],
        provenance={"source_urls": ["https://example.test/summary"]},
        count=0,
    )
    plan = QueryPlan(
        intent="station_inventory",
        inventory_ref=handle.ref,
        filters={},
        semantic={"station": station_id},
    )
    return ctx, plan


@pytest.mark.asyncio
async def test_inventory_zero_chars_location_not_found():
    context, process = _make_mocks()
    ctx, plan = _empty_inventory_ctx_and_plan("USGS-99999999")
    daily_fetcher = AsyncMock(spec=USGSWaterDataFetcher)
    daily_fetcher.fetch_monitoring_locations = AsyncMock(
        return_value=PagedResult(features=[], source_urls=["https://example.test/ml"])
    )
    await handle_inventory(
        context, process, plan, ctx, request="What data is at USGS-99999999?", water_data_fetcher=daily_fetcher
    )
    assert not process.create_artifact.called
    daily_fetcher.fetch_monitoring_locations.assert_called_once_with({"id": "USGS-99999999"}, limit=1)
    reply_text = context.reply.call_args[0][0]
    assert (
        "USGS has no monitoring location with ID USGS-99999999. "
        "Please check the ID (format: USGS- followed by 8–15 digits)." in reply_text
    )

    # Also verify USGSNotFoundError produces the same not-found message and no artifact
    context2, process2 = _make_mocks()
    daily_fetcher.fetch_monitoring_locations = AsyncMock(side_effect=USGSNotFoundError("404"))
    await handle_inventory(
        context2, process2, plan, ctx, request="What data is at USGS-99999999?", water_data_fetcher=daily_fetcher
    )
    assert not process2.create_artifact.called
    assert "USGS has no monitoring location with ID USGS-99999999." in context2.reply.call_args[0][0]


@pytest.mark.asyncio
async def test_inventory_zero_chars_location_found_no_data():
    context, process = _make_mocks()
    ctx, plan = _empty_inventory_ctx_and_plan("USGS-02087500")
    daily_fetcher = AsyncMock(spec=USGSWaterDataFetcher)
    daily_fetcher.fetch_monitoring_locations = AsyncMock(
        return_value=PagedResult(
            features=[{"id": "USGS-02087500", "properties": {"monitoring_location_name": "NEUSE RIVER NEAR CLAYTON, NC"}}],
            source_urls=["https://example.test/ml"],
        )
    )
    await handle_inventory(
        context, process, plan, ctx, request="What data is at USGS-02087500?", water_data_fetcher=daily_fetcher
    )
    assert not process.create_artifact.called
    daily_fetcher.fetch_monitoring_locations.assert_called_once_with({"id": "USGS-02087500"}, limit=1)
    reply_text = context.reply.call_args[0][0]
    assert "USGS-02087500 (NEUSE RIVER NEAR CLAYTON, NC) exists, but USGS lists no sensor or sample data for it." in reply_text


@pytest.mark.asyncio
async def test_inventory_zero_chars_lookup_error_keeps_default_wording():
    context, process = _make_mocks()
    ctx, plan = _empty_inventory_ctx_and_plan("USGS-02087500")
    daily_fetcher = AsyncMock(spec=USGSWaterDataFetcher)
    daily_fetcher.fetch_monitoring_locations = AsyncMock(side_effect=USGSServiceError("timeout"))
    await handle_inventory(
        context, process, plan, ctx, request="What data is at USGS-02087500?", water_data_fetcher=daily_fetcher
    )
    assert not process.create_artifact.called
    reply_text = context.reply.call_args[0][0]
    assert "I couldn't find any data parameters associated with that station." in reply_text
