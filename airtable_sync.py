"""
Airtable sync module — uploads scraped data from CSVs to Airtable.

Usage:
    python airtable_sync.py                  # sync from default output dir
    python airtable_sync.py --dir path/to/csvs  # sync from custom dir

Requires AIRTABLE_API_KEY and AIRTABLE_BASE_ID in .env

Type conversion: Converts scraped string values to proper Airtable types
(currency, date, percent, checkbox) before upload.

CRM preservation: Uses replace=False (merge mode) so manually-entered
CRM fields (status, notes, priority, etc.) are never overwritten by sync.
"""

import os
import sys
import argparse
import warnings
import sqlite3
import pandas as pd
from dotenv import load_dotenv

warnings.simplefilter(action="ignore")
load_dotenv()

from config import (
    AIRTABLE_TABLE_NAMES,
    AIRTABLE_EXCLUDE_COLUMNS,
    FIELD_CONVERTERS,
    SETUP_LINKED_RECORDS,
    OUTPUT_DIR,
    DB_FILE,
)

AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")

# CSV filename -> config key mapping
CSV_MAP = {
    "Property.csv": "property",
    "Transaction.csv": "transaction",
    "Chargees.csv": "charges",
    "Parties.csv": "parties",
}

# Sync order: Properties first (needed for linked record ID mapping)
SYNC_ORDER = ["Property.csv", "Transaction.csv", "Chargees.csv", "Parties.csv"]


def get_table(table_name):
    """Get a pyairtable Table instance."""
    from pyairtable import Api
    api = Api(AIRTABLE_API_KEY)
    return api.table(AIRTABLE_BASE_ID, table_name)


def build_property_id_map():
    """Fetch all Properties records and return {record_id: airtable_rec_id} mapping."""
    table = get_table(AIRTABLE_TABLE_NAMES["property"])
    records = table.all(fields=["record_id"])
    return {r["fields"]["record_id"]: r["id"] for r in records if "record_id" in r["fields"]}


def upload_dataframe(df, config_key, property_id_map=None):
    """Upload a DataFrame to the corresponding Airtable table using batch upsert.

    Uses type converters to send proper field types (currency, date, etc.)
    and replace=False to preserve CRM-only fields.
    """
    table_name = AIRTABLE_TABLE_NAMES[config_key]
    exclude_cols = AIRTABLE_EXCLUDE_COLUMNS.get(config_key, [])
    converters = FIELD_CONVERTERS.get(config_key, {})

    # Drop columns not meant for Airtable
    upload_df = df.drop(columns=[c for c in exclude_cols if c in df.columns], errors='ignore')

    table = get_table(table_name)

    # Convert rows to Airtable record format with proper types
    records = []
    for _, row in upload_df.iterrows():
        fields = {}
        for col, val in row.items():
            if col in converters:
                converted = converters[col](val)
                if converted is not None:
                    fields[col] = converted
                # Omit None — don't send null for typed fields
            else:
                # Default: send as string, skip empty values
                str_val = "" if pd.isna(val) else str(val)
                if str_val:
                    fields[col] = str_val

        # Optionally populate linked record field
        if property_id_map and "property_record_id" in row and config_key != "property":
            prop_rec_id = property_id_map.get(row["property_record_id"])
            if prop_rec_id:
                fields["Property"] = [prop_rec_id]

        records.append({"fields": fields})

    # Batch upsert in chunks of 10 (Airtable API limit)
    total = len(records)
    upserted = 0
    chunk_size = 10

    for i in range(0, total, chunk_size):
        chunk = records[i:i + chunk_size]
        try:
            table.batch_upsert(
                chunk,
                key_fields=["record_id"],
                replace=False,  # Merge mode: preserve CRM-only fields
            )
            upserted += len(chunk)
            print(f"  {table_name}: {upserted}/{total} records synced", end="\r", flush=True)
        except Exception as e:
            print(f"\n  Error uploading to {table_name} (batch {i // chunk_size + 1}): {e}")

    print(f"  {table_name}: {upserted}/{total} records synced")


