from __future__ import annotations

import json
import os
import re
from pathlib import Path

from dotenv import load_dotenv
from openai import AsyncOpenAI
from pydantic import BaseModel, Field

load_dotenv()

from water_agent.metadata.references.store import ReferenceStore
from water_agent.metadata.registry import QueryableRegistry, daily_registry
from water_agent.results import RequestContext
from water_agent.metadata.resolver import ReferenceResolver
from water_agent.tools.queryable_search import QueryableSearchTool
from water_agent.tools.reference_resolve import ReferenceResolveTool
from water_agent.tools.reference_search import ReferenceSearchTool
from water_agent.tools.spatial_query import SpatialQueryTool
from water_agent.tools.station_inventory import StationInventoryTool
from usgs_water_data import USGSWaterDataFetcher

DEFAULT_REFERENCES_DIR = (
    Path(__file__).resolve().parents[2] / "metadata" / "raw" / "references"
)

MAX_TOOL_ITERATIONS = 8

SYSTEM_PROMPT = """\
You are the query planner for the USGS Water Data Agent. You classify natural-language questions
about USGS water monitoring data and plan them across 5 core workflows into an exact, typed JSON execution plan:

1. Spatial Station Discovery ("Where is parameter X monitored in [state/county]?")
   - Identifies which monitoring stations measure a parameter across a state or county.
   - Requires a geographic constraint (at least state_name). Call spatial_query(parameter_code, state_name, county_name).
   - If user asks where streamflow or discharge is monitored, resolve parameter_code="00060" before calling spatial_query.
   - If user asks "Where is X monitored?" WITHOUT a state or county, do NOT call spatial_query. Set intent="clarification" and ask them to narrow by state or county.
   - If spatial_query returns 0 stations, this is a definitive, valid discovery outcome (meaning no stations monitor that parameter in that geography). Do NOT retry or re-run spatial_query; immediately emit the spatial_discovery plan with the returned stations_ref.
   - Result plan: {"intent": "spatial_discovery", "modality": "discrete" | "continuous", "semantic": {...}, "filters": {...}, "stations_ref": "<ref from tool>"}

2. Dual-Modality Station Inventory ("What information/data is measured at station X?")
   - Produces a complete inventory of everything monitored at a specific USGS location.
   - Call station_inventory(monitoring_location_id). Do NOT specify a parameter_code filter.
   - Result plan: {"intent": "station_inventory", "semantic": {...}, "filters": {}, "inventory_ref": "<ref from tool>"}

3. Station Parameter Counts ("How many records of parameter X at station Y?")
   - Reports observation counts for a specific parameter or parameter family at an individual station.
   - Extract the monitoring_location_id (e.g. USGS-0423205010) and parameter/family into filters: {"monitoring_location_id": "...", "parameter_code": "..."}.
   - If user asks about a family (e.g. phosphorus, calcium, nitrate), set parameter_code="<family_name>" (e.g. "phosphorus").
   - If user asks about specific 5-digit code(s) (e.g. 00660), set parameter_code="<code_or_codes>".
   - Do NOT call search_reference. You may call station_inventory if helpful, or emit the plan directly.
   - Result plan: {"intent": "station_parameter_count", "semantic": {"station": "<station_id>", "parameter": "<param_or_family>"}, "filters": {"monitoring_location_id": "<station_id>", "parameter_code": "<param_or_family>"}}

4. Discrete Observation Values ("Show me the values/readings for X at station Y")
   - Retrieves actual laboratory water quality measurement records (e.g. calcium, nutrients, metals).
   - Resolve parameter_code and monitoring_location_id into filters. Do NOT call station_inventory.
   - Result plan: {"intent": "observation", "modality": "discrete", "semantic": {...}, "filters": {"parameter_code": "<pcode>", "monitoring_location_id": "<station_id>"}}

5. Continuous Daily Values (Hydrology & Streamflow)
   - Queries real-time and daily statistical observations from automated sensor stations (streamflow, gage height, temperature).
   - Questions like "What is the maximum streamflow in North Carolina?", "Show me the mean daily discharge at station X", or "What is the latest gage height at station X?" are CONTINUOUS OBSERVATIONS (intent="observation", modality="continuous"), NOT spatial_discovery.
   - Resolve parameter_code (streamflow/discharge -> 00060, gage height -> 00065), statistic_id (maximum -> 00001, mean -> 00003, minimum -> 00002), and location/state into filters.
   - For queries like "What is the maximum streamflow in North Carolina?", resolve parameter_code="00060", statistic_id="00001", state_code="37". Do NOT call spatial_query.
   - For "latest" or "recent" queries (e.g. "What is the latest gage height at USGS-02087500?"), default statistic_id="00003" (mean daily reading). Do not ask for clarification on "latest".
   - Result plan: {"intent": "observation", "modality": "continuous", "semantic": {...}, "filters": {"parameter_code": "<pcode>", "statistic_id": "<stat_id>", ...}}

6. Clarification & Guardrails:
   - If a question asks a general question without a station ID or geographic scope (e.g. "List parameters related to turbidity"), or if a parameter is ambiguous or outside retrievable metadata:
   - Result plan: {"intent": "clarification", "semantic": {...}, "filters": {}, "clarification_needed": "<polite explanation guiding user to provide a station or geographic area>"}

You have five tools:
- search_queryables(query): find which Daily field is relevant to a concept.
- search_reference(field_name, query): find candidate USGS values for a field by keyword.
- resolve_reference(field_name, value): deterministically resolve human-readable terms to USGS codes.
- spatial_query(parameter_code, state_name, county_name, huc): find stations in an area.
- station_inventory(monitoring_location_id): discover what is monitored at a station.

CRITICAL RULES:
- Never fabricate a USGS code yourself. Every code must come from tool results.
- For discrete parameters in verified families (e.g., phosphorus, calcium, nitrate, turbidity, etc.): pick the first resolved code; downstream presentation automatically handles all constituent codes in the family.
- If a tool returns a JSON object with a `ref` field, copy that `ref` string verbatim into your final JSON. Do not fabricate refs.
- Never retry a tool call with the same arguments if it returns 0 results or an empty list; 0 results is a valid, definitive discovery outcome.

Respond with ONLY a JSON object (no markdown code fences) of the form:
{"intent": "observation" | "spatial_discovery" | "station_inventory" | "station_parameter_count" | "clarification", "modality": "continuous" | "discrete" | "mixed", "semantic": {"<concept>": "<plain phrase>", ...}, "filters": {"<field_name>": "<resolved_value>", ...}, "stations_ref": "<ref from spatial_query or omit>", "inventory_ref": "<ref from station_inventory or omit>", "clarification_needed": null}
"""

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "search_queryables",
            "description": (
                "Search the Daily dataset's queryable fields by keyword to find "
                "which field is relevant to a concept mentioned by the user."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "A keyword or phrase describing the concept to search for, e.g. 'parameter' or 'state'.",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_reference",
            "description": (
                "Search the controlled reference values for a specific Daily "
                "field by keyword, to find candidate USGS values."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "field_name": {
                        "type": "string",
                        "description": "The Daily queryable field name, e.g. 'parameter_code', 'state_code', 'statistic_id'.",
                    },
                    "query": {
                        "type": "string",
                        "description": "A keyword describing the value to search for, ideally in USGS terminology.",
                    },
                },
                "required": ["field_name", "query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "resolve_reference",
            "description": (
                "Deterministically resolve one known value for a Daily field "
                "to its exact USGS code."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "field_name": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["field_name", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "spatial_query",
            "description": (
                "Discover monitoring locations that measure a specific parameter in a geographic area. "
                "Use this when the user asks 'Where is X monitored in Y?'. Returns a list of station IDs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "parameter_code": {
                        "type": "string",
                        "description": "The exact 5-digit parameter_code to search for (e.g. '00300'). MUST be resolved first.",
                    },
                    "state_name": {
                        "type": "string",
                        "description": "The full state name (e.g. 'Florida').",
                    },
                    "county_name": {
                        "type": "string",
                        "description": "Optional county name (e.g. 'Leon County').",
                    },
                    "huc": {
                        "type": "string",
                        "description": "Optional hydrologic unit code.",
                    },
                },
                "required": ["parameter_code", "state_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "station_inventory",
            "description": (
                "Discover what parameters/information are available at a specific monitoring location. "
                "Use this when the user asks 'What information is available at station X?' or 'How many records for parameter Y are at station X?'. "
                "Do NOT use this when the user asks to see, show, or retrieve actual data values/measurements. "
                "Returns a dictionary of parameters and their date ranges and counts."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "monitoring_location_id": {
                        "type": "string",
                        "description": "The exact USGS station ID (e.g. 'USGS-02238500').",
                    },
                },
                "required": ["monitoring_location_id"],
            },
        },
    },
]


