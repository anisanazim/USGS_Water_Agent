from __future__ import annotations

import pytest

from usgs_water_data import PagedResult
from water_agent.dto import (
    ContinuousDailyReading,
    ContinuousParameterInventory,
    DiscreteCharacteristicItem,
    DiscreteObservationRecord,
    ParameterBreakdownItem,
    SpatialQueryResult,
    SpatialStationDTO,
    StationInventoryResult,
)
from water_agent.fetchers.samples_fetcher import DiscreteFetchResult


def test_spatial_query_result_dto():
    station1 = SpatialStationDTO(
        id="USGS-02087500",
        name="NEUSE RIVER NEAR CLAYTON, NC",
        site_type="Stream",
        county="Johnston County",
        latitude=35.65,
        longitude=-78.41,
    )
    breakdown = ParameterBreakdownItem(
        pcode="00915",
        descriptor="Calcium, water, filtered, milligrams per liter",
        unit="mg/l",
        station_count=42,
    )

    res = SpatialQueryResult(
        stations=[station1],
        request_url="https://api.waterdata.usgs.gov/samples-data/locations/site",
        modality="discrete",
        family="calcium",
        pcodes=["00915"],
        pcode_breakdown=[breakdown],
        summary="Found 1 stations monitoring calcium",
    )

    # 1. Typed attribute access
    assert res.modality == "discrete"
    assert res.family == "calcium"
    assert len(res.stations) == 1
    assert res.stations[0].id == "USGS-02087500"
    assert res.pcode_breakdown[0].station_count == 42
    assert res.station_ids == ["USGS-02087500"]

    # 2. Transparent dict subscripting for legacy consumer compatibility
    assert res["modality"] == "discrete"
    assert res["family"] == "calcium"
    assert res["stations"][0]["id"] == "USGS-02087500"
    assert "stations" in res
    assert res.get("non_existent_key", "default") == "default"

    # 3. Model dump / JSON serialization
    dump = res.model_dump()
    assert dump["modality"] == "discrete"
    assert dump["stations"][0]["id"] == "USGS-02087500"


def test_station_inventory_result_dto():
    param_cont = ContinuousParameterInventory(
        name="Discharge, cubic feet per second",
        begin="1990-01-01",
        end="2024-01-01",
        estimated_daily_records=12418,
    )
    char_disc = DiscreteCharacteristicItem(
        characteristic="Calcium",
        descriptor="Calcium, dissolved, mg/L",
        count=85,
        first_activity="1995-04-12",
        most_recent_activity="2023-11-05",
    )

    res = StationInventoryResult(
        monitoring_location_id="USGS-02087500",
        parameters={"00060": param_cont},
        discrete_parameters={"Inorganics, Major, Metals": [char_disc]},
        discrete_total_observations=85,
        request_url="https://api.waterdata.usgs.gov/time-series-metadata",
    )

    # Typed access
    assert res.monitoring_location_id == "USGS-02087500"
    assert res.parameters["00060"].name == "Discharge, cubic feet per second"
    assert res.discrete_parameters["Inorganics, Major, Metals"][0].count == 85
    assert res.discrete_total_observations == 85

    # Dict-like subscription
    assert res["monitoring_location_id"] == "USGS-02087500"
    assert res["parameters"]["00060"]["name"] == "Discharge, cubic feet per second"
    assert res["discrete_parameters"]["Inorganics, Major, Metals"][0]["count"] == 85


def test_paged_result_to_daily_readings():
    raw_feature = {
        "properties": {
            "monitoring_location_id": "USGS-02087500",
            "parameter_code": "00060",
            "statistic_id": "00003",
            "value": 142.5,
            "unit": "ft3/s",
            "time": "2024-05-01",
            "approval_status": "Approved",
        }
    }
    paged = PagedResult(features=[raw_feature], source_urls=["https://test.url"])

    readings = paged.to_daily_readings()
    assert len(readings) == 1
    r = readings[0]
    assert isinstance(r, ContinuousDailyReading)
    assert r.monitoring_location_id == "USGS-02087500"
    assert r.value == 142.5
    assert r.unit == "ft3/s"
    assert r["value"] == 142.5  # Dict accessible


def test_discrete_fetch_result_to_observation_records():
    raw_record = {
        "activity_date": "2023-06-15",
        "activity_time": "10:30:00",
        "result_value": 14.2,
        "unit": "mg/l",
        "pcode": "00915",
        "characteristic_name": "Calcium",
        "location_id": "USGS-02087500",
    }
    fetch_result = DiscreteFetchResult(
        records=[raw_record],
        total_count=1,
        source_urls=["https://test.samples.url"],
    )

    records = fetch_result.to_observation_records()
    assert len(records) == 1
    rec = records[0]
    assert isinstance(rec, DiscreteObservationRecord)
    assert rec.activity_date == "2023-06-15"
    assert rec.result_value == 14.2
    assert rec.unit == "mg/l"
    assert rec["pcode"] == "00915"


def test_dict_accessible_model_contains_semantics():
    res_empty = SpatialQueryResult(stations=[])
    assert "stations" in res_empty
    assert "error" not in res_empty
    assert res_empty.get("error") is None

    res_error = SpatialQueryResult(stations=[], error="USGS API timeout")
    assert "error" in res_error
    assert res_error.get("error") == "USGS API timeout"
    assert res_error["error"] == "USGS API timeout"
