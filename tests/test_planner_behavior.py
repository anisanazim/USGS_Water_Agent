import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock
from water_agent.query_planner import QueryPlanner, QueryPlan
from water_agent.results import RequestContext, ResultStore

def mock_planner():
    # Setup a mock client and planner
    client = MagicMock()
    client.chat.completions.create = AsyncMock()
    planner = QueryPlanner(client=client, model="test-model")
    planner._dispatch = {} # We'll inject tools directly
    return planner, client

@pytest.mark.asyncio
async def test_shared_planner_concurrency():
    planner, _ = mock_planner()
    
    class FakeSpatialTool:
        result_kind = "stations"
        @staticmethod
        def summarize(res): return ("Summary", [{"id": "USGS-123"}], 1)
        
        async def __call__(self, arg):
            await asyncio.sleep(0.01) # Force interleaving
            return {"stations": ["USGS-123"]}
            
    planner._dispatch["spatial_query"] = FakeSpatialTool()
    
    ctx_a = RequestContext("req-A")
    ctx_b = RequestContext("req-B")
    
    # We call _call_tool directly to simulate concurrency
    async def run_req(ctx, arg):
        return await planner._call_tool("spatial_query", {"arg": arg}, ctx)
        
    res_a, res_b = await asyncio.gather(
        run_req(ctx_a, "A"),
        run_req(ctx_b, "B")
    )
    
    ref_a = res_a["ref"]
    ref_b = res_b["ref"]
    
    assert ctx_a.store.get(ref_a, "stations")
    with pytest.raises(Exception):
        ctx_b.store.get(ref_a, "stations")
        
    assert ctx_b.store.get(ref_b, "stations")
    with pytest.raises(Exception):
        ctx_a.store.get(ref_b, "stations")

@pytest.mark.asyncio
async def test_two_spatial_calls():
    planner, client = mock_planner()
    
    class FakeSpatialTool:
        result_kind = "stations"
        @staticmethod
        def summarize(res): return ("Summary", [], 1)
        
        async def __call__(self, **kwargs):
            return {"stations": [f"USGS-{kwargs.get('arg', 1)}"]}
            
    planner._dispatch["spatial_query"] = FakeSpatialTool()
    
    ctx = RequestContext("req-1")
    
    # Call tool twice directly to get refs
    res1 = await planner._call_tool("spatial_query", {"arg": "1"}, ctx)
    res2 = await planner._call_tool("spatial_query", {"arg": "2"}, ctx)
    
    ref2 = res2["ref"]
    
    # Setup LLM response to return ref2
    message = MagicMock()
    message.tool_calls = []
    message.content = f'{{"semantic": {{}}, "filters": {{}}, "stations_ref": "{ref2}", "clarification_needed": null}}'
    
    response = MagicMock()
    response.choices = [MagicMock(message=message)]
    client.chat.completions.create.return_value = response
    
    plan = await planner.plan("query", ctx)
    assert plan.stations_ref == ref2
    assert ctx.store.get(plan.stations_ref, "stations").items["stations"] == ["USGS-2"]

@pytest.mark.asyncio
async def test_invalid_ref_reprompt():
    planner, client = mock_planner()
    ctx = RequestContext("req-1")
    
    # Setup LLM responses: 
    # 1. Invalid ref
    # 2. Re-prompt gives another invalid ref
    msg1 = MagicMock()
    msg1.tool_calls = []
    msg1.content = '{"semantic": {}, "filters": {}, "stations_ref": "stations:invalid", "clarification_needed": null}'
    
    msg2 = MagicMock()
    msg2.tool_calls = []
    msg2.content = '{"semantic": {}, "filters": {}, "stations_ref": "stations:still_invalid", "clarification_needed": null}'
    
    resp1 = MagicMock(choices=[MagicMock(message=msg1)])
    resp2 = MagicMock(choices=[MagicMock(message=msg2)])
    client.chat.completions.create.side_effect = [resp1, resp2]
    
    plan = await planner.plan("query", ctx)
    assert plan.clarification_needed == "I ran into an internal error while trying to answer your question."
    assert plan.stations_ref is None
    
    # Verify exactly 2 calls, second call had no tools
    assert client.chat.completions.create.call_count == 2
    args, kwargs = client.chat.completions.create.call_args_list[1]
    assert kwargs.get("tools") == []

