from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator

import httpx

from water_agent.config import USGS_API_KEY
from water_agent.metadata.discrete_families import (
    DISCRETE_FAMILIES,
    DiscreteParameter,
    get_family_for_pcode,
    get_parameter,
    is_mixed_modality,
)
from water_agent.results.context import RequestContext
from water_agent.results.store import ResultHandle

logger = logging.getLogger(__name__)

SAMPLES_BASE_URL = "https://api.waterdata.usgs.gov/samples-data"
DEFAULT_SAMPLES_TIMEOUT = 45.0
MAX_LOCATIONS_THRESHOLD = 5000
MAX_RESULTS_THRESHOLD = 25000
STREAM_MAX_ROWS = 25000
DISCRETE_MAX_RECORDS = MAX_RESULTS_THRESHOLD


class SamplesError(Exception):
    """Base error for USGS Samples API operations."""


class SamplesRateLimitError(SamplesError):
    """Raised when the Samples API returns HTTP 429 or quota is exhausted."""


class SamplesValidationError(SamplesError):
    """Raised when the Samples API returns HTTP 400 for invalid parameters."""


class SamplesOverflowError(SamplesError):
    """Raised when a query exceeds safe records threshold and cannot be safely partitioned."""

    def __init__(self, count: int, threshold: int, message: str):
        super().__init__(message)
        self.count = count
        self.threshold = threshold


@dataclass
class DiscreteFetchResult:
    records: list[dict[str, Any]]
    total_count: int
    source_urls: list[str]
    handle: ResultHandle | None = None
    warning: str | None = None
    partitioned: bool = False

    def to_observation_records(self) -> list[Any]:
        """Convert raw discrete observation records to strongly-typed DiscreteObservationRecord DTOs."""
        from water_agent.dto import DiscreteObservationRecord
        return [DiscreteObservationRecord(**rec) for rec in self.records]


def to_iso_date(date_str: str | None) -> str | None:
    """Normalize date strings (YYYY-MM-DD or M/D/YYYY) to ISO YYYY-MM-DD."""
    if not date_str:
        return None
    date_str = date_str.strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
        return date_str
    parts = date_str.split("/")
    if len(parts) == 3:
        try:
            m, d, y = parts
            return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
        except (ValueError, TypeError):
            return date_str
    return date_str


