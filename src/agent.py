from __future__ import annotations

import json
import logging
from typing import Any, Optional

import httpx
from ichatbio.agent import IChatBioAgent
from ichatbio.agent_response import ResponseContext
from ichatbio.server import build_agent_app
from ichatbio.types import AgentCard, AgentEntrypoint, Artifact
from pydantic import BaseModel, Field

from pipeline import (
    ParameterResolver,
    StructuredQuery,
    USGSDataExecutor,
    USGSRequestRouter,
    USGSResponseNormalizer,
    WaterQueryParser,
)
from usgs_water_data import (
    USGSNotFoundError,
    USGSServiceError,
    USGSValidationError,
    USGSWaterDataFetcher,
)

logger = logging.getLogger(__name__)

MAX_ARTIFACT_CHARS = 4000
ARTIFACT_FETCH_TIMEOUT_SECONDS = 15.0


class WaterQueryParams(BaseModel):
    site_id: str = Field(default="", description="USGS monitoring site ID, such as 01646500 or USGS-01646500")
    parameter_codes: list[str] = Field(default_factory=lambda: ["00060"], description="USGS parameter codes, e.g. ['00060', '00065']")
    locations_artifact: Optional[Artifact] = Field(default=None)


DESCRIPTION = """\
This agent fetches the daily USGS water monitoring observations for a single site or a state-level query.
It supports direct natural-language questions like:
- What is the latest discharge at site 02329000?
- Give me the most recent gage height at USGS-10172640
- Show the latest water temperature in Florida
- What's the streamflow and water temperature at site 01646500?
"""


