import json
from pathlib import Path

from water_agent.metadata.daily import DAILY_FIELD_CONFIG
from water_agent.metadata.models import QueryableDefinition, parse_queryable

DAILY_QUERYABLES_PATH = (
    Path(__file__).resolve().parents[3]
    / "metadata"
    / "raw"
    / "daily"
    / "queryables.json"
)


class QueryableRegistry:
    def __init__(self, field_definitions: dict[str, QueryableDefinition]):
        self._fields = field_definitions

    def get(self, field_name: str) -> QueryableDefinition | None:
        return self._fields.get(field_name)

    def has(self, field_name: str) -> bool:
        return field_name in self._fields

    def all(self) -> dict[str, QueryableDefinition]:
        return self._fields.copy()


def _load_daily_field_definitions() -> dict[str, QueryableDefinition]:
    """Parse the raw Daily queryables (name/title/description) and overlay
    the hand-configured semantic_type/reference for fields that have one,
    so search_queryables() has text to match against for every field, not
    just the ones with a controlled reference."""
    with DAILY_QUERYABLES_PATH.open("r", encoding="utf-8") as file:
        data = json.load(file)

    definitions: dict[str, QueryableDefinition] = {}

    for name, raw_definition in data["properties"].items():
        definition = parse_queryable(name, raw_definition)

        config = DAILY_FIELD_CONFIG.get(name)
        if config is not None:
            definition.semantic_type = config.semantic_type
            definition.reference = config.reference

        definitions[name] = definition

    return definitions


daily_registry = QueryableRegistry(_load_daily_field_definitions())