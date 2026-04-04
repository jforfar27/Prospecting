"""
Charge Maturity Pipeline

Queries the SQLite database for charges with upcoming due dates,
buckets them into maturity windows, joins with property/transaction/party
data, and exports a prioritized CSV report.

Usage:
    python charge_maturity.py [--export-csv] [--min-principal 500000]
"""

import argparse
import csv
import os
import sqlite3
import sys
from datetime import datetime, date

import json
import logging
import requests

from config import DB_FILE, OUTPUT_DIR, CHARGE_MATURITY_CONFIG, MATURITY_ALERT_CONFIG
from type_converters import parse_currency, parse_date, parse_percent

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logger = logging.getLogger(__name__)


def get_db_connection():
    """Open a read-only connection to the SQLite database."""
    if not os.path.exists(DB_FILE):
        print("ERROR: Database not found at %s" % DB_FILE)
        print("Run the scraper first: python run_pipeline.py --headless")
        sys.exit(1)
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def query_charges_with_details(conn):
    """Query charges joined with Property, Transaction, and Parties (Transferee)."""
    sql = """
        SELECT
            c.record_id       AS charge_record_id,
            c.chargee         AS chargee,
            c.principal        AS principal,
            c.rate             AS rate,
            c.due_date         AS due_date,
            c.property_record_id AS property_record_id,
            p.address          AS address,
            p.city             AS city,
            p.region           AS region,
            t.purchase_price   AS purchase_price,
            t.sale_date        AS sale_date,
            pa.legal_name      AS owner_name,
            pa.phone           AS owner_phone
        FROM Charges c
        LEFT JOIN Property p
            ON c.property_record_id = p.record_id
        LEFT JOIN "Transaction" t
            ON c.property_record_id = t.property_record_id
        LEFT JOIN Parties pa
            ON c.property_record_id = pa.property_record_id
            AND pa.party_role = 'Transferee'
        WHERE c.due_date IS NOT NULL
            AND c.due_date != ''
    """
    return conn.execute(sql).fetchall()


def parse_due_date(raw_date):
    """Parse a due_date string to a date object using type_converters.parse_date."""
    iso = parse_date(raw_date)
    if iso is None:
        return None
    try:
        return datetime.strptime(iso, "%Y-%m-%d").date()
    except ValueError:
        return None


def assign_bucket(due, today, bucket_boundaries):
    """Assign a due date to a maturity bucket. Returns bucket label or None if outside range."""
    from dateutil.relativedelta import relativedelta

    for i, months in enumerate(bucket_boundaries):
        lower = 0 if i == 0 else bucket_boundaries[i - 1]
        upper = months
        start = today + relativedelta(months=lower)
        end = today + relativedelta(months=upper)
        if start <= due < end:
            return "%d-%d months" % (lower, upper)
    return None


def build_maturity_report(min_principal=None):
    """Build the charge maturity report as a list of dicts, grouped by bucket."""
    if min_principal is None:
        min_principal = CHARGE_MATURITY_CONFIG["min_principal"]

    bucket_boundaries = CHARGE_MATURITY_CONFIG["buckets_months"]
    today = date.today()

    conn = get_db_connection()
    rows = query_charges_with_details(conn)
    conn.close()

    records = []
    for row in rows:
        due = parse_due_date(row["due_date"])
        if due is None:
            continue

        principal_val = parse_currency(row["principal"])
        if principal_val is None:
            principal_val = 0.0

        if principal_val < min_principal:
            continue

        bucket = assign_bucket(due, today, bucket_boundaries)
        if bucket is None:
            continue

        rate_val = parse_percent(row["rate"])
        purchase_val = parse_currency(row["purchase_price"])

        records.append({
            "bucket": bucket,
            "address": row["address"] or "",
            "city": row["city"] or "",
            "region": row["region"] or "",
            "purchase_price": purchase_val,
            "chargee": row["chargee"] or "",
            "principal": principal_val,
            "rate": rate_val,
            "due_date": due.strftime("%Y-%m-%d"),
            "owner_name": row["owner_name"] or "",
            "owner_phone": row["owner_phone"] or "",
            "property_record_id": row["property_record_id"] or "",
        })

    # Sort by bucket order, then principal descending (bigger deals first)
    bucket_order = {
        "%d-%d months" % (0 if i == 0 else bucket_boundaries[i - 1], m): i
        for i, m in enumerate(bucket_boundaries)
    }
    records.sort(key=lambda r: (bucket_order.get(r["bucket"], 99), -(r["principal"] or 0)))

    return records


