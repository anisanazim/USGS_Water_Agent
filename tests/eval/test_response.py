from __future__ import annotations

import json
import os
from pathlib import Path
import pytest
from dotenv import load_dotenv

load_dotenv()

from deepeval.models import OpenAIModel
from deepeval.metrics import FaithfulnessMetric, AnswerRelevancyMetric, GEval
from deepeval.test_case import LLMTestCase, SingleTurnParams

FIXTURES_DIR = Path(__file__).parent / "datasets" / "synthesis_fixtures"


@pytest.fixture
def eval_llm() -> OpenAIModel:
    return OpenAIModel(
        model=os.environ.get("OPENAI_MODEL", "gpt-5.6-luna"),
        base_url=os.environ.get("OPENAI_BASE_URL"),
        api_key=os.environ.get("OPENAI_API_KEY"),
    )


def test_response_faithfulness_and_relevancy(eval_llm: OpenAIModel):
    """Evaluates agent response synthesis using frozen USGS API payloads.
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
        evaluation_params=[SingleTurnParams.ACTUAL_OUTPUT],
        model=eval_llm,
        threshold=0.7,
    )
    scientific_geval.measure(test_case)
    assert scientific_geval.score >= 0.7, f"Scientific G-Eval failed ({scientific_geval.score}): {scientific_geval.reason}"

