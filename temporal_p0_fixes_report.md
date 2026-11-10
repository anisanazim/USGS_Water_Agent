# Temporal Handling P0 Bug Fixes Report

**Branch:** `feature/temporal-p0-fixes`  
**Baseline Commit:** `3774f0d` (`feat(eval): complete Priority 1 evaluation harness and failure-mode benchmark scorecard`)  
**Status:** Completed & Verified (`120` baseline -> `135` initial P0 commits -> `148` with pre-merge follow-ups; all `148` offline unit/regression tests passing locally)

---

## 1. Executive Summary & Scope

This report documents the resolution of four Priority 0 (P0) correctness bugs in temporal data querying and parameter resolution within the USGS Water Agent, plus pre-merge review follow-ups.

### Scope Boundaries
In accordance with task constraints:
- **Strictly In-Scope (P0)**: Fail-closed date parsing and calendar validation, explicit `datetime` interval validation, single-per-fetch logging of unparseable CSV row dates, applying date bounds to station parameter counts, sanitizing OGC daily-values parameters and tracking truncation cleanly (without false truncation on fixed latest-N limits), and guarding organic carbon parameters (`00680`/`00681`) against inorganic carbon/DIC/PIC requests across all filter and semantic fields.
- **Strictly Out-of-Scope (P1–P3)**: Dedicated `temporal.py` NLP parser, month/season parsing, relative date resolution, planner prompt changes, and modality routing overhauls. These are captured in Section 5.

---

## 2. Issues Diagnosed, Solutions Implemented, and Before/After Behavior

### Item 1: Fail Closed on Unparseable and Invalid Dates (+ Row Date Warning Follow-up)
*Commits: `f7b1db8`, `575bc7e`*

#### The Issue
- In `src/water_agent/fetchers/samples_fetcher.py`, `to_iso_date` previously returned unrecognized strings unchanged (e.g., `"January 2020"`). When forwarded to the USGS Samples API as `activityStartDateLower` or `activityStartDateUpper`, USGS returned an `HTTP 400 Bad Request`.
- Regex-only checks failed to validate calendar dates, allowing invalid dates such as `"2023-02-30"` to be sent upstream.
- During CSV streaming projection (`_project_result_row`), rows with malformed dates silently coerced to `None` with no aggregate warning.

#### Solution
1. **`DateParseError` Exception**: Added `class DateParseError(ValueError)` in `samples_fetcher.py`, carrying the invalid `value`.
2. **Strict Calendar Validation**: `to_iso_date()` accepts only `YYYY-MM-DD` and `M/D/YYYY`, validating real calendar dates via `datetime.date(year, month, day)`. Unrecognized strings or invalid calendar dates raise `DateParseError`. `None` or `""` returns `None`.
3. **`resolve_date_window(filters)`**: Reads `start_date | activityStartDateLower` and `end_date | activityStartDateUpper`, normalizes both via `to_iso_date()`, and raises `DateParseError` if `start > end`.
4. **Row Date Parse Warning (`_project_result_row` / `_stream_and_project_results`)**: Counts rows whose non-empty date raised `DateParseError` during projection (coercing `activity_date` to `None`) and logs one `WARNING` per fetch with the total count.
5. **Fail-Closed Handlers & Tools**:
   - `src/water_agent/handlers/observation.py`: Catches `DateParseError` prior to any fetch and replies telling the user which value failed and that dates must be `YYYY-MM-DD` (with an example).
   - `src/water_agent/tools/discrete_query.py`: Uses `resolve_date_window` and returns `{"status": "error", "error": ...}` on `DateParseError` without fetching.

#### Before vs. After Behavior
- **Before:** `"January 2020"` or `"2023-02-30"` was forwarded to USGS, triggering an upstream `HTTP 400`.
- **After:** Rejected immediately prior to any network request with format guidance and zero upstream calls; CSV stream rows with bad dates emit a single warning with the row count per fetch.

---

### Item 2: Bounded Station Parameter Counts
*Commit: `605d584`*

#### The Issue
- In `src/water_agent/handlers/count.py` (`handle_parameter_count`), params sent to `/results/count` only contained `monitoringLocationIdentifier` and `usgsPCode`, so queries with `start_date`/`end_date` returned lifetime totals.
- The per-pcode breakdown table came from station inventory metadata (lifetime counts) and appeared next to bounded totals without qualification.

#### Solution
1. **Window Resolution**: Integrated `resolve_date_window(plan.filters)`, handling `DateParseError` fail-closed without fetching.
2. **Upstream Bounds**: Populates `activityStartDateLower` and/or `activityStartDateUpper` on `/results/count` params when present.
3. **Transparent User Reply**:
   - Qualifies the total in the reply (`"between 2020-01-01 and 2023-07-31"`, `"from 2020-01-01"`, or `"up to 2023-07-31"`).
   - Relabels the per-pcode breakdown column to `"Lifetime observations (all dates)"` whenever a date window is active.
