from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

from water_agent.config import USGS_API_VERSION

USGS_BASE_URL = f"https://api.waterdata.usgs.gov/ogcapi/{USGS_API_VERSION}"

DEFAULT_TIMEOUT_SECONDS = 15.0
MAX_ATTEMPTS = 4
RETRY_BACKOFF_BASE_SECONDS = 1.0
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


@dataclass
class PagedResult:
    features: list[dict[str, Any]]
    source_urls: list[str]

    def to_daily_readings(self) -> list[Any]:
        """Convert raw GeoJSON features to strongly typed ContinuousDailyReading DTOs."""
        from water_agent.dto import ContinuousDailyReading
        readings = []
        for f in self.features:
            props = f.get("properties", {})
            readings.append(
                ContinuousDailyReading(
                    monitoring_location_id=props.get("monitoring_location_id", ""),
                    parameter_code=props.get("parameter_code", ""),
                    statistic_id=props.get("statistic_id"),
                    value=props.get("value"),
                    unit=props.get("unit"),
                    time=props.get("time"),
                    approval_status=props.get("approval_status"),
                    raw_properties=props,
                )
            )
        return readings


class USGSError(Exception):
    """Base class for all USGS water data errors."""


class USGSValidationError(USGSError):
    """Input (site id, state name, params) failed validation before any request was made."""


class USGSNotFoundError(USGSError):
    """The USGS API had no data for an otherwise valid request."""


class USGSServiceError(USGSError):
    """The USGS API is unreachable, timed out, or returned a server-side error after retries."""


