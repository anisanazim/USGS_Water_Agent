import pytest
import httpx
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

from water_agent.fetchers.samples_fetcher import USGSSamplesFetcher, DiscreteFetchResult
from water_agent.metadata.references.store import ReferenceStore
from water_agent.metadata.registry import daily_registry
from water_agent.metadata.resolver import ReferenceResolver
from water_agent.tools.discrete_query import DiscreteQueryTool
from water_agent.results import RequestContext
from agent import USGSWaterAgent


@pytest.fixture
def resolver():
    ref_dir = Path(__file__).parent.parent / "metadata" / "raw" / "references"
    store = ReferenceStore(ref_dir)
    return ReferenceResolver(daily_registry, store)


@pytest.mark.asyncio
async def test_discrete_query_tool_value_dominant(resolver):
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    fetcher = USGSSamplesFetcher(client=mock_client)

    # Mock count
    count_resp = httpx.Response(
        status_code=200,
        json={"recordCount": 1},
        headers={"x-ratelimit-remaining": "3999", "x-ratelimit-limit": "4000"},
        request=httpx.Request("GET", "https://api.waterdata.usgs.gov/samples-data/results/count"),
    )
    mock_client.get.return_value = count_resp
    mock_client.build_request.return_value = httpx.Request(
        "GET", "https://api.waterdata.usgs.gov/samples-data/results/narrow"
    )

    async def mock_stream_project(url, params):
        return [
            {
                "activity_date": "2023-05-15",
                "activity_time": "10:00:00",
                "result_value": 15.2,
                "unit": "mg/l",
                "pcode": "00915",
                "location_id": "USGS-02087500",
            }
        ]

    fetcher._stream_and_project_results = mock_stream_project

    tool = DiscreteQueryTool(fetcher=fetcher, resolver=resolver)
    ctx = RequestContext(request_id="test-ctx-1")

    res = await tool.query_results(
        parameter="calcium",
        state_name="North Carolina",
        context=ctx,
    )

    assert res["status"] == "resolved"
    assert res["pcode"] == "00915"
    assert res["unit"] == "mg/l"
    assert "1 discrete records of Calcium, water, filtered (00915), mg/l" in res["citation"]
    assert res["modality"] == "discrete"
    assert res["mixed_modality"] is False
    assert res["ref"] is not None


@pytest.mark.asyncio
async def test_discrete_query_tool_nitrate_flat_lookup(resolver):
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    fetcher = USGSSamplesFetcher(client=mock_client)
    tool = DiscreteQueryTool(fetcher=fetcher, resolver=resolver)

    # In flat lookup, nitrate resolves without clarification to all 13 pcodes
    from water_agent.metadata.discrete_families import resolve_discrete_parameter
    res = resolve_discrete_parameter("nitrate")
    assert res.status == "resolved"
    assert len(res.pcodes) == 13


@pytest.mark.asyncio
async def test_discrete_query_tool_spatial_invariant_union(resolver):
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    fetcher = USGSSamplesFetcher(client=mock_client)

    count_resp = httpx.Response(
        status_code=200,
        json={"recordCount": 2},
        headers={"x-ratelimit-remaining": "3999", "x-ratelimit-limit": "4000"},
        request=httpx.Request("GET", "https://api.waterdata.usgs.gov/samples-data/locations/count"),
    )
    mock_client.get.return_value = count_resp
    mock_client.build_request.return_value = httpx.Request(
        "GET", "https://api.waterdata.usgs.gov/samples-data/locations/site"
    )

    calls = []
    async def mock_locations(params, context=None, pcode=None):
        calls.append(params.get("usgsPCode"))
        return DiscreteFetchResult(
            records=[
                {"location_id": "USGS-00915-01", "location_name": "Test Site 1"},
                {"location_id": "USGS-00910-01", "location_name": "Test Site 2"},
            ],
            total_count=2,
            source_urls=["https://test.url"],
        )

    fetcher.fetch_locations = mock_locations
    tool = DiscreteQueryTool(fetcher=fetcher, resolver=resolver)
    ctx = RequestContext(request_id="test-spatial-1")

    res = await tool.find_stations(
        parameter="calcium",
        state_name="North Carolina",
        context=ctx,
    )

    # Spatial query for calcium unions across all 4 family pcodes in a single request
    assert res["status"] == "resolved"
    assert set(res["pcodes"]) == {"00910", "00915", "00916", "00918"}
    assert set(calls[0]) == {"00910", "00915", "00916", "00918"}
    assert res["total_count"] == 2
    assert res["modality"] == "discrete"
    assert res["ref"] is not None


