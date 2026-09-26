"""
Live data feed for the "Sources & cohorts" tab of the ERP Desk Leads page: where leads come from, and how each arrival
cohort behaves over its first days. Written for DATA ANALYSTS (real numbers, definitions, filters, export).
  GET /api/leads/sources       the source table + the cohort curves for one set of filters (JSON)
  GET /api/leads/sources.csv   the same view as a CSV file for Excel (part=sources | cohorts)

It is a second read of the SAME leads the Leads page already defines. Nothing here re-implements a rule:

  base rows        desktop/leads_data.BASE_SQL - one row per lead with its SLA `state`, its stage flags and its first
                   reply. Its "past SLA" branch is desktop/today_data.lead_past_sla_sql, the very SQL the Today page
                   uses, so Today, Leads and this tab can never disagree about who is late.
  filters          desktop/leads_data.parse_filters / where_sql - the same whitelists, the same bound parameters.
                   This feed adds three more parameters of its own (bucket, metric, cohort) and one sort vocabulary.
  wording          desktop/sla_words.py - the five SLA outcomes and the two KPI tile texts, shared with Today/Leads.
  low numbers      desktop/leads_data.rate() - a percentage appears only with at least LOW_N leads behind it, else
                   the page shows "1 of 2". Nothing divides by zero, and no line is drawn through a single point.

What this tab adds that the Leads page does not have:
  SOURCE TABLE     one row per source: leads, share of the view, how many reached each pipeline stage (the SAME strict
                   funnel rule as the Leads page: a lead counts at a stage only if it also has every earlier stage),
                   the five SLA outcomes, the on-time rate, how many are past SLA now, the median time to first reply
                   with its n, and which reps the source's leads went to. Sorted server-side from a fixed vocabulary,
                   so the CSV is byte-for-byte the table on screen.
  COHORT CURVES    leads grouped by the day / week / month they ARRIVED (the database session's own calendar, see
                   `time_zone` in the payload), then followed day by day since each lead's own arrival:
                     replied   cumulative leads with a reply on record by day 0, 1, 2 ...
                     clickup   cumulative leads whose ClickUp task was created
                     past_sla  cumulative leads whose deadline passed without a reply having arrived by then
                   A cohort is only drawn up to the age EVERY lead in it has reached (its youngest lead's age), so the
                   denominator of every point is the full cohort - no point is diluted by leads that simply have not
                   had the time yet.

Honest by construction, which matters because today this database holds one single cohort:
  * a cohort with only one age point is drawn as POINTS, never as a line;
  * with a single cohort the page says in one line why there is only one, and what it needs to become useful;
  * the y axis switches to plain counts as soon as ANY cohort has fewer than LOW_N leads, and every figure carries its n;
  * a reply stamped before its lead arrived counts from day 0 and is reported as a data anomaly, never clamped silently.

Contract, same as report_data / today_data / leads_data: never raises into the caller. A query that fails degrades only
its own panel; every filter value is whitelisted and bound as a parameter, every sort key comes from a fixed dict, so no
request text ever reaches the SQL text. Read-only session, no write, no secret served.
"""
from __future__ import annotations

import csv
import io
import logging
import math
import statistics
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Mapping

from desktop import leads_data as ld
from desktop import sla_words as W
from desktop import today_data as td
from desktop.flow_data import _iso, _redact
from erp.db import read_connect, read_engine as engine

logger = logging.getLogger("erp_desk.sources")

# ---- knobs ----------------------------------------------------------------------------------------------------------
LOW_N = ld.LOW_N                 # a percentage needs at least this many leads behind it (shared with the Leads page)
MAX_COHORT_LINES = 12            # at most this many cohort curves are drawn (the newest); a warning says when more exist
MIN_USEFUL_COHORTS = 3           # below this the page says plainly that a cohort view cannot show a pattern yet
MAX_AGE_DAYS = {"day": 30, "week": 60, "month": 120}   # how far past arrival a cohort curve is followed, per bucket
COHORT_ROWS_MAX = 20_000         # leads read for the cohort maths; above this the chart refuses instead of lying
REPLY_ROWS_MAX = ld.REPLY_ROWS_MAX
SOURCE_ROWS_MAX = 40             # rows in the source table (a warning says how many more exist)
REPS_PER_SOURCE = 4              # rep chips shown per source row before "+n more"
EXPORT_MAX_ROWS = 20_000
CACHE_SECONDS = ld.CACHE_SECONDS
CACHE_KEYS_MAX = 24
QUERY_BUDGET_SECONDS = 6.0
EXPORT_BUDGET_SECONDS = 20.0
STATEMENT_TIMEOUT_MS = ld.STATEMENT_TIMEOUT_MS
BUILD_WAIT_SECONDS = ld.BUILD_WAIT_SECONDS
MAX_CONCURRENT_BUILDS = 3

# ---- fixed vocabularies (whitelists) --------------------------------------------------------------------------------
BUCKETS = [   # key, singular noun, how a cohort of this bucket is labelled
    ("day", "day", "the calendar day the lead arrived"),
    ("week", "week", "the ISO week (Monday to Sunday) the lead arrived in"),
    ("month", "month", "the calendar month the lead arrived in"),
]
BUCKET_KEYS = [k for k, _n, _w in BUCKETS]
BUCKET_NOUN = {k: n for k, n, _w in BUCKETS}
BUCKET_MEANING = {k: w for k, _n, w in BUCKETS}

METRICS = [   # key, short label, what one lead has to have done to be counted, the column of the cohort query
    ("replied", "Replied", "a reply is on record (a lead_updates row)", "reply_age"),
    ("clickup", "Reached ClickUp", "a ClickUp task was created for the lead (lead_clickup_sync.last_synced_at)", "task_age"),
    ("past_sla", "Past SLA", "the deadline passed without a reply having arrived by then", "breach_age"),
]
METRIC_KEYS = [k for k, _l, _m, _c in METRICS]
METRIC_LABEL = {k: label for k, label, _m, _c in METRICS}
METRIC_MEANING = {k: m for k, _l, m, _c in METRICS}
METRIC_COLUMN = {k: c for k, _l, _m, c in METRICS}
DEFAULT_METRIC = "replied"

# Sort key -> (column label, the value a source row is ordered by). The ONLY place a sort order comes from: the request
# names a key, never a column. Every function returns an ASCENDING value; `dir` decides the direction, and a row whose
# value is missing (no median, because nobody replied) always lands at the end, whichever direction is asked for.
SORTS = {
    "leads": ("Leads", lambda r: r["n"]),
    "name": ("Source", lambda r: r["name"].lower()),
    "assigned": ("Assigned", lambda r: r["stages"]["assigned"]["n"]),
    "analyzed": ("AI analysed", lambda r: r["stages"]["analyzed"]["n"]),
    "task": ("ClickUp", lambda r: r["stages"]["task"]["n"]),
    "replied": ("Replied", lambda r: r["stages"]["update"]["n"]),
    "on_time": ("On time", lambda r: r["counts"]["on_time"]),
    "past_sla": ("Past SLA", lambda r: r["counts"]["past_sla"]),
    "median": ("Median first reply", lambda r: r["median_h"]),
}
SORT_LABEL = {k: label for k, (label, _fn) in SORTS.items()}
DEFAULT_SORT, DEFAULT_DIR = "leads", "desc"
COHORT_OPTIONS_MAX = 400      # how many cohort keys the filter bar offers (the newest); the chart draws MAX_COHORT_LINES
CSV_PARTS = ("sources", "cohorts")
# Every parameter name this endpoint understands. Anything else is a 400 naming the parameter (lesson L-098).
KNOWN_PARAMS = ("from", "to", "rep", "source", "status", "sync", "bucket", "metric", "cohort", "sort", "dir", "part")

# The Leads page's "anomaly" rule, as SQL: evidence of a later stage while an earlier one is missing.
_ANOMALY_SQL = ("((NOT b.st_assigned AND (b.st_analyzed OR b.st_task OR b.st_update)) OR "
                "(NOT b.st_analyzed AND (b.st_task OR b.st_update)) OR (NOT b.st_task AND b.st_update))")

