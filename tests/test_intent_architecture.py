from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agent import USGSWaterAgent
from water_agent.metadata.models import QueryIntent, ParameterModality
from water_agent.query_planner import QueryPlan


def test_query_plan_intent_inference():
    # Spatial intent inferred from stations_ref
    p1 = QueryPlan(stations_ref="ref-123")
    assert p1.intent == QueryIntent.SPATIAL_DISCOVERY

    # Inventory intent inferred from inventory_ref without parameter_code
    p2 = QueryPlan(inventory_ref="inv-123")
    assert p2.intent == QueryIntent.STATION_INVENTORY

    # Parameter count intent inferred from inventory_ref with parameter_code
    p3 = QueryPlan(inventory_ref="inv-123", filters={"parameter_code": "00665"})
    assert p3.intent == QueryIntent.STATION_PARAMETER_COUNT

    # Observation intent inferred from filters
    p4 = QueryPlan(filters={"parameter_code": "00060", "monitoring_location_id": "02087500"})
    assert p4.intent == QueryIntent.OBSERVATION

    # Clarification inferred from clarification_needed
    p5 = QueryPlan(clarification_needed="Please specify a location")
    assert p5.intent == QueryIntent.CLARIFICATION

    # Fallback to clarification when completely empty
    p6 = QueryPlan()
    assert p6.intent == QueryIntent.CLARIFICATION


def test_query_plan_alias_normalization():
    p1 = QueryPlan(intent="spatial", modality="discrete")
    assert p1.intent == QueryIntent.SPATIAL_DISCOVERY
    assert p1.modality == ParameterModality.DISCRETE

    p2 = QueryPlan(intent="count", modality="continuous")
    assert p2.intent == QueryIntent.STATION_PARAMETER_COUNT
    assert p2.modality == ParameterModality.CONTINUOUS

    p3 = QueryPlan(intent="inventory")
    assert p3.intent == QueryIntent.STATION_INVENTORY


def test_query_filters_normalization_and_typed_filters():
    from water_agent.metadata.models import QueryFilters, StationLocation, ParameterStationBreakdown, StationCandidateItem

    plan = QueryPlan(
        filters={
            "parameter_code": "60",
            "statistic_id": "3",
            "monitoring_location_id": "  02087500  ",
            "state_code": "US:37",
            "custom_field": "extra_value",
        }
    )

    # filters dict is auto-normalized
    assert plan.filters["parameter_code"] == "00060"
    assert plan.filters["statistic_id"] == "00003"
    assert plan.filters["monitoring_location_id"] == "02087500"
    assert plan.filters["state_code"] == "US:37"
    assert plan.filters["custom_field"] == "extra_value"

    # typed_filters property returns strongly typed QueryFilters
    typed = plan.typed_filters
    assert isinstance(typed, QueryFilters)
    assert typed.parameter_code == "00060"
    assert typed.statistic_id == "00003"
    assert typed.monitoring_location_id == "02087500"

    # Boundary models serialization
    station = StationLocation(
        id="USGS-0423205010",
        name="FALL CREEK NEAR DRYDEN NY",
        site_type="Stream",
        county="Tompkins County",
        state="New York",
    )
    assert station.id == "USGS-0423205010"
    assert station.model_dump()["site_type"] == "Stream"

    breakdown = ParameterStationBreakdown(
        pcode="00915",
        descriptor="Calcium, dissolved",
        unit="mg/l",
        station_count=3663,
    )
    assert breakdown.pcode == "00915"
    assert breakdown.station_count == 3663

    candidate = StationCandidateItem(
        descriptor="Calcium",
        count=14,
        pcode="00915",
        unit="mg/l",
    )
    assert candidate.count == 14
    assert candidate.pcode == "00915"


