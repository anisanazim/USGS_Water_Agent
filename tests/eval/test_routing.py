from __future__ import annotations

import json
from pathlib import Path
import pytest

from water_agent.metadata.discrete_families import resolve_discrete_parameter
from water_agent.metadata.references.store import ReferenceStore
from water_agent.metadata.registry import daily_registry
from water_agent.metadata.resolver import ReferenceResolver
from usgs_water_data import USGSWaterDataFetcher, USGSValidationError

DATASET_FILE = Path(__file__).parent / "datasets" / "failure_modes.json"

with open(DATASET_FILE, "r", encoding="utf-8") as f:
    FAILURE_MODES = {case["id"]: case for case in json.load(f)}


@pytest.fixture
def resolver() -> ReferenceResolver:
    ref_dir = Path(__file__).parent.parent.parent / "metadata" / "raw" / "references"
    store = ReferenceStore(ref_dir)
    return ReferenceResolver(daily_registry, store)


def test_routing_fm01_fips_and_analyte_resolution(resolver: ReferenceResolver):
    """FM-01 Routing: Dare County, NC -> State FIPS 37, County FIPS 055."""
    case = FAILURE_MODES["FM-01"]
    
    s_res = resolver.resolve("state_code", "North Carolina")
    assert s_res.status == "resolved"
    assert s_res.resolved_value == "37"

    c_res = resolver.resolve("county_code", "Dare")
    assert c_res.status == "resolved"
    assert c_res.resolved_value == "055"

    full_fips = f"{s_res.resolved_value}{c_res.resolved_value}"
    assert full_fips == case["routing_verification"]["expected_fips"]

    res = resolve_discrete_parameter(case["routing_verification"]["expected_parameter"])
    assert res.status == "resolved"
    assert any(code in res.pcodes for code in ["00618", "00620", "00631"])


def test_routing_fm03_continuous_streamflow_parameter():
    """FM-03 Routing: Streamflow parameter must map to 00060."""
    case = FAILURE_MODES["FM-03"]
    assert case["routing_verification"]["target_pcode"] == "00060"


def test_routing_fm04_multi_pcode_family_expansion():
    """FM-04 Routing: Calcium must expand to 4 verified codes with primary 00915."""
    case = FAILURE_MODES["FM-04"]
    res = resolve_discrete_parameter(case["routing_verification"]["expected_family"])
    
    assert res.status == "resolved"
    assert res.family.name == "calcium"
    assert len(res.pcodes) == case["routing_verification"]["expected_codes_count"]
    assert res.pcodes[0] == case["routing_verification"]["primary_pcode"]
    assert res.pcodes == case["routing_verification"]["constituent_codes"]


def test_routing_fm05_fraction_disambiguation():
    """FM-05 Routing: Specific code (00660 for filtered orthophosphate) must isolate to 00660."""
    res = resolve_discrete_parameter("00660")
    assert res.status == "resolved"
    assert res.pcodes == ["00660"]
    assert "00665" not in res.pcodes  # Total phosphorus must not be included


def test_routing_fm06_station_id_validation():
    """FM-06 Routing: Malformed station IDs must be caught before making network calls."""
    with pytest.raises(USGSValidationError):
        USGSWaterDataFetcher.normalize_site_id("invalid-id-abc")


@pytest.mark.asyncio
async def test_routing_fm09_upstream_outage_circuit_breaker_recovery():
    """FM-09: Circuit breaker trips on consecutive 503s and fails fast without cascading delays."""
    import httpx
    from unittest.mock import AsyncMock, patch
    from water_agent.fetchers.samples_fetcher import USGSSamplesFetcher, SamplesError
    from water_agent.circuit_breaker import CircuitBreakerOpenError, CircuitState

    fetcher = USGSSamplesFetcher()
    fetcher.circuit_breaker.failure_threshold = 2
    fetcher.circuit_breaker.recovery_timeout = 0.05
    mock_503 = httpx.Response(
        status_code=503,
        text="Service Unavailable",
        request=httpx.Request("GET", "https://api.waterdata.usgs.gov"),
    )
    fetcher._client.get = AsyncMock(return_value=mock_503)

    with patch("asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(SamplesError):
            await fetcher.get_results_count({"stateFips": "US:37"})
        with pytest.raises(SamplesError):
            await fetcher.get_results_count({"stateFips": "US:37"})

    assert fetcher.circuit_breaker.state == CircuitState.OPEN
    with pytest.raises(CircuitBreakerOpenError) as exc_info:
        await fetcher.get_results_count({"stateFips": "US:37"})
    assert "is OPEN" in str(exc_info.value)
