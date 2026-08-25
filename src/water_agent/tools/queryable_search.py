from water_agent.metadata.registry import QueryableRegistry

class QueryableSearchTool:
    def __init__(self, registry: QueryableRegistry):
        self.registry = registry

    def search(self, query: str) -> list[dict]:
        query = query.casefold().strip()

        results = []

        for queryable in self.registry.all().values():
            searchable_text = " ".join(
                value
                for value in [
                    queryable.name,
                    queryable.title,
                    queryable.description,
                ]
                if value
            ).casefold()

            if query in searchable_text:
                results.append(
                    {
                        "name": queryable.name,
                        "title": queryable.title,
                        "description": queryable.description,
                        "semantic_type": queryable.semantic_type,
                    }
                )

        return results