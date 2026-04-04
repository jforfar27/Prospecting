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
from datetime import datetime, timedelta
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
    """Add unit_count, unit_count_source, and price_per_unit columns to Property table if missing."""
    conn = sqlite3.connect(db_file)
    cur = conn.cursor()
    try:
        # Check if Property table exists
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='Property'")
        if not cur.fetchone():
            return
        cur.execute('PRAGMA table_info("Property")')
        existing_cols = {row[1] for row in cur.fetchall()}
        for col in ["unit_count", "unit_count_source", "price_per_unit",
                    "cmhc_zone",
                    "market_median_ppu", "market_avg_ppu", "market_min_ppu",
                    "market_max_ppu", "comp_count", "ppu_vs_market"]:
            if col not in existing_cols:
                cur.execute(f'ALTER TABLE "Property" ADD COLUMN "{col}" TEXT')
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
# Price per unit computation
# ---------------------------------------------------------------------------

def _parse_price(val):
    """Convert price string like '$4,500,000' to float."""
    if not val or (isinstance(val, float) and pd.isna(val)):
        return None
    cleaned = re.sub(r'[$,\s]', '', str(val).strip())
    try:
        return float(cleaned)
    except ValueError:
        return None


def compute_price_per_unit(db_file):
    """Compute price_per_unit for all properties that have both unit_count and a purchase_price.

    Joins Property.unit_count with Transaction.purchase_price and writes
    price_per_unit back to the Property table. Returns count of updated rows.
    """
    ensure_unit_columns(db_file)
    conn = sqlite3.connect(db_file)
    try:
        # Check both tables exist
        tables = pd.read_sql_query(
            "SELECT name FROM sqlite_master WHERE type='table'", conn
        )
        table_names = set(tables["name"].tolist())
        if "Property" not in table_names or "Transaction" not in table_names:
            return 0

        # Get properties with unit_count
        props = pd.read_sql_query(
            'SELECT record_id, unit_count FROM "Property" '
            'WHERE unit_count IS NOT NULL AND unit_count != ""',
            conn,
        )
        if props.empty:
            return 0

        # Get first transaction per property (by property_record_id)
        txns = pd.read_sql_query(
            'SELECT property_record_id, purchase_price FROM "Transaction"',
            conn,
        )
        if txns.empty:
            return 0

        # Deduplicate: keep first transaction per property
        txns = txns.drop_duplicates(subset="property_record_id", keep="first")

        # Merge
        merged = props.merge(
            txns,
            left_on="record_id",
            right_on="property_record_id",
            how="inner",
        )

        updated = 0
        for _, row in merged.iterrows():
            units = _parse_price(row["unit_count"])  # stored as text
            price = _parse_price(row["purchase_price"])
            if units and units > 0 and price and price > 0:
                ppu = round(price / units, 2)
                conn.execute(
                    'UPDATE "Property" SET price_per_unit = ? WHERE record_id = ?',
                    (str(ppu), row["record_id"]),
                )
                updated += 1

        conn.commit()
        return updated
    finally:
        conn.close()


