# Result Handle Refactor — Decision Log

## Table of Contents
- [2026-09-17 14:28] Report-back: Code audit findings
---

## [2026-09-17 14:28] Report-back: Code audit findings
**Step:** Pre-implementation audit (PROCESS step)
**Issue:** Three questions to answer before any code changes: (a) is QueryPlanner reused across requests, (b) what is the actual station element type, (c) any other per-request state on long-lived objects?

**Findings:**

**(a) QueryPlanner IS a singleton, confirmed.** [agent.py:39-49](file:///c:/Users/shaik/OneDrive/Desktop/Github/usgs-water-agent/src/agent.py#L39-L49): `self._query_planner` is `None` in `__init__`, lazily constructed once in the `query_planner` property, and cached. Every call to `_handle_answer_daily_query` at [agent.py:115](file:///c:/Users/shaik/OneDrive/Desktop/Github/usgs-water-agent/src/agent.py#L115) uses `self.query_planner.plan(request)` — the same planner instance for every request. This confirms the blocker: `self._ctx` would have exactly the same concurrency bug as `self._last_spatial_results`.

**(b) Station elements are bare strings.** Both paths in [spatial_query.py](file:///c:/Users/shaik/OneDrive/Desktop/Github/usgs-water-agent/src/water_agent/tools/spatial_query.py):
- State-only path (line 44-48): extracts `monitoring_location_id` from time-series-metadata properties -> `set` of strings -> sorted list.
- County path (line 73): extracts `f["id"]` from monitoring-locations features -> `set` of strings. The GeoJSON features from `/monitoring-locations` DO carry `properties` with `monitoring_location_name`, `site_type_description`, `county_name`, `state_name`, etc. — but the code discards all of them, keeping only the bare ID.

This means `sample: list[dict]` with `id/name/site_type` cannot be populated from the current tool output. The summarizer `summarize_stations(stations: list[str])` also can't produce meaningful summaries beyond count.

**(c) No other per-request mutable state on long-lived objects.** The `_dispatch` dict on QueryPlanner holds tool instances, but those tools (`SpatialQueryTool`, `StationInventoryTool`) only have `self.fetcher` and `self.resolver` — long-lived, stateless-per-request. The `USGSWaterDataFetcher` holds an `httpx.AsyncClient` for connection pooling, but that's safe for concurrent use. The only per-request state is `self._last_spatial_results` and `self._last_inventory_results` at [query_planner.py:272-279](file:///c:/Users/shaik/OneDrive/Desktop/Github/usgs-water-agent/src/water_agent/query_planner.py#L272-L279).

**Additional finding:** The `create_artifact` SDK signature at [agent_response.py:106-127](file:///c:/Users/shaik/OneDrive/Desktop/Github/usgs-water-agent/.venv/Lib/site-packages/ichatbio/agent_response.py#L106-L127) is `async def create_artifact(self, mimetype, description, uris=None, content=None, metadata=None)`. It does NOT return an artifact ID. So `StoredResult.artifact_id` cannot be populated from the SDK return value.

**Options considered for station element shape:**
1. Keep bare strings, make sample just `[{"id": "USGS-xxx"}, ...]` — minimal info, but no enrichment needed and doesn't block the refactor.
2. Enrich stations in `SpatialQueryTool` by carrying name/county/site_type from the monitoring-locations features — requires changing the tool, adding a second API call on the state-only path (currently only calls time-series-metadata), and changing the return type.

**Decision:** Option 2 is the right long-term answer but it's a feature change, not a refactor. The refactor requirement says "Do not rewrite working agent functionality merely to make tests easier." I'll use option 1 for now: `sample` contains `[{"id": "USGS-xxx"}, ...]` for bare-string station lists. The summarizer produces `"38 stations (e.g. USGS-02087140, USGS-02087183, ...)"`. I'll add a plan note that enriching stations is deferred. For inventory results (which ARE dicts keyed by param_code), sample can include `{"code": "00060", "name": "Discharge"}`.

**Options considered for artifact_id:**
1. Keep the field, set it to `None` always — dead code.
2. Remove the field entirely.
3. Keep the field, generate a local UUID as an identifier and include it in the artifact metadata so it's at least correlated.

**Decision:** Option 3. Generate a UUID for `artifact_id` before calling `create_artifact`, include it in the artifact's `metadata` dict. It's not returned by the SDK, but it creates a correlation key between the store and the artifact.

**Consequence:** Sample shape for stations is `list[dict]` with `{"id": str}` only. The summarizer must accept `list[str]` (current tool output) and produce the sample internally. Enrichment deferred to a later ticket.

**Status:** resolved

---

## [2026-09-17 14:40] Station enrichment: audit was wrong about state-only path
**Step:** Pre-implementation (item 3 investigation)
**Issue:** The audit said enrichment was deferred because it "requires changing the tool's API call pattern." The user challenged this, saying the properties are "already fetched and discarded." Neither statement is fully accurate.

**Findings from live API calls:**

- **`/time-series-metadata` response** (used by state-only path, line 40): properties include `monitoring_location_id`, `parameter_name`, `parameter_code`, `state_name`, `begin`, `end`, `unit_of_measure`, `hydrologic_unit_code` — but **NOT** `monitoring_location_name`, `site_type`, or `county_name`. These fields simply aren't in this endpoint's schema.

- **`/monitoring-locations` response** (used by county path, line 72): properties include `monitoring_location_name`, `site_type`, `county_name`, `state_name`, and many more. The county path already fetches this data but only extracts `f["id"]` at line 73.

**Conclusion:** Two paths, two different answers:
- **County path:** name/site_type/county ARE already fetched and discarded. Enriching is ~3 lines of change.
- **State-only path:** name/site_type/county are NOT available. Would require an additional `/monitoring-locations` API call, which is genuinely a new API call pattern.

**Options considered:**
1. Enrich county path only (where data is free), leave state-only path with bare IDs.
2. Enrich both by adding a monitoring-locations call to the state-only path.
3. Defer both entirely.

**Decision:** Option 1. Enrich the county path in Step 2 since the data is already fetched. Leave state-only path bare — adding a second API call there is a feature change. Document the asymmetry.

**Consequence:** Sample shape for stations will be `[{"id": "USGS-xxx"}]` on the state-only path and `[{"id": "USGS-xxx", "name": "...", "site_type": "...", "county": "..."}]` on the county path. The summarizer must handle both shapes. The LLM sees richer samples for county queries but not state-only ones.

**Status:** resolved

---

## [2026-09-17 14:40] plan() callers: only one
**Step:** Pre-implementation (item 12 investigation)
**Issue:** Need to identify all callers of `plan()` before changing its signature.

**Findings:** Grep for `.plan(` and `QueryPlanner` across the entire repo:
- `agent.py:115` — `plan = await self.query_planner.plan(request)` — **the only caller**.
- `ask.py` — does NOT call `plan()` directly; it goes through `agent.run()` → `_handle_answer_daily_query` → `self.query_planner.plan(request)`.
- No test files call `plan()` directly.

**Decision:** Only `agent.py:115` needs updating in Step 4.

**Consequence:** None — cleaner than expected.

**Status:** resolved

---

## [2026-09-17 14:40] truncated semantics and fetcher pagination
**Step:** Pre-implementation (item 2 investigation)
**Issue:** `truncated` was defined as "count > len(sample)" which incorrectly marks normal complete results as truncated. Should mean upstream result is incomplete.

**Findings:** The fetcher ([usgs_water_data.py:166-204](file:///c:/Users/shaik/OneDrive/Desktop/Github/usgs-water-agent/src/usgs_water_data.py#L166-L204)) does NOT report whether pagination was exhausted. It sends `limit=N` and returns whatever comes back. The OGC API does return `numberMatched`/`numberReturned` in the response, but the fetcher strips those — it only returns `features`, `count` (which is `len(features)`), `request_url`, and `filters`.

**Options considered:**
1. Modify fetcher to pass through `numberMatched` and compare to `numberReturned` to detect truncation.
2. Set `truncated=False` always and defer.

**Decision:** Option 2 — modifying the fetcher is out of scope for this refactor. `truncated` defaults to `False`. Log as deferred.

**Consequence:** `truncated` field exists on `ResultHandle` but is always `False` until a future PR wires up the fetcher to report upstream pagination state.

**Status:** deferred
# #   [ 2 0 2 6 - 0 9 - 1 7 ]   S t e p   7   C o m p l e t e d :   R e m o v e d   s t a s h   r e s i d u e  
 # #   [ 2 0 2 6 - 0 9 - 1 7 ]   S t e p   8   C o m p l e t e d :   A d d e d   6   b e h a v i o r   t e s t s  
 # #   [ 2 0 2 6 - 0 9 - 1 7 ]   S t e p   9   C o m p l e t e d :   W i r e d   a r t i f a c t s   a n d   a r t i f a c t _ i d  
 # #   [ 2 0 2 6 - 0 9 - 1 7 ]   S t e p   1 0   C o m p l e t e d :   F u l l   r e g r e s s i o n   a n d   m a n u a l   t e s t s   p a s s e d  
 
## [2026-09-18] Custom Quota Request (Pre-Implementation)
**Step:** Pre-implementation (API Key limits)
**Issue:** The default USGS key tier is 1,000 requests/hour. Multi-page spatial queries will consume this quickly.
**Action:** User must send an email to wdfn@usgs.gov to request a custom quota. Noted here for tracking.
**Status:** pending user action

## [2026-09-18] A2 Constraint: Date Arithmetic
**Step:** Pre-implementation (Step 1 - Daily Values)
**Issue:** When the planner is updated to extract date ranges (A2 slice), the LLM must not resolve relative dates to absolute dates.
**Decision:** LLMs are unreliable at date arithmetic. The planner should emit relative definitions (e.g. {"relative": "P10Y"}) and CODE must resolve it against the current date.
**Status:** deferred to A2 slice

## [2026-09-18] Wrong-Answer Bug: Superlative Queries
**Step:** Pre-implementation (Step 1 - Daily Values)
**Issue:** The user asked "What is the maximum streamflow in North Carolina?". The planner correctly mapped "maximum" to `statistic_id=00001` (Daily Maximum), but the system then fetches `limit=5, sortby=-time` and reports "Most recent matching USGS Daily observation: 22.3 ft^3/s".
**Finding:** The agent is answering a geographic superlative question (what is the all-time maximum streamflow in the state) by providing the most recent reading of the daily-max statistic from whatever station happened to update last. The agent does NOT present it as the "all-time maximum" (it honestly says "Most recent matching..."), but it fails to answer the user's actual question, returning functionally wrong data for the user's intent.
**Status:** logged for future fix (out of scope for current fetcher slice)

## [2026-09-18] Planner Intent-Extraction Failures (A2 Slice)
**Step:** Pre-implementation (Step 1 - Daily Values)
**Issue:** Three distinct failures in the query planner's intent extraction, all preventing the fetcher from correctly serving daily values. All belong to the A2 planner slice:

1. **Superlative misinterpretation ("maximum"):**
   - **Tested:** "What is the maximum streamflow in North Carolina?"
   - **Result:** Resolves `statistic_id=00001` (Daily Maximum), then fetches `limit=5, sortby=-time`.
   - **Bug:** Answers a geographic all-time superlative question by returning the most recent single daily-max observation. Wrong answer (though honestly labeled as "Most recent matching...").

2. **Ambiguity on temporal phrasing ("most recent"):**
   - **Tested:** "What is the most recent streamflow reading at USGS-02087500?"
   - **Result:** Returns `clarification_needed` ("What 'most recent' statistic are you asking for? The most recent observation, maximum value, instantaneous discharge?"). Fetches nothing.

3. **Over-eager tool calling for specific stations:**
   - **Tested:** "latest streamflow at USGS-02087500", "what is the discharge at USGS-02087500?", "streamflow at USGS-02087500"
   - **Result:** The planner eagerly calls `station_inventory(monitoring_location_id="USGS-02087500")` because it is the only tool that accepts a station ID. The agent then intercepts the `inventory_ref` and returns an inventory summary instead of fetching daily values.
   - **Bug:** The `latest-readings` path is functionally unreachable for specific stations through the normal planner prompt.

**Status:** logged for future fix in the A2 planner slice

## [2026-10-12] Samples API: Parameter Catalog & Count Verification (Steps 1 & 2)
**Step:** Implementation (Discrete Data Slice - Steps 1 & 2)
**Issues & Discoveries:**
1. **Catalog Endpoint Format (Discovery):** Requesting mimeType=text/csv on https://api.waterdata.usgs.gov/samples-data/codeservice/characteristics yielded HTTP 415 ('Unsupported data format'). OpenAPI inspection confirmed the endpoint only serves application/json. Retrieved full catalog (11,949 items), confirmed and stripped all 663 Information group entries, and committed raw JSON (characteristics_2026-10-12.json), full CSV (characteristics_2026-10-12.csv), and filtered CSV (characteristics_filtered.csv with 11,286 rows).
2. **Catalog Pcode Validation:** All 73 advisor-specified parameter codes across the 9 families were validated against the catalog. 0 absent; 100% present.
3. **Pcode Collisions:** 6 pcodes have multiple catalog entries (e.g. 00400 has E. coli and pH; 00095 has specific conductance and water temp; 00300 has DO and transparency). For family mapping, matched the entry aligning with the target family.
4. **Medium Derivation:** Evaluated descriptors for medium keywords; all 73 pcodes resolved to medium='water'.
5. **Statewide Count Query Latencies (Discovery):** Large statewide count queries on results/count (e.g., 00300 DO, 00095 SpCond, 00618 Nitrate, 00400 pH, 00665 Phosphorus) exceeded default 45s timeouts. Implemented tiered retries (up to 180s) which successfully completed without server errors. Rate-limit guard logged x-ratelimit-remaining (~3,900+ / 4,000 quota), preventing throttling.
6. **Committed Config:** Generated src/water_agent/metadata/discrete_families.py and metadata/raw/references/discrete_families.json encoding all families, metadata, and verified NC counts.
**Status:** resolved (Steps 1 & 2 complete)

## [2026-10-12] Step 2b: Candidate Relationship & Unit Compatibility Classification
**Step:** Implementation (Discrete Data Slice - Step 2b)
**Classifications & Unit Safety:**
1. **dominant (auto-resolve):**
   - calcium: 00915 (filtered, mg/L) holds 98.5% of family records. (00910 has unit mg/L CaCO3 but is negligible with only 2 records). Auto-resolves.
   - chlorophyll_a: 70953 (phytoplankton, ug/L) holds 99.4% of family records. Auto-resolves.
   - dissolved_oxygen: 00300 (unfiltered, mg/L) holds 99.0% of family records. Auto-resolves.
2. **same_quantity (union by default, report split):**
   - electrical_conductivity: 00095 (field, 81.6%) and 90095 (lab, 18.3%) both share identical unit (uS/cm @25C). Union occurrences, report field vs lab split.
   - ph: 00400 (field, 80.3%) and 00403 (lab, 19.7%) both share identical unit (std units). Union occurrences, report field vs lab split.
   - organic_carbon: Retagged as nested_fraction. 00680 (TOC unfiltered, 56.5%) and 00681 (DOC filtered, 43.5%) both share mg/L as C. Because DOC is a dissolved sub-fraction of TOC, report TOC and DOC counts SEPARATELY, never combined. Values must NEVER be summed.
3. **different_quantity (clarify with counts; DO NOT SUM):**
   - nitrate (FLAGGED CONFLICTING UNITS): Substantive candidates mix mg/L as N (00618, 00631, 00620, 00630) with mg/L asNO3 (71851, 71850). Mixing or summing them causes major chemical error due to 4.43x MW conversion factor and species distinction (NO3 vs NO3+NO2). Clarify with counts.
   - phosphorus (FLAGGED CONFLICTING UNITS): Substantive candidates mix mg/L as P (00665, 00671, 00666, 70507) with mg/L asPO4 (00660, 00650, 71886) and ug/L as P (91004). Mixing or summing them causes major chemical error due to 3.06x MW conversion factor and form distinction (total P vs dissolved PO4). Clarify with counts.
   - turbidity_tss (FLAGGED CONFLICTING UNITS): Substantive candidates mix gravimetric solids (mg/L on 00530) with distinct optical scatter units (NTRU on 63676, NTU on 00076, FNU on 63680, JTU on 00070). Different physical quantities and instrument optics that must never be summed. Clarify with counts.
4. **Spatial Invariant:**
   - Spatial discovery queries ('where is X monitored') union across all family pcodes regardless of classification, de-duplicated by station ID. A station measuring any form of the parameter is an active monitoring location for that parameter. Counting distinct locations is always safe.
5. **Response Contract Units:**
   - Every answer citing a value or count must explicitly name the pcode, the descriptor, and the unit (`DiscreteParameter.format_citation()`).
**Status:** resolved


## [2026-10-12] Step 3: Discrete Data Fetcher (USGSSamplesFetcher) Implementation
**Step:** Implementation (Discrete Data Slice - Step 3)
**Implementation Details & Guardrails:**
1. **Count-First Guard:** All data queries execute a preflight count check (`get_results_count`, `get_locations_count`).
   - If count == 0, returns empty result immediately without downloading data profile.
   - If count > DISCRETE_MAX_RECORDS (5,000 threshold), the query is automatically partitioned: state queries are split across counties using `metadata/raw/references/counties.json`; county queries are split across annual date chunks. If any partition still exceeds threshold, raises `SamplesOverflowError` with explicit counts for clarification.
2. **Streaming CSV Parser:** Uses `httpx.AsyncClient.stream()` and `csv.reader` on lines as they arrive (`aiter_lines()`). Projects ~15 essential fields (location, pcode, activity start date, result measure value, units, detection limits, flags) and discards ~50 unused columns on the fly.
3. **Date Normalization:** Dates in USGS Samples CSV are returned in `M/D/YYYY` format; normalized to standard ISO `YYYY-MM-DD` via `to_iso_date()`.
4. **Three-State HTTP Response Handling:**
   - HTTP 200 with CSV data -> parsed and returned.
   - HTTP 200 with headers only (no records) -> handled gracefully as empty result.
   - HTTP 400 (validation error) -> logs warning and specific parameter errors.
   - HTTP 429 (rate limiting) -> aborts cleanly, logging remaining quota.
5. **Rate-Limit & Security:**
   - API key provided via `X-Api-Key` header; sanitized URL logging ensures API key is NEVER leaked in query strings or source URLs.
   - Result handles stored in `ResultStore` with clean source URLs, parameter citations, units, record counts, and discrete data disclaimer.
**Status:** resolved (Step 3 fetcher complete and tested)

## [2026-10-12] Step 3 Live Scale Verification & Partitioning Guardrails
**Step:** Verification (Discrete Data Slice - Step 3 Closeout)
**Findings & Metrics:**
1. **Live Scale Test 1(a) (Calcium 00915, NC):**
   - Direct streaming fetch retrieved all 27,409 records and 3,657 distinct stations in 2 requests (1 count + 1 narrow data stream).
   - Wall-clock: 111.27s (0.45s count, 110.82s data stream). Peak memory: 13.42 MB. Total bytes: 14,578,999 bytes (13.90 MB).
   - Verified 100% lossless against Step 2 baseline (3,657 stations).
2. **Streaming Limits (Test 1(b) pH 00400, NC):**
   - Querying 43,972 records unpartitioned resulted in `RemoteProtocolError: peer closed connection without sending complete message body` after 41.19s streaming. Demonstrates server-side socket resets on unconstrained streams >35,000-40,000 rows.
3. **Timeout Recovery & Parameter Fix (Test 1(c) SpCond 00095, NC):**
   - Cold query took 148.91s (streamed 29.18 MB, 54,442 records, 4,522 stations).
   - Upstream parameter for date filtering in modern Samples API is `activityStartDateLower` and `activityStartDateUpper` (legacy `startDateLo` caused HTTP 400). Updated fetcher.
   - Forced timeout triggered narrow-and-retry, recovering recent 5-year window in 3.7s with warning attached.
4. **Partitioning Architecture Decision:**
   - County partitioning in NC issues ~170-185 HTTP requests against 1,000/hour quota and suffers from unindexed historical table scans.
   - Temporal partitioning (by year) uses 5-10 requests and completes in seconds because `activityStartDateLower` is indexed upstream.
   - Set separate safe thresholds: `MAX_LOCATIONS_THRESHOLD = 5,000` (~2.25 MB); `MAX_RESULTS_THRESHOLD = 25,000` (~2.8 MB).
**Status:** resolved

## [2026-10-12] Step 4: Parameter Resolution and Disambiguation
**Step:** Implementation (Discrete Data Slice - Step 4)
**Implementation Details:**
1. **Query-Type Branching First:**
   - `resolve_discrete_parameter()` branches on `query_type == "spatial"` first, returning all family pcodes with `action="union_stations"`. Dominant rule is bypassed for spatial queries.
   - Value and count queries branch on classification (`dominant` auto-resolves, `same_quantity` unions occurrences, `nested_fraction` separates fractions, `different_quantity` requests clarification).
2. **ReferenceResolver Ambiguous Dead-End Fix:**
   - `ReferenceResolver.resolve()` automatically delegates parameter fields (`parameter_code`, `parameter`, `characteristic`, `usgsPCode`) to `resolve_discrete_parameter()`.
   - Continuous parameter multi-matches in `ReferenceStore` are filtered by default `medium="Water"`, resolving spurious ambiguities against sediment/tissue/soil entries.
3. **Response Model Extensions:**
   - Extended `ResolutionResult` with `status="clarification_needed"`, `resolved_values`, `action`, `dominant_value`, and `modality`.
   - Backward-compatible dictionary rendering in `ReferenceResolveTool`.
4. **Test Suite:**
   - 59 total tests passing across full suite.
**Status:** resolved (Step 4 complete and ready for Step 5)

## [2026-10-12] Step 4 Closeout: Timeout Partitioning, 1(c) Reconciliation, and Threshold Refactor
**Step:** Implementation & Verification (Discrete Data Slice - Step 4 Closeout)
**Decisions & Findings:**
1. **Item 1: Elimination of Silent Truncation on Timeout:**
   - Replaced narrow-and-retry (which silently bounded unconstrained queries to post-2021) with complete temporal partitioning (`_partition_results_temporally`).
   - On timeout or network drop (`httpx.ReadTimeout`, `httpx.ConnectTimeout`, `httpx.RemoteProtocolError`), the fetcher chunks the time range into discrete date windows, streaming each window and unioning all records to retrieve 100% of data.
   - If temporal partitioning also fails, an explicit `SamplesError` is raised. Silent date-range narrowing is eliminated.
   - Any bounded result explicitly includes record counts and date bounds in `ResultHandle.summary` and provenance metadata.
2. **Item 2: Reconciliation of 1(c) Record Count (54,442 vs 55,685):**
   - Verified empirically on the live USGS count service that the 1,243-record and 3-station delta between Test 1(c) and the Step 2 baseline was caused entirely by `activityMediaName=Water`.
   - Step 2 baseline without media filter: 55,685 records across 4,525 stations.
   - Query with `activityMediaName=Water`: 54,442 records across 4,522 stations.
   - Delta accounts for non-water media: `activityMediaName='Other'` (1,124 records), `activityMediaName='Sediment'` (50 records), and other non-aqueous media (69 records). Exactly 3 stations in NC monitor specific conductance only in non-water media. Zero records were lost.
3. **Item 3: Threshold Refactor & Per-Request Streaming Ceiling:**
   - Separated thresholds: `MAX_LOCATIONS_THRESHOLD = 5,000` (locations have no timestamp, so county partitioning is used when exceeding 5,000); `MAX_RESULTS_THRESHOLD = 25,000` (results default to temporal partitioning).
   - Added hard per-request row ceiling `STREAM_MAX_ROWS = 25,000` below ~30,000 rows to prevent USGS server-side socket resets (`RemoteProtocolError`).
   - Maintained `max_records` in `USGSSamplesFetcher` for backward compatibility.
**Status:** resolved

## [2026-10-12] Step 5: Discrete Tools and Routing Implementation
**Step:** Implementation (Discrete Data Slice - Step 5)
**Decisions & Findings:**
1. **DiscreteQueryTool Implementation:**
   - Created `DiscreteQueryTool` in `src/water_agent/tools/discrete_query.py`.
   - `query_results()`: Resolves parameters via `resolve_discrete_parameter()` (auto-resolving dominant pcodes, separating fractions, or requesting clarification for different quantities). Generates citations conforming strictly to the contract: `<count> discrete records of <descriptor> (<pcode>), <unit>`.
   - `find_stations()`: Enforces the **Spatial Invariant Rule**, querying and unioning monitoring locations across all parameter codes in the family and de-duplicating by location ID.
   - Flags `mixed_modality=True` for mixed-modality parameters (pH, DO, specific conductance, water temperature, turbidity) with the required disclaimer without unioning continuous data.
2. **USGSWaterAgent Discrete Routing:**
   - In `src/agent.py` (`_handle_answer_daily_query`), added inspection of resolved `parameter_code`. If the parameter belongs to discrete reference metadata (`get_parameter(param_code) is not None`), execution routes to `USGSSamplesFetcher` instead of Daily API.
   - Generates structured artifacts (`mimetype="application/json"`) correlated with `ResultStore` handles and outputs compliant responses.
   - Preserves public `USGSWaterAgent` class signature and entrypoints (`run`, `answer_daily_query`), with zero changes to the query planner prompt or continuous/OGC execution path.
3. **Test Suite Verification:**
   - Added unit test suite in `tests/test_discrete_tools_and_agent.py` covering dominant value queries, ambiguous clarification requests, spatial invariant union across family pcodes, and end-to-end agent routing.
   - Total test suite now contains 65 tests, 100% passing.
**Status:** resolved (Discrete data slice Steps 1–5 fully implemented and verified)

## [2026-10-13] Steps 1–3: Repeated Parameters, Reachability, and Deterministic Routing
**Step:** Implementation (Discrete Data Defect Fixes - Steps 1–3)
**Decisions & Findings:**
1. **Step 1: Repeated Query Parameters Refactor:**
   - USGS Samples API supports repeated query parameters (`usgsPCode=00665&usgsPCode=00660...`) in a single HTTP request for server-side family unions.
   - Comma-separated `usgsPCode="00665,00660"` was confirmed to silently fail upstream (returns `recordCount: 0` without error).
   - Added `_normalize_params()` guard in `USGSSamplesFetcher` converting comma-separated strings to parameter lists.
   - Refactored `DiscreteQueryTool.find_stations` to dispatch family unions in a single request.
   - Empirically measured: Calcium spatial latency dropped 4.1x from 19.16s to 4.71s (1 request, 3,663 stations lossless); Nitrate Wake County dropped from 10.38s to 1.96s (1 request, 113 stations).
2. **Step 2: Reachability & Deterministic Tool-Level Routing Architecture:**
   - *Decision:* Adopted **Agent / Tool-level deterministic routing** on resolved parameter codes rather than planner-level tool selection.
   - *Rationale:* Modality is an intrinsic property of the USGS hydrological observation infrastructure and catalog, not user intent. Code-level routing based on authoritative metadata (`discrete_families.py` and `parameter-codes.json`) provides 100% precision, prevents LLM tool hallucinations, requires minimal planner prompt modification (two sentences), and avoids divergent decision paths between planner and agent.
   - *Routing Details:*
     - `SpatialQueryTool` checks if `parameter_code` is discrete-only (`is_discrete and not is_mixed_modality`). If so, delegates to `DiscreteQueryTool.find_stations` with family union and returns normalized stations under `kind="stations"`. Continuous and mixed-modality parameters (DO, pH, specific conductance) strictly remain on the continuous OGC path per slice scope.
     - `StationInventoryTool` queries both continuous OGC time-series metadata and discrete summary data from `https://api.waterdata.usgs.gov/samples-data/summary/{id}`, excluding the `Information` metadata group.
     - `agent.py:_handle_answer_daily_query`: Inspects result provenance. For discrete spatial queries, replies according to citation contract (citing primary descriptor, dominant pcode, unit, total unioned stations, and discrete disclaimer). For station inventory, renders dual-modality sections (Continuous Sensor Data and Discrete Water Quality Samples).
3. **Step 3: Parameter Code Selection and Clarification Fixes:**
   - *Item 3a (Calcium Auto-Resolve):* `ReferenceSearchTool.search("parameter_code", "calcium")` intercepts discrete families, auto-resolving dominant substantive pcode `00915` (Calcium, filtered, 27,409 NC records, 98.5% dominance) instead of `00910` (unfiltered, 2 NC records). For spatial queries, `ReferenceResolver.resolve` returns `00915` as `dominant_val` while preserving the full 4-pcode family union.
   - *Item 3b (Phosphorus Clarification):* `ReferenceSearchTool.search("parameter_code", "phosphorus")` returns the substantive aqueous candidates from `discrete_families.json` with descriptors, units, and NC counts, completely excluding non-water media (bed sediment, suspended sediment). `QueryPlanner._extract_candidates` extracts up to 10 candidates with units and counts.
4. **Test Suite:**
   - All 71 tests passing (including 4 new tests specifically verifying Step 2 & 3 discrete features).
**Status:** resolved (Ready for Step 4 verification)

## [2026-10-13] Discrete Slice Closeout: Clarification Lead-In, Spatial Primary Param Policy, and Timestamp Formatting
**Step:** Refinement & Final Verification (Discrete Data Slice Closeout)
**Decisions & Findings:**
1. **Fix 1: Station-Scoped Clarification Lead-In & Candidate Sourcing:**
   - Station inventory results from `/samples-data/summary/{id}` stored in `ctx.store` are directly inspected when clarification is needed for a station-scoped query.
   - Lead-in is derived dynamically from the station's actual monitored parameters (e.g., `Multiple distinct phosphorus parameters are monitored at **USGS-0423205010**...`), completely eliminating contradictory "The station inventory does not list..." phrasing.
   - Candidate counts and date ranges are rendered using the station's own observations (4,107 obs for 00671, 4,101 obs for 00660, 4,051 obs for 00665), eliminating statewide NC total leakage on station queries.
   - Priority matching rules ensure exact descriptor matches take precedence over broad characteristic name matches, guaranteeing 100% accurate pcode and unit citations.
   - Statewide counts are preserved as a fallback only when no station scope exists, explicitly labeled `Available candidates (North Carolina baseline totals):`.
2. **Fix 2: Omission of "Primary Parameter" for Non-Dominant Spatial Queries:**
   - The "Primary parameter:" citation line is now strictly restricted to families with mathematically verified dominance (`fam.relationship == "dominant"`, e.g. Calcium 98.5%, Chlorophyll a, Dissolved Oxygen).
   - For all other families (`different_quantity`, `same_quantity`, `nested_fraction`), the "Primary parameter:" line is omitted in spatial responses. The report cites the total count and unioned pcodes without falsely elevating any candidate as primary.
   - Empirical investigation on Nitrate in Wake County, NC confirmed that the 7 trace/99xxx codes (`99133`, `91003`, `99121`, `99124`, `99137`, `99889`, `51290`) contribute **0 unique stations** beyond the 6 substantive pcodes (6 substantive = 113 stations, 7 trace = 19 stations, union of all 13 = 113 stations).
3. **Fix 3: Timestamp Formatting:**
   - Introduced `format_date_for_display()` across continuous and discrete inventories, stripping microsecond noise (`.000001`) and ISO time strings (`T21:27:00`) down to clean `YYYY-MM-DD` strings.
4. **Verification & Regression Checks:**
   - Test suite expanded to 74 tests, 100% passing.
   - Live query 4b (Nitrate Wake County): 113 stations, no Primary parameter line, discrete modality cited.
   - Live query 4c (Phosphorus USGS-0423205010): Station-derived lead-in, station counts (4,107 / 4,101 / 4,051) and exact pcodes (00671 / 00660 / 00665).
   - Live query 4d (USGS-0423205010 Inventory): Dual modality, clean `YYYY-MM-DD` timestamps throughout.
   - Continuous path regression check: Streamflow Wake County (38 stations) and Discharge count at USGS-02087500 (39,165 records) preserved with zero diff.
**Status:** resolved (Discrete data slice fully closed out; approved for iChatBio connection)

---

## [2026-10-14] Simplification Slice: Step 1 — Removal of Classification Taxonomy

**Step:** Step 1 (SIMPLIFICATION SLICE)  
**Issue:** The discrete data path was originally designed around a candidate relationship taxonomy (`dominant`, `same_quantity`, `nested_fraction`, `different_quantity`). Its sole purpose was to decide when counts could be summed or whether a pcode could be auto-resolved as dominant on behalf of the user. However, under the established system contract, the agent *never* sums, computes, merges, ranks, or picks a "primary" parameter. The taxonomy was therefore dead code.

**Decisions & Changes:**
1. **Removed Taxonomy Schema & Logic:**
   - Deleted the `relationship` field and all four values (`dominant`, `same_quantity`, `nested_fraction`, `different_quantity`) across `discrete_families.json` and `discrete_families.py`.
   - Deleted `has_conflicting_units` and `substantive_units` from `DiscreteFamily` definitions.
   - Removed `CandidateRelationship` type.
   - Removed query-type and classification branching from `resolve_discrete_parameter()`. The function now performs a direct, flat lookup returning `status="resolved"` with all family parameter codes ordered by verified record count descending.
   - Removed auto-resolution logic (e.g. Calcium silently resolving to `00915`).
   - Removed all emissions of the `Primary parameter:` line from agent responses and spatial queries.
   - Removed count combining for same-quantity families (e.g., pH `43,972 + 10,767 -> 54,739`).
2. **Retained Flat Lookup & Guardrails:**
   - Kept flat lookup mapping: `family name -> list of pcodes` and `pcode -> metadata`.
   - Kept per-pcode metadata: `descriptor`, `characteristicNameUserSupplied`, `unit_of_measure`, `medium`, and verified NC counts.
   - Kept Information group exclusion (663 metadata entries remain permanently filtered out).
   - Kept NWIS-registry constituent pinning against the 37 crosswalk-artifact parameter codes.
3. **Aligned Tools & Resolvers:**
   - Updated `ReferenceResolver.resolve()`: Returns `resolved_values=discrete_res.pcodes` directly without dominant value selection or action branching.
   - Updated `ReferenceSearchTool.search()`: Presents all parameters in the family ordered by verified record count descending without auto-resolving.
   - Updated `SpatialQueryTool` & `agent.py`: Stripped `dominant_pcode` and `Primary parameter:` line construction.
4. **Test Verification:**
   - Updated test suite (`test_reference_resolver.py`, `test_discrete_tools_and_agent.py`, `test_samples_fetcher.py`) to align with flat lookup behavior.
   - All 75 tests passing (100% green).

**Status:** resolved (Step 1 complete; ready for report-back)

---

## [2026-10-14] Simplification Slice: Steps 2–4 — Four Query Shapes, Behavioral Decisions, and Presentation Contract

**Step:** Implementation (SIMPLIFICATION SLICE - Steps 2, 3, 4)  
**Decisions & Findings:**

1. **Step 2: Support for the Four Discrete Query Shapes:**
   - **Shape 2a ("Where is calcium monitored in North Carolina?"):**
     - Executes server-side family union (`00910`, `00915`, `00916`, `00918`) in 1 request with repeated `usgsPCode` parameters.
     - Reports total distinct deduplicated station count (3,663 in NC).
     - Emits deduplication statement: *"Stations monitoring multiple forms appear once in this total."*
     - Generates per-parameter station count breakdown table citing NWIS pcode, full descriptor, unit, and count.
     - Embeds first 50 station IDs in chat response and full station set in JSON artifact.
     - Includes discrete modality disclaimer and water media disclosure.
     - Strictly omits auto-resolution and "Primary parameter:" lines.
   - **Shape 2b ("What information is available at USGS-0423205010?"):**
     - Queries both `/time-series-metadata` (OGC API) and `/summary/{id}` (Samples API).
     - Renders distinct dual-modality sections: `### Continuous Sensors` and `### Discrete Samples`.
     - Explicitly labels continuous time-series records as `"estimated from YYYY-MM-DD to YYYY-MM-DD at daily interval"`, avoiding misleading pseudo-exact totals.
     - Lists discrete observations by characteristic group with exact counts and dates.
     - Permanently filters out internal `Information` metadata group (663 entries).
     - Formats all dates to `YYYY-MM-DD` using `format_date_for_display()`.
   - **Shape 2c ("How many phosphorus measurements at USGS-0423205010?"):**
     - Queries `/summary/{station_id}` for family parameter codes.
     - Presents every matching parameter as an individual row with 5-digit pcode, descriptor, unit, exact station observation count, and date range (`YYYY-MM-DD`).
     - Never merges or sums counts across parameter codes.
     - Never interrupts user with a clarification question asking to choose between parameters in the same family.
     - Sourced truthfully from station summary; never claims parameter is absent if present in station summary.
   - **Shape 2d ("Show me the calcium values at USGS-0423205010"):**
     - Executes count-first guard via `/results/count`.
     - Emits observation total and renders first 10 observations in a clean markdown table: `| Date | Time | Value | Unit | Parameter Code | Descriptor |`.
     - Embeds complete observation dataset in attached JSON artifact.
     - Applies date filters (`activityStartDateLower`, `activityStartDateUpper`) formatted as `YYYY-MM-DD`.
     - Discloses modality and media constraints.

2. **Step 3: The Four Behavioral Decisions:**
   - **Decision 3a (No Auto-Resolution):** All 9 discrete families resolve to their complete parameter lists on spatial queries and present full candidate breakdowns without silent collapse to a dominant code.
   - **Decision 3b (Parameters Outside Verified Families):** For constituents outside the 9 verified families (e.g., mercury), the agent searches `characteristics_filtered.csv` / `parameter-codes.json`, presents matching USGS registry codes with descriptors, units, and groups, plainly informs the user that no verified family configuration exists yet, and asks the user to pick.
   - **Decision 3c (Media Filtering Disclosure):** Preserves `activityMediaName=Water` filter on all discrete requests and emits the standard disclosure in every discrete response:
     > *"Sample media: Water (samples in other media such as sediment/tissue are excluded)."*
   - **Decision 3d (Mixed-Modality Parameter Routing):** Mixed-modality parameters (pH `00400`, DO `00300`, specific conductance `00095`, turbidity `00076`) route to the discrete Samples API with the mixed-modality disclaimer:
     > *"Note: Continuous sensor data for this parameter also exists in USGS systems and is not included in this discrete query."*
     The system never unions continuous and discrete modalities.

3. **Step 4: The Presentation & Provenance Contract:**
   - Every row across all responses carries 5-digit NWIS pcode, full descriptor, unit of measure, count, and date range (`YYYY-MM-DD`).
   - Strict present-don't-decide principle: zero merging, zero computing, zero ranking.
   - All response artifacts carry `source_urls`, retrieval timestamp (`retrieved_at` in ISO 8601 UTC), and queried `pcodes`.

4. **Test Suite Verification:**
   - Added unit and regression tests in `tests/test_discrete_tools_and_agent.py` covering shapes 2b, 2d, outside-family mercury search (3b), and mixed-modality routing (3d).
   - Test suite expanded to 79 tests, 100% passing.

**Status:** resolved (Steps 2–4 fully implemented, tested, and documented)
