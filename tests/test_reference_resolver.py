from pathlib import Path

from water_agent.metadata.models import ResolutionResult
from water_agent.metadata.references.store import ReferenceStore
from water_agent.metadata.registry import daily_registry
from water_agent.metadata.resolver import ReferenceResolver


def create_resolver():
    references_dir = (
        Path(__file__).parent.parent
        / "src"
        / "water_agent"
        / "metadata"
        / "references"
    )

    store = ReferenceStore(references_dir)

    return ReferenceResolver(
        registry=daily_registry,
        store=store,
    )


def test_resolve_state_name():
    resolver = create_resolver()

    result = resolver.resolve(
        "state_code",
        "North Carolina",
    )

    assert result.status == "resolved"
    assert result.resolved_value == "37"
    assert result.matched_field == "state_name"


def test_resolve_state_postal_code():
    resolver = create_resolver()

    result = resolver.resolve(
        "state_code",
        "NC",
    )

    assert result.status == "resolved"
    assert result.resolved_value == "37"
    assert result.matched_field == "state_postal_code"


def test_unknown_value():
    resolver = create_resolver()

    result = resolver.resolve(
        "state_code",
        "Not A Real State",
    )

    assert result.status == "not_found"
    assert result.input_value == "Not A Real State"



def test_resolution_result():
    result = ResolutionResult(
        status="resolved",
        input_value="North Carolina",
        resolved_value="37",
        matched_field="state_name",
    )

    assert result.status == "resolved"
    assert result.resolved_value == "37"
    assert result.matched_field == "state_name"

def test_ambiguous_reference():
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

    # "Georgia" is intentionally ambiguous in this test setup.
    # We will create a temporary index entry with two matches.
    reference = queryable.reference

    store._indexes[reference.collection] = {
        "state_name": {
            "georgia": [
                {
                    "properties": {
                        "state_name": "Georgia",
                        "state_fips_code": "13",
                    }
                },
                {
                    "properties": {
                        "state_name": "Georgia",
                        "state_fips_code": "GA",
                    }
                },
            ]
        }
    }

    resolver = ReferenceResolver(
        registry=daily_registry,
        store=store,
    )

    result = resolver.resolve(
        "state_code",
        "Georgia",
    )

    assert result.status == "ambiguous"
    assert result.input_value == "Georgia"
    assert len(result.candidates) == 2