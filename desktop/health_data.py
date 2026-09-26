"""
Live data feed for the Health page of ERP Desk (GET /api/health): "is this system actually working, and if not
what do I do about it?" - written first for the report viewer (one honest headline, one plain-English card per
dependency), with the developer's facts folded into every card as a copyable fix hint and the analyst's question
answered by the "What this means for the numbers" block.

THIS PAGE NEVER RUNS ANYTHING AND NEVER CHANGES A SETTING. It reads verdicts other code already produced and
tells you what to do yourself. Every fix hint is text to copy, never a button that acts.

Where every verdict comes from (nothing is probed a second time, lesson L-030):
  * the six summary verdicts        - desktop/today_data.build_health(), the VERY cells the Today page's health
                                      strip shows. The strip is the summary, this page is the detail: they share
                                      the code and the cache, so they cannot disagree.
  * "is the trigger really armed?"  - the Data Flow snapshot (desktop/flow_data.FlowStore): the webhook probe, the
                                      read-only Windows Task Scheduler scan, ERP Desk's own loops. That logic lives
                                      there and nowhere else.
  * "what does it mean for the numbers" - desktop/today_data.blind_inputs(), the same function the Today page's
                                      honesty caveat uses, so the two can never name different blind spots.
  * configuration                   - erp/config.py, as booleans only: "set" / "not set". No value, no part of a
                                      value, no length, no prefix of any setting ever enters the payload
                                      (tests/health_scenarios.py proves it with sentinel values).

This module opens no database connection of its own, starts no thread and adds no poller: it composes two
snapshots that already exist (the Data Flow snapshot, refreshed by its own background thread, and the Today
payload). The Today payload is itself built on demand behind a 5 s single-flight cache SHARED with the Today
page - when that cache has expired, reading it here can make the shared TodayStore open a fresh connection,
exactly as opening the Today page would. This module never opens a second one of its own; it is the shared
store's own on-demand build, not a Health-specific probe. Its own cache is CACHE_SECONDS, and a request never
parks longer than BUILD_WAIT_SECONDS behind another request's build - the same shape, and therefore the same
timeout budget, as desktop/today_data.TodayStore.

Rules, in one place (the page shows the same text under "How this is decided"):

  Card state        the Today page's five colours: ok (green) | warn (amber) | bad (red) | off (grey: deliberately
                    not set up) | unknown (grey: still checking, or could not be checked).
  Card word         the Data Flow vocabulary for WHY it has that colour: healthy / stale / error / never-run /
                    mocked / offline / idle / unavailable.
  Needs attention   a card counts when its state is `bad` or `warn`, OR when the Today page's own blind-inputs
                    logic names it - so "N things need attention" here and the Today headline are decided by the
                    same facts. A card that is `off` on purpose and blinds no number is listed, not counted.
  Headline          no card needs attention -> "Everything that should be running is running"; otherwise
                    "N things need attention". When the health feed itself has not produced a verdict yet the
                    headline says so instead of guessing.
  Not the same question as Today's headline: Today answers "is the work on time?", this page answers "is the
                    machinery running?". It can be green here and amber there (leads past their SLA), and amber here
                    and green there (a recommended job Task Scheduler does not have blinds no number, so Today never
                    looks at it) - both sentences are shown side by side so the difference is visible, never hidden.
                    The ONE rule that always holds, and the only cross-page direction the merge gate asserts, is:
                    all clear here => the Today page is not reporting a blind input. mark_attention() enforces it.

Contract, same as today_data / flow_data: never raises into the caller. A card whose source cannot be read
degrades to `unknown` on its own and the rest of the page keeps working - including when PostgreSQL is down,
which is exactly when this page is needed. Nothing here takes input from the browser (the only query parameter,
`fresh`, just skips the cache) and nothing writes anywhere.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timezone

from desktop import today_data
from desktop.flow_data import _ago, _iso
from erp import config as _config

logger = logging.getLogger("erp_desk.health")

CACHE_SECONDS = 5.0            # /api/health is cached this long (the page polls every ~15 s)
MIN_FRESH_SECONDS = 1.0        # ?fresh=1 cannot hammer the shared stores faster than this
BUILD_WAIT_SECONDS = 6.0       # a request never waits longer than this for another request's build

# The vocabulary. `state` decides the colour, `word` says why - both already exist in this project.
STATES = ("ok", "warn", "bad", "off", "unknown")
WORDS = ("healthy", "stale", "error", "never-run", "mocked", "offline", "idle", "unavailable")
_WORD_FROM_FLOW = {"healthy": "healthy", "stale": "stale", "error": "error", "never-run": "never-run",
                   "mocked": "mocked", "offline": "offline", "idle": "idle", "unavailable": "unavailable",
                   "info": "idle"}
_WORD_FROM_STATE = {"ok": "healthy", "warn": "stale", "bad": "error", "off": "mocked", "unknown": "unavailable"}

GROUPS = [
    {"id": "doors", "title": "Where the data lives and how it gets in",
     "tag": "if one of these is down, numbers go blank or quietly incomplete"},
    {"id": "helpers", "title": "What enriches and notifies", "tag": "the work still happens, with less in it"},
    {"id": "jobs", "title": "Scheduled jobs", "tag": "one card per job, as Windows Task Scheduler really has it"},
    {"id": "app", "title": "ERP Desk itself", "tag": "the loops inside this window and the Script Center it starts"},
]

PY = r"venv\Scripts\python.exe"


def _fix(kind: str, text: str, note: str = "", copy: bool = True) -> dict:
    """One fix hint. kind: command (paste in PowerShell) | click (do it in the app) | config (edit .env) | owner (needs the owner)."""
    return {"kind": kind, "text": text, "note": note, "copy": bool(copy)}


# --------------------------------------------------------------------------- the six cards that mirror the Today strip
# Each one reuses a Today health cell verbatim for its state and its "why"; this table adds only what the detail page
# needs on top: which Data Flow node holds the vocabulary word, the real consequence, and what to do about it.
SUMMARY_CARDS = [
    {
        "id": "database", "cell": "database", "node": "postgres", "group": "doors", "label": "PostgreSQL",
        # read from erp/config.py (DB_NAME/DB_HOST/DB_PORT), never hardcoded, so this card can never name the wrong
        # database, host or port after a .env change (lesson L-085) - built here as an f-string over an already
        # imported module's attributes (no function call), so it stays import-safe (lesson L-075/L-132).
        "where": f"{_config.DB_NAME} @ {_config.DB_HOST}:{_config.DB_PORT}", "sub": "the only place this system stores anything",
        "impact": "Every number on Today, Leads and Reporting goes blank. The pages still open and say so, but nothing "
                  "can be counted, and no script that writes (webhook, pull, digest) can finish.",
        "fix": lambda: [_fix("command", "Get-Service *postgres*", "Check whether the PostgreSQL service is running on this machine."),
                        _fix("owner", "Start the PostgreSQL 18 service in Windows Services, then press Refresh here.",
                             "Starting a Windows service needs an administrator - this page will not do it for you.", copy=False)],
    },
    {
        "id": "webhook", "cell": "webhook", "node": "webhook", "group": "doors", "label": "Lead webhook listener",
        "where": "erp/webhook_app.py on 127.0.0.1:8000", "sub": "the door web-form leads come in through",
        "impact": "Web-form leads cannot arrive. Nothing is lost at the form itself, but nothing reaches this database "
                  "either, so \"new leads today\" can only be right for leads entered another way.",
        "fix": lambda: [_fix("command", "run_webhook.bat", "Double-click it or run it from the project root; keep the window open (Ctrl+C stops it)."),
                _fix("command", PY + " -m uvicorn erp.webhook_app:app --host 127.0.0.1 --port 8000",
                     "The same listener without the .bat wrapper.")],
    },
    {
        "id": "clickup", "cell": "clickup", "node": "clickup_push", "group": "doors", "label": "ClickUp",
        "where": "erp/clickup_client.py", "sub": "where the Sales rep actually works, and where replies come back from",
        "impact": "No real task is created for a new lead, and no rep's reply can ever be read back. \"Awaiting first "
                  "reply\" and \"past SLA\" then look worse than reality, because the only evidence of a reply this "
                  "database has is a ClickUp comment.",
        "fix": lambda: [_fix("config", "CLICKUP_API_TOKEN", "Put it in .env (it is git-ignored), then restart ERP Desk."),
                _fix("config", "CLICKUP_LIST_ID", "The list new lead tasks are created in. Also in .env."),
                _fix("click", "Data Flow (Ctrl+4) -> the ClickUp node shows what has and has not synced.", copy=False)],
    },
    {
        "id": "deepseek", "cell": "deepseek", "node": "deepseek", "group": "helpers", "label": "DeepSeek AI",
        "where": "erp/ai_client.py", "sub": "scores each lead and writes the digest and weekly narrative",
        "impact": "Leads still arrive, are de-duplicated, assigned and pushed to ClickUp. Only the reading is lost: "
                  "every potential score becomes the fixed heuristic fallback, and the digest has no narrative.",
        "fix": lambda: [_fix("config", "DEEPSEEK_API_KEY", "Put it in .env, then restart ERP Desk. Without it nothing breaks - the fallback runs."),
                _fix("click", "Leads (Ctrl+5) -> the funnel's \"analysed by AI\" stage shows how many leads have a real analysis.", copy=False)],
    },
    {
        "id": "email", "cell": "email", "node": "emailer", "group": "helpers", "label": "Email (SMTP)",
        "where": "erp/emailer.py", "sub": "the daily digest and the Script Center failure alerts",
        "impact": "The digest is still written to disk and shown in the app, but nobody is told by mail, and a failed "
                  "Script Center test run alerts no one.",
        "fix": lambda: [_fix("config", "SMTP_HOST", "Plus SMTP_PORT / SMTP_USER / SMTP_PASSWORD if your server needs them."),
                _fix("config", "ALERT_EMAIL_FROM", "Who the mail comes from."),
                _fix("config", "ALERT_EMAIL_TO", "Who receives the digest and the alerts.")],
    },
]

# --------------------------------------------------------------------------- one card per scheduled job
JOB_CARDS = [
    {
        "id": "job_clickup_pull", "key": "clickup_pull", "node": "clickup_pull", "label": "ClickUp comment pull",
        "where": "erp/clickup_pull.py", "sub": "recommended every ~15 minutes",
        "impact": "A rep's reply never reaches the database. \"Leads awaiting first reply\" and \"Leads past SLA now\" "
                  "then count leads that may already have been answered in ClickUp.",
        "fix": lambda: [_fix("command", PY + " -m erp.clickup_pull", "Runs it once, right now, from the project root."),
                _fix("owner", "Register it in Windows Task Scheduler every 15 minutes (needs the owner).",
                     "Action: " + PY + " -m erp.clickup_pull, started in the project folder.", copy=False)],
    },
    {
        "id": "job_digest_daily", "key": "digest_daily", "node": "daily_digest", "label": "Daily digest (the emailed one)",
        "where": "run_daily_digest.bat", "sub": "the only run that emails the team",
        "impact": "No digest is emailed. ERP Desk still writes one by itself while it is open (see \"Auto daily digest\" "
                  "below), so the briefing on Reporting can be fresh while nobody's inbox has it.",
        "fix": lambda: [_fix("command", "run_daily_digest.bat", "Generates and emails today's digest once."),
                _fix("owner", "Register run_daily_digest.bat in Windows Task Scheduler each morning (needs the owner).", copy=False)],
    },
    {
        "id": "job_weekly_report", "key": "weekly_report", "node": "weekly_report", "label": "Weekly AI report",
        "where": "erp/weekly_report.py", "sub": "recommended every Monday morning",
        "impact": "The seven-day AI summary is simply not produced. Nothing else depends on it.",
        "fix": lambda: [_fix("command", PY + " -m erp.weekly_report", "Prints the report to the console; it writes no file."),
                _fix("owner", "Register it in Windows Task Scheduler every Monday (needs the owner).", copy=False)],
    },
]
# A task registered on this machine for a job the map does not ask to be scheduled is still shown, honestly.
EXTRA_JOB_LABELS = {"daily_check": "Daily check", "sync_employees": "Staff sync",
                    "webhook": "Lead webhook, started by Task Scheduler"}

# --------------------------------------------------------------------------- ERP Desk's own loops + Script Center
APP_CARDS = [
    {
        "id": "desk_report", "node": "live_report", "label": "Live report refresher",
        "where": "desktop/report_data.py", "sub": "a background thread in this window, every DESKTOP_REFRESH_SECONDS seconds",
        "impact": "The Reporting page stops updating itself and keeps showing the last snapshot it got. Today and "
                  "Leads are unaffected - they read on demand.",
        "fix": lambda: [_fix("click", "Press \"Refresh report\" in the left sidebar (or R on the Reporting page).", copy=False),
                _fix("command", "stop_erp_desktop.bat", "Then start ERP Desk again if the thread does not come back.")],
    },
    {
        "id": "desk_flow", "node": None, "label": "System checks (this feed)",
        "where": "desktop/flow_data.py", "sub": "the webhook probe, the Task Scheduler scan and the live counters behind every card here",
        "impact": "Every verdict on this page and on Data Flow freezes at its last check. Nothing is wrong with the "
                  "system itself, but this page stops being evidence of it.",
        "fix": lambda: [_fix("click", "Press Refresh at the top of this page (or R).", copy=False),
                _fix("click", "Data Flow (Ctrl+4) -> Refresh also re-scans Task Scheduler and the project map.", copy=False),
                _fix("command", PY + " -m desktop --status", "Prints what ERP Desk currently has running.")],
    },
    {
        "id": "desk_digest", "node": "daily_digest", "label": "Auto daily digest",
        "where": "desktop/digest_service.py", "sub": "ERP Desk writes today's digest once a day while it is open, and never emails it",
        "impact": "Today's briefing on the Reporting page stays on yesterday's digest until somebody generates one.",
        "fix": lambda: [_fix("click", "Reporting (Ctrl+2) -> \"Generate today's digest\".", copy=False),
                _fix("config", "DESKTOP_AUTO_DIGEST", "Set it to 1 in .env and restart ERP Desk to turn the once-a-day run back on.")],
    },
    {
        "id": "script_center", "node": "script_center", "label": "Script Center",
        "where": "dashboard/script_center.py", "sub": "the Management page, a Streamlit app ERP Desk starts and supervises",
        "impact": "The Management page stays blank: no script catalog, no mindmap, no editor, no test runner. "
                  "Nothing in the data pipeline depends on it.",
        "fix": lambda: [_fix("command", "stop_erp_desktop.bat", "Then start ERP Desk again - it restarts Script Center for you."),
                _fix("command", "run_script_center.bat", "Starts it standalone on http://localhost:8502 instead."),
                _fix("click", "Read erp_desktop_streamlit.log in the project folder for the reason it stopped.", copy=False)],
    },
]

# --------------------------------------------------------------------------- what a blind input costs, by page
# Keyed by the ids desktop/today_data.blind_inputs() produces, so the two blocks can never name different blind spots.
BLIND_EFFECTS = {
    "webhook": ["Today - \"New leads today\", the \"since yesterday\" line and every lead-based tile",
                "Leads - the whole funnel, because leads that never arrived cannot be in it",
                "Reporting - leads over time, by source and by rep"],
    "pull": ["Today - \"Leads awaiting first reply\" and \"Leads past SLA now\" (both over-count: a lead answered in ClickUp still looks unanswered)",
             "Leads - SLA outcome, time to first reply, the \"reply on record\" stage of the funnel",
             "Reporting - SLA by rep"],
    "clickup": ["Today - \"Leads awaiting first reply\" and \"Leads past SLA now\" (no reply can ever be recorded)",
                "Leads - the ClickUp task stage of the funnel and the whole SLA outcome split",
                "Reporting - SLA by rep"],
    "checks": ["Nothing is known yet - the first round of system checks has not finished."],
    "database": ["Every number on Today, Leads and Reporting"],
    "feed": ["Every number on Today, Leads and Reporting"],
}

# --------------------------------------------------------------------------- configuration truth ("set" / "not set" ONLY)
# (config attribute, what it is, which integration it belongs to, what you lose without it). The VALUE is never read
# into the payload - only bool(value). tests/health_scenarios.py proves that with sentinel values.
CONFIG_KEYS = [
    ("CLICKUP_API_TOKEN", "ClickUp API token", "clickup", "Without it every task is a mock task (MOCK-...)."),
    ("CLICKUP_LIST_ID", "ClickUp list id", "clickup", "The list new lead tasks are created in."),
    ("DEEPSEEK_API_KEY", "DeepSeek API key", "deepseek", "Without it every lead score is the heuristic fallback."),
    ("SMTP_HOST", "SMTP server", "email", "Without it nothing is ever emailed (logged and skipped, never a crash)."),
    ("SMTP_USER", "SMTP user", "email", "Only needed if your mail server authenticates."),
    ("SMTP_PASSWORD", "SMTP password", "email", "Only needed if your mail server authenticates."),
    ("ALERT_EMAIL_FROM", "Alert sender address", "email", "Who the digest and the failure alerts come from."),
    ("ALERT_EMAIL_TO", "Alert recipient address", "email", "Who receives them."),
    ("DB_PASSWORD", "Database password", "database", "Blank only works if PostgreSQL is set to trust this user."),
]
INTEGRATIONS = [
    ("clickup", "ClickUp", ("CLICKUP_API_TOKEN", "CLICKUP_LIST_ID"),
     "Real tasks are created and their comments can be pulled back.",
     "Mock mode: tasks get MOCK-... ids, nothing leaves this machine, no reply can ever be read."),
    ("deepseek", "DeepSeek AI", ("DEEPSEEK_API_KEY",),
     "Leads get a real AI reading and the digest gets a narrative.",
     "Fallback mode: a fixed heuristic score, no narrative. Nothing crashes."),
    ("email", "Email (SMTP)", ("SMTP_HOST", "ALERT_EMAIL_FROM", "ALERT_EMAIL_TO"),
     "The digest and failure alerts are delivered.",
     "Not set up: mail is logged and skipped."),
]

NEVER_RUNS = ("This page never runs anything and never changes a setting. It reads what the system already knows and "
              "tells you, in words you can copy, what to do yourself.")
NO_SECRETS = ("Only \"set\" or \"not set\" ever leaves the server - no value, no part of a value, not even its length. "
              "The .env file stays on this machine.")

# GET /api/health accepts exactly one query parameter name. Whitelisted the same way as /api/leads/analysis
# (desktop/leads_data.unknown_params, lesson L-098): an unknown NAME is a 400 that names the parameter, checked
# by the route before it ever reaches HealthStore's cache, so a typo cannot be normalised into a cache hit that
# silently ignores it.
KNOWN_PARAMS = {"fresh"}


def unknown_params(params) -> list[dict]:
    """Parameter NAMES this endpoint does not understand: [{"param", "value", "why"}], empty when every name is known.
    The value is never echoed back (lesson L-098)."""
    return [{"param": name, "value": "", "why": "not a parameter of this view; known: " + ", ".join(sorted(KNOWN_PARAMS))}
            for name in sorted(params) if name not in KNOWN_PARAMS]


# ============================================================================ small helpers
def _n(count: int, one: str, many: str | None = None) -> str:
    return f"{count} {one if count == 1 else (many or one + 's')}"


def _word(flow_state: str | None, state: str) -> str:
    """The Data Flow vocabulary word for a card: taken from the node's own health state when there is one."""
    w = _WORD_FROM_FLOW.get(flow_state or "")
    return w or _WORD_FROM_STATE.get(state, "unavailable")


