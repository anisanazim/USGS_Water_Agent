import json
from pathlib import Path

from water_agent.metadata.models import parse_queryable


QUERYABLES_PATH = Path(
    "metadata/raw/daily/queryables.json"
)


def test_parse_queryables():
    with QUERYABLES_PATH.open(
        "r",
        encoding="utf-8",
    ) as file:
        data = json.load(file)

    properties = data["properties"]

    parsed = [
        parse_queryable(name, definition)
        for name, definition in properties.items()
    ]

    assert len(parsed) > 0

    print("\nDaily Queryables:")
    print("-" * 80)

    for queryable in parsed:
        print(f"\nNAME: {queryable.name}")
        print(f"TITLE: {queryable.title}")
        print(f"TYPE: {queryable.data_type}")
        print(f"FORMAT: {queryable.format}")
        print(f"ROLE: {queryable.ogc_role}")
        print(f"DESCRIPTION: {queryable.description}")

    print("\nTotal queryables:", len(parsed))