"""
Script Center - a single-page, visually-led control room for every Python
script in this project. Five full-width "slide" sections you scroll through
(Overview -> Mindmap -> Catalog -> Editor & test runner -> Logs & digest),
each with its own background treatment and scroll-reveal entrance animation,
sharing one palette/typography so the page reads as one connected system
rather than five stacked widgets.

Functionally identical to the original: catalog scanning, the interactive
mindmap, the in-browser editor with its path-safety restriction, the test
runner (safe one-shot vs. server-script distinction), structured JSON-lines
logging, email-on-failure, and the daily digest panel all still work exactly
as before - only the presentation changed.

Run (on its own port, 8502):
    venv\\Scripts\\python.exe -m streamlit run dashboard/script_center.py --server.port 8502

Or just double-click run_script_center.bat, which also applies this tool's
brand theme via CLI flags (kept out of .streamlit/config.toml on purpose, so
the theme travels with this launch only). ERP Desk embeds this page as its
Management tab and starts it the same way on port 47651.
"""
import ast
import html
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(PROJECT_ROOT))  # allow "import erp" from the project root

from erp import emailer  # noqa: E402
from erp.typography import FONT_NAME, FONT_STACK, GOOGLE_FONTS_CSS_URL, streamlit_font_css  # noqa: E402

try:
    from erp.daily_digest import METRIC_LABELS as DIGEST_METRIC_LABELS  # noqa: E402
except Exception:
    DIGEST_METRIC_LABELS = {}  # keep the page working even if that module can't be imported

LOG_FILE = PROJECT_ROOT / "script_center.log"
DIGEST_LATEST_FILE = PROJECT_ROOT / "daily_digest_latest.json"

# ---------------------------------------------------------------------------
# Palette + fonts - the project's one design language (also in
# desktop/static/shell.css), so this tool reads as part of the same product:
# Montserrat for everything people read, a monospace only for literal code
# (the editor, file paths, logs); the tokens live in erp/typography.py.
# Native widget theming (buttons, selectboxes,
# dataframe, borders, radius) is applied separately via CLI --theme.* flags
# in run_script_center.bat, deliberately NOT via .streamlit/config.toml,
# so the theme belongs to this launch instead of to every Streamlit app
# started from this folder.
# ---------------------------------------------------------------------------
INK = "#1c1d22"
INK_DIM = "#6b6f76"
BLUE = "#3452eb"
CORAL = "#ff5a36"
GREEN = "#12b886"
VIOLET = "#7c5cff"
AMBER = "#f2b705"
RED = "#e0393e"
BG = "#f5f3ef"
SURFACE = "#ffffff"
LINE = "#e7e2d8"
OTHER = "#b6b0a1"

st.set_page_config(page_title="Script Center", page_icon=":material/hub:", layout="wide")

# ---------------------------------------------------------------------------
# Skill catalog: which directories to scan + categorization heuristics
# ---------------------------------------------------------------------------
SCAN_DIRS = ["erp", "dashboard"]  # + project root itself, handled separately

SKILL_COLORS = {
    "Lead-to-Sale": CORAL,
    "Support Tickets": BLUE,
    "Reporting & Dashboards": VIOLET,
    "Operations": GREEN,
    "Integrations": AMBER,
    "Other": OTHER,
}
SKILL_ORDER = list(SKILL_COLORS.keys())

# One small Material Symbol per skill group, used as the card grid's visual
# marker (kept distinct from Streamlit's own :material/ icon usage, since raw
# HTML cards need the webfont loaded explicitly - see inject_css()).
SKILL_ICONS = {
    "Lead-to-Sale": "bolt",
    "Support Tickets": "support_agent",
    "Reporting & Dashboards": "insights",
    "Operations": "settings_suggest",
    "Integrations": "hub",
    "Other": "folder_open",
}


def categorize_skill(rel_path: str) -> str:
    full = rel_path.lower()  # full relative path, so e.g. "dashboard/script_center.py" matches "dashboard"
    if any(k in full for k in ("lead", "clickup", "ai_client")):
        return "Lead-to-Sale"
    if "ticket" in full:
        return "Support Tickets"
    if any(k in full for k in ("dashboard", "report", "digest", "chart", "timeline")):
        return "Reporting & Dashboards"
    if any(k in full for k in ("sync_employees", "business_hours", "daily_check")):
        return "Operations"
    if any(k in full for k in ("webhook_app", "emailer")):
        return "Integrations"
    return "Other"


# ---------------------------------------------------------------------------
# One-shot / server script metadata (see README for the full list + reasons)
# ---------------------------------------------------------------------------
SERVER_SCRIPTS = {"erp/webhook_app.py", "dashboard/script_center.py"}

ONE_SHOT_SCRIPTS = {
    "erp/daily_check.py": {
        "module": "erp.daily_check",
        "risk": "db",
        "note": "Syncs ClickUp comments into the DB and updates sync timestamps - writes to the real database.",
    },
    "erp/weekly_report.py": {
        "module": "erp.weekly_report",
        "risk": "read",
        "note": "Read-only: only runs SELECT queries and calls the AI agent. No writes.",
    },
    "erp/sync_employees.py": {
        "module": "erp.sync_employees",
        "extra_args": ["--department", "SALES"],
        "risk": "db",
        "note": "Creates/updates rows in the users table from ClickUp workspace members - writes to the real database.",
    },
    "erp/clickup_pull.py": {
        "module": "erp.clickup_pull",
        "risk": "db",
        "note": "Inserts new rows into lead_updates and updates sync timestamps - writes to the real database.",
    },
    "erp/daily_digest.py": {
        "module": "erp.daily_digest",
        "risk": "local_file",
        "note": "Read-only DB queries; writes daily_digest_history.json / daily_digest_latest.json locally; "
                "emails the digest only if SMTP is configured in .env.",
    },
    "erp/emailer.py": {
        "module": "erp.emailer",
        "risk": "network",
        "note": "Sends one real test email only if SMTP_* is configured in .env (safe no-op otherwise).",
    },
}

RISK_STYLE = {
    "read": ("info", ":material/visibility:"),
    "local_file": ("info", ":material/description:"),
    "safe": ("success", ":material/check_circle:"),
    "db": ("warning", ":material/database:"),
    "network": ("warning", ":material/public:"),
}

# Metrics where a rise is a good sign - everything else in the digest is
# treated as "lower is better" (breaches, unsynced items, backlog). Used only
# to color the direction arrow on the digest report card; never invents a
# number that daily_digest.py itself didn't compute.
DIGEST_GOOD_WHEN_UP = {"total_leads", "avg_potential_score"}


