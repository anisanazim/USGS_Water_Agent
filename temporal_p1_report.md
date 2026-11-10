# Temporal Handling P1 Implementation Report

**Branch:** `feature/temporal-p1`  
**Base Commit:** `7173563` (`feature/temporal-p0-fixes`)  
**Status:** Completed & Verified (`120` baseline `3774f0d` -> `148` P0 `7173563` -> `218` initial P1 -> `223` after pre-merge follow-ups; all `223` offline unit/regression tests passing locally)

---

## 1. Executive Summary & Design Rule

P1 introduces structured temporal understanding across the USGS Water Agent so users can ask bounded, seasonal, and relative date queries (e.g., `"between January 2020 and July 2023"`, `"winter 2022"`, `"March 2021"`, `"last winter"`, `"past 5 years"`, `"since 2020"`).

### Core Design Rule
- **LLM Role (Extraction Only):** The query planner extracts only the user's words into structured fields on top-level `QueryPlan.temporal` (`TemporalRequest`). The LLM never computes calendar dates and never writes dates into `filters`.
- **Deterministic Code Role (All Date Arithmetic):** Pure deterministic logic in `src/water_agent/temporal.py` validates inputs, resolves calendar bounds (including leap years and relative offsets against an injected `today` from `current_date()`), and serializes API parameters for Samples and Daily OGC endpoints.

### Out of Scope (Deferred to P2 / P3)
- Cross-year month/season filtering (`spec.needs_month_filter == True`, such as `"March"` with no year or `"during the winter"`): fails closed in P1 with a clear message and zero upstream fetches. Deferred to **P2**.
- Month/season count aggregations with per-pcode breakdown, distinct-sample-date counts, and excluding parameter code `90400`: deferred to **P2**.
- Water-year phrases (`WY2023`), discrete-vs-continuous modality routing changes, and sub-day timestamps in OGC `datetime`: deferred to **P3**.

---

## 2. Implementation Details by Item

### Item 1: Deterministic Temporal Module (`src/water_agent/temporal.py`)
*Commits: `fd58a32`, `8e65a93`*

Implemented pure, side-effect-free models and resolver in [`src/water_agent/temporal.py`](src/water_agent/temporal.py):

1. **`TemporalRequest(BaseModel)`** (`extra="ignore"`):
   - Loose types (`phrase`, `start`, `end`, `month: int | str | None`, `season`, `year: int | str | None`, `relative`, `unit`, `n: int | str | None`) so LLM output shapes never break `QueryPlan` instantiation before validation.
2. **`TemporalSpec` (frozen dataclass)**:
   - Fields: `start: date | None`, `end: date | None`, `months: frozenset[int]`, `label: str`, `phrase: str | None`.
   - `needs_month_filter` property: Returns `True` if `months` is non-empty AND (`start is None` or `end is None` or `[start, end]` covers any month outside `months`).
   - `to_samples_params()`: Produces `activityStartDateLower` / `activityStartDateUpper` ISO strings.
   - `to_ogc_datetime()`: Produces `"A/B"`, `"A/.."`, `"../B"`, or `None`.
   - `to_metadata()` & `format_interpretation()`: Shared serialization (`{"phrase", "start", "end", "months", "label"}`) and consistent reply phrasing (`"Interpreted '<phrase>' as <label>."`).
3. **`resolve_temporal(req: TemporalRequest, today: date) -> TemporalSpec`**:
   Raises `TemporalResolutionError(ValueError)` with user-facing guidance on invalid/ambiguous/future inputs. For `season="winter"` with explicit `year`, permits `year == today.year + 1` (since winter `YYYY-1..YYYY` begins Dec 1 of `today.year`) while keeping the `start > today` future check so upcoming Dec winters are still rejected before Dec 1.

#### Decision Table

