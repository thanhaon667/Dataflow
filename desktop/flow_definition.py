"""
The Data Flow map of this project - ONE place to edit.

This file only *describes* the system (what each stage is, which file implements
it, what goes in / out, how it is triggered, and how the pieces connect). The
live numbers, health states and "is it really scheduled?" checks are computed
from the real database / logs / Task Scheduler by desktop/flow_data.py and merged
onto this description; the browser (desktop/static/flow.js) only draws the result.

To extend the map later:
  * add a dict to NODES (copy a neighbour) and give it a lane/col/row;
  * add a dict to EDGES ("from" -> "to", the data label, and the trigger);
  * optionally add a "stats" list (numbers shown in the details drawer) and/or a
    health function for the node in flow_data.HEALTH (without one the node shows
    as a plain, static box);
  * restart ERP Desk (this file is read once, at start-up).
Nothing else needs to change.

The map cannot go stale silently: on every refresh flow_data.py scans the project's Python scripts
(see SCAN below), compares them with the source files the nodes declare and puts the result on the
Data Flow page ("The map is out of date" card, or "Map is in sync"):
  * a script that no node covers and that is not in IGNORE  -> "unmapped": add a node (+ edges), or
    add it to IGNORE below with a one-line reason (that is the deliberate way to dismiss it);
  * a node whose declared file no longer exists              -> "stale node": fix or remove the node;
  * an IGNORE entry whose file is gone (or that a node now covers) -> tidy the list;
  * edges pointing to unknown nodes, automated edges without armed_by, unknown armed_by keys ...
                                                             -> "inconsistent": fix the dict.
Which source files a node stands for: its optional "files" list of project-relative paths, otherwise
the paths in its "script" / "file" strings (comma separated). Runtime artifacts that may legitimately
not exist yet (daily_digest_latest.json, daily_digest_history.json ...) do NOT belong in "files".

--- Vocabulary -------------------------------------------------------------
Trigger kinds (how data starts moving):
  event       something happens and the code runs at once (a webhook call, an inline call)
  scheduled   Windows Task Scheduler (or another OS timer) starts it
  background  a loop inside ERP Desk itself (the live refresher, the once-a-day auto digest)
  manual      a person clicks a button / double-clicks a .bat / runs a command
  passive     no process at all: a database view or a file that is simply read when needed
                                                         (used on edges only)
  external    happens outside this machine (a visitor, your mail server); nodes only

A node's "triggers" list says what CAN start it; flow_data works out which of them
are really armed right now ("armed_by"):
  "external"                    happens outside this machine, cannot be verified
  "always"                      nothing to check
  "probe:webhook"               the FastAPI listener answers on 127.0.0.1:8000
  "flag:digest_auto"            ERP Desk's once-a-day auto digest is switched on
  "flag:report_refresher"       ERP Desk's live report refresher is running
  "schedule:<key>"              a real Windows scheduled task exists whose command contains
                                one of SCHEDULE_MARKERS[<key>]
A trigger flagged "recommended": True is what the README suggests; if it is not
armed it is shown honestly as "recommended, not set up".
A node's triggers are normally ALTERNATIVE ways to start the same job (any armed one makes it
automated). Set "roles": True on a node whose triggers are separate jobs (e.g. DeepSeek scores
each lead AND writes the digest): then it only counts as fully automated when all of them are
armed, and as "partial" when just some are. Every edge carries its own "armed_by" (see EDGES).

Layout: lanes group columns; a node sits on a (col, row) grid. `row` may be fractional.
An edge may add "via": [[x, row], ...] waypoints to route around nodes: x is in column units
(k = the middle of column k, k + 0.5 = the middle of the gap after column k) and row is the top
of a node row, so a waypoint between two nodes sits in the free band between them. Edges between
two nodes of the same column are drawn as a straight vertical link (or a side loop when another
node is in the way). Edge "counter" names a live counter from flow_data (throughput).
"""
from __future__ import annotations

# --------------------------------------------------------------------------- layout
LAYOUT = {
    "width": 1240,      # design-space width in px (the page scales it to fit the window)
    "node_w": 172,
    "node_h": 66,       # a node may override with its own "h" (px), e.g. the tall PostgreSQL hub
    "row_pitch": 88,
    "top": 96,          # room for the lane headers
    "pad_x": 30,        # left margin
    "pad_r": 64,        # right margin (room for loop-back edges)
    "cols": 5,
}

LANES = [
    {"id": "intake", "n": "01", "title": "Intake", "tag": "where data enters", "cols": [0], "color": "#ff5a36"},
    {"id": "storage", "n": "02", "title": "Storage & Logic", "tag": "dedup, assign, persist", "cols": [1], "color": "#3452eb"},
    {"id": "integrations", "n": "03", "title": "AI & Integrations", "tag": "enrich, hand off, pull back", "cols": [2], "color": "#7c5cff"},
    {"id": "reporting", "n": "04", "title": "Monitoring & Reporting", "tag": "digest, dashboards, outputs", "cols": [3, 4], "color": "#12b886"},
]

# Ports/URLs probed on localhost (fixed here; the browser can never choose them).
PROBES = {
    "webhook": "http://127.0.0.1:8000/health",                # run_webhook.bat
}

# A scheduled task counts as "this job" when its command line contains one of these.
SCHEDULE_MARKERS = {
    "clickup_pull": ["erp.clickup_pull", "clickup_pull.py"],
    "digest_daily": ["run_daily_digest.bat", "erp.daily_digest"],
    "weekly_report": ["erp.weekly_report", "weekly_report.py"],
    "daily_check": ["run_daily_check.bat", "erp.daily_check"],
    "sync_employees": ["erp.sync_employees", "sync_employees.py"],
    "webhook": ["run_webhook.bat", "erp.webhook_app"],
    "marketing_rollup": ["run_marketing_rollup.bat", "erp.marketing.rollup"],
    "marketing_autorun": ["run_marketing_autorun.bat", "erp.marketing.autorun"],
}

# --------------------------------------------------------------------------- map sync check
# Which Python scripts are compared with the map. Every top-level folder that contains .py files
# plus the .py files in the project root are scanned (recursively); hidden folders and virtualenvs
# (any folder holding a pyvenv.cfg) are always skipped.
SCAN = {
    "skip_dirs": ["venv", ".venv", "env", "node_modules", "__pycache__", "site-packages", "build", "dist", "tests", "test"],
    "skip_files": ["__init__.py", "__main__.py", "conftest.py", "setup.py"],
    "skip_prefixes": ["test_"],
    "skip_suffixes": ["_test.py"],
}

