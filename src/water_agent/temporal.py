"""Deterministic temporal parsing and date resolution for the USGS Water Agent.

The LLM query planner extracts only structured user expressions into ``TemporalRequest``.
All actual calendar arithmetic, leap-year handling, relative windows, and validation happen
deterministically here using an injected ``today`` date.
"""
from __future__ import annotations

import calendar
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from water_agent.fetchers.samples_fetcher import DateParseError, resolve_date_window

logger = logging.getLogger(__name__)

MIN_YEAR = 1800

MONTH_NAMES: dict[str, int] = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

SEASON_MONTHS: dict[str, frozenset[int]] = {
    "winter": frozenset({12, 1, 2}),
    "spring": frozenset({3, 4, 5}),
    "summer": frozenset({6, 7, 8}),
    "fall": frozenset({9, 10, 11}),
    "autumn": frozenset({9, 10, 11}),
}

VALID_RELATIVES = frozenset({"this", "last", "past", "since"})
VALID_UNITS = frozenset({"year", "month", "season"})


class TemporalResolutionError(ValueError):
    """Raised when a TemporalRequest cannot be deterministically resolved or is invalid."""


class TemporalRequest(BaseModel):
    """Structured temporal fields extracted verbatim from user input by the query planner."""

    model_config = ConfigDict(extra="ignore")

    phrase: str | None = None        # user's own words, echoed back in replies
    start: str | None = None         # partial ISO: "2020", "2020-01", "2020-01-15"
    end: str | None = None           # partial ISO
    month: int | str | None = None   # 1-12 or month name/abbrev
    season: str | None = None        # winter | spring | summer | fall | autumn
    year: int | str | None = None    # with month or season; for winter = year of its January
    relative: str | None = None      # this | last | past | since
    unit: str | None = None          # year | month | season
    n: int | str | None = None       # for "past N <unit>"


@dataclass(frozen=True)
class TemporalSpec:
    """Resolved inclusive calendar bounds and optional month mask for handlers/fetchers."""

    start: date | None          # inclusive
    end: date | None            # inclusive
    months: frozenset[int]      # empty = all months
    label: str                  # e.g. "winter 2022 (2021-12-01 to 2022-02-28)"
    phrase: str | None = None

    @property
    def needs_month_filter(self) -> bool:
        """True if months is restricted and [start, end] is unbounded or spans outside months."""
        if not self.months:
            return False
        if self.start is None or self.end is None:
            return True
        span_months = (self.end.year - self.start.year) * 12 + (self.end.month - self.start.month) + 1
        if span_months > len(self.months):
            return True
        covered = {((self.start.month - 1 + offset) % 12) + 1 for offset in range(span_months)}
        return not covered.issubset(self.months)

    def to_samples_params(self) -> dict[str, str]:
        """Return Samples API date parameters."""
        params: dict[str, str] = {}
        if self.start is not None:
            params["activityStartDateLower"] = self.start.isoformat()
        if self.end is not None:
            params["activityStartDateUpper"] = self.end.isoformat()
        return params

    def to_ogc_datetime(self) -> str | None:
        """Return OGC API date interval ('A/B', 'A/..', '../B'), or None if unbounded."""
        if self.start is not None and self.end is not None:
            return f"{self.start.isoformat()}/{self.end.isoformat()}"
        if self.start is not None:
            return f"{self.start.isoformat()}/.."
        if self.end is not None:
            return f"../{self.end.isoformat()}"
        return None

    def to_metadata(self) -> dict[str, Any]:
        """Serialize resolved window for artifact payload and metadata."""
        return {
            "phrase": self.phrase,
            "start": self.start.isoformat() if self.start is not None else None,
            "end": self.end.isoformat() if self.end is not None else None,
            "months": sorted(self.months),
            "label": self.label,
        }

    def format_interpretation(self) -> str:
        """User-facing sentence stating how a temporal phrase or date filter was interpreted."""
        if self.phrase:
            return f"Interpreted '{self.phrase}' as {self.label}."
        return f"Interpreted date filter as {self.label}."