class USGSWaterAgent(IChatBioAgent):
    def __init__(self):
        super().__init__()
        self.fetcher = USGSWaterDataFetcher()

    def _parse_question(self, request: str) -> StructuredQuery:
        parser = WaterQueryParser()
        return parser.parse(request)

    def _build_usgs_request(self, structured_query: StructuredQuery) -> dict[str, Any]:
        router = USGSRequestRouter(self.fetcher)
        return router.build_request(structured_query)

    async def _execute_usgs_query(self, request_params: dict[str, Any]) -> dict[str, Any]:
        executor = USGSDataExecutor(self.fetcher)
        return await executor.execute(request_params)

    def _normalize_response(self, raw_response: dict[str, Any], structured_query: StructuredQuery) -> tuple[dict[str, Any], str]:
        normalizer = USGSResponseNormalizer()
        return normalizer.normalize(raw_response, structured_query)

    def get_agent_card(self) -> AgentCard:
        return AgentCard(
            name="USGS Water Data Agent",
            description="Fetch the daily USGS water monitoring observations using site IDs or state-level natural-language questions.",
            icon="https://waterdata.usgs.gov/favicon.ico",
            entrypoints=[
                AgentEntrypoint(
                    id="enrich_water_locations",
                    description=DESCRIPTION,
                    parameters=WaterQueryParams,
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
            "enrich_water_locations": self._handle_enrich_water_locations,
        }

    async def _handle_enrich_water_locations(
        self, context: ResponseContext, request: str, params: Optional[BaseModel]
    ) -> None:
        print(f"[DEBUG][Agent] Handling enrich_water_locations request={request!r}")
        structured_query = self._resolve_structured_query(request, params)
        print(f"[DEBUG][Agent] Resolved structured_query before parameter resolver: {structured_query.as_dict()}")

        resolver = ParameterResolver()
        structured_query = resolver.resolve(structured_query)
        print(f"[DEBUG][Agent] Resolved structured_query after parameter resolver: {structured_query.as_dict()}")

        if not structured_query.site_id and not structured_query.state_name:
            print("[DEBUG][Agent] Missing site_id/state_name; returning user-facing validation message")
            await context.reply(
                "I can answer site-specific USGS questions, but I need a site ID or a US state name."
            )
            return

        request_params = self._build_usgs_request(structured_query)
        print(f"[DEBUG][Agent] Calling USGS executor with request_params={request_params}")
        raw_response = await self._execute_usgs_query(request_params)
        print(f"[DEBUG][Agent] Raw USGS response count={raw_response.get('count')} site_id={raw_response.get('site_id')}")
        artifact_payload, summary = self._normalize_response(raw_response, structured_query)
        print(f"[DEBUG][Agent] Normalized summary={summary}")

        if self._has_locations_artifact(params):
            print("[DEBUG][Agent] Merging with locations artifact")
            await self._reply_with_enriched_artifact(context, params, artifact_payload, summary)
            return

        print("[DEBUG][Agent] Replying with summary + JSON artifact")
        await context.reply(summary)
        await self._reply_json(context, artifact_payload)

    def _resolve_structured_query(self, request: str, params: Optional[BaseModel]) -> StructuredQuery:
        # If params exist but site_id is empty, fall back to natural-language parsing
        if params is not None and isinstance(params, WaterQueryParams):
            if params.site_id.strip():
                codes = list(params.parameter_codes) if params.parameter_codes else ["00060"]
                return StructuredQuery(
                    site_id=params.site_id.strip(),
                    state_name=None,
                    parameter_codes=codes,
                    parameter_names=["user-provided"] * len(codes),
                    raw_request=request,
                )
            # fallback to natural-language parsing
        return self._parse_question(request)


    @staticmethod
    def _has_locations_artifact(params: Optional[BaseModel]) -> bool:
        return (
            params is not None
            and isinstance(params, WaterQueryParams)
            and params.locations_artifact is not None
        )

    async def _reply_with_enriched_artifact(
        self,
        context: ResponseContext,
        params: WaterQueryParams,
        artifact_payload: dict[str, Any],
        summary: str,
    ) -> None:
        try:
            artifact_content = await self._read_artifact(params.locations_artifact)
        except Exception as exc:
            logger.warning("Failed to read locations_artifact: %s", exc)
            await context.reply(
                "I fetched the USGS data, but couldn't read the provided locations artifact to merge it in."
            )
            await context.reply(summary)
            await self._reply_json(context, artifact_payload)
            return

        if not isinstance(artifact_content, list):
            await context.reply(
                "The provided locations artifact wasn't a list of records, so I couldn't merge USGS data into it."
            )
            await context.reply(summary)
            await self._reply_json(context, artifact_payload)
            return

        enriched = []
        for item in artifact_content:
            if isinstance(item, dict):
                enriched_item = dict(item)
                enriched_item["usgsWaterProperties"] = artifact_payload
                enriched.append(enriched_item)

        await context.reply(summary)
        await self._reply_json(context, enriched)

    async def _reply_json(self, context: ResponseContext, payload: Any) -> None:
        """Reply with valid JSON, never truncating mid-string. If the
        payload is too large, trim the underlying data (fewer readings)
        rather than slicing the serialized text, which can produce
        invalid JSON.
        """
        serialized = json.dumps(payload, indent=2)
        if len(serialized) <= MAX_ARTIFACT_CHARS:
            await context.reply(serialized)
            return

        trimmed: Any = payload
        if isinstance(payload, dict) and isinstance(payload.get("readings"), list):
            readings = payload["readings"]
            trimmed = dict(payload)
            trimmed["readings"] = readings[:5]
            trimmed["_note"] = f"showing 5 of {len(readings)} readings; full payload was too large to display inline"
            serialized = json.dumps(trimmed, indent=2)
        elif isinstance(payload, list):
            trimmed = payload[:5]
            serialized = json.dumps(
                {"_note": f"showing 5 of {len(payload)} records; full payload was too large to display inline",
                 "items": trimmed},
                indent=2,
            )

        if len(serialized) > MAX_ARTIFACT_CHARS:
            serialized = json.dumps(
                {"note": "Result payload too large to display inline."}, indent=2
            )

        await context.reply(serialized)

    async def _read_artifact(self, artifact: Artifact) -> Any:
        async with httpx.AsyncClient(follow_redirects=True, timeout=ARTIFACT_FETCH_TIMEOUT_SECONDS) as client:
            last_error: Exception | None = None
            for url in artifact.get_urls():
                try:
                    response = await client.get(url)
                except httpx.HTTPError as exc:
                    last_error = exc
                    continue
                if response.is_success:
                    try:
                        return response.json()
                    except ValueError as exc:
                        last_error = exc
                        continue
        raise ValueError(f"Artifact content could not be retrieved: {last_error}")

def create_app():
    return build_agent_app(USGSWaterAgent())