# The severity badge next to the vocabulary word. It is printed only when it says something the word does not:
# "ERROR - NOT WORKING" and "UNAVAILABLE - NOT KNOWN" are the same statement twice, "MOCKED - ON PURPOSE" is not.
_TAG = {"ok": None, "warn": "CHECK THIS", "bad": "NOT WORKING", "off": "ON PURPOSE", "unknown": "NOT KNOWN"}


def _tag(state: str, word: str) -> str | None:
    t = _TAG.get(state)
    if not t or word.upper() == t:
        return None
    if state == "bad" and word == "error":
        return None
    if state == "unknown" and word == "unavailable":
        return None
    return t


def _impact_title(state: str) -> str:
    """A grey/amber/red card is not a hypothesis: say what it is costing NOW, and only say "if" when it works."""
    if state == "ok":
        return "If this stops"
    if state == "unknown":
        return "If it is not working"
    return "What this is costing you right now"


def _sentence(text: str) -> str:
    t = (text or "").strip()
    if t and t[-1] not in ".!?":
        t += "."
    return t


def _norm(text: str) -> str:
    return " ".join((text or "").lower().replace("–", "-").split()).strip(" .!?;:")


def _dedup_detail(detail: str, why: str) -> str:
    """Drop every sentence of the second line that the first line already says (lesson L-145, per card).

    The `why` sentence and the Data Flow node's own `detail` are written by two different modules, so they
    routinely overlap: "Connected and answering." above "Connected. 47 rows across the 7 tracked tables.",
    or "Switched off (DESKTOP_AUTO_DIGEST=0) - ..." above "Switched off (DESKTOP_AUTO_DIGEST=0)." Printing
    the same fact twice on every card is exactly the noise a screenshot makes obvious and a diff does not.
    Only the sentences that add something survive; a detail that adds nothing disappears entirely.
    """
    d, w = (detail or "").strip(), _norm(why)
    if not d or not w:
        return d
    kept = [s for s in re.split(r"(?<=[.!?])\s+", d) if _norm(s) and _norm(s) not in w]
    return " ".join(kept).strip()


