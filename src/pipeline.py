from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from usgs_water_data import USGSNotFoundError, USGSServiceError, USGSValidationError

PARAMETER_ALIASES = {
    "discharge": "00060",
    "streamflow": "00060",
    "stream flow": "00060",
    "gage height": "00065",
    "gauge height": "00065",
    "stage": "00065",
    "water temperature": "00010",
    "water temp": "00010",
    "temperature": "00010",
    "temp": "00010",
    "barometric pressure": "00025",
    "air pressure": "00025",
    "pressure": "00025",
    "dissolved oxygen": "00300",
    "specific conductance": "00095",
    "conductivity": "00095",
    "turbidity": "63680",
    "nitrate": "00618",
    "Total Nitrogen": "00601",
    "ph": "00400",
}

STATE_ALIASES = {
    "alabama": "Alabama", "alaska": "Alaska", "arizona": "Arizona",
    "arkansas": "Arkansas", "california": "California", "colorado": "Colorado",
    "connecticut": "Connecticut", "delaware": "Delaware", "florida": "Florida",
    "georgia": "Georgia", "hawaii": "Hawaii", "idaho": "Idaho",
    "illinois": "Illinois", "indiana": "Indiana", "iowa": "Iowa",
    "kansas": "Kansas", "kentucky": "Kentucky", "louisiana": "Louisiana",
    "maine": "Maine", "maryland": "Maryland", "massachusetts": "Massachusetts",
    "michigan": "Michigan", "minnesota": "Minnesota", "mississippi": "Mississippi",
    "missouri": "Missouri", "montana": "Montana", "nebraska": "Nebraska",
    "nevada": "Nevada", "new hampshire": "New Hampshire", "new jersey": "New Jersey",
    "new mexico": "New Mexico", "new york": "New York", "north carolina": "North Carolina",
    "north dakota": "North Dakota", "ohio": "Ohio", "oklahoma": "Oklahoma",
    "oregon": "Oregon", "pennsylvania": "Pennsylvania", "rhode island": "Rhode Island",
    "south carolina": "South Carolina", "south dakota": "South Dakota",
    "tennessee": "Tennessee", "texas": "Texas", "utah": "Utah", "vermont": "Vermont",
    "virginia": "Virginia", "washington": "Washington", "west virginia": "West Virginia",
    "wisconsin": "Wisconsin", "wyoming": "Wyoming",
    "district of columbia": "District of Columbia", "washington dc": "District of Columbia",
    "puerto rico": "Puerto Rico",
}

ABBREVIATION_ALIASES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia", "PR": "Puerto Rico",
}

@dataclass
class StructuredQuery:
    site_id: str | None = None
    state_name: str | None = None
    parameter_codes: list[str] = field(default_factory=lambda: ["00060"])
    parameter_names: list[str] = field(default_factory=lambda: ["discharge"])
    raw_request: str = ""

    @property
    def parameter_code(self) -> str:
        """First/primary parameter code. Kept for backward compatibility
        with callers that only deal with a single parameter."""
        return self.parameter_codes[0] if self.parameter_codes else "00060"

    @property
    def parameter_name(self) -> str:
        return self.parameter_names[0] if self.parameter_names else "discharge"

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def as_dict(self) -> dict[str, Any]:
        return {
            "site_id": self.site_id,
            "state_name": self.state_name,
            "parameter_codes": self.parameter_codes,
            "parameter_names": self.parameter_names,
            "raw_request": self.raw_request,
        }