@pytest.mark.asyncio
async def test_agent_routes_discrete_parameter_to_samples():
    """Agent intercepts discrete parameter from planner and queries Samples API, returning citation contract response."""
    agent = USGSWaterAgent()

    mock_process = AsyncMock()
    mock_process.__aenter__.return_value = mock_process
    mock_process.__aexit__.return_value = None

    mock_context = MagicMock()
    mock_context.begin_process.return_value = mock_process
    mock_context.reply = AsyncMock()

    # Mock planner returning plan with calcium 00915
    plan_mock = MagicMock()
    plan_mock.semantic = {"parameter": "calcium", "state": "North Carolina"}
    plan_mock.clarification_needed = None
    plan_mock.inventory_ref = None
    plan_mock.stations_ref = None
    plan_mock.filters = {"parameter_code": "00915", "state_code": "37"}
    plan_mock.model_dump.return_value = {"filters": plan_mock.filters}

    agent.query_planner.plan = AsyncMock(return_value=plan_mock)

    # Mock USGSSamplesFetcher
    mock_fetch_result = DiscreteFetchResult(
        records=[
            {
                "activity_date": "2023-06-01",
                "activity_time": "09:00:00",
                "result_value": 18.5,
                "unit": "mg/l",
                "pcode": "00915",
                "location_id": "USGS-02087500",
            }
        ],
        total_count=1,
        source_urls=["https://api.waterdata.usgs.gov/samples-data/results/narrow"],
    )

    with patch("agent.USGSSamplesFetcher") as MockFetcherCls:
        mock_instance = AsyncMock()
        mock_instance.__aenter__.return_value = mock_instance
        mock_instance.fetch_results.return_value = mock_fetch_result
        MockFetcherCls.return_value = mock_instance

        await agent._handle_answer_daily_query(
            context=mock_context,
            request="What is the calcium concentration in North Carolina?",
            params=None,
        )

    # Verify context reply and contract
    assert mock_context.reply.called
    reply_text = mock_context.reply.call_args[0][0]
    assert "**1** discrete records of Calcium, water, filtered (00915), mg/l" in reply_text
    assert "2023-06-01" in reply_text
    assert "18.5" in reply_text
    assert "Modality: discrete sample measurements (not continuous sensors)" in reply_text
    assert "Sample media: Water (samples in other media such as sediment/tissue are excluded)." in reply_text


def test_reference_search_calcium_all_pcodes(resolver):
    from water_agent.tools.reference_search import ReferenceSearchTool
    tool = ReferenceSearchTool(resolver)
    results = tool.search("parameter_code", "calcium")
    assert len(results) == 4
    assert results[0]["resolved_value"] == "00915"
    assert "Calcium, water, filtered" in results[0]["matched_value"]
    assert results[1]["resolved_value"] == "00916"
    assert results[2]["resolved_value"] == "00910"
    assert results[3]["resolved_value"] == "00918"


def test_reference_search_phosphorus_candidates(resolver):
    from water_agent.tools.reference_search import ReferenceSearchTool
    tool = ReferenceSearchTool(resolver)
    results = tool.search("parameter_code", "phosphorus")
    assert len(results) > 1
    # Check that all candidates contain descriptor, units, and NC records
    pcodes = [r["resolved_value"] for r in results]
    assert "00665" in pcodes
    assert "00660" in pcodes
    assert "00671" in pcodes
    assert any("mg/l as P" in r["matched_value"] for r in results)
    assert any("NC records" in r["matched_value"] for r in results)


@pytest.mark.asyncio
async def test_spatial_query_discrete_routing_calcium(resolver):
    from water_agent.tools.spatial_query import SpatialQueryTool
    from usgs_water_data import USGSWaterDataFetcher

    mock_fetcher = MagicMock(spec=USGSWaterDataFetcher)
    mock_discrete_tool = AsyncMock()
    mock_discrete_tool.find_stations.return_value = {
        "status": "resolved",
        "family": "calcium",
        "pcodes": ["00910", "00915", "00916", "00918"],
        "total_count": 3663,
        "stations": [{"location_id": "USGS-02087500", "location_name": "Test Site", "site_type": "Stream", "county_name": "Wake"}],
        "summary": "Found 3,663 discrete locations",
        "modality": "discrete",
        "mixed_modality": False,
        "source_urls": ["https://test.url"],
    }

    tool = SpatialQueryTool(mock_fetcher, resolver, discrete_tool=mock_discrete_tool)
    res = await tool.find_stations("00915", "North Carolina")

    assert res["modality"] == "discrete"
    assert res["family"] == "calcium"
    assert set(res["pcodes"]) == {"00910", "00915", "00916", "00918"}
    assert len(res["stations"]) == 1
    assert res["stations"][0]["id"] == "USGS-02087500"
    mock_discrete_tool.find_stations.assert_called_once_with(
        parameter="00915",
        state_name="North Carolina",
        county_name=None,
    )


