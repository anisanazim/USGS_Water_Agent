from water_agent.metadata.registry import daily_registry
from water_agent.tools.queryable_search import QueryableSearchTool


def create_tool():
    return QueryableSearchTool(daily_registry)


def test_search_matches_field_name():
    tool = create_tool()

    results = tool.search("parameter code")

    names = {result["name"] for result in results}
    assert "parameter_code" in names


def test_search_matches_title():
    tool = create_tool()

    results = tool.search("state")

    names = {result["name"] for result in results}
    assert "state_code" in names
    assert "state_name" in names


def test_search_no_matches():
    tool = create_tool()

    assert tool.search("not a real field at all") == []


def test_search_result_shape():
    tool = create_tool()

    results = tool.search("state_code")

    assert len(results) == 1
    result = results[0]
    assert result["name"] == "state_code"
    assert result["semantic_type"] == "controlled_reference"
