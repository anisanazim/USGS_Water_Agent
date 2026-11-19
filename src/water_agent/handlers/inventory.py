from __future__ import annotations

from datetime import datetime, timezone
import json
import uuid
from typing import Any

from ichatbio.agent_response import ResponseContext
from water_agent.config import settings
from water_agent.handlers.common import (
    append_temporal_not_applied_note,
    format_date_for_display,
    format_family_name,
    temporal_not_applied_notes,
)
from water_agent.handlers.reply_utils import ResultSummary, send_result
from water_agent.metadata.discrete_families import get_parameter, resolve_discrete_parameter
from water_agent.query_planner import QueryPlan
from water_agent.results import RequestContext


async def handle_inventory(
    context: ResponseContext,
    process: Any,
    plan: QueryPlan,
    ctx: RequestContext,
    request: str,
) -> None:
    """Handle Workflow 2 (Dual-Modality Station Inventory) and Workflow 3 (Station Parameter Counts)."""
    stored = ctx.store.get(plan.inventory_ref, "inventory")
    params_dict = stored.items.get("parameters", {})
    discrete_dict = stored.items.get("discrete_parameters", {})
    discrete_total = stored.items.get("discrete_total_observations", 0)
    req_url = stored.items.get("request_url", "")
    discrete_url = stored.items.get("discrete_summary_url", "")

    if req_url:
        await process.log(f'Continuous request URL: "{req_url}"', data={"url": req_url})
    if discrete_url:
        await process.log(f'Discrete summary URL: "{discrete_url}"', data={"url": discrete_url})

    station_id_str = (
        stored.items.get("monitoring_location_id")
        or plan.filters.get("monitoring_location_id")
        or "this station"
    )
    temporal_notes = temporal_not_applied_notes(plan)
    artifact_id = uuid.uuid4().hex
    base_metadata = {
        "ref": plan.inventory_ref,
        "source_urls": stored.provenance.get("source_urls", []),
        "artifact_id": artifact_id,
        "retrieved_at": stored.provenance.get("retrieved_at") or datetime.now(timezone.utc).isoformat(),
    }

    # Check if the user asked about a SPECIFIC parameter (Workflow 3)
    requested_param_code = plan.filters.get("parameter_code")

    if requested_param_code:
        if requested_param_code in params_dict:
            param_data = params_dict[requested_param_code]
            count = param_data.get("estimated_daily_records")
            await process.log(f"Found parameter {param_data['name']} with estimated count {count}.")

            begin_str = format_date_for_display(param_data.get("begin"))
            end_str = format_date_for_display(param_data.get("end"))
            if count is not None:
                hl = (
                    f"There are an estimated **{count:,} daily records** for {param_data['name']} "
                    f"at this station, collected from {begin_str} to {end_str}."
                )
            else:
                hl = (
                    f"Data for {param_data['name']} is available at this station from {begin_str} "
                    f"to {end_str}, but I could not estimate the exact number of records."
                )

            summary = ResultSummary(
                kind_label="station inventory",
                subject=str(param_data["name"]),
                scope=station_id_str,
                unit_label="characteristics",
                total_available=1,
                retrieved=1,
                highlights=[hl],
                inline_items=[
                    f"- **{param_data['name']}** ({requested_param_code}): estimated from {begin_str} to {end_str} at daily interval"
                ],
                notes=temporal_notes,
            )
            meta = {**base_metadata, "count": 1}
            await send_result(context, process, plan, summary, stored.items, meta)
            ctx.store.set_artifact_id(plan.inventory_ref, artifact_id)
            return

        # Discrete parameter or family at station (Shape 2c)
        param_resolve = resolve_discrete_parameter(requested_param_code)
        family_name = param_resolve.family.name if param_resolve.family else requested_param_code

        matching_rows = []
        for grp, items in discrete_dict.items():
            for itm in items:
                desc_lower = itm["descriptor"].lower()
                char_lower = itm["characteristic"].lower()
                matched_p = None
                if param_resolve.family:
                    for pcode, p in param_resolve.family.parameters.items():
                        if p.descriptor.lower() == desc_lower or p.characteristic_name.lower() == char_lower:
                            matched_p = p
                            break
                elif requested_param_code in itm.get("descriptor", ""):
                    matched_p = get_parameter(requested_param_code)

                if matched_p or (family_name.lower() in desc_lower or family_name.lower() in char_lower):
                    matching_rows.append({
                        "pcode": matched_p.pcode if matched_p else "",
                        "descriptor": itm["descriptor"],
                        "unit": matched_p.unit_of_measure if matched_p else "",
                        "count": itm["count"],
                        "first_activity": format_date_for_display(itm.get("first_activity")),
                        "most_recent_activity": format_date_for_display(itm.get("most_recent_activity")),
                    })

        matching_rows.sort(key=lambda x: x["count"], reverse=True)
        disp_family = format_family_name(family_name)

        if matching_rows:
            inline_items = []
            for r in matching_rows:
                p_part = f"**{r['pcode']}**: " if r.get("pcode") else ""
                u_part = f" ({r['unit']})" if r.get("unit") else ""
                d_part = f" ({r['first_activity']} to {r['most_recent_activity']})" if r.get("first_activity") else ""
                inline_items.append(
                    f"- {p_part}{r['descriptor']}{u_part} — {r['count']:,} observations{d_part}"
                )

            highlights: list[str] = []
            if param_resolve.family:
                active_pcodes = {r["pcode"] for r in matching_rows if r.get("pcode")}
                zero_pcodes = [p for p in param_resolve.family.parameters if p not in active_pcodes]
                if zero_pcodes:
                    highlights.append(
                        f"Remaining {len(zero_pcodes)} {disp_family} parameter codes have 0 observations at this station."
                    )

            notes = [
                "Modality: discrete sample measurements (not continuous sensors).",
                "Sample media: Water (samples in other media such as sediment/tissue are excluded).",
                *temporal_notes,
            ]
            summary = ResultSummary(
                kind_label="station inventory",
                subject=disp_family,
                scope=station_id_str,
                unit_label="characteristics",
                total_available=len(matching_rows),
                retrieved=len(matching_rows),
                highlights=highlights,
                inline_header=f"Discrete {disp_family} measurements:",
                inline_items=inline_items,
                notes=notes,
            )
            meta = {**base_metadata, "count": len(matching_rows)}
            await send_result(context, process, plan, summary, stored.items, meta)
            ctx.store.set_artifact_id(plan.inventory_ref, artifact_id)
            return

        await process.log(f"Parameter code {requested_param_code} not found at station.")
        await process.create_artifact(
            mimetype="application/json",
            description=f"USGS station inventory - {disp_family} at {station_id_str}, all dates (0 characteristics)",
            content=json.dumps(stored.items, indent=2).encode("utf-8"),
            metadata={**base_metadata, "count": 0},
        )
        ctx.store.set_artifact_id(plan.inventory_ref, artifact_id)
        reply_msg = append_temporal_not_applied_note(
            f"I couldn't find any discrete {disp_family} observations at this station.", plan
        )
        await context.reply(reply_msg, data=plan.model_dump())
        return

    # General station inventory (Workflow 2)
    all_discrete_entries: list[tuple[str, dict[str, Any]]] = [
        (grp, item)
        for grp, items in discrete_dict.items()
        for item in sorted(items, key=lambda x: x.get("count", 0), reverse=True)
    ]
    total_chars = len(params_dict) + len(all_discrete_entries)

    if total_chars == 0:
        await process.create_artifact(
            mimetype="application/json",
            description=f"USGS station inventory - all parameters at {station_id_str}, all dates (0 characteristics)",
            content=json.dumps(stored.items, indent=2).encode("utf-8"),
            metadata={**base_metadata, "count": 0},
        )
        ctx.store.set_artifact_id(plan.inventory_ref, artifact_id)
        reply_msg = append_temporal_not_applied_note(
            "I couldn't find any data parameters associated with that station.", plan
        )
        await context.reply(reply_msg, data=plan.model_dump())
        return

    highlights = []
    notes = []

    if params_dict:
        highlights.append("### Continuous Sensors")
        for _param_code, param_data in params_dict.items():
            begin_str = format_date_for_display(param_data.get("begin"))
            end_str = format_date_for_display(param_data.get("end"))
            highlights.append(
                f"- **{param_data['name']}**: estimated from {begin_str} to {end_str} at daily interval"
            )

    discrete_inline_lines: list[str] = []
    if discrete_dict and all_discrete_entries:
        highlights.append("### Discrete Samples")
        highlights.append(
            f"Found **{discrete_total:,}** discrete observations across {len(discrete_dict)} "
            "characteristic groups (Information group excluded):"
        )
        for grp, items in discrete_dict.items():
            grp_total = sum(x.get("count", 0) for x in items)
            first_dates = [
                d for d in (format_date_for_display(x.get("first_activity")) for x in items) if d
            ]
            last_dates = [
                d for d in (format_date_for_display(x.get("most_recent_activity")) for x in items) if d
            ]
            first_str = min(first_dates) if first_dates else "unknown"
            last_str = max(last_dates) if last_dates else "unknown"
            highlights.append(
                f"- **{grp}**: {len(items)} characteristics, {grp_total:,} observations ({first_str} to {last_str})"
            )

        for _grp, x in all_discrete_entries:
            d1 = format_date_for_display(x.get("first_activity"))
            d2 = format_date_for_display(x.get("most_recent_activity"))
            discrete_inline_lines.append(
                f"- **{x['descriptor']}**: {x['count']:,} observations ({d1} to {d2})"
            )

        notes.append("Modality: discrete sample measurements (not continuous sensors).")
        notes.append("Sample media: Water (samples in other media such as sediment/tissue are excluded).")

    notes.extend(temporal_notes)

    # Inline full discrete characteristic list ONLY when total number of characteristics <= inline_items_max
    inline_items: list[str] | None = (
        discrete_inline_lines
        if (discrete_inline_lines and total_chars <= settings.inline_items_max)
        else None
    )

    summary = ResultSummary(
        kind_label="station inventory",
        subject="all parameters",
        scope=station_id_str,
        unit_label="characteristics",
        total_available=total_chars,
        retrieved=total_chars,
        highlights=highlights,
        inline_header="Discrete characteristics:" if inline_items else None,
        inline_items=inline_items,
        notes=notes,
    )
    meta = {**base_metadata, "count": total_chars}
    await send_result(context, process, plan, summary, stored.items, meta)
    ctx.store.set_artifact_id(plan.inventory_ref, artifact_id)
