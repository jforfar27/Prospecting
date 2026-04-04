"""
CMHC zone tagging — maps properties to CMHC Rental Market Survey zones.

Tags each property with its CMHC zone based on address/neighbourhood matching.
This enables zone-level comp analysis (comparing $/unit within the same CMHC zone).

Zone keys are prefixed with the CMA name to avoid collisions across cities
(e.g. toronto_zone_3, ottawa_zone_1).

Usage:
    python cmhc_data.py                                     # show zone definitions
    python cmhc_data.py --tag                               # tag all properties in DB
    python cmhc_data.py --tag --default toronto_zone_3      # set default for unmatched Toronto properties
"""

import os
import re
import sys
import argparse
import sqlite3
import pandas as pd

import config

# ---------------------------------------------------------------------------
# CMHC Zone Definitions
# ---------------------------------------------------------------------------
# Zone keys are prefixed with CMA name: "{cma}_zone_{n}"
# This avoids collisions across CMAs (Toronto Zone 1 ≠ Ottawa Zone 1).
#
# Source: CMHC Housing Market Information Portal (October 2025 Survey)
# Toronto: https://www03.cmhc-schl.gc.ca/hmip-pimh/en/TableMapChart/Table?TableId=2.1.31.3&GeographyId=2270
# Ottawa:  https://www03.cmhc-schl.gc.ca/hmip-pimh/en/TableMapChart/Table?TableId=2.1.31.3&GeographyId=1265

# --- Toronto CMA ---