# ---- SQL --------------------------------------------------------------------------------------------------------
# The filter whitelist this feed adds to the reps/sources of the Leads page: which cohort keys really exist, per bucket,
# plus the database session's own time zone (it decides every day boundary here - lesson L-097).
COHORT_KEYS_SQL = """
SELECT 'tz' AS kind, current_setting('TimeZone') AS value, CAST(0 AS bigint) AS n
UNION ALL SELECT 'day',   to_char(date_trunc('day',   created_at), 'YYYY-MM-DD'), count(*) FROM leads GROUP BY 2
UNION ALL SELECT 'week',  to_char(date_trunc('week',  created_at), 'YYYY-MM-DD'), count(*) FROM leads GROUP BY 2
UNION ALL SELECT 'month', to_char(date_trunc('month', created_at), 'YYYY-MM-DD'), count(*) FROM leads GROUP BY 2
"""

SOURCE_TABLE_SQL = """
SELECT b.src AS name, count(*) AS n,
       count(*) FILTER (WHERE b.st_assigned) AS s_assigned,
       count(*) FILTER (WHERE b.st_assigned AND b.st_analyzed) AS s_analyzed,
       count(*) FILTER (WHERE b.st_assigned AND b.st_analyzed AND b.st_task) AS s_task,
       count(*) FILTER (WHERE b.st_assigned AND b.st_analyzed AND b.st_task AND b.st_update) AS s_update,
       count(*) FILTER (WHERE b.st_update) AS replied_any,
       count(*) FILTER (WHERE b.state = 'on_time') AS c_on_time,
       count(*) FILTER (WHERE b.state = 'late') AS c_late,
       count(*) FILTER (WHERE b.state = 'past_sla') AS c_past_sla,
       count(*) FILTER (WHERE b.state = 'pending') AS c_pending,
       count(*) FILTER (WHERE b.state = 'no_deadline') AS c_no_deadline,
       count(*) FILTER (WHERE __ANOM__) AS anomalies
FROM b WHERE __W__ GROUP BY 1
""".replace("__ANOM__", _ANOMALY_SQL)

REPS_SQL = "SELECT b.src AS name, b.rep, count(*) AS n FROM b WHERE __W__ GROUP BY 1, 2"

REPLIES_SQL = ("SELECT b.src AS name, b.hours_to_reply AS hours FROM b "
               "WHERE __W__ AND b.first_reply IS NOT NULL LIMIT " + str(REPLY_ROWS_MAX))

# One row per lead in view: which cohort it belongs to, how old it is now, and at what AGE (in days since its own
# arrival) each event happened. All the cohort maths is then pure Python, so every rule below is unit-testable.
COHORT_SQL = """
SELECT to_char(date_trunc(:bucket, b.created_at), 'YYYY-MM-DD') AS cohort,
       EXTRACT(EPOCH FROM (now() - b.created_at)) / 86400.0 AS age_now,
       EXTRACT(EPOCH FROM (b.first_reply - b.created_at)) / 86400.0 AS reply_age,
       EXTRACT(EPOCH FROM (cs.last_synced_at - b.created_at)) / 86400.0 AS task_age,
       CASE WHEN b.state IN ('late', 'past_sla')
            THEN EXTRACT(EPOCH FROM (b.sla_due_at - b.created_at)) / 86400.0 END AS breach_age,
       (b.st_task AND cs.last_synced_at IS NULL) AS task_undated
FROM b LEFT JOIN lead_clickup_sync cs ON cs.lead_id = b.lead_id
WHERE __W__ LIMIT """ + str(COHORT_ROWS_MAX + 1)


# ============================================================================ the view (filters this feed adds)
@dataclass(frozen=True)
class View:
    """The three choices that are this tab's own, plus the table's sort. Every value comes from a whitelist above."""
    bucket: str = "day"
    metric: str = DEFAULT_METRIC
    cohort: str | None = None
    sort: str = DEFAULT_SORT
    dir: str = DEFAULT_DIR
    bucket_auto: bool = True      # True = the server chose the bucket, False = the user picked it


def default_bucket(counts: Mapping[str, int]) -> tuple[str, str]:
    """(bucket, why) - the finest bucket the data span can actually carry.

    `counts` is how many distinct cohorts each bucket would produce for the leads in the database. The finest bucket
    that stays at or below MAX_COHORT_LINES wins, because a chart with 90 daily curves is not a chart. The "why" is
    shown on the page, so the reader never has to guess which bucket they are looking at or why."""
    for key in BUCKET_KEYS:
        n = int(counts.get(key) or 0)
        if n and n <= MAX_COHORT_LINES:
            others = [f"{BUCKET_NOUN[k]} would give {counts.get(k) or 0}" for k in BUCKET_KEYS if k != key and counts.get(k)]
            same = [k for k in BUCKET_KEYS if k != key and int(counts.get(k) or 0) == n]
            if n == 1:
                why = ("every lead in the database arrived inside a single " + BUCKET_NOUN[key] +
                       (", and " + " and ".join(BUCKET_NOUN[k] for k in same) + " would give one cohort too" if same else "") +
                       " - " + BUCKET_NOUN[key] + " is shown because it is the finest bucket there is")
            else:
                why = (f"{BUCKET_NOUN[key]} gives {n} cohorts, which fits the chart (at most {MAX_COHORT_LINES} curves); " +
                       ", ".join(others) if others else f"{BUCKET_NOUN[key]} gives {n} cohorts")
            return key, why
    last = BUCKET_KEYS[-1]
    n = int(counts.get(last) or 0)
    return last, (f"even by {BUCKET_NOUN[last]} the leads fall into {n} cohorts, more than the {MAX_COHORT_LINES} curves "
                  "the chart draws, so only the newest are shown" if n else "there is no lead to bucket yet")


def bucket_reason(counts: Mapping[str, int], bucket: str, auto: bool) -> str:
    """The sentence under the chart title. It must describe the bucket ACTUALLY in use: printing the automatic choice's
    reasoning next to a bucket the reader picked by hand reads like the page is arguing with itself."""
    if auto:
        return default_bucket(counts)[1]
    others = "; ".join(f"{BUCKET_NOUN[k]} would give {int(counts.get(k) or 0)}" for k in BUCKET_KEYS if k != bucket)
    n = int(counts.get(bucket) or 0)
    tail = f" (more than the {MAX_COHORT_LINES} curves the chart draws, so only the newest are shown)" if n > MAX_COHORT_LINES else ""
    return f"{n} cohort{'' if n == 1 else 's'} at this bucket{tail}; {others}"


def parse_view(params: Mapping[str, list[str]], cohort_keys: Mapping[str, list[str]],
               bucket_counts: Mapping[str, int]) -> tuple[View, list[dict]]:
    """Request parameters -> (View, problems). bucket / metric / sort / dir come from fixed lists, `cohort` from the
    cohort keys that really exist for the chosen bucket. A value that is not on a list is reported (the API answers
    400 naming the parameter) and never reaches SQL - an export that quietly ignored a filter would hand the analyst
    the wrong rows (lesson L-098)."""
    problems: list[dict] = []
    out: dict = {}

    def one(name: str) -> str | None:
        vals = [v for v in (params.get(name) or []) if v is not None]
        if len(vals) > 1:
            problems.append(ld._problem(name, vals[0], "give this filter only once"))
        return vals[-1] if vals else None

    auto_bucket, auto_why = default_bucket(bucket_counts)
    raw = one("bucket")
    if raw in (None, "", "auto"):
        out["bucket"], out["bucket_auto"] = auto_bucket, True
    elif raw not in BUCKET_KEYS:
        problems.append(ld._problem("bucket", raw, "not one of: " + ", ".join(BUCKET_KEYS) + ", auto"))
        out["bucket"], out["bucket_auto"] = auto_bucket, True
    else:
        out["bucket"], out["bucket_auto"] = raw, False

    raw = one("metric")
    if raw not in (None, ""):
        if raw not in METRIC_KEYS:
            problems.append(ld._problem("metric", raw, "not one of: " + ", ".join(METRIC_KEYS)))
        else:
            out["metric"] = raw

    raw = one("cohort")
    if raw not in (None, "", "all"):
        allowed = list(cohort_keys.get(out["bucket"]) or [])
        if raw not in allowed:
            problems.append(ld._problem("cohort", raw, f"not a {BUCKET_NOUN[out['bucket']]} cohort that exists in the database"))
        else:
            out["cohort"] = raw

    raw = one("sort")
    if raw not in (None, ""):
        if raw not in SORTS:
            problems.append(ld._problem("sort", raw, "not one of: " + ", ".join(SORTS)))
        else:
            out["sort"] = raw
    raw = one("dir")
    if raw not in (None, ""):
        if raw.lower() not in ("asc", "desc"):
            problems.append(ld._problem("dir", raw, "must be asc or desc"))
        else:
            out["dir"] = raw.lower()

    raw = one("part")
    if raw not in (None, "") and raw not in CSV_PARTS:
        problems.append(ld._problem("part", raw, "not one of: " + ", ".join(CSV_PARTS)))
    return View(**out), problems


