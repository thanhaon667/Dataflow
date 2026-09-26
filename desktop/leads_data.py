"""
Live data feed for the Leads page of ERP Desk: a lead funnel + SLA explorer for data analysts.
  GET /api/leads/analysis    every panel of the page for one set of filters (JSON)
  GET /api/leads/export.csv  the leads behind those same filters, as a CSV file for Excel

Everything comes from read-only SQL on `v_leads_summary` plus the base tables `lead_updates`, `lead_ai_analysis`
(reply and analysis evidence). Nothing is typed in, nothing is written, no secret is served.

What is different from the Reporting page: Reporting ships a whole snapshot to the browser and filters it there by clicking.
This feed filters on the SERVER (bound SQL parameters, whitelisted values), so the same filters drive every panel, the paged
table and the CSV, and it adds what a snapshot cannot: stage-to-stage conversion, SLA outcomes split by whether a reply exists,
a time-to-first-reply histogram, a detail table you can sort and page, and an export.

The one shared definition (never re-implemented here): "awaiting first reply" and "past SLA" come from
desktop/today_data.lead_awaiting_sql / lead_past_sla_sql, the very SQL the Today page uses. With no filter set, "Past SLA now"
on this page is therefore the number on the Today tile (the smoke gate asserts it).

  Reply evidence     a lead_updates row (a ClickUp comment pulled by erp/clickup_pull.py); its time is occurred_at, else synced_at.
                     First reply = the earliest such time for the lead.
  SLA outcome        (exactly one per lead)
                       no_deadline  the lead has no sla_due_at
                       on_time      first reply at or before sla_due_at
                       late         first reply after sla_due_at (answered, but too late)
                       past_sla     no reply and sla_due_at is in the past  (= Today's "Leads past SLA now")
                       pending      no reply yet, still inside the deadline
  Funnel             stages the pipeline really has, each proven by a row in a real table: received (leads), assigned to a rep
                     (a current lead_assignments row), analysed by AI (lead_ai_analysis), ClickUp task on record
                     (lead_clickup_sync synced/mocked), reply on record (lead_updates). STRICT: a lead counts at a stage only if it
                     also has every earlier stage, so a conversion can never exceed 100%; leads with later-stage evidence but a
                     missing earlier stage are counted separately and shown as a data warning, never hidden.
  Low numbers        a percentage is shown only when its denominator is at least LOW_N; otherwise the page shows "3 of 4".
                     Nothing divides by zero. Medians show their n; a 90th percentile needs P90_MIN_N replies.
  Time to first reply  wall-clock time from created_at to the first reply. A reply stamped before the lead arrived is a data
                     anomaly: counted, listed, never clamped into the statistics.

Contract, same as report_data / today_data: never raises into the caller. A query that fails degrades only its own panel to
"unavailable"; every filter value is validated against a whitelist (the actual reps and sources in the database, fixed lists for
the rest) and bound as a parameter, the sort column comes from a fixed dict, so no request text ever reaches the SQL text.
"""
from __future__ import annotations

import csv
import io
import logging
import math
import re
import statistics
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Collection, Mapping

from sqlalchemy import text

from desktop import sla_words as W
from desktop import today_data as td
from desktop.flow_data import _iso, _redact
from erp.db import read_connect, read_engine as engine

logger = logging.getLogger("erp_desk.leads")

LOW_N = 5                     # a percentage needs a denominator of at least this many leads; below it the page shows "3 of 4"
P90_MIN_N = 10                # a 90th percentile needs at least this many replies
PAGE_SIZES = (10, 25, 50, 100)
DEFAULT_PAGE_SIZE = 25
EXPORT_MAX_ROWS = 50_000      # the CSV holds at most this many rows (the response header says when it was cut)
REPLY_ROWS_MAX = 20_000       # replies read for the histogram / medians
TREND_MAX_DAYS = 120          # the per-day chart shows at most this many days (the newest)
BREAKDOWN_MAX_ROWS = 12       # rows in the per-rep / per-source tables
CACHE_SECONDS = 3.0
CACHE_KEYS_MAX = 24
QUERY_BUDGET_SECONDS = 6.0    # all queries of one analysis together (see td.collect: L-083, L-090)
EXPORT_BUDGET_SECONDS = 20.0  # the export reads more rows, so it gets more time
STATEMENT_TIMEOUT_MS = td.STATEMENT_TIMEOUT_MS
BUILD_WAIT_SECONDS = 6.0      # a request never parks longer than this waiting for a free build slot (L-091)
MAX_CONCURRENT_BUILDS = 3
MAX_FILTER_VALUES = 50
MAX_VALUE_LEN = 150
UNASSIGNED = "(unassigned)"   # what a lead without a current rep is called in the filters and the tables
NO_SOURCE = "(none)"          # ... and a lead without a source
DATE_MIN, DATE_MAX = date(2000, 1, 1), date(2100, 12, 31)
CSV_FORMULA_LEADERS = ("=", "+", "-", "@", ";", "\t", "\r")    # a text cell starting with one of these is prefixed with a quote

# ---- fixed vocabularies (whitelists) --------------------------------------------------------------------------------
STATES = W.OUTCOMES          # SLA outcomes (key, short label, plain-words meaning) - ONE vocabulary, shared with the Today
STATE_KEYS = W.KEYS          # page: desktop/sla_words.py. Neither page invents its own name for a state any more.
STATE_LABEL = W.LABEL
STATE_RANK_SQL = "CASE b.state WHEN 'past_sla' THEN 0 WHEN 'late' THEN 1 WHEN 'pending' THEN 2 WHEN 'no_deadline' THEN 3 ELSE 4 END"
SYNC_VALUES = ("pending", "synced", "failed", "mocked", "none")     # lead_clickup_sync.sync_status (03_leads_schema.sql), 'none' = no sync row
STAGES = [   # key, label, evidence, flag column of the base query (None = every lead)
    ("received", "Lead received", "a row in leads", None),
    ("assigned", "Assigned to a rep", "a current row in lead_assignments", "st_assigned"),
    ("analyzed", "Analysed by AI", "a row in lead_ai_analysis", "st_analyzed"),
    ("task", "ClickUp task on record", "lead_clickup_sync says synced or mocked", "st_task"),
    ("update", "Reply on record", "a row in lead_updates", "st_update"),
]
FLAGS = [s[3] for s in STAGES if s[3]]
SORTS = {   # sort key -> SQL expression on the base query `b`. The ONLY place a sort column comes from (never request text).
    "created_at": "b.created_at", "lead_id": "b.lead_id", "name": "lower(b.full_name)", "source": "b.src", "rep": "b.rep",
    "state": STATE_RANK_SQL, "first_reply": "b.first_reply", "sla_due_at": "b.sla_due_at", "score": "b.score",
    "hours": "b.hours_to_reply",
}
DEFAULT_SORT, DEFAULT_DIR = "created_at", "desc"
HIST_EDGES_H = (0.25, 0.5, 1, 2, 4, 8, 24, 72)     # histogram bin edges, in hours
KNOWN_PARAMS = ("from", "to", "rep", "source", "status", "sync", "sort", "dir", "page", "page_size")
_ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# ---- the base query: one row per lead, everything derived once ------------------------------------------------------
# `v_leads_summary` carries the lead, its current rep, sync status, latest AI score and deadline; the base tables add the
# reply evidence and the stage flags. `state` is the SLA outcome above; its "past SLA" branch is Today's own SQL fragment.
BASE_SQL = f"""
WITH first_reply AS (
  SELECT u.lead_id, min({td.REPLY_AT_SQL}) AS at FROM lead_updates u GROUP BY u.lead_id
), b AS (
  SELECT v.lead_id, v.full_name, v.company,
         COALESCE(v.source, '{NO_SOURCE}') AS src,
         COALESCE(v.assigned_sales_rep, '{UNASSIGNED}') AS rep,
         COALESCE(v.sync_status, 'none') AS sync,
         v.potential_score AS score, v.created_at, v.sla_due_at, fr.at AS first_reply,
         EXTRACT(EPOCH FROM (fr.at - v.created_at)) / 3600.0 AS hours_to_reply,
         CASE
           WHEN v.sla_due_at IS NULL THEN 'no_deadline'
           WHEN fr.at IS NOT NULL AND fr.at <= v.sla_due_at THEN 'on_time'
           WHEN fr.at IS NOT NULL THEN 'late'
           WHEN {td.lead_past_sla_sql("v", "v.lead_id")} THEN 'past_sla'
           ELSE 'pending'
         END AS state,
         (v.assignment_reason IS NOT NULL) AS st_assigned,
         EXISTS (SELECT 1 FROM lead_ai_analysis a WHERE a.lead_id = v.lead_id) AS st_analyzed,
         COALESCE(v.sync_status IN ('synced', 'mocked'), FALSE) AS st_task,
         (fr.at IS NOT NULL) AS st_update
  FROM v_leads_summary v
  LEFT JOIN first_reply fr ON fr.lead_id = v.lead_id
)
"""