@pytest.mark.asyncio
async def test_agent_handles_discrete_spatial_citation_contract():
    agent = USGSWaterAgent()

    mock_process = AsyncMock()
    mock_process.__aenter__.return_value = mock_process
    mock_process.__aexit__.return_value = None

    mock_context = MagicMock()
    mock_context.begin_process.return_value = mock_process
    mock_context.reply = AsyncMock()

    # Create dummy stored item in ctx.store
    mock_handle = MagicMock()
    mock_handle.items = {
        "stations": [{"id": "USGS-02087500", "name": "Neuse River", "site_type": "Stream", "county": "Wake County"}],
        "modality": "discrete",
        "family": "calcium",
        "pcodes": ["00910", "00915", "00916", "00918"],
        "mixed_modality": False,
    }
    mock_handle.provenance = {"modality": "discrete", "source_urls": ["https://test.url"]}

    plan_mock = MagicMock()
    plan_mock.semantic = {"parameter": "calcium", "state": "North Carolina"}
    plan_mock.clarification_needed = None
    plan_mock.inventory_ref = None
    plan_mock.stations_ref = "ref-stations-1"
    plan_mock.filters = {"parameter_code": "00915", "state_code": "37"}
    plan_mock.model_dump.return_value = {"filters": plan_mock.filters}

    agent.query_planner.plan = AsyncMock(return_value=plan_mock)

    with patch("agent.RequestContext") as MockCtxCls:
        ctx_inst = MagicMock()
        ctx_inst.store.get.return_value = mock_handle
        MockCtxCls.return_value = ctx_inst

        await agent._handle_answer_daily_query(
            context=mock_context,
            request="Where is calcium monitored in North Carolina?",
            params=None,
        )

    assert mock_context.reply.called
    reply_text = mock_context.reply.call_args[0][0]
    assert "Found **1** distinct monitoring locations for calcium" in reply_text
    assert "Stations monitoring multiple forms appear once in this total" in reply_text
    assert "Primary parameter:" not in reply_text
    assert "Modality: discrete sample measurements (not continuous sensors)" in reply_text
    assert "Sample media: Water (samples in other media such as sediment/tissue are excluded)." in reply_text
    assert "USGS-02087500" in reply_text


@pytest.mark.asyncio
async def test_spatial_query_different_quantity_drops_primary_parameter():
    """Nitrate is classified different_quantity, so spatial query must NOT include a 'Primary parameter' line."""
    agent = USGSWaterAgent()

    mock_process = AsyncMock()
    mock_process.__aenter__.return_value = mock_process
    mock_process.__aexit__.return_value = None

    mock_context = MagicMock()
    mock_context.begin_process.return_value = mock_process
    mock_context.reply = AsyncMock()

    mock_handle = MagicMock()
    mock_handle.items = {
        "stations": [{"id": "USGS-02087190", "name": "Neuse River", "site_type": "Stream", "county": "Wake County"}],
        "modality": "discrete",
        "family": "nitrate",
        "pcodes": ["00630", "00618", "00631", "71851", "00620", "71850"],
        "pcode_breakdown": [{"pcode": "00618", "descriptor": "Nitrate", "unit": "mg/l", "station_count": 1}],
        "mixed_modality": True,
    }
    mock_handle.provenance = {"modality": "discrete", "source_urls": ["https://test.url"]}

    plan_mock = MagicMock()
    plan_mock.semantic = {"parameter": "nitrate", "state": "North Carolina", "county": "Wake County"}
    plan_mock.clarification_needed = None
    plan_mock.inventory_ref = None
    plan_mock.stations_ref = "ref-stations-nitrate"
    plan_mock.filters = {"parameter_code": "00618", "state_code": "37", "county_code": "183"}
    plan_mock.model_dump.return_value = {"filters": plan_mock.filters}

    agent.query_planner.plan = AsyncMock(return_value=plan_mock)

    with patch("agent.RequestContext") as MockCtxCls:
        ctx_inst = MagicMock()
        ctx_inst.store.get.return_value = mock_handle
        MockCtxCls.return_value = ctx_inst

        await agent._handle_answer_daily_query(
            context=mock_context,
            request="Where is nitrate monitored in Wake County, NC?",
            params=None,
        )

    assert mock_context.reply.called
    reply_text = mock_context.reply.call_args[0][0]
    assert "Found **1** distinct monitoring locations for nitrate" in reply_text
    assert "Stations monitoring multiple forms appear once in this total" in reply_text
    assert "Breakdown by parameter code:" in reply_text
    # Critical requirement: No Primary parameter line
    assert "Primary parameter:" not in reply_text
    assert "Modality: discrete sample measurements (not continuous sensors)" in reply_text
    assert "Sample media: Water (samples in other media such as sediment/tissue are excluded)." in reply_text