from typing import Any, Optional
from pydantic import BaseModel, Field, field_validator, model_validator
from water_agent.metadata.models import ParameterModality, QueryFilters, QueryIntent

INTENT_ALIASES = {
    "spatial": QueryIntent.SPATIAL_DISCOVERY,
    "spatial_discovery": QueryIntent.SPATIAL_DISCOVERY,
    "spatial_station_discovery": QueryIntent.SPATIAL_DISCOVERY,
    "inventory": QueryIntent.STATION_INVENTORY,
    "station_inventory": QueryIntent.STATION_INVENTORY,
    "count": QueryIntent.STATION_PARAMETER_COUNT,
    "station_parameter_count": QueryIntent.STATION_PARAMETER_COUNT,
    "parameter_count": QueryIntent.STATION_PARAMETER_COUNT,
    "observation": QueryIntent.OBSERVATION,
    "observation_values": QueryIntent.OBSERVATION,
    "continuous_daily_values": QueryIntent.OBSERVATION,
    "clarification": QueryIntent.CLARIFICATION,
    "clarify": QueryIntent.CLARIFICATION,
}

MODALITY_ALIASES = {
    "continuous": ParameterModality.CONTINUOUS,
    "discrete": ParameterModality.DISCRETE,
    "mixed": ParameterModality.MIXED,
}


