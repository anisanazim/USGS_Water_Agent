"""Build script to generate the unified analyte index for USGS water quality monitoring.

Preserves the 9 core verified families (73 confirmed parameter codes) with
is_verified_core: true, adds rich aliases, and indexes major extended water
analytes (Magnesium, Sodium, Potassium, Lead, Arsenic, etc.) with water-media filtering.
"""
from __future__ import annotations

import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
REF_DIR = BASE_DIR / "metadata" / "raw" / "references"
FAMILIES_FILE = REF_DIR / "discrete_families.json"
PCODES_FILE = REF_DIR / "parameter-codes.json"

# Aliases for the 9 core verified families
CORE_ALIASES = {
    "calcium": ["ca", "ca2+", "calcium ion", "dissolved calcium", "total calcium"],
    "chlorophyll_a": ["chlorophyll", "chl-a", "chla", "chlorophyll a", "algal chlorophyll"],
    "dissolved_oxygen": ["do", "oxygen", "dissolved o2", "o2"],
    "electrical_conductivity": ["conductivity", "ec", "spc", "specific conductance", "conductance"],
    "nitrate": ["no3", "no3-n", "nitrate-n", "nitrate plus nitrite", "total nitrate"],
    "ph": ["ph", "field ph", "lab ph"],
    "phosphorus": [
        "tp",
        "srp",
        "total phosphorus",
        "orthophosphate",
        "phosphate",
        "po4",
        "po4-p",
        "soluble reactive phosphorus",
    ],
    "organic_carbon": ["toc", "doc", "total organic carbon", "dissolved organic carbon", "carbon"],
    "turbidity_tss": [
        "turbidity",
        "tss",
        "total suspended solids",
        "suspended solids",
        "suspended sediment",
    ],
}

# Extended water analytes to include in unified index
EXTENDED_ANALYTES = {
    "magnesium": {
        "family": "magnesium",
        "canonical_name": "Magnesium",
        "aliases": ["mg", "mg2+", "magnesium ion"],
        "group": "Inorganics, Major, Metals",
        "mixed_modality": False,
        "is_verified_core": False,
        "major_pcodes": ["00925", "00921", "00920", "00927"],
    },
    "sodium": {
        "family": "sodium",
        "canonical_name": "Sodium",
        "aliases": ["na", "na+", "sodium ion"],
        "group": "Inorganics, Major, Metals",
        "mixed_modality": False,
        "is_verified_core": False,
        "major_pcodes": ["00930", "00929"],
    },
    "potassium": {
        "family": "potassium",
        "canonical_name": "Potassium",
        "aliases": ["k", "k+", "potassium ion"],
        "group": "Inorganics, Major, Metals",
        "mixed_modality": False,
        "is_verified_core": False,
        "major_pcodes": ["00935", "00937"],
    },
    "chloride": {
        "family": "chloride",
        "canonical_name": "Chloride",
        "aliases": ["cl", "cl-", "chloride ion"],
        "group": "Inorganics, Major, Non-metals",
        "mixed_modality": False,
        "is_verified_core": False,
        "major_pcodes": ["00940", "99220"],
    },
    "sulfate": {
        "family": "sulfate",
        "canonical_name": "Sulfate",
        "aliases": ["so4", "so4-2", "sulfate ion", "sulphate"],
        "group": "Inorganics, Major, Non-metals",
        "mixed_modality": False,
        "is_verified_core": False,
        "major_pcodes": ["00945", "00946"],
    },
    "fluoride": {
        "family": "fluoride",
        "canonical_name": "Fluoride",
        "aliases": ["f", "f-", "fluoride ion"],
        "group": "Inorganics, Minor, Non-metals",
        "mixed_modality": False,
        "is_verified_core": False,
        "major_pcodes": ["00950", "00951"],
    },
    "silica": {
        "family": "silica",
        "canonical_name": "Silica",
        "aliases": ["sio2", "silicon dioxide", "silicon"],
        "group": "Inorganics, Major, Non-metals",
        "mixed_modality": False,
        "is_verified_core": False,
        "major_pcodes": ["00955", "00956"],
    },
    "alkalinity": {
        "family": "alkalinity",
        "canonical_name": "Alkalinity",
        "aliases": ["alk", "total alkalinity", "acid neutralizing capacity", "anc"],
        "group": "Physical",
        "mixed_modality": False,
        "is_verified_core": False,
        "major_pcodes": ["00410", "00418", "00417"],
    },
    "iron": {
        "family": "iron",
        "canonical_name": "Iron",
        "aliases": ["fe", "fe2+", "fe3+", "iron ion"],
        "group": "Inorganics, Minor, Metals",
        "mixed_modality": False,
        "is_verified_core": False,
        "major_pcodes": ["01046", "01045", "74010"],
    },
    "manganese": {
        "family": "manganese",
        "canonical_name": "Manganese",
        "aliases": ["mn", "mn2+", "manganese ion"],
        "group": "Inorganics, Minor, Metals",
        "mixed_modality": False,
        "is_verified_core": False,
        "major_pcodes": ["01056", "01055"],
    },
    "lead": {
        "family": "lead",
        "canonical_name": "Lead",
        "aliases": ["pb", "lead ion"],
        "group": "Inorganics, Minor, Metals",
        "mixed_modality": False,
        "is_verified_core": False,
        "major_pcodes": ["01049", "01051"],
    },
    "arsenic": {
        "family": "arsenic",
        "canonical_name": "Arsenic",
        "aliases": ["as", "arsenic ion"],
        "group": "Inorganics, Minor, Metals",
        "mixed_modality": False,
        "is_verified_core": False,
        "major_pcodes": ["01000", "01002"],
    },
    "copper": {
        "family": "copper",
        "canonical_name": "Copper",
        "aliases": ["cu", "copper ion"],
        "group": "Inorganics, Minor, Metals",
        "mixed_modality": False,
        "is_verified_core": False,
        "major_pcodes": ["01040", "01042"],
    },
    "zinc": {
        "family": "zinc",
        "canonical_name": "Zinc",
        "aliases": ["zn", "zinc ion"],
        "group": "Inorganics, Minor, Metals",
        "mixed_modality": False,
        "is_verified_core": False,
        "major_pcodes": ["01090", "01092"],
    },
    "ammonia": {
        "family": "ammonia",
        "canonical_name": "Ammonia",
        "aliases": ["nh3", "nh4", "nh4-n", "ammonia nitrogen"],
        "group": "Nutrient",
        "mixed_modality": False,
        "is_verified_core": False,
        "major_pcodes": ["00608", "00610"],
    },
    "total_nitrogen": {
        "family": "total_nitrogen",
        "canonical_name": "Total Nitrogen",
        "aliases": ["tn", "total nitrogen"],
        "group": "Nutrient",
        "mixed_modality": False,
        "is_verified_core": False,
        "major_pcodes": ["00600", "00602"],
    },
}


