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


def _city_phrase(city):
    return city.strip() if city and city.strip() else "your area"


# ------------------------------ Comps ------------------------------

def _fetch_comps(conn, target_city, target_region, exclude_record_id, limit=3):
    """Return up to `limit` recent multi-res sale comps. Same-city matches
    rank first, then same-region fallback. Excludes the target property
    and portfolio flags. Requires unit_count and purchase_price populated.
    """
    target_city = (target_city or "").strip()
    target_region = (target_region or "").strip()
    if not target_city and not target_region:
        return []
    sql = """
        SELECT p.address, p.city, p.region, p.unit_count,
               t.sale_date, t.purchase_price, p.price_per_unit
        FROM Property p
        JOIN "Transaction" t ON p.record_id = t.property_record_id
        WHERE p.record_id != :exclude_id
          AND t.sale_date IS NOT NULL
          AND t.sale_date >= DATE('now', '-36 months')
          AND (t.portfolio_flag IS NULL
               OR LOWER(TRIM(t.portfolio_flag))
                  NOT IN ('portfolio','true','yes','y','1'))
          AND p.unit_count IS NOT NULL
          AND t.purchase_price IS NOT NULL
          AND (
            LOWER(TRIM(p.city)) = :target_city
            OR LOWER(TRIM(p.region)) = :target_region
          )
        ORDER BY
          CASE WHEN LOWER(TRIM(p.city)) = :target_city
               THEN 0 ELSE 1 END,
          t.sale_date DESC
        LIMIT :lim
    """
    rows = conn.execute(sql, {
        "exclude_id": exclude_record_id or "",
        "target_city": target_city.lower(),
        "target_region": target_region.lower(),
        "lim": limit,
    }).fetchall()
    comps = []
    for r in rows:
        try:
            units = int(r["unit_count"])
        except (ValueError, TypeError):
            continue
        if units <= 0:
            continue
        try:
            price = float(r["purchase_price"])
        except (ValueError, TypeError):
            continue
        if price <= 0:
            continue
        ppu_val = r["price_per_unit"]
        try:
            ppu = float(ppu_val) if ppu_val is not None else (price / units)
        except (ValueError, TypeError):
            ppu = price / units
        comps.append({
            "address": (r["address"] or "").strip(),
            "city": (r["city"] or "").strip(),
            "region": (r["region"] or "").strip(),
            "units": units,
            "sale_date": (r["sale_date"] or "").strip(),
            "price": price,
            "ppu": ppu,
        })
    return comps


def _fmt_sold(sale_date):
    if not sale_date:
        return ""
    return sale_date[:7]


def _fmt_price_short(v):
    try:
        f = float(v)
    except (ValueError, TypeError):
        return ""
    if f >= 1_000_000:
        return "$%.1fM" % (f / 1_000_000.0)
    if f >= 1_000:
        return "$%.0fk" % (f / 1_000.0)
    return "$%.0f" % f


def _truncate(s, width):
    s = s or ""
    if len(s) <= width:
        return s
    if width <= 1:
        return s[:width]
    return s[: width - 1] + "."


def _format_comps_table(comps, target_city):
    """Render comps as a fixed-width plain-text table. Returns '' if
    fewer than 2 comps are available.
    """
    if not comps or len(comps) < 2:
        return ""
    target_city_l = (target_city or "").strip().lower()
    headers = ["Address", "Sold", "Units", "Price", "$/Unit", "Area"]
    widths = [20, 7, 5, 8, 8, 14]
    rendered = []
    for c in comps:
        if c["city"] and c["city"].strip().lower() == target_city_l:
            area = c["city"]
        elif c["region"]:
            area = c["region"]
        else:
            area = c["city"] or ""
        rendered.append([
            _truncate(c["address"], widths[0]),
            _fmt_sold(c["sale_date"]),
            str(c["units"]),
            _fmt_price_short(c["price"]),
            _fmt_price_short(c["ppu"]),
            _truncate(area, widths[5]),
        ])

    def fmt_row(cells):
        out = []
        for i, cell in enumerate(cells):
            if i in (0, 5):
                out.append(cell.ljust(widths[i]))
            else:
                out.append(cell.rjust(widths[i]))
        return "  " + "  ".join(out)

    sep = ["-" * w for w in widths]
    lines = [
        "Recent comps in the area:",
        "",
        fmt_row(headers),
        fmt_row(sep),
    ]
    for r in rendered:
        lines.append(fmt_row(r))
    return "\n".join(lines)


# ------------------------------ Templates ------------------------------

def _street_only(draft):
    s = (draft["address"] or "").strip()
    return s or _short_address(draft["address"], draft["city"])


