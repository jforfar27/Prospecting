"""
Unit count lookup — finds the number of units in multi-residential buildings.

Tries multiple sources in order:
1. RealTrack raw_text / site_description (already scraped)
2. Toronto Open Data — Apartment Building Registration
3. Web search fallback (DuckDuckGo)

Usage:
    python unit_lookup.py                  # look up last 20 properties
    python unit_lookup.py --limit 50       # look up last 50 properties
    python unit_lookup.py --all            # look up all properties missing unit_count
    python unit_lookup.py --address "123 Main St" --city "Toronto"  # single lookup
"""

import re
import os
import sys
import time
import argparse
import sqlite3
import requests
import pandas as pd
from urllib.parse import quote_plus

import config


# ---------------------------------------------------------------------------
# Source 1: Parse unit count from RealTrack's own scraped text
# ---------------------------------------------------------------------------

UNIT_PATTERNS = [
    # "123 units", "123-unit", "123 suites", "123 suite"
    r'(\d{1,4})\s*[-–]?\s*(?:unit|units|suite|suites)\b',
    # "units: 123"
    r'(?:units|suites)\s*[:=]\s*(\d{1,4})',
    # "containing 123 residential"
    r'containing\s+(\d{1,4})\s+(?:residential|rental|apartment)',
    # "123 apartment" / "123 residential"
    r'(\d{1,4})\s+(?:apartment|residential)\s+(?:unit|suite)',
    # "X-storey, Y-unit" pattern
    r'\d{1,3}\s*[-–]?\s*stor[e]?y.*?(\d{1,4})\s*[-–]?\s*unit',
]


def parse_units_from_text(text):
    """Extract unit count from free text. Returns int or None."""
    if not text:
        return None
    text_lower = text.lower()
    for pattern in UNIT_PATTERNS:
        match = re.search(pattern, text_lower)
        if match:
            count = int(match.group(1))
            # Sanity check: multi-res buildings typically 3–2000 units
            if 3 <= count <= 2000:
                return count
    return None


def lookup_from_realtrack(row):
    """Try to extract unit count from already-scraped RealTrack data."""
    # Check site_description first (more structured)
    units = parse_units_from_text(row.get("site_description", ""))
    if units:
        return units, "realtrack_site_description"

    # Check raw_text (full page dump)
    units = parse_units_from_text(row.get("raw_text", ""))
    if units:
        return units, "realtrack_raw_text"

    return None, None


# ---------------------------------------------------------------------------
# Source 2: Toronto Open Data — Apartment Building Registration
# ---------------------------------------------------------------------------

TORONTO_OPEN_DATA_URL = "https://ckan0.cf.opendata.toronto.ca/api/3/action/datastore_search"
# Resource ID for Apartment Building Registration dataset
TORONTO_ABR_RESOURCE_ID = "1f0c8f60-2bba-4e40-8db2-75f6f2e706b0"


def normalize_address_for_search(address):
    """Extract the street number and name for fuzzy matching."""
    if not address:
        return "", ""
    # Extract leading number
    num_match = re.match(r'^(\d+)', address.strip())
    street_num = num_match.group(1) if num_match else ""
    # Extract street name (remove unit/suite numbers, directional suffixes)
    clean = re.sub(r'^[\d\-]+\s*', '', address.strip())
    clean = re.sub(r'\s*(unit|suite|apt|#)\s*\S+', '', clean, flags=re.IGNORECASE)
    street_name = clean.strip()
    return street_num, street_name