def _card(id_: str, group: str, label: str, state: str, word: str, why: str, *, sub: str = "", where: str = "", impact: str = "",
          fix: list | None = None, checked_at: str | None = None, ok_at: str | None = None, detail: str = "",
          attention: bool = False, extra: str = "") -> dict:
    state = state if state in STATES else "unknown"
    word = word if word in WORDS else "unavailable"
    why = _sentence(why)
    return {
        "id": id_, "group": group, "label": label, "sub": sub, "where": where,
        "state": state, "word": word, "tag": _tag(state, word),
        "why": why, "impact": _sentence(impact), "impact_title": _impact_title(state),
        "detail": _dedup_detail(detail, why), "extra": extra,
        "checked_at": checked_at, "checked_text": ("checked " + _ago(checked_at)) if checked_at else "not checked yet",
        "ok_at": ok_at, "ok_text": ("last succeeded " + _ago(ok_at)) if ok_at else "no success on record",
        "fix": list(fix or []), "attention": bool(attention),
        # A green card still shows its hint - but as "if it ever stops", not as a to-do list under something that works.
        "fix_title": "If it ever stops" if state == "ok" else "What to do",
    }


def _unavailable_card(id_: str, group: str, label: str, why: str, fix: list | None = None) -> dict:
    """A card whose own check crashed or has not produced a verdict yet. `fix` is the dependency's OWN fix hints
    (already resolved from its spec's lazy "fix" list), so degrading to 'unavailable' does not also strip the one
    thing a reader could actually copy and try - it only adds "press Refresh" on top, it never replaces the real hint."""
    return _card(id_, group, label, "unknown", "unavailable", why,
                 impact="Only this card is affected - every other card on this page was read separately.",
                 fix=[*(fix or []), _fix("click", "Press Refresh at the top of this page.", copy=False)])