@pytest.mark.asyncio
async def test_agent_routes_to_spatial_handler():
    agent = USGSWaterAgent()
    mock_process = AsyncMock()
    mock_process.__aenter__.return_value = mock_process
    mock_context = MagicMock()
    mock_context.begin_process.return_value = mock_process

    plan = QueryPlan(
        intent=QueryIntent.SPATIAL_DISCOVERY,
        stations_ref="ref-stations-test",
    )
    agent.query_planner.plan = AsyncMock(return_value=plan)

    with patch("agent.handle_spatial", new_callable=AsyncMock) as mock_handle:
        await agent._handle_query_water_data(
            context=mock_context,
            request="Where is streamflow monitored in Wake County?",
            params=None,
        )
        assert mock_handle.called


@pytest.mark.asyncio
async def test_agent_routes_to_inventory_handler():
    agent = USGSWaterAgent()
    mock_process = AsyncMock()
    mock_process.__aenter__.return_value = mock_process
    mock_context = MagicMock()
    mock_context.begin_process.return_value = mock_process

    plan = QueryPlan(
        intent=QueryIntent.STATION_INVENTORY,
        inventory_ref="ref-inv-test",
    )
    agent.query_planner.plan = AsyncMock(return_value=plan)

    with patch("agent.handle_inventory", new_callable=AsyncMock) as mock_handle:
        await agent._handle_query_water_data(
            context=mock_context,
            request="What information is available at USGS-0423205010?",
            params=None,
        )
        assert mock_handle.called


@pytest.mark.asyncio
async def test_agent_routes_to_parameter_count_handler():
    agent = USGSWaterAgent()
    mock_process = AsyncMock()
    mock_process.__aenter__.return_value = mock_process
    mock_context = MagicMock()
    mock_context.begin_process.return_value = mock_process

    plan = QueryPlan(
        intent=QueryIntent.STATION_PARAMETER_COUNT,
        inventory_ref="ref-inv-test",
        filters={"parameter_code": "00665"},
    )
    agent.query_planner.plan = AsyncMock(return_value=plan)

    mock_context.reply = AsyncMock()

    with patch("agent.handle_parameter_count", new_callable=AsyncMock) as mock_handle:
        await agent._handle_query_water_data(
            context=mock_context,
            request="How many phosphorus records are at USGS-0423205010?",
            params=None,
        )
        assert mock_handle.called


@pytest.mark.asyncio
async def test_agent_routes_to_observation_handler():
    agent = USGSWaterAgent()
    mock_process = AsyncMock()
    mock_process.__aenter__.return_value = mock_process
    mock_context = MagicMock()
    mock_context.begin_process.return_value = mock_process

    plan = QueryPlan(
        intent=QueryIntent.OBSERVATION,
        filters={"parameter_code": "00915", "monitoring_location_id": "0423205010"},
    )
    agent.query_planner.plan = AsyncMock(return_value=plan)

    with patch("agent.handle_observation", new_callable=AsyncMock) as mock_handle:
        await agent._handle_query_water_data(
            context=mock_context,
            request="Show me the calcium values at USGS-0423205010.",
            params=None,
        )
        assert mock_handle.called


@pytest.mark.asyncio
async def test_agent_routes_unscoped_catalog_query_to_clarification():
    agent = USGSWaterAgent()
    mock_process = AsyncMock()
    mock_process.__aenter__.return_value = mock_process
    mock_context = MagicMock()
    mock_context.begin_process.return_value = mock_process

    plan = QueryPlan(
        intent=QueryIntent.CLARIFICATION,
        clarification_needed=(
            "Please provide a USGS monitoring station ID (e.g. 'USGS-0423205010') "
            "or a geographic area (e.g. 'Wake County, NC') to query water-data observations or monitoring locations."
        ),
    )
    agent.query_planner.plan = AsyncMock(return_value=plan)

    with patch("agent.handle_clarification", new_callable=AsyncMock) as mock_handle:
        await agent._handle_query_water_data(
            context=mock_context,
            request="List all available USGS water-monitoring parameters related to turbidity.",
            params=None,
        )
        assert mock_handle.called
