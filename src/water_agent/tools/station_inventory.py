import logging
from datetime import datetime
from typing import Any
from usgs_water_data import USGSWaterDataFetcher

logger = logging.getLogger(__name__)

def calc_days(begin: str, end: str) -> int | None:
    if begin == "Unknown" or end == "Unknown":
        return None
    try:
        b = datetime.fromisoformat(begin.replace("Z", "+00:00"))
        e = datetime.fromisoformat(end.replace("Z", "+00:00"))
        return max(0, (e - b).days + 1)
    except (ValueError, TypeError):
        return None

class StationInventoryTool:
    """Tool to discover what parameters are available at a specific monitoring location.
    
    Queries time-series-metadata for the station and summarizes the available
    parameters, including their date ranges.
    """
    
    def __init__(self, fetcher: USGSWaterDataFetcher):
        self.fetcher = fetcher
        
    async def get_inventory(self, monitoring_location_id: str) -> dict[str, Any]:
        """Summarize available parameters and date ranges at a station.
        
        Returns a dictionary containing:
        - "parameters": mapping parameter codes to their name, begin/end dates, and estimated records
        - "request_url": the USGS API URL hit
        """
        filters = {
            "monitoring_location_id": monitoring_location_id
        }
        
        result = await self.fetcher.fetch_time_series_metadata(filters=filters, limit=2000)
        
        inventory = {}
        
        for feature in result.get("features", []):
            props = feature.get("properties", {})
            param_name = props.get("parameter_name")
            param_code = props.get("parameter_code")
            
            if not param_name or not param_code:
                continue
                
            begin = props.get("begin", "Unknown")
            end = props.get("end", "Unknown")
            
            # Since a station might have multiple time series for the same parameter (e.g. different statistics),
            # we'll want to find the overall earliest begin and latest end date.
            if param_code not in inventory:
                inventory[param_code] = {
                    "name": param_name,
                    "begin": begin,
                    "end": end
                }
            else:
                current_begin = inventory[param_code]["begin"]
                current_end = inventory[param_code]["end"]
                
                # Simple string comparison works for ISO 8601 dates (e.g., "2020-01-01")
                if begin != "Unknown" and (current_begin == "Unknown" or begin < current_begin):
                    inventory[param_code]["begin"] = begin
                if end != "Unknown" and (current_end == "Unknown" or end > current_end):
                    inventory[param_code]["end"] = end
                    
        # Calculate estimated records after merging all date ranges
        for param_data in inventory.values():
            param_data["estimated_daily_records"] = calc_days(param_data["begin"], param_data["end"])
                    
        return {
            "parameters": inventory,
            "request_url": result.get("request_url", ""),
        }
