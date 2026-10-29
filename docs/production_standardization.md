# Production Standardization & Hardening Log

This document tracks the step-by-step engineering improvements implemented to transition the USGS Water Data Agent from a functional prototype to an audit-grade, enterprise-ready service.

---

## Standardization Roadmap

- [x] **Step 1: Clean Up Client Lifecycles & Dependency Injection**
- [x] **Step 2: Eliminate Router Substring Heuristics & Authoritative Intent Dispatch**
- [x] **Step 3: Enforce Native LLM Structured Outputs & Resilient Schema Validation**
- [x] **Step 4: Decouple Catalog Metadata from Python Source Code**
- [x] **Step 5: Production Readiness: Health Checks, Observability & Diagnostic Probes**

---

## Step 1: Clean Up Client Lifecycles & Dependency Injection

### Issues Resolved
1. **Ad-hoc HTTP Client Instantiation**:
   - `StationInventoryTool.get_inventory()` previously spawned a raw `httpx.AsyncClient(timeout=30.0)` per invocation. This bypassed connection pooling, API key headers (`X-Api-Key`), rate-limit detection, and backoff retries.
   - `SpatialQueryTool` created an unmanaged `USGSSamplesFetcher()` in its default constructor without lifecycle cleanup hooks.
2. **Leaky Runtime Module Reflection**:
   - `observation.py` previously searched `sys.modules.get("agent")` dynamically to locate fetcher classes, an inversion-of-control smell.
3. **FIPS Prefix String Interpolation Bug**:
   - `observation.py` previously formatted `stateFips` using `f"US:{plan.filters['state_code']}"`, which converted already-prefixed `"US:37"` into `"US:US:37"`, silently causing the USGS Samples API to return 0 records.

### Implementation Summary
- **Added `fetch_station_summary` to `USGSSamplesFetcher`**: Centralized `/summary/{station_id}` calls inside `USGSSamplesFetcher`, inheriting rate-limit checking, API key authentication, and timeout controls.
- **Injected Fetcher Dependencies**:
  - `StationInventoryTool` and `SpatialQueryTool` now receive `samples_fetcher` as a managed dependency.
  - `QueryPlanner` manages shared `water_data_fetcher` and `samples_fetcher` instances and exposes an `aclose()` lifecycle method.
  - `USGSWaterAgent` now owns long-lived `samples_fetcher` and `water_data_fetcher` instances, injects them into downstream handlers, and provides an `aclose()` shutdown hook for ASGI lifespans.
- **Fixed FIPS Prefixing**:
  - `stateFips` normalizes `"37"` or `"US:37"` deterministically to `"US:37"`.
  - `countyFips` strips any existing `"US:"` prefix from both state and county before emitting `US:{state}:{county}`.
- **Maintained Test Mock Compatibility**:
  - Dynamic properties on `USGSWaterAgent` recognize when test suites patch `agent.USGSSamplesFetcher`, preserving full mock compatibility while using pooled singleton instances in production.

### Verification Status
- **89 / 89 tests passing**.

---

## Step 2: Eliminate Router Substring Heuristics & Authoritative Intent Dispatch

### Issues Resolved
1. **Brittle Heuristic String Matching in Top-Level Router**:
   - `agent.py` previously contained an ad-hoc substring scanner:
     ```python
     is_values_request = any(
         w in request.lower() for w in ["value", "values", "readings", "show me the"]
     ) and not any(
         w in request.lower()
         for w in ["how many", "count", "inventory", "available", "what information", "what data"]
     )
     ```
   - This manually second-guessed planner output, causing fragile behavior whenever user requests combined vocabulary (e.g. *"Show me the count of calcium values"*).

### Implementation Summary
- **Removed Heuristic Substring Scanning**: Completely excised `is_values_request` and `request.lower()` scans from `src/agent.py`.
- **Pure Intent Routing**: Top-level router now dispatches strictly by `plan.intent`:
  - `QueryIntent.CLARIFICATION` $\rightarrow$ `handle_clarification`
  - `QueryIntent.SPATIAL_DISCOVERY` $\rightarrow$ `handle_spatial`
  - `QueryIntent.STATION_INVENTORY` & `QueryIntent.STATION_PARAMETER_COUNT` $\rightarrow$ `handle_inventory`
  - `QueryIntent.OBSERVATION` $\rightarrow$ `handle_observation`