class QueryPlan(BaseModel):
    intent: Optional[QueryIntent] = None
    modality: Optional[ParameterModality] = None
    semantic: dict[str, str] = Field(default_factory=dict)
    filters: dict[str, str] = Field(default_factory=dict)
    stations_ref: str | None = Field(default=None, description="Copy the ref verbatim from a spatial_query tool result")
    inventory_ref: str | None = Field(default=None, description="Copy the ref verbatim from a station_inventory tool result")
    clarification_needed: str | None = None
    ambiguous_candidates: list[str] = Field(default_factory=list)

    @field_validator("filters", mode="before")
    @classmethod
    def validate_and_normalize_filters(cls, v: Any) -> dict[str, str]:
        if isinstance(v, QueryFilters):
            return {k: str(val) for k, val in v.model_dump(exclude_none=True).items()}
        if isinstance(v, dict):
            validated = QueryFilters(**v)
            return {k: str(val) for k, val in validated.model_dump(exclude_none=True).items()}
        return v or {}

    @property
    def typed_filters(self) -> QueryFilters:
        """Returns strongly-typed and validated QueryFilters model."""
        return QueryFilters(**self.filters)

    @model_validator(mode="before")
    @classmethod
    def normalize_intent_and_modality(cls, data: Any) -> Any:
        if isinstance(data, dict):
            raw_intent = data.get("intent")
            if isinstance(raw_intent, str):
                norm_intent = INTENT_ALIASES.get(raw_intent.lower().strip())
                if norm_intent:
                    data["intent"] = norm_intent
            raw_mod = data.get("modality")
            if isinstance(raw_mod, str):
                norm_mod = MODALITY_ALIASES.get(raw_mod.lower().strip())
                if norm_mod:
                    data["modality"] = norm_mod
        return data

    @model_validator(mode="after")
    def infer_intent_if_missing(self) -> "QueryPlan":
        if self.intent is None:
            if self.clarification_needed:
                self.intent = QueryIntent.CLARIFICATION
            elif self.stations_ref:
                self.intent = QueryIntent.SPATIAL_DISCOVERY
            elif self.inventory_ref:
                if self.filters.get("parameter_code"):
                    self.intent = QueryIntent.STATION_PARAMETER_COUNT
                else:
                    self.intent = QueryIntent.STATION_INVENTORY
            elif self.filters:
                self.intent = QueryIntent.OBSERVATION
            else:
                self.intent = QueryIntent.CLARIFICATION
        return self


