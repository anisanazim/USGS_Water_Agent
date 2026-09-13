from typing import Any
from usgs_water_data import USGSWaterDataFetcher

class StationInventoryTool:
    """Tool to discover what parameters are available at a specific monitoring location.
    
    Queries time-series-metadata for the station and summarizes the available
    parameters, including their date ranges.
    """
    
    def __init__(self, fetcher: USGSWaterDataFetcher):
        self.fetcher = fetcher
        
    async def get_inventory(self, monitoring_location_id: str) -> dict[str, dict[str, str]]:
        """Summarize available parameters and date ranges at a station.
        
        Returns a dictionary mapping parameter names to their begin/end dates.
        Example: {"Gage height": {"begin": "2000-01-01", "end": "2023-01-01"}}
        """
        filters = {
            "monitoring_location_id": monitoring_location_id
        }
        
        result = await self.fetcher.fetch_time_series_metadata(filters=filters, limit=2000)
        
        inventory = {}
        
        for feature in result.get("features", []):
            props = feature.get("properties", {})
            param_name = props.get("parameter_name")
            
            if not param_name:
                continue
                
            begin = props.get("begin", "Unknown")
            end = props.get("end", "Unknown")
            
            # Since a station might have multiple time series for the same parameter (e.g. different statistics),
            # we'll want to find the overall earliest begin and latest end date.
            if param_name not in inventory:
                inventory[param_name] = {"begin": begin, "end": end}
            else:
                current_begin = inventory[param_name]["begin"]
                current_end = inventory[param_name]["end"]
                
                # Simple string comparison works for ISO 8601 dates (e.g., "2020-01-01")
                if begin != "Unknown" and (current_begin == "Unknown" or begin < current_begin):
                    inventory[param_name]["begin"] = begin
                if end != "Unknown" and (current_end == "Unknown" or end > current_end):
                    inventory[param_name]["end"] = end
                    
        return inventory
