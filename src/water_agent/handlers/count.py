from __future__ import annotations

import ast
import json
import logging
import re
import uuid
from typing import Any

from ichatbio.agent_response import ResponseContext
from water_agent.fetchers.samples_fetcher import USGSSamplesFetcher
from water_agent.metadata.discrete_families import (
    DISCRETE_FAMILIES,
    get_family_for_pcode,
    get_parameter,
    resolve_discrete_parameter,
)
from water_agent.query_planner import QueryPlan
from water_agent.results import RequestContext

logger = logging.getLogger(__name__)


def extract_station_id(plan: QueryPlan, request: str) -> str | None:
    """Extract and normalize the USGS station identifier."""
    raw = (
        plan.filters.get("monitoring_location_id")
        or plan.filters.get("station")
        or plan.semantic.get("station")
    )
    if raw:
        raw_s = str(raw).strip()
        if raw_s.upper().startswith("USGS-"):
            return raw_s.upper()
        if re.fullmatch(r"\d{8,15}", raw_s):
            return f"USGS-{raw_s}"

    # Search in natural language request text
    m = re.search(r"\bUSGS-(\d{8,15})\b", request, re.IGNORECASE)
    if m:
        return f"USGS-{m.group(1)}"
    m2 = re.search(r"\b(\d{8,15})\b", request)
    if m2:
        return f"USGS-{m2.group(1)}"
    return None


def extract_pcodes_and_family(
    plan: QueryPlan, request: str
) -> tuple[list[str], str, list[dict[str, str]]]:
    """Extract constituent parameter codes, family display name, and parameter metadata."""
    raw_pcode = (
        plan.filters.get("parameter_code")
        or plan.filters.get("parameters")
        or plan.semantic.get("parameter")
        or ""
    )

    pcodes: list[str] = []
    family_name: str = ""

    # 1. Parse raw_pcode if it's a dict, list, or stringified collection
    if isinstance(raw_pcode, dict):
        pcodes = [
            str(k).strip()
            for k in raw_pcode.keys()
            if str(k).strip().isdigit() and len(str(k).strip()) == 5
        ]
    elif isinstance(raw_pcode, (list, tuple)):
        pcodes = [
            str(p).strip()
            for p in raw_pcode
            if str(p).strip().isdigit() and len(str(p).strip()) == 5
        ]
    elif isinstance(raw_pcode, str) and ("{" in raw_pcode or "[" in raw_pcode or "," in raw_pcode):
        found = re.findall(r"\b\d{5}\b", raw_pcode)
        if found:
            pcodes = found
    elif isinstance(raw_pcode, str) and raw_pcode.strip().isdigit() and len(raw_pcode.strip()) == 5:
        pcodes = [raw_pcode.strip()]

    # 2. Check if request mentions a series of explicit 5-digit parameter codes
    st_match = re.search(r"\bUSGS-(\d{8,15})\b", request, re.IGNORECASE)
    st_num = st_match.group(1) if st_match else ""
    found_in_req = [
        c
        for c in re.findall(r"\b\d{5}\b", request)
        if c not in st_num
    ]
    if len(found_in_req) >= 2 and not pcodes:
        pcodes = found_in_req

    # 3. Check for parameter family (e.g. 'phosphorus', 'calcium', 'nitrate')
    search_term = ""
    if isinstance(raw_pcode, str) and not raw_pcode.isdigit() and "[" not in raw_pcode and "{" not in raw_pcode:
        search_term = raw_pcode.strip()
    if not search_term:
        req_lower = request.lower()
        for fam_key in sorted(DISCRETE_FAMILIES.keys(), key=len, reverse=True):
            if re.search(r"\b" + re.escape(fam_key) + r"\b", req_lower):
                search_term = fam_key
                break

    if search_term:
        res = resolve_discrete_parameter(search_term)
        if res.family:
            family_name = res.family.name
            if not pcodes:
                pcodes = list(res.family.parameters.keys())
        elif not family_name:
            family_name = search_term

    if pcodes and (not family_name or "{" in family_name or ":" in family_name):
        fam = get_family_for_pcode(pcodes[0])
        family_name = fam.name if fam else f"parameter {pcodes[0]}"

    if not family_name or "{" in family_name:
        family_name = "requested parameter"

    # Deduplicate while preserving order
    unique_pcodes = list(dict.fromkeys(pcodes))

    # Build metadata definitions
    meta_list: list[dict[str, str]] = []
    for p in unique_pcodes:
        p_obj = get_parameter(p)
        if p_obj:
            meta_list.append(
                {
                    "pcode": p,
                    "descriptor": p_obj.descriptor,
                    "unit": p_obj.unit_of_measure,
                }
            )
        else:
            meta_list.append(
                {
                    "pcode": p,
                    "descriptor": f"USGS parameter code {p}",
                    "unit": "",
                }
            )

    return unique_pcodes, family_name, meta_list


