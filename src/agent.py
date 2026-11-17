from __future__ import annotations

import asyncio
from contextvars import ContextVar
import logging
import math
import time
import uuid
from typing import Any, Optional

import httpx
from ichatbio.agent import IChatBioAgent
from ichatbio.agent_response import ResponseContext
from ichatbio.server import build_agent_app
from ichatbio.types import AgentCard, AgentEntrypoint
from pydantic import BaseModel

from usgs_water_data import (
    USGSNotFoundError,
    USGSServiceError,
    USGSValidationError,
    USGSWaterDataFetcher as _RealUSGSWaterDataFetcher,
)
from water_agent.circuit_breaker import CircuitBreakerOpenError
from water_agent.config import settings
from water_agent.fetchers.samples_fetcher import (
    SamplesError,
    SamplesRateLimitError,
    SamplesValidationError,
    USGSSamplesFetcher as _RealUSGSSamplesFetcher,
)

USGSWaterDataFetcher = _RealUSGSWaterDataFetcher
USGSSamplesFetcher = _RealUSGSSamplesFetcher
from water_agent.handlers import (
    format_date_for_display,
    handle_clarification,
    handle_inventory,
    handle_parameter_count,
    handle_observation,
    handle_spatial,
)
from water_agent.heartbeat import heartbeat
from water_agent.metadata.models import QueryIntent
from water_agent.query_planner import QueryPlanner
from water_agent.results import RequestContext

logger = logging.getLogger(__name__)

current_intent: ContextVar[str] = ContextVar("current_intent", default="unknown")


SLOW_USGS_MESSAGE = (
    "USGS has been unresponsive for several minutes. Please try again later."
)


def _is_timeout(exc: BaseException, max_depth: int = 5) -> bool:
    """Walk the __cause__/__context__ chain (max depth 5) looking for httpx.TimeoutException."""
    cur: BaseException | None = exc
    seen: set[int] = set()
    for _ in range(max_depth + 1):
        if cur is None or id(cur) in seen:
            break
        seen.add(id(cur))
        if isinstance(cur, httpx.TimeoutException):
            return True
        cur = cur.__cause__ if cur.__cause__ is not None else cur.__context__
    return False


def _trigger_warmup() -> None:
    try:
        from water_agent.keepwarm import last_samples_target, request_warmup
        target = last_samples_target.get()
        if target:
            request_warmup(*target)
    except Exception:
        pass

QUERY_WATER_DATA_DESCRIPTION = """\
Answers natural-language questions about USGS water monitoring data across both \
continuous automated sensor feeds (streamflow, gage height, temperature) and \
discrete laboratory water quality samples (calcium, nutrients, metals, pH, etc.). \
Supports spatial station discovery, dual-modality station inventories, parameter observation \
counts, and discrete observation values using USGS metadata and an LLM planning loop. \
Examples: "Where is calcium monitored in North Carolina?", "What information is available at \
USGS-0423205010?", "How many phosphorus records are at USGS-0423205010?", or "What is the \
maximum streamflow in North Carolina?".
"""
ANSWER_DAILY_QUERY_DESCRIPTION = QUERY_WATER_DATA_DESCRIPTION


