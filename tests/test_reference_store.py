from pathlib import Path
from water_agent.metadata.registry import daily_registry
from water_agent.metadata.references.store import ReferenceStore

def test_find_state():
    references_dir = (
        Path(__file__).parent.parent
        / "src"
        / "water_agent"
        / "metadata"
        / "references"
    )

    store = ReferenceStore(references_dir)

    queryable = daily_registry.get("state_code")

    assert queryable is not None
    assert queryable.reference is not None

    matches = store.find(
        reference=queryable.reference,
        field="state_name",
        value="North Carolina",
    )

    assert len(matches) >= 1

    properties = matches[0]["properties"]

    assert properties["state_name"] == "North Carolina"
    assert properties["state_fips_code"] == "37"

def test_find_uses_index():
    references_dir = (
        Path(__file__).parent.parent
        / "src"
        / "water_agent"
        / "metadata"
        / "references"
    )

    store = ReferenceStore(references_dir)

    queryable = daily_registry.get("state_code")

    assert queryable is not None
    assert queryable.reference is not None

    reference = queryable.reference

    matches = store.find(
        reference=reference,
        field="state_name",
        value="North Carolina",
    )

    assert len(matches) >= 1

    assert "state-identifiers" in store._indexes
    assert "state_name" in store._indexes["state-identifiers"]

    assert "north carolina" in store._indexes[
        "state-identifiers"
    ]["state_name"]


def test_find_state_by_postal_code():
    references_dir = (
        Path(__file__).parent.parent
        / "src"
        / "water_agent"
        / "metadata"
        / "references"
    )

    store = ReferenceStore(references_dir)

    queryable = daily_registry.get("state_code")

    assert queryable is not None
    assert queryable.reference is not None

    matches = store.find(
        reference=queryable.reference,
        field="state_postal_code",
        value="NC",
    )

    assert len(matches) >= 1
    assert matches[0]["properties"]["state_fips_code"] == "37"