# Scripts that deliberately have no node: "relative/path.py": "why it is not part of the flow".
# This is the way to dismiss a script from the "The map is out of date" card. Keep the reason honest
# and short; an entry whose file disappears (or that a node now covers) is reported so the list stays tidy.
IGNORE = {
    # helpers that a node already implies (they move no data on their own)
    "erp/config.py": "Settings loader for .env: every node that calls the DB or an API implies it.",
    "erp/db.py": "SQLAlchemy engine helper: the PostgreSQL node already stands for it.",
    "erp/business_hours.py": "SLA business-hours arithmetic used inside erp/leads.py (the Dedup + assign node).",
    "erp/typography.py": "Font tokens (Montserrat) shared by Script Center, the e-mail HTML and ERP Desk: presentation only, no data moves.",
    # ERP Desk / Data Flow plumbing (the app that draws this page, not a stage of the pipeline)
    "desktop/launcher.py": "ERP Desk supervisor: starts the servers, the window and the child processes.",
    "desktop/server.py": "ERP Desk web server: serves the pages and the JSON feeds, moves no business data.",
    "desktop/winutil.py": "Win32 helpers (job objects, single instance) for the ERP Desk launcher.",
    "desktop/make_icon.py": "Generates the ERP Desk app icon. Cosmetic, one-off.",
    "desktop/flow_definition.py": "This map itself.",
    "desktop/flow_data.py": "Live numbers + the sync check of this map.",
    "desktop/sla_words.py": "The shared English for the lead SLA outcomes, imported by the Today and Leads feeds: wording only, no data moves.",
    # desktop/channels_data.py split into cohesive modules (clean-code pass 3); the Channels feed node names channels_data.py, which re-exports them
    "desktop/channels_request.py": "Channels feed, request layer (whitelisted params, Request / View, parsing): part of desktop/channels_data.py, no data moves.",
    "desktop/channels_money.py": "Channels feed, measures and currency-aware money cells: part of desktop/channels_data.py, pure functions.",
    "desktop/channels_text.py": "Channels feed, wording (date ranges, page states, install / empty help): part of desktop/channels_data.py, pure functions.",
    "desktop/channels_series.py": "Channels feed, daily / weekly series and filter options: part of desktop/channels_data.py, pure functions.",
    "desktop/channels_queries.py": "Channels feed, SQL builders and read-only state readers: part of desktop/channels_data.py (same two rollup tables, same node).",
    "desktop/channels_csv.py": "Channels feed, CSV export rows: part of desktop/channels_data.py, pure functions.",
    # the Channels page's Insights panel and Excel report (GET /api/channels/insights, GET /api/channels/report.xlsx): same rollup tables, same Live Reporting node
    "desktop/insights_rules.py": "Channels Insights engine: six fixed, explainable rules (pure functions, thresholds as named constants): part of the Live Reporting node, no data moves.",
    "desktop/insights_queries.py": "Channels Insights SQL builders (read-only, bound parameters, the two rollup tables): part of the Live Reporting node, same tables as desktop/channels_data.py.",
    "desktop/insights_data.py": "Channels Insights feed and Excel report store (GET /api/channels/insights and /api/channels/report.xlsx): part of the Live Reporting node, reads the rollup only.",
    # the Channels page's Placements panel (GET /api/channels/placements[.csv]) and the placement rules of Insights: same Live Reporting node
    "desktop/placements_queries.py": "Channels Placements SQL builders (read-only, bound parameters, the ONE placement rollup table + two name lookups): part of the Live Reporting node, never interaction_fact.",
    "erp/marketing/sample_placements.py": "Writes a SYNTHETIC placement CSV (invented sites, apps, numbers) to data_inbox/ so the Placements feature can be tried; loads nothing, moves no data.",
    "desktop/insights_xlsx.py": "Channels Excel writer (openpyxl, numeric cells, formula-safe text): part of the Live Reporting node, pure.",
    # desktop/flow_data.py split into cohesive modules (clean-code pass 3); flow_data.py re-exports them
    "desktop/flow_util.py": "Data Flow feed helpers (time formatting, redaction, log tailing): part of desktop/flow_data.py, pure functions.",
    "desktop/flow_metrics.py": "Data Flow feed live readings (read-only DB counts, log / JSON files, webhook probe, schtasks scan): part of desktop/flow_data.py.",
    "desktop/flow_triggers.py": "Data Flow feed trigger state (is each trigger armed): part of desktop/flow_data.py, pure functions.",
    "desktop/flow_health.py": "Data Flow feed node health functions: part of desktop/flow_data.py, pure functions over the live readings.",
    "desktop/flow_mapcheck.py": "Data Flow feed map sync check (unmapped scripts, stale nodes, inconsistent edges): part of desktop/flow_data.py.",
    # desktop/today_data.py split into cohesive modules (clean-code pass 3); today_data.py re-exports them
    "desktop/today_util.py": "Today feed helpers (timeouts, number / span formatting, log-once guard): part of desktop/today_data.py.",
    "desktop/today_sql.py": "Today feed query texts (read-only SELECTs, incl. the shared lead past-SLA fragments): part of desktop/today_data.py, same tables and node.",
    "desktop/today_build.py": "Today feed panel builders (tiles, attention list, since-yesterday line, detail drawer): part of desktop/today_data.py, pure functions.",
    "desktop/today_health.py": "Today feed health cells and blind-input list, read from the Data Flow snapshot: part of desktop/today_data.py, pure functions.",
    "desktop/today_assemble.py": "Today feed status sentence, rules text and payload assembly: part of desktop/today_data.py, pure functions.",
    # marketing ingestion pipeline (the marketing_ingest node covers the pipeline itself)
    "erp/marketing/connectors/base.py": "Abstract connector contract (records()/map_record()) and ConnectorError; moves no data on its own - the flat-file, ad_performance, email_campaign and placement_performance connectors implement it.",
    "erp/marketing/perf_check.py": "One-off SCALE VERIFICATION script (throwaway schema, millions of synthetic rows). Not part of the live pipeline - see docs/marketing-data-architecture.md.",
    "erp/marketing/rollup_check.py": "One-off SCALE VERIFICATION of the rollup layer (throwaway schema, millions of synthetic rows). Not part of the live pipeline - see docs/marketing-data-architecture.md s.8.",
    "erp/marketing/sample_check.py": "One-off VERIFICATION of the ad_performance / email_campaign connectors on the owner's two real sample files, in a throwaway schema that is dropped afterwards. Not part of the live pipeline - see docs/marketing-data-architecture.md s.9.",
}


