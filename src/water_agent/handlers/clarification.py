from __future__ import annotations

import logging
import re
from typing import Any

from ichatbio.agent_response import ResponseContext
from water_agent.handlers.common import format_date_for_display
from water_agent.metadata.discrete_families import (
    DISCRETE_FAMILIES,
    get_parameter,
    resolve_discrete_parameter,
)
from water_agent.query_planner import QueryPlan
from water_agent.results import RequestContext

logger = logging.getLogger(__name__)


async def handle_clarification(
    context: ResponseContext,
    process: Any,
    plan: QueryPlan,
    ctx: RequestContext,
    request: str,
) -> None:
    """Handle Workflow 6: Clarification, Disambiguation & Guardrails."""
    # Check for station-scoped inventory in context store
    inv_stored = None
    if plan.inventory_ref:
        try:
            inv_stored = ctx.store.get(plan.inventory_ref, "inventory")
        except Exception:
            pass
    if not inv_stored:
        for h in ctx.store.refs():
            if h.kind == "inventory":
                try:
                    inv_stored = ctx.store.get(h.ref, "inventory")
                    break
                except Exception:
                    pass

    # 1. Station-scoped clarification / count resolution (Workflow 3 fallback)
    if inv_stored:
        station_id = (
            inv_stored.items.get("monitoring_location_id")
            or plan.semantic.get("station")
            or plan.filters.get("monitoring_location_id")
        )
        discrete_dict = inv_stored.items.get("discrete_parameters", {})

        STOPWORDS = {
            "usgs", "water", "data", "query", "search", "only", "exact", "show", "find",
            "parameter", "parameters", "characteristic", "characteristics", "record", "records",
            "name", "description", "matches", "containing", "such", "available", "site", "sites",
            "station", "stations", "verified", "about", "what", "which", "where"
        }

        param_term = (
            plan.semantic.get("parameter")
            or plan.semantic.get("measurement")
            or plan.filters.get("parameter_code")
            or ""
        )
        if not param_term:
            req_lower = request.lower()
            # Sort by length descending so specific names like 'phosphorus' match before 'ph'
            for fam_key in sorted(DISCRETE_FAMILIES.keys(), key=len, reverse=True):
                if re.search(r"\b" + re.escape(fam_key) + r"\b", req_lower):
                    param_term = fam_key
                    break

        param_resolve = resolve_discrete_parameter(param_term) if param_term else None
        family_name = param_resolve.family.name if (param_resolve and param_resolve.family) else param_term

        station_candidates = []
        for grp, items in discrete_dict.items():
            for itm in items:
                desc_lower = itm["descriptor"].lower()
                char_lower = itm["characteristic"].lower()

                matched_pcode = None
                matched_unit = None
                if param_resolve and param_resolve.family:
                    for pcode, p in param_resolve.family.parameters.items():
                        if p.descriptor.lower() == desc_lower:
                            matched_pcode = pcode
                            matched_unit = p.unit_of_measure
                            break
                    if not matched_pcode:
                        for pcode, p in param_resolve.family.parameters.items():
                            if p.characteristic_name.lower() == char_lower:
                                matched_pcode = pcode
                                matched_unit = p.unit_of_measure
                                break

                is_match = False
                if matched_pcode:
                    is_match = True
                elif param_term:
                    pt_lower = param_term.lower()
                    if pt_lower in desc_lower or pt_lower in char_lower:
                        is_match = True
                    elif pt_lower.rstrip("s") in desc_lower or pt_lower.rstrip("s") in char_lower:
                        is_match = True

                if is_match:
                    station_candidates.append({
                        "descriptor": itm["descriptor"],
                        "count": itm["count"],
                        "first_activity": format_date_for_display(itm.get("first_activity")),
                        "most_recent_activity": format_date_for_display(itm.get("most_recent_activity")),
                        "pcode": matched_pcode,
                        "unit": matched_unit,
                    })

        station_candidates.sort(key=lambda x: x["count"], reverse=True)

        if station_candidates and param_resolve and param_resolve.family:
            disp_name = family_name or "discrete"
            st_str = f"**{station_id}**" if station_id else "this station"
            reply_text = f"Found discrete {disp_name} measurements at {st_str}:\n\n"
            for c in station_candidates:
                u_part = f" ({c['unit']})" if c.get("unit") else ""
                p_part = f"**{c['pcode']}**: " if c.get("pcode") else ""
                d_part = f" ({c['first_activity']} to {c['most_recent_activity']})" if c.get("first_activity") else ""
                reply_text += f"- {p_part}{c['descriptor']}{u_part} — {c['count']:,} observations{d_part}\n"

            active_pcodes = {c['pcode'] for c in station_candidates if c.get('pcode')}
            zero_pcodes = [p for p in param_resolve.family.parameters if p not in active_pcodes]
            if zero_pcodes:
                reply_text += f"\n*(Remaining {len(zero_pcodes)} {disp_name} parameter codes have 0 observations at this station.)*\n"

            reply_text += "\nModality: discrete sample measurements (not continuous sensors).\n"
            reply_text += "Sample media: Water (samples in other media such as sediment/tissue are excluded)."
            await context.reply(reply_text, data=plan.model_dump())
            return

        if station_candidates:
            disp_name = family_name or "requested"
            st_str = f"**{station_id}**" if station_id else "this station"
            reply_text = f"Found data for {disp_name} at {st_str}:\n\n"
            for c in station_candidates[:6]:
                u_part = f" ({c['unit']})" if c.get("unit") else ""
                p_part = f"**{c['pcode']}**: " if c.get("pcode") else ""
                d_part = f" ({c['first_activity']} to {c['most_recent_activity']})" if c.get("first_activity") else ""
                reply_text += f"- {p_part}{c['descriptor']}{u_part} — {c['count']:,} observations{d_part}\n"
            reply_text += "\nModality: discrete sample measurements (not continuous sensors).\n"
            reply_text += "Sample media: Water (samples in other media such as sediment/tissue are excluded)."
            await context.reply(reply_text, data=plan.model_dump())
            return

    # 2. General Clarification (no station inventory)
    reply_text = plan.clarification_needed or (
        "Please provide a USGS monitoring station ID (e.g. 'USGS-0423205010') "
        "or a geographic area (e.g. 'Wake County, NC') to query water-data observations or monitoring locations."
    )

    if plan.ambiguous_candidates:
        await process.log(
            f"Ambiguous candidates considered: {', '.join(plan.ambiguous_candidates)}",
            data={"candidates": plan.ambiguous_candidates},
        )
        if not any(c in reply_text for c in plan.ambiguous_candidates[:2]):
            reply_text += "\n\nAvailable candidates:\n"
            for c in plan.ambiguous_candidates[:6]:
                clean_c = c.replace(" (North Carolina baseline totals)", "").strip()
                reply_text += f"- {clean_c}\n"

    await process.log(f"Clarification needed: {reply_text}")
    await context.reply(reply_text, data=plan.model_dump())
