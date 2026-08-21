from water_agent.metadata.models import ResolutionResult
from water_agent.metadata.registry import QueryableRegistry
from water_agent.metadata.references.store import ReferenceStore


class ReferenceResolver:
    def __init__(
        self,
        registry: QueryableRegistry,
        store: ReferenceStore,
    ):
        self.registry = registry
        self.store = store

    def get_reference(self, field_name: str):
        queryable = self.registry.get(field_name)

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
    ) -> ResolutionResult:

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
                return ResolutionResult(
                    status="ambiguous",
                    input_value=value,
                    matched_field=search_field,
                    candidates=[
                        match["properties"]
                        for match in matches
                    ],
                )

        return ResolutionResult(
            status="not_found",
            input_value=value,
        )