def build_master_df():
    """Build a flat denormalized DataFrame joining Property + Transaction + Parties + Charges."""
    from type_converters import parse_currency, parse_date, parse_percent, parse_number, parse_checkbox

    conn = sqlite3.connect(DB_FILE)
    df = pd.read_sql_query('''
        SELECT
            p.record_id       AS property_record_id,
            p.address,
            p.city,
            p.region,
            p.pin,
            p.site_description,
            t.sale_date,
            t.purchase_price,
            t.cash,
            t.assumed_vbt_debt,
            t.portfolio_flag,
            transferor.legal_name   AS transferor,
            transferor.phone        AS transferor_phone,
            transferor.address      AS transferor_address,
            transferor.city         AS transferor_city,
            transferee.legal_name   AS transferee,
            transferee.phone        AS transferee_phone,
            transferee.address      AS transferee_address,
            transferee.city         AS transferee_city,
            pc.chargee             AS primary_chargee,
            pc.principal           AS charge_principal,
            pc.rate                AS charge_rate,
            pc.due_date            AS charge_due_date,
            pc.num_charges
        FROM Property p
        LEFT JOIN "Transaction" t
            ON p.record_id = t.property_record_id
        LEFT JOIN Parties transferor
            ON p.record_id = transferor.property_record_id
            AND transferor.party_role = 'Transferor'
        LEFT JOIN Parties transferee
            ON p.record_id = transferee.property_record_id
            AND transferee.party_role = 'Transferee'
        LEFT JOIN (
            SELECT
                property_record_id,
                chargee,
                principal,
                rate,
                due_date,
                COUNT(*) OVER (PARTITION BY property_record_id) AS num_charges,
                ROW_NUMBER() OVER (PARTITION BY property_record_id
                                   ORDER BY ROWID) AS rn
            FROM Charges
        ) pc ON p.record_id = pc.property_record_id AND pc.rn = 1
    ''', conn)

    # Build a lookup from legal_name/spe → parent_company using contacts table
    try:
        contacts = pd.read_sql_query(
            "SELECT parent_company, spe_names FROM contacts", conn
        )
        name_to_parent = {}
        for _, c in contacts.iterrows():
            name_to_parent[c["parent_company"]] = c["parent_company"]
            if c["spe_names"]:
                for spe in str(c["spe_names"]).split("; "):
                    spe = spe.strip()
                    if spe:
                        name_to_parent[spe] = c["parent_company"]
    except Exception:
        name_to_parent = {}

    conn.close()

    # Resolve buyer_parent and seller_parent using the lookup
    if name_to_parent:
        df["buyer_parent"] = df["transferee"].map(
            lambda x: name_to_parent.get(x, x) if pd.notna(x) and x else ""
        )
        df["seller_parent"] = df["transferor"].map(
            lambda x: name_to_parent.get(x, x) if pd.notna(x) and x else ""
        )

    # Apply type converters matching Master table field types
    converters = {
        "sale_date": parse_date,
        "purchase_price": parse_currency,
        "cash": parse_currency,
        "assumed_vbt_debt": parse_currency,
        "portfolio_flag": parse_checkbox,
        "charge_principal": parse_currency,
        "charge_rate": lambda v: str(v) if v and not pd.isna(v) else None,
        "charge_due_date": parse_date,
        "num_charges": parse_number,
    }

    records = []
    for _, row in df.iterrows():
        fields = {}
        for col, val in row.items():
            if col in converters:
                converted = converters[col](val)
                if converted is not None:
                    fields[col] = converted
            else:
                str_val = "" if pd.isna(val) else str(val)
                if str_val:
                    fields[col] = str_val
        records.append({"fields": fields})

    return records


def sync_master():
    """Sync the flat Master table from SQLite joined data."""
    table_name = AIRTABLE_TABLE_NAMES["master"]
    table = get_table(table_name)
    records = build_master_df()
    total = len(records)
    upserted = 0
    chunk_size = 10

    print(f"  Syncing Master table ({total} flat records)...")
    for i in range(0, total, chunk_size):
        chunk = records[i:i + chunk_size]
        try:
            table.batch_upsert(
                chunk,
                key_fields=["property_record_id"],
                replace=False,  # Preserve CRM-only fields (status, priority, notes)
            )
            upserted += len(chunk)
            print(f"  Master: {upserted}/{total} records synced", end="\r", flush=True)
        except Exception as e:
            print(f"\n  Error uploading to Master (batch {i // chunk_size + 1}): {e}")

    print(f"  Master: {upserted}/{total} records synced")
    return upserted == total


def sync_all(output_dir=None):
    """Sync all CSV files from output directory to Airtable."""
    if not AIRTABLE_API_KEY or not AIRTABLE_BASE_ID:
        print("WARNING: AIRTABLE_API_KEY or AIRTABLE_BASE_ID not set in .env — skipping Airtable sync")
        return False

    output_dir = output_dir or OUTPUT_DIR
    print(f"Syncing to Airtable from {output_dir}...")

    # Build property ID map for linked records (if enabled)
    property_id_map = None
    if SETUP_LINKED_RECORDS:
        # Sync Properties first, then build the map
        csv_path = os.path.join(output_dir, "Property.csv")
        if os.path.exists(csv_path):
            try:
                df = pd.read_csv(csv_path)
                if not df.empty:
                    upload_dataframe(df, "property")
            except Exception as e:
                print(f"  Error processing Property.csv: {e}")

        try:
            property_id_map = build_property_id_map()
            print(f"  Built property ID map: {len(property_id_map)} records")
        except Exception as e:
            print(f"  Warning: Could not build property ID map: {e}")
            print("  (Linked record fields will not be populated)")
            property_id_map = None

    success = True
    for csv_file in SYNC_ORDER:
        config_key = CSV_MAP[csv_file]

        # Skip Properties if already synced above for linked records
        if SETUP_LINKED_RECORDS and config_key == "property":
            continue

        csv_path = os.path.join(output_dir, csv_file)
        if not os.path.exists(csv_path):
            print(f"  Skipping {csv_file}: file not found")
            continue

        try:
            df = pd.read_csv(csv_path)
            if df.empty:
                print(f"  Skipping {csv_file}: empty")
                continue
            upload_dataframe(df, config_key, property_id_map=property_id_map)
        except Exception as e:
            print(f"  Error processing {csv_file}: {e}")
            success = False

    # Sync Master table (flat denormalized view)
    try:
        if not sync_master():
            success = False
    except Exception as e:
        print(f"  Error syncing Master table: {e}")
        success = False

    status = "complete" if success else "completed with errors"
    print(f"Airtable sync {status}!")
    return success


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sync RealTrack data to Airtable")
    parser.add_argument("--dir", default=OUTPUT_DIR, help="Directory containing CSV files")
    args = parser.parse_args()
    sync_all(args.dir)
