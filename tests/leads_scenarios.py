"""
Scenario table for the ERP Desk "Leads" page feed (desktop/leads_data.py), kept in the repo so the Reviewer, the next agent and
the merge gate (tests/smoke.py, check 4) can re-run it (lesson L-089).

  * pure functions, no database: filter parsing and whitelisting (a hostile value never reaches SQL), WHERE text + bound
    parameters, page/sort whitelist, rate() and the low-n rule, the strict funnel and its anomaly count, the response-time
    histogram, CSV cell escaping and the BOM, the "Definitions" text following LEAD_SLA_HOURS, panel assembly when a query fails
  * SQL scenarios on SYNTHETIC data, when the database is reachable: a private set of TEMPORARY tables (leads, users,
    lead_assignments, lead_ai_analysis, lead_clickup_sync, lead_updates) and a temporary v_leads_summary is created inside ONE
    database session, shadowing the real objects for that session only (pg_temp comes first in the search path). Nothing is ever
    written to a real table, no sequence is used (ids are explicit), and the session's temp objects vanish when it closes. The
    feed's own functions then run against it, so every number below is checked against values worked out by hand AND against
    direct SQL, and the Leads "past SLA" number is compared with the Today page's number computed on the same data.

Run (from the project root):
    venv\\Scripts\\python.exe -B -m tests.leads_scenarios
"""
from __future__ import annotations

import contextlib
import sys
from datetime import date
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

INJECTIONS = ["x' OR '1'='1", "a'; DROP TABLE leads; --", "%' UNION SELECT password FROM users --", "../../etc/passwd", "rep\x00x", "$(calc)"]


# =========================================================================================== pure functions
def _check_filters(add) -> None:
    from desktop import leads_data as ld

    reps, sources = {"Ann Rep", "Ben Rep", ld.UNASSIGNED}, {"web", "ads", ld.NO_SOURCE}
    f, prob = ld.parse_filters({"rep": ["Ann Rep"], "source": ["web", "ads"], "status": ["past_sla"], "sync": ["synced"],
                                "from": ["2026-09-01"], "to": ["2026-09-30"]}, reps, sources)
    add("filters: a valid request parses with no problems", not prob and f.reps == ("Ann Rep",) and f.sources == ("web", "ads")
        and f.status == "past_sla" and f.sync == "synced" and f.d_from == date(2026, 9, 1) and f.d_to == date(2026, 9, 30), (f, prob))
    add("filters: an empty request is 'no filter'", ld.parse_filters({}, reps, sources)[0] == ld.Filters() and ld.Filters().active() == 0)
    add("filters: the same rep twice is one value", ld.parse_filters({"rep": ["Ann Rep", "Ann Rep"]}, reps, sources)[0].reps == ("Ann Rep",))
    add("filters: status=all / empty means no status filter", ld.parse_filters({"status": ["all"], "sync": [""]}, reps, sources)[0].status is None)

    for bad in INJECTIONS:
        for name in ("rep", "source"):
            f, prob = ld.parse_filters({name: [bad]}, reps, sources)
            add(f"whitelist: hostile {name}={bad[:24]!r} is refused, not filtered", bool(prob) and not getattr(f, "reps" if name == "rep" else "sources"), prob)
        _f, prob = ld.parse_filters({"status": [bad]}, reps, sources)
        add(f"whitelist: hostile status={bad[:24]!r} is refused", bool(prob) and _f.status is None, prob)
        _f, prob = ld.parse_filters({"sync": [bad]}, reps, sources)
        add(f"whitelist: hostile sync={bad[:24]!r} is refused", bool(prob) and _f.sync is None, prob)
        _f, prob = ld.parse_filters({"from": [bad]}, reps, sources)
        add(f"whitelist: hostile from={bad[:24]!r} is refused", bool(prob) and _f.d_from is None, prob)
        _s, prob = ld.parse_page({"sort": [bad]})
        add(f"whitelist: hostile sort={bad[:24]!r} falls back to the default and is reported", bool(prob) and _s.sort == ld.DEFAULT_SORT, prob)
    _f, prob = ld.parse_filters({"rep": ["Zed Unknown"]}, reps, sources)
    add("whitelist: a rep that is not in the database is refused (an export never silently ignores a filter)", bool(prob))
    _f, prob = ld.parse_filters({"rep": ["x" * 400]}, reps, sources)
    add("whitelist: an over-long value is refused", bool(prob))
    _f, prob = ld.parse_filters({"status": ["past_sla", "late"]}, reps, sources)
    add("filters: a single-valued filter given twice is a problem", bool(prob))
    for d in ("2026-13-45", "20260901", "1999-12-31", "2101-01-01", "2026-9-1", "yesterday", "2026-09-01T00:00"):
        _f, prob = ld.parse_filters({"to": [d]}, reps, sources)
        add(f"dates: {d!r} is refused", bool(prob) and _f.d_to is None, prob)
    _f, prob = ld.parse_filters({"from": ["2026-09-10"], "to": ["2026-09-01"]}, reps, sources)
    add("dates: start after end is a problem", bool(prob))

    s, prob = ld.parse_page({"sort": ["hours"], "dir": ["ASC"], "page": ["3"], "page_size": ["50"]})
    add("page: sort / dir / page / page_size parse", not prob and (s.sort, s.dir, s.page, s.size) == ("hours", "asc", 3, 50), (s, prob))
    for k, v in (("dir", "sideways"), ("page", "0"), ("page", "-1"), ("page", "1e9"), ("page_size", "7"), ("page_size", "100000")):
        _s, prob = ld.parse_page({k: [v]})
        add(f"page: {k}={v} is refused", bool(prob), prob)
    add("page: every sort key has a fixed SQL expression", all(isinstance(e, str) and e.startswith(("b.", "lower(", "CASE")) for e in ld.SORTS.values()))
    add("page: _order_sql only ever emits a whitelisted expression", all(ld._order_sql(ld.PageSpec(k, "asc")).startswith("ORDER BY " + e) for k, e in ld.SORTS.items())
        and ld._order_sql(ld.PageSpec("bogus", "sideways")).startswith("ORDER BY " + ld.SORTS[ld.DEFAULT_SORT]) and " DESC " in ld._order_sql(ld.PageSpec("bogus", "sideways")))

    # WHERE text: only fixed fragments; every value is a bound parameter
    full = ld.Filters(date(2026, 9, 1), date(2026, 9, 30), ("Ann Rep", "x' OR 1=1 --"), ("web",), "late", "synced")
    sql, params = ld.where_sql(full)
    add("where: all six filters become bound parameters", set(params) == {"d_from", "d_to_excl", "reps", "sources", "status", "sync"}, params)
    add("where: no value text is in the SQL", not any(v in sql for v in ("Ann Rep", "OR 1=1", "web", "late", "synced", "2026")), sql)
    add("where: the end date is inclusive (excluded bound is the next day)", params["d_to_excl"] == date(2026, 10, 1), params)
    add("where: no filter is 'TRUE'", ld.where_sql(ld.Filters()) == ("TRUE", {}))
    qs = ld.filters_to_query(full, ld.PageSpec())
    add("export link: reproduces the filters (repeated rep, url-encoded)", "rep=Ann+Rep" in qs and "rep=x%27+OR+1%3D1+--" in qs and "from=2026-09-01" in qs and "sort=created_at" in qs, qs)


