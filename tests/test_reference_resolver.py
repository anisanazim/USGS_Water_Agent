from pathlib import Path

from water_agent.metadata.models import ResolutionResult
from water_agent.metadata.references.store import ReferenceStore
from water_agent.metadata.registry import daily_registry
from water_agent.metadata.resolver import ReferenceResolver


def create_resolver():
    references_dir = (
        Path(__file__).parent.parent
        / "metadata"
        / "raw"
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


def test_resolve_state_fips_code_is_not_ambiguous():
    """state-identifiers is a global country+subdivision table where
    non-US entries can reuse the same FIPS-style number (e.g. a Dominican
    Republic province also has state_fips_code "37"). The US-only filter
    on state_code's ReferenceConfig should keep this resolvable."""
    resolver = create_resolver()

    result = resolver.resolve(
        "state_code",
        "37",
    )

    assert result.status == "resolved"
    assert result.resolved_value == "37"


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
        / "metadata"
        / "raw"
        / "references"
    )

    store = ReferenceStore(references_dir)

    queryable = daily_registry.get("state_code")

    assert queryable is not None
    assert queryable.reference is not None

    # "Georgia" is intentionally ambiguous in this test setup.
    # We will create a temporary index entry with two matches.
    reference = queryable.reference

    store._indexes[store._index_key(reference)] = {
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


def test_resolve_discrete_calcium_spatial_returns_family_union():
    resolver = create_resolver()
    result = resolver.resolve("parameter_code", "calcium", query_type="spatial")

    assert result.status == "resolved"
    assert result.modality == "discrete"
    assert set(result.resolved_values) == {"00910", "00915", "00916", "00918"}


def test_resolve_discrete_calcium_value_returns_family_pcodes_no_auto_resolve():
    resolver = create_resolver()
    result = resolver.resolve("parameter_code", "calcium", query_type="value")

    assert result.status == "resolved"
    assert result.modality == "discrete"
    assert set(result.resolved_values) == {"00910", "00915", "00916", "00918"}


def test_resolve_discrete_exact_pcode():
    resolver = create_resolver()
    result = resolver.resolve("parameter_code", "00915", query_type="value")

    assert result.status == "resolved"
    assert result.resolved_values == ["00915"]
    assert result.resolved_value == "00915"


def test_resolve_discrete_ph():
    resolver = create_resolver()
    result = resolver.resolve("parameter_code", "ph", query_type="value")

    assert result.status == "resolved"
    assert result.modality == "discrete"
    assert "00400" in result.resolved_values
    assert "00403" in result.resolved_values


def test_resolve_discrete_organic_carbon():
    resolver = create_resolver()
    result = resolver.resolve("parameter_code", "organic carbon", query_type="value")

    assert result.status == "resolved"
    assert result.modality == "discrete"
    assert set(result.resolved_values) == {"00680", "00681"}


def test_resolve_discrete_nitrate_flat_lookup():
    resolver = create_resolver()
    result = resolver.resolve("parameter_code", "nitrate", query_type="value")

    assert result.status == "resolved"
    assert result.modality == "discrete"
    assert len(result.resolved_values) == 13
    assert "00630" in result.resolved_values
    assert "00618" in result.resolved_values


def test_resolve_parameter_medium_disambiguation():
    resolver = create_resolver()
    # Continuous lookup where medium defaults to Water filters out Sediment matches
    result = resolver.resolve("parameter_code", "Total nitrogen, ss", medium="Water")
    assert result.status == "resolved"
    assert result.resolved_value == "00601"