"""
Outreach Cadence Generator

For every charge entering the 9-month, 6-month, and 3-month touchpoint windows,
generate ready-to-send email drafts, phone scripts, and LinkedIn DMs populated
with the owner's details and deal specifics.

Each (property, chargee, window) combo is generated only once and tracked in
the outreach_log table so you don't duplicate outreach. Output goes to:
  - output/outreach_queue.csv      (draft content, ready to copy/paste)
  - Airtable "Outreach" table      (manage send status from CRM)

Usage:
    python outreach.py                         # Generate new outreach drafts
    python outreach.py --export-csv            # Also write outreach_queue.csv
    python outreach.py --sync-airtable         # Also push to Airtable
    python outreach.py --force-regenerate      # Re-generate even if already sent
    python outreach.py --sender "Your Name"    # Override sender name in templates
"""

import argparse
import csv
import os
import sqlite3
from datetime import datetime, date

from config import DB_FILE, OUTPUT_DIR, OUTREACH_CONFIG
from charge_maturity import (
    get_db_connection,
    query_charges_with_details,
    parse_due_date,
)
from type_converters import parse_currency, parse_percent

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# Touchpoint windows. Each entry: (window_key, lower_months, upper_months, label)
# When a charge's due_date falls in [today+lower, today+upper), we generate a
# draft for that window (once).
WINDOWS = [
    ("9_month", 6, 9, "9 months out"),
    ("6_month", 3, 6, "6 months out"),
    ("3_month", 0, 3, "3 months out"),
]


# ------------------------------ Tracking ------------------------------

