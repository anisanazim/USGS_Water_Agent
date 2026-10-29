import pytest
import httpx
from unittest.mock import AsyncMock, MagicMock, patch

from water_agent.fetchers.samples_fetcher import (
    USGSSamplesFetcher,
    SamplesRateLimitError,
    SamplesValidationError,
    SamplesOverflowError,
    to_iso_date,
)
from water_agent.results import RequestContext


def test_to_iso_date():
    assert to_iso_date("2023-05-15") == "2023-05-15"
    assert to_iso_date("5/15/2023") == "2023-05-15"
    assert to_iso_date("01/02/1995") == "1995-01-02"
    assert to_iso_date(None) is None
    assert to_iso_date("") is None


@pytest.mark.asyncio
async def test_count_first_guard_zero_records():
    """If count is 0, fetch_results returns immediately without calling the data endpoint."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)

    # Mock response for /results/count
    count_resp = httpx.Response(
        status_code=200,
        json={"recordCount": 0},
        headers={"x-ratelimit-remaining": "3999", "x-ratelimit-limit": "4000"},
        request=httpx.Request("GET", "https://api.waterdata.usgs.gov/samples-data/results/count"),
    )
    mock_client.get.return_value = count_resp
    mock_client.build_request.return_value = httpx.Request(
        "GET", "https://api.waterdata.usgs.gov/samples-data/results/narrow"
    )

    fetcher = USGSSamplesFetcher(client=mock_client)
    res = await fetcher.fetch_results({"stateFips": "US:37", "usgsPCode": "00918"})

    assert res.total_count == 0
    assert len(res.records) == 0
    # Crucial assertion: stream was NEVER called because count == 0
    assert not mock_client.stream.called


@pytest.mark.asyncio
async def test_fetch_results_streaming_projection_and_store():
    """Valid response streams, projects fields, converts dates, and stores in ResultStore."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)

    # Mock count endpoint
    count_resp = httpx.Response(
        status_code=200,
        json={"recordCount": 2},
        headers={"x-ratelimit-remaining": "3990", "x-ratelimit-limit": "4000"},
        request=httpx.Request("GET", "https://api.waterdata.usgs.gov/samples-data/results/count"),
    )
    mock_client.get.return_value = count_resp
    mock_client.build_request.return_value = httpx.Request(
        "GET", "https://api.waterdata.usgs.gov/samples-data/results/narrow?stateFips=US:37&usgsPCode=00915"
    )

    csv_data = [
        "Activity_StartDate,Activity_StartTime,Result_Measure,Result_MeasureUnit,USGSpcode,Result_Characteristic,Location_Identifier,Location_Name,Extra_Col_1,Extra_Col_2\n",
        "2/14/2021,10:30:00,12.5,mg/l,00915,Calcium,USGS-02087500,Neuse River,ignore1,ignore2\n",
        "2021-03-01,11:00:00,14.0,mg/l,00915,Calcium,USGS-02087500,Neuse River,ignore3,ignore4\n",
    ]

    # Mock streaming response
    mock_stream_resp = AsyncMock()
    mock_stream_resp.status_code = 200
    mock_stream_resp.raise_for_status = MagicMock()
    mock_stream_resp.headers = {"x-ratelimit-remaining": "3989", "x-ratelimit-limit": "4000"}

    async def aiter_lines():
        for line in csv_data:
            yield line

    mock_stream_resp.aiter_lines = aiter_lines
    mock_client.stream.return_value.__aenter__.return_value = mock_stream_resp

    context = RequestContext(request_id="req-test-1")
    fetcher = USGSSamplesFetcher(client=mock_client)

    result = await fetcher.fetch_results(
        {"stateFips": "US:37", "usgsPCode": "00915"},
        context=context,
        pcode="00915",
    )

    assert result.total_count == 2
    assert len(result.records) == 2

    # Check projection & date conversion
    r0 = result.records[0]
    assert r0["activity_date"] == "2021-02-14"
    assert r0["result_value"] == 12.5
    assert r0["unit"] == "mg/l"
    assert r0["pcode"] == "00915"
    assert r0["location_id"] == "USGS-02087500"
    assert "Extra_Col_1" not in r0

    r1 = result.records[1]
    assert r1["activity_date"] == "2021-03-01"
    assert r1["result_value"] == 14.0

    # Check ResultHandle contract
    assert result.handle is not None
    assert result.handle.kind == "samples_results"
    assert result.handle.count == 2
    assert "discrete records of Calcium, water, filtered (00915), mg/l" in result.handle.summary
    assert "discrete sample data" in result.handle.summary

    # Verify stored in ResultStore
    stored = context.store.get(result.handle.ref, expected_kind="samples_results")
    assert stored.handle.ref == result.handle.ref
    assert stored.provenance["pcode"] == "00915"
    assert stored.provenance["unit_of_measure"] == "mg/l"


