# Automations & Workflow Guide

This guide covers three layers of automation for the RealTrack prospecting pipeline:

1. **Pipeline Orchestration** — `run_pipeline.py` (end-to-end execution)
2. **Scheduled Runs** — Cron jobs for hands-off operation
3. **Airtable Automations** — In-app triggers for CRM workflow

---

## 1. Pipeline Orchestrator (`run_pipeline.py`)

The orchestrator runs the full pipeline in sequence: **scrape → export → unit lookup → CMHC estimates → sync → notify**.

### Quick Start

```bash
# Full pipeline (interactive browser)
python run_pipeline.py

# Full pipeline (headless, resume from last run)
python run_pipeline.py --headless --resume

# Sync-only (re-push existing CSVs to Airtable)
python run_pipeline.py --sync-only

# Scrape-only (no Airtable sync)
python run_pipeline.py --scrape-only

# See what would run without executing
python run_pipeline.py --dry-run
```

### Features

- **Lock file** prevents concurrent runs (safe for cron)
- **Structured logging** to `output/logs/pipeline_YYYYMMDD_HHMMSS.log`
- **Auto-cleanup** of logs older than 30 days
- **Exit codes**: 0 = success, 1 = failure (useful for cron/CI)
- **Graceful Ctrl+C handling**

### Notifications

Send a notification when the pipeline finishes (success or failure):

```bash
# Slack webhook
python run_pipeline.py --headless --notify slack

# Desktop notification (Linux notify-send / macOS)
python run_pipeline.py --headless --notify desktop

# Email (requires system `mail` command)
python run_pipeline.py --headless --notify email
```

**Setup for Slack notifications:**
1. Create a Slack Incoming Webhook at https://api.slack.com/messaging/webhooks
2. Add to `.env`: `SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...`

**Setup for email notifications:**
1. Ensure `mail` / `sendmail` is configured on your system
2. Add to `.env`: `NOTIFY_EMAIL=you@example.com`

---

## 2. Scheduled Runs (Cron)

### Auto-Install

```bash
# Install weekly cron job (Sunday 2 AM) with Slack notification
python run_pipeline.py --schedule --notify slack

# Remove the cron job
python run_pipeline.py --unschedule
```

### Manual Cron Setup

If you prefer to set up cron manually:

```bash
crontab -e
```

Add one of these lines:

```cron
# Weekly (Sunday 2 AM)
0 2 * * 0 cd /path/to/Prospecting && python3 run_pipeline.py --headless --resume --notify slack >> output/logs/cron.log 2>&1

# Daily (3 AM)
0 3 * * * cd /path/to/Prospecting && python3 run_pipeline.py --headless --resume >> output/logs/cron.log 2>&1

# Every 6 hours
0 */6 * * * cd /path/to/Prospecting && python3 run_pipeline.py --headless --resume >> output/logs/cron.log 2>&1
```

### Custom Schedule

Set the `PIPELINE_CRON_SCHEDULE` environment variable before running `--schedule`:

```bash
# Daily at 3 AM
export PIPELINE_CRON_SCHEDULE="0 3 * * *"
python run_pipeline.py --schedule
```

### Monitoring Scheduled Runs

```bash
# Check recent logs
ls -lt output/logs/pipeline_*.log | head -5

# View latest log
cat output/logs/$(ls -t output/logs/pipeline_*.log | head -1)

# Check cron log
tail -50 output/logs/cron.log

# Verify cron is installed
crontab -l | grep realtrack
```

---

## 3. Airtable Automations

Airtable has a built-in automation system (Automations tab in your base). These run server-side inside Airtable — no code or hosting needed.

### Recommended Automations

#### A. Auto-Tag New Leads

**Trigger:** When a record is created in **Properties**
**Condition:** `property_status` is empty
**Action:** Update record → set `property_status` to "New Lead"

This ensures every synced property starts in the pipeline.

#### B. Mortgage Maturity Alert

**Trigger:** At a scheduled time (daily, 9 AM)
**Find records:** In **Charges** where:
  - `due_date` is within the next 6 months
  - `maturity_status` is not "Maturing <6mo"
