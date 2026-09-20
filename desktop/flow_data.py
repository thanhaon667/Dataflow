"""
Live data feed for the Data Flow page of ERP Desk.

desktop/flow_definition.py describes the system (nodes, edges, triggers). This module
looks at the REAL system and merges what it finds onto that description:

  * numbers        - read-only SELECTs against PostgreSQL (leads, assignments, AI analyses,
                     ClickUp sync rows, pulled comments, staff, tickets, views, the powerbi role)
  * last run/error - script_center.log, daily_digest.log, the digest JSON files, ERP Desk's own
                     in-memory state (report refresher, digest job, Script Center process)
  * automation     - is each trigger really armed? The webhook listener is probed on localhost,
                     Windows Task Scheduler is queried with `schtasks /query` (read-only, fixed
                     argv) and matched against the commands each job would use, and ERP Desk's
                     own background loops are checked directly. Anything that is only
                     recommended but not set up is reported as exactly that.
  * health         - healthy / stale / error / never-run / mocked / offline / idle / unavailable
  * map sync       - the map is declared by hand, so this module also checks it against the project:
                     Python scripts no node covers ("unmapped"), nodes whose file is gone ("stale"),
                     stale IGNORE entries and inconsistent edges. Result: `map_check` in the payload.

Same contract as report_data.ReportStore: one JSON snapshot in memory, refreshed by a
background thread (and on demand), given a new `version` only when something changed, never
raising into callers (on a DB error the DB-derived nodes say "unavailable"; file-derived ones
keep working). Nothing here takes input from the browser, and no secret value is ever put in
the snapshot - only booleans such as "API key configured".
"""
from __future__ import annotations

import ast
import csv
import hashlib
import io
import json
import logging
import os
import re
import subprocess
import threading
import time
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

from sqlalchemy import text

from desktop import winutil
from desktop import flow_definition
from desktop.flow_definition import (
    EDGES, IGNORE, LANES, LAYOUT, NODES, PROBES, SCAN, SCHEDULE_MARKERS, TRACE,
)
from erp.db import engine

logger = logging.getLogger("erp_desk.flow")

SCHEDULER_TTL = 60          # seconds between `schtasks` scans (a scan takes ~5 s)
MAP_CHECK_TTL = 30          # seconds between project scans for the map sync check (the Refresh button forces one)
MAP_SCAN_LIMIT = 20000      # give up ("check unavailable") when a folder tree is absurdly large
STALE_FACTOR = 1.5          # "stale" = older than 1.5x the expected cadence
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # never via a proxy