def unknown_params(params: Mapping[str, list[str]]) -> list[dict]:
    """Any parameter name this endpoint does not understand. A typo such as ?sourc=web must never come back as 200 with
    every lead in the file (lesson L-098): the name itself is whitelisted, not only the value. Same helper, same wording
    and same 400 as the Funnel & SLA tab - only the list of names differs."""
    return ld.unknown_params(params, KNOWN_PARAMS)


def cohort_where(f: ld.Filters, view: View) -> tuple[str, dict]:
    """The Leads page's WHERE plus this tab's cohort filter. The cohort key is bound as a parameter and compared through
    date_trunc, so the comparison uses exactly the bucket the key was made with."""
    where, params = ld.where_sql(f)
    if view.cohort:
        where += " AND date_trunc(:bucket, b.created_at) = date_trunc(:bucket, CAST(:cohort AS timestamptz))"
        params = {**params, "bucket": view.bucket, "cohort": view.cohort}
    return where, params


def view_to_query(f: ld.Filters, view: View, part: str | None = None) -> str:
    """The query string that reproduces exactly what is on screen (used for the export links, so the file can never
    describe a different set of leads than the table above it - lesson L-101)."""
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
    pairs.append(("bucket", view.bucket))
    pairs.append(("metric", view.metric))
    if view.cohort:
        pairs.append(("cohort", view.cohort))
    pairs += [("sort", view.sort), ("dir", view.dir)]
    if part:
        pairs.append(("part", part))
    return urlencode(pairs)


# ============================================================================ labels (pure)
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def cohort_label(key: str, bucket: str) -> str:
    """'16 Sep 2026' / 'week of 14 Sep 2026' / 'Sep 2026' - the server words it, the page only prints it."""
    try:
        d = date.fromisoformat(key)
    except (TypeError, ValueError):
        return str(key)
    if bucket == "month":
        return f"{_MONTHS[d.month - 1]} {d.year}"
    if bucket == "week":
        return f"week of {d.day} {_MONTHS[d.month - 1]} {d.year}"
    return f"{d.day} {_MONTHS[d.month - 1]} {d.year}"


def cohort_end(key: str, bucket: str) -> str | None:
    """The last day a lead could arrive and still belong to this cohort (inclusive) - shown in the hover and the CSV."""
    try:
        d = date.fromisoformat(key)
    except (TypeError, ValueError):
        return None
    if bucket == "day":
        return d.isoformat()
    if bucket == "week":
        return (d + timedelta(days=6)).isoformat()
    nxt = date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1)
    return (nxt - timedelta(days=1)).isoformat()


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


# ============================================================================ the source table (pure)
def sort_sources(rows: list[dict], view: View) -> list[dict]:
    """Order the source rows by a whitelisted key. Rows whose value is missing (no median because nobody replied yet)
    always go last, in both directions - they are "unknown", not "smallest". Ties keep alphabetical order."""
    keyfn = SORTS.get(view.sort, SORTS[DEFAULT_SORT])[1]
    rows = sorted(rows, key=lambda r: r["name"].lower())          # stable tie-break
    present = [r for r in rows if keyfn(r) is not None]
    missing = [r for r in rows if keyfn(r) is None]
    present.sort(key=keyfn, reverse=(view.dir != "asc"))
    return present + missing


def build_sources(table_rows: list[dict], rep_rows: list[dict] | None, reply_rows: list[dict] | None,
                  view: View) -> dict:
    """[{name, n, s_*, c_*, anomalies}] + rep rows + reply hours -> one row per source, sorted by the whitelisted key.

    Every stage count is STRICT, exactly like the Leads page's funnel: a lead counts as "reached ClickUp" only if it
    also has a rep and an AI analysis. A lead with a later stage but a missing earlier one is counted in `anomalies`
    and reported, never hidden. Every rate goes through leads_data.rate(), so a percentage never appears below LOW_N."""
    hours: dict[str, list[float]] = {}
    for r in reply_rows or []:
        h = r.get("hours")
        if h is not None and float(h) >= 0:
            hours.setdefault(r["name"], []).append(float(h))
    reps: dict[str, list[dict]] = {}
    for r in rep_rows or []:
        reps.setdefault(r["name"], []).append({"name": r["rep"], "n": int(r["n"])})

    total = sum(int(r["n"]) for r in table_rows)
    rows: list[dict] = []
    anomalies = 0
    for r in table_rows:
        name, n = r["name"], int(r["n"])
        counts = {k: int(r["c_" + k] or 0) for k in W.KEYS}
        decided = counts["on_time"] + counts["late"] + counts["past_sla"]
        hs = sorted(hours.get(name, []))
        spread = sorted(reps.get(name, []), key=lambda x: (-x["n"], x["name"].lower()))
        anomalies += int(r["anomalies"] or 0)
        med = statistics.median(hs) if hs else None
        rows.append({
            "name": name, "n": n, "share": ld.rate(n, total),
            "stages": {"assigned": ld.rate(int(r["s_assigned"] or 0), n), "analyzed": ld.rate(int(r["s_analyzed"] or 0), n),
                       "task": ld.rate(int(r["s_task"] or 0), n), "update": ld.rate(int(r["s_update"] or 0), n)},
            "replied_any": int(r["replied_any"] or 0),
            "counts": counts, "decided": decided,
            "on_time_rate": ld.rate(counts["on_time"], decided), "past_sla": counts["past_sla"],
            "replied": len(hs), "median_h": med, "median_text": ld._hours_text(med) if med is not None else None,
            "median_known": reply_rows is not None,
            "reps": spread[:REPS_PER_SOURCE], "reps_more": max(0, len(spread) - REPS_PER_SOURCE),
            "reps_total": len(spread), "reps_known": rep_rows is not None,
            "anomalies": int(r["anomalies"] or 0),
        })
    rows = sort_sources(rows, view)
    more = max(0, len(rows) - SOURCE_ROWS_MAX)
    best = max((r for r in rows if r["decided"] >= LOW_N), key=lambda r: r["on_time_rate"]["pct"] or 0, default=None)
    return {"rows": rows[:SOURCE_ROWS_MAX], "more": more, "total_names": len(rows), "total_leads": total,
            "anomalies": anomalies, "sort": view.sort, "dir": view.dir,
            "best": {"name": best["name"], "rate": best["on_time_rate"]} if best else None}


# ============================================================================ the cohorts (pure)
def _floor_age(v) -> int | None:
    """An age in days -> the day bucket it falls in. A reply stamped BEFORE its lead arrived gives a negative age; it
    is counted from day 0 (it was already answered when the clock started) and reported separately as an anomaly."""
    if v is None:
        return None
    try:
        return int(math.floor(float(v)))
    except (TypeError, ValueError):
        return None


