"""
Airtable sync module — uploads scraped data from CSVs to Airtable.

Usage:
    python airtable_sync.py                  # sync from default output dir
    python airtable_sync.py --dir path/to/csvs  # sync from custom dir

Requires AIRTABLE_API_KEY and AIRTABLE_BASE_ID in .env
"""

import os
import sys
import argparse
import warnings
import pandas as pd
from dotenv import load_dotenv

warnings.simplefilter(action="ignore")
load_dotenv()

from config import AIRTABLE_TABLE_NAMES, AIRTABLE_EXCLUDE_COLUMNS, OUTPUT_DIR

AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")

# CSV filename -> config key mapping
CSV_MAP = {
    "Property.csv": "property",
    "Transaction.csv": "transaction",
    "Chargees.csv": "charges",
    "Parties.csv": "parties",
}


def get_table(table_name):
    """Get a pyairtable Table instance."""
    from pyairtable import Api
    api = Api(AIRTABLE_API_KEY)
    return api.table(AIRTABLE_BASE_ID, table_name)


def upload_dataframe(df, config_key):
    """Upload a DataFrame to the corresponding Airtable table using batch upsert."""
    table_name = AIRTABLE_TABLE_NAMES[config_key]
    exclude_cols = AIRTABLE_EXCLUDE_COLUMNS.get(config_key, [])

    # Drop columns not meant for Airtable
    upload_df = df.drop(columns=[c for c in exclude_cols if c in df.columns], errors='ignore')

    # Replace NaN with empty string for Airtable
    upload_df = upload_df.fillna("")

    table = get_table(table_name)

    # Convert rows to Airtable record format
    records = []
    for _, row in upload_df.iterrows():
        fields = {col: str(val) for col, val in row.items()}
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
                replace=True,
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

    success = True
    for csv_file, config_key in CSV_MAP.items():
        csv_path = os.path.join(output_dir, csv_file)
        if not os.path.exists(csv_path):
            print(f"  Skipping {csv_file}: file not found")
            continue

        try:
            df = pd.read_csv(csv_path)
            if df.empty:
                print(f"  Skipping {csv_file}: empty")
                continue
            upload_dataframe(df, config_key)
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
