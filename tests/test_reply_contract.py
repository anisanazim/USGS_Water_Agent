from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from usgs_water_data import PagedResult, USGSWaterDataFetcher
from water_agent.fetchers.samples_fetcher import DiscreteFetchResult, USGSSamplesFetcher
from water_agent.handlers.count import handle_parameter_count
from water_agent.handlers.inventory import handle_inventory
from water_agent.handlers.observation import handle_observation
from water_agent.handlers.spatial import handle_spatial
from water_agent.query_planner import QueryPlan
from water_agent.results import RequestContext

FORBIDDEN_SUBSTRINGS = [
    "…and",
    "...and",
    "more)",
    "more in this group",
    "Most recent 10",
    "Continuous sensor data is also available",
    "saved to artifact.)",
    "To view the actual numerical measurements",
]


def _make_mocks():
    process = MagicMock()
    process.log = AsyncMock()
    process.create_artifact = AsyncMock()
    context = MagicMock()
    context.reply = AsyncMock()
    return context, process


async def _run_case(handler_kind: str, n: int):
    """Execute the specified handler with `n` fake items.

    Returns (reply_text, reply_data, artifact_description, all_tokens, tail_tokens)
    where:
      - all_tokens: every item token expected to appear when n <= inline_items_max
      - tail_tokens: item tokens from the list/end that must NOT appear when n > inline_items_max
    """
    context, process = _make_mocks()
    ctx = RequestContext(request_id=f"contract-{handler_kind}-{n}")

    if handler_kind == "discrete_observation":
        base = date(2022, 1, 1)
        records = [
            {
                "activity_date": (base + timedelta(days=i)).isoformat(),
                "activity_time": "10:00:00",
                "result_value": 1001.5 + i,
                "unit": "mg/l",
                "pcode": "00300",  # mixed-modality pcode to verify no continuous-sensor prompt
            }
            for i in range(n)
        ]
        fetcher = AsyncMock(spec=USGSSamplesFetcher)
        fetcher.fetch_results = AsyncMock(
            return_value=DiscreteFetchResult(
                records=records,
                total_count=n,
                source_urls=["https://example.test/narrow"],
            )
        )
        plan = QueryPlan(
            intent="observation",
            modality="discrete",
            filters={
                "monitoring_location_id": "USGS-0423205010",
                "parameter_code": "00300",
            },
        )
        await handle_observation(context, process, plan, ctx, samples_fetcher=fetcher)
        all_tokens = [f"{1001.5 + i:g}" for i in range(n)]
        # Exclude first/last dates (which legitimately appear in date-span highlight);
        # check values and interior dates from the end of the list.
        tail_tokens = [f"{1001.5 + i:g}" for i in range(max(0, n - 5), n)]
        if n >= 10:
            tail_tokens.append((base + timedelta(days=n - 3)).isoformat())

    elif handler_kind == "daily_bounded":
        base = date(2022, 1, 1)
        # Newest first (matching sortby="-time")
        features = [
            {
                "properties": {
                    "monitoring_location_id": "USGS-02087500",
                    "parameter_code": "00060",
                    "value": 2001.5 + i,
                    "unit_of_measure": "ft3/s",
                    "time": (base + timedelta(days=i)).isoformat(),
                }
            }
            for i in reversed(range(n))
        ]
        fetcher = AsyncMock(spec=USGSWaterDataFetcher)
        fetcher.fetch_daily_values = AsyncMock(
            return_value=PagedResult(
                features=features,
                source_urls=["https://example.test/daily"],
                truncated=False,
            )
        )
        plan = QueryPlan(
            intent="observation",
            modality="continuous",
            filters={
                "monitoring_location_id": "USGS-02087500",
                "parameter_code": "00060",
                "start_date": "2022-01-01",
                "end_date": "2022-04-01",
            },
            semantic={"parameter": "streamflow"},
        )
        await handle_observation(context, process, plan, ctx, water_data_fetcher=fetcher)
        all_tokens = [f"{2001.5 + i:g}" for i in range(n)]
        # Exclude index n-1 (newest / "Most recent value" highlight) and index 0 date (earliest highlight);
        # check older/end items from the table.
        tail_tokens = [f"{2001.5 + i:g}" for i in range(min(5, n - 1))]
        if n >= 10:
            tail_tokens.append((base + timedelta(days=2)).isoformat())

    elif handler_kind == "spatial_discrete":
        stations = [
            {
                "id": f"USGS-9000{i:04d}",
                "name": f"SiteAlpha-{i:03d}",
                "site_type": "Stream",
                "county": "Wake County",
            }
            for i in range(1, n + 1)
        ]
        handle = ctx.store.put(
            kind="stations",
            items={
                "stations": stations,
                "modality": "discrete",
                "family": "ph",
                "pcodes": ["00400", "00403"],
                "mixed_modality": True,
                "pcode_breakdown": [
                    {"pcode": "00400", "descriptor": "pH, field", "unit": "std units", "station_count": n}
                ],
            },
            summary=f"{n} stations",
            sample=[],
            provenance={"modality": "discrete", "source_urls": ["https://example.test/locations"]},
            count=n,
        )
        plan = QueryPlan(
            intent="spatial_discovery",
            modality="discrete",
            stations_ref=handle.ref,
            filters={"parameter_code": "00400", "state_code": "37"},
            semantic={"parameter": "pH", "state": "North Carolina"},
        )
        await handle_spatial(context, process, plan, ctx)
        all_tokens = [f"USGS-9000{i:04d}" for i in range(1, n + 1)]
        tail_tokens = [f"USGS-9000{i:04d}" for i in range(max(1, n - 4), n + 1)]

    elif handler_kind == "spatial_continuous":
        stations = [
            {
                "id": f"USGS-8000{i:04d}",
                "name": f"GageBeta-{i:03d}",
                "site_type": "Stream",
                "county": "Wake County",
            }
            for i in range(1, n + 1)
        ]
        handle = ctx.store.put(
            kind="stations",
            items={"stations": stations, "modality": "continuous"},
            summary=f"{n} stations",
            sample=[],
            provenance={"modality": "continuous", "source_urls": ["https://example.test/monitoring-locations"]},
            count=n,
        )
        plan = QueryPlan(
            intent="spatial_discovery",
            modality="continuous",
            stations_ref=handle.ref,
            filters={"parameter_code": "00060", "state_code": "37"},
            semantic={"parameter": "streamflow", "state": "North Carolina"},
        )
        await handle_spatial(context, process, plan, ctx)
        all_tokens = [f"USGS-8000{i:04d}" for i in range(1, n + 1)]
        tail_tokens = [f"USGS-8000{i:04d}" for i in range(max(1, n - 4), n + 1)]

    elif handler_kind == "inventory_general":
        discrete_items = [
            {
                "characteristic": f"CharItem-{i:03d}",
                "descriptor": f"CharItem-{i:03d}, water",
                "count": 100 + i,
                "first_activity": "2010-01-01",
                "most_recent_activity": "2023-01-01",
            }
            for i in range(1, n + 1)
        ]
        handle = ctx.store.put(
            kind="inventory",
            items={
                "monitoring_location_id": "USGS-0423205010",
                "parameters": {},
                "discrete_parameters": {"Nutrient": discrete_items},
                "discrete_total_observations": sum(x["count"] for x in discrete_items),
            },
            summary="inventory",
            sample=[],
            provenance={"source_urls": ["https://example.test/summary"]},
            count=n,
        )
        plan = QueryPlan(
            intent="station_inventory",
            inventory_ref=handle.ref,
            filters={},
            semantic={"station": "USGS-0423205010"},
        )
        await handle_inventory(context, process, plan, ctx, request="What data is at USGS-0423205010?")
        all_tokens = [f"CharItem-{i:03d}, water" for i in range(1, n + 1)]
        tail_tokens = [f"CharItem-{i:03d}, water" for i in range(max(1, n - 4), n + 1)]

    elif handler_kind == "inventory_family":
        discrete_items = [
            {
                "characteristic": f"customchem-form-{i:03d}",
                "descriptor": f"customchem-form-{i:03d}, water",
                "count": 50 + i,
                "first_activity": "2015-01-01",
                "most_recent_activity": "2023-06-01",
            }
            for i in range(1, n + 1)
        ]
        handle = ctx.store.put(
            kind="inventory",
            items={
                "monitoring_location_id": "USGS-0423205010",
                "parameters": {},
                "discrete_parameters": {"Custom": discrete_items},
                "discrete_total_observations": sum(x["count"] for x in discrete_items),
            },
            summary="inventory",
            sample=[],
            provenance={"source_urls": ["https://example.test/summary"]},
            count=n,
        )
        plan = QueryPlan(
            intent="station_inventory",
            inventory_ref=handle.ref,
            filters={"parameter_code": "customchem"},
            semantic={"station": "USGS-0423205010", "parameter": "customchem"},
        )
        await handle_inventory(context, process, plan, ctx, request="What customchem data is at USGS-0423205010?")
        all_tokens = [f"customchem-form-{i:03d}, water" for i in range(1, n + 1)]
        tail_tokens = [f"customchem-form-{i:03d}, water" for i in range(max(1, n - 4), n + 1)]

    elif handler_kind == "count":
        pcodes = [f"{70000 + i:05d}" for i in range(1, n + 1)]
        fetcher = AsyncMock()
        fetcher.base_url = "https://api.waterdata.usgs.gov/samples-data"
        fetcher._build_source_url = lambda url, p: f"{url}?mocked"
        fetcher._normalize_params = lambda p: p
        fetcher.get_results_count = AsyncMock(return_value=500)

        plan = QueryPlan(
            intent="station_parameter_count",
            filters={
                "monitoring_location_id": "USGS-0423205010",
                "parameter_code": pcodes,
            },
            semantic={"station": "USGS-0423205010", "parameter": "test_family"},
        )
        await handle_parameter_count(
            context,
            process,
            plan,
            ctx,
            request="How many test_family records at USGS-0423205010?",
            samples_fetcher=fetcher,
        )
        all_tokens = pcodes
        tail_tokens = pcodes[-5:]

    else:
        raise ValueError(f"Unknown handler_kind: {handler_kind}")

    assert context.reply.called
    assert process.create_artifact.called
    reply_text = context.reply.call_args[0][0]
    reply_data = context.reply.call_args.kwargs["data"]
    artifact_desc = process.create_artifact.call_args.kwargs["description"]
    return reply_text, reply_data, artifact_desc, all_tokens, tail_tokens


