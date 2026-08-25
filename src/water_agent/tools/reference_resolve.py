from water_agent.metadata.resolver import ReferenceResolver


class ReferenceResolveTool:
    def __init__(self, resolver: ReferenceResolver):
        self.resolver = resolver

    def resolve(self, field_name: str, value: str) -> dict:
        result = self.resolver.resolve(field_name, value)
        return result.model_dump()
