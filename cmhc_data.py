"""
CMHC zone tagging — maps properties to CMHC Rental Market Survey zones.

Tags each property with its CMHC zone based on address/neighbourhood matching.
This enables zone-level comp analysis (comparing $/unit within the same CMHC zone).

Usage:
    python cmhc_data.py                          # show zone definitions
    python cmhc_data.py --tag                    # tag all properties in DB
    python cmhc_data.py --tag --default zone_3   # set default zone for unmatched Toronto properties
"""

import os
import re
import sys
import argparse
import sqlite3
import pandas as pd

import config

# ---------------------------------------------------------------------------
# CMHC Zone Definitions — Toronto CMA
# ---------------------------------------------------------------------------
# Zones are defined by CMHC and group census tracts within the Toronto CMA.
# Source: CMHC Housing Market Information Portal (October 2025 Survey)
# https://www03.cmhc-schl.gc.ca/hmip-pimh/en/TableMapChart/Table?TableId=2.1.31.3&GeographyId=2270

CMHC_ZONES = {
    "zone_1": {
        "name": "Toronto Downtown",
        "description": "Downtown core, Waterfront, CityPlace, St. Lawrence, Liberty Village",
        "neighbourhoods": [
            "downtown", "waterfront", "cityplace", "st. lawrence", "liberty village",
            "king west", "queen west", "financial district", "harbourfront",
            "distillery", "corktown", "regent park", "moss park", "garden district",
            "entertainment district",
        ],
        "address_patterns": [],
    },
    "zone_2": {
        "name": "Toronto East",
        "description": "East York, Riverdale, Danforth, Beaches, Leslieville",
        "neighbourhoods": [
            "east york", "riverdale", "danforth", "the beaches", "beaches",
            "leslieville", "greektown", "broadview", "upper beaches",
            "woodbine corridor", "greenwood", "crescent town",
        ],
        "address_patterns": ["danforth", "broadview", "woodbine", "coxwell"],
    },
    "zone_3": {
        "name": "Toronto Central",
        "description": "Midtown, Yonge-Eglinton, Forest Hill, Davisville, St. Clair, Rosedale",
        "neighbourhoods": [
            "midtown", "yonge-eglinton", "yonge eglinton", "forest hill",
            "davisville", "st. clair", "deer park", "summerhill", "rosedale",
            "moore park", "leaside", "bennington heights", "chaplin estates",
            "south hill", "casa loma", "the annex", "annex", "yorkville",
            "church-yonge corridor", "mount pleasant", "north toronto",
        ],
        "address_patterns": [
            "yonge", "eglinton", "st clair", "st. clair", "davisville",
            "mount pleasant", "avenue rd", "avenue road",
        ],
    },
    "zone_4": {
        "name": "Toronto North",
        "description": "North York, Willowdale, Don Mills, Bayview Village",
        "neighbourhoods": [
            "north york", "willowdale", "don mills", "bayview village",
            "newtonbrook", "lansing", "pleasant view", "parkwoods",
            "henry farm", "york mills", "bridle path", "banbury",
            "hogg's hollow", "flemingdon park",
        ],
        "address_patterns": [
            "sheppard", "finch", "don mills", "bayview", "leslie",
            "york mills",
        ],
    },
    "zone_5": {
        "name": "Toronto West",
        "description": "High Park, Parkdale, Junction, Bloor West Village, Swansea",
        "neighbourhoods": [
            "high park", "parkdale", "junction", "bloor west", "swansea",
            "roncesvalles", "wallace emerson", "dovercourt", "dufferin grove",
            "little portugal", "brockton village", "corso italia",
            "weston", "mount dennis",
        ],
        "address_patterns": [
            "bloor west", "roncesvalles", "parkdale", "high park",
            "dundas west", "junction",
        ],
    },
    "zone_6": {
        "name": "Etobicoke North",
        "description": "Rexdale, Islington, Humber, Richview, West Humber",
        "neighbourhoods": [
            "rexdale", "islington", "humber", "richview", "west humber",
            "thistletown", "smithfield", "kingsview village", "humberwood",
            "clairville", "woodbine gardens", "martingrove",
        ],
        "address_patterns": ["rexdale", "islington", "kipling", "martin grove"],
    },
    "zone_7": {
        "name": "Etobicoke South",
        "description": "Mimico, New Toronto, Long Branch, Lakeshore",
        "neighbourhoods": [
            "mimico", "new toronto", "long branch", "alderwood",
            "lakeshore", "humber bay", "stonegate",
        ],
        "address_patterns": ["lakeshore", "lake shore", "mimico", "long branch"],
    },
    "zone_8": {
        "name": "Scarborough",
        "description": "Scarborough City Centre, Agincourt, Malvern, Guildwood",
        "neighbourhoods": [
            "scarborough", "agincourt", "malvern", "guildwood",
            "woburn", "west hill", "morningside", "rouge", "highland creek",
            "dorset park", "wexford", "bendale", "cliffside", "birchmount park",
            "ionview", "eglinton east", "kennedy park", "scarborough village",
        ],
        "address_patterns": [
            "scarborough", "mccowan", "markham rd", "markham road",
            "kennedy rd", "kennedy road", "brimley", "midland",
            "lawrence east", "ellesmere",
        ],
    },
}

