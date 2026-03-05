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

    status = "complete" if success else "completed with errors"
    print(f"Airtable sync {status}!")
    return success


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sync RealTrack data to Airtable")
    parser.add_argument("--dir", default=OUTPUT_DIR, help="Directory containing CSV files")
    args = parser.parse_args()
    sync_all(args.dir)
