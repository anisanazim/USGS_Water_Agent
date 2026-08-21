from water_agent.metadata.daily import DAILY_FIELD_CONFIG
from water_agent.metadata.models import QueryableConfig


class QueryableRegistry:
    def __init__(self, field_config: dict[str, QueryableConfig]):
        self._fields = field_config

    def get(self, field_name: str) -> QueryableConfig | None:
        return self._fields.get(field_name)

    def has(self, field_name: str) -> bool:
        return field_name in self._fields

    def all(self) -> dict[str, QueryableConfig]:
        return self._fields.copy()


daily_registry = QueryableRegistry(DAILY_FIELD_CONFIG)