import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from agent import USGSWaterAgent
from water_agent.config import settings
from usgs_water_data import USGSServiceError, USGSValidationError, USGSNotFoundError
from water_agent.fetchers.samples_fetcher import SamplesError


@pytest.fixture
def mock_context():
    ctx = MagicMock()
    ctx.reply = AsyncMock()
    return ctx


@pytest.mark.asyncio
async def test_deadline_timeout_slow_usgs_reply_and_log(mock_context, caplog):
    agent = USGSWaterAgent()

    async def slow_handler(context, request, params):
        await asyncio.sleep(0.5)

    mock_warmup = MagicMock()
    with patch.object(agent, "_handle_query_water_data", slow_handler):
        with patch("water_agent.config.settings.request_deadline_seconds", 0.05):
            with patch("agent._trigger_warmup", mock_warmup):
                with caplog.at_level(logging.INFO):
                    await agent.run(mock_context, "test request", "query_water_data", None)

    # Exactly one reply sent
    assert mock_context.reply.call_count == 1
    reply_msg = mock_context.reply.call_args[0][0]
    assert "USGS is waking up or responding slowly right now" in reply_msg
    assert "Please ask again in a minute or two." in reply_msg

    # Summary log line has outcome=deadline
    summary_logs = [r.message for r in caplog.records if "outcome=" in r.message]
    assert len(summary_logs) >= 1
    assert "outcome=deadline" in summary_logs[-1]
    assert "elapsed_s=" in summary_logs[-1]

    # Warmup trigger called
    assert mock_warmup.call_count == 1


@pytest.mark.asyncio
async def test_cancelled_error_propagates(mock_context):
    agent = USGSWaterAgent()

    async def cancelling_handler(context, request, params):
        raise asyncio.CancelledError()

    with patch.object(agent, "_handle_query_water_data", cancelling_handler):
        with pytest.raises(asyncio.CancelledError):
            await agent.run(mock_context, "test request", "query_water_data", None)


@pytest.mark.asyncio
async def test_error_replies_never_end_with_colon_space(mock_context):
    agent = USGSWaterAgent()

    async def empty_error_handler(context, request, params):
        raise USGSServiceError("")

    with patch.object(agent, "_handle_query_water_data", empty_error_handler):
        await agent.run(mock_context, "test request", "query_water_data", None)
    assert mock_context.reply.call_count == 1
    reply_msg = mock_context.reply.call_args[0][0]
    assert not reply_msg.strip().endswith(":")
    assert not reply_msg.endswith(": ")


@pytest.mark.asyncio
async def test_successful_request_summary_log(mock_context, caplog):
    agent = USGSWaterAgent()

    async def ok_handler(context, request, params):
        await context.reply("Data found!")

    with patch.object(agent, "_handle_query_water_data", ok_handler):
        with caplog.at_level(logging.INFO):
            await agent.run(mock_context, "test request", "query_water_data", None)

    assert mock_context.reply.call_count == 1
    summary_logs = [r.message for r in caplog.records if "outcome=" in r.message]
    assert len(summary_logs) >= 1
    assert "outcome=ok" in summary_logs[-1]