@pytest.mark.asyncio
async def test_headers_only_zero_data_rows():
    """200 response with headers only returns an empty result (not an error)."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)

    count_resp = httpx.Response(
        status_code=200,
        json={"recordCount": 0},
        headers={"x-ratelimit-remaining": "3990", "x-ratelimit-limit": "4000"},
        request=httpx.Request("GET", "https://api.waterdata.usgs.gov/samples-data/results/count"),
    )
    mock_client.get.return_value = count_resp
    mock_client.build_request.return_value = httpx.Request(
        "GET", "https://api.waterdata.usgs.gov/samples-data/results/narrow"
    )

    fetcher = USGSSamplesFetcher(client=mock_client)
    res = await fetcher.fetch_results({"stateFips": "US:37", "usgsPCode": "00918"})
    assert res.total_count == 0
    assert res.records == []


@pytest.mark.asyncio
async def test_rate_limit_429_aborts_immediately():
    """HTTP 429 raises SamplesRateLimitError without retrying."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)

    count_resp = httpx.Response(
        status_code=429,
        text="Rate limit exceeded",
        headers={"x-ratelimit-limit": "4000", "x-ratelimit-remaining": "0"},
        request=httpx.Request("GET", "https://api.waterdata.usgs.gov/samples-data/results/count"),
    )
    mock_client.get.return_value = count_resp

    fetcher = USGSSamplesFetcher(client=mock_client)
    with pytest.raises(SamplesRateLimitError) as exc_info:
        await fetcher.fetch_results({"stateFips": "US:37", "usgsPCode": "00915"})
    assert "429 Too Many Requests" in str(exc_info.value)


@pytest.mark.asyncio
async def test_invalid_parameter_400():
    """HTTP 400 with 'Invalid parameter' raises SamplesValidationError."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)

    count_resp = httpx.Response(
        status_code=400,
        text="400 - Invalid parameter ['badParam']",
        request=httpx.Request("GET", "https://api.waterdata.usgs.gov/samples-data/results/count"),
    )
    mock_client.get.return_value = count_resp

    fetcher = USGSSamplesFetcher(client=mock_client)
    with pytest.raises(SamplesValidationError) as exc_info:
        await fetcher.fetch_results({"badParam": "foo"})
    assert "Invalid parameter" in str(exc_info.value)


@pytest.mark.asyncio
async def test_overflow_guard_aborts_when_not_partitionable():
    """When query exceeds max_records and cannot be partitioned, raises SamplesOverflowError."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)

    count_resp = httpx.Response(
        status_code=200,
        json={"recordCount": 65000},
        headers={"x-ratelimit-remaining": "3990", "x-ratelimit-limit": "4000"},
        request=httpx.Request("GET", "https://api.waterdata.usgs.gov/samples-data/results/count"),
    )
    mock_client.get.return_value = count_resp

    # No state or county filter provided -> cannot partition by county
    fetcher = USGSSamplesFetcher(client=mock_client, max_records=5000)
    with pytest.raises(SamplesOverflowError) as exc_info:
        await fetcher.fetch_results({"monitoringLocationIdentifier": "USGS-01"})

    assert "exceeds the safe threshold" in str(exc_info.value)
    assert exc_info.value.count == 65000
    assert exc_info.value.threshold == 5000