class WaterQueryParser:
    def parse(self, request: str) -> StructuredQuery:
        text = (request or "").strip()
        resolved_params = self._resolve_parameters(text)
        state_name = self._resolve_state(text)
        site_id = self._resolve_site_id(text)

        structured_query = StructuredQuery(
            site_id=site_id,
            state_name=state_name,
            parameter_codes=[code for code, _ in resolved_params],
            parameter_names=[name for _, name in resolved_params],
            raw_request=text,
        )
        # print(f"[DEBUG][Parser] Parsed query -> site_id={site_id}, state_name={state_name}, params={structured_query.parameter_codes}, names={structured_query.parameter_names}")
        return structured_query

    def _resolve_parameters(self, request_text: str) -> list[tuple[str, str]]:
        """Return every distinct parameter mentioned in the text, ordered by
        where they first appear (leftmost first), longest-alias-wins on
        overlapping matches at the same position, deduplicated by code.
        Falls back to discharge if nothing is recognized.
        """
        lowered = (request_text or "").lower()
        matches: list[tuple[int, int, str, str]] = []  # (start, -len, code, alias)

        for alias, code in PARAMETER_ALIASES.items():
            pattern = r"\b" + re.escape(alias) + r"\b"
            for m in re.finditer(pattern, lowered):
                matches.append((m.start(), -len(alias), code, alias))

        matches.sort()

        resolved: list[tuple[str, str]] = []
        seen_codes: set[str] = set()
        for _, _, code, alias in matches:
            if code in seen_codes:
                continue
            seen_codes.add(code)
            resolved.append((code, alias))

        return resolved or [("00060", "discharge")]

    def _resolve_state(self, request_text: str) -> str | None:
        """Match full state names case-insensitively (leftmost/longest wins,
        so "New York" beats a partial). Two-letter postal abbreviations are
        only matched case-sensitively against the ORIGINAL text, since
        lowercased abbreviations like "in", "or", "hi", "me" are common
        English words and would otherwise cause false positives."""
        text = request_text or ""
        lowered = text.lower()
        best: tuple[int, int, str] | None = None  # (start, -len, state_name)

        for alias, state_name in STATE_ALIASES.items():
            pattern = r"\b" + re.escape(alias) + r"\b"
            for match in re.finditer(pattern, lowered):
                candidate = (match.start(), -len(alias), state_name)
                if best is None or candidate[:2] < best[:2]:
                    best = candidate

        if best is not None:
            return best[2]

        for abbr, state_name in ABBREVIATION_ALIASES.items():
            pattern = r"\b" + re.escape(abbr) + r"\b"
            match = re.search(pattern, text)  # case-sensitive, un-lowered text
            if match:
                return state_name

        return None

    def _resolve_site_id(self, request_text: str) -> str | None:
        """USGS site numbers are 8-15 digits. We require a word boundary and
        a minimum of 8 digits (rather than 5) to avoid false-matching things
        like zip codes, years, or arbitrary counts mentioned in a sentence."""
        site_match = re.search(r"\bUSGS-\d{8,15}\b", request_text or "", re.IGNORECASE)
        if not site_match:
            site_match = re.search(r"\b\d{8,15}\b", request_text or "")
        if not site_match:
            return None
        site_id = site_match.group(0)
        if site_id.upper().startswith("USGS-"):
            return site_id.upper()
        return site_id


class ParameterResolver:
    """Ensures a structured query always has at least one parameter, and
    normalizes/deduplicates parameter codes regardless of whether they came
    from the NL parser or were supplied directly by an API caller."""

    def resolve(self, structured_query: StructuredQuery) -> StructuredQuery:
        normalized: list[str] = []
        seen: set[str] = set()
        for code in structured_query.parameter_codes or []:
            code = str(code).strip()
            if code and code not in seen:
                seen.add(code)
                normalized.append(code)

        if not normalized:
            structured_query.parameter_codes = ["00060"]
            structured_query.parameter_names = ["discharge"]
            return structured_query

        structured_query.parameter_codes = normalized
        if len(structured_query.parameter_names) != len(normalized):
            structured_query.parameter_names = list(normalized)
        return structured_query


class USGSRequestRouter:
    def __init__(self, fetcher):
        self.fetcher = fetcher

    def build_request(self, structured_query: StructuredQuery) -> dict[str, Any]:
        request_params: dict[str, Any] = {"f": "json", "limit": 10}

        if structured_query.site_id:
            request_params["monitoring_location_id"] = self.fetcher.normalize_site_id(structured_query.site_id)

        if structured_query.state_name:
            request_params["state_name"] = structured_query.state_name

        if structured_query.parameter_codes:
            request_params["parameter_codes"] = list(structured_query.parameter_codes)

        print(f"[DEBUG][Router] Built USGS request params: {request_params}")
        return request_params