def _ensure_outreach_log(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS outreach_log (
            property_record_id TEXT,
            chargee TEXT,
            window TEXT,
            generated_at TEXT,
            status TEXT DEFAULT 'queued',
            PRIMARY KEY (property_record_id, chargee, window)
        )
    """)
    conn.commit()


def _already_generated(conn):
    rows = conn.execute(
        "SELECT property_record_id, chargee, window FROM outreach_log"
    ).fetchall()
    return {(r[0], r[1] or "", r[2]) for r in rows}


def _mark_generated(conn, drafts):
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    for d in drafts:
        conn.execute(
            "INSERT OR IGNORE INTO outreach_log "
            "(property_record_id, chargee, window, generated_at, status) "
            "VALUES (?, ?, ?, ?, 'queued')",
            (d["property_record_id"], d["chargee"] or "", d["window"], now),
        )
    conn.commit()


# ------------------------------ Windowing ------------------------------

def _assign_window(due, today):
    from dateutil.relativedelta import relativedelta
    for key, lower, upper, label in WINDOWS:
        start = today + relativedelta(months=lower)
        end = today + relativedelta(months=upper)
        if start <= due < end:
            return key, label
    return None, None


# ------------------------------ Formatting ------------------------------

def _fmt_currency(v):
    if v is None:
        return "n/a"
    try:
        return "${:,.0f}".format(float(v))
    except (ValueError, TypeError):
        return "n/a"


def _fmt_percent(v):
    """parse_percent returns a decimal (0.0425 for 4.25%), so multiply by 100."""
    if v is None:
        return "n/a"
    try:
        return "{:.2f}%".format(float(v) * 100.0)
    except (ValueError, TypeError):
        return "n/a"


def _first_name(full_name):
    """Best-effort first name extraction from an owner's legal name."""
    if not full_name:
        return "there"
    name = full_name.strip()
    # Strip common corporate suffixes — if it's a numbered/corp, use generic
    upper = name.upper()
    corp_markers = (" INC", " LTD", " CORP", " LLC", " GP", " LP",
                    " HOLDINGS", " CAPITAL", " ONTARIO INC", " PROPERTIES")
    if any(m in upper for m in corp_markers) or name.split()[0].isdigit():
        return "there"
    # Take the first token with letters
    parts = name.split()
    for p in parts:
        if p.replace(".", "").isalpha() and len(p) > 1:
            return p.title()
    return "there"


def _short_address(address, city):
    parts = [p for p in [address, city] if p]
    return ", ".join(parts) if parts else "your property"


# ------------------------------ Templates ------------------------------

def _build_email(draft, sender):
    first = _first_name(draft["owner_name"])
    addr = _short_address(draft["address"], draft["city"])
    principal = _fmt_currency(draft["principal"])
    rate = _fmt_percent(draft["rate"])
    due = draft["due_date"] or "the upcoming maturity"
    chargee = draft["chargee"] or "the current lender"
    window = draft["window"]

    if window == "9_month":
        subject = "Quick note re: %s" % addr
        body = (
            "Hi %s,\n\n"
            "I track multi-residential mortgage maturities in Ontario and noticed "
            "the charge on %s with %s is coming up around %s "
            "(%s @ %s).\n\n"
            "You're about 9 months out, so no rush — but if you'd like to see what "
            "the refinancing landscape looks like now vs. locking in closer to maturity, "
            "happy to share some recent $/unit comps and lender indications for your area.\n\n"
            "Worth a 15-min call in the next few weeks?\n\n"
            "Best,\n%s"
        ) % (first, addr, chargee, due, principal, rate, sender)

    elif window == "6_month":
        subject = "%s — 6 months to maturity" % addr
        body = (
            "Hi %s,\n\n"
            "Following up — the %s charge with %s on %s matures around %s. "
            "At 6 months out, this is usually when owners start comparing refinancing "
            "options seriously.\n\n"
            "I can pull together:\n"
            "  - Current $/unit comps in your submarket\n"
            "  - 2-3 lender indications at today's rates\n"
            "  - A refinance-vs-sell quick-look\n\n"
            "Want me to put that together? No obligation, just useful data.\n\n"
            "Best,\n%s"
        ) % (first, principal, chargee, addr, due, sender)

    else:  # 3_month
        subject = "%s maturity — %s" % (addr, due)
        body = (
            "Hi %s,\n\n"
            "Your %s charge with %s on %s is due %s — ~90 days out.\n\n"
            "Two things I can help with right now:\n"
            "  1) Refinancing: I've got current term-sheet indications from 3-4 "
            "active multi-res lenders and can have quotes back within a week.\n"
            "  2) If you're considering a sale instead, I have active multi-res buyers "
            "looking in your area at today's $/unit comps.\n\n"
            "Happy to jump on a quick call this week — what works?\n\n"
            "Best,\n%s"
        ) % (first, principal, chargee, addr, due, sender)

    return subject, body


def _build_call_script(draft, sender):
    first = _first_name(draft["owner_name"])
    addr = _short_address(draft["address"], draft["city"])
    principal = _fmt_currency(draft["principal"])
    due = draft["due_date"] or "the upcoming maturity"
    chargee = draft["chargee"] or "your current lender"
    window = draft["window"]

    opener = (
        "Hi %s, this is %s. I track multi-res mortgage maturities in Ontario and "
        "I see the %s charge with %s on %s comes due around %s. Got a minute?"
        % (first, sender, principal, chargee, addr, due)
    )

    if window == "9_month":
        pitch = (
            "You're ~9 months out, so no pressure. I just wanted to introduce myself "
            "and offer to share comps and refi indications when you're ready to look. "
            "Would it be helpful if I sent a short market summary by email this week?"
        )
    elif window == "6_month":
        pitch = (
            "At 6 months out most owners start shopping financing. I can pull 2-3 "
            "lender indications and current $/unit comps for your submarket — want "
            "me to put that in front of you this week?"
        )
    else:
        pitch = (
            "You're ~90 days out, so timing matters. I've got active multi-res lender "
            "term sheets and also buyers at today's pricing. Want me to send both sides "
            "so you can decide refinance vs. sell?"
        )

    return opener + "\n\n" + pitch


def _build_linkedin(draft, sender):
    first = _first_name(draft["owner_name"])
    addr = _short_address(draft["address"], draft["city"])
    window_label = {"9_month": "~9 months out", "6_month": "~6 months out",
                    "3_month": "~90 days out"}[draft["window"]]
    return (
        "Hi %s — noticed the mortgage on %s comes up for renewal (%s). "
        "I track multi-res maturities and can share current comps and lender "
        "indications if useful. Open to connecting?"
    ) % (first, addr, window_label)


# ------------------------------ Core build ------------------------------

def build_drafts(force_regenerate=False, min_principal=None, sender=None):
    """Return a list of new outreach drafts (not yet generated)."""
    if min_principal is None:
        min_principal = OUTREACH_CONFIG.get("min_principal", 0)
    if sender is None:
        sender = OUTREACH_CONFIG.get("sender_name", "the team")

    today = date.today()
    conn = get_db_connection()
    rows = query_charges_with_details(conn)
    conn.close()

    conn_rw = sqlite3.connect(DB_FILE)
    _ensure_outreach_log(conn_rw)
    already = set() if force_regenerate else _already_generated(conn_rw)
    conn_rw.close()

    drafts = []
    for row in rows:
        due = parse_due_date(row["due_date"])
        if due is None:
            continue

        principal_val = parse_currency(row["principal"]) or 0.0
        if principal_val < min_principal:
            continue

        window_key, window_label = _assign_window(due, today)
        if window_key is None:
            continue

        key = (row["property_record_id"] or "", row["chargee"] or "", window_key)
        if key in already:
            continue

        draft = {
            "property_record_id": row["property_record_id"] or "",
            "window": window_key,
            "window_label": window_label,
            "address": row["address"] or "",
            "city": row["city"] or "",
            "region": row["region"] or "",
            "owner_name": row["owner_name"] or "",
            "owner_phone": row["owner_phone"] or "",
            "chargee": row["chargee"] or "",
            "principal": principal_val,
            "rate": parse_percent(row["rate"]),
            "due_date": due.strftime("%Y-%m-%d"),
            "generated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status": "queued",
        }
        subject, body = _build_email(draft, sender)
        draft["email_subject"] = subject
        draft["email_body"] = body
        draft["call_script"] = _build_call_script(draft, sender)
        draft["linkedin_message"] = _build_linkedin(draft, sender)
        drafts.append(draft)

    # Sort: most urgent window first, then largest principal
    window_order = {"3_month": 0, "6_month": 1, "9_month": 2}
    drafts.sort(key=lambda d: (window_order[d["window"]], -(d["principal"] or 0)))
    return drafts


def mark_generated(drafts):
    if not drafts:
        return
    conn = sqlite3.connect(DB_FILE)
    _ensure_outreach_log(conn)
    _mark_generated(conn, drafts)
    conn.close()


# ------------------------------ Exports ------------------------------

def export_csv(drafts, filepath=None):
    if filepath is None:
        filepath = os.path.join(OUTPUT_DIR, "outreach_queue.csv")
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    fieldnames = [
        "window_label", "due_date", "address", "city", "region",
        "owner_name", "owner_phone", "chargee", "principal", "rate",
        "email_subject", "email_body", "call_script", "linkedin_message",
        "status", "generated_at", "property_record_id", "window",
    ]
    with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(drafts)
    print("Exported %d outreach drafts to %s" % (len(drafts), filepath))
    return filepath


def print_summary(drafts):
    counts = {"9_month": 0, "6_month": 0, "3_month": 0}
    principal_by_window = {"9_month": 0.0, "6_month": 0.0, "3_month": 0.0}
    for d in drafts:
        counts[d["window"]] += 1
        principal_by_window[d["window"]] += d["principal"] or 0.0

    print("")
    print("=" * 58)
    print("  OUTREACH QUEUE  --  %s" % date.today().strftime("%Y-%m-%d"))
    print("=" * 58)
    print("  %-14s %8s %18s" % ("Window", "Drafts", "Total Principal"))
    print("  " + "-" * 44)
    for key, label in [("3_month", "3 months out"), ("6_month", "6 months out"),
                       ("9_month", "9 months out")]:
        print("  %-14s %8d %18s" % (
            label, counts[key], _fmt_currency(principal_by_window[key])))
    print("  " + "-" * 44)
    print("  %-14s %8d %18s" % (
        "TOTAL", len(drafts),
        _fmt_currency(sum(principal_by_window.values()))))
    print("")


def sync_to_airtable(drafts):
    """Sync outreach drafts to Airtable 'Outreach' table."""
    if not drafts:
        print("No drafts to sync.")
        return False
    try:
        from pyairtable import Api
    except ImportError:
        print("pyairtable not installed -- skipping Airtable sync")
        return False

    api_key = os.getenv("AIRTABLE_API_KEY")
    base_id = os.getenv("AIRTABLE_BASE_ID")
    if not api_key or not base_id:
        print("WARNING: AIRTABLE_API_KEY or AIRTABLE_BASE_ID not set -- skipping")
        return False

    api = Api(api_key)
    table = api.table(base_id, "Outreach")

    at_records = []
    for d in drafts:
        outreach_id = "%s|%s|%s" % (d["property_record_id"], d["chargee"], d["window"])
        fields = {
            "outreach_id": outreach_id,
            "window": d["window_label"],
            "due_date": d["due_date"],
            "address": d["address"],
            "city": d["city"],
            "region": d["region"],
            "owner_name": d["owner_name"],
            "owner_phone": d["owner_phone"],
            "chargee": d["chargee"],
            "email_subject": d["email_subject"],
            "email_body": d["email_body"],
            "call_script": d["call_script"],
            "linkedin_message": d["linkedin_message"],
            "status": "Queued",
            "property_record_id": d["property_record_id"],
        }
        if d["principal"]:
            fields["principal"] = d["principal"]
        if d["rate"]:
            fields["rate"] = d["rate"]
        at_records.append({"fields": fields})

    try:
        table.batch_upsert(at_records, key_fields=["outreach_id"], typecast=True)
        print("Synced %d outreach drafts to Airtable." % len(at_records))
        return True
    except Exception as e:
        print("Airtable sync failed: %s" % e)
        return False


# ------------------------------ CLI ------------------------------

def main():
    parser = argparse.ArgumentParser(description="Generate outreach drafts for upcoming maturities")
    parser.add_argument("--export-csv", action="store_true", help="Write outreach_queue.csv")
    parser.add_argument("--sync-airtable", action="store_true", help="Sync to Airtable Outreach table")
    parser.add_argument("--force-regenerate", action="store_true",
                        help="Regenerate drafts even if already in outreach_log")
    parser.add_argument("--min-principal", type=float, default=None,
                        help="Minimum charge principal to generate for")
    parser.add_argument("--sender", default=None, help="Your name (used in templates)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Build and preview without marking as generated")
    args = parser.parse_args()

    drafts = build_drafts(
        force_regenerate=args.force_regenerate,
        min_principal=args.min_principal,
        sender=args.sender,
    )
    print_summary(drafts)
    if not drafts:
        print("No new outreach drafts. (Use --force-regenerate to rebuild all.)")
        return

    if args.export_csv or not args.sync_airtable:
        export_csv(drafts)
    if args.sync_airtable:
        sync_to_airtable(drafts)
    if not args.dry_run:
        mark_generated(drafts)
        print("Logged %d drafts as generated." % len(drafts))
    else:
        print("[dry-run] Not marking as generated.")


if __name__ == "__main__":
    main()
