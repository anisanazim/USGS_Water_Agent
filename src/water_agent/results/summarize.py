from typing import Any
from .store import MAX_SAMPLE

def summarize_stations(stations: list[Any], max_sample: int = MAX_SAMPLE) -> tuple[str, list[dict]]:
    count = len(stations)
    
    sample_dicts = []
    for s in stations[:max_sample]:
        if hasattr(s, "model_dump"):
            sample_dicts.append(s.model_dump())
        elif isinstance(s, dict):
            sample_dicts.append(dict(s))
        elif hasattr(s, "id"):
            sample_dicts.append({"id": str(getattr(s, "id"))})
        else:
            sample_dicts.append({"id": str(s)})
            
    summary_examples = ", ".join([
        str(d.get("id") or d.get("location_id") or d)
        for d in sample_dicts
        if d.get("id") or d.get("location_id")
    ])
    summary_string = f"{count} stations (e.g. {summary_examples}, …)" if count > max_sample and summary_examples else f"{count} stations"
    
    return summary_string, sample_dicts

def summarize_inventory(params: dict, max_sample: int = MAX_SAMPLE) -> tuple[str, list[dict]]:
    count = len(params)
    
    sample_dicts = []
    for k, v in list(params.items())[:max_sample]:
        if hasattr(v, "name"):
            name_val = getattr(v, "name")
        elif isinstance(v, dict) and "name" in v:
            name_val = v["name"]
        elif hasattr(v, "get"):
            name_val = v.get("name")
        else:
            name_val = str(v)
        sample_dicts.append({"code": str(k), "name": str(name_val)})
        
    summary_examples = ", ".join([str(d["name"]) for d in sample_dicts if d.get("name")])
    summary_string = f"{count} parameters (e.g. {summary_examples}, …)" if count > max_sample and summary_examples else f"{count} parameters"
    
    return summary_string, sample_dicts