# --------------------------------------------------------------------------- nodes
NODES = [
    # ============================ 01 INTAKE ====================================
    {
        "id": "lead_form", "label": "Lead form", "kind": "source", "icon": "form",
        "lane": "intake", "col": 0, "row": 0.9, "stage": False,
        "file": None,
        "summary": "Any web form, landing page, ad or chat channel that captures a prospect. It lives outside this repo and just POSTs JSON to the webhook.",
        "inputs": ["A visitor fills in a form (landing page, Google/Facebook Ads, LinkedIn, Zalo OA, referral, event...)"],
        "outputs": ["JSON: full_name, email and/or phone, company, source"],
        "triggers": [{"kind": "event", "label": "A visitor submits the form", "armed_by": "external"}],
        "stats": [
            {"label": "Leads received", "path": "leads.total", "fmt": "int"},
            {"label": "Distinct sources", "path": "leads.sources", "fmt": "int"},
            {"label": "Last lead", "path": "leads.last_at", "fmt": "time"},
        ],
        "headline": {"path": "leads.total", "fmt": "int", "short": "leads in"},
        "live": "lead_form",
    },
    {
        "id": "webhook", "label": "Lead webhook", "kind": "service", "icon": "webhook",
        "lane": "intake", "col": 0, "row": 2.0, "stage": True,
        "file": "erp/webhook_app.py", "script": "erp/webhook_app.py",
        "summary": "FastAPI listener, POST /webhooks/leads. Validates the payload (needs an email or a phone) and hands it to the pipeline. It is a long-running server, so it only works while it is started.",
        "inputs": ["HTTP POST with the lead JSON"],
        "outputs": ["Validated lead -> erp/leads.py process_new_lead()", "HTTP 200 with the result, or 500 on failure"],
        "triggers": [{"kind": "event", "label": "Every POST /webhooks/leads", "armed_by": "probe:webhook",
                      "detail": "Event-driven, but only while the listener is running (run_webhook.bat keeps it open; there is no service or scheduled task that starts it)."}],
        "stats": [
            {"label": "Listener :8000", "path": "probe.webhook", "fmt": "bool_up"},
            {"label": "Leads received (DB)", "path": "leads.total", "fmt": "int"},
            {"label": "Last lead", "path": "leads.last_at", "fmt": "time"},
        ],
        "headline": {"path": "probe.webhook", "fmt": "bool_up", "short": "listener"},
        "live": "webhook",
    },
    {
        "id": "ticket_entry", "label": "Support tickets", "kind": "source", "icon": "ticket",
        "lane": "intake", "col": 0, "row": 4.0, "stage": False,
        "file": "db/sql/02_seed_cskh.sql",
        "summary": "Customer Support / IT Support tickets share one `tickets` table. There is no ingestion script yet: rows come from the seed SQL or are typed into the database directly.",
        "inputs": ["Seed SQL / manual SQL entry"],
        "outputs": ["Rows in tickets, ticket_comments, ticket_status_history"],
        "triggers": [{"kind": "manual", "label": "Rows added by hand or by seed SQL", "armed_by": "always"}],
        "stats": [
            {"label": "Tickets", "path": "tickets.total", "fmt": "int"},
            {"label": "Open", "path": "tickets.open", "fmt": "int"},
            {"label": "Last ticket", "path": "tickets.last_at", "fmt": "time"},
        ],
        "headline": {"path": "tickets.total", "fmt": "int", "short": "tickets"},
        "live": "ticket_entry",
    },
    {
        "id": "marketing_ingest", "label": "Marketing ingest", "kind": "job", "icon": "pull",
        "lane": "intake", "col": 0, "row": 5.5, "stage": True,
        "file": "erp/marketing/ingest.py", "script": "erp/marketing/ingest.py",
        "files": ["erp/marketing/model.py", "erp/marketing/schema.py", "erp/marketing/pipeline.py",
                  "erp/marketing/ingest.py", "erp/marketing/parse.py", "erp/marketing/connectors/flat_file.py",
                  "erp/marketing/connectors/strict_csv.py", "erp/marketing/connectors/ad_performance.py",
                  "erp/marketing/connectors/email_campaign.py", "erp/marketing/connectors/placement_performance.py",
                  "db/sql/10_marketing_currency.sql"],
        "summary": "Land -> validate/type -> dedupe -> load a marketing/channel export (clicks, reactions, sessions, conversions, "
                    "spend, revenue) into the star schema (interaction_fact + dimensions). Four connectors: the generic flat-file/CSV "
                    "reader, and two STRICT source-specific ones for the owner's real exports - ad_performance (paid social, weekly rows, "
                    "money like '475,401 dong') and email_campaign (daily rows, EUR): exact headers, no guessed columns, no guessed "
                    "M/D vs D/M dates, rows that break their sanity rules rejected and counted; the fourth, placement_performance, reads a display-network "
                    "PLACEMENT report (site / app / banner slot, size, position, viewability; 14-column header, currency on the cell or --currency, "
                    "placement text sanitised). Money keeps the currency the source "
                    "reported (interaction_fact.currency, added by db/sql/10 - the owner runs it) and is never converted; two rows that "
                    "share a natural key but differ are both KEPT and reported, never silently overwritten. A real API connector "
                    "(Facebook Ads etc.) is Phase 2 and needs the owner's credentials, so nothing calls this automatically - it only "
                    "runs when a person runs it.",
        "inputs": ["A CSV / export file handed to it by a person (--connector flat_file | ad_performance | email_campaign | placement_performance)"],
        "outputs": ["marketing_landing (raw)", "marketing_source/channel/campaign/creative/identity (dimensions)",
                     "interaction_fact (monthly-partitioned fact, money + currency)", "marketing_ingest_run (one row per batch)",
                     "a printed run summary: rejected rows with reasons, natural-key collisions kept, spend and revenue per currency"],
        "triggers": [{"kind": "manual", "label": "python -m erp.marketing.ingest --connector <name> --csv <file> [--dry-run]", "armed_by": "always"}],
        "stats": [
            {"label": "Tables installed", "path": "marketing.installed", "fmt": "bool_yes"},
            {"label": "Batches run", "path": "marketing.runs", "fmt": "int"},
            {"label": "Fact rows loaded", "path": "marketing.fact_rows", "fmt": "int"},
            {"label": "Rejected (malformed)", "path": "marketing.rows_invalid", "fmt": "int"},
            {"label": "Last run", "path": "marketing.last_run_at", "fmt": "time"},
        ],
        "headline": {"path": "marketing.fact_rows", "fmt": "int", "short": "interactions"},
        "live": "marketing_ingest",
    },
    {
        "id": "marketing_autorun", "label": "Marketing autorun", "kind": "job", "icon": "pull",
        "lane": "intake", "col": 0, "row": 6.9, "stage": True,
        "file": "erp/marketing/autorun.py", "script": "erp/marketing/autorun.py",
        "files": ["erp/marketing/autorun.py", "run_marketing_autorun.bat"],
        "summary": "The INBOX PROCESSOR: an analyst drops marketing exports (CSV files) in data_inbox/incoming and one command turns them "
                    "into loaded data and a fresh rollup. For each CSV it detects the connector from the header with the existing strict "
                    "checks (ad_performance, email_campaign) and falls back to the generic flat_file ONLY when the file names its channel "
                    "(filename <channel>__<currency>__x.csv, or a channel column) - otherwise it guesses nothing and moves the file to failed/ "
                    "with a .reason.txt. It loads through the existing ingest pipeline (no parsing repeated here), moves the file to processed/ "
                    "or failed/ (never deleting or rewriting the original), skips an identical file it already loaded (SHA-256 kept in "
                    ".autorun_index.json inside the inbox), then runs the incremental rollup refresh ONCE. --dry-run reads and reports "
                    "only. UNSCHEDULED: nothing runs it except a person - registering it in Task Scheduler is the owner's decision.",
        "inputs": ["*.csv files dropped in data_inbox/incoming (or MARKETING_INBOX / --inbox) by a person"],
        "outputs": ["rows in interaction_fact via the marketing ingest pipeline", "a refreshed rollup (marketing_rollup, once per run)",
                     "files moved to processed/ or failed/ (+ <name>.reason.txt) next to the inbox",
                     "a run summary table + data_inbox/marketing_autorun.log",
                     ".autorun_index.json (content hashes + the last run) inside the inbox"],
        "triggers": [
            {"kind": "scheduled", "label": "Task Scheduler: run_marketing_autorun.bat, e.g. every hour", "armed_by": "schedule:marketing_autorun",
             "recommended": True,
             "detail": "Not set up until the owner creates the task. The job is safe to repeat: an identical file is never loaded twice."},
            {"kind": "manual", "label": "python -m erp.marketing.autorun [--dry-run]  (or run_marketing_autorun.bat)", "armed_by": "always"},
        ],
        "stats": [
            {"label": "Last result", "path": "autorun.result", "fmt": "text"},
            {"label": "Last run", "path": "autorun.at", "fmt": "time"},
            {"label": "Files handled by the last run", "path": "autorun.files", "fmt": "int"},
            {"label": "Rows loaded by the last run", "path": "autorun.rows_loaded", "fmt": "int"},
            {"label": "Files failed in the last run", "path": "autorun.failed", "fmt": "int"},
            {"label": "Duplicates skipped in the last run", "path": "autorun.duplicate", "fmt": "int"},
        ],
        "headline": {"path": "autorun.rows_loaded", "fmt": "int", "short": "rows last run"},
        "live": "marketing_autorun",
    },

    # ======================== 02 STORAGE & LOGIC ===============================
    {
        "id": "leads_logic", "label": "Dedup + assign", "kind": "service", "icon": "logic",
        "lane": "storage", "col": 1, "row": 1.9, "stage": True,
        "file": "erp/leads.py", "script": "erp/leads.py",
        "summary": "The orchestrator. Inserts the lead, looks for an existing lead with the same email/phone (then keeps the same rep), otherwise picks the active Sales rep with the fewest current leads, then calls the AI and ClickUp and commits everything in one transaction.",
        "inputs": ["Validated lead payload from the webhook"],
        "outputs": ["INSERT leads + lead_assignments", "Lead facts -> AI", "Task payload -> ClickUp", "One commit at the end"],
        "triggers": [{"kind": "event", "label": "Called by the webhook for each lead", "armed_by": "probe:webhook"}],
        "stats": [
            {"label": "Assignments", "path": "assign.total", "fmt": "int"},
            {"label": "Round-robin", "path": "assign.rr", "fmt": "int"},
            {"label": "Duplicates re-routed", "path": "assign.dup", "fmt": "int"},
            {"label": "Active Sales reps", "path": "assign.reps_active", "fmt": "int"},
            {"label": "Last assignment", "path": "assign.last_at", "fmt": "time"},
        ],
        "headline": {"path": "assign.total", "fmt": "int", "short": "assigned"},
        "live": "leads_logic",
    },
    {
        "id": "postgres", "label": "PostgreSQL", "kind": "store", "icon": "db",
        "lane": "storage", "col": 1, "row": 3.0, "h": 224, "stage": False,
        "file": "db/sql/01_schema.sql, 03_leads_schema.sql", "script": None,
        "files": ["db/sql/01_schema.sql", "db/sql/03_leads_schema.sql"],   # what the sync check verifies (the display string above is loose)
        "summary": "The single source of truth (database erp_support): leads, lead_assignments, lead_ai_analysis, lead_clickup_sync, lead_updates, users, departments, tickets and friends. Everything else reads from here or writes to here.",
        "inputs": ["Lead + assignment + AI result + ClickUp link (webhook pipeline)", "Pulled ClickUp comments", "Staff from ClickUp", "Tickets (manual)"],
        "outputs": ["Rows for the daily jobs, the live report, the dashboards", "Read-only views for Power BI"],
        "triggers": [{"kind": "event", "label": "Any INSERT / UPDATE / SELECT", "armed_by": "always"}],
        "stats": [
            {"label": "leads", "path": "leads.total", "fmt": "int"},
            {"label": "lead_assignments", "path": "assign.total", "fmt": "int"},
            {"label": "lead_ai_analysis", "path": "ai.total", "fmt": "int"},
            {"label": "lead_clickup_sync", "path": "clickup.total", "fmt": "int"},
            {"label": "lead_updates", "path": "updates.total", "fmt": "int"},
            {"label": "users", "path": "users.total", "fmt": "int"},
            {"label": "tickets", "path": "tickets.total", "fmt": "int"},
        ],
        "headline": {"text": "erp_support"},
        "live": "postgres",
    },
    {
        "id": "views", "label": "Report views", "kind": "store", "icon": "views",
        "lane": "storage", "col": 1, "row": 5.9, "stage": False,
        "file": "db/sql/05_leads_sla.sql, 06_powerbi_readonly.sql",
        "files": ["db/sql/05_leads_sla.sql", "db/sql/06_powerbi_readonly.sql"],
        "summary": "v_leads_summary and v_tickets_summary flatten the joins (rep, AI score, ClickUp status, SLA breach). The read-only role powerbi_reader can SELECT these two views and nothing else.",
        "inputs": ["Tables in erp_support (computed at query time)"],
        "outputs": ["Flat, SLA-aware rows for Power BI, the digest, the daily check and the dashboards"],
        "triggers": [{"kind": "passive", "label": "Evaluated whenever something SELECTs from them", "armed_by": "always"}],
        "stats": [
            {"label": "v_leads_summary rows", "path": "views.leads_rows", "fmt": "int"},
            {"label": "v_tickets_summary rows", "path": "views.tickets_rows", "fmt": "int"},
            {"label": "Role powerbi_reader", "path": "views.role_ok", "fmt": "bool_yes"},
        ],
        "headline": {"path": "views.leads_rows", "fmt": "int", "short": "rows"},
        "live": "views",
    },
    {
        "id": "marketing_rollup", "label": "Marketing rollup", "kind": "job", "icon": "pull",
        "lane": "storage", "col": 1, "row": 7.0, "stage": True,
        "file": "erp/marketing/rollup.py", "script": "erp/marketing/rollup.py",
        "files": ["erp/marketing/rollup.py", "erp/marketing/rollup_placements.py", "erp/marketing/partitions.py", "db/sql/09_marketing_rollup.sql",
                  "db/sql/11_marketing_placements.sql",
                  "run_marketing_rollup.bat"],
        "summary": "Phase 4: keeps the daily channel/campaign ROLLUP of interaction_fact current so a report never has to "
                    "aggregate millions of raw rows. The rollup keeps the currency and the grain (day or week) in its key, so money is never "
                    "added across currencies and a weekly export is never read as one day. Recomputes only the last few days plus any older day whose facts changed "
                    "(DELETE range + INSERT ... GROUP BY, idempotent), pre-creates the next monthly fact partitions, and "
                    "REPORTS - never drops - partitions older than the retention threshold. Nothing schedules it today: "
                    "registering it in Task Scheduler is a system setting only the owner changes. Its two rollup tables are what ERP Desk's Channels "
                    "page (Ctrl+7) reads; that page never triggers a refresh, it shows whatever the last run left. The same run also keeps the OPTIONAL "
                    "placement rollup (interaction_placement_rollup, db/sql/11: day x campaign x placement x size x position x currency, incremental, with a "
                    "per-campaign cap that folds the long tail into one bucket) that the Placements panel reads; without db/sql/11 that step logs one line and is skipped.",
        "inputs": ["interaction_fact (read, one date range at a time)"],
        "outputs": ["interaction_daily_rollup (per day/channel/campaign/currency/grain)", "interaction_daily_channel_rollup (per day/channel/currency/grain)",
                     "interaction_placement_rollup (optional, db/sql/11: per day/campaign/placement/size/position/currency)",
                     "marketing_rollup_run (one row per run)", "new empty interaction_fact partitions (look-ahead)",
                     "read by the Channels page of ERP Desk (GET /api/channels, and GET /api/channels/placements for the placement rollup) - the only reports that read these tables"],
        "triggers": [
            {"kind": "scheduled", "label": "Task Scheduler: run_marketing_rollup.bat, e.g. every night", "armed_by": "schedule:marketing_rollup",
             "recommended": True,
             "detail": "run_marketing_rollup.bat after the night's loads (docs/marketing-data-architecture.md s.8). Not set up until the owner creates the task."},
            {"kind": "manual", "label": "python -m erp.marketing.rollup  (or run_marketing_rollup.bat)", "armed_by": "always"},
        ],
        "stats": [
            {"label": "Tables installed", "path": "rollup.installed", "fmt": "bool_yes"},
            {"label": "Refresh runs", "path": "rollup.runs", "fmt": "int"},
            {"label": "Rows written by the last run", "path": "rollup.last_rows", "fmt": "int"},
            {"label": "Day x channel rows", "path": "rollup.channel_days", "fmt": "int"},
            {"label": "Newest day in the rollup", "path": "rollup.newest_day", "fmt": "time"},
            {"label": "Last good run", "path": "rollup.last_ok_at", "fmt": "time"},
        ],
        "headline": {"path": "rollup.channel_days", "fmt": "int", "short": "day x channel"},
        "live": "marketing_rollup",
    },

    # ====================== 03 AI & INTEGRATIONS ===============================
    {
        "id": "deepseek", "label": "DeepSeek AI", "kind": "service", "icon": "brain",
        "lane": "integrations", "col": 2, "row": 0.8, "stage": True,
        "file": "erp/ai_client.py", "script": "erp/ai_client.py",
        "summary": "Scores each new lead (scale, 0-100 potential, organization type, a note for the Sales rep) and writes the free-text narratives of the daily digest and the weekly report. With no API key, or if the call fails, it falls back to a fixed heuristic so the pipeline never blocks.",
        "inputs": ["Lead facts (new lead)", "Aggregate counts (daily digest)", "7 days of leads + updates (weekly report)"],
        "outputs": ["scale_estimate, potential_score, organization_type, ai_notes -> lead_ai_analysis", "Narrative text for the digest / weekly report"],
        "triggers": [
            {"kind": "event", "label": "Inline for every new lead", "armed_by": ["probe:webhook"]},
            {"kind": "background", "label": "Digest narrative in the ERP Desk daily auto-digest", "armed_by": ["flag:digest_auto"]},
        ],
        "roles": True,   # the triggers above are separate jobs (not alternatives): running only one of them = "partly automated"
        "stats": [
            {"label": "Analyses stored", "path": "ai.total", "fmt": "int"},
            {"label": "Real AI results", "path": "ai.real", "fmt": "int"},
            {"label": "Fallback results", "path": "ai.fallback", "fmt": "int"},
            {"label": "Avg. potential score", "path": "ai.avg_score", "fmt": "dec1"},
            {"label": "API key configured", "path": "env.deepseek", "fmt": "bool_yes"},
            {"label": "Last analysis", "path": "ai.last_at", "fmt": "time"},
        ],
        "headline": {"path": "ai.total", "fmt": "int", "short": "analyses"},
        "live": "deepseek",
    },
    {
        "id": "clickup_push", "label": "Task builder", "kind": "service", "icon": "task",
        "lane": "integrations", "col": 2, "row": 2.0, "stage": True,
        "file": "erp/clickup_client.py", "script": "erp/clickup_client.py",
        "summary": "Creates one ClickUp task per lead, assigned to the chosen rep, with the AI notes in the description. Without a token/list id it creates a mock task (sync_status = 'mocked') so the pipeline still runs end to end.",
        "inputs": ["Lead, AI analysis, the rep's clickup_user_id"],
        "outputs": ["ClickUp task (API call)", "task id + sync_status -> lead_clickup_sync"],
        "triggers": [{"kind": "event", "label": "Inline for every new lead", "armed_by": "probe:webhook"}],
        "stats": [
            {"label": "Tasks synced", "path": "clickup.synced", "fmt": "int"},
            {"label": "Mocked", "path": "clickup.mocked", "fmt": "int"},
            {"label": "Failed", "path": "clickup.failed", "fmt": "int"},
            {"label": "ClickUp configured", "path": "env.clickup", "fmt": "bool_yes"},
            {"label": "Last task created", "path": "clickup.last_at", "fmt": "time"},
        ],
        "headline": {"path": "clickup.synced", "fmt": "int", "short": "tasks synced"},
        "live": "clickup_push",
    },
    {
        "id": "clickup", "label": "ClickUp", "kind": "external", "icon": "clickup",
        "lane": "integrations", "col": 2, "row": 3.1, "stage": False,
        "file": None,
        "summary": "Where the Sales rep actually works the lead: opens the task, reads the AI notes, writes comments. The only human step in the pipeline.",
        "inputs": ["New task from the pipeline", "Comments typed by the Sales rep"],
        "outputs": ["Task comments (fetched later by clickup_pull)", "Workspace members (fetched by sync_employees)"],
        "triggers": [{"kind": "manual", "label": "A Sales rep works the task (human)", "armed_by": "always"}],
        "stats": [
            {"label": "Tasks tracked", "path": "clickup.synced", "fmt": "int"},
            {"label": "Comments seen in DB", "path": "updates.total", "fmt": "int"},
            {"label": "Last comment", "path": "updates.last_occurred", "fmt": "time"},
        ],
        "headline": {"path": "clickup.synced", "fmt": "int", "short": "tasks"},
        "live": "clickup",
    },
    {
        "id": "clickup_pull", "label": "Comment pull", "kind": "job", "icon": "pull",
        "lane": "integrations", "col": 2, "row": 4.2, "stage": True,
        "file": "erp/clickup_pull.py", "script": "erp/clickup_pull.py",
        "summary": "Fetches new comments for every 'synced' ClickUp task and stores them in lead_updates, remembering how far it got (last_comment_pulled_at). Mock-mode tasks are skipped. The README recommends running it every 15 minutes.",
        "inputs": ["ClickUp task comments (API)", "lead_clickup_sync rows with sync_status = 'synced'"],
        "outputs": ["INSERT lead_updates", "UPDATE lead_clickup_sync.last_comment_pulled_at"],
        "triggers": [
            {"kind": "scheduled", "label": "Task Scheduler, every ~15 min", "armed_by": "schedule:clickup_pull", "recommended": True,
             "detail": "python -m erp.clickup_pull, e.g. every 15 minutes (README)."},
            {"kind": "manual", "label": "python -m erp.clickup_pull, or run_daily_check.bat", "armed_by": "always"},
        ],
        "expected_every_hours": 0.25,
        "stats": [
            {"label": "Comments pulled", "path": "updates.total", "fmt": "int"},
            {"label": "Tasks polled", "path": "clickup.synced", "fmt": "int"},
            {"label": "Last pull", "path": "clickup.last_pull_at", "fmt": "time"},
        ],
        "headline": {"path": "updates.total", "fmt": "int", "short": "comments"},
        "live": "clickup_pull",
    },
    {
        "id": "sync_employees", "label": "Staff sync", "kind": "job", "icon": "users",
        "lane": "integrations", "col": 2, "row": 5.3, "stage": True,
        "file": "erp/sync_employees.py", "script": "erp/sync_employees.py",
        "summary": "Copies the ClickUp workspace members into the users table (matched by email): existing staff get their clickup_user_id, new people are created with the role 'sales'. Run it whenever someone joins the workspace.",
        "inputs": ["ClickUp workspace members (id, email, username)"],
        "outputs": ["UPDATE / INSERT users (clickup_user_id, is_active)"],
        "triggers": [{"kind": "manual", "label": "python -m erp.sync_employees --department SALES", "armed_by": "always"}],
        "stats": [
            {"label": "Staff in DB", "path": "users.total", "fmt": "int"},
            {"label": "With a ClickUp id", "path": "users.linked", "fmt": "int"},
            {"label": "Active Sales reps", "path": "users.sales_active", "fmt": "int"},
        ],
        "headline": {"path": "users.linked", "fmt": "int", "short": "staff linked"},
        "live": "sync_employees",
    },

    # ================= 04 MONITORING & REPORTING (jobs column) =================
    {
        "id": "weekly_report", "label": "Weekly report", "kind": "job", "icon": "report",
        "lane": "reporting", "col": 3, "row": 0.8, "stage": True,
        "file": "erp/weekly_report.py", "script": "erp/weekly_report.py",
        "summary": "Summarizes the last 7 days of leads and updates and asks the AI for volume per rep, high-potential leads, observations and next-week actions. Prints to the console; it keeps no log or file.",
        "inputs": ["Leads + updates of the last 7 days (DB)"],
        "outputs": ["AI-written weekly report (console text)"],
        "triggers": [
            {"kind": "scheduled", "label": "Task Scheduler, e.g. Monday morning", "armed_by": "schedule:weekly_report", "recommended": True,
             "detail": "python -m erp.weekly_report every Monday (README)."},
            {"kind": "manual", "label": "python -m erp.weekly_report", "armed_by": "always"},
        ],
        "expected_every_hours": 168,
        "stats": [
            {"label": "Leads in the last 7 days", "path": "leads.n7d", "fmt": "int"},
            {"label": "Updates in the last 7 days", "path": "updates.n7d", "fmt": "int"},
        ],
        "headline": {"path": "leads.n7d", "fmt": "int", "short": "leads / 7d"},
        "live": "weekly_report",
    },
    {
        "id": "daily_digest", "label": "Daily digest", "kind": "job", "icon": "digest",
        "lane": "reporting", "col": 3, "row": 2.0, "stage": True,
        "file": "erp/daily_digest.py", "script": "erp/daily_digest.py",
        "files": ["erp/daily_digest.py", "desktop/digest_service.py"],   # digest_service.py = the in-app once-a-day run (the background trigger)
        "summary": "Gathers 7 key metrics, compares them with yesterday, asks the AI for a status / opportunities / risks / actions narrative, saves daily_digest_latest.json (+ history) and, when run from the .bat, emails it. ERP Desk generates it by itself once a day while open, but never emails.",
        "inputs": ["Metrics from the DB and views", "Yesterday's numbers (daily_digest_history.json)", "AI narrative"],
        "outputs": ["daily_digest_latest.json + daily_digest_history.json", "HTML email (Task Scheduler run only)", "daily_digest.log"],
        "triggers": [
            {"kind": "background", "label": "ERP Desk auto-digest, once a day while the app is open (no email)", "armed_by": "flag:digest_auto"},
            {"kind": "scheduled", "label": "Task Scheduler: run_daily_digest.bat each morning (emails)", "armed_by": "schedule:digest_daily", "recommended": True,
             "detail": "run_daily_digest.bat every morning (README). This is the only run that emails the team."},
            {"kind": "manual", "label": "Generate button in the app / run_daily_digest.bat", "armed_by": "always"},
        ],
        "expected_every_hours": 24,
        "stats": [
            {"label": "Latest digest", "path": "digest.date", "fmt": "text"},
            {"label": "Generated", "path": "digest.generated_at", "fmt": "time"},
            {"label": "Days recorded", "path": "digest.days", "fmt": "int"},
            {"label": "AI narrative", "path": "digest.ai_configured", "fmt": "bool_yes"},
            {"label": "Emailed (last run)", "path": "digest.email_sent", "fmt": "bool_yes"},
            {"label": "Last failure logged", "path": "dlog.last_error_at", "fmt": "time"},
        ],
        "headline": {"path": "digest.generated_at", "fmt": "time", "short": "ran"},
        "live": "daily_digest",
    },
    {
        "id": "emailer", "label": "Email sender", "kind": "service", "icon": "mail",
        "lane": "reporting", "col": 3, "row": 3.2, "stage": True,
        "file": "erp/emailer.py", "script": "erp/emailer.py",
        "summary": "Plain SMTP (stdlib) used for the emailed digest and the Script Center failure alerts. If SMTP_HOST / ALERT_EMAIL_FROM / ALERT_EMAIL_TO are missing it logs a warning and skips - it never crashes the caller.",
        "inputs": ["Subject + HTML body from the digest / Script Center"],
        "outputs": ["Email to ALERT_EMAIL_TO"],
        "triggers": [{"kind": "event", "label": "When a scheduled digest run (or a Script Center failure alert) sends mail", "armed_by": "schedule:digest_daily",
                      "detail": "In-app digest runs never email, so mail only leaves when run_daily_digest.bat runs (Task Scheduler) or someone ticks 'email me on failure' in Script Center."}],
        "stats": [
            {"label": "SMTP configured", "path": "env.smtp", "fmt": "bool_yes"},
            {"label": "Last digest emailed", "path": "digest.email_sent", "fmt": "bool_yes"},
        ],
        "headline": {"path": "env.smtp", "fmt": "bool_on", "short": "SMTP"},
        "live": "emailer",
    },
    {
        "id": "daily_check", "label": "Daily check", "kind": "job", "icon": "check",
        "lane": "reporting", "col": 3, "row": 4.6, "stage": True,
        "file": "erp/daily_check.py", "script": "erp/daily_check.py",
        "summary": "Pulls the latest ClickUp comments, then prints everything that needs attention: SLA breaches, leads not synced to ClickUp, leads without an AI analysis, leads with no follow-up for over 2 days.",
        "inputs": ["Fresh comments (calls clickup_pull.pull_all)", "Views + tables (DB)"],
        "outputs": ["Console list of items needing attention"],
        "triggers": [{"kind": "manual", "label": "run_daily_check.bat / python -m erp.daily_check", "armed_by": "always"}],
        "stats": [
            {"label": "Items needing attention", "path": "attention.total", "fmt": "int"},
            {"label": "Leads breaching SLA", "path": "attention.sla_leads", "fmt": "int"},
            {"label": "Not synced to ClickUp", "path": "attention.unsynced", "fmt": "int"},
            {"label": "Not AI-analyzed", "path": "attention.not_analyzed", "fmt": "int"},
            {"label": "No follow-up > 2 days", "path": "attention.stale_leads", "fmt": "int"},
        ],
        "headline": {"path": "attention.total", "fmt": "int", "short": "to check"},
        "live": "daily_check",
    },

    # ============== 04 MONITORING & REPORTING (outputs column) =================
    {
        "id": "digest_files", "label": "Digest files", "kind": "file", "icon": "file",
        "lane": "reporting", "col": 4, "row": 0.6, "stage": False,
        "file": "daily_digest_latest.json, daily_digest_history.json",
        "files": [],   # runtime artifacts: generated by the digest, may legitimately not exist yet
        "summary": "The hand-over point between the digest job and everything that shows it: the latest digest (metrics, deltas, AI narrative) and one line per day of history for the sparklines.",
        "inputs": ["Written by erp/daily_digest.py"],
        "outputs": ["Read by the live Reporting page (Today's briefing) and by Script Center"],
        "triggers": [{"kind": "passive", "label": "Rewritten by each digest run, read on demand", "armed_by": "always"}],
        "stats": [
            {"label": "Digest date", "path": "digest.date", "fmt": "text"},
            {"label": "Written", "path": "digest.generated_at", "fmt": "time"},
            {"label": "Days of history", "path": "digest.days", "fmt": "int"},
        ],
        "headline": {"path": "digest.date", "fmt": "text", "short": ""},
        "live": "digest_files",
    },
    {
        "id": "live_report", "label": "Live Reporting", "kind": "output", "icon": "chart",
        "lane": "reporting", "col": 4, "row": 1.6, "stage": True,
        "file": "desktop/report_data.py, desktop/today_data.py, desktop/leads_data.py, desktop/sources_data.py, desktop/health_data.py, desktop/channels_data.py, desktop/placements_data.py", "script": None,
        "summary": "The Reporting page of this app: a background thread re-queries the database every few seconds, the page polls for changes and animates only what really changed. It also shows today's briefing from the digest files. The same node covers the Today landing page (the first page the app opens on): one plain-English status sentence, a 'since yesterday' line, six tiles, who to chase and a system-health strip, read on demand from the database (each query time-boxed) and from this Data Flow feed. A tile, an attention row or a clause of the 'since yesterday' line opens a detail drawer showing one lead or ticket in full (owner, arrival, deadline, the lead_ai_analysis summary, the lead_clickup_sync task and the newest lead_updates note); its rows travel in the same payload, so there is still exactly one poller. The status sentence consults the same armed-trigger verdicts: with the lead webhook offline or the ClickUp comment pull unscheduled it says 'No problems found, but the numbers may be incomplete' instead of a bare 'All good'. The Leads page (Ctrl+5) is a funnel + SLA explorer for analysts on the same node: server-side filters (date range, rep, source, status), stage-to-stage conversion, SLA outcomes, time to first reply, per-rep / per-source tables, a sortable detail table and a CSV download, all read on demand from v_leads_summary, lead_updates and lead_ai_analysis with the same 'past SLA' SQL as the Today page. Its second tab, 'Sources & cohorts' (desktop/sources_data.py), reads the same base rows again for analysts: one row per lead source (share of the view, how many reached each pipeline stage, the five SLA outcomes, median time to first reply, which reps got them) plus arrival-cohort curves - leads grouped by the day, week or month they arrived and followed day by day since arrival for 'replied', 'reached ClickUp' or 'past SLA'. Only the tab on screen polls, so the Leads page still has exactly one poller; the bucket, the metric and the selected cohort are whitelisted server-side like every other filter. The Health page (Ctrl+6, desktop/health_data.py) is the detail view of the Today health strip: one card per dependency (PostgreSQL, the lead webhook, ClickUp, DeepSeek, SMTP, each scheduled job, ERP Desk's own loops, Script Center) with its state, why, when it was last checked and last succeeded, what stops working without it and a copyable fix hint. It opens no connection and starts no thread: it composes this Data Flow snapshot and the Today payload, and it never runs or changes anything."
                   + " The Channels page (Ctrl+7, desktop/channels_data.py; its Insights panel and Excel report are desktop/insights_*.py) is the marketing channel-performance report for managers and analysts: a plain-English headline, six KPI tiles (sessions, clicks, conversions, conversion rate, spend, revenue) with a previous-period delta and a sparkline, a per-day line chart per channel plus a per-week chart for a weekly source (a week is never drawn as one day), a per-channel table (share of sessions, CTR, conversion rate, spend, revenue, cost per conversion; sortable on the server) with a campaign drill-down, filters (date range, channel, campaign, currency), a Definitions drawer and three CSV downloads. Money is shown in the currency the source reported and is never converted or added across currencies: with several currencies in view every spend / revenue figure is listed per currency; derived revenue is labelled derived. It reads ONLY the two Phase 4 rollup tables (interaction_daily_channel_rollup, and interaction_daily_rollup for the drill-down or a campaign filter) plus the channel / campaign name lookups and the refresh journal - never interaction_fact or the landing table - so opening it costs the same at any raw volume. It does not run or schedule anything: the rollup job stays manual (or a Task Scheduler entry the owner creates), the page shows whatever exists. In the owner's database today the rollup tables are not installed, so it draws a 'not installed' setup card with the exact psql command, and once installed but empty a 'no data loaded yet' card - never a zero.",
        "inputs": ["Leads, tickets, timeline (DB; the queries live in desktop/report_data.py)", "Digest files",
                   "Today page: read-only SQL (leads, lead_updates, tickets, and for the detail drawer also lead_ai_analysis and "
                   "lead_clickup_sync) plus the health verdicts of this Data Flow feed",
                   "Leads page: read-only SQL (v_leads_summary, lead_updates, lead_ai_analysis), filtered on the server",
                   "Sources & cohorts tab: the same read-only SQL plus lead_clickup_sync.last_synced_at (the day a ClickUp task appeared)",
                   "Health page: no source of its own - the Today health cells (desktop/today_data.build_health) plus this "
                   "Data Flow snapshot (armed triggers, Task Scheduler scan, node health) and erp/config.py as booleans only",
                   "Channels page: read-only SQL on the two rollup tables (interaction_daily_channel_rollup, interaction_daily_rollup), the channel / "
                   "campaign name lookups and the refresh journal (marketing_rollup_run) - only when the owner has installed db/sql/10_marketing_currency.sql and db/sql/09_marketing_rollup.sql. "
                   "The Channels Insights panel and the Excel report read the same two rollup tables (one campaign-level scan over the analysed and the previous window) and never interaction_fact. "
                   "The Placements panel (and the placement rules of Insights, and a Placements sheet of the Excel report) read ONE more table, interaction_placement_rollup, only when the owner has installed db/sql/11_marketing_placements.sql; without it the panel says so and nothing else changes"],
        "outputs": ["Linked, cross-filtered dashboards in this window",
                    "Today page: status sentence, a 'since yesterday' line, KPI tiles, attention list, system health and a click-through "
                    "detail drawer (lead / ticket in full, AI summary, ClickUp task and last pulled note) - all in one GET /api/today",
                    "Leads page: funnel, SLA and reply-time analysis (GET /api/leads/analysis) and a CSV download of the filtered leads (GET /api/leads/export.csv)",
                    "Sources & cohorts tab: the per-source table and the arrival-cohort curves (GET /api/leads/sources) plus two aggregate CSV downloads, source table and cohort curves (GET /api/leads/sources.csv)",
                    "Health page: one honest headline, one card per dependency with a fix hint, what the amber/red items "
                    "mean for the numbers, and configuration truth as 'set' / 'not set' (GET /api/health)",
                    "Channels page: headline, KPI tiles, per-day chart, per-channel table and campaign drill-down (GET /api/channels) and three aggregate CSV "
                    "downloads - channels, daily, campaigns (GET /api/channels.csv); or, until the rollup exists, a setup card",
                    "Channels Insights (GET /api/channels/insights, desktop/insights_*.py): severity-sorted findings from six fixed rules (spend without conversions, cost-per-conversion jump, "
                    "conversion-rate drop, stale channel / data gap, spend concentration, campaigns worth scaling) with the evidence and a suggested action, judged per currency, plus plain-text automation ideas "
                    "that schedule and run nothing; and an Excel workbook of the same view (GET /api/channels/report.xlsx: Summary, Channels, Campaigns, Placements when loaded, Insights, Definitions). "
                    "Channels Placements (GET /api/channels/placements and /api/channels/placements.csv): where display ads ran (site, app or banner slot) with size, position, CTR, cost per conversion and viewability per currency, "
                    "five more explainable rules in Insights (exclude candidates, scale candidates, low viewability, spend concentration, a clearly labelled reallocation ESTIMATE) - suggestions only, nothing is changed for the owner"],
        "triggers": [{"kind": "background", "label": "ERP Desk refresher thread, every few seconds", "armed_by": "flag:report_refresher"},
                     {"kind": "manual", "label": "Today, Leads (both tabs), Health and Channels pages: read on demand while the page is open (no background job)", "armed_by": "always"}],
        "stats": [
            {"label": "DB re-queries so far", "path": "report.refresh_count", "fmt": "int"},
            {"label": "Refresh interval (s)", "path": "report.interval", "fmt": "int"},
            {"label": "Last DB check", "path": "report.refreshed_at", "fmt": "time"},
        ],
        "headline": {"path": "report.interval", "fmt": "int", "short": "s refresh"},
        "live": "live_report",
    },
    {
        "id": "script_center", "label": "Script Center", "kind": "output", "icon": "terminal",
        "lane": "reporting", "col": 4, "row": 2.6, "stage": True,
        "file": "dashboard/script_center.py", "script": "dashboard/script_center.py",
        "summary": "The Management page: catalog, mindmap, editor and test runner for every script, a structured activity log (script_center.log) and the latest digest. Streamlit, started and supervised by ERP Desk.",
        "inputs": ["The scripts on disk", "Digest files"],
        "outputs": ["Edited files, test runs, script_center.log, optional failure emails"],
        "triggers": [{"kind": "manual", "label": "You open the page / press Test run", "armed_by": "always"}],
        "stats": [
            {"label": "Logged runs / checks", "path": "center.entries", "fmt": "int"},
            {"label": "Failed", "path": "center.errors", "fmt": "int"},
            {"label": "Last activity", "path": "center.last_at", "fmt": "time"},
        ],
        "headline": {"path": "center.entries", "fmt": "int", "short": "logged"},
        "live": "script_center",
    },
    {
        "id": "inbox", "label": "Team inbox", "kind": "external", "icon": "inbox",
        "lane": "reporting", "col": 4, "row": 3.6, "stage": False,
        "file": None,
        "summary": "Whoever is listed in ALERT_EMAIL_TO receives the emailed digest and failure alerts.",
        "inputs": ["Emails from erp/emailer.py"],
        "outputs": ["Humans read the briefing"],
        "triggers": [{"kind": "external", "label": "Delivery by your mail server", "armed_by": "external"}],
        "stats": [{"label": "SMTP configured", "path": "env.smtp", "fmt": "bool_yes"}],
        "headline": {"path": "env.smtp", "fmt": "bool_on", "short": "SMTP"},
        "live": "inbox",
    },
    {
        "id": "powerbi", "label": "Power BI", "kind": "external", "icon": "pbi",
        "lane": "reporting", "col": 4, "row": 4.6, "stage": False,
        "file": "db/sql/06_powerbi_readonly.sql",
        "summary": "Power BI Desktop connects with the read-only role powerbi_reader (DirectQuery or Import) to the two summary views - it can not see raw tables or write anything.",
        "inputs": ["v_leads_summary, v_tickets_summary (role powerbi_reader)"],
        "outputs": ["Your own Power BI reports"],
        "triggers": [{"kind": "manual", "label": "You refresh / open the report in Power BI", "armed_by": "always"}],
        "stats": [{"label": "Role powerbi_reader exists", "path": "views.role_ok", "fmt": "bool_yes"}],
        "headline": {"path": "views.role_ok", "fmt": "bool_ok", "short": "role"},
        "live": "powerbi",
    },
]


