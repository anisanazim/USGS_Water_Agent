import logging
from datetime import datetime
from typing import Any
from usgs_water_data import USGSWaterDataFetcher
from water_agent.fetchers.samples_fetcher import USGSSamplesFetcher
from water_agent.results.summarize import summarize_inventory

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
    """
    Looks up what parameters are monitored at a given USGS station.
    """
    
    result_kind = "inventory"
    
    @staticmethod
    def summarize(result: dict) -> tuple[str, list[dict], int]:
        continuous_params = result.get("parameters", {})
        discrete_params = result.get("discrete_parameters", {})
        summary, sample = summarize_inventory(continuous_params)
        
        if discrete_params:
            discrete_total = result.get("discrete_total_observations", 0)
            discrete_groups = list(discrete_params.keys())
            discrete_char_count = sum(len(items) for items in discrete_params.values())
            summary += f"; discrete: {discrete_total:,} obs across {discrete_char_count} characteristics in {len(discrete_groups)} groups ({', '.join(discrete_groups[:3])})"
            for grp, items in discrete_params.items():
                for itm in items[:2]:
                    sample.append({
                        "modality": "discrete",
                        "group": grp,
                        "name": itm["descriptor"],
                        "count": itm["count"],
                    })
        total_count = len(continuous_params) + sum(len(items) for items in discrete_params.values())
        return summary, sample, total_count
    
    def __init__(
        self,
        fetcher: USGSWaterDataFetcher,
        samples_fetcher: USGSSamplesFetcher | None = None,
    ):
        self.fetcher = fetcher
        self.samples_fetcher = samples_fetcher
        
    async def get_inventory(self, monitoring_location_id: str) -> dict[str, Any]:
        """Summarize available parameters and date ranges at a station.
        
        Returns a dictionary containing:
        - "parameters": mapping parameter codes to their name, begin/end dates, and estimated records
        - "request_url": the USGS API URL hit
        """
        filters = {
            "monitoring_location_id": monitoring_location_id
        }
        
        result = await self.fetcher.fetch_time_series_metadata(filters=filters, limit=10000)
        
        inventory = {}
        
        for feature in result.features:
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

        # Fetch discrete summary from USGS Samples API using managed fetcher
        station_id_norm = monitoring_location_id.strip()
        if not station_id_norm.startswith("USGS-") and station_id_norm.isdigit():
            station_id_norm = f"USGS-{station_id_norm}"

        if self.samples_fetcher is not None:
            discrete_inventory, total_discrete_observations, summary_url = (
                await self.samples_fetcher.fetch_station_summary(station_id_norm)
            )
        else:
            async with USGSSamplesFetcher() as sf:
                discrete_inventory, total_discrete_observations, summary_url = (
                    await sf.fetch_station_summary(station_id_norm)
                )

        from water_agent.dto import (
            ContinuousParameterInventory,
            DiscreteCharacteristicItem,
            StationInventoryResult,
        )

        typed_parameters = {
            k: ContinuousParameterInventory(**v) for k, v in inventory.items()
        }
        typed_discrete = {
            k: [DiscreteCharacteristicItem(**item) if isinstance(item, dict) else item for item in items]
            for k, items in discrete_inventory.items()
        }

        return StationInventoryResult(
            monitoring_location_id=station_id_norm,
            parameters=typed_parameters,
            discrete_parameters=typed_discrete,
            discrete_total_observations=total_discrete_observations,
            request_url=result.source_urls[0] if result.source_urls else "",
            discrete_summary_url=summary_url,
        )

