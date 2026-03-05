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