@pytest.mark.asyncio
async def test_empty_result_valid_handle():
    planner, _ = mock_planner()
    
    class FakeSpatialTool:
        result_kind = "stations"
        @staticmethod
        def summarize(res): return ("Summary", [], 0)
        
        async def __call__(self, **kwargs):
            return {"stations": []}
            
    planner._dispatch["spatial_query"] = FakeSpatialTool()
    ctx = RequestContext("req-1")
    
    res = await planner._call_tool("spatial_query", {}, ctx)
    assert res["count"] == 0
    ref = res["ref"]
    assert ctx.store.get(ref, "stations").items["stations"] == []

@pytest.mark.asyncio
async def test_response_count_equals_handle_count():
    planner, client = mock_planner()
    
    class FakeSpatialTool:
        result_kind = "stations"
        @staticmethod
        def summarize(res): return ("Summary", [], 3)
        
        async def __call__(self, **kwargs):
            return {"stations": ["1", "2", "3"]}
            
    planner._dispatch["spatial_query"] = FakeSpatialTool()
    ctx = RequestContext("req-1")
    
    res = await planner._call_tool("spatial_query", {}, ctx)
    assert res["count"] == 3
    ref = res["ref"]
    
    msg = MagicMock()
    msg.tool_calls = []
    msg.content = f'{{"semantic": {{}}, "filters": {{}}, "stations_ref": "{ref}", "clarification_needed": null}}'
    client.chat.completions.create.return_value = MagicMock(choices=[MagicMock(message=msg)])
    
    plan = await planner.plan("query", ctx)
    stored = ctx.store.get(plan.stations_ref, "stations")
    assert stored.handle.count == 3

@pytest.mark.asyncio
async def test_tool_result_size_limit():
    planner, client = mock_planner()
    
    class FakeSpatialTool:
        result_kind = "stations"
        @staticmethod
        def summarize(res): 
            # E.g. max sample is 5
            return ("Summary", [{"id": f"USGS-{i}"} for i in range(5)], len(res["stations"]))
            
        async def __call__(self, **kwargs):
            # Create a huge result
            return {"stations": [f"USGS-{i}" for i in range(10000)]}
            
    planner._dispatch["spatial_query"] = FakeSpatialTool()
    ctx = RequestContext("req-1")
    
    res = await planner._call_tool("spatial_query", {}, ctx)
    
    import json
    res_json = json.dumps(res)
    # The tool result passed to LLM shouldn't exceed 4KB (4096 bytes)
    assert len(res_json) < 4096


def test_parse_plan_resilience():
    # 1. Pure JSON
    p1 = QueryPlanner._parse_plan('{"intent": "spatial_discovery", "filters": {"parameter_code": "00060"}}')
    assert p1.filters.get("parameter_code") == "00060"

    # 2. Markdown fence with ```json
    p2 = QueryPlanner._parse_plan('```json\n{"intent": "observation", "filters": {"parameter_code": "00915"}}\n```')
    assert p2.filters.get("parameter_code") == "00915"

    # 3. Conversational preamble and postamble with markdown block
    p3 = QueryPlanner._parse_plan(
        "Sure, here is the execution plan for your query:\n\n"
        "```json\n"
        '{"intent": "station_inventory", "inventory_ref": "inv-123"}\n'
        "```\n\n"
        "Please let me know if you need additional parameters!"
    )
    assert p3.inventory_ref == "inv-123"

    # 4. Raw embedded JSON without markdown fences
    p4 = QueryPlanner._parse_plan('The plan is {"intent": "clarification", "clarification_needed": "Specify state"} based on your request.')
    assert p4.clarification_needed == "Specify state"

    # 5. Malformed text fallback
    p5 = QueryPlanner._parse_plan("No JSON here at all, completely unparseable.")
    assert p5.clarification_needed == "I encountered an error planning the query. Please try rephrasing your question."


@pytest.mark.asyncio
async def test_call_tool_with_spatial_query_result_dto():
    from water_agent.dto import SpatialQueryResult
    planner, _ = mock_planner()

    class FakeSpatialTool:
        result_kind = "stations"

        @staticmethod
        def summarize(res):
            return ("0 stations", [], 0)

        async def __call__(self, **kwargs):
            return SpatialQueryResult(
                stations=[],
                modality="discrete",
                family="chlorophyll a",
                summary="Found 0 distinct locations",
            )

    planner._dispatch["spatial_query"] = FakeSpatialTool()
    ctx = RequestContext("req-dto-test")

    res = await planner._call_tool("spatial_query", {}, ctx)

    # Must NOT be intercepted as an error
    assert "error" not in res or not res.get("error")
    assert "ref" in res
    assert res["kind"] == "stations"
    assert res["count"] == 0
    assert len(ctx.tool_errors) == 0
    assert ctx.store.get(res["ref"], "stations") is not None