# ============================================================================ tiny helpers
def _iso(value) -> str | None:
    """datetime -> 'YYYY-MM-DDTHH:MM:SSZ' (UTC). Naive datetimes are taken as local time."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.astimezone()
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return None


def _local_iso(s: str | None) -> str | None:
    """'2026-09-20T10:12:59' / '2026-09-20 10:12:59' (naive local) -> UTC iso."""
    if not s or not isinstance(s, str):  # a malformed log/digest field must not take down the whole feed
        return None
    try:
        return _iso(datetime.fromisoformat(s.strip().replace(" ", "T")))
    except ValueError:
        return None


def _parse(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _age_hours(iso: str | None) -> float | None:
    dt = _parse(iso)
    return None if dt is None else max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 3600)


def _ago(iso: str | None) -> str:
    h = _age_hours(iso)
    if h is None:
        return "never"
    minutes = h * 60
    if minutes < 1:
        return "just now"
    if minutes < 90:
        return f"{round(minutes)}m ago"
    if h < 48:
        return f"{round(h)}h ago"
    return f"{round(h / 24)}d ago"


def _latest(*isos: str | None) -> str | None:
    vals = [i for i in isos if i]
    return max(vals) if vals else None


_REDACT = [
    (re.compile(r"(?i)(://[^:/\s]+:)[^@\s]+@"), r"\1***@"),                          # user:password@host
    (re.compile(r"(?i)\b(bearer|token|api[_-]?key|password|passwd|secret)\b\s*[:=]?\s*\S+"), r"\1 ***"),
    (re.compile(r"\b[A-Za-z0-9_\-]{32,}\b"), "***"),                               # long opaque tokens
]


def _redact(msg: str | None, limit: int = 180) -> str:
    """Log / exception text is shown in the UI: strip anything credential-shaped and cap the length."""
    s = (msg or "").strip().splitlines()[0] if (msg or "").strip() else ""
    for rx, rep in _REDACT:
        s = rx.sub(rep, s)
    return s[:limit]


def _read_tail(path: Path, max_bytes: int) -> str:
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()   # drop a partial first line
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


# ============================================================================ data sources
def _q(conn, sql: str, params: dict | None = None):
    """Run one read-only query; returns the first row as a dict, or None on failure (missing table...)."""
    try:
        row = conn.execute(text(sql), params or {}).mappings().first()
        return dict(row) if row is not None else None
    except Exception as exc:  # noqa: BLE001 - a missing view must not take the whole page down
        logger.debug("flow query failed: %s", exc)
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None


def _i(v) -> int | None:
    return None if v is None else int(v)


def _db_metrics() -> tuple[dict, str | None]:
    """Everything the page shows from the database. Returns (metrics, error)."""
    m: dict = {}
    try:
        conn = engine.connect()
    except Exception as exc:  # noqa: BLE001
        return m, f"{type(exc).__name__}: {_redact(str(exc))}"
    with conn:
        r = _q(conn, """SELECT count(*) AS total, max(created_at) AS last_at,
                        count(*) FILTER (WHERE created_at > now() - interval '24 hours') AS n24h,
                        count(*) FILTER (WHERE created_at > now() - interval '7 days') AS n7d,
                        count(DISTINCT source) AS sources FROM leads""")
        if r is None:
            return {}, "Could not read the leads table"
        m["leads"] = {"total": _i(r["total"]), "last_at": _iso(r["last_at"]), "n24h": _i(r["n24h"]),
                      "n7d": _i(r["n7d"]), "sources": _i(r["sources"])}

        r = _q(conn, """SELECT count(*) AS total, count(*) FILTER (WHERE is_current) AS current,
                        max(assigned_at) AS last_at,
                        count(*) FILTER (WHERE assignment_reason = 'existing_duplicate') AS dup,
                        count(*) FILTER (WHERE assignment_reason = 'round_robin_new') AS rr,
                        count(*) FILTER (WHERE assigned_at > now() - interval '24 hours') AS n24h
                        FROM lead_assignments""")
        reps = _q(conn, """SELECT count(*) AS n FROM users u JOIN departments d ON d.id = u.department_id
                           AND d.code = 'SALES' WHERE u.is_active""")
        m["assign"] = {"total": _i(r and r["total"]), "current": _i(r and r["current"]), "last_at": _iso(r and r["last_at"]),
                       "dup": _i(r and r["dup"]), "rr": _i(r and r["rr"]), "n24h": _i(r and r["n24h"]),
                       "reps_active": _i(reps and reps["n"])}

        r = _q(conn, """SELECT count(*) AS total, count(*) FILTER (WHERE raw_response IS NOT NULL) AS real,
                        max(analyzed_at) AS last_at, avg(potential_score) AS avg_score,
                        count(*) FILTER (WHERE analyzed_at > now() - interval '24 hours') AS n24h
                        FROM lead_ai_analysis""")
        total = _i(r and r["total"])
        m["ai"] = {"total": total, "real": _i(r and r["real"]),
                   "fallback": None if total is None else total - int(r["real"] or 0),
                   "last_at": _iso(r and r["last_at"]), "n24h": _i(r and r["n24h"]),
                   "avg_score": round(float(r["avg_score"]), 1) if r and r["avg_score"] is not None else None}

        r = _q(conn, """SELECT count(*) AS total,
                        count(*) FILTER (WHERE sync_status = 'synced') AS synced,
                        count(*) FILTER (WHERE sync_status = 'mocked') AS mocked,
                        count(*) FILTER (WHERE sync_status = 'pending') AS pending,
                        count(*) FILTER (WHERE sync_status = 'failed') AS failed,
                        max(last_synced_at) AS last_at, max(last_comment_pulled_at) AS last_pull_at,
                        count(*) FILTER (WHERE last_synced_at > now() - interval '24 hours') AS n24h
                        FROM lead_clickup_sync""")
        m["clickup"] = {k: (_iso(r[k]) if k in ("last_at", "last_pull_at") else _i(r[k])) for k in r} if r else {}
        if r:  # the "task created" counter counts real + mocked tasks
            m["clickup"]["created"] = (m["clickup"].get("synced") or 0) + (m["clickup"].get("mocked") or 0)

        r = _q(conn, """SELECT count(*) AS total, max(synced_at) AS last_at, max(occurred_at) AS last_occurred,
                        count(*) FILTER (WHERE occurred_at > now() - interval '7 days') AS n7d,
                        count(*) FILTER (WHERE synced_at > now() - interval '24 hours') AS n24h
                        FROM lead_updates""")
        m["updates"] = {"total": _i(r and r["total"]), "last_at": _iso(r and r["last_at"]),
                        "last_occurred": _iso(r and r["last_occurred"]), "n7d": _i(r and r["n7d"]), "n24h": _i(r and r["n24h"])}

        r = _q(conn, "SELECT count(*) AS total, count(clickup_user_id) AS linked FROM users")
        m["users"] = {"total": _i(r and r["total"]), "linked": _i(r and r["linked"]), "sales_active": m["assign"]["reps_active"]}

        r = _q(conn, """SELECT count(*) AS total,
                        count(*) FILTER (WHERE status IN ('open', 'in_progress', 'pending')) AS open,
                        max(created_at) AS last_at FROM tickets""")
        m["tickets"] = {"total": _i(r and r["total"]), "open": _i(r and r["open"]), "last_at": _iso(r and r["last_at"])}

        r = _q(conn, """SELECT
                 (SELECT count(*) FROM leads l WHERE NOT EXISTS (SELECT 1 FROM lead_assignments a WHERE a.lead_id = l.id)) AS no_assignment,
                 (SELECT count(*) FROM leads l WHERE NOT EXISTS (SELECT 1 FROM lead_ai_analysis a WHERE a.lead_id = l.id)) AS no_analysis,
                 (SELECT count(*) FROM leads l WHERE NOT EXISTS (SELECT 1 FROM lead_clickup_sync s WHERE s.lead_id = l.id
                        AND s.sync_status IN ('synced', 'mocked'))) AS no_task""")
        m["gaps"] = {k: _i(v) for k, v in (r or {}).items()}

        lv = _q(conn, "SELECT count(*) AS n FROM v_leads_summary")
        tv = _q(conn, "SELECT count(*) AS n FROM v_tickets_summary")
        role = _q(conn, "SELECT 1 AS ok FROM pg_roles WHERE rolname = 'powerbi_reader'")
        m["views"] = {"leads_rows": _i(lv and lv["n"]), "tickets_rows": _i(tv and tv["n"]), "role_ok": role is not None}

        # the same five checks erp/daily_check.py prints
        r = _q(conn, """SELECT
                 (SELECT count(*) FROM v_leads_summary WHERE sla_breached) AS sla_leads,
                 (SELECT count(*) FROM v_leads_summary WHERE sync_status IS DISTINCT FROM 'synced') AS unsynced,
                 (SELECT count(*) FROM leads l WHERE NOT EXISTS (SELECT 1 FROM lead_ai_analysis a WHERE a.lead_id = l.id)) AS not_analyzed,
                 (SELECT count(*) FROM leads l WHERE NOT EXISTS (SELECT 1 FROM lead_updates u WHERE u.lead_id = l.id)
                        AND l.created_at < now() - interval '2 days') AS stale_leads,
                 (SELECT count(*) FROM v_tickets_summary WHERE sla_breached AND status NOT IN ('resolved', 'closed')) AS sla_tickets""")
        att = {k: _i(v) for k, v in (r or {}).items()}
        att["total"] = sum(v or 0 for v in att.values()) if att else None
        m["attention"] = att

        m["_trace_row"] = _q(conn, """SELECT l.id, l.source, l.created_at,
                 (SELECT min(assigned_at) FROM lead_assignments WHERE lead_id = l.id) AS assigned_at,
                 (SELECT assignment_reason FROM lead_assignments WHERE lead_id = l.id ORDER BY assigned_at LIMIT 1) AS reason,
                 (SELECT min(analyzed_at) FROM lead_ai_analysis WHERE lead_id = l.id) AS analyzed_at,
                 (SELECT potential_score FROM lead_ai_analysis WHERE lead_id = l.id ORDER BY analyzed_at DESC LIMIT 1) AS score,
                 (SELECT last_synced_at FROM lead_clickup_sync WHERE lead_id = l.id) AS synced_at,
                 (SELECT sync_status FROM lead_clickup_sync WHERE lead_id = l.id) AS sync_status,
                 (SELECT min(synced_at) FROM lead_updates WHERE lead_id = l.id) AS update_at
                 FROM leads l ORDER BY l.created_at DESC, l.id DESC LIMIT 1""")
    return m, None


# Older daily_digest.log files were polluted by other processes' asyncio errors (see erp/daily_digest.py
# _setup_logging); none of these is a digest failure.
_ASYNCIO_NOISE = ("_ProactorBasePipeTransport", "Accept failed on a socket", "Task exception was never retrieved")

_LOG_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ \[(\w+)\] (.*)$")


def _file_metrics(root: Path, digest) -> dict:
    """Digest files, daily_digest.log, script_center.log, erp_report.html - all read-only."""
    out: dict = {}

    latest_path = digest.latest_file if digest else root / "daily_digest_latest.json"
    history_path = digest.history_file if digest else root / "daily_digest_history.json"
    d: dict = {"exists": False, "date": None, "generated_at": None, "ai_configured": None, "email_sent": None,
               "days": 0, "is_today": False, "auto_generate": bool(digest and digest.auto_generate)}
    try:
        latest = json.loads(latest_path.read_text(encoding="utf-8"))
        if isinstance(latest, dict) and "metrics" in latest:
            d.update(exists=True, date=latest.get("date"), generated_at=_local_iso(latest.get("generated_at")),
                     ai_configured=bool(latest.get("ai_configured")), email_sent=bool(latest.get("email_sent")),
                     is_today=latest.get("date") == date.today().isoformat())
    except (OSError, json.JSONDecodeError):
        pass
    try:
        d["days"] = sum(1 for ln in history_path.read_text(encoding="utf-8").splitlines() if ln.strip())
    except OSError:
        pass
    out["digest"] = d

    dl: dict = {"last_error_at": None, "last_error": None, "last_info_at": None}
    for line in _read_tail(root / "daily_digest.log", 300_000).splitlines():
        mm = _LOG_LINE.match(line)
        if not mm:
            continue
        ts, level, msg = _local_iso(mm.group(1)), mm.group(2), mm.group(3)
        if any(noise in msg for noise in _ASYNCIO_NOISE):   # harmless Windows asyncio socket noise - not a digest failure
            continue
        if level in ("ERROR", "CRITICAL"):
            if not dl["last_error_at"] or ts >= dl["last_error_at"]:
                dl["last_error_at"], dl["last_error"] = ts, _redact(msg)
        elif not dl["last_info_at"] or ts >= dl["last_info_at"]:
            dl["last_info_at"] = ts
    out["dlog"] = dl

    c: dict = {"entries": 0, "ok": 0, "errors": 0, "last_at": None, "last_error_at": None, "per_script": {}}
    for line in _read_tail(root / "script_center.log", 600_000).splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(e, dict):
            continue
        ts, script, ok = _local_iso(e.get("timestamp")), str(e.get("script") or ""), e.get("status") == "ok"
        c["entries"] += 1
        c["ok" if ok else "errors"] += 1
        c["last_at"] = _latest(c["last_at"], ts)
        if not ok:
            c["last_error_at"] = _latest(c["last_error_at"], ts)
        s = c["per_script"].setdefault(script, {"last_ok": None, "last_error": None, "runs": 0, "actions": set()})
        s["runs"] += 1
        if e.get("action") == "test_run" or not ok:   # a syntax check is not a run
            s["last_ok" if ok else "last_error"] = _latest(s["last_ok" if ok else "last_error"], ts)
    for s in c["per_script"].values():
        s.pop("actions", None)
    out["center"] = c

    f: dict = {"report_html_at": None, "report_html_kb": None, "timeline_png_at": None, "timeline_png_kb": None}
    for prefix, name in (("report_html", "erp_report.html"), ("timeline_png", "task_timeline.png")):
        try:
            st = (root / name).stat()
            f[prefix + "_at"] = _iso(datetime.fromtimestamp(st.st_mtime, tz=timezone.utc))
            f[prefix + "_kb"] = max(1, round(st.st_size / 1024))
        except OSError:
            pass
    out["files"] = f
    return out


def _probe(url: str, timeout: float = 0.4) -> bool:
    try:
        with _OPENER.open(url, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:  # noqa: BLE001
        return False


def _env_flags() -> dict:
    """Booleans only. Never the values."""
    try:
        from erp import config
        return {"deepseek": bool(config.DEEPSEEK_API_KEY),
                "clickup": bool(config.CLICKUP_API_TOKEN and config.CLICKUP_LIST_ID),
                "smtp": bool(config.SMTP_HOST and config.ALERT_EMAIL_FROM and config.ALERT_EMAIL_TO)}
    except Exception:  # noqa: BLE001
        return {"deepseek": False, "clickup": False, "smtp": False}


def scan_scheduler(job: "winutil.JobObject | None" = None) -> dict:
    """Ask Windows Task Scheduler (read-only `schtasks /query`) which tasks run this project's jobs."""
    res: dict = {"available": False, "error": None, "checked_at": _iso(datetime.now(timezone.utc)), "tasks": [], "matches": {}}
    try:
        proc = subprocess.Popen(["schtasks", "/query", "/fo", "CSV", "/v"], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=winutil.CREATE_NO_WINDOW)
        if job:
            job.add(proc)
        try:
            raw, _err = proc.communicate(timeout=40)
        except subprocess.TimeoutExpired:
            winutil.kill_tree(proc.pid)
            proc.communicate()
            res["error"] = "schtasks timed out"
            return res
        if proc.returncode != 0:
            res["error"] = "schtasks returned an error"
            return res
        txt = raw.decode("oem", "replace") if winutil.IS_WINDOWS else raw.decode("utf-8", "replace")
        rows = list(csv.reader(io.StringIO(txt)))
    except FileNotFoundError:
        res["error"] = "schtasks is not available on this system"
        return res
    except Exception as exc:  # noqa: BLE001
        res["error"] = f"{type(exc).__name__}: {_redact(str(exc))}"
        return res

    res["available"] = True
    header = rows[0] if rows else []
    seen: set[str] = set()
    for row in rows[1:]:
        if row == header or len(row) < 19:
            continue
        name, status, action, start_in = row[1], row[3], row[8], row[9]
        state = row[11] if len(row) > 11 else ""
        hay = f"{action} {start_in}".lower()
        keys = [k for k, marks in SCHEDULE_MARKERS.items() if any(mk.lower() in hay for mk in marks)]
        if not keys or name in seen:
            continue
        seen.add(name)
        disabled = "disabled" in (status or "").lower() or "disabled" in (state or "").lower()
        task = {"name": name.lstrip("\\"), "keys": keys, "enabled": not disabled, "next_run": row[2], "last_run": row[5],
                "last_result": row[6], "schedule": row[18] if len(row) > 18 else ""}
        res["tasks"].append(task)
        if not disabled:
            for k in keys:
                res["matches"].setdefault(k, []).append(task["name"])
    return res


