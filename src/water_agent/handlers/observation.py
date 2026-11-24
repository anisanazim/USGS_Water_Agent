from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from ichatbio.agent_response import ResponseContext
from usgs_water_data import USGSWaterDataFetcher
from water_agent.fetchers.samples_fetcher import (
    DateParseError,
    USGSSamplesFetcher,
    validate_datetime_interval,
)
from water_agent.handlers.common import (
    format_date_for_display,
    format_family_name,
    format_window_error,
    resolve_handler_temporal_window,
)
from water_agent.handlers.reply_utils import ResultSummary, send_result
from water_agent.metadata.discrete_families import (
    INORGANIC_CARBON_UNSUPPORTED_MSG,
    DiscreteParameter,
    get_catalog_parameter,
    get_family_for_pcode,
    get_parameter,
    is_inorganic_carbon_request,
    resolve_discrete_parameter,
)
from water_agent.metadata.models import ParameterModality
from water_agent.metadata.registry import daily_registry
from water_agent.query_planner import QueryPlan
from water_agent.results import RequestContext

logger = logging.getLogger(__name__)

LATEST_READINGS_COUNT = 5


def _format_ogc_window(dt: str) -> str:
    if "/" not in dt:
        return dt
    left, right = dt.split("/", 1)
    left_fmt = "earliest" if left.strip() == ".." else left.strip()
    right_fmt = "latest" if right.strip() == ".." else right.strip()
    return f"{left_fmt} to {right_fmt}"


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
        "qualifier": properties.get("qualifier"),
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
    if is_inorganic_carbon_request(plan.filters, plan.semantic):
        await context.reply(INORGANIC_CARBON_UNSUPPORTED_MSG, data=plan.model_dump())
        return

    if not plan.filters:
        await process.log("No observation filters could be resolved.")
        await context.reply(
            "I couldn't resolve any observation filters for that question. "
            "Please provide a monitoring station ID (e.g. 'USGS-0423205010') or a location.",
            data=plan.model_dump(),
        )
        return

    # Determine modality: check if parameter is discrete or continuous
    is_continuous = (
        plan.modality == ParameterModality.CONTINUOUS
        or (plan.modality is None and bool(plan.filters.get("statistic_id")))
    )
    param_code = plan.filters.get("parameter_code")
    query_codes: list[str] = []
    family_obj = None
    discrete_param = None

    # Handle comma-separated parameter codes (discrete multi-code queries)
    if not is_continuous and isinstance(param_code, str) and "," in param_code:
        raw_codes = [c.strip() for c in param_code.split(",") if c.strip()]
        valid_params: list[DiscreteParameter] = []
        unknown_codes: list[str] = []
        for c in raw_codes:
            p_obj = (get_parameter(c) or get_catalog_parameter(c)) if (c.isdigit() and len(c) == 5) else None
            if p_obj is not None:
                valid_params.append(p_obj)
            else:
                unknown_codes.append(c)
        if not raw_codes or unknown_codes:
            bad = ", ".join(unknown_codes) if unknown_codes else param_code
            await process.log(f"Unrecognized parameter code(s): {bad}.")
            await context.reply(
                f"Unrecognized USGS parameter code(s): {bad}. "
                "Please provide valid 5-digit USGS parameter codes.",
                data=plan.model_dump(),
            )
            return
        query_codes = [p.pcode for p in valid_params]
        discrete_param = valid_params[0]
        param_code = discrete_param.pcode
        fam_objs = [get_family_for_pcode(c) for c in query_codes]
        fam_names = {f.name for f in fam_objs if f is not None}
        if len(fam_names) == 1 and all(f is not None for f in fam_objs):
            family_obj = fam_objs[0]
    else:
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
            if p_res.status == "clarification_needed" and p_res.query_type == "unsupported":
                await context.reply(p_res.message, data=plan.model_dump())
                return
            if p_res.status == "resolved" and p_res.pcodes:
                query_codes = p_res.pcodes
                family_obj = p_res.family
                dominant = p_res.pcodes[0]
                discrete_param = get_parameter(dominant)
                param_code = dominant
                plan.filters["parameter_code"] = dominant

    spec, window_ok = await resolve_handler_temporal_window(context, process, plan)
    if not window_ok:
        return

    date_lower = spec.start.isoformat() if spec and spec.start else None
    date_upper = spec.end.isoformat() if spec and spec.end else None
    temporal_meta = spec.to_metadata() if spec is not None else None

    # 1. Discrete Samples API Path (Workflow 4)
    if discrete_param and not is_continuous:
        pcode_arg = query_codes if len(query_codes) > 1 else (query_codes[0] if query_codes else param_code)
        mod_reason = f"modality={plan.modality.value}" if plan.modality is not None else "modality=none, no statistic_id"
        await process.log(
            f"Routing to discrete Samples API (reason: {mod_reason}) "
            f"for {discrete_param.descriptor} ({param_code}) with codes: {query_codes or [param_code]}."
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
        if spec is not None:
            samples_params.update(spec.to_samples_params())

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

        interp_prefix = f"{spec.format_interpretation()}\n\n" if spec is not None else ""

        if not samples_res.records:
            await process.log("No matching discrete observations found.")
            codes_searched = query_codes if query_codes else ([param_code] if param_code else [])
            codes_str = ", ".join(codes_searched)
            zero_scope = plan.filters.get("monitoring_location_id") or "the requested area"
            zero_window = (
                f" for {date_lower or 'earliest'} to {date_upper or 'latest'}"
                if (date_lower or date_upper)
                else ""
            )
            if len(codes_searched) > 1:
                fam_label = format_family_name(family_obj.name) if family_obj else "discrete"
                head_line = f"No {fam_label} samples found (codes searched: {codes_str}) at {zero_scope}{zero_window}."
            else:
                head_line = (
                    f"No {discrete_param.descriptor} samples found "
                    f"(codes searched: {codes_str}) at {zero_scope}{zero_window}."
                )
            reply_parts = [head_line]
            if spec is not None:
                reply_parts.append(spec.format_interpretation())
            if samples_res.source_urls:
                reply_parts.append(f"Source URL: {', '.join(samples_res.source_urls)}")
            reply_parts.append("Sample media: Water (samples in other media such as sediment/tissue are excluded).")
            await context.reply(
                "\n\n".join(reply_parts),
                data=plan.model_dump(),
            )
            return

        if len(query_codes) <= 1:
            subject_desc = f"{discrete_param.descriptor} ({param_code}), {discrete_param.unit_of_measure}"
        else:
            fam_disp = format_family_name(family_obj.name) if family_obj else discrete_param.descriptor
            subject_desc = f"{fam_disp.capitalize() if family_obj else discrete_param.descriptor}"

        artifact_id = uuid.uuid4().hex
        scope = plan.filters.get("monitoring_location_id") or "the requested area"
        window_desc = (
            f"{date_lower or 'earliest'} to {date_upper or 'latest'}"
            if (date_lower or date_upper)
            else None
        )

        dated = sorted(r["activity_date"] for r in samples_res.records if r.get("activity_date"))
        highlights = [f"Sample dates range from {dated[0]} to {dated[-1]}."] if dated else []

        sorted_records = sorted(
            samples_res.records,
            key=lambda x: (x.get("activity_date") or "", x.get("activity_time") or ""),
            reverse=True,
        )
        inline_header = (
            "| Date | Time | Value | Unit | Parameter Code |\n"
            "| :--- | :--- | :--- | :--- | :--- |"
        )
        inline_items: list[str] = []
        for r in sorted_records:
            d = format_date_for_display(r.get("activity_date")) or "--"
            t = r.get("activity_time") or "--"
            v = r.get("result_value")
            val_str = f"{v:g}" if isinstance(v, (int, float)) else str(v if v is not None else "--")
            row_pcode = r.get("pcode") or param_code
            row_param = get_parameter(row_pcode)
            u = r.get("unit") or (row_param.unit_of_measure if row_param else discrete_param.unit_of_measure)
            inline_items.append(f"| {d} | {t} | {val_str} | {u} | {row_pcode} |")

        artifact_content = {
            "source_urls": samples_res.source_urls,
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "pcodes": query_codes if query_codes else [param_code],
            "count": samples_res.total_count,
            "filters": plan.filters,
            "date_lower": date_lower,
            "date_upper": date_upper,
            "temporal": temporal_meta,
            "summary": samples_res.handle.summary if samples_res.handle else None,
            "records": samples_res.records,
        }
        artifact_metadata = {
            "count": samples_res.total_count,
            "pcode": param_code,
            "pcodes": query_codes if query_codes else [param_code],
            "modality": "discrete",
            "source_urls": samples_res.source_urls,
            "retrieved_at": artifact_content["retrieved_at"],
            "artifact_id": artifact_id,
            "date_lower": date_lower,
            "date_upper": date_upper,
            "temporal": temporal_meta,
        }

        summary = ResultSummary(
            kind_label="discrete observations",
            subject=subject_desc,
            scope=scope,
            unit_label="records",
            total_available=samples_res.total_count,
            retrieved=len(samples_res.records),
            window=window_desc,
            warning=getattr(samples_res, "warning", None),
            interpretation=spec.format_interpretation() if spec is not None else None,
            highlights=highlights,
            inline_header=inline_header,
            inline_items=inline_items,
            notes=[
                "Modality: discrete sample measurements (not continuous sensors). "
                "Continuous sensor data (daily values) is a separate dataset and was not included in this answer; "
                "request daily values for this parameter and station to retrieve it.",
                "Sample media: Water (samples in other media such as sediment/tissue are excluded).",
            ],
        )

        await send_result(context, process, plan, summary, artifact_content, artifact_metadata)
        if samples_res.handle:
            ctx.store.set_artifact_id(samples_res.handle.ref, artifact_id)
        return

    # 2. Continuous Daily Values Path (Workflow 5)
    route_reasons = []
    if plan.modality == ParameterModality.CONTINUOUS:
        route_reasons.append("modality=continuous")
    elif plan.modality is None and plan.filters.get("statistic_id"):
        route_reasons.append(f"statistic_id={plan.filters['statistic_id']}")
    if not route_reasons:
        mod_label = plan.modality.value if plan.modality else "none"
        route_reasons.append(f"modality={mod_label}, no discrete parameter resolved")
    await process.log(f"Routing to continuous Daily OGC API (reason: {', '.join(route_reasons)}).")

    dropped_keys = []
    ogc_filters: dict[str, str] = {}
    for k, v in plan.filters.items():
        if k in ("time", "datetime"):
            dropped_keys.append(k)
        elif daily_registry.has(k):
            ogc_filters[k] = str(v)
        else:
            dropped_keys.append(k)
    if dropped_keys:
        logger.debug("Dropped non-queryable keys for Daily OGC API: %s", dropped_keys)

    # Build datetime interval: explicit datetime in plan.filters wins (validated as A/B), otherwise from TemporalSpec
    explicit_dt = plan.filters.get("datetime")
    spec_dt = spec.to_ogc_datetime() if spec is not None else None
    if explicit_dt not in (None, ""):
        try:
            dt: str | None = validate_datetime_interval(explicit_dt)
        except DateParseError as e:
            await process.log(f"Invalid date filter: {e}")
            await context.reply(format_window_error(e), data=plan.model_dump())
            return
        interp_text = (
            spec.format_interpretation()
            if (spec is not None and dt == spec_dt)
            else f"Interpreted date filter as {dt}."
        )
    else:
        dt = spec_dt
        interp_text = spec.format_interpretation() if spec is not None else ""

    if water_data_fetcher is not None:
        if dt:
            result = await water_data_fetcher.fetch_daily_values(
                ogc_filters, datetime=dt, limit=None, sortby="-time"
            )
        else:
            result = await water_data_fetcher.fetch_daily_values(
                ogc_filters, limit=LATEST_READINGS_COUNT, sortby="-time"
            )
    else:
        import sys
        agent_mod = sys.modules.get("agent")
        daily_cls = getattr(agent_mod, "USGSWaterDataFetcher", USGSWaterDataFetcher) if agent_mod else USGSWaterDataFetcher
        async with daily_cls() as fetcher:
            if dt:
                result = await fetcher.fetch_daily_values(
                    ogc_filters, datetime=dt, limit=None, sortby="-time"
                )
            else:
                result = await fetcher.fetch_daily_values(
                    ogc_filters, limit=LATEST_READINGS_COUNT, sortby="-time"
                )

    await process.log(
        f'Final request URL: "{result.source_urls[0] if result.source_urls else ""}"',
        data={"urls": result.source_urls},
    )

    if not result.features:
        await process.log("No matching Daily observations found.")
        zero_param = (
            plan.semantic.get("parameter")
            or (f"parameter {plan.filters.get('parameter_code')}" if plan.filters.get("parameter_code") else "this parameter")
        )
        zero_loc_parts = []
        if plan.semantic.get("county"):
            zero_loc_parts.append(plan.semantic["county"])
        if plan.semantic.get("state"):
            zero_loc_parts.append(plan.semantic["state"])
        zero_station = (
            plan.filters.get("monitoring_location_id")
            or (", ".join(zero_loc_parts) if zero_loc_parts else "the requested area")
        )
        zero_window = _format_ogc_window(dt) if dt else "recent dates"
        no_daily_msg = (
            f"No daily (continuous sensor) values were found for {zero_param} at {zero_station} for {zero_window}.\n\n"
            "Discrete sample measurements are a separate dataset; request them explicitly if needed."
        )
        if interp_text:
            no_daily_msg += f"\n\n{interp_text}"
        await context.reply(
            no_daily_msg,
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

    latest = readings[0]
    param_desc = (
        plan.semantic.get("parameter")
        or (f"parameter {plan.filters.get('parameter_code')}" if plan.filters.get("parameter_code") else "observation")
    )
    station_filter = plan.filters.get("monitoring_location_id")
    loc_parts = []
    if plan.semantic.get("county"):
        loc_parts.append(plan.semantic["county"])
    if plan.semantic.get("state"):
        loc_parts.append(plan.semantic["state"])
    if station_filter:
        scope = str(station_filter)
    elif loc_parts:
        scope = ", ".join(loc_parts)
    else:
        unique_sites = list(
            dict.fromkeys(r["monitoring_location_id"] for r in readings if r.get("monitoring_location_id"))
        )
        scope = ", ".join(unique_sites) if unique_sites and len(unique_sites) <= 3 else "the requested area"

    inline_header = (
        "| Date | Value | Unit | Station |\n"
        "| :--- | :--- | :--- | :--- |"
    )
    inline_items = [
        f"| {r.get('time') or '--'} | {r.get('value') if r.get('value') is not None else '--'} | "
        f"{r.get('unit') or '--'} | {r.get('monitoring_location_id') or '--'} |"
        for r in readings
    ]

    now_iso = datetime.now(timezone.utc).isoformat()
    artifact_content = {
        "filters": plan.filters,
        "date_lower": date_lower,
        "date_upper": date_upper,
        "temporal": temporal_meta,
        "retrieved_at": now_iso,
        "readings": readings,
    }
    artifact_metadata = {
        "filters": plan.filters,
        "count": len(readings),
        "source_urls": result.source_urls,
        "truncated": result.truncated,
        "date_lower": date_lower,
        "date_upper": date_upper,
        "temporal": temporal_meta,
        "retrieved_at": now_iso,
    }

    most_recent_line = (
        f"Most recent value: {latest['value']} {latest['unit']} on {latest['time']} at "
        f"{latest['monitoring_location_id']}."
    )

    if dt:
        dates = [r["time"] for r in readings if r.get("time")]
        earliest_date = min(dates) if dates else "unknown"
        latest_date = max(dates) if dates else "unknown"
        warning = (
            f"capped at {len(readings):,} by the agent; narrow the date range for the rest"
            if result.truncated
            else None
        )
        summary = ResultSummary(
            kind_label="daily values",
            subject=param_desc,
            scope=scope,
            unit_label="daily values",
            total_available=None if result.truncated else len(readings),
            retrieved=len(readings),
            window=_format_ogc_window(dt),
            warning=warning,
            interpretation=interp_text or None,
            highlights=[
                f"Dates range from {earliest_date} to {latest_date}.",
                most_recent_line,
            ],
            inline_header=inline_header,
            inline_items=inline_items,
        )
    else:
        summary = ResultSummary(
            kind_label="daily values",
            subject=param_desc,
            scope=scope,
            unit_label="daily values",
            total_available=len(readings),
            retrieved=len(readings),
            window=f"latest {len(readings)}",
            warning=None,
            highlights=[most_recent_line],
            inline_header=inline_header,
            inline_items=inline_items,
        )

    await send_result(context, process, plan, summary, artifact_content, artifact_metadata)