# ---------------------------------------------------------------------------
# Script discovery (re-scanned on every run - the catalog is tiny, and we
# want edits/saves to show up immediately without a stale cache)
# ---------------------------------------------------------------------------
def scan_scripts() -> list[dict]:
    candidates = []
    for d in SCAN_DIRS:
        dpath = PROJECT_ROOT / d
        if dpath.is_dir():
            candidates.extend(sorted(dpath.glob("*.py")))
    candidates.extend(sorted(PROJECT_ROOT.glob("*.py")))

    scripts = []
    for abs_path in candidates:
        rel_path = abs_path.relative_to(PROJECT_ROOT).as_posix()
        try:
            source = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            source = ""

        purpose = "(no docstring)"
        try:
            doc = ast.get_docstring(ast.parse(source))
            if doc:
                first_para = doc.strip().split("\n\n")[0]
                purpose = " ".join(first_para.split())
        except SyntaxError:
            purpose = "(could not parse - syntax error in file)"

        try:
            mtime = datetime.fromtimestamp(abs_path.stat().st_mtime)
        except OSError:
            mtime = None

        line_count = source.count("\n") + (1 if source and not source.endswith("\n") else 0)

        scripts.append({
            "rel_path": rel_path,
            "abs_path": abs_path,
            "filename": abs_path.name,
            "purpose": purpose,
            "skill": categorize_skill(rel_path),
            "last_modified": mtime,
            "line_count": line_count,
        })
    return sorted(scripts, key=lambda s: (s["skill"] != "Other", SKILL_ORDER.index(s["skill"]), s["rel_path"]))


def resolve_safe_path(rel_path: str) -> Path | None:
    """Resolve rel_path against the project root and reject anything that
    escapes it (path traversal) or isn't an existing .py file we scanned."""
    candidate = PROJECT_ROOT / rel_path
    try:
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(PROJECT_ROOT.resolve())
    except (ValueError, OSError):
        return None
    if resolved.suffix != ".py" or not resolved.is_file():
        return None
    return resolved


# ---------------------------------------------------------------------------
# Structured logging - one JSON object per line in script_center.log
# ---------------------------------------------------------------------------
def log_event(script: str, action: str, status: str, duration_ms: int, error: str | None = None) -> None:
    entry = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "script": script,
        "action": action,
        "status": status,
        "duration_ms": duration_ms,
        "error": (error or "")[:2000],
    }
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass  # logging must never crash the app


def read_recent_logs(limit: int = 30, status_filter: str | None = None) -> list[dict]:
    if not LOG_FILE.exists():
        return []
    rows = []
    for line in LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    rows.reverse()  # file is append-only chronological -> newest first
    if status_filter and status_filter != "All":
        rows = [r for r in rows if r.get("status") == status_filter.lower()]
    return rows[:limit]


# ---------------------------------------------------------------------------
# Subprocess runner (test run / syntax check)
# ---------------------------------------------------------------------------
def build_command(rel_path: str, meta: dict) -> list[str]:
    if meta.get("module"):
        return [sys.executable, "-m", meta["module"], *meta.get("extra_args", [])]
    return [sys.executable, str(PROJECT_ROOT / rel_path)]


def run_subprocess(cmd: list[str], timeout: int = 30) -> dict:
    start = time.time()
    try:
        proc = subprocess.run(
            cmd, cwd=PROJECT_ROOT, capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", errors="replace",
        )
        duration_ms = int((time.time() - start) * 1000)
        return {
            "status": "ok" if proc.returncode == 0 else "error",
            "returncode": proc.returncode,
            "stdout": proc.stdout, "stderr": proc.stderr,
            "duration_ms": duration_ms,
        }
    except subprocess.TimeoutExpired as e:
        duration_ms = int((time.time() - start) * 1000)
        return {
            "status": "error", "returncode": None,
            "stdout": e.stdout or "", "stderr": (e.stderr or "") + f"\n[Timed out after {timeout}s]",
            "duration_ms": duration_ms,
        }
    except Exception as e:  # never let a bad command crash the dashboard
        duration_ms = int((time.time() - start) * 1000)
        return {"status": "error", "returncode": None, "stdout": "", "stderr": str(e), "duration_ms": duration_ms}


# ---------------------------------------------------------------------------
# Mindmap (hand-built radial graph via vis-network, loaded from CDN - no new
# pip dependency). Rendered inside an iframe (st.iframe) since it needs to
# run its own JavaScript; everything else on the page is native Streamlit or
# static CSS.
#
# Click/hover bridge: clicking or hovering a script node posts a small
# message (window.parent.postMessage) up to the page. A single listener
# installed by inject_bridge_and_reveal_js() picks it up and visually ties
# that node to its card in the catalog grid below (pulsing highlight ring in
# the same skill color + smooth-scroll), then - on click - clicks that card's
# real "Open in editor" button so the editor section actually opens the same
# script. This is what makes the mindmap, catalog and editor read as one
# connected system instead of three separate widgets.
# ---------------------------------------------------------------------------
def build_mindmap_html(scripts: list[dict]) -> str:
    by_skill: dict[str, list[dict]] = {}
    for s in scripts:
        by_skill.setdefault(s["skill"], []).append(s)

    nodes = [{
        "id": "root", "label": "Python\nScripts", "shape": "dot", "size": 40,
        "color": {"background": INK, "border": INK},
        "font": {"color": "#ffffff", "size": 18, "face": FONT_STACK},
    }]
    edges = []

    for skill in SKILL_ORDER:
        group = by_skill.get(skill) or []
        if not group:
            continue
        color = SKILL_COLORS[skill]
        gid = f"group::{skill}"
        nodes.append({
            "id": gid, "label": f"{skill} ({len(group)})", "shape": "box",
            "color": {"background": color, "border": color,
                      "highlight": {"background": color, "border": INK}},
            "font": {"color": "#ffffff", "size": 13, "face": FONT_STACK},
            "margin": 10, "shapeProperties": {"borderRadius": 10},
        })
        edges.append({"from": "root", "to": gid, "color": {"color": color, "opacity": 0.55}, "width": 3})

        for s in group:
            sid = f"script::{s['rel_path']}"
            title = f"{s['filename']} ({s['skill']})\n{s['purpose']}\n\nClick to jump to it in the catalog + editor below."
            nodes.append({
                "id": sid, "label": s["filename"], "shape": "dot",
                "size": min(11 + s["line_count"] / 35, 20),
                "color": {"background": "#ffffff", "border": color,
                          "highlight": {"background": color, "border": INK}},
                "font": {"color": INK, "size": 11, "face": FONT_STACK},
                "borderWidth": 2, "title": title,
            })
            edges.append({"from": gid, "to": sid, "color": {"color": color, "opacity": 0.4}, "width": 1.5})

    # json.dumps does NOT escape "/", so a docstring/filename containing the
    # literal substring "</script>" would otherwise close this inline <script>
    # block early and let arbitrary HTML/JS after it execute in the iframe
    # (confirmed injection path - see README/Script Center notes). Escaping
    # every "</" as "<\/" is a no-op for the JSON the browser parses (a
    # backslash before "/" is a valid, ignored JSON string escape) but makes
    # it impossible for the payload to contain a real closing tag.
    nodes_json = json.dumps(nodes).replace("</", "<\\/")
    edges_json = json.dumps(edges).replace("</", "<\\/")

    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="{GOOGLE_FONTS_CSS_URL}">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/vis-network@9.1.9/styles/vis-network.min.css">
