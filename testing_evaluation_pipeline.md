# 3-Layer Testing & Evaluation Architecture Decision Record (ADR)

> **Document Version**: 2.1  
> **Last Updated**: 2026-10-29  
> **Status**: Approved & Adopted  
> **Repository**: `usgs-water-agent`

---

## 1. Executive Summary & Core Philosophy

This document formalizes every architectural, testing, and evaluation decision made for the USGS Water Data Agent. 

### Core Philosophy: "Test Determinism Deterministically, Judge Generative Quality with LLMs"
1. **Never use an LLM-as-a-judge where deterministic code or schema validation can do the job.** Using an LLM to check whether an enum equals `spatial_discovery` or if a P-code is `00060` is wasteful, slow, and introduces stochastic test flakiness.
2. **Use LLM-as-a-judge (DeepEval) only where language nuance, synthesis faithfulness, or open-ended reasoning occur** (e.g. evaluating whether the "gist" of a clarification prompt is accurate, or whether the final water data synthesis hallucinated numbers).
3. **Isolate evaluation tiers**: Disentangle LLM planning, deterministic routing, upstream government API stability, and final response generation so failures are immediately localized.
4. **Prioritize failure-mode coverage over combinatorial geography**: Testing 50 identical counties is a vanity metric, but geography still matters at the boundaries. A targeted subset of geographic edge cases (ambiguous counties across states, punctuation like *St. Johns*, and administrative units like *La Salle Parish*) is essential alongside core failure modes.
5. **Keep a permanent regression museum**: Every real bug discovered in development must have a permanent regression test.
6. **Guard against API drift with a live smoke suite**: Frozen fixtures protect evaluation tests from government downtime, but they cannot detect upstream USGS schema deprecations or auth changes. A tiny nightly live smoke suite bridges this gap.

---

## 2. Architecture Decision Records (ADRs)

### ADR-01: Disentangling Evaluation into Dedicated Tiers
* **Context**: Monolithic end-to-end runs (`test_end_to_end_query`) make root-cause triage difficult. A test failure could stem from planner intent misclassification, router P-code mapping, USGS API network drops, or LLM hallucination.
* **Decision**: Split the test pipeline into isolated tiers:
  1. `tests/eval/test_planner.py`: Evaluates the LLM's query understanding and intent/entity extraction.
  2. `tests/eval/test_routing.py`: Evaluates parameter resolver, FIPS mapper, and endpoint dispatcher (zero network, instant execution).
  3. `tests/eval/test_response.py`: Evaluates generative synthesis, tables, and scientific disclaimers using DeepEval on frozen fixtures.
  4. `tests/regression/test_known_bugs.py`: Dedicated museum of past verified bugs.
  5. `tests/smoke/test_live_smoke.py`: Nightly live sanity check against upstream USGS APIs.
* **Why**: Provides instantaneous root-cause attribution.

---

### ADR-02: Hybrid Planner Evaluation (Deterministic Schema + LLM-as-Judge for Clarification Text)
* **Context**: The `QueryPlanner` outputs structured enums/entities alongside open-text fields like `clarification_needed`. Asserting `plan.intent == "clarification"` is deterministic, but asserting `plan.clarification_needed == "Please specify a state or county"` is brittle against minor LLM phrasing variations.
* **Decision**: 
  * Strict deterministic assertions (`assert plan.intent == ...`) for structured fields.
  * DeepEval `GEval` metric assessing the **semantic gist** of `clarification_needed` (threshold $\ge 0.7$).
* **Why**: Eliminates false negatives from phrasing variations while keeping 90% of planner assertions instantaneous and zero-cost.

---

### ADR-03: Deterministic Routing Tests with Zero Network Latency
* **Context**: Once planned, routing through FIPS mapping, Unified Analyte Index expansion, and modality dispatching is pure Python logic.
* **Decision**: Execute all routing tests as pure unit tests without contacting external APIs or LLMs (<0.15s runtime).
* **Why**: Guarantees zero network flakiness in CI/CD.

---

### ADR-04: Response Synthesis on Frozen USGS Fixture Payloads
* **Context**: Testing response formatting against live USGS endpoints conflates government API uptime with agent intelligence quality.
* **Decision**: Feed the LLM synthesizer pre-recorded USGS response fixtures paired with benchmark user queries. Grade using DeepEval (`FaithfulnessMetric`, `AnswerRelevancyMetric`, `GEval`).
* **Why**: Isolates LLM comprehension and formatting from upstream network instability.

---

### ADR-05: Prioritize Failure-Mode Coverage Over Combinatorial Geography
* **Context**: Testing dozens of happy-path county permutations adds token cost without finding real bugs. However, ignoring geography entirely creates blind spots for geographic edge cases.
* **Decision**: Target the **9 critical failure modes**, incorporating key geographic boundary cases:

