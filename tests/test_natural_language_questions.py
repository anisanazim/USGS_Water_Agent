import re

from agent import USGSWaterAgent


def test_parse_latest_discharge_question():
    agent = USGSWaterAgent()
    parsed = agent._parse_question("What is the latest discharge at site 02329000")

    assert parsed["site_id"] == "02329000"
    assert parsed["parameter_code"] == "00060"
    assert parsed["parameter_name"] == "discharge"


def test_parse_latest_gage_height_question():
    agent = USGSWaterAgent()
    parsed = agent._parse_question("Give me the most recent gage height at USGS-10172640")

    assert parsed["site_id"] == "USGS-10172640"
    assert parsed["parameter_code"] == "00065"
    assert parsed["parameter_name"] == "gage height"


def test_parse_latest_water_temperature_in_florida_question():
    agent = USGSWaterAgent()
    parsed = agent._parse_question("Show the latest water temperature in Florida")

    assert parsed["state_name"] == "Florida"
    assert parsed["parameter_code"] == "00010"
    assert parsed["parameter_name"] == "water temperature"
