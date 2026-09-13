from typing import Any
import json
from pydantic import BaseModel
from usgs_water_data import USGSWaterDataFetcher
from water_agent.metadata.resolver import ReferenceResolver

class SpatialQueryTool:
    """Tool to discover monitoring locations for a given parameter in a geographic area.
    
    If the geographic constraint is a state, we can query time-series-metadata directly.
    If it's a county (which time-series-metadata doesn't support filtering by natively),
    we perform a local intersection: all stations in the county intersected with all
    stations in the state that monitor the parameter.
    """
    
    def __init__(self, fetcher: USGSWaterDataFetcher, resolver: ReferenceResolver):
        self.fetcher = fetcher
        self.resolver = resolver
        
    async def find_stations(self, parameter_code: str, state_name: str, county_name: str | None = None, huc: str | None = None) -> list[str]:
        """Find monitoring locations that have data for the specified parameter in the specified geography."""
        # Note: In a real complex application we would map state names to codes and county names to codes
        # via the ReferenceStore. For this workflow, the LLM will provide the parameter_code directly
        # and the state_name.
        
        # Scenario 1: Native time-series-metadata query
        if not county_name:
            filters = {
                "parameter_code": parameter_code,
                "state_name": state_name
            }
            if huc:
                filters["hydrologic_unit_code"] = huc
                
            result = await self.fetcher.fetch_time_series_metadata(filters=filters, limit=2000)
            stations = {f["properties"]["monitoring_location_id"] for f in result["features"] if "monitoring_location_id" in f["properties"]}
            return sorted(list(stations))
            
        # Scenario 2: Two-step intersection for county queries
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
        ml_result = await self.fetcher.fetch_monitoring_locations(filters=ml_filters, limit=2000)
        county_stations = {f["id"] for f in ml_result["features"] if "id" in f}
        
        if not county_stations:
            return []
            
        # Step B: Get all stations in the state that monitor this parameter
        ts_filters = {
            "parameter_code": parameter_code,
            "state_name": state_name
        }
        ts_result = await self.fetcher.fetch_time_series_metadata(filters=ts_filters, limit=10000)
        param_stations = {f["properties"]["monitoring_location_id"] for f in ts_result["features"] if "monitoring_location_id" in f["properties"]}
        
        # Intersect
        matching_stations = county_stations.intersection(param_stations)
        return sorted(list(matching_stations))