def export_csv(records, filepath=None):
    """Export records to CSV."""
    if filepath is None:
        filepath = os.path.join(OUTPUT_DIR, "charge_maturity_report.csv")

    os.makedirs(os.path.dirname(filepath), exist_ok=True)

    fieldnames = [
        "bucket", "address", "city", "region", "purchase_price",
        "chargee", "principal", "rate", "due_date",
        "owner_name", "owner_phone", "property_record_id",
    ]

    with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(records)

    print("Exported %d records to %s" % (len(records), filepath))
    return filepath


def print_summary(records):
    """Print a summary table showing count and total principal per bucket."""
    bucket_boundaries = CHARGE_MATURITY_CONFIG["buckets_months"]
    bucket_labels = []
    for i, m in enumerate(bucket_boundaries):
        lower = 0 if i == 0 else bucket_boundaries[i - 1]
        bucket_labels.append("%d-%d months" % (lower, m))

    # Aggregate
    summary = {}
    for label in bucket_labels:
        summary[label] = {"count": 0, "total_principal": 0.0}

    for r in records:
        b = r["bucket"]
        if b in summary:
            summary[b]["count"] += 1
            summary[b]["total_principal"] += r["principal"] or 0.0

    total_count = sum(s["count"] for s in summary.values())
    total_principal = sum(s["total_principal"] for s in summary.values())

    # Print table
    print("")
    print("=" * 62)
    print("  CHARGE MATURITY REPORT  --  %s" % date.today().strftime("%Y-%m-%d"))
    print("=" * 62)
    print("")
    print("  %-18s %8s %18s" % ("Bucket", "Count", "Total Principal"))
    print("  " + "-" * 48)

    for label in bucket_labels:
        s = summary[label]
        principal_str = "${:,.0f}".format(s["total_principal"])
        print("  %-18s %8d %18s" % (label, s["count"], principal_str))

    print("  " + "-" * 48)
    total_str = "${:,.0f}".format(total_principal)
    print("  %-18s %8d %18s" % ("TOTAL", total_count, total_str))
    print("")

    if total_count == 0:
        print("  No charges maturing in the next %d months." % bucket_boundaries[-1])
        print("")


