from water_agent.metadata.resolver import ReferenceResolver
from water_agent.metadata.discrete_families import (
    resolve_discrete_parameter,
    get_parameter,
)


class ReferenceSearchTool:
    def __init__(self, resolver: ReferenceResolver):
        self.resolver = resolver

    def search(self, field_name: str, query: str) -> list[dict]:
        field_name = getattr(self.resolver, "FIELD_ALIASES", {}).get(field_name, field_name)
        # 1. Intercept parameter fields to consult discrete parameter families
        if field_name in ("parameter_code", "parameter", "characteristic", "usgsPCode"):
            q_clean = query.strip()
            # Canonical continuous hydrologic parameters
            q_lower = q_clean.lower()
            if q_lower in ("streamflow", "stream flow", "discharge", "flow", "stream_flow", "daily discharge", "mean discharge"):
                return [
                    {
                        "matched_field": "parameter_name",
                        "matched_value": "Discharge / Streamflow (ft3/s)",
                        "resolved_value": "00060",
                    },
                    {
                        "matched_field": "parameter_name",
                        "matched_value": "Gage height (ft)",
                        "resolved_value": "00065",
                    },
                ]
            if q_lower in ("gage height", "gage_height", "gauge height", "stage", "water level"):
                return [
                    {
                        "matched_field": "parameter_name",
                        "matched_value": "Gage height (ft)",
                        "resolved_value": "00065",
                    },
                    {
                        "matched_field": "parameter_name",
                        "matched_value": "Discharge / Streamflow (ft3/s)",
                        "resolved_value": "00060",
                    },
                ]

            # If query is an exact 5-digit pcode
            if q_clean.isdigit() and len(q_clean) == 5:
                p_obj = get_parameter(q_clean)
                if p_obj:
                    return [
                        {
                            "matched_field": "parameter_code",
                            "matched_value": f"{p_obj.descriptor} ({p_obj.unit_of_measure})",
                            "resolved_value": p_obj.pcode,
                        }
                    ]

            discrete_res = resolve_discrete_parameter(q_clean, query_type="value")
            if discrete_res.status == "resolved" and discrete_res.family:
                params = sorted(
                    discrete_res.family.parameters.values(),
                    key=lambda p: p.verified_records_nc,
                    reverse=True,
                )
                return [
                    {
                        "matched_field": "descriptor",
                        "matched_value": (
                            f"{p.descriptor} ({p.unit_of_measure}) - {p.verified_records_nc:,} NC records"
                            if p.verified_records_nc > 0
                            else f"{p.descriptor} ({p.unit_of_measure})"
                        ),
                        "resolved_value": p.pcode,
                    }
                    for p in params
                ]

        # 2. General ReferenceStore lookup (for daily continuous parameters and other fields)
        reference = self.resolver.get_reference(field_name)
        matches = self.resolver.store.search(reference, query)

        if matches:
            return [
                {
                    "matched_field": match["matched_field"],
                    "matched_value": match["matched_value"],
                    "resolved_value": match["resolved_value"],
                }
                for match in matches
            ]

        # 3. Parameters outside verified families (Step 3b, e.g. mercury)
        if field_name in ("parameter_code", "parameter", "characteristic", "usgsPCode"):
            discrete_res = resolve_discrete_parameter(query.strip(), query_type="value")
            if discrete_res.status == "clarification_needed" and discrete_res.candidates:
                return [
                    {
                        "matched_field": "parameter_code",
                        "matched_value": discrete_res.message,
                        "clarification_needed": discrete_res.message,
                    }
                ]

        return []

