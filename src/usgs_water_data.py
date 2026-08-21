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
        print(f"[DEBUG][USGS] Requesting URL={url} params={params}")

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = await self._client.get(url, params=params)
            except httpx.TimeoutException as exc:
                last_exc = exc
                logger.warning("USGS request timed out (attempt %d/%d) url=%s", attempt, MAX_ATTEMPTS, url)
                print(f"[DEBUG][USGS] Attempt {attempt}/{MAX_ATTEMPTS} timed out for {url}")
            except httpx.TransportError as exc:
                last_exc = exc
                logger.warning("USGS request transport error (attempt %d/%d) url=%s err=%s", attempt, MAX_ATTEMPTS, url, exc)
                print(f"[DEBUG][USGS] Attempt {attempt}/{MAX_ATTEMPTS} transport error for {url}: {exc}")
            else:
                print(f"[DEBUG][USGS] Attempt {attempt}/{MAX_ATTEMPTS} response status={response.status_code} for {url}")
                if response.status_code == 404:
                    raise USGSNotFoundError(f"No data found for request to {url}")

                if response.status_code in RETRYABLE_STATUS_CODES:
                    last_exc = USGSServiceError(f"USGS API returned HTTP {response.status_code}")
                    logger.warning(
                        "USGS API returned retryable status %d (attempt %d/%d) url=%s",
                        response.status_code, attempt, MAX_ATTEMPTS, url,
                    )
                    print(f"[DEBUG][USGS] Retryable HTTP status {response.status_code} from {url}")
                elif response.is_error:
                    # Non-retryable client error: bad request, auth, etc.
                    raise USGSServiceError(
                        f"USGS API returned HTTP {response.status_code}: {response.text[:200]}"
                    )
                else:
                    try:
                        payload = response.json()
                        print(f"[DEBUG][USGS] Successful JSON response from {url}: keys={list(payload.keys())[:10]}")
                        return payload
                    except ValueError as exc:
                        raise USGSServiceError("USGS API returned a response that could not be parsed as JSON") from exc

            if attempt < MAX_ATTEMPTS:
                await asyncio.sleep(RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))

        raise USGSServiceError(
            f"USGS API request failed after {MAX_ATTEMPTS} attempts: {last_exc}"
        ) from last_exc

    async def fetch_monitoring_locations_by_state(self, state_name: str, limit: int = 10) -> list[dict[str, Any]]:
        url = f"{self.base_url}/collections/monitoring-locations/items"
        params: dict[str, Any] = {"state_name": state_name, "f": "json", "limit": limit}
        print(f"[DEBUG][USGS] Fetching monitoring locations by state: state_name={state_name}, limit={limit}")
        payload = await self._request(url, params)
        features = payload.get("features", [])
        print(f"[DEBUG][USGS] State lookup returned {len(features)} monitoring-location features")
        return features

    async def fetch_latest_continuous(
        self,
        site_id: str,
        parameter_codes: list[str] | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Fetch latest-continuous observations for a single site."""
        normalized_site = self.normalize_site_id(site_id)

        url = f"{self.base_url}/collections/daily/items"
        params: dict[str, Any] = {
            "monitoring_location_id": normalized_site,
            "f": "json",
            "limit": limit,
        }

        parameter_list = [str(code).strip() for code in (parameter_codes or []) if str(code).strip()]
        if parameter_list:
            params["parameter_code"] = ",".join(parameter_list)

        print(f"[DEBUG][USGS] Fetching latest continuous data: site_id={normalized_site}, parameter_codes={parameter_list}, limit={limit}")
        payload = await self._request(url, params)
        features = payload.get("features", [])
        print(f"[DEBUG][USGS] Latest continuous payload returned {len(features)} features for {normalized_site}")

        return {
            "site_id": normalized_site,
            "parameter_codes": parameter_list,
            "features": features,
            "count": len(features),
        }

    async def enrich_record_with_latest_continuous(
        self,
        record: dict[str, Any],
        site_id: str,
        parameter_codes: list[str] | None = None,
    ) -> dict[str, Any]:
        """Attach latest continuous observations to a single record."""
        payload = await self.fetch_latest_continuous(site_id=site_id, parameter_codes=parameter_codes)
        enriched = dict(record)
        enriched["usgsWaterProperties"] = payload
        return enriched

    async def get_parameter_catalog(self) -> dict[str, Any]:
        """Return metadata for parameter codes from the USGS API."""
        url = f"{self.base_url}/collections/parameter-codes/items"
        return await self._request(url, {"f": "json", "limit": 5})

    async def fetch_site_metadata(self, site_id: str) -> dict[str, Any]:
        """Fetch monitoring-location metadata (name, coordinates, drainage
        area, etc.) for a single site. Demonstrates that adding a new
        collection is just one more method built on `_request` — it gets
        retry/backoff and typed exceptions for free.
        """
        normalized_site = self.normalize_site_id(site_id)
        url = f"{self.base_url}/collections/monitoring-locations/items/{normalized_site}"
        print(f"[DEBUG][USGS] Fetching site metadata for {normalized_site}")
        return await self._request(url, {"f": "json"})


async def fetch_latest_continuous_data(
    site_id: str,
    parameter_codes: list[str] | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    async with USGSWaterDataFetcher() as fetcher:
        return await fetcher.fetch_latest_continuous(
            site_id=site_id,
            parameter_codes=parameter_codes,
            limit=limit,
        )