OPTIONS_SQL = BASE_SQL + """
SELECT 'rep' AS kind, b.rep AS value, count(*) AS n FROM b GROUP BY b.rep
UNION ALL SELECT 'source', b.src, count(*) FROM b GROUP BY b.src
UNION ALL SELECT 'sync', b.sync, count(*) FROM b GROUP BY b.sync
"""
META_SQL = """
SELECT to_char(now(), 'YYYY-MM-DD') AS today,
       (SELECT count(*) FROM leads) AS lead_rows, (SELECT count(*) FROM v_leads_summary) AS view_rows,
       to_char(min(created_at), 'YYYY-MM-DD') AS first_day, to_char(max(created_at), 'YYYY-MM-DD') AS last_day
FROM leads
"""


# ============================================================================ filters (pure)
@dataclass(frozen=True)
class Filters:
    d_from: date | None = None
    d_to: date | None = None
    reps: tuple = ()
    sources: tuple = ()
    status: str | None = None
    sync: str | None = None

    def active(self) -> int:
        return sum(1 for x in (self.d_from or self.d_to, self.reps, self.sources, self.status, self.sync) if x)


@dataclass(frozen=True)
class PageSpec:
    sort: str = DEFAULT_SORT
    dir: str = DEFAULT_DIR
    page: int = 1
    size: int = DEFAULT_PAGE_SIZE


def _problem(param: str, value, why: str) -> dict:
    v = str(value)
    return {"param": param, "value": v if len(v) <= 60 else v[:57] + "...", "why": why}


def _values(params: Mapping[str, list[str]], name: str) -> list[str]:
    return [v for v in (params.get(name) or []) if v is not None]


def parse_filters(params: Mapping[str, list[str]], reps_ok: Collection[str], sources_ok: Collection[str],
                  syncs_ok: Collection[str] = SYNC_VALUES) -> tuple[Filters, list[dict]]:
    """Request parameters -> (Filters, problems). Every value is checked against a whitelist: rep / source against what is in the
    database right now, status / sync against fixed lists, dates against ISO format and sane bounds. A value that fails is
    reported in `problems` (the API answers 400) and never reaches SQL; it is never silently dropped, because an export that
    quietly ignored a filter would hand the analyst the wrong rows."""
    problems: list[dict] = []
    out: dict = {}

    def one(name: str) -> str | None:
        vals = _values(params, name)
        if len(vals) > 1:
            problems.append(_problem(name, vals[0], "give this filter only once"))
        return vals[-1] if vals else None

    for name, key in (("from", "d_from"), ("to", "d_to")):
        raw = one(name)
        if raw in (None, ""):
            continue
        try:
            if not _ISO_DAY.match(raw):
                raise ValueError
            d = date.fromisoformat(raw)
            if not DATE_MIN <= d <= DATE_MAX:
                raise ValueError
            out[key] = d
        except ValueError:
            problems.append(_problem(name, raw, "not a date between 2000-01-01 and 2100-12-31 (use YYYY-MM-DD)"))
    if out.get("d_from") and out.get("d_to") and out["d_from"] > out["d_to"]:
        problems.append(_problem("from", out["d_from"].isoformat(), "the start date is after the end date"))

    for name, key, ok in (("rep", "reps", reps_ok), ("source", "sources", sources_ok)):
        vals = []
        for v in _values(params, name):
            if v == "" or v in vals:
                continue
            if len(v) > MAX_VALUE_LEN or v not in ok:
                problems.append(_problem(name, v, f"not a known {name}"))
            else:
                vals.append(v)
        if len(vals) > MAX_FILTER_VALUES:
            problems.append(_problem(name, f"{len(vals)} values", f"at most {MAX_FILTER_VALUES} values"))
            vals = vals[:MAX_FILTER_VALUES]
        out[key] = tuple(vals)

    for name, allowed in (("status", STATE_KEYS), ("sync", list(syncs_ok))):
        raw = one(name)
        if raw in (None, "", "all"):
            continue
        if raw not in allowed:
            problems.append(_problem(name, raw, "not one of: " + ", ".join(allowed)))
        else:
            out[name] = raw
    return Filters(**out), problems


def unknown_params(params: Mapping[str, list[str]], known: Collection[str] = KNOWN_PARAMS) -> list[dict]:
    """Parameter NAMES this endpoint does not understand. Lesson L-098: a typo such as ?statuss=past_sla must not come
    back as 200 with every lead in the file, so the name is whitelisted just like the value, and the answer is a 400
    that names the parameter without echoing what was in it. `fresh` is the one name every feed accepts."""
    return [_problem(name, "", "not a parameter of this view; known: " + ", ".join(known))
            for name in sorted(params) if name not in known and name != "fresh"]


def parse_page(params: Mapping[str, list[str]]) -> tuple[PageSpec, list[dict]]:
    """sort / dir / page / page_size, whitelisted the same way (sort key from SORTS, size from PAGE_SIZES)."""
    problems: list[dict] = []
    vals = {n: (_values(params, n) or [None])[-1] for n in ("sort", "dir", "page", "page_size")}
    sort = vals["sort"] or DEFAULT_SORT
    if sort not in SORTS:
        problems.append(_problem("sort", sort, "not one of: " + ", ".join(SORTS)))
        sort = DEFAULT_SORT
    direction = (vals["dir"] or DEFAULT_DIR).lower()
    if direction not in ("asc", "desc"):
        problems.append(_problem("dir", direction, "must be asc or desc"))
        direction = DEFAULT_DIR
    page, size = 1, DEFAULT_PAGE_SIZE
    if vals["page"] not in (None, ""):
        if re.fullmatch(r"\d{1,6}", vals["page"]) and int(vals["page"]) >= 1:
            page = int(vals["page"])
        else:
            problems.append(_problem("page", vals["page"], "must be a whole number from 1"))
    if vals["page_size"] not in (None, ""):
        if re.fullmatch(r"\d{1,4}", vals["page_size"]) and int(vals["page_size"]) in PAGE_SIZES:
            size = int(vals["page_size"])
        else:
            problems.append(_problem("page_size", vals["page_size"], "must be one of " + ", ".join(map(str, PAGE_SIZES))))
    return PageSpec(sort, direction, page, size), problems


