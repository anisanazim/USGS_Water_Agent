# Temporal Handling P0 Bug Fixes Report

**Branch:** `feature/temporal-p0-fixes`  
**Baseline Commit:** `3774f0d` (`feat(eval): complete Priority 1 evaluation harness and failure-mode benchmark scorecard`)  
**Status:** Completed & Verified (135 offline unit/regression tests passing)

---

## 1. Executive Summary & Scope

This report documents the resolution of four Priority 0 (P0) correctness bugs in temporal data querying and parameter resolution within the USGS Water Agent. 

### Scope Boundaries
In accordance with task constraints:
- **Strictly In-Scope (P0)**: Fail-closed date parsing and calendar validation, applying date filters to station parameter counts, sanitizing OGC daily-values parameters and tracking truncation, and guarding organic carbon parameters (00680/00681) against inorganic carbon requests.
- **Strictly Out-of-Scope (P1–P3)**: Natural language month/season parsing (e.g., `"January 2020"`, `"winter"`), relative date resolution (e.g., `"last 30 days"`, `"yesterday"`), new `temporal.py` modules, planner prompt alterations, and discrete-vs-continuous routing overhauls. These items are documented as backlog TODOs.

---

## 2. Issues Diagnosed, Solutions Implemented, and Before/After Behavior

### Item 1: Fail Closed on Unparseable and Invalid Dates
*Commit: `f7b1db8`*

#### The Issue
- In `src/water_agent/fetchers/samples_fetcher.py`, `to_iso_date` previously used a lax regex and returned unparsed strings unchanged (e.g. `"January 2020"`). When forwarded to the USGS Samples API as `activityStartDateLower` or `activityStartDateUpper`, USGS returned an `HTTP 400 Bad Request`.
- Furthermore, regex pattern matching did not validate calendar dates, allowing invalid dates like `"2023-02-30"` to be submitted upstream.

#### Solution
1. **`DateParseError` Exception**: Added `class DateParseError(ValueError)` in `samples_fetcher.py`, carrying the invalid `value`.
2. **Strict Calendar Validation**: `to_iso_date()` accepts only `YYYY-MM-DD` and `M/D/YYYY` formats, validating them via `datetime.date(year, month, day)`. Invalid calendar dates or any unrecognized strings raise `DateParseError`. `None` or empty strings return `None`.
3. **`resolve_date_window(filters)`**: Added standard helper reading `start_date | activityStartDateLower` and `end_date | activityStartDateUpper`, normalizing both with `to_iso_date`, and asserting `start <= end` (raising `DateParseError` if `start > end`).
4. **Fail-Closed Handlers & Tools**:
   - `src/water_agent/handlers/observation.py`: Catches `DateParseError` before any fetch, logs the issue, and replies to the user with a helpful format message (`"Dates must be specified in YYYY-MM-DD format (for example, '2023-01-15')."`).
   - `src/water_agent/tools/discrete_query.py`: Normalizes inputs via `resolve_date_window` and returns the standard error shape `{"status": "error", "error": ...}` on `DateParseError`.

#### Before vs. After Behavior
- **Before:** `"January 2020"` or `"2023-02-30"` was sent to USGS, causing an unhandled upstream `HTTP 400` crash.
- **After:** Caught immediately prior to any network request. The agent responds informing the user that the date format must be `YYYY-MM-DD`, with zero upstream network calls.

---

### Item 2: Bounded Station Parameter Counts
*Commit: `605d584`*

#### The Issue
- In `src/water_agent/handlers/count.py` (`handle_parameter_count`), query parameters sent to `/results/count` only contained `monitoringLocationIdentifier` and `usgsPCode`. Even when the user or planner specified a date range, the query returned the station's lifetime observation count.
- The per-pcode breakdown table pulled counts from the station inventory, which are lifetime numbers, presenting lifetime figures alongside a bounded total without distinction.