# ============================================================================ the cards
def _by_id(flow: dict | None) -> dict:
    return {n["id"]: n for n in (flow or {}).get("nodes", []) or [] if isinstance(n, dict) and n.get("id")}


def _node_health(nodes: dict, node_id: str | None) -> dict:
    return (nodes.get(node_id or "") or {}).get("health") or {}


def _trigger(nodes: dict, node_id: str | None, kind: str) -> dict | None:
    for t in (nodes.get(node_id or "") or {}).get("triggers", []) or []:
        if t.get("kind") == kind:
            return t
    return None


def build_summary_cards(cells: list[dict], flow: dict | None, checked_at: str | None) -> list[dict]:
    """The five dependencies the Today strip already judges, as full cards. The state and the "why" are the strip's own."""
    nodes = _by_id(flow)
    by_cell = {c.get("id"): c for c in cells or []}
    out = []
    for spec in SUMMARY_CARDS:
        try:
            cell = by_cell.get(spec["cell"])
            if cell is None:
                out.append(_unavailable_card(spec["id"], spec["group"], spec["label"],
                                             "The system-health feed has not produced a verdict for this yet", spec["fix"]()))
                continue
            h = _node_health(nodes, spec["node"])
            state = cell.get("state", "unknown")
            out.append(_card(spec["id"], spec["group"], spec["label"], state, _word(h.get("state"), state),
                             cell.get("text", ""), sub=spec["sub"], where=spec["where"], impact=spec["impact"], fix=spec["fix"](),
                             checked_at=checked_at, ok_at=h.get("last_ok_at"),
                             detail=_sentence(cell.get("detail") or h.get("detail") or "")))
        except Exception:  # noqa: BLE001 - one card may never take down the page
            logger.exception("health card %s failed", spec.get("id"))
            out.append(_unavailable_card(spec["id"], spec["group"], spec["label"], "This check crashed - see erp_desktop.log", spec["fix"]()))
    return out


