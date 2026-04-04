"""
CMHC Rental Market Survey reference data for estimating rental income and cap rates.

Data source: CMHC Rental Market Survey (October 2025), published December 2025.
Update these tables when new CMHC surveys are released (typically annually in December).

Usage:
    python cmhc_data.py                          # show current CMHC data
    python cmhc_data.py --compute                # compute rental estimates for all properties
    python cmhc_data.py --compute --zone zone_3  # compute for a specific zone only
"""

import os
import sys
import argparse
import sqlite3
import pandas as pd

import config

# ---------------------------------------------------------------------------
# CMHC Zone Definitions — Toronto CMA
# ---------------------------------------------------------------------------
# Zones are defined by CMHC and group census tracts within the Toronto CMA.
# Source: CMHC Housing Market Information Portal
# https://www03.cmhc-schl.gc.ca/hmip-pimh/en/TableMapChart/Table?TableId=2.1.31.3&GeographyId=2270&GeographyTypeId=3

CMHC_ZONES = {
    "zone_1": {
        "name": "Toronto Downtown",
        "description": "Downtown core, Waterfront, CityPlace, St. Lawrence",
    },
    "zone_2": {
        "name": "Toronto East",
        "description": "East York, Riverdale, Danforth, Beaches",
    },
    "zone_3": {
        "name": "Toronto Central",
        "description": "Midtown, Yonge-Eglinton, Forest Hill, Davisville, St. Clair",
    },
    "zone_4": {
        "name": "Toronto North",
        "description": "North York, Willowdale, Don Mills",
    },
    "zone_5": {
        "name": "Toronto West",
        "description": "High Park, Parkdale, Junction, Bloor West",
    },
    "zone_6": {
        "name": "Etobicoke North",
        "description": "Rexdale, Islington, Humber",
    },
    "zone_7": {
        "name": "Etobicoke South",
        "description": "Mimico, New Toronto, Long Branch",
    },
    "zone_8": {
        "name": "Scarborough",
        "description": "Scarborough City Centre, Agincourt, Malvern",
    },
}

# ---------------------------------------------------------------------------
# CMHC Rental Market Data — October 2025 Survey
# ---------------------------------------------------------------------------
# Average monthly rents by bedroom type for purpose-built rental apartments.
# Source: CMHC Rental Market Survey, October 2025 (published Dec 2025)
#
# To update: replace values below with new survey data from CMHC portal.
# Zone-level bedroom breakdowns estimated from CMA ratios where exact
# zone data was not accessible. Values marked (est) are derived from the
# confirmed zone total and CMA bedroom-type proportions.
#
# CMA-level confirmed data points:
#   - Toronto CMA total average: $1,917
#   - Toronto CMA 2-bed average: $2,034
#   - New tenant 1-bed: $2,073 / Long-term 1-bed: $1,711
#   - Toronto Central zone total: $2,163
#   - Toronto Central vacancy: 2.8%
#   - Toronto Central universe: ~36,050 units

SURVEY_YEAR = 2025

CMHC_RENTS = {
    # Zone 3: Toronto Central — confirmed total $2,163, vacancy 2.8%
    "zone_3": {
        "survey_date": "October 2025",
        "vacancy_rate": 2.8,  # %
        "universe": 36050,
        "avg_rent": {
            "bachelor": 1450,   # (est) from CMA ratio
            "one_bed": 1935,    # (est) from CMA ratio
            "two_bed": 2295,    # (est) from CMA ratio
            "three_bed_plus": 2810,  # (est) from CMA ratio
            "total": 2163,      # confirmed CMHC
        },
    },
    # Toronto CMA overall — for reference / fallback
    "toronto_cma": {
        "survey_date": "October 2025",
        "vacancy_rate": 3.0,  # confirmed CMHC
        "universe": None,
        "avg_rent": {
            "bachelor": 1285,   # (est)
            "one_bed": 1711,    # confirmed CMHC (long-term tenant)
            "two_bed": 2034,    # confirmed CMHC
            "three_bed_plus": 2490,  # (est)
            "total": 1917,      # confirmed CMHC
        },
    },
}

# ---------------------------------------------------------------------------
# Default assumptions for cap rate calculation
# ---------------------------------------------------------------------------

