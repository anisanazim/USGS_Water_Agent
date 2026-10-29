from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from ichatbio.agent_response import ResponseContext
from usgs_water_data import USGSWaterDataFetcher
from water_agent.fetchers.samples_fetcher import USGSSamplesFetcher, to_iso_date
from water_agent.handlers.common import format_date_for_display
from water_agent.metadata.discrete_families import (
    get_parameter,
    is_mixed_modality,
    resolve_discrete_parameter,
)
from water_agent.query_planner import QueryPlan
from water_agent.results import RequestContext

LATEST_READINGS_COUNT = 5


def _normalize_daily_feature(feature: dict[str, Any]) -> dict[str, Any]:
    properties = feature.get("properties", {})
    return {
        "monitoring_location_id": properties.get("monitoring_location_id"),
        "parameter_code": properties.get("parameter_code"),
        "statistic_id": properties.get("statistic_id"),
        "value": properties.get("value"),
        "unit": properties.get("unit_of_measure"),
        "time": properties.get("time"),
        "approval_status": properties.get("approval_status"),
    }


async def handle_observation(
    context: ResponseContext,
    process: Any,
    plan: QueryPlan,
    ctx: RequestContext,
    samples_fetcher: USGSSamplesFetcher | None = None,
    water_data_fetcher: USGSWaterDataFetcher | None = None,
) -> None:
    """Handle Workflow 4 (Discrete Observation Values) and Workflow 5 (Continuous Daily Values)."""
    if not plan.filters:
        await process.log("No observation filters could be resolved.")
        await context.reply(
            "I couldn't resolve any observation filters for that question. "
            "Please provide a monitoring station ID (e.g. 'USGS-0423205010') or a location.",
            data=plan.model_dump(),
        )
        return

    # Determine modality: check if parameter is discrete or continuous
    param_code = plan.filters.get("parameter_code")
    query_codes: list[str] = []
    family_obj = None

    # Check if param_code is an exact 5-digit pcode
    is_exact_pcode = bool(
        param_code and str(param_code).strip().isdigit() and len(str(param_code).strip()) == 5
    )

    if is_exact_pcode:
        discrete_param = get_parameter(str(param_code).strip())
        if discrete_param:
            query_codes = [str(param_code).strip()]
            param_code = str(param_code).strip()
    else:
        p_term = param_code or plan.semantic.get("parameter") or plan.semantic.get("measurement") or ""
        p_res = resolve_discrete_parameter(p_term)
        if p_res.status == "resolved" and p_res.pcodes:
            query_codes = p_res.pcodes
            family_obj = p_res.family
            dominant = p_res.pcodes[0]
            discrete_param = get_parameter(dominant)
            param_code = dominant
            plan.filters["parameter_code"] = dominant

    # 1. Discrete Samples API Path (Workflow 4)
    if discrete_param:
        pcode_arg = query_codes if len(query_codes) > 1 else (query_codes[0] if query_codes else param_code)
        await process.log(
            f"Routing discrete parameter {discrete_param.descriptor} ({param_code}) to Samples API with codes: {query_codes or [param_code]}."
        )
        samples_params: dict[str, Any] = {
            "usgsPCode": pcode_arg,
            "activityMediaName": "Water",
        }
        if plan.filters.get("state_code"):
            st = str(plan.filters["state_code"]).strip()
            samples_params["stateFips"] = st if st.startswith("US:") else f"US:{st}"
        if plan.filters.get("county_code") and plan.filters.get("state_code"):
            st = str(plan.filters["state_code"]).strip()
            st_num = st.split(":")[-1] if ":" in st else st
            co = str(plan.filters["county_code"]).strip()
            co_num = co.split(":")[-1] if ":" in co else co
            samples_params["countyFips"] = f"US:{st_num}:{co_num}"
        if plan.filters.get("monitoring_location_id"):
            samples_params["monitoringLocationIdentifier"] = plan.filters["monitoring_location_id"]
        if plan.filters.get("start_date") or plan.filters.get("activityStartDateLower"):
            samples_params["activityStartDateLower"] = to_iso_date(
                plan.filters.get("start_date") or plan.filters.get("activityStartDateLower")
            )
        if plan.filters.get("end_date") or plan.filters.get("activityStartDateUpper"):
            samples_params["activityStartDateUpper"] = to_iso_date(
                plan.filters.get("end_date") or plan.filters.get("activityStartDateUpper")
            )

        if samples_fetcher is not None:
            samples_res = await samples_fetcher.fetch_results(
                params=samples_params,
                context=ctx,
                pcode=param_code,
            )
        else:
            import sys
            agent_mod = sys.modules.get("agent")
            samples_cls = getattr(agent_mod, "USGSSamplesFetcher", USGSSamplesFetcher) if agent_mod else USGSSamplesFetcher
            async with samples_cls() as fallback_fetcher:
                samples_res = await fallback_fetcher.fetch_results(
                    params=samples_params,
                    context=ctx,
                    pcode=param_code,
                )

        if samples_res.source_urls:
            await process.log(
                f'Final request URL: "{samples_res.source_urls[0]}"',
                data={"urls": samples_res.source_urls},
            )

        if not samples_res.records:
            await process.log("No matching discrete observations found.")
            await context.reply(
                f"I resolved the filters for your question, but found no matching USGS discrete observations for {discrete_param.descriptor} ({param_code}).\n\n"
                f"Sample media: Water (samples in other media such as sediment/tissue are excluded).",
                data=plan.model_dump(),
            )
            return

        loc_info = f" at **{plan.filters['monitoring_location_id']}**" if plan.filters.get("monitoring_location_id") else ""
        if is_exact_pcode or len(query_codes) <= 1:
            subject_desc = f"{discrete_param.descriptor} ({param_code}), {discrete_param.unit_of_measure}"
        else:
            subject_desc = f"{family_obj.name.capitalize() if family_obj else discrete_param.descriptor}"

        artifact_id = uuid.uuid4().hex
        artifact_content = {
            "source_urls": samples_res.source_urls,
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "pcodes": query_codes if query_codes else [param_code],
            "count": samples_res.total_count,
            "filters": plan.filters,
            "summary": samples_res.handle.summary if samples_res.handle else None,
            "records": samples_res.records,
        }
        await process.create_artifact(
            mimetype="application/json",
            description=f"USGS Discrete Observations - {subject_desc}",
            content=json.dumps(artifact_content, indent=2).encode("utf-8"),
            metadata={
                "count": samples_res.total_count,
                "pcode": param_code,
                "pcodes": query_codes if query_codes else [param_code],
                "modality": "discrete",
                "source_urls": samples_res.source_urls,
                "retrieved_at": artifact_content["retrieved_at"],
                "artifact_id": artifact_id,
            },
        )
        if samples_res.handle:
            ctx.store.set_artifact_id(samples_res.handle.ref, artifact_id)

        sorted_records = sorted(
            [r for r in samples_res.records if r.get("activity_date")],
            key=lambda x: (x.get("activity_date", ""), x.get("activity_time", "") or ""),
            reverse=True,
        )

        response_text = (
            f"Found **{samples_res.total_count:,}** discrete records of {subject_desc}{loc_info}.\n\n"
            f"First 10 observations:\n\n"
            f"| Date | Time | Value | Unit | Parameter Code | Descriptor |\n"
            f"| :--- | :--- | :--- | :--- | :--- | :--- |\n"
        )
        for r in sorted_records[:10]:
            d = format_date_for_display(r.get("activity_date"))
            t = r.get("activity_time") or "--"
            v = r.get("result_value")
            val_str = f"{v:g}" if isinstance(v, (int, float)) else str(v if v is not None else "--")
            row_pcode = r.get("pcode") or param_code
            row_param = get_parameter(row_pcode)
            u = r.get("unit") or (row_param.unit_of_measure if row_param else discrete_param.unit_of_measure)
            row_desc = row_param.descriptor if row_param else (r.get("characteristic_name") or discrete_param.descriptor)
            response_text += f"| {d} | {t} | {val_str} | {u} | {row_pcode} | {row_desc} |\n"

        if samples_res.total_count > 10:
            response_text += f"\n*(Full result set of {samples_res.total_count:,} records saved to artifact.)*\n"

        response_text += "\nModality: discrete sample measurements (not continuous sensors).\n"
        if is_mixed_modality(param_code):
            response_text += "This shows discrete sample measurements. Continuous sensor data is also available for this parameter via the continuous monitoring endpoint.\n"
        response_text += "Sample media: Water (samples in other media such as sediment/tissue are excluded)."

        await context.reply(response_text, data=plan.model_dump())
        return

    # 2. Continuous Daily Values Path (Workflow 5)
    dt = plan.filters.pop("datetime", None)
    if water_data_fetcher is not None:
        if dt:
            result = await water_data_fetcher.fetch_daily_values(
                plan.filters, datetime=dt, limit=None, sortby=None
            )
        else:
            result = await water_data_fetcher.fetch_daily_values(
                plan.filters, limit=LATEST_READINGS_COUNT, sortby="-time"
            )
    else:
        import sys
        agent_mod = sys.modules.get("agent")
        daily_cls = getattr(agent_mod, "USGSWaterDataFetcher", USGSWaterDataFetcher) if agent_mod else USGSWaterDataFetcher
        async with daily_cls() as fetcher:
            if dt:
                result = await fetcher.fetch_daily_values(
                    plan.filters, datetime=dt, limit=None, sortby=None
                )
            else:
                result = await fetcher.fetch_daily_values(
                    plan.filters, limit=LATEST_READINGS_COUNT, sortby="-time"
                )

    await process.log(
        f'Final request URL: "{result.source_urls[0] if result.source_urls else ""}"',
        data={"urls": result.source_urls},
    )

    if not result.features:
        await process.log("No matching Daily observations found.")
        await context.reply(
            "I resolved the filters for your question, but found no "
            "matching USGS Daily observations.",
            data=plan.model_dump(),
        )
        return

    readings = [
        _normalize_daily_feature(feature)
        for feature in result.features
    ]
    await process.log(
        f"Found {len(readings)} matching observation(s); "
        f"most recent is from {readings[0]['time']}."
    )

    await process.create_artifact(
        mimetype="application/json",
        description="USGS Daily observations",
        content=json.dumps(
            {"filters": plan.filters, "readings": readings}, indent=2
        ).encode("utf-8"),
        metadata={"filters": plan.filters, "count": len(readings), "source_urls": result.source_urls},
    )

    latest = readings[0]
    param_desc = (
        plan.semantic.get("parameter")
        or (f"parameter {plan.filters.get('parameter_code')}" if plan.filters.get("parameter_code") else "observation")
    )
    loc_parts = []
    if plan.semantic.get("county"):
        loc_parts.append(plan.semantic["county"])
    if plan.semantic.get("state"):
        loc_parts.append(plan.semantic["state"])
    loc_str = f" in {', '.join(loc_parts)}" if loc_parts else ""

    await context.reply(
        f"Most recent matching USGS Daily {param_desc} observation{loc_str}: {latest['value']} "
        f"{latest['unit']} on {latest['time']} at "
        f"{latest['monitoring_location_id']}.",
        data={"filters": plan.filters},
    )