class USGSWaterDataFetcher:
    """Async client for the USGS Water Data OGC API.

    Intended to be constructed once and reused (e.g. as a long-lived
    attribute on the agent) so the underlying HTTP connection pool is
    shared across requests. Call `aclose()` on shutdown if your host
    framework supports a shutdown hook; otherwise use it as an async
    context manager for one-off calls.
    """

    def __init__(self, base_url: str = USGS_BASE_URL, timeout: float = DEFAULT_TIMEOUT_SECONDS):
        self.base_url = base_url.rstrip("/")
        from water_agent.circuit_breaker import CircuitBreaker
        from water_agent.config import USGS_API_KEY, settings

        headers = {"X-Api-Key": USGS_API_KEY} if USGS_API_KEY else {}
        self._client = httpx.AsyncClient(timeout=timeout, headers=headers)
        self.circuit_breaker = CircuitBreaker(
            name="usgs_ogc_api",
            failure_threshold=settings.circuit_breaker_failure_threshold,
            recovery_timeout=settings.circuit_breaker_recovery_timeout,
            expected_exceptions=(USGSServiceError, httpx.HTTPError, TimeoutError),
            excluded_exceptions=(USGSValidationError, USGSNotFoundError),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _ensure_client(self) -> httpx.AsyncClient:
        """Ensure the underlying AsyncClient is open, recreating it if previously closed."""
        if getattr(self._client, "is_closed", False):
            headers = {"X-Api-Key": USGS_API_KEY} if USGS_API_KEY else {}
            self._client = httpx.AsyncClient(timeout=self.timeout, headers=headers)
        return self._client

    async def __aenter__(self) -> "USGSWaterDataFetcher":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.aclose()

    @staticmethod
    def normalize_site_id(site_id: str) -> str:
        """Validate and normalize a site id to the 'USGS-########' form.

        Raises USGSValidationError rather than silently accepting a
        malformed id, so bad input fails fast before any network call.
        """
        raw = str(site_id).strip()
        if not raw:
            raise USGSValidationError("site_id is required")

        upper = raw.upper()
        numeric_part = upper[len("USGS-"):] if upper.startswith("USGS-") else upper

        if not re.fullmatch(r"\d{8,15}", numeric_part):
            raise USGSValidationError(
                f"'{raw}' does not look like a valid USGS site number "
                "(expected 8-15 digits, optionally prefixed with 'USGS-')"
            )
        return f"USGS-{numeric_part}"

    async def _request(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        """Execute request protected by circuit breaker to fail fast during outages."""
        return await self.circuit_breaker.call(self._raw_request, url, params)

    async def _raw_request(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        """GET with retry/backoff on timeouts, connection errors, and
        retryable HTTP status codes (429 / 5xx). Non-retryable errors
        (4xx other than 429, unparseable bodies) raise immediately.
        """
        from water_agent.tracing import start_span
        async with start_span("usgs_ogc.http_get", attributes={"http.url": url, "http.method": "GET"}) as span:
            last_exc: Exception | None = None
            logger.debug("Requesting URL=%s params=%s", url, params)

            for attempt in range(1, MAX_ATTEMPTS + 1):
                response: httpx.Response | None = None
                try:
                    client = self._ensure_client()
                    response = await client.get(url, params=params)
                except httpx.TimeoutException as exc:
                    last_exc = exc
                    logger.warning("USGS request timed out (attempt %d/%d) url=%s", attempt, MAX_ATTEMPTS, url)

                except httpx.TransportError as exc:
                    last_exc = exc
                    logger.warning("USGS request transport error (attempt %d/%d) url=%s err=%s", attempt, MAX_ATTEMPTS, url, exc)

                else:
                    logger.debug("Attempt %d/%d response status=%d for %s", attempt, MAX_ATTEMPTS, response.status_code, url)
                    span.set_attribute("http.status_code", response.status_code)
                    if response.status_code == 404:
                        raise USGSNotFoundError(f"No data found for request to {url}")

                    if response.status_code in RETRYABLE_STATUS_CODES:
                        last_exc = USGSServiceError(f"USGS API returned HTTP {response.status_code}")
                        logger.warning(
                            "USGS API returned retryable status %d (attempt %d/%d) url=%s",
                            response.status_code, attempt, MAX_ATTEMPTS, url,
                        )

                    elif response.is_error:
                        # Non-retryable client error: bad request, auth, etc.
                        raise USGSServiceError(
                            f"USGS API returned HTTP {response.status_code}: {response.text[:200]}"
                        )
                    else:
                        try:
                            payload = response.json()
                            logger.debug("Successful response from %s", url)
                            return payload
                        except ValueError as exc:
                            raise USGSServiceError("USGS API returned a response that could not be parsed as JSON") from exc

                if attempt < MAX_ATTEMPTS:
                    wait_time = RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                    if response is not None and response.status_code == 429:
                        retry_after = response.headers.get("Retry-After")
                        if retry_after:
                            try:
                                wait_time = max(wait_time, float(retry_after))
                            except ValueError:
                                pass
                        else:
                            wait_time = max(wait_time, 2.0 * attempt)
                    await asyncio.sleep(wait_time)

            span.set_error(str(last_exc))
            raise USGSServiceError(
                f"USGS API request failed after {MAX_ATTEMPTS} attempts: {last_exc}"
            ) from last_exc

    async def _fetch_all_pages(self, url: str, params: dict[str, Any], max_records: int) -> PagedResult:
        features: list[dict[str, Any]] = []
        source_urls: list[str] = []
        
        current_url = url
        current_params = params.copy()
        
        while len(features) < max_records:
            # Mask API key if it ends up in the request URL for logging/provenance
            # (Though we use headers, httpx.URL doesn't include headers in the URL string)
            request_url = str(httpx.URL(current_url, params=current_params))
            source_urls.append(request_url)
            
            payload = await self._request(current_url, current_params)
            page_features = payload.get("features", [])
            features.extend(page_features)
            
            if not page_features:
                break
                
            next_link = None
            for link in payload.get("links", []):
                if link.get("rel") == "next":
                    next_link = link.get("href")
                    break
                    
            if next_link:
                current_url = next_link
                current_params = {} # The next link has all params baked in
            else:
                break
                
        return PagedResult(features=features[:max_records], source_urls=source_urls)

    async def fetch_daily_values(
        self,
        filters: dict[str, str],
        datetime: str | None = None,
        limit: int | None = None,
        sortby: str | None = None,
    ) -> PagedResult:
        """Query the Daily collection with arbitrary queryable filters.
        
        Use `datetime` for exact date range bounds (e.g.,
        "2023-01-01T00:00:00Z/2023-12-31T00:00:00Z"). Full ISO date ranges are
        supported by the API (though ISO durations like "P30D" are rejected).
        
        If you want the most recent observations regardless of date, omit
        `datetime` and use `sortby="-time"` with a small `limit`.
        """
        url = f"{self.base_url}/collections/daily/items"
        params: dict[str, Any] = {
            **filters,
            "f": "json",
            "skipGeometry": "true",
        }
        if datetime is not None:
            params["datetime"] = datetime
        if limit is not None:
            params["limit"] = min(limit, 20000)
        else:
            params["limit"] = 20000
        if sortby is not None:
            params["sortby"] = sortby
            
        max_records = min(limit, 10000) if limit is not None else 10000
        logger.debug("Fetching daily values up to %d records", max_records)
        return await self._fetch_all_pages(url, params, max_records)

    async def fetch_time_series_metadata(self, filters: dict[str, str], limit: int = 1000) -> PagedResult:
        url = f"{self.base_url}/collections/time-series-metadata/items"
        params: dict[str, Any] = {
            **filters,
            "f": "json",
            "limit": min(limit, 20000),
            "properties": "monitoring_location_id,parameter_code,parameter_name,begin,end",
            "skipGeometry": "true",
        }
        max_records = min(limit, 5000)
        logger.debug("Fetching time-series-metadata up to %d records", max_records)
        return await self._fetch_all_pages(url, params, max_records)

    async def fetch_monitoring_locations(self, filters: dict[str, str], limit: int = 1000) -> PagedResult:
        url = f"{self.base_url}/collections/monitoring-locations/items"
        params: dict[str, Any] = {
            **filters,
            "f": "json",
            "limit": min(limit, 20000),
            "properties": "id,monitoring_location_name,site_type,county_name",
            "skipGeometry": "true",
        }
        max_records = min(limit, 5000)
        logger.debug("Fetching monitoring-locations up to %d records", max_records)
        return await self._fetch_all_pages(url, params, max_records)

