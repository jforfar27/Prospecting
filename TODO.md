# TODO

Parking lot for features not yet built.

## Corporate registry enrichment for corp-owned titles

**Problem.** ~70% of Ontario multi-res title shows up in a corp name
(Inc/Ltd/LP/numbered co). Right now `outreach.py` drops the greeting
name entirely for those (`"Hi,"`) because we have no individual
contact. Better would be to look up the registered director(s) and
use their name on the outreach.

**Proposed design**

1. New table `party_enrichment`:
   ```
   property_record_id TEXT
   corp_name          TEXT
   director_name      TEXT
   director_email     TEXT  (nullable)
   registered_address TEXT  (nullable)
   lookup_source      TEXT  (e.g. "opencorporates", "obr", "manual")
   looked_up_at       TEXT  (ISO timestamp)
   PRIMARY KEY (property_record_id, corp_name)
   ```

2. New script `enrich_parties.py` that runs after the scraper:
   - Select all Parties where `party_role = 'Transferee'` AND
     `legal_name` matches the corp markers in
     `outreach.py:_first_name`
   - Skip rows already present in `party_enrichment` younger than N
     days (default 90)
   - Query the registry API, store results
   - Graceful fallback when the API can't resolve a corp (store a
     row with null director_name so we don't re-query constantly)

3. Hook into `outreach.py`:
   - When `_first_name` returns None, check `party_enrichment` for a
     `director_name` before falling back to `"Hi,"`
   - If found, run the first-name extraction on the director name
     and use it in the greeting

4. CRM-only override field in Airtable Parties:
   - `contact_override_name`, `contact_override_email`
   - Airtable sync uses merge mode, so manual overrides won't be
     clobbered
   - `outreach.py` prefers override > enrichment > "Hi,"

**Data source options**

| Source | Cost | API | Coverage | Notes |
|--------|------|-----|----------|-------|
| OpenCorporates | Free tier 500/mo, then paid | Yes | Patchy on small Ontario numbered cos | Good starting point |
| Ontario Business Registry | Free | No | Full | No API, scraping fights CAPTCHA |
| OnCorp / Dye & Durham / ESC | ~$2-8/lookup | Yes | Full, authoritative | Cost scales with volume |

Start with OpenCorporates free tier. Measure hit rate on actual
scraped data. If coverage < 60%, layer in a paid fallback for the
misses only.

**Cost ceiling at current volumes**

Assuming ~500 corp lookups/quarter and $4/lookup on the paid fallback
for ~40% miss rate from OpenCorporates → ~$800/quarter. Budget check
before building.

**Out of scope (for now)**

- Director email scraping (LinkedIn, company websites) — manual-only
- Multi-director corps: just take the first director listed; user can
  override manually via the CRM field
- Historical officer changes — only pull current directors
