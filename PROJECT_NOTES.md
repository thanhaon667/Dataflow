# erp_support — Project Notes (business + technical)

Last updated: 2026-09-20 (after merging the 3D Data Flow, `b41f8b5`) · Repository: `C:\Users\AKIBA\Desktop\python` · Branch `main`

This file is the single "hand-over" document for the project: what it is, why it is built the
way it is, how every part fits together, how to run and operate it, what is (and is not) automated
today, and the traps we already hit. `README.md` is the short quick-start; this is the long form.
**No secret values appear in this file** — only variable names.

---

## 1. What this project is

A small, self-hosted operations system for a Customer Support (CSKH) / Sales team:

1. **Lead-to-Sale pipeline** — a lead form posts to a webhook → the lead is stored in PostgreSQL,
   de-duplicated, assigned to a Sales rep, analysed by an AI model, turned into a ClickUp task, and
   the rep's ClickUp comments are pulled back into the database.
2. **Support tickets** — a ticket model shared by IT and Customer Support (schema + SLA policies exist;
   no live ticket data yet).
3. **Reporting** — a CSKH Streamlit dashboard, a live interactive report, a daily AI briefing (digest),
   a weekly AI report, a health-check script, and read-only access for Power BI.
4. **Control layer** — "Script Center" (catalog + mindmap + editor + test runner for every script) and
   **ERP Desk** (a local, desktop-looking app that unifies Reporting, Management and an animated
   **Data Flow** map).

Everything runs on one Windows machine. Nothing is exposed to the internet.

## 2. Architecture at a glance

```
 lead form ──POST──▶ erp/webhook_app.py (FastAPI :8000)
                          │  erp/leads.py
                          │   1. insert lead (raw payload kept as JSONB, SLA due date set)
                          │   2. dedup: same email OR phone → same rep as before
                          │      else → active rep with the fewest current leads
                          │   3. erp/ai_client.py → DeepSeek → scale / potential score / org type / notes
                          │   4. erp/clickup_client.py → ClickUp task (assigned, AI notes in description)
                          ▼
                   PostgreSQL 18  db "erp_support"  (owner role erp_app)
                          ▲                    │
   erp/clickup_pull.py ───┘ (comments back)    ├─▶ views v_leads_summary, v_tickets_summary
   erp/sync_employees.py (ClickUp members→users)│      └─▶ role powerbi_reader (read-only) → Power BI
                                                ├─▶ erp/daily_check.py / weekly_report.py / daily_digest.py
                                                └─▶ ERP Desk (desktop/)             (:47650 shell, :47651 Script Center)
```

Design choices worth remembering (the "why"):

| Decision | Reason |
|---|---|
| One shared `tickets` table for IT + CSKH, split by `department_id` | New departments need rows, not new tables/code |
| Lead assignment kept as history (`lead_assignments.is_current`) | Audit trail; owner can change over time |
| AI result in its own table (`lead_ai_analysis`) | Re-analysis without overwriting; keeps history |
| Raw webhook payload stored as JSONB | Form fields can change; nothing is lost |
| Dedup on email **or** phone | Forms often miss one field; better to catch repeats than to be strict |
| Assign to rep with fewest active leads | Fair by real workload, not by arrival order |
| Trigger on `tickets` writes status history | History is recorded no matter which code path changes a status |
| Summary **views** for dashboards/Power BI | One `SELECT`, and read-only rights can be granted on views only |
| Every external integration has a "mock / no-op when unconfigured" fallback | The pipeline never crashes because ClickUp/DeepSeek/SMTP is missing |
| `.env` loaded with `override=True` | A machine-wide variable of the same name must not shadow the project's own `.env` |

## 3. Technology stack (versions actually installed in `venv`)