4. **Artifact Metadata & Baseline Fidelity**: Records `"date_lower"` and `"date_upper"` (`None` when absent) in artifact payload and metadata, while keeping no-date responses byte-for-byte identical to baseline.

#### Before vs. After Behavior
- **Before:** Bounded parameter count queries returned lifetime totals and unlabeled lifetime per-pcode counts.
- **After:** Upstream count query applies the date bounds, the reply states the applied window, and the lifetime breakdown column is explicitly labeled.

---

### Item 3: Daily-Values Continuous Query Sanitization, Explicit `datetime` Validation, & Truncation Tracking
*Commits: `8edd88d`, `575bc7e`*

#### The Issue
- In `src/water_agent/handlers/observation.py` and `src/usgs_water_data.py`:
  - `fetch_daily_values` spread `plan.filters` directly into the OGC query (`**filters`), leaking non-queryable keys (`start_date`, `end_date`, unknown keys).
  - Nothing populated `filters["datetime"]`, leaving the bounded daily branch dead code, while an explicit `plan.filters["datetime"]` was passed upstream unvalidated.
  - Bounded daily queries passed `sortby=None`, making the `"Most recent"` label on `readings[0]` unreliable.
  - `_fetch_all_pages` silently truncated at `max_records` (10,000) without surfacing truncation to callers or users.

#### Solution
1. **Queryable Sanitization**: Builds `ogc_filters` keeping only keys where `daily_registry.has(key)` is true (dropping `"time"`, `"datetime"`, `"start_date"`, `"end_date"`, and unknown keys, logging dropped keys at `DEBUG`).
2. **Explicit `datetime` & Window Interval Building (`validate_datetime_interval`)**:
   - If explicit `plan.filters["datetime"]` is non-empty, validates it as `"A/B"` where each side is normalized via `to_iso_date()` or `".."` (disallowing `"../.."`, invalid dates, malformed strings, or `start > end`), raising `DateParseError` on failure with the same user-facing reply and zero fetches.
   - Otherwise resolves `"start/end"`, `"start/.."`, or `"../end"` from `resolve_date_window(plan.filters)`.
   - Bounded queries pass `sortby="-time"`, and format the reply with the window, count returned, earliest and latest dates, and most recent reading.
3. **Truncation Condition & Fixed Latest-N Handling**:
   - Added `truncated: bool = False` to `PagedResult`.
   - In `_fetch_all_pages(..., fixed_limit: bool = False)`: marks `truncated = True` (unless `fixed_limit=True`) when either `len(features) >= max_records` at the moment a `"next"` link still existed, OR `len(features) > max_records` (requiring slicing to `max_records`).
   - `fetch_daily_values` passes `fixed_limit=(limit is not None and limit <= 10000)` so fixed latest-N queries (e.g., `limit=5` returning 5 rows with a `"next"` link) do **not** falsely set `truncated = True`, while unbounded queries (`limit=None`) or requests exceeding the 10,000 cap still report `truncated = True`.
   - When `truncated=True`, the reply states results were capped and advises a narrower window, and `metadata["truncated"]` is recorded on the artifact.

#### Before vs. After Behavior
- **Before:** Non-queryables leaked into OGC queries; bounded date queries didn't run or lacked sorting/validation; truncation at 10,000 rows was silent (and later latest-N `limit=5` with a `"next"` link risked false truncation).
- **After:** Only validated OGC queryables are sent; both `datetime` intervals and `start_date`/`end_date` are validated and sorted (`sortby="-time"`); truncation is tracked accurately for capped ranges and suppressed for satisfied fixed limits.

---

### Item 4: Inorganic Carbon Guard
*Commits: `988450b`, `575bc7e`*

#### The Issue
- The approved carbon scope is Total Organic Carbon (`00680`) and Dissolved Organic Carbon (`00681`), and `organic_carbon` has the alias `"carbon"`.
- **Before (on baseline `3774f0d`)**: `resolve_discrete_parameter` returned `status="not_found"` for `"inorganic carbon"`, `"DIC"`, and `"PIC"` when the local untracked catalog CSV was absent (or unrelated sediment/pesticide candidates if `characteristics_filtered.csv` was populated locally). The critical risk was the LLM planner shortening `"inorganic carbon"` to `"carbon"`, causing `resolve_discrete_parameter("carbon")` and `extract_pcodes_and_family` to resolve via the `"carbon"` alias to `00680`/`00681`.

#### Solution
1. **`DiscreteResolutionResult` Type & Shared Guard Helper (`discrete_families.py`)**:
   - Added `"unsupported"` to `DiscreteResolutionResult.query_type` (`Literal["spatial", "value", "count", "unsupported"]`).
   - Added `INORGANIC_CARBON_PATTERN` (`\b(inorganic\s+carbon|dic|pic)\b`, case-insensitive), `INORGANIC_CARBON_UNSUPPORTED_MSG`, and shared helper `is_inorganic_carbon_request(filters, semantic, request)`.
   - `resolve_discrete_parameter()` returns `status="clarification_needed"`, `query_type="unsupported"`, empty `pcodes`, and `INORGANIC_CARBON_UNSUPPORTED_MSG` naming `00680` and `00681`.
