from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ichatbio.agent_response import ResponseContext
from water_agent.config import settings


@dataclass
class ResultSummary:
    kind_label: str          # "discrete observations" | "daily values" | "monitoring locations" | "station inventory" | "discrete counts"
    subject: str             # e.g. "Dissolved oxygen (DO), water, unfiltered (00300), mg/l"
    scope: str               # station id or area, e.g. "USGS-0423205010" / "Wake County, NC"
    unit_label: str          # "records" | "daily values" | "stations" | "characteristics" | "observations"
    total_available: int
    retrieved: int
    window: str | None = None          # "2020-01-01 to 2023-07-31"; None = all dates
    warning: str | None = None         # only real strings; ignore non-str (mocks)
    interpretation: str | None = None  # spec.format_interpretation() when a date spec exists
    highlights: list[str] = field(default_factory=list)   # e.g. "Sample dates range from A to B."
    inline_header: str | None = None   # e.g. a markdown table header
    inline_items: list[str] | None = None  # FULL list of lines; used only if complete and len <= max
    notes: list[str] = field(default_factory=list)  # modality / sample media / temporal-not-applied

    def __post_init__(self) -> None:
        if not isinstance(self.warning, str) or not self.warning.strip():
            self.warning = None
        else:
            self.warning = self.warning.strip()

    @property
    def complete(self) -> bool:
        return self.retrieved >= self.total_available and not self.warning


def artifact_description(s: ResultSummary) -> str:
    return (
        f"USGS {s.kind_label} - {s.subject} at {s.scope}, "
        f"{s.window or 'all dates'} ({s.retrieved:,} {s.unit_label})"
    )


def should_inline(s: ResultSummary, inline_max: int | None = None) -> bool:
    max_items = settings.inline_items_max if inline_max is None else inline_max
    return (
        s.complete
        and s.inline_items is not None
        and 0 < len(s.inline_items) <= max_items
    )


def build_reply_text(
    s: ResultSummary,
    description: str,
    *,
    inline_max: int | None = None,
) -> str:
    sections: list[str] = []

    # 1. Status line
    if s.complete:
        status = (
            f"Retrieved **all {s.retrieved:,}** {s.unit_label} of {s.subject} at **{s.scope}**. "
            "This is the complete result; nothing was truncated."
        )
    else:
        reason = f": {s.warning}" if s.warning else ""
        status = (
            f"Retrieved **{s.retrieved:,} of {s.total_available:,}** {s.unit_label} of "
            f"{s.subject} at **{s.scope}**. The result is incomplete{reason}."
        )
    sections.append(status)

    # 2. Date interpretation (if any)
    if s.interpretation and s.interpretation.strip():
        sections.append(s.interpretation.strip())

    # 3. Highlights (if any, one line each)
    highlights = [h for h in s.highlights if isinstance(h, str) and h.strip()]
    if highlights:
        sections.append("\n".join(highlights))

    # 4. Inline block (complete or nothing)
    inlined = should_inline(s, inline_max=inline_max)
    if inlined and s.inline_items is not None:
        if s.inline_header:
            sections.append(f"{s.inline_header}\n" + "\n".join(s.inline_items))
        else:
            sections.append("\n".join(s.inline_items))

    # 5. Artifact pointer
    pointer = (
        f'All {s.retrieved:,} {s.unit_label} are in the artifact created in this step: "{description}".'
    )
    if not inlined:
        pointer += (
            f" Read that artifact for the full list; splitting the request will not return additional {s.unit_label}."
        )
    sections.append(pointer)

    # 6. Notes (each on its own line)
    notes = [n for n in s.notes if isinstance(n, str) and n.strip()]
    if notes:
        sections.append("\n".join(notes))

    return "\n\n".join(sections)


async def send_result(
    context: ResponseContext,
    process: Any,
    plan: Any,
    s: ResultSummary,
    artifact_content: dict[str, Any],
    artifact_metadata: dict[str, Any],
) -> str:
    description = artifact_description(s)
    meta = dict(artifact_metadata)
    meta["description"] = description

    await process.create_artifact(
        mimetype="application/json",
        description=description,
        content=json.dumps(artifact_content, indent=2).encode("utf-8"),
        metadata=meta,
    )

    inlined = should_inline(s)
    reply_text = build_reply_text(s, description)

    reply_data = plan.model_dump() if hasattr(plan, "model_dump") else {}
    reply_data["result"] = {
        "total_available": s.total_available,
        "retrieved": s.retrieved,
        "complete": s.complete,
        "artifact_description": description,
        "items_inlined": inlined,
    }
    await context.reply(reply_text, data=reply_data)
    return description