| Input Category | Rule / Resolution | Output (`start`, `end`, `months`, `needs_month_filter`) |
| :--- | :--- | :--- |
| **Explicit ranges** (`start`, `end`) | `YYYY` -> Jan 1 / Dec 31; `YYYY-MM` -> 1st / `monthrange` (leap-year accurate); `YYYY-MM-DD` -> exact day; `"in 2021"` -> `start="2021", end="2021"`; `"since 2020"` -> `start="2020", end=None` | Exact bounds (or open bound), `months=∅`, `False` |
| **Month + year** | Exact calendar month (`1`..last day) | Full month bounds, `months={m}`, `False` |
| **Season + year** | `spring` `3–5`, `summer` `6–8`, `fall`/`autumn` `9–11`; `winter` = `Dec (year-1)` .. `end of Feb (year)` (`winter 2022` -> `2021-12-01..2022-02-28`, `winter 2022-23` -> `year=2023`, leap `winter 2024` -> `2024-02-29`; permits `year == today.year + 1` if `start <= today`, e.g., `today=2026-12-15` permits `winter 2027` -> `2026-12-01..2027-02-28`) | Full season bounds, `months=season`, `False` |
| **Month / season without year** | No year invented | `start=None, end=None`, `months={m}` or season months, **`True`** |
| **`this year` / `last year`** | `this year` -> Jan 1 of `today.year` .. `today`; `last year` -> prior full calendar year | Calendar bounds, `months=∅`, `False` |
| **`this month` / `last month`** | `this month` -> 1st of current month .. `today`; `last month` -> prior full calendar month (Jan rolls back to Dec `year-1`) | Month bounds, `months={m}`, `False` |
| **`past N years/months`** | Rolling `(today - N)` .. `today`; Feb 29 clamps to Feb 28 on non-leap target years; month-end clamps cleanly | Rolling bounds, `months=∅`, `False` |
| **`last <month>`** | Most recent completed instance before `today.month` (`2026-11-10`: `"last March"` -> `2026-03`; `2026-03-15`: `"last March"` -> `2025-03`) | Full month bounds, `months={m}`, `False` |
| **`this <month>`** | That month in `today.year`; errors (`"hasn't happened yet"`) if `start > today` | Full month bounds, `months={m}`, `False` |
| **`last <season>`** | Most recent completed instance with `end < today` (`2026-11-10`: `last winter` -> `2025-12-01..2026-02-28`, `last summer` -> `2026-06-01..2026-08-31`, `last fall` -> `2025-09-01..2025-11-30`) | Full season bounds, `months=season`, `False` |
| **`this <season>`** | Season instance containing `today`; errors suggesting `"last <season>"` if `today` is outside | Full season bounds, `months=season`, `False` |
| **`since YYYY`** | `YYYY-01-01` to open end (`end=None`) | `start=YYYY-01-01, end=None`, `months=∅`, `False` |
| **Validation** | `month ∉ 1..12`, unknown season, `year < 1800` or `> today.year` (or `> today.year + 1` for winter), `n < 1`, `month+season`, `relative+start/end`, `start > end`, `start > today` | Raises `TemporalResolutionError` (`resolve_plan_window` returns `None` for empty `{}`) |

---

### Item 2: QueryPlan + Planner Contract (`src/water_agent/query_planner.py`)
*Commit: `18d399b`*

1. **`QueryPlan` Fields & Resilient Validation**:
   - Added `temporal: TemporalRequest | None = None` and `temporal_error: str | None = None` at top level (not inside `filters`).
   - Added `_sanitize_temporal` invoked via `normalize_intent_and_modality` (`@model_validator(mode="before")`) and `validate_and_normalize_temporal` (`@field_validator("temporal", mode="before")`), backed by shared `validate_temporal_syntax()` in `temporal.py`. Invalid temporal objects (e.g., `month="13"`, `season="monsoon"`, wrong types such as `{"month": [3]}`) degrade cleanly to `temporal=None` + populated `temporal_error` without breaking `_parse_plan`.
2. **`SYSTEM_PROMPT`**:
   - Added Section `7. Dates and time periods`: instructs the LLM to extract user time words into top-level `temporal` verbatim, never perform date arithmetic, never emit date filters into `filters`, omit `temporal` for `"latest"`/`"recent"`, and clarify on vague phrases (`"a while ago"`, `"back then"`).
   - Updated JSON schema line with `"temporal": {...} or omit`.
   - Included all 12 canonical extraction examples.
