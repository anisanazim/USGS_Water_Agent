from __future__ import annotations

from datetime import datetime, timezone
import uuid
from typing import Any

from ichatbio.agent_response import ResponseContext
from water_agent.handlers.common import (
    append_temporal_not_applied_note,
    format_family_name,
    temporal_not_applied_notes,
)
from water_agent.handlers.reply_utils import ResultSummary, send_result
from water_agent.metadata.discrete_families import get_parameter
from water_agent.query_planner import QueryPlan
from water_agent.results import RequestContext


def _format_station_line(item: Any) -> str:
    if isinstance(item, dict):
        sid = item.get("id") or item.get("location_id") or ""
        parts = []
        name = item.get("name") or item.get("location_name")
        if name:
            parts.append(str(name))
        stype = item.get("site_type")
        if stype:
            parts.append(str(stype))
        county = item.get("county") or item.get("county_name")
        if county:
            parts.append(str(county))
        details = f" - {', '.join(parts)}" if parts else ""
        return f"- `{sid}`{details}"
    return f"- `{item}`"


def _resolve_spatial_scope(plan: QueryPlan, stored_items: dict[str, Any] | None = None) -> str:
    if stored_items:
        geo_parts = []
        if stored_items.get("county_name"):
            geo_parts.append(str(stored_items["county_name"]))
        if stored_items.get("state_name"):
            geo_parts.append(str(stored_items["state_name"]))
        if geo_parts:
            return ", ".join(geo_parts)
    geo_parts = []
    if plan.semantic.get("county"):
        geo_parts.append(str(plan.semantic["county"]))
    if plan.semantic.get("state"):
        geo_parts.append(str(plan.semantic["state"]))
    if geo_parts:
        return ", ".join(geo_parts)
    if plan.filters.get("county_code") and plan.filters.get("state_code"):
        return f"county {plan.filters['county_code']}, state {plan.filters['state_code']}"
    if plan.filters.get("state_code"):
        return f"state {plan.filters['state_code']}"
    if plan.filters.get("monitoring_location_id"):
        return str(plan.filters["monitoring_location_id"])
    return "the requested area"


async def handle_spatial(
    context: ResponseContext,
    process: Any,
    plan: QueryPlan,
    ctx: RequestContext,
) -> None:
    """Handle Workflow 1: Spatial Station Discovery ('Where is X monitored?')."""
    stored = ctx.store.get(plan.stations_ref, "stations")
    stations = stored.items.get("stations", [])
    req_url = stored.items.get("request_url", "")
    if req_url:
        await process.log(f'Final request URL: "{req_url}"', data={"url": req_url})
    await process.log(f"Found {len(stations)} matching monitoring location(s).")

    is_discrete = (
        stored.items.get("modality") == "discrete"
        or stored.provenance.get("modality") == "discrete"
    )

    if not stations:
        pcode = plan.filters.get("parameter_code")
        param_obj = get_parameter(pcode) if pcode else None
        family_name = (
            stored.items.get("family")
            or stored.provenance.get("family")
            or (param_obj.descriptor if param_obj else None)
            or plan.semantic.get("parameter")
            or (f"parameter {pcode}" if pcode else "the requested parameter")
        )
        geo_parts = []
        if stored.items.get("county_name"):
            geo_parts.append(str(stored.items["county_name"]))
        if stored.items.get("state_name"):
            geo_parts.append(str(stored.items["state_name"]))
        if not geo_parts:
            if plan.semantic.get("county"):
                geo_parts.append(plan.semantic["county"])
            if plan.semantic.get("state"):
                geo_parts.append(plan.semantic["state"])
        geo_str = f" in {', '.join(geo_parts)}" if geo_parts else ""

        disp_family = format_family_name(family_name)
        reply_msg = f"I found no USGS monitoring locations for {disp_family}{geo_str}."
        if stored.provenance.get("mixed_modality") or stored.items.get("mixed_modality"):
            reply_msg += (
                "\n\nNote: This query searched discrete water quality laboratory samples. "
                "Continuous automated in-situ sensors (e.g. optical fluorescence sensors) "
                "may exist on the continuous sensor network."
            )
        reply_msg = append_temporal_not_applied_note(reply_msg, plan)
        await context.reply(reply_msg, data=plan.model_dump())
        return

    pcode = plan.filters.get("parameter_code")
    scope = _resolve_spatial_scope(plan, stored.items)
    breakdown: list[dict[str, Any]] = []
    highlights: list[str] = []
    notes: list[str] = []

    if is_discrete:
        param_obj = get_parameter(pcode) if pcode else None
        desc = param_obj.descriptor if param_obj else stored.items.get("family", "discrete parameter")
        pcodes = stored.items.get("pcodes") or stored.provenance.get("pcodes") or ([pcode] if pcode else [])
        family_name = stored.items.get("family") or stored.provenance.get("family") or desc
        subject = format_family_name(family_name)
        breakdown = stored.items.get("pcode_breakdown") or stored.provenance.get("pcode_breakdown") or []

        highlights.append("Stations monitoring multiple forms appear once in this total.")
        if breakdown:
            highlights.append("Breakdown by parameter code:")
            for b in breakdown:
                u_str = f" ({b['unit']})" if b.get("unit") else ""
                highlights.append(
                    f"- **{b['pcode']}**: {b['descriptor']}{u_str} — {b['station_count']:,} stations"
                )

        notes.append("Modality: discrete sample measurements (not continuous sensors).")
        notes.append("Sample media: Water (samples in other media such as sediment/tissue are excluded).")
    else:
        pcodes = [pcode] if pcode else [""]
        raw_subject = (
            plan.semantic.get("parameter")
            or (f"parameter {pcode}" if pcode else "water monitoring")
        )
        subject = format_family_name(raw_subject)

    notes.extend(temporal_not_applied_notes(plan))

    inline_items = [_format_station_line(station) for station in stations]

    artifact_id = uuid.uuid4().hex
    now_iso = stored.provenance.get("retrieved_at") or datetime.now(timezone.utc).isoformat()
    artifact_content: dict[str, Any] = {
        "source_urls": stored.provenance.get("source_urls", []),
        "retrieved_at": now_iso,
        "pcodes": pcodes if is_discrete else [plan.filters.get("parameter_code", "")],
        "count": len(stations),
        "stations": stored.items.get("stations", []),
    }
    if is_discrete and breakdown:
        artifact_content["pcode_breakdown"] = breakdown

    artifact_metadata = {
        "count": len(stations),
        "ref": plan.stations_ref,
        "modality": "discrete" if is_discrete else "continuous",
        "source_urls": stored.provenance.get("source_urls", []),
        "pcodes": pcodes if is_discrete else [plan.filters.get("parameter_code", "")],
        "retrieved_at": now_iso,
        "artifact_id": artifact_id,
    }

    summary = ResultSummary(
        kind_label="monitoring locations",
        subject=f"for {subject}",
        scope=scope,
        preposition="in",
        unit_label="stations",
        total_available=len(stations),
        retrieved=len(stations),
        highlights=highlights,
        inline_header="Monitoring locations:",
        inline_items=inline_items,
        notes=notes,
    )

    await send_result(context, process, plan, summary, artifact_content, artifact_metadata)
    ctx.store.set_artifact_id(plan.stations_ref, artifact_id)