@pytest.mark.asyncio
async def test_station_scoped_clarification_lead_in_and_counts():
    """Shape 2c: Station-scoped discrete query presents all family rows with counts and dates without asking clarification."""
    agent = USGSWaterAgent()

    mock_process = AsyncMock()
    mock_process.__aenter__.return_value = mock_process
    mock_process.__aexit__.return_value = None

    mock_context = MagicMock()
    mock_context.begin_process.return_value = mock_process
    mock_context.reply = AsyncMock()

    mock_inv_handle = MagicMock()
    mock_inv_handle.items = {
        "monitoring_location_id": "USGS-0423205010",
        "parameters": {},
        "discrete_parameters": {
            "Nutrient": [
                {
                    "characteristic": "Phosphorus",
                    "descriptor": "Orthophosphate as phosphorus, water, filtered",
                    "count": 4107,
                    "first_activity": "1980-10-30",
                    "most_recent_activity": "2026-01-05T00:00:00.000001",
                },
                {
                    "characteristic": "Phosphorus",
                    "descriptor": "Orthophosphate as orthophosphate, water, filtered",
                    "count": 4101,
                    "first_activity": "1980-10-30",
                    "most_recent_activity": "2026-01-05",
                },
                {
                    "characteristic": "Phosphorus",
                    "descriptor": "Phosphorus as phosphorus, water, unfiltered",
                    "count": 4051,
                    "first_activity": "1980-08-30",
                    "most_recent_activity": "2026-01-05",
                },
            ]
        },
        "discrete_total_observations": 12259,
    }

    plan_mock = MagicMock()
    plan_mock.semantic = {"parameter": "phosphorus", "station": "USGS-0423205010"}
    plan_mock.clarification_needed = "Multiple parameter types exist."
    plan_mock.ambiguous_candidates = ["Phosphorus as phosphorus, water, unfiltered (mg/l as P) - 20,404 NC records (00665)"]
    plan_mock.inventory_ref = "inv-ref-1"
    plan_mock.stations_ref = None
    plan_mock.filters = {"monitoring_location_id": "USGS-0423205010"}
    plan_mock.model_dump.return_value = {"filters": plan_mock.filters}

    agent.query_planner.plan = AsyncMock(return_value=plan_mock)

    with patch("agent.RequestContext") as MockCtxCls:
        ctx_inst = MagicMock()
        ctx_inst.store.get.return_value = mock_inv_handle
        MockCtxCls.return_value = ctx_inst

        await agent._handle_answer_daily_query(
            context=mock_context,
            request="How many phosphorus records are at USGS-0423205010?",
            params=None,
        )

    assert mock_context.reply.called
    reply_text = mock_context.reply.call_args[0][0]
    # 1. Never claims absence
    assert "does not list" not in reply_text
    assert "not listed" not in reply_text
    # 2. Step 2c contract: Presents all rows without asking user to pick
    assert "Found discrete phosphorus measurements at **USGS-0423205010**" in reply_text
    assert "Could you clarify which specific measurement" not in reply_text
    # 3. Station-specific counts shown
    assert "4,107 observations" in reply_text
    assert "4,101 observations" in reply_text
    assert "4,051 observations" in reply_text
    # 4. Dates formatted cleanly (microseconds stripped)
    assert "2026-01-05" in reply_text
    assert ".000001" not in reply_text
    # 5. NC baseline totals must NOT leak into station response
    assert "20,404" not in reply_text
    # 6. Disclosures
    assert "Modality: discrete sample measurements (not continuous sensors)" in reply_text
    assert "Sample media: Water (samples in other media such as sediment/tissue are excluded)." in reply_text