# --------------------------------------------------------------------------- edges
# "trigger" is how this hop is started by design. "armed_by" is what must be true for THIS hop to
# be running right now (same vocabulary as a node trigger's armed_by, a string or a list = any of):
# armed -> drawn live (bright, fast dots), not armed -> dormant (dashed), cannot be checked -> unknown.
# It is per edge on purpose: a node can be armed for one job (DeepSeek writes the digest narrative
# every day) while a hop through it (lead -> DeepSeek) still needs the webhook to be up.
# "driver" is only the node whose health colours the edge (red when that node is in error).
# Manual / passive edges need no armed_by.
EDGES = [
    # ---- lead pipeline (event-driven)
    {"id": "e_form_webhook", "from": "lead_form", "to": "webhook", "trigger": "event", "driver": "webhook", "armed_by": "probe:webhook",
     "counter": "leads",
     "label": "HTTP POST", "data": "Lead JSON: full_name, email / phone, company, source"},
    {"id": "e_webhook_logic", "from": "webhook", "to": "leads_logic", "trigger": "event", "driver": "webhook", "armed_by": "probe:webhook",
     "counter": "leads",
     "label": "process_new_lead()", "data": "Validated LeadPayload (needs an email or a phone)"},
    {"id": "e_logic_db", "from": "leads_logic", "to": "postgres", "trigger": "event", "driver": "leads_logic", "armed_by": "probe:webhook",
     "counter": "assign",
     "label": "insert + assign", "data": "INSERT leads (+ SLA due date), dedup lookup by email/phone, INSERT lead_assignments (round_robin_new | existing_duplicate)"},
    {"id": "e_logic_ai", "from": "leads_logic", "to": "deepseek", "trigger": "event", "driver": "deepseek", "armed_by": "probe:webhook",
     "counter": "ai",
     "label": "analyze_lead()", "data": "Lead facts as a prompt (name, contact, company, source)"},
    {"id": "e_ai_db", "from": "deepseek", "to": "postgres", "trigger": "event", "driver": "deepseek", "armed_by": "probe:webhook",
     "counter": "ai",
     "label": "AI result", "data": "scale_estimate, potential_score, organization_type, ai_notes -> lead_ai_analysis"},
    {"id": "e_logic_push", "from": "leads_logic", "to": "clickup_push", "trigger": "event", "driver": "clickup_push", "armed_by": "probe:webhook",
     "counter": "clickup",
     "label": "create_task()", "data": "Task name, description with the AI notes, the rep's clickup_user_id"},
    {"id": "e_push_clickup", "from": "clickup_push", "to": "clickup", "trigger": "event", "driver": "clickup_push", "armed_by": "probe:webhook",
     "counter": "clickup",
     "label": "ClickUp API", "data": "POST /list/{id}/task (or a mock task when ClickUp is not configured)"},
    {"id": "e_push_db", "from": "clickup_push", "to": "postgres", "trigger": "event", "driver": "clickup_push", "armed_by": "probe:webhook",
     "counter": "clickup",
     "label": "task id", "data": "clickup_task_id + sync_status (synced | mocked) -> lead_clickup_sync"},
    # ---- pull back
    {"id": "e_clickup_pull", "from": "clickup", "to": "clickup_pull", "trigger": "scheduled", "driver": "clickup_pull", "armed_by": "schedule:clickup_pull",
     "counter": "updates",
     "label": "comments", "data": "Sales rep comments on each synced task (ClickUp API)"},
    {"id": "e_pull_db", "from": "clickup_pull", "to": "postgres", "trigger": "scheduled", "driver": "clickup_pull", "armed_by": "schedule:clickup_pull",
     "counter": "updates",
     "label": "lead_updates", "data": "INSERT lead_updates + UPDATE last_comment_pulled_at"},
    {"id": "e_clickup_staff", "from": "clickup", "to": "sync_employees", "trigger": "manual", "driver": "sync_employees", "counter": "staff",
     "label": "members", "data": "Workspace members: id, email, username"},
    {"id": "e_staff_db", "from": "sync_employees", "to": "postgres", "trigger": "manual", "driver": "sync_employees", "counter": "staff",
     "label": "users", "data": "UPDATE users.clickup_user_id / INSERT new staff (role sales)"},
    {"id": "e_tickets_db", "from": "ticket_entry", "to": "postgres", "trigger": "manual", "driver": "ticket_entry", "counter": "tickets",
     "label": "SQL", "data": "tickets, ticket_comments, ticket_status_history (seed SQL / hand entry)"},
    {"id": "e_marketing_db", "from": "marketing_ingest", "to": "postgres", "trigger": "manual", "driver": "marketing_ingest", "counter": "marketing",
     "label": "land -> load", "data": "marketing_landing, marketing_source/channel/campaign/creative/identity, interaction_fact (with its currency), marketing_ingest_run "
                                       "(bulk INSERT ... ON CONFLICT, never row by row)"},
    {"id": "e_rollup_db", "from": "marketing_rollup", "to": "postgres", "trigger": "scheduled", "driver": "marketing_rollup", "armed_by": "schedule:marketing_rollup",
     "counter": "rollup",
     "label": "fact -> rollup", "data": "SELECT interaction_fact by date range (grouped by currency and grain), then DELETE + INSERT interaction_daily_rollup / interaction_daily_channel_rollup (and, when db/sql/11 is installed, interaction_placement_rollup) + marketing_rollup_run; "
                                        "creates empty future interaction_fact partitions (never detaches or drops one)"},
    {"id": "e_autorun_ingest", "from": "marketing_autorun", "to": "marketing_ingest", "trigger": "scheduled", "driver": "marketing_autorun",
     "armed_by": "schedule:marketing_autorun", "counter": "autorun",
     "label": "CSV -> pipeline", "data": "Each *.csv in the inbox: connector detected from the header (or the filename / channel-column hint), then loaded by the "
                                          "existing ingest pipeline; the file moves to processed/ or failed/ (+ .reason.txt)"},
    {"id": "e_autorun_rollup", "from": "marketing_autorun", "to": "marketing_rollup", "trigger": "scheduled", "driver": "marketing_autorun",
     "armed_by": "schedule:marketing_autorun", "counter": "autorun",
     "label": "refresh once", "data": "After the files: ONE incremental rollup refresh, only when at least one file loaded rows"},
    # ---- views
    {"id": "e_db_views", "from": "postgres", "to": "views", "trigger": "passive", "driver": "views", "counter": None,
     "label": "joins", "data": "v_leads_summary + v_tickets_summary are computed from the tables on every SELECT"},
    {"id": "e_views_pbi", "from": "views", "to": "powerbi", "trigger": "manual", "driver": "powerbi", "counter": None,
     "label": "powerbi_reader", "data": "SELECT-only access to the two views (DirectQuery or Import)",
     "via": [[1.5, 5.6], [2.5, 5.6], [3.5, 5.5]]},
    # ---- monitoring / reporting reads
    {"id": "e_db_check", "from": "postgres", "to": "daily_check", "trigger": "manual", "driver": "daily_check", "counter": None,
     "label": "attention queries", "data": "SLA breaches, unsynced / un-analyzed leads, leads with no follow-up",
     "via": [[1.5, 5.05], [2.5, 5.05]]},
    {"id": "e_check_pull", "from": "daily_check", "to": "clickup_pull", "trigger": "manual", "driver": "daily_check", "counter": None,
     "label": "pull_all()", "data": "The daily check starts with a comment pull"},
    {"id": "e_db_digest", "from": "postgres", "to": "daily_digest", "trigger": "background", "driver": "daily_digest", "armed_by": ["flag:digest_auto", "schedule:digest_daily"],
     "counter": "digests",
     "label": "7 metrics", "data": "Totals, SLA breaches, sync gaps, open tickets, avg score (via the views)",
     "via": [[1.5, 2.93], [2.5, 2.93]]},
    {"id": "e_digest_ai", "from": "daily_digest", "to": "deepseek", "trigger": "background", "driver": "daily_digest", "armed_by": ["flag:digest_auto", "schedule:digest_daily"],
     "counter": "digests",
     "label": "narrative", "data": "Aggregate counts only (no customer data) -> AI narrative"},
    {"id": "e_db_weekly", "from": "postgres", "to": "weekly_report", "trigger": "scheduled", "driver": "weekly_report", "armed_by": "schedule:weekly_report",
     "counter": None,
     "label": "7-day data", "data": "Leads + updates of the last 7 days",
     "via": [[1.5, 1.72], [2.5, 1.72]]},
    {"id": "e_weekly_ai", "from": "weekly_report", "to": "deepseek", "trigger": "scheduled", "driver": "weekly_report", "armed_by": "schedule:weekly_report",
     "counter": None,
     "label": "prompt", "data": "The week's leads + updates as JSON -> AI-written report"},
    {"id": "e_digest_mail", "from": "daily_digest", "to": "emailer", "trigger": "scheduled", "driver": "emailer", "armed_by": "schedule:digest_daily",
     "counter": None,
     "label": "HTML digest", "data": "Subject + HTML body (Task Scheduler runs only)"},
    {"id": "e_mail_inbox", "from": "emailer", "to": "inbox", "trigger": "scheduled", "driver": "emailer", "armed_by": "schedule:digest_daily",
     "counter": None,
     "label": "SMTP", "data": "Email to ALERT_EMAIL_TO"},
    {"id": "e_digest_files", "from": "daily_digest", "to": "digest_files", "trigger": "background", "driver": "daily_digest", "armed_by": ["flag:digest_auto", "schedule:digest_daily"],
     "counter": "digests",
     "label": "JSON", "data": "daily_digest_latest.json + daily_digest_history.json"},
    {"id": "e_files_live", "from": "digest_files", "to": "live_report", "trigger": "background", "driver": "live_report", "armed_by": "flag:report_refresher",
     "counter": None,
     "label": "briefing", "data": "Today's briefing cards (status, deltas, opportunities / risks / actions)"},
    {"id": "e_files_center", "from": "digest_files", "to": "script_center", "trigger": "manual", "driver": "script_center", "counter": None,
     "label": "latest digest", "data": "The digest card in Script Center's Logs & digest section"},
    {"id": "e_db_live", "from": "postgres", "to": "live_report", "trigger": "background", "driver": "live_report", "armed_by": "flag:report_refresher",
     "counter": "refreshes",
     "label": "re-query", "data": "Leads, tickets and the work-in-progress timeline, re-queried every few seconds (the Today, Leads and Health pages add their own read-only queries, on demand; "
             "the Channels page reads only the marketing rollup tables, and only once db/sql/10 and db/sql/09 are installed)",
     "via": [[1.5, 1.86], [2.5, 1.86], [3.5, 1.86]]},
]


