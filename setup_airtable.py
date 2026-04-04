"""
Airtable base setup — creates the 4 required tables with correct field types.

Usage:
    python setup_airtable.py

Requires AIRTABLE_API_KEY and AIRTABLE_BASE_ID in .env
The base must already exist in Airtable (create it manually first).
This script creates the tables and fields inside it.

Migration note: If tables already exist with old field types, delete them
in Airtable UI first, then re-run this script.
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
# Fields marked with # CRM are managed manually in Airtable, never overwritten by sync
TABLE_SCHEMAS = {
    "Properties": [
        {"name": "record_id", "type": "singleLineText"},
        {"name": "address", "type": "singleLineText"},
        {"name": "city", "type": "singleLineText"},
        {"name": "region", "type": "singleLineText"},
        {"name": "pin", "type": "singleLineText"},
        {"name": "site_description", "type": "multilineText"},
        {"name": "instrument_number", "type": "singleLineText"},
        {"name": "acreage", "type": "number", "options": {"precision": 2}},
        {"name": "assessment_roll_number", "type": "singleLineText"},
        {"name": "unit_count", "type": "number", "options": {"precision": 0}},
        {"name": "price_per_unit", "type": "currency", "options": {"precision": 2, "symbol": "$"}},
        {"name": "market_median_ppu", "type": "currency", "options": {"precision": 0, "symbol": "$"}},
        {"name": "market_avg_ppu", "type": "currency", "options": {"precision": 0, "symbol": "$"}},
        {"name": "market_min_ppu", "type": "currency", "options": {"precision": 0, "symbol": "$"}},
        {"name": "market_max_ppu", "type": "currency", "options": {"precision": 0, "symbol": "$"}},
        {"name": "comp_count", "type": "number", "options": {"precision": 0}},
        {"name": "ppu_vs_market", "type": "singleLineText"},
        # CRM fields
        {"name": "status", "type": "singleSelect", "options": {
            "choices": [
                {"name": "New Lead", "color": "blueLight2"},
                {"name": "Researching", "color": "cyanLight2"},
                {"name": "Contacted", "color": "yellowLight2"},
                {"name": "Meeting", "color": "orangeLight2"},
                {"name": "Proposal", "color": "purpleLight2"},
                {"name": "Won", "color": "greenLight2"},
                {"name": "Lost", "color": "redLight2"},
                {"name": "On Hold", "color": "grayLight2"},
            ]
        }},
        {"name": "priority", "type": "singleSelect", "options": {
            "choices": [
                {"name": "High", "color": "redLight2"},
                {"name": "Medium", "color": "yellowLight2"},
                {"name": "Low", "color": "grayLight2"},
            ]
        }},
        {"name": "notes", "type": "multilineText"},
        {"name": "next_step", "type": "singleLineText"},
        {"name": "next_step_date", "type": "date", "options": {"dateFormat": {"name": "local"}}},
        {"name": "contact_log", "type": "multilineText"},
        {"name": "tags", "type": "multipleSelects", "options": {
            "choices": [
                {"name": "Distressed"},
                {"name": "Value-Add"},
                {"name": "Mortgage Maturing"},
                {"name": "Portfolio"},
                {"name": "Off-Market"},
                {"name": "High Priority"},
            ]
        }},
    ],
    "Transactions": [
        {"name": "record_id", "type": "singleLineText"},
        {"name": "property_record_id", "type": "singleLineText"},
        {"name": "sale_date", "type": "date", "options": {"dateFormat": {"name": "local"}}},
        {"name": "purchase_price", "type": "currency", "options": {"precision": 2, "symbol": "$"}},
        {"name": "cash", "type": "currency", "options": {"precision": 2, "symbol": "$"}},
        {"name": "assumed_vbt_debt", "type": "currency", "options": {"precision": 2, "symbol": "$"}},
        {"name": "portfolio_flag", "type": "checkbox"},
    ],
    "Charges": [
        {"name": "record_id", "type": "singleLineText"},
        {"name": "property_record_id", "type": "singleLineText"},
        {"name": "chargee", "type": "singleLineText"},
        {"name": "principal", "type": "currency", "options": {"precision": 2, "symbol": "$"}},
        {"name": "rate", "type": "percent", "options": {"precision": 2}},
        {"name": "registered_date", "type": "date", "options": {"dateFormat": {"name": "local"}}},
        {"name": "due_date", "type": "date", "options": {"dateFormat": {"name": "local"}}},
        # CRM field
        {"name": "maturity_status", "type": "singleSelect", "options": {
            "choices": [
                {"name": "Active"},
                {"name": "Maturing <6mo"},
                {"name": "Maturing <12mo"},
                {"name": "Matured"},
                {"name": "Discharged"},
            ]
        }},
    ],
    "Parties": [
        {"name": "record_id", "type": "singleLineText"},
        {"name": "property_record_id", "type": "singleLineText"},
        {"name": "party_role", "type": "singleSelect", "options": {
            "choices": [
                {"name": "Transferor"},
                {"name": "Transferee"},
            ]
        }},
        {"name": "legal_name", "type": "singleLineText"},
        {"name": "legal_name_2", "type": "singleLineText"},
        {"name": "phone", "type": "phoneNumber"},
        {"name": "attention", "type": "singleLineText"},
        {"name": "care_of", "type": "singleLineText"},
        {"name": "address", "type": "singleLineText"},
        {"name": "city", "type": "singleLineText"},
        {"name": "province", "type": "singleLineText"},
        {"name": "postal_code", "type": "singleLineText"},
        # CRM fields
        {"name": "email", "type": "email"},
        {"name": "contact_notes", "type": "multilineText"},
        {"name": "last_contacted", "type": "date", "options": {"dateFormat": {"name": "local"}}},
        {"name": "contact_status", "type": "singleSelect", "options": {
            "choices": [
                {"name": "Not Contacted"},
                {"name": "Attempted"},
                {"name": "Responded"},
                {"name": "Active Relationship"},
                {"name": "Do Not Contact"},
            ]
        }},
    ],
}

# Order matters: Properties must be created first for linked records
TABLE_CREATE_ORDER = ["Properties", "Transactions", "Charges", "Parties"]


def setup_tables():
    if not AIRTABLE_API_KEY or not AIRTABLE_BASE_ID:
        print("ERROR: AIRTABLE_API_KEY and AIRTABLE_BASE_ID must be set in .env")
        sys.exit(1)

    from pyairtable import Api
    from config import SETUP_LINKED_RECORDS

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

    # Create tables in order (Properties first)
    for table_name in TABLE_CREATE_ORDER:
        fields = TABLE_SCHEMAS[table_name]
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

    # Optional: Add linked record fields to child tables
    if SETUP_LINKED_RECORDS and created:
        print("\nSetting up linked record fields...")
        try:
            # Refresh schema to get table IDs
            schema = base.schema()
            table_id_map = {t.name: t.id for t in schema.tables}

            if "Properties" in table_id_map:
                properties_table_id = table_id_map["Properties"]
                child_tables = ["Transactions", "Charges", "Parties"]

                for child_name in child_tables:
                    if child_name not in table_id_map:
                        continue

                    # Check if "Property" link field already exists
                    child_schema = next(t for t in schema.tables if t.name == child_name)
                    existing_fields = {f.name for f in child_schema.fields}
                    if "Property" in existing_fields:
                        print(f"  {child_name}: 'Property' link field already exists — skipping")
                        continue

                    print(f"  Adding 'Property' link to {child_name}...", end=" ")
                    try:
                        child_table = api.table(AIRTABLE_BASE_ID, child_name)
                        child_table.create_field(
                            "Property",
                            "multipleRecordLinks",
                            options={"linkedTableId": properties_table_id}
                        )
                        print("OK")
                        time.sleep(0.3)
                    except Exception as e:
                        print(f"FAILED: {e}")
                        print("  (You can add linked record fields manually in Airtable UI)")
        except Exception as e:
            print(f"  Error setting up linked records: {e}")
            print("  (You can add linked record fields manually in Airtable UI)")

    print(f"\nDone! Created: {created or 'none'}. Skipped: {skipped or 'none'}.")
    if created:
        print("Your Airtable base is ready. You can now run:")
        print("  python realtrack_scraper.py --sync")
        print("\nRecommended: Set up views in Airtable UI:")
        print("  - Properties: 'Deal Pipeline' (Kanban by status)")
        print("  - Properties: 'New Leads' (filter status=New Lead)")
        print("  - Charges: 'Maturing Soon' (filter due_date within 12 months)")
        print("  - Parties: 'Needs Outreach' (filter contact_status=Not Contacted)")


if __name__ == "__main__":
    setup_tables()
