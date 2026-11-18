from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from water_agent.handlers.reply_utils import (
    ResultSummary,
    artifact_description,
    build_reply_text,
    send_result,
)


def test_artifact_description_format():
    s_bounded = ResultSummary(
        kind_label="discrete observations",
        subject="Dissolved oxygen (DO), water, unfiltered (00300), mg/l",
        scope="USGS-0423205010",
        unit_label="records",
        total_available=36,
        retrieved=36,
        window="2020-01-01 to 2023-07-31",
    )
    assert artifact_description(s_bounded) == (
        "USGS discrete observations - Dissolved oxygen (DO), water, unfiltered (00300), mg/l "
        "at USGS-0423205010, 2020-01-01 to 2023-07-31 (36 records)"
    )

    s_all = ResultSummary(
        kind_label="monitoring locations",
        subject="calcium",
        scope="Wake County, NC",
        unit_label="stations",
        total_available=1200,
        retrieved=1200,
    )
    assert artifact_description(s_all) == (
        "USGS monitoring locations - calcium at Wake County, NC, all dates (1,200 stations)"
    )


def test_build_reply_text_inlines_when_under_limit():
    items = [f"| 2023-01-0{i} | {10 + i} |" for i in range(1, 4)]
    s = ResultSummary(
        kind_label="discrete observations",
        subject="Calcium (00915), mg/l",
        scope="USGS-0423205010",
        unit_label="records",
        total_available=3,
        retrieved=3,
        interpretation="Interpreted date range as 2023-01-01 to 2023-01-31.",
        highlights=["Sample dates range from 2023-01-01 to 2023-01-03."],
        inline_header="| Date | Value |\n| :--- | :--- |",
        inline_items=items,
        notes=["Modality: discrete sample measurements (not continuous sensors)."],
    )
    desc = artifact_description(s)
    text = build_reply_text(s, desc)

    assert "Retrieved **all 3** records of Calcium (00915), mg/l at **USGS-0423205010**." in text
    assert "This is the complete result; nothing was truncated." in text
    assert "Interpreted date range as 2023-01-01 to 2023-01-31." in text
    assert "Sample dates range from 2023-01-01 to 2023-01-03." in text
    for item in items:
        assert item in text
    assert f'All 3 records are in the artifact created in this step: "{desc}".' in text
    assert "will not return additional" not in text
    assert "Modality: discrete sample measurements" in text


def test_build_reply_text_omits_inline_when_over_limit():
    items = [f"- `USGS-{i:08d}`" for i in range(1, 61)]
    s = ResultSummary(
        kind_label="monitoring locations",
        subject="calcium",
        scope="North Carolina",
        unit_label="stations",
        total_available=60,
        retrieved=60,
        inline_header="Monitoring locations:",
        inline_items=items,
    )
    desc = artifact_description(s)
    text = build_reply_text(s, desc)

    assert "Retrieved **all 60** stations of calcium at **North Carolina**." in text
    assert "This is the complete result; nothing was truncated." in text
    assert "USGS-00000001" not in text
    assert "USGS-00000060" not in text
    assert "Monitoring locations:" not in text
    assert "will not return additional stations" in text
    assert desc in text


def test_build_reply_text_incomplete_result():
    s = ResultSummary(
        kind_label="discrete observations",
        subject="Dissolved oxygen (00300)",
        scope="USGS-01646500",
        unit_label="records",
        total_available=9,
        retrieved=5,
        warning="Some date ranges could not be retrieved",
        inline_items=["row1", "row2", "row3", "row4", "row5"],
    )
    desc = artifact_description(s)
    text = build_reply_text(s, desc)

    assert s.complete is False
    assert "Retrieved **5 of 9** records" in text
    assert "The result is incomplete: Some date ranges could not be retrieved." in text
    assert "This is the complete result" not in text
    # Incomplete results never inline items
    assert "row1" not in text
    assert "will not return additional records" in text


def test_magicmock_warning_is_ignored():
    mock_res = MagicMock()
    s = ResultSummary(
        kind_label="discrete observations",
        subject="Calcium (00915)",
        scope="USGS-0423205010",
        unit_label="records",
        total_available=3,
        retrieved=3,
        warning=mock_res.warning,
        inline_items=["r1", "r2", "r3"],
    )
    assert s.warning is None
    assert s.complete is True
    text = build_reply_text(s, artifact_description(s))
    assert "This is the complete result; nothing was truncated." in text
    assert "r1" in text


@pytest.mark.asyncio
async def test_send_result_creates_artifact_and_sends_structured_reply():
    context = MagicMock()
    context.reply = AsyncMock()
    process = MagicMock()
    process.create_artifact = AsyncMock()
    plan = MagicMock()
    plan.model_dump.return_value = {"intent": "observation", "filters": {"parameter_code": "00915"}}

    s = ResultSummary(
        kind_label="discrete observations",
        subject="Calcium (00915)",
        scope="USGS-0423205010",
        unit_label="records",
        total_available=2,
        retrieved=2,
        inline_items=["row-1", "row-2"],
    )
    desc = await send_result(
        context,
        process,
        plan,
        s,
        artifact_content={"records": [1, 2]},
        artifact_metadata={"count": 2},
    )

    assert desc == artifact_description(s)
    assert process.create_artifact.called
    art_kwargs = process.create_artifact.call_args.kwargs
    assert art_kwargs["description"] == desc
    assert art_kwargs["metadata"]["description"] == desc
    assert art_kwargs["metadata"]["count"] == 2

    assert context.reply.called
    reply_text = context.reply.call_args[0][0]
    reply_data = context.reply.call_args.kwargs["data"]
    assert desc in reply_text
    assert reply_data["intent"] == "observation"
    assert reply_data["result"] == {
        "total_available": 2,
        "retrieved": 2,
        "complete": True,
        "artifact_description": desc,
        "items_inlined": True,
    }

