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
not exist yet (daily_digest_latest.json, erp_report.html ...) do NOT belong in "files".

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
    "cskh_dashboard": "http://127.0.0.1:8501/_stcore/health",  # run_dashboard.bat
}

# A scheduled task counts as "this job" when its command line contains one of these.
SCHEDULE_MARKERS = {
    "clickup_pull": ["erp.clickup_pull", "clickup_pull.py"],
    "digest_daily": ["run_daily_digest.bat", "erp.daily_digest"],
    "weekly_report": ["erp.weekly_report", "weekly_report.py"],
    "daily_check": ["run_daily_check.bat", "erp.daily_check"],
    "sync_employees": ["erp.sync_employees", "sync_employees.py"],
    "webhook": ["run_webhook.bat", "erp.webhook_app"],
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
    # ERP Desk / Data Flow plumbing (the app that draws this page, not a stage of the pipeline)
    "desktop/launcher.py": "ERP Desk supervisor: starts the servers, the window and the child processes.",
    "desktop/server.py": "ERP Desk web server: serves the pages and the JSON feeds, moves no business data.",
    "desktop/winutil.py": "Win32 helpers (job objects, single instance) for the ERP Desk launcher.",
    "desktop/make_icon.py": "Generates the ERP Desk app icon. Cosmetic, one-off.",
    "desktop/flow_definition.py": "This map itself.",
    "desktop/flow_data.py": "Live numbers + the sync check of this map.",
    # standalone learning demos (the Script Center lists them as LEARNING_DEMOS too)
    "postwebhook.py": "Standalone demo that POSTs a sample order to a third-party webhook. Not the lead pipeline.",
    "sql_change_webhook_demo.py": "Self-contained teaching demo of the polling pattern, runs on mock data.",
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
        "file": "desktop/report_data.py", "script": None,
        "summary": "The Reporting page of this app: a background thread re-queries the database every few seconds, the page polls for changes and animates only what really changed. It also shows today's briefing from the digest files.",
        "inputs": ["Leads, tickets, timeline (DB, same queries as erp/html_report.py)", "Digest files"],
        "outputs": ["Linked, cross-filtered dashboards in this window"],
        "triggers": [{"kind": "background", "label": "ERP Desk refresher thread, every few seconds", "armed_by": "flag:report_refresher"}],
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
        "id": "html_report", "label": "HTML report", "kind": "output", "icon": "chart2",
        "lane": "reporting", "col": 4, "row": 4.6, "stage": True,
        "file": "erp/html_report.py", "script": "erp/html_report.py",
        "summary": "Generates the standalone erp_report.html (Plotly + custom CSS/JS) - a static snapshot you can open or send around. Also the source of the queries the live page reuses.",
        "inputs": ["Leads, tickets, timeline (DB)"],
        "outputs": ["erp_report.html"],
        "triggers": [{"kind": "manual", "label": "python -m erp.html_report", "armed_by": "always"}],
        "stats": [
            {"label": "erp_report.html written", "path": "files.report_html_at", "fmt": "time"},
            {"label": "Size (KB)", "path": "files.report_html_kb", "fmt": "int"},
        ],
        "headline": {"path": "files.report_html_at", "fmt": "time", "short": "built"},
        "live": "html_report",
    },
    {
        "id": "cskh_dashboard", "label": "CSKH dashboard", "kind": "output", "icon": "dash",
        "lane": "reporting", "col": 4, "row": 5.6, "stage": True,
        "file": "dashboard/streamlit_app.py", "script": "dashboard/streamlit_app.py",
        "summary": "The original Streamlit dashboard: support tickets, leads & sales, and a staff vs. ClickUp cross-check.",
        "inputs": ["Tickets, leads, staff (DB)", "ClickUp members"],
        "outputs": ["Interactive tables and charts (http://localhost:8501)"],
        "triggers": [{"kind": "manual", "label": "run_dashboard.bat (on demand)", "armed_by": "always"}],
        "stats": [{"label": "Running on :8501", "path": "probe.cskh_dashboard", "fmt": "bool_up"}],
        "headline": {"path": "probe.cskh_dashboard", "fmt": "bool_up", "short": "app"},
        "live": "cskh_dashboard",
    },
    {
        "id": "powerbi", "label": "Power BI", "kind": "external", "icon": "pbi",
        "lane": "reporting", "col": 4, "row": 6.6, "stage": False,
        "file": "db/sql/06_powerbi_readonly.sql",
        "summary": "Power BI Desktop connects with the read-only role powerbi_reader (DirectQuery or Import) to the two summary views - it can not see raw tables or write anything.",
        "inputs": ["v_leads_summary, v_tickets_summary (role powerbi_reader)"],
        "outputs": ["Your own Power BI reports"],
        "triggers": [{"kind": "manual", "label": "You refresh / open the report in Power BI", "armed_by": "always"}],
        "stats": [{"label": "Role powerbi_reader exists", "path": "views.role_ok", "fmt": "bool_yes"}],
        "headline": {"path": "views.role_ok", "fmt": "bool_ok", "short": "role"},
        "live": "powerbi",
    },
    {
        "id": "timeline_chart", "label": "Timeline chart", "kind": "output", "icon": "gantt",
        "lane": "reporting", "col": 4, "row": 7.6, "stage": True,
        "file": "erp/task_timeline_chart.py", "script": "erp/task_timeline_chart.py",
        "summary": "Draws a Gantt-style picture of what each employee is handling right now (active leads + open tickets, each bar ending at its SLA due date, red when breached) and saves it as task_timeline.png. Read-only against the database; it overwrites the PNG every time.",
        "inputs": ["Active leads (current assignment) + open tickets, with SLA due dates (DB)"],
        "outputs": ["task_timeline.png in the project root"],
        "triggers": [{"kind": "manual", "label": "python -m erp.task_timeline_chart", "armed_by": "always"}],
        "stats": [
            {"label": "task_timeline.png written", "path": "files.timeline_png_at", "fmt": "time"},
            {"label": "Size (KB)", "path": "files.timeline_png_kb", "fmt": "int"},
        ],
        "headline": {"path": "files.timeline_png_at", "fmt": "time", "short": "built"},
        "live": "timeline_chart",
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
    # ---- views
    {"id": "e_db_views", "from": "postgres", "to": "views", "trigger": "passive", "driver": "views", "counter": None,
     "label": "joins", "data": "v_leads_summary + v_tickets_summary are computed from the tables on every SELECT"},
    {"id": "e_views_pbi", "from": "views", "to": "powerbi", "trigger": "manual", "driver": "powerbi", "counter": None,
     "label": "powerbi_reader", "data": "SELECT-only access to the two views (DirectQuery or Import)",
     "via": [[1.5, 6.35], [2.5, 6.35]]},
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
     "label": "re-query", "data": "Leads, tickets and the work-in-progress timeline, re-queried every few seconds",
     "via": [[1.5, 1.86], [2.5, 1.86], [3.5, 1.86]]},
    {"id": "e_db_html", "from": "postgres", "to": "html_report", "trigger": "manual", "driver": "html_report", "counter": "reports",
     "label": "report queries", "data": "The same queries as the live page -> a static HTML snapshot",
     "via": [[1.5, 4.05], [2.5, 4.1], [3.5, 4.3]]},
    {"id": "e_db_timeline", "from": "postgres", "to": "timeline_chart", "trigger": "manual", "driver": "timeline_chart", "counter": None,
     "label": "task query", "data": "Active leads + open tickets per employee with their SLA due dates",
     "via": [[1.5, 7.3], [2.5, 7.3], [3.5, 7.3]]},
    {"id": "e_db_cskh", "from": "postgres", "to": "cskh_dashboard", "trigger": "manual", "driver": "cskh_dashboard", "counter": None,
     "label": "tickets + leads", "data": "Tickets, leads and staff for the CSKH dashboard",
     "via": [[1.5, 5.2], [2.5, 5.2], [3.5, 5.75]]},
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
