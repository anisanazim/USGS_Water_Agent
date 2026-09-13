import pytest
from unittest.mock import AsyncMock, MagicMock
from water_agent.tools.spatial_query import SpatialQueryTool

@pytest.fixture
def mock_fetcher():
    fetcher = MagicMock()
    # Setup async mock methods
    fetcher.fetch_time_series_metadata = AsyncMock()
    fetcher.fetch_monitoring_locations = AsyncMock()
    return fetcher

@pytest.fixture
def mock_resolver():
    resolver = MagicMock()
    return resolver

@pytest.mark.asyncio
async def test_find_stations_state_only(mock_fetcher, mock_resolver):
    # Setup mock data for time-series-metadata
    mock_fetcher.fetch_time_series_metadata.return_value = {
        "features": [
            {"properties": {"monitoring_location_id": "USGS-123"}},
            {"properties": {"monitoring_location_id": "USGS-456"}},
            {"properties": {"monitoring_location_id": "USGS-123"}}, # Duplicate to test deduplication
        ]
    }

    tool = SpatialQueryTool(mock_fetcher, mock_resolver)
    
    stations = await tool.find_stations("00300", "Florida")
    
    assert stations == ["USGS-123", "USGS-456"]
    
    # Verify the fetcher was called correctly
    mock_fetcher.fetch_time_series_metadata.assert_called_once_with(
        filters={"parameter_code": "00300", "state_name": "Florida"},
        limit=2000
    )
    # Resolver should not be called for state-only
    mock_resolver.resolve.assert_not_called()

@pytest.mark.asyncio
async def test_find_stations_with_county(mock_fetcher, mock_resolver):
    # Mock resolver responses
    def mock_resolve(field, value):
        result = MagicMock()
        result.status = "resolved"
        if field == "state_code":
            result.resolved_value = "12"
        elif field == "county_code":
            result.resolved_value = "12073"
        return result
    
    mock_resolver.resolve.side_effect = mock_resolve
    
    # Mock monitoring-locations (all stations in Leon County)
    mock_fetcher.fetch_monitoring_locations.return_value = {
        "features": [
            {"id": "USGS-111"},
            {"id": "USGS-222"},
            {"id": "USGS-333"},
        ]
    }
    
    # Mock time-series-metadata (all DO stations in Florida)
    mock_fetcher.fetch_time_series_metadata.return_value = {
        "features": [
            {"properties": {"monitoring_location_id": "USGS-222"}},
            {"properties": {"monitoring_location_id": "USGS-333"}},
            {"properties": {"monitoring_location_id": "USGS-444"}}, # Not in Leon County
        ]
    }
    
    tool = SpatialQueryTool(mock_fetcher, mock_resolver)
    stations = await tool.find_stations("00300", "Florida", county_name="Leon County")
    
    # Should intersect (222, 333)
    assert stations == ["USGS-222", "USGS-333"]
    
    mock_fetcher.fetch_monitoring_locations.assert_called_once_with(
        filters={"state_code": "12", "county_code": "12073"},
        limit=2000
    )
    
    mock_fetcher.fetch_time_series_metadata.assert_called_once_with(
        filters={"parameter_code": "00300", "state_name": "Florida"},
        limit=10000
    )

@pytest.mark.asyncio
async def test_find_stations_unresolved_county(mock_fetcher, mock_resolver):
    # Mock resolver responses to fail on county
    def mock_resolve(field, value):
        result = MagicMock()
        if field == "state_code":
            result.status = "resolved"
            result.resolved_value = "12"
        elif field == "county_code":
            result.status = "not_found"
        return result
    
    mock_resolver.resolve.side_effect = mock_resolve
    
    tool = SpatialQueryTool(mock_fetcher, mock_resolver)
    
    with pytest.raises(ValueError, match="Could not resolve county name"):
        await tool.find_stations("00300", "Florida", county_name="Invalid County")
