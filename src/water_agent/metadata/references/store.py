from pathlib import Path
import json

from water_agent.metadata.models import ReferenceConfig


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

    def _build_index(
        self,
        reference: ReferenceConfig,
    ) -> None:
        collection = reference.collection

        if collection in self._indexes:
            return

        data = self.load(collection)

        collection_indexes: dict[str, dict[str, list[dict]]] = {}

        for field in reference.search_fields:
            field_index: dict[str, list[dict]] = {}

            for feature in data.get("features", []):
                properties = feature.get("properties", {})

                value = properties.get(field)

                if value is None:
                    continue

                normalized_value = str(value).casefold().strip()

                field_index.setdefault(
                    normalized_value,
                    [],
                ).append(feature)

            collection_indexes[field] = field_index

        self._indexes[collection] = collection_indexes

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
            reference.collection
        ][field].get(normalized_value, [])