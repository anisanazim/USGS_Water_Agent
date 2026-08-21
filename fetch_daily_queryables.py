import json
from pathlib import Path

import requests


USGS_BASE_URL = "https://api.waterdata.usgs.gov/ogcapi/v0"

OUTPUT_PATH = Path("metadata/raw/daily/queryables.json")


def fetch_daily_queryables() -> dict:
    url = f"{USGS_BASE_URL}/collections/daily/queryables"

    params = {
        "f": "json",
        "lang": "en-US",
    }

    response = requests.get(
        url,
        params=params,
        timeout=30,
    )

    response.raise_for_status()

    return response.json()


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
    print("Fetching Daily queryables...")

    data = fetch_daily_queryables()

    save_json(data, OUTPUT_PATH)

    print(f"Saved to: {OUTPUT_PATH}")
    print(f"Top-level keys: {list(data.keys())}")


if __name__ == "__main__":
    main()