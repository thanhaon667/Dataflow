"""
Live data feed for the Reporting page of ERP Desk.

`ReportStore` keeps one JSON-serialisable snapshot of the whole report in
memory. A background thread re-queries PostgreSQL every few seconds (and the
shell's Refresh button forces an immediate re-query); the snapshot only gets a
new `version` when the data actually changed, so the page can poll cheaply
("changed since v?") and animate only real changes.

This module owns the report's queries and its headline sentence (`load_all`,
`build_insight`). They used to live in erp/html_report.py, which generated a
standalone erp_report.html; that surface was dropped on 2026-09-23 when the
project cut back to ERP Desk as its only user interface, so the code moved here
unchanged - ERP Desk is the only thing that ever called it. On top of `load_all`
sits one per-lead "which pipeline stages has it reached" query, so the funnel can
be cross-filtered by rep / source / time on the client.

Never raises into callers: on a DB error the last good snapshot is kept and the
error text is exposed as `error` so the UI can show "showing data from HH:MM".
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import text

from erp.db import read_connect, read_engine as engine

logger = logging.getLogger("erp_desk.report")

LEAD_COLS = [
    "lead_id", "full_name", "company", "source", "created_at", "assigned_sales_rep",
    "scale_estimate", "potential_score", "sync_status", "sla_due_at", "sla_breached",
]
TICKET_COLS = [
    "ticket_code", "department_name", "category", "priority", "status", "channel",
    "customer_name", "company", "assignee_name", "created_at", "sla_due_at", "sla_breached",
]
TIMELINE_COLS = ["task_type", "task_name", "employee", "start_time", "end_time", "sla_breached"]

# Which pipeline stages each lead has reached (the same stage definitions as the funnel in load_all below).
STAGE_SQL = """
    SELECT l.id AS lead_id,
      EXISTS (SELECT 1 FROM lead_assignments la WHERE la.lead_id = l.id AND la.is_current) AS st_assigned,
      EXISTS (SELECT 1 FROM lead_ai_analysis a WHERE a.lead_id = l.id) AS st_analyzed,
      EXISTS (SELECT 1 FROM lead_clickup_sync s WHERE s.lead_id = l.id
              AND s.sync_status IN ('synced', 'mocked')) AS st_task,
      EXISTS (SELECT 1 FROM lead_updates u WHERE u.lead_id = l.id) AS st_update
    FROM leads l