# ============================================================================ automation
KIND_PRIORITY = ("event", "scheduled", "background")   # "real automation" kinds


class _Ctx:
    """Everything one refresh knows, handed to the health functions."""

    def __init__(self, v: dict, sched: dict, stream_state: str, report_alive: bool, digest_job: dict | None) -> None:
        self.v = v
        self.sched = sched
        self.stream_state = stream_state
        self.report_alive = report_alive
        self.digest_job = digest_job or {}

    def scheduled(self, key: str) -> bool | None:
        if not self.sched.get("available"):
            return None
        return key in self.sched.get("matches", {})


def _armed_one(spec: str, ctx: _Ctx) -> bool | None:
    if spec == "always":
        return True
    if spec == "external":
        return None
    if spec.startswith("probe:"):
        return bool(ctx.v["probe"].get(spec[6:]))
    if spec == "flag:digest_auto":
        return bool(ctx.v["digest"]["auto_generate"])
    if spec == "flag:report_refresher":
        return ctx.report_alive
    if spec.startswith("schedule:"):
        return ctx.scheduled(spec[9:])
    return None


def _armed(spec, ctx: _Ctx) -> bool | None:
    specs = spec if isinstance(spec, (list, tuple)) else [spec]
    vals = [_armed_one(s, ctx) for s in specs]
    if any(v is True for v in vals):
        return True
    if any(v is None for v in vals):
        return None
    return False


def _trigger_state_text(t: dict, armed: bool | None, ctx: _Ctx) -> str:
    spec = t.get("armed_by")
    specs = spec if isinstance(spec, (list, tuple)) else [spec]
    if armed is True:
        if any(str(s).startswith("schedule:") for s in specs):
            names = [n for s in specs if str(s).startswith("schedule:") for n in ctx.sched.get("matches", {}).get(str(s)[9:], [])]
            return "Scheduled on this machine" + (f": {names[0]}" if names else "")
        if "flag:digest_auto" in specs:
            return "Running: ERP Desk generates it once a day while open"
        if "flag:report_refresher" in specs:
            return "Running: refresher thread is alive"
        if any(str(s).startswith("probe:") for s in specs):
            return "Listener is answering"
        return "Available"
    if armed is False:
        if any(str(s).startswith("schedule:") for s in specs):
            return "Recommended, but NOT scheduled on this machine" if t.get("recommended") else "Not scheduled on this machine"
        if any(str(s).startswith("probe:") for s in specs):
            return "Not running right now - start it to arm this trigger"
        if "flag:digest_auto" in specs:
            return "Switched off (DESKTOP_AUTO_DIGEST=0)"
        return "Not armed"
    if any(str(s).startswith("schedule:") for s in specs):
        return "Could not check Task Scheduler" if ctx.sched.get("error") else "Checking Task Scheduler..."
    return "Happens outside this machine - cannot be verified here"


def _edge_state(e: dict, ctx: _Ctx) -> tuple[str, str]:
    """(mode, why) of one edge, from ITS OWN armed_by - never from the driver node's aggregate state.

    live = the trigger behind this very hop is armed right now; dormant = automated by design but the
    trigger is not armed; unknown = cannot be verified from here (e.g. Task Scheduler not readable).
    """
    if e["trigger"] == "passive":
        return "passive", "Nothing runs: it is read whenever something needs it."
    if e["trigger"] not in KIND_PRIORITY:
        return "manual", "Moves only when a person starts it."
    spec = e.get("armed_by")
    if spec is None:   # a missing armed_by must never look 'live'
        return "unknown", "No armed_by declared for this hop in flow_definition.EDGES."
    armed = _armed(spec, ctx)
    why = _trigger_state_text({"armed_by": spec, "recommended": bool(e.get("recommended"))}, armed, ctx)
    return {True: "live", False: "dormant"}.get(armed, "unknown"), why


def _resolve_triggers(node: dict, ctx: _Ctx) -> tuple[list[dict], str, str]:
    """(triggers with live state, automation, effective kind)."""
    trig = []
    for t in node["triggers"]:
        armed = _armed(t.get("armed_by", "always"), ctx)
        trig.append({"kind": t["kind"], "label": t["label"], "detail": t.get("detail", ""),
                     "recommended": bool(t.get("recommended")), "armed": armed,
                     "state": _trigger_state_text(t, armed, ctx)})
    auto_kinds = [t for t in trig if t["kind"] in KIND_PRIORITY]
    active = [t for t in auto_kinds if t["armed"] is True]
    if active:
        if node.get("roles") and len(active) < len(auto_kinds):
            return trig, "partial", active[0]["kind"]   # separate jobs: only some of them are running
        return trig, "active", active[0]["kind"]
    if auto_kinds:
        intended = auto_kinds[0]["kind"]
        return trig, "available", intended
    kinds = {t["kind"] for t in trig}
    if "manual" in kinds:
        return trig, "manual", "manual"
    if "passive" in kinds:
        return trig, "passive", "passive"
    return trig, "external", "external"