def _task_error(task: dict) -> bool:
    return str(task.get("last_result", "")).strip() not in today_data._TASK_NOT_RUN


def build_job_cards(flow: dict | None, checked_at: str | None) -> list[dict]:
    """One card per scheduled job, from the read-only Task Scheduler scan the Data Flow feed already did."""
    nodes = _by_id(flow)
    sch = (flow or {}).get("scheduler") or {}
    tasks = [t for t in sch.get("tasks", []) or [] if isinstance(t, dict)]
    sched_checked = sch.get("checked_at") or checked_at
    pending, available = bool(sch.get("pending")), sch.get("available")
    out: list[dict] = []
    claimed: set[str] = set()

    for spec in JOB_CARDS:
        try:
            key = spec["key"]
            claimed.add(key)
            mine = [t for t in tasks if key in (t.get("keys") or [])]
            enabled = [t for t in mine if t.get("enabled")]
            h = _node_health(nodes, spec["node"])
            hstate, ok_at = h.get("state"), h.get("last_ok_at")
            names = ", ".join(t.get("name", "?") for t in enabled[:2]) + (f" +{len(enabled) - 2} more" if len(enabled) > 2 else "")
            if pending or available is None:
                state, word, why = "unknown", "unavailable", "Windows Task Scheduler is still being read, so nobody knows yet whether this job is registered"
            elif available is False:
                state, word, why = "unknown", "unavailable", "Windows Task Scheduler could not be read (" + (sch.get("error") or "no details") + "), so it is not known whether this job is registered"
            elif not enabled:
                state, word = "warn", "offline"
                # "last succeeded ..." / "no success on record" is on every card's meta row already, and says it
                # more precisely (last_ok_at is the last SUCCESS, not merely the last run) - so it is not repeated here.
                why = "Not scheduled on this machine - it runs only when somebody starts it"
                # The Data Flow node this job shares can carry a success from somewhere else entirely - job_digest_daily's
                # "daily_digest" node is also fed by ERP Desk's own in-app auto-digest (desktop/digest_service.py), which
                # is not this scheduled job at all. An unscheduled job must never be credited with a "last succeeded"
                # it did not earn from its own trigger, so this card's own success clock stays unset until it IS scheduled.
                ok_at = None
            elif any(_task_error(t) for t in enabled):
                bad = next(t for t in enabled if _task_error(t))
                state, word = "bad", "error"
                why = f"Scheduled as \"{bad.get('name', '?')}\", but its last run reported an error (result {str(bad.get('last_result', '')).strip() or '?'})"
            elif hstate == "error":
                state, word, why = "bad", "error", f"Scheduled as \"{names}\", but its last run failed: " + (h.get("detail") or "see the log")
            elif hstate == "stale":
                state, word, why = "warn", "stale", f"Scheduled as \"{names}\", but it has not produced anything recently: " + (h.get("detail") or "")
            elif hstate == "never-run":
                state, word, why = "warn", "never-run", f"Scheduled as \"{names}\", but it has never produced anything yet"
            elif hstate == "mocked":
                state, word, why = "off", "mocked", f"Scheduled as \"{names}\", but it has nothing real to do: " + (h.get("detail") or "")
            elif hstate in (None, "unavailable"):
                state, word, why = "unknown", "unavailable", f"Scheduled as \"{names}\"; whether its last run worked could not be read (the database is not answering)"
            else:
                state, word, why = "ok", "healthy", f"Scheduled as \"{names}\" - " + (h.get("detail") or "it is running by itself")
            # No `detail` line: for a scheduled job the Data Flow trigger's own state text ("Scheduled on this
            # machine", "Recommended, but NOT scheduled on this machine", "Checking Task Scheduler...") only ever
            # restates the sentence above it in jargon - three identical pairs of lines under "Scheduled jobs".
            out.append(_card(spec["id"], "jobs", spec["label"], state, word, why, sub=spec["sub"], where=spec["where"],
                             impact=spec["impact"], fix=spec["fix"](), checked_at=sched_checked, ok_at=ok_at,
                             extra=(f"Next run: {enabled[0].get('next_run')}" if enabled and enabled[0].get("next_run") else "")))
        except Exception:  # noqa: BLE001
            logger.exception("health job card %s failed", spec.get("id"))
            out.append(_unavailable_card(spec["id"], "jobs", spec["label"], "This check crashed - see erp_desktop.log", spec["fix"]()))

    # Anything else this machine really schedules for this project: shown because it exists, not because the map asks for it.
    seen: set[str] = set()
    for t in tasks:
        for key in t.get("keys") or []:
            if key in claimed or key in seen or not t.get("enabled"):
                continue
            seen.add(key)
            label = EXTRA_JOB_LABELS.get(key, key)
            err = _task_error(t)
            out.append(_card(f"job_{key}", "jobs", label, "bad" if err else "ok", "error" if err else "healthy",
                             f"Registered on this machine as \"{t.get('name', '?')}\"" +
                             (f", and its last run reported an error (result {str(t.get('last_result', '')).strip() or '?'})" if err
                              else ", and its last run did not report an error"),
                             where="Windows Task Scheduler", sub="found on this machine; the Data Flow map does not require this one to be scheduled",
                             impact="Whatever this job does is not done by itself while it is failing." if err
                                    else "Nothing depends on it being scheduled - it is a bonus, not a requirement.",
                             fix=[_fix("owner", "Open Windows Task Scheduler and read the task's History tab.", copy=False)],
                             checked_at=sched_checked))
    return out


