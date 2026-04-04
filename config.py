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
    "master": "Master",
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

# --- Entity Resolution / BD Outreach Configuration ---
ENTITY_RESOLUTION_CONFIG = {
    # Only resolve parties from transactions in the last N years
    "lookback_years": 5,
    # Minimum properties to qualify as a "portfolio player"
    "portfolio_threshold": 3,
    # Skip these party names (not real companies)
    "ignore_names": [
        "Named Individual(s)",
        "Ontario Superior Court of Justice",
        "Her Majesty the Queen",
        "Canada Mortgage and Housing Corporation",
        "CMHC",
    ],
    # Known SPE → parent company mappings (manually curated overrides)
    # Add entries here as you confirm them — these take priority over auto-resolution
    "manual_mappings": {
        # "SPE Name": "Parent Company",
        # Example: "DD 149 Henry Ltd": "Starlight Investments",
    },
    # Fuzzy dedup: minimum similarity ratio (0.0-1.0) to consider two names a match
    "fuzzy_threshold": 0.85,
}

# --- Pipeline / Scheduling Configuration ---
PIPELINE_CONFIG = {
    # Log retention (days) — logs older than this are auto-deleted
    "log_retention_days": 30,
    # Default cron schedule (weekly Sunday 2 AM) — override with PIPELINE_CRON_SCHEDULE env var
    "default_cron_schedule": "0 2 * * 0",
}

# --- Charge Maturity Configuration ---
CHARGE_MATURITY_CONFIG = {
    "buckets_months": [3, 6, 9, 12],
    "min_principal": 0,  # minimum charge principal to include
}

# --- Charge Maturity Alert Configuration ---
MATURITY_ALERT_CONFIG = {
    "alert_bucket": "0-3 months",
    "min_alert_principal": 100000,  # only alert on charges >= $100K
}
