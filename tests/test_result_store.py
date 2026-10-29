import json
import pytest
from datetime import datetime, timezone
from water_agent.results import ResultStore, UnknownRefError, WrongKindError

def test_result_store_put_and_get():
    store = ResultStore("req-1")
    prov = {"retrieved_at": datetime.now(timezone.utc).isoformat(), "source_urls": []}
    
    handle = store.put("stations", ["USGS-123", "USGS-456"], "2 stations", [{"id": "USGS-123"}, {"id": "USGS-456"}], prov, count=2)
    
    assert handle.kind == "stations"
    assert handle.count == 2
    assert handle.sample == [{"id": "USGS-123"}, {"id": "USGS-456"}]
    assert handle.truncated is False
    assert handle.summary == "2 stations"
    
    stored = store.get(handle.ref, "stations")
    assert stored.handle == handle
    assert stored.items == ["USGS-123", "USGS-456"]

def test_result_store_get_unknown_ref():
    store = ResultStore("req-1")
    with pytest.raises(UnknownRefError):
        store.get("stations:invalid", "stations")

def test_result_store_get_wrong_kind():
    store = ResultStore("req-1")
    prov = {"retrieved_at": datetime.now(timezone.utc).isoformat(), "source_urls": []}
    handle = store.put("stations", [], "0 stations", [], prov, count=0)
    
    with pytest.raises(WrongKindError):
        store.get(handle.ref, "inventory")

def test_result_store_json_size():
    store = ResultStore("req-1")
    prov = {"retrieved_at": datetime.now(timezone.utc).isoformat(), "source_urls": []}
    
    # 5,000 stations
    items = [f"USGS-{i}" for i in range(5000)]
    handle = store.put("stations", items, "5000 stations", [{"id": "USGS-0"}], prov, count=5000)
    
    handle_json = json.dumps(handle.model_dump())
    assert len(handle_json) < 2048 # Under 2KB

def test_result_store_distinct_refs():
    store = ResultStore("req-1")
    prov = {"retrieved_at": datetime.now(timezone.utc).isoformat(), "source_urls": []}
    
    h1 = store.put("stations", [], "0", [], prov, count=0)
    h2 = store.put("stations", [], "0", [], prov, count=0)
    
    assert h1.ref != h2.ref

def test_result_store_refs():
    store = ResultStore("req-1")
    prov = {"retrieved_at": datetime.now(timezone.utc).isoformat(), "source_urls": []}
    
    store.put("stations", [], "0", [], prov, count=0)
    
    refs = store.refs()
    assert len(refs) == 1
    assert type(refs[0]).__name__ == "ResultHandle"

def test_result_store_available_refs():
    store = ResultStore("req-1")
    prov = {"retrieved_at": datetime.now(timezone.utc).isoformat(), "source_urls": []}
    
    h1 = store.put("stations", [], "0", [], prov, count=0)
    
    avail = store.available_refs()
    assert len(avail) == 1
    assert avail[0] == {"ref": h1.ref, "kind": "stations", "count": 0}

def test_result_store_empty_result():
    store = ResultStore("req-1")
    prov = {"retrieved_at": datetime.now(timezone.utc).isoformat(), "source_urls": []}
    
    handle = store.put("stations", [], "0 stations", [], prov, count=0)
    assert handle.count == 0
    
    stored = store.get(handle.ref, "stations")
    assert stored.items == []

def test_result_store_set_artifact_id():
    store = ResultStore("req-1")
    prov = {"retrieved_at": datetime.now(timezone.utc).isoformat(), "source_urls": []}
    
    handle = store.put("stations", [], "0", [], prov, count=0)
    store.set_artifact_id(handle.ref, "art-123")
    
    stored = store.get(handle.ref, "stations")
    assert stored.artifact_id == "art-123"