def _check_math(add) -> None:
    from desktop import leads_data as ld

    r = ld.rate(2, 3)
    add("rate: 2 of 3 has no percentage (n < LOW_N) and says why", r == {"n": 2, "of": 3, "pct": None, "low_n": True}, r)
    r = ld.rate(0, 0)
    add("rate: 0 of 0 does not divide by zero", r["pct"] is None and r["low_n"] and r["of"] == 0, r)
    r = ld.rate(None, None)
    add("rate: missing numbers are 0 of 0", r["of"] == 0 and r["n"] == 0)
    add("rate: exactly LOW_N leads gives a percentage", ld.rate(1, ld.LOW_N)["pct"] == 20.0 and not ld.rate(1, ld.LOW_N)["low_n"])
    add("rate: one below LOW_N does not", ld.rate(1, ld.LOW_N - 1)["pct"] is None)
    add("rate: 4 of 6 = 66.7", ld.rate(4, 6)["pct"] == 66.7)

    add("percentile: empty list is None, nearest-rank otherwise", ld.percentile([], 90) is None and ld.percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 90) == 9 and ld.percentile([5], 90) == 5)

    # SLA
    sla = ld.build_sla([{"state": "on_time", "n": 4, "waiting": 0}, {"state": "late", "n": 1, "waiting": 0}, {"state": "past_sla", "n": 1, "waiting": 1},
                        {"state": "pending", "n": 2, "waiting": 2}, {"state": "no_deadline", "n": 1, "waiting": 1}])
    add("sla: totals, decided and breached", (sla["total"], sla["decided"], sla["breached"], sla["waiting"], sla["past_sla_now"]) == (9, 6, 2, 4, 1), sla)
    add("sla: on-time rate is on the decided leads only (pending / no deadline excluded)", sla["on_time_rate"] == {"n": 4, "of": 6, "pct": 66.7, "low_n": False}, sla["on_time_rate"])
    empty = ld.build_sla([])
    add("sla: no leads at all is all zeros with no percentage", empty["total"] == 0 and empty["on_time_rate"]["pct"] is None and empty["decided"] == 0)
    add("sla: an unknown state key is ignored, not counted", ld.build_sla([{"state": "bogus", "n": 3, "waiting": 3}])["total"] == 0)

    # funnel
    def pat(a, b, c, d, n):
        return {"st_assigned": a, "st_analyzed": b, "st_task": c, "st_update": d, "n": n}
    fun = ld.build_funnel([pat(True, True, True, True, 3), pat(True, True, True, False, 4), pat(True, True, False, False, 2), pat(False, False, False, False, 1)])
    add("funnel: strict cumulative counts", [s["count"] for s in fun["stages"]] == [10, 9, 9, 7, 3], [s["count"] for s in fun["stages"]])
    add("funnel: step conversion is against the previous stage", fun["stages"][3]["step"] == {"n": 7, "of": 9, "pct": 77.8, "low_n": False}, fun["stages"][3]["step"])
    add("funnel: overall conversion is against all leads", fun["stages"][4]["overall"]["pct"] == 30.0, fun["stages"][4]["overall"])
    add("funnel: dropped per stage and the biggest drop", [s["dropped"] for s in fun["stages"]] == [0, 1, 0, 2, 4] and fun["biggest_drop"] == {"key": "update", "label": "Reply on record", "n": 4}, fun)
    add("funnel: no stage can convert above 100%", all(s["step"] is None or s["step"]["n"] <= s["step"]["of"] for s in fun["stages"]))
    add("funnel: no anomaly in a clean funnel", fun["anomalies"] == 0)
    weird = ld.build_funnel([pat(False, True, True, True, 2), pat(True, True, True, True, 3), pat(True, False, False, True, 1)])
    add("funnel: later-stage evidence without an earlier stage is counted, never hidden", weird["anomalies"] == 3 and [s["count"] for s in weird["stages"]] == [6, 4, 3, 3, 3], weird)
    z = ld.build_funnel([])
    add("funnel: no leads at all - zeros, no division", z["total"] == 0 and all(s["count"] == 0 for s in z["stages"]) and z["biggest_drop"] is None and z["stages"][1]["step"]["pct"] is None)
    small = ld.build_funnel([pat(True, True, True, True, 2), pat(True, True, True, False, 1)])
    add("funnel: a 3-lead funnel shows counts, not percentages (low n)", small["stages"][4]["step"]["pct"] is None and small["stages"][4]["step"]["n"] == 2 and small["stages"][4]["step"]["of"] == 3)
    withsync = ld.build_funnel([pat(True, True, True, True, 1)], [{"sync": "synced", "n": 3}, {"sync": "failed", "n": 5}])
    add("funnel: sync statuses sorted by size", [x["value"] for x in withsync["sync"]] == ["failed", "synced"], withsync["sync"])

    # response time
    t = ld.build_ttfr([1.0, 30.0, 2.0, -1.0, 3.0, None])
    add("ttfr: negative hours are anomalies, excluded from every statistic", t["n"] == 4 and t["anomalies"] == 1 and t["median_h"] == 2.5 and t["fastest_text"] == "1 h", t)
    add("ttfr: bins are exhaustive and add up to n", sum(b["n"] for b in t["bins"]) == 4 and len(t["bins"]) == len(ld.HIST_EDGES_H) + 1, t["bins"])
    add("ttfr: bin edges are half-open (exactly 1 h is in the 1-2 h bin)", [b["n"] for b in ld.build_ttfr([1.0])["bins"]][3] == 1)
    add("ttfr: p90 needs P90_MIN_N replies", t["p90_h"] is None and ld.build_ttfr([float(i) for i in range(1, ld.P90_MIN_N + 1)])["p90_h"] == float(ld.P90_MIN_N - 1))
    e = ld.build_ttfr([])
    add("ttfr: no replies is n=0 with no median (no crash)", e["n"] == 0 and e["median_text"] is None and e["p90_text"] is None)

    # breakdown
    bd = ld.build_breakdown([{"name": "A", "state": "on_time", "n": 2}, {"name": "A", "state": "past_sla", "n": 1}, {"name": "B", "state": "pending", "n": 1}],
                            [{"rep": "A", "hours": 2.0}, {"rep": "A", "hours": 4.0}, {"rep": "A", "hours": -3.0}], "rep")
    a = bd["rows"][0]
    add("breakdown: largest first, on-time rate with the low-n rule, median with its n", a["name"] == "A" and a["n"] == 3 and a["on_time_rate"]["low_n"] and a["replied"] == 2 and a["median_text"] == "3 h", a)
    add("breakdown: a name with nothing decided has no percentage", bd["rows"][1]["decided"] == 0 and bd["rows"][1]["on_time_rate"]["pct"] is None)
    add("breakdown: replies unavailable -> median_known False", ld.build_breakdown([{"name": "A", "state": "on_time", "n": 1}], None, "rep")["rows"][0]["median_known"] is False)
    many = ld.build_breakdown([{"name": f"n{i:02d}", "state": "on_time", "n": 1} for i in range(ld.BREAKDOWN_MAX_ROWS + 3)], [], "rep")
    add("breakdown: capped, and says how many are not shown", len(many["rows"]) == ld.BREAKDOWN_MAX_ROWS and many["more"] == 3)

    # trend
    tr = ld.build_trend([{"day": "2026-09-16", "n": 3, "breached": 2}, {"day": "2026-09-18", "n": 1, "breached": 0}], ld.Filters(), "2026-09-20")
    add("trend: zero days are filled in up to today", tr["days"] == ["2026-09-16", "2026-09-17", "2026-09-18", "2026-09-19", "2026-09-20"] and tr["n"] == [3, 0, 1, 0, 0], tr)
    add("trend: nothing in view -> empty series", ld.build_trend([], ld.Filters(), "2026-09-20")["days"] == [])
    long_ = ld.build_trend([{"day": "2025-01-01", "n": 1, "breached": 0}], ld.Filters(), "2026-09-20")
    add("trend: clipped to the newest TREND_MAX_DAYS and says so", len(long_["days"]) == ld.TREND_MAX_DAYS and long_["clipped"] and long_["days"][-1] == "2026-09-20")


