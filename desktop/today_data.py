"""
Live data feed for the Today page of ERP Desk (GET /api/today): "how is the business doing right now",
in plain English, for people who only want the answer.

Everything on the page comes from real sources, never from constants:
  * numbers, sparklines, attention list  - read-only SELECTs against PostgreSQL (leads, lead_updates,
                                            tickets, lead_clickup_sync), one connection, each query on its own
  * the detail drawer                     - read-only SELECTs that add lead_ai_analysis (the AI summary), the newest
                                            lead_updates row (the last note pulled from ClickUp) and lead_clickup_sync
                                            (task id / state). It travels in the SAME /api/today payload, so the page
                                            still has exactly one poller; nothing is fetched when a drawer opens.
  * system health                         - the Data Flow feed (desktop/flow_data.FlowStore snapshot). The
                                            "is it really armed?" logic (webhook probe, Task Scheduler scan,
                                            ERP Desk's own loops) lives ONLY there; this module just translates
                                            its verdicts into six plain-English cells (lesson L-030). The Health
                                            page (Ctrl+6, desktop/health_data.py) is the detail view of those very
                                            cells - it reuses build_health() and blind_inputs() rather than
                                            probing anything again, so the strip and that page cannot disagree.
  * last job run                          - the digest file time (via the flow snapshot) and the newest
                                            ClickUp comment pull time (DB)

Rules, in one place (the page shows the same text under "How this is decided"):

  Awaiting first reply    a lead with no reply on record yet: no lead_updates row (a ClickUp comment pulled by
                          erp/clickup_pull.py) at or before the moment we look. That is the only evidence of a
                          reply the database has, so it under-counts only when the pull job has not run. The page's
                          own English for this and the four other SLA outcomes lives in desktop/sla_words.py, which
                          the Leads page imports too, so the two pages cannot word the same state differently.
  Past SLA now            a lead that is awaiting a first reply AND whose sla_due_at (LEAD_SLA_HOURS business
                          hours after it came in, stored by erp/leads.py) is in the past. A lead the rep already
                          answered - even late - is no longer actionable and is not listed.
  Ticket past SLA         open ticket (open / in_progress / pending) whose sla_due_at is in the past.
  Attention list          every lead past SLA, every ticket past SLA, and open tickets not past SLA that have been
                          open for more than TICKET_STALE_HOURS. Worst first, at most ATTENTION_MAX shown
                          (tickets always get at least 2 of the rows when there are that many, so a pile of late leads
                          cannot hide them).
  Day                     the database's calendar day (session time zone, named on the page), midnight to now.
  Since yesterday         one plain line under the headline about what changed during THAT day: leads that came in,
                          replies that came in, and whether the "past SLA" count grew compared with the end of
                          yesterday (the second-to-last point of the same 7-day series the sparklines use, so it cannot
                          disagree with them). A clause is dropped rather than guessed when its source is blind (the
                          ClickUp pull is not running, so replies cannot be counted), unreadable, or when the database
                          holds nothing from before today (no comparable history). With no clause left, no line is shown.

  Detail drawer           clicking a tile or a row in "Needs a human" opens what a non-technical person needs about one
                          lead or ticket: who it is (name and company only - e-mail, phone and the raw payload are never
                          put in the payload), the rep who owns it, when it arrived, the deadline and how late, the AI
                          summary from lead_ai_analysis if one exists, the ClickUp task id / state / link if it is
                          synced, and the newest note pulled from ClickUp (lead_updates). A tile opens the list of what
                          it counts, worst-first, at most DETAIL_ROWS rows; every row opens the same panel.

  Status sentence  (first rule that matches wins)
    1. database unreadable                     -> level "unknown"    "Can’t read today’s numbers - ..."
    2. any lead / ticket past SLA              -> level "attention"  "Needs attention - 2 leads past their 5-business-hour SLA"
    3. lead or ticket numbers unreadable       -> level "watch"      "Partly unavailable - ..."
    4. a system check is red (failed syncs...) -> level "watch"      "Mostly fine - nothing is overdue, but ..."
    5. inputs are blind (see below)            -> level "watch"      "No problems found, but the numbers may be incomplete (3 new leads today, none overdue)"
    6. otherwise                               -> level "ok"         "All good - 3 new leads today, none overdue"

  Blind inputs (lesson L-084): the numbers can only be complete when the doors they arrive through are open. Read from the
  SAME health cells the strip shows (never re-probed here): the lead webhook is not answering (web-form leads cannot arrive),
  the ClickUp comment pull is not scheduled / could not be checked / failed its last run (replies never reach the database),
  ClickUp is not connected at all, or the health feed has not finished its first pass. Any of them adds a plain-English
  `status.caveat` sentence under the headline ("Numbers may be incomplete: ...") to EVERY level except "unknown" (which
  already says the database is unreadable), and turns rule 6 into rule 5 so the page never says a bare "All good" about
  data that cannot arrive. Rules 1-4 keep their text and their order.

Contract, same as report_data / flow_data: never raises into the caller. Any single source that fails degrades
only its own tile / list / cell to "unavailable" (state "unknown"); the rest of the page keeps working. Nothing
here takes input from the browser (the only query parameter, `fresh`, just skips a 5 s cache), nothing writes
anywhere, and no secret value is ever put in the payload.

CODE LAYOUT (clean-code pass 3: this module was split, behaviour unchanged). This file keeps TodayStore, collect() and the
cache / build-slot constants: they read `engine` and BUILD_WAIT_SECONDS, which the tests patch on this module. Everything else lives
in siblings, lowest layer first, and is re-exported below so every importer of desktop.today_data keeps working:
  today_util      logger, timeouts, number / span formatting, log-once guard
  today_sql       tuning constants and every query text (lead_awaiting_sql / lead_past_sla_sql)
  today_build     KPI tiles, last job, attention list, since-yesterday line, detail drawer
  today_health    the six health cells and blind_inputs()
  today_assemble  status sentence, rules text and payload assembly
"""
from __future__ import annotations