def build_cohorts(rows: list[dict], view: View, truncated: bool = False) -> dict:
    """[{cohort, age_now, reply_age, task_age, breach_age, task_undated}] -> one curve per arrival cohort.

    The rules, all visible on the page and in the Definitions drawer:
      * a cohort is followed only to the age its YOUNGEST lead has reached, so every point's denominator is the whole
        cohort (n never shrinks along the curve) and no point is diluted by leads that have not had the time yet;
      * the curve stops at MAX_AGE_DAYS[bucket] however old the cohort is;
      * a cohort with a single age point is marked `single_point`: the page draws points, never a line (a line through
        one point is a trend nobody measured);
      * `y_mode` is "pct" only when EVERY cohort drawn has at least LOW_N leads; otherwise the chart shows counts, so
        two cohorts of 2 and 200 leads are never compared as percentages;
      * at most MAX_COHORT_LINES curves (the newest); `more` says how many were left out."""
    col = METRIC_COLUMN[view.metric if view.metric in METRIC_COLUMN else DEFAULT_METRIC]
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(str(r["cohort"]), []).append(r)
    keys = sorted(groups)
    more = max(0, len(keys) - MAX_COHORT_LINES)
    drawn = keys[-MAX_COHORT_LINES:] if more else keys
    cap = MAX_AGE_DAYS.get(view.bucket, 30)

    cohorts: list[dict] = []
    anomalies = 0
    undated = 0
    clipped = False
    for key in drawn:
        g = groups[key]
        n = len(g)
        ages_now = [_floor_age(r["age_now"]) for r in g]
        observed = min([a for a in ages_now if a is not None], default=0)
        observed = max(0, observed)
        if observed > cap:
            observed, clipped = cap, True
        events = [_floor_age(r.get(col)) for r in g]
        anomalies += sum(1 for r in g if _floor_age(r.get(col)) is not None and float(r[col]) < 0)
        undated += sum(1 for r in g if view.metric == "clickup" and r.get("task_undated"))
        points = []
        for age in range(observed + 1):
            cum = sum(1 for e in events if e is not None and e <= age)
            points.append({"age": age, "cum": cum, "n": n, "rate": ld.rate(cum, n)})
        final = points[-1] if points else {"cum": 0, "rate": ld.rate(0, n)}
        cohorts.append({
            "key": key, "label": cohort_label(key, view.bucket), "start": key, "end": cohort_end(key, view.bucket),
            "n": n, "observed_age": observed, "points": points, "single_point": len(points) < 2,
            "final": final["cum"], "final_rate": final["rate"], "low_n": n < LOW_N,
        })
    y_mode = "pct" if cohorts and all(c["n"] >= LOW_N for c in cohorts) else "count"
    max_age = max((c["observed_age"] for c in cohorts), default=0)
    return {"bucket": view.bucket, "bucket_noun": BUCKET_NOUN[view.bucket], "metric": view.metric,
            "metric_label": METRIC_LABEL[view.metric], "metric_meaning": METRIC_MEANING[view.metric],
            "cohorts": cohorts, "more": more, "total_cohorts": len(keys), "max_age": max_age, "age_cap": cap,
            "clipped": clipped, "y_mode": y_mode, "anomalies": anomalies, "undated": undated,
            "single_cohort": len(cohorts) == 1, "truncated": truncated,
            "leads": sum(c["n"] for c in cohorts)}


# ============================================================================ the honest banner (pure)
def build_banner(cohorts: dict | None, sources: dict | None, blind: list[str], span_days: int | None) -> dict | None:
    """The one calm paragraph that says what this view can and cannot tell you yet. First rule that matches wins.
    It never scolds: it names what the view needs, and whether anything is standing in the way of getting it."""
    needs: list[str] = []
    webhook_blind = "webhook" in (blind or [])
    if webhook_blind:
        needs.append("the lead webhook listening again - while it is offline no new lead can arrive, so no new cohort "
                     "can ever start (Data Flow, Ctrl+4, shows its state)")
    if cohorts is None or sources is None:
        return None
    n_leads = sources.get("total_leads") or 0
    n_cohorts = len(cohorts.get("cohorts") or [])
    if not n_leads:
        return {"level": "empty", "title": "No lead matches these filters",
                "text": "Nothing to compare yet. Loosen a filter, or press Reset all in the filter bar above.",
                "needs": needs}
    if n_cohorts <= 1:
        span = ("" if span_days is None else
                ", and the first and the last lead in the database arrived " +
                ("less than a day" if span_days <= 0 else _plural(span_days, "day")) + " apart")
        one = cohorts["cohorts"][0] if n_cohorts else None
        need = [f"leads arriving over several {BUCKET_NOUN[cohorts['bucket']]}s - a cohort chart compares groups that "
                f"arrived at different times, and it starts to show a pattern from about {MIN_USEFUL_COHORTS} cohorts",
                f"at least {LOW_N} leads in each cohort before any figure can honestly become a percentage"] + needs
        return {"level": "single", "title": "One cohort only - this is what a single cohort looks like",
                "text": (f"Every one of the {_plural(n_leads, 'lead')} in view arrived inside one "
                         f"{BUCKET_NOUN[cohorts['bucket']]}"
                         + (f" ({one['label']}, {_plural(one['n'], 'lead')})" if one else "") + span
                         + ". There is therefore nothing to compare one curve against, so the figures below are drawn "
                           "as points, never as a trend line."),
                "needs": need}
    small = [c for c in cohorts["cohorts"] if c["n"] < LOW_N]
    if len(small) == n_cohorts:
        return {"level": "low_n", "title": f"Every cohort here has fewer than {LOW_N} leads",
                "text": (f"The {n_cohorts} cohorts hold {_plural(n_leads, 'lead')} between them, so the chart shows "
                         "plain counts instead of percentages and every figure carries its n. One lead more or less "
                         "would move a percentage by tens of points, which is why none is shown."),
                "needs": [f"about {LOW_N} leads per cohort before percentages start meaning anything"] + needs}
    if n_cohorts < MIN_USEFUL_COHORTS or needs:
        return {"level": "thin", "title": f"{n_cohorts} cohorts - readable, but still thin",
                "text": (f"{_plural(n_leads, 'lead')} in {n_cohorts} cohorts. "
                         + (f"{_plural(len(small), 'cohort')} of them have fewer than {LOW_N} leads and are shown as "
                            "counts. " if small else "")
                         + "The comparison gets sharper with every week of arrivals."),
                "needs": needs}
    return None


