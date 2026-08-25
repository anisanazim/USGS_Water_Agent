from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv
from openai import AsyncOpenAI
from pydantic import BaseModel, Field

load_dotenv()

from water_agent.metadata.references.store import ReferenceStore
from water_agent.metadata.registry import QueryableRegistry, daily_registry
from water_agent.metadata.resolver import ReferenceResolver
from water_agent.tools.queryable_search import QueryableSearchTool
from water_agent.tools.reference_resolve import ReferenceResolveTool
from water_agent.tools.reference_search import ReferenceSearchTool

DEFAULT_REFERENCES_DIR = (
    Path(__file__).resolve().parents[2] / "metadata" / "raw" / "references"
)

MAX_TOOL_ITERATIONS = 8

SYSTEM_PROMPT = """\
You turn a natural-language question about USGS Daily water data into a set of
exact Daily API filters, without ever guessing a USGS code yourself.

You have three tools:
- search_queryables(query): find which Daily field is relevant to a concept
  the user mentioned (e.g. "parameter" -> parameter_code). This searches
  field names/descriptions literally, so use short plain nouns, not the
  user's full phrase.
- search_reference(field_name, query): find candidate USGS values for a
  field by keyword. Use USGS terminology, not the user's wording, if you
  suspect they differ (e.g. USGS calls streamflow "Discharge"). Each
  candidate already includes its final `resolved_value` (the exact USGS
  code) - that IS the answer for that candidate, nothing more to do with it.
- resolve_reference(field_name, value): deterministically resolve ONE
  human-readable term you already trust to be exact (e.g. the user
  literally wrote "North Carolina") straight to its USGS code, skipping
  search. It only accepts human-readable terms, never a code you already
  have - a code is not itself a searchable term and will not resolve.

Typical flow per concept: call search_reference with your best guess at
USGS terminology, then use the `resolved_value` of the best/only match
directly as the final filter value. Do not call resolve_reference on a
value you got from search_reference - you already have the answer. Only
use resolve_reference when you have an exact term and want to skip
searching entirely. Never fabricate a code - every value in your final
answer must have come from a tool result.

Hydrology has specialized jargon that often differs from everyday words
(e.g. a layperson's "streamflow" or "flow rate" is USGS's "discharge"; a
layperson's "stage" is USGS's "gage height"). If your best match for a
concept is a qualified or compound variant (extra descriptors, a
sub-measurement, a time-basis suffix) rather than the plain base term, that
is a signal you haven't found the right USGS word yet - try one more
search using the field's technical term before finalizing.

When you are done, respond with ONLY a JSON object (no prose, no markdown
fences) of the form:
{"semantic": {"<concept>": "<plain-language phrase>", ...}, "filters": {"<field_name>": "<resolved_value>", ...}, "clarification_needed": null}

`semantic` is a short, plain-language summary of what you understood the
user to be asking for - in the user's own words/concepts (e.g.
"measurement": "streamflow", "location": "Texas", "statistic": "maximum"),
NOT USGS terminology or codes. Only include concepts the user actually
mentioned. Do not include a time range - date filtering isn't supported
yet, so leave any time period out of `semantic` entirely rather than
stating something that won't affect the actual query.

If the question is ambiguous (e.g. multiple plausible fields or values and
you cannot tell which the user means), instead respond with:
{"semantic": {<whatever you understood so far>}, "filters": {<whatever you already resolved, if anything>}, "clarification_needed": "<question to ask the user>"}
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
]


class QueryPlan(BaseModel):
    semantic: dict[str, str] = Field(default_factory=dict)
    filters: dict[str, str]
    clarification_needed: str | None = None
    ambiguous_candidates: list[str] = Field(default_factory=list)


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
    ):
        self.client = client or AsyncOpenAI(
            api_key=os.environ.get("OPENAI_API_KEY"),
            base_url=os.environ.get("OPENAI_BASE_URL") or None,
        )
        self.model = model or os.environ.get("OPENAI_MODEL", "gpt-4o")

        self.registry = registry
        resolver = ReferenceResolver(
            registry=registry,
            store=ReferenceStore(references_dir),
        )

        self._dispatch = {
            "search_queryables": QueryableSearchTool(registry).search,
            "search_reference": ReferenceSearchTool(resolver).search,
            "resolve_reference": ReferenceResolveTool(resolver).resolve,
        }

    def _call_tool(self, name: str, arguments: dict) -> dict | list:
        tool_fn = self._dispatch.get(name)
        if tool_fn is None:
            return {"error": f"Unknown tool: {name}"}

        try:
            return tool_fn(**arguments)
        except (ValueError, TypeError) as exc:
            return {"error": str(exc)}

    async def plan(self, question: str) -> QueryPlan:
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
                if result_plan.clarification_needed:
                    result_plan.ambiguous_candidates = last_candidates
                return result_plan

            messages.append(message.model_dump(exclude_unset=True))

            for tool_call in message.tool_calls:
                arguments = json.loads(tool_call.function.arguments or "{}")
                result = self._call_tool(tool_call.function.name, arguments)
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

        return QueryPlan(
            filters={},
            clarification_needed=(
                "I wasn't able to resolve this question after several tool "
                "calls. Could you rephrase it?"
            ),
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
                f'{candidate["matched_value"]} ({candidate["resolved_value"]})'
                for candidate in result[:5]
            ]

        if (
            tool_name == "resolve_reference"
            and isinstance(result, dict)
            and result.get("status") == "ambiguous"
        ):
            labels = []
            for candidate in result.get("candidates", [])[:5]:
                name_key = next(
                    (key for key in candidate if key.endswith("_name")), None
                )
                labels.append(str(candidate.get(name_key, candidate)))
            return labels

        return []

    @staticmethod
    def _parse_plan(content: str) -> QueryPlan:
        try:
            data = json.loads(content)
            return QueryPlan(**data)
        except (json.JSONDecodeError, TypeError, ValueError):
            return QueryPlan(
                filters={},
                clarification_needed=content or "I couldn't determine the query filters.",
            )
