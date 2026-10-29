from __future__ import annotations

import json
import uuid
from typing import Any

from ichatbio.agent_response import ResponseContext
from water_agent.metadata.discrete_families import get_parameter
from water_agent.query_planner import QueryPlan
from water_agent.results import RequestContext


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
        if plan.semantic.get("county"):
            geo_parts.append(plan.semantic["county"])
        if plan.semantic.get("state"):
            geo_parts.append(plan.semantic["state"])
        geo_str = f" in {', '.join(geo_parts)}" if geo_parts else ""

        reply_msg = f"I found no USGS monitoring locations for {family_name}{geo_str}."
        if stored.provenance.get("mixed_modality") or stored.items.get("mixed_modality"):
            reply_msg += (
                "\n\nNote: This query searched discrete water quality laboratory samples. "
                "Continuous automated in-situ sensors (e.g. optical fluorescence sensors) "
                "may exist on the continuous sensor network."
            )
        await context.reply(reply_msg, data=plan.model_dump())
        return

    if is_discrete:
        pcode = plan.filters.get("parameter_code")
        param_obj = get_parameter(pcode) if pcode else None
        desc = param_obj.descriptor if param_obj else stored.items.get("family", "discrete parameter")
        pcodes = stored.items.get("pcodes") or stored.provenance.get("pcodes") or ([pcode] if pcode else [])
        family_name = stored.items.get("family") or stored.provenance.get("family") or desc
        breakdown = stored.items.get("pcode_breakdown") or stored.provenance.get("pcode_breakdown") or []

        response_text = (
            f"Found **{len(stations):,}** distinct monitoring locations for {family_name}.\n"
            f"Stations monitoring multiple forms appear once in this total.\n\n"
        )

        if breakdown:
            response_text += "Breakdown by parameter code:\n"
            for b in breakdown:
                u_str = f" ({b['unit']})" if b.get("unit") else ""
                response_text += f"- **{b['pcode']}**: {b['descriptor']}{u_str} — {b['station_count']:,} stations\n"
            response_text += "\n"

        response_text += "Monitoring locations:\n\n"

        def get_station_id(item):
            return (item.get("id") or item.get("location_id")) if isinstance(item, dict) else item

        def get_station_details(item):
            if isinstance(item, dict):
                parts = []
                name = item.get("name") or item.get("location_name")
                if name:
                    parts.append(name)
                stype = item.get("site_type")
                if stype:
                    parts.append(stype)
                county = item.get("county") or item.get("county_name")
                if county:
                    parts.append(county)
                return f" - {', '.join(parts)}" if parts else ""
            return ""

        for station in stations[:50]:
            sid = get_station_id(station)
            details = get_station_details(station)
            response_text += f"- `{sid}`{details}\n"
        if len(stations) > 50:
            response_text += f"\n*(...and {len(stations) - 50:,} more)*\n"

        response_text += "\nModality: discrete sample measurements (not continuous sensors).\n"
        if stored.items.get("mixed_modality") or stored.provenance.get("mixed_modality"):
            response_text += "This shows discrete sample measurements. Continuous sensor data is also available for this parameter via the continuous monitoring endpoint.\n"
        response_text += "Sample media: Water (samples in other media such as sediment/tissue are excluded)."
    else:
        response_text = f"Found **{len(stations)}** monitoring locations:\n\n"

        def get_station_id(item):
            return item["id"] if isinstance(item, dict) else item

        def get_station_details(item):
            if isinstance(item, dict):
                parts = []
                if item.get("name"):
                    parts.append(item["name"])
                if item.get("site_type"):
                    parts.append(item["site_type"])
                if item.get("county"):
                    parts.append(item["county"])
                return f" - {', '.join(parts)}" if parts else ""
            return ""

        for station in stations[:50]:
            sid = get_station_id(station)
            details = get_station_details(station)
            response_text += f"- `{sid}`{details}\n"
        if len(stations) > 50:
            response_text += f"\n*(...and {len(stations) - 50} more)*"

    artifact_id = uuid.uuid4().hex
    artifact_content = {
        "source_urls": stored.provenance.get("source_urls", []),
        "retrieved_at": stored.provenance.get("retrieved_at", ""),
        "pcodes": pcodes if is_discrete else [plan.filters.get("parameter_code", "")],
        "count": len(stations),
        "stations": stored.items.get("stations", []),
    }
    if is_discrete and breakdown:
        artifact_content["pcode_breakdown"] = breakdown

    await process.create_artifact(
        mimetype="application/json",
        description="USGS Spatial Query Results",
        content=json.dumps(artifact_content, indent=2).encode("utf-8"),
        metadata={
            "count": len(stations),
            "ref": plan.stations_ref,
            "modality": "discrete" if is_discrete else "continuous",
            "source_urls": stored.provenance.get("source_urls", []),
            "pcodes": pcodes if is_discrete else [plan.filters.get("parameter_code", "")],
            "retrieved_at": artifact_content["retrieved_at"],
            "artifact_id": artifact_id,
        },
    )
    ctx.store.set_artifact_id(plan.stations_ref, artifact_id)

    await context.reply(response_text, data=plan.model_dump())
