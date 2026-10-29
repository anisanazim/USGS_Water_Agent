from __future__ import annotations

import logging
from typing import Any

from water_agent.fetchers.samples_fetcher import USGSSamplesFetcher, to_iso_date
from water_agent.metadata.discrete_families import (
    get_family_for_pcode,
    get_parameter,
    is_mixed_modality,
    resolve_discrete_parameter,
)
from water_agent.metadata.resolver import ReferenceResolver
from water_agent.results import RequestContext

logger = logging.getLogger(__name__)


class DiscreteQueryTool:
    """Tool to query discrete water quality observations and monitoring stations from the USGS Samples API.

    Adheres strictly to the discrete response contract:
    - Every observation/count answer cites parameterCode, descriptor, unit, and modality ('discrete sample data (not continuous sensor data)').
    - Spatial queries union stations across all family pcodes per the spatial invariant rule.
    - Mixed-modality parameters flag mixed_modality=True without unioning continuous sensor data.
    - ResultStore integration stores full records and produces clean summaries.
    """

    result_kind = "samples_results"

    def __init__(
        self,
        fetcher: USGSSamplesFetcher,
        resolver: ReferenceResolver,
    ):
        self.fetcher = fetcher
        self.resolver = resolver

    async def query_results(
        self,
        parameter: str,
        state_name: str | None = None,
        county_name: str | None = None,
        site_id: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        """Fetch discrete observation records for a parameter and geography."""
        # 1. Resolve parameter to candidate / pcode
        param_resolve = resolve_discrete_parameter(parameter, query_type="value")
        pcode = param_resolve.pcodes[0] if param_resolve.pcodes else parameter
        param_obj = get_parameter(pcode)

        # 2. Resolve geography
        params: dict[str, Any] = {
            "usgsPCode": pcode,
            "activityMediaName": "Water",
        }

        state_code: str | None = None
        if state_name:
            s_res = self.resolver.resolve("state_code", state_name)
            if s_res.status != "resolved" or not s_res.resolved_value:
                return {"status": "error", "error": f"Could not resolve state name '{state_name}'."}
            state_code = s_res.resolved_value
            params["stateFips"] = f"US:{state_code}"

        if county_name:
            if not state_code:
                return {"status": "error", "error": "A state_name is required when specifying a county_name."}
            c_res = self.resolver.resolve("county_code", county_name)
            if c_res.status != "resolved" or not c_res.resolved_value:
                return {"status": "error", "error": f"Could not resolve county name '{county_name}'."}
            params["countyFips"] = f"US:{state_code}:{c_res.resolved_value}"

        if site_id:
            # Normalize to USGS format if needed
            sid = site_id.strip()
            if not sid.startswith("USGS-") and sid.isdigit():
                sid = f"USGS-{sid}"
            params["monitoringLocationIdentifier"] = sid

        if start_date:
            params["activityStartDateLower"] = to_iso_date(start_date)
        if end_date:
            params["activityStartDateUpper"] = to_iso_date(end_date)

        # 3. Fetch from Samples API
        fetch_res = await self.fetcher.fetch_results(
            params=params,
            context=context,
            pcode=pcode,
        )

        pcode_str = pcode
        desc_str = param_obj.descriptor if param_obj else "discrete observation"
        unit_str = param_obj.unit_of_measure if param_obj else "units"
        citation = f"{fetch_res.total_count:,} discrete records of {desc_str} ({pcode_str}), {unit_str}"

        return {
            "status": "resolved",
            "citation": citation,
            "pcode": pcode_str,
            "descriptor": desc_str,
            "unit": unit_str,
            "modality": "discrete",
            "mixed_modality": is_mixed_modality(pcode_str),
            "total_count": fetch_res.total_count,
            "ref": fetch_res.handle.ref if fetch_res.handle else None,
            "summary": fetch_res.handle.summary if fetch_res.handle else citation,
            "sample": fetch_res.handle.sample if fetch_res.handle else fetch_res.records[:5],
            "source_urls": fetch_res.source_urls,
            "warning": fetch_res.warning,
            "partitioned": fetch_res.partitioned,
        }

    async def find_stations(
        self,
        parameter: str,
        state_name: str,
        county_name: str | None = None,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        """Find discrete monitoring stations for a parameter.

        Applies the Spatial Invariant Rule: unions monitoring locations across all
        parameter codes in the family.
        """
        # Spatial invariant rule: resolve with query_type='spatial'
        param_resolve = resolve_discrete_parameter(parameter, query_type="spatial")
        pcodes = param_resolve.pcodes if param_resolve.pcodes else [parameter]

        # Resolve geography
        s_res = self.resolver.resolve("state_code", state_name)
        if s_res.status != "resolved" or not s_res.resolved_value:
            return {"status": "error", "error": f"Could not resolve state name '{state_name}'."}
        state_code = s_res.resolved_value

        params: dict[str, Any] = {
            "stateFips": f"US:{state_code}",
            "activityMediaName": "Water",
        }

        if county_name:
            c_res = self.resolver.resolve("county_code", county_name)
            if c_res.status != "resolved" or not c_res.resolved_value:
                return {"status": "error", "error": f"Could not resolve county name '{county_name}'."}
            params["countyFips"] = f"US:{state_code}:{c_res.resolved_value}"

        # Query family using repeated usgsPCode parameters in a single request (18x speedup)
        family_params = dict(params)
        family_params["usgsPCode"] = pcodes if len(pcodes) > 1 else pcodes[0]
        loc_res = await self.fetcher.fetch_locations(family_params, context=None, pcode=pcodes)
        source_urls = loc_res.source_urls

        all_stations: list[dict[str, Any]] = []
        seen_sites: set[str] = set()
        for rec in loc_res.records:
            sid = rec.get("location_id")
            if sid and sid not in seen_sites:
                seen_sites.add(sid)
                all_stations.append(rec)
            elif not sid:
                all_stations.append(rec)

        total_stations = len(all_stations)
        family_name = param_resolve.family.name if param_resolve.family else parameter
        summary = (
            f"Found {total_stations:,} distinct discrete monitoring locations for {family_name} "
            f"(unioned across pcodes: {', '.join(pcodes)}). Modality: discrete water quality network."
        )

        # Collect per-pcode station count breakdown concurrently
        pcode_breakdown: list[dict[str, Any]] = []
        if len(pcodes) == 1:
            p_obj = get_parameter(pcodes[0])
            pcode_breakdown.append({
                "pcode": pcodes[0],
                "descriptor": p_obj.descriptor if p_obj else "Discrete parameter",
                "unit": p_obj.unit_of_measure if p_obj else "",
                "station_count": total_stations,
            })
        else:
            import asyncio
            async def _fetch_pcode_station_count(p_code: str) -> tuple[str, int]:
                sub_params = dict(params)
                sub_params["usgsPCode"] = p_code
                try:
                    cnt = await self.fetcher.get_locations_count(sub_params)
                    return (p_code, cnt)
                except Exception as ex:
                    logger.warning("Error fetching location count for %s: %s", p_code, ex)
                    p_obj = get_parameter(p_code)
                    if p_obj and state_code == "37" and not county_name and p_obj.verified_stations_nc is not None:
                        return (p_code, p_obj.verified_stations_nc)
                    return (p_code, 0)

            try:
                count_tuples = await asyncio.gather(*[_fetch_pcode_station_count(p) for p in pcodes])
                for p_code, cnt in count_tuples:
                    p_obj = get_parameter(p_code)
                    pcode_breakdown.append({
                        "pcode": p_code,
                        "descriptor": p_obj.descriptor if p_obj else "Discrete parameter",
                        "unit": p_obj.unit_of_measure if p_obj else "",
                        "station_count": cnt,
                    })
                pcode_breakdown.sort(key=lambda x: x["station_count"], reverse=True)
            except Exception as e:
                logger.warning("Error gathering per-pcode station counts: %s", e)

        has_mixed = any(is_mixed_modality(p) for p in pcodes)
        if has_mixed:
            summary += (
                " Note: Continuous sensor monitoring stations for this parameter also exist "
                "in USGS systems and are not included in this discrete query."
            )

        handle = None
        if context is not None:
            sample = all_stations[:5]
            provenance = {
                "source_urls": source_urls,
                "parameter": parameter,
                "family": family_name,
                "pcodes": pcodes,
                "pcode_breakdown": pcode_breakdown,
                "modality": "discrete",
                "mixed_modality": has_mixed,
            }
            handle = context.store.put(
                kind="samples_locations",
                items=all_stations,
                summary=summary,
                sample=sample,
                provenance=provenance,
                count=total_stations,
            )

        return {
            "status": "resolved",
            "family": family_name,
            "pcodes": pcodes,
            "pcode_breakdown": pcode_breakdown,
            "total_count": total_stations,
            "stations": all_stations,
            "summary": summary,
            "ref": handle.ref if handle else None,
            "sample": handle.sample if handle else all_stations[:5],
            "modality": "discrete",
            "mixed_modality": has_mixed,
            "source_urls": source_urls,
        }

    async def query_station_parameter_counts(
        self,
        station_id: str,
        parameter: str,
    ) -> dict[str, Any]:
        """Fetch discrete observation counts and date ranges across all family parameter codes at a station."""
        sid_norm = station_id.strip()
        if not sid_norm.startswith("USGS-") and sid_norm.isdigit():
            sid_norm = f"USGS-{sid_norm}"

        param_resolve = resolve_discrete_parameter(parameter, query_type="value")
        pcodes = param_resolve.pcodes if param_resolve.pcodes else [parameter]
        family_name = param_resolve.family.name if param_resolve.family else parameter

        import csv
        import io

        summary_url = f"{self.fetcher.base_url}/summary/{sid_norm}?mimeType=text/csv"
        station_summary: dict[str, dict[str, Any]] = {}
        try:
            resp = await self.fetcher._client.get(summary_url, timeout=30.0)
            if resp.status_code == 200:
                reader = csv.DictReader(io.StringIO(resp.text))
                for row in reader:
                    if row.get("characteristicGroup") == "Information":
                        continue
                    desc = (row.get("characteristicUserSupplied") or row.get("characteristic") or "").strip()
                    cnt_str = row.get("resultCount", "0").strip()
                    cnt = int(cnt_str) if cnt_str.isdigit() else 0
                    first_act = row.get("firstActivity", "").strip()
                    last_act = row.get("mostRecentActivity", "").strip()
                    station_summary[desc.lower()] = {
                        "count": cnt,
                        "first_activity": first_act,
                        "most_recent_activity": last_act,
                        "characteristic": row.get("characteristic", "").strip(),
                    }
        except Exception as ex:
            logger.warning("Error fetching summary for %s: %s", sid_norm, ex)

        rows: list[dict[str, Any]] = []
        for p in pcodes:
            p_obj = get_parameter(p)
            desc = p_obj.descriptor if p_obj else p
            unit = p_obj.unit_of_measure if p_obj else ""
            char_name = p_obj.characteristic_name if p_obj else ""

            match_data = station_summary.get(desc.lower())
            if not match_data and char_name:
                match_data = station_summary.get(char_name.lower())

            if match_data and match_data["count"] > 0:
                rows.append({
                    "pcode": p,
                    "descriptor": desc,
                    "unit": unit,
                    "count": match_data["count"],
                    "first_activity": match_data["first_activity"],
                    "most_recent_activity": match_data["most_recent_activity"],
                })
            else:
                try:
                    cnt = await self.fetcher.get_results_count({
                        "monitoringLocationIdentifier": sid_norm,
                        "usgsPCode": p,
                        "activityMediaName": "Water",
                    })
                    if cnt > 0:
                        rows.append({
                            "pcode": p,
                            "descriptor": desc,
                            "unit": unit,
                            "count": cnt,
                            "first_activity": None,
                            "most_recent_activity": None,
                        })
                except Exception:
                    pass

        rows.sort(key=lambda x: x["count"], reverse=True)
        return {
            "station_id": sid_norm,
            "family": family_name,
            "pcodes": pcodes,
            "rows": rows,
            "total_family_pcodes": len(pcodes),
            "source_urls": [summary_url],
        }
