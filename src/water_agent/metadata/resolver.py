from __future__ import annotations
from typing import Any, Literal

from water_agent.metadata.models import ResolutionResult
from water_agent.metadata.registry import QueryableRegistry
from water_agent.metadata.references.store import ReferenceStore
from water_agent.metadata.discrete_families import resolve_discrete_parameter


class ReferenceResolver:
    FIELD_ALIASES = {
        "county_name": "county_code",
        "county": "county_code",
        "state_name": "state_code",
        "state": "state_code",
        "parameter": "parameter_code",
        "characteristic": "parameter_code",
        "usgsPCode": "parameter_code",
        "statistic_code": "statistic_id",
        "statistic": "statistic_id",
        "station": "monitoring_location_id",
        "station_id": "monitoring_location_id",
        "site_id": "monitoring_location_id",
        "monitoring_location": "monitoring_location_id",
    }

    def __init__(
        self,
        registry: QueryableRegistry,
        store: ReferenceStore,
    ):
        self.registry = registry
        self.store = store

    def get_reference(self, field_name: str):
        canonical_field = self.FIELD_ALIASES.get(field_name, field_name)
        queryable = self.registry.get(canonical_field)

        if queryable is None:
            raise ValueError(
                f"Unknown queryable field: {field_name}"
            )

        if queryable.reference is None:
            raise ValueError(
                f"Queryable '{field_name}' does not have "
                "a reference configuration"
            )

        return queryable.reference

    def resolve(
        self,
        field_name: str,
        value: str,
        query_type: Literal["spatial", "value", "count"] = "value",
        medium: str = "Water",
    ) -> ResolutionResult:
        field_name = self.FIELD_ALIASES.get(field_name, field_name)

        # Direct station/location identifier passthrough
        if field_name in ("monitoring_location_id", "station", "station_id", "site_id"):
            val_clean = value.strip()
            return ResolutionResult(
                status="resolved",
                input_value=value,
                resolved_value=val_clean,
                matched_field="monitoring_location_id",
            )

        # Statistic resolution: map temporal 'latest'/'current' to mean daily statistic 00003
        if field_name in ("statistic_id", "statistic", "stat_id"):
            val_clean = value.strip().lower()
            if val_clean in ("latest", "current", "most recent", "recent", "today", "now"):
                return ResolutionResult(
                    status="resolved",
                    input_value=value,
                    resolved_value="00003",
                    matched_field="statistic_name",
                )

        # County resolution: handle both raw name ('Wake') and with suffix ('Wake County')
        if field_name == "county_code":
            ref = self.get_reference("county_code")
            val_clean = value.strip()
            cands = [val_clean]
            if not val_clean.lower().endswith("county"):
                cands.append(f"{val_clean} County")
            elif val_clean.lower().endswith(" county"):
                cands.append(val_clean[:-7].strip())

            for cand in cands:
                for search_field in ref.search_fields:
                    matches = self.store.find(
                        reference=ref,
                        field=search_field,
                        value=cand,
                    )
                    if len(matches) == 1:
                        props = matches[0]["properties"]
                        return ResolutionResult(
                            status="resolved",
                            input_value=value,
                            resolved_value=str(props[ref.value_field]),
                            matched_field=search_field,
                        )

        # 1. Discrete family resolution check for parameter fields
        if field_name in ("parameter_code", "parameter", "characteristic", "usgsPCode"):
            val_clean = value.strip().lower()
            if val_clean in ("streamflow", "stream flow", "discharge", "streamflow (discharge)", "mean discharge", "daily discharge"):
                return ResolutionResult(
                    status="resolved",
                    input_value=value,
                    resolved_value="00060",
                    matched_field="parameter_name",
                    modality="continuous",
                )
            if val_clean in ("gage height", "gage_height", "gauge height", "stage", "water level"):
                return ResolutionResult(
                    status="resolved",
                    input_value=value,
                    resolved_value="00065",
                    matched_field="parameter_name",
                    modality="continuous",
                )

            discrete_res = resolve_discrete_parameter(value, query_type=query_type)
            if discrete_res.status == "resolved":
                return ResolutionResult(
                    status="resolved",
                    input_value=value,
                    resolved_value=discrete_res.pcodes[0] if len(discrete_res.pcodes) == 1 else None,
                    resolved_values=discrete_res.pcodes,
                    modality="discrete",
                    message=discrete_res.message,
                )
        # 2. General ReferenceStore lookup
        reference = self.get_reference(field_name)

        for search_field in reference.search_fields:
            matches = self.store.find(
                reference=reference,
                field=search_field,
                value=value,
            )

            if len(matches) == 1:
                properties = matches[0]["properties"]
                return ResolutionResult(
                    status="resolved",
                    input_value=value,
                    resolved_value=str(
                        properties[reference.value_field]
                    ),
                    matched_field=search_field,
                )

            if len(matches) > 1:
                # Disambiguation: check if filtering by medium (default "Water") resolves ambiguity
                if "medium" in matches[0].get("properties", {}):
                    water_matches = [
                        m
                        for m in matches
                        if str(m.get("properties", {}).get("medium", "")).lower() == medium.lower()
                    ]
                    if len(water_matches) == 1:
                        properties = water_matches[0]["properties"]
                        return ResolutionResult(
                            status="resolved",
                            input_value=value,
                            resolved_value=str(
                                properties[reference.value_field]
                            ),
                            matched_field=search_field,
                        )
                    if len(water_matches) > 1:
                        matches = water_matches

                return ResolutionResult(
                    status="ambiguous",
                    input_value=value,
                    matched_field=search_field,
                    candidates=[
                        match["properties"]
                        for match in matches
                    ],
                )

        # 3. Parameters outside verified families (Step 3b, e.g. mercury)
        if field_name in ("parameter_code", "parameter", "characteristic", "usgsPCode"):
            discrete_res = resolve_discrete_parameter(value, query_type=query_type)
            if discrete_res.status == "clarification_needed":
                return ResolutionResult(
                    status="clarification_needed",
                    input_value=value,
                    action="clarify",
                    candidates=discrete_res.candidates or [],
                    modality="discrete",
                    message=discrete_res.message,
                )

        return ResolutionResult(
            status="not_found",
            input_value=value,
        )