def where_sql(f: Filters) -> tuple[str, dict]:
    """Filters -> (WHERE text, bound parameters). The text is assembled ONLY from the fixed fragments below; every value travels
    as a bound parameter (a date, a list, a whitelisted token). Nothing from the request is ever formatted into the SQL."""
    clauses: list[str] = []
    params: dict = {}
    if f.d_from:
        clauses.append("b.created_at >= CAST(:d_from AS date)")
        params["d_from"] = f.d_from
    if f.d_to:
        clauses.append("b.created_at < CAST(:d_to_excl AS date)")          # the end day is inclusive
        params["d_to_excl"] = f.d_to + timedelta(days=1)
    if f.reps:
        clauses.append("b.rep = ANY(CAST(:reps AS text[]))")
        params["reps"] = list(f.reps)
    if f.sources:
        clauses.append("b.src = ANY(CAST(:sources AS text[]))")
        params["sources"] = list(f.sources)
    if f.status:
        clauses.append("b.state = :status")
        params["status"] = f.status
    if f.sync:
        clauses.append("b.sync = :sync")
        params["sync"] = f.sync
    return (" AND ".join(clauses) if clauses else "TRUE"), params


def filters_to_query(f: Filters, page: PageSpec | None = None) -> str:
    """The query string that reproduces these filters (used for the export link, so the file always matches the screen)."""
    from urllib.parse import urlencode
    pairs: list[tuple[str, str]] = []
    if f.d_from:
        pairs.append(("from", f.d_from.isoformat()))
    if f.d_to:
        pairs.append(("to", f.d_to.isoformat()))
    pairs += [("rep", r) for r in f.reps] + [("source", s) for s in f.sources]
    if f.status:
        pairs.append(("status", f.status))
    if f.sync:
        pairs.append(("sync", f.sync))
    if page:
        pairs += [("sort", page.sort), ("dir", page.dir)]
    return urlencode(pairs)


# ============================================================================ math (pure)
def rate(part: int | None, whole: int | None) -> dict:
    """part of whole. `pct` is None (and low_n True) when the whole is smaller than LOW_N, so 2 of 3 never reads as "66.7%".
    A zero or missing whole gives pct None without dividing."""
    part, whole = int(part or 0), int(whole or 0)
    pct = None if whole < LOW_N else round(part / whole * 100, 1)
    return {"n": part, "of": whole, "pct": pct, "low_n": whole < LOW_N}


def _hours_text(hours: float | None) -> str:
    return "-" if hours is None else td._span(hours * 3600)


def percentile(values: list[float], p: float) -> float | None:
    """Nearest-rank percentile (p in 0..100). None for an empty list."""
    if not values:
        return None
    s = sorted(values)
    k = max(1, math.ceil(p / 100 * len(s)))
    return s[min(k, len(s)) - 1]


def build_sla(state_rows: list[dict]) -> dict:
    """[{state, n, waiting}] -> counts per outcome, totals, and the two rates. `waiting` = leads with no reply at all."""
    counts = {k: 0 for k in STATE_KEYS}
    waiting = 0
    for r in state_rows:
        if r["state"] in counts:
            counts[r["state"]] += int(r["n"])
            waiting += int(r["waiting"])
    total = sum(counts.values())
    decided = counts["on_time"] + counts["late"] + counts["past_sla"]      # outcome known: answered, or the deadline passed unanswered
    breached = counts["late"] + counts["past_sla"]
    return {
        "counts": counts, "total": total, "decided": decided, "breached": breached, "waiting": waiting,
        "inside_deadline": counts["pending"], "past_sla_now": counts["past_sla"],
        "on_time_rate": rate(counts["on_time"], decided), "breached_rate": rate(breached, decided),
        "shares": [{"state": k, "label": STATE_LABEL[k], "n": counts[k], "rate": rate(counts[k], total)} for k in STATE_KEYS],
    }


def build_funnel(pattern_rows: list[dict], sync_rows: list[dict] | None = None) -> dict:
    """[{st_assigned, st_analyzed, st_task, st_update, n}] (one row per flag combination) -> the strict funnel.
    reached[i] = leads that have stage i AND every earlier stage. Conversion is against the previous stage, overall against all
    leads received. `anomalies` = leads that have evidence of a later stage while an earlier one is missing."""
    total = sum(int(r["n"]) for r in pattern_rows)
    reached = [total]
    anomalies = 0
    for i, flag in enumerate(FLAGS):
        reached.append(sum(int(r["n"]) for r in pattern_rows if all(r[f] for f in FLAGS[: i + 1])))
    for r in pattern_rows:
        flags = [bool(r[f]) for f in FLAGS]
        first_missing = next((i for i, v in enumerate(flags) if not v), None)
        if first_missing is not None and any(flags[first_missing + 1:]):
            anomalies += int(r["n"])
    stages = []
    for i, (key, label, evidence, _flag) in enumerate(STAGES):
        prev = reached[i - 1] if i else None
        stages.append({"key": key, "label": label, "evidence": evidence, "count": reached[i],
                       "step": rate(reached[i], prev) if i else None, "overall": rate(reached[i], total),
                       "dropped": (prev - reached[i]) if i else 0})
    drops = [s for s in stages[1:] if s["dropped"] > 0]
    biggest = max(drops, key=lambda s: s["dropped"]) if drops else None
    return {"stages": stages, "total": total, "anomalies": anomalies,
            "biggest_drop": {"key": biggest["key"], "label": biggest["label"], "n": biggest["dropped"]} if biggest else None,
            "sync": [{"value": r["sync"], "n": int(r["n"])} for r in sorted(sync_rows, key=lambda r: -int(r["n"]))] if sync_rows is not None else None}


def _bin_label(lo: float | None, hi: float | None, short: bool = False) -> str:
    def t(h: float) -> str:
        if short:
            return f"{round(h * 60)}m" if h < 1 else (f"{h:g}h" if h < 24 else f"{h / 24:g}d")
        return f"{round(h * 60)} min" if h < 1 else (f"{h:g} h" if h < 24 else f"{h / 24:g} d")
    if lo is None:
        return f"<{t(hi)}" if short else f"under {t(hi)}"
    if hi is None:
        return f">{t(lo)}" if short else f"over {t(lo)}"
    return f"{t(lo)}-{t(hi)}" if short else f"{t(lo)} to {t(hi)}"


