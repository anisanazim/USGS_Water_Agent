from pathlib import Path

from water_agent.metadata.references.store import ReferenceStore
from water_agent.metadata.registry import daily_registry
from water_agent.metadata.resolver import ReferenceResolver
from water_agent.tools.reference_search import ReferenceSearchTool


def create_tool():
    references_dir = (
        Path(__file__).parent.parent
        / "metadata"
        / "raw"
        / "references"
    )

    store = ReferenceStore(references_dir)
    resolver = ReferenceResolver(registry=daily_registry, store=store)

    return ReferenceSearchTool(resolver)


def test_search_matches_state_name_substring():
    tool = create_tool()

    results = tool.search("state_code", "carolina")

    names = {result["matched_value"] for result in results}
    assert "North Carolina" in names
    assert "South Carolina" in names


def test_search_returns_resolved_value():
    tool = create_tool()

    results = tool.search("state_code", "North Carolina")

    assert len(results) == 1
    assert results[0]["resolved_value"] == "37"
    assert results[0]["matched_field"] == "state_name"


def test_search_no_matches():
    tool = create_tool()

    results = tool.search("state_code", "Not A Real Place")

    assert results == []


def test_search_unknown_field_raises():
    tool = create_tool()

    try:
        tool.search("does_not_exist", "anything")
        assert False, "expected ValueError"
    except ValueError:
        pass