def lookup_from_toronto_opendata(address, city):
    """Query Toronto Open Data Apartment Building Registration for unit count."""
    if not city or "toronto" not in city.lower():
        return None, None

    street_num, street_name = normalize_address_for_search(address)
    if not street_num:
        return None, None

    # Search by street number first — the dataset uses SITE_ADDRESS field
    try:
        # Try exact address search
        params = {
            "resource_id": TORONTO_ABR_RESOURCE_ID,
            "q": f"{street_num} {street_name}",
            "limit": 5,
        }
        resp = requests.get(TORONTO_OPEN_DATA_URL, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        if data.get("success") and data["result"]["records"]:
            for record in data["result"]["records"]:
                site_addr = str(record.get("SITE_ADDRESS", "")).lower()
                if street_num in site_addr:
                    units = record.get("CONFIRMED_UNITS") or record.get("CONFIRMED_STOREYS")
                    if units and str(units).isdigit():
                        return int(units), "toronto_opendata"

        # Fallback: search by street number only
        params["q"] = street_num
        params["filters"] = f'{{"SITE_ADDRESS": "{street_num}"}}'
        resp = requests.get(TORONTO_OPEN_DATA_URL, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        if data.get("success") and data["result"]["records"]:
            for record in data["result"]["records"]:
                site_addr = str(record.get("SITE_ADDRESS", "")).lower()
                # Check if street name partially matches
                if street_name and any(
                    word in site_addr for word in street_name.lower().split()[:2]
                ):
                    units = record.get("CONFIRMED_UNITS")
                    if units and str(units).isdigit():
                        return int(units), "toronto_opendata"

    except Exception as e:
        print(f"    Toronto Open Data error: {e}")

    return None, None


# ---------------------------------------------------------------------------
# Source 3: Web search fallback (DuckDuckGo HTML)
# ---------------------------------------------------------------------------

def lookup_from_web_search(address, city):
    """Search DuckDuckGo for the address and try to extract unit count."""
    if not address:
        return None, None

    query = f'"{address}" {city or ""} units apartment building'
    search_url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        resp = requests.get(search_url, headers=headers, timeout=10)
        resp.raise_for_status()

        # Parse all result snippets
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(resp.text, "html.parser")
        snippets = [a.get_text() for a in soup.select(".result__snippet")]

        # Also grab result titles
        titles = [a.get_text() for a in soup.select(".result__title")]
        all_text = " ".join(snippets + titles)

        units = parse_units_from_text(all_text)
        if units:
            return units, "web_search"

    except Exception as e:
        print(f"    Web search error: {e}")

    return None, None


# ---------------------------------------------------------------------------
# Orchestrator: try all sources in order
# ---------------------------------------------------------------------------

def lookup_unit_count(row):
    """Try all sources in priority order. Returns (unit_count, source) or (None, None)."""
    address = row.get("address", "")
    city = row.get("city", "")

    # Source 1: RealTrack's own data
    units, source = lookup_from_realtrack(row)
    if units:
        return units, source

    # Source 2: Toronto Open Data
    units, source = lookup_from_toronto_opendata(address, city)
    if units:
        return units, source

    # Source 3: Web search
    units, source = lookup_from_web_search(address, city)
    if units:
        return units, source

    return None, None


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------

def ensure_unit_columns(db_file):
    """Add unit_count and unit_count_source columns to Property table if missing."""
    conn = sqlite3.connect(db_file)
    cur = conn.cursor()
    try:
        cur.execute('PRAGMA table_info("Property")')
        existing_cols = {row[1] for row in cur.fetchall()}
        if "unit_count" not in existing_cols:
            cur.execute('ALTER TABLE "Property" ADD COLUMN "unit_count" TEXT')
        if "unit_count_source" not in existing_cols:
            cur.execute('ALTER TABLE "Property" ADD COLUMN "unit_count_source" TEXT')
        conn.commit()
    finally:
        conn.close()


def get_properties_for_lookup(db_file, limit=20, all_missing=False):
    """Get properties that need unit count lookup."""
    conn = sqlite3.connect(db_file)
    try:
        # Check if table exists
        tables = pd.read_sql_query(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='Property'", conn
        )
        if tables.empty:
            print("No Property table found. Run the scraper first.")
            return pd.DataFrame()

        ensure_unit_columns(db_file)

        if all_missing:
            query = 'SELECT * FROM "Property" WHERE unit_count IS NULL OR unit_count = ""'
        else:
            query = f'SELECT * FROM "Property" ORDER BY rowid DESC LIMIT {limit}'

        return pd.read_sql_query(query, conn)
    finally:
        conn.close()


def save_unit_count(db_file, record_id, unit_count, source):
    """Update a property's unit_count and source in the database."""
    conn = sqlite3.connect(db_file)
    try:
        conn.execute(
            'UPDATE "Property" SET unit_count = ?, unit_count_source = ? WHERE record_id = ?',
            (str(unit_count), source, record_id),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Single address lookup (no DB needed)
# ---------------------------------------------------------------------------

def lookup_single(address, city):
    """Look up unit count for a single address (no DB)."""
    row = {"address": address, "city": city, "site_description": "", "raw_text": ""}
    # Skip realtrack source since there's no scraped data
    units, source = lookup_from_toronto_opendata(address, city)
    if units:
        return units, source
    units, source = lookup_from_web_search(address, city)
    if units:
        return units, source
    return None, None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_lookup(limit=20, all_missing=False):
    """Run unit count lookup for properties in the database."""
    db_file = config.DB_FILE

    if not os.path.exists(db_file):
        print(f"Database not found: {db_file}")
        print("Run the scraper first: python realtrack_scraper.py")
        return

    properties = get_properties_for_lookup(db_file, limit=limit, all_missing=all_missing)
    if properties.empty:
        print("No properties to look up.")
        return

    total = len(properties)
    found = 0
    results = []

    print(f"Looking up unit counts for {total} properties...\n")

    for idx, row in properties.iterrows():
        record_id = row["record_id"]
        address = row.get("address", "")
        city = row.get("city", "")

        # Skip if already has unit_count (unless --all forces re-check)
        existing = row.get("unit_count", "")
        if existing and str(existing).strip() and not all_missing:
            print(f"  [{idx + 1}/{total}] {address}, {city} — already has {existing} units")
            continue

        print(f"  [{idx + 1}/{total}] {address}, {city}...", end=" ", flush=True)

        units, source = lookup_unit_count(row.to_dict())

        if units:
            save_unit_count(db_file, record_id, units, source)
            print(f"✓ {units} units (source: {source})")
            found += 1
            results.append({
                "record_id": record_id,
                "address": address,
                "city": city,
                "unit_count": units,
                "source": source,
            })
        else:
            print("— not found")
            results.append({
                "record_id": record_id,
                "address": address,
                "city": city,
                "unit_count": None,
                "source": None,
            })

        # Rate limit between web requests
        time.sleep(1)

    print(f"\nDone: {found}/{total} properties matched with unit counts.")

    if results:
        results_df = pd.DataFrame(results)
        results_file = os.path.join(config.OUTPUT_DIR, "unit_lookup_results.csv")
        results_df.to_csv(results_file, index=False)
        print(f"Results saved: {results_file}")

    return results


def parse_args():
    parser = argparse.ArgumentParser(description="Look up unit counts for scraped properties")
    parser.add_argument("--limit", type=int, default=20, help="Number of recent properties to look up (default: 20)")
    parser.add_argument("--all", action="store_true", dest="all_missing", help="Look up all properties missing unit_count")
    parser.add_argument("--address", help="Single address lookup (no DB needed)")
    parser.add_argument("--city", default="", help="City for single address lookup")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    if args.address:
        units, source = lookup_single(args.address, args.city)
        if units:
            print(f"{args.address}, {args.city}: {units} units (source: {source})")
        else:
            print(f"{args.address}, {args.city}: unit count not found")
    else:
        run_lookup(limit=args.limit, all_missing=args.all_missing)