3. **Network Eval Cases**:
   - Added `TEMP-01` through `TEMP-12` to [`tests/eval/datasets/failure_modes.json`](tests/eval/datasets/failure_modes.json) and `test_planner_temporal_cases` in [`tests/eval/test_planner.py`](tests/eval/test_planner.py) (excluded from offline pytest runs).

---

### Item 3: Shared Window Resolution Helper (`src/water_agent/temporal.py`)
*Commits: `d717de2`, `8e65a93`*

1. **`current_date() -> date`**: Module-level UTC clock helper called once per request by handlers (cleanly monkeypatchable in tests).
2. **`resolve_plan_window(plan, today=None) -> TemporalSpec | None`**:
   - If `plan.temporal_error` is set (or an unparseable `dict` triggers Pydantic `ValidationError`), logs the full raw error at `INFO` level and raises `TemporalResolutionError` with a clean user-facing message only (`"I couldn't understand the date period in your question. Try e.g. '2020-01-01 to 2023-07-31', 'March 2021', or 'winter 2022'."`) so internal Pydantic details never leak into user replies. Clean user-facing `TemporalResolutionError` messages from `resolve_temporal` itself pass through unchanged.
   - Resolves `plan.temporal` via `resolve_temporal` if any field is populated. If conflicting legacy date filters (`start_date`/`end_date`/`activityStartDate*`) are also present, `temporal` wins and logs at `INFO`.
   - Falls back to legacy date filters via P0 `resolve_date_window(plan.filters)` wrapped in a `TemporalSpec` (`months=∅`, label `"<start> to <end>"`).
   - Returns `None` if neither `temporal` nor legacy dates exist.

---

### Item 4: Handler Wiring (`observation.py`, `count.py`, `spatial.py`, `inventory.py`, `common.py`)
*Commits: `f38b0c7`, `8e65a93`*

1. **Shared Helper (`resolve_handler_temporal_window` in [`src/water_agent/handlers/common.py`](src/water_agent/handlers/common.py))**:
   - Invokes `temporal.current_date()` and `temporal.resolve_plan_window(plan, today=today)` once per request.
   - Catches `(TemporalResolutionError, DateParseError)`, logs, replies cleanly, and returns without fetching.
   - Rejects `spec.needs_month_filter` with `"Filtering by <label> across multiple years isn't supported yet. Add a year (e.g. 'March 2021', 'winter 2022') or a date range."` and returns with zero fetches.
2. **Observation & Count Handler Integration ([`observation.py`](src/water_agent/handlers/observation.py) & [`count.py`](src/water_agent/handlers/count.py))**:
   - Replaced all P0 `resolve_date_window` calls in handlers. No handler computes dates or calls `date.today()`.
   - Discrete Samples paths apply `spec.to_samples_params()`.
   - Daily OGC path applies `spec.to_ogc_datetime()`, preserving P0 precedence where explicit validated `plan.filters["datetime"]` still wins.
   - Replies state `"Interpreted '<phrase>' as <label>."` (replacing P0 `window_phrase` in `count.py`) when a window is applied; no-date responses remain unchanged.
   - Added `"temporal": {"phrase", "start", "end", "months", "label"}` alongside `"date_lower"` and `"date_upper"` in artifact payloads and metadata.
   - Guarded `resolve_plan_window` against auto-created `MagicMock` attributes in legacy unit tests (`isinstance` checks on `temporal_error` and `temporal`).
3. **Spatial & Inventory Non-Fatal Notes ([`spatial.py`](src/water_agent/handlers/spatial.py), [`inventory.py`](src/water_agent/handlers/inventory.py), [`common.py`](src/water_agent/handlers/common.py))**:
   - Added `append_temporal_not_applied_note()` in `common.py` and wired it into all reply paths of `handle_spatial` and `handle_inventory`.
   - Whenever `plan.temporal` (any field) or `plan.temporal_error` is present, the request still succeeds normally and appends: `"Note: the date period in your question isn't applied to station discovery/inventory."`

---

## 3. Before / After Examples (Original Three Queries)