| Layer | Tech |
|---|---|
| Language / runtime | Python 3.14.7 (Windows) |
| Database | PostgreSQL 18 (Windows service `postgresql-x64-18`, port 5432), driver `psycopg2-binary` 2.9.13, `SQLAlchemy` 2.0.52 |
| API / webhook | FastAPI 0.141.1 + Uvicorn 0.53.0 |
| Dashboards | Streamlit 1.64.0 (CSKH dashboard + Script Center) |
| Charts / report | Plotly 7.1.0 (live report, HTML report), Matplotlib 3.11.2 (PNG timeline; also Power BI's Python visual), pandas 3.0.5, NumPy 2.5.3 |
| AI | DeepSeek chat-completions API (`deepseek-chat`, JSON mode for lead analysis) via `requests` 2.34.2 |
| Task tool | ClickUp REST API v2 |
| Config | `python-dotenv` 1.2.3 (`.env`) |
| Desktop packaging | Microsoft Edge `--app` window + local FastAPI shell; icon generated with Pillow 12.3.0. **No Electron, no `.exe`** |
| Front end (ERP Desk) | Hand-written vanilla JavaScript + SVG + Canvas 2D + CSS (no framework, no chart library for Data Flow). Data Flow also has a 3D mode on a home-made WebGL2 engine (`desktop/static/flow3d/`, no third-party code, works offline) |
| Email | stdlib `smtplib` (no dependency) |
| BI | Power BI Desktop (Import mode for Python visuals) |

Size: ~10.8k lines across `.py/.js/.css/.sql/.html`.

## 4. Repository map

```
.claude/workflows/build-feature.js  Reusable Builder → Reviewer → Auditor multi-agent workflow (see §12)
db/sql/                             00 create DB+user · 01 schema · 02 CSKH seed · 03 lead schema ·
                                    04 sales seed · 05 lead SLA · 06 Power BI read-only role
erp/                                Business logic and scheduled scripts (see §6)
erp/typography.py                   Font tokens (Montserrat) for every Python-rendered surface (see §11)
erp/marketing/                      Marketing/channel interaction data: star schema + ingestion pipeline (Phase 1) + daily rollup refresh and partition maintenance (Phase 4: rollup.py, partitions.py) + two strict connectors for the owner's real exports (connectors/ad_performance.py, email_campaign.py, strict_csv.py, parse.py; sample_check.py verifies them on the real files in a throwaway schema), see §5 and docs/marketing-data-architecture.md
db/sql/07_marketing_schema.sql, 08_leads_indexes.sql, 09_marketing_rollup.sql, 10_marketing_currency.sql   Marketing star schema DDL + the two missing `leads` indexes + the two rollup tables + the fact's currency column (all additive; order 07 -> 10 -> 09; 10 and 09 are the owner's to run)
run_marketing_rollup.bat            Launcher for the rollup refresh (not scheduled by anything)
dashboard/script_center.py          Script Center (:8502 standalone, :47651 inside ERP Desk = the Management tab)
desktop/                            ERP Desk: launcher, shell server, Today landing page, Leads explorer (+ its Sources & cohorts tab), Health page, live report, Data Flow (see §7)
tests/smoke.py                      Read-only merge-gate smoke check (run_smoke.bat, see §12)
tests/today_scenarios.py, tests/leads_scenarios.py, tests/sources_scenarios.py, tests/health_scenarios.py   Scenario tables run by smoke check 4
desktop/sla_words.py                 The one place the five lead SLA outcomes are put into words (Today + Leads import it)
run_*.bat / *.vbs / install_*.bat   Launchers (see §9)
setup_env.bat                       Create venv + install requirements
README.md                           Quick start
PROJECT_NOTES.md                    This file
```

Runtime/generated files (gitignored): `.env`, `*.log`, `daily_digest_latest.json`,
`daily_digest_history.json`, `.erp_desktop/` (instance file, Edge profile).

## 5. Database

Database `erp_support`, owner role `erp_app`. **15 tables + 2 views.** Full column list: run `\d+ <table>`
in psql or open the Data Flow / schema pages; the SQL is in `db/sql/`.

| Group | Tables |
|---|---|
| Shared | `departments` (CSKH, IT, SALES), `users` (incl. `clickup_user_id`, `is_active`), `ticket_categories`, `sla_policies` |
| Tickets (IT + CSKH) | `customers`, `orders`, `tickets`, `ticket_status_history`, `ticket_comments`, `clickup_sync` |
| Lead-to-Sale | `leads` (raw JSONB, `sla_due_at`), `lead_assignments`, `lead_ai_analysis`, `lead_clickup_sync`, `lead_updates` |
| Views | `v_tickets_summary` (with computed `sla_breached`), `v_leads_summary` (current rep + latest AI + sync state + `sla_breached`) |

Roles:

| Role | Purpose | Rights |
|---|---|---|
| `postgres` | Superuser (only for creating DB/roles) | all |
| `erp_app` | The application | owner of `erp_support`; **no** `CREATEROLE` |
| `powerbi_reader` | Power BI | `CONNECT` + `SELECT` on the 2 views only; raw tables denied (tested) |

Rules encoded in the data layer:
- **Lead SLA = 5 business hours** (08:00–17:00, Mon–Fri) from creation; computed in `erp/business_hours.py`, stored in `leads.sla_due_at`.
  Tune with `LEAD_SLA_HOURS`. Business-hour window is constants in that module.
- Ticket SLA minutes per department/priority live in `sla_policies` (CSKH and IT rows seeded).
- Trigger `trg_tickets_before_update`: on status change writes `ticket_status_history` and sets `resolved_at` / `closed_at`.
- `leads` requires at least one of email / phone (`chk_lead_contact`). Tickets require exactly one origin (customer *or* internal requester).

Current data (2026-09-20): 11 leads (realistic demo data pushed through the real pipeline), 2 real Sales users
(synced from ClickUp), 0 tickets. All original fake employees/tickets/customers/orders were wiped on request.

**Marketing / channel interaction data (Phase 1, 2026-09-24, additive)** - a separate star schema, installed in the
same `public` schema but never joined into the tables above except through `marketing_identity.lead_id`: the
append-only `marketing_landing` (raw payload) + `marketing_ingest_run` (one row per batch), the small dimensions
`marketing_source` / `marketing_channel` / `marketing_campaign` / `marketing_creative` / `marketing_identity`, and
the monthly-partitioned fact `interaction_fact`. DDL: `db/sql/07_marketing_schema.sql` (plus
`db/sql/08_leads_indexes.sql`, the one exception to "additive-only" tables - it only adds two indexes on the
existing `leads` table: `idx_leads_created_at`, `idx_leads_source_created_at`). Pipeline: `erp/marketing/`. Full
design, partition/index reasoning and real scale-verification numbers: **`docs/marketing-data-architecture.md`**.
The ERP Desk Channels page (Phase 3, section 7) reads only the rollup tables; no real connector yet (Phase 2) - see that doc's "what Phase 2/3/4 would
add" section.

**Marketing currency, duplicate keys and grain (2026-09-25, additive)** - the owner's two real exports (git-ignored `data_inbox/`:
118 weekly paid-social rows in VND, 128 daily e-mail rows in EUR) forced four decisions, all built and verified on the real files in a
throwaway schema (`erp/marketing/sample_check.py`; details and numbers: `docs/marketing-data-architecture.md` section 9).
(1) **Currency:** `db/sql/10_marketing_currency.sql` adds `interaction_fact.currency` (`CHAR(3) NOT NULL DEFAULT 'USD'`, CHECK on the
shape, idempotent, no rewrite); the owner runs it (this project never touches the real tables); the rollup key carries `currency`
and `grain`; money is never converted and the Channels page never adds across currencies (a `currency` filter, per-currency
lines, `MULTIPLE` in the CSV). (2) **Duplicate natural keys:** `ad_performance` numbers rows within an identical
(channel, start, campaign, adset, ad, device) key so both rows of the 6 pairs are kept; every run reports collisions; re-import is
idempotent; the numbering is a file position (stable only while the export order is). (3) **Grain:** weekly rows sit on their start
date with `attrs.grain = 'week'`; the page draws them in their own per-week chart and counts weeks. (4) **Two strict connectors**
(exact header, no guessed `M/D` vs `D/M`, `Decimal` money, sanity rules that reject rows). Revenue of `data1` is DERIVED
(purchases x value per purchase, its own `Revenue` column is empty) and is marked derived in attrs, the rollup
(`revenue_derived_micros`) and the page.

**Marketing rollup (Phase 4, 2026-09-25, additive)** - `db/sql/09_marketing_rollup.sql` adds
`interaction_daily_rollup` (day x channel x campaign, campaign 0 = none), `interaction_daily_channel_rollup` (day x
channel, summed from the first inside the same transaction) and the refresh journal `marketing_rollup_run`. Only additive
counts/sums are stored; ratios are computed at read time. `python -m erp.marketing.rollup` (`run_marketing_rollup.bat`)
recomputes the last `--days` (3) days plus older days whose facts were loaded/restated since the last good run, creates
the next `MARKETING_PARTITIONS_AHEAD` months of fact partitions, and reports (never drops) partitions past
`MARKETING_RETENTION_MONTHS`. Not scheduled by anything; the Data Flow node shows it as recommended, not set up. Design,
real numbers and how to schedule it: `docs/marketing-data-architecture.md` section 8.

## 6. `erp/` module reference

| File | Role | Trigger today |
|---|---|---|
| `config.py` | Reads `.env` (safe int parsing for `DESKTOP_*`) | imported everywhere |
| `db.py` | SQLAlchemy `engine` / `SessionLocal` | imported everywhere |
| `webhook_app.py` | FastAPI `POST /webhooks/leads`, `GET /health` | event (when running) |
| `leads.py` | Insert, dedup, assign, AI, ClickUp task, all in one transaction | via webhook |
| `ai_client.py` | `analyze_lead()` (JSON: scale, potential 0–100, org type, notes) and `chat()`; heuristic fallback if no key | via leads/digest/report |
| `clickup_client.py` | Create task, list comments, list workspace members; mock mode if token/list missing | via leads, pull, sync |
| `clickup_pull.py` | Pull new task comments → `lead_updates` with timestamps | manual / recommended schedule |
| `sync_employees.py` | ClickUp members → `users` (matched by email; default dept SALES) | manual |
| `business_hours.py` | Business-hour arithmetic for SLA due dates | library |
| `daily_check.py` | Sync comments, then list what needs attention (SLA breaches, unsynced leads, unanalysed leads, stale leads) | manual / recommended schedule |
| `weekly_report.py` | 7-day data → DeepSeek → written weekly report | manual / recommended schedule |
| `daily_digest.py` | Day-over-day metrics + AI narrative (status, opportunities, risks, actions); `--no-email` flag; failure → log + alert email + exit 1 | Task Scheduler (recommended) and ERP Desk auto once/day (no email) |
| `emailer.py` | `send_email()` via SMTP; logs and returns `False` if unconfigured, never raises | via digest / Script Center |

`html_report.py` (standalone Plotly report → `erp_report.html`) and `task_timeline_chart.py`
(→ `task_timeline.png`) were removed on 2026-09-23 (§17). The report queries and the headline
sentence they shared with ERP Desk now live in `desktop/report_data.py`.

## 7. Front ends

Since 2026-09-23 there is exactly one user interface, **ERP Desk**, with the Script Center embedded in it
as the Management tab (§17). The CSKH Streamlit dashboard on :8501 and the standalone HTML report are gone.

**Script Center** (`dashboard/script_center.py`, `run_script_center.bat`, :8502): single page of "slide" sections — overview,
vis-network mindmap (skill groups → scripts, click links to card + editor), script card grid, editor with
path-traversal guard, test runner (one-shot scripts run with timeout; servers only syntax-checked; DB/network scripts
labelled), structured log `script_center.log`, daily digest report card. Email-on-failure checkbox.