# ============================================================================ KPI tiles (pure)
def build_kpis(sources: dict | None, cohorts: dict | None, total_leads: int | None, filtered: bool,
               errors: dict[str, str]) -> list[dict]:
    """Five headline numbers. A tile whose panel failed says so instead of showing a zero."""
    def tile(id_, label, hint, **kw):
        t = {"id": id_, "label": label, "hint": hint, "state": "ok", "value": None, "text": None, "sub": "", "tone": "blue"}
        t.update(kw)
        return t

    h_src = "How many different lead sources the leads in view came from. A lead with no source at all is counted under '" + ld.NO_SOURCE + "'."
    h_top = "The source that brought the most leads in view, and its share. A share becomes a percentage only with at least " + str(LOW_N) + " leads behind it."
    h_coh = "How many arrival cohorts the leads in view fall into, with the bucket the chart is using. Cohorts are what the curves below compare."
    h_rep = "Leads in view with a reply on record (a ClickUp comment pulled into the database). This is the same evidence of a reply the Today and Leads pages use."
    h_past = W.hint_past_sla(f"{td._sla_hours():g} business hours ({td._sla_window()})")   # same text as Today and Leads
    bad = "could not be read"
    if sources is None:
        return [tile("sources", "Sources in view", h_src, state="unavailable", sub=errors.get("sources", bad), tone="grey"),
                tile("top", "Biggest source", h_top, state="unavailable", sub=errors.get("sources", bad), tone="grey"),
                tile("cohorts", "Arrival cohorts", h_coh, state="unavailable", sub=errors.get("cohorts", bad), tone="grey"),
                tile("replied", "Leads with a reply", h_rep, state="unavailable", sub=errors.get("sources", bad), tone="grey"),
                tile("past", W.TILE_PAST_SLA, h_past, state="unavailable", sub=errors.get("sources", bad), tone="grey")]
    rows, total = sources["rows"], sources["total_leads"]
    tiles = [tile("sources", "Sources in view", h_src, value=sources["total_names"], tone="blue",
                  sub=(f"across {_plural(total, 'lead')}" + (f" of {total_leads} in the database" if filtered and total_leads is not None else "")))]
    if rows:
        top = max(rows, key=lambda r: r["n"])
        share = top["share"]
        # underscores become spaces for reading, exactly as the filter pills and the table do; the value itself is not
        # normalised anywhere (two spellings of one campaign stay two rows - see the Definitions).
        tiles.append(tile("top", "Biggest source", h_top, text=top["name"].replace("_", " "), tone="violet",
                          sub=(f"{top['n']} of {share['of']} leads" + (f" · {round(share['pct'])}%" if share["pct"] is not None else " · too few leads for a percentage"))))
    else:
        tiles.append(tile("top", "Biggest source", h_top, text="-", tone="grey", sub="no lead in view"))
    if cohorts is None:
        tiles.append(tile("cohorts", "Arrival cohorts", h_coh, state="unavailable", sub=errors.get("cohorts", bad), tone="grey"))
    else:
        n = len(cohorts["cohorts"])
        tiles.append(tile("cohorts", "Arrival cohorts", h_coh, value=n, tone="amber" if n < MIN_USEFUL_COHORTS else "blue",
                          sub=(f"by {cohorts['bucket_noun']}" + (f", {cohorts['more']} older not drawn" if cohorts["more"] else "")
                               + ("" if n >= MIN_USEFUL_COHORTS else " - too few to compare yet"))))
    replied = sum(r["replied_any"] for r in rows)
    r_rate = ld.rate(replied, total)
    tiles.append(tile("replied", "Leads with a reply", h_rep, value=replied, tone="green" if replied else "amber",
                      sub=(f"of {_plural(total, 'lead')} in view" + (f" · {round(r_rate['pct'])}%" if r_rate["pct"] is not None else ""))))
    past = sum(r["past_sla"] for r in rows)
    tiles.append(tile("past", W.TILE_PAST_SLA, h_past, value=past, tone="red" if past else "green",
                      sub="still no reply after the deadline" if past else W.SUB_NONE_PAST_SLA))
    return tiles


# ============================================================================ definitions (generated from the constants)
def build_definitions(view: View, tz: str | None) -> list[dict]:
    """The "Definitions" drawer of this tab. Built from the same constants the numbers are built from (lesson L-085),
    and it names the database session's time zone, because that is what decides every day boundary here (L-097)."""
    zone = tz or "the database session's own time zone"
    sla = f"{td._sla_hours():g} business hours ({td._sla_window()})"
    stages = "\n".join(f"{i + 1}. {label} - {ev}" for i, (_k, label, ev, _f) in enumerate(ld.STAGES))
    metrics = "\n".join(f"{METRIC_LABEL[k]}: {METRIC_MEANING[k]}." for k in METRIC_KEYS)
    buckets = ", ".join(f"{BUCKET_NOUN[k]} = {BUCKET_MEANING[k]}" for k in BUCKET_KEYS)
    return [
        {"id": "scope", "term": "What this tab counts", "text": "Exactly the leads the filter bar above describes - the same filters, the same whitelists and the same SQL as the Funnel & SLA tab next to it. The two tabs can never show a different set of leads for the same filters."},
        {"id": "zone", "term": "Time zone of every day boundary", "text": f"Leads are bucketed into cohorts, and the date range is applied, by the calendar day of the database session, whose time zone is {zone}. Your browser may be in another zone, so a lead that arrived near local midnight can sit in what looks like the neighbouring day. Every timestamp in the CSV is UTC and says so in the column name, and each export carries the zone in its own column."},
        {"id": "source", "term": "Source", "text": f"leads.source, exactly as the lead form sent it. A lead with no source is grouped under '{ld.NO_SOURCE}'. Nothing is normalised, merged or spell-corrected: two spellings of one campaign are two rows here, which is itself worth seeing."},
        {"id": "share", "term": "Share of the view", "text": f"The source's leads divided by all leads in view. It becomes a percentage only with at least {LOW_N} leads in view; below that the table shows the plain count, such as '2 of 3', because a percentage of three leads misleads."},
        {"id": "stages", "term": "Reached each stage", "text": f"The same strict funnel the Funnel & SLA tab draws, counted per source:\n{stages}\nA lead counts at a stage only if it also has every earlier stage, so a column can never exceed the one to its left. Leads with a later stage but a missing earlier one are counted as a data warning above the table, never hidden."},
        {"id": "outcomes", "term": "SLA outcomes", "text": "Every lead is in exactly one:\n" + "\n".join(f"{label}: {meaning}." for _k, label, meaning in W.OUTCOMES) + f"\nThe deadline is {sla} after the lead came in. 'Past SLA' is the same rule, from the same SQL, as '{W.TILE_PAST_SLA}' on the Today page and on the Funnel & SLA tab: with no filter set all three numbers are equal."},
        {"id": "ontime", "term": "On-time rate", "text": f"Answered on time divided by every lead of that source whose outcome is known (answered on time + answered late + past SLA). Leads still inside their deadline are left out, because nobody yet knows how they will end. Shown as a percentage only from {LOW_N} decided leads."},
        {"id": "median", "term": "Median first reply", "text": "Clock time from arrival to the first reply on record (nights and weekends count, unlike the SLA deadline), median over the leads of that source that have a reply. The n next to it is how many replies stand behind it. A reply stamped before its lead arrived is left out of this median and reported as an anomaly."},
        {"id": "reps", "term": "Rep spread", "text": f"Which sales reps the source's leads are currently assigned to, biggest first, at most {REPS_PER_SOURCE} shown. It answers 'is this source one person's problem or everybody's?'. A lead with no current assignment is counted under '{ld.UNASSIGNED}'."},
        {"id": "cohort", "term": "Arrival cohort", "text": f"All the leads that arrived inside one bucket: {buckets}. The bucket is chosen for you as the finest one that produces at most {MAX_COHORT_LINES} cohorts, and the page says which and why; you can override it. Cohorts are what the curves compare - not sources."},
        {"id": "age", "term": "Days since arrival", "text": "The x axis. Day 0 is the first 24 hours after a lead arrived, day 1 the next, and so on - measured from each lead's OWN arrival, not from the start of its cohort, so a lead that came in on the Friday of a weekly cohort is not credited with four days it never had."},
        {"id": "curve", "term": "What a curve shows", "text": "Cumulative, never per-day: the point at day 3 is every lead of the cohort that had done it by the end of day 3. A curve can therefore only rise. The three metrics are:\n" + metrics},
        {"id": "observed", "term": "Where a curve stops", "text": f"A cohort is followed only up to the age its YOUNGEST lead has reached, so every point is divided by the whole cohort and the n never changes along the curve. A curve also stops at day {MAX_AGE_DAYS.get(view.bucket, 30)} for {BUCKET_NOUN[view.bucket]} cohorts. A cohort with only one age point is drawn as points, never as a line: a line through a single point is a trend nobody measured."},
        {"id": "ymode", "term": "Percentages or counts", "text": f"The chart shows percentages only when EVERY cohort drawn has at least {LOW_N} leads. As soon as one is smaller, all curves switch to plain counts, so a cohort of 2 is never compared with a cohort of 200 as if they were equally certain. Every hover always carries the raw count and the n."},
        {"id": "click", "term": "Clicking a point", "text": "Clicking a point on a curve narrows the table below (and its CSV) to the leads of that cohort. The chart itself deliberately keeps every cohort, so you can still see what you are comparing the selected one against; the chip in the filter bar and the line under the chart both say which cohort is selected."},
        {"id": "anomaly", "term": "Data anomalies", "text": "A reply time-stamped before its lead arrived is counted from day 0 (it was already answered when the clock started) and reported above the chart. It is never silently clamped or dropped. A ClickUp task with no last_synced_at timestamp cannot be placed on a day and is reported the same way."},
        {"id": "controls", "term": "Which control changes what", "text": "So that no control is ever quietly doing nothing: the date range, rep, source, status and ClickUp-sync filters narrow BOTH the table and the chart. The cohort bucket regroups the chart (and decides which cohorts you can select). The metric chooses which curve is drawn and changes only the chart; the source table does not depend on it. The selected cohort narrows the table and its CSV, and is marked - not filtered away - in the cohort CSV, so the curve you see and the file you download still hold the same cohorts. Sorting applies to the table and its CSV; the cohort file is always ordered by cohort, then by day."},
        {"id": "csv", "term": "The two CSV files", "text": f"'Source table' is one row per source exactly as shown, in the same sort order; 'Cohort curves' is one row per cohort and day since arrival, with a column marking the cohort selected on screen. Both apply the filters on screen, are UTF-8 with a byte-order mark (Excel reads accents correctly), use CRLF line ends and prefix any text cell starting with = + - @ ; tab or carriage return with an apostrophe so a spreadsheet cannot run it as a formula. Neither file contains a per-lead row, so {CSV_EXCLUDED}. At most {EXPORT_MAX_ROWS:,} rows."},
        {"id": "fresh", "term": "How fresh is this?", "text": f"Read from the database when you open the tab, change a filter or press Refresh (results are reused for {CACHE_SECONDS:g} s). Each panel is read independently, so one that cannot be read says 'unavailable' while the rest keep working. Every query is cut off after {STATEMENT_TIMEOUT_MS / 1000:g} s and all of them together after {QUERY_BUDGET_SECONDS:g} s."},
    ]