<script src="https://cdn.jsdelivr.net/npm/vis-network@9.1.9/standalone/umd/vis-network.min.js"></script>
<style>
  html,body {{ margin:0; padding:0; background:{SURFACE}; cursor:default; }}
  #mindmap {{ width:100%; height:640px; }}
  .vis-tooltip {{
    font-family:{FONT_STACK} !important; font-size:12.5px !important;
    background:{INK} !important; color:#fff !important; border:none !important;
    border-radius:8px !important; padding:8px 10px !important; max-width:280px; white-space:pre-line !important;
  }}
  #mindmap-fallback {{
    display:none; height:640px; align-items:center; justify-content:center; text-align:center;
    font-family:{FONT_STACK}; color:{INK_DIM}; padding:0 40px; flex-direction:column; gap:8px;
  }}
</style></head>
<body>
<div id="mindmap"></div>
<div id="mindmap-fallback">
  <div style="font-size:18px;font-weight:700;color:{INK};">Mindmap couldn't load</div>
  <div>This visual needs an internet connection to reach cdn.jsdelivr.net (blocked by a firewall/proxy, or you're offline). The rest of the dashboard still works normally.</div>
</div>
<script>
  if (typeof vis === 'undefined') {{
    document.getElementById('mindmap').style.display = 'none';
    document.getElementById('mindmap-fallback').style.display = 'flex';
  }} else {{
  // vis-network measures and draws its labels on a <canvas> ONCE, in whatever font is usable at that moment. Wait for
  // the web font (max 2.5 s: offline / blocked -> the fallback stack is used) so labels are not sized for the fallback.
  function startMindmap() {{
  var nodes = new vis.DataSet({nodes_json});
  var edges = new vis.DataSet({edges_json});
  var network = new vis.Network(
    document.getElementById('mindmap'),
    {{ nodes: nodes, edges: edges }},
    {{
      interaction: {{ hover: true, tooltipDelay: 80, dragView: true, zoomView: true }},
      physics: {{
        solver: 'forceAtlas2Based',
        forceAtlas2Based: {{ gravitationalConstant: -75, springLength: 130, springConstant: 0.05, avoidOverlap: 0.7 }},
        stabilization: {{ iterations: 200 }}
      }},
      layout: {{ improvedLayout: true }},
      edges: {{ smooth: {{ type: 'continuous' }} }}
    }}
  );
  function post(type, id) {{
    if (String(id).indexOf('script::') !== 0) return;
    window.parent.postMessage({{source: 'sc-bridge', type: type, rel: id.slice(8)}}, '*');
  }}
  network.on('click', function (params) {{ if (params.nodes.length) post('select', params.nodes[0]); }});
  network.on('hoverNode', function (params) {{ post('hover', params.node); }});
  network.on('blurNode', function (params) {{ post('unhover', params.node); }});
  network.body.container.style.cursor = 'grab';
  }}
  var started = false;
  function go() {{ if (started) return; started = true; startMindmap(); }}
  if (document.fonts && document.fonts.load) {{
    Promise.all(['400', '600', '700'].map(function (w) {{ return document.fonts.load(w + ' 14px "{FONT_NAME}"').catch(function () {{ return []; }}); }})).then(go, go);
    setTimeout(go, 2500);
  }} else {{ go(); }}
  }}
</script>
</body></html>"""


# ---------------------------------------------------------------------------
# Static brand CSS (the project's shared design language) - additive .sc-* classes
# only, never overrides Streamlit's own internal DOM/classes, except for the
# handful of `div[class*="st-key-..."]` selectors documented inline below,
# which style Streamlit's own st.container(key=...) wrapper divs (the only
# way to give a *real* container - one that can hold a real st.button - a
# custom background/card look).
# ---------------------------------------------------------------------------
def inject_css() -> None:
    st.html(f"""
<link rel="preconnect" href="https://fonts.googleapis.com">
<style>
@import url('{GOOGLE_FONTS_CSS_URL}');
@import url('https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined:opsz,wght,FILL,GRAD@20..48,300..500,0..1,0&display=block');

.material-symbols-outlined {{ font-family:'Material Symbols Outlined'; font-weight:400; font-style:normal;
  font-size:18px; line-height:1; letter-spacing:normal; text-transform:none; display:inline-block;
  white-space:nowrap; word-wrap:normal; direction:ltr; vertical-align:middle; }}

/* Typography (erp/typography.py): Montserrat for everything people read; a monospace only for literal code
   (the editor, file paths, log / terminal output). Applied to the app root too, so the page is Montserrat even
   when it is started without run_script_center.bat's --theme.font flags. */
{streamlit_font_css(include_import=False, code_widget_keys=("sc_editor",))}
/* file paths and error text in the cards are literal code: keep the monospace over the blanket rule above */
.stApp .sc-card-path, .stApp .sc-feed-err {{ font-family:var(--code) !important; }}

/* Force this tool's light background regardless of how the app is launched.
   run_script_center.bat passes --theme.* CLI flags, but this page can also be
   opened directly (e.g. plain "streamlit run dashboard/script_center.py"),
   in which case Streamlit falls back to its own "auto" theme and may render
   a dark app background depending on the OS/browser color scheme. All of the
   custom .sc-* text below is hardcoded to the dark INK color for the light
   brand palette, so without this override it can end up as dark text on a
   dark background and become unreadable. Pinning the background here makes
   the custom sections readable independently of the launch method or the
   native widget theme. */
html, body, .stApp,
[data-testid="stAppViewContainer"], [data-testid="stMain"], [data-testid="stHeader"] {{
  background-color: {BG} !important;
  color: {INK};
}}
[data-testid="stMainBlockContainer"] {{ padding-top: 2rem; max-width: 1220px; }}

/* ---------- Slide sections (st.container(key="slide_...")) ---------- */
div[class*="st-key-slide_"] {{
  border-radius: 28px;
  padding: 34px clamp(18px, 4vw, 44px) 40px;
  margin-bottom: 6px;
  border: 1px solid {LINE};
  position: relative;
  overflow: hidden;
}}
div[class*="st-key-slide_hero"] {{
  background:
    radial-gradient(circle at 12% -10%, rgba(255,90,54,.16), transparent 42%),
    radial-gradient(circle at 100% 0%, rgba(124,92,255,.14), transparent 38%),
    {BG};
  border-color: transparent;
}}
div[class*="st-key-slide_mindmap"] {{
  background:
    radial-gradient(circle, {LINE} 1.4px, transparent 1.4px) 0 0/22px 22px,
    {SURFACE};
}}
div[class*="st-key-slide_catalog"] {{
  background: {BG};
}}
div[class*="st-key-slide_editor"] {{
  background: {SURFACE};
}}
div[class*="st-key-slide_logs"] {{
  background:
    radial-gradient(circle at 90% 0%, rgba(52,82,235,.10), transparent 45%),
    {BG};
}}

/* Numbered "chapter" divider that opens every slide, editorial-report style */
.sc-eyebrow {{ display:flex; align-items:center; gap:10px; margin-bottom:6px; }}
.sc-eyebrow-num {{
  font-family:var(--font); font-size:11px; font-weight:500; color:#fff;
  background:{INK}; border-radius:999px; padding:3px 9px; letter-spacing:.03em;
}}
.sc-eyebrow-label {{ font-family:var(--font); font-size:11px; letter-spacing:.12em;
  text-transform:uppercase; color:{INK_DIM}; }}
.sc-slide-title {{ font-family:var(--font); font-weight:700; font-size:clamp(20px,2.6vw,28px);
  color:{INK}; margin:2px 0 4px; }}
.sc-slide-sub {{ font-size:13px; color:{INK_DIM}; margin-bottom:18px; max-width:680px; line-height:1.55; }}

/* Connector thread between slides: a short dashed line + dot, reinforcing
   that these are stops on one continuous path rather than unrelated blocks. */
.sc-connector {{ display:flex; justify-content:center; margin: -2px 0; height:34px; position:relative; z-index:1; }}
.sc-connector::before {{
  content:''; width:2px; height:100%;
  background: repeating-linear-gradient(to bottom, {LINE} 0 6px, transparent 6px 12px);
}}
.sc-connector::after {{
  content:''; position:absolute; top:50%; left:50%; transform:translate(-50%,-50%);
  width:9px; height:9px; border-radius:50%; background:{VIOLET}; box-shadow:0 0 0 5px {BG};
}}

/* ---------- Masthead + hero ---------- */
.sc-masthead {{ display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:12px; margin-bottom:20px; }}
.sc-brand {{ display:flex; align-items:center; gap:10px; font-family:var(--font); font-weight:600; font-size:19px; color:{INK}; }}
.sc-brand-mark {{ width:30px; height:30px; border-radius:9px; background:linear-gradient(135deg,{CORAL},{VIOLET}); flex:none; }}
.sc-meta {{ font-family:var(--font); font-size:11.5px; color:{INK_DIM}; }}

.sc-hero {{ margin-bottom:22px; max-width:780px; }}
.sc-hero-title {{ font-family:var(--font); font-size:clamp(26px,3.6vw,40px); font-weight:700; margin:0 0 8px; line-height:1.15; color:{INK}; }}
.sc-hero-sub {{ font-size:14.5px; color:{INK_DIM}; margin:0; line-height:1.6; }}

.sc-kpi-row {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:14px; margin-bottom:20px; }}
.sc-kpi {{ background:{SURFACE}; border:1px solid {LINE}; border-radius:16px; padding:18px 18px 16px;
  transition: transform .2s ease, box-shadow .2s ease; }}