HANDLER_KINDS = [
    "discrete_observation",
    "daily_bounded",
    "spatial_discrete",
    "spatial_continuous",
    "inventory_general",
    "inventory_family",
    "count",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("handler_kind", HANDLER_KINDS)
@pytest.mark.parametrize("count", [3, 60])
async def test_reply_contract_across_all_data_handlers(handler_kind: str, count: int):
    reply_text, reply_data, artifact_desc, all_tokens, tail_tokens = await _run_case(handler_kind, count)

    # Rule 2: Complete status wording and structured result block
    assert "This is the complete result; nothing was truncated." in reply_text
    assert "result" in reply_data
    assert reply_data["result"]["complete"] is True
    assert reply_data["result"]["artifact_description"] == artifact_desc

    # Rule 3: Reply names the exact artifact description passed to create_artifact
    assert artifact_desc in reply_text

    # Rule 1: Complete or nothing (3 -> all inlined; 60 -> none inlined)
    if count == 3:
        assert reply_data["result"]["items_inlined"] is True
        for token in all_tokens:
            assert token in reply_text, f"{handler_kind} (n=3) missing inlined token {token!r}"
    else:
        assert reply_data["result"]["items_inlined"] is False
        assert "will not return additional" in reply_text
        for token in tail_tokens:
            assert token not in reply_text, f"{handler_kind} (n=60) leaked item token {token!r} in reply"

    # Rule 4: Forbidden partial-list / follow-up / mixed-modality substrings never appear
    for bad in FORBIDDEN_SUBSTRINGS:
        assert bad not in reply_text, f"{handler_kind} (n={count}) contained forbidden substring {bad!r}"