# ============================================================================ health functions
def _h(state: str, detail: str, ok: str | None = None, err: str | None = None, notes: list[str] | None = None) -> dict:
    return {"state": state, "detail": detail, "last_ok_at": ok, "last_error_at": err, "notes": notes or []}


def _classify(ok: str | None, err: str | None, expected_h: float | None) -> str:
    if err and (not ok or err > ok):
        return "error"
    if not ok:
        return "never-run"
    if expected_h:
        age = _age_hours(ok)
        if age is not None and age > expected_h * STALE_FACTOR:
            return "stale"
    return "healthy"


def _script_h(c: _Ctx, node: dict, ok_extra: list | None = None, err_extra: list | None = None,
              no_record: str = "No run recorded anywhere (the .bat / command line leaves no trace; only Script Center runs are logged).",
              mocked: str | None = None) -> dict:
    rel = node.get("script") or ""
    s = c.v["center"]["per_script"].get(rel, {})
    ok = _latest(s.get("last_ok"), *(ok_extra or []))
    err = _latest(s.get("last_error"), *(err_extra or []))
    expected = node.get("expected_every_hours")
    state = _classify(ok, err, expected)
    notes = []
    if state == "never-run":
        detail = no_record
    elif state == "error":
        detail = f"Last failure {_ago(err)}, after the last success ({_ago(ok)})." if ok else f"Last failure {_ago(err)}; no success recorded."
    elif state == "stale":
        detail = f"Last run {_ago(ok)}; expected about every {_fmt_hours(expected)}."
    else:
        detail = f"Last run {_ago(ok)}."
    if err and ok and err < ok:
        notes.append(f"A failure was logged {_ago(err)}, followed by a successful run {_ago(ok)}.")
    if mocked and state in ("healthy", "never-run"):
        state = "mocked" if state == "healthy" else state
        notes.append(mocked)
    return _h(state, detail, ok, err, notes)


def _fmt_hours(h: float | None) -> str:
    if not h:
        return "-"
    if h < 1:
        return f"{round(h * 60)} minutes"
    if h < 48:
        return f"{h:g} hours"
    return f"{round(h / 24)} days"


def _db_guard(fn):
    def wrapper(c: _Ctx, node: dict) -> dict:
        if not c.v["db"]["ok"]:
            return _h("unavailable", "Database unreachable - " + (c.v["db"]["error"] or "no details"))
        return fn(c, node)
    return wrapper


def _hl_lead_form(c, n):
    L = c.v["leads"]
    if not L["total"]:
        return _h("never-run", "No lead has reached the database yet.")
    return _h("healthy", f"{L['total']} leads from {L['sources']} distinct sources; latest {_ago(L['last_at'])}.", L["last_at"])


def _hl_webhook(c, n):
    L = c.v["leads"] if c.v["db"]["ok"] else {"last_at": None, "total": None}
    up = c.v["probe"]["webhook"]
    if up:
        return _h("healthy", "The listener answers on 127.0.0.1:8000.", L["last_at"])
    tail = f" The last lead reached the database {_ago(L['last_at'])}." if L["last_at"] else " No lead has ever been received."
    return _h("offline", "Nothing is listening on 127.0.0.1:8000 - the webhook only runs while run_webhook.bat (or uvicorn) is open." + tail, L["last_at"])


def _hl_ticket_entry(c, n):
    T = c.v["tickets"]
    if not T["total"]:
        return _h("never-run", "No tickets in the database (no ingestion script exists; rows are added by SQL).")
    return _h("healthy", f"{T['total']} tickets, {T['open']} open.", T["last_at"])


def _hl_leads_logic(c, n):
    A, G = c.v["assign"], c.v["gaps"]
    if not A["total"]:
        return _h("never-run", "No lead has been assigned yet.")
    if G.get("no_assignment"):
        return _h("error", f"{G['no_assignment']} lead(s) have no assignment - the pipeline stopped after the insert.", A["last_at"], A["last_at"])
    return _h("healthy", f"Every lead has a Sales rep ({A['rr']} round-robin, {A['dup']} duplicate re-routes).", A["last_at"])


def _hl_postgres(c, n):
    if not c.v["db"]["ok"]:
        return _h("error", "Cannot connect: " + (c.v["db"]["error"] or "unknown error"))
    rows = sum((c.v[k].get("total") or 0) for k in ("leads", "assign", "ai", "clickup", "updates", "users", "tickets"))
    return _h("healthy", f"Connected. {rows} rows across the 7 tracked tables.", c.v["leads"]["last_at"])


def _hl_views(c, n):
    V = c.v["views"]
    if V["leads_rows"] is None or V["tickets_rows"] is None:
        return _h("error", "A reporting view could not be queried (was 05_leads_sla.sql / 01_schema.sql applied?).")
    if not V["role_ok"]:
        return _h("never-run", "Views are fine, but the read-only role powerbi_reader has not been created (06_powerbi_readonly.sql).")
    return _h("healthy", f"Both views answer; role powerbi_reader exists.")


def _hl_deepseek(c, n):
    A = c.v["ai"]
    if not c.v["env"]["deepseek"]:
        return _h("mocked", "DEEPSEEK_API_KEY is not set: the fixed heuristic fallback runs and every score is a placeholder.", A["last_at"])
    if not A["total"]:
        return _h("never-run", "API key is configured, but no lead has been analyzed yet.")
    notes = []
    if A["fallback"]:
        notes.append(f"{A['fallback']} of {A['total']} analyses came from the fallback (no raw AI response stored: API error or earlier mock mode).")
    return _h("healthy", f"{A['real']} real AI analyses stored; latest {_ago(A['last_at'])}.", A["last_at"], None, notes)


def _hl_clickup_push(c, n):
    K = c.v["clickup"]
    if K.get("failed"):
        return _h("error", f"{K['failed']} task(s) failed to sync (see lead_clickup_sync.error_message).", K["last_at"], K["last_at"])
    if not c.v["env"]["clickup"]:
        return _h("mocked", "CLICKUP_API_TOKEN / CLICKUP_LIST_ID are not set: tasks are created in mock mode (MOCK-xxxx ids).", K.get("last_at"))
    if not K.get("created"):
        return _h("never-run", "ClickUp is configured, but no task has been created yet.")
    notes = []
    if K.get("mocked"):
        notes.append(f"{K['mocked']} earlier task(s) were mock tasks.")
    if K.get("pending"):
        notes.append(f"{K['pending']} task(s) still pending.")
    return _h("healthy", f"{K['synced']} real ClickUp tasks created; latest {_ago(K['last_at'])}.", K["last_at"], None, notes)


def _hl_clickup(c, n):
    K = c.v["clickup"]
    if not c.v["env"]["clickup"]:
        return _h("mocked", "ClickUp is not configured - the workspace is never contacted.")
    if not K.get("synced"):
        return _h("never-run", "ClickUp is configured, but no task exists there yet.")
    return _h("healthy", f"{K['synced']} tasks live in the workspace.", K["last_at"])


def _hl_clickup_pull(c, n):
    K, U = c.v["clickup"], c.v["updates"]
    scheduled = c.scheduled("clickup_pull")
    if not K.get("synced"):
        if not c.v["env"]["clickup"]:
            return _h("mocked", "ClickUp is not configured. Only 'synced' tasks are pulled, so mock tasks are skipped.")
        return _h("never-run", "No synced ClickUp task exists yet, so there is nothing to pull.")
    h = _script_h(c, n, ok_extra=[K.get("last_pull_at")], no_record="No pull recorded yet.")
    if scheduled is False:
        h["notes"].insert(0, "Not scheduled: comments are pulled only when someone runs it (README recommends every ~15 min).")
    elif scheduled is None:
        h["notes"].insert(0, "Task Scheduler could not be checked.")
    if h["state"] == "stale":
        h["detail"] = f"Last pull {_ago(h['last_ok_at'])} - it should run about every 15 minutes." + (
            " No scheduled task exists." if scheduled is False else "")
    return h