class USGSWaterAgent(IChatBioAgent):
    def __init__(
        self,
        query_planner: Optional[QueryPlanner] = None,
        samples_fetcher: Optional[USGSSamplesFetcher] = None,
        water_data_fetcher: Optional[USGSWaterDataFetcher] = None,
    ):
        super().__init__()
        self._query_planner = query_planner
        self._samples_fetcher = samples_fetcher
        self._water_data_fetcher = water_data_fetcher
        self._owns_fetchers = (samples_fetcher is None and water_data_fetcher is None)

    @property
    def samples_fetcher(self) -> _RealUSGSSamplesFetcher:
        import sys
        agent_mod = sys.modules.get("agent")
        cls = getattr(agent_mod, "USGSSamplesFetcher", _RealUSGSSamplesFetcher) if agent_mod else _RealUSGSSamplesFetcher
        if cls is not _RealUSGSSamplesFetcher:
            return cls()
        if self._samples_fetcher is None:
            self._samples_fetcher = _RealUSGSSamplesFetcher()
        return self._samples_fetcher

    @property
    def water_data_fetcher(self) -> _RealUSGSWaterDataFetcher:
        import sys
        agent_mod = sys.modules.get("agent")
        cls = getattr(agent_mod, "USGSWaterDataFetcher", _RealUSGSWaterDataFetcher) if agent_mod else _RealUSGSWaterDataFetcher
        if cls is not _RealUSGSWaterDataFetcher:
            return cls()
        if self._water_data_fetcher is None:
            self._water_data_fetcher = _RealUSGSWaterDataFetcher()
        return self._water_data_fetcher

    @property
    def query_planner(self) -> QueryPlanner:
        # Built lazily so instantiating the agent (e.g. in tests) doesn't
        # require an LLM API key to be configured.
        if self._query_planner is None:
            self._query_planner = QueryPlanner(
                water_data_fetcher=self.water_data_fetcher,
                samples_fetcher=self.samples_fetcher,
            )
        return self._query_planner

    async def aclose(self) -> None:
        """Close shared underlying HTTP clients if owned."""
        if self._owns_fetchers:
            await self.samples_fetcher.aclose()
            await self.water_data_fetcher.aclose()
        if self._query_planner is not None:
            await self._query_planner.aclose()

    async def check_health(self) -> Any:
        """Perform diagnostic health and readiness probe on all upstream dependencies."""
        from water_agent.health import HealthChecker
        checker = HealthChecker(
            samples_fetcher=self.samples_fetcher,
            water_data_fetcher=self.water_data_fetcher,
        )
        return await checker.run_checks()

    def get_agent_card(self) -> AgentCard:
        return AgentCard(
            name="USGS Water Data Agent",
            description="Answer natural-language questions about USGS continuous sensor and discrete laboratory water monitoring data by resolving them to exact USGS query filters and fetching real observations.",
            icon="https://waterdata.usgs.gov/favicon.ico",
            entrypoints=[
                AgentEntrypoint(
                    id="query_water_data",
                    description=QUERY_WATER_DATA_DESCRIPTION,
                    parameters=None,
                ),
            ],
        )

    async def run(
        self,
        context: ResponseContext,
        request: str,
        entrypoint: str,
        params: Optional[BaseModel],
        metadata: dict[str, Any] | None = None,
    ):
        logger.debug("run() entrypoint=%s, request=%r, params=%s", entrypoint, request, params)
        start_time = time.monotonic()
        request_id = (metadata or {}).get("request_id") or str(uuid.uuid4())
        intent = "unknown"
        outcome = "ok"
        current_intent.set("unknown")
        try:
            from water_agent.keepwarm import last_samples_target, start_keepwarm_loop
            last_samples_target.set(None)
            if settings.keepwarm_enabled:
                start_keepwarm_loop()
        except Exception:
            pass

        try:
            handler = self._entrypoint_handlers.get(entrypoint)
            if handler is None:
                logger.warning("Unknown entrypoint: %s", entrypoint)
                outcome = "error"
                await context.reply(f"Unknown entrypoint: {entrypoint}")
                return

            async with asyncio.timeout(settings.request_deadline_seconds):
                await handler(context, request, params)

        except TimeoutError:
            outcome = "deadline"
            _trigger_warmup()
            await context.reply(SLOW_USGS_MESSAGE)

        except (USGSValidationError, SamplesValidationError) as exc:
            outcome = "error"
            logger.info("Validation error for request %r: %s", request, exc)
            err_msg = str(exc).strip()
            reason = f": {err_msg}" if err_msg else ""
            await context.reply(f"I couldn't process that request{reason}")

        except USGSNotFoundError as exc:
            outcome = "error"
            logger.info("No data found for request %r: %s", request, exc)
            err_msg = str(exc).strip()
            reason = f": {err_msg}" if err_msg else ""
            await context.reply(f"I couldn't find any USGS data for that request{reason}")

        except SamplesRateLimitError as exc:
            outcome = "error"
            logger.warning("USGS Samples API rate limit reached for request %r: %s", request, exc)
            await context.reply(
                "The USGS API rate limit was temporarily reached. Please try your request again in a few moments."
            )

        except (USGSServiceError, SamplesError) as exc:
            if _is_timeout(exc):
                outcome = "deadline"
                _trigger_warmup()
                await context.reply(SLOW_USGS_MESSAGE)
            else:
                outcome = "error"
                logger.error("USGS service error for request %r: %s", request, exc)
                err_msg = str(exc).strip()
                reason = f": {err_msg}" if err_msg else ""
                await context.reply(
                    f"The USGS service encountered an error processing that request{reason}"
                )

        except CircuitBreakerOpenError as exc:
            outcome = "deadline"
            _trigger_warmup()
            logger.warning("Circuit breaker open for request %r: %s", request, exc)
            if exc.retry_after > 0:
                reply_msg = f"{SLOW_USGS_MESSAGE} (try again in about {math.ceil(exc.retry_after)} s)"
            else:
                reply_msg = SLOW_USGS_MESSAGE
            await context.reply(reply_msg)

        except asyncio.CancelledError:
            raise

        except Exception:
            outcome = "error"
            logger.exception("Unexpected error handling request %r", request)
            await context.reply(
                "Something went wrong while processing your request. This has been logged for investigation."
            )

        finally:
            elapsed_s = time.monotonic() - start_time
            resolved_intent = current_intent.get()
            if resolved_intent and resolved_intent != "unknown":
                intent = resolved_intent
            logger.info(
                "request_id=%s intent=%s elapsed_s=%.1f outcome=%s",
                request_id, intent, elapsed_s, outcome
            )

    @property
    def _entrypoint_handlers(self):
        return {
            "query_water_data": self._handle_query_water_data,
            "answer_daily_query": self._handle_query_water_data,  # Backward-compatible alias
        }

    async def _handle_query_water_data(
        self, context: ResponseContext, request: str, params: Optional[BaseModel]
    ) -> None:
        async with context.begin_process("Processing USGS water data query") as process:
            await process.log(f"Request received: {request!r}")

            ctx = RequestContext(request_id=str(uuid.uuid4()))
            ctx.ichatbio_process = process
            plan = await self.query_planner.plan(request, ctx)

            if plan.intent:
                intent_val = plan.intent.value if hasattr(plan.intent, "value") else str(plan.intent)
                current_intent.set(intent_val)

            if plan.semantic:
                await process.log(
                    f"Semantic interpretation: {plan.semantic}", data=plan.semantic
                )

            async with heartbeat(process):
                # Intent-Driven Router across the 5 core workflows + clarification
                # 1. Clarification & Disambiguation (Workflow 6)
                if plan.intent == QueryIntent.CLARIFICATION or plan.clarification_needed:
                    await handle_clarification(context, process, plan, ctx, request)
                    return

                # 2. Spatial Station Discovery (Workflow 1)
                if plan.intent == QueryIntent.SPATIAL_DISCOVERY or plan.stations_ref:
                    await handle_spatial(context, process, plan, ctx)
                    return

                # 3. Station Parameter Counts (Workflow 3)
                if plan.intent == QueryIntent.STATION_PARAMETER_COUNT:
                    await handle_parameter_count(
                        context, process, plan, ctx, request, samples_fetcher=self.samples_fetcher
                    )
                    return

                # 4. Station Inventory (Workflow 2)
                if (
                    plan.intent == QueryIntent.STATION_INVENTORY
                    or getattr(plan, "inventory_ref", None)
                ):
                    await handle_inventory(context, process, plan, ctx, request)
                    return

                # 4. Observation Values (Workflows 4 & 5: Discrete & Continuous)
                if plan.intent == QueryIntent.OBSERVATION or plan.filters:
                    await handle_observation(
                        context,
                        process,
                        plan,
                        ctx,
                        samples_fetcher=self.samples_fetcher,
                        water_data_fetcher=self.water_data_fetcher,
                    )
                    return

                # Fallback guardrail
                await handle_clarification(context, process, plan, ctx, request)

    # Backward-compatible handler alias
    _handle_answer_daily_query = _handle_query_water_data


def create_app():
    from contextlib import asynccontextmanager

    from water_agent.keepwarm import start_keepwarm_loop, stop_keepwarm_loop

    agent = USGSWaterAgent()
    app = build_agent_app(agent)

    inner_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(scope_app):
        if settings.keepwarm_enabled:
            start_keepwarm_loop()
        try:
            async with inner_lifespan(scope_app) as state:
                yield state
        finally:
            stop_keepwarm_loop()

    app.router.lifespan_context = lifespan
    return app