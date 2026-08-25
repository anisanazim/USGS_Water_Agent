from pathlib import Path

from water_agent.metadata.references.store import ReferenceStore
from water_agent.metadata.registry import daily_registry
from water_agent.metadata.resolver import ReferenceResolver
from water_agent.tools.reference_resolve import ReferenceResolveTool


def create_tool():
    references_dir = (
        Path(__file__).parent.parent
        / "metadata"
        / "raw"
        / "references"
    )

    store = ReferenceStore(references_dir)
    resolver = ReferenceResolver(registry=daily_registry, store=store)

    return ReferenceResolveTool(resolver)


def test_resolve_returns_plain_dict():
    tool = create_tool()

    result = tool.resolve("state_code", "North Carolina")

    assert result == {
        "status": "resolved",
        "input_value": "North Carolina",
        "resolved_value": "37",
        "matched_field": "state_name",
        "candidates": [],
    }