**ERP Desk** (`desktop/`): a local web app in a chrome-less Edge window.
- `launcher.py` — single-instance, starts everything, waits for health, opens the window, cleans up all child processes
  (Windows Job Object via `winutil.py`). Flags: `--no-window`, `--stop`, `--status`, `--console`.
- `server.py` — FastAPI on `127.0.0.1:47650`. Read endpoints: `/api/ping`, `/api/status`, `/api/today`, `/api/health`, `/api/report/data`, `/api/digest`, `/api/flow`, `/api/leads/analysis`, `/api/leads/export.csv`, `/api/leads/sources`, `/api/leads/sources.csv`.
  Token-protected POSTs: report/flow refresh, digest generate, heartbeat, window focus, quit. Host-header check, static path-traversal guard.
- Pages: **Today** (the landing page ERP Desk opens on: one plain-English status sentence, six KPI tiles with 7-day sparklines, an attention list of leads/tickets past SLA and a six-cell system-health strip; `desktop/today_data.py` + `static/today.{js,css}`; read-only SQL on demand with a 5 s cache and NO background thread, health taken from the Data Flow snapshot so the armed-trigger logic exists once; rules are documented at the top of `today_data.py`, shown on the page under "How this is decided" and in the README; each failing source degrades only its own tile; the headline never says a bare "All good" while the lead webhook is offline, the ClickUp pull is unscheduled/unchecked or ClickUp is not connected: it becomes amber "No problems found, but the numbers may be incomplete (...)" plus a `status.caveat` sub-line, from the same health cells as the strip; the "How this is decided" text is generated by `build_rules()` from `LEAD_SLA_HOURS`, `erp/business_hours.py` and the module constants; every query runs under `SET LOCAL` statement/lock timeouts and a 5 s total budget, `today.js` aborts a request after 8 s and shows STALE, one request in flight; the pure builders and the timeout behaviour are covered by `tests/today_scenarios.py`, also run by smoke check 4; a KPI tile, a "Needs a human" row and each clause of the "since yesterday" line open a **detail drawer** whose lists and items (`payload.details`, built by `build_details()` from `DETAIL_LEADS_SQL` / `DETAIL_TICKETS_SQL`, at most `DETAIL_ROWS` = 12 rows per list, plus `lead_ai_analysis`, `lead_clickup_sync` and the newest `lead_updates` row) travel in the same payload - Escape closes it, focus is trapped and restored with `focus({preventScroll:true})` and `#view-today` is `overflow:hidden` so the off-screen drawer can never scroll the stage sideways, L-099), **Reporting** (live, re-queries every `DESKTOP_REFRESH_SECONDS`, cross-filtering, daily briefing),
  **Management** (embeds Script Center), **Data Flow** (below), **Leads** (the analyst's page, next bullet) and **Health** (the bullet after it). Shortcuts: `Ctrl+1/2/3/4/5/6/7` (Today / Reporting / Management / Data Flow / Leads / Health / Channels).
- **Leads** (`desktop/leads_data.py` + `static/leads.{js,css}`, nav item 5, `GET /api/leads/analysis` and `GET /api/leads/export.csv`): a funnel + SLA explorer for data analysts. It is a distinct navigation item, not a Reporting tab, because Reporting ships one snapshot to the browser and filters it there, while Leads filters on the server so that one filter set (date range, rep, source, SLA status, ClickUp sync status) drives every panel, the paged table and the CSV. Read-only SQL on `v_leads_summary` (base query `BASE_SQL`: one row per lead with its SLA outcome and stage flags) plus `lead_updates` and `lead_ai_analysis`; no thread, 3 s cache per filter set, at most 3 concurrent builds (a busy store answers 503 instead of parking threads). "Past SLA" is built from `lead_past_sla_sql` / `lead_awaiting_sql` in `desktop/today_data.py` (the Today queries were refactored onto the same helpers), so the two pages cannot disagree; the smoke gate asserts equality. The funnel has only stages the schema can prove (leads, lead_assignments, lead_ai_analysis, lead_clickup_sync synced/mocked, lead_updates; there is no lead-status column), is strict (never above 100%) and reports leads with a later stage but a missing earlier one as a warning. Filters are whitelisted (`parse_filters`) and bound as parameters (`where_sql`), the sort column comes from the fixed `SORTS` dict, an invalid value is a 400, never silently dropped. Percentages need `LOW_N` leads, a 90th percentile `P90_MIN_N` replies. CSV: BOM, CRLF, formula-safe cells (`csv_cell`), fixed non-PII column list. The Definitions drawer text is built by `build_definitions()` from the same constants. Same timeout design as Today (statements re-armed with `td.arm_timeouts`, total budget, client abort at 20 s, last numbers dimmed and aged). Tests: `tests/leads_scenarios.py` (pure functions, SQL on temporary tables in one private session that shadow the real ones, failure paths), run by smoke check 4.
- **Leads -> "Sources & cohorts"** (`desktop/sources_data.py` + `static/sources.{js,css}`, the SECOND TAB of nav item 5,
  `GET /api/leads/sources` and `GET /api/leads/sources.csv?part=sources|cohorts`): where leads come from, and how each
  arrival cohort behaves over its first days. A tab, not a sixth nav item, because it is the same leads under the same
  filters: the filter bar, the Definitions drawer, the live pill and the header CSV button are shared, only the tab on
  screen polls (one poller per page), while `Ctrl+6` opens the Health page. It re-uses `leads_data.BASE_SQL`
  (so "past SLA" is still `today_data.lead_past_sla_sql`), `leads_data.parse_filters` / `where_sql` / `rate` / `csv_cell`
  and `sla_words`; it re-implements no rule of its own. It adds three whitelisted parameters - `bucket` (day/week/month,
  whitelisted against the buckets, default the finest one that yields at most `MAX_COHORT_LINES` = 12 cohorts, with the
  reason printed on the page), `metric` (replied / clickup / past_sla) and `cohort` (whitelisted against the cohort keys
  that really exist for that bucket) - and its own `SORTS` vocabulary for the source table. The cohort maths is pure
  Python (`build_cohorts`): an event's age is `floor(days since THAT lead's arrival)`, a cohort is followed only to the
  age its youngest lead has reached (the denominator never shrinks), a cohort with one age point is `single_point` and is
  drawn as points, and the chart falls back to counts as soon as any cohort drawn is below `LOW_N`. The cohort filter
  narrows the table and its CSV but deliberately NOT the chart (stated on the page and in the Definitions); in the cohort
  CSV it is a `selected_on_screen` column, so the parameter is used and nothing is silently ignored. Both CSV files hold
  aggregate rows only - no per-lead column exists, so no e-mail, phone or payload can leak. Tests:
  `tests/sources_scenarios.py` (cohort maths, low-n rules, bucket choice, whitelists, CSV, plus SQL on a private
  synthetic session with FOUR arrival cohorts), run by smoke check 4.
- **Channels** (`desktop/channels_data.py` + `static/channels.{js,css}`, nav item 7 / `Ctrl+7`, `GET /api/channels` and
  `GET /api/channels.csv?part=channels|daily|campaigns`): the Phase 3 channel-performance report. Since 2026-09-25 money is per
  currency and never summed across currencies (`money_cell()` is the one place that decides; the `money_*` queries add money only
  inside one currency; a whitelisted `currency` filter; `MULTIPLE` in the CSV), the daily rollup grain `week` is drawn in its own
  per-week chart (never as one day), there are six tiles (revenue added, derived revenue labelled) and "low volume" needs few
  sessions AND few clicks. It is the ONLY reader of
  the two Phase 4 rollup tables and reads nothing else that grows with events (three lookups: `marketing_channel`,
  `marketing_campaign`, `marketing_rollup_run`). `ChannelsStore` follows the `LeadsStore` pattern (a few seconds of cache
  keyed by the whitelisted parameters, `BoundedSemaphore` build slots with a wait cap, no thread) and reuses
  `leads_data._Reader`/`_problem`/`csv_cell`/`LOW_N`. Parameter names are checked before the cache (L-098). Four page
  states, chosen by `page_state()`: `not_installed` (the owner-run psql fix), `empty`, `no_match` (filters select nothing),
  `ready`. A campaign filter cannot be answered by the channel rollup, so with one set the tiles/chart/table read the
  campaign rollup restricted to it (the payload's `source_table` and a warning say so). A focus channel that the channel
  filter excludes is `outside_filter`, not "no campaign rows". With no `from`/`to` the range is the last 30 days ENDING AT
  THE NEWEST ROLLUP DAY (not today), and the export URLs carry the resolved range so a link keeps its meaning.
- **Health** (`desktop/health_data.py` + `static/health.{js,css}`, nav item 6, `GET /api/health`): "is this system actually working, and if not what do I do about it?" — the **detail view of the Today health strip**, not a second opinion. `assemble(flow_snapshot, today_payload)` is pure: it reuses `today_data.build_health()` for the five summary verdicts, `today_data.blind_inputs()` for "what this means for the numbers", and the Data Flow snapshot for the armed-trigger facts (webhook probe, Task Scheduler scan, ERP Desk's own loops) — that logic stays in `flow_data.py` only (L-030). `HealthStore` owns no engine, no probe and no thread: 5 s cache, `BUILD_WAIT_SECONDS` = 6 s so a request never parks behind a stuck build, and `assemble(None, None)` still returns a complete, honest payload. Cards: PostgreSQL, webhook, ClickUp, DeepSeek, SMTP, **one per scheduled job** (plus any extra task really registered on this machine), the three ERP Desk loops and the Script Center — each with state + Data Flow word, why, last checked / last succeeded, the real consequence and a copyable fix hint (`kind` = command / config / click / owner). Configuration is reported as `set` / `not set` **only**: `build_config()` never reads a value into the payload, and both `tests/health_scenarios.py` (sentinel values) and smoke check 4 (this machine's real `.env`) prove it. The page calls no state-changing endpoint and says so in its hero. With PostgreSQL down the database card explains it and every other card still renders; with no flow snapshot at all the headline still surfaces a red card instead of a grey "Still checking". The PostgreSQL card's `where` text is built from `erp.config.DB_NAME/DB_HOST/DB_PORT` (never hardcoded, L-085); `GET /api/health` accepts only `fresh` and refuses any other query parameter NAME with a `400` naming it, checked in `server.py` before `HealthStore`'s cache (L-098); an unscheduled job's card never shows a "last succeeded" time borrowed from a Data Flow node another trigger (e.g. ERP Desk's own in-app auto-digest) could have advanced; and a card that degrades to "unavailable" (its own check crashed, or no verdict yet) keeps that dependency's own fix hints instead of only "press Refresh". `HealthStore` itself opens no connection, but the Today payload it reads is the SAME shared `TodayStore`, whose own on-demand build can open one when its few-second cache has expired - the docs say "opens no database connection **of its own**" for exactly this reason.
- **Data Flow** — a **2D | 3D** switch (3D default, choice kept in `localStorage`; 2D is also the automatic fallback when WebGL2 is missing, the GPU is too slow or reduced-motion is on). 2D: hand-drawn SVG paths + Canvas particles (`getPointAtLength`) + CSS keyframes (`static/flow.js`, which also owns the switch and hands the 3D module its data, drawer and tooltip builders). 3D: WebGL2 scene with bloom / fog / reflective floor / cinematic tour (`static/flow3d/{shaders,engine,flow3d}.js`, `static/flow3d.css`); mounted only while the page is visible and fully disposed when you leave. Structure is declared in
  `desktop/flow_definition.py` (`NODES`, `EDGES` incl. `armed_by`, `LANES`, `TRACE`, `IGNORE`); live numbers/health in
  `desktop/flow_data.py`. Every automation claim is verified (Task Scheduler is queried; an edge is "live" only if its own trigger is armed).
  A **map-sync check** scans the project's `.py` files and shows "Map is in sync" or a card listing unmapped scripts,
  stale nodes and inconsistent edges. **The map does not discover new features by itself — see §12.**
- Shortcut/icon: `install_erp_desktop.bat` (Desktop + Start Menu "ERP Desk"; `/uninstall` removes).

## 8. Configuration (`.env`, names only)

| Variable | Purpose | Default / note |
|---|---|---|
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | PostgreSQL connection (role `erp_app`) | 127.0.0.1 / 5432 / erp_support / erp_app |
| `CLICKUP_API_TOKEN`, `CLICKUP_LIST_ID` | ClickUp; either empty → mock tasks (`MOCK-…`) | workspace "CS zone", space "Team Space", list "Leads - Sales" |
| `DEEPSEEK_API_KEY`, `DEEPSEEK_API_URL` | AI; empty key → heuristic fallback | `https://api.deepseek.com` |
| `LEAD_SLA_HOURS` | Lead SLA in business hours | 5 |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `ALERT_EMAIL_FROM`, `ALERT_EMAIL_TO` | Email alerts / digest; empty host → silently skipped | **not configured yet** |
| `DESKTOP_PORT`, `DESKTOP_STREAMLIT_PORT` | ERP Desk ports (fall back to any free port) | 47650 / 47651 |
| `DESKTOP_REFRESH_SECONDS` | Live report re-query interval (min 3) | 10 |
| `DESKTOP_AUTO_DIGEST` | Generate today's digest once per day inside ERP Desk (1 AI call, never emails) | on |
| `DB_CONNECT_TIMEOUT` | Seconds a NEW database connection may take (libpq `connect_timeout` in `erp/db.py`, shared by every script); a hung, not refused, PostgreSQL then raises instead of blocking forever. Queries/writes unaffected | 5 |
| `MARKETING_SCHEMA` | Schema the marketing pipeline reads/writes (`erp/marketing/`); the scale verification points this at a throwaway `perf_*` schema instead | public |
| `MARKETING_BATCH_SIZE` | Records per transaction while ingesting a marketing/channel export (min 100) | 5000 |
| `MARKETING_PARTITIONS_AHEAD` | Months of `interaction_fact` partitions the rollup job pre-creates beyond the current month (0-60) | 3 |
| `MARKETING_RETENTION_MONTHS` | A monthly fact partition entirely older than this is REPORTED as a retention candidate (never dropped by the job) | 24 |
| `POWERBI_DB_USER`, `POWERBI_DB_PASSWORD` | Reference only; used by Power BI, not the app | — |