DEFAULT_ASSUMPTIONS = {
    # Operating expense ratio (% of gross rental income)
    # Typical multi-res in Ontario: 35-50% depending on age/condition
    "expense_ratio": 0.40,
    # Weighted average bedroom mix for estimating per-unit rent
    # when unit breakdown is unknown. Based on typical Ontario multi-res.
    "bedroom_mix": {
        "bachelor": 0.05,
        "one_bed": 0.40,
        "two_bed": 0.40,
        "three_bed_plus": 0.15,
    },
}


# ---------------------------------------------------------------------------
# Rental income and cap rate calculations
# ---------------------------------------------------------------------------

def estimate_avg_rent_per_unit(zone_key="zone_3", bedroom_mix=None):
    """Estimate weighted average monthly rent per unit based on bedroom mix.

    Uses CMHC average rents for the zone and a bedroom mix (default from
    DEFAULT_ASSUMPTIONS) to compute a blended per-unit monthly rent.
    """
    zone_data = CMHC_RENTS.get(zone_key)
    if not zone_data:
        return None

    mix = bedroom_mix or DEFAULT_ASSUMPTIONS["bedroom_mix"]
    rents = zone_data["avg_rent"]

    weighted_rent = (
        rents["bachelor"] * mix["bachelor"]
        + rents["one_bed"] * mix["one_bed"]
        + rents["two_bed"] * mix["two_bed"]
        + rents["three_bed_plus"] * mix["three_bed_plus"]
    )
    return round(weighted_rent, 2)


def estimate_rental_income(unit_count, zone_key="zone_3", bedroom_mix=None):
    """Estimate gross annual rental income for a property.

    Returns (monthly_income, annual_income) or (None, None).
    """
    if not unit_count or unit_count <= 0:
        return None, None

    avg_rent = estimate_avg_rent_per_unit(zone_key, bedroom_mix)
    if not avg_rent:
        return None, None

    monthly = round(avg_rent * unit_count, 2)
    annual = round(monthly * 12, 2)
    return monthly, annual


def estimate_noi(annual_income, expense_ratio=None):
    """Estimate Net Operating Income from gross income and expense ratio."""
    if not annual_income or annual_income <= 0:
        return None
    ratio = expense_ratio or DEFAULT_ASSUMPTIONS["expense_ratio"]
    return round(annual_income * (1 - ratio), 2)


def estimate_cap_rate(purchase_price, annual_income, expense_ratio=None):
    """Estimate capitalization rate: NOI / purchase_price × 100.

    Returns cap rate as percentage (e.g. 4.5 means 4.5%), or None.
    """
    if not purchase_price or purchase_price <= 0:
        return None
    noi = estimate_noi(annual_income, expense_ratio)
    if not noi:
        return None
    return round((noi / purchase_price) * 100, 2)


# ---------------------------------------------------------------------------
# Database integration
# ---------------------------------------------------------------------------

def _parse_number(val):
    """Parse a number from text."""
    if not val or (isinstance(val, float) and pd.isna(val)):
        return None
    import re
    cleaned = re.sub(r'[$,\s]', '', str(val).strip())
    try:
        return float(cleaned)
    except ValueError:
        return None


def ensure_cmhc_columns(db_file):
    """Add CMHC-related columns to Property table if missing."""
    conn = sqlite3.connect(db_file)
    cur = conn.cursor()
    try:
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='Property'")
        if not cur.fetchone():
            return
        cur.execute('PRAGMA table_info("Property")')
        existing = {row[1] for row in cur.fetchall()}
        for col in ["cmhc_zone", "estimated_monthly_rent", "estimated_annual_income",
                     "estimated_noi", "estimated_cap_rate"]:
            if col not in existing:
                cur.execute(f'ALTER TABLE "Property" ADD COLUMN "{col}" TEXT')
        conn.commit()
    finally:
        conn.close()


