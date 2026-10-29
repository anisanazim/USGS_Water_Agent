from typing import Any
import json
from pydantic import BaseModel
from usgs_water_data import USGSWaterDataFetcher
from water_agent.results.summarize import summarize_stations
from water_agent.metadata.discrete_families import (
    DISCRETE_FAMILIES,
    get_family_for_pcode,
    get_parameter,
    is_mixed_modality,
    resolve_discrete_parameter,
)
from water_agent.metadata.resolver import ReferenceResolver

class SpatialQueryTool:
    """Tool to discover monitoring locations for a given parameter in a geographic area.
    
    If the geographic constraint is a state, we can query time-series-metadata directly.
    If it's a county (which time-series-metadata doesn't support filtering by natively),
    we perform a local intersection: all stations in the county intersected with all
    stations in the state that monitor the parameter.
    """
    
    result_kind = "stations"
    
    @staticmethod
    def summarize(result: dict) -> tuple[str, list[dict], int]:
        summary, sample = summarize_stations(result["stations"])
        return summary, sample, len(result["stations"])

    def __init__(
        self,
        fetcher: USGSWaterDataFetcher,
        resolver: ReferenceResolver,
        discrete_tool: Any | None = None,
        samples_fetcher: Any | None = None,
    ):
        self.fetcher = fetcher
        self.resolver = resolver
        if discrete_tool is not None:
            self.discrete_tool = discrete_tool
        else:
            from water_agent.tools.discrete_query import DiscreteQueryTool
            from water_agent.fetchers.samples_fetcher import USGSSamplesFetcher
            self.discrete_tool = DiscreteQueryTool(
                fetcher=samples_fetcher or USGSSamplesFetcher(), resolver=resolver
            )
        
    async def find_stations(self, parameter_code: str, state_name: str, county_name: str | None = None, huc: str | None = None) -> dict[str, Any]:
        """Find monitoring locations that have data for the specified parameter in the specified geography."""
        from water_agent.metadata.discrete_families import (
            get_family_for_pcode,
            get_parameter,
            resolve_discrete_parameter,
        )

        # Modality check: if parameter is discrete (including mixed modality parameters per Step 3d),
        # route deterministically to DiscreteQueryTool
        from water_agent.metadata.discrete_families import is_mixed_modality
        is_discrete = (
            get_parameter(parameter_code) is not None
            or get_family_for_pcode(parameter_code) is not None
            or resolve_discrete_parameter(parameter_code).status == "resolved"
        )
        if is_discrete:
            res = await self.discrete_tool.find_stations(
                parameter=parameter_code,
                state_name=state_name,
                county_name=county_name,
            )
            if res.get("status") == "error":
                return {"error": res.get("error", "Error in discrete spatial query")}

            from water_agent.dto import (
                ParameterBreakdownItem,
                SpatialQueryResult,
                SpatialStationDTO,
            )

            normalized_stations = []
            for s in res.get("stations", []):
                sid = s.get("location_id") or s.get("id")
                normalized_stations.append(
                    SpatialStationDTO(
                        id=sid,
                        name=s.get("location_name") or s.get("name"),
                        site_type=s.get("site_type"),
                        county=s.get("county_name") or s.get("county"),
                        latitude=s.get("latitude"),
                        longitude=s.get("longitude"),
                    )
                )

            breakdown_items = [
                ParameterBreakdownItem(**b) if isinstance(b, dict) else b
                for b in res.get("pcode_breakdown", [])
            ]

            return SpatialQueryResult(
                stations=normalized_stations,
                request_url=res.get("source_urls", [""])[0] if res.get("source_urls") else "",
                metadata_url="",
                source_urls=res.get("source_urls", []),
                modality="discrete",
                family=res.get("family"),
                pcodes=res.get("pcodes", []),
                pcode_breakdown=breakdown_items,
                mixed_modality=res.get("mixed_modality", False),
                summary=res.get("summary", ""),
            )

        # Continuous OGC path
        state_resolve = self.resolver.resolve("state_code", state_name)
        if state_resolve.status != "resolved":
            return {"error": f"Could not resolve state name '{state_name}' to a state code."}
        state_code = state_resolve.resolved_value

        if not state_code:
            return {"error": f"Could not resolve state name '{state_name}' to a state code."}
            
        if not county_name:
            filters = {
                "parameter_code": parameter_code,
                "state_name": state_name,
            }
            if huc:
                filters["huc"] = huc
                
            result = await self.fetcher.fetch_time_series_metadata(filters=filters, limit=10000)
            
            # Extract unique monitoring_location_ids
            station_ids = {
                f.get("properties", {}).get("monitoring_location_id")
                for f in result.features
                if f.get("properties", {}).get("monitoring_location_id")
            }
            
            return {
                "stations": sorted(list(station_ids)),
                "request_url": result.source_urls[0] if result.source_urls else "",
            }
            
        # If county is provided, we need to intersect.ion for county queries
        # First, we need to resolve the state and county to their codes to query monitoring-locations
        state_resolve = self.resolver.resolve("state_code", state_name)
        if state_resolve.status != "resolved":
            raise ValueError(f"Could not resolve state name: {state_name}")
        state_code = state_resolve.resolved_value
        
        county_resolve = self.resolver.resolve("county_code", county_name)
        if county_resolve.status != "resolved":
            raise ValueError(f"Could not resolve county name: {county_name}")
        county_code = county_resolve.resolved_value
        
        # Step A: Get all stations in the county
        ml_filters = {
            "state_code": state_code,
            "county_code": county_code
        }
        result = await self.fetcher.fetch_monitoring_locations(filters=ml_filters, limit=2000)
        county_station_ids = {f["id"] for f in result.features if "id" in f}
        
        if not county_station_ids:
            return {
                "stations": [],
                "request_url": result.source_urls[0] if result.source_urls else "",
                "metadata_url": ""
            }
            
        # Step B: Get all stations in the state that monitor this parameter
        ts_filters = {
            "parameter_code": parameter_code,
            "state_name": state_name
        }
        metadata_result = await self.fetcher.fetch_time_series_metadata(filters=ts_filters, limit=10000)
        metadata_station_ids = {f["properties"]["monitoring_location_id"] for f in metadata_result.features if "monitoring_location_id" in f["properties"]}
        
        # 3. Intersect
        valid_stations = county_station_ids.intersection(metadata_station_ids)
        
        enriched_stations = []
        for f in result.features:
            if f.get("id") in valid_stations:
                props = f.get("properties", {})
                enriched_stations.append({
                    "id": f["id"],
                    "name": props.get("monitoring_location_name"),
                    "site_type": props.get("site_type"),
                    "county": props.get("county_name")
                })
        
        # Sort by ID for consistency
        enriched_stations.sort(key=lambda x: x["id"])
        
        return {
            "stations": enriched_stations,
            "request_url": result.source_urls[0] if result.source_urls else "",
            "metadata_url": metadata_result.source_urls[0] if metadata_result.source_urls else ""
        }