def build_app_cards(flow: dict | None, checked_at: str | None, feed_ok: bool, feed_error: str | None) -> list[dict]:
    """ERP Desk's own background loops and the Script Center process it supervises."""
    nodes = _by_id(flow)
    out: list[dict] = []
    for spec in APP_CARDS:
        try:
            h = _node_health(nodes, spec["node"])
            hstate, ok_at, detail = h.get("state"), h.get("last_ok_at"), h.get("detail") or ""
            if spec["id"] == "desk_flow":
                if not feed_ok or flow is None:
                    state, word = "warn", "stale"
                    why = "The system checks have not produced a snapshot yet" + (f" ({feed_error})" if feed_error else "")
                    ok_at = None
                else:
                    state, word = "ok", "healthy"
                    why = f"Running - the webhook probe, the Task Scheduler scan and the live counters were last read {_ago(checked_at)}"
                    ok_at = checked_at
                out.append(_card(spec["id"], "app", spec["label"], state, word, why, sub=spec["sub"], where=spec["where"],
                                 impact=spec["impact"], fix=spec["fix"](), checked_at=checked_at, ok_at=ok_at))
                continue
            if spec["id"] == "desk_digest":
                trig = _trigger(nodes, "daily_digest", "background")
                armed = (trig or {}).get("armed")
                if trig is None:
                    state, word, why = "unknown", "unavailable", "The Data Flow snapshot does not say whether the once-a-day run is on"
                elif armed is True:
                    state, word, why = "ok", "healthy", "On - ERP Desk writes today's digest once a day while this window is open (it never emails)"
                elif armed is False:
                    state, word, why = "off", "idle", "Switched off (DESKTOP_AUTO_DIGEST=0) - a digest is only written when you ask for one or the scheduled job runs"
                else:
                    state, word, why = "unknown", "unavailable", "Could not be checked"
                out.append(_card(spec["id"], "app", spec["label"], state, word, why, sub=spec["sub"], where=spec["where"],
                                 impact=spec["impact"], fix=spec["fix"](), checked_at=checked_at, ok_at=h.get("last_ok_at"),
                                 detail=_sentence((trig or {}).get("state") or "")))
                continue
            if spec["id"] == "desk_report":
                trig = _trigger(nodes, "live_report", "background")
                armed = (trig or {}).get("armed")
                dead = " and the refresher thread is not alive either" if armed is False else ""
                if hstate == "error":
                    # the RED is the failed re-query, so say that first; the thread being down is a second fact
                    state, word = "bad", "error"
                    why = (detail or "The last re-query of the database failed") + dead
                elif armed is False:
                    state, word = "warn", "offline"
                    why = "The refresher thread is not alive - the Reporting page is showing whatever it last received"
                elif armed is None or hstate is None:
                    state, word, why = "unknown", "unavailable", detail or "Whether the refresher thread is alive could not be read"
                elif hstate == "never-run":
                    state, word, why = "warn", "never-run", detail or "The first snapshot has not been produced yet"
                else:
                    state, word, why = "ok", "healthy", detail or "The refresher thread is alive"
                out.append(_card(spec["id"], "app", spec["label"], state, word, why, sub=spec["sub"], where=spec["where"],
                                 impact=spec["impact"], fix=spec["fix"](), checked_at=checked_at, ok_at=ok_at,
                                 # with the thread down the sentence above already says so; the trigger's own
                                 # "Not armed" underneath would only repeat it in jargon
                                 detail="" if armed is False else _sentence((trig or {}).get("state") or "")))
                continue
            # script_center
            state = {"healthy": "ok", "stale": "warn", "error": "bad", "never-run": "warn", "mocked": "off",
                     "offline": "warn", "idle": "off", "unavailable": "unknown"}.get(hstate or "", "unknown")
            out.append(_card(spec["id"], "app", spec["label"], state, _word(hstate, state),
                             detail or "No verdict yet", sub=spec["sub"], where=spec["where"], impact=spec["impact"],
                             fix=spec["fix"](), checked_at=checked_at, ok_at=ok_at))
        except Exception:  # noqa: BLE001
            logger.exception("health app card %s failed", spec.get("id"))
            out.append(_unavailable_card(spec["id"], "app", spec["label"], "This check crashed - see erp_desktop.log", spec["fix"]()))
    return out


# ============================================================================ headline, numbers, configuration
def mark_attention(cards: list[dict], blind_ids: list[str]) -> list[dict]:
    """A card needs attention when it is red/amber, or when the Today page's blind-input logic names it (L-084).

    The mapping from a blind-input id to a card id is explicit, so the two pages agree by construction:
      webhook -> the webhook card, clickup -> the ClickUp card, pull -> the comment-pull job card.
    """
    named = {"webhook": "webhook", "clickup": "clickup", "pull": "job_clickup_pull"}
    want = {named[b] for b in blind_ids if b in named}
    for c in cards:
        c["attention"] = c["state"] in ("bad", "warn") or c["id"] in want
    return cards


def build_headline(cards: list[dict], feed_ok: bool) -> dict:
    """One honest sentence. Same facts as the Today headline; a different question (machinery, not workload)."""
    counts = {s: sum(1 for c in cards if c["state"] == s) for s in STATES}
    need = [c for c in cards if c["attention"]]
    unknown = [c for c in cards if c["state"] == "unknown"]
    off = [c for c in cards if c["state"] == "off" and not c["attention"]]
    # With no system-check snapshot almost nothing is known - but a card that is already RED (the database refusing
    # to answer is the case that matters) must never be hidden behind a grey "Still checking" headline.
    if not cards or (not feed_ok and not any(c["state"] == "bad" for c in cards)):
        return {"level": "unknown", "text": "Still checking", "rest": "the system checks have not finished their first pass",
                "sub": "Nothing here is a verdict yet. Give it a few seconds and press Refresh.",
                "counts": counts, "attention": len(need), "names": [], "off": len(off)}
    if need:
        worst = "bad" if any(c["state"] == "bad" for c in need) else "warn"
        names = [c["label"] for c in need]
        rest = ", ".join(names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "")
        sub = ("Each card below says what stops working and exactly what to do about it. "
               "This page will not do any of it for you.")
        if not feed_ok:
            sub += (" The rest of the system checks have not finished their first pass, so the other cards say "
                    "\"not known\" rather than \"fine\" - this list can still grow.")
        return {"level": "attention" if worst == "bad" else "watch",
                "text": _n(len(need), "thing") + " need" + ("s" if len(need) == 1 else "") + " attention",
                "rest": rest, "sub": sub,
                "counts": counts, "attention": len(need), "names": names, "off": len(off)}
    sub = "Every dependency this system needs is answering, and every job it should run by itself is registered."
    if unknown:
        sub += f" {_n(len(unknown), 'check')} could not be completed - those cards say so."
    if off:
        sub += f" {_n(len(off), 'thing')} " + ("is" if len(off) == 1 else "are") + " deliberately off; the cards say what you lose."
    return {"level": "ok", "text": "Everything that should be running is running", "rest": "",
            "sub": sub, "counts": counts, "attention": 0, "names": [], "off": len(off)}