# --------------------------------------------------------------------------- lead journey (replay)
# The steps the "Trace the latest lead" button plays, in order. `stage` is looked up in the real
# rows of the newest lead (received | assigned | analyzed | task | update) so a step is only shown as
# reached if that lead really got there.
TRACE = [
    {"edge": "e_form_webhook", "stage": "received", "text": "Submitted through {source}"},
    {"edge": "e_webhook_logic", "stage": "received", "text": "Validated and handed to process_new_lead()"},
    {"edge": "e_logic_db", "stage": "assigned", "text": "Saved, duplicate check, assigned to a rep ({reason})"},
    {"edge": "e_logic_ai", "stage": "analyzed", "text": "Lead facts sent to the AI"},
    {"edge": "e_ai_db", "stage": "analyzed", "text": "AI score {score} stored in lead_ai_analysis"},
    {"edge": "e_logic_push", "stage": "task", "text": "Task payload built with the AI notes"},
    {"edge": "e_push_clickup", "stage": "task", "text": "ClickUp task created ({sync})"},
    {"edge": "e_push_db", "stage": "task", "text": "Task id saved in lead_clickup_sync"},
    {"edge": "e_clickup_pull", "stage": "update", "text": "Sales rep's comment fetched from ClickUp"},
    {"edge": "e_pull_db", "stage": "update", "text": "Comment stored in lead_updates"},
]
