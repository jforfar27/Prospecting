# CLAUDE.md — Prospecting (RealTrack Scraper + Airtable CRM)

## Project Overview

A multi-residential property acquisition prospecting system that scrapes property data from RealTrack.com (Canadian property registry), stores it in SQLite, exports to CSV/Excel, and syncs to Airtable for CRM workflow. Built for real estate investors to identify deals, track mortgage maturities, and manage outreach.

## Architecture

```
realtrack_scraper.py  ──→  functions.py (browser automation + HTML parsing + SQLite)
        │                        ↓
        │                  output/RealTrack.db  →  output/*.csv + .xlsx
        ↓
run_pipeline.py (orchestrator: scrape → sync → cleanup → notify)
        ↓
airtable_sync.py  ──→  type_converters.py (string → typed values)
        ↓
Airtable (4 linked tables: Properties, Transactions, Charges, Parties)
```

**Data model:** Property is the parent entity. Transaction, Charges, and Parties are children linked via `property_record_id`. All columns are TEXT in SQLite; type conversion happens during Airtable sync.

## Key Files

| File | Purpose |
|---|---|
| `config.py` | Centralized config: search defaults, output paths, Airtable schema, field type converters, CRM-only fields |
| `functions.py` | Core library: Chrome driver setup (undetected-chromedriver), RealTrack login, search execution, BeautifulSoup HTML parsing (4 data types), SQLite upsert/read |
| `realtrack_scraper.py` | Main scraper entry point: CLI args, browser loop, parse + store each result, export CSVs/Excel. Supports `--resume` for incremental runs |
| `run_pipeline.py` | Orchestrator: subprocess scraper, Airtable sync, log cleanup, notifications (Slack/email/desktop), cron scheduling, lock file concurrency |
| `airtable_sync.py` | Uploads CSVs to Airtable with type conversion; uses batch_upsert with `replace=False` to preserve CRM fields |
| `setup_airtable.py` | One-time schema creation: 4 tables with proper field types, linked records, CRM pipeline fields |
| `type_converters.py` | Parsing functions: `parse_currency`, `parse_date`, `parse_percent`, `parse_number`, `parse_checkbox` — all handle NaN/None gracefully |

## Common Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Basic scrape (opens Chrome, logs in, scrapes, exports to output/)
python realtrack_scraper.py

# Headless scrape with resume + Airtable sync
python realtrack_scraper.py --headless --resume --sync

# Custom search filters
python realtrack_scraper.py --type "Commercial" --min-amount 2000000 --start-year 2020

# Full pipeline with notifications
python run_pipeline.py --headless --resume --notify slack

# Sync-only (CSVs must already exist in output/)
python run_pipeline.py --sync-only

# Create Airtable base schema (one-time)
python setup_airtable.py

# Install/uninstall cron schedule
python run_pipeline.py --schedule
python run_pipeline.py --unschedule

# Dry run (preview what would execute)
python run_pipeline.py --dry-run
```

## Environment Variables

Configured via `.env` file (copy from `.env.example`):

| Variable | Required | Purpose |
|---|---|---|
| `REALTRACK_USERNAME` | Yes | RealTrack.com login |
| `REALTRACK_PASSWORD` | Yes | RealTrack.com password |
| `AIRTABLE_API_KEY` | For sync | Airtable Personal Access Token |
| `AIRTABLE_BASE_ID` | For sync | Target Airtable base |
| `SLACK_WEBHOOK_URL` | No | Slack notification webhook |
| `NOTIFY_EMAIL` | No | Email notification recipient |
| `PIPELINE_CRON_SCHEDULE` | No | Override default cron (weekly Sun 2 AM) |

**Never commit `.env` — it is gitignored.**

## Output Directory

All generated files go to `output/` (gitignored except `.gitkeep`):

- `RealTrack.db` — SQLite database (primary data store, 4 tables)
- `Property.csv`, `Transaction.csv`, `Chargees.csv`, `Parties.csv` — per-table exports
- `realtrack_export_*.xlsx` — consolidated Excel (one row per property with all related data)
- `logs/` — timestamped pipeline logs (auto-cleaned after 30 days)

## Development Conventions

### Code Patterns
- **HTML parsing:** BeautifulSoup with try/except per field extraction; returns empty DataFrame on failure
- **Error handling:** Skip failed pages and log them; never crash the full scrape for one bad page
- **SQLite:** `INSERT OR REPLACE` on `record_id` (primary key) — idempotent upserts
- **Airtable sync:** `batch_upsert` with `replace=False` — preserves manually-entered CRM fields (status, priority, notes, contact_log, etc.)
- **Type conversion:** All converters in `type_converters.py` return `None` on unparseable input; registered in `config.FIELD_CONVERTERS`
- **Config-driven:** Search parameters, table names, excluded columns, CRM-only fields, and field converters all defined in `config.py`
- **Logging:** `run_pipeline.py` uses dual-stream (file + console); scraper uses print(); airtable_sync uses carriage-return progress

### CRM-Only Fields
Fields defined in `config.CRM_ONLY_FIELDS` are managed exclusively in Airtable and never overwritten by sync. These include pipeline status, priority, notes, contact tracking, and tags.

### Adding New Parsed Fields
1. Add extraction logic in the relevant `get_*_df_from_soup()` function in `functions.py`
2. If the field needs type conversion for Airtable, add a converter in `type_converters.py` and register it in `config.FIELD_CONVERTERS`
3. If the field should not sync to Airtable, add it to `config.AIRTABLE_EXCLUDE_COLUMNS`

### Dependencies
Defined in `requirements.txt`: pandas, beautifulsoup4, chrome-version, python-dotenv, undetected-chromedriver, openpyxl, pyairtable. No test framework or linter is configured.

### Git Conventions
- Commit messages: imperative mood, descriptive summary (e.g., "Add pipeline orchestrator, scheduling, and Airtable automation guide")
- Branch: `master` is the main branch
