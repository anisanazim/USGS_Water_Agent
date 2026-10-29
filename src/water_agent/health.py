from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

from water_agent.metadata.discrete_families import DISCRETE_FAMILIES

logger = logging.getLogger(__name__)


class HealthStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


class ComponentCheck(BaseModel):
    name: str
    status: HealthStatus
    latency_ms: Optional[float] = None
    message: Optional[str] = None
    details: dict[str, Any] = Field(default_factory=dict)


class HealthReport(BaseModel):
    status: HealthStatus
    timestamp: str
    version: str = "1.0.0"
    checks: dict[str, ComponentCheck]


class HealthChecker:
    """Diagnostic health and readiness probe for the USGS Water Data Agent."""

    def __init__(
        self,
        samples_fetcher: Any = None,
        water_data_fetcher: Any = None,
        probe_timeout_seconds: float = 5.0,
    ):
        self.samples_fetcher = samples_fetcher
        self.water_data_fetcher = water_data_fetcher
        self.probe_timeout_seconds = probe_timeout_seconds

    async def check_catalog(self) -> ComponentCheck:
        try:
            family_count = len(DISCRETE_FAMILIES)
            if family_count == 0:
                return ComponentCheck(
                    name="metadata_catalog",
                    status=HealthStatus.UNHEALTHY,
                    message="Metadata catalog is empty",
                )
            return ComponentCheck(
                name="metadata_catalog",
                status=HealthStatus.HEALTHY,
                message=f"Catalog loaded with {family_count} parameter families",
                details={"family_count": family_count},
            )
        except Exception as exc:
            return ComponentCheck(
                name="metadata_catalog",
                status=HealthStatus.UNHEALTHY,
                message=f"Failed to load metadata catalog: {exc}",
            )

    async def check_ogc_api(self) -> ComponentCheck:
        if self.water_data_fetcher is None:
            return ComponentCheck(
                name="ogc_api",
                status=HealthStatus.UNHEALTHY,
                message="water_data_fetcher is not configured",
            )
        cb = getattr(self.water_data_fetcher, "circuit_breaker", None)
        if cb and getattr(cb, "state", None) and getattr(cb.state, "value", None) == "open":
            return ComponentCheck(
                name="ogc_api",
                status=HealthStatus.UNHEALTHY,
                message="USGS OGC API circuit breaker is OPEN (failing fast)",
                details=cb.status_details(),
            )

        client = getattr(self.water_data_fetcher, "_client", None)
        base_url = getattr(self.water_data_fetcher, "base_url", "https://api.waterdata.usgs.gov/ogcapi/v0")
        url = f"{base_url}/collections?f=json"

        if client is None or not hasattr(client, "get"):
            return ComponentCheck(
                name="ogc_api",
                status=HealthStatus.HEALTHY,
                message="water_data_fetcher is mocked",
            )

        start = time.perf_counter()
        try:
            resp = await client.get(url, timeout=self.probe_timeout_seconds)
            elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
            status_code = getattr(resp, "status_code", 200)
            if status_code == 200:
                return ComponentCheck(
                    name="ogc_api",
                    status=HealthStatus.HEALTHY,
                    latency_ms=elapsed_ms,
                    message="USGS OGC API reachable and responsive",
                    details={"status_code": status_code, "url": url},
                )
            return ComponentCheck(
                name="ogc_api",
                status=HealthStatus.DEGRADED,
                latency_ms=elapsed_ms,
                message=f"USGS OGC API returned HTTP {status_code}",
                details={"status_code": status_code, "url": url},
            )
        except Exception as exc:
            elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
            return ComponentCheck(
                name="ogc_api",
                status=HealthStatus.UNHEALTHY,
                latency_ms=elapsed_ms,
                message=f"USGS OGC API check failed: {exc}",
                details={"error": str(exc), "url": url},
            )

    async def check_samples_api(self) -> ComponentCheck:
        if self.samples_fetcher is None:
            return ComponentCheck(
                name="samples_api",
                status=HealthStatus.UNHEALTHY,
                message="samples_fetcher is not configured",
            )
        cb = getattr(self.samples_fetcher, "circuit_breaker", None)
        if cb and getattr(cb, "state", None) and getattr(cb.state, "value", None) == "open":
            return ComponentCheck(
                name="samples_api",
                status=HealthStatus.UNHEALTHY,
                message="USGS Samples API circuit breaker is OPEN (failing fast)",
                details=cb.status_details(),
            )

        client = getattr(self.samples_fetcher, "_client", None)
        base_url = getattr(self.samples_fetcher, "base_url", "https://api.waterdata.usgs.gov/samples-data")
        url = f"{base_url}/codes/sample-media?f=json"

        if client is None or not hasattr(client, "get"):
            return ComponentCheck(
                name="samples_api",
                status=HealthStatus.HEALTHY,
                message="samples_fetcher is mocked",
            )

        start = time.perf_counter()
        try:
            resp = await client.get(url, timeout=self.probe_timeout_seconds)
            elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
            status_code = getattr(resp, "status_code", 200)
            if status_code == 200:
                return ComponentCheck(
                    name="samples_api",
                    status=HealthStatus.HEALTHY,
                    latency_ms=elapsed_ms,
                    message="USGS Samples API reachable and responsive",
                    details={"status_code": status_code, "url": url},
                )
            return ComponentCheck(
                name="samples_api",
                status=HealthStatus.DEGRADED,
                latency_ms=elapsed_ms,
                message=f"USGS Samples API returned HTTP {status_code}",
                details={"status_code": status_code, "url": url},
            )
        except Exception as exc:
            elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
            return ComponentCheck(
                name="samples_api",
                status=HealthStatus.UNHEALTHY,
                latency_ms=elapsed_ms,
                message=f"USGS Samples API check failed: {exc}",
                details={"error": str(exc), "url": url},
            )

    async def check_llm_config(self) -> ComponentCheck:
        api_key = os.getenv("OPENAI_API_KEY")
        if api_key:
            masked = f"{api_key[:4]}...{api_key[-4:]}" if len(api_key) > 8 else "***"
            return ComponentCheck(
                name="llm_config",
                status=HealthStatus.HEALTHY,
                message="OpenAI API key configured",
                details={"key_configured": True, "masked_key": masked},
            )
        return ComponentCheck(
            name="llm_config",
            status=HealthStatus.DEGRADED,
            message="OPENAI_API_KEY is not set in environment",
            details={"key_configured": False},
        )

    async def run_checks(self) -> HealthReport:
        checks: dict[str, ComponentCheck] = {}
        checks["metadata_catalog"] = await self.check_catalog()
        checks["llm_config"] = await self.check_llm_config()
        checks["ogc_api"] = await self.check_ogc_api()
        checks["samples_api"] = await self.check_samples_api()

        statuses = [c.status for c in checks.values()]
        if any(s == HealthStatus.UNHEALTHY for s in statuses):
            if checks["metadata_catalog"].status == HealthStatus.UNHEALTHY:
                overall = HealthStatus.UNHEALTHY
            elif (
                checks["ogc_api"].status == HealthStatus.UNHEALTHY
                and checks["samples_api"].status == HealthStatus.UNHEALTHY
            ):
                overall = HealthStatus.UNHEALTHY
            else:
                overall = HealthStatus.DEGRADED
        elif any(s == HealthStatus.DEGRADED for s in statuses):
            overall = HealthStatus.DEGRADED
        else:
            overall = HealthStatus.HEALTHY

        return HealthReport(
            status=overall,
            timestamp=datetime.now(timezone.utc).isoformat(),
            checks=checks,
        )
