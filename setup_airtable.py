"""
Airtable base setup — creates the 4 required tables with correct field types.

Usage:
    python setup_airtable.py

Requires AIRTABLE_API_KEY and AIRTABLE_BASE_ID in .env
The base must already exist in Airtable (create it manually first).
This script creates the tables and fields inside it.
"""

import os
import sys
import time
from dotenv import load_dotenv

load_dotenv()

AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")

# Table definitions: name -> list of field dicts
# First field in each table becomes the primary field
TABLE_SCHEMAS = {
    "Properties": [
        {"name": "record_id", "type": "singleLineText"},
        {"name": "address", "type": "singleLineText"},
        {"name": "city", "type": "singleLineText"},
        {"name": "region", "type": "singleLineText"},
        {"name": "pin", "type": "singleLineText"},
        {"name": "site_description", "type": "multilineText"},
        {"name": "instrument_number", "type": "singleLineText"},
        {"name": "acreage", "type": "singleLineText"},
        {"name": "assessment_roll_number", "type": "singleLineText"},
    ],
    "Transactions": [
        {"name": "record_id", "type": "singleLineText"},
        {"name": "sale_date", "type": "singleLineText"},
        {"name": "purchase_price", "type": "singleLineText"},
        {"name": "cash", "type": "singleLineText"},
        {"name": "assumed_vbt_debt", "type": "singleLineText"},
        {"name": "portfolio_flag", "type": "singleLineText"},
    ],
    "Charges": [
        {"name": "record_id", "type": "singleLineText"},
        {"name": "chargee", "type": "singleLineText"},
        {"name": "principal", "type": "singleLineText"},
        {"name": "rate", "type": "singleLineText"},
        {"name": "registered_date", "type": "singleLineText"},
        {"name": "due_date", "type": "singleLineText"},
    ],
    "Parties": [
        {"name": "record_id", "type": "singleLineText"},
        {"name": "party_role", "type": "singleLineText"},
        {"name": "legal_name", "type": "singleLineText"},
        {"name": "legal_name_2", "type": "singleLineText"},
        {"name": "phone", "type": "phoneNumber"},
        {"name": "attention", "type": "singleLineText"},
        {"name": "care_of", "type": "singleLineText"},
        {"name": "address", "type": "singleLineText"},
        {"name": "city", "type": "singleLineText"},
        {"name": "province", "type": "singleLineText"},
        {"name": "postal_code", "type": "singleLineText"},
    ],
}


def setup_tables():
    if not AIRTABLE_API_KEY or not AIRTABLE_BASE_ID:
        print("ERROR: AIRTABLE_API_KEY and AIRTABLE_BASE_ID must be set in .env")
        sys.exit(1)

    from pyairtable import Api

    api = Api(AIRTABLE_API_KEY)
    base = api.base(AIRTABLE_BASE_ID)

    # Check existing tables to avoid duplicates
    print(f"Connecting to base {AIRTABLE_BASE_ID}...")
    try:
        schema = base.schema()
        existing_tables = {t.name for t in schema.tables}
        print(f"Existing tables: {existing_tables or 'none'}")
    except Exception as e:
        print(f"Error reading base schema: {e}")
        print("Make sure your PAT has schema.bases:read and schema.bases:write scopes.")
        sys.exit(1)

    created = []
    skipped = []

    for table_name, fields in TABLE_SCHEMAS.items():
        if table_name in existing_tables:
            print(f"  Skipping '{table_name}' — already exists")
            skipped.append(table_name)
            continue

        print(f"  Creating '{table_name}' with {len(fields)} fields...", end=" ")
        try:
            base.create_table(table_name, fields=fields)
            print("OK")
            created.append(table_name)
            time.sleep(0.3)  # Respect rate limits
        except Exception as e:
            print(f"FAILED: {e}")

    print(f"\nDone! Created: {created or 'none'}. Skipped: {skipped or 'none'}.")
    if created:
        print("Your Airtable base is ready. You can now run:")
        print("  python realtrack_scraper.py --sync")


if __name__ == "__main__":
    setup_tables()
