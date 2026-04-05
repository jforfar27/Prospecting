# Prospecting Pipeline — RealTrack Scraper + Airtable CRM

## What This Is

A weekly prospecting pipeline that scrapes multi-residential property acquisition data from RealTrack.com (Canadian real estate) and syncs it to an Airtable CRM for deal tracking. The pipeline runs on Windows 11.

## Architecture

```
realtrack_scraper.py   → Selenium/Chrome scrapes RealTrack page-by-page
    ↓
functions.py           → HTML parsing (BeautifulSoup) + SQLite upsert
    ↓
output/RealTrack.db    → SQLite (primary store) + CSV/Excel exports
    ↓
airtable_sync.py       → Batch upsert to Airtable via pyairtable
    ↓
run_pipeline.py        → Orchestrator: scrape → export → sync → notify
```

### Key Files

| File | Purpose |
|------|---------|
| `run_pipeline.py` | End-to-end orchestrator. Use this for weekly runs. |
| `realtrack_scraper.py` | Selenium scraper for RealTrack.com |
| `functions.py` | Chrome driver setup, page parsing, SQLite I/O |
| `airtable_sync.py` | CSV → Airtable sync with type conversion |
| `config.py` | Search params, table names, field converters, CRM field list |
| `type_converters.py` | String → currency/date/percent/number/checkbox converters |
| `setup_airtable.py` | One-time Airtable table creation script |
| `dashboard.sh` | Interactive CLI dashboard — view stats, tweak params, launch runs |

## Running the Weekly Pipeline

Resume mode is **on by default** — the pipeline always skips already-scraped records unless you pass `--no-resume`.

### Standard weekly run (headless, resume is automatic):
```bash
python run_pipeline.py --headless
```

### Interactive dashboard (view stats, pick params, launch runs):
```bash
bash dashboard.sh
```

### Sync-only (re-push existing CSVs without scraping):
```bash
python run_pipeline.py --sync-only
```

### Dry run (see what would execute):
```bash
python run_pipeline.py --dry-run
```

### Full fresh scrape (disable resume):
```bash
python run_pipeline.py --headless --no-resume
```

### With Slack notification:
```bash
python run_pipeline.py --headless --notify slack
```

## Prerequisites

- **Chrome browser** must be installed (undetected-chromedriver requires it)
- **Python 3.12+** requires `setuptools` installed in the venv (provides `distutils` which `undetected-chromedriver` needs): `pip install setuptools`
- **Python venv** with deps: `pip install -r requirements.txt`
- **Always use the venv Python** — either `source venv/Scripts/activate` first, or call `venv/Scripts/python.exe` directly
- **`.env` file** with credentials (see `.env.example`):
  - `REALTRACK_USERNAME` / `REALTRACK_PASSWORD` — RealTrack login
  - `AIRTABLE_API_KEY` — Personal Access Token with schema + data read/write scopes
  - `AIRTABLE_BASE_ID` — Target Airtable base
  - Optional: `SLACK_WEBHOOK_URL`, `NOTIFY_EMAIL`

## Important Behavior Notes

- **Lock file**: `output/.pipeline.lock` prevents concurrent runs. If a run crashes, delete it manually.
- **Resume mode** (on by default): Skips already-scraped `record_id`s found in SQLite. Pass `--no-resume` to scrape from scratch.
- **Airtable sync uses merge mode** (`replace=False`): CRM-only fields (status, priority, notes, tags, contact_log, etc.) are **never overwritten** by sync. Only scraped data fields are updated.
- **CRM-only fields** are defined in `config.py:CRM_ONLY_FIELDS` — these are managed manually in Airtable.
- **Scraper is slow**: It navigates one record at a time. Large result sets (1000+ records) take hours.
- **Logs**: Written to `output/logs/pipeline_YYYYMMDD_HHMMSS.log`. Auto-cleaned after 30 days.
- **Exit codes**: 0 = success, 1 = failure.

## Search Configuration

Default search params in `config.py`:
```python
SEARCH_CONFIG = {
    "property_type": "Multi Residential",
    "start_year": "96",
    "min_amount": "4000000",
    "records_per_page": "50 records",
}
```

Override via CLI: `--type "Commercial" --min-amount 2000000 --start-year 00`

## Airtable Schema

4 tables with linked records (Properties → Transactions, Charges, Parties):

- **Properties**: address, city, region, PIN, site_description, instrument_number, acreage, assessment_roll_number + CRM fields (status, priority, notes, next_step, next_step_date, contact_log, tags)
- **Transactions**: sale_date, purchase_price, cash, assumed_vbt_debt, portfolio_flag (linked to Property)
- **Charges**: chargee, principal, rate, registered_date, due_date, maturity_status (linked to Property)
- **Parties**: party_role, legal_name, phone, attention, care_of, address, city, province, postal_code + CRM fields (email, contact_notes, last_contacted, contact_status) (linked to Property)

All child tables link back to Properties via `property_record_id` / `Property` linked record field.

## Output Files

All in `output/` directory (gitignored):
- `RealTrack.db` — SQLite database
- `Property.csv`, `Transaction.csv`, `Chargees.csv`, `Parties.csv`
- `realtrack_export_YYYYMMDD_HHMMSS.xlsx` — consolidated Excel
- `logs/` — pipeline log files

## Common Issues

- **`distutils` missing (Python 3.12+)**: `undetected-chromedriver` depends on `distutils` which was removed in Python 3.12. Fix: `pip install setuptools` in the venv.
- **Chrome version mismatch**: `undetected-chromedriver` auto-detects Chrome version. If Chrome auto-updates and breaks, updating the `chrome-version` package may help.
- **RealTrack login failure**: Check credentials in `.env`. The site may have changed its login page structure — check `functions.py:signin_and_load_search_page`.
- **Airtable rate limits**: Sync uploads in batches of 10. Large syncs may hit rate limits — the error is logged but non-fatal.
- **Stale lock file**: If `output/.pipeline.lock` exists but no pipeline is running, delete it.
- **`Transaction` table quoting**: SQLite query uses `'"Transaction"'` because "Transaction" is a reserved word.

## Development Guidelines

- The scraper parsing logic in `functions.py` is tightly coupled to RealTrack's HTML structure. If the site changes layout, the `get_*_df_from_soup()` functions will need updating.
- Type converters in `type_converters.py` handle the string→typed conversion for Airtable. Add new converters there and register them in `config.py:FIELD_CONVERTERS`.
- Airtable table setup is in `setup_airtable.py` — only needed once per base. If schema changes, delete tables in Airtable UI and re-run.