def build_ttfr(hours: list[float | None], replied_rows: int | None = None) -> dict:
    """Wall-clock hours from arrival to first reply -> histogram + summary. Negative values (a reply stamped BEFORE the lead
    arrived) are data anomalies: counted and kept out of every statistic."""
    good = [float(h) for h in hours if h is not None and h >= 0]
    anomalies = sum(1 for h in hours if h is not None and h < 0)
    edges = list(HIST_EDGES_H)
    bounds = [None] + edges + [None]
    bins = []
    for i in range(len(edges) + 1):
        lo, hi = bounds[i], bounds[i + 1]
        n = sum(1 for h in good if (lo is None or h >= lo) and (hi is None or h < hi))
        bins.append({"label": _bin_label(lo, hi), "short": _bin_label(lo, hi, True), "lo": lo, "hi": hi, "n": n})
    n = len(good)
    med = statistics.median(good) if good else None
    p90 = percentile(good, 90) if n >= P90_MIN_N else None
    return {"n": n, "anomalies": anomalies, "bins": bins,
            "median_h": med, "median_text": _hours_text(med) if med is not None else None,
            "p90_h": p90, "p90_text": _hours_text(p90) if p90 is not None else None,
            "fastest_text": _hours_text(min(good)) if good else None, "slowest_text": _hours_text(max(good)) if good else None,
            "truncated": bool(replied_rows is not None and replied_rows >= REPLY_ROWS_MAX)}


def build_breakdown(state_rows: list[dict], reply_rows: list[dict] | None, key: str) -> dict:
    """[{name, state, n}] per rep or per source + reply rows [{rep|src, hours}] -> one row per name: outcome counts, on-time rate
    (LOW_N rule), median time to first reply (with its n). Sorted by size; capped at BREAKDOWN_MAX_ROWS."""
    per: dict[str, dict] = {}
    for r in state_rows:
        row = per.setdefault(r["name"], {"name": r["name"], "n": 0, "counts": {k: 0 for k in STATE_KEYS}})
        if r["state"] in row["counts"]:
            row["counts"][r["state"]] += int(r["n"])
            row["n"] += int(r["n"])
    hours: dict[str, list[float]] = {}
    for r in reply_rows or []:
        h = r.get("hours")
        if h is not None and h >= 0:
            hours.setdefault(r[key], []).append(float(h))
    rows = []
    for name, row in per.items():
        c = row["counts"]
        decided = c["on_time"] + c["late"] + c["past_sla"]
        hs = hours.get(name, [])
        row.update({"decided": decided, "on_time_rate": rate(c["on_time"], decided), "past_sla": c["past_sla"],
                    "replied": len(hs), "median_text": _hours_text(statistics.median(hs)) if hs else None,
                    "median_known": reply_rows is not None})
        rows.append(row)
    rows.sort(key=lambda r: (-r["n"], r["name"].lower()))
    more = max(0, len(rows) - BREAKDOWN_MAX_ROWS)
    return {"rows": rows[:BREAKDOWN_MAX_ROWS], "more": more, "total_names": len(rows)}


def build_trend(rows: list[dict], f: Filters, today: str | None) -> dict:
    """[{day, n, breached}] -> zero-filled per-day series. The axis runs from the first day with a lead (or the filter's start)
    to today (or the filter's end): a run of zero days IS information here ("nothing came in since ...")."""
    by_day = {r["day"]: r for r in rows}
    if not by_day:
        return {"days": [], "n": [], "breached": [], "clipped": False}
    days_sorted = sorted(by_day)
    first, last = date.fromisoformat(days_sorted[0]), date.fromisoformat(days_sorted[-1])
    start = max(first, f.d_from) if f.d_from else first
    try:
        end_default = date.fromisoformat(today) if today else last
    except ValueError:
        end_default = last
    end = max(min(f.d_to, end_default) if f.d_to else end_default, last)
    if end < start:
        start, end = first, last
    clipped = (end - start).days + 1 > TREND_MAX_DAYS
    if clipped:
        start = end - timedelta(days=TREND_MAX_DAYS - 1)
    days, n, breached = [], [], []
    d = start
    while d <= end:
        k = d.isoformat()
        r = by_day.get(k)
        days.append(k)
        n.append(int(r["n"]) if r else 0)
        breached.append(int(r["breached"]) if r else 0)
        d += timedelta(days=1)
    return {"days": days, "n": n, "breached": breached, "clipped": clipped}


def _note(r: dict, now: datetime) -> str:
    """One plain phrase saying where a lead stands, in the wording of the Today page."""
    state, due, first = r["state"], r.get("sla_due_at"), r.get("first_reply")
    if state == "no_deadline":
        return "no deadline on record"
    if state in ("on_time", "late") and first is not None and r.get("created_at") is not None:
        if first < r["created_at"]:
            return "reply is time-stamped before the lead arrived (data anomaly)"
        wait = td._span((first - r["created_at"]).total_seconds())
        if state == "on_time":
            return f"replied {wait} after arrival"
        return f"replied {wait} after arrival, {td._span((first - due).total_seconds())} after the deadline"
    if state == "past_sla" and due is not None:
        return f"{td._span((now - due).total_seconds())} past the deadline, no reply"
    if state == "pending" and due is not None:
        return f"deadline in {td._span((due - now).total_seconds())}"
    return ""