**Action 1:** Update matching records → set `maturity_status` to "Maturing <6mo"
**Action 2:** Send email/Slack notification with the list of maturing charges

Variation: Create a second automation for 12-month horizon with "Maturing <12mo".

#### C. Follow-Up Reminders

**Trigger:** At a scheduled time (daily, 8 AM)
**Find records:** In **Properties** where:
  - `next_step_date` is today or past
  - `property_status` is NOT "Won", "Lost", or "Archived"
**Action:** Send email notification listing properties that need follow-up

#### D. Activity Logging

**Trigger:** When `property_status` changes in **Properties**
**Action:** Run a script (Airtable Scripting action):

```javascript
// Append status change to contact_log with timestamp
let table = base.getTable('Properties');
let record = await input.recordAsync('Record', table);
let currentLog = record.getCellValueAsString('contact_log') || '';
let status = record.getCellValueAsString('property_status');
let timestamp = new Date().toLocaleDateString('en-CA'); // YYYY-MM-DD
let newEntry = `[${timestamp}] Status changed to: ${status}`;
let updatedLog = currentLog ? `${newEntry}\n${currentLog}` : newEntry;
await table.updateRecordAsync(record.id, { 'contact_log': updatedLog });
```

#### E. Deal Won Notification

**Trigger:** When `property_status` changes to "Won" in **Properties**
**Action:** Send email/Slack with property address, purchase price, and linked party details

#### F. Outreach Status Sync

**Trigger:** When `outreach_status` changes in **Parties** to "Responded" or "Meeting Scheduled"
**Find records:** Find the linked Property record
**Action:** Update the Property's `property_status` to "Meeting" (if currently "Contacted")

### Setting Up Automations

1. Open your Airtable base
2. Click **Automations** in the top toolbar
3. Click **+ Create automation**
4. Choose your trigger (e.g., "When record matches conditions")
5. Add conditions and actions as described above
6. Test with a sample record
7. Toggle the automation **ON**

### Airtable Automation Limits

| Plan       | Automation Runs/Month |
|------------|----------------------|
| Free       | 100                  |
| Team       | 25,000               |
| Business   | 100,000              |
| Enterprise | 500,000              |

The daily-scheduled automations (B, C) use ~30 runs/month each. Record-triggered automations (A, D, E, F) use 1 run per event. The free plan is sufficient for low-volume prospecting.

---

## 4. Recommended Workflow

### Daily Workflow

1. **Morning**: Check Airtable "Follow-Up Due" view for today's tasks
2. **Research**: Open properties in "New Lead" status, review charges/parties
3. **Outreach**: Contact owners, update `outreach_status` on Parties
4. **Log**: Add notes to `contact_log`, set `next_step` and `next_step_date`
5. **Progress**: Move properties through pipeline stages

### Weekly Workflow

1. **Pipeline runs automatically** (Sunday night via cron)
2. **Monday morning**: Check "New Leads" view for freshly scraped properties
3. **Review**: Prioritize new leads (`priority` field), add `tags`
4. **Check**: Review "Maturing Soon" charges view for opportunity signals

### Pipeline Stages

```
New Lead → Researching → Contacted → Meeting → Proposal → Won / Lost
                                                              ↓
                                                          Archived
```

| Stage       | Description                              | Typical Actions                        |
|-------------|------------------------------------------|----------------------------------------|
| New Lead    | Auto-set on sync. Fresh from scraper.    | Review property details, charges       |
| Researching | Doing due diligence                      | Check comps, site visit, title search  |
| Contacted   | Reached out to owner/agent               | Email/call, update contact_log         |
| Meeting     | Meeting scheduled or completed           | Prepare offer, discuss terms           |
| Proposal    | Offer submitted                          | Track response, negotiate              |
| Won         | Deal closed                              | Celebrate                              |
| Lost        | Deal fell through                        | Log reason in notes                    |
| Archived    | No longer pursuing                       | Can be revisited later                 |
