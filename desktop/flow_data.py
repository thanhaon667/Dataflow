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

CODE LAYOUT (clean-code pass 3: this module was split, behaviour unchanged). This file keeps the store (FlowStore), the
HEALTH table and the definition-file mtime; everything else lives in siblings, lowest layer first, and is re-exported below so
every importer of desktop.flow_data keeps working:
  flow_util       logger, time / age helpers, redaction, log tailing
  flow_metrics    database counts, log / JSON readings, webhook probe, Task Scheduler scan, trace
  flow_triggers   is each trigger armed, trigger and edge state text
  flow_health     healthy / stale / error classification and one health function per node
  flow_mapcheck   the map sync check (unmapped scripts, stale nodes, inconsistent edges)
"""
from __future__ import annotations

import ast as ast
import csv as csv
import hashlib
import io as io
import json
import logging as logging
import os as os
import re as re
import subprocess as subprocess
import threading
import time
import urllib.request  # noqa: F401
from datetime import date as date, datetime, timezone as timezone
from pathlib import Path
from typing import Callable
from sqlalchemy import text as text
from desktop import winutil as winutil
from desktop import flow_definition
from desktop.flow_definition import EDGES, IGNORE as IGNORE, LANES, LAYOUT, NODES, PROBES, SCAN as SCAN, SCHEDULE_MARKERS as SCHEDULE_MARKERS, TRACE as TRACE
from erp.db import read_connect as read_connect, read_engine as engine  # noqa: F401
# Names that moved into the sibling modules; re-exported here (`x as x` = explicit re-export) so every importer of this module keeps working.
from desktop.flow_util import (
    logger as logger, _iso as _iso, _local_iso as _local_iso, _parse as _parse, _age_hours as _age_hours,
    _ago as _ago, _latest as _latest, _REDACT as _REDACT, _redact as _redact, _read_tail as _read_tail,
)
from desktop.flow_metrics import (
    _OPENER as _OPENER, _q as _q, _i as _i, _db_metrics as _db_metrics, _ASYNCIO_NOISE as _ASYNCIO_NOISE,
    _LOG_LINE as _LOG_LINE, _file_metrics as _file_metrics, _probe as _probe, _env_flags as _env_flags,
    scan_scheduler as scan_scheduler, _resolve_path as _resolve_path, _empty_metrics as _empty_metrics,
    _build_trace as _build_trace, _iso_from_offset as _iso_from_offset,
)
from desktop.flow_triggers import (
    KIND_PRIORITY as KIND_PRIORITY, _Ctx as _Ctx, _armed_one as _armed_one, _armed as _armed,
    _trigger_state_text as _trigger_state_text, _edge_state as _edge_state, _resolve_triggers as _resolve_triggers,
)
from desktop.flow_health import (
    STALE_FACTOR as STALE_FACTOR, _h as _h, _classify as _classify, _script_h as _script_h, _fmt_hours as _fmt_hours,
    _db_guard as _db_guard, _hl_lead_form as _hl_lead_form, _hl_webhook as _hl_webhook,
    _hl_ticket_entry as _hl_ticket_entry, _hl_marketing_ingest as _hl_marketing_ingest,
    _hl_marketing_rollup as _hl_marketing_rollup, _hl_marketing_autorun as _hl_marketing_autorun, _hl_leads_logic as _hl_leads_logic, _hl_postgres as _hl_postgres,
    _hl_views as _hl_views, _hl_deepseek as _hl_deepseek, _hl_clickup_push as _hl_clickup_push,
    _hl_clickup as _hl_clickup, _hl_clickup_pull as _hl_clickup_pull, _hl_sync_employees as _hl_sync_employees,
    _hl_daily_check as _hl_daily_check, _hl_daily_digest as _hl_daily_digest, _hl_weekly_report as _hl_weekly_report,
    _hl_emailer as _hl_emailer, _hl_inbox as _hl_inbox, _hl_digest_files as _hl_digest_files,
    _hl_live_report as _hl_live_report, _hl_script_center as _hl_script_center, _hl_powerbi as _hl_powerbi,
)
from desktop.flow_mapcheck import (
    MAP_SCAN_LIMIT as MAP_SCAN_LIMIT, FIX_HINT as FIX_HINT, NODE_TRIGGER_KINDS as NODE_TRIGGER_KINDS,
    EDGE_TRIGGER_KINDS as EDGE_TRIGGER_KINDS, ARMED_FLAGS as ARMED_FLAGS, COUNTER_KEYS as COUNTER_KEYS,
    _file_mtime as _file_mtime, _key as _key, _clean_rel as _clean_rel, declared_files as declared_files,
    scan_scripts as scan_scripts, _first_doc_line as _first_doc_line, map_integrity as map_integrity,
    check_map as check_map, map_check_unavailable as map_check_unavailable,
)

SCHEDULER_TTL = 60          # seconds between `schtasks` scans (a scan takes ~5 s)
MAP_CHECK_TTL = 30          # seconds between project scans for the map sync check (the Refresh button forces one)

HEALTH: dict[str, Callable] = {
    "lead_form": _db_guard(_hl_lead_form), "webhook": _hl_webhook, "ticket_entry": _db_guard(_hl_ticket_entry),
    "leads_logic": _db_guard(_hl_leads_logic), "postgres": _hl_postgres, "views": _db_guard(_hl_views),
    "deepseek": _db_guard(_hl_deepseek), "clickup_push": _db_guard(_hl_clickup_push), "clickup": _db_guard(_hl_clickup),
    "clickup_pull": _db_guard(_hl_clickup_pull), "sync_employees": _db_guard(_hl_sync_employees),
    "daily_check": _hl_daily_check, "daily_digest": _hl_daily_digest, "weekly_report": _hl_weekly_report,
    "emailer": _hl_emailer, "inbox": _hl_inbox, "digest_files": _hl_digest_files, "live_report": _hl_live_report,
    "script_center": _hl_script_center,
    "powerbi": _db_guard(_hl_powerbi),
    "marketing_ingest": _db_guard(_hl_marketing_ingest),
    "marketing_rollup": _db_guard(_hl_marketing_rollup),
    "marketing_autorun": _hl_marketing_autorun,
}

_DEF_FILE = getattr(flow_definition, "__file__", None)
_DEF_MTIME_AT_START = _file_mtime(_DEF_FILE)   # flow_definition.py is read once, when this process starts


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
            "marketing": {"total": v["marketing"]["rows_loaded"], "last_at": v["marketing"]["last_run_at"], "n24h": None},
            "rollup": {"total": v["rollup"]["last_rows"], "last_at": v["rollup"]["last_ok_at"], "n24h": None},
            "autorun": {"total": v["autorun"]["rows_loaded"], "last_at": v["autorun"]["at"], "n24h": None},
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
                                        "digest", "dlog", "center", "env", "probe", "db")},
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
