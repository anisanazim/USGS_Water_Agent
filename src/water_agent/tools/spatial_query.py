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
        
    async def find_stations(self, parameter_code: str, state_name: str, county_name: str | None = None, huc: str | None = None) -> dict[str, Any]:
        """Find monitoring locations that have data for the specified parameter in the specified geography."""
        # Note: In a real complex application we would map state names to codes and county names to codes
        # via the ReferenceStore. For this workflow, the LLM will provide the parameter_code directly
        state_resolve = self.resolver.resolve("state_code", state_name)
        if state_resolve.status != "resolved":
            return {"error": f"Could not resolve state name '{state_name}' to a state code."}
        state_code = state_resolve.resolved_value

        if not state_code:
            return {"error": f"Could not resolve state name '{state_name}' to a state code."}
            
        if not county_name:
            filters = {
                "parameter_code": parameter_code,
                "state_code": state_code,
            }
            if huc:
                filters["huc"] = huc
                
            result = await self.fetcher.fetch_time_series_metadata(filters=filters, limit=2000)
            features = result.get("features", [])
            
            # Extract unique monitoring_location_ids
            station_ids = {
                f.get("properties", {}).get("monitoring_location_id")
                for f in features
                if f.get("properties", {}).get("monitoring_location_id")
            }
            
            return {
                "stations": sorted(list(station_ids)),
                "request_url": result.get("request_url", ""),
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
        county_station_ids = {f["id"] for f in result.get("features", []) if "id" in f}
        
        if not county_station_ids:
            return {
                "stations": [],
                "request_url": result.get("request_url", ""),
                "metadata_url": ""
            }
            
        # Step B: Get all stations in the state that monitor this parameter
        ts_filters = {
            "parameter_code": parameter_code,
            "state_name": state_name
        }
        metadata_result = await self.fetcher.fetch_time_series_metadata(filters=ts_filters, limit=10000)
        metadata_station_ids = {f["properties"]["monitoring_location_id"] for f in metadata_result["features"] if "monitoring_location_id" in f["properties"]}
        
        # 3. Intersect
        valid_stations = county_station_ids.intersection(metadata_station_ids)
        
        return {
            "stations": sorted(list(valid_stations)),
            "request_url": result.get("request_url", ""),
            "metadata_url": metadata_result.get("request_url", "")
        }