# ============================================================================ assembly (pure)
def assemble(raw: dict, f: ld.Filters, view: View, options: dict, meta: dict, tz: str | None,
             caveat: str | None, blind: list[str], now: datetime | None = None) -> dict:
    """Raw query blocks (each rows or {"error"}) -> the JSON the page draws. Pure: no I/O."""
    now = now or datetime.now(timezone.utc)
    panels: dict[str, dict] = {}
    errors: dict[str, str] = {}

    def panel(name: str, build, *needs: str):
        bad = [n for n in needs if td._failed(raw.get(n))]
        if bad:
            err = (raw.get(bad[0]) or {}).get("error", "could not be read") if isinstance(raw.get(bad[0]), dict) else "could not be read"
            panels[name] = {"state": "unavailable", "error": err}
            errors[name] = err
            return
        try:
            panels[name] = {"state": "ok", **build()}
        except Exception as exc:  # noqa: BLE001 - a bug in one builder must not take the whole tab down
            logger.exception("Sources feed: panel %s failed to build", name)
            err = f"{type(exc).__name__}: {_redact(str(exc))}"
            panels[name] = {"state": "unavailable", "error": err}
            errors[name] = err

    reps = None if td._failed(raw.get("reps")) else raw["reps"]
    replies = None if td._failed(raw.get("replies")) else raw["replies"]
    panel("sources", lambda: build_sources(raw["table"], reps, replies, view), "table")
    if panels["sources"]["state"] == "ok" and not panels["sources"]["rows"]:
        panels["sources"]["state"] = "empty"

    cohort_rows = raw.get("cohort")
    if isinstance(cohort_rows, list) and len(cohort_rows) > COHORT_ROWS_MAX:
        panels["cohorts"] = {"state": "too_big", "error": (
            f"{len(cohort_rows) - 1:,}+ leads are in view: more than the {COHORT_ROWS_MAX:,} this chart reads. "
            "Narrow the date range or pick a source, and the curves come back.")}
        errors["cohorts"] = "too many leads in view"
    else:
        panel("cohorts", lambda: build_cohorts(raw["cohort"], view), "cohort")
        if panels["cohorts"]["state"] == "ok" and not panels["cohorts"]["cohorts"]:
            panels["cohorts"]["state"] = "empty"

    src_panel = panels["sources"] if panels["sources"]["state"] in ("ok", "empty") else None
    coh_panel = panels["cohorts"] if panels["cohorts"]["state"] in ("ok", "empty") else None
    kpis = build_kpis(src_panel, coh_panel, options.get("total"), bool(f.active() or view.cohort), errors)

    warnings: list[str] = []
    if src_panel and src_panel.get("anomalies"):
        n = src_panel["anomalies"]
        warnings.append(f"{_plural(n, 'lead')} show{'s' if n == 1 else ''} evidence of a later stage while an earlier one "
                        "is missing (a reply on a lead with no rep, say); the stage columns count a lead only up to the "
                        "stage it truly passed, so the columns can read lower than you expect.")
    if src_panel and src_panel.get("more"):
        warnings.append(f"{src_panel['more']} more source{'s' if src_panel['more'] != 1 else ''} are not in the table "
                        f"(the {SOURCE_ROWS_MAX} largest are shown). The CSV has the same {SOURCE_ROWS_MAX} rows.")
    if coh_panel and coh_panel.get("anomalies"):
        n = coh_panel["anomalies"]
        warnings.append(f"{_plural(n, 'lead')} in the chart {'has' if n == 1 else 'have'} a "
                        f"{METRIC_LABEL[view.metric].lower()} time-stamp earlier than {'its' if n == 1 else 'their'} own "
                        "arrival; they are counted from day 0 rather than dropped or clamped.")
    if coh_panel and coh_panel.get("undated"):
        n = coh_panel["undated"]
        warnings.append(f"{_plural(n, 'lead')} reached ClickUp but {'has' if n == 1 else 'have'} no last_synced_at "
                        "time-stamp, so they cannot be placed on a day and are missing from this curve (they still "
                        "count in the ClickUp column of the table).")
    if coh_panel and coh_panel.get("more"):
        warnings.append(f"{coh_panel['more']} older cohort{'s' if coh_panel['more'] != 1 else ''} are not drawn "
                        f"(the newest {MAX_COHORT_LINES} are). Use the date range to reach them.")
    if coh_panel and coh_panel.get("clipped"):
        warnings.append(f"A cohort is older than the {MAX_AGE_DAYS.get(view.bucket, 30)} days these curves follow; "
                        "it is drawn up to that day only.")
    if meta.get("view_rows") is not None and meta.get("lead_rows") is not None and meta["view_rows"] != meta["lead_rows"]:
        warnings.append(f"v_leads_summary returns {meta['view_rows']} rows for {meta['lead_rows']} leads (a lead has more "
                        "than one current rep or sync row), so some counts here may be inflated.")

    span = None
    try:
        if options.get("first_day") and options.get("last_day"):
            span = (date.fromisoformat(options["last_day"]) - date.fromisoformat(options["first_day"])).days
    except (TypeError, ValueError):
        span = None
    banner = build_banner(coh_panel, src_panel, blind, span)

    all_ok = all(p["state"] in ("ok", "empty") for p in panels.values())
    return {
        "ok": all_ok, "generated_at": _iso(now), "day": meta.get("today"), "time_zone": tz,
        "in_view": (src_panel or {}).get("total_leads"),
        "filters": {"from": f.d_from.isoformat() if f.d_from else None, "to": f.d_to.isoformat() if f.d_to else None,
                    "rep": list(f.reps), "source": list(f.sources), "status": f.status, "sync": f.sync,
                    "cohort": view.cohort, "active": f.active() + (1 if view.cohort else 0)},
        "view": {"bucket": view.bucket, "bucket_auto": view.bucket_auto, "bucket_noun": BUCKET_NOUN[view.bucket],
                 "bucket_why": bucket_reason({b["value"]: b["cohorts"] for b in options.get("buckets", [])},
                                             view.bucket, view.bucket_auto),
                 "metric": view.metric, "metric_label": METRIC_LABEL[view.metric],
                 "cohort": view.cohort, "cohort_label": cohort_label(view.cohort, view.bucket) if view.cohort else None,
                 "sort": view.sort, "dir": view.dir},
        "options": options, "caveat": caveat, "blind_inputs": blind, "warnings": warnings, "banner": banner,
        "panels": panels, "kpis": kpis, "definitions": build_definitions(view, tz),
        "export_url": "/api/leads/sources.csv?" + view_to_query(f, view, "sources"),
        "export_cohort_url": "/api/leads/sources.csv?" + view_to_query(f, view, "cohorts"),
        "sources": {k: ("ok" if v["state"] in ("ok", "empty") else v["state"]) for k, v in panels.items()},
        "config": {"low_n": LOW_N, "max_lines": MAX_COHORT_LINES, "min_useful_cohorts": MIN_USEFUL_COHORTS,
                   "age_cap": MAX_AGE_DAYS.get(view.bucket, 30), "sla_hours": td._sla_hours(),
                   "sla_window": td._sla_window(), "export_max_rows": EXPORT_MAX_ROWS, "source_rows_max": SOURCE_ROWS_MAX},
    }