def test_format_date_for_display():
    from agent import format_date_for_display
    assert format_date_for_display("1919-07-23T00:00:00.000001") == "1919-07-23"
    assert format_date_for_display("2026-10-13T06:15:00.000001") == "2026-10-13"
    assert format_date_for_display("2026-01-05") == "2026-01-05"
    assert format_date_for_display("Unknown") == "Unknown"
    assert format_date_for_display(None) == ""


@pytest.mark.asyncio
async def test_shape_2b_station_inventory_dual_modality():
    """Shape 2b: Station inventory formats continuous as 'estimated from YYYY-MM-DD to YYYY-MM-DD at daily interval' and discrete with exact counts."""
    agent = USGSWaterAgent()

    mock_process = AsyncMock()
    mock_process.__aenter__.return_value = mock_process
    mock_process.__aexit__.return_value = None

    mock_context = MagicMock()
    mock_context.begin_process.return_value = mock_process
    mock_context.reply = AsyncMock()

    mock_inv_handle = MagicMock()
    mock_inv_handle.items = {
        "monitoring_location_id": "USGS-0423205010",
        "parameters": {
            "00060": {
                "name": "Discharge",
                "begin": "2000-01-01",
                "end": "2023-01-01",
                "estimated_daily_records": 8402,
            }
        },
        "discrete_parameters": {
            "Inorganics": [
                {
                    "characteristic": "Calcium",
                    "descriptor": "Calcium, water, filtered",
                    "count": 105,
                    "first_activity": "1981-08-13",
                    "most_recent_activity": "1989-06-07",
                }
            ]
        },
        "discrete_total_observations": 105,
    }
    mock_inv_handle.provenance = {"source_urls": ["https://test.url"]}

    plan_mock = MagicMock()
    plan_mock.semantic = {"station": "USGS-0423205010"}
    plan_mock.clarification_needed = None
    plan_mock.inventory_ref = "inv-ref-2b"
    plan_mock.stations_ref = None
    plan_mock.filters = {}
    plan_mock.model_dump.return_value = {"filters": {}}

    agent.query_planner.plan = AsyncMock(return_value=plan_mock)

    with patch("agent.RequestContext") as MockCtxCls:
        ctx_inst = MagicMock()
        ctx_inst.store.get.return_value = mock_inv_handle
        MockCtxCls.return_value = ctx_inst

        await agent._handle_answer_daily_query(
            context=mock_context,
            request="What information is available at USGS-0423205010?",
            params=None,
        )

    assert mock_context.reply.called
    reply_text = mock_context.reply.call_args[0][0]
    assert "### Continuous Sensors" in reply_text
    assert "Discharge**: estimated from 2000-01-01 to 2023-01-01 at daily interval" in reply_text
    assert "~8,402 records" not in reply_text
    assert "### Discrete Samples" in reply_text
    assert "Information group excluded" in reply_text
    assert "Calcium, water, filtered**: 105 observations" in reply_text
    assert "Sample media: Water (samples in other media such as sediment/tissue are excluded)." in reply_text


