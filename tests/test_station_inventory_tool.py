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
            # Discharge (00060) multiple time ranges (e.g. daily mean vs daily max)
            {"properties": {"parameter_name": "Discharge", "begin": "2000-01-01", "end": "2010-01-01"}},
            {"properties": {"parameter_name": "Discharge", "begin": "1995-01-01", "end": "2023-01-01"}},
            # Gage height (00065) missing begin/end
            {"properties": {"parameter_name": "Gage height"}},
            # Dissolved oxygen
            {"properties": {"parameter_name": "Dissolved oxygen", "begin": "2020-05-01", "end": "Unknown"}}
        ]
    }

    tool = StationInventoryTool(mock_fetcher)
    
    inventory = await tool.get_inventory("USGS-12345678")
    
    assert "Discharge" in inventory
    # It should pick the earliest begin and latest end
    assert inventory["Discharge"]["begin"] == "1995-01-01"
    assert inventory["Discharge"]["end"] == "2023-01-01"
    
    assert "Gage height" in inventory
    assert inventory["Gage height"]["begin"] == "Unknown"
    assert inventory["Gage height"]["end"] == "Unknown"
    
    assert "Dissolved oxygen" in inventory
    assert inventory["Dissolved oxygen"]["begin"] == "2020-05-01"
    assert inventory["Dissolved oxygen"]["end"] == "Unknown"
    
    # Verify the fetcher was called correctly
    mock_fetcher.fetch_time_series_metadata.assert_called_once_with(
        filters={"monitoring_location_id": "USGS-12345678"},
        limit=2000
    )

@pytest.mark.asyncio
async def test_get_inventory_empty(mock_fetcher):
    mock_fetcher.fetch_time_series_metadata.return_value = {"features": []}
    tool = StationInventoryTool(mock_fetcher)
    inventory = await tool.get_inventory("USGS-000")
    assert inventory == {}