def build_options(base: dict, cohort_rows: list[dict]) -> tuple[dict, dict[str, list[str]], dict[str, list[dict]], str | None]:
    """The filter bar's choices: the Leads page's reps / sources / statuses, plus this tab's buckets, metrics and the
    cohort keys that really exist in the database, per bucket. That last list IS the whitelist the `cohort` parameter
    is checked against. Returns (options, keys per bucket, labelled cohorts per bucket, the session time zone)."""
    tz = next((str(r["value"]) for r in cohort_rows if r["kind"] == "tz"), None)
    keys: dict[str, list[str]] = {}
    counts: dict[str, int] = {}
    per_bucket: dict[str, list[dict]] = {}
    for b in BUCKET_KEYS:
        rows = sorted((r for r in cohort_rows if r["kind"] == b), key=lambda r: str(r["value"]))
        keys[b] = [str(r["value"]) for r in rows]
        counts[b] = len(rows)
        per_bucket[b] = [{"value": str(r["value"]), "label": cohort_label(str(r["value"]), b), "n": int(r["n"])}
                         for r in rows][-COHORT_OPTIONS_MAX:]
    auto, why = default_bucket(counts)
    options = dict(base)
    options.update({
        "buckets": [{"value": k, "label": BUCKET_NOUN[k].capitalize(), "meaning": BUCKET_MEANING[k], "cohorts": counts[k]} for k in BUCKET_KEYS],
        "metrics": [{"value": k, "label": METRIC_LABEL[k], "meaning": METRIC_MEANING[k]} for k in METRIC_KEYS],
        "sorts": [{"value": k, "label": SORT_LABEL[k]} for k in SORTS],
        "cohorts": per_bucket.get(auto, []),
        "bucket_auto": auto, "bucket_why": why, "time_zone": tz,
    })
    return options, keys, per_bucket, tz


# ============================================================================ CSV (pure)
SOURCE_CSV_COLUMNS = [
    ("source", "name"), ("leads", "n"), ("leads_in_view", "_total"), ("share_pct", "_share"),
    ("reached_assigned", "_st_assigned"), ("reached_ai_analysed", "_st_analyzed"), ("reached_clickup", "_st_task"),
    ("reached_replied", "_st_update"), ("replied_any_stage", "replied_any"),
    ("sla_answered_on_time", "_c_on_time"), ("sla_answered_late", "_c_late"), ("sla_past_sla_no_reply", "_c_past_sla"),
    ("sla_waiting_inside_deadline", "_c_pending"), ("sla_no_deadline", "_c_no_deadline"),
    ("sla_decided", "decided"), ("on_time_rate_pct", "_on_time_pct"),
    ("median_hours_to_first_reply", "median_h"), ("replies_counted", "replied"),
    ("sales_reps", "_reps"), ("stage_anomalies", "anomalies"), ("filter_time_zone", "_tz"),
]
COHORT_CSV_COLUMNS = [
    ("cohort_start", "cohort_start"), ("cohort_label", "cohort_label"), ("cohort_end", "cohort_end"),
    ("bucket", "bucket"), ("bucket_time_zone", "time_zone"), ("leads_in_cohort", "n"),
    ("days_since_arrival", "age"), ("metric", "metric"), ("reached_cumulative", "cum"), ("reached_pct", "pct"),
    ("selected_on_screen", "selected"),
]
CSV_EXCLUDED = ("no lead id, lead name, e-mail address, phone number, company, AI note or raw payload can appear in "
                "either file at all - they hold aggregate rows only")