def build_numbers(blind: list[dict], db_down: bool) -> dict:
    """"What this means for the numbers": the blind inputs the Today caveat already names, with the pages they hit."""
    items = []
    if db_down:
        items.append({"id": "database", "clause": "the database is not answering, so no page can count anything",
                      "affects": list(BLIND_EFFECTS["database"])})
    for b in blind or []:
        if b.get("id") == "database" and db_down:
            continue
        items.append({"id": b.get("id"), "clause": b.get("clause", ""),
                      "affects": list(BLIND_EFFECTS.get(b.get("id"), []))})
    if not items:
        return {"complete": True, "items": [],
                "text": "Nothing on this page is holding a number back. Every figure on Today, Leads and Reporting is "
                        "as complete as the database can make it.",
                "note": "This is the same check the Today page uses for its \"Numbers may be incomplete\" line, so the "
                        "two can never disagree."}
    return {"complete": False, "items": items,
            "text": "Some figures are incomplete right now, because of the amber and red cards above:",
            "note": "This is the same check the Today page uses for its \"Numbers may be incomplete\" line, so the "
                    "two can never disagree."}


def build_config() -> dict:
    """Configuration truth, read through erp/config.py. Booleans only - see NO_SECRETS."""
    try:
        from erp import config
    except Exception as exc:  # noqa: BLE001
        logger.exception("health: erp.config could not be read")
        return {"available": False, "error": type(exc).__name__, "rows": [], "integrations": [],
                "note": NO_SECRETS, "source": ".env via erp/config.py"}
    def is_set(name: str) -> bool:
        try:
            return bool(str(getattr(config, name, "") or "").strip())
        except Exception:  # noqa: BLE001
            return False
    rows = [{"key": k, "label": label, "integration": grp, "why": why, "state": "set" if is_set(k) else "not set"}
            for k, label, grp, why in CONFIG_KEYS]
    integrations = []
    for id_, label, keys, when_on, when_off in INTEGRATIONS:
        have = [k for k in keys if is_set(k)]
        if len(have) == len(keys):
            mode = "configured"
        elif have:
            mode = "partly configured"
        else:
            mode = "not set up" if id_ == "email" else "mocked"
        integrations.append({"id": id_, "label": label, "mode": mode,
                             "state": "ok" if mode == "configured" else ("warn" if mode == "partly configured" else "off"),
                             "why": when_on if mode == "configured" else when_off,
                             "missing": [k for k in keys if k not in have]})
    return {"available": True, "error": None, "rows": rows, "integrations": integrations,
            "note": NO_SECRETS, "source": ".env, read through erp/config.py (load_dotenv(override=True))"}


def build_rules(cards: list[dict]) -> list[str]:
    """The text under "How this is decided", generated from the same constants the verdicts use (L-085)."""
    return [
        NEVER_RUNS,
        "Every verdict here is the one the Today page's health strip already shows, plus the detail the strip has no "
        "room for. The strip and these cards come from the same code and the same snapshot, so they cannot disagree.",
        "Whether an automated step is really armed (the webhook answering, a Windows Task Scheduler entry existing, "
        "ERP Desk's own loops being alive) is decided in desktop/flow_data.py and nowhere else - this page only reads it.",
        "Colour: green = working, amber = check it, red = broken, grey = deliberately not set up or not checkable. "
        "The word under the colour (" + " / ".join(WORDS) + ") is the Data Flow page's own vocabulary for why.",
        "\"Needs attention\" counts a card that is red or amber, and any card the Today page's blind-input check names "
        "(the webhook, ClickUp, the comment pull). A card that is grey on purpose and blinds no number is listed but "
        "not counted - the number of cards on this page is " + str(len(cards)) + ".",
        "This page answers \"is the machinery running?\". The Today page answers \"is the work on time?\". Both "
        "sentences are shown above, because a system can be perfectly healthy while leads are past their deadline.",
        "\"Last checked\" is when the check behind that card last ran; \"last succeeded\" is the newest success the "
        "database or the logs can prove. A job with no run on record says exactly that, and never guesses.",
        "Configuration shows \"set\" or \"not set\" only. " + NO_SECRETS,
        f"Speed: this page opens no database connection of its own. It composes two snapshots that already exist - "
        "the Today payload's own on-demand build (shared with the Today page) is what may open one, when its few-"
        f"second cache has expired - and caches the result for {CACHE_SECONDS:g} s; a request never waits more than "
        f"{BUILD_WAIT_SECONDS:g} s for another request's build. When PostgreSQL is down the database card explains it "
        "and every other card still renders.",
    ]