#### Solution
1. **Window Resolution**: Integrated `resolve_date_window(plan.filters)`, handling `DateParseError` with fail-closed user replies.
2. **Upstream Bounds**: Added `activityStartDateLower` and/or `activityStartDateUpper` to the Samples API params whenever dates are present.
3. **Transparent User Reply**:
   - Added date qualification in the reply text (e.g., `"Found **X** discrete observation(s) between 2020-01-01 and 2023-07-31..."`, `"from 2020-01-01..."`, or `"up to 2023-07-31..."`).
   - When a date window is applied, relabeled the inventory table breakdown column to `"Lifetime observations (all dates)"` to avoid misleading the user.
4. **Artifact Metadata**: Added `"date_lower"` and `"date_upper"` (set to `None` when absent) to both artifact payload and metadata.
5. **No-Date Fidelity**: Retained byte-for-byte exact compatibility with baseline behavior when no date window is requested (using `"Station Observations"` column title and unwindowed phrase).

#### Before vs. After Behavior
- **Before:** "How many phosphorus records at USGS-0423205010 from 2020-01-01?" returned the station's lifetime count (e.g., 3,000+), ignoring the date filter completely.
- **After:** Applies `activityStartDateLower="2020-01-01"`, returns the exact bounded count, explicitly states the window in the response, and clearly disclaims that the per-pcode inventory table reflects lifetime observations.

---

### Item 3: Daily-Values Continuous Query Sanitization & Truncation Tracking
*Commit: `8edd88d`*

#### The Issue
- In `src/water_agent/handlers/observation.py` (continuous path) and `src/usgs_water_data.py`:
  - `fetch_daily_values` unpacked `plan.filters` directly into the OGC API query string (`**filters`). Planner keys like `start_date`, `end_date`, `parameter_code`, or arbitrary filters were passed upstream to endpoints that reject unrecognized queryables.
  - Nothing ever populated `filters["datetime"]`, rendering the bounded continuous branch dead code.
  - In the bounded continuous branch, `sortby` was unset, but the reply labeled `readings[0]` as "Most recent", which was factually inaccurate.
  - `_fetch_all_pages` silently truncated results when hitting `max_records` (10,000) without alerting the caller or user.

#### Solution
1. **Queryable Parameter Sanitization**: Filtered `plan.filters` against `daily_registry.has(key)`, explicitly discarding `"time"`, `"datetime"`, `"start_date"`, `"end_date"`, and unknown keys, logging dropped keys at `DEBUG` level.
2. **OGC `datetime` Interval**: Resolved `datetime` parameter from `resolve_date_window(plan.filters)` into ISO intervals (`"start/end"`, `"start/.."`, or `"../end"`). Explicit `plan.filters["datetime"]` takes precedence if present; falls back to latest-N if absent.
3. **Sorting & Format**: Passed `sortby="-time"` so the latest reading is reliably first. Updated response formatting to report total records retrieved, date boundaries (earliest and latest), and the most recent value.
4. **Truncation Flagging**:
   - Added `truncated: bool = False` to `PagedResult` in `src/usgs_water_data.py`.
   - Set `truncated = True` in `_fetch_all_pages` if `len(features) >= max_records` while a `"next"` link exists.
   - Surfaced a truncation warning in the reply advising the user that results were capped and suggesting a narrower time window, and persisted `"truncated": True` in artifact metadata.

#### Before vs. After Behavior
- **Before:** Non-queryable keys leaked into OGC query parameters; bounded date branches were ignored; result sets hitting limits were truncated silently without notification.
- **After:** Only registry-validated queryables are sent; date windows generate clean OGC `datetime` ranges with descending time sorts; truncation is tracked and communicated transparently.

---

### Item 4: Inorganic Carbon Guard
*Commit: `988450b`*

#### The Issue
- The approved parameter scope for carbon in the USGS agent is Total Organic Carbon (`00680`) and Dissolved Organic Carbon (`00681`). The `organic_carbon` parameter family includes the alias `"carbon"`.
- Requests for inorganic carbon (e.g. `"how many inorganic carbon records at USGS-01646500"`) previously resolved `"carbon"` to the `organic_carbon` family, erroneously returning organic carbon codes (`00680`/`00681`).