def sync_to_airtable(records):
    """Sync charge maturity records to Airtable Charge Maturities table."""
    from pyairtable import Api
    from dotenv import load_dotenv
    load_dotenv()

    api_key = os.getenv("AIRTABLE_API_KEY")
    base_id = os.getenv("AIRTABLE_BASE_ID")
    if not api_key or not base_id:
        print("WARNING: AIRTABLE_API_KEY or AIRTABLE_BASE_ID not set -- skipping")
        return False

    api = Api(api_key)
    table = api.table(base_id, "Charge Maturities")

    at_records = []
    for r in records:
        fields = {
            "charge_id": r["property_record_id"] + "_" + r["due_date"],
            "bucket": r["bucket"],
            "address": r["address"],
            "city": r["city"],
            "region": r["region"],
            "chargee": r["chargee"],
            "owner_name": r["owner_name"],
            "property_record_id": r["property_record_id"],
        }
        if r["principal"]:
            fields["principal"] = r["principal"]
        if r["rate"]:
            fields["rate"] = r["rate"]
        if r["due_date"]:
            fields["due_date"] = r["due_date"]
        if r["purchase_price"]:
            fields["purchase_price"] = r["purchase_price"]
        if r["owner_phone"]:
            fields["owner_phone"] = r["owner_phone"]
        at_records.append({"fields": fields})

    total = len(at_records)
    upserted = 0
    chunk_size = 10

    print("  Syncing %d maturities to Airtable..." % total)
    for i in range(0, total, chunk_size):
        chunk = at_records[i:i + chunk_size]
        try:
            table.batch_upsert(chunk, key_fields=["charge_id"], replace=False)
            upserted += len(chunk)
            print("  Maturities: %d/%d synced" % (upserted, total), end="\r", flush=True)
        except Exception as e:
            print("\n  Error (batch %d): %s" % (i // chunk_size + 1, e))

    print("  Maturities: %d/%d synced" % (upserted, total))
    return upserted == total


def _ensure_alert_table(conn):
    """Create the maturity_alerts tracking table if it does not exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS maturity_alerts (
            charge_record_id TEXT PRIMARY KEY,
            alerted_at TEXT,
            bucket TEXT,
            principal REAL
        )
    """)
    conn.commit()


def _get_already_alerted(conn):
    """Return a set of charge_record_ids that have already been alerted."""
    rows = conn.execute("SELECT charge_record_id FROM maturity_alerts").fetchall()
    return {row[0] for row in rows}


def _mark_alerted(conn, charges):
    """Insert alerted charges into the tracking table."""
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    for c in charges:
        key = c["property_record_id"] + "|" + (c["chargee"] or "")
        conn.execute(
            "INSERT OR IGNORE INTO maturity_alerts "
            "(charge_record_id, alerted_at, bucket, principal) "
            "VALUES (?, ?, ?, ?)",
            (key, now, c["bucket"], c["principal"]),
        )
    conn.commit()


def _format_currency(value):
    """Format a number as currency string."""
    if value is None:
        return "$0"
    return "${:,.0f}".format(value)


def _build_alert_message(new_charges, csv_path):
    """Build the alert summary as plain text and Slack blocks."""
    count = len(new_charges)
    total_principal = sum(c["principal"] or 0 for c in new_charges)
    top5 = sorted(new_charges, key=lambda c: -(c["principal"] or 0))[:5]

    # Plain text version (ASCII only for Windows cp1252 compatibility)
    lines = []
    lines.append("=== New Charge Maturities (0-3 months) ===")
    lines.append("")
    lines.append("  New maturities: %d" % count)
    lines.append("  Total principal: %s" % _format_currency(total_principal))
    lines.append("")
    lines.append("  Top deals by principal:")
    for i, c in enumerate(top5, 1):
        lines.append(
            "  %d. %s | %s | %s | %s | due %s"
            % (
                i,
                c["address"] or "N/A",
                c["owner_name"] or "N/A",
                c["chargee"] or "N/A",
                _format_currency(c["principal"]),
                c["due_date"] or "N/A",
            )
        )
    lines.append("")
    if csv_path:
        lines.append("  View full report: %s" % csv_path)
    plain_text = "\n".join(lines)

    # Slack blocks
    top5_bullets = "\n".join(
        "- *%s* | %s | %s | %s | due %s"
        % (
            c["address"] or "N/A",
            c["owner_name"] or "N/A",
            c["chargee"] or "N/A",
            _format_currency(c["principal"]),
            c["due_date"] or "N/A",
        )
        for c in top5
    )

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "New Charge Maturities (0-3 months)",
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "*%d new maturities* | Total principal: *%s*"
                % (count, _format_currency(total_principal)),
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "*Top deals by principal:*\n%s" % top5_bullets,
            },
        },
    ]
    if csv_path:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "View full report: `%s`" % csv_path,
                    }
                ],
            }
        )

    return plain_text, blocks