"""


# ---------------------------------------------------------------------------
# Data loading (moved here verbatim from erp/html_report.py, 2026-09-23)
# ---------------------------------------------------------------------------
def load_df(query: str) -> pd.DataFrame:
    with read_connect(engine) as conn:
        return pd.read_sql(text(query), conn)


def load_all():
    """Leads / tickets / timeline for the Reporting page. (A fourth query - a funnel aggregate - used to run here too;
    ERP Desk never read its return slot, so it was dropped on 2026-09-24: one fewer read-only round trip per refresh,
    same payload. The funnel numbers the page actually shows are computed from `leads` itself, client-side.)"""
    leads = load_df("SELECT * FROM v_leads_summary ORDER BY created_at")
    tickets = load_df("SELECT * FROM v_tickets_summary ORDER BY created_at")

    timeline = load_df("""
        SELECT
            'Lead' AS task_type,
            l.full_name AS task_name,
            rep.full_name AS employee,
            la.assigned_at AS start_time,
            COALESCE(l.sla_due_at, la.assigned_at + INTERVAL '1 day') AS end_time,
            COALESCE(l.sla_due_at IS NOT NULL AND now() > l.sla_due_at, FALSE) AS sla_breached
        FROM leads l
        JOIN lead_assignments la ON la.lead_id = l.id AND la.is_current
        JOIN users rep ON rep.id = la.sales_rep_id

        UNION ALL

        SELECT
            'Ticket' AS task_type,
            t.ticket_code AS task_name,
            ag.full_name AS employee,
            t.created_at AS start_time,
            COALESCE(t.sla_due_at, t.created_at + INTERVAL '1 day') AS end_time,
            COALESCE(t.sla_due_at IS NOT NULL AND now() > t.sla_due_at, FALSE) AS sla_breached
        FROM tickets t
        JOIN users ag ON ag.id = t.assignee_id
        WHERE t.status NOT IN ('resolved', 'closed')

        ORDER BY employee, start_time
    """)

    return leads, tickets, timeline


# ---------------------------------------------------------------------------
# Narrative insight (computed from the data, not fabricated)
# ---------------------------------------------------------------------------
def build_insight(leads: pd.DataFrame) -> tuple[str, str]:
    if leads.empty:
        return "The pipeline is empty.", "No lead has come in yet - once one does, this page will track it end to end."

    breach_pct = leads["sla_breached"].mean() * 100
    breached_count = int(leads["sla_breached"].sum())
    top_rep = leads["assigned_sales_rep"].value_counts().idxmax()
    top_count = int(leads["assigned_sales_rep"].value_counts().max())

    if breach_pct >= 99.5:
        headline = "Every active lead has breached its SLA."
    elif breach_pct == 0:
        headline = "Zero SLA breaches. The pipeline is healthy."
    else:
        headline = f"{breach_pct:.0f}% of active leads have breached SLA."

    sub = f"{breached_count} of {len(leads)} lead(s) are overdue right now. {top_rep} carries the heaviest load with {top_count} active lead(s)."
    return headline, sub


def _jsonable(value):
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if value is pd.NaT:
        return None
    if isinstance(value, pd.Timestamp):
        ts = value.tz_localize("UTC") if value.tzinfo is None else value.tz_convert("UTC")
        return ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, datetime):
        ts = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if hasattr(value, "item"):  # numpy scalar
        try:
            return _jsonable(value.item())
        except Exception:
            return str(value)
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _records(df: pd.DataFrame, cols: list[str]) -> list[dict]:
    if df is None or df.empty:
        return []
    present = [c for c in cols if c in df.columns]
    return [{c: _jsonable(v) for c, v in zip(present, row)} for row in df[present].itertuples(index=False, name=None)]


def build_snapshot_data() -> dict:
    """Query the DB and return the plain-data part of the snapshot."""
    leads, tickets, timeline = load_all()
    with read_connect(engine) as conn:
        stages = pd.read_sql(text(STAGE_SQL), conn)
    if not leads.empty:
        leads = leads.merge(stages, on="lead_id", how="left")
    stage_cols = ["st_assigned", "st_analyzed", "st_task", "st_update"]
    lead_rows = _records(leads, LEAD_COLS + stage_cols)
    for row in lead_rows:  # missing stage flags (lead absent from the stage query) count as "not reached"
        for c in stage_cols:
            row[c] = bool(row.get(c))
    headline, sub = build_insight(leads)
    return {
        "leads": lead_rows,
        "tickets": _records(tickets, TICKET_COLS),
        "timeline": _records(timeline, TIMELINE_COLS),
        "insight": {"headline": headline, "sub": sub},
    }


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class ReportStore:
    def __init__(self, interval_seconds: int = 10) -> None:
        self.interval = max(3, int(interval_seconds))
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._snapshot: dict | None = None
        self._error: str | None = None
        self._last_logged_error: str | None = None
        self._refreshed_at: str | None = None
        self._refresh_count = 0

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="report-refresher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.refresh()
            self._stop.wait(self.interval)

    # -- refresh -----------------------------------------------------------
    def refresh(self) -> bool:
        """Re-query the DB. Returns True when the data changed. Safe to call
        from any thread; concurrent calls are serialized."""
        with self._refresh_lock:
            try:
                data = build_snapshot_data()
            except Exception as exc:  # DB down, view missing, ...
                msg = f"{type(exc).__name__}: {str(exc).splitlines()[0][:200] if str(exc) else ''}"
                with self._lock:
                    self._error = msg
                if msg != self._last_logged_error:  # log a persistent failure once, not every 10 s
                    logger.warning("Report refresh failed: %s", msg)
                    self._last_logged_error = msg
                return False

            if self._last_logged_error:
                logger.info("Report refresh recovered")
                self._last_logged_error = None
            version = hashlib.sha1(json.dumps(data, sort_keys=True).encode("utf-8")).hexdigest()[:12]
            now = _now_iso()
            with self._lock:
                changed = self._snapshot is None or self._snapshot["version"] != version
                if changed:
                    self._snapshot = {"version": version, "generated_at": now, "data": data}
                self._error = None
                self._refreshed_at = now
                self._refresh_count += 1
            if changed:
                logger.info("Report data changed -> version %s (%d leads, %d tickets)",
                            version, len(data["leads"]), len(data["tickets"]))
            return changed

    # -- read side ---------------------------------------------------------
    def meta(self) -> dict:
        with self._lock:
            snap = self._snapshot
            return {
                "ok": snap is not None and self._error is None,
                "has_data": snap is not None,
                "version": snap["version"] if snap else None,
                "generated_at": snap["generated_at"] if snap else None,
                "refreshed_at": self._refreshed_at,
                "error": self._error,
                "interval_seconds": self.interval,
                "refresh_count": self._refresh_count,
            }

    def get(self, since: str | None = None) -> dict:
        """Full snapshot, or just the meta when `since` is already current."""
        meta = self.meta()
        with self._lock:
            snap = self._snapshot
        if snap is None:
            return {**meta, "changed": False, "data": None}
        if since and since == snap["version"]:
            return {**meta, "changed": False}
        return {**meta, "changed": True, "data": snap["data"]}