def _check_csv(add) -> None:
    from datetime import datetime, timezone
    from decimal import Decimal

    from desktop import leads_data as ld

    for lead in ("=", "+", "-", "@", ";", "\t", "\r"):
        add(f"csv: text starting with {lead!r} is neutralised", ld.csv_cell(lead + "SUM(A1)") == "'" + lead + "SUM(A1)")
    add("csv: a normal cell is untouched", ld.csv_cell("Nguyen Van A") == "Nguyen Van A" and ld.csv_cell("a=b") == "a=b" and ld.csv_cell("Ana - Ben") == "Ana - Ben")
    add("csv: a real negative NUMBER is not turned into text", ld.csv_cell(-1.5) == "-1.50" and ld.csv_cell(-3) == "-3" and ld.csv_cell(Decimal("-0.25")) == "-0.25")
    add("csv: None, booleans and datetimes", ld.csv_cell(None) == "" and ld.csv_cell(True) == "yes" and ld.csv_cell(False) == "no"
        and ld.csv_cell(datetime(2026, 9, 16, 7, 10, 27, tzinfo=timezone.utc)) == "2026-09-16T07:10:27Z")
    add("csv: NUL bytes are stripped", "\x00" not in ld.csv_cell("a\x00b"))
    row = {k: None for _, k in ld.CSV_COLUMNS}
    row.update(lead_id=1, full_name='=HYPERLINK("http://evil.example","x")', company="@SUM(1+1)", src="web", rep="Ann, Rep", hours_to_reply=-2.0, st_update=True)
    raw = ld.to_csv([row])
    add("csv: starts with a UTF-8 byte-order mark (Excel)", raw[:3] == b"\xef\xbb\xbf")
    text = raw[3:].decode("utf-8")
    lines = text.split("\r\n")
    add("csv: header row, one data row, CRLF line ends", lines[0].startswith("lead_id,created_at_utc,lead_name") and len(lines) == 3 and lines[2] == "", lines[:2])
    add("csv: formula cells are prefixed inside their (quoted) field", "\"'=HYPERLINK(" in text and "'@SUM(1+1)" in text, text)
    add("csv: a comma inside a value stays one quoted cell", '"Ann, Rep"' in text)
    add("csv: no secret / PII / payload column is exported", not any(w in ld.to_csv([]).decode("utf-8").lower() for w in ("email", "phone", "raw_payload", "token", "password", "ai_notes")))
    add("csv: accents survive the round trip", "Nguyễn Thị Hồng" in ld.to_csv([{**row, "full_name": "Nguyễn Thị Hồng"}])[3:].decode("utf-8"))


def _check_text(add) -> None:
    """Definitions, KPI wording and panel assembly."""
    from desktop import leads_data as ld
    from desktop import today_data as td
    from erp import config

    defs = ld.build_definitions()
    ids = [d["id"] for d in defs]
    add("definitions: every metric family has an entry", {"lead", "reply", "deadline", "outcomes", "past", "ontime", "ttfr", "funnel", "csv"} <= set(ids), ids)
    joined = " ".join(d["text"] for d in defs)
    add("definitions: quote the current SLA hours, business window, low-n and page limits", f"{td._sla_hours():g} business hours" in joined and td._sla_window() in joined
        and f"at least {ld.LOW_N} leads" in joined and f"{ld.EXPORT_MAX_ROWS:,}" in joined)
    old = config.LEAD_SLA_HOURS
    try:
        config.LEAD_SLA_HOURS = 3.5
        add("definitions: follow LEAD_SLA_HOURS when the setting changes (lesson L-085)", "3.5 business hours" in " ".join(d["text"] for d in ld.build_definitions()))
        kp = ld.build_kpis({"state": "empty", **ld.build_sla([])}, {"state": "empty", "median_text": None, "n": 0}, 0, False)
        # the tile hints now come from desktop/sla_words.py and quote the deadline, so the rule is no longer "never name a
        # number" but "only ever the live one": with LEAD_SLA_HOURS patched to 3.5 the default 5 must be nowhere in sight
        s = str(kp)
        add("kpis: any SLA number in a tile hint is the live one, never a stale hardcoded 5",
            "3.5 business hours" in s and s.count("business hours") == s.count("3.5 business hours"), s[:200])
    finally:
        config.LEAD_SLA_HOURS = old
    add("definitions: no hardcoded copy of the outcome list (text is built from STATES)", all(label in joined for _k, label, _m in ld.STATES))

    # KPI wording for tiny numbers
    sla3 = ld.build_sla([{"state": "on_time", "n": 2, "waiting": 0}, {"state": "past_sla", "n": 1, "waiting": 1}])
    k3 = {k["id"]: k for k in ld.build_kpis({"state": "ok", **sla3}, {"state": "ok", "n": 2, "median_text": "1 h", "anomalies": 0}, 20, True)}
    add("kpis: 2 of 3 answered on time reads '2 of 3', never a percentage", k3["rate"]["text"] == "2 of 3" and k3["rate"]["value"] is None and "too few" in k3["rate"]["sub"], k3["rate"])
    add("kpis: a filtered view says how many leads exist in total", k3["leads"]["sub"] == "of 20 in the database", k3["leads"])
    k0 = {k["id"]: k for k in ld.build_kpis({"state": "empty", **ld.build_sla([])}, {"state": "empty", "median_text": None, "n": 0}, 0, False)}
    add("kpis: zero leads - dashes, zeros, no percentage, no crash", k0["rate"]["text"] == "-" and k0["past"]["value"] == 0 and k0["median"]["text"] == "-", k0)
    ku = ld.build_kpis({"state": "unavailable", "error": "boom"}, {"state": "unavailable", "error": "boom"}, None, False)
    add("kpis: an unavailable source makes every tile say so", all(k["state"] == "unavailable" for k in ku))

    # a failing query degrades only its own panel
    f, page = ld.Filters(), ld.PageSpec()
    ok_raw = {"sla": [{"state": "on_time", "n": 1, "waiting": 0}], "funnel": [{"st_assigned": True, "st_analyzed": True, "st_task": True, "st_update": True, "n": 1}],
              "sync": [{"sync": "synced", "n": 1}], "replies": [{"rep": "A", "src": "web", "state": "on_time", "hours": 1.0}],
              "by_rep": [{"name": "A", "state": "on_time", "n": 1}], "by_source": [{"name": "web", "state": "on_time", "n": 1}],
              "trend": [{"day": "2026-09-16", "n": 1, "breached": 0}], "table_total": [{"n": 0}], "table": []}
    opts = ld.build_options([{"kind": "rep", "value": "A", "n": 1}], {"lead_rows": 1, "today": "2026-09-16"})
    good = ld.assemble(ok_raw, f, page, opts, {"today": "2026-09-16", "lead_rows": 1, "view_rows": 1}, None, [])
    add("assemble: every panel ok on good data", good["ok"] and all(p["state"] in ("ok", "empty") for p in good["panels"].values()), {k: v["state"] for k, v in good["panels"].items()})
    for broken in ("sla", "funnel", "replies", "by_rep", "by_source", "trend", "table"):
        bad = dict(ok_raw, **{broken: {"error": "OperationalError: boom"}})
        out = ld.assemble(bad, f, page, opts, {"today": "2026-09-16", "lead_rows": 1, "view_rows": 1}, None, [])
        states = {k: v["state"] for k, v in out["panels"].items()}
        down = {k for k, v in states.items() if v == "unavailable"}
        expect = {"replies": {"ttfr"}, "sla": {"sla"}, "funnel": {"funnel"}, "by_rep": {"by_rep"}, "by_source": {"by_source"}, "trend": {"trend"}, "table": {"table"}}[broken]
        add(f"assemble: a failing '{broken}' query degrades only {sorted(expect)}", down == expect and not out["ok"], states)
    tot = ld.assemble(dict(ok_raw, table={"error": "x"}), f, page, opts, {"today": "2026-09-16"}, None, [])
    add("assemble: a broken table does not blank the tiles", [k["state"] for k in tot["kpis"]].count("ok") >= 4)
    w = ld.assemble(ok_raw, f, page, opts, {"today": "2026-09-16", "lead_rows": 3, "view_rows": 4}, None, [])
    add("assemble: a view that returns more rows than there are leads is warned about", any("v_leads_summary returns 4 rows for 3 leads" in x for x in w["warnings"]), w["warnings"])
    anom = ld.assemble(dict(ok_raw, replies=[{"rep": "A", "src": "web", "state": "on_time", "hours": -2.0}]), f, page, opts, {"today": "2026-09-16"}, None, [])
    add("assemble: a reply before its lead is a visible warning, in correct singular grammar", any("1 reply is time-stamped before its lead arrived" in x for x in anom["warnings"]), anom["warnings"])
    fa = ld.assemble(dict(ok_raw, funnel=[{"st_assigned": False, "st_analyzed": True, "st_task": True, "st_update": True, "n": 2}]), f, page, opts, {"today": "2026-09-16"}, None, [])
    add("assemble: a funnel anomaly warning uses plural grammar for 2 leads", any("2 leads show evidence" in x for x in fa["warnings"]), fa["warnings"])
    nr = ld.build_kpis({"state": "ok", **ld.build_sla([{"state": "past_sla", "n": 11, "waiting": 11}])}, {"state": "empty", "median_text": None, "n": 0}, 11, False)
    rate_tile = next(k for k in nr if k["id"] == "rate")
    add("kpis: 0% on-time with NO reply on record anywhere says replies may simply not be synced", "no lead has a reply on record" in rate_tile["sub"], rate_tile)
    some = ld.build_kpis({"state": "ok", **ld.build_sla([{"state": "past_sla", "n": 10, "waiting": 10}, {"state": "late", "n": 1, "waiting": 0}])}, {"state": "ok", "median_text": "1 h", "n": 1}, 11, False)
    add("kpis: ... but not once at least one reply exists", "no lead has a reply" not in next(k for k in some if k["id"] == "rate")["sub"])

    # the honesty line is the Today page's, from the same functions
    cav = ld.LeadsStore(flow=None)._caveat(2)
    add("caveat: with no health snapshot the page says the numbers may be incomplete (never a bare all-clear)", cav[0] is not None and cav[0].startswith("Numbers may be incomplete"), cav)