def _build_email(draft, sender, comps_table=""):
    first = _first_name(draft["owner_name"])
    addr = _street_only(draft)
    city = _city_phrase(draft["city"])
    due = draft["due_date"] or "the upcoming maturity"
    chargee_name = (draft["chargee"] or "").strip()
    charge_lower = ("your %s charge" % chargee_name) if chargee_name else "your mortgage"
    charge_upper = charge_lower[0].upper() + charge_lower[1:]
    window = draft["window"]

    if window == "9_month":
        subject = addr
        body = (
            "Hi %s,\n\n"
            "I was pulling comps in %s for a client's deal and came "
            "across your acquisition of %s. I'm a multi-res lender "
            "(CMHC and conventional). It looks like %s comes due "
            "around %s, roughly 9 months out. My timing could be off "
            "if you've already refinanced.\n\n"
            "If not, I wanted to introduce myself early. When you "
            "get closer to maturity I'd be glad to quote the "
            "renewal, and I'm also open to looking at anything else "
            "you have in flight.\n\n"
            "%s"
        ) % (first, city, addr, charge_lower, due, sender)

    elif window == "6_month":
        subject = addr
        body = (
            "Hi %s,\n\n"
            "I was pulling comps in %s for a client's file and your "
            "acquisition of %s came up. I do multi-res financing, "
            "CMHC and conventional. %s looks like it matures around "
            "%s, roughly 6 months out. Apologies in advance if "
            "you've already renewed.\n\n"
            "If you're starting to look at options, I'd like to "
            "quote the refi. I'm also open to looking at "
            "acquisitions or other assets you're financing right "
            "now. Would it make sense to connect this week?\n\n"
            "%s"
        ) % (first, city, addr, charge_upper, due, sender)

    else:  # 3_month
        subject = addr
        body = (
            "Hi %s,\n\n"
            "I was running comps in %s for a client and came across "
            "%s. I'm a multi-res lender. %s looks like it's due "
            "around %s, roughly 90 days out. If you're already set "
            "on the renewal, no need to respond.\n\n"
            "If you're still looking, I can turn around terms "
            "quickly (CMHC or conventional, depending on what fits "
            "the asset). I'd also be glad to look at anything else "
            "you have in flight.\n\n"
            "Do you have time for a call this week?\n\n"
            "%s"
        ) % (first, city, addr, charge_upper, due, sender)

    if comps_table:
        body = body + "\n\n" + comps_table

    return subject, body


def _build_call_script(draft, sender):
    first = _first_name(draft["owner_name"])
    addr = _street_only(draft)
    city = _city_phrase(draft["city"])
    due = draft["due_date"] or "the upcoming maturity"
    chargee_name = (draft["chargee"] or "").strip()
    charge_lower = ("your %s charge" % chargee_name) if chargee_name else "your mortgage"
    window = draft["window"]

    opener = (
        "Hi %s, this is %s. I do multi-res financing, CMHC and "
        "conventional. I was pulling comps in %s for a client's "
        "deal and your acquisition of %s came up. It looks like "
        "%s comes due around %s, though my timing could be off. "
        "Have you locked in the refi already, or are you still "
        "looking at options?"
        % (first, sender, city, addr, charge_lower, due)
    )

    if window == "9_month":
        pitch = (
            "You've got time. I just wanted to introduce myself. "
            "When you're closer I can put quotes together, CMHC or "
            "conventional. Is there anything else you're working "
            "on I should know about?"
        )
    elif window == "6_month":
        pitch = (
            "Good timing to start looking. I can pull term sheets "
            "at current rates, CMHC if it fits the asset, "
            "conventional if not. Would it be useful if I put "
            "something together this week?"
        )
    else:
        pitch = (
            "You're getting tight on time. I can turn around terms "
            "in a few days. Is CMHC on the table for this one, or "
            "are we looking conventional? And while we're at it, "
            "is there anything else in flight I could quote?"
        )

    return opener + "\n\n" + pitch


def _build_linkedin(draft, sender):
    first = _first_name(draft["owner_name"])
    addr = _street_only(draft)
    city = _city_phrase(draft["city"])
    window_phrase = {
        "9_month": "9 months",
        "6_month": "6 months",
        "3_month": "90 days",
    }[draft["window"]]
    return (
        "Hi %s, I was pulling comps in %s for a client and came "
        "across %s. I'm a multi-res lender (CMHC and conventional). "
        "The mortgage looks like it comes up in about %s, though my "
        "info could be stale. If you're still looking at renewal "
        "or refi options I'd like to quote it, and I'm open to "
        "looking at anything else you have in flight. Would it "
        "make sense to connect?"
    ) % (first, city, addr, window_phrase)


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
    _ensure_outreach_log(conn)
    already = set() if force_regenerate else _already_generated(conn)

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
        comps = _fetch_comps(
            conn,
            draft["city"],
            draft["region"],
            draft["property_record_id"],
            limit=3,
        )
        comps_table = _format_comps_table(comps, draft["city"])
        subject, body = _build_email(draft, sender, comps_table=comps_table)
        draft["email_subject"] = subject
        draft["email_body"] = body
        draft["call_script"] = _build_call_script(draft, sender)
        draft["linkedin_message"] = _build_linkedin(draft, sender)
        draft["comps_count"] = len(comps) if comps_table else 0
        drafts.append(draft)

    conn.close()

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
        "comps_count", "status", "generated_at", "property_record_id",
        "window",
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