def test_citation_matches_pcode_count_not_family_aggregate():
    """Assert that a citation's record count matches that pcode's own count, never a family aggregate."""
    from water_agent.metadata.discrete_families import DISCRETE_FAMILIES, get_parameter

    # Check pH family: 00400 has 43,972 in NC; 00403 has 10,767; total family is 54,739
    ph_family = DISCRETE_FAMILIES["ph"]
    total_family_records = sum(p.verified_records_nc for p in ph_family.parameters.values())
    assert total_family_records == 54739

    p00400 = get_parameter("00400")
    assert p00400 is not None
    assert p00400.verified_records_nc == 43972

    # Test formatting citation directly
    citation_00400 = p00400.format_citation(record_count=p00400.verified_records_nc)
    assert "43,972 records of pH, water, unfiltered, field (00400)" in citation_00400
    assert "std units" in citation_00400
    # Crucial assertion: Must NOT contain the family total (54,739) or the illustrative typo 80,412
    assert "54,739" not in citation_00400
    assert "80,412" not in citation_00400

    # Test via ResultStore in fetcher
    context = RequestContext(request_id="test-ph")
    fetcher = USGSSamplesFetcher()
    fake_records = [
        {"activity_date": "2023-01-01", "result_value": 7.2, "unit": "std units", "pcode": "00400", "location_id": f"USGS-{i}"}
        for i in range(10)
    ]
    res = fetcher._build_results_result(
        records=fake_records,
        total_count=43972,
        source_urls=["https://test.url"],
        pcode="00400",
        context=context,
    )
    assert res.handle is not None
    handle_obj = context.store.get(res.handle.ref, expected_kind="samples_results")
    assert "43,972 discrete records of pH, water, unfiltered, field (00400)" in res.handle.summary
    assert "std units" in res.handle.summary
    assert "54,739" not in res.handle.summary
    assert "80,412" not in res.handle.summary
    assert handle_obj.provenance["pcode"] == "00400"


def test_spatial_calcium_query_returns_family_union_not_dominant_alone():
    """Assert that all queries for calcium return all family pcodes without auto-resolving to 00915 alone."""
    from water_agent.metadata.discrete_families import resolve_discrete_parameter

    spatial_res = resolve_discrete_parameter("calcium", query_type="spatial")
    assert spatial_res.status == "resolved"
    assert set(spatial_res.pcodes) == {"00910", "00915", "00916", "00918"}

    value_res = resolve_discrete_parameter("calcium", query_type="value")
    assert value_res.status == "resolved"
    assert set(value_res.pcodes) == {"00910", "00915", "00916", "00918"}

    count_res = resolve_discrete_parameter("calcium", query_type="count")
    assert count_res.status == "resolved"
    assert set(count_res.pcodes) == {"00910", "00915", "00916", "00918"}


@pytest.mark.asyncio
async def test_timeout_recovers_via_temporal_partitioning_lossless():
    """When direct query times out, fetcher recovers by partitioning temporally and retrieving 100% of records."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)

    # Count says 500 records
    count_resp = httpx.Response(
        status_code=200,
        json={"recordCount": 500},
        headers={"x-ratelimit-remaining": "3990", "x-ratelimit-limit": "4000"},
        request=httpx.Request("GET", "https://api.waterdata.usgs.gov/samples-data/results/count"),
    )
    mock_client.get.return_value = count_resp

    fetcher = USGSSamplesFetcher(client=mock_client)

    calls = []
    async def mock_stream_project(url, params):
        calls.append(dict(params))
        if len(calls) == 1:
            # First call without date filter times out
            raise httpx.ReadTimeout("Timeout waiting for server")
        # Subsequent temporal partition calls return chunks
        return [
            {
                "activity_date": f"2023-0{len(calls)}-01",
                "result_value": 350.0 + len(calls),
                "unit": "uS/cm @25C",
                "pcode": "00095",
                "location_id": f"USGS-0208750{len(calls)}",
            }
        ]

    fetcher._stream_and_project_results = mock_stream_project

    res = await fetcher.fetch_results(
        {"stateFips": "US:37", "usgsPCode": "00095", "activityMediaName": "Water"},
        pcode="00095",
    )

    # Assertions
    # First call had no date bounds; subsequent calls partitioned temporally with date bounds
    assert len(calls) > 1
    assert "activityStartDateLower" not in calls[0]
    assert "activityStartDateLower" in calls[1]
    assert "activityStartDateUpper" in calls[1]

    # Verify lossless partitioned return
    assert res.partitioned is True
    assert len(res.records) == len(calls) - 1
    assert res.total_count == len(res.records)


@pytest.mark.asyncio
async def test_timeout_raises_explicit_error_if_partitioning_also_fails():
    """When direct query and temporal partitioning both fail, raises explicit SamplesError."""
    from water_agent.fetchers.samples_fetcher import SamplesError

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    count_resp = httpx.Response(
        status_code=200,
        json={"recordCount": 500},
        headers={"x-ratelimit-remaining": "3990", "x-ratelimit-limit": "4000"},
        request=httpx.Request("GET", "https://api.waterdata.usgs.gov/samples-data/results/count"),
    )
    mock_client.get.return_value = count_resp

    fetcher = USGSSamplesFetcher(client=mock_client)

    async def always_fails_stream(url, params):
        raise httpx.ReadTimeout("Persistent timeout")

    fetcher._stream_and_project_results = always_fails_stream

    with pytest.raises(SamplesError, match="Failed to retrieve discrete observations"):
        await fetcher.fetch_results(
            {"stateFips": "US:37", "usgsPCode": "00095", "activityMediaName": "Water"},
            pcode="00095",
        )


@pytest.mark.asyncio
async def test_large_query_exceeding_threshold_triggers_temporal_partitioning():
    """Queries exceeding MAX_RESULTS_THRESHOLD (25,000) partition temporally rather than single streaming."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)

    # Initial count says 40,000 records
    count_resp = httpx.Response(
        status_code=200,
        json={"recordCount": 40000},
        headers={"x-ratelimit-remaining": "3990", "x-ratelimit-limit": "4000"},
        request=httpx.Request("GET", "https://api.waterdata.usgs.gov/samples-data/results/count"),
    )
    mock_client.get.return_value = count_resp

    fetcher = USGSSamplesFetcher(client=mock_client)

    partition_called = False
    async def mock_partition_temporally(params, profile, initial_count, context, pcode):
        nonlocal partition_called
        partition_called = True
        return fetcher._build_results_result(
            records=[{"activity_date": "2023-01-01", "result_value": 1.0, "unit": "mg/l", "pcode": "00915"}],
            total_count=1,
            source_urls=["https://test.url"],
            pcode=pcode,
            context=context,
            partitioned=True,
        )

    fetcher._partition_results_temporally = mock_partition_temporally

    res = await fetcher.fetch_results(
        {"stateFips": "US:37", "usgsPCode": "00915"},
        pcode="00915",
    )

    assert partition_called is True
    assert res.partitioned is True


