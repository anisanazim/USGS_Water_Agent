from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from dotenv import load_dotenv

load_dotenv()

from usgs_water_data import USGSValidationError, USGSWaterDataFetcher
from water_agent.circuit_breaker import CircuitBreakerOpenError, CircuitState
from water_agent.fetchers.samples_fetcher import SamplesError, USGSSamplesFetcher
from water_agent.metadata.discrete_families import resolve_discrete_parameter
from water_agent.metadata.models import ParameterModality, QueryIntent
from water_agent.metadata.references.store import ReferenceStore
from water_agent.metadata.registry import daily_registry
from water_agent.metadata.resolver import ReferenceResolver
from water_agent.query_planner import QueryPlanner
from water_agent.results import RequestContext

DATASET_FILE = Path(__file__).parent / "datasets" / "failure_modes.json"
REFERENCES_DIR = Path(__file__).parent.parent.parent / "metadata" / "raw" / "references"


@dataclass
class CaseResult:
    case_id: str
    name: str
    layer: str
    passed: bool
    duration_ms: float
    summary: str
    error: str | None = None


class FailureModeBenchmarkRunner:
    """Automated benchmark runner executing all 9 failure-mode cases (FM-01 through FM-09)
    across Planner, Routing, and Fault Tolerance layers, generating a consolidated scorecard.
    """

    def __init__(self) -> None:
        with open(DATASET_FILE, "r", encoding="utf-8") as f:
            self.dataset = {case["id"]: case for case in json.load(f)}

        store = ReferenceStore(REFERENCES_DIR)
        self.resolver = ReferenceResolver(daily_registry, store)
        self.planner = QueryPlanner()
        self.results: list[CaseResult] = []

    async def aclose(self) -> None:
        await self.planner.aclose()

    async def run_all(self) -> list[CaseResult]:
        self.results.clear()
        cases = [
            self.eval_fm01_zero_discovery,
            self.eval_fm02_missing_geo,
            self.eval_fm03_modality_collision,
            self.eval_fm04_multi_pcode_expansion,
            self.eval_fm05_fraction_disambiguation,
            self.eval_fm06_invalid_station_id,
            self.eval_fm07_multiple_candidates,
            self.eval_fm08_no_parameter,
            self.eval_fm09_tool_failure,
        ]
        for case_fn in cases:
            res = await case_fn()
            self.results.append(res)
        return self.results

    async def eval_fm01_zero_discovery(self) -> CaseResult:
        case = self.dataset["FM-01"]
        t0 = time.perf_counter()
        try:
            ctx = RequestContext(request_id="bench_fm01")
            plan = await self.planner.plan(case["query"], ctx)
            assert plan.intent == QueryIntent.SPATIAL_DISCOVERY
            assert plan.modality == ParameterModality.DISCRETE

            s_res = self.resolver.resolve("state_code", "North Carolina")
            c_res = self.resolver.resolve("county_code", "Dare")
            full_fips = f"{s_res.resolved_value}{c_res.resolved_value}"
            assert full_fips == case["routing_verification"]["expected_fips"]

            p_res = resolve_discrete_parameter("nitrate")
            assert any(code in p_res.pcodes for code in ["00618", "00620", "00631"])

            dur = (time.perf_counter() - t0) * 1000
            return CaseResult("FM-01", case["name"], "Planner + Router", True, dur, f"Intent=spatial_discovery, FIPS={full_fips}")
        except Exception as exc:
            dur = (time.perf_counter() - t0) * 1000
            return CaseResult("FM-01", case["name"], "Planner + Router", False, dur, "Validation failed", str(exc))

    async def eval_fm02_missing_geo(self) -> CaseResult:
        case = self.dataset["FM-02"]
        t0 = time.perf_counter()
        try:
            ctx = RequestContext(request_id="bench_fm02")
            plan = await self.planner.plan(case["query"], ctx)
            assert plan.intent == QueryIntent.CLARIFICATION
            assert plan.clarification_needed is not None
            text = plan.clarification_needed.lower()
            keywords = case["expected_planner"]["clarification_gist_keywords"]
            assert any(kw in text for kw in keywords), f"None of {keywords} found in: {plan.clarification_needed}"

            dur = (time.perf_counter() - t0) * 1000
            return CaseResult("FM-02", case["name"], "Planner + Gist", True, dur, "Clarification triggered for state/county")
        except Exception as exc:
            dur = (time.perf_counter() - t0) * 1000
            return CaseResult("FM-02", case["name"], "Planner + Gist", False, dur, "Clarification check failed", str(exc))

    async def eval_fm03_modality_collision(self) -> CaseResult:
        case = self.dataset["FM-03"]
        t0 = time.perf_counter()
        try:
            ctx = RequestContext(request_id="bench_fm03")
            plan = await self.planner.plan(case["query"], ctx)
            assert plan.intent == QueryIntent.OBSERVATION
            assert plan.modality == ParameterModality.CONTINUOUS
            assert plan.filters.get("monitoring_location_id") == "USGS-02087500"
            assert plan.filters.get("parameter_code") == "00060"

            dur = (time.perf_counter() - t0) * 1000
            return CaseResult("FM-03", case["name"], "Planner + Router", True, dur, "Continuous daily discharge routed (00060)")
        except Exception as exc:
            dur = (time.perf_counter() - t0) * 1000
            return CaseResult("FM-03", case["name"], "Planner + Router", False, dur, "Routing collision check failed", str(exc))

    async def eval_fm04_multi_pcode_expansion(self) -> CaseResult:
        case = self.dataset["FM-04"]
        t0 = time.perf_counter()
        try:
            ctx = RequestContext(request_id="bench_fm04")
            plan = await self.planner.plan(case["query"], ctx)
            assert plan.intent == QueryIntent.STATION_PARAMETER_COUNT
            assert plan.filters.get("monitoring_location_id") == "USGS-0423205010"

            res = resolve_discrete_parameter("calcium")
            assert res.status == "resolved"
            assert res.pcodes[0] == case["routing_verification"]["primary_pcode"]
            assert res.pcodes == case["routing_verification"]["constituent_codes"]

            dur = (time.perf_counter() - t0) * 1000
            return CaseResult("FM-04", case["name"], "Planner + Router", True, dur, f"Calcium expanded to 4 pcodes ({res.pcodes[0]} primary)")
        except Exception as exc:
            dur = (time.perf_counter() - t0) * 1000
            return CaseResult("FM-04", case["name"], "Planner + Router", False, dur, "Multi-pcode expansion failed", str(exc))

    async def eval_fm05_fraction_disambiguation(self) -> CaseResult:
        case = self.dataset["FM-05"]
        t0 = time.perf_counter()
        try:
            res = resolve_discrete_parameter("00660")
            assert res.status == "resolved"
            assert res.pcodes == ["00660"]
            assert "00665" not in res.pcodes
            assert "70507" not in res.pcodes

            dur = (time.perf_counter() - t0) * 1000
            return CaseResult("FM-05", case["name"], "Router", True, dur, "Filtered orthophosphate isolated (00660, no 00665)")
        except Exception as exc:
            dur = (time.perf_counter() - t0) * 1000
            return CaseResult("FM-05", case["name"], "Router", False, dur, "Fraction disambiguation failed", str(exc))

    async def eval_fm06_invalid_station_id(self) -> CaseResult:
        case = self.dataset["FM-06"]
        t0 = time.perf_counter()
        try:
            with pytest.raises(USGSValidationError):
                USGSWaterDataFetcher.normalize_site_id("invalid-id-abc")

            dur = (time.perf_counter() - t0) * 1000
            return CaseResult("FM-06", case["name"], "Router / Validation", True, dur, "Pre-network site ID validation caught invalid ID")
        except Exception as exc:
            dur = (time.perf_counter() - t0) * 1000
            return CaseResult("FM-06", case["name"], "Router / Validation", False, dur, "Invalid station validation failed", str(exc))

    async def eval_fm07_multiple_candidates(self) -> CaseResult:
        case = self.dataset["FM-07"]
        t0 = time.perf_counter()
        try:
            ctx = RequestContext(request_id="bench_fm07")
            plan = await self.planner.plan(case["query"], ctx)
            assert plan.intent == QueryIntent.CLARIFICATION
            assert plan.clarification_needed is not None
            text = plan.clarification_needed.lower()
            keywords = case["expected_planner"]["clarification_gist_keywords"]
            assert any(kw in text for kw in keywords)

            dur = (time.perf_counter() - t0) * 1000
            return CaseResult("FM-07", case["name"], "Planner + Gist", True, dur, "Clarification triggered for ambiguous county (Orange)")
        except Exception as exc:
            dur = (time.perf_counter() - t0) * 1000
            return CaseResult("FM-07", case["name"], "Planner + Gist", False, dur, "Multi-candidate state check failed", str(exc))

    async def eval_fm08_no_parameter(self) -> CaseResult:
        case = self.dataset["FM-08"]
        t0 = time.perf_counter()
        try:
            ctx = RequestContext(request_id="bench_fm08")
            plan = await self.planner.plan(case["query"], ctx)
            assert plan.intent in [QueryIntent.STATION_INVENTORY, QueryIntent.CLARIFICATION]
            if plan.intent == QueryIntent.OBSERVATION:
                assert plan.filters.get("parameter_code") is not None

            dur = (time.perf_counter() - t0) * 1000
            return CaseResult("FM-08", case["name"], "Planner", True, dur, "Station query without parameter routed safely")
        except Exception as exc:
            dur = (time.perf_counter() - t0) * 1000
            return CaseResult("FM-08", case["name"], "Planner", False, dur, "No parameter routing check failed", str(exc))

    async def eval_fm09_tool_failure(self) -> CaseResult:
        case = self.dataset["FM-09"]
        t0 = time.perf_counter()
        try:
            fetcher = USGSSamplesFetcher()
            fetcher.circuit_breaker.failure_threshold = 2
            fetcher.circuit_breaker.recovery_timeout = 0.05
            mock_503 = httpx.Response(
                status_code=503,
                text="Service Unavailable",
                request=httpx.Request("GET", "https://api.waterdata.usgs.gov"),
            )
            fetcher._client.get = AsyncMock(return_value=mock_503)

            with patch("water_agent.fetchers.samples_fetcher.asyncio.sleep", new_callable=AsyncMock):
                with pytest.raises(SamplesError):
                    await fetcher.get_results_count({"stateFips": "US:37"})
                with pytest.raises(SamplesError):
                    await fetcher.get_results_count({"stateFips": "US:37"})

            assert fetcher.circuit_breaker.state == CircuitState.OPEN
            with pytest.raises(CircuitBreakerOpenError) as exc_info:
                await fetcher.get_results_count({"stateFips": "US:37"})
            assert "is OPEN" in str(exc_info.value)

            dur = (time.perf_counter() - t0) * 1000
            return CaseResult("FM-09", case["name"], "Fault Tolerance", True, dur, "Circuit breaker tripped OPEN; fail-fast OK")
        except Exception as exc:
            dur = (time.perf_counter() - t0) * 1000
            return CaseResult("FM-09", case["name"], "Fault Tolerance", False, dur, "Circuit breaker check failed", str(exc))

    def generate_scorecard_markdown(self) -> str:
        passed_count = sum(1 for r in self.results if r.passed)
        total_count = len(self.results)
        pct = (passed_count / total_count * 100) if total_count else 0.0

        lines = [
            "# USGS Water Data Agent - Failure-Mode Benchmark Scorecard",
            "",
            f"> **Benchmark Date**: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}  ",
            f"> **Overall Score**: **{passed_count}/{total_count} ({pct:.1f}%)** {'✅ PASS' if passed_count == total_count else '❌ FAIL'}",
            "",
            "| ID | Case Name | Test Layer | Status | Duration | Verification Summary |",
            "| :--- | :--- | :--- | :---: | :---: | :--- |",
        ]
        for r in self.results:
            status_icon = "✅ PASS" if r.passed else "❌ FAIL"
            lines.append(
                f"| **{r.case_id}** | `{r.name}` | {r.layer} | {status_icon} | {r.duration_ms:.0f}ms | {r.summary} |"
            )

        lines.extend([
            "",
            "## Benchmark Assessment",
            f"- **All 9 Failure Modes Evaluated**: {'All passed without regression.' if passed_count == total_count else 'Regressions detected.'}",
            "- **Planner Layer**: Verified semantic intent classification and ambiguity clarification.",
            "- **Routing Layer**: Verified zero-network FIPS mapping, P-code family expansion, and fraction isolation.",
            "- **Fault Tolerance Layer**: Verified circuit breaker tripping and fast-fail abort under upstream 503 outages.",
        ])
        return "\n".join(lines)