def _hl_sync_employees(c, n):
    U = c.v["users"]
    h = _script_h(c, n, no_record="No run recorded (this script keeps no log).")
    if U["linked"]:
        state = "healthy" if h["state"] in ("healthy", "never-run") else h["state"]
        return _h(state, f"{U['linked']} of {U['total']} staff carry a ClickUp id, so it has run at least once (no run log is kept).", h["last_ok_at"], h["last_error_at"], h["notes"])
    if not c.v["env"]["clickup"]:
        return _h("mocked", "ClickUp is not configured, so there are no members to sync.")
    return h


def _hl_daily_check(c, n):
    h = _script_h(c, n)
    A = c.v["attention"]
    if A.get("total") is not None:
        h["notes"].append(f"Right now it would list {A['total']} item(s) needing attention.")
    return h


def _hl_daily_digest(c, n):
    d, dl = c.v["digest"], c.v["dlog"]
    h = _script_h(c, n, ok_extra=[d["generated_at"]], err_extra=[dl["last_error_at"]],
                  no_record="No digest has been generated yet.")
    if dl["last_error"] and dl["last_error_at"] and (not h["last_ok_at"] or dl["last_error_at"] > h["last_ok_at"]):
        h["detail"] = f"Last run failed {_ago(dl['last_error_at'])}: {dl['last_error']}"
    if d["exists"] and d["ai_configured"] is False and h["state"] == "healthy":
        h["state"] = "mocked"
        h["notes"].append("The digest was written without an AI key: the narrative is a placeholder note.")
    job = c.digest_job
    if job.get("started_at"):
        when = _ago(_local_iso((job.get("finished_at") or job["started_at"])[:19]))
        h["notes"].append(f"In-app run ({job.get('trigger')}) {when}: {job.get('state')}" + (f" - {job['message']}" if job.get("message") else "") + ".")
    if d["exists"]:
        h["notes"].append("Emailed on its last run." if d["email_sent"] else "Its last run did not email (in-app runs never email; SMTP is also unconfigured)." if not c.v["env"]["smtp"] else "Its last run did not email (in-app runs never email).")
    return h


def _hl_weekly_report(c, n):
    return _script_h(c, n, no_record="No run recorded anywhere (it prints to the console and keeps no log; only Script Center runs are logged).",
                     mocked=None if c.v["env"]["deepseek"] else "DEEPSEEK_API_KEY is not set: it would dump raw JSON instead of an AI report.")


def _hl_emailer(c, n):
    d = c.v["digest"]
    if not c.v["env"]["smtp"]:
        return _h("mocked", "SMTP_HOST / ALERT_EMAIL_FROM / ALERT_EMAIL_TO are not all set: every send is skipped with a warning.")
    if d["exists"] and d["email_sent"]:
        return _h("healthy", f"SMTP configured; the last digest was emailed.", d["generated_at"])
    return _h("never-run", "SMTP is configured, but no emailed digest is recorded yet.")


def _hl_inbox(c, n):
    if not c.v["env"]["smtp"]:
        return _h("mocked", "ALERT_EMAIL_TO / SMTP are not configured - nothing is delivered.")
    return _h("healthy", "Recipient configured (delivery is up to your mail server).")


def _hl_digest_files(c, n):
    d = c.v["digest"]
    if not d["exists"]:
        return _h("never-run", "daily_digest_latest.json does not exist yet.")
    if d["is_today"]:
        return _h("healthy", f"Today's digest is on disk ({d['days']} day(s) of history).", d["generated_at"])
    return _h("stale", f"The latest digest is from {d['date']}, not today.", d["generated_at"])


def _hl_live_report(c, n):
    R = c.v["report"]
    if R.get("error"):
        return _h("error", f"Last DB re-query failed: {_redact(R['error'])}. The page keeps showing the last good data.", R.get("refreshed_at"), R.get("refreshed_at"))
    if not R.get("has_data"):
        return _h("never-run", "The first snapshot is still loading.")
    return _h("healthy", f"Re-queried {R['refresh_count']} time(s), every {R['interval']} s.", R.get("refreshed_at"))


def _hl_html_report(c, n):
    F = c.v["files"]
    if not F["report_html_at"]:
        return _h("never-run", "erp_report.html has not been generated yet.")
    return _h("healthy", f"erp_report.html was written {_ago(F['report_html_at'])} ({F['report_html_kb']} KB).", F["report_html_at"])


def _hl_timeline_chart(c, n):
    F = c.v["files"]
    if not F["timeline_png_at"]:
        return _h("never-run", "task_timeline.png has not been generated yet (python -m erp.task_timeline_chart).")
    return _h("healthy", f"task_timeline.png was written {_ago(F['timeline_png_at'])} ({F['timeline_png_kb']} KB).", F["timeline_png_at"])


def _hl_cskh(c, n):
    if c.v["probe"]["cskh_dashboard"]:
        return _h("healthy", "Running on http://localhost:8501.")
    return _h("idle", "Not open right now (run_dashboard.bat starts it on demand).")


def _hl_script_center(c, n):
    C = c.v["center"]
    st = c.stream_state
    base = {"ready": "healthy", "starting": "stale", "restarting": "stale", "down": "error"}.get(st, "healthy")
    detail = {"ready": "The Streamlit process is up (supervised by ERP Desk).", "starting": "Starting up...",
              "restarting": "Restarting after an unexpected exit...", "down": "Stopped several times in a row - see erp_desktop_streamlit.log."}.get(st, "Status unknown.")
    if C["entries"]:
        detail += f" {C['entries']} logged action(s), {C['errors']} failed; last activity {_ago(C['last_at'])}."
    return _h(base, detail, C["last_at"], C["last_error_at"] if C["errors"] else None)


def _hl_powerbi(c, n):
    if c.v["views"]["role_ok"]:
        return _h("healthy", "The read-only role powerbi_reader exists; Power BI itself is outside this app (cannot see whether it is open).")
    return _h("never-run", "The role powerbi_reader has not been created yet.")


HEALTH: dict[str, Callable] = {
    "lead_form": _db_guard(_hl_lead_form), "webhook": _hl_webhook, "ticket_entry": _db_guard(_hl_ticket_entry),
    "leads_logic": _db_guard(_hl_leads_logic), "postgres": _hl_postgres, "views": _db_guard(_hl_views),
    "deepseek": _db_guard(_hl_deepseek), "clickup_push": _db_guard(_hl_clickup_push), "clickup": _db_guard(_hl_clickup),
    "clickup_pull": _db_guard(_hl_clickup_pull), "sync_employees": _db_guard(_hl_sync_employees),
    "daily_check": _hl_daily_check, "daily_digest": _hl_daily_digest, "weekly_report": _hl_weekly_report,
    "emailer": _hl_emailer, "inbox": _hl_inbox, "digest_files": _hl_digest_files, "live_report": _hl_live_report,
    "html_report": _hl_html_report, "timeline_chart": _hl_timeline_chart, "cskh_dashboard": _hl_cskh, "script_center": _hl_script_center,
    "powerbi": _db_guard(_hl_powerbi),
}


# ============================================================================ map sync check
# The map (flow_definition.py) is declared by hand. These checks tell the user when it has drifted from
# the project, so a forgotten script / a deleted file / a typo in an edge can never go unnoticed.
# Everything here is read-only, takes no input from the browser, and degrades to "check unavailable".
FIX_HINT = ("Add a node (with its edges) to NODES / EDGES in desktop/flow_definition.py, or add the file to IGNORE "
            "there with a one-line reason, then restart ERP Desk.")
NODE_TRIGGER_KINDS = ("event", "scheduled", "background", "manual", "passive", "external")
EDGE_TRIGGER_KINDS = ("event", "scheduled", "background", "manual", "passive")
ARMED_FLAGS = ("flag:digest_auto", "flag:report_refresher")
COUNTER_KEYS = ("leads", "assign", "ai", "clickup", "updates", "staff", "tickets", "digests", "refreshes", "reports")