## 9. Ports and launchers

| Port | Service | Started by |
|---|---|---|
| 5432 | PostgreSQL | Windows service |
| 8000 | Lead webhook | `run_webhook.bat` |
| 8502 | Script Center (standalone) | `run_script_center.bat` |
| 47650 / 47651 | ERP Desk shell + live report / embedded Script Center | ERP Desk shortcut, `run_erp_desktop.bat/.vbs`, debug: `run_erp_desktop_debug.bat`, stop: `stop_erp_desktop.bat` |

Other launchers: `run_smoke.bat` (smoke gate; pauses only when double-clicked, `SMOKE_NO_PAUSE=1` disables that, exits with the real code), `run_daily_check.bat`, `run_daily_digest.bat` (emails; no `pause`, Task-Scheduler safe, exits with real code), `setup_env.bat`.

Logs: `erp_desktop.log`, `erp_desktop_streamlit.log`, `script_center.log` (JSON lines), `daily_digest.log`.

Test a lead end to end:
```powershell
Invoke-RestMethod http://127.0.0.1:8000/webhooks/leads -Method Post -ContentType application/json `
  -Body (@{full_name="Jane Doe"; email="jane@example.com"; company="Acme"; source="landing_page"} | ConvertTo-Json)
```

## 10. Automation status — honest snapshot (2026-09-20)

| Item | State |
|---|---|
| Webhook | **Manual** — only receives leads while `run_webhook.bat` is running |
| ERP Desk live refresh + once-a-day auto digest | **Running automatically while the app is open** |
| Windows Task Scheduler | **No tasks belong to this project yet** |
| `clickup_pull`, `weekly_report`, `daily_digest` (emailing), `daily_check` | Recommended to schedule; not scheduled |
| Email | Not configured (`SMTP_*` empty) → alerts/digests are logged, not sent |
| ClickUp | Real (token + list configured); 11 real tasks created for the demo leads |
| Comments back from ClickUp | 0 (no rep has commented yet) |

Suggested schedules (run in PowerShell, adjust times):
```powershell
schtasks /create /tn "ERP Comment Pull"  /sc minute /mo 15 /tr "cmd /c cd /d C:\Users\AKIBA\Desktop\python && venv\Scripts\python.exe -m erp.clickup_pull"
schtasks /create /tn "ERP Daily Digest"  /sc daily /st 08:00 /tr "C:\Users\AKIBA\Desktop\python\run_daily_digest.bat"
schtasks /create /tn "ERP Weekly Report" /sc weekly /d MON /st 08:30 /tr "cmd /c cd /d C:\Users\AKIBA\Desktop\python && venv\Scripts\python.exe -m erp.weekly_report"
```
(`schtasks` has no start-in setting, so either use the `.bat` wrappers or `cmd /c cd /d … &&` as above — the scripts rely on the project root as working directory for `python -m` imports and relative files.
The Data Flow page recognises these tasks by their command line, so keep the script names in the command.)

## 11. Design language (all UIs)

Palette: ink `#1c1d22`, dim `#6b6f76`, blue `#3452eb`, coral `#ff5a36`, green `#12b886`, violet `#7c5cff`, amber `#f2b705`,
red `#e0393e`, background `#f5f3ef`, surface `#ffffff`, line `#e7e2d8`.
Font: **Montserrat** on every SCREEN people read (headings, body, labels, KPI numbers, buttons, chart text; weights
400 body / 500 labels / 600 headings / 700 big numbers - nothing asks Google Fonts for a weight no CSS rule paints, because the
charts wait for every requested weight to load), from Google Fonts (`display=swap`) with the fallback stack `'Segoe UI',
system-ui, sans-serif`, so the pages still look right offline. A system monospace (`Consolas, 'Cascadia Mono'`) is kept ONLY for
literal code: the Script Center editor (that one text area, not every text area), file paths and `<code>`, log / terminal / JSON
blocks. **There is no image output any more**: matplotlib cannot load a web font, and the one surface that rasterised one
(`erp/task_timeline_chart.py` → `task_timeline.png`) was removed on 2026-09-23; its matplotlib font helper
(`apply_matplotlib_style()`, "Montserrat when installed, else Segoe UI, else the default, logged once" - lesson L-122)
was removed from `erp/typography.py` on 2026-09-24 after a full cycle with no caller. Numerals: running prose uses Montserrat's default (proportional)
figures, because its tabular "1" has a foot serif that makes "11" read as "1 1"; `font-variant-numeric: tabular-nums` is
switched on for tables, chart axes and **every number that counts up, is redrawn by a poll, or stands beside other numbers**
(the KPI values on Today / Reporting / Leads, the Data Flow tallies, the funnel counts) - Montserrat's "1" is about half the
width of its "4", so a moving proportional number resizes its own tile on every frame. `tests/smoke.py` check 6 holds that
list, the painted-weight rule, and the Streamlit `--theme.font` value parsed by Streamlit's own parser.
**Where the font is defined (change it in these places only):** ERP Desk = the CSS variables `--font` / `--code` in
`desktop/static/shell.css` plus the `<link>` in `desktop/static/shell.html` and the weight list in the `shell.js` font gate
(charts read the variable through `Desk.font`);
everything rendered by Python = `erp/typography.py` (Script Center, the Streamlit `--theme.*` flags used by
`desktop/launcher.py`, e-mail HTML, matplotlib); `run_script_center.bat` carries a literal copy of the
`--theme.*` font flags (a `.bat` cannot import Python). `tests/smoke.py` fails if the old fonts reappear or those copies disagree.
History: before 2026-09-21 the design language used Fraunces (headings), Public Sans (body) and IBM Plex Mono (labels).
Motifs: rounded soft cards, count-up KPI tiles, animated gradient insight banner, scroll-reveal, dot-grids.
All animation honours `prefers-reduced-motion`.