class USGSSamplesFetcher:
    """Dedicated fetcher for discrete water-quality data from the USGS Samples API.
    
    Operates separately from the OGC continuous API. Enforces count-first guards,
    streaming CSV projection, overflow partitioning, rate-limit protection,
    and integrates with RequestContext / ResultStore.
    """

    def __init__(
        self,
        base_url: str = SAMPLES_BASE_URL,
        timeout: float = DEFAULT_SAMPLES_TIMEOUT,
        max_locations_threshold: int = MAX_LOCATIONS_THRESHOLD,
        max_results_threshold: int = MAX_RESULTS_THRESHOLD,
        stream_max_rows: int = STREAM_MAX_ROWS,
        max_records: int | None = None,
        client: httpx.AsyncClient | None = None,
        counties_reference_path: str | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        if max_records is not None:
            max_results_threshold = max_records
        self.max_locations_threshold = max_locations_threshold
        self.max_results_threshold = max_results_threshold
        self.stream_max_rows = stream_max_rows
        self.max_records = max_results_threshold
        self._counties_path = counties_reference_path or os.path.join(
            "metadata", "raw", "references", "counties.json"
        )
        self._counties_cache: dict[str, list[dict[str, str]]] = {}

        if client is not None:
            self._client = client
            self._owns_client = False
        else:
            headers = {"X-Api-Key": USGS_API_KEY} if USGS_API_KEY else {}
            self._client = httpx.AsyncClient(timeout=self.timeout, headers=headers)
            self._owns_client = True

        from water_agent.circuit_breaker import CircuitBreaker
        from water_agent.config import settings

        self.circuit_breaker = CircuitBreaker(
            name="usgs_samples_api",
            failure_threshold=settings.circuit_breaker_failure_threshold,
            recovery_timeout=settings.circuit_breaker_recovery_timeout,
            expected_exceptions=(httpx.HTTPError, TimeoutError, SamplesError),
            excluded_exceptions=(SamplesValidationError,),
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _ensure_client(self) -> httpx.AsyncClient:
        """Ensure the underlying AsyncClient is open, recreating it if previously closed."""
        if self._owns_client and getattr(self._client, "is_closed", False):
            headers = {"X-Api-Key": USGS_API_KEY} if USGS_API_KEY else {}
            self._client = httpx.AsyncClient(timeout=self.timeout, headers=headers)
        return self._client

    async def __aenter__(self) -> USGSSamplesFetcher:
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.aclose()

    def _check_rate_limit(self, headers: httpx.Headers) -> None:
        limit_str = headers.get("x-ratelimit-limit")
        rem_str = headers.get("x-ratelimit-remaining")
        if limit_str and rem_str:
            try:
                limit = int(limit_str)
                rem = int(rem_str)
                if rem < 100 or (rem / limit) < 0.10:
                    logger.warning(
                        f"USGS Samples API quota low: {rem}/{limit} remaining. Pausing 10s..."
                    )
            except ValueError:
                pass

    def _build_source_url(self, endpoint: str, params: dict[str, Any]) -> str:
        req = self._ensure_client().build_request("GET", endpoint, params=params)
        return str(req.url)

    def _normalize_params(self, params: dict[str, Any]) -> dict[str, Any]:
        """Normalize query parameters and guard against invalid formats.

        Comma-separated usgsPCode values (e.g. '00665,00660') are silently rejected
        by the USGS Samples API with recordCount 0. This guard splits any comma-separated
        string into a list of individual pcodes, which httpx serializes as repeated
        query parameters (e.g. usgsPCode=00665&usgsPCode=00660).
        """
        clean_params = dict(params)
        pcode = clean_params.get("usgsPCode")
        if isinstance(pcode, str) and "," in pcode:
            logger.info("Splitting comma-separated usgsPCode into repeated query parameters: %r", pcode)
            clean_params["usgsPCode"] = [p.strip() for p in pcode.split(",") if p.strip()]
        return clean_params

    async def _safe_get(self, url: str, params: dict[str, Any] | None = None) -> httpx.Response:
        """Issue an HTTP GET request guarded by the CircuitBreaker, retries, and Tracing Span."""
        from water_agent.tracing import start_span
        async with start_span("usgs_samples.http_get", attributes={"http.url": url, "http.method": "GET"}) as span:
            async def _do():
                max_attempts = 4
                last_exc: Exception | None = None
                client = self._ensure_client()
                for attempt in range(1, max_attempts + 1):
                    try:
                        resp = await client.get(url, params=params, timeout=self.timeout)
                    except (httpx.TimeoutException, httpx.TransportError) as exc:
                        last_exc = exc
                        logger.warning("USGS Samples request error (attempt %d/%d) url=%s: %s", attempt, max_attempts, url, exc)
                        if attempt < max_attempts:
                            await asyncio.sleep(1.0 * (2 ** (attempt - 1)))
                            continue
                        raise SamplesError(f"USGS Samples request failed: {exc}") from exc

                    span.set_attribute("http.status_code", resp.status_code)
                    if resp.status_code == 429:
                        last_exc = SamplesRateLimitError(
                            f"Samples API 429 Too Many Requests (Limit: {resp.headers.get('x-ratelimit-limit')})"
                        )
                        logger.warning(
                            "USGS Samples API returned 429 (attempt %d/%d) url=%s", attempt, max_attempts, url
                        )
                        if attempt < max_attempts:
                            retry_after = resp.headers.get("Retry-After")
                            wait_time = 2.0 * attempt
                            if retry_after:
                                try:
                                    wait_time = max(wait_time, float(retry_after))
                                except ValueError:
                                    pass
                            await asyncio.sleep(wait_time)
                            continue
                        raise last_exc

                    if resp.status_code in {500, 502, 503, 504}:
                        last_exc = SamplesError(f"USGS Samples API server error HTTP {resp.status_code}: {resp.text[:200]}")
                        logger.warning("USGS Samples API server error %d (attempt %d/%d)", resp.status_code, attempt, max_attempts)
                        if attempt < max_attempts:
                            await asyncio.sleep(1.0 * (2 ** (attempt - 1)))
                            continue
                        raise last_exc

                    if resp.status_code == 400:
                        err_text = resp.text
                        if "Invalid parameter" in err_text:
                            raise SamplesValidationError(f"Invalid parameter in query: {err_text}")
                        raise SamplesError(f"HTTP 400 bad request: {err_text}")

                    resp.raise_for_status()
                    self._check_rate_limit(resp.headers)
                    return resp

                if last_exc:
                    raise last_exc

            return await self.circuit_breaker.call(_do)

    # -------------------------------------------------------------------------
    # COUNT-FIRST API METHODS
    # -------------------------------------------------------------------------

    async def get_results_count(self, params: dict[str, Any]) -> int:
        """Query /results/count with application/json to check matching record count."""
        count_params = self._normalize_params(params)
        count_params["mimeType"] = "application/json"
        url = f"{self.base_url}/results/count"

        resp = await self._safe_get(url, params=count_params)
        data = resp.json()
        return int(data.get("recordCount", 0))

    async def get_locations_count(self, params: dict[str, Any]) -> int:
        """Query /locations/count with application/json to check matching station count."""
        count_params = self._normalize_params(params)
        count_params["mimeType"] = "application/json"
        url = f"{self.base_url}/locations/count"

        resp = await self._safe_get(url, params=count_params)
        data = resp.json()
        return int(data.get("recordCount", 0))

    async def fetch_station_summary(
        self, station_id: str
    ) -> tuple[dict[str, list[dict[str, Any]]], int, str]:
        """Fetch discrete analytical summary for a station from /summary/{station_id}.

        Returns:
            tuple of (discrete_inventory, total_discrete_observations, summary_url)
        """
        station_id_norm = station_id.strip()
        if not station_id_norm.startswith("USGS-") and station_id_norm.isdigit():
            station_id_norm = f"USGS-{station_id_norm}"

        url = f"{self.base_url}/summary/{station_id_norm}"
        params = {"mimeType": "text/csv"}
        clean_url = self._build_source_url(url, params)

        discrete_inventory: dict[str, list[dict[str, Any]]] = {}
        total_discrete_observations = 0

        try:
            resp = await self._safe_get(url, params=params)
            if resp.status_code == 200:
                self._check_rate_limit(resp.headers)
                reader = csv.DictReader(io.StringIO(resp.text))
                for row in reader:
                    group = row.get("characteristicGroup", "").strip()
                    if group == "Information":
                        continue  # Exclude non-substantive Information metadata group

                    desc = row.get("characteristicUserSupplied", "").strip() or row.get("characteristic", "").strip()
                    count_str = row.get("resultCount", "").strip()
                    count = int(count_str) if count_str.isdigit() else 0
                    first_act = row.get("firstActivity", "").strip()
                    last_act = row.get("mostRecentActivity", "").strip()

                    if group not in discrete_inventory:
                        discrete_inventory[group] = []

                    discrete_inventory[group].append({
                        "characteristic": row.get("characteristic", "").strip(),
                        "descriptor": desc,
                        "count": count,
                        "first_activity": first_act,
                        "most_recent_activity": last_act,
                    })
                    total_discrete_observations += count
        except Exception as e:
            logger.warning("Failed to fetch discrete summary for %s: %s", station_id_norm, e)

        return discrete_inventory, total_discrete_observations, clean_url

    # -------------------------------------------------------------------------
    # STREAMING DATA PROFILE FETCHERS
    # -------------------------------------------------------------------------

    async def fetch_results(
        self,
        params: dict[str, Any],
        profile: str = "narrow",
        context: RequestContext | None = None,
        pcode: str | None = None,
    ) -> DiscreteFetchResult:
        """Fetch discrete observations with count-first guard, overflow partition, and streaming projection."""
        pcode_val = pcode or params.get("usgsPCode")
        if isinstance(pcode_val, list):
            pcode_val = pcode_val[0]

        # 3a. Count-first guard
        count = await self.get_results_count(params)
        if count == 0:
            logger.info("Count-first guard returned 0 matching records. Returning empty result.")
            clean_url = self._build_source_url(f"{self.base_url}/results/{profile}", params)
            return self._build_results_result(
                records=[],
                total_count=0,
                source_urls=[clean_url],
                pcode=pcode_val,
                context=context,
            )

        # 3b. Overflow check: if count exceeds max_results_threshold or stream_max_rows, partition
        if count > self.max_results_threshold or count > self.stream_max_rows:
            return await self._handle_results_overflow(
                params=params,
                profile=profile,
                initial_count=count,
                context=context,
                pcode=pcode_val,
            )

        # 3c & 3d. Fetch single batch with streaming projection
        return await self._fetch_results_direct(
            params=params,
            profile=profile,
            expected_count=count,
            context=context,
            pcode=pcode_val,
        )

    async def _fetch_results_direct(
        self,
        params: dict[str, Any],
        profile: str,
        expected_count: int,
        context: RequestContext | None,
        pcode: str | None,
    ) -> DiscreteFetchResult:
        fetch_params = dict(params)
        fetch_params["mimeType"] = "text/csv"
        url = f"{self.base_url}/results/{profile}"
        clean_url = self._build_source_url(url, fetch_params)

        try:
            records = await self._stream_and_project_results(url, fetch_params)
        except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.RemoteProtocolError) as e:
            logger.warning(
                f"Direct streaming failed on {url} ({type(e).__name__}: {e}). "
                f"Attempting temporal partitioning to fetch 100% of data without silent truncation..."
            )
            try:
                return await self._partition_results_temporally(
                    params=params,
                    profile=profile,
                    initial_count=expected_count,
                    context=context,
                    pcode=pcode,
                )
            except Exception as partition_err:
                logger.error(f"Temporal partitioning recovery failed: {partition_err}")
                raise SamplesError(
                    f"Failed to retrieve discrete observations: streaming timed out and "
                    f"temporal partitioning failed ({partition_err})."
                ) from partition_err

        return self._build_results_result(
            records=records,
            total_count=len(records),
            source_urls=[clean_url],
            pcode=pcode,
            context=context,
        )

    async def _stream_and_project_results(
        self, url: str, params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        header: list[str] | None = None

        async with self._ensure_client().stream("GET", url, params=params, timeout=self.timeout) as resp:
            if resp.status_code == 429:
                raise SamplesRateLimitError("429 Too Many Requests")
            if resp.status_code == 400:
                body = await resp.aread()
                text = body.decode("utf-8", errors="replace")
                if "Invalid parameter" in text:
                    raise SamplesValidationError(f"Invalid parameter: {text}")
                raise SamplesError(f"HTTP 400: {text}")

            resp.raise_for_status()
            self._check_rate_limit(resp.headers)

            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                try:
                    row = next(csv.reader([line]))
                except Exception:
                    continue

                if header is None:
                    header = row
                    continue

                row_dict = dict(zip(header, row))
                projected = self._project_result_row(row_dict)
                records.append(projected)

        return records

    def _project_result_row(self, row: dict[str, str]) -> dict[str, Any]:
        """Project result CSV row to standard schema and ISO date format."""
        raw_date = row.get("Activity_StartDate") or row.get("ActivityStartDate")
        iso_date = to_iso_date(raw_date)

        raw_val = row.get("Result_Measure") or row.get("ResultMeasureValue")
        val = None
        if raw_val is not None and raw_val != "":
            try:
                val = float(raw_val)
            except ValueError:
                val = raw_val

        return {
            "activity_date": iso_date,
            "activity_time": row.get("Activity_StartTime") or row.get("ActivityStartTime"),
            "result_value": val,
            "unit": row.get("Result_MeasureUnit") or row.get("ResultMeasure/MeasureUnitCode"),
            "pcode": row.get("USGSpcode") or row.get("ParameterCode"),
            "characteristic_name": row.get("Result_Characteristic") or row.get("CharacteristicName"),
            "detection_condition": row.get("Result_ResultDetectionCondition") or row.get("ResultDetectionConditionText"),
            "detection_limit": row.get("DetectionLimit_MeasureA") or row.get("DetectionQuantitationLimitMeasure/MeasureValue"),
            "location_id": row.get("Location_Identifier") or row.get("LocationIdentifier"),
            "location_name": row.get("Location_Name") or row.get("LocationName"),
            "sample_fraction": row.get("Result_SampleFraction"),
        }

    # -------------------------------------------------------------------------
    # LOCATIONS FETCH
    # -------------------------------------------------------------------------

    async def fetch_locations(
        self,
        params: dict[str, Any],
        context: RequestContext | None = None,
        pcode: str | None = None,
    ) -> DiscreteFetchResult:
        """Fetch discrete monitoring locations matching criteria."""
        clean_params = self._normalize_params(params)
        count = await self.get_locations_count(clean_params)
        url = f"{self.base_url}/locations/site"
        fetch_params = dict(clean_params)
        fetch_params["mimeType"] = "text/csv"
        clean_url = self._build_source_url(url, fetch_params)

        if count == 0:
            return self._build_locations_result(
                records=[],
                total_count=0,
                source_urls=[clean_url],
                pcode=pcode,
                context=context,
            )

        if count > self.max_locations_threshold:
            logger.info(
                f"Locations count {count:,} exceeds threshold ({self.max_locations_threshold:,}). "
                f"Partitioning geographically by county..."
            )
            return await self._handle_locations_overflow(
                params=params,
                initial_count=count,
                context=context,
                pcode=pcode,
            )

        records = await self._stream_and_project_locations(url, fetch_params)
        return self._build_locations_result(
            records=records,
            total_count=len(records),
            source_urls=[clean_url],
            pcode=pcode,
            context=context,
        )

    async def _stream_and_project_locations(
        self, url: str, params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        header: list[str] | None = None

        async with self._ensure_client().stream("GET", url, params=params, timeout=self.timeout) as resp:
            if resp.status_code == 429:
                raise SamplesRateLimitError("429 Too Many Requests")
            if resp.status_code == 400:
                body = await resp.aread()
                text = body.decode("utf-8", errors="replace")
                if "Invalid parameter" in text:
                    raise SamplesValidationError(f"Invalid parameter: {text}")
                raise SamplesError(f"HTTP 400: {text}")

            resp.raise_for_status()
            self._check_rate_limit(resp.headers)

            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                try:
                    row = next(csv.reader([line]))
                except Exception:
                    continue

                if header is None:
                    header = row
                    continue

                row_dict = dict(zip(header, row))
                projected = self._project_location_row(row_dict)
                records.append(projected)

        return records

    def _project_location_row(self, row: dict[str, str]) -> dict[str, Any]:
        """Project location CSV row to standard schema."""
        lat_str = row.get("Location_Latitude") or row.get("LatitudeMeasure")
        lon_str = row.get("Location_Longitude") or row.get("LongitudeMeasure")
        lat = None
        lon = None
        try:
            if lat_str:
                lat = float(lat_str)
            if lon_str:
                lon = float(lon_str)
        except ValueError:
            pass

        return {
            "location_id": row.get("Location_Identifier") or row.get("LocationIdentifier"),
            "location_name": row.get("Location_Name") or row.get("LocationName"),
            "latitude": lat,
            "longitude": lon,
            "county_name": row.get("Location_CountyName") or row.get("CountyName"),
            "state_name": row.get("Location_State") or row.get("StateName"),
            "site_type": row.get("Location_Type") or row.get("LocationType"),
        }

    # -------------------------------------------------------------------------
    # OVERFLOW PARTITIONING (STEP 3b)
    # -------------------------------------------------------------------------

    async def _handle_results_overflow(
        self,
        params: dict[str, Any],
        profile: str,
        initial_count: int,
        context: RequestContext | None,
        pcode: str | None,
    ) -> DiscreteFetchResult:
        """Default overflow strategy for results is temporal partitioning."""
        logger.info(
            f"Results overflow ({initial_count:,} records > threshold). "
            f"Partitioning temporally by date chunks..."
        )
        return await self._partition_results_temporally(
            params=params,
            profile=profile,
            initial_count=initial_count,
            context=context,
            pcode=pcode,
        )

    async def _partition_results_temporally(
        self,
        params: dict[str, Any],
        profile: str,
        initial_count: int,
        context: RequestContext | None,
        pcode: str | None,
    ) -> DiscreteFetchResult:
        """Partition discrete observations across date chunks to retrieve 100% of data without silent truncation."""
        current_year = datetime.now(timezone.utc).year
        start_str = params.get("activityStartDateLower")
        end_str = params.get("activityStartDateUpper")

        windows: list[tuple[str, str]] = []
        if start_str or end_str:
            s_year = int(start_str[:4]) if start_str else 1900
            e_year = int(end_str[:4]) if end_str else current_year
            step = 5 if (e_year - s_year) > 10 else 2
            y = s_year
            while y <= e_year:
                chunk_end_year = min(y + step - 1, e_year)
                w_lo = start_str if y == s_year and start_str else f"{y:04d}-01-01"
                w_hi = end_str if chunk_end_year == e_year and end_str else f"{chunk_end_year:04d}-12-31"
                windows.append((w_lo, w_hi))
                y = chunk_end_year + 1
        else:
            windows = [
                ("1900-01-01", "1979-12-31"),
                ("1980-01-01", "1989-12-31"),
                ("1990-01-01", "1999-12-31"),
                ("2000-01-01", "2009-12-31"),
                ("2010-01-01", "2019-12-31"),
                ("2020-01-01", f"{current_year}-12-31"),
            ]

        all_records: list[dict[str, Any]] = []
        source_urls: list[str] = []
        recovered_any = False

        for w_lo, w_hi in windows:
            sub_params = dict(params)
            sub_params["activityStartDateLower"] = w_lo
            sub_params["activityStartDateUpper"] = w_hi

            try:
                w_count = await self.get_results_count(sub_params)
            except Exception as e:
                logger.warning(f"Temporal chunk count error for {w_lo} to {w_hi}: {e}")
                continue

            if w_count == 0:
                continue

            recovered_any = True

            # If chunk still exceeds hard streaming ceiling, sub-partition annually
            if w_count > self.stream_max_rows:
                sy = int(w_lo[:4])
                ey = int(w_hi[:4])
                for yr in range(sy, ey + 1):
                    yr_params = dict(params)
                    yr_params["activityStartDateLower"] = f"{yr:04d}-01-01"
                    yr_params["activityStartDateUpper"] = f"{yr:04d}-12-31"
                    try:
                        yr_cnt = await self.get_results_count(yr_params)
                    except Exception:
                        continue
                    if yr_cnt == 0:
                        continue
                    if yr_cnt > self.stream_max_rows:
                        # Fall back to county partitioning for this single extreme year if stateFips is present
                        if params.get("stateFips") and not params.get("countyFips"):
                            logger.info(f"Year {yr} has {yr_cnt:,} records > ceiling ({self.stream_max_rows:,}). Using county partitioning for this year.")
                            yr_rec, yr_urls = await self._partition_county_by_year(yr_params, profile, yr_cnt)
                            all_records.extend(yr_rec)
                            source_urls.extend(yr_urls)
                            continue
                        raise SamplesOverflowError(
                            count=yr_cnt,
                            threshold=self.max_results_threshold,
                            message=(
                                f"Query matches {yr_cnt:,} records, which exceeds the safe threshold of "
                                f"{self.max_results_threshold:,}. Narrow your question to a specific station or shorter time window."
                            ),
                        )
                    fetch_yr_params = dict(yr_params)
                    fetch_yr_params["mimeType"] = "text/csv"
                    yr_url = f"{self.base_url}/results/{profile}"
                    yr_records = await self._stream_and_project_results(yr_url, fetch_yr_params)
                    all_records.extend(yr_records)
                    source_urls.append(self._build_source_url(yr_url, fetch_yr_params))
                    await asyncio.sleep(0.05)
            else:
                fetch_sub_params = dict(sub_params)
                fetch_sub_params["mimeType"] = "text/csv"
                sub_url = f"{self.base_url}/results/{profile}"
                chunk_records = await self._stream_and_project_results(sub_url, fetch_sub_params)
                all_records.extend(chunk_records)
                source_urls.append(self._build_source_url(sub_url, fetch_sub_params))
                await asyncio.sleep(0.05)

        if not recovered_any and initial_count > 0:
            if params.get("stateFips") and not params.get("countyFips"):
                logger.info("Temporal partitioning could not connect to date chunks. Falling back to county partitioning...")
                return await self._handle_results_overflow_by_county(params, profile, initial_count, context, pcode)
            raise SamplesError(
                f"Failed to retrieve discrete observations: temporal partitioning failed for query matching {initial_count:,} records."
            )

        return self._build_results_result(
            records=all_records,
            total_count=len(all_records),
            source_urls=source_urls,
            pcode=pcode,
            context=context,
            partitioned=True,
            params=params,
        )

    async def _handle_results_overflow_by_county(
        self,
        params: dict[str, Any],
        profile: str,
        initial_count: int,
        context: RequestContext | None,
        pcode: str | None,
    ) -> DiscreteFetchResult:
        state_fips = params.get("stateFips")
        county_fips = params.get("countyFips")

        # Case 1: Statewide overflow -> partition by county
        if state_fips and not county_fips:
            state_code = state_fips.replace("US:", "")
            counties = self._get_counties_for_state(state_code)
            logger.info(
                f"Statewide query overflow ({initial_count} records > {self.max_results_threshold}). "
                f"Partitioning across {len(counties)} counties for state {state_code}..."
            )

            all_records: list[dict[str, Any]] = []
            source_urls: list[str] = []

            for county in counties:
                c_fips = f"US:{state_code}:{county['county_fips_code']}"
                sub_params = dict(params)
                sub_params["countyFips"] = c_fips

                c_count = await self.get_results_count(sub_params)
                if c_count == 0:
                    continue

                if c_count > self.max_results_threshold:
                    c_records, c_urls = await self._partition_county_by_year(
                        sub_params, profile, c_count
                    )
                    all_records.extend(c_records)
                    source_urls.extend(c_urls)
                else:
                    c_res = await self._fetch_results_direct(
                        params=sub_params,
                        profile=profile,
                        expected_count=c_count,
                        context=None,
                        pcode=pcode,
                    )
                    all_records.extend(c_res.records)
                    source_urls.extend(c_res.source_urls)

                await asyncio.sleep(0.05)

            return self._build_results_result(
                records=all_records,
                total_count=len(all_records),
                source_urls=source_urls,
                pcode=pcode,
                context=context,
                partitioned=True,
                params=params,
            )

        # Case 2: County-level overflow -> try annual chunks
        if county_fips:
            logger.info(
                f"County query overflow ({initial_count} records > {self.max_results_threshold}). "
                f"Partitioning by annual chunks..."
            )
            c_records, c_urls = await self._partition_county_by_year(
                params, profile, initial_count
            )
            return self._build_results_result(
                records=c_records,
                total_count=len(c_records),
                source_urls=c_urls,
                pcode=pcode,
                context=context,
                partitioned=True,
                params=params,
            )

        raise SamplesOverflowError(
            count=initial_count,
            threshold=self.max_results_threshold,
            message=(
                f"Query matches {initial_count:,} records, which exceeds the safe threshold of "
                f"{self.max_results_threshold:,}. Narrow your question to a specific station or shorter time window."
            ),
        )

    async def _handle_locations_overflow(
        self,
        params: dict[str, Any],
        initial_count: int,
        context: RequestContext | None,
        pcode: str | None,
    ) -> DiscreteFetchResult:
        state_fips = params.get("stateFips")
        county_fips = params.get("countyFips")

        if state_fips and not county_fips:
            state_code = state_fips.replace("US:", "")
            counties = self._get_counties_for_state(state_code)
            logger.info(
                f"Locations overflow ({initial_count} > {self.max_locations_threshold}). "
                f"Partitioning across {len(counties)} counties for state {state_code}..."
            )

            all_records: list[dict[str, Any]] = []
            source_urls: list[str] = []
            seen_stations: set[str] = set()

            for county in counties:
                c_fips = f"US:{state_code}:{county['county_fips_code']}"
                sub_params = dict(params)
                sub_params["countyFips"] = c_fips

                c_count = await self.get_locations_count(sub_params)
                if c_count == 0:
                    continue

                sub_url = f"{self.base_url}/locations/site"
                sub_fetch_params = dict(sub_params)
                sub_fetch_params["mimeType"] = "text/csv"
                clean_url = self._build_source_url(sub_url, sub_fetch_params)

                c_records = await self._stream_and_project_locations(sub_url, sub_fetch_params)
                for rec in c_records:
                    sid = rec.get("location_id")
                    if sid and sid not in seen_stations:
                        seen_stations.add(sid)
                        all_records.append(rec)
                    elif not sid:
                        all_records.append(rec)
                source_urls.append(clean_url)
                await asyncio.sleep(0.05)

            return self._build_locations_result(
                records=all_records,
                total_count=len(all_records),
                source_urls=source_urls,
                pcode=pcode,
                context=context,
            )

        raise SamplesOverflowError(
            count=initial_count,
            threshold=self.max_locations_threshold,
            message=(
                f"Locations query matches {initial_count:,} stations, exceeding threshold of "
                f"{self.max_locations_threshold:,}. Narrow your question to a specific county."
            ),
        )

    async def _partition_county_by_year(
        self, params: dict[str, Any], profile: str, current_count: int
    ) -> tuple[list[dict[str, Any]], list[str]]:
        current_year = datetime.now(timezone.utc).year
        records: list[dict[str, Any]] = []
        urls: list[str] = []

        # Check last 5 years
        for year in range(current_year - 4, current_year + 1):
            year_params = dict(params)
            year_params["activityStartDateLower"] = f"{year}-01-01"
            year_params["activityStartDateUpper"] = f"{year}-12-31"

            y_count = await self.get_results_count(year_params)
            if y_count == 0:
                continue

            if y_count > self.max_records:
                raise SamplesOverflowError(
                    count=y_count,
                    threshold=self.max_records,
                    message=(
                        f"Query matches {y_count:,} records for county in year {year}, exceeding safe "
                        f"threshold of {self.max_records:,}. Narrow your question to a specific station."
                    ),
                )

            res = await self._fetch_results_direct(
                params=year_params,
                profile=profile,
                expected_count=y_count,
                context=None,
                pcode=params.get("usgsPCode"),
            )
            records.extend(res.records)
            urls.extend(res.source_urls)

        return records, urls

    def _get_counties_for_state(self, state_fips: str) -> list[dict[str, str]]:
        state_fips_pad = state_fips.zfill(2)
        if state_fips_pad in self._counties_cache:
            return self._counties_cache[state_fips_pad]

        counties: list[dict[str, str]] = []
        if os.path.exists(self._counties_path):
            try:
                with open(self._counties_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for feat in data.get("features", []):
                    props = feat.get("properties", {})
                    if props.get("country_code") == "US" and props.get("state_fips_code") == state_fips_pad:
                        counties.append({
                            "county_fips_code": props.get("county_fips_code", ""),
                            "county_name": props.get("county_name", "")
                        })
            except Exception as e:
                logger.warning(f"Failed to load counties from {self._counties_path}: {e}")

        self._counties_cache[state_fips_pad] = counties
        return counties

    # -------------------------------------------------------------------------
    # RESULTSTORE AND RESPONSE CONTRACT INTEGRATION (STEP 3f)
    # -------------------------------------------------------------------------

    def _build_results_result(
        self,
        records: list[dict[str, Any]],
        total_count: int,
        source_urls: list[str],
        pcode: str | None,
        context: RequestContext | None,
        warning: str | None = None,
        partitioned: bool = False,
        params: dict[str, Any] | None = None,
    ) -> DiscreteFetchResult:
        handle = None
        if context is not None:
            param_obj = get_parameter(pcode) if pcode else None
            pcode_str = pcode or "unknown"
            desc_str = param_obj.descriptor if param_obj else "discrete observation"
            unit_str = param_obj.unit_of_measure if param_obj else "units"

            # Compute distinct stations
            stations = {r["location_id"] for r in records if r.get("location_id")}
            station_count = len(stations)

            # Compute date range
            dates = [r["activity_date"] for r in records if r.get("activity_date")]
            date_range_str = (
                f" between {min(dates)} and {max(dates)}" if dates else ""
            )

            # Response contract citation:
            citation = f"{total_count:,} discrete records of {desc_str} ({pcode_str}), {unit_str}"
            summary = (
                f"{citation} across {station_count:,} stations{date_range_str}. "
                f"Modality: discrete sample data (not continuous sensor data)."
            )

            if pcode and is_mixed_modality(pcode):
                summary += (
                    " Note: Continuous sensor data for this parameter also exists "
                    "in USGS systems and is not included in this discrete query."
                )

            if warning:
                summary += f" [Note: {warning}]"
            elif params and (params.get("activityStartDateLower") or params.get("activityStartDateUpper")):
                bounds = []
                if params.get("activityStartDateLower"):
                    bounds.append(f"from {params['activityStartDateLower']}")
                if params.get("activityStartDateUpper"):
                    bounds.append(f"to {params['activityStartDateUpper']}")
                summary += f" (Time bounds: {' '.join(bounds)})"

            sample = records[:5]
            provenance = {
                "source_urls": source_urls,
                "pcode": pcode_str,
                "descriptor": desc_str,
                "unit_of_measure": unit_str,
                "station_count": station_count,
                "min_date": min(dates) if dates else None,
                "max_date": max(dates) if dates else None,
                "modality": "discrete",
                "partitioned": partitioned,
                "warning": warning,
                "date_lower": params.get("activityStartDateLower") if params else None,
                "date_upper": params.get("activityStartDateUpper") if params else None,
            }

            handle = context.store.put(
                kind="samples_results",
                items=records,
                summary=summary,
                sample=sample,
                provenance=provenance,
                count=total_count,
            )

        return DiscreteFetchResult(
            records=records,
            total_count=total_count,
            source_urls=source_urls,
            handle=handle,
            warning=warning,
            partitioned=partitioned,
        )

    def _build_locations_result(
        self,
        records: list[dict[str, Any]],
        total_count: int,
        source_urls: list[str],
        pcode: str | None,
        context: RequestContext | None,
    ) -> DiscreteFetchResult:
        handle = None
        if context is not None:
            if isinstance(pcode, (list, tuple)):
                pcode_str = ", ".join(pcode)
                desc_str = f"family ({pcode_str})"
            else:
                param_obj = get_parameter(pcode) if pcode else None
                pcode_str = pcode or "parameter"
                desc_str = param_obj.descriptor if param_obj else "discrete monitoring"

            summary = (
                f"{total_count:,} discrete monitoring stations for {desc_str} ({pcode_str}). "
                f"Modality: discrete water quality network."
            )

            sample = records[:5]
            provenance = {
                "source_urls": source_urls,
                "pcode": pcode_str,
                "descriptor": desc_str,
                "modality": "discrete",
            }

            handle = context.store.put(
                kind="samples_locations",
                items=records,
                summary=summary,
                sample=sample,
                provenance=provenance,
                count=total_count,
            )

        return DiscreteFetchResult(
            records=records,
            total_count=total_count,
            source_urls=source_urls,
            handle=handle,
        )