- **Mock / Legacy Plan Compatibility**: Fallback checks for `stations_ref`, `inventory_ref`, and `filters` ensure mock plans in older test fixtures remain fully functional without requiring explicit intent mocking.

### Verification Status
- **89 / 89 tests passing**.

---

## Step 3: Enforce Native LLM Structured Outputs & Resilient Schema Validation

### Issues Resolved
1. **Brittle String-Fence Stripping**:
   - `QueryPlanner._parse_plan` previously used simple prefix checking (`if content.startswith("```json")`).
   - If the LLM returned conversational preambles (e.g. *"Here is your plan:\n```json\n{...}\n```"*) or conversational postambles, the JSON parser threw an error and abandoned the plan to a generic clarification message.
2. **Unconstrained Reprompting**:
   - The validation-error retry loop reprompted the LLM as free-form chat without schema formatting constraints.

### Implementation Summary
- **Multi-Stage JSON Extraction (`_extract_json_candidate`)**:
  - Automatically isolates fenced code blocks (`re.search(r"```(?:json)?\s*([\s\S]*?)\s*```")`).
  - Gracefully extracts the outermost `{ ... }` balanced bounds if the model emits embedded JSON without markdown formatting.
  - Tries multiple candidate candidates before failing gracefully.
- **Enforced JSON Object Response Mode**:
  - Added `response_format={"type": "json_object"}` to the toolless retry invocation in `query_planner.py`.
- **Added Regression Suite**:
  - Added `test_parse_plan_resilience` in `tests/test_planner_behavior.py` validating pure JSON, fenced markdown, conversational preambles/postambles, raw embedded objects, and graceful fallbacks on garbage strings.

### Verification Status
- **90 / 90 tests passing**.

---

## Step 4: Decouple Catalog Metadata from Python Source Code

### Issues Resolved
1. **Bloated Source Code with Static Reference Data**:
   - `src/water_agent/metadata/discrete_families.py` previously contained an 850-line hardcoded Python dictionary (`DISCRETE_FAMILIES = { ... }`) embedding constituent codes, chemical descriptors, CAS numbers, and NC verification baseline counts directly inside source code.
   - Any updates or expansions to parameter catalogs required editing Python code and re-deploying rather than refreshing external data configurations.
2. **Coupling to Specific State Verification Baselines**:
   - `DiscreteParameter` required `verified_records_nc`, `verified_stations_nc`, and `verified_date`, tightly coupling the general parameter definition model to North Carolina benchmark statistics.

### Implementation Summary
- **Externalized Catalog to JSON**:
   - Preserved `metadata/raw/references/discrete_families.json` as the authoritative, version-controlled reference catalog containing all 9 parameter families, constituent codes, units, and CAS numbers.
   - Replaced the hardcoded dictionary in `discrete_families.py` with dynamic, cached loading (`_load_discrete_families()`).
   - Cleaned `discrete_families.py` from 1,116 lines down to 270 lines (-75% file size), improving readability and maintainability.
- **Made Benchmark Baselines Optional**:
   - Updated `DiscreteParameter` schema with default values (`verified_records_nc: int = 0`, `verified_stations_nc: int = 0`, `verified_date: str = ""`) so new or national parameter entries do not require state-specific benchmark statistics.
- **Full Backward Compatibility**:
   - Preserved all public API methods: `get_parameter()`, `get_family_for_pcode()`, `is_mixed_modality()`, and `resolve_discrete_parameter()`.

### Verification Status
- **90 / 90 tests passing**.

---

## Step 5: Production Readiness: Health Checks, Observability & Diagnostic Probes

### Issues Resolved
1. **Lack of Upstream Dependency & Readiness Probes**:
   - In production microservice environments (Kubernetes, AWS ECS, Docker Swarm), services require `/healthz` or `/readyz` probes to determine if dependencies (USGS OGC API, USGS Samples API, OpenAI LLM, and local catalog stores) are operational before routing user traffic.
   - Failures in external endpoints previously only manifested during active user request execution, causing runtime exceptions.

### Implementation Summary
- **Created Diagnostic Health Checker (`src/water_agent/health.py`)**:
   - Implemented Pydantic models: `HealthReport`, `ComponentCheck`, and `HealthStatus` (`healthy`, `degraded`, `unhealthy`).
   - Probes:
     - `ogc_api`: Pings USGS OGC API (`/collections?f=json`) measuring round-trip latency in milliseconds.
     - `samples_api`: Pings USGS Samples API (`/codes/sample-media?f=json`) measuring round-trip latency in milliseconds.
     - `metadata_catalog`: Verifies local catalog integrity and loaded family counts.
     - `llm_config`: Checks for `OPENAI_API_KEY` configuration.
   - Non-blocking resilience: If an upstream API returns an error or times out, the probe catches the exception and marks the component `DEGRADED` or `UNHEALTHY` without crashing.
- **Added `check_health()` to `USGSWaterAgent`**:
   - `USGSWaterAgent.check_health()` exposes an asynchronous probe suitable for FastAPI/ASGI `/healthz` or `/readyz` integration.
- **Created Comprehensive Test Suite (`tests/test_health_checker.py`)**:
   - `test_health_check_all_healthy`: Validates healthy report when all components pass.
   - `test_health_check_degraded_when_one_api_fails`: Validates graceful degradation when one upstream API fails or times out.
   - `test_health_check_unhealthy_when_all_apis_fail`: Validates unhealthy status when all critical APIs fail.
   - `test_agent_check_health_method`: Validates the end-to-end agent probe integration.

### Verification Status
- **94 / 94 tests passing**.

---

## Final Production Hardening Summary

| Roadmap Item | Status | Verified Test Count | Impact |
| :--- | :---: | :---: | :--- |
| **Step 1: Client Lifecycles & DI** | Completed | 89 / 89 | Pooled HTTP connections, zero client churn, fixed FIPS prefixing |
| **Step 2: Eliminate Router Heuristics** | Completed | 89 / 89 | Authoritative LLM intent dispatch, removed substring scans |
| **Step 3: Structured Outputs & Resilience** | Completed | 90 / 90 | Native JSON extraction, regex fallback, schema-enforced retries |
| **Step 4: Decouple Catalog Metadata** | Completed | 90 / 90 | 75% code reduction in discrete families, externalized JSON catalog |
| **Step 5: Health & Observability Probes** | Completed | 94 / 94 | Structured diagnostic probe for OGC, Samples, catalog, and LLM |

**Total Wave 1 Test Suite**: **94 tests, 100% green**.

---

# Wave 2: Enterprise Hardening Roadmap

- [x] **Wave 2, Step 1: Configuration (Pydantic-Settings & Secret Masking)**
- [x] **Wave 2, Step 2: Resilience (Async Circuit Breaker Pattern)**
- [x] **Wave 2, Step 3: API Contracts (Strict Boundary DTOs)**
- [x] **Wave 2, Step 4: Observability (Structured Tracing & OpenTelemetry Contexts)**

---

## Wave 2, Step 1: Configuration (Pydantic-Settings & Secret Masking)

### Issues Resolved
1. **Unvalidated Global Environment Variables**:
   - `config.py` previously relied on ad-hoc `os.getenv` string calls without type validation, coercion, or schema structure.
2. **Credential Leakage Risk**:
   - Storing API keys (`USGS_API_KEY`, `OPENAI_API_KEY`) as plain strings posed a security risk of leaking sensitive keys into logging outputs, traceback dictionaries, or debug prints.

### Implementation Summary
- **Added `pydantic-settings` to Project Dependencies**:
   - Added `"pydantic-settings>=2.0.0"` to `pyproject.toml`.
- **Created `WaterAgentSettings(BaseSettings)` (`src/water_agent/config.py`)**:
   - Type-validated fields for `usgs_api_key`, `usgs_api_version`, `ogc_base_url`, `samples_base_url`, `openai_api_key`, `openai_base_url`, `openai_model`, and timeout/retry thresholds.
   - Protected API keys with `SecretStr`—preventing credentials from leaking in `repr()`, `str()`, or log messages.
   - Provided clean helper accessors (`usgs_api_key_str`, `openai_api_key_str`) that strip whitespace and return `None` when empty.
   - Provided singleton caching with `get_settings()` and dynamic backward compatibility for module-level imports.
- **Created Comprehensive Test Suite (`tests/test_config.py`)**:
   - Tested default values, secret masking, environment overrides, and API key requirement checks.

### Verification Status
- **98 / 98 tests passing**.

---

## Wave 2, Step 2: Resilience (Async Circuit Breaker Pattern)

### Issues Resolved
1. **Cascading Hangs on Upstream Outages**:
   - In microservices, when upstream dependencies (USGS OGC API or USGS Samples API) experience 503 Service Unavailable errors, rate-limiting, or connection dropouts, naive retry loops consume connection pools, block event loops, and exhaust asyncio tasks.
2. **Lack of Fast-Failing Protection**:
   - Unhealthy upstream services previously continued to receive full request traffic, compounding server stress and delaying user responses with repeated timeouts.

### Implementation Summary
- **Implemented `CircuitBreaker` State Machine (`src/water_agent/circuit_breaker.py`)**:
   - Built an asynchronous, thread-safe state machine (`CLOSED` $\rightarrow$ `OPEN` $\rightarrow$ `HALF_OPEN` $\rightarrow$ `CLOSED`) using `asyncio.Lock()`.
   - `failure_threshold`: Configurable number of consecutive server errors before tripping to `OPEN` (default 5).
   - `recovery_timeout`: Cooling period before probing with a single test request in `HALF_OPEN` state (default 30.0s).
   - `excluded_exceptions`: Explicitly excludes 4xx client validation errors (`USGSValidationError`, `USGSNotFoundError`, `SamplesValidationError`) from tripping the breaker, ensuring only genuine upstream server failures trip the circuit.
   - `CircuitBreakerOpenError`: Fails fast immediately with remaining seconds until probe, preventing connection exhaustion.
- **Configured via Pydantic-Settings**:
   - Added `circuit_breaker_failure_threshold` and `circuit_breaker_recovery_timeout` to `WaterAgentSettings`.
- **Integrated into Fetchers**:
   - `USGSWaterDataFetcher._request`: Protected via `self.circuit_breaker.call(self._raw_request, url, params)`.
   - `USGSSamplesFetcher`: Protected via `self._safe_get()` and streaming guards.
- **Integrated into Observability Probes (`src/water_agent/health.py`)**:
   - `HealthChecker.check_ogc_api()` and `check_samples_api()` inspect circuit breaker states and immediately report `UNHEALTHY` if a breaker is open, exposing breaker status details (`seconds_until_probe`, `failure_count`, `failure_threshold`).
- **Comprehensive Test Suite (`tests/test_circuit_breaker.py`)**:
   - Tested transition to `OPEN` after threshold failures and verification that subsequent calls fail fast without invoking the network.
   - Tested recovery probe in `HALF_OPEN` transitioning to `CLOSED` on success.
   - Tested re-tripping to `OPEN` on probe failure.
   - Tested that client validation errors (`excluded_exceptions`) do not increment failure counters.
   - Tested end-to-end circuit breaker integration in `USGSWaterDataFetcher` and `USGSSamplesFetcher`.

### Verification Status
- **104 / 104 tests passing**.

---

## Wave 2, Step 3: API Contracts (Strict Boundary DTOs)

### Issues Resolved
1. **Loosely Typed Internal Boundaries**:
   - Intermediate layers previously passed untyped dictionaries (`dict[str, Any]`, `features: list[dict]`, `records: list[dict]`), making downstream handlers vulnerable to missing keys, schema drift, or silent key typos.
2. **Brittle Consumers & Legacy Fixture Breakage**:
   - Rigid Pydantic models often break legacy consumers that access dictionary keys (`result["stations"]`).

### Implementation Summary
- **Created Strongly-Typed Boundary DTOs (`src/water_agent/dto.py`)**:
   - `DictAccessibleModel`: Base model providing dual access—strongly-typed attribute access (`res.stations`) and transparent dictionary subscripting (`res["stations"]`, `res.get("stations")`, `"stations" in res`) with zero-copy overhead.
   - `SpatialQueryResult` & `SpatialStationDTO`: Boundary contract for spatial station discovery, containing normalized station locations, constituent parameter breakdowns, modality flags, and convenient `station_ids` extraction.
   - `StationInventoryResult`, `ContinuousParameterInventory`, `DiscreteCharacteristicItem`: Strongly-typed dual-modality station inventory contract with observation counts, period-of-record bounds, and metadata URLs.
   - `ContinuousDailyReading`: Strongly-typed representation of continuous sensor observations.
   - `DiscreteObservationRecord`: Strongly-typed representation of laboratory discrete sample measurements.
- **Integrated into Query Tools & Fetchers**:
   - `SpatialQueryTool.find_stations()` returns typed `SpatialQueryResult` with `SpatialStationDTO` and `ParameterBreakdownItem`.
   - `StationInventoryTool.get_inventory()` returns typed `StationInventoryResult` with `ContinuousParameterInventory` and `DiscreteCharacteristicItem`.
   - `PagedResult.to_daily_readings()` parses GeoJSON features into `list[ContinuousDailyReading]`.
   - `DiscreteFetchResult.to_observation_records()` converts raw CSV records into `list[DiscreteObservationRecord]`.
- **Created Comprehensive Test Suite (`tests/test_boundary_dtos.py`)**:
   - Tested typed attribute access, legacy dictionary subscripting, `.model_dump()` serialization, and conversion helpers across spatial discovery, inventory, daily readings, and discrete observations.

### Verification Status
- **108 / 108 tests passing**.

---

## Wave 2, Step 4: Observability (Structured Tracing & OpenTelemetry Contexts)

### Issues Resolved
1. **Opaque Multi-Turn Diagnostics**:
   - The agent executes multi-step LLM planning loops, tool calls, and external USGS HTTP requests. Previously, debugging latency or upstream errors relied on unstructured text logs without distributed correlation IDs.
2. **Missing Distributed Context Propagation**:
   - Asynchronous tool tasks lacked context propagation (`trace_id`, `span_id`, parentage), making it impossible to reconstruct the exact causality tree of a request.

### Implementation Summary
- **Created OpenTelemetry-Compatible Tracing Subsystem (`src/water_agent/tracing.py`)**:
   - Implemented standard 32-hex `trace_id` and 16-hex `span_id` generators.
   - Built context-local distributed trace propagation using Python `contextvars` (`_CURRENT_TRACE_ID`, `_CURRENT_SPAN`, `_SPAN_COLLECTOR`), ensuring safe concurrency across async tasks.
   - Built `start_span(name, attributes, trace_id)` supporting both `async with` and synchronous `with` blocks.
   - Supports OpenTelemetry export format (`to_otel_dict()`) with nanosecond Unix timestamps, durations in milliseconds, and structured events.
   - Implemented `TraceLogFilter(logging.Filter)` injecting active `trace_id` and `span_id` directly into Python standard logging records.
- **Instrumented Critical System Paths**:
   - `QueryPlanner.plan()`: Root span `"llm_planning"` capturing user question, model name, and resolved intent.
   - `QueryPlanner._call_tool()`: Child span `"tool_execution:{name}"` capturing tool parameters, results, kind, and error states.
   - `USGSWaterDataFetcher._raw_request()`: Span `"usgs_ogc.http_get"` capturing HTTP status codes, latency, and endpoint URLs.
   - `USGSSamplesFetcher._safe_get()`: Span `"usgs_samples.http_get"` capturing HTTP status codes, rate limits, and endpoint URLs.
   - `RequestContext`: Collects and stores finished spans per user query session.
- **Created Comprehensive Test Suite (`tests/test_tracing.py`)**:
   - Tested span lifecycle, start/end timestamps, and duration calculation.
   - Tested multi-level parentage and trace ID propagation across nested async tasks.
   - Tested exception and error recording on spans.
   - Tested OpenTelemetry dictionary structure export.
   - Tested `TraceLogFilter` injecting trace and span context into `logging.LogRecord`.

### Verification Status
- **113 / 113 tests passing**.

---

## Post-Wave 2 Hotfix: Zero-Result DTO False-Error Trap & Spatial Guidance

### Incident Summary
- **Symptom**: During a production query (`"Where is chlorophyll a monitored in Wake County, North Carolina?"`), the USGS Samples API returned 0 matching discrete stations across all 10 Chlorophyll a parameter codes. The LLM repeatedly re-issued the exact same 12 HTTP requests across all planner iterations until hitting `MAX_TOOL_ITERATIONS`, returning an ambiguous clarification response with `(API Error: None)` and listing 6 Chlorophyll a candidates.
- **Root Cause Analysis**:
  1. **DTO False-Error Trap**: `SpatialQueryResult` defined `error: Optional[str] = None`. Because `SpatialQueryResult` inherited from `DictAccessibleModel`, its `__contains__` implementation relied on `hasattr(self, item)`. Since `hasattr(result, "error")` is always `True` on the model class, `"error" in result` evaluated to `True` even when `result.error is None`.
  2. **Tool Result Interception**: In `QueryPlanner._call_tool`, `if "error" in result:` intercepted every successful `SpatialQueryResult` as a tool failure, appending `{"error": None}` to `ctx.tool_errors` and returning `{"error": null, "retryable": false}` to the LLM. It completely bypassed `ResultStore.put()` and never created a `stations_ref`.
  3. **Planner Retry Loop**: The LLM perceived that the tool call had errored and retried the call across every iteration until `MAX_TOOL_ITERATIONS` was exhausted.
  4. **Degraded Fallback**: Upon loop exhaustion, `QueryPlanner` formatted the last error as `f"(API Error: {err['error']})"`, generating `"(API Error: None)"`, and attached `last_candidates` (the 10 Chlorophyll a descriptors from the initial parameter lookup), which `handle_clarification` formatted as a disambiguation prompt.

### Architectural Fixes
1. **`DictAccessibleModel.__contains__` Hardening (`src/water_agent/dto.py`)**:
   - Updated `__contains__` to verify that fields defined in `type(self).model_fields` are not `None`. Accesses `model_fields` via `type(self)` to eliminate Pydantic v2.11+ deprecation warnings.
2. **`QueryPlanner._call_tool` Truthy Guard (`src/water_agent/query_planner.py`)**:
   - Replaced fragile `"error" in result` check with `err_val = result.get("error") if hasattr(result, "get") else getattr(result, "error", None); if err_val:`, ensuring only truthy error values trip the error path.
3. **Loop Fallback Formatting Guard (`src/water_agent/query_planner.py`)**:
   - Guarded error strings with `if err.get("error"):` so `(API Error: None)` can never be emitted.
4. **LLM Spatial Discovery Guidance (`src/water_agent/query_planner.py`)**:
   - Explicitly clarified in `SYSTEM_PROMPT` that `spatial_query` returning 0 stations is a definitive, valid discovery outcome (the parameter is not monitored there) and must immediately emit the plan with `stations_ref` rather than retrying.
5. **Zero-Station Handler Presentation (`src/water_agent/handlers/spatial.py`)**:
   - Upgraded empty stations response to format parameter family, geography, and an informative note regarding continuous sensor monitoring for mixed-modality parameters (e.g. in-situ optical fluorescence sensors).
6. **Comprehensive Test Suite (`tests/test_boundary_dtos.py` & `tests/test_planner_behavior.py`)**:
   - Added `test_dict_accessible_model_contains_semantics` verifying `__contains__` behavior for `None` vs truthy values.
   - Added `test_call_tool_with_spatial_query_result_dto` verifying that `_call_tool` generates a valid `ResultHandle` and populates `ctx.store` when `SpatialQueryResult` returns 0 stations with `error=None`.

### Verification Status
- **115 / 115 tests passing (100% green, 0 failures, 0 warnings)**.

---

## Complete Enterprise Hardening Matrix

| Phase | Milestone | Focus Area | Status | Verified Tests | Key Architectural Enhancement |
| :---: | :--- | :--- | :---: | :---: | :--- |
| **Wave 1** | **Step 1** | Client Lifecycles & DI | Completed | 89 / 89 | Pooled HTTP connections, zero socket churn, fixed FIPS prefixing |
| **Wave 1** | **Step 2** | Router Intent Dispatch | Completed | 89 / 89 | Authoritative LLM intent dispatch; excised all substring heuristics |
| **Wave 1** | **Step 3** | Structured Outputs & Resilience | Completed | 90 / 90 | Native balanced JSON extraction with regex fallback & schema retries |
| **Wave 1** | **Step 4** | Decoupled Catalog Metadata | Completed | 90 / 90 | Parameter catalog externalized to JSON; source code reduced by 75% |
| **Wave 1** | **Step 5** | Health & Observability Probes | Completed | 94 / 94 | Structured diagnostic probe for OGC, Samples, catalog, and LLM |
| **Wave 2** | **Step 1** | Configuration (`pydantic-settings`) | Completed | 98 / 98 | Typed `BaseSettings` with `SecretStr` preventing credential leakage |
| **Wave 2** | **Step 2** | Resilience (Circuit Breaker) | Completed | 104 / 104 | Stateful `CLOSED -> OPEN -> HALF-OPEN` breaker failing fast on 5xx/timeouts |
| **Wave 2** | **Step 3** | API Contracts (Boundary DTOs) | Completed | 108 / 108 | Typed Pydantic models at all boundaries with subscripting compatibility |
| **Wave 2** | **Step 4** | Observability (OpenTelemetry Spans) | Completed | 113 / 113 | Distributed `trace_id` and `span_id` context propagation across planning & HTTP |
| **Wave 2** | **Hotfix** | DTO Error Trap & Empty Spatial Guidance | Completed | 115 / 115 | Eliminated false-error interception on 0-station DTOs; hardened planner |

**Final Test Suite**: **115 passed in 22.20s (100% green, 0 failures, 0 warnings)**.