.sc-kpi:hover {{ transform:translateY(-3px); box-shadow:0 12px 26px rgba(28,29,34,.08); }}
.sc-kpi-value {{ font-family:var(--font); font-size:30px; font-weight:700; color:{INK}; font-variant-numeric:tabular-nums; }}
.sc-kpi-label {{ font-size:12.5px; font-weight:600; margin-top:3px; color:{INK}; }}
.sc-kpi-caption {{ font-size:11px; color:{INK_DIM}; margin-top:2px; }}

.sc-insight {{ border-radius:18px; padding:22px 26px; margin-bottom:6px; color:#fff;
  background:linear-gradient(120deg,{CORAL},#ff8a4c,{VIOLET}); background-size:220% 220%;
  animation: sc-gradient 10s ease infinite; }}
.sc-insight-eyebrow {{ font-family:var(--font); font-size:10.5px; letter-spacing:.1em; text-transform:uppercase; opacity:.85; }}
.sc-insight-headline {{ font-family:var(--font); font-size:clamp(19px,2.4vw,25px); font-weight:700; margin:6px 0 6px; }}
.sc-insight-sub {{ font-size:13px; opacity:.95; max-width:640px; line-height:1.55; }}

.sc-legend {{ display:flex; flex-wrap:wrap; gap:8px 14px; margin-bottom:14px; }}
.sc-legend-item {{ display:flex; align-items:center; gap:6px; font-size:12px; color:{INK}; font-family:var(--font); }}
.sc-dot {{ width:10px; height:10px; border-radius:50%; flex:none; }}

/* ---------- Mindmap panel ---------- */
div[class*="st-key-sc_mindmap_frame"] {{ border-radius:18px; overflow:hidden; border:1px solid {LINE}; background:{SURFACE}; }}
div[class*="st-key-sc_mindmap_frame"] iframe {{ display:block; }}
.sc-mindmap-hint {{ display:flex; align-items:center; gap:8px; margin-top:12px; font-size:12.5px; color:{INK_DIM}; }}
.sc-mindmap-hint .material-symbols-outlined {{ font-size:16px; color:{VIOLET}; }}

/* ---------- Script catalog card grid ---------- */
.sc-card-toolbar {{ display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:10px; margin-bottom:14px; }}
.sc-card-count {{ font-family:var(--font); font-size:11.5px; color:{INK_DIM}; }}

div[class*="st-key-sc_card_grid"] {{
  display:grid !important;
  grid-template-columns: repeat(auto-fill, minmax(258px, 1fr)) !important;
  gap: 16px !important;
}}
div[class*="st-key-sc_card_"]:not([class*="st-key-sc_card_grid"]) {{
  background:{SURFACE}; border:1px solid {LINE}; border-radius:18px; padding:0 0 12px;
  transition: transform .22s cubic-bezier(.16,1,.3,1), box-shadow .22s ease, border-color .22s ease;
  opacity:0; transform:translateY(14px);
}}
div[class*="st-key-sc_card_"]:not([class*="st-key-sc_card_grid"]).sc-card-in-view {{ opacity:1; transform:translateY(0); }}
div[class*="st-key-sc_card_"]:not([class*="st-key-sc_card_grid"]):hover {{
  transform:translateY(-4px); box-shadow:0 14px 30px rgba(28,29,34,.09);
}}
div[class*="st-key-sc_card_"]:not([class*="st-key-sc_card_grid"]).sc-linked {{
  border-color: var(--linked-color, {VIOLET});
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--linked-color, {VIOLET}) 35%, transparent), 0 16px 32px rgba(28,29,34,.14);
  transform:translateY(-4px);
}}
div[class*="st-key-sc_card_"]:not([class*="st-key-sc_card_grid"]).sc-hover-linked {{
  border-color: var(--linked-color, {VIOLET}); transform:translateY(-2px);
}}
div[class*="st-key-sc_card_"]:not([class*="st-key-sc_card_grid"]) .stButton button {{
  margin: 4px 14px 0; width: calc(100% - 28px);
}}

.sc-card-accent {{ height:4px; width:100%; background:var(--accent,{VIOLET}); }}
.sc-card-body {{ padding:14px 16px 4px; }}
.sc-card-top {{ display:flex; align-items:center; gap:7px; margin-bottom:8px; }}
.sc-card-top .material-symbols-outlined {{ font-size:17px; }}
.sc-card-skill {{ font-family:var(--font); font-size:10.5px; font-weight:500; letter-spacing:.04em; text-transform:uppercase; }}
.sc-card-title {{ font-family:var(--font); font-weight:600; font-size:16px; color:{INK}; line-height:1.25; }}
.sc-card-path {{ font-family:var(--code); font-size:10.5px; color:{INK_DIM}; margin:2px 0 10px; word-break:break-all; }}
.sc-card-badges {{ display:flex; gap:8px; flex-wrap:wrap; margin-bottom:10px; }}
.sc-badge {{ display:inline-flex; align-items:center; gap:4px; font-size:10.5px; font-family:var(--font);
  color:{INK_DIM}; background:{BG}; border:1px solid {LINE}; border-radius:999px; padding:2px 8px; }}