#### Solution
1. **Guard Regex**: Defined `INORGANIC_CARBON_PATTERN = re.compile(r"\b(inorganic\s+carbon|dic|pic)\b", re.IGNORECASE)` in `src/water_agent/metadata/discrete_families.py`.
2. **Resolution Guard**: In `resolve_discrete_parameter()`, any term matching the pattern immediately returns `status="clarification_needed"` and `query_type="unsupported"` with the message:
   `"Inorganic carbon (including DIC and PIC) is not supported. Supported carbon parameters are Total Organic Carbon (00680) and Dissolved Organic Carbon (00681)."`
3. **Count Extraction & Handler Guards**:
   - In `src/water_agent/handlers/count.py` (`extract_pcodes_and_family`), requests or parameters matching the inorganic carbon pattern return empty pcodes `[]`.
   - In `handle_parameter_count()`, requests containing inorganic carbon reply with the unsupported message and return immediately without calling `/results/count`.
4. **Observation Guard**: In `src/water_agent/handlers/observation.py`, unsupported resolution results reply with the guidance message and immediately terminate without calling upstream fetchers.

#### Before vs. After Behavior
- **Before:** An inquiry about "inorganic carbon" was resolved to TOC (00680) and DOC (00681).
- **After:** The agent recognizes inorganic carbon, DIC, and PIC as unsupported parameters, replies naming the supported organic carbon codes (00680 and 00681), and executes zero upstream requests.

---

## 3. Git Commit Summary

Branch: `feature/temporal-p0-fixes`

| Commit | Item | Description |
| :--- | :--- | :--- |
| `f7b1db8` | Item 1 | `fix(temporal): fail closed on invalid date strings in Samples fetcher and tools` |
| `605d584` | Item 2 | `fix(temporal): apply date window to station parameter counts` |
| `8edd88d` | Item 3 | `fix(temporal): continuous daily values date windowing, non-queryable filter stripping, and truncation tracking` |
| `988450b` | Item 4 | `fix(carbon): guard inorganic carbon, DIC, and PIC from resolving to organic carbon` |

---

## 4. Verification & Test Suite Results

### Full Offline Test Suite Execution
Command:
```powershell
python -m pytest --ignore=tests/smoke --ignore=tests/test_e2e_queries.py --ignore=tests/eval tests/
```

**Results:**
- **135 passed** in 41.55s (100% pass rate).
- Key test files executed:
  - `tests/test_samples_fetcher.py` (17 tests, including `to_iso_date`, `resolve_date_window`, fail-closed dates)
  - `tests/test_discrete_tools_and_agent.py` (21 tests, including count date windows, no-date baseline fidelity, lifetime labels)
  - `tests/test_daily_temporal.py` (3 tests, including 3-page truncation, OGC filter sanitization, sortby `"-time"`)
  - `tests/test_inorganic_carbon_guard.py` (5 tests, covering `resolve_discrete_parameter`, `extract_pcodes_and_family`, count handler, observation handler)
  - All existing regression and unit test suites (`tests/regression/`, `test_circuit_breaker.py`, `test_intent_architecture.py`, etc.)

### Skipped Suites (as mandated by requirements)
- `tests/smoke/`: Skipped (hits live USGS endpoints).
- `tests/test_e2e_queries.py`: Skipped (hits live USGS and LLM endpoints; references external test harness utilities).
- `tests/eval/`: Skipped (requires live network access and benchmark datasets).

---

## 5. Backlog & Future Work (TODOs for P1–P3)

The following capabilities were intentionally excluded from this P0 bug-fix PR to maintain minimal, risk-free correctness changes:

1. **TODO (P1 - Temporal NLP Parser)**: Implement a dedicated `water_agent.temporal` parser to interpret natural language date expressions, such as:
   - Month-year expressions: `"January 2020"` -> `2020-01-01` to `2020-01-31`.
   - Single months across years or specific months: `"during the month of march"`.
   - Seasonal phrases: `"during the winter"` -> meteorological winter (`YYYY-12-01` to `(YYYY+1)-02-28/29`).
2. **TODO (P2 - Relative Date Resolution)**: Support relative offsets (e.g., `"last 30 days"`, `"past year"`, `"yesterday"`) anchored to execution timestamps.
3. **TODO (P3 - Planner Prompt & Modality Routing)**: Enhance planner few-shot prompts to extract standard ISO date bounds during intent decomposition and optimize continuous vs. discrete routing for temporal range queries.
