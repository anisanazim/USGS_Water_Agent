from __future__ import annotations

import json
import os
from pathlib import Path
import pytest
from dotenv import load_dotenv

load_dotenv()

from deepeval.models import OpenAIModel
from deepeval.metrics import GEval
from deepeval.test_case import LLMTestCase, SingleTurnParams

from water_agent.query_planner import QueryPlanner
from water_agent.metadata.models import QueryIntent, ParameterModality
from water_agent.results import RequestContext

DATASET_FILE = Path(__file__).parent / "datasets" / "failure_modes.json"

with open(DATASET_FILE, "r", encoding="utf-8") as f:
    FAILURE_MODES = {case["id"]: case for case in json.load(f)}


@pytest.fixture
def planner() -> QueryPlanner:
    return QueryPlanner()


@pytest.fixture
def eval_llm() -> OpenAIModel:
    return OpenAIModel(
        model=os.environ.get("OPENAI_MODEL", "gpt-5.6-luna"),
        base_url=os.environ.get("OPENAI_BASE_URL"),
        api_key=os.environ.get("OPENAI_API_KEY"),
    )


@pytest.mark.asyncio
async def test_planner_fm01_zero_discovery(planner: QueryPlanner):
    """FM-01 Planner: Nitrate in Dare County, NC -> spatial_discovery, discrete."""
    case = FAILURE_MODES["FM-01"]
    ctx = RequestContext(request_id="test_fm01")
    plan = await planner.plan(case["query"], ctx)

    assert plan.intent == QueryIntent.SPATIAL_DISCOVERY
    assert plan.modality == ParameterModality.DISCRETE
    assert plan.stations_ref is not None or "North Carolina" in str(plan.filters)


@pytest.mark.asyncio
async def test_planner_fm02_missing_geo_clarification(planner: QueryPlanner, eval_llm: OpenAIModel):
    """FM-02 Planner: 'Where is streamflow monitored?' (no state/county) -> clarification.
    Uses DeepEval G-Eval LLM-as-judge to verify the gist of the clarification text.
    """
    case = FAILURE_MODES["FM-02"]
    ctx = RequestContext(request_id="test_fm02")
    plan = await planner.plan(case["query"], ctx)

    # 1. Deterministic Intent Check
    assert plan.intent == QueryIntent.CLARIFICATION
    assert plan.clarification_needed is not None

    # 2. LLM-as-Judge Gist Evaluation (DeepEval)
    gist_metric = GEval(
        name="Location Clarification Gist Check",
        criteria=(
            "Evaluate whether the actual output asks the user to narrow down their query "
            "by providing a geographic constraint such as a state or county."
        ),
        evaluation_params=[SingleTurnParams.ACTUAL_OUTPUT],
        model=eval_llm,
        threshold=0.7,
    )

    test_case = LLMTestCase(
        input=case["query"],
        actual_output=plan.clarification_needed,
    )
    gist_metric.measure(test_case)

    assert gist_metric.score >= 0.7, f"Clarification gist failed ({gist_metric.score}): {gist_metric.reason}"


@pytest.mark.asyncio
async def test_planner_fm03_modality_collision_streamflow(planner: QueryPlanner):
    """FM-03 Planner: Streamflow at station -> continuous observation, pcode 00060."""
    case = FAILURE_MODES["FM-03"]
    ctx = RequestContext(request_id="test_fm03")
    plan = await planner.plan(case["query"], ctx)

    assert plan.intent == QueryIntent.OBSERVATION
    assert plan.modality == ParameterModality.CONTINUOUS
    assert plan.filters.get("monitoring_location_id") == "USGS-02087500"
    assert plan.filters.get("parameter_code") == "00060"


@pytest.mark.asyncio
async def test_planner_fm04_multi_pcode_calcium_count(planner: QueryPlanner):
    """FM-04 Planner: 'How many calcium records at USGS-0423205010?' -> station_parameter_count."""
    case = FAILURE_MODES["FM-04"]
    ctx = RequestContext(request_id="test_fm04")
    plan = await planner.plan(case["query"], ctx)

    assert plan.intent == QueryIntent.STATION_PARAMETER_COUNT
    assert plan.filters.get("monitoring_location_id") == "USGS-0423205010"
    assert "calcium" in plan.filters.get("parameter_code", "").lower()


@pytest.mark.asyncio
async def test_planner_fm07_multiple_candidates_state_clarification(planner: QueryPlanner, eval_llm: OpenAIModel):
    """FM-07 Planner: 'Where is nitrate monitored in Orange County?' (ambiguous state).
    Must trigger clarification requesting state disambiguation.
    """
    case = FAILURE_MODES["FM-07"]
    ctx = RequestContext(request_id="test_fm07")
    plan = await planner.plan(case["query"], ctx)

    # 1. Deterministic Intent Check
    assert plan.intent == QueryIntent.CLARIFICATION
    assert plan.clarification_needed is not None

    # 2. LLM-as-Judge Gist Evaluation (DeepEval)
    gist_metric = GEval(
        name="State Disambiguation Gist Check",
        criteria=(
            "Evaluate whether the actual output asks the user to clarify which state "
            "they are referring to (since Orange County exists in multiple states like NC, CA, FL, NY)."
        ),
        evaluation_params=[SingleTurnParams.ACTUAL_OUTPUT],
        model=eval_llm,
        threshold=0.7,
    )
    test_case = LLMTestCase(
        input=case["query"],
        actual_output=plan.clarification_needed,
    )
    gist_metric.measure(test_case)
    assert gist_metric.score >= 0.7, f"Disambiguation gist failed ({gist_metric.score}): {gist_metric.reason}"


@pytest.mark.asyncio
async def test_planner_fm08_no_parameter_station_query(planner: QueryPlanner):
    """FM-08 Planner: 'Show me measurements at USGS-02087500' (no parameter).
    Must either route to station_inventory or trigger clarification; must NEVER
    emit observation with a null/missing parameter_code.
    """
    case = FAILURE_MODES["FM-08"]
    ctx = RequestContext(request_id="test_fm08")
    plan = await planner.plan(case["query"], ctx)

    # Must be either station_inventory or clarification
    assert plan.intent in [QueryIntent.STATION_INVENTORY, QueryIntent.CLARIFICATION]
    # If observation was mistakenly emitted, parameter_code must not be empty
    if plan.intent == QueryIntent.OBSERVATION:
        assert plan.filters.get("parameter_code") is not None