def _file_mtime(path: str | None) -> float | None:
    try:
        return os.stat(path).st_mtime if path else None
    except OSError:
        return None


_DEF_FILE = getattr(flow_definition, "__file__", None)
_DEF_MTIME_AT_START = _file_mtime(_DEF_FILE)   # flow_definition.py is read once, when this process starts


def _key(rel: str) -> str:
    """Comparison key of a project-relative path (Windows paths are case-insensitive)."""
    return rel.casefold() if winutil.IS_WINDOWS else rel


def _clean_rel(p) -> str | None:
    """A project-relative posix path, or None when it is empty, absolute or climbs out of the project."""
    if not isinstance(p, str):
        return None
    s = p.strip().replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    if not s or s.startswith("/") or re.match(r"^[A-Za-z]:", s) or ".." in s.split("/"):
        return None
    return s


def declared_files(node: dict) -> list[str]:
    """The source files a node stands for: its explicit "files" list, else the paths in "script" / "file"."""
    raw = node.get("files")
    if raw is None:
        raw = []
        for k in ("script", "file"):
            v = node.get(k)
            if isinstance(v, str):
                raw.extend(v.split(","))
    seen: set[str] = set()
    out = []
    for x in raw:
        if isinstance(x, str) and x.strip() and x.strip() not in seen:
            seen.add(x.strip())
            out.append(x.strip())
    return out


def scan_scripts(root: Path) -> list[str]:
    """Every project .py script (relative posix paths), minus the standard exclusions in flow_definition.SCAN.

    Walks the whole project tree but prunes hidden folders, virtualenvs and SCAN["skip_dirs"], so it is cheap
    (a few ms here). Does not follow symlinks.
    """
    skip_dirs = {d.casefold() for d in SCAN.get("skip_dirs", [])}
    skip_files = {f.casefold() for f in SCAN.get("skip_files", [])}
    prefixes = tuple(p.casefold() for p in SCAN.get("skip_prefixes", []))
    suffixes = tuple(x.casefold() for x in SCAN.get("skip_suffixes", []))
    out: list[str] = []
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d.casefold() not in skip_dirs
                             and not os.path.exists(os.path.join(dirpath, d, "pyvenv.cfg")))
        for fn in sorted(filenames):
            seen += 1
            if seen > MAP_SCAN_LIMIT:
                raise RuntimeError("the project tree is too large to scan")
            low = fn.casefold()
            if not low.endswith(".py") or low in skip_files or low.startswith(prefixes) or low.endswith(suffixes):
                continue
            out.append(Path(dirpath, fn).relative_to(root).as_posix())
    out.sort(key=str.casefold)
    return out


def _first_doc_line(path: Path) -> str | None:
    """First meaningful line of a script's module docstring; None when there is none or the file cannot be parsed."""
    try:
        if path.stat().st_size > 512_000:
            return None
        doc = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8-sig", errors="replace")))
    except Exception:  # noqa: BLE001 - a malformed / unreadable script must never break the feed
        return None
    for line in (doc or "").splitlines():
        if re.search(r"[A-Za-z0-9]", line):
            return _redact(line.strip(), 160) or None
    return None


def map_integrity(nodes: list | None = None, edges: list | None = None, lanes: list | None = None,
                  trace: list | None = None) -> list[dict]:
    """Inconsistencies inside the definition itself (each would otherwise show up as a silent 'unknown' or a blank)."""
    nodes = NODES if nodes is None else nodes
    edges = EDGES if edges is None else edges
    lanes = LANES if lanes is None else lanes
    trace = TRACE if trace is None else trace
    issues: list[dict] = []

    def add(kind: str, where, message: str) -> None:
        issues.append({"kind": kind, "where": str(where)[:80], "message": message})

    def bad_spec(spec) -> str | None:
        for sp in (spec if isinstance(spec, (list, tuple)) else [spec]):
            ok = (sp in ("always", "external") or sp in ARMED_FLAGS
                  or (isinstance(sp, str) and sp.startswith("probe:") and sp[6:] in PROBES)
                  or (isinstance(sp, str) and sp.startswith("schedule:") and sp[9:] in SCHEDULE_MARKERS))
            if not ok:
                return str(sp)
        return None

    ids: set = set()
    lane_ids = {ln.get("id") for ln in lanes}
    for n in nodes:
        nid = n.get("id")
        if nid in ids:
            add("duplicate_id", nid, f"Node id '{nid}' is used more than once.")
        ids.add(nid)
        if n.get("lane") not in lane_ids:
            add("unknown_lane", nid, f"Node '{nid}' is on lane '{n.get('lane')}', which is not in LANES.")
        for t in n.get("triggers") or []:
            if t.get("kind") not in NODE_TRIGGER_KINDS:
                add("bad_trigger", nid, f"Node '{nid}' has a trigger of unknown kind '{t.get('kind')}'.")
            bad = bad_spec(t.get("armed_by", "always"))
            if bad:
                add("bad_armed_by", nid, f"Node '{nid}' has a trigger with the unknown armed_by '{bad}'.")
    edge_ids: set = set()
    for e in edges:
        eid = e.get("id")
        if eid in edge_ids:
            add("duplicate_id", eid, f"Edge id '{eid}' is used more than once.")
        edge_ids.add(eid)
        for k in ("from", "to", "driver"):
            if e.get(k) not in ids:
                add("unknown_node", eid, f"Edge '{eid}' refers to the unknown node '{e.get(k)}' ({k}).")
        if e.get("trigger") not in EDGE_TRIGGER_KINDS:
            add("bad_trigger", eid, f"Edge '{eid}' has the unknown trigger '{e.get('trigger')}'.")
        elif e["trigger"] in KIND_PRIORITY and e.get("armed_by") is None:
            add("missing_armed_by", eid, f"Automated edge '{eid}' has no armed_by, so it can only ever show as 'unknown'.")
        if e.get("armed_by") is not None:
            bad = bad_spec(e["armed_by"])
            if bad:
                add("bad_armed_by", eid, f"Edge '{eid}' has the unknown armed_by '{bad}'.")
        if e.get("counter") and e["counter"] not in COUNTER_KEYS:
            add("bad_counter", eid, f"Edge '{eid}' names the unknown counter '{e['counter']}'.")
    for st in trace:
        if st.get("edge") not in edge_ids:
            add("unknown_edge", st.get("edge"), f"TRACE refers to the unknown edge '{st.get('edge')}'.")
    return issues


def check_map(root: Path, nodes: list | None = None, ignore: dict | None = None) -> dict:
    """Compare the scripts on disk with the map. Raises on an unexpected failure (the caller degrades)."""
    nodes = NODES if nodes is None else nodes
    ignore = IGNORE if ignore is None else ignore
    scripts = scan_scripts(root)

    covered: dict[str, str] = {}          # comparison key -> node id
    stale_nodes: list[dict] = []
    for n in nodes:
        for raw in declared_files(n):
            rel = _clean_rel(raw)
            if rel is None:
                reason = "not a project-relative path"
            elif not (root / rel).is_file():
                reason = "file not found (moved, renamed or deleted?)"
            else:
                covered.setdefault(_key(rel), n.get("id"))
                continue
            stale_nodes.append({"node": n.get("id"), "label": n.get("label"), "path": str(raw)[:200], "reason": reason})

    ignored: dict[str, tuple[str, str]] = {}   # key -> (relative path, reason)
    stale_ignores: list[dict] = []
    for raw, why in ignore.items():
        rel = _clean_rel(raw)
        why = why.strip() if isinstance(why, str) else ""
        if rel is None:
            stale_ignores.append({"path": str(raw)[:200], "reason": "not a project-relative path"})
        elif not (root / rel).is_file():
            stale_ignores.append({"path": rel, "reason": "the file no longer exists - remove this IGNORE entry"})
        elif _key(rel) in covered:
            stale_ignores.append({"path": rel, "reason": f"node '{covered[_key(rel)]}' covers it now - remove this IGNORE entry"})
        elif not why:
            stale_ignores.append({"path": rel, "reason": "no reason given - say in a few words why it is not part of the flow"})
        else:
            ignored[_key(rel)] = (rel, why)

    unmapped: list[dict] = []
    mapped = 0
    ignored_seen: list[dict] = []
    for rel in scripts:
        k = _key(rel)
        if k in covered:
            mapped += 1
        elif k in ignored:
            ignored_seen.append({"path": ignored[k][0], "reason": ignored[k][1][:240]})
        else:
            unmapped.append({"path": rel, "doc": _first_doc_line(root / rel)})

    try:
        integrity = map_integrity()
    except Exception as exc:  # noqa: BLE001 - a malformed definition entry: report it, keep the rest of the check
        integrity = [{"kind": "check_failed", "where": "definition", "message": f"Could not validate the definition: {type(exc).__name__}: {_redact(str(exc))}"}]
    problems = len(unmapped) + len(stale_nodes) + len(stale_ignores) + len(integrity)
    return {"available": True, "error": None, "in_sync": problems == 0, "scanned": len(scripts), "mapped": mapped,
            "ignored": ignored_seen, "unmapped": unmapped, "stale_nodes": stale_nodes, "stale_ignores": stale_ignores,
            "integrity": integrity, "hint": FIX_HINT, "checked_at": _iso(datetime.now(timezone.utc))}


