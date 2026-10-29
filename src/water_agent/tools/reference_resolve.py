from water_agent.metadata.resolver import ReferenceResolver


class ReferenceResolveTool:
    def __init__(self, resolver: ReferenceResolver):
        self.resolver = resolver

    def resolve(self, field_name: str, value: str, query_type: str = "value") -> dict:
        result = self.resolver.resolve(field_name, value, query_type=query_type)
        data = result.model_dump()
        clean = {
            "status": data["status"],
            "input_value": data["input_value"],
            "resolved_value": data["resolved_value"],
            "matched_field": data["matched_field"],
            "candidates": data["candidates"],
        }
        for k in ["resolved_values", "action", "dominant_value", "unit_of_measure", "modality", "message"]:
            if data.get(k):
                clean[k] = data[k]
        return clean

