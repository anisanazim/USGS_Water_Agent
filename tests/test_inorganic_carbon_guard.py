import pytest
from unittest.mock import AsyncMock, MagicMock

from water_agent.handlers.count import extract_pcodes_and_family, handle_parameter_count
from water_agent.handlers.observation import handle_observation
from water_agent.metadata.discrete_families import resolve_discrete_parameter
from water_agent.query_planner import QueryPlan
from water_agent.results import RequestContext


def test_resolve_inorganic_carbon_guard():
    """Inorganic carbon terms (inorganic carbon, DIC, PIC) must not resolve to 00680/00681."""
    unsupported_terms = [
        "inorganic carbon",
        "dissolved inorganic carbon",
        "particulate inorganic carbon",
        "DIC",
        "dic",
        "PIC",
        "pic",
    ]
    for term in unsupported_terms:
        res = resolve_discrete_parameter(term)
        assert res.status == "clarification_needed"
        assert res.query_type == "unsupported"
        assert "00680" not in res.pcodes
        assert "00681" not in res.pcodes
        assert "00680" in res.message
        assert "00681" in res.message


def test_resolve_organic_carbon_remains_supported():
    """Organic carbon terms must continue to resolve to 00680 and 00681."""
    supported_terms = [
        "carbon",
        "organic carbon",
        "total organic carbon",
        "dissolved organic carbon",
        "toc",
        "doc",
    ]
    for term in supported_terms:
        res = resolve_discrete_parameter(term)
        assert res.status == "resolved"
        assert "00680" in res.pcodes
        assert "00681" in res.pcodes


def test_extract_pcodes_and_family_inorganic_carbon():
    """extract_pcodes_and_family must not resolve to 00680/00681 for inorganic carbon requests."""
    # Case 1: Planner produced parameter_code="carbon" but user asked for inorganic carbon
    plan1 = QueryPlan(filters={"parameter_code": "carbon"})
    pcodes1, family1, _ = extract_pcodes_and_family(
        plan1, "how many inorganic carbon records at USGS-01646500"
    )
    assert "00680" not in pcodes1
    assert "00681" not in pcodes1
    assert pcodes1 == []

    # Case 2: User request with DIC
    plan2 = QueryPlan(filters={"parameter_code": "carbon"})
    pcodes2, family2, _ = extract_pcodes_and_family(
        plan2, "how many DIC records at USGS-01646500"
    )
    assert "00680" not in pcodes2
    assert "00681" not in pcodes2
    assert pcodes2 == []

    # Case 3: Filter explicitly set to inorganic carbon
    plan3 = QueryPlan(filters={"parameter_code": "inorganic carbon"})
    pcodes3, family3, _ = extract_pcodes_and_family(
        plan3, "how many records at USGS-01646500"
    )
    assert "00680" not in pcodes3
    assert "00681" not in pcodes3
    assert pcodes3 == []


@pytest.mark.asyncio
async def test_handle_parameter_count_inorganic_carbon_no_fetch():
    """handle_parameter_count replies with unsupported message and makes no fetch."""
    mock_process = AsyncMock()
    mock_process.__aenter__.return_value = mock_process
    mock_process.__aexit__.return_value = None

    mock_context = MagicMock()
    mock_context.begin_process.return_value = mock_process
    mock_context.reply = AsyncMock()

    mock_fetcher = AsyncMock()

    plan = QueryPlan(
        intent="station_parameter_count",
        filters={
            "monitoring_location_id": "USGS-01646500",
            "parameter_code": "carbon",
        },
    )
    ctx = RequestContext(request_id="test-inorganic-carbon-count")

    await handle_parameter_count(
        context=mock_context,
        process=mock_process,
        plan=plan,
        ctx=ctx,
        request="how many inorganic carbon records at USGS-01646500",
        samples_fetcher=mock_fetcher,
    )

    assert mock_context.reply.called
    reply_msg = mock_context.reply.call_args[0][0]
    assert "Inorganic carbon" in reply_msg
    assert "00680" in reply_msg
    assert "00681" in reply_msg
    assert not mock_fetcher.get_results_count.called


@pytest.mark.asyncio
async def test_handle_observation_inorganic_carbon_no_fetch():
    """handle_observation replies with unsupported message and makes no upstream fetch."""
    mock_process = AsyncMock()
    mock_process.__aenter__.return_value = mock_process
    mock_process.__aexit__.return_value = None

    mock_context = MagicMock()
    mock_context.begin_process.return_value = mock_process
    mock_context.reply = AsyncMock()

    mock_samples_fetcher = AsyncMock()
    mock_water_data_fetcher = AsyncMock()

    plan = QueryPlan(
        intent="observation",
        filters={"parameter_code": "inorganic carbon"},
        semantic={"parameter": "inorganic carbon"},
    )
    ctx = RequestContext(request_id="test-inorganic-carbon-obs")

    await handle_observation(
        context=mock_context,
        process=mock_process,
        plan=plan,
        ctx=ctx,
        samples_fetcher=mock_samples_fetcher,
        water_data_fetcher=mock_water_data_fetcher,
    )

    assert mock_context.reply.called
    reply_msg = mock_context.reply.call_args[0][0]
    assert "Inorganic carbon" in reply_msg
    assert "00680" in reply_msg
    assert "00681" in reply_msg
    assert not mock_samples_fetcher.get_results.called
    assert not mock_water_data_fetcher.fetch_daily_values.called

