#!/usr/bin/env python3
"""
End-to-end pipeline orchestrator for the RealTrack prospecting system.

Runs the full pipeline: scrape → export → Airtable sync, with logging,
error handling, notifications, and scheduling support.

Usage:
    python run_pipeline.py                          # Full pipeline (scrape + sync)
    python run_pipeline.py --sync-only              # Skip scraping, just sync existing data
    python run_pipeline.py --scrape-only            # Scrape + export, no Airtable sync
    python run_pipeline.py --dry-run                # Show what would run without executing
    python run_pipeline.py --notify slack           # Send completion notification to Slack
    python run_pipeline.py --schedule               # Install as a cron job (interactive)

All scraper CLI args (--headless, --resume, --type, --min-amount, --start-year)
are passed through to the scraper.
"""

import os
import sys
import json
import time
import logging
import argparse
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

import config

# --- Logging Setup ---

LOG_DIR = os.path.join(config.OUTPUT_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

def setup_logging(log_level=logging.INFO):
    """Configure logging to both file and console."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = os.path.join(LOG_DIR, f"pipeline_{timestamp}.log")

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # File handler (detailed)
    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    # Console handler (summary)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(log_level)
    console_handler.setFormatter(formatter)

    logger = logging.getLogger("pipeline")
    logger.setLevel(logging.DEBUG)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger, log_file


# --- Pipeline Steps ---

class PipelineResult:
    """Tracks results across pipeline steps."""

    def __init__(self):
        self.started_at = datetime.now()
        self.steps = {}
        self.errors = []

    def record_step(self, name, success, details="", duration_s=0):
        self.steps[name] = {
            "success": success,
            "details": details,
            "duration_s": round(duration_s, 1),
        }
        if not success:
            self.errors.append(f"{name}: {details}")

    @property
    def success(self):
        return all(s["success"] for s in self.steps.values())

    @property
    def duration(self):
        return (datetime.now() - self.started_at).total_seconds()

    def summary(self):
        lines = [
            f"Pipeline {'SUCCEEDED' if self.success else 'FAILED'}",
            f"Duration: {self.duration:.0f}s ({self.duration / 60:.1f} min)",
            "",
        ]
        for name, info in self.steps.items():
            status = "OK" if info["success"] else "FAIL"
            lines.append(f"  [{status}] {name} ({info['duration_s']}s) — {info['details']}")
        if self.errors:
            lines.append("")
            lines.append("Errors:")
            for err in self.errors:
                lines.append(f"  - {err}")
        return "\n".join(lines)


def step_scrape(args, logger):
    """Run the scraper as a subprocess to isolate browser/Selenium dependencies."""
    logger.info("Starting scraper...")

    cmd = [sys.executable, "realtrack_scraper.py"]
    if args.headless:
        cmd.append("--headless")
    if args.resume:
        cmd.append("--resume")
    if args.property_type:
        cmd.extend(["--type", args.property_type])
    if args.min_amount:
        cmd.extend(["--min-amount", args.min_amount])
    if args.start_year:
        cmd.extend(["--start-year", args.start_year])

    # Never pass --sync; the orchestrator handles sync separately
    start = time.time()
    result = subprocess.run(
        cmd,
        capture_output=False,
        text=True,
        cwd=os.path.dirname(os.path.abspath(__file__)),
    )
    elapsed = time.time() - start

    if result.returncode != 0:
        return False, f"Scraper exited with code {result.returncode}", elapsed

    return True, "Scrape + export complete", elapsed


def step_unit_lookup(logger):
    """Look up unit counts for properties missing them."""
    logger.info("Starting unit count lookup...")
    start = time.time()

    try:
        from unit_lookup import run_lookup
        results = run_lookup(all_missing=True)
        elapsed = time.time() - start
        found = sum(1 for r in (results or []) if r.get("unit_count"))
        total = len(results or [])
        return True, f"{found}/{total} unit counts found", elapsed
    except Exception as e:
        elapsed = time.time() - start
        return False, str(e), elapsed


def step_sync(logger):
    """Sync CSVs to Airtable."""
    logger.info("Starting Airtable sync...")
    start = time.time()

    try:
        from airtable_sync import sync_all
        success = sync_all(config.OUTPUT_DIR)
        elapsed = time.time() - start

        if success:
            return True, "All tables synced", elapsed
        else:
            return False, "Sync completed with errors", elapsed
    except Exception as e:
        elapsed = time.time() - start
        return False, str(e), elapsed


def step_entity_resolution(logger):
    """Resolve SPE party names to parent companies and sync to Airtable."""
    logger.info("Starting entity resolution...")
    start = time.time()
    try:
        from entity_resolver import resolve_all, sync_contacts_to_airtable
        import sqlite3
        resolve_all()
        conn = sqlite3.connect(config.DB_FILE)
        sync_contacts_to_airtable(conn)
        conn.close()
        elapsed = time.time() - start
        return True, "Contacts resolved + synced", elapsed
    except Exception as e:
        elapsed = time.time() - start
        return False, str(e), elapsed


def step_charge_maturity(logger):
    """Run charge maturity report and sync to Airtable."""
    logger.info("Starting charge maturity pipeline...")
    start = time.time()
    try:
        from charge_maturity import build_maturity_report, print_summary, export_csv, sync_to_airtable
        records = build_maturity_report()
        print_summary(records)
        export_csv(records)
        sync_to_airtable(records)
        elapsed = time.time() - start
        return True, f"{len(records)} maturing charges synced", elapsed
    except Exception as e:
        elapsed = time.time() - start
        return False, str(e), elapsed


def step_outreach(logger):
    """Generate outreach drafts for upcoming maturities and sync to Airtable."""
    logger.info("Generating outreach drafts...")
    start = time.time()
    try:
        from outreach import build_drafts, export_csv, sync_to_airtable, mark_generated
        drafts = build_drafts()
        if not drafts:
            elapsed = time.time() - start
            return True, "No new outreach drafts needed", elapsed
        export_csv(drafts)
        sync_to_airtable(drafts)
        mark_generated(drafts)
        elapsed = time.time() - start
        return True, f"{len(drafts)} outreach drafts generated", elapsed
    except Exception as e:
        elapsed = time.time() - start
        return False, str(e), elapsed


def step_maturity_alerts(logger):
    """Check for new 0-3 month maturities and send alerts."""
    logger.info("Checking for new maturity alerts...")
    start = time.time()
    try:
        from charge_maturity import alert_new_maturities
        count = alert_new_maturities()
        elapsed = time.time() - start
        return True, f"{count} new alerts sent", elapsed
    except Exception as e:
        elapsed = time.time() - start
        return False, str(e), elapsed


def step_cleanup_logs(logger, keep_days=30):
    """Remove log files older than keep_days."""
    cutoff = datetime.now() - timedelta(days=keep_days)
    removed = 0
    for f in Path(LOG_DIR).glob("pipeline_*.log"):
        if datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
            f.unlink()
            removed += 1
    if removed:
        logger.info(f"Cleaned up {removed} old log files")
    return True, f"Removed {removed} old logs", 0


# --- Notifications ---

def send_notification(method, result, logger):
    """Send pipeline completion notification."""
    message = result.summary()

    if method == "slack":
        _notify_slack(message, result.success, logger)
    elif method == "email":
        _notify_email(message, result.success, logger)
    elif method == "desktop":
        _notify_desktop(message, result.success, logger)
    else:
        logger.warning(f"Unknown notification method: {method}")


def _notify_slack(message, success, logger):
    """Send notification to Slack via webhook."""
    webhook_url = os.getenv("SLACK_WEBHOOK_URL")
    if not webhook_url:
        logger.warning("SLACK_WEBHOOK_URL not set in .env — skipping Slack notification")
        return

    try:
        import urllib.request
        emoji = ":white_check_mark:" if success else ":x:"
        payload = {
            "text": f"{emoji} *RealTrack Pipeline*\n```{message}```",
        }
        req = urllib.request.Request(
            webhook_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10)
        logger.info("Slack notification sent")
    except Exception as e:
        logger.error(f"Slack notification failed: {e}")


def _notify_email(message, success, logger):
    """Send notification via email (uses system mail command)."""
    email_to = os.getenv("NOTIFY_EMAIL")
    if not email_to:
        logger.warning("NOTIFY_EMAIL not set in .env — skipping email notification")
        return

    try:
        status = "SUCCESS" if success else "FAILURE"
        subject = f"RealTrack Pipeline {status}"
        subprocess.run(
            ["mail", "-s", subject, email_to],
            input=message,
            text=True,
            timeout=10,
        )
        logger.info(f"Email notification sent to {email_to}")
    except Exception as e:
        logger.error(f"Email notification failed: {e}")


def _notify_desktop(message, success, logger):
    """Send desktop notification (Linux notify-send / macOS osascript)."""
    try:
        status = "Complete" if success else "Failed"
        title = f"RealTrack Pipeline {status}"
        if sys.platform == "linux":
            subprocess.run(["notify-send", title, message[:200]], timeout=5)
        elif sys.platform == "darwin":
            subprocess.run([
                "osascript", "-e",
                f'display notification "{message[:200]}" with title "{title}"',
            ], timeout=5)
        logger.info("Desktop notification sent")
    except Exception as e:
        logger.debug(f"Desktop notification failed: {e}")


# --- Scheduling ---

def install_cron(args):
    """Install or update the cron job for scheduled pipeline runs."""
    import shutil

    python_path = shutil.which("python3") or sys.executable
    script_path = os.path.abspath(__file__)
    working_dir = os.path.dirname(script_path)

    # Build the command
    cmd_parts = [python_path, script_path, "--headless", "--resume"]
    if args.notify:
        cmd_parts.extend(["--notify", args.notify])

    cron_cmd = f"cd {working_dir} && {' '.join(cmd_parts)}"

    # Default: run weekly on Sunday at 2 AM
    schedule = os.getenv("PIPELINE_CRON_SCHEDULE", "0 2 * * 0")

    cron_line = f"{schedule} {cron_cmd} >> {os.path.join(LOG_DIR, 'cron.log')} 2>&1"

    print("\n--- Cron Job Setup ---")
    print(f"Schedule:  {schedule}")
    print(f"Command:   {cron_cmd}")
    print(f"Cron line: {cron_line}")
    print()

    # Check existing crontab
    try:
        existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        current_cron = existing.stdout if existing.returncode == 0 else ""
    except FileNotFoundError:
        print("ERROR: crontab not found. Are you on a system that supports cron?")
        return

    # Remove any existing pipeline cron entry
    marker = "# realtrack-pipeline"
    filtered_lines = [
        line for line in current_cron.splitlines()
        if marker not in line and "run_pipeline.py" not in line
    ]

    new_cron = "\n".join(filtered_lines).strip()
    new_cron += f"\n{cron_line} {marker}\n"

    confirm = input("Install this cron job? [y/N] ").strip().lower()
    if confirm != "y":
        print("Cancelled.")
        return

    proc = subprocess.run(
        ["crontab", "-"],
        input=new_cron,
        text=True,
        capture_output=True,
    )
    if proc.returncode == 0:
        print("Cron job installed successfully!")
        print(f"View with: crontab -l")
        print(f"Remove with: crontab -e (delete the realtrack-pipeline line)")
    else:
        print(f"Failed to install cron job: {proc.stderr}")


def uninstall_cron():
    """Remove the pipeline cron job."""
    try:
        existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        if existing.returncode != 0:
            print("No crontab found.")
            return

        marker = "# realtrack-pipeline"
        filtered = [
            line for line in existing.stdout.splitlines()
            if marker not in line and "run_pipeline.py" not in line
        ]
        new_cron = "\n".join(filtered).strip() + "\n"

        subprocess.run(["crontab", "-"], input=new_cron, text=True)
        print("Cron job removed.")
    except FileNotFoundError:
        print("crontab not found.")


# --- Lock File (Prevent Concurrent Runs) ---

LOCK_FILE = os.path.join(config.OUTPUT_DIR, ".pipeline.lock")


def acquire_lock():
    """Prevent concurrent pipeline runs."""
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE) as f:
                lock_data = json.load(f)
            pid = lock_data.get("pid")
            started = lock_data.get("started", "unknown")

            # Check if the process is still running
            try:
                os.kill(pid, 0)
                print(f"ERROR: Pipeline already running (PID {pid}, started {started})")
                print(f"If this is stale, delete {LOCK_FILE}")
                return False
            except (OSError, TypeError):
                # Process is dead, stale lock
                pass
        except (json.JSONDecodeError, KeyError):
            pass

    with open(LOCK_FILE, "w") as f:
        json.dump({
            "pid": os.getpid(),
            "started": datetime.now().isoformat(),
        }, f)
    return True


def release_lock():
    """Remove the lock file."""
    try:
        os.remove(LOCK_FILE)
    except FileNotFoundError:
        pass


# --- Main ---

def parse_args():
    parser = argparse.ArgumentParser(
        description="RealTrack pipeline orchestrator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python run_pipeline.py --headless --resume          Full pipeline, headless + resume
  python run_pipeline.py --sync-only                  Re-sync existing data to Airtable
  python run_pipeline.py --headless --notify slack     Run + notify Slack on completion
  python run_pipeline.py --schedule                   Install as weekly cron job
  python run_pipeline.py --unschedule                 Remove cron job
        """,
    )

    # Pipeline mode
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--sync-only", action="store_true", help="Skip scraping, only sync to Airtable")
    mode.add_argument("--scrape-only", action="store_true", help="Scrape + export only, no Airtable sync")
    mode.add_argument("--dry-run", action="store_true", help="Show what would run without executing")
    mode.add_argument("--schedule", action="store_true", help="Install as a cron job")
    mode.add_argument("--unschedule", action="store_true", help="Remove the cron job")

    # Notifications
    parser.add_argument("--notify", choices=["slack", "email", "desktop"],
                        help="Send notification on completion")

    # Pass-through scraper args
    parser.add_argument("--headless", action="store_true", help="Run Chrome in headless mode")
    parser.add_argument("--resume", action="store_true", default=True, help="Resume from last scraped record (default: on)")
    parser.add_argument("--no-resume", dest="resume", action="store_false", help="Scrape all records from scratch")
    parser.add_argument("--type", dest="property_type", help="Override property type")
    parser.add_argument("--min-amount", help="Override minimum sale amount")
    parser.add_argument("--start-year", help="Override start year")

    return parser.parse_args()


