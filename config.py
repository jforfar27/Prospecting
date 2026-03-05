import os

# --- Search Configuration ---
# Modify these to change what the scraper searches for on RealTrack
SEARCH_CONFIG = {
    "property_type": "Multi Residential",
    "start_year": "96",
    "min_amount": "4000000",
    "records_per_page": "50 records",
}

# --- Output Configuration ---
OUTPUT_DIR = "output"
DB_FILE = os.path.join(OUTPUT_DIR, "RealTrack.db")

# --- Airtable Configuration ---
# Table names in your Airtable base (must match exactly)
AIRTABLE_TABLE_NAMES = {
    "property": "Properties",
    "transaction": "Transactions",
    "charges": "Charges",
    "parties": "Parties",
}

# Columns to exclude from Airtable sync (stored in SQLite only)
AIRTABLE_EXCLUDE_COLUMNS = {
    "property": ["raw_text"],
}

# Whether to create linked record fields during setup
SETUP_LINKED_RECORDS = True

# CRM-only fields — never overwritten by sync (managed manually in Airtable)
CRM_ONLY_FIELDS = {
    "property": ["status", "priority", "notes", "next_step", "next_step_date", "contact_log", "tags"],
    "transaction": [],
    "charges": ["maturity_status"],
    "parties": ["email", "contact_notes", "last_contacted", "contact_status"],
}

# Per-table field type converters for Airtable sync
# Maps field names to converter functions from type_converters.py
def _build_field_converters():
    from type_converters import parse_currency, parse_date, parse_percent, parse_number, parse_checkbox
    return {
        "property": {
            "acreage": parse_number,
        },
        "transaction": {
            "sale_date": parse_date,
            "purchase_price": parse_currency,
            "cash": parse_currency,
            "assumed_vbt_debt": parse_currency,
            "portfolio_flag": parse_checkbox,
        },
        "charges": {
            "principal": parse_currency,
            "rate": parse_percent,
            "registered_date": parse_date,
            "due_date": parse_date,
        },
        "parties": {},
    }

FIELD_CONVERTERS = _build_field_converters()