import logging as logging
import threading
import time
from datetime import date as date, datetime, timezone
from sqlalchemy import text as text
from desktop import sla_words as W  # noqa: F401
from desktop.flow_data import _ago as _ago, _iso, _redact
from erp import business_hours as business_hours
from erp.db import read_connect, read_engine as engine
# Names that moved into the sibling modules; re-exported here (`x as x` = explicit re-export) so every importer of this module keeps working.
from desktop.today_util import (
    logger as logger, STATEMENT_TIMEOUT_MS as STATEMENT_TIMEOUT_MS, LOCK_TIMEOUT_MS as LOCK_TIMEOUT_MS,
    QUERY_BUDGET_SECONDS as QUERY_BUDGET_SECONDS, _n as _n, _span as _span, _pretty_day as _pretty_day, _i as _i,
    _delta as _delta, _rows as _rows, arm_timeouts as arm_timeouts, _logged as _logged, _log_once as _log_once,
    _failed as _failed,
)
from desktop.today_sql import (
    DAYS as DAYS, TICKET_STALE_HOURS as TICKET_STALE_HOURS, ATTENTION_MAX as ATTENTION_MAX,
    OPEN_STATUS_LIST as OPEN_STATUS_LIST, OPEN_STATUSES as OPEN_STATUSES, DETAIL_ROWS as DETAIL_ROWS,
    _DAYS_CTE as _DAYS_CTE, REPLY_AT_SQL as REPLY_AT_SQL, lead_awaiting_sql as lead_awaiting_sql,
    lead_past_sla_sql as lead_past_sla_sql, LEADS_SERIES_SQL as LEADS_SERIES_SQL, LEADS_NOW_SQL as LEADS_NOW_SQL,
    TICKETS_SERIES_SQL as TICKETS_SERIES_SQL, TICKETS_NOW_SQL as TICKETS_NOW_SQL, ATTN_LEADS_SQL as ATTN_LEADS_SQL,
    ATTN_TICKETS_SQL as ATTN_TICKETS_SQL, LAST_PULL_SQL as LAST_PULL_SQL, CHANGES_SQL as CHANGES_SQL,
    DETAIL_LEADS_SQL as DETAIL_LEADS_SQL, DETAIL_TICKETS_SQL as DETAIL_TICKETS_SQL,
)
from desktop.today_build import (
    JOB_FRESH_HOURS as JOB_FRESH_HOURS, NOTE_MAX_CHARS as NOTE_MAX_CHARS, AI_NOTES_MAX_CHARS as AI_NOTES_MAX_CHARS,
    CLICKUP_TASK_URL as CLICKUP_TASK_URL, _WEEKDAYS as _WEEKDAYS, _sla_hours as _sla_hours,
    _sla_window as _sla_window, _tile as _tile, _unavailable as _unavailable, build_tiles as build_tiles,
    last_job as last_job, build_attention as build_attention, _n_or_no as _n_or_no, build_since as build_since,
    DETAIL_LISTS as DETAIL_LISTS, PRIVACY_NOTE as PRIVACY_NOTE, _clip as _clip, _fact as _fact, _when as _when,
    _lead_outcome as _lead_outcome, _lead_item as _lead_item, _ticket_item as _ticket_item, _row_of as _row_of,
    build_details as build_details,
)
from desktop.today_health import (
    _TASK_NOT_RUN as _TASK_NOT_RUN, _cell as _cell, _pull_state as _pull_state, build_health as build_health,
    blind_inputs as blind_inputs,
)
from desktop.today_assemble import (
    _caveat as _caveat, _status as _status, build_status as build_status, assemble as assemble,
    build_rules as build_rules,
)

