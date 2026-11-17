"""Periodic process-log heartbeats to keep iChatBio connections alive during slow USGS calls."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
import time
from typing import Any, AsyncIterator

from water_agent.config import settings

logger = logging.getLogger(__name__)
_monotonic = time.monotonic


def _format_elapsed(elapsed: float) -> str:
    if elapsed < 1.0:
        return f"{elapsed:.2f}"
    return str(int(elapsed))


def _format_heartbeat_message(elapsed: float, cap_seconds: float) -> str:
    n = _format_elapsed(elapsed)
    if elapsed < 60.0:
        return f"Waiting for USGS to respond ({n} s)…"
    cap = int(round(cap_seconds))
    return (
        f"Still waiting for USGS ({n} s). After a quiet period USGS can take a few minutes to "
        f"wake up; I'll keep waiting up to {cap} s."
    )


@asynccontextmanager
async def heartbeat(
    process: Any,
    interval: float | None = None,
) -> AsyncIterator[None]:
    """Send periodic ``process.log`` heartbeats while awaiting slow operations.

    iChatBio enforces a 60 s *idle* timeout that resets on every message sent by the
    agent (including ``process.log``). The first heartbeat fires only after ``interval``
    seconds so fast requests emit no extra logs.
    """
    eff_interval = settings.heartbeat_interval_seconds if interval is None else interval
    if process is None or eff_interval <= 0:
        yield
        return

    start = _monotonic()

    async def _beat() -> None:
        while True:
            await asyncio.sleep(eff_interval)
            elapsed = _monotonic() - start
            msg = _format_heartbeat_message(elapsed, settings.request_deadline_seconds)
            try:
                await process.log(msg)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("Heartbeat process.log failed; stopping heartbeat: %s", exc)
                return

    task = asyncio.create_task(_beat())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