| Query | Before (P0 `7173563`) | After (P1 `8e65a93`) |
| :--- | :--- | :--- |
| **a.** *"Retrieve records of dissolved oxygen available between January 2020 and July 2023 at xxxx"* | Raw month-year strings failed closed in P0 (`DateParseError`: expected `YYYY-MM-DD`). | Planner emits `temporal={"phrase": "between January 2020 and July 2023", "start": "2020-01", "end": "2023-07"}` -> resolved to `2020-01-01`..`2023-07-31`; Samples API queried with `activityStartDateLower=2020-01-01`, `activityStartDateUpper=2023-07-31`; reply states `"Interpreted 'between January 2020 and July 2023' as 2020-01-01 to 2023-07-31."` |
| **b.** *"Summarize how many records of pH are available during the month of march at xxxx"* | Returned lifetime pH count at the station, ignoring March. | Planner emits `temporal={"phrase": "during the month of March", "month": 3}` -> `spec.needs_month_filter == True`; handler fails closed with zero fetches: `"Filtering by March across multiple years isn't supported yet. Add a year (e.g. 'March 2021', 'winter 2022') or a date range."` |
| **c.** *"Summarize how many records of carbon are available during the winter at xxxx"* | Returned lifetime organic-carbon count at the station, ignoring winter. | Planner emits `temporal={"phrase": "during the winter", "season": "winter"}` -> `spec.needs_month_filter == True`; handler fails closed with zero fetches: `"Filtering by winter across multiple years isn't supported yet. Add a year (e.g. 'March 2021', 'winter 2022') or a date range."` *(Bounded `"winter 2022"` queries `2021-12-01`..`2022-02-28`.)* |

---

## 4. Verification & Test Suite Results

### Offline Test Progression
- **Baseline (`3774f0d`):** `120` offline tests.
- **After P0 + follow-ups (`7173563`):** `148` offline tests (`+28`).
- **After Initial P1 (`3846c22`):** `218` offline tests (`+70`).
- **After P1 Pre-Merge Follow-ups (`8e65a93`):** **`223` passed in 30.75s** (`+5` additional offline tests: in-progress Dec winter `2027` resolution vs Nov future rejection, Pydantic `ValidationError` non-leakage on `{"month": [3]}`, and spatial/inventory temporal disclaimer tests).

Command run after final code edit:
```powershell
python -m pytest --ignore=tests/smoke --ignore=tests/test_e2e_queries.py --ignore=tests/eval tests/ -v
```

### Skipped Suites & Eval / Baseline Notes
- **Skipped (live USGS / LLM):** `tests/smoke/`, `tests/test_e2e_queries.py`, `tests/eval/`.
- **Network/LLM eval cases not executed offline:** The 12 new planner temporal cases (`TEMP-01` through `TEMP-12` in `tests/eval/datasets/failure_modes.json` and `tests/eval/test_planner.py`) were added following the existing eval dataset schema; they require live LLM access (`--ignore=tests/eval` excluded them during offline verification).
- **Known baseline note:** `test_step_3b_outside_verified_families_mercury` passes locally with generated reference data (`characteristics_filtered.csv`) and fails in clean checkouts without that untracked file (pre-existing baseline issue on `3774f0d`).

---

## 5. Git Commit Log (`feature/temporal-p1`)

| Commit | Item | Message |
| :--- | :--- | :--- |
| `fd58a32` | Item 1 | `feat(temporal): add deterministic temporal resolver, models, and unit tests` |
| `18d399b` | Item 2 | `feat(planner): add top-level temporal contract, resilient validation, and prompt examples` |
| `d717de2` | Item 3 | `feat(temporal): add resolve_plan_window shared helper and current_date clock` |
| `f38b0c7` | Item 4 | `feat(temporal): wire resolve_plan_window into observation and count handlers` |
| `3846c22` | Docs | `docs: add temporal handling P1 implementation report` |
| `8e65a93` | Follow-ups 1–3 | `fix(temporal): P1 pre-merge follow-ups for error sanitization, in-progress winter year, and spatial/inventory notes` |

---

## 6. Next Steps (P2 / P3 Backlog)

- **P2:** Implement cross-year month/season filtering when `spec.needs_month_filter` is `True`, month/season count aggregations with per-pcode breakdown, distinct sample-date counts, and excluding pcode `90400`.
- **P3:** Water-year phrases (`WY2023`), discrete-vs-continuous modality routing refinements, and timestamp precision in OGC `datetime`.