# Municipalities outside City of Toronto (in Toronto CMA)
SUBURBAN_ZONES = {
    "zone_9": {
        "name": "Mississauga",
        "cities": ["mississauga"],
    },
    "zone_10": {
        "name": "Brampton",
        "cities": ["brampton"],
    },
    "zone_11": {
        "name": "Vaughan/Richmond Hill",
        "cities": ["vaughan", "richmond hill"],
    },
    "zone_12": {
        "name": "Markham",
        "cities": ["markham"],
    },
    "zone_13": {
        "name": "Oakville",
        "cities": ["oakville"],
    },
    "zone_14": {
        "name": "Burlington",
        "cities": ["burlington"],
    },
    "zone_15": {
        "name": "Oshawa/Whitby",
        "cities": ["oshawa", "whitby"],
    },
    "zone_16": {
        "name": "Ajax/Pickering",
        "cities": ["ajax", "pickering"],
    },
    "zone_17": {
        "name": "Milton/Halton Hills",
        "cities": ["milton", "halton hills", "georgetown"],
    },
}


# ---------------------------------------------------------------------------
# Zone matching logic
# ---------------------------------------------------------------------------

def match_zone(address, city, site_description=""):
    """Match a property to a CMHC zone based on address, city, and site description.

    Priority:
    1. Suburban municipality match (exact city)
    2. Toronto neighbourhood match (from site_description or address)
    3. Toronto address pattern match (street names)
    4. Default: None (unmatched)

    Returns (zone_key, zone_name) or (None, None).
    """
    city_lower = (city or "").strip().lower()
    addr_lower = (address or "").strip().lower()
    site_lower = (site_description or "").strip().lower()
    combined = f"{addr_lower} {site_lower}"

    # 1. Check suburban municipalities first
    for zone_key, zone_data in SUBURBAN_ZONES.items():
        if city_lower in zone_data["cities"]:
            return zone_key, zone_data["name"]

    # 2. For Toronto properties, try neighbourhood matching
    if "toronto" in city_lower or not city_lower:
        best_match = None
        best_score = 0

        for zone_key, zone_data in CMHC_ZONES.items():
            # Check neighbourhood names in site description and address
            for neighbourhood in zone_data["neighbourhoods"]:
                if neighbourhood in combined:
                    score = len(neighbourhood)  # longer match = more specific
                    if score > best_score:
                        best_score = score
                        best_match = (zone_key, zone_data["name"])

            # Check address patterns (street names)
            for pattern in zone_data.get("address_patterns", []):
                if pattern in addr_lower:
                    score = len(pattern)
                    if score > best_score:
                        best_score = score
                        best_match = (zone_key, zone_data["name"])

        if best_match:
            return best_match

    return None, None


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------