def compute_cmhc_estimates(db_file, zone_key="zone_3"):
    """Compute CMHC-based rental income and cap rate estimates for properties.

    Applies to all properties that have unit_count and a purchase_price.
    Returns count of updated properties.
    """
    ensure_cmhc_columns(db_file)
    conn = sqlite3.connect(db_file)
    try:
        # Check tables exist
        tables = pd.read_sql_query(
            "SELECT name FROM sqlite_master WHERE type='table'", conn
        )
        table_names = set(tables["name"].tolist())
        if "Property" not in table_names or "Transaction" not in table_names:
            return 0

        # Load properties with unit_count, joined to transaction for purchase_price
        df = pd.read_sql_query("""
            SELECT
                p.record_id,
                p.unit_count,
                t.purchase_price
            FROM "Property" p
            INNER JOIN "Transaction" t ON t.property_record_id = p.record_id
            WHERE p.unit_count IS NOT NULL AND p.unit_count != ''
        """, conn)

        if df.empty:
            return 0

        # Deduplicate (first transaction per property)
        df = df.drop_duplicates(subset="record_id", keep="first")

        updated = 0
        for _, row in df.iterrows():
            units = _parse_number(row["unit_count"])
            price = _parse_number(row["purchase_price"])
            if not units or units <= 0:
                continue

            monthly, annual = estimate_rental_income(units, zone_key)
            if not monthly:
                continue

            noi = estimate_noi(annual)
            cap_rate = estimate_cap_rate(price, annual) if price else None

            conn.execute(
                """UPDATE "Property" SET
                    cmhc_zone = ?,
                    estimated_monthly_rent = ?,
                    estimated_annual_income = ?,
                    estimated_noi = ?,
                    estimated_cap_rate = ?
                WHERE record_id = ?""",
                (
                    zone_key,
                    str(round(monthly, 2)),
                    str(round(annual, 2)),
                    str(round(noi, 2)) if noi else None,
                    f"{cap_rate:.2f}%" if cap_rate else None,
                    row["record_id"],
                ),
            )
            updated += 1

        conn.commit()
        return updated
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Display / CLI
# ---------------------------------------------------------------------------

def show_cmhc_data(zone_key=None):
    """Print CMHC reference data for a zone or all zones."""
    zones = [zone_key] if zone_key else list(CMHC_RENTS.keys())

    for zk in zones:
        data = CMHC_RENTS.get(zk)
        if not data:
            print(f"No CMHC data for zone: {zk}")
            continue

        zone_info = CMHC_ZONES.get(zk, {})
        name = zone_info.get("name", zk)
        desc = zone_info.get("description", "")

        print(f"\n{'='*60}")
        print(f"CMHC Zone: {name} ({zk})")
        if desc:
            print(f"Areas: {desc}")
        print(f"Survey: {data['survey_date']}")
        print(f"{'='*60}")
        print(f"  Vacancy Rate:  {data['vacancy_rate']}%")
        if data.get("universe"):
            print(f"  Universe:      {data['universe']:,} units")
        print(f"\n  Average Rents (monthly):")
        for bed_type, rent in data["avg_rent"].items():
            label = bed_type.replace("_", " ").replace("bed", "-bed").title()
            print(f"    {label:20s} ${rent:,.0f}")

        # Show blended estimate
        avg_rent = estimate_avg_rent_per_unit(zk)
        if avg_rent:
            print(f"\n  Blended avg rent/unit:  ${avg_rent:,.0f}/mo")
            print(f"  (using default bedroom mix: {DEFAULT_ASSUMPTIONS['bedroom_mix']})")

        # Show example cap rate calc
        print(f"\n  Example: 100-unit building at $20M purchase price")
        monthly, annual = estimate_rental_income(100, zk)
        noi = estimate_noi(annual)
        cap = estimate_cap_rate(20_000_000, annual)
        if monthly and annual and noi and cap:
            print(f"    Gross monthly income:  ${monthly:,.0f}")
            print(f"    Gross annual income:   ${annual:,.0f}")
            print(f"    NOI (at {DEFAULT_ASSUMPTIONS['expense_ratio']:.0%} expenses): ${noi:,.0f}")
            print(f"    Estimated cap rate:    {cap:.2f}%")


def parse_args():
    parser = argparse.ArgumentParser(description="CMHC rental market data and estimates")
    parser.add_argument("--compute", action="store_true",
                        help="Compute rental income and cap rate estimates for properties in DB")
    parser.add_argument("--zone", default="zone_3",
                        help="CMHC zone key (default: zone_3 = Toronto Central)")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    if args.compute:
        db_file = config.DB_FILE
        if not os.path.exists(db_file):
            print(f"Database not found: {db_file}")
            print("Run the scraper first: python realtrack_scraper.py")
            sys.exit(1)
        updated = compute_cmhc_estimates(db_file, zone_key=args.zone)
        print(f"Updated {updated} properties with CMHC rental estimates (zone: {args.zone})")
    else:
        show_cmhc_data(args.zone)