def map_check_unavailable(exc: BaseException) -> dict:
    return {"available": False, "error": f"{type(exc).__name__}: {_redact(str(exc))}", "in_sync": None, "scanned": 0, "mapped": 0,
            "ignored": [], "unmapped": [], "stale_nodes": [], "stale_ignores": [], "integrity": [], "hint": FIX_HINT,
            "checked_at": _iso(datetime.now(timezone.utc))}


# ============================================================================ snapshot
def _resolve_path(v: dict, path: str):
    cur = v
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _empty_metrics() -> dict:
    z = lambda *keys: {k: None for k in keys}  # noqa: E731
    return {
        "leads": z("total", "last_at", "n24h", "n7d", "sources"),
        "assign": z("total", "current", "last_at", "dup", "rr", "n24h", "reps_active"),
        "ai": z("total", "real", "fallback", "last_at", "n24h", "avg_score"),
        "clickup": {}, "updates": z("total", "last_at", "last_occurred", "n7d", "n24h"),
        "users": z("total", "linked", "sales_active"), "tickets": z("total", "open", "last_at"),
        "gaps": {}, "views": {"leads_rows": None, "tickets_rows": None, "role_ok": False}, "attention": {},
    }


def _build_trace(row: dict | None) -> dict | None:
    """The newest lead's real journey, as the steps flow_definition.TRACE describes."""
    if not row:
        return None
    reached = {
        "received": (True, _iso(row["created_at"])),
        "assigned": (row["assigned_at"] is not None, _iso(row["assigned_at"])),
        "analyzed": (row["analyzed_at"] is not None, _iso(row["analyzed_at"])),
        "task": (row["sync_status"] in ("synced", "mocked"), _iso(row["synced_at"])),
        "update": (row["update_at"] is not None, _iso(row["update_at"])),
    }
    fmt = {"source": row["source"] or "unknown", "reason": row["reason"] or "-", "score": row["score"] if row["score"] is not None else "n/a",
           "sync": row["sync_status"] or "-"}
    steps = []
    for st in TRACE:
        ok, at = reached[st["stage"]]
        steps.append({"edge": st["edge"], "stage": st["stage"], "reached": ok, "at": at, "text": st["text"].format(**fmt)})
    return {"lead_id": row["id"], "source": fmt["source"], "created_at": reached["received"][1], "steps": steps}