def run_pipeline(args):
    """Execute the full pipeline with logging and error handling."""
    logger, log_file = setup_logging()
    result = PipelineResult()

    logger.info("=" * 50)
    logger.info("RealTrack Pipeline Started")
    logger.info(f"Mode: {'sync-only' if args.sync_only else 'scrape-only' if args.scrape_only else 'full'}")
    logger.info(f"Log file: {log_file}")
    logger.info("=" * 50)

    try:
        # Step 1: Scrape (unless sync-only)
        if not args.sync_only:
            ok, details, elapsed = step_scrape(args, logger)
            result.record_step("Scrape + Export", ok, details, elapsed)
            if not ok:
                logger.error(f"Scraper failed: {details}")
                if not args.scrape_only:
                    logger.info("Attempting Airtable sync with existing data...")

        # Step 2: Unit count lookup (after scrape, before sync)
        if not args.sync_only:
            ok, details, elapsed = step_unit_lookup(logger)
            result.record_step("Unit Lookup", ok, details, elapsed)
            if not ok:
                logger.warning(f"Unit lookup had issues: {details}")

        # Step 3: Airtable Sync (unless scrape-only)
        if not args.scrape_only:
            ok, details, elapsed = step_sync(logger)
            result.record_step("Airtable Sync", ok, details, elapsed)

        # Step 4: Entity Resolution + Contacts sync
        if not args.scrape_only:
            ok, details, elapsed = step_entity_resolution(logger)
            result.record_step("Entity Resolution", ok, details, elapsed)

        # Step 5: Charge Maturity report + Airtable sync
        if not args.scrape_only:
            ok, details, elapsed = step_charge_maturity(logger)
            result.record_step("Charge Maturity", ok, details, elapsed)

        # Step 6: Maturity alerts (0-3 month notifications)
        if not args.scrape_only:
            ok, details, elapsed = step_maturity_alerts(logger)
            result.record_step("Maturity Alerts", ok, details, elapsed)

        # Step 7: Outreach cadence generator (9/6/3-month drafts)
        if not args.scrape_only:
            ok, details, elapsed = step_outreach(logger)
            result.record_step("Outreach Drafts", ok, details, elapsed)

        # Step 8: Cleanup old logs
        step_cleanup_logs(logger)

    except KeyboardInterrupt:
        logger.warning("Pipeline interrupted by user")
        result.record_step("Interrupted", False, "Ctrl+C")
    except Exception as e:
        logger.exception(f"Unexpected pipeline error: {e}")
        result.record_step("Unexpected Error", False, str(e))

    # Summary
    logger.info("")
    logger.info(result.summary())

    # Notification
    if args.notify:
        send_notification(args.notify, result, logger)

    return result


def main():
    args = parse_args()

    # Handle scheduling commands
    if args.schedule:
        install_cron(args)
        return
    if args.unschedule:
        uninstall_cron()
        return

    # Dry run
    if args.dry_run:
        steps = []
        n = 1
        if not args.sync_only:
            steps.append(f"{n}. Scrape RealTrack + export CSVs")
            n += 1
            steps.append(f"{n}. Look up unit counts for new properties")
            n += 1
        if not args.scrape_only:
            steps.append(f"{n}. Sync to Airtable")
            n += 1
        steps.append(f"{n}. Cleanup old logs")
        n += 1
        if args.notify:
            steps.append(f"{n}. Notify via {args.notify}")

        print("Dry run — would execute:")
        for s in steps:
            print(f"  {s}")
        print(f"\nScraper flags: headless={args.headless}, resume={args.resume}")
        return

    # Acquire lock
    if not acquire_lock():
        sys.exit(1)

    try:
        result = run_pipeline(args)
        sys.exit(0 if result.success else 1)
    finally:
        release_lock()


if __name__ == "__main__":
    main()
