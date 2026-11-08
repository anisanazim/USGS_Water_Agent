from __future__ import annotations

import json
import os
from pathlib import Path
import pytest
from dotenv import load_dotenv

load_dotenv()

from deepeval.models import GPTModel
from deepeval.metrics import FaithfulnessMetric, AnswerRelevancyMetric, GEval
from deepeval.test_case import LLMTestCase, LLMTestCaseParams

FIXTURES_DIR = Path(__file__).parent / "datasets" / "synthesis_fixtures"


@pytest.fixture
def eval_llm() -> GPTModel:
    return GPTModel(
        model=os.environ.get("OPENAI_MODEL", "gpt-5.6-luna"),
        base_url=os.environ.get("OPENAI_BASE_URL"),
        api_key=os.environ.get("OPENAI_API_KEY"),
    )


def test_response_count_phosphorus(eval_llm: GPTModel):
    """Evaluates agent response synthesis for discrete station parameter count.
    Verifies:
    1. Faithfulness: Count of 12,337 strictly originates from API payload (no hallucinations).
    2. Answer Relevancy: Directly addresses phosphorus count at USGS-0423205010.
    3. Scientific Accuracy G-Eval: Enforces discrete modality and media disclaimers.
    """
    fixture_file = FIXTURES_DIR / "count_phosphorus.json"
    with open(fixture_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Convert API payload into textual retrieval context for DeepEval
    retrieval_context = [
        f"USGS Station: {data['station_id']}",
        f"Parameter: {data['parameter']}",
        f"USGS API recordCount: {data['usgs_api_payload']['recordCount']}",
        f"Constituent Parameter Codes: {list(data['usgs_api_payload']['pcodeCounts'].keys())}",
    ]

    test_case = LLMTestCase(
        input=data["query"],
        actual_output=data["agent_response"],
        retrieval_context=retrieval_context,
    )

    # 1. Faithfulness Metric (Check for hallucinated numbers)
    faithfulness = FaithfulnessMetric(threshold=0.8, model=eval_llm)
    faithfulness.measure(test_case)
    assert faithfulness.score >= 0.8, f"Faithfulness check failed ({faithfulness.score}): {faithfulness.reason}"

    # 2. Answer Relevancy Metric
    relevancy = AnswerRelevancyMetric(threshold=0.8, model=eval_llm)
    relevancy.measure(test_case)
    assert relevancy.score >= 0.8, f"Answer Relevancy failed ({relevancy.score}): {relevancy.reason}"

    # 3. Scientific Accuracy G-Eval (Modality & Fraction check)
    scientific_geval = GEval(
        name="Scientific Quality & Modality Disclaimer",
        criteria=(
            "Verify that the response includes a clear modality disclaimer stating that "
            "these are discrete sample measurements (not continuous sensors) and mentions sample media."
        ),
        evaluation_params=[LLMTestCaseParams.ACTUAL_OUTPUT],
        model=eval_llm,
        threshold=0.7,
    )
    scientific_geval.measure(test_case)
    assert scientific_geval.score >= 0.7, f"Scientific G-Eval failed ({scientific_geval.score}): {scientific_geval.reason}"


def test_response_spatial_nitrate_wake(eval_llm: GPTModel):
    """Evaluates agent response synthesis for spatial discovery (Wake County, NC Nitrate).
    Verifies:
    1. Faithfulness: Station count (8) and coordinates originate faithfully from payload.
    2. Answer Relevancy: Directly addresses where nitrate is monitored in Wake County, NC.
    3. Scientific Accuracy G-Eval: Enforces discrete modality disclaimer and constituent codes.
    """
    fixture_file = FIXTURES_DIR / "spatial_nitrate_wake.json"
    with open(fixture_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    payload = data["usgs_api_payload"]
    stations_summary = [
        f"{loc['id']} ({loc['name']}) at lat={loc['latitude']}, lon={loc['longitude']}: {loc['sample_count']} samples"
        for loc in payload["locations"]
    ]

    retrieval_context = [
        f"Query Location: {data['county']} County, {data['state']} (FIPS: {data['fips']})",
        f"Target Analyte: {data['parameter']}",
        f"Total Locations Count: {payload['locationsCount']}",
        f"Constituent Parameter Codes: {payload['constituentPcodes']}",
        "Locations List: " + "; ".join(stations_summary),
    ]

    test_case = LLMTestCase(
        input=data["query"],
        actual_output=data["agent_response"],
        retrieval_context=retrieval_context,
    )

    # 1. Faithfulness Metric
    faithfulness = FaithfulnessMetric(threshold=0.8, model=eval_llm)
    faithfulness.measure(test_case)
    assert faithfulness.score >= 0.8, f"Spatial Faithfulness check failed ({faithfulness.score}): {faithfulness.reason}"

    # 2. Answer Relevancy Metric
    relevancy = AnswerRelevancyMetric(threshold=0.8, model=eval_llm)
    relevancy.measure(test_case)
    assert relevancy.score >= 0.8, f"Spatial Answer Relevancy failed ({relevancy.score}): {relevancy.reason}"

    # 3. Scientific Modality and Spatial G-Eval
    spatial_geval = GEval(
        name="Spatial Discovery & Modality Quality",
        criteria=(
            "Verify that the response presents a clear structured table of monitoring stations with IDs and coordinates, "
            "accurately summarizes nitrate monitoring in Wake County, NC, and includes the mandatory discrete "
            "modality disclaimer (discrete sample measurements, not continuous sensors)."
        ),
        evaluation_params=[LLMTestCaseParams.ACTUAL_OUTPUT],
        model=eval_llm,
        threshold=0.7,
    )
    spatial_geval.measure(test_case)
    assert spatial_geval.score >= 0.7, f"Spatial G-Eval failed ({spatial_geval.score}): {spatial_geval.reason}"


def test_response_observation_streamflow_clayton(eval_llm: GPTModel):
    """Evaluates agent response synthesis for continuous daily streamflow observation.
    Verifies:
    1. Faithfulness: Streamflow reading (1,420.0 ft3/s) and date (2026-10-28) strictly adhere to payload.
    2. Answer Relevancy: Accurately reports latest daily streamflow for USGS-02087500.
    3. Scientific Accuracy G-Eval: Distinguishes continuous daily sensor modality from discrete lab samples.
    """
    fixture_file = FIXTURES_DIR / "observation_streamflow_clayton.json"
    with open(fixture_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    payload = data["usgs_api_payload"]
    retrieval_context = [
        f"USGS Station: {payload['monitoring_location_id']} ({payload['monitoring_location_name']})",
        f"Parameter: {payload['parameter_code']} - {payload['parameter_description']}",
        f"Statistic: {payload['statistic_code']} - {payload['statistic_description']}",
        f"Observed Daily Value: {payload['latest_value']} {payload['unit']}",
        f"Observation Date: {payload['observation_date']}",
        f"Approval Status: {payload['approval_status']}",
        "Modality: Continuous daily sensor observations (OGC API - Features)",
    ]

    test_case = LLMTestCase(
        input=data["query"],
        actual_output=data["agent_response"],
        retrieval_context=retrieval_context,
    )

    # 1. Faithfulness Metric
    faithfulness = FaithfulnessMetric(threshold=0.8, model=eval_llm)
    faithfulness.measure(test_case)
    assert faithfulness.score >= 0.8, f"Continuous Faithfulness check failed ({faithfulness.score}): {faithfulness.reason}"

    # 2. Answer Relevancy Metric
    relevancy = AnswerRelevancyMetric(threshold=0.8, model=eval_llm)
    relevancy.measure(test_case)
    assert relevancy.score >= 0.8, f"Continuous Answer Relevancy failed ({relevancy.score}): {relevancy.reason}"

    # 3. Continuous Modality G-Eval
    continuous_geval = GEval(
        name="Continuous Daily Sensor Observation Quality",
        criteria=(
            "Verify that the response accurately reports the streamflow/discharge value of 1,420.0 ft³/s "
            "for USGS-02087500 on October 28, 2026, with correct units, and correctly identifies this as "
            "continuous daily sensor data rather than discrete bottle samples."
        ),
        evaluation_params=[LLMTestCaseParams.ACTUAL_OUTPUT],
        model=eval_llm,
        threshold=0.7,
    )
    continuous_geval.measure(test_case)
    assert continuous_geval.score >= 0.7, f"Continuous G-Eval failed ({continuous_geval.score}): {continuous_geval.reason}"
