from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import httpx

logger = logging.getLogger(__name__)

USGS_BASE_URL = "https://api.waterdata.usgs.gov/ogcapi/v0"

DEFAULT_TIMEOUT_SECONDS = 15.0
MAX_ATTEMPTS = 3
RETRY_BACKOFF_BASE_SECONDS = 0.5
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


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
        self._client = httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

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
        """GET with retry/backoff on timeouts, connection errors, and
        retryable HTTP status codes (429 / 5xx). Non-retryable errors
        (4xx other than 429, unparseable bodies) raise immediately.
        """
        last_exc: Exception | None = None
        logger.debug("Requesting URL=%s params=%s", url, params)

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = await self._client.get(url, params=params)
            except httpx.TimeoutException as exc:
                last_exc = exc
                logger.warning("USGS request timed out (attempt %d/%d) url=%s", attempt, MAX_ATTEMPTS, url)

            except httpx.TransportError as exc:
                last_exc = exc
                logger.warning("USGS request transport error (attempt %d/%d) url=%s err=%s", attempt, MAX_ATTEMPTS, url, exc)

            else:
                logger.debug("Attempt %d/%d response status=%d for %s", attempt, MAX_ATTEMPTS, response.status_code, url)
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
                await asyncio.sleep(RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))

        raise USGSServiceError(
            f"USGS API request failed after {MAX_ATTEMPTS} attempts: {last_exc}"
        ) from last_exc

    async def fetch_daily_values(
        self,
        filters: dict[str, str],
        limit: int = 5,
    ) -> dict[str, Any]:
        """Query the Daily collection with arbitrary queryable filters
        (parameter_code, statistic_id, state_code, monitoring_location_id,
        etc.). Sorted most-recent-first (`sortby=-time`) rather than date-
        bounded: a bare filter like state_code with no site or date
        otherwise returns arbitrary records spanning decades (confirmed
        empirically), and this server rejects ISO 8601 duration strings
        like "P30D" for `datetime` despite USGS's own docs listing them as
        valid - sortby sidesteps needing to guess a "recent" window.
        """
        url = f"{self.base_url}/collections/daily/items"
        params: dict[str, Any] = {
            **filters,
            "f": "json",
            "limit": limit,
            "sortby": "-time",
        }
        request_url = str(httpx.URL(url, params=params))

        logger.debug("Fetching daily values: %s", request_url)
        payload = await self._request(url, params)
        features = payload.get("features", [])
        logger.debug("Daily values query returned %d features", len(features))

        return {
            "filters": filters,
            "request_url": request_url,
            "features": features,
            "count": len(features),
        }

    async def fetch_time_series_metadata(self, filters: dict[str, str], limit: int = 1000) -> dict[str, Any]:
        url = f"{self.base_url}/collections/time-series-metadata/items"
        params: dict[str, Any] = {
            **filters,
            "f": "json",
            "limit": limit,
        }
        request_url = str(httpx.URL(url, params=params))
        logger.debug("Fetching time-series-metadata: %s", request_url)
        payload = await self._request(url, params)
        features = payload.get("features", [])
        logger.debug("time-series-metadata query returned %d features", len(features))
        
        return {
            "filters": filters,
            "request_url": request_url,
            "features": features,
            "count": len(features),
        }

    async def fetch_monitoring_locations(self, filters: dict[str, str], limit: int = 1000) -> dict[str, Any]:
        url = f"{self.base_url}/collections/monitoring-locations/items"
        params: dict[str, Any] = {
            **filters,
            "f": "json",
            "limit": limit,
        }
        request_url = str(httpx.URL(url, params=params))
        logger.debug("Fetching monitoring-locations: %s", request_url)
        payload = await self._request(url, params)
        features = payload.get("features", [])
        logger.debug("monitoring-locations query returned %d features", len(features))
        
        return {
            "filters": filters,
            "request_url": request_url,
            "features": features,
            "count": len(features),
        }
