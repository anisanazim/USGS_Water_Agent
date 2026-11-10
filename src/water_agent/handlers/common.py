from __future__ import annotations

from typing import TYPE_CHECKING, Any

from water_agent import temporal
from water_agent.fetchers.samples_fetcher import DateParseError

if TYPE_CHECKING:
    from ichatbio.agent_response import ResponseContext
    from water_agent.query_planner import QueryPlan


def format_date_for_display(val: str | None) -> str:
    """Format an ISO date/timestamp string to YYYY-MM-DD for clean user display."""
    if not val or val == "Unknown":
        return val or ""
    val_str = str(val).strip()
    if "T" in val_str:
        return val_str.split("T")[0]
    if " " in val_str:
        return val_str.split(" ")[0]
    return val_str


def format_window_error(exc: Exception) -> str:
    """Format a DateParseError or TemporalResolutionError as a clear user-facing reply."""
    if isinstance(exc, DateParseError):
        return (
            f"I could not understand the date filter: '{exc.value}'. "
            "Dates must be specified in YYYY-MM-DD format (for example, '2023-01-15')."
        )
    return str(exc)


async def resolve_handler_temporal_window(
    context: ResponseContext,
    process: Any,
    plan: QueryPlan,
) -> tuple[temporal.TemporalSpec | None, bool]:
    """Resolve `plan`'s temporal window once per request using `temporal.current_date()`.

    Returns `(spec, True)` when valid (or None when no temporal constraint applies).
    Replies to the user and returns `(None, False)` on invalid windows or unsupported
    multi-year month/season filters (P2).
    """
    today = temporal.current_date()
    try:
        spec = temporal.resolve_plan_window(plan, today=today)
    except (temporal.TemporalResolutionError, DateParseError) as exc:
        await process.log(f"Invalid date filter: {exc}")
        await context.reply(format_window_error(exc), data=plan.model_dump())
        return None, False

    if spec is not None and spec.needs_month_filter:
        msg = (
            f"Filtering by {spec.label} across multiple years isn't supported yet. "
            "Add a year (e.g. 'March 2021', 'winter 2022') or a date range."
        )
        await process.log(msg)
        await context.reply(msg, data=plan.model_dump())
        return None, False

    return spec, True
