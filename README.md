# ERP Customer Support / Lead-to-Sale

Automation system: lead received from a form -> saved to PostgreSQL -> duplicate check ->
assigned to a Sales rep -> analyzed by AI (DeepSeek) -> ClickUp task created -> updates
synced back -> dashboard for tracking + AI-generated weekly report.

Also includes a Customer Support/IT Support ticket model (departments share a single `tickets` table).

## Directory structure

```
db/sql/                 Scripts to create the database, schema, sample data (run in order 00 -> 05)
erp/
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
desktop/                ERP Desk - the local "desktop app": launcher, shell server, live report page, Data Flow page (flow_definition.py = the map + its IGNORE list, flow_data.py = live numbers + the map sync check; static/flow.js = 2D map, static/flow3d/ = the 3D scene), icon + shortcut installer
dashboard/
  streamlit_app.py      Dashboard for support tickets, leads & sales, staff vs. ClickUp cross-check
  script_center.py      Script Center: catalog + mindmap + editor + test runner for every script here
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

$env:PGPASSWORD="ERP_APP_PASSWORD"
$psql = "C:\Program Files\PostgreSQL\18\bin\psql.exe"
& $psql -U erp_app -h 127.0.0.1 -p 5432 -d erp_support -f db/sql/01_schema.sql
& $psql -U erp_app -h 127.0.0.1 -p 5432 -d erp_support -f db/sql/02_seed_cskh.sql
& $psql -U erp_app -h 127.0.0.1 -p 5432 -d erp_support -f db/sql/03_leads_schema.sql
& $psql -U erp_app -h 127.0.0.1 -p 5432 -d erp_support -f db/sql/04_seed_sales.sql
& $psql -U erp_app -h 127.0.0.1 -p 5432 -d erp_support -f db/sql/05_leads_sla.sql
```

## Quick start (double-click)

- `run_webhook.bat` - starts the lead-receiving webhook (keep the window open, Ctrl+C to stop)
- `run_dashboard.bat` - opens the dashboard to view data
- `run_daily_check.bat` - syncs ClickUp + lists items needing attention (SLA breaches,
  unsynced leads, leads not yet analyzed by AI, leads with no recent update)
- `run_script_center.bat` - opens the Script Center (see below) at http://localhost:8502
- `run_daily_digest.bat` - generates + emails the daily digest once
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

**Dashboard** (default port 8501):
```bash
venv\Scripts\python.exe -m streamlit run dashboard/streamlit_app.py
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
Opens at http://localhost:8502 (separate from the main dashboard on 8501, so both
can run at once). `run_script_center.bat` also applies this tool's own brand theme
via `--theme.*` CLI flags - deliberately not via `.streamlit/config.toml`, so
`dashboard/streamlit_app.py` keeps its current look untouched.

The page is one continuous scroll of five full-width "slide" sections - Overview ->
Mindmap -> Script catalog -> Editor & test runner -> Logs & digest - each with its
own background treatment, a numbered divider, and a scroll-reveal entrance animation
(same `IntersectionObserver` technique as `erp/html_report.py`), so it reads as one
connected system instead of stacked widgets:

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
  for the long-running servers (`erp/webhook_app.py`, `dashboard/streamlit_app.py`,
  `dashboard/script_center.py`), and a syntax check for everything else.
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

## ERP Desk - the desktop app (live Reporting + Management in one window)

A web app that runs locally and looks like a native Windows app: **no Electron, no .exe**.
The launcher starts everything, waits until it is healthy, then opens Microsoft Edge (Chrome
if Edge is missing) in `--app` mode - no tabs, no address bar - with its own profile
(`.erp_desktop/browser_profile`), so it gets its own window, taskbar entry and icon.

- **Reporting** (default page) - the live version of `erp/html_report.py`: animated KPI tiles,
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
- **Management** - the existing Script Center (`dashboard/script_center.py`), unchanged, running
  on its own port and embedded in the shell. `Ctrl+1` / `Ctrl+2` / `Ctrl+3` switch pages.
- **Data Flow** - a large animated map of how data really moves through the whole system and what
  starts each step (see the next section).

### Data Flow page - where data goes and how it is automated

The page has two views of the same map, chosen with the **2D | 3D** switch in the stage's title bar: a cinematic
**3D scene** (the default on this branch) and the classic flat **2D diagram**. Everything below - triggers, health,
live numbers, the map check, the drawer, the legend, the coverage cards - is the same in both; the "3D mode" item at
the end of the list below covers what is specific to the scene.

The Script Center mindmap groups scripts by skill; it does not show data moving. The **Data Flow** page
(third item in the navigation, `Ctrl+3`) does: the real pipeline as glowing nodes and edges in four lanes -
**Intake** (lead form -> `erp/webhook_app.py`, tickets) -> **Storage & Logic** (`erp/leads.py` dedup +
round-robin, PostgreSQL, the reporting views) -> **AI & Integrations** (DeepSeek, ClickUp task creation, the
Sales rep in ClickUp, `erp/clickup_pull.py`, `erp/sync_employees.py`) -> **Monitoring & Reporting**
(`daily_check`, `daily_digest` + `emailer`, `weekly_report`, the digest files, the live Reporting page,
`html_report`, the task timeline chart, the CSKH dashboard, Script Center, Power BI). Dots travel along every edge in the direction the
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
  (`daily_digest_latest.json`, `erp_report.html`) do not belong in a node's `files`. If the check itself cannot run
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
`DESKTOP_REFRESH_SECONDS`, `DESKTOP_AUTO_DIGEST` (see `.env.example`).

Security: state-changing endpoints (refresh, generate digest, quit) are POST-only, need a per-launch
random token and a same-origin request, and accept no path or command input; unknown `Host` headers
are rejected. The standalone `erp/html_report.py`, `run_dashboard.bat`, `run_script_center.bat` and
`run_webhook.bat` work exactly as before.

## Important notes

- The standard flow for adding a real Sales rep: invite them to the ClickUp workspace first, then
  run `python -m erp.sync_employees` to automatically create/update them in the `users` table
  (no need to type in `clickup_user_id` by hand).
- The original seed Sales reps (Do Thi Lan, Vu Minh Tuan, Bui Thi Nga) have been set to
  `is_active = FALSE` now that real staff exist, so they're no longer picked by the
  round-robin lead assignment.
- `erp/config.py` reads `.env` with `override=True` so it takes precedence over any
  system environment variable already present (avoids mix-ups with another project's
  API key on the same machine).
