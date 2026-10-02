# ERP Customer Support / Lead-to-Sale

Automation system: lead received from a form -> saved to PostgreSQL -> duplicate check ->
assigned to a Sales rep -> analyzed by AI (DeepSeek) -> ClickUp task created -> updates
synced back -> dashboard for tracking + AI-generated weekly report.

Also includes a Customer Support/IT Support ticket model (departments share a single `tickets` table).

**The user interface is ERP Desk** (`desktop/`, see below) - the local desktop app, with the Script Center
embedded in it as the Management tab. Since 2026-09-23 it is the only one: the standalone HTML report, the
CSKH Streamlit dashboard and the task-timeline PNG were removed (see the change log at the end).

## Directory structure

```
db/sql/                 Scripts to create the database, schema, sample data (run in order 00 -> 05); 07/08/09 are the
                        marketing star schema, the two missing `leads` indexes and the marketing rollup tables (additive, see below);
                        10 adds the currency column to the marketing fact (additive, the owner runs it: 07 -> 10 -> 09)
erp/
  marketing/            Marketing/channel interaction data: star schema helpers + the shared ingestion pipeline (Phase 1)
                        + the daily rollup refresh and partition maintenance (Phase 4: rollup.py, partitions.py,
                        rollup_check.py) + the two strict source-specific connectors for the owner's real exports
                        (connectors/ad_performance.py, email_campaign.py, on strict_csv.py + parse.py; sample_check.py verifies
                        them on the real files in a throwaway schema) - see "Marketing / channel interaction data" below
                        and docs/marketing-data-architecture.md
                        + the inbox processor autorun.py (CSV folder -> detect, load, move, rollup; run_marketing_autorun.bat)
  config.py             Reads environment variables from .env
  db.py                 SQLAlchemy engine/session connecting to PostgreSQL
  leads.py              Business logic: dedup, Sales assignment, pipeline orchestration
  ai_client.py          AI Agent calling DeepSeek (lead analysis + weekly report), with fallback
  clickup_client.py     Calls the ClickUp API: create task, fetch comments, fetch member list
  webhook_app.py        FastAPI - endpoint that receives leads from a form (POST /webhooks/leads)
  clickup_pull.py       Scheduled script: pulls comments from ClickUp tasks into the DB
  weekly_report.py      Scheduled script: AI-generated weekly summary report
  sync_employees.py     Syncs ClickUp workspace members INTO the users table (create/update)
  business_hours.py     Computes SLA due dates using business hours
  daily_check.py         Daily health check: sync + list items needing attention
  emailer.py            Sends email alerts/digests via SMTP (stdlib only), with a safe no-op fallback
  daily_digest.py       Scheduled script: daily metrics + deltas + AI narrative, emailed + saved to JSON
desktop/                ERP Desk - the local "desktop app": launcher, shell server, Today landing page (today_data.py), the shared SLA wording both it and Leads use (sla_words.py), Leads funnel + SLA explorer (leads_data.py), its "Sources & cohorts" tab (sources_data.py), Health page (health_data.py = one card per dependency, composed from the Today health cells + the Data Flow snapshot), live report page, Data Flow page (flow_definition.py = the map + its IGNORE list, flow_data.py = live numbers + the map sync check; static/flow.js = 2D map, static/flow3d/ = the 3D scene), icon + shortcut installer
dashboard/
  script_center.py      Script Center: catalog + mindmap + editor + test runner for every script here
                        (ERP Desk embeds it as the Management tab; also runs standalone on 8502)
tests/
  smoke.py              Read-only merge-gate smoke check (`run_smoke.bat`, see "Smoke check" below)
  smoke_selftest.py     Scenarios that prove the merge gate's own protections still work (run by smoke check 9)
  today_scenarios.py    Scenario table for the Today page's pure builders (headline, "since yesterday", the detail
                        drawer) and hang-safety (also run by the smoke gate)
  leads_scenarios.py    Scenario table for the Leads page: filters / whitelists, funnel + SLA math, CSV escaping, SQL on a synthetic session (also run by the smoke gate)
  sources_scenarios.py  Scenario table for the Leads page's "Sources & cohorts" tab: cohort maths (day-0 bucketing, cumulative
                        counts, a single-point cohort, a reply before arrival), low-n rules, bucket choice, whitelists, CSV;
                        plus SQL on a synthetic FOUR-cohort session (also run by the smoke gate)
  health_scenarios.py   Scenario table for the Health page: every card state / word / fix hint, the headline, what the
                        amber and red cards mean for the numbers, and the proof that no .env value can reach the payload
                        (also run by the smoke gate)
  marketing_autorun_scenarios.py  Scenario table for the inbox processor (detection, partial-file skip, duplicates by hash,
                        routing + reason sidecars, dry run, rollup once, dead database, end to end in a throwaway schema); run by hand
  marketing_scenarios.py  Scenario table for the marketing ingestion pipeline: typing/coercion, validation, malformed-row
                        handling, dedupe, the scoped GIN allowlist, the flat-file connector, DDL-vs-code agreement, plus the
                        rollup/partition logic (day windows, range planning, retention candidates, CLI exit codes), and the real-file work: strict
                        parsers (money, dates and the M/D vs D/M refusal, BOM), both connectors' column mapping, the duplicate-key
                        discriminator and its idempotency, the sanity rejections, the currency rules, migration 10 / rollup DDL
                        (pure logic, no database; also run by the smoke gate)
.env                    Real configuration (NOT committed to git, already in .gitignore)
.env.example            Example configuration for reference
```

## First-time setup

```bash
venv\Scripts\pip install -r requirements.txt
```

Fill in `.env` (copy from `.env.example`):
- `DB_*`: PostgreSQL connection details (a dedicated database for this project, not shared with other apps)
- `CLICKUP_API_TOKEN`, `CLICKUP_LIST_ID`: leave blank to run in mock mode (no real ClickUp calls)
- `DEEPSEEK_API_KEY`: leave blank to use the fallback (no real AI calls)

## Database initialization (run once, in this exact order)

```bash
& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U postgres -h 127.0.0.1 -p 5432 -v pw="'ERP_APP_PASSWORD'" -f db/sql/00_create_database.sql

$env:PGPASSWORD="<ERP_APP_PASSWORD>"
$psql = "C:\Program Files\PostgreSQL\18\bin\psql.exe"
& $psql -U erp_app -h 127.0.0.1 -p 5432 -d erp_support -f db/sql/01_schema.sql
& $psql -U erp_app -h 127.0.0.1 -p 5432 -d erp_support -f db/sql/02_seed_cskh.sql
& $psql -U erp_app -h 127.0.0.1 -p 5432 -d erp_support -f db/sql/03_leads_schema.sql
& $psql -U erp_app -h 127.0.0.1 -p 5432 -d erp_support -f db/sql/04_seed_sales.sql
& $psql -U erp_app -h 127.0.0.1 -p 5432 -d erp_support -f db/sql/05_leads_sla.sql
```

## Quick start (double-click)

- `run_webhook.bat` - starts the lead-receiving webhook (keep the window open, Ctrl+C to stop)
- `run_daily_check.bat` - syncs ClickUp + lists items needing attention (SLA breaches,
  unsynced leads, leads not yet analyzed by AI, leads with no recent update)
- `run_script_center.bat` - opens the Script Center (see below) at http://localhost:8502
- `run_daily_digest.bat` - generates + emails the daily digest once
- `run_smoke.bat` - the read-only smoke check / merge gate (9 checks; about 22 s on an idle machine, 45-55 s on a cold
  first run - see "Smoke check" below)
- `run_erp_desktop.bat` / `run_erp_desktop.vbs` - starts **ERP Desk** (the desktop app, see below); `install_erp_desktop.bat` adds Desktop + Start Menu shortcuts

## Running each component

**Lead-receiving webhook** (default port 8000):
```bash
venv\Scripts\python.exe -m uvicorn erp.webhook_app:app --host 127.0.0.1 --port 8000
```
Quick test:
```bash
curl -X POST http://127.0.0.1:8000/webhooks/leads -H "Content-Type: application/json" -d "{\"full_name\": \"Test\", \"email\": \"test@example.com\", \"company\": \"ABC\", \"source\": \"landing_page\"}"
```

**Sync ClickUp comments back to the DB** (scheduled, e.g. every 15 minutes via Task Scheduler):
```bash
venv\Scripts\python.exe -m erp.clickup_pull
```

**Weekly AI report** (scheduled, e.g. every Monday morning):
```bash
venv\Scripts\python.exe -m erp.weekly_report
```

**Sync staff FROM ClickUp INTO the Database** (run when a new member joins the workspace):
```bash
venv\Scripts\python.exe -m erp.sync_employees --department SALES
```
Matched by email: staff already in the DB -> `clickup_user_id` is updated; not found -> created
new with the default role `sales`. Re-run this whenever someone new is invited to the ClickUp workspace.

## Marketing / channel interaction data (Phases 1 + 3 + 4 - schema, pipeline, rollup, Channels report page - plus the real-file work)