.sc-badge .material-symbols-outlined {{ font-size:12.5px; }}
.sc-card-purpose {{ font-size:12px; color:{INK}; line-height:1.5; min-height:34px;
  display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; }}

/* ---------- Section detail (script badge strip, reused on editor slide) ---------- */
.sc-detail-strip {{ display:flex; gap:18px; flex-wrap:wrap; align-items:center; margin-bottom:10px; font-size:13px; color:{INK_DIM}; }}
.sc-skill-pill {{ color:#fff; padding:3px 10px; border-radius:999px; font-weight:600; font-size:11.5px; }}

/* ---------- Activity feed / timeline ---------- */
.sc-feed {{ background:{SURFACE}; border:1px solid {LINE}; border-radius:16px; padding:6px 4px; }}
.sc-feed-group-label {{ font-family:var(--font); font-size:10.5px; letter-spacing:.1em; text-transform:uppercase;
  color:{INK_DIM}; padding:10px 16px 4px; }}
.sc-feed-item {{ display:flex; align-items:flex-start; gap:12px; padding:9px 16px; position:relative; }}
.sc-feed-item::before {{ content:''; position:absolute; left:26px; top:26px; bottom:-3px; width:1.5px; background:{LINE}; }}
.sc-feed-item:last-child::before {{ display:none; }}
.sc-feed-icon {{ flex:none; width:22px; height:22px; border-radius:50%; display:flex; align-items:center; justify-content:center;
  color:#fff; z-index:1; }}
.sc-feed-icon .material-symbols-outlined {{ font-size:13px; }}
.sc-feed-main {{ flex:1; min-width:0; }}
.sc-feed-line1 {{ display:flex; flex-wrap:wrap; align-items:baseline; gap:8px; }}
.sc-feed-script {{ font-weight:600; color:{INK}; font-size:13px; }}
.sc-feed-action {{ font-size:11.5px; color:{INK_DIM}; font-family:var(--font); }}
.sc-feed-ts {{ font-family:var(--font); font-size:10.5px; color:{INK_DIM}; margin-left:auto; white-space:nowrap; }}
.sc-feed-err {{ color:{RED}; font-family:var(--code); font-size:11px; margin-top:3px; }}

/* ---------- Daily digest report card ---------- */
.sc-digest-meta-row {{ display:flex; gap:8px; flex-wrap:wrap; margin-bottom:14px; }}
.sc-digest-tag {{ font-family:var(--font); font-size:10.5px; color:{INK_DIM}; background:{BG};
  border:1px solid {LINE}; border-radius:999px; padding:3px 10px; }}
.sc-digest-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:12px; margin-bottom:18px; }}
.sc-digest-tile {{ background:{SURFACE}; border:1px solid {LINE}; border-radius:14px; padding:14px 16px; }}
.sc-digest-tile-label {{ font-size:11.5px; color:{INK_DIM}; font-weight:600; margin-bottom:4px; }}
.sc-digest-tile-value-row {{ display:flex; align-items:baseline; gap:8px; }}
.sc-digest-tile-value {{ font-family:var(--font); font-size:24px; font-weight:700; color:{INK}; font-variant-numeric:tabular-nums; }}
.sc-digest-delta {{ font-family:var(--font); font-size:11px; font-weight:600; }}
div[class*="st-key-sc_narrative_card"] {{
  background:linear-gradient(160deg,{SURFACE},{BG}); border:1px solid {LINE}; border-radius:18px;
  padding:20px 24px 6px;
}}
.sc-narrative-label {{ display:flex; align-items:center; gap:7px; font-family:var(--font); font-size:11px;
  letter-spacing:.08em; text-transform:uppercase; color:{VIOLET}; margin-bottom:10px; }}
.sc-narrative-label .material-symbols-outlined {{ font-size:16px; }}
div[class*="st-key-sc_narrative_card"] [data-testid="stMarkdownContainer"] {{
  font-size:13.5px; line-height:1.7; color:{INK};
}}

/* ---------- Generic scroll-reveal, applied to elements this file authors
   directly (kpi/insight/cards-panel/feed/digest wrappers) ---------- */
.sc-reveal {{ opacity:0; transform:translateY(18px) scale(.99);
  transition: opacity .7s cubic-bezier(.16,1,.3,1), transform .7s cubic-bezier(.16,1,.3,1); }}
.sc-reveal.in-view {{ opacity:1; transform:translateY(0) scale(1); }}

@keyframes sc-gradient {{ 0%,100% {{ background-position:0% 50%; }} 50% {{ background-position:100% 50%; }} }}
@media (prefers-reduced-motion: reduce) {{
  .sc-reveal, div[class*="st-key-sc_card_"] {{ transition:none !important; opacity:1 !important; transform:none !important; }}
}}
</style>
""")


def render_masthead_and_hero() -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    st.html(f"""
<div class="sc-masthead">
  <div class="sc-brand"><span class="sc-brand-mark"></span>ERP Intelligence &middot; Script Center</div>
  <div class="sc-meta">LIVE FROM {PROJECT_ROOT.name} &middot; {now}</div>
</div>
<div class="sc-hero">
  <div class="sc-hero-title">Every Python script, at a glance</div>
  <p class="sc-hero-sub">What each script does, which skill it belongs to, how the whole codebase connects -
  select a node in the mindmap and watch it light up its card and its editor entry below. Scroll to explore.</p>
</div>
""")


def render_kpi_row(scripts: list[dict]) -> None:
    total = len(scripts)
    skill_groups = len({s["skill"] for s in scripts})
    total_lines = sum(s["line_count"] for s in scripts)
    no_doc = sum(1 for s in scripts if s["purpose"].startswith("(no docstring)"))

    tiles = [
        ("Scripts tracked", total, "across erp/, dashboard/, and the project root"),
        ("Skill groups", skill_groups, "categories on the mindmap below"),
        ("Total lines", total_lines, "combined, across all tracked scripts"),
        ("Missing docstring", no_doc, "scripts with no purpose to show yet"),
    ]
    html_parts = ['<div class="sc-kpi-row sc-reveal">']
    for label, value, caption in tiles:
        html_parts.append(f"""
        <div class="sc-kpi">
          <div class="sc-kpi-value">{value}</div>
          <div class="sc-kpi-label">{label}</div>
          <div class="sc-kpi-caption">{caption}</div>
        </div>""")
    html_parts.append("</div>")
    st.html("".join(html_parts))


def render_insight_banner(scripts: list[dict]) -> None:
    recent = read_recent_logs(limit=100)
    recent_errors = [r for r in recent if r.get("status") == "error"]
    no_doc = [s for s in scripts if s["purpose"].startswith("(no docstring)")]

    if recent_errors:
        latest = recent_errors[0]
        headline = f"{len(recent_errors)} recent test run(s) failed."
        sub = f"Most recent: {latest.get('script')} at {latest.get('timestamp')} - see the log panel below."
    elif no_doc:
        headline = f"{len(no_doc)} of {len(scripts)} scripts have no module docstring."
        sub = "Add a short docstring at the top of the file so the catalog can describe its purpose."
    else:
        headline = f"All {len(scripts)} scripts across {len({s['skill'] for s in scripts})} skill groups are documented and healthy."
        sub = "No recent test failures and every script has a purpose on record."

    st.html(f"""
<div class="sc-insight sc-reveal">
  <div class="sc-insight-eyebrow">Today's read</div>
  <div class="sc-insight-headline">{headline}</div>
  <div class="sc-insight-sub">{sub}</div>
</div>
""")


def render_legend() -> None:
    items = "".join(
        f'<div class="sc-legend-item"><span class="sc-dot" style="background:{color}"></span>{skill}</div>'
        for skill, color in SKILL_COLORS.items()
    )
    st.html(f'<div class="sc-legend">{items}</div>')


def render_eyebrow(step: str, label: str, title: str, sub: str) -> None:
    st.html(f"""
<div class="sc-eyebrow"><span class="sc-eyebrow-num">{step}</span><span class="sc-eyebrow-label">{label}</span></div>
<div class="sc-slide-title">{title}</div>
<div class="sc-slide-sub">{sub}</div>
""")


def render_connector() -> None:
    st.html('<div class="sc-connector"></div>')


# ---------------------------------------------------------------------------
# Script catalog - visual card grid (replaces the old plain st.dataframe).
# Each card is a REAL st.container(key=...) so it can hold a real st.button;
# a small marker <div data-rel="..."> inside it is what the JS bridge (see
# inject_bridge_and_reveal_js) uses to find + highlight + auto-open it from a
# mindmap click.
# ---------------------------------------------------------------------------
def _select_script(rel_path: str) -> None:
    st.session_state["sc_selected_script"] = rel_path
    st.session_state["sc_scroll_to_editor"] = True


def render_catalog_grid(scripts: list[dict], skill_filter: str) -> None:
    filtered = scripts if skill_filter == "All" else [s for s in scripts if s["skill"] == skill_filter]

    st.html(f"""<div class="sc-card-toolbar"><span class="sc-card-count sc-reveal in-view">
      {len(filtered)} of {len(scripts)} script(s) shown</span></div>""")

    with st.container(key="sc_card_grid"):
        for i, s in enumerate(scripts):
            if s not in filtered:
                continue
            color = SKILL_COLORS[s["skill"]]
            icon = SKILL_ICONS.get(s["skill"], "description")
            mtime = s["last_modified"].strftime("%Y-%m-%d %H:%M") if s["last_modified"] else "unknown"
            purpose = html.escape(s["purpose"])
            rel_esc = html.escape(s["rel_path"], quote=True)
            skill_esc = html.escape(s["skill"], quote=True)

            with st.container(key=f"sc_card_{i}", border=False):
                st.html(f"""
<div class="sc-card-accent" style="--accent:{color}" data-rel="{rel_esc}" data-skill="{skill_esc}" data-color="{color}"></div>
<div class="sc-card-body">
  <div class="sc-card-top">
    <span class="material-symbols-outlined" style="color:{color}">{icon}</span>
    <span class="sc-card-skill" style="color:{color}">{html.escape(s['skill'])}</span>
  </div>
  <div class="sc-card-title" title="{purpose}">{html.escape(s['filename'])}</div>
  <div class="sc-card-path">{html.escape(s['rel_path'])}</div>
  <div class="sc-card-badges">
    <span class="sc-badge"><span class="material-symbols-outlined">code</span>{s['line_count']} lines</span>
    <span class="sc-badge"><span class="material-symbols-outlined">history</span>{mtime}</span>
  </div>
  <div class="sc-card-purpose">{purpose}</div>
</div>
""")
                st.button(
                    "Open in editor", key=f"sc_open_{i}", icon=":material/edit:", width="stretch",
                    on_click=_select_script, args=(s["rel_path"],),
                )


# ---------------------------------------------------------------------------
# Activity feed - status-colored icon per entry, grouped by recency.
# ---------------------------------------------------------------------------
_ACTION_ICON = {"save": "save", "test_run": "play_arrow", "syntax_check": "rule"}


def render_activity_feed(logs: list[dict]) -> None:
    if not logs:
        st.caption("No activity logged yet - run a test or save a script to see it here.")
        return

    today_str = datetime.now().strftime("%Y-%m-%d")
    groups: dict[str, list[dict]] = {"Today": [], "Earlier": []}
    for r in logs:
        ts = r.get("timestamp", "")
        groups["Today" if ts.startswith(today_str) else "Earlier"].append(r)

    rows_html = []
    for group_label in ("Today", "Earlier"):
        rows = groups[group_label]
        if not rows:
            continue
        rows_html.append(f'<div class="sc-feed-group-label">{group_label}</div>')
        for r in rows:
            ok = r.get("status") == "ok"
            color = GREEN if ok else RED
            icon = "check" if ok else "close"
            action = r.get("action", "")
            action_icon = _ACTION_ICON.get(action, "bolt")
            err = r.get("error") or ""
            err_html = f'<div class="sc-feed-err">{html.escape(err[:220])}</div>' if err else ""
            rows_html.append(f"""
            <div class="sc-feed-item">
              <span class="sc-feed-icon" style="background:{color}">
                <span class="material-symbols-outlined">{icon}</span>
              </span>
              <div class="sc-feed-main">
                <div class="sc-feed-line1">
                  <span class="material-symbols-outlined" style="font-size:14px;color:{INK_DIM}">{action_icon}</span>
                  <span class="sc-feed-script">{html.escape(r.get('script',''))}</span>
                  <span class="sc-feed-action">{html.escape(action)} &middot; {r.get('duration_ms','?')} ms</span>
                  <span class="sc-feed-ts">{html.escape(r.get('timestamp',''))}</span>
                </div>
                {err_html}
              </div>
            </div>""")

    st.html(f'<div class="sc-feed sc-reveal">{"".join(rows_html)}</div>')


# ---------------------------------------------------------------------------
# Daily digest - designed "report card" (KPI tiles + delta arrows + AI
# narrative) instead of a dumped JSON blob.
# ---------------------------------------------------------------------------
def render_digest_card(digest: dict) -> None:
    st.html(f"""
<div class="sc-digest-meta-row sc-reveal in-view">
  <span class="sc-digest-tag">Date {html.escape(str(digest.get('date','')))}</span>
  <span class="sc-digest-tag">Generated {html.escape(str(digest.get('generated_at','')))}</span>
  <span class="sc-digest-tag">Email {'sent' if digest.get('email_sent') else 'not sent'}</span>
</div>""")

    metrics = digest.get("metrics", {}) or {}
    deltas = digest.get("deltas", {}) or {}
    tiles = []
    for key, value in metrics.items():
        label = DIGEST_METRIC_LABELS.get(key, key.replace("_", " ").title())
        if value is None:
            display_value = "-"
        elif isinstance(value, float):
            display_value = f"{value:.1f}"
        else:
            display_value = html.escape(str(value))
        delta = deltas.get(key)
        if delta is None:
            arrow, delta_color, delta_txt = "&middot;", INK_DIM, "n/a vs yesterday"
        elif delta == 0:
            arrow, delta_color, delta_txt = "&#9644;", INK_DIM, "unchanged"
        else:
            good_up = key in DIGEST_GOOD_WHEN_UP
            rising = delta > 0
            is_good = rising == good_up
            arrow = "&#9650;" if rising else "&#9660;"
            delta_color = GREEN if is_good else RED
            delta_txt = f"{'+' if rising else ''}{delta} vs yesterday"
        tiles.append(f"""
        <div class="sc-digest-tile">
          <div class="sc-digest-tile-label">{html.escape(label)}</div>
          <div class="sc-digest-tile-value-row">
            <span class="sc-digest-tile-value">{display_value}</span>
            <span class="sc-digest-delta" style="color:{delta_color}">{arrow} {delta_txt}</span>
          </div>
        </div>""")
    st.html(f'<div class="sc-digest-grid sc-reveal in-view">{"".join(tiles)}</div>')

    narrative = digest.get("narrative") or "(no narrative)"
    with st.container(key="sc_narrative_card"):
        st.html('<div class="sc-narrative-label">'
                '<span class="material-symbols-outlined">psychology</span>AI daily narrative</div>')
        st.markdown(narrative)


# ---------------------------------------------------------------------------
# Bottom-of-page JS: scroll-reveal (IntersectionObserver) + the
# mindmap<->card<->editor bridge described above.
# Runs at the end of every rerun; the message listener installs itself only
# once (window.__scBridgeInstalled guard) to avoid stacking duplicate
# listeners across reruns, while the reveal observer is recreated fresh each
# time since Streamlit recreates the DOM nodes on every rerun anyway.
# ---------------------------------------------------------------------------
def inject_bridge_and_reveal_js(scroll_to_editor: bool) -> None:
    scroll_js = (
        "setTimeout(function(){ var el = document.getElementById('slide-editor-anchor'); "
        "if (el) el.scrollIntoView({behavior:'smooth', block:'start'}); }, 80);"
        if scroll_to_editor else ""
    )
    st.html(f"""
<script>
(function() {{
  // -- scroll reveal --------------------------------------------------
  var targets = document.querySelectorAll('.sc-reveal:not(.in-view)');
  var cardTargets = document.querySelectorAll('div[class*="st-key-sc_card_"]:not([class*="st-key-sc_card_grid"]):not(.sc-card-in-view)');
  if (targets.length || cardTargets.length) {{
    var obs = new IntersectionObserver(function (entries) {{
      entries.forEach(function (entry) {{
        if (!entry.isIntersecting) return;
        entry.target.classList.add(entry.target.classList.contains('sc-reveal') ? 'in-view' : 'sc-card-in-view');
        obs.unobserve(entry.target);
      }});
    }}, {{ threshold: 0.12 }});
    targets.forEach(function (t) {{ obs.observe(t); }});
    cardTargets.forEach(function (t) {{ obs.observe(t); }});
  }}

  // -- mindmap <-> card <-> editor bridge ------------------------------
  if (!window.__scBridgeInstalled) {{
    window.__scBridgeInstalled = true;
    window.addEventListener('message', function (event) {{
      var data = event.data;
      if (!data || data.source !== 'sc-bridge' || !data.rel) return;
      var marker;
      try {{ marker = document.querySelector('[data-rel="' + CSS.escape(data.rel) + '"]'); }} catch (e) {{ return; }}
      if (!marker) return;
      var card = marker.closest('div[class*="st-key-sc_card_"]');
      if (!card) return;
      var color = marker.getAttribute('data-color') || '{VIOLET}';
      if (data.type === 'hover') {{
        card.style.setProperty('--linked-color', color);
        card.classList.add('sc-hover-linked');
      }} else if (data.type === 'unhover') {{
        card.classList.remove('sc-hover-linked');
      }} else if (data.type === 'select') {{
        card.style.setProperty('--linked-color', color);
        card.classList.add('sc-linked');
        card.scrollIntoView({{behavior: 'smooth', block: 'center'}});
        var btn = card.querySelector('button');
        if (btn) {{ setTimeout(function () {{ btn.click(); }}, 700); }}
        setTimeout(function () {{ card.classList.remove('sc-linked'); }}, 2800);
      }}
    }});
  }}

  {scroll_js}
}})();
</script>
""", unsafe_allow_javascript=True)


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------
inject_css()

scripts = scan_scripts()
scripts_by_rel = {s["rel_path"]: s for s in scripts}

# --- Slide 1: overview -------------------------------------------------------
with st.container(key="slide_hero"):
    st.html('<div id="slide-hero-anchor"></div>')
    render_masthead_and_hero()
    render_kpi_row(scripts)
    render_insight_banner(scripts)

render_connector()

# --- Slide 2: mindmap ---------------------------------------------------------
with st.container(key="slide_mindmap"):
    render_eyebrow("02", "The system, mapped", "Codebase mindmap",
                   "Python Scripts &rarr; skill group &rarr; script. Hover a node for its purpose; "
                   "click one to jump straight to it in the catalog and editor below - drag to rearrange, scroll to zoom.")
    render_legend()
    with st.container(key="sc_mindmap_frame"):
        st.iframe(build_mindmap_html(scripts), height=642)
    st.html("""
<div class="sc-mindmap-hint">
  <span class="material-symbols-outlined">south</span>
  Click any script node above - its card and its "Open in editor" entry light up below.
</div>
""")

render_connector()

# --- Slide 3: script catalog ---------------------------------------------------
with st.container(key="slide_catalog"):
    render_eyebrow("03", "What's tracked", "Script catalog",
                   "Every script this page tracks, grouped by skill - color-coded to match the mindmap above. "
                   "A node's highlight ring below shows exactly which card the mindmap just pointed at.")
    skill_filter = st.selectbox("Filter by skill", ["All"] + SKILL_ORDER, key="sc_skill_filter")
    render_catalog_grid(scripts, skill_filter)

render_connector()

# --- Slide 4: script detail, editor + test runner ------------------------------
with st.container(key="slide_editor"):
    st.html('<div id="slide-editor-anchor"></div>')
    render_eyebrow("04", "Inspect & run", "Script detail &amp; editor",
                   "Pick a script (or arrive here from the mindmap/catalog above) - view it, edit it, "
                   "save it straight to disk, and test-run it.")

    rel_options = [s["rel_path"] for s in scripts]
    selected_rel = st.selectbox("Choose a script", rel_options, key="sc_selected_script")
    selected = scripts_by_rel[selected_rel]

    badge_color = SKILL_COLORS[selected["skill"]]
    st.html(f"""
<div class="sc-detail-strip">
  <span class="sc-skill-pill" style="background:{badge_color}">{selected['skill']}</span>
  <span>{selected['rel_path']}</span>
  <span>{selected['line_count']} lines</span>
  <span>modified {selected['last_modified'].strftime('%Y-%m-%d %H:%M') if selected['last_modified'] else 'unknown'}</span>
</div>
<p style="font-size:13.5px;color:{INK};margin:0 0 12px">{html.escape(selected['purpose'])}</p>
""")

    editor_key = f"sc_editor::{selected_rel}"
    if editor_key not in st.session_state:
        try:
            st.session_state[editor_key] = selected["abs_path"].read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            st.session_state[editor_key] = f"# Could not read file: {e}"

    st.text_area("Source code", key=editor_key, height=380, label_visibility="collapsed")

    with st.container(horizontal=True):
        save_clicked = st.button("Save to disk", key="sc_save_btn", icon=":material/save:", type="primary")
        reload_clicked = st.button("Reload from disk (discard edits)", key="sc_reload_btn", icon=":material/refresh:")

    if reload_clicked:
        try:
            st.session_state[editor_key] = selected["abs_path"].read_text(encoding="utf-8", errors="replace")
            st.toast(f"Reloaded {selected_rel} from disk.", icon=":material/refresh:")
            st.rerun()
        except OSError as e:
            st.error(f"Could not reload: {e}")

    if save_clicked:
        safe_path = resolve_safe_path(selected_rel)
        start = time.time()
        if safe_path is None:
            st.error("Refused to save: the resolved path is outside the project root or isn't a tracked .py file.")
            log_event(selected_rel, "save", "error", 0, "path validation failed (outside project root)")
        else:
            try:
                safe_path.write_text(st.session_state[editor_key], encoding="utf-8")
                duration_ms = int((time.time() - start) * 1000)
                log_event(selected_rel, "save", "ok", duration_ms)
                st.success(f"Saved {selected_rel}.")
            except OSError as e:
                duration_ms = int((time.time() - start) * 1000)
                log_event(selected_rel, "save", "error", duration_ms, str(e))
                st.error(f"Could not save: {e}")

    st.html('<div class="sc-slide-title" style="font-size:19px;margin-top:22px">Test run</div>')

    if selected_rel in SERVER_SCRIPTS:
        st.info(
            "This is a long-running server - launch it via its .bat file instead of testing here. "
            "You can still check that it compiles without a Python syntax error.",
            icon=":material/dns:",
        )
        if st.button("Check syntax", key="sc_syntax_server", icon=":material/rule:"):
            result = run_subprocess([sys.executable, "-m", "py_compile", str(selected["abs_path"])], timeout=20)
            log_event(selected_rel, "syntax_check", result["status"], result["duration_ms"], result["stderr"] or None)
            if result["status"] == "ok":
                st.success(f"No syntax errors ({result['duration_ms']} ms).")
            else:
                st.error("Syntax check failed:")
                st.code(result["stderr"] or result["stdout"], language="text")

    elif selected_rel in ONE_SHOT_SCRIPTS:
        meta = ONE_SHOT_SCRIPTS[selected_rel]
        kind, icon = RISK_STYLE[meta["risk"]]
        getattr(st, kind)(meta["note"], icon=icon)

        needs_confirm = meta["risk"] in ("db", "network")
        confirmed = True
        if needs_confirm:
            confirmed = st.checkbox("I understand - run it anyway", key=f"sc_confirm::{selected_rel}")

        email_on_failure = st.checkbox("Email me if this fails", key=f"sc_email_fail::{selected_rel}")

        if st.button("Run test", key="sc_run_btn", icon=":material/play_arrow:",
                     type="primary", disabled=needs_confirm and not confirmed):
            cmd = build_command(selected_rel, meta)
            with st.spinner(f"Running {selected_rel}..."):
                result = run_subprocess(cmd, timeout=30)

            log_event(selected_rel, "test_run", result["status"], result["duration_ms"],
                      result["stderr"] if result["status"] == "error" else None)

            if result["status"] == "ok":
                st.success(f"Finished OK in {result['duration_ms']} ms (exit code {result['returncode']}).")
            else:
                st.error(f"Failed after {result['duration_ms']} ms (exit code {result['returncode']}).")
                if email_on_failure:
                    sent = emailer.send_email(
                        f"[Script Center] Test run failed: {selected_rel}",
                        f"<h3>Test run failed: {selected_rel}</h3>"
                        f"<p>Exit code: {result['returncode']} - duration: {result['duration_ms']} ms</p>"
                        f"<pre>{(result['stderr'] or result['stdout'] or '')[:4000]}</pre>",
                    )
                    st.caption(f"Failure email {'sent' if sent else 'not sent (SMTP not configured - see log)'}.")

            if result["stdout"]:
                with st.expander("stdout", expanded=result["status"] == "error", icon=":material/terminal:"):
                    st.code(result["stdout"], language="text")
            if result["stderr"]:
                with st.expander("stderr", expanded=result["status"] == "error", icon=":material/error:"):
                    st.code(result["stderr"], language="text")

    else:
        st.caption(
            "This file is a supporting module, not a standalone entry point - no automated test run is defined. "
            "You can still check that it compiles without a syntax error."
        )
        if st.button("Check syntax", key="sc_syntax_other", icon=":material/rule:"):
            result = run_subprocess([sys.executable, "-m", "py_compile", str(selected["abs_path"])], timeout=20)
            log_event(selected_rel, "syntax_check", result["status"], result["duration_ms"], result["stderr"] or None)
            if result["status"] == "ok":
                st.success(f"No syntax errors ({result['duration_ms']} ms).")
            else:
                st.error("Syntax check failed:")
                st.code(result["stderr"] or result["stdout"], language="text")

render_connector()

# --- Slide 5: logs + daily digest -----------------------------------------------
with st.container(key="slide_logs"):
    render_eyebrow("05", "Keep watch", "Activity log &amp; daily digest",
                   "Every test run, save and syntax check, newest first - plus the latest AI-generated daily digest.")

    log_filter = st.segmented_control("Filter", ["All", "Ok", "Error"], key="sc_log_filter", default="All")
    recent_logs = read_recent_logs(limit=30, status_filter=log_filter)
    render_activity_feed(recent_logs)

    if LOG_FILE.exists():
        st.download_button(
            "Download full log file", data=LOG_FILE.read_bytes(), file_name="script_center.log",
            icon=":material/download:",
        )

    st.html('<div class="sc-slide-title" style="font-size:19px;margin-top:28px">Email alerts &amp; daily digest</div>')

    email_ready = emailer.is_configured()
    st.write(
        (":material/check_circle: **Email alerts configured**" if email_ready
         else ":material/warning: **Email alerts not configured** - fill in SMTP_* and ALERT_EMAIL_* in `.env` "
              "to enable failure alerts and the daily digest email.")
    )

    if DIGEST_LATEST_FILE.exists():
        try:
            digest = json.loads(DIGEST_LATEST_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            digest = None

        if digest:
            render_digest_card(digest)
        else:
            st.warning("daily_digest_latest.json exists but could not be read.")
    else:
        st.info(
            "No digest generated yet. Run `venv\\Scripts\\python.exe -m erp.daily_digest` "
            "(or use the Test run panel above on erp/daily_digest.py) to generate the first one.",
            icon=":material/auto_awesome:",
        )

_scroll_to_editor = bool(st.session_state.pop("sc_scroll_to_editor", False))
inject_bridge_and_reveal_js(_scroll_to_editor)