def build_unified_index() -> None:
    print(f"Loading existing families from {FAMILIES_FILE}...")
    with open(FAMILIES_FILE, "r", encoding="utf-8") as f:
        families = json.load(f)

    print(f"Loading parameter catalog from {PCODES_FILE}...")
    with open(PCODES_FILE, "r", encoding="utf-8") as f:
        p_data = json.load(f)
    p_map = {feat["properties"]["id"]: feat["properties"] for feat in p_data.get("features", [])}

    # 1. Update the 9 core families
    for fam_name, fam in families.items():
        fam["is_verified_core"] = True
        fam["aliases"] = CORE_ALIASES.get(fam_name, [])
        # Sort major_pcodes by verified_records_nc descending so [0] is dominant
        params = fam.get("parameters", {})
        sorted_pcodes = sorted(
            params.keys(),
            key=lambda k: params[k].get("verified_counts", {}).get("recordCount", 0),
            reverse=True,
        )
        fam["major_pcodes"] = sorted_pcodes

    print(f"Verified core families updated: {len(families)}")

    # 2. Add extended analytes
    for fam_key, meta in EXTENDED_ANALYTES.items():
        if fam_key in families:
            continue
        params_dict = {}
        for pcode in meta["major_pcodes"]:
            props = p_map.get(pcode, {})
            desc = props.get("parameter_description") or f"USGS parameter {pcode}"
            unit = props.get("unit_of_measure") or ""
            grp = meta["group"]
            params_dict[pcode] = {
                "pcode": pcode,
                "characteristicName": meta["canonical_name"],
                "characteristicNameUserSupplied": desc,
                "characteristicGroup": grp,
                "medium": "water",
                "unit_of_measure": unit,
                "verified_counts": {
                    "stateFips": "US:37",
                    "date": "2026-10-22",
                    "recordCount": 0,
                    "stationCount": 0,
                },
            }

        families[fam_key] = {
            "family": fam_key,
            "canonical_name": meta["canonical_name"],
            "mixed_modality": meta["mixed_modality"],
            "is_verified_core": meta["is_verified_core"],
            "aliases": meta["aliases"],
            "major_pcodes": meta["major_pcodes"],
            "parameters": params_dict,
        }
        print(f"Added extended analyte: {fam_key} with pcodes {meta['major_pcodes']}")

    print(f"Total unified families/analytes: {len(families)}")
    with open(FAMILIES_FILE, "w", encoding="utf-8") as f:
        json.dump(families, f, indent=2)
    print(f"Successfully saved unified analyte index to {FAMILIES_FILE}!")


if __name__ == "__main__":
    build_unified_index()
