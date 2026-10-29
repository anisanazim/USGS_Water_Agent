from __future__ import annotations

import asyncio
import logging
import time
import pytest

from water_agent.tracing import (
    SpanStatus,
    TraceLogFilter,
    get_current_span,
    get_current_trace_id,
    start_span,
)


@pytest.mark.asyncio
async def test_span_generation_and_timing():
    async with start_span("database_query", attributes={"db.system": "postgresql"}) as span:
        assert span.name == "database_query"
        assert span.attributes["db.system"] == "postgresql"
        assert span.trace_id is not None
        assert span.span_id is not None
        assert get_current_trace_id() == span.trace_id
        assert get_current_span() == span
        await asyncio.sleep(0.02)

    assert span.end_time is not None
    assert span.duration_ms is not None
    assert span.duration_ms >= 15.0
    assert span.status == SpanStatus.OK


@pytest.mark.asyncio
async def test_distributed_trace_and_parent_propagation():
    async with start_span("parent_job", trace_id="custom-trace-1234567890abcdef") as root:
        assert root.trace_id == "custom-trace-1234567890abcdef"
        assert root.parent_span_id is None

        async with start_span("child_task") as child:
            # Child must inherit parent trace_id and point to parent span_id
            assert child.trace_id == root.trace_id
            assert child.parent_span_id == root.span_id

            async with start_span("grandchild_task") as grandchild:
                assert grandchild.trace_id == root.trace_id
                assert grandchild.parent_span_id == child.span_id

    # Outside span context, contextvars reset
    assert get_current_span() is None
    assert get_current_trace_id() is None


@pytest.mark.asyncio
async def test_span_error_recording():
    try:
        async with start_span("failing_operation") as span:
            raise ValueError("Upstream payload invalid")
    except ValueError:
        pass

    assert span.status == SpanStatus.ERROR
    assert span.status_message == "Upstream payload invalid"
    assert span.attributes["error"] is True
    assert span.attributes["error.type"] == "ValueError"
    assert span.attributes["error.message"] == "Upstream payload invalid"


def test_span_otel_export():
    with start_span("http_call", attributes={"http.method": "GET"}) as span:
        span.add_event("cache_miss", {"reason": "expired"})
        time.sleep(0.01)

    otel_data = span.to_otel_dict()
    assert otel_data["name"] == "http_call"
    assert otel_data["context"]["trace_id"] == span.trace_id
    assert otel_data["context"]["span_id"] == span.span_id
    assert "start_time_unix_nano" in otel_data
    assert "end_time_unix_nano" in otel_data
    assert otel_data["duration_ms"] > 0
    assert len(otel_data["events"]) == 1
    assert otel_data["events"][0]["name"] == "cache_miss"


def test_trace_logging_filter():
    log_filter = TraceLogFilter()
    record = logging.LogRecord("test", logging.INFO, "path", 10, "msg", (), None)

    # When no span active
    log_filter.filter(record)
    assert record.trace_id == "-"
    assert record.span_id == "-"

    # Inside a span
    with start_span("logged_step") as span:
        log_filter.filter(record)
        assert record.trace_id == span.trace_id
        assert record.span_id == span.span_id
