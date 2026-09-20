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
                                                ├─▶ dashboard/streamlit_app.py      (:8501, CSKH dashboard)
                                                ├─▶ erp/html_report.py              (standalone Plotly report)
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
| `.env` loaded with `override=True` | A machine-wide `DEEPSEEK_API_KEY` from another project once leaked in; the project's own `.env` must win |

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
dashboard/streamlit_app.py          CSKH dashboard (:8501)
dashboard/script_center.py          Script Center (:8502 standalone, :47651 inside ERP Desk)
desktop/                            ERP Desk: launcher, shell server, live report, Data Flow (see §7)
run_*.bat / *.vbs / install_*.bat   Launchers (see §9)
setup_env.bat                       Create venv + install requirements
README.md                           Quick start
PROJECT_NOTES.md                    This file
postwebhook.py, sql_change_webhook_demo.py   Learning demos (kept, marked "Learning Demos" in Script Center)
```

Runtime/generated files (gitignored): `.env`, `*.log`, `daily_digest_latest.json`,
`daily_digest_history.json`, `erp_report.html`, `.erp_desktop/` (instance file, Edge profile).

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
| `html_report.py` | Standalone animated Plotly report → `erp_report.html` | manual |
| `task_timeline_chart.py` | Per-employee task timeline → `task_timeline.png` | manual |

## 7. Front ends

**CSKH dashboard** (`dashboard/streamlit_app.py`, `run_dashboard.bat`, :8501): Pipeline funnel, Support Tickets,
Lead & Sale, Staff & ClickUp cross-check.

**Script Center** (`dashboard/script_center.py`, `run_script_center.bat`, :8502): single page of "slide" sections — overview,
vis-network mindmap (skill groups → scripts, click links to card + editor), script card grid, editor with
path-traversal guard, test runner (one-shot scripts run with timeout; servers only syntax-checked; DB/network scripts
labelled), structured log `script_center.log`, daily digest report card. Email-on-failure checkbox.

**ERP Desk** (`desktop/`): a local web app in a chrome-less Edge window.
- `launcher.py` — single-instance, starts everything, waits for health, opens the window, cleans up all child processes
  (Windows Job Object via `winutil.py`). Flags: `--no-window`, `--stop`, `--status`, `--console`.
- `server.py` — FastAPI on `127.0.0.1:47650`. Read endpoints: `/api/ping`, `/api/status`, `/api/report/data`, `/api/digest`, `/api/flow`.
  Token-protected POSTs: report/flow refresh, digest generate, heartbeat, window focus, quit. Host-header check, static path-traversal guard.
- Pages: **Reporting** (live, re-queries every `DESKTOP_REFRESH_SECONDS`, cross-filtering, daily briefing),
  **Management** (embeds Script Center), **Data Flow** (below). Shortcuts: `Ctrl+1/2/3`.
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
| `POWERBI_DB_USER`, `POWERBI_DB_PASSWORD` | Reference only; used by Power BI, not the app | — |

## 9. Ports and launchers

| Port | Service | Started by |
|---|---|---|
| 5432 | PostgreSQL | Windows service |
| 8000 | Lead webhook | `run_webhook.bat` |
| 8501 | CSKH dashboard | `run_dashboard.bat` |
| 8502 | Script Center (standalone) | `run_script_center.bat` |
| 47650 / 47651 | ERP Desk shell + live report / embedded Script Center | ERP Desk shortcut, `run_erp_desktop.bat/.vbs`, debug: `run_erp_desktop_debug.bat`, stop: `stop_erp_desktop.bat` |

Other launchers: `run_daily_check.bat`, `run_daily_digest.bat` (emails; no `pause`, Task-Scheduler safe, exits with real code), `setup_env.bat`.

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
Fonts (Google Fonts): **Fraunces** (headings/big numbers), **Public Sans** (body), **IBM Plex Mono** (labels/code).
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

## 13. Security model and open security to-dos

Implemented: least-privilege DB roles; Power BI role sees views only; parameterised SQL everywhere (`text()` with bound params, `CAST(... AS jsonb)`);
Script Center editor writes only to catalogued `.py` files inside the project (resolved-path check); subprocesses use argument lists (no shell);
mindmap payload escaped against `</script>` injection; ERP Desk binds to `127.0.0.1` only, token on every state-changing endpoint,
Host-header and static-path checks, all DB/log-derived text HTML-escaped; `.env` and runtime data are gitignored; no secret values in the API or logs.

**Housekeeping:** rotate any credential that has ever been pasted into a chat, ticket or log (ClickUp token, DeepSeek key, database passwords) and treat webhook URLs that contain tokens as secrets.

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
- Reporting/Data Flow send all leads to the browser with no `LIMIT` (fine for 11, revisit at thousands).
- Script Center subprocess timeout kills only the direct child (no scripts spawn children today).
- Repo housekeeping: `processed_orders.csv`, `summary_report.json` are regenerated demo outputs (uncommitted, safe to delete);
  empty stray files `App`, `cd`, `python` in the repo root are tracked but useless.
- Ticket module has schema + SLA but no live data or UI beyond the CSKH dashboard tab.
- **Data Flow 3D (merged into `main`, see §17):** built on a self-written WebGL2 engine, **not Three.js** (nothing third-party was downloaded — a deliberate trade: zero dependencies and fully offline, but *we* now maintain the engine). Node labels are HTML, so text is not occluded by other slabs; needs WebGL2 and a reasonably modern GPU (measured 40-63 fps on an Intel Iris Plus in headless Edge; it steps down by itself when slower, and hands over to 2D if it stays under ~10 fps at minimum quality; pure software WebGL runs at only a few fps); on a phone-width window the whole scene fits the screen so labels are tiny until you tap a node; touch input is basic (drag orbit, pinch zoom); the intro fly-in waits until the stage is scrolled into view (max 3.5 s); the tour caption can cover a node label when the target sits at the bottom left. Verified only in headless Edge on one machine, not in the real app window on other hardware.
- **This public repository is a sanitised snapshot** of a private working repository (real webhook URL, personal/other-project details and orphaned demo outputs are not included).
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
| Whole project in **English** (code, SQL, docs, AI prompts) | Owner's explicit request | AI notes/reports are now generated in English; Vietnamese docs not maintained |
| Desktop app = **local web app in an Edge `--app` window**, no Electron / no `.exe` | Owner's explicit architecture choice; nothing to build or sign | Needs Edge/Chrome installed; single-instance + cleanup handled by `desktop/launcher.py` |
| Features are built with the **multi-agent `build-feature` workflow** (§12) | Owner wants to state a goal once and receive only an independently verified result | Costs many tokens; can be cut off by usage limits (§14) |
| **Experiments go on a branch**, merged with `--no-ff` once the owner approves | Keeps `main` always working; the 3D Data Flow was developed this way | Branch history stays visible in `git log --graph` |
| Data Flow tells the **truth** about automation (2/14 automated, webhook offline, no scheduled tasks) | A pretty map that claims things run when they do not is worse than none | Some nodes look "dormant/dashed" until jobs are actually scheduled (§10) |
| Data Flow map is **declared by hand** + a sync check | Real data movement cannot be inferred reliably from code | New features must update `desktop/flow_definition.py` (workflow enforces it) |
| Data Flow 3D on a **self-written WebGL2 engine** instead of Three.js | No download/npm/build; fully offline; first draft already worked | We own the engine (context loss, leaks and performance were tested explicitly) |
| In-app daily digest **never sends email** | Avoid duplicate mail when the app is opened; the Task Scheduler `.bat` is the emailing path | `daily_digest_latest.json` may say "not emailed" after an in-app regeneration |
| Removed `data_processing_workflow.py` and the old sample employees/tickets | Owner: not important / wanted real data only | `processed_orders.csv`, `summary_report.json` are orphaned outputs (§15) |
| Only 2 real Sales reps (synced from ClickUp) drive lead assignment | Sample reps were deactivated then deleted | Round-robin alternates between those two people |
