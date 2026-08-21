from water_agent.metadata.registry import daily_registry

def test_get_queryable():
    field = daily_registry.get("state_code")

    assert field is not None
    assert field.semantic_type == "controlled_reference"


def test_state_reference():
    field = daily_registry.get("state_code")

    assert field is not None
    assert field.reference is not None

    assert field.reference.collection == "state-identifiers"
    assert field.reference.value_field == "state_fips_code"


def test_unknown_queryable():
    assert daily_registry.get("does_not_exist") is None