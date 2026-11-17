"""Background warm-up and keep-warm utilities for USGS Water Data API."""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
import logging
import random
import time
from typing import Any

import httpx

from water_agent.config import settings, USGS_API_KEY

logger = logging.getLogger(__name__)

last_samples_target: ContextVar[tuple[str, tuple[str, ...]] | None] = ContextVar(
    "last_samples_target", default=None
)

_WARMING: dict[str, asyncio.Task] = {}


def _get_active_warmings() -> dict[str, asyncio.Task]:
    """Prune finished tasks and return active ones."""
    done = [s for s, t in _WARMING.items() if t.done()]
    for s in done:
        _WARMING.pop(s, None)
    return _WARMING


async def _run_warmup_ping(station_id: str, pcodes: tuple[str, ...]) -> None:
    """Send ONE /results/count request with random start date and 180s timeout."""
    start_time = time.monotonic()
    result = "error"
    year = random.randint(1990, 2020)
    month = random.randint(1, 12)
    day = random.randint(1, 28)
    start_date = f"{year}-{month:02d}-{day:02d}"

    url = f"{settings.samples_base_url.rstrip('/')}/results/count"
    params: dict[str, Any] = {
        "monitoringLocationIdentifier": station_id,
        "activityStartDateLower": start_date,
        "mimeType": "application/json",
    }
    if pcodes:
        params["usgsPCode"] = list(pcodes)

    headers = {"X-Api-Key": USGS_API_KEY} if USGS_API_KEY else {}

    try:
        async with httpx.AsyncClient(timeout=180.0, headers=headers) as client:
            resp = await client.get(url, params=params)
            if resp.status_code == 200:
                result = "ok"
            else:
                result = "error"
    except httpx.TimeoutException:
        result = "timeout"
    except Exception:
        result = "error"
    finally:
        elapsed_s = time.monotonic() - start_time
        pcodes_str = ",".join(pcodes) if pcodes else "none"
        logger.info(
            "warmup station=%s pcodes=%s elapsed_s=%.1f result=%s",
            station_id, pcodes_str, elapsed_s, result
        )
        _WARMING.pop(station_id, None)


def request_warmup(station_id: str, pcodes: tuple[str, ...] = ()) -> None:
    """Trigger a single-flight background warm-up for a station."""
    active = _get_active_warmings()

    if station_id in active and not active[station_id].done():
        logger.debug("warm-up already running for station %s", station_id)
        return

    if len(active) >= settings.max_warmups:
        logger.info(
            "warmup cap reached (running=%d, max=%d), skipping station=%s",
            len(active), settings.max_warmups, station_id
        )
        return

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    task = loop.create_task(_run_warmup_ping(station_id, pcodes))
    _WARMING[station_id] = task


def parse_target_entry(entry: str) -> tuple[str, tuple[str, ...]]:
    """Parse 'station:pcode1,pcode2' into (station, (pcode1, pcode2))."""
    if ":" in entry:
        station, pcodes_str = entry.split(":", 1)
        pcodes = tuple(sorted(p.strip() for p in pcodes_str.split(",") if p.strip()))
        return station.strip(), pcodes
    return entry.strip(), ()


_KEEPWARM_TASK: asyncio.Task | None = None


async def _keepwarm_loop() -> None:
    """Run keep-warm rounds immediately at startup and then every interval."""
    try:
        while True:
            for target_entry in settings.keepwarm_targets:
                station, pcodes = parse_target_entry(target_entry)
                if station:
                    request_warmup(station, pcodes)
            await asyncio.sleep(settings.keepwarm_interval_seconds)
    except asyncio.CancelledError:
        logger.debug("keep-warm loop cancelled")
        raise
    except Exception as exc:
        logger.warning("Error in keep-warm loop: %s", exc)


def start_keepwarm_loop() -> asyncio.Task | None:
    """Start the background keep-warm loop if enabled and not already running."""
    global _KEEPWARM_TASK
    if not settings.keepwarm_enabled:
        logger.debug("keep-warm is disabled; not starting loop")
        return None

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None

    if _KEEPWARM_TASK is not None and not _KEEPWARM_TASK.done():
        task_loop = getattr(_KEEPWARM_TASK, "get_loop", lambda: None)()
        if task_loop is loop:
            return _KEEPWARM_TASK
        stop_keepwarm_loop()

    _KEEPWARM_TASK = loop.create_task(_keepwarm_loop())
    return _KEEPWARM_TASK


def stop_keepwarm_loop() -> None:
    """Stop the background keep-warm loop cleanly."""
    global _KEEPWARM_TASK
    if _KEEPWARM_TASK is not None and not _KEEPWARM_TASK.done():
        try:
            _KEEPWARM_TASK.cancel()
        except RuntimeError:
            pass
    _KEEPWARM_TASK = None