# ------------------------------------------------------------------------------- filter bar (JS, run under Node)
# The SALES REP / SOURCE pill strip's breakpoint, edge fade and scroll-preservation math (robustness cycle,
# 2026-09-24) lives in the browser (desktop/static/leads.js) with zero Python-reachable equivalent - the audit that
# asked for this coverage found no fg-multi/updateFade reference anywhere in this file. Rather than re-implement the
# logic a second time in Python (a THIRD copy free to drift, exactly what part 1 of this same fix just removed for
# the SLA wording), the pure math was extracted verbatim into desktop/static/leads_filterbar.js (no DOM, no
# `window`), which leads.js requires at runtime AND which this check runs for real under Node, so the browser and
# this scenario table exercise the exact same function bodies. See that file's header for the full rationale.
_NODE_DRIVER = r"""
const FB = require(__REQUIRE_PATH__);
function fade(scrollLeft, clientWidth, scrollWidth) { return FB.computeFade(scrollLeft, clientWidth, scrollWidth); }
const out = {
  full_row_min_width: FB.FULL_ROW_MIN_WIDTH,
  mode: { 390: FB.fgMultiMode(390), 820: FB.fgMultiMode(820), 1099: FB.fgMultiMode(1099),
          1100: FB.fgMultiMode(1100), 1101: FB.fgMultiMode(1101), 1440: FB.fgMultiMode(1440), 1920: FB.fgMultiMode(1920) },
  fade_fits_exact: fade(0, 500, 500),          // content exactly fills the strip: no overflow at all
  fade_fits_room: fade(0, 500, 300),           // content smaller than the strip: no overflow either
  fade_at_very_start: fade(0, 200, 400),       // scrolled all the way left: only the right side is clipped
  fade_at_very_end: fade(200, 200, 400),       // scrolled all the way right: only the left side is clipped
  fade_in_middle: fade(100, 200, 400),         // scrolled to the middle: BOTH sides are genuinely clipped
  fade_start_slack: fade(1, 200, 400),         // 1px short of 0: the slack still counts this as "at the start"
  fade_end_slack: fade(199, 200, 400),         // 1px short of the true end: the slack still counts as "at the end"
  scroll_basic: (function () {
    var m = FB.buildScrollMap([{ group: "rep", scrollLeft: 42 }, { group: "source", scrollLeft: 7 }]);
    return { rep: FB.scrollLeftFor(m, "rep"), source: FB.scrollLeftFor(m, "source"), missing: FB.scrollLeftFor(m, "nope") };
  })(),
  scroll_reordered: (function () {
    // the two groups are discovered in the OPPOSITE order after the rebuild (as a strip being added/removed, or the
    // DOM simply walking .fg-scroll in a different order, would do) - keying by data-group, not position, must
    // still resolve each one to ITS OWN saved offset, not the other group's.
    var m = FB.buildScrollMap([{ group: "source", scrollLeft: 15 }, { group: "rep", scrollLeft: 88 }]);
    return { rep: FB.scrollLeftFor(m, "rep"), source: FB.scrollLeftFor(m, "source") };
  })(),
  scroll_zero_not_restored: FB.scrollLeftFor(FB.buildScrollMap([{ group: "rep", scrollLeft: 0 }]), "rep"),
  scroll_new_group_not_present: FB.scrollLeftFor(FB.buildScrollMap([{ group: "rep", scrollLeft: 5 }]), "source")
};
process.stdout.write(JSON.stringify(out));
"""


def _find_node() -> str | None:
    import shutil
    node = shutil.which("node")
    if node:
        return node
    for cand in (r"C:\Program Files\nodejs\node.exe", r"C:\Program Files (x86)\nodejs\node.exe"):
        if Path(cand).is_file():
            return cand
    return None


