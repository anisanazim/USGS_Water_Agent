from __future__ import annotations
from typing import Any


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
