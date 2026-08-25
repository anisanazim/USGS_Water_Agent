from water_agent.metadata.resolver import ReferenceResolver


class ReferenceSearchTool:
    def __init__(self, resolver: ReferenceResolver):
        self.resolver = resolver

    def search(self, field_name: str, query: str) -> list[dict]:
        reference = self.resolver.get_reference(field_name)
        matches = self.resolver.store.search(reference, query)

        return [
            {
                "matched_field": match["matched_field"],
                "matched_value": match["matched_value"],
                "resolved_value": match["resolved_value"],
            }
            for match in matches
        ]