def _clean_message_payload(msg: Any) -> dict[str, Any]:
    """Clean assistant message payload for multi-turn requests, stripping provider-specific
    reasoning or encrypted tokens (e.g. Azure OpenAI / LiteLLM reasoning_items) that cause 400 errors."""
    if hasattr(msg, "model_dump"):
        dumped = msg.model_dump(exclude_unset=True)
    elif isinstance(msg, dict):
        dumped = dict(msg)
    else:
        dumped = {"role": "assistant", "content": str(msg)}
    dumped.pop("reasoning_items", None)
    dumped.pop("reasoning_content", None)
    return dumped


class QueryPlanner:
    """Runs a native tool-use loop where an LLM calls search_queryables,
    search_reference, and resolve_reference to turn a natural-language
    question into exact Daily API filters, without any hardcoded
    keyword->code mappings."""

    def __init__(
        self,
        client: AsyncOpenAI | None = None,
        model: str | None = None,
        registry: QueryableRegistry = daily_registry,
        references_dir: Path = DEFAULT_REFERENCES_DIR,
        water_data_fetcher: USGSWaterDataFetcher | None = None,
        samples_fetcher: USGSSamplesFetcher | None = None,
    ):
        self.client = client or AsyncOpenAI(
            api_key=os.environ.get("OPENAI_API_KEY"),
            base_url=os.environ.get("OPENAI_BASE_URL") or None,
            timeout=45.0,
        )
        self.model = model or os.environ.get("OPENAI_MODEL", "gpt-5.6-luna")

        from water_agent.fetchers.samples_fetcher import USGSSamplesFetcher
        self.water_data_fetcher = water_data_fetcher or USGSWaterDataFetcher()
        self.samples_fetcher = samples_fetcher or USGSSamplesFetcher()
        self._owns_fetchers = (water_data_fetcher is None and samples_fetcher is None)

        self.registry = registry
        resolver = ReferenceResolver(
            registry=registry,
            store=ReferenceStore(references_dir),
        )

        self._dispatch = {
            "search_queryables": QueryableSearchTool(registry).search,
            "search_reference": ReferenceSearchTool(resolver).search,
            "resolve_reference": ReferenceResolveTool(resolver).resolve,
            "spatial_query": SpatialQueryTool(
                self.water_data_fetcher, resolver, samples_fetcher=self.samples_fetcher
            ).find_stations,
            "station_inventory": StationInventoryTool(
                self.water_data_fetcher, samples_fetcher=self.samples_fetcher
            ).get_inventory,
        }

    async def aclose(self) -> None:
        """Close shared underlying HTTP clients if owned."""
        if self._owns_fetchers:
            await self.water_data_fetcher.aclose()
            await self.samples_fetcher.aclose()

    async def _call_tool(
        self, name: str, arguments: dict[str, Any], ctx: RequestContext
    ) -> dict | list:
        from water_agent.tracing import start_span
        async with start_span(f"tool_execution:{name}", attributes={"tool": name, "arguments": arguments}) as span:
            tool_fn = self._dispatch.get(name)
            if tool_fn is None:
                span.set_error(f"Unknown tool: {name}")
                return {"error": f"Unknown tool: {name}"}

            try:
                import inspect
                if inspect.iscoroutinefunction(tool_fn) or \
                   inspect.iscoroutinefunction(getattr(tool_fn, "__func__", None)) or \
                   (hasattr(tool_fn, "__call__") and inspect.iscoroutinefunction(tool_fn.__call__)):
                    result = await tool_fn(**arguments)
                else:
                    result = tool_fn(**arguments)
                    
                err_val = result.get("error") if hasattr(result, "get") else getattr(result, "error", None)
                if err_val:
                    span.set_error(str(err_val))
                    ctx.tool_errors.append({"tool": name, "error": str(err_val), "retryable": False})
                    return {"error": str(err_val), "retryable": False}

                # Check if the tool class has a result_kind
                # tool_fn could be a bound method
                tool_class = None
                if hasattr(tool_fn, "__self__"):
                    tool_class = tool_fn.__self__.__class__
                elif hasattr(tool_fn, "__class__"): # For callable objects
                    tool_class = tool_fn.__class__
                    
                if tool_class and hasattr(tool_class, "result_kind"):
                    kind = tool_class.result_kind
                    summary, sample, count = tool_class.summarize(result)
                    
                    from datetime import datetime, timezone
                    provenance = {
                        "retrieved_at": datetime.now(timezone.utc).isoformat(),
                        "source_urls": [url for key in ("request_url", "metadata_url", "discrete_summary_url") if (url := result.get(key))]
                    }
                    for prop in ("modality", "family", "pcodes", "pcode_breakdown", "mixed_modality"):
                        if prop in result:
                            provenance[prop] = result[prop]
                    
                    handle = ctx.store.put(kind, result, summary, sample, provenance, count, truncated=False)
                    span.set_attribute("result.kind", str(kind))
                    span.set_attribute("result.count", count)
                    if hasattr(ctx, "spans"):
                        ctx.spans.append(span)
                    return handle.model_dump()
                    
                if hasattr(ctx, "spans"):
                    ctx.spans.append(span)
                return result
            except Exception as exc:
                span.set_error(exc)
                if hasattr(ctx, "spans"):
                    ctx.spans.append(span)
                ctx.tool_errors.append({"tool": name, "error": str(exc), "retryable": True})
                return {"error": str(exc), "retryable": True}

    async def plan(self, question: str, ctx: RequestContext) -> QueryPlan:
        from water_agent.tracing import start_span
        async with start_span("llm_planning", attributes={"question": question}, trace_id=ctx.request_id) as span:
            plan_res = await self._plan_loop(question, ctx, span)
            if hasattr(ctx, "spans"):
                ctx.spans.append(span)
            if plan_res.intent:
                span.set_attribute("plan.intent", plan_res.intent.value)
            return plan_res

    async def _plan_loop(self, question: str, ctx: RequestContext, root_span: Any = None) -> QueryPlan:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": question},
        ]

        # Tracks the most recent multi-candidate lookup seen in the loop,
        # so that if the model ends up asking for clarification, we can
        # show *what* was ambiguous instead of just the model's own
        # paraphrase of it.
        last_candidates: list[str] = []

        for _ in range(MAX_TOOL_ITERATIONS):
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=TOOL_SCHEMAS,
            )
            message = response.choices[0].message

            if not message.tool_calls:
                result_plan = self._parse_plan(message.content or "")
                
                from water_agent.results import UnknownRefError, WrongKindError

                # Ref validation
                def validate_refs(plan: QueryPlan) -> str | None:
                    try:
                        if plan.stations_ref:
                            ctx.store.get(plan.stations_ref, "stations")
                        if plan.inventory_ref:
                            ctx.store.get(plan.inventory_ref, "inventory")
                        # Consistency check
                        if not plan.stations_ref and ("parameter_code" in plan.filters or "state_code" in plan.filters):
                            if any(h.kind == "stations" for h in ctx.store.refs()):
                                return "Missing stations_ref in plan despite spatial intent and available stations ref."
                        return None
                    except (UnknownRefError, WrongKindError) as e:
                        return str(e)
                
                validation_error = validate_refs(result_plan)
                if validation_error:
                    # Retry once without tool calls
                    retry_msg = f"Invalid plan: {validation_error}. Available refs: {ctx.store.available_refs()}. Output a corrected JSON with no tool calls."
                    messages.append(_clean_message_payload(message))
                    messages.append({"role": "user", "content": retry_msg})
                    
                    retry_response = await self.client.chat.completions.create(
                        model=self.model,
                        messages=messages,
                        tools=[], # No tools allowed on retry
                    )
                    retry_message = retry_response.choices[0].message
                    try:
                        result_plan = self._parse_plan(retry_message.content or "")
                        validation_error = validate_refs(result_plan)
                    except Exception as e:
                        validation_error = str(e)
                        
                    if validation_error:
                        # Second failure
                        error_text = "I ran into an internal error while trying to answer your question."
                        if ctx.tool_errors:
                            err = ctx.tool_errors[-1]
                            if err.get("error"):
                                error_text = f"The USGS API returned an error: {err['error']}"
                        return QueryPlan(
                            filters={},
                            clarification_needed=error_text,
                            ambiguous_candidates=[],
                        )
                    
                if result_plan.clarification_needed:
                    result_plan.ambiguous_candidates = last_candidates
                    if ctx.tool_errors:
                        err = ctx.tool_errors[-1]
                        if err.get("error"):
                            result_plan.clarification_needed += f"\n\n(API Error: {err['error']})"
                        
                return result_plan

            messages.append(_clean_message_payload(message))

            for tool_call in message.tool_calls:
                arguments = json.loads(tool_call.function.arguments or "{}")
                result = await self._call_tool(tool_call.function.name, arguments, ctx)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": json.dumps(result),
                    }
                )

                candidates = self._extract_candidates(
                    tool_call.function.name, result
                )
                if candidates:
                    last_candidates = candidates

        error_text = "I wasn't able to resolve this question after several tool calls. Could you rephrase it?"
        if ctx.tool_errors:
            err = ctx.tool_errors[-1]
            if err.get("error"):
                error_text = f"The USGS API returned an error: {err['error']}"

        return QueryPlan(
            filters={},
            clarification_needed=error_text,
            ambiguous_candidates=last_candidates,
        )

    @staticmethod
    def _extract_candidates(tool_name: str, result: object) -> list[str]:
        """Human-readable labels for a multi-candidate lookup, if this
        tool call produced one - either search_reference returning more
        than one match, or resolve_reference's formal `status="ambiguous"`.
        """
        if tool_name == "search_reference" and isinstance(result, list) and len(result) > 1:
            return [
                f'{candidate["matched_value"]}' + (f' ({candidate["resolved_value"]})' if candidate.get("resolved_value") else "")
                for candidate in result[:10]
            ]

        if (
            tool_name == "resolve_reference"
            and isinstance(result, dict)
            and result.get("status") in ("ambiguous", "clarification_needed")
        ):
            labels = []
            for candidate in result.get("candidates", [])[:10]:
                if isinstance(candidate, dict):
                    if "descriptor" in candidate and "unit" in candidate:
                        labels.append(f"{candidate['descriptor']} ({candidate['unit']}) ({candidate['pcode']})")
                    else:
                        name_key = next(
                            (key for key in candidate if key.endswith("_name")), None
                        )
                        label = str(candidate.get(name_key, candidate))
                        unit = candidate.get("unit_of_measure") or candidate.get("unit")
                        cid = candidate.get("id") or candidate.get("pcode")
                        if unit and cid:
                            label = f"{label} ({unit}) ({cid})"
                        elif cid:
                            label = f"{label} ({cid})"
                        labels.append(label)
                else:
                    labels.append(str(candidate))
            return labels

        return []

    @staticmethod
    def _extract_json_candidate(content: str) -> str:
        s = content.strip()
        # 1. Match code fence ```json ... ``` or ``` ... ```
        fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", s, re.IGNORECASE)
        if fence_match:
            candidate = fence_match.group(1).strip()
            if candidate.startswith("{") and candidate.endswith("}"):
                return candidate

        # 2. Extract outermost { ... }
        start = s.find("{")
        end = s.rfind("}")
        if start != -1 and end != -1 and end > start:
            return s[start : end + 1].strip()

        return s

    @classmethod
    def _parse_plan(cls, content: str) -> QueryPlan:
        raw = content.strip()
        extracted = cls._extract_json_candidate(raw)

        for attempt in (raw, extracted):
            if not attempt:
                continue
            try:
                data = json.loads(attempt)
                if isinstance(data, dict):
                    return QueryPlan(**data)
            except (json.JSONDecodeError, TypeError, ValueError):
                continue

        return QueryPlan(
            filters={},
            clarification_needed="I encountered an error planning the query. Please try rephrasing your question.",
        )
