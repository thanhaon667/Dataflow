"""Data Flow feed, live readings: the read-only database counts, the log / JSON file readings, the webhook probe, the Task Scheduler scan and the trace (split out of desktop/flow_data.py, unchanged).
"""
from __future__ import annotations

import csv
import io
import json
import re
import subprocess
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from sqlalchemy import text

from desktop import winutil
from desktop.flow_definition import SCHEDULE_MARKERS, TRACE
from erp.db import read_connect, read_engine as engine
from desktop.flow_util import _iso, _latest, _local_iso, _read_tail, _redact, logger


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # never via a proxy


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
        conn = read_connect(engine)
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

        # Marketing / channel interaction data (Phase 1: schema + pipeline, no live connector yet).
        # _q() already returns None on a missing table (L-005 fallback), so this degrades cleanly on a
        # database where db/sql/07_marketing_schema.sql has not been installed - same as every query above.
        r = _q(conn, """SELECT count(*) AS total, max(started_at) AS last_at, sum(rows_loaded) AS rows_loaded,
                        sum(rows_invalid) AS rows_invalid, count(*) FILTER (WHERE status = 'failed') AS failed,
                        count(*) FILTER (WHERE status = 'partial') AS partial
                        FROM marketing_ingest_run""")
        fact = _q(conn, "SELECT count(*) AS n FROM interaction_fact")
        m["marketing"] = {"installed": r is not None, "runs": _i(r and r["total"]), "last_run_at": _iso(r and r["last_at"]),
                          "rows_loaded": _i(r and r["rows_loaded"]), "rows_invalid": _i(r and r["rows_invalid"]),
                          "failed": _i(r and r["failed"]), "partial": _i(r and r["partial"]), "fact_rows": _i(fact and fact["n"])}

        # Phase 4 rollup job (erp/marketing/rollup.py). Reads only the small journal + the per-channel rollup (days x
        # channels rows): never counts interaction_fact or the campaign-level rollup. _q() returns None when db/sql/09
        # has not been installed, so this degrades like every block above.
        r = _q(conn, """SELECT count(*) AS total, max(finished_at) FILTER (WHERE status = 'ok') AS last_ok_at,
                        count(*) FILTER (WHERE status = 'failed') AS failed,
                        (SELECT status FROM marketing_rollup_run ORDER BY started_at DESC LIMIT 1) AS last_status,
                        (SELECT rows_written FROM marketing_rollup_run WHERE status = 'ok' ORDER BY started_at DESC LIMIT 1) AS last_rows,
                        (SELECT error_message FROM marketing_rollup_run ORDER BY started_at DESC LIMIT 1) AS last_error
                        FROM marketing_rollup_run""")
        ch = _q(conn, "SELECT count(*) AS n, max(event_date) AS newest FROM interaction_daily_channel_rollup")
        m["rollup"] = {"installed": r is not None, "runs": _i(r and r["total"]), "failed": _i(r and r["failed"]),
                       "last_ok_at": _iso(r and r["last_ok_at"]), "last_status": r and r["last_status"],
                       "last_rows": _i(r and r["last_rows"]), "last_error": r and r["last_error"],
                       "channel_days": _i(ch and ch["n"]), "newest_day": _iso(ch and ch["newest"])}

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
    """Digest files, daily_digest.log, script_center.log - all read-only."""
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

    # Marketing inbox processor (erp/marketing/autorun.py): the last REAL run it recorded in the inbox's index file.
    # A dry run records nothing, and no file means it has never run - shown as such, never as a zero.
    a: dict = {"ran": False, "at": None, "files": None, "ok": None, "failed": None, "duplicate": None, "rows_loaded": None,
               "rollup": None, "exit_code": None, "problem": None, "result": "never run"}
    try:
        from erp.marketing import autorun
        last = autorun.read_last_run()
        if last:
            failed = int(last.get("failed") or 0)
            a.update(ran=True, at=_local_iso(last.get("at")), files=last.get("files"), ok=last.get("ok"), failed=failed,
                     duplicate=last.get("duplicate"), rows_loaded=last.get("rows_loaded"), rollup=last.get("rollup"),
                     exit_code=last.get("exit_code"), problem=last.get("problem"))
            a["result"] = (("FAILED" if last.get("exit_code") else "ok")
                           + f": {last.get('ok', 0)} loaded, {last.get('duplicate', 0)} duplicate, {failed} failed")
    except Exception:  # noqa: BLE001 - a damaged index is "never run", not a broken page
        logger.warning("could not read the marketing autorun record", exc_info=True)
    out["autorun"] = a
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
        "marketing": {"installed": False, "runs": None, "last_run_at": None, "rows_loaded": None,
                     "rows_invalid": None, "failed": None, "partial": None, "fact_rows": None},
        "rollup": {"installed": False, "runs": None, "failed": None, "last_ok_at": None, "last_status": None,
                   "last_rows": None, "last_error": None, "channel_days": None, "newest_day": None},
        "autorun": {"ran": False, "at": None, "files": None, "ok": None, "failed": None, "duplicate": None, "rows_loaded": None,
                    "rollup": None, "exit_code": None, "problem": None, "result": "never run"},
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


def _iso_from_offset(s: str | None) -> str | None:
    """'2026-09-20T10:31:28+07:00' -> UTC iso."""
    if not s:
        return None
    try:
        return _iso(datetime.fromisoformat(s))
    except ValueError:
        return None