2. **Handler Enforcement (`count.py` & `observation.py`)**:
   - `is_inorganic_carbon_request` checks **all** of `plan.filters["parameter_code"]`, `plan.filters["parameters"]`, `plan.filters["parameter"]`, and every value in `plan.semantic` (plus `request` in `count.py`) rather than short-circuiting on the first truthy field.
   - Both `handle_parameter_count` and `handle_observation` reply with `INORGANIC_CARBON_UNSUPPORTED_MSG` (naming `00680` and `00681`) and return immediately with zero upstream fetches. `observation.py` also consistently handles `p_res.status == "clarification_needed" and p_res.query_type == "unsupported"`.

#### Before vs. After Behavior
- **Before:** `"inorganic carbon"` / `"DIC"` / `"PIC"` returned `not_found` (or unrelated catalog items) directly, or resolved to TOC (`00680`) / DOC (`00681`) whenever the planner emitted `parameter_code="carbon"`.
- **After:** Any request, filter, or semantic field containing `"inorganic carbon"`, `"DIC"`, or `"PIC"` is blocked upfront (even if `parameter_code="carbon"`), returning an explicit not-supported reply naming `00680` and `00681` with zero fetches.

---

## 3. Git Commit Summary

Branch: `feature/temporal-p0-fixes`

| Commit | Item | Description |
| :--- | :--- | :--- |
| `f7b1db8` | Item 1 | `fix(temporal): fail closed on invalid date strings in Samples fetcher and tools` |
| `605d584` | Item 2 | `fix(temporal): apply date window to station parameter counts` |
| `8edd88d` | Item 3 | `fix(temporal): continuous daily values date windowing, non-queryable filter stripping, and truncation tracking` |
| `988450b` | Item 4 | `fix(carbon): guard inorganic carbon, DIC, and PIC from resolving to organic carbon` |
| `58a5899` | Docs | `docs: add temporal handling P0 bug fixes report` |
| `575bc7e` | Follow-ups 1–6 | `fix(temporal): pre-merge follow-ups for guards, latest-N truncation, explicit datetime, and row warnings` |

---

## 4. Verification & Test Suite Results

### Offline Test Progression
- **Baseline (`3774f0d`):** `120` offline tests.
- **After P0 Items 1–4 (`58a5899`):** `135` offline tests (`+15`).
- **After Pre-Merge Follow-ups (`575bc7e`):** **`148` offline tests** (`+13`, total `+28` new offline tests added across `test_samples_fetcher.py`, `test_discrete_tools_and_agent.py`, `test_daily_temporal.py`, and `test_inorganic_carbon_guard.py`).

### Full Offline Suite Run
Command:
```powershell
python -m pytest --ignore=tests/smoke --ignore=tests/test_e2e_queries.py --ignore=tests/eval tests/ -v
```

**Results:**
- **148 passed in 56.67s** in this workspace (where locally generated `metadata/raw/references/characteristics_filtered.csv` exists).
- **Pre-existing Baseline Note:** `test_step_3b_outside_verified_families_mercury` fails in clean checkouts without the untracked `metadata/raw/references/characteristics_filtered.csv` file on baseline `3774f0d` as well as on this branch (pre-existing reference-data dependency; generated locally by `fetch_reference_data.py`).

### Skipped Suites (Network / Live LLM Excluded)
- `tests/smoke/`: Skipped (hits live USGS APIs).
- `tests/test_e2e_queries.py`: Skipped (hits live USGS/LLM endpoints and relies on external test harness utilities).
- `tests/eval/`: Skipped (live evaluation benchmarks and LLM judges).

---

## 5. Backlog & Future Work (Agreed P1–P3 Roadmap)

The following items remain intentionally out of scope for this P0 PR and represent the agreed follow-on roadmap:

- **P1 — Temporal Module & Discrete Path Integration**:
  - Implement `temporal.py` supporting date **range** resolution + **month mask** filtering.
  - Meteorological winter convention: e.g., `winter 2022 = Dec 2021–Feb 2022`.
  - Support a fixed set of relative date expressions with an injected `today` date for deterministic testing.
  - Planner emits structured temporal fields rather than pre-computed ISO dates.
  - Wire the temporal resolver into the discrete observation path.
- **P2 — Aggregations & Breakdowns**:
  - Month and season counts with per-code breakdown.
  - Distinct sample dates counting/reporting.
  - Exclude parameter code `90400`.
- **P3 — Routing & Hydrology Calendar Extensions**:
  - Improve discrete vs. continuous modality routing.
  - Support water-year phrases.