def compute_market_comps(db_file, months=36):
    """Compute market comp stats by comparing $/unit to peers in same CMHC zone.

    Grouping priority: CMHC zone > city > region.
    For each property with a price_per_unit, finds other sales in the same
    group within the last `months` months and computes median, average, min,
    max $/unit plus a percentage vs. market median.

    Returns count of properties updated.
    """
    ensure_unit_columns(db_file)

    # Tag properties with CMHC zones before computing comps
    from cmhc_data import tag_properties
    tag_properties(db_file, default_zone="toronto_zone_3")

    conn = sqlite3.connect(db_file)
    try:
        # Check tables exist
        tables = pd.read_sql_query(
            "SELECT name FROM sqlite_master WHERE type='table'", conn
        )
        table_names = set(tables["name"].tolist())
        if "Property" not in table_names or "Transaction" not in table_names:
            return 0

        # Load all properties with price_per_unit joined to their transaction
        base = pd.read_sql_query("""
            SELECT
                p.record_id,
                p.cmhc_zone,
                p.city,
                p.region,
                CAST(p.price_per_unit AS REAL) AS ppu,
                t.sale_date,
                t.portfolio_flag
            FROM "Property" p
            INNER JOIN "Transaction" t ON t.property_record_id = p.record_id
            WHERE p.price_per_unit IS NOT NULL AND p.price_per_unit != ''
              AND p.unit_count IS NOT NULL AND p.unit_count != ''
        """, conn)

        if base.empty:
            return 0

        # Deduplicate: keep first transaction per property
        base = base.drop_duplicates(subset="record_id", keep="first")

        # Parse sale dates and filter to time window
        from type_converters import parse_date
        cutoff = (datetime.now() - timedelta(days=months * 30)).strftime("%Y-%m-%d")

        def parse_sale_date(val):
            parsed = parse_date(val)
            return parsed if parsed else None

        base["sale_date_iso"] = base["sale_date"].apply(parse_sale_date)

        # Build comps pool: within time window, exclude portfolio sales
        comps_pool = base[
            (base["sale_date_iso"].notna()) &
            (base["sale_date_iso"] >= cutoff) &
            (~base["portfolio_flag"].str.lower().isin(["portfolio", "true", "yes", "1"]))
        ].copy()

        if comps_pool.empty:
            return 0

        # Normalize keys for matching
        comps_pool["zone_key"] = comps_pool["cmhc_zone"].fillna("").str.strip().str.lower()
        comps_pool["city_key"] = comps_pool["city"].str.strip().str.lower()
        comps_pool["region_key"] = comps_pool["region"].str.strip().str.lower()

        # Group comps by zone, city, and region for fast lookup
        zone_groups = {}
        for key, group in comps_pool.groupby("zone_key"):
            if key:
                zone_groups[key] = group

        city_groups = {}
        for key, group in comps_pool.groupby("city_key"):
            if key:
                city_groups[key] = group

        region_groups = {}
        for key, group in comps_pool.groupby("region_key"):
            if key:
                region_groups[key] = group

        # Compute comps for each property that has price_per_unit
        updated = 0
        for _, row in base.iterrows():
            ppu = row["ppu"]
            if not ppu or ppu <= 0:
                continue

            zone_key = str(row.get("cmhc_zone") or "").strip().lower()
            city_key = str(row["city"]).strip().lower() if row["city"] else ""
            region_key = str(row["region"]).strip().lower() if row["region"] else ""
            record_id = row["record_id"]

            # Priority 1: CMHC zone comps
            peers = None
            if zone_key and zone_key in zone_groups:
                zone_comps = zone_groups[zone_key]
                peers = zone_comps[zone_comps["record_id"] != record_id]["ppu"]

            # Priority 2: City-level comps (fallback if < 2 zone comps)
            if peers is None or len(peers) < 2:
                if city_key and city_key in city_groups:
                    city_comps = city_groups[city_key]
                    peers = city_comps[city_comps["record_id"] != record_id]["ppu"]

            # Priority 3: Region-level comps
            if peers is None or len(peers) < 2:
                if region_key and region_key in region_groups:
                    region_comps = region_groups[region_key]
                    peers = region_comps[region_comps["record_id"] != record_id]["ppu"]

            # Need at least 2 comps for meaningful stats
            if peers is None or len(peers) < 2:
                continue

            median_ppu = peers.median()
            avg_ppu = peers.mean()
            min_ppu = peers.min()
            max_ppu = peers.max()
            comp_count = len(peers)

            # Compute % vs market
            if median_ppu > 0:
                vs_market = (ppu - median_ppu) / median_ppu * 100
                vs_market_str = f"{vs_market:+.1f}%"
            else:
                vs_market_str = None

            conn.execute(
                """UPDATE "Property" SET
                    market_median_ppu = ?,
                    market_avg_ppu = ?,
                    market_min_ppu = ?,
                    market_max_ppu = ?,
                    comp_count = ?,
                    ppu_vs_market = ?
                WHERE record_id = ?""",
                (
                    str(round(median_ppu, 2)),
                    str(round(avg_ppu, 2)),
                    str(round(min_ppu, 2)),
                    str(round(max_ppu, 2)),
                    str(comp_count),
                    vs_market_str,
                    record_id,
                ),
            )
            updated += 1

        conn.commit()
        return updated
    finally:
        conn.close()


def export_comps(db_file, output_dir):
    """Export a Comps CSV joining property, transaction, and market comp data."""
    conn = sqlite3.connect(db_file)
    try:
        tables = pd.read_sql_query(
            "SELECT name FROM sqlite_master WHERE type='table'", conn
        )
        table_names = set(tables["name"].tolist())
        if "Property" not in table_names or "Transaction" not in table_names:
            return None

        query = """
            SELECT
                p.address,
                p.city,
                p.region,
                p.cmhc_zone,
                t.sale_date,
                t.purchase_price,
                p.unit_count,
                p.price_per_unit,
                p.market_median_ppu,
                p.market_avg_ppu,
                p.comp_count,
                p.ppu_vs_market,
                p.acreage,
                t.cash,
                t.assumed_vbt_debt,
                p.record_id
            FROM "Property" p
            LEFT JOIN "Transaction" t
                ON t.property_record_id = p.record_id
            ORDER BY t.sale_date DESC
        """
        df = pd.read_sql_query(query, conn)

        if df.empty:
            return None

        # Deduplicate (keep first transaction per property)
        df = df.drop_duplicates(subset="record_id", keep="first")

        comps_file = os.path.join(output_dir, "Comps.csv")
        df.to_csv(comps_file, index=False)
        print(f"Comps exported: {comps_file} ({len(df)} properties)")
        return comps_file
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

def run_lookup(limit=20, all_missing=False, comp_months=36):
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

    # Compute $/unit for all properties with unit_count + purchase_price
    updated = compute_price_per_unit(db_file)
    if updated:
        print(f"Computed $/unit for {updated} properties")

    # Compute market comps (median, avg, range, % vs market)
    comps_updated = compute_market_comps(db_file, months=comp_months)
    if comps_updated:
        print(f"Computed market comps for {comps_updated} properties")

    # Export comps table
    export_comps(db_file, config.OUTPUT_DIR)

    return results


def parse_args():
    parser = argparse.ArgumentParser(description="Look up unit counts for scraped properties")
    parser.add_argument("--limit", type=int, default=20, help="Number of recent properties to look up (default: 20)")
    parser.add_argument("--all", action="store_true", dest="all_missing", help="Look up all properties missing unit_count")
    parser.add_argument("--comp-months", type=int, default=36, help="Months of sales history for market comps (default: 36)")
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
        run_lookup(limit=args.limit, all_missing=args.all_missing, comp_months=args.comp_months)
