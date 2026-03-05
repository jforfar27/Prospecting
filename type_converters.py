"""
Type conversion functions for Airtable sync.

Converts scraped string values to proper Airtable field types
(currency, date, percent, number, checkbox).
"""

import re
from datetime import datetime
import pandas as pd


def parse_currency(val):
    """Convert '$1,234,567.89' or '1234567.89' to float. Returns None if unparseable."""
    if not val or (isinstance(val, float) and pd.isna(val)):
        return None
    cleaned = re.sub(r'[$,]', '', str(val).strip())
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_date(val):
    """Convert date strings to ISO 8601 (YYYY-MM-DD) for Airtable date fields.

    Handles formats from the scraper:
    - '15 Jan 2024' (sale_date)
    - '01/15/2024' (charges registered_date, due_date)
    """
    if not val or (isinstance(val, float) and pd.isna(val)):
        return None
    val = str(val).strip()
    if not val:
        return None
    formats = [
        "%d %b %Y",   # '15 Jan 2024'
        "%m/%d/%Y",   # '01/15/2024'
        "%d/%m/%Y",   # '15/01/2024'
        "%Y-%m-%d",   # already ISO
    ]
    for fmt in formats:
        try:
            return datetime.strptime(val, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def parse_percent(val):
    """Convert '5.25%' or '5.25' to 0.0525 (Airtable percent uses 0-1 scale)."""
    if not val or (isinstance(val, float) and pd.isna(val)):
        return None
    cleaned = str(val).strip().rstrip('%')
    if not cleaned:
        return None
    try:
        return float(cleaned) / 100.0
    except ValueError:
        return None


def parse_number(val):
    """Convert string number to float (e.g., acreage)."""
    if not val or (isinstance(val, float) and pd.isna(val)):
        return None
    cleaned = re.sub(r'[,\s]', '', str(val).strip())
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_checkbox(val):
    """Convert portfolio flag to boolean for Airtable checkbox field."""
    if not val or (isinstance(val, float) and pd.isna(val)):
        return False
    return str(val).strip().lower() in ('portfolio', 'true', 'yes', '1')