| Failure Mode ID | Name | Scenario | Expected Behavior |
| :--- | :--- | :--- | :--- |
| **FM-01** | `ZERO_DISCOVERY` | Querying an analyte in a county with 0 monitoring stations | Emits valid 0-station discovery plan; does NOT retry, loop, or hallucinate stations. |
| **FM-02** | `MISSING_GEO` | *"Where is streamflow monitored?"* (no state/county) | Does NOT query 1.5M national stations; immediately triggers `clarification`. |
| **FM-03** | `MODALITY_COLLISION` | *"Show me streamflow at station X"* (continuous vs discrete collision) | Routes to OGC continuous daily endpoint, NOT lab samples `/results/narrow`. |
| **FM-04** | `MULTI_PCODE_EXPANSION` | *"How many calcium records at station X?"* | Expands to all 4 constituent calcium codes with primary `00915`; no duplicate counts. |
| **FM-05** | `FRACTION_DISAMBIGUATION`| Specific fraction query (filtered orthophosphate) | Resolves to dissolved codes `00660` / `00671`, excluding unfiltered `00665` / `70507`. |
| **FM-06** | `INVALID_STATION_ID` | *"What is measured at USGS-00000000?"* (404 Not Found) | Fails fast before network or cleanly catches 404; zero unhandled tracebacks. |
| **FM-07** | `MULTIPLE_CANDIDATES` | *"Where is nitrate monitored in Orange County?"* (ambiguous state) | Detects ambiguity (NC vs CA vs FL vs NY); prompts user for state disambiguation. |
| **FM-08** | `NO_PARAMETER` | *"Show me measurements at station X"* (station without parameter) | Routes to `station_inventory` or clarifies parameter; does not query observation with null pcode. |
| **FM-09** | `TOOL_FAILURE` | Upstream USGS returns 429, 500, 503, or connection timeout | Circuit breaker trips; fails fast with friendly outage message; no cascading thread hangs. |

---

### ADR-06: Permanent Bug Museum (`tests/regression/test_known_bugs.py`)
* **Context**: Regressions often occur during refactoring.
* **Decision**: Maintain a dedicated regression suite locking in fixes for real past bugs:
  1. **Bug 1 (Azure Reasoning Token 400)**: `_clean_message_payload()` strips `reasoning_items` for `gpt-5.6-luna`.
  2. **Bug 2 (Substring Matching Bug)**: `"Total nitrogen, ss"` does not falsely match discrete family `"Total Nitrogen"`.
  3. **Bug 3 (USGS Comma-Separated Drop)**: Comma-delimited `usgsPCode` is converted into repeated query parameters.
  4. **Bug 4 (Indefinite Client Hang)**: `AsyncOpenAI` must have an explicit `timeout <= 45.0s`.
  5. **Bug 5 (Unit Test Sleep Stall)**: Circuit breaker tests mock retry backoffs so unit tests run in < 1.5s instead of 29s.

---

### ADR-07: Observability and Regression Hub (LangSmith)
* **Context**: Offline testing does not track live token costs or latency degradation across model updates.
* **Decision**: Instrument planner and execution runs with LangSmith `@traceable` and maintain a synchronized benchmark dataset.

---

### ADR-08: Tiny Live Smoke Suite (`tests/smoke/test_live_smoke.py`)
* **Context**: Relying exclusively on frozen fixtures creates a blind spot: if USGS changes their API URL structure, alters JSON schemas, changes rate limiting headers, or deprecates an endpoint, all fixture-based tests will pass while production silently fails.
* **Decision**: Create a minimal live smoke test suite:
  1. Consists of 3–5 lightweight queries calling real upstream USGS endpoints.
  2. Marked with `@pytest.mark.smoke`.
  3. **Excluded from standard PR and CI runs** (`pytest -m "not smoke"`).
  4. **Executed nightly via cron/scheduler** to provide early warning of upstream government API drift.

---

## 3. Directory Layout

```
tests/
├── unit/                                  # Fast deterministic unit tests (<2s)
│   ├── test_discrete_families.py
│   ├── test_spatial_handler.py
│   └── test_circuit_breaker.py
│
├── eval/                                  # Decoupled Evaluation Harness
│   ├── datasets/
│   │   ├── failure_modes.json             # Benchmark cases FM-01 through FM-09
│   │   └── synthesis_fixtures/            # Frozen USGS API payloads
│   │       ├── count_phosphorus.json
│   │       └── spatial_nitrate_wake.json
│   │
│   ├── test_planner.py                    # Planner LLM (Deterministic + G-Eval Gist)
│   ├── test_routing.py                    # Deterministic routing & FIPS mapping
│   └── test_response.py                   # Response synthesis (DeepEval Faithfulness/Relevancy/G-Eval)
│
├── regression/                            # Permanent Bug Museum
│   └── test_known_bugs.py                 # Bugs 1 through 5
│
└── smoke/                                 # Nightly Live Sanity Suite (3-5 live API tests)
    └── test_live_smoke.py                 # @pytest.mark.smoke (Live OGC, Samples, Endpoints)
```

---

## 4. Execution Schedule

| Suite | Frequency | Command | Latency | Network Needed? |
| :--- | :--- | :--- | :--- | :--- |
| **Unit & Routing** | Every git commit / PR | `pytest tests/eval/test_routing.py tests/regression/` | < 2s | No |
| **Eval Harness** | Pre-merge / Model bump | `pytest tests/eval/test_planner.py tests/eval/test_response.py` | ~40s | Yes (LLM gateway) |
| **Smoke Suite** | Nightly (Scheduled) | `pytest tests/smoke/ -m smoke` | ~15s | Yes (Live USGS + LLM) |