CACHE_SECONDS = 5.0            # /api/today is cached this long (the page polls every ~15 s)
MIN_FRESH_SECONDS = 1.0        # ?fresh=1 cannot hammer the database faster than this

BUILD_WAIT_SECONDS = 6.0       # a request never waits longer than this for another request's build (see TodayStore.get)


def collect(conn, budget_seconds: float = QUERY_BUDGET_SECONDS, statement_ms: int = STATEMENT_TIMEOUT_MS) -> dict:
    """Run every query on `conn`. Each block is independent: a failure gives {"error": ...} for that block only.

    Hang safety (lesson L-083): every statement first re-arms the statement/lock timeouts (a rollback after a failed block
    ends the transaction and with it the previous setting), the statement timeout never exceeds what is left of the total
    budget, and once the budget is used up the remaining statements are refused instead of queueing behind a slow database.
    So a stalled database costs about `budget_seconds` here, however many queries there are."""
    raw: dict = {}
    deadline = time.monotonic() + budget_seconds

    def rows(sql: str) -> list[dict]:
        left_ms = int((deadline - time.monotonic()) * 1000)
        if left_ms <= 50:
            raise TimeoutError("skipped: the database was too slow to answer in time")
        arm_timeouts(conn, min(statement_ms, left_ms))
        return _rows(conn, sql)

    def block(name: str, fn) -> None:
        try:
            raw[name] = fn()
        except Exception as exc:  # noqa: BLE001 - a missing table / view must only take its own tile down
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            raw[name] = {"error": f"{type(exc).__name__}: {_redact(str(exc))}"}
            _log_once(name, raw[name]["error"])

    def leads():
        series = rows(LEADS_SERIES_SQL)
        now = rows(LEADS_NOW_SQL)[0]
        return {"days": [r["day"] for r in series], "new": [int(r["new_leads"]) for r in series],
                "overdue": [int(r["overdue"]) for r in series], "awaiting": [int(r["awaiting"]) for r in series],
                "worst_late_s": _i(now["worst_late_s"]), "oldest_wait_s": _i(now["oldest_wait_s"]),
                "total": int(now["total"]), "today": now["today"]}

    def tickets():
        series = rows(TICKETS_SERIES_SQL)
        now = rows(TICKETS_NOW_SQL)[0]
        return {"days": [r["day"] for r in series], "opened": [int(r["opened"]) for r in series],
                "resolved": [int(r["resolved"]) for r in series], "open": [int(r["open_end"]) for r in series],
                "total": int(now["total"]), "overdue": int(now["overdue"]), "stale": int(now["stale"]),
                "worst_late_s": _i(now["worst_late_s"])}

    block("leads", leads)
    block("tickets", tickets)
    block("attn_leads", lambda: rows(ATTN_LEADS_SQL))
    block("attn_tickets", lambda: rows(ATTN_TICKETS_SQL))
    block("pull", lambda: {"at": _iso(rows(LAST_PULL_SQL)[0]["at"])})
    block("changes", lambda: rows(CHANGES_SQL)[0])
    # The drawer's rows come last on purpose: if the budget is nearly gone they are the part the page can live without
    # (the tiles then say so instead of losing their numbers).
    block("detail_leads", lambda: rows(DETAIL_LEADS_SQL))
    block("detail_tickets", lambda: rows(DETAIL_TICKETS_SQL))
    return raw