# ============================================================================ assemble (pure)
def assemble(flow_snap: dict | None, today: dict | None) -> dict:
    """The Data Flow snapshot + the Today payload -> the JSON the page draws. Pure: no I/O, never raises."""
    flow = (flow_snap or {}).get("data") if isinstance(flow_snap, dict) else None
    flow = flow if isinstance(flow, dict) else None
    feed_ok = flow is not None
    meta = flow_snap if isinstance(flow_snap, dict) else {}
    feed_error = meta.get("error")
    # "Last checked" must be when the checks last RAN, which is `refreshed_at`. `generated_at` only moves when the
    # snapshot's CONTENT changed (FlowStore.refresh keeps it while the version hash is stable), so on an idle machine
    # it can be hours old while the background thread is still probing every few seconds - every card would then
    # claim "checked 9h ago" and the desk_flow card would contradict itself (healthy, last read 9h ago). The Data
    # Flow page reads `refreshed_at` for exactly this reason (desktop/static/flow.js). Fall back only if it is absent.
    checked_at = _iso_or_none(meta.get("refreshed_at")) or _iso_or_none(meta.get("generated_at"))

    cells = (today or {}).get("health") if isinstance(today, dict) else None
    if not cells:
        try:
            cells = today_data.build_health(flow, None)
        except Exception:  # noqa: BLE001
            logger.exception("health: build_health fell over")
            cells = []
    today_status = (today or {}).get("status") if isinstance(today, dict) else None
    today_status = today_status if isinstance(today_status, dict) else {}

    have_today = isinstance(today, dict) and bool(today.get("health"))
    cards = build_summary_cards(cells, flow, checked_at)
    cards += build_job_cards(flow, checked_at)
    cards += build_app_cards(flow, checked_at, feed_ok, feed_error)
    if not feed_ok and not have_today:
        # today_data.build_health() reports the database as "Connected" when its caller passes no db_error - which is
        # true for the Today page (it just queried) but would be an invention here, where nobody has asked anything yet.
        for c in cards:
            if c["id"] == "database":
                c.update(state="unknown", word="unavailable", tag=_tag("unknown", "unavailable"),
                         impact_title=_impact_title("unknown"), fix_title="What to do",
                         why="Nothing has asked PostgreSQL yet, so whether it is answering is not known.")

    try:
        blind = today_data.blind_inputs(cells) if cells else [{"id": "checks", "clause": "the system checks have not finished yet"}]
    except Exception:  # noqa: BLE001
        logger.exception("health: blind_inputs fell over")
        blind = []
    blind_ids = [b.get("id") for b in blind]
    mark_attention(cards, blind_ids)
    db_card = next((c for c in cards if c["id"] == "database"), None)
    db_down = bool(db_card and db_card["state"] == "bad")
    headline = build_headline(cards, feed_ok)

    groups = []
    for g in GROUPS:
        mine = [c for c in cards if c["group"] == g["id"]]
        if mine:
            need = [c for c in mine if c["attention"]]
            groups.append({**g, "cards": [c["id"] for c in mine], "attention": len(need),
                           # the group's counter is coloured by the worst card in it, so a red group is not
                           # summarised by an amber pill
                           "attention_state": ("bad" if any(c["state"] == "bad" for c in need)
                                               else "warn" if need else None)})
    return {
        "ok": feed_ok and not db_down,
        "generated_at": _iso(datetime.now(timezone.utc)),
        "checked_at": checked_at,
        "feed_ok": feed_ok,
        "headline": headline,
        "today": {"level": today_status.get("level"), "headline": today_status.get("headline"),
                  "caveat": today_status.get("caveat")},
        "groups": groups,
        "cards": cards,
        "numbers": build_numbers(blind, db_down),
        "config": build_config(),
        "never_runs": NEVER_RUNS,
        "rules": build_rules(cards),
        "sources": {"health": "ok" if cells else "unavailable", "flow": "ok" if feed_ok else "unavailable",
                    "today": "ok" if isinstance(today, dict) and today.get("health") else "unavailable",
                    "scheduler": ((flow or {}).get("scheduler") or {}).get("available")},
    }


def _iso_or_none(value) -> str | None:
    """flow snapshots carry a local ISO string ('2026-09-23T10:31:28+07:00'); the page wants UTC like everything else."""
    if not value or not isinstance(value, str):
        return None
    try:
        return _iso(datetime.fromisoformat(value))
    except ValueError:
        return None


# ============================================================================ store
class HealthStore:
    """Builds the /api/health payload on demand from two snapshots that already exist. No thread, nothing to stop.

    It owns no database connection and no probe: `flow` is the Data Flow snapshot (its own background thread keeps it
    fresh) and `today` is the Today page's store (a few seconds of single-flight cache). Both are shared, so opening
    this page adds no poller to anything.
    """

    def __init__(self, flow=None, today=None, ttl: float = CACHE_SECONDS) -> None:
        self.flow = flow
        self.today = today
        self.ttl = ttl
        self._lock = threading.Lock()
        self._cache: dict | None = None
        self._at = 0.0

    def _flow_snapshot(self) -> dict | None:
        if self.flow is None:
            return None
        try:
            snap = self.flow.get()
            return snap if isinstance(snap, dict) else None
        except Exception:  # noqa: BLE001 - the cards degrade, the page does not care
            logger.exception("Health feed: could not read the Data Flow snapshot")
            return None

    def _today_payload(self) -> dict | None:
        if self.today is None:
            return None
        try:
            data = self.today.get()
            return data if isinstance(data, dict) else None
        except Exception:  # noqa: BLE001 - fall back to building the cells from the flow snapshot
            logger.exception("Health feed: could not read the Today payload")
            return None

    def _build(self) -> dict:
        return assemble(self._flow_snapshot(), self._today_payload())

    @staticmethod
    def _fallback(why: str) -> dict:
        out = assemble(None, None)
        out["headline"] = {"level": "unknown", "text": "Can't check the system right now", "rest": why,
                           "sub": "Press Refresh in a moment. Nothing on this page is a verdict until it can read its sources.",
                           "counts": {s: 0 for s in STATES}, "attention": 0, "names": [], "off": 0}
        return out

    def get(self, fresh: bool = False) -> dict:
        if not self._lock.acquire(timeout=BUILD_WAIT_SECONDS):
            logger.warning("Health feed: a build has been running for more than %.0f s; serving the last payload", BUILD_WAIT_SECONDS)
            return self._cache or self._fallback("a previous read is still running")
        try:
            age = time.monotonic() - self._at
            if self._cache is not None and age < (MIN_FRESH_SECONDS if fresh else self.ttl):
                return self._cache
            try:
                self._cache = self._build()
            except Exception as exc:  # noqa: BLE001 - last line of defence: never a 500 for the page
                logger.exception("Health feed failed")
                self._cache = self._fallback(type(exc).__name__)
            self._at = time.monotonic()
            return self._cache
        finally:
            self._lock.release()