def to_csv(columns: list[tuple[str, str]], rows: list[dict]) -> bytes:
    """UTF-8 with a byte-order mark (Excel then opens accents correctly), CRLF line ends, every cell through the Leads
    page's own csv_cell(), so a text cell that starts with = + - @ ; tab or CR can never run as a formula."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow([h for h, _ in columns])
    for r in rows:
        w.writerow([ld.csv_cell(r.get(k)) for _, k in columns])
    return b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8")


def source_csv_rows(sources: dict, tz: str | None) -> list[dict]:
    """The source table, flattened for the CSV - the same rows, in the same order, as the table on screen."""
    out = []
    for r in sources["rows"]:
        row = dict(r)
        row["_total"] = sources["total_leads"]
        row["_share"] = r["share"]["pct"]
        for k in ("assigned", "analyzed", "task", "update"):
            row["_st_" + k] = r["stages"][k]["n"]
        for k in W.KEYS:
            row["_c_" + k] = r["counts"][k]
        row["_on_time_pct"] = r["on_time_rate"]["pct"]
        row["_reps"] = "; ".join(f"{x['name']} ({x['n']})" for x in r["reps"]) + (f"; +{r['reps_more']} more" if r["reps_more"] else "")
        row["_tz"] = tz or ""
        out.append(row)
    return out


def cohort_csv_rows(cohorts: dict, tz: str | None, selected: str | None = None) -> list[dict]:
    """One row per (cohort, day since arrival) - the long format a pivot table wants. EVERY cohort is exported, exactly
    as the chart draws it; the cohort selected on screen is marked in its own column rather than filtering the others
    away, so the parameter is used and nothing is silently ignored (lesson L-098)."""
    out = []
    for c in cohorts["cohorts"]:
        mark = "" if not selected else ("yes" if c["key"] == selected else "no")
        for p in c["points"]:
            out.append({"cohort_start": c["start"], "cohort_label": c["label"], "cohort_end": c["end"],
                        "bucket": cohorts["bucket"], "time_zone": tz or "", "n": c["n"], "age": p["age"],
                        "metric": cohorts["metric"], "cum": p["cum"], "pct": p["rate"]["pct"], "selected": mark})
    return out


# ============================================================================ SQL panels (read-only)
def collect(rd: "ld._Reader", f: ld.Filters, view: View) -> dict:
    """Every panel's query, each isolated (a failure gives {"error": ...} for that block only). The cohort filter scopes
    the table, the rep spread, the reply times and the KPIs; the cohort CHART deliberately keeps every cohort, so the
    selected one can still be compared with the others (the page says so in one line under the chart)."""
    where_t, params_t = cohort_where(f, view)                 # table side: filters + the selected cohort
    where_c, params_c = ld.where_sql(f)                       # chart side: filters only
    params_c = {**params_c, "bucket": view.bucket}
    q = lambda sql, where: ld.BASE_SQL + sql.replace("__W__", where)    # noqa: E731 - `where` is fixed fragments only

    rd.block("table", lambda: rd.rows(q(SOURCE_TABLE_SQL, where_t), params_t))
    rd.block("reps", lambda: rd.rows(q(REPS_SQL, where_t), params_t))
    rd.block("replies", lambda: rd.rows(q(REPLIES_SQL, where_t), params_t))
    rd.block("cohort", lambda: rd.rows(q(COHORT_SQL, where_c), params_c))
    return rd.raw


# ============================================================================ store
class SourcesStore:
    """Builds the /api/leads/sources payloads on demand. A small cache (a few seconds, one entry per distinct request)
    keeps a burst of identical requests from re-reading the database; no thread, nothing to stop. Never raises into the
    route: every failure comes back as a status code and a sentence."""

    def __init__(self, flow=None, ttl: float = CACHE_SECONDS) -> None:
        self.flow = flow
        self.ttl = ttl
        self._cache: OrderedDict[tuple, tuple[float, tuple[int, dict]]] = OrderedDict()
        self._cache_lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(MAX_CONCURRENT_BUILDS)

    # -- helpers -------------------------------------------------------------
    def _caveat(self, past_sla: int | None) -> tuple[str | None, list[str]]:
        """The same honesty line as the Today and Leads pages (L-084), from the same health cells and functions."""
        try:
            flow_data = None
            if self.flow is not None:
                snap = self.flow.get()
                data = snap.get("data") if isinstance(snap, dict) else None
                flow_data = data if isinstance(data, dict) else None
            items = td.blind_inputs(td.build_health(flow_data, None))
            return td._caveat(items, past_sla or 0), [i["id"] for i in items]
        except Exception:  # noqa: BLE001 - the amber note degrades, the numbers do not care
            logger.exception("Sources feed: could not build the caveat")
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

    @staticmethod
    def _read_options(rd: "ld._Reader") -> tuple[dict, dict[str, list[str]], dict[str, list[dict]], str | None, dict]:
        base, meta = ld.read_options(rd)
        options, keys, per_bucket, tz = build_options(base, rd.rows(COHORT_KEYS_SQL))
        return options, keys, per_bucket, tz, meta

    @staticmethod
    def _parse(params: Mapping[str, list[str]], options: dict, keys: dict[str, list[str]],
               per_bucket: dict[str, list[dict]]) -> tuple[ld.Filters, View, list[dict]]:
        """Whitelist everything, then tell the filter bar which cohorts belong to the bucket actually in use."""
        problems = unknown_params(params)
        f, prob_f = ld.parse_filters(params, {o["value"] for o in options["reps"]}, {o["value"] for o in options["sources"]},
                                     [o["value"] for o in options["syncs"]])
        counts = {b["value"]: b["cohorts"] for b in options["buckets"]}
        view, prob_v = parse_view(params, keys, counts)
        options["cohorts"] = per_bucket.get(view.bucket, [])
        problems += prob_f + prob_v
        return f, view, problems

    # -- the payload ---------------------------------------------------------
    def sources(self, params: Mapping[str, list[str]], fresh: bool = False) -> tuple[int, dict]:
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
            logger.warning("Sources feed: all %d build slots are busy for more than %.0f s", MAX_CONCURRENT_BUILDS, BUILD_WAIT_SECONDS)
            with self._cache_lock:
                hit = self._cache.get(key)
            return hit[1] if hit else self._busy()
        try:
            try:
                result = self._build(params)
            except Exception as exc:  # noqa: BLE001 - last line of defence: never a 500 for the page
                logger.exception("Sources feed failed")
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
                rd = ld._Reader(conn, QUERY_BUDGET_SECONDS)
                try:
                    options, keys, per_bucket, tz, meta = self._read_options(rd)
                except Exception as exc:  # noqa: BLE001
                    conn.rollback()
                    err = f"{type(exc).__name__}: {_redact(str(exc))}"
                    ld._log_once("sources_options", err)
                    return 503, {"ok": False, "error": "Can’t read the leads right now - " + err, "unavailable": True}
                ld._logged.pop("sources_options", None)
                f, view, problems = self._parse(params, options, keys, per_bucket)
                if problems:
                    return self._bad_request(problems, options)
                raw = collect(rd, f, view)
                conn.rollback()
        except Exception as exc:  # noqa: BLE001 - DB down: the connect itself failed
            err = f"{type(exc).__name__}: {_redact(str(exc))}"
            ld._log_once("sources_database", err)
            return 503, {"ok": False, "error": "Can’t reach the database - " + err, "unavailable": True}
        ld._logged.pop("sources_database", None)
        past = None
        if not td._failed(raw.get("table")):
            past = sum(int(r["c_past_sla"] or 0) for r in raw["table"])
        caveat, blind = self._caveat(past)
        return 200, assemble(raw, f, view, options, meta, tz, caveat, blind)

    # -- the export ----------------------------------------------------------
    def export_csv(self, params: Mapping[str, list[str]]) -> tuple[int, bytes | dict, dict]:
        """(status, csv bytes | error dict, extra headers). Same filters, same whitelists, same SQL as the page: the
        file is the view on screen, never a wider one."""
        part = (params.get("part") or ["sources"])[-1]
        bad = unknown_params(params)
        if bad:
            code, body = self._bad_request(bad)
            return code, body, {}
        if not self._slots.acquire(timeout=BUILD_WAIT_SECONDS):
            code, body = self._busy()
            return code, body, {}
        try:
            with read_connect(engine).execution_options(postgresql_readonly=True) as conn:
                rd = ld._Reader(conn, EXPORT_BUDGET_SECONDS, statement_ms=int(EXPORT_BUDGET_SECONDS * 1000))
                try:
                    options, keys, per_bucket, tz, _meta = self._read_options(rd)
                except Exception as exc:  # noqa: BLE001
                    conn.rollback()
                    return 503, {"ok": False, "error": "Can’t read the leads right now - " + f"{type(exc).__name__}: {_redact(str(exc))}"}, {}
                f, view, problems = self._parse(params, options, keys, per_bucket)
                if problems:
                    code, body = self._bad_request(problems)
                    return code, body, {}
                if part not in CSV_PARTS:
                    part = "sources"
                if part == "sources":
                    where, p = cohort_where(f, view)
                    table = rd.rows(ld.BASE_SQL + SOURCE_TABLE_SQL.replace("__W__", where), p)
                    reps = rd.rows(ld.BASE_SQL + REPS_SQL.replace("__W__", where), p)
                    replies = rd.rows(ld.BASE_SQL + REPLIES_SQL.replace("__W__", where), p)
                    built = build_sources(table, reps, replies, view)
                    rows, columns = source_csv_rows(built, tz), SOURCE_CSV_COLUMNS
                else:
                    where, p = ld.where_sql(f)
                    p = {**p, "bucket": view.bucket}
                    raw_rows = rd.rows(ld.BASE_SQL + COHORT_SQL.replace("__W__", where), p)
                    if len(raw_rows) > COHORT_ROWS_MAX:
                        conn.rollback()
                        return 400, {"ok": False, "error": f"More than {COHORT_ROWS_MAX:,} leads are in view: narrow the "
                                                           "date range or pick a source, then export the cohort curves again.",
                                     "problems": [ld._problem("from", "", "the view is too wide for the cohort export")]}, {}
                    rows, columns = cohort_csv_rows(build_cohorts(raw_rows, view), tz, view.cohort), COHORT_CSV_COLUMNS
                conn.rollback()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Sources export failed: %s: %s", type(exc).__name__, _redact(str(exc)))
            return 503, {"ok": False, "error": f"The export could not be read ({type(exc).__name__}). Try again in a moment."}, {}
        finally:
            self._slots.release()
        truncated = len(rows) > EXPORT_MAX_ROWS
        rows = rows[:EXPORT_MAX_ROWS]
        # the cohort file always holds every cohort (the chart does too), so only the shared filters can narrow it
        narrowed = bool(f.active() or (view.cohort and part == "sources"))
        name = "lead_" + part + "_" + datetime.now().strftime("%Y%m%d_%H%M") + ("_filtered" if narrowed else "") + ".csv"
        return 200, to_csv(columns, rows), {"Content-Disposition": f'attachment; filename="{name}"',
                                            "X-Row-Count": str(len(rows)),
                                            "X-Export-Truncated": "true" if truncated else "false"}