async def handle_parameter_count(
    context: ResponseContext,
    process: Any,
    plan: QueryPlan,
    ctx: RequestContext,
    request: str,
    samples_fetcher: USGSSamplesFetcher | None = None,
) -> None:
    """Handle Workflow 3: Station Parameter Counts ('How many records of X at station Y?').
    
    Resolves the constituent parameter codes for the family, queries /results/count
    with all related pcodes to get the exact total sum, presents details of all
    queried codes and what each means, and provides next-step guidance for single-code
    counts or actual values.
    """
    station_id = extract_station_id(plan, request)
    if not station_id:
        await context.reply(
            "Please specify a USGS monitoring station identifier (e.g. USGS-0423205010)."
        )
        return

    pcodes, family_name, meta_list = extract_pcodes_and_family(plan, request)
    if not pcodes:
        await context.reply(
            f"I could not determine the specific USGS parameter codes for '{family_name}' at {station_id}. "
            "Please specify a parameter name (e.g. phosphorus, calcium, nitrate) or a 5-digit USGS parameter code."
        )
        return

    fetcher = samples_fetcher or USGSSamplesFetcher()
    params = {
        "monitoringLocationIdentifier": station_id,
        "usgsPCode": pcodes,
    }

    total_count = await fetcher.get_results_count(params)
    req_url = fetcher._build_source_url(
        f"{fetcher.base_url}/results/count", fetcher._normalize_params(params)
    )

    await process.log(f'Final request URL: "{req_url}"', data={"url": req_url})
    await process.log(
        f"Found {total_count:,} total discrete observation(s) across {len(pcodes)} parameter code(s)."
    )

    # Check if a station inventory is stored in RequestContext to correlate individual observation counts
    breakdown_by_pcode: dict[str, int] = {}
    inv_stored = None
    if getattr(plan, "inventory_ref", None):
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

    if inv_stored and inv_stored.items.get("discrete_parameters"):
        discrete_dict = inv_stored.items["discrete_parameters"]
        for grp, items in discrete_dict.items():
            for itm in items:
                desc_lower = itm["descriptor"].lower()
                char_lower = itm["characteristic"].lower()
                for m in meta_list:
                    p = get_parameter(m["pcode"])
                    if p:
                        if (
                            p.descriptor.lower() == desc_lower
                            or p.characteristic_name.lower() == char_lower
                        ):
                            breakdown_by_pcode[m["pcode"]] = itm["count"]
                    elif m["pcode"] in itm.get("descriptor", ""):
                        breakdown_by_pcode[m["pcode"]] = itm["count"]

    # Build user-facing response
    if len(pcodes) == 1:
        p_desc = meta_list[0]["descriptor"]
        p_unit = f" ({meta_list[0]['unit']})" if meta_list[0]["unit"] else ""
        response_text = (
            f"Found **{total_count:,}** discrete observations for **{p_desc}**{p_unit} "
            f"(code **{pcodes[0]}**) at **{station_id}**.\n\n"
        )
    else:
        response_text = (
            f"Found **{total_count:,}** total discrete measurements for **{family_name}** at **{station_id}**.\n\n"
            f"Constituent parameter codes included in this query:\n\n"
        )
        if breakdown_by_pcode:
            response_text += "| Parameter Code | Parameter Description | Unit | Station Observations |\n"
            response_text += "| :---: | --- | :---: | :---: |\n"
            for m in meta_list:
                c = breakdown_by_pcode.get(m["pcode"], 0)
                u = m["unit"] or "-"
                response_text += f"| **{m['pcode']}** | {m['descriptor']} | {u} | {c:,} |\n"
        else:
            response_text += "| Parameter Code | Parameter Description / What It Means | Unit |\n"
            response_text += "| :---: | --- | :---: |\n"
            for m in meta_list:
                u = m["unit"] or "-"
                response_text += f"| **{m['pcode']}** | {m['descriptor']} | {u} |\n"
        response_text += "\n"

    # Add next-step guidance
    response_text += "---\n"
    response_text += f"- To check the count of an individual parameter code, ask: *'How many records of {pcodes[0]} at {station_id}?'*\n"
    response_text += f"- To view the actual numerical measurements, ask: *'Show me the {family_name} values at {station_id}'* (which queries `/results/narrow`).\n\n"
    response_text += "Modality: discrete sample measurements (not continuous sensors).\n"
    response_text += "Sample media: Water (samples in other media such as sediment/tissue are excluded)."

    # Store artifact
    artifact_id = uuid.uuid4().hex
    artifact_payload = {
        "station_id": station_id,
        "family": family_name,
        "total_count": total_count,
        "parameter_codes": pcodes,
        "parameters": meta_list,
        "breakdown": breakdown_by_pcode,
        "request_url": req_url,
    }
    await process.create_artifact(
        mimetype="application/json",
        description=f"USGS Discrete Parameter Counts: {family_name} at {station_id}",
        content=json.dumps(artifact_payload, indent=2).encode("utf-8"),
        metadata={
            "count": total_count,
            "station_id": station_id,
            "family": family_name,
            "pcodes": pcodes,
            "source_urls": [req_url],
            "artifact_id": artifact_id,
        },
    )

    await context.reply(response_text, data=plan.model_dump())
