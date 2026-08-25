from pathlib import Path
import json

from water_agent.metadata.models import ReferenceConfig


def _matches_filters(properties: dict, filters: dict[str, str]) -> bool:
    return all(properties.get(key) == value for key, value in filters.items())


class ReferenceStore:
    def __init__(self, references_dir: Path):
        self.references_dir = references_dir
        self._cache: dict[str, dict] = {}
        self._indexes: dict[str, dict[str, dict[str, list[dict]]]] = {}

    def load(self, collection: str) -> dict:
        if collection in self._cache:
            return self._cache[collection]

        path = self.references_dir / f"{collection}.json"

        if not path.exists():
            raise FileNotFoundError(
                f"Reference data not found: {collection}"
            )

        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)

        self._cache[collection] = data

        return data

    @staticmethod
    def _index_key(reference: ReferenceConfig) -> str:
        filters_key = ",".join(
            f"{key}={value}" for key, value in sorted(reference.filters.items())
        )
        return f"{reference.collection}|{filters_key}"

    def _build_index(
        self,
        reference: ReferenceConfig,
    ) -> None:
        index_key = self._index_key(reference)

        if index_key in self._indexes:
            return

        data = self.load(reference.collection)

        collection_indexes: dict[str, dict[str, list[dict]]] = {}

        for field in reference.search_fields:
            field_index: dict[str, list[dict]] = {}

            for feature in data.get("features", []):
                properties = feature.get("properties", {})

                if not _matches_filters(properties, reference.filters):
                    continue

                value = properties.get(field)

                if value is None:
                    continue

                normalized_value = str(value).casefold().strip()

                field_index.setdefault(
                    normalized_value,
                    [],
                ).append(feature)

            collection_indexes[field] = field_index

        self._indexes[index_key] = collection_indexes

    def find(
        self,
        reference: ReferenceConfig,
        field: str,
        value: str,
    ) -> list[dict]:
        if field not in reference.search_fields:
            raise ValueError(
                f"Field '{field}' is not configured as a search field "
                f"for collection '{reference.collection}'"
            )

        self._build_index(reference)

        normalized_value = str(value).casefold().strip()

        return self._indexes[
            self._index_key(reference)
        ][field].get(normalized_value, [])

    def search(
        self,
        reference: ReferenceConfig,
        query: str,
    ) -> list[dict]:
        """Free-text substring search across all of a reference's
        search_fields, for discovering candidate values (e.g. "streamflow"
        -> the parameter-codes entry for 00060). Unlike `find`, this does
        not require an exact, single-field match."""
        normalized_query = str(query).casefold().strip()

        if not normalized_query:
            return []

        data = self.load(reference.collection)

        matches = []
        seen_features: set[int] = set()

        for feature in data.get("features", []):
            if id(feature) in seen_features:
                continue

            properties = feature.get("properties", {})

            if not _matches_filters(properties, reference.filters):
                continue

            for field in reference.search_fields:
                value = properties.get(field)

                if value is None:
                    continue

                if normalized_query in str(value).casefold():
                    seen_features.add(id(feature))
                    matches.append(
                        {
                            "matched_field": field,
                            "matched_value": value,
                            "resolved_value": str(
                                properties[reference.value_field]
                            ),
                            "properties": properties,
                        }
                    )
                    break

        return matches