def _send_slack_alert(webhook_url, plain_text, blocks):
    """Post alert to Slack via incoming webhook. Returns True on success."""
    payload = {"text": plain_text, "blocks": blocks}
    try:
        resp = requests.post(
            webhook_url,
            data=json.dumps(payload),
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        if resp.status_code == 200:
            logger.info("Slack alert sent successfully")
            return True
        else:
            logger.warning(
                "Slack webhook returned status %d: %s", resp.status_code, resp.text
            )
            return False
    except requests.RequestException as exc:
        logger.warning("Failed to send Slack alert: %s", exc)
        return False


def alert_new_maturities():
    """Find charges in the 0-3 month bucket not yet alerted and send notification.

    Sends a Slack message if SLACK_WEBHOOK_URL is configured in .env,
    otherwise prints the alert to stdout. Marks alerted charges in the
    maturity_alerts tracking table to avoid duplicate alerts.

    Returns the count of newly alerted charges.
    """
    alert_bucket = MATURITY_ALERT_CONFIG["alert_bucket"]
    min_principal = MATURITY_ALERT_CONFIG["min_alert_principal"]

    # Build the full report (uses its own DB connection internally)
    records = build_maturity_report(min_principal=min_principal)

    # Filter to the alert bucket only
    bucket_records = [r for r in records if r["bucket"] == alert_bucket]

    if not bucket_records:
        print("No charges in the '%s' bucket above %s."
              % (alert_bucket, _format_currency(min_principal)))
        return 0

    # Open a read-write connection to check/update alert tracking
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    _ensure_alert_table(conn)
    already_alerted = _get_already_alerted(conn)

    # Build a composite key matching what we store
    new_charges = [
        r for r in bucket_records
        if (r["property_record_id"] + "|" + (r["chargee"] or "")) not in already_alerted
    ]

    if not new_charges:
        print("All %d charges in '%s' have already been alerted."
              % (len(bucket_records), alert_bucket))
        conn.close()
        return 0

    # Build the CSV path for the report reference
    csv_path = os.path.join(OUTPUT_DIR, "charge_maturity_report.csv")

    plain_text, blocks = _build_alert_message(new_charges, csv_path)

    # Try Slack first, fall back to stdout
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if webhook_url:
        success = _send_slack_alert(webhook_url, plain_text, blocks)
        if success:
            print("Slack alert sent for %d new maturities." % len(new_charges))
        else:
            print("Slack alert failed -- printing to stdout instead:")
            print("")
            print(plain_text)
    else:
        print(plain_text)

    # Mark these charges as alerted
    _mark_alerted(conn, new_charges)
    conn.close()

    print("Marked %d charges as alerted in tracking table." % len(new_charges))
    return len(new_charges)


def main():
    parser = argparse.ArgumentParser(
        description="Charge Maturity Pipeline - find upcoming mortgage maturities"
    )
    parser.add_argument(
        "--export-csv",
        action="store_true",
        help="Export results to output/charge_maturity_report.csv",
    )
    parser.add_argument(
        "--sync-airtable",
        action="store_true",
        help="Sync maturities to Airtable Charge Maturities table",
    )
    parser.add_argument(
        "--alert",
        action="store_true",
        help="Check for new 0-3 month maturities and send alert via Slack or stdout",
    )
    parser.add_argument(
        "--min-principal",
        type=float,
        default=None,
        help="Minimum charge principal to include (default: %s)"
        % CHARGE_MATURITY_CONFIG["min_principal"],
    )
    args = parser.parse_args()

    min_principal = args.min_principal
    if min_principal is None:
        min_principal = CHARGE_MATURITY_CONFIG["min_principal"]

    records = build_maturity_report(min_principal=min_principal)
    print_summary(records)

    if args.export_csv:
        export_csv(records)
    if args.sync_airtable:
        sync_to_airtable(records)
    if args.alert:
        alert_new_maturities()


if __name__ == "__main__":
    main()