class USGSDataExecutor:
    def __init__(self, fetcher):
        self.fetcher = fetcher

    async def execute(self, request_params: dict[str, Any]) -> dict[str, Any]:
        parameter_codes = request_params.get("parameter_codes") or None
        limit = request_params.get("limit", 10)

        print(f"[DEBUG][Executor] Starting execution with params={request_params}")

        if "monitoring_location_id" in request_params:
            print(f"[DEBUG][Executor] Direct site query for site_id={request_params['monitoring_location_id']}, parameter_codes={parameter_codes}, limit={limit}")
            result = await self.fetcher.fetch_latest_continuous(
                site_id=request_params["monitoring_location_id"],
                parameter_codes=parameter_codes,
                limit=limit,
            )
            print(f"[DEBUG][Executor] Direct site query returned {result.get('count')} features")
            return result

        if "state_name" in request_params:
            state_name = request_params["state_name"]
            print(f"[DEBUG][Executor] State-based lookup for '{state_name}'")
            locations = await self.fetcher.fetch_monitoring_locations_by_state(state_name, limit=5)
            if not locations:
                raise USGSNotFoundError(f"No monitoring locations found for state '{state_name}'")

            first_location = locations[0]
            props = first_location.get("properties", {})
            site_id = props.get("id") or props.get("monitoring_location_id") or props.get("monitoring_location_number")
            if not site_id:
                raise USGSServiceError(
                    f"Found a monitoring location in '{state_name}' but it had no usable site identifier"
                )

            print(f"[DEBUG][Executor] Selected state site_id={site_id} from {len(locations)} monitoring locations")
            result = await self.fetcher.fetch_latest_continuous(
                site_id=site_id,
                parameter_codes=parameter_codes,
                limit=limit,
            )
            result["selected_from_state"] = state_name
            result["stations_considered"] = len(locations)
            print(f"[DEBUG][Executor] State-based query returned {result.get('count')} features")
            return result

        raise USGSValidationError("No valid site or state was provided for the USGS request")


class USGSResponseNormalizer:
    def normalize(self, raw_response: dict[str, Any], structured_query: StructuredQuery) -> tuple[dict[str, Any], str]:
        features = raw_response.get("features", [])
        site_id = raw_response.get("site_id") or structured_query.site_id
        print(f"[DEBUG][Normalizer] Raw response received: site_id={site_id}, feature_count={len(features)}")

        if not features:
            summary = f"No USGS latest-continuous data was returned for site {site_id or '(none resolved)'}."
            artifact_payload = {
                "site_id": site_id,
                "requested_parameter_codes": structured_query.parameter_codes,
                "readings": [],
                "count": 0,
            }
            print(f"[DEBUG][Normalizer] No features found; summary={summary}")
            return artifact_payload, summary

        readings = []
        for feature in features:
            props = feature.get("properties", {})
            readings.append({
                "parameter_code": props.get("parameter_code"),
                "value": props.get("value"),
                "unit": props.get("unit_of_measure"),
                "time": props.get("time"),
                "qualifiers": props.get("qualifier"),
            })

        summary_lines = []
        if "selected_from_state" in raw_response:
            summary_lines.append(
                f"Selected site {site_id} (1 of {raw_response.get('stations_considered', '?')} "
                f"stations found in {raw_response['selected_from_state']}):"
            )
        else:
            summary_lines.append(f"Latest reading(s) for site {site_id}:")

        for reading in readings:
            unit = reading["unit"] or ""
            summary_lines.append(
                f"  - {reading['parameter_code']}: {reading['value']} {unit} (as of {reading['time']})".rstrip()
            )

        artifact_payload = {
            "site_id": site_id,
            "requested_parameter_codes": structured_query.parameter_codes,
            "readings": readings,
            "count": len(readings),
        }
        return artifact_payload, "\n".join(summary_lines)