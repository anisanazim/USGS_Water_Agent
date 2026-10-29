from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import allure
import pytest
from ichatbio.agent_response import ResponseContext
from ichatbio.test_utils import InMemoryResponseChannel

from agent import USGSWaterAgent

QUERIES_FILE = Path(__file__).parent / "test_queries.json"

with open(QUERIES_FILE, "r", encoding="utf-8") as f:
    QUERIES = json.load(f)


@pytest.mark.asyncio
@pytest.mark.parametrize("item", QUERIES, ids=[item["id"] for item in QUERIES])
async def test_end_to_end_query(item: dict[str, Any]):
    qid = item["id"]
    query = item["query"]
    category = item["category"]

    allure.dynamic.feature(category)
    allure.dynamic.story(qid)
    allure.dynamic.title(f"[{qid}] {query}")
    allure.dynamic.description(
        f"**Category**: {category}\n\n**Natural Language Query**: `{query}`\n\n"
        f"**Expected Intent**: `{item.get('expected_intent')}`"
    )

    allure.attach(
        json.dumps(item, indent=2),
        name="test_definition",
        attachment_type=allure.attachment_type.JSON,
    )

    messages = []
    channel = InMemoryResponseChannel(messages)
    context = ResponseContext(channel)

    agent = USGSWaterAgent()

    try:
        with allure.step("Execute Agent Query"):
            await agent.run(context, query, "query_water_data", None, None)
    finally:
        await agent.aclose()

    # Process and attach outputs
    direct_replies: list[str] = []
    logs: list[str] = []
    artifacts: list[dict[str, Any]] = []

    for msg in messages:
        msg_type = type(msg).__name__
        if msg_type == "DirectResponse":
            direct_replies.append(msg.text)
        elif msg_type == "ProcessLogResponse":
            logs.append(msg.text)
        elif msg_type == "ArtifactResponse":
            artifacts.append(
                {
                    "description": msg.description,
                    "mimetype": msg.mimetype,
                    "content_length": len(msg.content or b""),
                }
            )

    allure.attach(
        "\n".join(logs),
        name="process_logs",
        attachment_type=allure.attachment_type.TEXT,
    )

    full_reply = "\n\n".join(direct_replies)
    allure.attach(
        full_reply,
        name="agent_response",
        attachment_type=allure.attachment_type.TEXT,
    )

    with allure.step("Verify Response Validity"):
        assert direct_replies, f"Agent did not return any DirectResponse for: {query}"
        assert (
            "Something went wrong while processing your request" not in full_reply
        ), f"Agent failed with internal error: {full_reply}"
        assert "(API Error: None)" not in full_reply, f"Agent returned degraded None error: {full_reply}"

    # Verify key terms if specified
    key_terms = item.get("key_terms", [])
    if key_terms:
        with allure.step(f"Verify Key Terms in Response: {key_terms}"):
            found_any = any(term.lower() in full_reply.lower() for term in key_terms)
            assert found_any, f"None of expected key terms {key_terms} found in response: {full_reply}"

    # Sleep between tests to respect USGS rate limits
    await asyncio.sleep(2.0)
