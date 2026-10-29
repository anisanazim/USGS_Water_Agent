from __future__ import annotations

import json
import uuid
from typing import Any

from ichatbio.agent_response import ResponseContext
from water_agent.handlers.common import format_date_for_display
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
                response_text = f"There are an estimated **{count:,} daily records** for {param_data['name']} at this station, collected from {begin_str} to {end_str}."
            else:
                response_text = f"Data for {param_data['name']} is available at this station from {begin_str} to {end_str}, but I could not estimate the exact number of records."
        else:
            # Discrete parameter or family at station (Shape 2c)
            station_id_str = stored.items.get("monitoring_location_id") or "this station"
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

            if matching_rows:
                response_text = f"Found discrete {family_name} measurements at **{station_id_str}**:\n\n"
                for r in matching_rows:
                    p_part = f"**{r['pcode']}**: " if r.get("pcode") else ""
                    u_part = f" ({r['unit']})" if r.get("unit") else ""
                    d_part = f" ({r['first_activity']} to {r['most_recent_activity']})" if r.get("first_activity") else ""
                    response_text += f"- {p_part}{r['descriptor']}{u_part} — {r['count']:,} observations{d_part}\n"

                if param_resolve.family:
                    active_pcodes = {r['pcode'] for r in matching_rows if r.get('pcode')}
                    zero_pcodes = [p for p in param_resolve.family.parameters if p not in active_pcodes]
                    if zero_pcodes:
                        response_text += f"\n*(Remaining {len(zero_pcodes)} {family_name} parameter codes have 0 observations at this station.)*\n"

                response_text += "\nModality: discrete sample measurements (not continuous sensors).\n"
                response_text += "Sample media: Water (samples in other media such as sediment/tissue are excluded)."
            else:
                await process.log(f"Parameter code {requested_param_code} not found at station.")
                response_text = f"I couldn't find any discrete {family_name} observations at this station."
    else:
        # General station inventory (Workflow 2)
        sections = []
        if params_dict:
            sec = "### Continuous Sensors\n"
            for param_code, param_data in params_dict.items():
                begin_str = format_date_for_display(param_data.get("begin"))
                end_str = format_date_for_display(param_data.get("end"))
                sec += f"- **{param_data['name']}**: estimated from {begin_str} to {end_str} at daily interval\n"
            sections.append(sec)

        if discrete_dict and discrete_total > 0:
            sec = f"### Discrete Samples\n"
            sec += f"Found **{discrete_total:,}** discrete observations across {len(discrete_dict)} characteristic groups (Information group excluded):\n\n"
            for grp, items in discrete_dict.items():
                top_items = sorted(items, key=lambda x: x["count"], reverse=True)[:5]
                item_strs = [
                    f"**{x['descriptor']}**: {x['count']:,} observations ({format_date_for_display(x.get('first_activity'))} to {format_date_for_display(x.get('most_recent_activity'))})"
                    for x in top_items
                ]
                sec += f"**{grp}** ({len(items)} characteristics):\n"
                for itm_str in item_strs:
                    sec += f"- {itm_str}\n"
                if len(items) > 5:
                    sec += f"*(...and {len(items) - 5} more in this group)*\n"
                sec += "\n"
            sec += "Modality: discrete sample measurements (not continuous sensors).\n"
            sec += "Sample media: Water (samples in other media such as sediment/tissue are excluded).\n"
            sections.append(sec)

        if not sections:
            response_text = "I couldn't find any data parameters associated with that station."
        else:
            station_id_str = stored.items.get("monitoring_location_id") or "this station"
            response_text = f"Available data parameters for **{station_id_str}**:\n\n" + "\n\n".join(sections)

    artifact_id = uuid.uuid4().hex
    await process.create_artifact(
        mimetype="application/json",
        description="USGS Station Inventory",
        content=json.dumps(stored.items, indent=2).encode("utf-8"),
        metadata={
            "count": len(params_dict) + len(discrete_dict),
            "ref": plan.inventory_ref,
            "source_urls": stored.provenance.get("source_urls", []),
            "artifact_id": artifact_id,
        },
    )
    ctx.store.set_artifact_id(plan.inventory_ref, artifact_id)

    await context.reply(response_text, data=plan.model_dump())
