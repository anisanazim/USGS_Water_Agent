from __future__ import annotations

import pytest
from pydantic import BaseModel

from water_agent.query_planner import QueryPlanner, _clean_message_payload
from water_agent.metadata.discrete_families import resolve_discrete_parameter
from water_agent.fetchers.samples_fetcher import USGSSamplesFetcher


def test_bug_1_azure_reasoning_token_sanitization():
    """Bug 1 Regression:
    gpt-5.6-luna outputs server-encrypted reasoning tokens (reasoning_items / reasoning_content).
    LiteLLM load balancing across Azure OpenAI instances throws 400 Bad Request if turn-1
    tokens are forwarded to turn-2. Verify _clean_message_payload strips them cleanly.
    """
    class MockMessage(BaseModel):
        role: str
        content: str
        reasoning_items: list[str] = ["encrypted_blob_123"]
        reasoning_content: str = "internal_reasoning"

    mock_msg = MockMessage(role="assistant", content="Here are the results")
    cleaned = _clean_message_payload(mock_msg)

    assert cleaned["role"] == "assistant"
    assert cleaned["content"] == "Here are the results"
    assert "reasoning_items" not in cleaned
    assert "reasoning_content" not in cleaned


def test_bug_2_discrete_family_strict_word_boundary_matching():
    """Bug 2 Regression:
    Substring matching previously caused 'Total nitrogen, ss' (a continuous parameter)
    to match the discrete family 'Total Nitrogen' because 'Total Nitrogen' in 'Total nitrogen, ss'.
    Verify exact/word-boundary check prevents false positive discrete family matches.
    """
    continuous_term = "Total nitrogen, ss"
    res = resolve_discrete_parameter(continuous_term)
    # Must NOT resolve to a discrete family
    assert res.status == "not_found", f"Expected not_found for '{continuous_term}', got '{res.status}'"
    assert res.family is None

    # Exact term should match cleanly
    res_calcium = resolve_discrete_parameter("calcium")
    assert res_calcium.status == "resolved"
    assert res_calcium.family.name == "calcium"

    res_phosphorus = resolve_discrete_parameter("phosphorus")
    assert res_phosphorus.status == "resolved"
    assert res_phosphorus.family.name == "phosphorus"


def test_bug_3_usgs_samples_comma_separated_pcode_splitting():
    """Bug 3 Regression:
    Passing comma-separated string '00665,00660' to USGS Samples API causes it to silently
    return recordCount=0. USGSSamplesFetcher._normalize_params must convert comma-separated
    strings into lists of individual codes for repeated HTTP query parameter serialization.
    """
    fetcher = USGSSamplesFetcher()
    params = {"usgsPCode": "00665,00660,00671", "stateFips": "US:37"}
    clean = fetcher._normalize_params(params)

    assert isinstance(clean["usgsPCode"], list)
    assert clean["usgsPCode"] == ["00665", "00660", "00671"]
    assert clean["stateFips"] == "US:37"


def test_bug_4_query_planner_client_explicit_timeout():
    """Bug 4 Regression:
    QueryPlanner without an explicit timeout causes AsyncOpenAI to hang indefinitely
    when network sockets are blocked or proxy connections stall.
    Verify client timeout is configured defensively.
    """
    planner = QueryPlanner()
    assert planner.client.timeout is not None
    assert planner.client.timeout <= 45.0