def build_table(rows: list[dict], total: int, page: PageSpec, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    pages = max(1, math.ceil(total / page.size)) if total else 1
    out = []
    for r in rows:
        out.append({
            "lead_id": r["lead_id"], "name": r["full_name"], "company": r["company"] or "", "source": r["src"], "rep": r["rep"],
            "sync": r["sync"], "score": r["score"], "created_at": _iso(r["created_at"]), "sla_due_at": _iso(r["sla_due_at"]),
            "first_reply_at": _iso(r["first_reply"]),
            "hours": None if r["hours_to_reply"] is None else round(float(r["hours_to_reply"]), 2),
            "state": r["state"], "state_label": STATE_LABEL.get(r["state"], r["state"]), "note": _note(r, now),
            "stages": {"assigned": bool(r["st_assigned"]), "analyzed": bool(r["st_analyzed"]), "task": bool(r["st_task"]),
                       "update": bool(r["st_update"])},
        })
    return {"rows": out, "total": total, "page": min(page.page, pages), "pages": pages, "page_size": page.size,
            "sort": page.sort, "dir": page.dir, "from": (min(page.page, pages) - 1) * page.size + 1 if out else 0,
            "to": (min(page.page, pages) - 1) * page.size + len(out)}


# ============================================================================ CSV (pure)
CSV_COLUMNS = [   # header, key in the export row
    ("lead_id", "lead_id"), ("created_at_utc", "created_at"), ("lead_name", "full_name"), ("company", "company"), ("source", "src"),
    ("sales_rep", "rep"), ("clickup_sync", "sync"), ("ai_score", "score"), ("sla_due_at_utc", "sla_due_at"),
    ("sla_outcome", "state"), ("first_reply_at_utc", "first_reply"), ("hours_to_first_reply", "hours_to_reply"),
    ("has_rep", "st_assigned"), ("has_ai_analysis", "st_analyzed"), ("has_clickup_task", "st_task"), ("has_reply", "st_update"),
]
CSV_EXCLUDED = "email, phone, ai_notes, raw_payload, and any credential or token: they are never selected"


def csv_cell(v) -> str:
    """One CSV cell. Text that starts with = + - @ ; tab or carriage return is prefixed with an apostrophe, so a spreadsheet
    shows it as text instead of running it as a formula (CSV injection). Numbers are written as numbers, never prefixed."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return "" if math.isnan(v) else f"{v:.2f}"
    if hasattr(v, "as_tuple"):                     # Decimal from the database
        return f"{float(v):.2f}"
    if isinstance(v, datetime):
        return _iso(v) or ""
    s = str(v).replace("\x00", "")
    return "'" + s if s.startswith(CSV_FORMULA_LEADERS) else s


def to_csv(rows: list[dict]) -> bytes:
    """UTF-8 with a byte-order mark (Excel then opens accents correctly), CRLF line ends, every cell through csv_cell()."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow([h for h, _ in CSV_COLUMNS])
    for r in rows:
        w.writerow([csv_cell(r.get(k)) for _, k in CSV_COLUMNS])
    return b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8")


# ============================================================================ definitions (generated from the constants)
def build_definitions() -> list[dict]:
    """The text of the "Definitions" drawer. Built from the same constants and config the computations use, on every call
    (lesson L-085): change LEAD_SLA_HOURS in .env, restart, and this text follows."""
    sla = f"{td._sla_hours():g} business hours ({td._sla_window()})"
    stage_txt = "\n".join(f"{i + 1}. {label} - {ev}" for i, (_k, label, ev, _f) in enumerate(STAGES))
    bins = ", ".join(b["label"] for b in build_ttfr([])["bins"])
    outcomes = "\n".join(f"{label}: {meaning}." for _k, label, meaning in STATES)
    return [
        {"id": "lead", "term": "A lead in view", "text": "One row of the leads table, read through the v_leads_summary view, that passes every filter at the top. Different filters combine with AND; several values of the same filter (two reps, say) combine with OR. The date range is on the day the lead came in, in the database's time zone, both ends included."},
        {"id": "reply", "term": "Reply on record", "text": "A row in lead_updates: a ClickUp comment pulled into the database by the comment-pull job. It is the only evidence of a reply the database has, so if the pull job has not run, replies exist that this page cannot see (the amber note at the top says so when that is the case). The reply time is the comment's own time, or the time it was pulled when ClickUp gave none. The first reply is the earliest one."},
        {"id": "deadline", "term": "SLA deadline", "text": f"{sla} after the lead came in. It is worked out once, when the lead arrives (erp/leads.py with erp/business_hours.py), and stored in leads.sla_due_at. This page reads it and never recomputes it."},
        {"id": "outcomes", "term": "SLA outcomes", "text": "Every lead has exactly one:\n" + outcomes},
        {"id": "past", "term": "Past SLA", "text": f"No reply on record and the deadline is in the past. This is the same rule, from the same SQL, as '{W.TILE_PAST_SLA}' on the Today page, and both pages take that label and this text from one module (desktop/sla_words.py): with no filter set the two numbers are equal and the two pages use the same words. A lead that was answered, even late, is '{W.LABEL['late']}', not '{W.LABEL['past_sla']}'."},
        {"id": "reporting", "term": "Difference from Reporting", "text": "The Reporting page's 'SLA breached' counts every lead whose deadline has passed, whether or not it was answered. Here that group is split in two: answered late, and still waiting past the deadline. Together they are 'Breached' below."},
        {"id": "ontime", "term": "On-time rate", "text": f"Answered on time divided by every lead whose outcome is known (answered on time + answered late + past SLA). Leads still inside their deadline are left out because nobody knows yet how they will end. A percentage appears only when at least {LOW_N} leads stand behind it; below that the page shows the plain count, such as '2 of 3', because a percentage of three leads misleads."},
        {"id": "ttfr", "term": "Time to first reply", "text": f"Clock time from the moment the lead came in to its first reply on record (nights and weekends count, unlike the SLA deadline). Only leads with a reply appear. The median is shown with how many replies stand behind it; the 90th percentile appears from {P90_MIN_N} replies. A reply stamped before the lead arrived is counted as a data anomaly and left out of these statistics (it still counts as a reply for the SLA outcome). Bins: {bins}."},
        {"id": "funnel", "term": "Funnel", "text": f"The stages the pipeline really has, each proven by a row in a real table:\n{stage_txt}\nA lead counts at a stage only if it also has every earlier one, so a conversion never exceeds 100%. 'Step' compares a stage with the one before it, 'overall' with all leads received. The database has no separate lead-status column; the ClickUp sync status (pending, synced, failed, mocked, none) is shown under the funnel. Leads with a later stage but a missing earlier one are counted in a warning, not hidden."},
        {"id": "breakdowns", "term": "By rep and by source", "text": f"The same outcomes per sales rep (the lead's current assignment; '{UNASSIGNED}' when none) and per source ('{NO_SOURCE}' when empty), largest first, at most {BREAKDOWN_MAX_ROWS} rows. Click a row to filter by it."},
        {"id": "trend", "term": "Leads per day", "text": f"Leads by the day they came in (database calendar day), zero days included, up to today, at most the last {TREND_MAX_DAYS} days. The red part is leads that ended up breached."},
        {"id": "table", "term": "Detail table", "text": f"Every lead in view, {', '.join(map(str, PAGE_SIZES))} per page, sortable by clicking a column heading. Nothing is cut off silently: the count under the table is the true number of leads in view."},
        {"id": "csv", "term": "Download CSV", "text": f"The leads behind the current filters and sort, one row each, at most {EXPORT_MAX_ROWS:,} rows (a header says when a file was cut). UTF-8 with a byte-order mark so Excel reads accents correctly. Text cells that begin with = + - @ ; tab or carriage return get a leading apostrophe so a spreadsheet cannot run them as a formula. Not exported: {CSV_EXCLUDED}."},
        {"id": "fresh", "term": "How fresh is this?", "text": f"Read from the database when you open the page, change a filter or press Refresh (results are reused for {CACHE_SECONDS:g} s). Each panel is read independently and a panel that cannot be read says 'unavailable' while the rest keep working. Every query is cut off after {STATEMENT_TIMEOUT_MS / 1000:g} s and all of them together after {QUERY_BUDGET_SECONDS:g} s."},
    ]


# ============================================================================ assembly (pure)
def _unavailable(err: dict | None = None) -> dict:
    return {"state": "unavailable", "error": (err or {}).get("error", "could not be read")}


def _failed(block) -> bool:
    return td._failed(block)


def build_kpis(sla_panel: dict, ttfr_panel: dict, total_leads: int | None, filtered: bool) -> list[dict]:
    """Five headline numbers from the SLA and reply panels. Any tile whose source panel failed says unavailable."""
    def tile(id_, label, hint, **kw):
        t = {"id": id_, "label": label, "hint": hint, "state": "ok", "value": None, "text": None, "sub": "", "tone": "blue"}
        t.update(kw)
        return t
    h_leads = "Leads that pass every filter. Different filters combine with AND."
    h_rate = f"Answered on time out of every lead whose outcome is known. Shown as a percentage only with at least {LOW_N} leads behind it."
    h_past = W.hint_past_sla(f"{td._sla_hours():g} business hours ({td._sla_window()})")   # same text on the Today page
    h_wait = W.hint_awaiting()                                                             # same text on the Today page
    h_med = "Clock time from arrival to the first reply on record, median of the leads that have one."
    if sla_panel["state"] not in ("ok", "empty"):
        bad = "could not be read"
        return [tile("leads", "Leads in view", h_leads, state="unavailable", sub=bad, tone="grey"),
                tile("rate", "SLA on-time rate", h_rate, state="unavailable", sub=bad, tone="grey"),
                tile("past", W.TILE_PAST_SLA, h_past, state="unavailable", sub=bad, tone="grey"),
                tile("waiting", W.TILE_AWAITING, h_wait, state="unavailable", sub=bad, tone="grey"),
                tile("median", "Median first reply", h_med, state="unavailable", sub=bad, tone="grey")]
    s = sla_panel
    tiles = [tile("leads", "Leads in view", h_leads, value=s["total"], tone="blue",
                  sub=(f"of {total_leads} in the database" if filtered and total_leads is not None else "no filter set"))]
    r = s["on_time_rate"]
    if not s["decided"]:
        tiles.append(tile("rate", "SLA on-time rate", h_rate, text="-", tone="grey", sub="no lead has a known outcome yet"))
    elif r["low_n"]:
        tiles.append(tile("rate", "SLA on-time rate", h_rate, text=f"{r['n']} of {r['of']}", tone="amber",
                          sub=f"answered on time - too few leads (under {LOW_N}) for a percentage"))
    else:
        no_reply_at_all = s["waiting"] == s["total"] and s["total"] > 0
        tiles.append(tile("rate", "SLA on-time rate", h_rate, value=r["pct"], tone="green" if r["pct"] >= 90 else "amber" if r["pct"] >= 70 else "red",
                          sub=(f"{r['n']} of {r['of']} known outcomes - but no lead has a reply on record, so replies may simply not be synced"
                               if no_reply_at_all else f"{r['n']} of {r['of']} leads with a known outcome")))
    tiles.append(tile("past", W.TILE_PAST_SLA, h_past, value=s["past_sla_now"], tone="red" if s["past_sla_now"] else "green",
                      sub="still no reply after the deadline" if s["past_sla_now"] else W.SUB_NONE_PAST_SLA))
    tiles.append(tile("waiting", W.TILE_AWAITING, h_wait, value=s["waiting"], tone="amber" if s["waiting"] else "green",
                      sub=(f"{s['inside_deadline']} still inside their deadline" if s["waiting"] else W.all_replied(in_view=True))))
    if ttfr_panel["state"] == "unavailable":
        tiles.append(tile("median", "Median first reply", h_med, state="unavailable", sub=ttfr_panel.get("error", "could not be read"), tone="grey"))
    elif ttfr_panel.get("median_text"):
        tiles.append(tile("median", "Median first reply", h_med, text=ttfr_panel["median_text"], tone="violet", sub=f"n = {ttfr_panel['n']} repl{'y' if ttfr_panel['n'] == 1 else 'ies'}"))
    else:
        tiles.append(tile("median", "Median first reply", h_med, text="-", tone="grey", sub="no reply on record yet"))
    return tiles


def assemble(raw: dict, f: Filters, page: PageSpec, options: dict, meta: dict, caveat: str | None, blind: list[str],
             now: datetime | None = None) -> dict:
    """Raw query blocks (each rows or {"error"}) -> the JSON the page draws. Pure: no I/O."""
    now = now or datetime.now(timezone.utc)
    panels: dict[str, dict] = {}

    def panel(name: str, build, *needs: str):
        bad = [n for n in needs if _failed(raw.get(n))]
        if bad:
            panels[name] = _unavailable(raw.get(bad[0]) if isinstance(raw.get(bad[0]), dict) else None)
            return
        try:
            panels[name] = {"state": "ok", **build()}
        except Exception as exc:  # noqa: BLE001 - a bug in one builder must not take the whole page down
            logger.exception("Leads feed: panel %s failed to build", name)
            panels[name] = {"state": "unavailable", "error": f"{type(exc).__name__}: {_redact(str(exc))}"}

    panel("sla", lambda: build_sla(raw["sla"]), "sla")
    if panels["sla"]["state"] == "ok" and not panels["sla"]["total"]:
        panels["sla"]["state"] = "empty"
    panel("funnel", lambda: build_funnel(raw["funnel"], None if _failed(raw.get("sync")) else raw["sync"]), "funnel")
    if panels["funnel"]["state"] == "ok" and not panels["funnel"]["total"]:
        panels["funnel"]["state"] = "empty"
    panel("ttfr", lambda: build_ttfr([r["hours"] for r in raw["replies"]], len(raw["replies"])), "replies")
    if panels["ttfr"]["state"] == "ok" and not panels["ttfr"]["n"] and not panels["ttfr"]["anomalies"]:
        panels["ttfr"]["state"] = "empty"
    replies = None if _failed(raw.get("replies")) else raw["replies"]
    panel("by_rep", lambda: build_breakdown(raw["by_rep"], replies, "rep"), "by_rep")
    panel("by_source", lambda: build_breakdown(raw["by_source"], replies, "src"), "by_source")
    for name in ("by_rep", "by_source"):
        if panels[name]["state"] == "ok" and not panels[name]["rows"]:
            panels[name]["state"] = "empty"
    panel("trend", lambda: build_trend(raw["trend"], f, meta.get("today")), "trend")
    if panels["trend"]["state"] == "ok" and not panels["trend"]["days"]:
        panels["trend"]["state"] = "empty"
    panel("table", lambda: build_table(raw["table"], int(raw["table_total"][0]["n"]), page, now), "table", "table_total")
    if panels["table"]["state"] == "ok" and not panels["table"]["total"]:
        panels["table"]["state"] = "empty"

    kpis = build_kpis(panels["sla"], panels["ttfr"], options.get("total"), bool(f.active()))
    warnings: list[str] = []
    fun = panels["funnel"]
    if fun["state"] == "ok" and fun["anomalies"]:
        n = fun["anomalies"]
        warnings.append(f"{n} lead{'s' if n != 1 else ''} show{'s' if n == 1 else ''} evidence of a later stage while an earlier one is missing "
                        "(for example a reply on a lead with no rep); the funnel counts a lead only up to the stage it truly passed.")
    tt = panels["ttfr"]
    if tt["state"] == "ok" and tt["anomalies"]:
        n = tt["anomalies"]
        warnings.append(f"{n} {'reply is' if n == 1 else 'replies are'} time-stamped before {'its' if n == 1 else 'their'} lead arrived and "
                        f"{'was' if n == 1 else 'were'} left out of the response-time numbers (it still counts as answered on time in the SLA outcome, "
                        "because a reply is on record before the deadline).")
    if meta.get("view_rows") is not None and meta.get("lead_rows") is not None and meta["view_rows"] != meta["lead_rows"]:
        warnings.append(f"v_leads_summary returns {meta['view_rows']} rows for {meta['lead_rows']} leads (a lead has more than one "
                        "current rep or sync row), so some counts below may be inflated.")
    if tt["state"] == "ok" and tt.get("truncated"):
        warnings.append(f"Only the first {REPLY_ROWS_MAX:,} replies were read for the response-time numbers.")

    all_ok = all(p["state"] in ("ok", "empty") for p in panels.values())
    return {
        "ok": all_ok, "generated_at": _iso(now), "day": meta.get("today"),
        "filters": {"from": f.d_from.isoformat() if f.d_from else None, "to": f.d_to.isoformat() if f.d_to else None,
                    "rep": list(f.reps), "source": list(f.sources), "status": f.status, "sync": f.sync, "active": f.active()},
        "page": {"sort": page.sort, "dir": page.dir, "page": page.page, "page_size": page.size},
        "options": options, "caveat": caveat, "blind_inputs": blind, "warnings": warnings,
        "panels": panels, "kpis": kpis, "definitions": build_definitions(),
        "export_url": "/api/leads/export.csv?" + filters_to_query(f, page),
        "sources": {k: ("unavailable" if v["state"] == "unavailable" else "ok") for k, v in panels.items()},
        "config": {"low_n": LOW_N, "p90_min_n": P90_MIN_N, "page_sizes": list(PAGE_SIZES), "sla_hours": td._sla_hours(),
                   "sla_window": td._sla_window(), "trend_max_days": TREND_MAX_DAYS, "export_max_rows": EXPORT_MAX_ROWS},
    }


def build_options(rows: list[dict], meta: dict) -> dict:
    """The filter bar's choices and the whitelist behind them: the reps and sources that really exist, right now."""
    def pick(kind: str) -> list[dict]:
        items = [{"value": r["value"], "n": int(r["n"])} for r in rows if r["kind"] == kind]
        items.sort(key=lambda x: (-x["n"], x["value"].lower()))
        return items
    seen_sync = {r["value"]: int(r["n"]) for r in rows if r["kind"] == "sync"}
    return {
        "reps": pick("rep"), "sources": pick("source"),
        "syncs": [{"value": v, "n": seen_sync.get(v, 0)} for v in list(SYNC_VALUES) + sorted(set(seen_sync) - set(SYNC_VALUES))],
        "statuses": [{"value": k, "label": label, "meaning": meaning, "short": W.SHORT[k]} for k, label, meaning in STATES],
        "total": int(meta.get("lead_rows") or 0), "first_day": meta.get("first_day"), "last_day": meta.get("last_day"),
        "today": meta.get("today"),
    }


# ============================================================================ SQL panels (read-only)
class _Reader:
    """Runs statements on one read-only connection with the same hang safety as the Today feed (td.collect, L-083 / L-090): the
    timeouts are re-armed before EVERY statement, each is capped by what is left of the total budget, and once the budget is
    used up the remaining statements are refused instead of queueing behind a slow database."""

    def __init__(self, conn, budget_seconds: float, statement_ms: int = STATEMENT_TIMEOUT_MS) -> None:
        self.conn, self.statement_ms = conn, statement_ms
        self.deadline = time.monotonic() + budget_seconds
        self.raw: dict = {}

    def rows(self, sql: str, params: dict | None = None) -> list[dict]:
        left_ms = int((self.deadline - time.monotonic()) * 1000)
        if left_ms <= 50:
            raise TimeoutError("skipped: the database was too slow to answer in time")
        td.arm_timeouts(self.conn, min(self.statement_ms, left_ms))
        return [dict(r) for r in self.conn.execute(text(sql), params or {}).mappings().all()]

    def block(self, name: str, fn) -> None:
        try:
            self.raw[name] = fn()
        except Exception as exc:  # noqa: BLE001 - a missing table / a slow query must only take its own panel down
            try:
                self.conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            self.raw[name] = {"error": f"{type(exc).__name__}: {_redact(str(exc))}"}
            _log_once(name, self.raw[name]["error"])


_logged: dict[str, str] = {}


def _log_once(name: str, msg: str) -> None:
    if _logged.get(name) != msg:
        _logged[name] = msg
        logger.warning("Leads feed: %s unavailable: %s", name, msg)


def read_options(rd: _Reader) -> tuple[dict, dict]:
    """(options, meta). Raises when the leads cannot be read at all: without the whitelist no filter can be validated."""
    meta = rd.rows(META_SQL)[0]
    return build_options(rd.rows(OPTIONS_SQL), meta), meta


def collect(rd: _Reader, f: Filters, page: PageSpec) -> dict:
    """Every panel's query, each isolated (a failure gives {"error": ...} for that block only)."""
    where, params = where_sql(f)
    P = dict(params)
    q = lambda tail: BASE_SQL + tail.replace("__W__", where)      # noqa: E731 - `where` is built from fixed fragments only

    rd.block("sla", lambda: rd.rows(q("SELECT b.state, count(*) AS n, count(*) FILTER (WHERE b.first_reply IS NULL) AS waiting "
                                      "FROM b WHERE __W__ GROUP BY b.state"), P))
    rd.block("funnel", lambda: rd.rows(q("SELECT b.st_assigned, b.st_analyzed, b.st_task, b.st_update, count(*) AS n "
                                         "FROM b WHERE __W__ GROUP BY 1, 2, 3, 4"), P))
    rd.block("sync", lambda: rd.rows(q("SELECT b.sync, count(*) AS n FROM b WHERE __W__ GROUP BY b.sync"), P))
    rd.block("replies", lambda: rd.rows(q("SELECT b.rep, b.src, b.state, b.hours_to_reply AS hours FROM b "
                                          f"WHERE __W__ AND b.first_reply IS NOT NULL LIMIT {REPLY_ROWS_MAX}"), P))
    rd.block("by_rep", lambda: rd.rows(q("SELECT b.rep AS name, b.state, count(*) AS n FROM b WHERE __W__ GROUP BY 1, 2"), P))
    rd.block("by_source", lambda: rd.rows(q("SELECT b.src AS name, b.state, count(*) AS n FROM b WHERE __W__ GROUP BY 1, 2"), P))
    rd.block("trend", lambda: rd.rows(q("SELECT to_char(date_trunc('day', b.created_at), 'YYYY-MM-DD') AS day, count(*) AS n, "
                                        "count(*) FILTER (WHERE b.state IN ('late', 'past_sla')) AS breached "
                                        "FROM b WHERE __W__ GROUP BY 1 ORDER BY 1"), P))
    rd.block("table_total", lambda: rd.rows(q("SELECT count(*) AS n FROM b WHERE __W__"), P))
    if not _failed(rd.raw.get("table_total")):
        total = int(rd.raw["table_total"][0]["n"])
        pages = max(1, math.ceil(total / page.size)) if total else 1
        offset = (min(page.page, pages) - 1) * page.size
        rd.block("table", lambda: rd.rows(q(_ROW_SELECT + _order_sql(page) + " LIMIT :lim OFFSET :off"), {**P, "lim": page.size, "off": offset}))
    else:
        rd.raw["table"] = rd.raw["table_total"]
    return rd.raw


_ROW_SELECT = ("SELECT b.lead_id, b.full_name, b.company, b.src, b.rep, b.sync, b.score, b.created_at, b.sla_due_at, b.first_reply, "
               "b.state, b.hours_to_reply, b.st_assigned, b.st_analyzed, b.st_task, b.st_update FROM b WHERE __W__ ")


def _order_sql(page: PageSpec) -> str:
    """ORDER BY text. The expression is looked up in SORTS and the direction in a two-item tuple: request text never gets in."""
    expr = SORTS[page.sort if page.sort in SORTS else DEFAULT_SORT]
    direction = "ASC" if page.dir == "asc" else "DESC"
    return f"ORDER BY {expr} {direction} NULLS LAST, b.lead_id DESC"


# ============================================================================ store
class LeadsStore:
    """Builds the /api/leads/* payloads on demand. A small cache (a few seconds, one entry per distinct filter set) keeps a burst
    of identical requests from re-reading the database; no thread, nothing to stop. Never raises into the route."""

    def __init__(self, flow=None, ttl: float = CACHE_SECONDS) -> None:
        self.flow = flow
        self.ttl = ttl
        self._cache: OrderedDict[tuple, tuple[float, tuple[int, dict]]] = OrderedDict()
        self._cache_lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(MAX_CONCURRENT_BUILDS)

    # -- helpers -------------------------------------------------------------
    def _flow_data(self) -> dict | None:
        if self.flow is None:
            return None
        try:
            snap = self.flow.get()
            data = snap.get("data") if isinstance(snap, dict) else None
            return data if isinstance(data, dict) else None
        except Exception:  # noqa: BLE001 - the amber note degrades, the numbers do not care
            logger.exception("Leads feed: could not read the Data Flow snapshot")
            return None

    def _caveat(self, past_sla: int | None) -> tuple[str | None, list[str]]:
        """The same honesty line as the Today page (L-084): built from the same health cells by the same functions."""
        try:
            items = td.blind_inputs(td.build_health(self._flow_data(), None))
            return td._caveat(items, past_sla or 0), [i["id"] for i in items]
        except Exception:  # noqa: BLE001
            logger.exception("Leads feed: could not build the caveat")
            return None, []

    @staticmethod
    def _norm(params: Mapping[str, list[str]]) -> tuple:
        return tuple(sorted((k, tuple(v)) for k, v in params.items() if k in KNOWN_PARAMS))

    @staticmethod
    def _bad_request(problems: list[dict], options: dict | None = None) -> tuple[int, dict]:
        first = problems[0]
        return 400, {"ok": False, "error": f"Invalid {first['param']}: {first['why']}", "problems": problems, "options": options}

    def _busy(self) -> tuple[int, dict]:
        return 503, {"ok": False, "error": "The data service is busy reading the database. Try again in a moment."}

    # -- the analysis --------------------------------------------------------
    def analysis(self, params: Mapping[str, list[str]], fresh: bool = False) -> tuple[int, dict]:
        bad = unknown_params(params)          # before the cache: _norm() drops unknown names, so a hit would hide the typo
        if bad:
            return self._bad_request(bad)
        key = self._norm(params)
        now = time.monotonic()
        if not fresh:
            with self._cache_lock:
                hit = self._cache.get(key)
                if hit and now - hit[0] < self.ttl:
                    return hit[1]
        if not self._slots.acquire(timeout=BUILD_WAIT_SECONDS):
            logger.warning("Leads feed: all %d build slots are busy for more than %.0f s", MAX_CONCURRENT_BUILDS, BUILD_WAIT_SECONDS)
            with self._cache_lock:
                hit = self._cache.get(key)
            return hit[1] if hit else self._busy()
        try:
            try:
                result = self._build(params)
            except Exception as exc:  # noqa: BLE001 - last line of defence: never a 500 for the page
                logger.exception("Leads feed failed")
                result = 500, {"ok": False, "error": f"Unexpected error ({type(exc).__name__}). See erp_desktop.log."}
        finally:
            self._slots.release()
        if result[0] == 200:
            with self._cache_lock:
                self._cache[key] = (time.monotonic(), result)
                self._cache.move_to_end(key)
                while len(self._cache) > CACHE_KEYS_MAX:
                    self._cache.popitem(last=False)
        return result

    def _build(self, params: Mapping[str, list[str]]) -> tuple[int, dict]:
        try:
            with read_connect(engine).execution_options(postgresql_readonly=True) as conn:
                rd = _Reader(conn, QUERY_BUDGET_SECONDS)
                try:
                    options, meta = read_options(rd)
                except Exception as exc:  # noqa: BLE001
                    conn.rollback()
                    err = f"{type(exc).__name__}: {_redact(str(exc))}"
                    _log_once("options", err)
                    return 503, {"ok": False, "error": "Can’t read the leads right now - " + err, "unavailable": True}
                _logged.pop("options", None)
                f, prob_f = parse_filters(params, {o["value"] for o in options["reps"]}, {o["value"] for o in options["sources"]},
                                          [o["value"] for o in options["syncs"]])
                page, prob_p = parse_page(params)
                if prob_f or prob_p:
                    return self._bad_request(prob_f + prob_p, options)
                raw = collect(rd, f, page)
                conn.rollback()
        except Exception as exc:  # noqa: BLE001 - DB down: the connect itself failed
            err = f"{type(exc).__name__}: {_redact(str(exc))}"
            _log_once("database", err)
            return 503, {"ok": False, "error": "Can’t reach the database - " + err, "unavailable": True}
        _logged.pop("database", None)
        past = None if _failed(raw.get("sla")) else sum(int(r["n"]) for r in raw["sla"] if r["state"] == "past_sla")
        caveat, blind = self._caveat(past)
        return 200, assemble(raw, f, page, options, meta, caveat, blind)

    # -- the export ----------------------------------------------------------
    def export_csv(self, params: Mapping[str, list[str]]) -> tuple[int, bytes | dict, dict]:
        """(status, csv bytes | error dict, extra headers). Same filters, same whitelists, same SQL as the page."""
        bad = unknown_params(params)
        if bad:
            code, body = self._bad_request(bad)
            return code, body, {}
        if not self._slots.acquire(timeout=BUILD_WAIT_SECONDS):
            code, body = self._busy()
            return code, body, {}
        try:
            with read_connect(engine).execution_options(postgresql_readonly=True) as conn:
                rd = _Reader(conn, EXPORT_BUDGET_SECONDS, statement_ms=int(EXPORT_BUDGET_SECONDS * 1000))
                try:
                    options, _meta = read_options(rd)
                except Exception as exc:  # noqa: BLE001
                    conn.rollback()
                    return 503, {"ok": False, "error": "Can’t read the leads right now - " + f"{type(exc).__name__}: {_redact(str(exc))}"}, {}
                f, prob_f = parse_filters(params, {o["value"] for o in options["reps"]}, {o["value"] for o in options["sources"]},
                                          [o["value"] for o in options["syncs"]])
                page, prob_p = parse_page(params)
                if prob_f or prob_p:
                    code, body = self._bad_request(prob_f + prob_p)
                    return code, body, {}
                where, p = where_sql(f)
                rows = rd.rows(BASE_SQL + _ROW_SELECT.replace("__W__", where) + _order_sql(page) + f" LIMIT {EXPORT_MAX_ROWS + 1}", p)
                conn.rollback()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Leads export failed: %s: %s", type(exc).__name__, _redact(str(exc)))
            return 503, {"ok": False, "error": f"The export could not be read ({type(exc).__name__}). Try again in a moment."}, {}
        finally:
            self._slots.release()
        truncated = len(rows) > EXPORT_MAX_ROWS
        rows = rows[:EXPORT_MAX_ROWS]
        name = "leads_" + datetime.now().strftime("%Y%m%d_%H%M") + ("_filtered" if f.active() else "") + ".csv"
        return 200, to_csv(rows), {"Content-Disposition": f'attachment; filename="{name}"', "X-Row-Count": str(len(rows)),
                                    "X-Export-Truncated": "true" if truncated else "false"}