def _check_filterbar_js(add) -> None:
    """The Leads filter bar's breakpoint / edge-fade / scroll-preservation pure functions
    (desktop/static/leads_filterbar.js), run for real under Node so this table exercises the exact code the browser
    runs, not a re-typed Python guess of what it does."""
    import json
    import subprocess

    node = _find_node()
    js_path = ROOT / "desktop" / "static" / "leads_filterbar.js"
    if not js_path.is_file():
        add("filterbar js: desktop/static/leads_filterbar.js exists", False, "file not found")
        return
    if not node:
        # Never block the gate on a machine with no Node installed (same "degrade, do not crash" philosophy as
        # every other optional dependency in this project, L-005) - but say so plainly, not silently.
        add("filterbar js: fade/breakpoint/scroll scenarios (skipped: node.exe not found on PATH)", True)
        return
    require_path = json.dumps(str(js_path).replace("\\", "/"))
    script = _NODE_DRIVER.replace("__REQUIRE_PATH__", require_path)
    try:
        r = subprocess.run([node, "-e", script], capture_output=True, text=True, encoding="utf-8", errors="replace",
                            timeout=15, check=False)
    except subprocess.TimeoutExpired:
        add("filterbar js: node driver finishes within 15s", False, "timed out")
        return
    except OSError as exc:
        add("filterbar js: node driver runs", False, f"{type(exc).__name__}: {exc}")
        return
    if r.returncode != 0:
        add("filterbar js: node driver exits 0", False, (r.stderr or r.stdout)[-600:])
        return
    try:
        out = json.loads(r.stdout)
    except ValueError:
        add("filterbar js: node driver prints valid JSON", False, r.stdout[-600:])
        return

    add("filterbar: FULL_ROW_MIN_WIDTH mirrors leads.css's `@container ld (min-width: 1100px)`", out["full_row_min_width"] == 1100, out["full_row_min_width"])
    m = out["mode"]
    add("breakpoint: 390px / 820px (narrow, laptop) stay compact", m["390"] == "compact" and m["820"] == "compact", m)
    add("breakpoint: 1099px (1px under) is still compact", m["1099"] == "compact", m)
    add("breakpoint: 1100px (the boundary itself) is already full-row", m["1100"] == "full-row", m)
    add("breakpoint: 1101px / 1440px / 1920px (wide) are full-row", m["1101"] == "full-row" and m["1440"] == "full-row" and m["1920"] == "full-row", m)

    add("fade: content that exactly fills the strip shows no fade on either side", out["fade_fits_exact"] == {"fadeStart": False, "fadeEnd": False}, out["fade_fits_exact"])
    add("fade: content smaller than the strip (room to spare) shows no fade either", out["fade_fits_room"] == {"fadeStart": False, "fadeEnd": False}, out["fade_fits_room"])
    add("fade: scrolled to the very start shows a fade only at the end (more content to the right)", out["fade_at_very_start"] == {"fadeStart": False, "fadeEnd": True}, out["fade_at_very_start"])
    add("fade: scrolled to the very end shows a fade only at the start (more content to the left)", out["fade_at_very_end"] == {"fadeStart": True, "fadeEnd": False}, out["fade_at_very_end"])
    add("fade: scrolled to the middle shows a fade on BOTH sides - genuinely clipped both ways", out["fade_in_middle"] == {"fadeStart": True, "fadeEnd": True}, out["fade_in_middle"])
    add("fade: 1px of slack from the start still reads as 'at the start' (no start fade)", out["fade_start_slack"] == {"fadeStart": False, "fadeEnd": True}, out["fade_start_slack"])
    add("fade: 1px of slack from the end still reads as 'at the end' (no end fade)", out["fade_end_slack"] == {"fadeStart": True, "fadeEnd": False}, out["fade_end_slack"])

    sb = out["scroll_basic"]
    add("scroll: each group's saved scrollLeft is returned for that group, not the other one", sb == {"rep": 42, "source": 7, "missing": None}, sb)
    sr = out["scroll_reordered"]
    add("scroll: two groups' positions never cross-contaminate, even discovered in the opposite DOM order", sr == {"rep": 88, "source": 15}, sr)
    add("scroll: a saved position of exactly 0 is treated as 'nothing to restore' (matches the page's own `if (was)`)", out["scroll_zero_not_restored"] is None, out["scroll_zero_not_restored"])
    add("scroll: a group that did not exist before the rebuild restores to nothing (not another group's offset)", out["scroll_new_group_not_present"] is None, out["scroll_new_group_not_present"])


# =========================================================================================== synthetic database
def _fixture_sql() -> list[str]:
    """Statements that build the private session objects. Hours are relative to now(), so the data never goes stale."""
    def h(x: float | None) -> str:
        return "NULL" if x is None else f"now() + interval '{x} hours'"

    users = [(9001, "Ann Rep"), (9002, "Ben Rep")]
    #        id    created  due     rep   source  AI     sync       reply(occurred)  name
    leads = [
        (9101, -72, -67, 9001, "web", True, "synced", -71, "Lead One"),      # on time, replied 1 h after arrival
        (9102, -72, -67, 9001, "ads", True, "synced", -42, "Lead Two"),      # answered late (30 h)
        (9103, -48, -43, 9002, "web", True, "synced", None, "Lead Three"),   # past SLA
        (9104, -1, 4, 9002, "ads", True, "pending", None, "Lead Four"),      # waiting, inside the deadline, no task yet
        (9105, -120, None, None, None, False, None, None, "Lead Five"),      # no deadline, no rep, no source, nothing else
        (9106, -96, -91, 9002, "web", True, "mocked", -94, "Lead Six"),      # on time, 2 h
        (9107, -24, -19, 9001, "web", True, "synced", -25, "Lead Seven"),    # reply stamped BEFORE arrival (data anomaly)
        (9108, -48, -43, None, "ads", True, "synced", -45, "Lead Eight"),    # reply but no rep at all (funnel anomaly), 3 h
        (9109, -2, 3, 9001, "ads", True, "synced", None, '=1+1 "evil"'),     # waiting; the name is a spreadsheet formula
    ]
    sql = [f"CREATE TEMP TABLE {t} (LIKE public.{t} INCLUDING DEFAULTS)" for t in ("users", "leads", "lead_assignments", "lead_ai_analysis", "lead_clickup_sync", "lead_updates")]
    for uid, name in users:
        sql.append(f"INSERT INTO users (id, full_name, email, role, is_active, created_at) VALUES ({uid}, '{name}', 'x{uid}@example.invalid', 'sales', true, now())")
    aid = 0
    for lid, created, due, rep, src, ai, sync, reply, name in leads:
        company = "@SUM(A1)" if lid == 9109 else "Synthetic Co"
        sql.append(f"INSERT INTO leads (id, full_name, email, company, source, created_at, sla_due_at) VALUES ({lid}, '{name.replace(chr(39), chr(39) * 2)}', 'l{lid}@example.invalid', "
                   f"'{company}', {'NULL' if src is None else repr(src)}, {h(created)}, {h(due)})")
        aid += 1
        if rep:
            sql.append(f"INSERT INTO lead_assignments (id, lead_id, sales_rep_id, assignment_reason, is_current, assigned_at) VALUES ({9500 + aid}, {lid}, {rep}, 'round_robin_new', true, {h(created)})")
        if ai:
            sql.append(f"INSERT INTO lead_ai_analysis (id, lead_id, model_name, potential_score, analyzed_at) VALUES ({9500 + aid}, {lid}, 'synthetic', {30 + aid}, {h(created)})")
        if sync:
            sql.append(f"INSERT INTO lead_clickup_sync (id, lead_id, sync_status) VALUES ({9500 + aid}, {lid}, '{sync}')")
        if reply is not None:
            sql.append(f"INSERT INTO lead_updates (id, lead_id, content, occurred_at) VALUES ({9500 + aid}, {lid}, 'synthetic reply', {h(reply)})")
    return sql