# ============================================================================ store
class TodayStore:
    """Builds the /api/today payload on demand (cached for a few seconds); no thread, nothing to stop."""

    def __init__(self, flow=None, ttl: float = CACHE_SECONDS) -> None:
        self.flow = flow                  # desktop.flow_data.FlowStore | None
        self.ttl = ttl
        self._lock = threading.Lock()
        self._cache: dict | None = None
        self._at = 0.0

    def _flow_data(self) -> dict | None:
        if self.flow is None:
            return None
        try:
            snap = self.flow.get()
            data = snap.get("data") if isinstance(snap, dict) else None
            return data if isinstance(data, dict) else None
        except Exception:  # noqa: BLE001 - the health strip degrades, the rest of the page does not care
            logger.exception("Today feed: could not read the Data Flow snapshot")
            return None

    def _build(self) -> dict:
        db_error: str | None = None
        raw: dict = {}
        try:
            with read_connect(engine).execution_options(postgresql_readonly=True) as conn:
                raw = collect(conn)
                conn.rollback()
        except Exception as exc:  # noqa: BLE001 - DB down: every DB-backed part says "unavailable"
            db_error = f"{type(exc).__name__}: {_redact(str(exc))}"
            _log_once("database", db_error)
        else:
            _logged.pop("database", None)
        return assemble(raw, self._flow_data(), db_error)

    @staticmethod
    def _fallback(rest: str, sub: str, why: str) -> dict:
        """A complete, honest payload for when nothing could be built: the page renders it as 'Can't read today's numbers'."""
        return {"ok": False, "generated_at": _iso(datetime.now(timezone.utc)), "day": None, "day_label": "Today", "tz": None,
                "status": _status("unknown", "Can’t read today’s numbers", rest, sub, [why], items=[{"id": "feed", "clause": rest}]),
                "since": build_since({}, ["feed"]),
                "kpis": [], "attention": {"available": False, "total": None, "items": [], "shown": 0, "note": None,
                                          "all_same_what": None},
                "details": build_details({}),
                "health": [], "job": None, "sources": {}, "rules": build_rules()}

    def get(self, fresh: bool = False) -> dict:
        # A build normally takes well under a second and is bounded (connect_timeout + QUERY_BUDGET_SECONDS). If one is still
        # running after BUILD_WAIT_SECONDS (a frozen socket the server-side timeouts cannot reach) this request does not park
        # behind it: it gets the last payload (its old generated_at makes the page say STALE) or an honest "busy" one.
        if not self._lock.acquire(timeout=BUILD_WAIT_SECONDS):
            logger.warning("Today feed: a build has been running for more than %.0f s; serving the last payload", BUILD_WAIT_SECONDS)
            return self._cache or self._fallback("the data service is busy", "Press Refresh in a moment.", "A previous read is still running.")
        try:
            age = time.monotonic() - self._at
            if self._cache is not None and age < (MIN_FRESH_SECONDS if fresh else self.ttl):
                return self._cache
            try:
                self._cache = self._build()
            except Exception as exc:  # noqa: BLE001 - last line of defence: never a 500 for the page
                logger.exception("Today feed failed")
                self._cache = self._fallback("an unexpected error occurred", "Press Refresh; if it persists see erp_desktop.log.", type(exc).__name__)
            self._at = time.monotonic()
            return self._cache
        finally:
            self._lock.release()
