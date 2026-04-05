"""
Entity Resolution Module — resolves SPE party names to parent companies
and builds BD outreach lists from RealTrack transaction data.

Usage:
    python entity_resolver.py                    # resolve + export outreach list
    python entity_resolver.py --export-csv       # also write CSV
    python entity_resolver.py --sync-airtable    # push to Airtable Contacts table

Reads from: output/RealTrack.db (Parties + Transaction tables)
Writes to:  output/RealTrack.db (contacts table) + optional CSV/Airtable
"""

import os
import re
import sqlite3
import argparse
import warnings
from datetime import datetime, timedelta
from collections import defaultdict
from difflib import SequenceMatcher

import pandas as pd
from dotenv import load_dotenv

warnings.simplefilter(action="ignore")
load_dotenv()

from config import DB_FILE, OUTPUT_DIR, ENTITY_RESOLUTION_CONFIG

# ---------------------------------------------------------------------------
# Database setup
# ---------------------------------------------------------------------------

def init_contacts_table(conn):
    """Create the contacts table if it doesn't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS contacts (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            parent_company    TEXT NOT NULL,
            spe_names         TEXT,           -- comma-separated SPE names
            party_role        TEXT,           -- Transferor, Transferee, or Both
            phone             TEXT,
            city              TEXT,
            province          TEXT,
            property_count    INTEGER DEFAULT 0,
            total_value       REAL DEFAULT 0,
            latest_txn_date   TEXT,
            earliest_txn_date TEXT,
            property_ids      TEXT,           -- comma-separated record_ids
            source            TEXT DEFAULT 'auto',  -- auto, manual, legal_name_2
            resolved_at       TEXT,
            -- BD outreach fields (manually enriched)
            contact_name      TEXT,
            contact_title     TEXT,
            contact_email     TEXT,
            contact_phone     TEXT,
            linkedin_url      TEXT,
            outreach_status   TEXT DEFAULT 'new',  -- new, contacted, responded, meeting, pass
            outreach_notes    TEXT,
            UNIQUE(parent_company)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_contacts_company
        ON contacts(parent_company)
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# Entity resolution logic
# ---------------------------------------------------------------------------

def get_recent_parties(conn, lookback_years=5):
    """Get all parties from transactions within the lookback window."""
    cutoff_year = datetime.now().year - lookback_years
    ignore_names = ENTITY_RESOLUTION_CONFIG["ignore_names"]
    placeholders = ",".join(["?"] * len(ignore_names))

    query = f"""
        SELECT
            p.legal_name,
            p.legal_name_2,
            p.party_role,
            p.phone,
            p.city,
            p.province,
            p.property_record_id,
            t.sale_date,
            t.purchase_price
        FROM Parties p
        JOIN "Transaction" t ON p.property_record_id = t.property_record_id
        WHERE p.legal_name != ''
        AND p.legal_name NOT IN ({placeholders})
    """
    rows = conn.execute(query, ignore_names).fetchall()

    # Filter by year from sale_date string
    filtered = []
    for r in rows:
        sale_date = r[7] or ""
        year = extract_year(sale_date)
        if year and year >= cutoff_year:
            filtered.append(r)

    return filtered


def extract_year(date_str):
    """Extract year from various date formats."""
    if not date_str:
        return None
    # Try "DD Mon YYYY" format
    match = re.search(r'(\d{4})', date_str)
    return int(match.group(1)) if match else None


def parse_purchase_price(val):
    """Parse purchase price string to float."""
    if not val:
        return 0
    cleaned = re.sub(r'[$,]', '', str(val).strip())
    try:
        return float(cleaned)
    except ValueError:
        return 0


def resolve_parent_company(legal_name, legal_name_2):
    """Determine the parent company for a party.

    Resolution order:
    1. Manual mappings from config (highest priority)
    2. legal_name_2 field (already scraped from RealTrack)
    3. Pattern-based cleanup of legal_name (strip SPE suffixes)
    """
    manual = ENTITY_RESOLUTION_CONFIG.get("manual_mappings", {})

    # 1. Check manual mappings
    if legal_name in manual:
        return manual[legal_name], "manual"

    # 2. Use legal_name_2 if available
    if legal_name_2 and str(legal_name_2).strip():
        return str(legal_name_2).strip(), "legal_name_2"

    # 3. Pattern-based resolution
    name = str(legal_name).strip()

    # Numbered companies: "1234567 Ontario Inc" → keep as-is (can't resolve without lookup)
    if re.match(r'^\d{5,}', name):
        return name, "numbered_co"

    # Strip common SPE suffixes to get cleaner company name
    # e.g. "Starlight (123 Main) GP Inc" → "Starlight"
    # But keep the full name if no clear parent pattern
    cleaned = name
    # Remove property-specific parenthetical: "(123 Main St)" or "(Hamilton)"
    cleaned = re.sub(r'\s*\([^)]*\)\s*', ' ', cleaned).strip()
    # Remove GP/LP/Propco/Nominee suffixes
    cleaned = re.sub(r'\s+(GP|LP|Propco|Nominee|Holdco|Property|Properties|Facility)\s+(Inc|Ltd|Corp|Corporation|Limited)?\s*$',
                     '', cleaned, flags=re.IGNORECASE).strip()
    # Remove trailing Inc/Ltd/Corp
    cleaned = re.sub(r'\s+(Inc|Ltd|Corp|Corporation|Limited|Co)\s*\.?\s*$', '', cleaned, flags=re.IGNORECASE).strip()

    if cleaned and cleaned != name:
        return cleaned, "pattern"

    return name, "as_is"


def resolve_numbered_companies(parties):
    """Pre-pass: resolve numbered companies by looking at co-parties on the same properties.

    For each numbered company (matching ^\\d{5,} or ^\\d{4}-\\d{4}), look at other party
    records on the same properties. If a co-party has a legal_name_2 (parent company),
    map the numbered company to that parent.

    Args:
        parties: list of tuples from get_recent_parties()
            (legal_name, legal_name_2, role, phone, city, province, prop_id, sale_date, price)

    Returns:
        dict of {numbered_company_name: resolved_parent_name}
    """
    numbered_co_re = re.compile(r'^(\d{5,}|\d{4}-\d{4})')

    # Build a map: property_record_id -> list of (legal_name, legal_name_2)
    property_parties = defaultdict(list)
    for row in parties:
        legal_name, legal_name_2, _, _, _, _, prop_id, _, _ = row
        property_parties[prop_id].append((legal_name, legal_name_2))

    # Find numbered companies and the properties they appear on
    numbered_cos = defaultdict(set)  # numbered_name -> set of property_ids
    for row in parties:
        legal_name = row[0]
        prop_id = row[6]
        if numbered_co_re.match(str(legal_name).strip()):
            numbered_cos[legal_name].add(prop_id)

    # For each numbered co, look at co-parties on the same properties
    resolved = {}
    for numbered_name, prop_ids in numbered_cos.items():
        # Collect all co-party parent names (from legal_name_2) across shared properties
        parent_candidates = defaultdict(int)  # parent_name -> count of properties
        for pid in prop_ids:
            for co_name, co_name_2 in property_parties[pid]:
                if co_name == numbered_name:
                    continue
                if co_name_2 and str(co_name_2).strip():
                    parent_candidates[str(co_name_2).strip()] += 1

        if parent_candidates:
            # Pick the most frequent co-party parent
            best_parent = max(parent_candidates, key=parent_candidates.get)
            resolved[numbered_name] = best_parent

    if resolved:
        print(f"  Numbered company pre-pass: resolved {len(resolved)} "
              f"of {len(numbered_cos)} numbered companies via co-party lookup")

    return resolved


def build_contact_records(parties):
    """Aggregate party records into contact records grouped by parent company."""
    company_data = defaultdict(lambda: {
        "spe_names": set(),
        "roles": set(),
        "phones": set(),
        "cities": set(),
        "provinces": set(),
        "property_ids": set(),
        "prices": [],
        "dates": [],
        "source": "auto",
    })

    for row in parties:
        legal_name, legal_name_2, role, phone, city, province, prop_id, sale_date, price = row
        parent, source = resolve_parent_company(legal_name, legal_name_2)

        d = company_data[parent]
        if legal_name != parent:
            d["spe_names"].add(legal_name)
        d["roles"].add(role)
        if phone:
            d["phones"].add(phone)
        if city:
            d["cities"].add(city)
        if province:
            d["provinces"].add(province)
        d["property_ids"].add(prop_id)
        d["prices"].append(parse_purchase_price(price))
        if sale_date:
            d["dates"].append(sale_date)
        if source in ("manual", "legal_name_2"):
            d["source"] = source

    # Build final records
    records = []
    for company, d in company_data.items():
        # Determine role
        roles = d["roles"]
        if "Transferor" in roles and "Transferee" in roles:
            role = "Both"
        elif "Transferee" in roles:
            role = "Transferee"
        else:
            role = "Transferor"

        # Best phone (most common)
        phone = max(d["phones"], key=lambda p: len(p)) if d["phones"] else ""

        # Sort dates to get earliest/latest
        sorted_dates = sorted(d["dates"])
        latest = sorted_dates[-1] if sorted_dates else ""
        earliest = sorted_dates[0] if sorted_dates else ""

        records.append({
            "parent_company": company,
            "spe_names": "; ".join(sorted(d["spe_names"])) if d["spe_names"] else "",
            "party_role": role,
            "phone": phone,
            "city": max(d["cities"], key=lambda c: len(c)) if d["cities"] else "",
            "province": next(iter(d["provinces"]), ""),
            "property_count": len(d["property_ids"]),
            "total_value": sum(d["prices"]),
            "latest_txn_date": latest,
            "earliest_txn_date": earliest,
            "property_ids": ", ".join(sorted(d["property_ids"])),
            "source": d["source"],
            "resolved_at": datetime.now().isoformat(),
        })

    # Sort by property count descending
    records.sort(key=lambda r: r["property_count"], reverse=True)
    return records


# ---------------------------------------------------------------------------
# Fuzzy dedup — merge near-duplicate contacts
# ---------------------------------------------------------------------------

def _normalize_name(name):
    """Normalize a company name for fuzzy comparison."""
    n = name.lower().strip()
    # Remove common suffixes
    for suffix in ["ltd", "inc", "corp", "corporation", "limited", "co", "company",
                   "apartments", "properties", "investments", "group", "reit"]:
        n = re.sub(rf'\b{suffix}\b\.?', '', n)
    # Remove extra whitespace
    n = re.sub(r'\s+', ' ', n).strip()
    return n


def _normalize_phone(phone):
    """Strip phone to digits only for comparison."""
    if not phone:
        return ""
    return re.sub(r'\D', '', phone)


def dedup_contacts(records):
    """Merge near-duplicate contact records using name similarity + phone/address matching.

    Two records are merged if:
    - Their normalized names match exactly (after stripping suffixes), OR
    - Their normalized names have a SequenceMatcher ratio >= fuzzy_threshold
      (only for names with 8+ chars, to avoid false positives on short names), OR
    - They share the same phone number AND city
    """
    # Build lookup by normalized name
    norm_groups = defaultdict(list)
    for i, rec in enumerate(records):
        norm = _normalize_name(rec["parent_company"])
        norm_groups[norm].append(i)

    # Also group by phone+city
    phone_groups = defaultdict(list)
    for i, rec in enumerate(records):
        phone = _normalize_phone(rec["phone"])
        city = (rec.get("city") or "").lower().strip()
        if phone and len(phone) >= 10 and city:
            phone_groups[(phone, city)].append(i)

    # Build union-find to track which records should merge
    parent_map = list(range(len(records)))

    def find(x):
        while parent_map[x] != x:
            parent_map[x] = parent_map[parent_map[x]]
            x = parent_map[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            # Keep the one with more properties as root
            if records[ra]["property_count"] >= records[rb]["property_count"]:
                parent_map[rb] = ra
            else:
                parent_map[ra] = rb

    # Union by normalized name (exact match)
    for indices in norm_groups.values():
        if len(indices) > 1:
            for idx in indices[1:]:
                union(indices[0], idx)

    # Union by fuzzy name similarity (Levenshtein via SequenceMatcher)
    fuzzy_threshold = ENTITY_RESOLUTION_CONFIG.get("fuzzy_threshold", 0.85)
    min_fuzzy_len = 8  # only fuzzy-match names at least this long
    fuzzy_merges = 0

    # Build list of (norm_name, first_index) for fuzzy comparison
    norm_representatives = []
    for norm, indices in norm_groups.items():
        if len(norm) >= min_fuzzy_len:
            norm_representatives.append((norm, indices[0]))

    # Compare each pair (O(n^2) but n is the number of unique normalized names)
    for i in range(len(norm_representatives)):
        norm_a, idx_a = norm_representatives[i]
        for j in range(i + 1, len(norm_representatives)):
            norm_b, idx_b = norm_representatives[j]
            # Skip if already in the same group
            if find(idx_a) == find(idx_b):
                continue
            ratio = SequenceMatcher(None, norm_a, norm_b).ratio()
            if ratio >= fuzzy_threshold:
                union(idx_a, idx_b)
                fuzzy_merges += 1

    if fuzzy_merges:
        print(f"  Fuzzy dedup: merged {fuzzy_merges} near-duplicate name pairs "
              f"(threshold={fuzzy_threshold})")

    # Union by phone+city
    for indices in phone_groups.values():
        if len(indices) > 1:
            for idx in indices[1:]:
                union(indices[0], idx)

    # Merge grouped records
    groups = defaultdict(list)
    for i in range(len(records)):
        groups[find(i)].append(i)

    merged = []
    merge_count = 0
    for root, indices in groups.items():
        if len(indices) == 1:
            merged.append(records[indices[0]])
            continue

        merge_count += len(indices) - 1
        # Merge: keep the record with the most properties as the base
        sorted_indices = sorted(indices, key=lambda i: records[i]["property_count"], reverse=True)
        base = dict(records[sorted_indices[0]])

        for idx in sorted_indices[1:]:
            other = records[idx]
            # Merge SPE names
            base_spes = set(base["spe_names"].split("; ")) if base["spe_names"] else set()
            other_spes = set(other["spe_names"].split("; ")) if other["spe_names"] else set()
            if other["parent_company"] != base["parent_company"]:
                other_spes.add(other["parent_company"])
            all_spes = base_spes | other_spes
            all_spes.discard("")
            base["spe_names"] = "; ".join(sorted(all_spes))

            # Merge property IDs
            base_props = set(base["property_ids"].split(", "))
            other_props = set(other["property_ids"].split(", "))
            all_props = base_props | other_props
            all_props.discard("")
            base["property_ids"] = ", ".join(sorted(all_props))
            base["property_count"] = len(all_props)

            # Sum values
            base["total_value"] = base["total_value"] + other["total_value"]

            # Merge roles
            roles = {base["party_role"], other["party_role"]}
            if "Both" in roles or ("Transferor" in roles and "Transferee" in roles):
                base["party_role"] = "Both"

            # Keep best phone (longest)
            if other["phone"] and len(other["phone"]) > len(base.get("phone") or ""):
                base["phone"] = other["phone"]

            # Prefer legal_name_2 source
            if other["source"] == "legal_name_2" and base["source"] != "manual":
                base["source"] = other["source"]

        merged.append(base)

    merged.sort(key=lambda r: r["property_count"], reverse=True)

    if merge_count:
        print(f"  Dedup: merged {merge_count} duplicate entries")

    return merged


# ---------------------------------------------------------------------------
# Upsert to SQLite
# ---------------------------------------------------------------------------

def upsert_contacts(conn, records):
    """Upsert contact records into SQLite, preserving manually-enriched fields."""
    init_contacts_table(conn)

    # Fields that auto-resolution updates (won't overwrite manual enrichments)
    auto_fields = [
        "spe_names", "party_role", "phone", "city", "province",
        "property_count", "total_value", "latest_txn_date",
        "earliest_txn_date", "property_ids", "source", "resolved_at",
    ]

    upserted = 0
    for rec in records:
        # Check if record exists
        existing = conn.execute(
            "SELECT id FROM contacts WHERE parent_company = ?",
            (rec["parent_company"],)
        ).fetchone()

        if existing:
            # Update only auto fields
            set_clause = ", ".join(f"{f} = ?" for f in auto_fields)
            values = [rec[f] for f in auto_fields] + [rec["parent_company"]]
            conn.execute(
                f"UPDATE contacts SET {set_clause} WHERE parent_company = ?",
                values
            )
        else:
            cols = ["parent_company"] + auto_fields
            placeholders = ", ".join(["?"] * len(cols))
            values = [rec[f] for f in cols]
            conn.execute(
                f"INSERT INTO contacts ({', '.join(cols)}) VALUES ({placeholders})",
                values
            )
        upserted += 1

    conn.commit()
    return upserted


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def export_csv(conn, output_path=None):
    """Export contacts to CSV for outreach."""
    output_path = output_path or os.path.join(OUTPUT_DIR, "outreach_contacts.csv")
    df = pd.read_sql_query("""
        SELECT
            parent_company, spe_names, party_role, phone, city, province,
            property_count, total_value, latest_txn_date, earliest_txn_date,
            contact_name, contact_title, contact_email, contact_phone,
            linkedin_url, outreach_status, outreach_notes, source
        FROM contacts
        ORDER BY property_count DESC
    """, conn)
    df.to_csv(output_path, index=False)
    print(f"  Exported {len(df)} contacts to {output_path}")
    return output_path


def print_summary(conn):
    """Print a summary of the contacts table."""
    stats = conn.execute("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN property_count >= 3 THEN 1 ELSE 0 END) as portfolio_players,
            SUM(CASE WHEN party_role = 'Transferee' THEN 1 ELSE 0 END) as buyers,
            SUM(CASE WHEN party_role = 'Transferor' THEN 1 ELSE 0 END) as sellers,
            SUM(CASE WHEN party_role = 'Both' THEN 1 ELSE 0 END) as both,
            SUM(CASE WHEN source = 'legal_name_2' THEN 1 ELSE 0 END) as resolved_l2,
            SUM(CASE WHEN source = 'manual' THEN 1 ELSE 0 END) as resolved_manual,
            SUM(CASE WHEN source = 'numbered_co' THEN 1 ELSE 0 END) as numbered,
            SUM(property_count) as total_props,
            ROUND(SUM(total_value) / 1e9, 2) as total_value_bn
        FROM contacts
    """).fetchone()

    print(f"""
  Entity Resolution Summary
  -------------------------------------
  Total contacts:      {stats[0]}
  Portfolio players:   {stats[1]} (3+ properties)
  Buyers:              {stats[2]}
  Sellers:             {stats[3]}
  Both:                {stats[4]}
  -------------------------------------
  Resolved via name_2: {stats[5]}
  Manual overrides:    {stats[6]}
  Numbered cos:        {stats[7]} (need manual review)
  -------------------------------------
  Total properties:    {stats[8]}
  Total deal value:    ${stats[9]}B
""")

    # Top 15 by property count
    print("  Top 15 Portfolio Players:")
    rows = conn.execute("""
        SELECT parent_company, property_count, party_role, phone, city,
               ROUND(total_value / 1e6, 1) as value_mm
        FROM contacts
        ORDER BY property_count DESC
        LIMIT 15
    """).fetchall()
    print(f"  {'Company':50s} {'Props':>5s} {'Role':>10s} {'Value($M)':>10s} {'City':>15s} {'Phone':>15s}")
    print(f"  {'-'*110}")
    for r in rows:
        print(f"  {r[0]:50s} {r[1]:>5d} {r[2]:>10s} {r[5]:>10.1f} {r[4] or '':>15s} {r[3] or '':>15s}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def resolve_all():
    """Run full entity resolution pipeline."""
    conn = sqlite3.connect(DB_FILE)

    print("  Fetching recent parties...")
    lookback = ENTITY_RESOLUTION_CONFIG["lookback_years"]
    parties = get_recent_parties(conn, lookback)
    print(f"  Found {len(parties)} party records (last {lookback} years)")

    # Pre-pass: resolve numbered companies via co-party lookup
    print("  Running numbered company pre-pass...")
    numbered_mappings = resolve_numbered_companies(parties)
    if numbered_mappings:
        # Inject into manual_mappings (don't overwrite existing manual entries)
        manual = ENTITY_RESOLUTION_CONFIG.get("manual_mappings", {})
        for num_name, parent in numbered_mappings.items():
            if num_name not in manual:
                manual[num_name] = parent
        ENTITY_RESOLUTION_CONFIG["manual_mappings"] = manual

    print("  Resolving parent companies...")
    records = build_contact_records(parties)
    print(f"  Resolved to {len(records)} unique contacts")

    print("  Running fuzzy dedup (name + phone/address matching)...")
    records = dedup_contacts(records)
    print(f"  {len(records)} contacts after dedup")

    print("  Upserting to contacts table...")
    upserted = upsert_contacts(conn, records)
    print(f"  {upserted} contacts upserted")

    print_summary(conn)
    conn.close()
    return records


def sync_contacts_to_airtable(conn):
    """Sync contacts table to Airtable Contacts table via batch upsert."""
    from pyairtable import Api

    AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")
    AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")

    if not AIRTABLE_API_KEY or not AIRTABLE_BASE_ID:
        print("  WARNING: AIRTABLE_API_KEY or AIRTABLE_BASE_ID not set -- skipping")
        return False

    api = Api(AIRTABLE_API_KEY)
    table = api.table(AIRTABLE_BASE_ID, "Contacts")

    df = pd.read_sql_query("SELECT * FROM contacts ORDER BY property_count DESC", conn)

    # Map SQLite columns to Airtable field names
    field_map = {
        "parent_company": "parent_company",
        "spe_names": "spe_names",
        "party_role": "party_role",
        "phone": "phone",
        "city": "city",
        "province": "province",
        "property_count": "property_count",
        "total_value": "total_value",
        "latest_txn_date": "latest_txn_date",
        "earliest_txn_date": "earliest_txn_date",
        "property_ids": "property_ids",
        "contact_name": "contact_name",
        "contact_title": "contact_title",
        "contact_email": "contact_email",
        "contact_phone": "contact_phone",
        "linkedin_url": "linkedin_url",
        "outreach_status": "outreach_status",
        "outreach_notes": "outreach_notes",
    }

    # CRM-only fields — skip if empty (don't overwrite manual Airtable entries)
    crm_fields = {"contact_name", "contact_title", "contact_email", "contact_phone",
                   "linkedin_url", "outreach_status", "outreach_notes"}

    records = []
    for _, row in df.iterrows():
        fields = {}
        for sqlite_col, at_field in field_map.items():
            val = row.get(sqlite_col)
            if pd.isna(val) or val is None or str(val).strip() == "":
                continue
            # Skip empty CRM fields (preserve Airtable manual entries)
            if sqlite_col in crm_fields and (not val or str(val).strip() in ("", "new")):
                continue
            # Convert types
            if sqlite_col == "property_count":
                fields[at_field] = int(val)
            elif sqlite_col == "total_value":
                fields[at_field] = float(val)
            else:
                fields[at_field] = str(val)
        records.append({"fields": fields})

    total = len(records)
    upserted = 0
    chunk_size = 10

    print(f"  Syncing {total} contacts to Airtable...")
    for i in range(0, total, chunk_size):
        chunk = records[i:i + chunk_size]
        try:
            table.batch_upsert(
                chunk,
                key_fields=["parent_company"],
                replace=False,
            )
            upserted += len(chunk)
            print(f"  Contacts: {upserted}/{total} synced", end="\r", flush=True)
        except Exception as e:
            print(f"\n  Error uploading contacts (batch {i // chunk_size + 1}): {e}")

    print(f"  Contacts: {upserted}/{total} synced")
    return upserted == total


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Resolve SPE entities and build outreach list")
    parser.add_argument("--export-csv", action="store_true", help="Export contacts to CSV")
    parser.add_argument("--sync-airtable", action="store_true", help="Sync contacts to Airtable")
    args = parser.parse_args()

    resolve_all()

    conn = sqlite3.connect(DB_FILE)
    if args.export_csv:
        export_csv(conn)
    if args.sync_airtable:
        sync_contacts_to_airtable(conn)
    conn.close()
