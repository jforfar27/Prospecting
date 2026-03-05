# Prospecting — RealTrack Scraper + Airtable Integration

Scrapes multi-residential property acquisition data from RealTrack.com and optionally syncs to Airtable for lead generation and deal tracking.

## What It Captures

| Table | Description |
|-------|-------------|
| **Property** | Address, city, region, PIN, site description, instrument number, acreage, assessment roll number |
| **Transaction** | Sale date, purchase price, cash amount, assumed/VTB debt, portfolio flag |
| **Charges** | Chargee name, principal, rate, due date, registered date |
| **Parties** | Transferor/Transferee names, phone, attention/care of, address |

## Setup

```bash
# Clone and enter the repo
git clone <repo-url> && cd Prospecting

# Create virtual environment
python -m venv venv
source venv/bin/activate  # or venv\Scripts\activate on Windows

# Install dependencies
pip install -r requirements.txt

# Configure credentials
cp .env.example .env
# Edit .env with your RealTrack username/password
# Add Airtable API key + base ID if using Airtable sync
```

**Requirements:** Chrome browser installed (the scraper uses undetected-chromedriver).

## Usage

### Basic scrape
```bash
python realtrack_scraper.py
```

### Headless mode (no browser window)
```bash
python realtrack_scraper.py --headless
```

### Scrape + auto-sync to Airtable
```bash
python realtrack_scraper.py --sync
```

### Resume an interrupted run
```bash
python realtrack_scraper.py --resume
```

### Custom search filters
```bash
python realtrack_scraper.py --type "Commercial" --min-amount 2000000 --start-year 00
```

### Manually sync CSVs to Airtable
```bash
python airtable_sync.py
```

## Configuration

Edit `config.py` to change default search parameters:

```python
SEARCH_CONFIG = {
    "property_type": "Multi Residential",
    "start_year": "96",
    "min_amount": "4000000",
    "records_per_page": "50 records",
}
```

## Output

All output files are written to the `output/` directory:
- `RealTrack.db` — SQLite database (primary data store)
- `Property.csv`, `Transaction.csv`, `Chargees.csv`, `Parties.csv` — individual table exports
- `realtrack_export_YYYYMMDD_HHMMSS.xlsx` — consolidated Excel report

## Airtable Setup

1. Create a new Airtable base (empty) at airtable.com
2. Generate a [Personal Access Token](https://airtable.com/create/tokens) with `schema.bases:read`, `schema.bases:write`, `data.records:read`, and `data.records:write` scopes
3. Add `AIRTABLE_API_KEY` and `AIRTABLE_BASE_ID` to your `.env` file
4. Run the setup script to create all 4 tables automatically:

```bash
python setup_airtable.py
```

This creates **Properties**, **Transactions**, **Charges**, and **Parties** tables with the correct fields. It skips any tables that already exist.

The sync uses upsert (insert or update) based on `record_id`, so it's safe to run repeatedly.

> **Note:** The `raw_text` field (full page text for future searching) is stored in SQLite only and not synced to Airtable.