TORONTO_ZONES = {
    "toronto_zone_1": {
        "name": "Toronto Downtown",
        "cma": "toronto",
        "description": "Downtown core, Waterfront, CityPlace, St. Lawrence, Liberty Village",
        "neighbourhoods": [
            "downtown", "waterfront", "cityplace", "st. lawrence", "liberty village",
            "king west", "queen west", "financial district", "harbourfront",
            "distillery", "corktown", "regent park", "moss park", "garden district",
            "entertainment district",
        ],
        "address_patterns": [],
    },
    "toronto_zone_2": {
        "name": "Toronto East",
        "cma": "toronto",
        "description": "East York, Riverdale, Danforth, Beaches, Leslieville",
        "neighbourhoods": [
            "east york", "riverdale", "danforth", "the beaches", "beaches",
            "leslieville", "greektown", "broadview", "upper beaches",
            "woodbine corridor", "greenwood", "crescent town",
        ],
        "address_patterns": ["danforth", "broadview", "woodbine", "coxwell"],
    },
    "toronto_zone_3": {
        "name": "Toronto Central",
        "cma": "toronto",
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
    "toronto_zone_4": {
        "name": "Toronto North",
        "cma": "toronto",
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
    "toronto_zone_5": {
        "name": "Toronto West",
        "cma": "toronto",
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
    "toronto_zone_6": {
        "name": "Etobicoke North",
        "cma": "toronto",
        "description": "Rexdale, Islington, Humber, Richview, West Humber",
        "neighbourhoods": [
            "rexdale", "islington", "humber", "richview", "west humber",
            "thistletown", "smithfield", "kingsview village", "humberwood",
            "clairville", "woodbine gardens", "martingrove",
        ],
        "address_patterns": ["rexdale", "islington", "kipling", "martin grove"],
    },
    "toronto_zone_7": {
        "name": "Etobicoke South",
        "cma": "toronto",
        "description": "Mimico, New Toronto, Long Branch, Lakeshore",
        "neighbourhoods": [
            "mimico", "new toronto", "long branch", "alderwood",
            "lakeshore", "humber bay", "stonegate",
        ],
        "address_patterns": ["lakeshore", "lake shore", "mimico", "long branch"],
    },
    "toronto_zone_8": {
        "name": "Scarborough",
        "cma": "toronto",
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

# Toronto CMA suburban municipalities
TORONTO_SUBURBAN = {
    "toronto_zone_9":  {"name": "Mississauga",        "cma": "toronto", "cities": ["mississauga"]},
    "toronto_zone_10": {"name": "Brampton",            "cma": "toronto", "cities": ["brampton"]},
    "toronto_zone_11": {"name": "Vaughan/Richmond Hill","cma": "toronto", "cities": ["vaughan", "richmond hill"]},
    "toronto_zone_12": {"name": "Markham",             "cma": "toronto", "cities": ["markham"]},
    "toronto_zone_13": {"name": "Oakville",            "cma": "toronto", "cities": ["oakville"]},
    "toronto_zone_14": {"name": "Burlington",          "cma": "toronto", "cities": ["burlington"]},
    "toronto_zone_15": {"name": "Oshawa/Whitby",       "cma": "toronto", "cities": ["oshawa", "whitby"]},
    "toronto_zone_16": {"name": "Ajax/Pickering",      "cma": "toronto", "cities": ["ajax", "pickering"]},
    "toronto_zone_17": {"name": "Milton/Halton Hills", "cma": "toronto", "cities": ["milton", "halton hills", "georgetown"]},
}

# --- Ottawa CMA ---

OTTAWA_ZONES = {
    "ottawa_zone_1": {
        "name": "Ottawa Centre",
        "cma": "ottawa",
        "description": "Downtown, Centretown, Sandy Hill, Lowertown, Byward Market",
        "neighbourhoods": [
            "centretown", "sandy hill", "lowertown", "byward market",
            "downtown", "golden triangle", "the glebe", "glebe",
            "old ottawa south", "old ottawa east",
        ],
        "address_patterns": ["elgin", "bank st", "rideau", "laurier"],
    },
    "ottawa_zone_2": {
        "name": "Ottawa East",
        "cma": "ottawa",
        "description": "Vanier, Overbrook, Beacon Hill, Gloucester",
        "neighbourhoods": [
            "vanier", "overbrook", "beacon hill", "gloucester",
            "cyrville", "pineview", "cardinal heights",
        ],
        "address_patterns": ["montreal rd", "montreal road", "ogilvie"],
    },
    "ottawa_zone_3": {
        "name": "Ottawa West",
        "cma": "ottawa",
        "description": "Westboro, Hintonburg, Nepean, Bayshore, Lincoln Fields",
        "neighbourhoods": [
            "westboro", "hintonburg", "mechanicsville", "tunney's pasture",
            "nepean", "bayshore", "lincoln fields", "carlingwood",
            "britannia", "bells corners",
        ],
        "address_patterns": ["carling", "richmond rd", "richmond road", "scott st"],
    },
    "ottawa_zone_4": {
        "name": "Ottawa South",
        "cma": "ottawa",
        "description": "Alta Vista, Hunt Club, South Keys, Greenboro",
        "neighbourhoods": [
            "alta vista", "hunt club", "south keys", "greenboro",
            "heron park", "riverview", "ellwood",
        ],
        "address_patterns": ["bank south", "hunt club", "heron"],
    },
    "ottawa_zone_5": {
        "name": "Kanata/Stittsville",
        "cma": "ottawa",
        "description": "Kanata, Stittsville, Bridlewood, Morgan's Grant",
        "neighbourhoods": [
            "kanata", "stittsville", "bridlewood", "morgan's grant",
            "beaverbrook", "katimavik",
        ],
        "address_patterns": ["kanata", "stittsville"],
    },
    "ottawa_zone_6": {
        "name": "Orléans",
        "cma": "ottawa",
        "description": "Orléans, Fallingbrook, Avalon, Chapel Hill",
        "neighbourhoods": [
            "orleans", "orléans", "fallingbrook", "avalon",
            "chapel hill", "convent glen", "queenswood heights",
        ],
        "address_patterns": ["orleans", "orléans", "innes"],
    },
    "ottawa_zone_7": {
        "name": "Barrhaven",
        "cma": "ottawa",
        "description": "Barrhaven, Longfields, Half Moon Bay",
        "neighbourhoods": [
            "barrhaven", "longfields", "half moon bay",
            "chapman mills", "stonebridge",
        ],
        "address_patterns": ["barrhaven", "strandherd", "fallowfield"],
    },
}

OTTAWA_SUBURBAN = {
    "ottawa_zone_8": {"name": "Gatineau", "cma": "ottawa", "cities": ["gatineau", "hull", "aylmer"]},
}

# ---------------------------------------------------------------------------
# Combined lookup — all CMAs
# ---------------------------------------------------------------------------

# All neighbourhood-matchable zones (City of Toronto, City of Ottawa)
CMHC_ZONES = {**TORONTO_ZONES, **OTTAWA_ZONES}

# All city-matchable suburban zones
SUBURBAN_ZONES = {**TORONTO_SUBURBAN, **OTTAWA_SUBURBAN}

# CMA detection by city name
CMA_CITY_MAP = {
    # Toronto CMA
    "toronto": "toronto",
    "east york": "toronto",
    "north york": "toronto",
    "york": "toronto",
    "etobicoke": "toronto",
    "scarborough": "toronto",
    # Ottawa CMA
    "ottawa": "ottawa",
    "nepean": "ottawa",
    "kanata": "ottawa",
    "gloucester": "ottawa",
    "vanier": "ottawa",
    "orleans": "ottawa",
    "orléans": "ottawa",
    "barrhaven": "ottawa",
}
# Add suburban cities
for zone_data in SUBURBAN_ZONES.values():
    for city in zone_data.get("cities", []):
        CMA_CITY_MAP[city] = zone_data["cma"]


def _detect_cma(city):
    """Detect which CMA a city belongs to."""
    city_lower = (city or "").strip().lower()
    return CMA_CITY_MAP.get(city_lower)


# ---------------------------------------------------------------------------
# Zone matching logic
# ---------------------------------------------------------------------------

def match_zone(address, city, site_description=""):
    """Match a property to a CMHC zone based on address, city, and site description.

    Priority:
    1. Suburban municipality match (exact city)
    2. Neighbourhood match (from site_description or address)
    3. Address pattern match (street names)
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

    # 2. Detect CMA to narrow zone search
    cma = _detect_cma(city)
    if not cma:
        return None, None

    # 3. Search zones within this CMA
    best_match = None
    best_score = 0

    for zone_key, zone_data in CMHC_ZONES.items():
        if zone_data.get("cma") != cma:
            continue

        # Check neighbourhood names in site description and address
        for neighbourhood in zone_data.get("neighbourhoods", []):
            if neighbourhood in combined:
                score = len(neighbourhood)
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
        default_zone: Optional zone_key for unmatched properties within a
                      known CMA (e.g. "toronto_zone_3" for Toronto Central).

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

        # Extract default CMA from default_zone (e.g. "toronto_zone_3" → "toronto")
        default_cma = None
        if default_zone:
            parts = default_zone.rsplit("_zone_", 1)
            default_cma = parts[0] if len(parts) == 2 else None

        tagged = 0
        unmatched = 0
        zone_counts = {}

        for _, row in props.iterrows():
            zone_key, zone_name = match_zone(
                row.get("address", ""),
                row.get("city", ""),
                row.get("site_description", ""),
            )

            # Apply default for unmatched properties in the default CMA
            if not zone_key and default_zone and default_cma:
                prop_cma = _detect_cma(row.get("city", ""))
                if prop_cma == default_cma:
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

def show_zones(cma_filter=None):
    """Print CMHC zone definitions."""
    cmas = {}
    for zk, zdata in {**CMHC_ZONES, **SUBURBAN_ZONES}.items():
        cma = zdata.get("cma", "unknown")
        if cma_filter and cma != cma_filter:
            continue
        cmas.setdefault(cma, []).append((zk, zdata))

    for cma in sorted(cmas.keys()):
        zones = cmas[cma]
        print(f"\n{'='*60}")
        print(f"CMHC Zones — {cma.upper()} CMA")
        print(f"{'='*60}")
        for zk, zdata in sorted(zones):
            desc = zdata.get("description", "")
            cities = ", ".join(c.title() for c in zdata.get("cities", []))
            detail = desc or cities
            print(f"  {zk:25s}  {zdata['name']:25s}  {detail}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(description="CMHC zone tagging for properties")
    parser.add_argument("--tag", action="store_true",
                        help="Tag all properties in DB with their CMHC zone")
    parser.add_argument("--default", default="toronto_zone_3", dest="default_zone",
                        help="Default zone for unmatched properties (default: toronto_zone_3)")
    parser.add_argument("--cma", default=None,
                        help="Filter zone display by CMA (e.g. toronto, ottawa)")
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
        show_zones(cma_filter=args.cma)
