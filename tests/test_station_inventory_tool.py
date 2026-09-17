import pytest
from unittest.mock import AsyncMock, MagicMock
from water_agent.tools.station_inventory import StationInventoryTool

@pytest.fixture
def mock_fetcher():
    fetcher = MagicMock()
    fetcher.fetch_time_series_metadata = AsyncMock()
    return fetcher

@pytest.mark.asyncio
async def test_get_inventory(mock_fetcher):
    # Setup mock data for time-series-metadata
    mock_fetcher.fetch_time_series_metadata.return_value = {
        "features": [
            {
                "properties": {
                    "parameter_code": "00060",
                    "parameter_name": "Discharge",
                    "begin": "2000-01-01T00:00:00Z",
                    "end": "2023-01-01T00:00:00Z"
                }
            },
            {
                "properties": {
                    "parameter_code": "00060",
                    "parameter_name": "Discharge",
                    "begin": "1995-01-01T00:00:00Z",
                    "end": "2010-01-01T00:00:00Z"
                }
            },
            {
                "properties": {
                    "parameter_code": "00065",
                    "parameter_name": "Gage height"
                }
            },
            {
                "properties": {
                    "parameter_code": "00300",
                    "parameter_name": "Dissolved oxygen",
                    "begin": "2020-05-01T00:00:00Z"
                }
            }
        ]
    }

    tool = StationInventoryTool(mock_fetcher)
    
    inventory_result = await tool.get_inventory("USGS-12345678")
    inventory = inventory_result["parameters"]
    
    assert "00060" in inventory
    assert inventory["00060"]["name"] == "Discharge"
    # It should pick the earliest begin and latest end
    assert inventory["00060"]["begin"] == "1995-01-01T00:00:00Z"
    assert inventory["00060"]["end"] == "2023-01-01T00:00:00Z"
    assert inventory["00060"]["estimated_daily_records"] == 10228
    
    assert "00065" in inventory
    assert inventory["00065"]["name"] == "Gage height"
    assert inventory["00065"]["begin"] == "Unknown"
    assert inventory["00065"]["end"] == "Unknown"
    assert inventory["00065"]["estimated_daily_records"] is None
    
    assert "00300" in inventory
    assert inventory["00300"]["name"] == "Dissolved oxygen"
    assert inventory["00300"]["begin"] == "2020-05-01T00:00:00Z"
    assert inventory["00300"]["end"] == "Unknown"
    assert inventory["00300"]["estimated_daily_records"] is None
    
    # Verify the fetcher was called correctly
    mock_fetcher.fetch_time_series_metadata.assert_called_once_with(
        filters={"monitoring_location_id": "USGS-12345678"},
        limit=2000
    )

@pytest.mark.asyncio
async def test_get_inventory_empty(mock_fetcher):
    mock_fetcher.fetch_time_series_metadata.return_value = {"features": []}
    tool = StationInventoryTool(mock_fetcher)
    inventory_result = await tool.get_inventory("USGS-000")
    assert inventory_result["parameters"] == {}