class _FixtureConn:
    """A real connection whose session has the synthetic temp objects. close() closes it (the temp objects go with it);
    execution_options() is recorded so a scenario can prove the feed asked for a READ ONLY connection."""

    def __init__(self, real, log):
        self._real, self._log = real, log

    def execution_options(self, **kw):
        self._log.append(kw)
        return self

    def __getattr__(self, name):
        return getattr(self._real, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def close(self):
        """Drop the temp objects first: the pool hands this session out again, and a leftover temp table would shadow the next one."""
        try:
            self._real.rollback()
            from sqlalchemy import text
            self._real.execute(text(
                "DROP VIEW IF EXISTS pg_temp.v_leads_summary; DROP TABLE IF EXISTS pg_temp.lead_updates, pg_temp.lead_clickup_sync, "
                "pg_temp.lead_ai_analysis, pg_temp.lead_assignments, pg_temp.leads, pg_temp.users"))
            self._real.commit()
        except Exception:  # noqa: BLE001 - never hand a dirty session back to the pool
            with contextlib.suppress(Exception):
                self._real.invalidate()
        self._real.close()


TEMP_TABLES = ("users", "leads", "lead_assignments", "lead_ai_analysis", "lead_clickup_sync", "lead_updates")


class FixtureEngine:
    """Stand-in for erp.db.engine while a scenario runs: every connect() opens a fresh session and builds the synthetic objects in it.
    `fixture_sql` is any callable returning the statements to run, so another scenario file (tests/sources_scenarios.py) can bring
    its own data - many arrival cohorts, say - without copying this machinery."""

    def __init__(self, real_engine, fixture_sql=None):
        self._engine = real_engine
        self._fixture_sql = fixture_sql or _fixture_sql
        self.options: list[dict] = []

    def connect(self):
        from sqlalchemy import text
        conn = self._engine.connect()
        try:
            viewdef = conn.execute(text("SELECT pg_get_viewdef(CAST('public.v_leads_summary' AS regclass))")).scalar()   # BEFORE the temp tables exist: unqualified names
            for stmt in self._fixture_sql():
                conn.execute(text(stmt))
            # L-107: prove the shadowing really is in place before anything unqualified is written or read
            missing = [t for t in TEMP_TABLES if conn.execute(text("SELECT to_regclass('pg_temp.' || :t)"), {"t": t}).scalar() is None]
            if missing:
                raise RuntimeError("temp shadowing incomplete for " + ", ".join(missing) + ": refusing to touch real tables")
            conn.execute(text("CREATE TEMP VIEW v_leads_summary AS " + viewdef.strip().rstrip(";")))
            conn.commit()             # temp objects only; the real tables are untouched. The session drops them when it closes
        except Exception:
            conn.close()
            raise
        return _FixtureConn(conn, self.options)


@contextlib.contextmanager
def synthetic_engine(module=None, fixture_sql=None):
    """Patch a feed module's engine with FixtureEngine for the duration of the with-block (default: desktop.leads_data)."""
    from erp.db import engine
    if module is None:
        from desktop import leads_data as module
    fx = FixtureEngine(engine, fixture_sql)
    old = module.engine
    module.engine = fx
    try:
        yield fx
    finally:
        module.engine = old


def _check_sql(add) -> None:
    from sqlalchemy import text

    from desktop import leads_data as ld
    from desktop import today_data as td

    with synthetic_engine() as fx:
        store = ld.LeadsStore(flow=None, ttl=0)
        code, p = store.analysis({}, fresh=True)
        add("sql: the analysis of the synthetic set answers 200 and every panel reads", code == 200 and p.get("ok"), (code, p.get("error"), {k: v.get("error") for k, v in (p.get("panels") or {}).items() if v.get("state") == "unavailable"}))
        if code != 200:
            return
        P = p["panels"]
        add("sql: the feed asked for a READ ONLY connection", bool(fx.options) and all(o.get("postgresql_readonly") is True for o in fx.options), fx.options)
        add("sql: 9 leads in view, 9 in the options", P["sla"]["total"] == 9 and p["options"]["total"] == 9)
        c = P["sla"]["counts"]
        add("sql: outcomes are exactly the hand-worked ones", c == {"on_time": 4, "late": 1, "past_sla": 1, "pending": 2, "no_deadline": 1}, c)
        add("sql: decided 6, breached 2, waiting 4, on-time rate 66.7%", (P["sla"]["decided"], P["sla"]["breached"], P["sla"]["waiting"], P["sla"]["on_time_rate"]["pct"]) == (6, 2, 4, 66.7), P["sla"])
        st = [s["count"] for s in P["funnel"]["stages"]]
        add("sql: strict funnel 9 > 7 > 7 > 6 > 4", st == [9, 7, 7, 6, 4], st)
        add("sql: the lead with a reply but no rep is a funnel anomaly", P["funnel"]["anomalies"] == 1 and P["funnel"]["biggest_drop"]["n"] == 2, P["funnel"])
        add("sql: sync statuses under the funnel", {x["value"]: x["n"] for x in P["funnel"]["sync"]} == {"synced": 6, "pending": 1, "mocked": 1, "none": 1}, P["funnel"]["sync"])
        tt = P["ttfr"]
        add("sql: response time - 4 usable replies, 1 anomaly, median 2.5 h", (tt["n"], tt["anomalies"], tt["median_h"]) == (4, 1, 2.5), tt)
        add("sql: histogram bins add up to the usable replies", sum(b["n"] for b in tt["bins"]) == 4)
        reps = {r["name"]: r for r in P["by_rep"]["rows"]}
        add("sql: per rep - Ann 4, Ben 3, unassigned 2", {k: v["n"] for k, v in reps.items()} == {"Ann Rep": 4, "Ben Rep": 3, ld.UNASSIGNED: 2}, {k: v["n"] for k, v in reps.items()})
        add("sql: per rep outcome counts", reps["Ben Rep"]["counts"]["past_sla"] == 1 and reps["Ben Rep"]["counts"]["on_time"] == 1 and reps["Ann Rep"]["counts"]["late"] == 1, reps)
        srcs = {r["name"]: r["n"] for r in P["by_source"]["rows"]}
        add("sql: per source - web 4, ads 4, (none) 1", srcs == {"web": 4, "ads": 4, ld.NO_SOURCE: 1}, srcs)
        add("sql: the trend has one bar per day with leads and the same total", sum(P["trend"]["n"]) == 9 and P["trend"]["breached"] and sum(P["trend"]["breached"]) == 2, P["trend"])
        add("sql: table total is 9 and rows are newest first", P["table"]["total"] == 9 and P["table"]["rows"][0]["lead_id"] == 9104, [r["lead_id"] for r in P["table"]["rows"]])
        add("sql: table shows the reply time and the plain-words note", {r["lead_id"]: r for r in P["table"]["rows"]}[9101]["hours"] == 1.0 and "replied 1 h after arrival" in {r["lead_id"]: r for r in P["table"]["rows"]}[9101]["note"])

        # cross-check against direct SQL, written differently on purpose
        with fx.connect() as conn:
            def one(sql):
                return conn.execute(text(sql)).scalar()
            direct_past = one("SELECT count(*) FROM leads l WHERE l.sla_due_at < now() AND NOT EXISTS (SELECT 1 FROM lead_updates u WHERE u.lead_id = l.id)")
            direct_late = one("SELECT count(*) FROM leads l JOIN (SELECT lead_id, min(COALESCE(occurred_at, synced_at)) t FROM lead_updates GROUP BY lead_id) r ON r.lead_id = l.id WHERE r.t > l.sla_due_at")
            direct_wait = one("SELECT count(*) FROM leads l WHERE NOT EXISTS (SELECT 1 FROM lead_updates u WHERE u.lead_id = l.id)")
            day_of_1 = one("SELECT to_char(created_at, 'YYYY-MM-DD') FROM leads WHERE id = 9101")
            direct_day = one(f"SELECT count(*) FROM leads WHERE to_char(created_at, 'YYYY-MM-DD') = '{day_of_1}'")
            # the Today page's own numbers, computed on the same synthetic data
            today_raw = td.collect(conn)
        add("sql: 'past SLA' equals direct SQL", P["sla"]["counts"]["past_sla"] == direct_past == 1, (P["sla"]["counts"]["past_sla"], direct_past))
        add("sql: 'answered late' equals direct SQL", P["sla"]["counts"]["late"] == direct_late == 1, (P["sla"]["counts"]["late"], direct_late))
        add("sql: 'awaiting first reply' equals direct SQL", P["sla"]["waiting"] == direct_wait == 4, (P["sla"]["waiting"], direct_wait))
        add("agreement: Leads 'past SLA now' == the Today page's 'Leads past SLA now' on the same data", not td._failed(today_raw.get("leads")) and today_raw["leads"]["overdue"][-1] == P["sla"]["past_sla_now"], (today_raw.get("leads"), P["sla"]["past_sla_now"]))
        add("agreement: Leads 'awaiting first reply' == the Today page's 'Awaiting first contact'", today_raw["leads"]["awaiting"][-1] == P["sla"]["waiting"], (today_raw["leads"]["awaiting"], P["sla"]["waiting"]))
        add("agreement: the Today attention list holds exactly the past-SLA lead", [r["id"] for r in today_raw["attn_leads"]] == [9103], today_raw["attn_leads"])

        # filters, one at a time, checked by hand
        def q(**kw):
            code, out = store.analysis({k: (v if isinstance(v, list) else [v]) for k, v in kw.items()}, fresh=True)
            return code, out
        code, o = q(rep="Ben Rep")
        add("sql filter: rep=Ben -> 3 leads, low-n on-time rate '1 of 2'", code == 200 and o["panels"]["sla"]["total"] == 3 and o["panels"]["sla"]["on_time_rate"] == {"n": 1, "of": 2, "pct": None, "low_n": True}, o.get("panels", {}).get("sla"))
        add("sql filter: the funnel, the table and the breakdowns follow the same filter", o["panels"]["funnel"]["total"] == 3 and o["panels"]["table"]["total"] == 3 and o["panels"]["by_source"]["rows"] and sum(r["n"] for r in o["panels"]["by_source"]["rows"]) == 3)
        code, o = q(rep=["Ben Rep", ld.UNASSIGNED])
        add("sql filter: two reps are OR-ed (3 + 2)", o["panels"]["sla"]["total"] == 5)
        code, o = q(source="ads", status="pending")
        add("sql filter: source AND status are AND-ed (ads + waiting = 2)", o["panels"]["sla"]["total"] == 2 and o["panels"]["table"]["total"] == 2, o["panels"]["sla"]["total"])
        code, o = q(status="past_sla")
        add("sql filter: status=past_sla -> exactly lead 9103", [r["lead_id"] for r in o["panels"]["table"]["rows"]] == [9103])
        code, o = q(sync="mocked")
        add("sql filter: sync=mocked -> 1 lead", o["panels"]["sla"]["total"] == 1)
        code, o = q(source=ld.NO_SOURCE)
        add("sql filter: the '(none)' source token finds the lead without a source", o["panels"]["sla"]["total"] == 1)
        code, o = q(**{"from": day_of_1, "to": day_of_1})
        add("sql filter: a one-day range equals the direct count for that day", o["panels"]["sla"]["total"] == direct_day and direct_day >= 1, (o["panels"]["sla"]["total"], direct_day))
        code, o = q(**{"from": "2099-01-01"})
        add("sql filter: a range with no leads -> every panel is 'empty', nothing is unavailable", code == 200 and o["ok"] and all(v["state"] == "empty" for k, v in o["panels"].items() if k in ("sla", "funnel", "ttfr", "by_rep", "by_source", "trend", "table")), {k: v["state"] for k, v in o["panels"].items()})
        add("sql filter: an empty view gives tiles with no percentage and no division", [k for k in o["kpis"] if k["id"] == "rate"][0]["text"] == "-")
        code, o = q(**{"from": "2000-01-01", "to": "2100-12-31"})
        add("sql filter: the widest range is everyone", o["panels"]["sla"]["total"] == 9)
        code, o = q(rep="x' OR '1'='1")
        add("sql filter: a hostile rep is a 400 that names the parameter, and nothing was run with it", code == 400 and o["problems"][0]["param"] == "rep", (code, o))
        code, o = q(sort="lead_id; DROP TABLE leads")
        add("sql filter: a hostile sort is a 400", code == 400, code)

        # table: sort + paging
        code, o = q(sort="lead_id", dir="asc", page_size="10")
        add("sql table: sort by id ascending", [r["lead_id"] for r in o["panels"]["table"]["rows"]][:3] == [9101, 9102, 9103])
        code, o = q(sort="state", dir="asc")
        add("sql table: sort by outcome puts 'past SLA' first", o["panels"]["table"]["rows"][0]["state"] == "past_sla", [r["state"] for r in o["panels"]["table"]["rows"]])
        code, o = q(sort="hours", dir="asc")
        add("sql table: sorting by reply time puts leads without a reply last (NULLS LAST)", o["panels"]["table"]["rows"][-1]["hours"] is None and o["panels"]["table"]["rows"][0]["hours"] == -1.0, [r["hours"] for r in o["panels"]["table"]["rows"]])
        code, o = q(page_size="10", page="1")
        add("sql table: page 1 of 1 for 9 leads at 10 per page", o["panels"]["table"]["pages"] == 1 and len(o["panels"]["table"]["rows"]) == 9)
        code, o = q(page="9")
        add("sql table: a page beyond the end is clamped, not an error", code == 200 and o["panels"]["table"]["page"] == 1 and len(o["panels"]["table"]["rows"]) == 9)

        # export
        code, body, headers = store.export_csv({})
        add("export: 200, a file, the row count header", code == 200 and isinstance(body, bytes) and headers["X-Row-Count"] == "9" and headers["X-Export-Truncated"] == "false"
            and "attachment" in headers["Content-Disposition"] and headers["Content-Disposition"].endswith('.csv"'), (code, headers))
        txt = body[3:].decode("utf-8")
        add("export: BOM + header + 9 data rows", body[:3] == b"\xef\xbb\xbf" and len(txt.split("\r\n")) == 11, len(txt.split("\r\n")))
        add("export: the lead whose name is a formula is neutralised, the company too", "'=1+1" in txt and "'@SUM(A1)" in txt and '"=1+1' not in txt and ",=1+1" not in txt, txt[:400])
        add("export: no e-mail or phone or payload leaks into the file", "example.invalid" not in txt and "raw_payload" not in txt)
        code, body, headers = store.export_csv({"status": ["past_sla"], "rep": ["Ben Rep"]})
        add("export: the same filters as the page (1 row, '_filtered' in the file name)", code == 200 and headers["X-Row-Count"] == "1" and "_filtered" in headers["Content-Disposition"] and b"Lead Three" in body)
        code, body, headers = store.export_csv({"rep": ["nobody"]})
        add("export: a bad filter is a 400 JSON error, never an unfiltered file", code == 400 and isinstance(body, dict), (code, type(body)))
        code, body, headers = store.export_csv({"from": ["2099-01-01"]})
        add("export: a filter with no rows gives a header-only file", code == 200 and headers["X-Row-Count"] == "0" and body[3:].decode().count("\r\n") == 1)

        # cache and hang-safety plumbing
        cached = ld.LeadsStore(flow=None, ttl=60)
        cached.analysis({}, fresh=True)
        n_before = len(fx.options)
        cached.analysis({})
        add("cache: an identical request inside the ttl does not touch the database again", len(fx.options) == n_before)
        add("cache: a different filter set is a different cache entry", cached._norm({"rep": ["a"]}) != cached._norm({"rep": ["b"]}) and cached._norm({"fresh": ["1"], "rep": ["a"]}) == cached._norm({"rep": ["a"]}))


def _check_real_db(add) -> None:
    """The real database, READ ONLY: the Leads number agrees with the Today number, and a hostile filter is refused."""
    from desktop import leads_data as ld
    from desktop import today_data as td
    from erp.db import engine

    store = ld.LeadsStore(flow=None, ttl=0)
    code, p = store.analysis({}, fresh=True)
    add("real db: the Leads analysis reads", code == 200 and p.get("ok"), (code, p.get("error")))
    if code != 200:
        return
    with engine.connect().execution_options(postgresql_readonly=True) as conn:
        today_raw = td.collect(conn)
        conn.rollback()
    if not td._failed(today_raw.get("leads")):
        add("real db: Leads 'past SLA now' == Today 'Leads past SLA now'", today_raw["leads"]["overdue"][-1] == p["panels"]["sla"]["past_sla_now"], (today_raw["leads"]["overdue"][-1], p["panels"]["sla"]["past_sla_now"]))
        add("real db: Leads total == Today's lead count", today_raw["leads"]["total"] == p["panels"]["sla"]["total"], (today_raw["leads"]["total"], p["panels"]["sla"]["total"]))
    code, o = store.analysis({"rep": ["x' OR '1'='1"]}, fresh=True)
    add("real db: a hostile filter is refused with a 400", code == 400, code)


def _check_failures(add) -> None:
    """Failure paths on purpose (lessons L-043 / L-083 / L-095): a query that fails, a database that refuses, a database that hangs."""
    import socket
    import threading
    import time

    from desktop import leads_data as ld
    from erp.db import make_engine

    # 1. one broken query takes down only its own panel (real SQL error on a real, synthetic session)
    with synthetic_engine():
        old = ld.SORTS["created_at"]
        ld.SORTS["created_at"] = "b.no_such_column"
        try:
            code, p = ld.LeadsStore(flow=None, ttl=0).analysis({}, fresh=True)
        finally:
            ld.SORTS["created_at"] = old
        states = {k: v["state"] for k, v in (p.get("panels") or {}).items()}
        add("failure: a SQL error in the table query makes ONLY the table 'unavailable'", code == 200 and states.get("table") == "unavailable"
            and all(v in ("ok", "empty") for k, v in states.items() if k != "table") and not p.get("ok"), states)
        add("failure: the KPI tiles and the other panels still have their numbers", [k["state"] for k in p["kpis"]].count("ok") == 5 and p["panels"]["sla"]["total"] == 9)
        add("failure: the error text is shown but cannot be a credential", "no_such_column" in p["panels"]["table"]["error"] and len(p["panels"]["table"]["error"]) <= 200)

    # 2. the database refuses the connection
    dead = make_engine("postgresql+psycopg2://127.0.0.1:9/none", connect_timeout=1)
    old_engine, ld.engine = ld.engine, dead
    try:
        t0 = time.monotonic()
        code, p = ld.LeadsStore(flow=None, ttl=0).analysis({}, fresh=True)
        add("failure: database refused -> an honest 503 with a message, not a 500 or a traceback", code == 503 and p.get("ok") is False and "database" in p.get("error", "").lower(), (code, p))
        code, body, _h = ld.LeadsStore(flow=None).export_csv({})
        add("failure: database refused -> the export is a 503 JSON error, not an empty file", code == 503 and isinstance(body, dict), (code, type(body)))
    finally:
        ld.engine = old_engine
        dead.dispose()

    # 3. the database ACCEPTS the connection and never answers (hung, not refused)
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    port = srv.getsockname()[1]
    held: list = []
    stop = threading.Event()

    def accept():
        srv.settimeout(0.3)
        while not stop.is_set():
            try:
                held.append(srv.accept()[0])
            except OSError:
                pass
    th = threading.Thread(target=accept, daemon=True)
    th.start()
    hung = make_engine(f"postgresql+psycopg2://127.0.0.1:{port}/none", connect_timeout=1)
    ld.engine = hung
    try:
        t0 = time.monotonic()
        code, p = ld.LeadsStore(flow=None, ttl=0).analysis({}, fresh=True)
        took = time.monotonic() - t0
        add("failure: a database that accepts and never answers ends within connect_timeout (+margin), not never", code == 503 and took < 10, (code, round(took, 1)))
        from erp.config import DB_CONNECT_TIMEOUT
        from erp.db import READ_KEEPALIVE_ARGS
        raw = old_engine.raw_connection()  # old_engine: the feed's REAL engine (erp.db.read_engine), saved before this test's override
        try:
            dsn = raw.driver_connection.info.dsn_parameters
        finally:
            raw.close()
        add("failure: the engine the feed really uses (erp.db.read_engine) carries connect_timeout (libpq dsn parameter)",
            str(dsn.get("connect_timeout")) == str(DB_CONNECT_TIMEOUT), dsn.get("connect_timeout"))
        add("failure: ... and the keepalive / tcp_user_timeout settings that bound an already-pooled frozen connection (L-091)",
            all(dsn.get(k) == str(v) for k, v in READ_KEEPALIVE_ARGS.items()), dsn)
    finally:
        ld.engine = old_engine
        hung.dispose()
        stop.set()
        th.join(2)
        for c in held:
            with contextlib.suppress(OSError):
                c.close()
        srv.close()

    # 4. build slots are bounded: when all are taken, a request gets 503 quickly instead of parking a server thread
    store = ld.LeadsStore(flow=None, ttl=0)
    old_wait = ld.BUILD_WAIT_SECONDS
    ld.BUILD_WAIT_SECONDS = 0.2
    taken = 0
    try:
        while store._slots.acquire(blocking=False):
            taken += 1
        t0 = time.monotonic()
        code, p = store.analysis({"rep": ["whatever"]}, fresh=True)
        add("failure: every build slot busy -> a quick 503 'busy', never a parked thread", code == 503 and time.monotonic() - t0 < 2 and "busy" in p.get("error", ""), (code, p))
    finally:
        ld.BUILD_WAIT_SECONDS = old_wait
        for _ in range(taken):
            store._slots.release()


def run(db_ok: bool = True) -> list[tuple[str, bool, object]]:
    """All scenarios. Returns [(name, passed, detail)]; never raises (a crash is one failed row)."""
    import logging
    rows: list[tuple[str, bool, object]] = []

    def add(name: str, ok, detail=None) -> None:
        rows.append((name, bool(ok), detail if not ok else ""))

    lg = logging.getLogger("erp_desk.leads")
    old_disabled, lg.disabled = lg.disabled, True
    try:
        fns = [_check_filters, _check_math, _check_csv, _check_text, _check_filterbar_js] + ([_check_sql, _check_real_db, _check_failures] if db_ok else [])
        for fn in fns:
            try:
                fn(add)
            except Exception as exc:  # noqa: BLE001
                import traceback
                add(f"{fn.__name__} crashed", False, f"{type(exc).__name__}: {exc} | {traceback.format_exc().splitlines()[-3][:160]}")
    finally:
        lg.disabled = old_disabled
        with contextlib.suppress(Exception):
            from desktop import leads_data as ld
            ld._logged.clear()
    return rows


def main() -> int:
    try:
        from sqlalchemy import text

        from erp.db import engine
        with engine.connect() as c:
            c.execute(text("SELECT 1"))
        db_ok = True
    except Exception:  # noqa: BLE001
        db_ok = False
    rows = run(db_ok)
    for name, ok, detail in rows:
        print(f"{'ok  ' if ok else 'FAIL'} {name}" + (f"   <- {detail}" if not ok else ""))
    bad = [r for r in rows if not r[1]]
    print(f"\n{len(rows) - len(bad)}/{len(rows)} scenarios passed" + ("" if db_ok else "  (database not reachable: the SQL scenarios were skipped)"))
    with contextlib.suppress(Exception):
        from erp.db import engine
        engine.dispose()
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