def _clean_opt_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise TemporalResolutionError("Boolean values are not valid temporal fields.")
    s = str(value).strip()
    return s if s else None


def _parse_int(value: int | str | None, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise TemporalResolutionError(f"Invalid {field_name}: '{value}'.")
    if isinstance(value, int):
        return value
    s = str(value).strip()
    if not s:
        return None
    if not re.fullmatch(r"-?\d+", s):
        raise TemporalResolutionError(f"Invalid {field_name}: '{value}'. Expected a whole number.")
    return int(s)


def _normalize_month(raw: int | str | None) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise TemporalResolutionError(f"Invalid month: '{raw}'.")
    if isinstance(raw, int):
        m = raw
    else:
        s = str(raw).strip()
        if not s:
            return None
        if re.fullmatch(r"-?\d+", s):
            m = int(s)
        else:
            m = MONTH_NAMES.get(s.lower())
            if m is None:
                raise TemporalResolutionError(
                    f"Unrecognized month '{raw}'. Use a month name (e.g. 'March') or number 1-12."
                )
    if not 1 <= m <= 12:
        raise TemporalResolutionError(f"Month {m} is out of range; expected 1-12.")
    return m


def _normalize_season(raw: str | None) -> str | None:
    s = _clean_opt_str(raw)
    if s is None:
        return None
    low = s.lower()
    if low not in SEASON_MONTHS:
        raise TemporalResolutionError(
            f"Unrecognized season '{raw}'. Supported seasons are winter, spring, summer, fall, or autumn."
        )
    return low


def _validate_year(
    year: int, today: date | None, max_year: int | None = None
) -> int:
    if year < MIN_YEAR:
        raise TemporalResolutionError(
            f"Year {year} is before minimum supported year {MIN_YEAR}."
        )
    upper = max_year if max_year is not None else (today.year if today is not None else None)
    if upper is not None and year > upper:
        raise TemporalResolutionError(
            f"Year {year} is out of valid range ({MIN_YEAR}-{upper})."
        )
    return year


def _parse_partial_iso(raw: str, is_end: bool, today: date | None) -> date:
    s = raw.strip()
    if re.fullmatch(r"\d{4}", s):
        y = _validate_year(int(s), today)
        return date(y, 12, 31) if is_end else date(y, 1, 1)

    if re.fullmatch(r"\d{4}-\d{1,2}", s):
        y_str, m_str = s.split("-")
        y = _validate_year(int(y_str), today)
        m = int(m_str)
        if not 1 <= m <= 12:
            raise TemporalResolutionError(f"Month in '{raw}' must be between 1 and 12.")
        day = calendar.monthrange(y, m)[1] if is_end else 1
        return date(y, m, day)

    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        try:
            d = date.fromisoformat(s)
        except ValueError:
            raise TemporalResolutionError(f"Invalid calendar date '{raw}'.") from None
        _validate_year(d.year, today)
        return d

    raise TemporalResolutionError(
        f"Unrecognized date format '{raw}'. Expected YYYY, YYYY-MM, or YYYY-MM-DD."
    )


def _season_bounds(season: str, year: int) -> tuple[date, date]:
    if season == "winter":
        start = date(year - 1, 12, 1)
        end = date(year, 2, calendar.monthrange(year, 2)[1])
    elif season == "spring":
        start = date(year, 3, 1)
        end = date(year, 5, 31)
    elif season == "summer":
        start = date(year, 6, 1)
        end = date(year, 8, 31)
    else:  # fall / autumn
        start = date(year, 9, 1)
        end = date(year, 11, 30)
    return start, end


def _subtract_years_clamped(d: date, n: int) -> date:
    target_year = d.year - n
    if target_year < MIN_YEAR:
        raise TemporalResolutionError(
            f"Computed start year {target_year} is before minimum supported year {MIN_YEAR}."
        )
    try:
        return d.replace(year=target_year)
    except ValueError:
        # Feb 29 clamps to Feb 28 on non-leap target years
        return date(target_year, d.month, 28)


def _subtract_months_clamped(d: date, n: int) -> date:
    total = d.year * 12 + (d.month - 1) - n
    target_year = total // 12
    target_month = (total % 12) + 1
    if target_year < MIN_YEAR:
        raise TemporalResolutionError(
            f"Computed start year {target_year} is before minimum supported year {MIN_YEAR}."
        )
    max_day = calendar.monthrange(target_year, target_month)[1]
    return date(target_year, target_month, min(d.day, max_day))


def _format_range_label(start: date | None, end: date | None) -> str:
    if start is not None and end is not None:
        return f"{start.isoformat()} to {end.isoformat()}"
    if start is not None:
        return f"{start.isoformat()} onward"
    if end is not None:
        return f"up to {end.isoformat()}"
    return ""


def _resolve_relative(
    relative: str,
    unit: str | None,
    n: int | None,
    month: int | None,
    season: str | None,
    year: int | None,
    phrase: str | None,
    today: date,
) -> TemporalSpec:
    if relative == "since":
        if month is not None or season is not None or unit is not None or n is not None:
            raise TemporalResolutionError(
                "'since' only accepts a year (e.g. 'since 2020')."
            )
        if year is None:
            raise TemporalResolutionError("Specify a starting year after 'since' (e.g. 'since 2020').")
        _validate_year(year, today)
        start = date(year, 1, 1)
        if start > today:
            raise TemporalResolutionError(f"Date '{start.isoformat()}' is in the future.")
        return TemporalSpec(
            start=start,
            end=None,
            months=frozenset(),
            label=f"{start.isoformat()} onward",
            phrase=phrase,
        )

    if relative == "past":
        if month is not None or season is not None:
            raise TemporalResolutionError(
                "Use 'last' for a named month or season (e.g. 'last March', 'last winter'), "
                "or specify 'past N years/months'."
            )
        if unit is None:
            raise TemporalResolutionError("Specify a unit with 'past' (e.g. 'past 5 years').")
        if n is None or n < 1:
            raise TemporalResolutionError("Specify a positive count >= 1 with 'past' (e.g. 'past 5 years').")
        if year is not None:
            raise TemporalResolutionError("Do not combine 'past N <unit>' with an explicit year.")

        if unit == "year":
            start = _subtract_years_clamped(today, n)
        elif unit == "month":
            start = _subtract_months_clamped(today, n)
        else:  # unit == "season"
            start = _subtract_months_clamped(today, 3 * n)

        desc = f"past {n} {unit}{'s' if n != 1 else ''}"
        return TemporalSpec(
            start=start,
            end=today,
            months=frozenset(),
            label=f"{desc} ({start.isoformat()} to {today.isoformat()})",
            phrase=phrase,
        )

    # relative in ("this", "last")
    if year is not None or n is not None:
        raise TemporalResolutionError(
            f"Do not combine '{relative}' with an explicit year or count."
        )

    if month is not None:
        m_name = calendar.month_name[month]
        if relative == "last":
            target_year = today.year if month < today.month else today.year - 1
            start = date(target_year, month, 1)
            end = date(target_year, month, calendar.monthrange(target_year, month)[1])
            desc = f"last {m_name}"
        else:  # "this"
            start = date(today.year, month, 1)
            if start > today:
                raise TemporalResolutionError(
                    f"{m_name} {today.year} hasn't happened yet."
                )
            end = date(today.year, month, calendar.monthrange(today.year, month)[1])
            desc = f"this {m_name}"

        return TemporalSpec(
            start=start,
            end=end,
            months=frozenset({month}),
            label=f"{desc} ({start.isoformat()} to {end.isoformat()})",
            phrase=phrase,
        )

    if season is not None:
        if relative == "last":
            for cand_year in (today.year + 1, today.year, today.year - 1):
                s_start, s_end = _season_bounds(season, cand_year)
                if s_end < today:
                    start, end = s_start, s_end
                    break
            else:  # pragma: no cover - mathematically unreachable for 3 consecutive years
                raise TemporalResolutionError(f"Could not resolve 'last {season}'.")
            desc = f"last {season}"
        else:  # "this"
            start = end = None
            for cand_year in (today.year + 1, today.year):
                s_start, s_end = _season_bounds(season, cand_year)
                if s_start <= today <= s_end:
                    start, end = s_start, s_end
                    break
            if start is None or end is None:
                raise TemporalResolutionError(
                    f"Today ({today.isoformat()}) is not in {season}; try 'last {season}'."
                )
            desc = f"this {season}"

        return TemporalSpec(
            start=start,
            end=end,
            months=SEASON_MONTHS[season],
            label=f"{desc} ({start.isoformat()} to {end.isoformat()})",
            phrase=phrase,
        )

    # relative ("this" | "last") with unit ("year" | "month" | "season")
    if unit is None:
        raise TemporalResolutionError(
            f"Incomplete relative time expression '{relative}'; specify year, month, or season."
        )

    if unit == "year":
        if relative == "this":
            start, end = date(today.year, 1, 1), today
        else:
            prev_y = today.year - 1
            start, end = date(prev_y, 1, 1), date(prev_y, 12, 31)
        return TemporalSpec(
            start=start,
            end=end,
            months=frozenset(),
            label=f"{relative} year ({start.isoformat()} to {end.isoformat()})",
            phrase=phrase,
        )

    if unit == "month":
        if relative == "this":
            start, end = date(today.year, today.month, 1), today
            m = today.month
        else:
            if today.month == 1:
                y, m = today.year - 1, 12
            else:
                y, m = today.year, today.month - 1
            start = date(y, m, 1)
            end = date(y, m, calendar.monthrange(y, m)[1])
        return TemporalSpec(
            start=start,
            end=end,
            months=frozenset({m}),
            label=f"{relative} month ({start.isoformat()} to {end.isoformat()})",
            phrase=phrase,
        )

    # unit == "season"
    cur_season = next(
        s_name
        for s_name, s_months in SEASON_MONTHS.items()
        if s_name != "autumn" and today.month in s_months
    )
    if relative == "this":
        return _resolve_relative(
            "this", None, None, None, cur_season, None, phrase, today
        )
    season_order = ["winter", "spring", "summer", "fall"]
    prev_season = season_order[(season_order.index(cur_season) - 1) % 4]
    return _resolve_relative(
        "last", None, None, None, prev_season, None, phrase, today
    )


def has_temporal_fields(req: TemporalRequest) -> bool:
    """Return True if any field on TemporalRequest is non-empty."""
    return any(
        val is not None and (not isinstance(val, str) or val.strip() != "")
        for val in (
            req.phrase,
            req.start,
            req.end,
            req.month,
            req.season,
            req.year,
            req.relative,
            req.unit,
            req.n,
        )
    )


def _normalize_and_validate_parts(
    req: TemporalRequest,
) -> tuple[str | None, str | None, str | None, int | None, str | None, int | None, str | None, str | None, int | None]:
    """Validate types, ranges, combinations, and partial ISO syntax independent of `today`."""
    phrase = _clean_opt_str(req.phrase)
    raw_start = _clean_opt_str(req.start)
    raw_end = _clean_opt_str(req.end)
    month = _normalize_month(req.month)
    season = _normalize_season(req.season)
    year = _parse_int(req.year, "year")
    if year is not None and year < MIN_YEAR:
        raise TemporalResolutionError(
            f"Year {year} is before minimum supported year {MIN_YEAR}."
        )
    raw_rel = _clean_opt_str(req.relative)
    relative = raw_rel.lower() if raw_rel else None
    raw_unit = _clean_opt_str(req.unit)
    unit = raw_unit.lower().rstrip("s") if raw_unit else None
    n = _parse_int(req.n, "n")

    if relative is not None and relative not in VALID_RELATIVES:
        raise TemporalResolutionError(
            f"Unrecognized relative time specifier '{raw_rel}'."
        )
    if unit is not None and unit not in VALID_UNITS:
        raise TemporalResolutionError(
            f"Unrecognized time unit '{raw_unit}'. Supported units are year, month, and season."
        )
    if n is not None and n < 1:
        raise TemporalResolutionError(f"Count 'n' must be at least 1 (got {n}).")

    if month is not None and season is not None:
        raise TemporalResolutionError("Cannot combine both a month and a season.")

    if relative is not None and (raw_start is not None or raw_end is not None):
        raise TemporalResolutionError(
            "Cannot combine a relative time expression with explicit start or end dates."
        )

    if (raw_start is not None or raw_end is not None) and (
        month is not None or season is not None or year is not None or unit is not None or n is not None
    ):
        raise TemporalResolutionError(
            "Cannot combine explicit start/end bounds with month, season, year, or unit fields."
        )

    if raw_start is not None:
        _parse_partial_iso(raw_start, is_end=False, today=None)
    if raw_end is not None:
        _parse_partial_iso(raw_end, is_end=True, today=None)

    if relative is None and (unit is not None or n is not None):
        raise TemporalResolutionError(
            "A relative qualifier ('this', 'last', or 'past N') is required with unit/count."
        )

    return phrase, raw_start, raw_end, month, season, year, relative, unit, n


def validate_temporal_syntax(req: TemporalRequest) -> None:
    """Validate structure and static values of a TemporalRequest without needing `today`."""
    if not has_temporal_fields(req):
        return
    _normalize_and_validate_parts(req)


def resolve_temporal(req: TemporalRequest, today: date) -> TemporalSpec:
    """Resolve a structured TemporalRequest into concrete dates using injected `today`."""
    (
        phrase,
        raw_start,
        raw_end,
        month,
        season,
        year,
        relative,
        unit,
        n,
    ) = _normalize_and_validate_parts(req)

    # Branch 1: Explicit range bounds
    if raw_start is not None or raw_end is not None:
        start = _parse_partial_iso(raw_start, is_end=False, today=today) if raw_start else None
        end = _parse_partial_iso(raw_end, is_end=True, today=today) if raw_end else None

        if start is not None and start > today:
            raise TemporalResolutionError(
                f"Start date {start.isoformat()} is rejected because that date range is in the future."
            )
        if start is not None and end is not None and start > end:
            raise TemporalResolutionError(
                f"Start date ({start.isoformat()}) cannot be after end date ({end.isoformat()})."
            )
        return TemporalSpec(
            start=start,
            end=end,
            months=frozenset(),
            label=_format_range_label(start, end),
            phrase=phrase,
        )

    # Branch 2: Relative expressions
    if relative is not None:
        return _resolve_relative(relative, unit, n, month, season, year, phrase, today)

    # Branch 3: Named month (with or without year)
    if month is not None:
        m_name = calendar.month_name[month]
        if year is not None:
            _validate_year(year, today)
            start = date(year, month, 1)
            if start > today:
                raise TemporalResolutionError(
                    f"{m_name} {year} is rejected because that date range is in the future."
                )
            end = date(year, month, calendar.monthrange(year, month)[1])
            return TemporalSpec(
                start=start,
                end=end,
                months=frozenset({month}),
                label=f"{m_name} {year} ({start.isoformat()} to {end.isoformat()})",
                phrase=phrase,
            )
        return TemporalSpec(
            start=None,
            end=None,
            months=frozenset({month}),
            label=m_name,
            phrase=phrase,
        )

    # Branch 4: Named season (with or without year)
    if season is not None:
        if year is not None:
            max_year = today.year + 1 if season == "winter" else today.year
            _validate_year(year, today, max_year=max_year)
            start, end = _season_bounds(season, year)
            if start > today:
                raise TemporalResolutionError(
                    f"{season} {year} is rejected because that date range is in the future."
                )
            return TemporalSpec(
                start=start,
                end=end,
                months=SEASON_MONTHS[season],
                label=f"{season} {year} ({start.isoformat()} to {end.isoformat()})",
                phrase=phrase,
            )
        return TemporalSpec(
            start=None,
            end=None,
            months=SEASON_MONTHS[season],
            label=season,
            phrase=phrase,
        )

    # Branch 5: Year only (equivalent to "in YYYY")
    if year is not None:
        _validate_year(year, today)
        start = date(year, 1, 1)
        end = date(year, 12, 31)
        return TemporalSpec(
            start=start,
            end=end,
            months=frozenset(),
            label=f"{start.isoformat()} to {end.isoformat()}",
            phrase=phrase,
        )

    if phrase is not None:
        raise TemporalResolutionError(
            f"Could not resolve a date range from '{phrase}'."
        )
    raise TemporalResolutionError("Empty temporal request.")


def current_date() -> date:
    """Return the current UTC calendar date. Handlers invoke this once per request."""
    return datetime.now(timezone.utc).date()


def resolve_plan_window(plan: Any, today: date | None = None) -> TemporalSpec | None:
    """Resolve the temporal window from `plan.temporal` or fall back to legacy date filters.

    - If `plan.temporal_error` is set, raises TemporalResolutionError asking for a clearer date.
    - If `plan.temporal` has any field set, resolves it with `today` (temporal wins over
      conflicting legacy filters, logging at INFO).
    - Otherwise falls back to legacy filters (`start_date` / `activityStartDate*`) via
      `resolve_date_window`. Returns None when neither temporal nor legacy dates exist.
    """
    if today is None:
        today = current_date()

    temporal_error = getattr(plan, "temporal_error", None)
    clean_error_msg = (
        "I couldn't understand the date period in your question. "
        "Try e.g. '2020-01-01 to 2023-07-31', 'March 2021', or 'winter 2022'."
    )
    if isinstance(temporal_error, str) and temporal_error.strip():
        logger.info("Invalid temporal specification on QueryPlan: %s", temporal_error)
        raise TemporalResolutionError(clean_error_msg)

    raw_filters = getattr(plan, "filters", None)
    filters = raw_filters if isinstance(raw_filters, dict) else {}
    req = getattr(plan, "temporal", None)
    if isinstance(req, dict):
        try:
            req = TemporalRequest.model_validate(req)
        except (ValidationError, TypeError, ValueError) as exc:
            logger.info("Invalid temporal dict on QueryPlan: %s", exc)
            raise TemporalResolutionError(clean_error_msg) from None
    elif not isinstance(req, TemporalRequest):
        req = None

    if req is not None and has_temporal_fields(req):
        spec = resolve_temporal(req, today=today)
        spec_start_iso = spec.start.isoformat() if spec.start is not None else None
        spec_end_iso = spec.end.isoformat() if spec.end is not None else None

        try:
            leg_start, leg_end = resolve_date_window(filters)
        except DateParseError:
            logger.info(
                "Both plan.temporal (%s) and legacy date filter are present and disagree; using plan.temporal.",
                spec.label,
            )
        else:
            if (leg_start is not None or leg_end is not None) and (
                leg_start != spec_start_iso or leg_end != spec_end_iso
            ):
                logger.info(
                    "Both plan.temporal (%s) and legacy filter dates (%s to %s) are present and disagree; using plan.temporal.",
                    spec.label,
                    leg_start,
                    leg_end,
                )
        return spec

    leg_start, leg_end = resolve_date_window(filters)
    if leg_start is None and leg_end is None:
        return None

    start_d = date.fromisoformat(leg_start) if leg_start else None
    end_d = date.fromisoformat(leg_end) if leg_end else None
    return TemporalSpec(
        start=start_d,
        end=end_d,
        months=frozenset(),
        label=_format_range_label(start_d, end_d),
        phrase=None,
    )