## 12. How this project is developed (workflow)

New features are built with the saved workflow `.claude/workflows/build-feature.js` (multi-agent, run from Claude Code):

1. **Builder** designs + implements, then runs what it built.
2. **Reviewer** (independent) actually starts/tests it and must not trust the Builder's summary.
3. **Auditor** (independent) hunts security, error-handling and requirement gaps.
4. Confirmed **blocking/major** findings go back to the Builder; up to 2 rounds. Failed agents are retried once; a missing review is never treated as approval.

Run it by asking for a feature, or explicitly: `Workflow({scriptPath: ".claude/workflows/build-feature.js", args: {requirement: "…"}})`
(lookup by `name` did not work in this environment; use `scriptPath`).

**Keeping the Data Flow map true:** any feature that adds/changes a script, table, integration, report or trigger must update
`desktop/flow_definition.py`; the workflow enforces this (a miss is a *major* review issue) and the page's sync card is the safety net.
After editing that file, **restart ERP Desk** (it is read once at start-up).

**Merge gate (added 2026-09-20).** `venv\Scripts\python.exe -m tests.smoke` (or `run_smoke.bat`) is the check the conductor runs before every merge: read-only, **9** rows PASS/FAIL (imports, DB + views, Data Flow map sync, ERP Desk pages on a spare port, secrets + stray files in `git diff main` and untracked files, agent-system files + typography, nothing left behind, branch discipline, the gate's own self-test). It takes **about 22 s on an idle machine and 45-55 s cold** (I/O-bound: it re-reads the whole tree, imports 26 modules and builds a scratch git repo) — never document a single number for it without saying which of the two you measured; `--timeout` (default 120 s) is the real limit. Exit 0 = green; 1 = failed; 2 = timed out; 3 = `--only` subset. It is a developer tool and deliberately NOT a node on the Data Flow map (`tests/` is excluded from the script scan). Details: README section "Smoke check".

**Agent system (added 2026-09-20).** `Law.md` (renamed from CLAUDE.md on 2026-09-23) + `.claude/AUTONOMY.md` define the conductor loop and authority levels; `.claude/agents/erp-{builder,reviewer,auditor}.md` are the role playbooks (loaded by the workflow via a read-first instruction); `.claude/lessons.md` is append-only shared memory that every agent reads and the Scribe step extends; `.claude/journal.md` logs each cycle; `.claude/backlog.md` queues ideas. Test workflow plumbing cheaply with `args: {smoke: true}`. Workflow files must stay LF (`.gitattributes`).

## 13. Security model and open security to-dos

Implemented: least-privilege DB roles; Power BI role sees views only; parameterised SQL everywhere (`text()` with bound params, `CAST(... AS jsonb)`);
Script Center editor writes only to catalogued `.py` files inside the project (resolved-path check); subprocesses use argument lists (no shell);
mindmap payload escaped against `</script>` injection; ERP Desk binds to `127.0.0.1` only, token on every state-changing endpoint,
Host-header and static-path checks, all DB/log-derived text HTML-escaped; `.env` and runtime data are gitignored; no secret values in the API or logs.

**Action needed:** rotate every credential that was ever pasted into a chat, ticket or shared document, and keep real values only in `.env` (git-ignored).

Before exposing anything beyond localhost, see the hardening checklist discussed earlier: authenticate the webhook (shared secret/HMAC) and the dashboards,
put TLS in front, rate-limit, never open port 5432 to the internet, back up (`pg_dump`) and patch, keep secrets in a secrets manager.

## 14. Gotchas we already paid for (do not repeat)

- **SQLAlchemy `text()`**: `:param::jsonb` breaks — write `CAST(:param AS jsonb)`.
- **psql variables** (`:pw`, `:'pw'`) are **not** substituted inside `DO $$ … $$`; use `\gexec` at top level. Special characters in passwords: set directly with `ALTER ROLE` if quoting misbehaves.
- **`erp_app` cannot create roles** — run `db/sql/06_powerbi_readonly.sql` as `postgres`.
- **Power BI**: Python visuals need **Import** mode (DirectQuery gives the opaque `ServiceErrorToClientError`); never mix fields from two unrelated tables in one visual; point *Options → Python scripting* at `venv\Scripts` (folder containing `python.exe`) and restart; the venv needs `pandas` + `matplotlib`.
- **A machine-wide `DEEPSEEK_API_KEY`** overrode `.env` once — `load_dotenv(override=True)` fixes it.
- **Windows console is cp1252** — scripts that print non-ASCII call `sys.stdout.reconfigure(encoding="utf-8")`.
- **Streamlit `st.markdown(unsafe_allow_html=True)`**: 4+ leading spaces turn HTML into a code block — build HTML strings without indentation.
- **Streamlit theme**: native widgets follow the OS theme unless launched via the `.bat` flags; custom sections pin a light background.
- **Windows asyncio noise** (`ConnectionResetError … _ProactorBasePipeTransport`, `Accept failed on a socket`, `Task exception was never retrieved`) shows up in logs when sockets reset at shutdown; the Data Flow health check ignores these patterns (`_ASYNCIO_NOISE` in `desktop/flow_data.py`).
- **Never call `logging.basicConfig` at import time in a module other processes import.** `erp/daily_digest.py` did, so the Script Center's Streamlit server wrote its whole log into `daily_digest.log` and the digest node turned red for no reason. It now sets up logging only in `_setup_logging()` when run as a job (`__main__`).
- **Browser tool / headless testing**: the hidden browser pane doesn't fire `requestAnimationFrame` or reliably create WebGL — verify animation/3D in a real window or headless Edge over DevTools (`msedge --headless=new --remote-debugging-port=…`, add `--use-angle=swiftshader --enable-unsafe-swiftshader --ignore-gpu-blocklist` only if there is no GPU; software rendering is far slower than real fps).
- **PowerShell tool guard**: a `Remove-Item` on a path built from `$env:TEMP` inside a multi-statement command was blocked ("system path"); delete scratch files with a separate, simple command instead.
- **Workflow runs can be cut off by the account's usage limit** (agents fail with "session limit"). Resume with the same `scriptPath` + `resumeFromRunId` (finished agents are cached), or relaunch and tell the new Builder to *continue the partial work in the tree* rather than start over. Always check `git status` and stray processes/ports (47650/47651) after an interrupted run.
- Scheduled `.bat` files must not end with `pause` (blocks Task Scheduler forever).

## 15. Known limitations / backlog

- Webhook and most jobs are not scheduled yet (§10); SMTP not configured → no real emails sent.
- ClickUp `clickup_user_id` exists only for the 2 synced reps; more reps must be invited to the workspace, then run `erp.sync_employees`.
- In-app digest regeneration overwrites `daily_digest_latest.json` (shows "not emailed" even if the scheduled run emailed earlier that day).
- Data Flow: node health functions and per-node stats are hand-written; only `.py` files are scanned by the sync check; a script counts as "mapped" if any node declares it (correctness of the description is not checked);
  webhook "armed" test is only `GET :8000/health`; "Open in Script Center" copies the path rather than deep-linking.
- Reporting/Data Flow send all leads to the browser with no `LIMIT` (fine for 11, revisit at thousands). The Leads page pages on the server, but its reply statistics read at most `REPLY_ROWS_MAX` (20,000) replies and the CSV at most `EXPORT_MAX_ROWS` (50,000) rows (both say so when cut).
- Leads page: replies are known only through `lead_updates` (ClickUp comments pulled by `erp.clickup_pull`), so with the pull job unscheduled every lead looks unanswered (the page says so in an amber note and on the on-time tile); "on time" counts any comment, including one by someone who is not the rep; a reply stamped before its lead arrived counts as on time but is flagged and left out of the response-time statistics; an already-pooled connection that freezes mid-life is now bounded by `erp.db.read_connect()`'s wall-clock checkout timeout plus `erp.db.read_engine`'s libpq keepalives / `tcp_user_timeout` (L-091, closed 2026-09-24 - see the Today row below), not only by the client abort and the build-slot limit.
- Script Center subprocess timeout kills only the direct child (no scripts spawn children today).
- Repo housekeeping: `processed_orders.csv`, `summary_report.json` are regenerated demo outputs (uncommitted, safe to delete);
  empty stray files `App`, `cd`, `python` in the repo root are tracked but useless.
- Ticket module has schema + SLA but no live data or UI beyond the CSKH dashboard tab.
- **Marketing/channel interaction data is Phases 1 + 4 only** (schema, pipeline, rollup + partition maintenance, scale
  verification, see `docs/marketing-data-architecture.md`): the connectors are the generic flat-file/CSV reader and two strict
  ones for the owner's real exports (Phase 2's real API connector needs the owner's credentials), and the ERP Desk Channels page (Phase 3) shows a setup card until the owner installs db/sql/10 and db/sql/09 (the currency column and the rollup tables are not in the real database yet; `interaction_fact` there has 0 rows). Known limits of the real-file work: the duplicate-key occurrence index is a file position (stable only while the export order is), derived revenue is a stated derivation that the page labels per channel but cannot mark per row, the chart draws counts only (no per-currency spend series), and the generic reader's `to_date` still tries day-first before month-first. Neither the
  ingest nor the rollup refresh is scheduled: the Data Flow map shows ingest as **manual** and the rollup as
  "recommended, not set up", honestly, since nothing arms them today (Task Scheduler is the owner's). The rollup job
  only REPORTS partitions past retention; dropping or detaching one is always the owner's decision.
- **Data Flow 3D (merged into `main`, see §17):** built on a self-written WebGL2 engine, **not Three.js** (nothing third-party was downloaded — a deliberate trade: zero dependencies and fully offline, but *we* now maintain the engine). Node labels are HTML, so text is not occluded by other slabs; needs WebGL2 and a reasonably modern GPU (measured 40-63 fps on an Intel Iris Plus in headless Edge; it steps down by itself when slower, and hands over to 2D if it stays under ~10 fps at minimum quality; pure software WebGL runs at only a few fps); on a phone-width window the whole scene fits the screen so labels are tiny until you tap a node; touch input is basic (drag orbit, pinch zoom); the intro fly-in waits until the stage is scrolled into view (max 3.5 s); the tour caption can cover a node label when the target sits at the bottom left. Verified only in headless Edge on one machine, not in the real app window on other hardware.
- **GitHub (public)** holds a sanitised snapshot of this project, not its full local history. Keep real webhook URLs and tokens out of every published file, and revoke any token that was ever committed.
- The branch `feature/dataflow-3d` is merged and still exists (safe to delete: `git branch -d feature/dataflow-3d`).
- Ideas not built: webhook authentication, `.bat/.vbs` scan in the map check, WebGL/Three.js-based post-processing upgrades or a sound layer for Data Flow, Vietnamese version of docs.

## 16. Git history (newest first)

| Commit | What |
|---|---|
| `b41f8b5` | Merge `feature/dataflow-3d` into `main` |
| `f018519` | Cinematic 3D mode for Data Flow (self-written WebGL2 engine, 2D fallback, tour) |
| `801ac2c` | `daily_digest.py` no longer hijacks other processes' logging; wider asyncio-noise filter |
| `fd0a81f` | `PROJECT_NOTES.md` hand-over document |
| `0191895` | Data Flow page + self-checking map, workflow hardening, README |
| `c9e03fc` | Remove obsolete `data_processing_workflow.py` demo |
| `f892ddc` | ERP Desk (local desktop app) with live report and Script Center |
| `0b617dd` | Script Center, emailer, daily digest, `build-feature` workflow |
| `588df60` | Plotly HTML report generator |
| `64a282d` | ERP CSKH / Lead-to-Sale system, English translation of the whole project |

## 17. Decision log (why things are the way they are)

| Decision | Why | Consequence |
|---|---|---|
| **ERP Desk is the only user interface** (2026-09-23): `erp/html_report.py` + `erp_report.html`, `dashboard/streamlit_app.py` + `run_dashboard.bat` (:8501) and `erp/task_timeline_chart.py` + `task_timeline.png` were deleted | Owner's explicit scope cut - one surface to keep good instead of four that drift apart | `load_all()` / `build_insight()` moved unchanged into `desktop/report_data.py` (the `/api/report/data` payload is byte-identical); the Data Flow map lost the `html_report`, `cskh_dashboard` and `timeline_chart` nodes with their three edges; port 8501 is free; `apply_matplotlib_style()` had no caller left (removed from `erp/typography.py` on 2026-09-24 after a full cycle with no caller). The e-mail jobs (`daily_digest`, `weekly_report`, `emailer`) and the Script Center were explicitly kept |
| **Montserrat** replaced Fraunces / Public Sans / IBM Plex Mono in every UI (2026-09-21) | Owner's explicit request for one consistent font | Wider text: small labels were re-tuned; Plotly must draw only after the font is loaded (its text-measure cache); matplotlib PNGs use Montserrat only if it is installed (§11; task_timeline.png is now untracked generated output) |
| **The Channels page reads only the rollup tables** (2026-09-25) | Phase 4 built them so a report costs days x channels, not events; a report that quietly fell back to `interaction_fact` would be fast today and unusable at scale | `desktop/channels_data.py` names its three lookups; an EXPLAIN check and a scenario assert no statement mentions the fact or landing table. In today's database (rollup not installed) it shows a setup card, never a zero |
| Whole project in **English** (code, SQL, docs, AI prompts) | Owner's explicit request | AI notes/reports are now generated in English; Vietnamese docs not maintained |
| Desktop app = **local web app in an Edge `--app` window**, no Electron / no `.exe` | Owner's explicit architecture choice; nothing to build or sign | Needs Edge/Chrome installed; single-instance + cleanup handled by `desktop/launcher.py` |
| Features are built with the **multi-agent `build-feature` workflow** (§12) | Owner wants to state a goal once and receive only an independently verified result | Costs many tokens; can be cut off by usage limits (§14) |
| **Experiments go on a branch**, merged with `--no-ff` once the owner approves | Keeps `main` always working; the 3D Data Flow was developed this way | Branch history stays visible in `git log --graph` |
| Data Flow tells the **truth** about automation (it counts the armed stages live; today the webhook is offline and no scheduled task exists, so most stages show as not running) | A pretty map that claims things run when they do not is worse than none | Some nodes look "dormant/dashed" until jobs are actually scheduled (§10) |
| **`tests/smoke.py` is the merge gate** (read-only, in-process server on a spare port, exit code 0 only if all 7 checks pass) | The loop must not merge on a builder's word; a cheap deterministic check catches broken imports, map drift, secrets, CRLF-damaged workflow files and leftovers | A red gate blocks the merge; owner's dirty `processed_orders.csv` / `summary_report.json` are tolerated only while uncommitted and unstaged |
| Data Flow map is **declared by hand** + a sync check | Real data movement cannot be inferred reliably from code | New features must update `desktop/flow_definition.py` (workflow enforces it) |
| Data Flow 3D on a **self-written WebGL2 engine** instead of Three.js | No download/npm/build; fully offline; first draft already worked | We own the engine (context loss, leaks and performance were tested explicitly) |
| In-app daily digest **never sends email** | Avoid duplicate mail when the app is opened; the Task Scheduler `.bat` is the emailing path | `daily_digest_latest.json` may say "not emailed" after an in-app regeneration |
| Removed `data_processing_workflow.py` and the old sample employees/tickets | Owner: not important / wanted real data only | `processed_orders.csv`, `summary_report.json` are orphaned outputs (§15) |
| Only 2 real Sales reps (synced from ClickUp) drive lead assignment | Sample reps were deactivated then deleted | Round-robin alternates between those two people |
| **Autonomous improvement system** (conductor + Builder/Reviewer/Auditor playbooks, `.claude/lessons.md`, journal, backlog, kill switch `.claude/PAUSE`) | Owner wants the project to keep improving with only occasional check-ins, without risking `main` | All agent work on `agent/*` branches, `stable/*` tag before every merge, no GitHub push without approval. Charter: `.claude/AUTONOMY.md`; "24/7" only via approved scheduled wake-ups (§12) |
| **Today** is the landing page and reads health from the **Data Flow snapshot** instead of re-checking things | Report viewers want one honest sentence; the armed-trigger logic (webhook probe, Task Scheduler scan) must exist in exactly one place (L-030). A lead counts as past SLA only while no rep note is on record | Today shares the `live_report` node in the map; "awaiting first reply" depends on `erp.clickup_pull` having run; the feed is on demand (5 s cache), no thread |
| Today's headline is **downgraded, not hidden, when its inputs are blind**; queries are **time-boxed** (`SET LOCAL statement_timeout` 4 s, lock 2 s, 5 s total) and the shared engine got `connect_timeout` 5 s | An "All good" about data that cannot arrive (webhook down, replies never pulled) misleads viewers (L-084); a hung database must show STALE / "unavailable" instead of a spinning page (L-083). Only the connect timeout touches other scripts | A connection that takes longer than `DB_CONNECT_TIMEOUT` (e.g. a heavily loaded remote DB) now fails instead of waiting; raise it in `.env` |
| The read-only ERP Desk feeds (Today, Leads, Sources & cohorts, Reporting, Data Flow) read through a separate **`erp.db.read_engine`** (libpq keepalives + `tcp_user_timeout`) via **`erp.db.read_connect()`** (a wall-clock checkout bound, 8 s, capped at `READ_CHECKOUT_MAX_INFLIGHT`=8 in-flight checkouts) instead of the plain shared `engine` (L-091, closed 2026-09-24; self-healing gap closed in a second audit pass the same day) | `connect_timeout` only bounds OPENING a fresh connection; an ALREADY-POOLED connection that freezes mid-life (the peer's OS stays up, its application stops answering) trips neither `connect_timeout` nor a keepalive, and `pool_pre_ping`'s own liveness probe runs before any `SET LOCAL statement_timeout` exists to bound it, so it could park a server thread until the client aborted. A first fix bounded the checkout with a fixed-size semaphore, but that semaphore permanently loses one permit to every genuinely-stuck checkout thread - after enough of them, no NEW checkout could ever get through it again to prove the database had recovered, turning a temporary outage into a restart-only one | Writes and the pipeline scripts (`erp/leads.py`, `clickup_pull.py`, `daily_check.py`, `daily_digest.py`, `sync_employees.py`, `weekly_report.py`) are unchanged, still on the plain `engine`. Proven against a TCP relay that forwards one good query then goes silent - a genuinely POOLED, previously-good connection, not a fresh connect to a dead port (`tests/today_scenarios.py`'s `_FrozenProxy`) - and a normal request is unaffected (still well under the client's fetch-abort timeout). Once the queue is full, a rate-capped background probe (`erp.db._maybe_start_recovery_probe`, bounded separately at `READ_CHECKOUT_PROBE_MAX_STUCK`=4) keeps trying a connection outside the exhausted pool; the moment one succeeds, a fresh semaphore is swapped in for all future checkouts - proven live end to end (`/api/today`, `/api/leads/analysis`, `/api/health` all fail fast then self-heal within ~1 s of the database answering again, no server restart) as well as in `tests/today_scenarios.py` |
| **Leads** is its own page with a **server-side** filter API and a CSV export, and Today's late-lead SQL was moved into shared helpers | Analysts need one filter set to drive every chart, the table and the file, plus stage conversion and reply-time statistics that a browser-side snapshot (Reporting) cannot give; two pages that each spell out "past SLA" would drift (L-085) | `desktop/today_data.py` now exports `lead_awaiting_sql` / `lead_past_sla_sql` / `REPLY_AT_SQL`, built into its queries as before (same rows); changing the rule there changes both pages. The Data Flow map is unchanged structurally (the `live_report` node gained `desktop/leads_data.py`) |
| Today's numbers are **clickable**: one drawer, fed by the **same** `/api/today` payload | A report viewer who sees "10 leads past SLA" needs to know WHO, without learning the schema or opening another page; a second endpoint would mean a second poller, a second cache and two versions of the truth | `desktop/today_data.py` also reads `lead_ai_analysis`, `lead_clickup_sync` and the newest `lead_updates` row for at most 12 rows per list (`DETAIL_ROWS`), last in `collect()` so a nearly-spent query budget drops the drawer rows rather than the tiles. The payload gained `details`, `since` and `tz`; nothing was renamed. A tile/clause only offers a click when its list really came back with rows |
| The five **SLA outcome names** live in one module, `desktop/sla_words.py`, imported by both pages | Today and Leads already shared the SQL (L-102) but each wrote its own English ("Past SLA now" vs "Leads past SLA now", "Awaiting first contact" vs "Awaiting first reply"); two names for one state is how a viewer learns to trust neither | Wording changes happen in one file. The Leads tile hints now quote the live `LEAD_SLA_HOURS` (they used to name no number), so `tests/leads_scenarios.py` asserts "the number is the live one" instead of "there is no number". The module is in the Data Flow `IGNORE` list: wording only, no data moves |
| **Lead sources + arrival cohorts are a TAB of the Leads page**, not a sixth navigation item, and they get their own read-only endpoint | It is the same set of leads under the same filters, so the filter bar, the Definitions drawer and the single poller must be shared - a second nav item would duplicate all three and invite the two pages to drift. A tab cannot reuse `/api/leads/analysis`, though: the per-source stage counts, the rep spread and the cohort ages are not in that payload, and bolting them on would make every Funnel & SLA poll pay for them | `desktop/sources_data.py` + `GET /api/leads/sources`; `leads.js` owns the tab state and points its one poll loop at whichever endpoint the visible tab needs; `Ctrl+6` was left free for the Health page, which took it on 2026-09-24. The `live_report` node of the Data Flow map gained `desktop/sources_data.py` |
| SALES REP / SOURCE filter pills scroll in their own one-row strip (`.fg-multi`/`.fg-scroll`) instead of wrapping the whole Leads filter bar; each strip claims a full row above ~1100px of content width and stays compact below it | Neither list has a fixed size - a busy team can have a dozen reps and as many sources - so letting them wrap internally made the bar grow to two or three rows and pushed content down unpredictably. A first version of the compact strip stayed compact at EVERY width, hiding most of a dozen-pill list even at 1920/1440 where there was plenty of room (audit finding, 2026-09-24) | `desktop/static/leads.{js,css}`, 2026-09-24; the label stays outside the scroller so it never scrolls out of view; a `@container ld (min-width: 1100px)` rule gives each strip its own row once there is no real space pressure, and a JS-driven edge fade-mask (`updateFade()`, kept accurate by a ResizeObserver + resize listener + 1 s poll, since not every viewport-size change is guaranteed to fire the first two) shows which side still has more pills instead of a hard clip. Each strip's scroll position and the focused pill both survive the innerHTML rebuild every filter change causes (keyed by `data-group`, L-100). Verified in headless Edge at 1920/1440/820/390 with a synthetic 8-rep/12-source set (`leads_data.read_options` monkeypatched, no DB write): at 1920/1440 all 8 reps and half the 12 sources are visible without scrolling (vs 1-2 of each before); every pill remains reachable by scroll and clickable at every width. The bar's own sticky-vs-static breakpoint stays at `max-height: 640px` (down from 860px) now that its height is bounded rather than growing with the data |
| A cohort curve stops at the age the cohort's **youngest** lead has reached, and a cohort with one age point is drawn as **points** | Otherwise the last points of a curve are divided by a cohort that has not had the time yet (the curve appears to fall), and "a trend" gets drawn through a single measurement. Today the database holds ONE cohort, so this is the normal case, not an edge case | `build_cohorts` computes `observed_age = min(age of the leads)`; `single_point` is in the payload and the smoke gate asserts `single_point == (len(points) < 2)`. The page prints one plain line saying why there is only one cohort and what the view needs |
| The selected cohort filters the **table** but not the **chart** | "Filtered" would leave one curve with nothing to compare it against, which is the whole point of a cohort chart | Stated on the page under the chart and in the Definitions drawer; in the cohort CSV the selection is a `selected_on_screen` column rather than a filter, so no parameter is silently ignored (L-098) |
| The **Health page reuses the Today page's verdicts** instead of probing anything itself, and the merge gate asserts their agreement in **one direction only** | Two health checks would drift the day one of them learned something the other did not (L-102), so the page is the *detail view* of the strip, not a second opinion. But the two pages answer different questions: this one counts every recommended job Task Scheduler does not have, while `today_data.blind_inputs()` only knows the webhook, ClickUp and the comment pull - so "Today says All good" does **not** imply "Health is green", and asserting it would turn the gate red on a correctly set-up machine | `health_data.assemble()` is pure and takes only the flow snapshot + the Today payload; `mark_attention()` maps each blind input to a card, so *Health all clear -> Today not blind* really holds and the gate asserts that direction (plus per-dependency equality). `tests/health_scenarios.py::_check_different_questions` pins down the legitimate divergence. "Last checked" comes from the snapshot's `refreshed_at` (when the checks ran), never `generated_at` (which only moves when the content changes) |
| **Currency is a column, money is never converted** (2026-09-25) | 1,000,000 VND is about 35 EUR: a sum across the owner's two files means nothing, and `interaction_fact` had no currency | `db/sql/10_marketing_currency.sql` (owner-run, additive); `currency` in both rollup keys; `money_cell()` on the Channels page; no exchange rates anywhere. The currency is deliberately not part of the dedupe identity (a restatement in another currency updates the row) |
| **Two different rows under one natural key are both kept and reported** (2026-09-25) | The generic dedupe (day, channel, campaign, creative, type) treats the second row of `data1`'s 6 pairs as a restatement and overwrites the first: silent data loss (it does not even look at adset or device) | `ad_performance` / `email_campaign` add the occurrence index to an exact external id; the pipeline reports kept and replaced collisions; the trade-off (the number is a file position) is stated in the docs |
| **A weekly row sits on its start day and the rollup has a `grain`** (2026-09-25) | A week's total drawn as one day is a false spike; counting "days with data" would read as an outage | `attrs.grain = 'week'`, rollup key `grain`, a separate per-week chart, coverage in weeks |
| The **"since yesterday"** line is computed server-side from the same queries and day rules, and stays **silent** when it cannot be honest | A viewer's first question is "what changed?"; answering it from a second query set would let the line and the tiles disagree (L-097), and a "0 replies" that really means "the pull job is not running" is worse than no line | `build_since()` drops a clause whose source is blind or whose query failed and prints a short note instead; with nothing on file from before today the whole line is omitted. The SLA comparison reuses the second-to-last point of the very 7-day series the sparkline draws |
