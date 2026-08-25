import json
import sys
from pathlib import Path

import requests


USGS_BASE_URL = "https://api.waterdata.usgs.gov/ogcapi/v0"

OUTPUT_DIR = Path("metadata/raw/references")

DEFAULT_COLLECTIONS = [
    "parameter-codes",
    "statistic-codes",
]


def _next_page_url(data: dict) -> str | None:
    for link in data.get("links", []):
        if link.get("rel") == "next":
            return link.get("href")
    return None


def fetch_reference_collection(collection: str, limit: int = 10000) -> dict:
    url = f"{USGS_BASE_URL}/collections/{collection}/items"
    params = {"f": "json", "limit": limit}

    response = requests.get(url, params=params, timeout=30)
    response.raise_for_status()
    data = response.json()

    features = list(data.get("features", []))
    next_url = _next_page_url(data)

    while next_url:
        response = requests.get(next_url, timeout=30)
        response.raise_for_status()
        page = response.json()

        features.extend(page.get("features", []))
        next_url = _next_page_url(page)

    data["features"] = features
    data["numberReturned"] = len(features)

    return data


def save_json(data: dict, output_path: Path) -> None:
    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with output_path.open("w", encoding="utf-8") as file:
        json.dump(
            data,
            file,
            indent=2,
            ensure_ascii=False,
        )


def main():
    collections = sys.argv[1:] or DEFAULT_COLLECTIONS

    for collection in collections:
        print(f"Fetching reference collection '{collection}'...")

        data = fetch_reference_collection(collection)
        output_path = OUTPUT_DIR / f"{collection}.json"
        save_json(data, output_path)

        print(f"  Saved {len(data.get('features', []))} features to: {output_path}")


if __name__ == "__main__":
    main()