class FlowStore:
    def __init__(self, project_root: Path, report=None, digest=None,
                 streamlit_state: Callable[[], str] | None = None, interval_seconds: int = 10,
                 job: "winutil.JobObject | None" = None) -> None:
        self.root = project_root
        self.report = report
        self.digest = digest
        self.streamlit_state = streamlit_state or (lambda: "unknown")
        self.interval = max(3, int(interval_seconds))
        self.job = job
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._stop = threading.Event()
        self._snapshot: dict | None = None
        self._error: str | None = None
        self._last_logged_error: str | None = None
        self._refreshed_at: str | None = None
        self._refresh_count = 0
        self._sched: dict = {"available": None, "error": None, "checked_at": None, "tasks": [], "matches": {}, "pending": True}
        self._threads: list[threading.Thread] = []
        self._map_cache: dict | None = None        # last map sync check (see _map_check)
        self._map_cache_at = 0.0
        self._map_logged: str | None = None
        self._layout_check()

    @staticmethod
    def _layout_check() -> None:
        """Log the definition's own inconsistencies once at start-up (they also appear on the page, in the map card)."""
        try:
            for issue in map_integrity():
                logger.warning("flow_definition: %s", issue["message"])
        except Exception:  # noqa: BLE001
            logger.exception("flow_definition check failed")

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        for target, name in ((self._loop, "flow-refresher"), (self._sched_loop, "flow-scheduler-probe")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.refresh()
            self._stop.wait(self.interval)

    def _sched_loop(self) -> None:
        while not self._stop.is_set():
            try:
                res = scan_scheduler(self.job)
            except Exception:  # noqa: BLE001
                logger.exception("Task Scheduler scan crashed")
                res = {"available": False, "error": "scan failed", "checked_at": None, "tasks": [], "matches": {}}
            with self._lock:
                self._sched = res
            if res["available"]:
                logger.info("Task Scheduler scan: %d task(s) belong to this project", len(res["tasks"]))
            elif res["error"]:
                logger.warning("Task Scheduler scan failed: %s", res["error"])
            self.refresh()
            self._stop.wait(SCHEDULER_TTL)

    # -- map sync check -------------------------------------------------------
    def _map_check(self, force: bool = False) -> dict:
        """The map-vs-project check, cached for MAP_CHECK_TTL seconds (`force` = the Refresh button). Never raises."""
        now = time.monotonic()
        if self._map_cache is None or force or now - self._map_cache_at >= MAP_CHECK_TTL:
            try:
                res = check_map(self.root)
            except Exception as exc:  # noqa: BLE001 - degrade to "check unavailable", never break the feed
                res = map_check_unavailable(exc)
            self._map_cache, self._map_cache_at = res, now
            sig = json.dumps([res["available"], res["error"], [u["path"] for u in res["unmapped"]],
                              [(s["node"], s["path"]) for s in res["stale_nodes"]], [s["path"] for s in res["stale_ignores"]],
                              [i["message"] for i in res["integrity"]]], sort_keys=True)
            if sig != self._map_logged:   # log a change once, not on every re-scan
                self._map_logged = sig
                if not res["available"]:
                    logger.warning("Data Flow map check unavailable: %s", res["error"])
                elif res["in_sync"]:
                    logger.info("Data Flow map is in sync (%d scripts mapped)", res["mapped"])
                else:
                    logger.warning("Data Flow map is out of date: %d unmapped script(s) %s, %d stale node file(s), %d stale IGNORE entr(ies), %d inconsistency(ies)",
                                   len(res["unmapped"]), [u["path"] for u in res["unmapped"]], len(res["stale_nodes"]),
                                   len(res["stale_ignores"]), len(res["integrity"]))
        out = dict(self._map_cache)
        mt = _file_mtime(_DEF_FILE)
        out["definition_changed"] = bool(mt is not None and _DEF_MTIME_AT_START is not None and mt != _DEF_MTIME_AT_START)
        return out

    # -- build ---------------------------------------------------------------
    def _build(self, force_check: bool = False) -> dict:
        v: dict = _empty_metrics()
        db_metrics, db_error = _db_metrics()
        trace_row = db_metrics.pop("_trace_row", None)
        v.update(db_metrics)
        v["db"] = {"ok": db_error is None, "error": db_error}
        v["env"] = _env_flags()
        v["probe"] = {k: _probe(url) for k, url in PROBES.items()}
        v.update(_file_metrics(self.root, self.digest))
        rmeta = self.report.meta() if self.report else {}
        report_alive = bool(self.report and getattr(getattr(self.report, "_thread", None), "is_alive", lambda: False)())
        v["report"] = {"refresh_count": rmeta.get("refresh_count"), "refreshed_at": _iso_from_offset(rmeta.get("refreshed_at")),
                       "interval": rmeta.get("interval_seconds") or self.interval, "error": rmeta.get("error"), "has_data": bool(rmeta.get("has_data"))}
        with self._lock:
            sched = dict(self._sched)
        digest_job = self.digest.job_state() if self.digest else None
        ctx = _Ctx(v, sched, self.streamlit_state(), report_alive, digest_job)

        counters = {
            "leads": v["leads"], "assign": v["assign"], "ai": v["ai"],
            "clickup": {"total": v["clickup"].get("created"), "last_at": v["clickup"].get("last_at"), "n24h": v["clickup"].get("n24h")},
            "updates": v["updates"],
            "staff": {"total": v["users"]["linked"], "last_at": None, "n24h": None},
            "tickets": v["tickets"],
            "digests": {"total": v["digest"]["days"], "last_at": v["digest"]["generated_at"], "n24h": None},
            "refreshes": {"total": v["report"]["refresh_count"], "last_at": v["report"]["refreshed_at"], "n24h": None},
            "reports": {"total": 1 if v["files"]["report_html_at"] else 0, "last_at": v["files"]["report_html_at"], "n24h": None},
        }

        nodes = []
        by_id: dict[str, dict] = {}
        for n in NODES:
            triggers, automation, effective = _resolve_triggers(n, ctx)
            fn = HEALTH.get(n.get("live") or n["id"])
            try:
                health = fn(ctx, n) if fn else _h("info", "Static description - no live data source is wired to this node yet.")
            except Exception:  # noqa: BLE001
                logger.exception("health check for %s failed", n["id"])
                health = _h("unavailable", "The live check for this node crashed - see erp_desktop.log.")
            stats = [{"label": s["label"], "value": _resolve_path(v, s["path"]), "fmt": s.get("fmt", "text")} for s in n.get("stats", [])]
            hd = n.get("headline") or {}
            headline = ({"text": hd["text"]} if "text" in hd else
                        {"value": _resolve_path(v, hd["path"]), "fmt": hd.get("fmt", "text"), "short": hd.get("short", "")}) if hd else None
            script = n.get("script")
            node = {
                "id": n["id"], "label": n["label"], "kind": n["kind"], "icon": n["icon"], "lane": n["lane"],
                "col": n["col"], "row": n["row"], "h": n.get("h"), "stage": n["stage"], "file": n.get("file"),
                "script": script if script and (self.root / script).is_file() else None,
                "summary": n["summary"], "inputs": n["inputs"], "outputs": n["outputs"],
                "triggers": triggers, "automation": automation, "trigger_kind": effective,
                "expected_every_hours": n.get("expected_every_hours"),
                "stats": stats, "headline": headline, "health": health,
            }
            nodes.append(node)
            by_id[n["id"]] = node

        edges = []
        for e in EDGES:
            drv = by_id.get(e["driver"])
            counter = counters.get(e.get("counter")) if e.get("counter") else None
            mode, why = _edge_state(e, ctx)
            edges.append({
                "id": e["id"], "from": e["from"], "to": e["to"], "trigger": e["trigger"], "mode": mode, "mode_why": why,
                "label": e["label"], "data": e["data"], "via": e.get("via") or [],
                "driver_health": drv["health"]["state"] if drv else None,
                "total": counter.get("total") if counter else None,
                "last_at": counter.get("last_at") if counter else None,
                "n24h": counter.get("n24h") if counter else None,
            })

        stages = [n for n in nodes if n["stage"]]
        coverage = {
            "total": len(stages),
            "active": sum(1 for n in stages if n["automation"] == "active"),
            "partial": sum(1 for n in stages if n["automation"] == "partial"),
            "available": sum(1 for n in stages if n["automation"] == "available"),
            "manual": sum(1 for n in stages if n["automation"] not in ("active", "partial", "available")),
            "not_set_up": [   # what the README recommends but this machine does not have (even when another trigger already covers the node)
                {"node": n["id"], "label": n["label"], "hint": t["label"]}
                for n in stages
                for t in n["triggers"] if t["recommended"] and t["armed"] is False
            ],
        }
        map_check = self._map_check(force_check)
        summary = {"leads": v["leads"]["total"], "assigned": v["assign"]["total"], "analyzed": v["ai"]["total"],
                   "tasks": v["clickup"].get("created"), "updates": v["updates"]["total"]}

        sched_out = {"available": sched.get("available"), "error": sched.get("error"), "pending": bool(sched.get("pending")),
                     "tasks": sched.get("tasks", []), "checked_at": sched.get("checked_at")}
        return {
            "layout": LAYOUT, "lanes": LANES, "nodes": nodes, "edges": edges, "coverage": coverage, "summary": summary,
            "scheduler": sched_out, "db": v["db"], "env": v["env"], "probe": v["probe"],
            "trace": _build_trace(trace_row),
            "map_check": map_check,
            "_stable": {   # what decides "did anything change?" (volatile counters like refresh_count are left out)
                "v": {k: v[k] for k in ("leads", "assign", "ai", "clickup", "updates", "users", "tickets", "gaps", "views", "attention",
                                        "digest", "dlog", "center", "files", "env", "probe", "db")},
                "h": {n["id"]: [n["health"]["state"], n["automation"]] for n in nodes},
                "e": {e["id"]: e["mode"] for e in edges},
                "s": [sched.get("available"), sorted(t["name"] for t in sched.get("tasks", []))],
                "m": {k: v for k, v in map_check.items() if k != "checked_at"},
            },
        }

    # -- refresh -------------------------------------------------------------
    def refresh(self, force: bool = False) -> bool:
        """Re-read everything. Returns True when something changed. Never raises.

        `force` (the Refresh button) also re-scans the project for the map sync check right away instead of
        waiting for MAP_CHECK_TTL.
        """
        with self._refresh_lock:
            try:
                data = self._build(force_check=force)
            except Exception as exc:  # noqa: BLE001
                msg = f"{type(exc).__name__}: {_redact(str(exc))}"
                with self._lock:
                    self._error = msg
                if msg != self._last_logged_error:
                    logger.exception("Flow refresh failed")
                    self._last_logged_error = msg
                return False
            if self._last_logged_error:
                logger.info("Flow refresh recovered")
                self._last_logged_error = None
            stable = data.pop("_stable")
            version = hashlib.sha1(json.dumps(stable, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:12]
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            with self._lock:
                changed = self._snapshot is None or self._snapshot["version"] != version
                if changed:
                    self._snapshot = {"version": version, "generated_at": now, "data": data}
                else:   # keep the volatile bits (report counters) fresh without bumping the version
                    self._snapshot["data"] = data
                self._error = None
                self._refreshed_at = now
                self._refresh_count += 1
            if changed:
                logger.info("Flow data changed -> version %s", version)
            return changed

    # -- read side -----------------------------------------------------------
    def meta(self) -> dict:
        with self._lock:
            snap = self._snapshot
            return {"ok": snap is not None and self._error is None, "has_data": snap is not None,
                    "version": snap["version"] if snap else None, "generated_at": snap["generated_at"] if snap else None,
                    "refreshed_at": self._refreshed_at, "error": self._error, "interval_seconds": self.interval}

    def get(self, since: str | None = None) -> dict:
        meta = self.meta()
        with self._lock:
            snap = self._snapshot
            data = snap["data"] if snap else None
        if snap is None:
            return {**meta, "changed": False, "data": None}
        if since and since == snap["version"]:
            return {**meta, "changed": False, "data": data}   # data is tiny; always send it so ages / counters stay fresh
        return {**meta, "changed": True, "data": data}


def _iso_from_offset(s: str | None) -> str | None:
    """'2026-09-20T10:31:28+07:00' -> UTC iso."""
    if not s:
        return None
    try:
        return _iso(datetime.fromisoformat(s))
    except ValueError:
        return None