def ensure_zone_column(db_file):
    """Add cmhc_zone column to Property table if missing."""
    conn = sqlite3.connect(db_file)
    cur = conn.cursor()
    try:
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='Property'")
        if not cur.fetchone():
            return
        cur.execute('PRAGMA table_info("Property")')
        existing = {row[1] for row in cur.fetchall()}
        if "cmhc_zone" not in existing:
            cur.execute('ALTER TABLE "Property" ADD COLUMN "cmhc_zone" TEXT')
        conn.commit()
    finally:
        conn.close()


def tag_properties(db_file, default_zone=None):
    """Tag all properties with their CMHC zone.

    Args:
        db_file: Path to SQLite database.
        default_zone: Optional zone_key for unmatched Toronto properties
                      (e.g. "zone_3" to default to Toronto Central).

    Returns count of tagged properties.
    """
    ensure_zone_column(db_file)
    conn = sqlite3.connect(db_file)
    try:
        tables = pd.read_sql_query(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='Property'", conn
        )
        if tables.empty:
            return 0

        props = pd.read_sql_query(
            'SELECT record_id, address, city, site_description FROM "Property"', conn
        )
        if props.empty:
            return 0

        tagged = 0
        unmatched = 0
        zone_counts = {}

        for _, row in props.iterrows():
            zone_key, zone_name = match_zone(
                row.get("address", ""),
                row.get("city", ""),
                row.get("site_description", ""),
            )

            # Apply default for unmatched Toronto properties
            if not zone_key and default_zone:
                city_lower = str(row.get("city", "")).strip().lower()
                if "toronto" in city_lower or not city_lower:
                    zone_key = default_zone
                    zone_info = CMHC_ZONES.get(zone_key, SUBURBAN_ZONES.get(zone_key, {}))
                    zone_name = zone_info.get("name", default_zone)

            if zone_key:
                conn.execute(
                    'UPDATE "Property" SET cmhc_zone = ? WHERE record_id = ?',
                    (zone_key, row["record_id"]),
                )
                tagged += 1
                zone_counts[zone_key] = zone_counts.get(zone_key, 0) + 1
            else:
                unmatched += 1

        conn.commit()

        if zone_counts:
            print(f"Tagged {tagged} properties by CMHC zone:")
            for zk, count in sorted(zone_counts.items(), key=lambda x: -x[1]):
                zname = CMHC_ZONES.get(zk, SUBURBAN_ZONES.get(zk, {})).get("name", zk)
                print(f"  {zk} ({zname}): {count}")
        if unmatched:
            print(f"Unmatched: {unmatched} properties")

        return tagged
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def show_zones():
    """Print CMHC zone definitions."""
    print("CMHC Survey Zones — Toronto CMA\n")

    print("City of Toronto zones:")
    print("-" * 60)
    for zk in sorted(CMHC_ZONES.keys()):
        zone = CMHC_ZONES[zk]
        print(f"  {zk:8s}  {zone['name']:25s}  {zone['description']}")

    print(f"\nSuburban municipalities:")
    print("-" * 60)
    for zk in sorted(SUBURBAN_ZONES.keys()):
        zone = SUBURBAN_ZONES[zk]
        cities = ", ".join(c.title() for c in zone.get("cities", []))
        print(f"  {zk:8s}  {zone['name']:25s}  {cities}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(description="CMHC zone tagging for properties")
    parser.add_argument("--tag", action="store_true",
                        help="Tag all properties in DB with their CMHC zone")
    parser.add_argument("--default", default="zone_3", dest="default_zone",
                        help="Default zone for unmatched Toronto properties (default: zone_3)")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    if args.tag:
        db_file = config.DB_FILE
        if not os.path.exists(db_file):
            print(f"Database not found: {db_file}")
            sys.exit(1)
        tagged = tag_properties(db_file, default_zone=args.default_zone)
        print(f"\nDone: {tagged} properties tagged")
    else:
        show_zones()