@pytest.mark.asyncio
async def test_failure_mode_benchmark_consolidated_scorecard():
    """Automated benchmark test running all 9 failure modes (FM-01 to FM-09) and verifying
    100% pass rate with consolidated scorecard generation.
    """
    runner = FailureModeBenchmarkRunner()
    try:
        results = await runner.run_all()
        scorecard = runner.generate_scorecard_markdown()

        # Print the scorecard table to test output
        print("\n" + "=" * 90)
        print(scorecard)
        print("=" * 90 + "\n")

        # Save scorecard artifact to metadata directory
        scorecard_path = Path(__file__).parent.parent.parent / "metadata" / "benchmark_scorecard.md"
        scorecard_path.parent.mkdir(parents=True, exist_ok=True)
        with open(scorecard_path, "w", encoding="utf-8") as f:
            f.write(scorecard)

        # Assert 100% pass rate
        failures = [r for r in results if not r.passed]
        assert not failures, f"Benchmark failed on {len(failures)} cases: {[f.case_id + ': ' + (f.error or '') for f in failures]}"
    finally:
        await runner.aclose()


if __name__ == "__main__":
    async def main():
        runner = FailureModeBenchmarkRunner()
        try:
            print("Running USGS Water Agent Failure Mode Benchmark across FM-01 -> FM-09...")
            await runner.run_all()
            scorecard = runner.generate_scorecard_markdown()
            print("\n" + scorecard + "\n")
        finally:
            await runner.aclose()

    asyncio.run(main())