@pytest.mark.asyncio
async def test_shape_2d_values_query_table_and_disclosures():
    """Shape 2d: Values query formats markdown table of first 10 rows and attaches disclosures."""
    agent = USGSWaterAgent()

    mock_process = AsyncMock()
    mock_process.__aenter__.return_value = mock_process
    mock_process.__aexit__.return_value = None

    mock_context = MagicMock()
    mock_context.begin_process.return_value = mock_process
    mock_context.reply = AsyncMock()

    records = [
        {
            "activity_date": f"2023-05-{i:02d}",
            "activity_time": "10:00:00",
            "result_value": 15.0 + i,
            "unit": "mg/l",
            "pcode": "00915",
            "location_id": "USGS-0423205010",
        }
        for i in range(1, 15)
    ]
    mock_fetch_result = DiscreteFetchResult(
        records=records,
        total_count=14,
        source_urls=["https://test.url"],
    )

    plan_mock = MagicMock()
    plan_mock.semantic = {"parameter": "calcium", "station": "USGS-0423205010"}
    plan_mock.clarification_needed = None
    plan_mock.inventory_ref = None
    plan_mock.stations_ref = None
    plan_mock.filters = {"parameter_code": "00915", "monitoring_location_id": "USGS-0423205010"}
    plan_mock.model_dump.return_value = {"filters": plan_mock.filters}

    agent.query_planner.plan = AsyncMock(return_value=plan_mock)

    with patch("agent.USGSSamplesFetcher") as MockFetcherCls:
        mock_instance = AsyncMock()
        mock_instance.__aenter__.return_value = mock_instance
        mock_instance.fetch_results.return_value = mock_fetch_result
        MockFetcherCls.return_value = mock_instance

        await agent._handle_answer_daily_query(
            context=mock_context,
            request="Show me the calcium values at USGS-0423205010",
            params=None,
        )

    assert mock_context.reply.called
    reply_text = mock_context.reply.call_args[0][0]
    assert "Found **14** discrete records of Calcium, water, filtered (00915), mg/l" in reply_text
    assert "First 10 observations:" in reply_text
    assert "| Date | Time | Value | Unit | Parameter Code | Descriptor |" in reply_text
    assert "Full result set of 14 records saved to artifact" in reply_text
    assert "Modality: discrete sample measurements (not continuous sensors)" in reply_text
    assert "Sample media: Water (samples in other media such as sediment/tissue are excluded)." in reply_text


def test_step_3b_outside_verified_families_mercury():
    """Step 3b: Parameters outside 9 families (mercury) return catalog matches and prompt user to pick."""
    from water_agent.metadata.discrete_families import resolve_discrete_parameter

    res = resolve_discrete_parameter("mercury")
    assert res.status == "clarification_needed"
    assert len(res.candidates) > 0
    assert "Mercury is not in the verified parameter catalog" in res.message
    assert "Which parameter code would you like to query?" in res.message
    assert any(c["pcode"] == "64755" or c["pcode"] == "30280" for c in res.candidates)


@pytest.mark.asyncio
async def test_step_3d_mixed_modality_routing_and_disclaimer(resolver):
    """Step 3d: Mixed-modality parameters route to discrete Samples API with disclaimer that continuous sensors also exist."""
    from water_agent.tools.spatial_query import SpatialQueryTool
    from usgs_water_data import USGSWaterDataFetcher

    mock_fetcher = MagicMock(spec=USGSWaterDataFetcher)
    mock_discrete_tool = AsyncMock()
    mock_discrete_tool.find_stations.return_value = {
        "status": "resolved",
        "family": "ph",
        "pcodes": ["00400", "00403"],
        "total_count": 500,
        "stations": [{"location_id": "USGS-01", "location_name": "Test Site"}],
        "summary": "Found 500 locations",
        "modality": "discrete",
        "mixed_modality": True,
        "source_urls": ["https://test.url"],
    }

    tool = SpatialQueryTool(mock_fetcher, resolver, discrete_tool=mock_discrete_tool)
    res = await tool.find_stations("00400", "North Carolina")

    assert res["modality"] == "discrete"
    assert res["family"] == "ph"
    assert res["mixed_modality"] is True
    mock_discrete_tool.find_stations.assert_called_once_with(
        parameter="00400",
        state_name="North Carolina",
        county_name=None,
    )


def test_phosphorus_ph_substring_collision_prevention():
    """Verify that 'total phosphorus' and 'orthophosphate' resolve to phosphorus, not pH."""
    from water_agent.metadata.discrete_families import resolve_discrete_parameter

    res_tp = resolve_discrete_parameter("total phosphorus")
    assert res_tp.status == "resolved"
    assert res_tp.family.name == "phosphorus"
    assert "00665" in res_tp.pcodes
    assert "00400" not in res_tp.pcodes

    res_op = resolve_discrete_parameter("orthophosphate")
    assert res_op.status == "resolved"
    assert res_op.family.name == "phosphorus"
    assert "00671" in res_op.pcodes
    assert "00400" not in res_op.pcodes

    res_ph = resolve_discrete_parameter("pH")
    assert res_ph.status == "resolved"
    assert res_ph.family.name == "ph"
    assert "00400" in res_ph.pcodes


def test_resolver_field_name_aliases(resolver):
    """Verify that resolver handles field name aliases like county_name -> county_code."""
    res = resolver.resolve("county_name", "Wake County")
    assert res.status == "resolved"
    assert res.resolved_value == "183"

    res_state = resolver.resolve("state_name", "North Carolina")
    assert res_state.status == "resolved"
    assert res_state.resolved_value == "37"