def test_comma_separated_pcode_split_guard():
    """Guard verifies that comma-separated usgsPCode values are split into list of strings."""
    fetcher = USGSSamplesFetcher()
    
    # Comma-separated string
    raw_params = {"stateFips": "US:37", "usgsPCode": "00665,00660, 00671"}
    clean = fetcher._normalize_params(raw_params)
    assert clean["usgsPCode"] == ["00665", "00660", "00671"]
    
    # Already a list
    list_params = {"stateFips": "US:37", "usgsPCode": ["00665", "00660"]}
    clean2 = fetcher._normalize_params(list_params)
    assert clean2["usgsPCode"] == ["00665", "00660"]
    
    # Single string without comma
    single_params = {"stateFips": "US:37", "usgsPCode": "00665"}
    clean3 = fetcher._normalize_params(single_params)
    assert clean3["usgsPCode"] == "00665"


@pytest.mark.asyncio
async def test_repeated_parameter_union_equals_per_pcode_loop_union_phosphorus():
    """Assert repeated-parameter union == per-pcode loop union for the phosphorus family."""
    pcode_data = {
        "00665": [{"location_id": "USGS-1"}, {"location_id": "USGS-2"}],
        "00660": [{"location_id": "USGS-2"}, {"location_id": "USGS-3"}],
        "00671": [{"location_id": "USGS-3"}, {"location_id": "USGS-4"}],
        "00666": [{"location_id": "USGS-1"}, {"location_id": "USGS-5"}],
        "00650": [{"location_id": "USGS-5"}],
        "70507": [{"location_id": "USGS-6"}],
    }

    # 1. Per-pcode loop union
    loop_union_ids = set()
    for p, locs in pcode_data.items():
        for item in locs:
            loop_union_ids.add(item["location_id"])

    # 2. Repeated-parameter simulated response (server returns all combined)
    all_server_rows = []
    for locs in pcode_data.values():
        all_server_rows.extend(locs)

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    # Mock count for repeated params
    mock_client.get.return_value = httpx.Response(
        status_code=200,
        json={"recordCount": len(loop_union_ids)},
        headers={"x-ratelimit-remaining": "3999", "x-ratelimit-limit": "4000"},
        request=httpx.Request("GET", "https://api.waterdata.usgs.gov/samples-data/locations/count"),
    )
    fetcher = USGSSamplesFetcher(client=mock_client)
    fetcher._stream_and_project_locations = AsyncMock(return_value=all_server_rows)

    phosphorus_pcodes = list(pcode_data.keys())
    res = await fetcher.fetch_locations(
        {"stateFips": "US:37", "usgsPCode": phosphorus_pcodes},
        pcode=phosphorus_pcodes,
    )

    repeated_union_ids = {r["location_id"] for r in res.records}
    assert repeated_union_ids == loop_union_ids
    assert repeated_union_ids == {"USGS-1", "USGS-2", "USGS-3", "USGS-4", "USGS-5", "USGS-6"}