A general-purpose, scale-ready place for marketing/channel data (clicks, reactions, sessions, conversions -
Facebook-Ads-scale or bigger), additive to the tables above: a star schema (`interaction_fact`, monthly-partitioned,
plus small `marketing_source` / `channel` / `campaign` / `creative` / `identity` dimensions and an append-only
`marketing_landing` raw layer) and a shared Python pipeline (`erp/marketing/`) that any source-specific connector
plugs into. See **`docs/marketing-data-architecture.md`** for the full design, the partition/index reasoning and the
real scale-verification numbers. The ERP Desk **Channels** page (`Ctrl+7`, Phase 3, described under ERP Desk below) reads only the two rollup tables; there is
no real external API connector yet (Phase 2 - needs the owner's credentials); today's one connector is
a generic flat-file/CSV reader plus two strict connectors for the owner's real exports (see "The owner's two real export files" below), so nothing runs automatically (the Data Flow map shows it as **manual**, honestly).

Install the tables (additive-only, safe to re-run; then `10_marketing_currency.sql` once - see below):
```bash
& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U erp_app -h 127.0.0.1 -p 5432 -d erp_support -f db\sql\07_marketing_schema.sql
& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U erp_app -h 127.0.0.1 -p 5432 -d erp_support -f db\sql\08_leads_indexes.sql
```

Load a CSV / channel export (any columns - the generic connector recognises what it can and keeps the rest in `attrs`; `--currency VND` sets its currency, default USD):
```bash
venv\Scripts\python.exe -m erp.marketing.ingest --csv path\to\export.csv --source flat_file
venv\Scripts\python.exe -m erp.marketing.ingest --csv path\to\export.csv --dry-run   # validate only, writes nothing
```

Run status: `ok`, `partial` (some rows loaded, a few rejected at the SQL stage) or `failed` (nothing loaded, or aborted). A systematic SQL failure (missing privilege/partition, schema mismatch) aborts the run early; the budget is `MARKETING_MAX_ISOLATED_ROWS` in `.env` (default 50).

### Phase 4 - the daily rollup and partition maintenance

A report page must never aggregate the raw fact (an unpruned full-table `GROUP BY` cost 3.36 s at 3M rows and grows with
history). `interaction_daily_rollup` (per day / channel / campaign) and `interaction_daily_channel_rollup` (per day /
channel) hold additive counts and sums only; every ratio (CTR, CPC, CVR ...) is computed at read time. The refresh job
recomputes only the last few days plus any older day whose facts changed, so it costs what the *changed days* cost, not
what the history costs. Measured numbers and the design reasoning: `docs/marketing-data-architecture.md` section 8.

Install the two rollup tables (additive-only, safe to re-run; needs 07 and 10 first - the refresh groups by the fact's currency):
```bash
& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U erp_app -h 127.0.0.1 -p 5432 -d erp_support -f db\sql\09_marketing_rollup.sql
```

Run it (or double-click / schedule `run_marketing_rollup.bat`; arguments are passed through):
```bash
venv\Scripts\python.exe -m erp.marketing.rollup --full --yes   # first build: every month that has facts
venv\Scripts\python.exe -m erp.marketing.rollup                # routine: last 3 days + any older day that changed
venv\Scripts\python.exe -m erp.marketing.rollup --days 7
venv\Scripts\python.exe -m erp.marketing.rollup --partitions-only
```
One run = (1) create this month + the next `MARKETING_PARTITIONS_AHEAD` (3) months of `interaction_fact` partitions so a
scheduled load never hits "no partition found", (2) refresh the rollup (idempotent; safe twice in a row and on an empty
fact), (3) **report** the partitions older than `MARKETING_RETENTION_MONTHS` (24) with the exact detach/drop commands -
it never runs them, that is the owner's decision. Log: `marketing_rollup.log`; exit code 0 ok, 1 a step failed, 2 bad
arguments. **Nothing schedules it today** (Task Scheduler is a system setting only the owner changes); the Data Flow map
shows it as "recommended, not set up" until a task whose command contains `run_marketing_rollup.bat` exists.
`erp/marketing/rollup_check.py --yes` is the one-off scale verification in a throwaway `perf_rollup` schema (not a gate).

### Placements (display-network placement report)

Where display ads ran (a site, a marketplace app or a banner slot) and what each place cost and produced. Optional, additive, all wording generic.

```
venv\Scripts\python.exe -m erp.marketing.sample_placements --rows 5000                 # a SYNTHETIC file in data_inbox\ (loads nothing)
venv\Scripts\python.exe -m erp.marketing.ingest --connector placement_performance --csv <file> --currency EUR --dry-run
venv\Scripts\python.exe -m erp.marketing.rollup                                        # refreshes the placement rollup too
```

Install once as the database owner: `db/sql/11_marketing_placements.sql` (one new table; without it the Channels page's **Placements** panel says so and
nothing else changes). The inbox processor detects the placement header by itself (a `<channel>__<currency>__x.csv` name supplies a currency the Cost
cells lack). The Channels page (Ctrl+7) shows the sortable Placements table with the same filters, a Placements CSV and a Placements sheet in the Excel
report; Insights gains five explainable placement rules (exclude, scale, low viewability, concentration, and a labelled reallocation ESTIMATE) - suggestions
only, nothing is changed for you. Blank means unknown, never 0; money stays per currency. Tests: `venv\Scripts\python.exe -B -m tests.placement_scenarios`.
Design and measurements: `docs/marketing-data-architecture.md` section 11.

### Marketing inbox processor (`python -m erp.marketing.autorun`)

For an analyst who receives marketing exports: drop the CSV files in **`data_inbox/incoming/`** (git-ignored; change it with
`--inbox` or `MARKETING_INBOX` in `.env`) and run **`run_marketing_autorun.bat`** (or `python -m erp.marketing.autorun`). It is a
one-shot job, not a service, and a personal reporting toolkit - the company's own systems stay as they are.

```
venv\Scripts\python.exe -m erp.marketing.autorun --dry-run    # detect + validate + report; moves nothing, writes nothing
venv\Scripts\python.exe -m erp.marketing.autorun              # the real run
venv\Scripts\python.exe -m erp.marketing.autorun --inbox D:\exports\marketing --schema public
```

For every `*.csv` (a file modified in the last 5 seconds is skipped as "still being written"; other file types are ignored):
1. **Detect the connector** from the header with the connectors' own strict checks - `ad_performance`, then `email_campaign`. The generic
   `flat_file` is used **only** when the file names its channel: a filename `<channel>__<currency>__whatever.csv`
   (`facebook__usd__march.csv`) or a `channel` column. Anything else is **not guessed**: it goes to `failed/` with a
   `<name>.reason.txt` saying exactly what was missing.
2. **Load** it through the existing ingest pipeline (nothing is parsed or validated twice); its status (`ok` / `partial` / `failed`) and
   failure budget decide the outcome. A file where every row is rejected counts as failed.
3. **Move** it to `processed/` (ok, partial, or an exact duplicate) or `failed/` - folders next to the inbox, created on demand, files
   renamed with a timestamp prefix so a same-named file never overwrites another. The original bytes are only renamed, never rewritten or deleted.
4. After all files, if at least one loaded rows, run the incremental **rollup refresh once**.
5. Print + log a summary table (file, connector, read / loaded / rejected, status, destination). Exit code 0 unless a file, the database
   or the rollup failed (1); 2 for a bad argument. Log: `data_inbox/marketing_autorun.log`.

**Idempotent by content:** the SHA-256 of every file that loaded is kept per schema in `<inbox>/.autorun_index.json` (the run journal has
no hash column and a migration was not worth it); the same bytes dropped again are moved to `processed/` as a duplicate and not loaded
again. Delete that file to force a re-load (the load is an idempotent upsert anyway). If the database is down the run stops and every
file stays in the inbox to be retried. One run at a time per inbox (a `.autorun.lock` file). **Nothing schedules it**: the Data Flow map
shows it as "recommended, not set up"; registering `run_marketing_autorun.bat` in Task Scheduler is the owner's decision.
Tests: `venv\Scripts\python.exe -B -m tests.marketing_autorun_scenarios` (139 scenarios; the end-to-end part uses a throwaway
`perf_autorun_*` schema and copies of `data1.csv` / `data2.csv`, never `public`). Design notes: `docs/marketing-data-architecture.md` section 10.

### The owner's two real export files (currency, duplicates, weekly grain, two strict connectors)

Two real exports drove a round of work (design, decisions and the numbers verified on them:
`docs/marketing-data-architecture.md` section 9). They live in the git-ignored `data_inbox/` (`data1.csv` = 118 rows of paid-social ad
performance, weekly, money like `475,401 ₫`; `data2.csv` = 128 rows of e-mail campaign metrics, daily, EUR) and are never committed.

- **Money keeps its currency and is never converted.** New additive migration **`db/sql/10_marketing_currency.sql`** adds
  `interaction_fact.currency` (`CHAR(3) NOT NULL DEFAULT 'USD'`, idempotent, no table rewrite; the owner runs it - this project never
  runs it on the real database). Order: **07 (installed) -> 10 -> 09**. The rollup tables carry `currency` and `grain` (`day` | `week`)
  in their key. The **Channels page** shows money in the currency it was reported in, never adds across currencies (several in view =
  one line per currency, no total; the CSV says `MULTIPLE` and leaves the amounts blank), has a **currency filter**, a **Revenue**
  tile and column (derived revenue is labelled *derived*), and draws a weekly source in its **own per-week chart** (a week is never
  shown as one day's activity).
- **Duplicate natural keys are kept, not overwritten.** `data1` has 6 pairs of rows with the same (week, campaign, adset, ad, device)
  and different metrics. `ad_performance` numbers such rows (occurrence 0, 1, ...) so both load, and every run **reports** natural-key
  collisions in its summary. Re-importing the same file is idempotent; the trade-off is that the numbering depends on the export's row
  order staying stable (docs section 9.2).
- **Two strict connectors** next to the generic one: `python -m erp.marketing.ingest --connector ad_performance|email_campaign --csv <file>
  [--channel X] [--schema S] [--dry-run] [--date-format mdy|dmy]`. They accept only their exact header (no fuzzy column guessing),
  refuse to guess `M/D/YYYY` vs `D/M/YYYY` (`6/16/2024` proves month-first; an ambiguous file needs `--date-format`), parse `475,401 ₫`
  with `Decimal` (no float), and reject rows that break their sanity rules (`email_campaign`: `unique_opens <= delivered`,
  `unique_clicks <= unique_opens`). The channel is a parameter (`paid_social` / `email` by default).

```bash
venv\Scripts\python.exe -m erp.marketing.ingest --connector ad_performance --csv data_inbox\data1.csv --dry-run   # what would load, what is rejected, spend per currency, key collisions
venv\Scripts\python.exe -m erp.marketing.ingest --connector email_campaign --csv data_inbox\data2.csv --dry-run
& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U erp_app -h 127.0.0.1 -p 5432 -d erp_support -f db\sql\10_marketing_currency.sql   # once, before loading or building the rollup
```

Verified on the real files in a **throwaway** schema (never the real tables): `venv\Scripts\python.exe -u -m erp.marketing.sample_check --yes
--data1 data_inbox\data1.csv --data2 data_inbox\data2.csv` installs 07 + 10 + 09 into `perf_samples`, loads both files through the real
command line, proves row counts, the sum of every measure, spend per currency and the 6 duplicate pairs against the source files, checks
the rollup against a direct `GROUP BY` per currency, imports both files a second time (0 new rows), and drops the schema (it also
compares the schemas and the `public` tables before and after).

## Connecting Power BI

A dedicated read-only role (`powerbi_reader`) exists for Power BI — it can only
`SELECT` from `v_tickets_summary` and `v_leads_summary`, nothing else (no raw
tables, no write access). Create/refresh it with:
```bash
& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U postgres -h 127.0.0.1 -p 5432 -d erp_support -v pw="'NEW_PASSWORD'" -f db/sql/06_powerbi_readonly.sql
```

In Power BI Desktop: **Get Data > PostgreSQL database** (requires the Npgsql
driver — Power BI prompts to install it on first use if missing), then:
- **Server:** `127.0.0.1:5432`
- **Database:** `erp_support`
- Choose **DirectQuery** (live data, no manual refresh needed) or **Import**
  (faster visuals, needs a scheduled refresh)
- Credentials: database, username `powerbi_reader`, password from `.env`
  (`POWERBI_DB_PASSWORD`)
- Select the two views (`v_tickets_summary`, `v_leads_summary`) as your tables

## Script Center - a control room for every script in this project

A single-page Streamlit dashboard for non-developers: what each script does, which
"skill" it belongs to, a visual mindmap of the whole codebase, an in-browser code
editor that saves straight back to disk, a one-click test runner with structured
logs, email alerts on failure, and the latest AI-generated daily digest.

```bash
run_script_center.bat
```
or
```bash
venv\Scripts\python.exe -m streamlit run dashboard/script_center.py --server.port 8502
```
Opens at http://localhost:8502. ERP Desk starts the same page on its own port
(47651) and embeds it as the **Management** tab, so the two can run side by side.
`run_script_center.bat` also applies this tool's own brand theme via `--theme.*`
CLI flags - deliberately not via `.streamlit/config.toml`, which every Streamlit
app started from this folder would pick up.

The page is one continuous scroll of five full-width "slide" sections - Overview ->
Mindmap -> Script catalog -> Editor & test runner -> Logs & digest - each with its
own background treatment, a numbered divider, and a scroll-reveal entrance animation
(`IntersectionObserver`), so it reads as one connected system instead of stacked
widgets:

- **Overview** - masthead, hero, KPI tiles, and a "today's read" insight banner.
- **Mindmap** - the visual centerpiece: an interactive `Python Scripts -> skill group
  -> script` graph (vis-network via CDN, no new dependency), color-coded by skill,
  hover for a script's purpose. **Click or hover a script node and it visually ties
  itself to that script's card in the catalog below** (matching color accent + a
  pulsing highlight ring + smooth-scroll) **and clicking actually opens it in the
  editor/test-runner section** - the mindmap, catalog and editor read as one
  connected system rather than three separate panels.
- **Script catalog** - a color-coded card grid (icon, skill, line-count/last-modified
  badges, purpose) instead of a plain data table, filterable by skill, with an "Open
  in editor" button on every card.
- **Editor & test runner** - pick a script (or arrive here from the mindmap/catalog),
  edit its source in a text area, save it back to disk (writes are restricted to
  files inside the project root - no path traversal), and test-run it: one-click
  execution for the safe one-shot scripts (with clear warnings before anything that
  writes to the real database or calls a real external webhook), a syntax-only check
  for the long-running servers (`erp/webhook_app.py`, `dashboard/script_center.py`),
  and a syntax check for everything else.
- **Logs & daily digest** - every save/test/syntax-check is appended to
  `script_center.log` (one JSON object per line) and shown as a visual activity feed
  (status-colored icon per entry, grouped by "Today"/"Earlier") instead of raw text
  rows; check "Email me if this fails" before running a test to get an email if it
  fails (uses `erp/emailer.py`, stdlib `smtplib` only). Below that, the latest
  AI-generated daily digest renders as a report card (KPI tiles with day-over-day
  delta arrows + the AI narrative) instead of a dumped JSON blob. `erp/daily_digest.py`
  reads today's key metrics (total leads, SLA breaches, ClickUp sync issues, etc.),
  compares them to yesterday (`daily_digest_history.json`), asks the AI agent for a
  short status/opportunities/risks/actions narrative, emails it, and writes
  `daily_digest_latest.json`, which this panel displays. Schedule
  `run_daily_digest.bat` with Windows Task Scheduler to run every morning.

To enable email alerts and the daily digest email, fill in `SMTP_HOST`, `SMTP_PORT`,
`SMTP_USER`, `SMTP_PASSWORD`, `ALERT_EMAIL_FROM`, `ALERT_EMAIL_TO` in `.env`
(see `.env.example`). Leave `SMTP_HOST` blank to keep everything else working
with email sending silently skipped (same fallback philosophy as ClickUp/DeepSeek).

## ERP Desk - the desktop app (Today + live Reporting + Management + Data Flow + Leads in one window)

A web app that runs locally and looks like a native Windows app: **no Electron, no .exe**.
The launcher starts everything, waits until it is healthy, then opens Microsoft Edge (Chrome
if Edge is missing) in `--app` mode - no tabs, no address bar - with its own profile
(`.erp_desktop/browser_profile`), so it gets its own window, taskbar entry and icon.

- **Today** (the page ERP Desk opens on; `Ctrl+1`) - "today at a glance" for people who only want the answer,
  in plain English, about one screen on a full-HD monitor (it scrolls on a smaller one). Everything comes from real sources
  (`desktop/today_data.py`, read-only SQL plus the Data Flow feed; no number is typed in):
  1. **One status sentence**, e.g. *All good - 3 new leads today, none overdue* or *Needs attention - 2 leads past their
     5-business-hour SLA* (green / coral / amber / grey). Rules, first match wins: database unreadable -> "Can't read
     today's numbers"; any lead or ticket past its SLA -> "Needs attention"; a lead/ticket number unreadable -> "Partly
     unavailable"; a system check is red -> "Mostly fine - nothing is overdue, but ..."; an input is blind (next
     paragraph) -> amber "No problems found, but the numbers may be incomplete (3 new leads today, none overdue)";
     otherwise "All good".
     **Honesty sub-line.** New leads only arrive while the lead webhook is running, and a rep's reply only reaches the
     database when the ClickUp comment pull (`erp.clickup_pull`) is scheduled and ClickUp is connected. When either is
     not the case (or the health checks have not finished yet) the page never says a bare "All good": an amber card under
     the headline says, for example, *Numbers may be incomplete: new leads only arrive while the webhook is running (it is
     offline) and replies are only read from ClickUp when the pull job is scheduled (it is not).* The same card appears
     under "Needs attention" (with a reminder that the listed leads may already have been answered in ClickUp); the
     "Needs attention" wording and logic are unchanged. The verdict comes from the very health cells of the strip below
     (`status.caveat` / `status.blind` in `/api/today`), never from a second probe.
  2. **Since yesterday** - one plain line under the headline, e.g. *Since yesterday: 2 new leads, 1 reply came in from
     ClickUp, nothing new went past its SLA*, with ticket movement added when there are tickets on file. It is computed
     server-side from the same read-only queries, the same day boundary and the same database time zone (named in the
     tooltip) as the tiles, so it can never tell a different story. It is honest rather than tidy: a clause whose source is
     blind (the ClickUp pull is not known to be running) or whose query failed is replaced by a short note - never by a
     zero - and when the database holds nothing from before today there is nothing to compare, so no line is shown at all.
     Each clause that has rows behind it is a button that opens the detail drawer on exactly those rows.
  3. **Six tiles** (big Montserrat numbers): new leads today, leads past SLA now, open tickets, tickets opened / resolved today,
     leads awaiting first reply, last job run (daily digest written or ClickUp comments pulled). Each has a 7-day
     sparkline; the snapshot tiles show the change since the end of yesterday. A tile whose number really has rows behind it
     shows a chevron and opens the drawer (item 4).
  4. **Click-through detail drawer** - clicking a tile, a "Needs a human" row or a "since yesterday" clause slides in a panel
     (the same shape as the Leads page's Definitions drawer). A tile opens **the list of what it counts**, worst first, at most
     12 rows, saying how many more there are; every row opens **one lead or ticket in full**: who it is (name, company and
     source only - e-mail, phone and the raw form payload are never sent to the browser), which rep owns it, when it arrived,
     the deadline and how late, the AI summary from `lead_ai_analysis` if there is one, the ClickUp task, its state and a link
     to it when it is really synced, and the newest note pulled from ClickUp (`lead_updates`) with its author and which stamp
     the time is. Each of those says plainly when it is absent ("No ClickUp comment has been pulled for this lead yet - which
     is exactly what the database knows, not proof that nobody called"). The "Leads awaiting first reply" list also offers the
     other half of the question - the leads that already have a reply. Escape closes it, focus is trapped inside it and
     returns to whatever opened it, and everything it shows travels in the **same** `/api/today` payload, so opening it reads
     nothing extra and the page still has exactly one poller.
  5. **Needs a human** - the worst items first: leads past their SLA (rep and how late) and tickets past their SLA or
     open more than 24 h (assignee, how late / how old); a friendly empty state when there is nothing to chase. Each row
     opens its own detail panel.
  6. **System health** - database, lead webhook, ClickUp, DeepSeek, email, scheduled jobs, each green / amber / grey
     (red only for a real failure) with the reason in words. It reuses the Data Flow feed, so it never says something
     runs unless its trigger is armed (webhook answering, a real Windows scheduled task exists ...). The **Health page**
     (`Ctrl+6`) is the detail view of this very strip - the same cells, in full, with what depends on each one and a fix
     hint to copy.
  7. Buttons **Open Reporting** and **See Data Flow**, and a live pill with a Refresh button (`R`). It re-reads every 15 s
     while open. The "How this is decided" fold at the bottom lists the definitions.

  Definitions worth knowing: a lead is **past SLA** when its deadline (`LEAD_SLA_HOURS` business hours, stored in
  `leads.sla_due_at`) has passed and **no reply is on record** (a ClickUp comment pulled into
  `lead_updates`); a lead answered late is no longer listed. Because replies only reach the database through
  `erp.clickup_pull`, an unscheduled pull job makes "awaiting first reply" look worse than reality; the tile says so.
  The English for those five SLA outcomes lives in exactly one module, `desktop/sla_words.py`, which **both** the Today page
  and the Leads page import, so the two pages can never describe the same state with different words (they already shared the
  SQL) - including the compact form used by a filter pill, a table cell or a chip (`sla_words.SHORT`, carried to the browser
  on every payload's `options.statuses[].short` and read by both `static/leads.js` and `static/sources.js` instead of each
  keeping its own copy, audit fix 2026-09-24). "Day" is the database's calendar day (midnight to now) in the database session's time zone, which the page names. A source that cannot be read (table missing, database down)
  turns only its own tile / list / cell into "unavailable"; the page keeps working. The JSON is `GET /api/today`
  (read-only; `?fresh=1` only skips a 5 s cache).

  **It cannot hang.** Every Today query is cut off after 4 s (`SET LOCAL statement_timeout`, plus a 2 s lock timeout,
  set only for these read-only queries) and all of them together after 5 s, so a stalled database gives "unavailable"
  tiles, not a frozen page. A brand-new connection that accepts the TCP handshake but never answers is abandoned after
  `DB_CONNECT_TIMEOUT` seconds (default 5; set in `erp/db.py`, so it applies to every script that opens a connection but
  changes nothing about queries or writes). An **already-pooled connection that then freezes mid-life** (lesson L-091,
  closed 2026-09-24) is a separate gap plain `connect_timeout` cannot cover, since `pool_pre_ping`'s own liveness probe
  runs before any statement timeout exists to bound it: Today, Leads, Sources & cohorts and Reporting all read through
  `erp.db.read_engine` (libpq TCP keepalives + `tcp_user_timeout`, for a peer whose OS has genuinely stopped answering)
  and `erp.db.read_connect()` (a wall-clock checkout bound, 8 s, for a peer whose OS is still alive but its application
  never answers - the case keepalives alone cannot see; proven with a TCP relay that forwards one good query then goes
  silent, `tests/today_scenarios.py`'s `_FrozenProxy`). Writes and the pipeline scripts are unchanged. In the browser
  the request is aborted after 8 s and the pill turns **STALE** ("as of hh:mm:ss" in the status line) while the last
  numbers stay on screen; only one request is ever in flight, and a page that never got any data says "taking too
  long" instead of spinning. The text under "How this is decided" is
  generated from `LEAD_SLA_HOURS`, `erp/business_hours.py` and the constants in `desktop/today_data.py`, so it follows
  your configuration. All of this is covered by `tests/today_scenarios.py` (run alone with
  `venv\Scripts\python.exe -B -m tests.today_scenarios`, and as part of the smoke gate).
- **Reporting** (`Ctrl+2`) - the live pipeline dashboard (`desktop/report_data.py`, which owns the report
  queries and the headline sentence): animated KPI tiles,
  funnel, source donut, per-rep dot-grid, leads-over-time, score bands, SLA-by-rep, tickets,
  a work-in-progress timeline and a ribbon of the latest leads. All panels are **linked**: click a
  rep, source, day, score band, funnel stage or SLA bar anywhere (or use the filter bar) and every
  other panel re-filters and highlights. The page refreshes itself (a server-side refresher
  re-queries PostgreSQL every `DESKTOP_REFRESH_SECONDS`, default 10 s; the page checks for changes
  every 5/10/30/60 s or paused). It shows a **LIVE** pill with "synced Ns ago", a manual Refresh
  button (also the `R` key) and a toast when data actually changed ("+1 new lead").
  **Today's briefing** turns `daily_digest_latest.json` (from `erp/daily_digest.py`) into cards:
  status signal, metric tiles with day-over-day deltas + sparklines, opportunities / risks /
  checkable suggested actions. With no digest yet it shows a placeholder; the **Generate today's
  digest** button runs `python -m erp.daily_digest --no-email` in the background, and (unless
  `DESKTOP_AUTO_DIGEST=0`) the app does it once a day by itself while it is open. In-app runs
  make one AI call (aggregate counts only, no customer data) but **never send email** - neither
  the digest nor a failure alert. The emailed digest stays the job of `run_daily_digest.bat` /
  Task Scheduler (which still emails as before), so opening the app can't cause a duplicate.
- **Leads** (`Ctrl+5`, or `#leads`) - the analyst's page, in two tabs: a **lead funnel + SLA explorer** with real filters and a CSV export
  (`desktop/leads_data.py`, `static/leads.{js,css}`; read-only SQL on `v_leads_summary`, `lead_updates`, `lead_ai_analysis`).
  It is its own navigation item and not a tab inside Reporting because the two answer different questions with a different
  mechanism: Reporting ships one snapshot to the browser and cross-filters it there by clicking (great for looking around,
  no server-side filter, no export); Leads filters **on the server**, so one filter set drives every chart, the paged table
  and the file you download. What it adds beyond Reporting: stage-to-stage conversion, the SLA outcome split into
  answered on time / answered late / still waiting past the deadline / inside the deadline, a time-to-first-reply
  histogram, a sortable paged table and `Download CSV`.
  1. **Filters** (sticky bar): date range (presets or from/to), sales rep, source, SLA status, ClickUp sync status. Several
     values of one filter combine with OR, different filters with AND. Clicking a segment, a breakdown row, a table cell or a
     day bar sets the same filters. Every value is whitelisted on the server (rep and source against what is in the database
     right now, status / sync / sort against fixed lists, dates as ISO days) and bound as a SQL parameter, never formatted
     into the SQL text. An invalid value is a `400` that names it - never silently ignored, so an export can not quietly
     hand you the wrong rows. The SALES REP and SOURCE groups have no fixed size (a busy team can have a dozen of each), so
     each sits in its own one-row, horizontally-scrolling strip (`.fg-scroll`) instead of wrapping the whole bar to two or
     three rows - the label stays put, only the pill strip scrolls, and every pill is still one scroll + one click away at
     any window width. Above ~1100px of content width each strip claims a full row of its own, so most or all pills are
     visible without scrolling (audit fix, 2026-09-24 - the earlier version stayed compact even with room to spare and hid
     most of a dozen-pill list); below that it stays compact. A soft edge fade (not a hard clip) shows which side, if any,
     still has more pills, and scrolling a strip - or the focused pill inside it - survives every filter change instead of
     snapping back to the start (verified at 1920/1440/820/390 with a synthetic 8-rep/12-source set, never written to the
     real database). The breakpoint, fade-visibility and scroll-preservation math is pure logic extracted into
     `desktop/static/leads_filterbar.js` (no DOM), which `leads.js` and `sources.js` both call at runtime and which
     `tests/leads_scenarios.py` runs for real under Node (audit fix, 2026-09-24 - this had no scenario-table coverage before;
     falls back to a visible "skipped" row, not a silent pass, on a machine with no `node` on PATH). The bar also gives up
     its sticky position on a genuinely short window (`max-height: 640px`, down from 860px now that the bar itself is
     shorter) so it never eats most of the screen.
  2. **Five tiles**: leads in view, SLA on-time rate, past SLA now, awaiting first reply, median time to first reply.
  3. **Lead funnel** - the stages the pipeline really has, each proven by a row in a real table: received (`leads`), assigned to a
     rep (`lead_assignments`), analysed by AI (`lead_ai_analysis`), ClickUp task on record (`lead_clickup_sync` synced/mocked),
     reply on record (`lead_updates`). There is no lead-status column in the schema, so nothing is invented; the ClickUp sync
     status is shown under the funnel. Strict: a lead counts at a stage only if it also has every earlier stage, so a
     conversion can never exceed 100%; leads with later-stage evidence but a missing earlier stage are counted in a visible
     warning, not hidden.
  4. **SLA outcome** and **time to first reply** (histogram, median with its n, fastest / slowest, 90th percentile only from
     `P90_MIN_N` replies). "Past SLA" is the Today page's own SQL (`lead_past_sla_sql` in `desktop/today_data.py`, reused, not
     re-implemented), so with no filter set "Past SLA now" here equals the Today tile (the smoke gate asserts it).
  5. **By sales rep** and **by source** (outcome bars, on-time rate, median reply), **Leads per day** (zero days included,
     red = ended up breached), and a **detail table** (click a heading to sort; 10 / 25 / 50 / 100 per page; the count under
     it is the true total; one card per lead on a narrow window).
  6. **Download CSV** = `GET /api/leads/export.csv` with the same filters and sort (at most 50,000 rows, a header says when it
     was cut). UTF-8 with a byte-order mark so Excel reads accents; text cells starting with `=`, `+`, `-`, `@`, `;`, tab or CR
     get a leading apostrophe (CSV injection); no e-mail, phone, AI notes, raw payload or credential column is selected.
  7. **Definitions** drawer: every metric in plain words, generated by the server from the same constants and settings the
     numbers use (`LEAD_SLA_HOURS`, business hours, `LOW_N`, page sizes, export limit), so the text follows your configuration.
  Small numbers stay honest: a percentage is shown only when at least `LOW_N` (5) leads stand behind it, otherwise the page says
  "2 of 3"; nothing divides by zero; empty views say what to loosen. The same amber honesty note as the Today page appears
  when the webhook is offline or the ClickUp comment pull is not scheduled (replies then exist that this page cannot see).
  Each panel is read independently: a query that fails turns only its own panel into "unavailable". Every statement is cut off
  after 4 s and all of one page together after 6 s (`SET LOCAL statement_timeout` re-armed before every statement, read-only
  session, no thread); the browser aborts after 20 s, keeps the last numbers on screen dimmed and labelled with their age.
  The JSON is `GET /api/leads/analysis` (read-only; `?fresh=1` only skips a 3 s cache). Tests: `venv\Scripts\python.exe -B -m
  tests.leads_scenarios` (also run by the smoke gate; the SQL scenarios use temporary tables in one private database session and
  write nothing to a real table).
- **Leads -> "Sources & cohorts"** (the second tab of the same `Ctrl+5` page) - **where leads come from, and how each arrival
  behaves over its first days** (`desktop/sources_data.py`, `static/sources.{js,css}`). It is a tab of the Leads page rather
  than a navigation item of its own because it answers a question about *the same leads under the same filters*: the filter bar,
  the Definitions drawer, the live pill and the CSV button are shared, and only the tab on screen polls, so the page still has
  exactly one poller. The "By source" panel on the first tab links straight into it.
  1. **Source table** - one row per `leads.source` (a lead with none is `(none)`): leads and share of the view, how many
     **reached each pipeline stage** (assigned / AI analysed / ClickUp / replied, the *same strict funnel* as the first tab),
     the five SLA outcomes as a bar, on-time and past-SLA counts, the **median time to first reply with its n**, and the
     **rep spread** (which reps those leads went to). Click a heading to sort (server-side, from a fixed list, so the CSV is
     exactly the table); click a source name to filter every panel by it. One card per source on a narrow window.
  2. **Cohort curves** - leads grouped by the **day / week / month they arrived** and followed day by day **since each lead's
     own arrival**: cumulative *replied*, *reached ClickUp* or *past SLA* (you pick the metric). Hovering a curve gives the
     cohort, its n and the exact figures; **clicking a point narrows the table below to that cohort** (the chart deliberately
     keeps every cohort, so there is still something to compare against - the line under the chart says so). The bucket is
     chosen for you as the finest one that produces at most 12 curves, and the page prints **which and why**; you can override
     it. A cohort is followed only to the age its youngest lead has reached, so the denominator never shrinks along a curve.
  3. **Honest with today's data.** The database currently holds one single arrival cohort, so the page says so in one calm
     line and draws the figures as points; a cohort with only one age point is **never** joined into a line. A percentage
     appears only with at least `LOW_N` (5) leads behind it - otherwise you get "1 of 2" - and the *whole* chart falls back to
     plain counts as soon as any cohort drawn is smaller than that. A reply time-stamped before its lead arrived counts from
     day 0 and is reported as a data anomaly rather than clamped away. Every day boundary is the **database session's**
     calendar day; the zone is named in the footer, in the Definitions drawer and as a column in both CSV files (L-097).
  4. **Two CSV files**: `Download CSV` gives the source table exactly as shown; `Cohort CSV` gives one row per cohort and day
     since arrival. Both are `GET /api/leads/sources.csv` (`part=sources|cohorts`), UTF-8 with a byte-order mark, CRLF,
     formula-safe. They hold **aggregate rows only**, so no lead id, name, e-mail, phone, AI note or raw payload can appear.
  5. **Every parameter name and value is whitelisted on the server** - including the three this tab adds (`bucket`, `metric`,
     `cohort`) - and anything unknown is a `400` that names the parameter, on the JSON endpoint *and* on the CSV. The same
     guard was added to `GET /api/leads/analysis`, which used to accept an unknown name silently (lesson L-098).
  The JSON is `GET /api/leads/sources` (read-only, 3 s cache, same timeouts and per-panel degradation as the first tab).
  Tests: `venv\Scripts\python.exe -B -m tests.sources_scenarios` (also run by the smoke gate). Its SQL scenarios build
  **four** arrival cohorts in temporary tables inside one private session, so the multi-cohort behaviour is exercised even
  while the real database holds one; nothing is ever written to a real table.
- **Health** (`Ctrl+6`, or `#health`) - the one place that answers *"is this system actually working, and if not what do I
  do about it?"*. It is the **detail view of the Today page's health strip**, not a second opinion: every verdict comes from
  `desktop/today_data.build_health()` and the Data Flow snapshot (`desktop/flow_data.py`), which is where the
  "is the trigger really armed?" logic lives and where it stays. The strip is the summary, this page is the detail, and the
  smoke gate asserts the two never disagree about the same dependency.
  1. **One honest headline** - *Everything that should be running is running*, or *N things need attention* (and *Still
     checking* until the first pass of system checks has finished). Beside it, in the same hero, the Today headline, because
     the two answer different questions: this page asks *is the machinery running?*, Today asks *is the work on time?*. A
     system can be perfectly healthy while leads are past their deadline, and hiding one behind the other would be dishonest.
     The reverse happens too, and is equally legitimate: this page counts **every** recommended job Windows Task Scheduler
     does not have, while Today only looks at the three doors that decide whether a number can exist (webhook, ClickUp, the
     comment pull) - so Today can say *All good* while this page asks you to register the weekly report. The one rule that
     always holds, and the one the smoke gate asserts, is the other way round: if this page says all clear, Today may not be
     reporting a blind input.
  2. **One card per dependency** - PostgreSQL, the lead webhook listener, ClickUp, DeepSeek, SMTP/email, **each scheduled job
     on its own** (the ClickUp comment pull, the emailed daily digest, the weekly AI report - plus any other task this machine
     really has registered for the project), ERP Desk's own loops (live report refresher, the system-check feed itself, the
     once-a-day auto digest) and the Script Center. Each card carries: the colour (green working / amber check it / red broken
     / grey deliberately off or not checkable) and the Data Flow word for *why* (healthy, stale, error, never-run, mocked,
     offline, idle, unavailable); one plain-English sentence; **when it was last checked** (when the check behind that card
     really ran - the system-check feed's `refreshed_at`, not the last time its verdict changed) **and when it last
     succeeded**; **what depends on it** in real consequences ("web-form leads cannot arrive", "no rep's reply can ever be read back") - headed
     *what this is costing you right now* on a card that is already amber/red/grey and *if this stops* only on a green one;
     and a **fix hint you can copy** - the exact command (`run_webhook.bat`, `venv\Scripts\python.exe -m erp.clickup_pull`),
     the exact setting to fill in (`CLICKUP_API_TOKEN` in `.env`), the exact place to click, or an honest "register it in
     Windows Task Scheduler (needs the owner)". A row of chips above the cards jumps to any of them.
  3. **What this means for the numbers** - which figures are incomplete *right now* because of the amber and red cards, page
     by page. It reuses the very blind-inputs logic behind the Today page's "Numbers may be incomplete" caveat, so the two
     can never name different blind spots.
  4. **Configuration truth** - which integrations are configured / partly configured / mocked / not set up, and each setting
     as **`set` or `not set`**, read through `erp/config.py`. No value, no part of a value, not even its length ever leaves the
     server; the smoke gate proves it against the machine's real `.env` and the scenario table proves it again with sentinel
     values.

  **The page never runs anything and never changes a setting** - it says so in the hero, and it means it: every hint is text
  to copy, not a button that acts, and the page calls no state-changing endpoint. It opens **no database connection of its own,
  starts no thread and adds no poller**: `GET /api/health` (read-only; `?fresh=1` only skips a 5 s cache; any other parameter
  NAME is a `400` that names it, checked before the cache, lesson L-098) composes two snapshots that already exist - the Data
  Flow snapshot and the Today payload - with the same timeout budget as the Today feed, and a request never waits more than 6 s
  behind another request's build. The Today payload's own on-demand build is shared with the Today page, so reading it here can
  make THAT store open a fresh connection once its few-second cache has expired - exactly as opening the Today page would - but
  the Health feed itself never opens a second one. Every card degrades on its own, so the page is **still useful when
  PostgreSQL is down**: the database card explains it in words with what to do, and every other card still renders. The
  PostgreSQL card's location (`erp_support @ host:port`) is read from `erp/config.py`, never hardcoded, so it cannot name the
  wrong database after a `.env` change. Tests: `venv\Scripts\python.exe -B -m tests.health_scenarios` (also run by the smoke
  gate, which additionally validates the live `/api/health` shape, its `400` guard and the no-secrets property).
- **Channels** (`Ctrl+7`, or `#channels`; `desktop/channels_data.py` + `static/channels.{js,css}`, read-only
  `GET /api/channels` and `GET /api/channels.csv`) - the **marketing channel-performance report** (Phase 3 of
  `docs/marketing-data-architecture.md`) for managers (one sentence and six tiles) and analysts (per-channel table,
  campaign drill-down, definitions, CSV). It reads **only the two rollup tables**
  (`interaction_daily_channel_rollup` = day x channel, and `interaction_daily_rollup` = day x channel x campaign, only for
  the drill-down or a campaign filter) plus three tiny lookups (`marketing_channel`, `marketing_campaign`,
  `marketing_rollup_run`): never `interaction_fact`, never `marketing_landing`, so it costs the same at any raw volume
  (proved with `EXPLAIN` on a 400,000-fact throwaway schema; every number was also compared with a direct `GROUP BY` of the
  facts). The schema is `MARKETING_SCHEMA`, every identifier goes through `erp.marketing.schema.check_identifier`, every value
  is a bound parameter, the session is read-only `REPEATABLE READ` (tiles, chart, table and CSV describe the same rows even
  while a refresh commits).
  **What you see when the owner's database is as it is today** (the rollup tables are NOT installed, `interaction_fact`
  is empty): a **"not installed" setup card** with the exact owner-run `psql` commands for `db/sql/10_marketing_currency.sql` and
  `db/sql/09_marketing_rollup.sql` in that order (and `07` first if those are missing), then the load and rollup commands - no tile, no chart, no number, no zero. Installed
  but empty is a **"no data loaded yet"** card. With data: (1) a server-written headline ("399,997 sessions from 1 Sep to 30 Sep
  2026, up 3.3% on the previous 30 days. Channel 4 brought 8.3% of them.") with the date range and the **database session
  time zone** printed next to it (L-097); (2) six tiles - sessions, clicks, conversions, conversion rate, spend, revenue - each with a
  previous-period change and a sparkline, each degrading on its own; (3) a per-day Plotly chart by channel from the channel
  rollup (a day with no rollup row is a gap, never a zero) and, for a weekly source, its own per-week chart (a week is never drawn as one day);
  (4) a per-channel table with share bar, CTR, conversion rate, spend, revenue
  and cost per conversion, **sortable on the server** from a fixed vocabulary, with a **campaign drill-down** (click a channel;
  at most 25 campaigns, then an honest "N more" note); (5) filters - range, channels, campaign, currency - applied the same way to tiles,
  chart, table and CSV; (6) three **CSV** downloads (channels, daily, campaigns of the selected channel: UTF-8 with BOM,
  CRLF, formula-safe cells, aggregate rows only - no identity or personal column exists in the rollup); (7) a **Definitions**
  drawer generated from the same constants as the code.
  **Honesty rules:** rates (CTR = clicks / impressions, conversion rate = conversions / clicks, cost per conversion) are
  computed at read time from the additive sums; a percentage needs a denominator of at least 5, below that the page shows
  counts ("2 of 3"); a zero denominator is "no clicks", never a division; spend appears only when the sum is above 0
  ("no spend field" otherwise, never 0.00); **money is shown in the currency it was reported in and never converted or added across
  currencies** (several currencies in view = one line per currency, no total; the CSV writes `MULTIPLE` and leaves the amounts blank;
  revenue that a connector derived is labelled *derived*); a change against the previous period is a percentage only when the previous
  number is at least 5. Query parameter NAMES and values are whitelisted (an unknown name is a `400` that names it, checked
  before the cache, also on the CSV). The page never runs, schedules or installs anything: the rollup job stays manual (or an
  entry the owner adds to Task Scheduler) and the page shows whatever the last run left, with "rollup refreshed ..." and a
  warning when that is more than 2 days ago. One poller (30 s, only while the page is open), a 3 s server cache, at most 3
  concurrent builds, every query inside the statement timeout and a 6 s total budget. Tests:
  `venv\Scripts\python.exe -B -m tests.channels_scenarios` (whitelists incl. the CSV, sort vocabulary, rate maths with zero
  denominators, low-n, page-state selection, CSV escaping, no PII columns, the no-cross-currency and weekly-grain rules, the
  currency filter's whitelist, and SQL scenarios against the real 07 + 10 + 09 DDL in a temporary schema, including two
  currencies and a weekly source); the smoke gate runs them and requests `/api/channels` in whatever state the real database is in.
- **Channels Insights + Excel report** (slice 2 of the marketing automation flow; on the Channels page, `desktop/insights_{rules,queries,data,xlsx}.py`
  + `static/insights.{js,css}`, read-only `GET /api/channels/insights` and `GET /api/channels/report.xlsx`, same filters and the same
  400-on-unknown-parameter rule as `/api/channels`). An **Insights** panel above the chart shows severity-sorted cards (critical / warning / info),
  each with the evidence and the exact comparison ("412 clicks >= 50 minimum"), a suggested action and a confidence ("enough data" / "thin data").
  Six fixed rules, no learning: spend without conversions, cost-per-conversion jump and conversion-rate drop against the previous equal window,
  stale channel / holes in the window, spend concentration in one campaign, and campaigns worth scaling. Every threshold is a named constant
  shown in the **How these are decided** drawer (generated from the same constants). Money is judged per currency and never mixed; a 0 that
  means "unknown" (a source with no conversion field) is a note, not a finding; below a rule's volume floor nothing is said and a small sample
  is only ever "thin data" (info). The panel says which window it analysed and offers **Analyse all data**; the default 30-day window may sit
  outside the data. An **automation ideas** strip turns recurring findings into plain-text suggestions (for example "daily stale-channel
  check"); it never schedules or runs anything. **Download Excel** (next to Download CSV) gives a workbook with Summary, Channels, Campaigns,
  Insights and Definitions sheets: numbers are real numeric cells with number formats (VND without decimals), a styled frozen header row,
  fitted columns, text that starts with `=`, `+`, `-`, `@` is prefixed with an apostrophe (no formulas from data), the file is streamed with the
  xlsx content type and refused above 12 MB. Nothing to launch: open ERP Desk, `Ctrl+7`. Tests:
  `venv\Scripts\python.exe -B -m tests.insights_scenarios` (each rule fires / stays quiet / thin, window maths, currency isolation, 400s,
  states, temporary-schema SQL, the xlsx opened with openpyxl); the smoke gate runs them. `openpyxl` was already in `requirements.txt`.
- **Management** - the existing Script Center (`dashboard/script_center.py`), unchanged, running
  on its own port and embedded in the shell. `Ctrl+1` (Today) / `Ctrl+2` (Reporting) / `Ctrl+3` (Management) / `Ctrl+4` (Data Flow) / `Ctrl+5` (Leads) / `Ctrl+6` (Health) / `Ctrl+7` (Channels) switch pages. ERP Desk always opens on Today; a `#reporting` / `#dataflow` / `#leads` / `#health` / `#channels` in the URL still wins.
- **Data Flow** - a large animated map of how data really moves through the whole system and what
  starts each step (see the next section).

### Data Flow page - where data goes and how it is automated

The page has two views of the same map, chosen with the **2D | 3D** switch in the stage's title bar: a cinematic
**3D scene** (the default on this branch) and the classic flat **2D diagram**. Everything below - triggers, health,
live numbers, the map check, the drawer, the legend, the coverage cards - is the same in both; the "3D mode" item at
the end of the list below covers what is specific to the scene.

The Script Center mindmap groups scripts by skill; it does not show data moving. The **Data Flow** page
(fourth item in the navigation, `Ctrl+4`) does: the real pipeline as glowing nodes and edges in four lanes -
**Intake** (lead form -> `erp/webhook_app.py`, tickets) -> **Storage & Logic** (`erp/leads.py` dedup +
round-robin, PostgreSQL, the reporting views) -> **AI & Integrations** (DeepSeek, ClickUp task creation, the
Sales rep in ClickUp, `erp/clickup_pull.py`, `erp/sync_employees.py`) -> **Monitoring & Reporting**
(`daily_check`, `daily_digest` + `emailer`, `weekly_report`, the digest files, the live Reporting page,
Script Center, the team inbox, Power BI). Dots travel along every edge in the direction the
data moves (faster and denser = more recent activity); nodes pulse when they did something recently.

- **Trigger, shown honestly.** Every node says what starts it: **event** (webhook / inline call), **scheduled**
  (Windows Task Scheduler), **background** (ERP Desk's own live refresher and once-a-day auto digest) or
  **manual** (a button, a `.bat`, a command). The page checks whether each trigger is *really armed*: it probes
  the webhook listener on `127.0.0.1:8000`, queries Task Scheduler (`schtasks /query`, read-only) for tasks whose
  command matches each job, and checks ERP Desk's own loops. Something the README only recommends (for example
  `clickup_pull` every 15 minutes) is labelled "recommended, NOT scheduled on this machine", and the
  **Automation coverage** card counts stages as running by themselves / partly running / automatable but not
  running / manual. Each *edge* is checked on its own (its `armed_by` in `EDGES`), not by its driver node: with the
  webhook offline the lead -> DeepSeek -> database hops stay dashed even though DeepSeek also writes the daily
  digest narrative (such a node shows as "partly automated").
- **Live numbers and health.** Counts come from read-only SQL (leads, assignments, AI analyses, ClickUp sync
  rows, pulled comments, staff, tickets, views, the `powerbi_reader` role); last success / last error come from
  `script_center.log`, `daily_digest.log`, the digest JSON files and ERP Desk's own state. Each node gets a
  state: healthy / stale / error / no run yet / mocked (running on a fallback such as no API key) / offline /
  idle / unavailable (its data could not be read). The page refreshes on the same cadence as Reporting.
- **Click a node** for its drawer: purpose, file (with an "Open in Script Center" shortcut that switches to the
  Management page and copies the path), data in / out, triggers and their real state, live numbers, health,
  last success / error. **Hover** a node or an edge for a tooltip and to light up its neighbours.
  **Trace latest lead** (`T`) replays the newest lead's real journey step by step with the timestamps stored
  in the database. `prefers-reduced-motion` turns the animation off (static dots, no pulses).
- **Nothing is hard-coded in the browser.** The map lives in one file, `desktop/flow_definition.py`
  (`NODES`, `EDGES`, `LANES`, which triggers each node has, what to look for in Task Scheduler). To add a stage,
  copy a node dict, give it a lane/col/row, and add an edge dict; live numbers for it come from a `stats` list
  (dotted paths such as `leads.total`, see `desktop/flow_data.py`) and, optionally, a health function in
  `flow_data.HEALTH`. Without one the node shows as a plain static box. The file is read once, so **after editing
  it, restart ERP Desk** (Quit, then open it again).
- **The map warns you when it is out of date.** Because the structure is written by hand, it can not discover a new
  script by itself - so the page checks it for you. On every refresh (re-scanned at most every 30 s; the Refresh
  button scans at once) ERP Desk lists the project's Python scripts (every top-level folder plus the root; hidden
  folders, virtualenvs, `__init__.py`, `__main__.py` and tests are skipped) and compares them with the source files
  the nodes declare (a node's `files` list, else its `script` / `file` paths). Two outcomes:
  - **"Map is in sync: N scripts mapped"** - a small green line under the header (with a "see the IGNORE list"
    toggle) so you know the check is running.
  - **"The map is out of date"** - an amber card near the top (plus a `map out of date - N` pill next to LIVE)
    that lists, each with its first docstring line: *scripts that are not on the map*; *nodes that point to a
    missing file* (moved / renamed / deleted); *`IGNORE` entries to tidy* (file gone, or a node covers it now, or no
    reason given); and *the map contradicting itself* (edges to unknown nodes, automated edges without `armed_by`,
    an unknown `armed_by` / trigger / counter key, duplicate ids, a lane that does not exist, a `TRACE` step for a
    missing edge). The same problems are also logged once in `erp_desktop.log`.

  **How to fix what it lists:** open `desktop/flow_definition.py` and either (1) add a node to `NODES` (and its
  edges to `EDGES`, with an `armed_by` on every automated one) for a real pipeline piece, or (2) add the script to
  `IGNORE` - `"erp/helper.py": "one-line reason"` - when it deliberately has no place in the flow (a helper a node
  already implies, ERP Desk plumbing, a demo). Then **restart ERP Desk**. If you edited the file but have not
  restarted, the card says so ("was edited after ERP Desk started"). Runtime artifacts that may not exist yet
  (`daily_digest_latest.json`, `daily_digest_history.json`) do not belong in a node's `files`. If the check itself cannot run
  (an unreadable folder, say) the page shows "Map check unavailable" and everything else keeps working; a script
  that does not parse is still listed, just without a description.
- **3D mode (WebGL, on by default).** The same live feed (`/api/flow`: nodes, edges, lanes, health, live numbers, edge
  live / dormant / unknown state, the latest lead's trace) is drawn as one continuous scene. Nothing is duplicated
  or invented: geometry comes from the map's own layout (column / row -> x / y, lane -> depth), colours and line styles
  from the same vocabulary as the 2D legend, particle density and speed from `flow.js`'s own `flowParams()`, and the
  details drawer, tooltips, legend, coverage cards and refresh cadence are the 2D page's.
  - *Scene.* The four lanes are glass panes stepped back into depth (Intake nearest, Monitoring & Reporting farthest)
    over a reflective grid floor with a light band that runs in the direction the data moves. Nodes are dark glass
    slabs with a glowing frame and a crisp HTML label (icon, name, live headline). **Trigger** = slab / label colour
    (event coral, scheduled blue, background violet, manual grey) plus the badge; **automation** = frame style (solid
    glowing = running by itself, dashed = partly running / automatable but not running, thin = manual or passive,
    dotted = outside this machine); **health** = the floating beacon (solid = healthy, half = stale, flickering =
    error, hollow wireframe = no run yet / offline / idle, hatched = mocked) with the same state text under the slab
    as in 2D. Edges are emissive tubes: bright and streaming = running now, dashed = automated by design but not
    running, dotted = a person starts it; light particles travel from source to target, denser and faster the more
    recent the activity. Bloom, fog, soft-focus edges, chromatic aberration, vignette and film grain finish the picture.
  - *Interaction.* Drag = orbit (damped, limited to about 55 degrees either side and to angles that keep the labels readable), right-drag or Shift-drag = pan, wheel = zoom (only after you
    click the scene, or in **Immersive** mode, so the page still scrolls normally), double-click / **Reset view** /
    `Home` = back to the overview, `F` = Immersive. Hover a node or an edge for its tooltip and to light up its
    neighbours; **click a node** and the camera glides to it while the usual details drawer opens (the scene shifts so
    the node stays visible beside the drawer); `Esc` closes it and the camera returns. When a live counter goes up
    between two refreshes a shockwave travels down that edge and bursts at the node it feeds.
  - *Cinematic tour* (button, or `T`): a letterboxed camera flight along the real journey of the newest lead, step by
    step from the database timestamps (the same `TRACE` as the 2D "Trace latest lead"), with a caption and progress
    bar; a step the lead never reached ends the flight honestly. `Esc`, the button or any click on the scene stops it.
    The scene sits below the summary cards, so starting the tour first scrolls it into view (smoothly, or instantly
    with reduced motion), and the flight waits until the scene is on screen. The opening fly-in likewise waits until
    about half of the scene is visible (it starts anyway after a few seconds if only a strip of it ever shows).
  - *Fallbacks.* If WebGL2 is missing, the GPU context cannot be created, the 3D code fails, or the OS asks for
    `prefers-reduced-motion`, the page shows the 2D map instead with a small notice (an automatic fallback is not
    remembered, so a machine that can do 3D keeps doing it). The choice you make with the 2D | 3D buttons is
    remembered in the browser (`localStorage`). A lost GPU context is restored automatically.
  - *Performance.* The frame rate is measured all the time (chip in the top-left corner). Below ~40 fps the scene steps
    down by itself - device pixel ratio (capped at 2), MSAA, bloom levels, particle count / trails, reflections, dust -
    through four levels (high / medium / low / minimal); after half a minute of steady full frame rate it climbs one
    level back up (never into a level that already failed again on this machine), so one stall does not leave the scene
    soft for good; if even the lowest level crawls (under ~10 fps, e.g. pure software rendering) the page hands over to
    the 2D map. The render loop stops when the window is hidden, and the
    whole scene - every GPU buffer, texture, framebuffer, program and the WebGL context itself - is released whenever
    you leave the Data Flow page, so switching pages repeatedly does not accumulate memory.
  - *No third-party library, works offline.* The scene runs on a small WebGL2 engine written for this app:
    `desktop/static/flow3d/shaders.js` (GLSL), `engine.js` (matrices, GPU-resource tracking, bloom / composite chain,
    orbit camera) and `flow3d.js` (scene, interaction, tour, quality control); no CDN, no npm, no build step. It was
    written from scratch so that nothing third-party has to be vendored; swapping in a pinned, vendored Three.js later
    would only touch `flow3d/`.
    Requirements: a browser with WebGL2 (Edge / Chrome; hardware acceleration recommended).
  - *Extending the map needs nothing here.* The 3D scene, like the 2D map, is built from `desktop/flow_definition.py`;
    add a node or an edge there (and restart ERP Desk) and it appears in both views.
- **Safe by construction.** `GET /api/flow` is read-only and takes no path or command input (the only URLs it
  contacts are the fixed localhost probes in the definition file); the Refresh button is a token-protected POST
  like the other actions; every string from the database or a log is escaped before it reaches the page and
  credential-shaped text is redacted; only booleans such as "API key configured" are exposed, never values.

**Install the shortcut (once)** - creates `ERP Desk` on the Desktop and in the Start Menu, with a
generated icon (`desktop/make_icon.py`, Pillow or stdlib fallback):
```bash
install_erp_desktop.bat            (or: powershell -ExecutionPolicy Bypass -File desktop\install_shortcut.ps1)
install_erp_desktop.bat /uninstall (removes them again)
```
**Launch** - double-click the shortcut (or `run_erp_desktop.vbs` / `run_erp_desktop.bat`). No console
window stays open. Launching again while it is running just brings the existing window to the
front (single instance). For troubleshooting use `run_erp_desktop_debug.bat` (shows the log live).

**Quit** - close the window, or use the **Quit** button in the sidebar, or `stop_erp_desktop.bat`
(`python -m desktop --stop`). Every child process (Script Center, digest run, browser) is tied to a
Windows Job Object, so nothing is left running - even if the launcher itself is killed.

**Logs** - `erp_desktop.log` (launcher, live report, digest) and `erp_desktop_streamlit.log`
(Script Center), both in the project folder (git-ignored). `python -m desktop --status` prints
what is running.

**Ports** (127.0.0.1 only, never exposed to the network): shell + live report `47650`, Script Center
`47651`. Override with `DESKTOP_PORT` / `DESKTOP_STREAMLIT_PORT` in `.env`; if a port is already used
by another program a free one is picked automatically and logged. Other optional settings:
`DESKTOP_REFRESH_SECONDS`, `DESKTOP_AUTO_DIGEST`, `DB_CONNECT_TIMEOUT` (see `.env.example`).

Security: state-changing endpoints (refresh, generate digest, quit) are POST-only, need a per-launch
random token and a same-origin request, and accept no path or command input; unknown `Host` headers
are rejected. `run_script_center.bat` and `run_webhook.bat` work exactly as before.

## Typography - one font (Montserrat) on every screen

Every **screen** uses **Montserrat** (Google Fonts, weights 400 / 500 / 600 / 700, `display=swap`) with the fallback stack
`'Segoe UI', system-ui, sans-serif`, so nothing breaks offline: ERP Desk (all pages, Plotly charts, the 2D / 3D Data Flow
labels), the Script Center (including the mindmap canvas) and the e-mail HTML. The colour palette is unchanged. A system
monospace (`Consolas, 'Cascadia Mono'`) is kept only for literal code: the Script Center editor (and only that text area -
every other free-text box stays in the text font), file paths, `<code>`, log / terminal / JSON blocks.

**There is no image output any more.** matplotlib rasterises with installed font *files* and cannot load a web font, so a PNG
would carry Montserrat only when Montserrat is installed on the PC. The one surface that produced one
(`erp/task_timeline_chart.py` -> `task_timeline.png`) was removed in the 2026-09-23 scope cut, and its matplotlib font
helper (`apply_matplotlib_style()`) was removed from `erp/typography.py` on 2026-09-24 once it had sat with no caller
for a full cycle (see PROJECT_NOTES.md if a future image output needs the same recipe again).

**Weights**: 400 body, 500 labels, 600 headings, 700 big numbers. Nothing asks Google Fonts for a weight no CSS rule paints -
an extra weight is a file downloaded for nothing *and* a later first chart draw, because ERP Desk's charts wait for
`Promise.all` of every weight in `shell.js`'s font gate.

**Figures**: prose keeps Montserrat's default proportional figures (its tabular "1" has a foot serif, so "11" reads "1 1"),
but a number that counts up, is redrawn by a poll, or stands beside other numbers gets `font-variant-numeric: tabular-nums` in
its own rule - Montserrat's "1" is about half the width of its "4", so a moving number otherwise resizes its own tile on every
frame. `.tnum` in `shell.css` is the utility for one-off cases in hand-written markup.

To change the font later, edit one line per surface: `--font` in `desktop/static/shell.css` (+ the `<link>` in `shell.html`,
and the weight list in `shell.js`'s font gate) for ERP Desk, and `erp/typography.py` for everything Python renders (Streamlit
`--theme` flags, e-mail, matplotlib); `run_script_center.bat` holds a literal copy of the Streamlit font flags. The smoke gate
(check 6) fails if a retired font name reappears on any tracked line other than a change-log / history line, if `--font` /
`--code` in `shell.css`, the Google Fonts `<link>` in `shell.html` and `erp/typography.py` stop agreeing (family *and*
weights), if that `.bat` copy drifts, if a downloaded weight is painted
nowhere or the `shell.js` gate loads a different list, if one of the moving-number rules loses its tabular figures, or if the
Streamlit `--theme.font` value stops parsing - through *Streamlit's own* parser - to exactly the URL `erp/typography.py`
builds. (That last one matters: a `--theme.font` value is split on its **first** colon, so a fallback stack appended after the
URL is swallowed into it and Google Fonts then answers without `display: swap`. Fallback stacks belong in CSS only.) Because
the gate hardcodes the current family name, **changing the project font means editing `tests/smoke.py` too** - it is not
literally one line per surface. One more note: the first ERP Desk load without internet uses the fallback font until the
network is back.

## Smoke check - the merge gate (`tests/smoke.py`)

A **read-only** check that the whole project still hangs together. The autonomous
improvement loop (`.claude/AUTONOMY.md`, step 6) merges a branch only when it exits with code 0; it is just as useful
before you commit by hand.

**How long it takes** (measure it, do not trust a number in a document - the gate prints its own per-check times and a
total). On this machine, with nothing else running and the project's files already in the Windows file cache, three
consecutive runs took **22.1 / 22.6 / 22.9 s**, of which check 4 (11.8 s) and check 9 (5.0 s) are most of the bill.
That figure is **not** what a cold run costs: the gate reads every tracked file several times, imports 26 modules and
builds a throw-away git repository, so it is disk- and antivirus-bound rather than CPU-bound. The first run after a
reboot, or any run competing with another process that is churning the disk (a recursive grep over `venv\`, a virus
scan, a build), takes **45-57 s** here - measured repeatedly, and reproduced independently by a reviewer at 46-47 s.
Both figures are real; budget for the slow one when you automate around the gate, and use `--timeout` (default 120 s)
rather than a stopwatch as the hard limit.

```bash
run_smoke.bat
```
or
```bash
venv\Scripts\python.exe -B -m tests.smoke
```

The `-B` is not cosmetic: `runpy` compiles `tests\__init__.py` and `tests\smoke.py` to bytecode *before* the module
can set `sys.dont_write_bytecode`, so without it a `tests\__pycache__` folder appears in your work tree (the gate's own
check 9 fails when it finds one).

It prints one PASS/FAIL row per check with a one-line reason (details of a failing check follow the table):

| # | Check | What it verifies |
|---|---|---|
| 1 | Imports & compile | every project `.py` compiles (in memory, no `.pyc`); a module of `erp/`, `desktop/` and `dashboard/` is imported only when **every** statement that runs at import time is on an allowlist (imports, `def`/`class`, constant assignments, a guarded `__main__`, simple `logging` / `os.environ` / `Path` calls) and it imports no compile-only module - anything else is compile-only and listed with `--verbose`. A per-module `IMPORT_SAFE_EXTRA` entry (with a written reason) widens the list for one reviewed file |
| 2 | Database (read-only) | PostgreSQL answers in a `READ ONLY` session; `v_leads_summary`, `v_tickets_summary` and the core tables can be `SELECT`ed |
| 3 | Data Flow map sync | `desktop/flow_data.check_map`: no unmapped scripts, no stale nodes or `IGNORE` entries, no unknown edges (unknown nodes, missing `armed_by`) |
| 4 | ERP Desk server + pages | the FastAPI app is started in-process on a spare `127.0.0.1` port (never 47650/47651), the shell, its assets and the JSON feeds (Today - its shape is validated: status sentence, the six tiles, attention list, six health cells, the `status.caveat` honesty sub-line whenever the webhook / ClickUp pull is not verified, and no degradation while the database is reachable - Reporting, Data Flow, digest, status) must answer HTTP 200, a POST without the app token must be refused; the Leads feed (`/api/leads/analysis` shape, funnel / SLA totals adding up, no percentage under `LOW_N` leads, "Past SLA now" equal to the Today tile, a hostile filter refused with a 400, and `/api/leads/export.csv` with its byte-order mark, header and formula-safe cells) is validated too; the scenario table `tests/leads_scenarios.py` (filter whitelists, funnel / SLA / CSV math, SQL on a synthetic session, failing panels, refused / hung database) and the scenario table `tests/today_scenarios.py` (headline / caveat for all-green, blind, needs-attention and database-down inputs, the "since yesterday" line including blind and no-history cases, the detail drawer's lists and items with and without an AI analysis or a ClickUp note, rules text follows the config, statement timeout, hung-database and busy-store behaviour) also runs here; the Health feed (`/api/health`: every declared card present exactly once in one group, state / word / severity badge / consequence heading inside the vocabulary, every card saying why, what depends on it and at least one copyable fix hint, an unknown query parameter NAME refused with a 400 that names it (lesson L-098), and **no value of any `.env` setting anywhere in the response**, checked against this machine's real configuration - plus the cross-check that each Health card agrees with the Today strip cell for the same dependency) is validated too, and `tests/health_scenarios.py` runs here as well; the marketing ingestion pipeline's pure logic (typing/coercion, validation, malformed-row handling, dedupe, the scoped GIN allowlist, the flat-file connector, DDL-vs-code agreement) is also checked here, `tests/marketing_scenarios.py` - it needs no database and no server, so it is not itself proof the schema installs; then the server is stopped. The Script Center is never started |
| 5 | Secrets & stray files | everything since `git merge-base main HEAD` - the working-tree diff **plus every commit and commit message of the branch** (`git log -p`), so a secret that was added and removed again still fails the gate, and a `main` that moved on after the branch was cut never shows up as a reversed change - plus untracked files are scanned for ClickUp `pk_` tokens, DeepSeek `sk-` keys, webhook URLs with tokens (including URLs whose token is an opaque path segment on any host), credentials in URLs, password / token literals (`WEBHOOK_TOKEN = "..."`, `X-Webhook-Token: ...` headers, `os.getenv("..._TOKEN", "<literal>")` defaults), the sha256 denylist of the webhook token that was leaked once (`KNOWN_LEAKED`: only the hash is stored, so the value is caught wherever it reappears) and any value copied from your local `.env`; and for stray files (`*.log`, scratch, csv/json outputs, empty files, `.env` files, the owner's `processed_orders.csv` / `summary_report.json` if they were staged or committed). A **strong** secret name (`password`, `secret`, `api_key`, `api_token`, ...) is exempt only for a clearly fake value (`YOUR_...`, `changeme`, `xxxx`, `<x>`, `${X}`, `example`) - a real-looking value such as `hunter22xyz` or `ADMIN2026` is always reported. Matched text is never printed |
| 6 | Agent system files + typography | `Law.md`, `.claude/**` playbooks, `lessons.md` (unique ids), the workflow script exist; `lessons.md` and the workflow have no CR or control bytes; no **tracked** file names a retired font except on a change-log / history *line* (whole documents are never exempt); `--font` and `--code` in `shell.css`, the Google Fonts `<link>` in `shell.html` and `erp/typography.py` are cross-checked against each other, and the `.bat` font flags equal `streamlit_theme_args()` |
| 7 | Nothing left behind | no new files in the work tree, no port still listening, no stray threads or logging handlers |
| 8 | Branch discipline | `FAIL` when the checked-out branch **is** `main` / `master` and tracked source files are modified there (`Law.md` rule 1); the owner's own `processed_orders.csv` / `summary_report.json` do not count |
| 9 | Gate self-test | `tests/smoke_selftest.py`: every protection above has a scenario that must pass - the `-B` flag and the opt-in pause of `run_smoke.bat`, merge-base vs. the tip of `main`, the history and commit-message scan, the import allowlist, the strict placeholder rules, git missing / hanging / no repository / detached HEAD / unborn branch / missing base / unrelated histories, branch discipline, the typography line rules and the font cross-check. Uses a throw-away git repository in the system temp folder and deletes it again |

**Exit code:** `0` = everything passed (green gate), `1` = at least one check failed, `2` = the run timed out
(`--timeout`, default 120 s), `3` = a `--only` subset passed (not a gate). A database, server or git problem is a clean
`FAIL` line, never a traceback. Options: `--verbose` (details of every check), `--only 3,5`, `--base <branch>`
(default `main`; the gate diffs against `git merge-base <branch> HEAD`).

It never writes to the database, never starts the Script Center and never makes a real ClickUp / DeepSeek / e-mail
call. **`run_smoke.bat` never waits for a key press** - not even when you double-click it - unless the environment
variable `SMOKE_PAUSE` is set (`set SMOKE_PAUSE=1` first, or use a shortcut that sets it). There is deliberately no
"was it double-clicked?" detection: `%cmdcmdline%` looks the same for Explorer, `cmd /c`, PowerShell and Task
Scheduler, so such a test would either hang unattended jobs or never fire (this replaces the earlier
`SMOKE_NO_PAUSE`, which is no longer needed because not pausing is now the default). To exempt a module from check 1
add it to `IMPORT_SKIP` in `tests/smoke.py`; a file that is not part of a change but must exist (test data) belongs
under `tests/fixtures/`. The smoke check is a developer tool, not part of the data pipeline, so it has no node on the
Data Flow map (`tests/` is excluded from the map's script scan).

## Important notes

- The standard flow for adding a real Sales rep: invite them to the ClickUp workspace first, then
  run `python -m erp.sync_employees` to automatically create/update them in the `users` table
  (no need to type in `clickup_user_id` by hand).
- The original seed Sales reps (Do Thi Lan, Vu Minh Tuan, Bui Thi Nga) have been set to
  `is_active = FALSE` now that real staff exist, so they're no longer picked by the
  round-robin lead assignment.
- `erp/config.py` reads `.env` with `override=True` so it takes precedence over any
  system environment variable already present (avoids mix-ups with another application's
  API key on the same machine).

## Change log

- **2026-09-25 - Channels Insights + Excel report.** The Channels page gained an Insights panel (six explainable rules, per-currency, thin-data guards, automation ideas as text only) and a Download Excel button; see the Channels Insights bullet under ERP Desk.
- **2026-09-25 - the Channels page (`Ctrl+7`), Phase 3 of the marketing data work.** ERP Desk gained a seventh page, the channel-performance
  report, reading only the two rollup tables (`desktop/channels_data.py`, `static/channels.{js,css}`, `tests/channels_scenarios.py`, read-only
  `GET /api/channels` and `/api/channels.csv`). In the owner's database today it shows a setup card with the exact psql command (the rollup
  is not installed yet); nothing is scheduled or installed by it. No new thread, poller or dependency.

- **2026-09-24 - Health page follow-ups.** Closed eight small items the Reviewer/Auditor left open on the Health page,
  mechanical fixes only, nothing redesigned: the PostgreSQL card's location is read from `erp/config.py`
  (`DB_NAME`/`DB_HOST`/`DB_PORT`) instead of being hardcoded, so it can never name the wrong database; the docs no
  longer overstate "opens no database connection" (the shared Today store's own on-demand build can still open one);
  `static/health.js`'s redraw signature no longer includes the relative-time text, so a card grid rebuild does not
  happen once a minute and keyboard focus / the "Copied" confirmation survive polling (the "ago" text still updates
  in place); a card that crashes to "unavailable" keeps its own real fix hints instead of only "press Refresh"; an
  unscheduled job no longer shows a "last succeeded" time it did not earn from its own trigger; `GET /api/health`
  now refuses an unknown query parameter NAME with a `400` that names it, checked before the cache (lesson L-098,
  matching `/api/leads/analysis`); `desktop/health_data.py` builds its fix hints lazily so the merge gate's import
  check (check 1) can import it instead of treating it as compile-only. The Today page's "Needs a human" phrase-hoist
  scenarios (every row sharing the same phrase, a mixed list, a single-row list) moved from `tests/health_scenarios.py`
  to `tests/today_scenarios.py`, where the function they test (`today_data.build_attention`) actually lives.

- **2026-09-24 - the Health page (`Ctrl+6`).** ERP Desk gained a sixth page: one card per dependency, with what
  depends on it and a fix hint you can copy, plus "what this means for the numbers" and configuration truth as
  `set` / `not set`. It **extends** the Today page's health strip instead of duplicating it - the same
  `desktop/today_data.build_health()` cells and the same Data Flow snapshot, so the strip and the page cannot
  disagree (the smoke gate asserts it). New: `desktop/health_data.py`, `desktop/static/health.{js,css}`,
  `tests/health_scenarios.py` and the read-only `GET /api/health`; no new thread, no new poller, no new dependency.
  In the same change the "Needs a human" list on Today stopped repeating a phrase every row shares and hoists it
  above the list, and the Data Flow feed stopped reporting the Script Center as "healthy" when nothing is
  supervising it (it now says "unavailable", which is what it knows).

- **2026-09-23 - "Sources & cohorts", a second tab on the Leads page.** A detailed, interactive lead-source report for
  data analysts: one row per source (leads, share, how many reached each strict pipeline stage, the five SLA outcomes,
  median time to first reply with its n, the rep spread), plus arrival-cohort curves - leads grouped by the day, week or
  month they arrived and followed day by day since their own arrival for *replied*, *reached ClickUp* or *past SLA*.
  New read-only endpoints `GET /api/leads/sources` and `GET /api/leads/sources.csv` (`part=sources|cohorts`), a new feed
  `desktop/sources_data.py` and `static/sources.{js,css}`. It is a tab rather than a new navigation item, so the filters,
  the Definitions drawer and the single poller are shared with the Funnel & SLA tab. Built to be
  correct today - the database holds one single arrival cohort, which the page says plainly and draws as points, never as
  a trend line - and useful once leads arrive over weeks. `GET /api/leads/analysis` now also refuses an unknown parameter
  NAME with a 400 instead of ignoring it (lesson L-098), and the global `svg { stroke: currentColor }` rule no longer
  draws a box around every Plotly legend entry. New scenario table `tests/sources_scenarios.py` (cohort maths, low-n
  rules, whitelists, CSV, and SQL on a synthetic four-cohort session); smoke check 4 now requests the new endpoints and
  compares their "past SLA now" with the Funnel & SLA tab's and the Today page's.

- **2026-09-23 - ERP Desk is the only user interface.** At the owner's request the other report /
  dashboard surfaces were removed: `erp/html_report.py` (and its output `erp_report.html`),
  `dashboard/streamlit_app.py` with `run_dashboard.bat` (the CSKH Streamlit dashboard on port 8501) and
  `erp/task_timeline_chart.py` (and its `task_timeline.png`). The report queries and the headline
  sentence ERP Desk shares with the old HTML report (`load_all()`, `build_insight()`) moved unchanged
  into `desktop/report_data.py`, so the Reporting page renders exactly as before - the `/api/report/data`
  payload is byte-identical for the same database state. The Data Flow map lost three nodes
  (`html_report`, `cskh_dashboard`, `timeline_chart`) and three edges (`e_db_html`, `e_db_cskh`,
  `e_db_timeline`) and is in sync again; port 8501 is no longer used by anything here. Kept, unchanged:
  the whole `erp/` pipeline, the e-mail jobs (`daily_digest`, `weekly_report`, `emailer`) and the Script
  Center, which ERP Desk embeds as its Management tab and which still runs standalone on 8502.

## Social listening (Social page)

`db/sql/12_social_listening.sql` creates the additive `sl` schema: a raw landing table for keyword exports, `sl.run_cleaning(batch_id)`
(dedupe, spam flags, brand / topic / sentiment tagging; dictionaries are editable tables) and analysis views. The Social page of ERP Desk
(`desktop/social_data.py` + `static/social.{js,css}`, nav item 8, `GET /api/social`) reads only that schema, read-only. To try it with
invented data: run `12_social_listening.sql`, then `python db/sample/sl_generate_sample.py`. Without the schema the page shows the
install command instead of numbers.
