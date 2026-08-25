from __future__ import annotations

import json
import logging
from typing import Any, Optional

from ichatbio.agent import IChatBioAgent
from ichatbio.agent_response import ResponseContext
from ichatbio.server import build_agent_app
from ichatbio.types import AgentCard, AgentEntrypoint
from pydantic import BaseModel

from usgs_water_data import (
    USGSNotFoundError,
    USGSServiceError,
    USGSValidationError,
    USGSWaterDataFetcher,
)
from water_agent.query_planner import QueryPlanner

logger = logging.getLogger(__name__)

ANSWER_DAILY_QUERY_DESCRIPTION = """\
Answers a natural-language question about USGS Daily water data by \
resolving it to the exact Daily API filters (e.g. parameter_code, \
statistic_id, state_code) using USGS's own field and reference metadata - \
no hardcoded keyword mappings - then querying the Daily API and returning \
the most recent matching observation(s). Asks a clarifying question \
instead if the request is ambiguous. \
Example: "Show me the maximum streamflow in North Carolina" resolves to \
parameter_code=00060, statistic_id=00001, state_code=37 and returns the \
most recent matching daily reading.
"""

DAILY_VALUES_LIMIT = 5


class USGSWaterAgent(IChatBioAgent):
    def __init__(self):
        super().__init__()
        self.fetcher = USGSWaterDataFetcher()
        self._query_planner: Optional[QueryPlanner] = None

    @property
    def query_planner(self) -> QueryPlanner:
        # Built lazily so instantiating the agent (e.g. in tests) doesn't
        # require an LLM API key to be configured.
        if self._query_planner is None:
            self._query_planner = QueryPlanner()
        return self._query_planner

    def get_agent_card(self) -> AgentCard:
        return AgentCard(
            name="USGS Water Data Agent",
            description="Answer natural-language questions about USGS Daily water monitoring data by resolving them to exact USGS query filters and fetching real observations.",
            icon="https://waterdata.usgs.gov/favicon.ico",
            entrypoints=[
                AgentEntrypoint(
                    id="answer_daily_query",
                    description=ANSWER_DAILY_QUERY_DESCRIPTION,
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
        print(f"[DEBUG][Agent] run() entrypoint={entrypoint}, request={request!r}, params={params}")
        handler = self._entrypoint_handlers.get(entrypoint)
        if handler is None:
            print(f"[DEBUG][Agent] Unknown entrypoint: {entrypoint}")
            await context.reply(f"Unknown entrypoint: {entrypoint}")
            return

        try:
            await handler(context, request, params)

        except USGSValidationError as exc:
            logger.info("Validation error for request %r: %s", request, exc)
            await context.reply(f"I couldn't process that request: {exc}")

        except USGSNotFoundError as exc:
            logger.info("No data found for request %r: %s", request, exc)
            await context.reply(f"I couldn't find any USGS data for that request: {exc}")

        except USGSServiceError as exc:
            logger.error("USGS service error for request %r: %s", request, exc)
            await context.reply(
                "The USGS water data service is currently unavailable or timed out. Please try again shortly."
            )

        except Exception:
            logger.exception("Unexpected error handling request %r", request)
            await context.reply(
                "Something went wrong while processing your request. This has been logged for investigation."
            )

    @property
    def _entrypoint_handlers(self):
        return {
            "answer_daily_query": self._handle_answer_daily_query,
        }

    async def _handle_answer_daily_query(
        self, context: ResponseContext, request: str, params: Optional[BaseModel]
    ) -> None:
        async with context.begin_process("Answering USGS Daily data question") as process:
            await process.log(f"Request received: {request!r}")

            plan = await self.query_planner.plan(request)

            if plan.semantic:
                await process.log(
                    f"Semantic interpretation: {plan.semantic}", data=plan.semantic
                )

            if plan.clarification_needed:
                if plan.ambiguous_candidates:
                    await process.log(
                        f"Ambiguous candidates considered: {', '.join(plan.ambiguous_candidates)}",
                        data={"candidates": plan.ambiguous_candidates},
                    )
                await process.log(f"Clarification needed: {plan.clarification_needed}")
                await context.reply(plan.clarification_needed, data=plan.model_dump())
                return

            if not plan.filters:
                await process.log("No Daily API filters could be resolved.")
                await context.reply(
                    "I couldn't resolve any Daily API filters for that question.",
                    data=plan.model_dump(),
                )
                return

            await process.log(
                f"Resolved parameters: {plan.filters}", data=plan.filters
            )

            result = await self.fetcher.fetch_daily_values(
                plan.filters, limit=DAILY_VALUES_LIMIT
            )

            await process.log(
                f'Final request URL: "{result["request_url"]}"',
                data={"url": result["request_url"]},
            )

            if not result["features"]:
                await process.log("No matching Daily observations found.")
                await context.reply(
                    "I resolved the filters for your question, but found no "
                    "matching USGS Daily observations.",
                    data=plan.model_dump(),
                )
                return

            readings = [
                self._normalize_daily_feature(feature)
                for feature in result["features"]
            ]
            await process.log(
                f"Found {len(readings)} matching observation(s); "
                f"most recent is from {readings[0]['time']}."
            )

            await process.create_artifact(
                mimetype="application/json",
                description="USGS Daily observations",
                content=json.dumps(
                    {"filters": plan.filters, "readings": readings}, indent=2
                ).encode("utf-8"),
                metadata={"filters": plan.filters, "count": len(readings)},
            )

        latest = readings[0]
        await context.reply(
            f"Most recent matching USGS Daily observation: {latest['value']} "
            f"{latest['unit']} on {latest['time']} at "
            f"{latest['monitoring_location_id']}.",
            data={"filters": plan.filters},
        )

    @staticmethod
    def _normalize_daily_feature(feature: dict[str, Any]) -> dict[str, Any]:
        properties = feature.get("properties", {})
        return {
            "monitoring_location_id": properties.get("monitoring_location_id"),
            "parameter_code": properties.get("parameter_code"),
            "statistic_id": properties.get("statistic_id"),
            "value": properties.get("value"),
            "unit": properties.get("unit_of_measure"),
            "time": properties.get("time"),
            "approval_status": properties.get("approval_status"),
        }


def create_app():
    return build_agent_app(USGSWaterAgent())
