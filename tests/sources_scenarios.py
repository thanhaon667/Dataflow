"""
Scenario table for the ERP Desk Leads page's "Sources & cohorts" tab (desktop/sources_data.py), kept in the repo so the
Reviewer, the next agent and the merge gate (tests/smoke.py, check 4) can re-run it (lesson L-089).

  * pure functions, no database: the cohort maths (day-0 bucketing, cumulative counts, a cohort of one lead, a cohort
    with a single age point, a reply stamped before its lead arrived, a cohort with no events at all), the low-n rules
    (when a percentage is allowed and when the chart falls back to counts), the automatic bucket choice and its stated
    reason, the source table's strict stage counts and its whitelisted sort, the filter whitelist for the JSON endpoint
    AND the CSV (parameter NAMES as well as values, lesson L-098), CSV escaping / BOM / CRLF and the absence of any
    per-lead column, the honest banner and the Definitions text.
  * SQL scenarios on SYNTHETIC data, when the database is reachable: the same private TEMPORARY-table session the Leads
    scenarios use (tests/leads_scenarios.FixtureEngine, which refuses to run unless the shadowing is proven - L-107),
    but with leads arriving in FOUR different day cohorts, so the multi-cohort behaviour this tab is built for is
    exercised even though the real database holds a single cohort. Nothing is ever written to a real table.
  * the real database, read only: the "past SLA" number here must equal the Funnel & SLA tab's and the Today page's.

Run (from the project root):
    venv\\Scripts\\python.exe -B -m tests.sources_scenarios
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

INJECTIONS = ["x' OR '1'='1", "a'; DROP TABLE leads; --", "%' UNION SELECT password FROM users --", "../../etc/passwd",
              "cohort\x00x", "$(calc)", "2026-09-16'; --"]


# =========================================================================================== helpers for the pure tests
def _row(cohort: str, age_now: float, reply=None, task=None, breach=None, undated=False) -> dict:
    return {"cohort": cohort, "age_now": age_now, "reply_age": reply, "task_age": task, "breach_age": breach,
            "task_undated": undated}


def _view(**kw):
    from desktop import sources_data as sx
    return sx.View(**kw)


def _cums(panel: dict, i: int = 0) -> list[int]:
    return [p["cum"] for p in panel["cohorts"][i]["points"]]


# =========================================================================================== pure: the cohort maths
def _check_cohorts(add) -> None:
    from desktop import sources_data as sx

    v = _view(bucket="day", metric="replied")

    # day-0 bucketing: anything that happened inside the first 24 h counts at day 0, not day 1
    rows = [_row("2026-09-01", 5, reply=0.0), _row("2026-09-01", 5, reply=0.99),
            _row("2026-09-01", 5, reply=1.0), _row("2026-09-01", 5, reply=4.7), _row("2026-09-01", 5, reply=None)]
    p = sx.build_cohorts(rows, v)
    c = p["cohorts"][0]
    add("cohort: one cohort of 5 leads is followed to the age its youngest lead reached", c["n"] == 5 and c["observed_age"] == 5, c)
    add("cohort: day-0 bucketing - 0.0 h and 0.99 d both land on day 0, 1.0 d lands on day 1",
        _cums(p) == [2, 3, 3, 3, 4, 4], _cums(p))
    add("cohort: the curve is cumulative, so it can only rise", all(b >= a for a, b in zip(_cums(p), _cums(p)[1:])))
    add("cohort: every point's denominator is the whole cohort (n never shrinks along the curve)",
        all(pt["n"] == 5 for pt in c["points"]))
    add("cohort: a lead with no event never appears in the numerator", c["final"] == 4 and c["n"] == 5)
    add("cohort: n >= LOW_N, so the figures may be percentages", c["final_rate"]["pct"] == 80.0 and p["y_mode"] == "pct",
        (c["final_rate"], p["y_mode"]))

    # a reply stamped BEFORE its lead arrived: counted from day 0, reported, never clamped away or dropped
    rows = [_row("2026-09-01", 3, reply=-0.4), _row("2026-09-01", 3, reply=-9.0), _row("2026-09-01", 3, reply=2.0)]
    p = sx.build_cohorts(rows, v)
    add("cohort: a reply time-stamped before its lead arrived counts from day 0", _cums(p) == [2, 2, 3, 3], _cums(p))
    add("cohort: ... and is reported as a data anomaly", p["anomalies"] == 2, p["anomalies"])

    # a cohort with no events at all
    p = sx.build_cohorts([_row("2026-09-01", 2), _row("2026-09-01", 2)], v)
    add("cohort: a cohort where nothing has happened yet is drawn at zero, not hidden",
        _cums(p) == [0, 0, 0] and p["cohorts"][0]["final"] == 0, p["cohorts"][0])
    add("cohort: with fewer than LOW_N leads the chart falls back to plain counts", p["y_mode"] == "count")
    add("cohort: ... and no percentage is computed for it", p["cohorts"][0]["final_rate"]["pct"] is None)

    # a cohort of ONE lead, and a cohort with only one age point
    p = sx.build_cohorts([_row("2026-09-01", 4, reply=1.0)], v)
    add("cohort: a cohort of one lead is 1 of 1, never 100%", p["cohorts"][0]["final_rate"]["pct"] is None
        and p["cohorts"][0]["final"] == 1, p["cohorts"][0]["final_rate"])
    add("cohort: a cohort of one lead still gets its whole curve", len(p["cohorts"][0]["points"]) == 5)
    p = sx.build_cohorts([_row("2026-09-23", 0.3), _row("2026-09-23", 0.1)], v)
    add("cohort: a cohort young enough for day 0 only is a SINGLE POINT (never a line through one point)",
        p["cohorts"][0]["single_point"] is True and len(p["cohorts"][0]["points"]) == 1, p["cohorts"][0])
    p = sx.build_cohorts([_row("2026-09-23", 1.2), _row("2026-09-23", 0.4)], v)
    add("cohort: a cohort is cut back to its YOUNGEST lead's age, so no point is diluted",
        p["cohorts"][0]["observed_age"] == 0 and p["cohorts"][0]["single_point"] is True, p["cohorts"][0])
    p = sx.build_cohorts([_row("2026-09-23", -0.5, reply=None)], v)
    add("cohort: a lead stamped in the future does not produce a negative age", p["cohorts"][0]["observed_age"] == 0)

    # several cohorts: order, colours-by-age, the y-mode rule, the cap on how many are drawn
    rows = ([_row("2026-09-01", 9, reply=1.0)] * 6 + [_row("2026-09-08", 3, reply=0.0)] * 5)
    p = sx.build_cohorts(rows, v)
    add("cohorts: several cohorts come back oldest first", [c["key"] for c in p["cohorts"]] == ["2026-09-01", "2026-09-08"])
    add("cohorts: percentages are allowed only when EVERY cohort has at least LOW_N leads", p["y_mode"] == "pct")
    p = sx.build_cohorts(rows + [_row("2026-09-15", 1, reply=0.0)], v)
    add("cohorts: one small cohort switches the WHOLE chart to counts", p["y_mode"] == "count",
        [(c["key"], c["n"]) for c in p["cohorts"]])
    many = []
    for i in range(1, 20):
        many.append(_row("2026-09-%02d" % i, 3, reply=0.0))
    p = sx.build_cohorts(many, v)
    add("cohorts: at most MAX_COHORT_LINES curves are drawn, the newest, and the rest are counted",
        len(p["cohorts"]) == sx.MAX_COHORT_LINES and p["more"] == 19 - sx.MAX_COHORT_LINES
        and p["cohorts"][-1]["key"] == "2026-09-19", (len(p["cohorts"]), p["more"]))
    add("cohorts: total_cohorts still reports every cohort there is", p["total_cohorts"] == 19)

    # the age cap
    p = sx.build_cohorts([_row("2026-01-01", 400, reply=1.0)], _view(bucket="day"))
    add("cohorts: a curve stops at the age cap of its bucket",
        p["cohorts"][0]["observed_age"] == sx.MAX_AGE_DAYS["day"] and p["clipped"] is True, p["cohorts"][0]["observed_age"])

    # the other two metrics read their own column, and an undated ClickUp task is reported
    rows = [_row("2026-09-01", 3, reply=None, task=0.2, breach=1.0), _row("2026-09-01", 3, task=None, breach=None, undated=True)]
    add("metric: 'clickup' counts the ClickUp column", _cums(sx.build_cohorts(rows, _view(metric="clickup"))) == [1, 1, 1, 1])
    add("metric: 'past_sla' counts the breach column", _cums(sx.build_cohorts(rows, _view(metric="past_sla"))) == [0, 1, 1, 1])
    add("metric: 'replied' counts neither", _cums(sx.build_cohorts(rows, _view(metric="replied"))) == [0, 0, 0, 0])
    add("metric: a ClickUp task with no time-stamp is reported, not silently missing",
        sx.build_cohorts(rows, _view(metric="clickup"))["undated"] == 1)
    add("metric: an empty set of leads gives no cohort and no division",
        sx.build_cohorts([], v)["cohorts"] == [] and sx.build_cohorts([], v)["y_mode"] == "count")


# =========================================================================================== pure: buckets, labels, banner
def _check_buckets(add) -> None:
    from desktop import sources_data as sx

    b, why = sx.default_bucket({"day": 1, "week": 1, "month": 1})
    add("bucket: with one single day of leads the finest bucket wins and the page says why",
        b == "day" and "single day" in why and "finest" in why, (b, why))
    b, why = sx.default_bucket({"day": 9, "week": 3, "month": 1})
    add("bucket: day is kept while it fits the chart", b == "day" and "9 cohorts" in why, (b, why))
    b, why = sx.default_bucket({"day": 90, "week": 13, "month": 4})
    add("bucket: too many days -> month (week would also be above the limit)", b == "month", (b, why))
    b, why = sx.default_bucket({"day": 40, "week": 7, "month": 2})
    add("bucket: too many days -> week", b == "week" and "7 cohorts" in why, (b, why))
    b, why = sx.default_bucket({"day": 4000, "week": 600, "month": 140})
    add("bucket: even month above the limit is chosen, and said so", b == "month" and "more than" in why, (b, why))
    b, why = sx.default_bucket({})
    add("bucket: no lead at all does not crash", b == "month" and "no lead" in why, (b, why))
    counts = {"day": 4, "week": 3, "month": 1}
    add("bucket: the reason printed on the page describes the bucket ACTUALLY in use, not the automatic one",
        sx.bucket_reason(counts, "week", False).startswith("3 cohorts at this bucket")
        and "day would give 4" in sx.bucket_reason(counts, "week", False), sx.bucket_reason(counts, "week", False))
    add("bucket: with the automatic bucket the reason is the automatic one",
        sx.bucket_reason(counts, "day", True) == sx.default_bucket(counts)[1])
    add("bucket: a hand-picked bucket above the curve limit says so",
        f"more than the {sx.MAX_COHORT_LINES}" in sx.bucket_reason({"day": 90, "week": 13, "month": 4}, "day", False))

    add("label: a day cohort is a date", sx.cohort_label("2026-09-16", "day") == "16 Sep 2026")
    add("label: a week cohort names its Monday", sx.cohort_label("2026-09-14", "week") == "week of 14 Sep 2026")
    add("label: a month cohort is a month", sx.cohort_label("2026-09-01", "month") == "Sep 2026")
    add("label: a broken key is printed, not crashed on", sx.cohort_label("not-a-date", "day") == "not-a-date")
    add("label: the end of a day / week / month cohort is right",
        (sx.cohort_end("2026-09-16", "day"), sx.cohort_end("2026-09-14", "week"), sx.cohort_end("2026-09-01", "month"),
         sx.cohort_end("2026-12-01", "month")) == ("2026-09-16", "2026-09-20", "2026-09-30", "2026-12-31"))


def _check_banner(add) -> None:
    from desktop import sources_data as sx

    def coh(ns, bucket="day"):
        return {"bucket": bucket, "cohorts": [{"key": f"2026-09-{i + 1:02d}", "label": "x", "n": n} for i, n in enumerate(ns)]}

    b = sx.build_banner(coh([11]), {"total_leads": 11}, [], 0)
    add("banner: one cohort is explained, not scolded about", b["level"] == "single" and "One cohort only" in b["title"]
        and "less than a day" in b["text"] and "scold" not in b["text"].lower(), b)
    add("banner: ... and it says what the view needs to become useful",
        any("several days" in n for n in b["needs"]) and any(str(sx.LOW_N) in n for n in b["needs"]), b["needs"])
    b = sx.build_banner(coh([11]), {"total_leads": 11}, ["webhook"], 0)
    add("banner: an offline webhook is named as the reason no new cohort can start (no second probe, the same health cells)",
        any("webhook" in n for n in b["needs"]), b["needs"])
    b = sx.build_banner(coh([2, 3, 1]), {"total_leads": 6}, [], 20)
    add("banner: every cohort under LOW_N -> counts, and it says so", b["level"] == "low_n" and "counts" in b["text"], b)
    b = sx.build_banner(coh([9, 8, 7]), {"total_leads": 24}, [], 30)
    add("banner: three healthy cohorts and nothing blind -> no banner at all", b is None, b)
    b = sx.build_banner(coh([9, 8]), {"total_leads": 17}, [], 20)
    add("banner: two cohorts is 'thin', not 'broken'", b["level"] == "thin" and "sharper" in b["text"], b)
    b = sx.build_banner(coh([]), {"total_leads": 0}, [], None)
    add("banner: no lead in view says how to get leads back", b["level"] == "empty" and "Reset all" in b["text"], b)
    add("banner: no panel at all -> no banner (never invent one)", sx.build_banner(None, None, [], 3) is None)


# =========================================================================================== pure: the source table
def _src_row(name, n, assigned=None, analyzed=None, task=None, update=None, replied_any=None, states=None, anomalies=0):
    states = states or {}
    r = {"name": name, "n": n, "s_assigned": n if assigned is None else assigned,
         "s_analyzed": n if analyzed is None else analyzed, "s_task": n if task is None else task,
         "s_update": 0 if update is None else update, "replied_any": (0 if update is None else update) if replied_any is None else replied_any,
         "anomalies": anomalies}
    for k in ("on_time", "late", "past_sla", "pending", "no_deadline"):
        r["c_" + k] = states.get(k, 0)
    return r


def _check_table(add) -> None:
    from desktop import sources_data as sx

    rows = [
        _src_row("web", 10, analyzed=9, task=8, update=6, states={"on_time": 5, "late": 2, "past_sla": 1, "pending": 2}),
        _src_row("ads", 4, analyzed=4, task=3, update=1, states={"on_time": 1, "past_sla": 3}),
        _src_row("(none)", 1, states={"pending": 1}, anomalies=1),
    ]
    reps = [{"name": "web", "rep": "Ann", "n": 7}, {"name": "web", "rep": "Ben", "n": 3}, {"name": "ads", "rep": "Ann", "n": 4}]
    replies = [{"name": "web", "hours": 1.0}, {"name": "web", "hours": 3.0}, {"name": "ads", "hours": -2.0}]
    p = sx.build_sources(rows, reps, replies, _view())
    web = p["rows"][0]
    add("table: rows are ordered by the default sort (most leads first)", [r["name"] for r in p["rows"]] == ["web", "ads", "(none)"])
    add("table: the share of the view is a percentage only above LOW_N",
        web["share"]["pct"] == 66.7 and p["rows"][2]["share"]["n"] == 1, (web["share"], p["rows"][2]["share"]))
    add("table: the stage counts are the strict funnel's, each with its own n",
        [web["stages"][k]["n"] for k in ("assigned", "analyzed", "task", "update")] == [10, 9, 8, 6])
    add("table: a stage percentage needs LOW_N too", p["rows"][1]["stages"]["task"]["pct"] is None
        and p["rows"][1]["stages"]["task"]["of"] == 4, p["rows"][1]["stages"]["task"])
    add("table: the SLA outcomes are the five shared ones and they add up to the source's leads",
        sum(web["counts"].values()) == 10 and set(web["counts"]) == set(sx.W.KEYS), web["counts"])
    add("table: 'decided' leaves out the leads still inside their deadline", web["decided"] == 8)
    add("table: the on-time rate is over the decided leads", web["on_time_rate"]["n"] == 5 and web["on_time_rate"]["of"] == 8)
    add("table: a median leaves out a reply stamped before its lead arrived",
        web["median_h"] == 2.0 and web["replied"] == 2 and p["rows"][1]["median_h"] is None, (web["median_h"], p["rows"][1]["median_h"]))
    add("table: the rep spread is biggest first and carries the n", [(x["name"], x["n"]) for x in web["reps"]] == [("Ann", 7), ("Ben", 3)])
    add("table: a source's stage anomalies are counted, not hidden", p["anomalies"] == 1 and p["rows"][2]["anomalies"] == 1)
    add("table: 'best answered' needs LOW_N decided leads behind it", p["best"]["name"] == "web", p["best"])
    add("table: a rep / reply query that failed is marked unknown, not zero",
        sx.build_sources(rows, None, None, _view())["rows"][0]["median_known"] is False
        and sx.build_sources(rows, None, None, _view())["rows"][0]["reps_known"] is False)
    add("table: an empty view divides by nothing", sx.build_sources([], [], [], _view())["total_leads"] == 0)

    # the whitelisted sort, in both directions, with "unknown" always last
    for key in sx.SORTS:
        srt = sx.sort_sources(list(p["rows"]), _view(sort=key, dir="desc"))
        add(f"sort: '{key}' desc keeps every row and puts no 'unknown' first",
            len(srt) == 3 and (key != "median" or srt[-1]["median_h"] is None), [r["name"] for r in srt])
    asc = sx.sort_sources(list(p["rows"]), _view(sort="leads", dir="asc"))
    add("sort: asc really reverses", [r["name"] for r in asc] == ["(none)", "ads", "web"], [r["name"] for r in asc])
    asc = sx.sort_sources(list(p["rows"]), _view(sort="median", dir="asc"))
    add("sort: a missing median stays at the end in ASC order too", asc[-1]["median_h"] is None and asc[0]["name"] == "web",
        [(r["name"], r["median_h"]) for r in asc])
    add("sort: an unknown key silently cannot happen - parse_view refuses it first",
        sx.parse_view({"sort": ["oops"]}, {"day": []}, {"day": 1})[1] != [])


# =========================================================================================== pure: the filter whitelist
def _check_filters(add) -> None:
    from desktop import leads_data as ld
    from desktop import sources_data as sx

    keys = {"day": ["2026-09-16", "2026-09-17"], "week": ["2026-09-14"], "month": ["2026-09-01"]}
    counts = {"day": 2, "week": 1, "month": 1}

    v, prob = sx.parse_view({}, keys, counts)
    add("view: an empty request takes the automatic bucket and the default metric",
        not prob and v.bucket == "day" and v.bucket_auto and v.metric == sx.DEFAULT_METRIC and v.cohort is None, (v, prob))
    v, prob = sx.parse_view({"bucket": ["week"], "metric": ["clickup"], "sort": ["median"], "dir": ["asc"]}, keys, counts)
    add("view: a valid request parses with no problems",
        not prob and v.bucket == "week" and not v.bucket_auto and v.metric == "clickup" and v.sort == "median" and v.dir == "asc", (v, prob))
    v, prob = sx.parse_view({"cohort": ["2026-09-17"]}, keys, counts)
    add("view: a cohort that exists for the chosen bucket is accepted", not prob and v.cohort == "2026-09-17", (v, prob))
    v, prob = sx.parse_view({"bucket": ["month"], "cohort": ["2026-09-17"]}, keys, counts)
    add("view: a cohort key of ANOTHER bucket is refused, not silently ignored",
        bool(prob) and prob[0]["param"] == "cohort" and v.cohort is None, prob)

    for bad in INJECTIONS:
        for name in ("bucket", "metric", "cohort", "sort", "dir"):
            _v, prob = sx.parse_view({name: [bad]}, keys, counts)
            add(f"whitelist: hostile {name}={bad[:22]!r} is refused with a 400 naming the parameter",
                bool(prob) and prob[0]["param"] == name and bad not in prob[0]["why"], prob)
    for name in ("bucket", "metric", "cohort", "sort", "dir", "part"):
        _v, prob = sx.parse_view({name: ["", ""]}, keys, counts)
        add(f"whitelist: {name} given twice is a problem", bool(prob) or name in ("cohort", "metric", "sort", "dir", "part"), prob)

    add("whitelist: a parameter NAME this view does not know is a 400 (L-098)",
        [p["param"] for p in sx.unknown_params({"sourc": ["web"], "source": ["web"]})] == ["sourc"])
    add("whitelist: ... and the 400 never echoes the value back",
        sx.unknown_params({"tok": ["hunter2"]})[0]["value"] == "")
    add("whitelist: 'fresh' is the one extra name every feed accepts", sx.unknown_params({"fresh": ["1"]}) == [])
    add("whitelist: the Funnel & SLA endpoint whitelists its names the same way",
        [p["param"] for p in ld.unknown_params({"statuss": ["past_sla"]})] == ["statuss"])
    add("whitelist: every name the page itself sends is accepted by the view it belongs to",
        not sx.unknown_params({k: ["x"] for k in sx.KNOWN_PARAMS}) and not ld.unknown_params({k: ["x"] for k in ld.KNOWN_PARAMS}))

    # the WHERE text is built only from fixed fragments; every value travels bound
    f, _ = ld.parse_filters({"source": ["web"]}, {"Ann"}, {"web"})
    where, params = sx.cohort_where(f, sx.View(bucket="day", cohort="2026-09-16"))
    add("sql: the cohort filter is bound, never formatted into the SQL text",
        "2026-09-16" not in where and params["cohort"] == "2026-09-16" and params["bucket"] == "day", (where, params))
    add("sql: the cohort is compared through date_trunc with the SAME bucket it was made with",
        where.count("date_trunc(:bucket") == 2, where)
    where2, params2 = sx.cohort_where(f, sx.View(bucket="day"))
    add("sql: no cohort selected means no cohort clause at all", "date_trunc" not in where2 and "cohort" not in params2)
    q = sx.view_to_query(f, sx.View(bucket="week", metric="clickup", cohort="2026-09-14"), "cohorts")
    add("export link: the query string reproduces exactly what is on screen",
        "bucket=week" in q and "metric=clickup" in q and "cohort=2026-09-14" in q and "part=cohorts" in q and "source=web" in q, q)


# =========================================================================================== pure: the CSV
def _check_csv(add) -> None:
    from desktop import sources_data as sx

    rows = [_src_row("=cmd|' /c calc'!A1", 6, update=2, states={"on_time": 2, "past_sla": 4}),
            _src_row("+web", 3, update=0, states={"pending": 3})]
    built = sx.build_sources(rows, [{"name": "=cmd|' /c calc'!A1", "rep": "@Ann", "n": 6}],
                             [{"name": "=cmd|' /c calc'!A1", "hours": 2.0}], _view())
    blob = sx.to_csv(sx.SOURCE_CSV_COLUMNS, sx.source_csv_rows(built, "Asia/Bangkok"))
    add("csv: starts with a UTF-8 byte-order mark", blob[:3] == b"\xef\xbb\xbf")
    text_ = blob[3:].decode("utf-8")
    add("csv: CRLF line ends", "\r\n" in text_ and "\n" not in text_.replace("\r\n", ""))
    import csv as _csv
    import io as _io
    table = [r for r in _csv.reader(_io.StringIO(text_, newline="")) if r]
    head = table[0]
    add("csv: the header is the source table's columns", head[0] == "source" and "leads" in head and len(head) == len(sx.SOURCE_CSV_COLUMNS))
    add("csv: no sensitive or per-lead column can appear",
        not any(w in " ".join(head).lower() for w in ("email", "phone", "payload", "token", "password", "secret", "lead_id", "lead_name")), head)
    add("csv: every row has as many cells as the header", all(len(r) == len(head) for r in table))
    for r in table[1:]:
        for cell in r:
            if cell[:1] in ("=", "+", "@", "\t", "\r", ";"):
                add("csv: a cell starts with a formula character", False, cell[:24])
                break
    add("csv: a source name that is a spreadsheet formula is quoted out", table[1][0].startswith("'="), table[1][0])
    add("csv: a rep name that is a formula is quoted out too", any(c.startswith("'@") for c in table[1]), table[1])
    add("csv: the time zone that decides the day boundaries travels with the file", table[1][-1] == "Asia/Bangkok", table[1][-1])
    add("csv: a percentage the low-n rule forbids is left empty, not guessed",
        table[2][head.index("on_time_rate_pct")] == "", table[2])

    coh = sx.build_cohorts([_row("2026-09-01", 2, reply=0.0)] * 6 + [_row("2026-09-08", 1, reply=None)], _view())
    blob = sx.to_csv(sx.COHORT_CSV_COLUMNS, sx.cohort_csv_rows(coh, "Asia/Bangkok", "2026-09-08"))
    table = [r for r in _csv.reader(_io.StringIO(blob[3:].decode("utf-8"), newline="")) if r]
    add("csv (cohorts): one row per cohort and day since arrival", len(table) == 1 + 3 + 2, len(table))
    add("csv (cohorts): the bucket time zone is a column of its own", table[0][4] == "bucket_time_zone" and table[1][4] == "Asia/Bangkok")
    add("csv (cohorts): the selected cohort is MARKED, not filtered away (nothing is silently ignored)",
        {r[-1] for r in table[1:]} == {"yes", "no"} and len({r[0] for r in table[1:]}) == 2, [r[-1] for r in table[1:]])
    add("csv (cohorts): with no cohort selected the column is simply empty",
        {r[-1] for r in _csv.reader(_io.StringIO(sx.to_csv(sx.COHORT_CSV_COLUMNS, sx.cohort_csv_rows(coh, "UTC")).decode("utf-8-sig"), newline="")) if r} == {"", "selected_on_screen"})
    add("csv: an empty set of rows still produces a header", len(sx.to_csv(sx.SOURCE_CSV_COLUMNS, [])) > 3)


# =========================================================================================== pure: the words
def _check_text(add) -> None:
    from desktop import sources_data as sx
    from desktop import today_data as td

    defs = sx.build_definitions(_view(bucket="day"), "Asia/Bangkok")
    joined = " ".join(d["text"] for d in defs)
    add("definitions: every entry has a term and a text", defs and all(d.get("term") and d.get("text") for d in defs))
    add("definitions: the day-boundary time zone is named (L-097)", "Asia/Bangkok" in joined)
    add("definitions: the SLA text comes from the live configuration (L-085)", f"{td._sla_hours():g} business hours" in joined)
    add("definitions: the low-n threshold is the constant, not a literal", f"{sx.LOW_N} leads" in joined or f"from {sx.LOW_N} " in joined)
    add("definitions: the age cap is the constant of the bucket in use", f"day {sx.MAX_AGE_DAYS['day']}" in joined, sx.MAX_AGE_DAYS)
    add("definitions: the 'past SLA' entry points at the shared wording module", sx.W.TILE_PAST_SLA in joined)
    add("definitions: every metric is explained", all(sx.METRIC_MEANING[k] in joined for k in sx.METRIC_KEYS))
    add("definitions: it says which control changes what, so no control is quietly doing nothing",
        any(d["id"] == "controls" for d in defs) and "does not depend on it" in joined)
    add("definitions: it states that the files hold no per-lead row", sx.CSV_EXCLUDED in joined)
    defs2 = sx.build_definitions(_view(bucket="month"), None)
    add("definitions: with no time zone read it does not invent one",
        "the database session" in " ".join(d["text"] for d in defs2) and f"day {sx.MAX_AGE_DAYS['month']}" in " ".join(d["text"] for d in defs2))

    tiles = sx.build_kpis(sx.build_sources([_src_row("web", 6, update=2, states={"on_time": 2, "past_sla": 4})], [], [], _view()),
                          sx.build_cohorts([_row("2026-09-01", 2, reply=0.0)] * 6, _view()), 6, False, {})
    add("tiles: five tiles, each with a label and a hint", len(tiles) == 5 and all(t["label"] and t["hint"] for t in tiles))
    named = sx.build_kpis(sx.build_sources([_src_row("website_contact_form", 6, states={"past_sla": 6})], [], [], _view()),
                          None, 6, False, {"cohorts": "x"})
    add("tiles: the biggest source reads like the filter pills and the table (underscores are spaces)",
        named[1]["text"] == "website contact form", named[1]["text"])
    add("tiles: the 'past SLA' tile takes its label and hint from the shared module (the three pages agree)",
        tiles[-1]["label"] == sx.W.TILE_PAST_SLA and tiles[-1]["value"] == 4, tiles[-1])
    add("tiles: a failed panel says unavailable instead of showing a zero",
        all(t["state"] == "unavailable" for t in sx.build_kpis(None, None, 6, False, {"sources": "boom"})))
    add("tiles: an empty view does not divide by zero",
        sx.build_kpis(sx.build_sources([], [], [], _view()), sx.build_cohorts([], _view()), 0, False, {})[1]["text"] == "-")


# =========================================================================================== SQL on synthetic cohorts
def _fixture_sql() -> list[str]:
    """Ten leads in FOUR day cohorts (10, 7, 4 and 0 days ago), so the multi-cohort behaviour is exercised even though
    the real database holds a single cohort. Every timestamp is anchored to date_trunc('day', now()) so a lead can never
    drift into the neighbouring calendar day while the tests run (lesson L-097)."""
    def day(n: int) -> str:
        return f"(date_trunc('day', now()) - interval '{n} days')"

    def plus(n: int, hours) -> str:
        return "NULL" if hours is None else f"({day(n)} + interval '{hours} hours')"

    users = [(8001, "Ann Rep"), (8002, "Ben Rep")]
    #        id    days  due_h  rep   source     ai     sync      sync_h  reply_h  name
    leads = [
        (8101, 10, 5, 8001, "web", True, "synced", 0.1, 24, "Old Late"),          # replied on day 1, after the deadline
        (8102, 10, 5, 8002, "ads", True, "synced", 0.1, 2, "Old On Time"),        # replied on day 0, inside it
        (8103, 10, 5, 8001, "web", True, "synced", 0.1, None, "Old Silent"),      # past SLA
        (8104, 10, 5, 8002, "referral", True, "pending", None, None, "Old No Task"),   # past SLA, never reached ClickUp
        (8105, 10, 5, 8001, "ads", True, "synced", 0.1, 11, "Old Late Two"),      # replied on day 0, after the deadline
        (8106, 7, 5, 8001, "web", True, "synced", 0.1, -3, "Reply Before"),       # reply stamped BEFORE arrival
        (8107, 7, 5, 8002, "web", True, "mocked", 0.1, None, "Mid Silent"),
        (8108, 4, 5, 8001, "referral", True, "synced", 0.1, None, "Quiet One"),   # a cohort where nothing happened
        (8109, 4, 5, 8002, "referral", True, "synced", 0.1, None, "Quiet Two"),
        (8110, 0, None, 8001, "ads", False, None, None, None, "=Fresh Today"),    # today: one lead, one age point, formula name
    ]
    sql = [f"CREATE TEMP TABLE {t} (LIKE public.{t} INCLUDING DEFAULTS)"
           for t in ("users", "leads", "lead_assignments", "lead_ai_analysis", "lead_clickup_sync", "lead_updates")]
    for uid, name in users:
        sql.append(f"INSERT INTO users (id, full_name, email, role, is_active, created_at) "
                   f"VALUES ({uid}, '{name}', 'x{uid}@example.invalid', 'sales', true, now())")
    for lid, d, due_h, rep, src, ai, sync, sync_h, reply_h, name in leads:
        due = "(now() + interval '48 hours')" if due_h is None else plus(d, due_h)
        sql.append(f"INSERT INTO leads (id, full_name, email, company, source, created_at, sla_due_at) "
                   f"VALUES ({lid}, '{name}', 'l{lid}@example.invalid', 'Synthetic Co', '{src}', {plus(d, 0)}, {due})")
        sql.append(f"INSERT INTO lead_assignments (id, lead_id, sales_rep_id, assignment_reason, is_current, assigned_at) "
                   f"VALUES ({8500 + lid}, {lid}, {rep}, 'round_robin_new', true, {plus(d, 0)})")
        if ai:
            sql.append(f"INSERT INTO lead_ai_analysis (id, lead_id, model_name, potential_score, analyzed_at) "
                       f"VALUES ({8500 + lid}, {lid}, 'synthetic', 50, {plus(d, 0)})")
        if sync:
            sql.append(f"INSERT INTO lead_clickup_sync (id, lead_id, sync_status, last_synced_at) "
                       f"VALUES ({8500 + lid}, {lid}, '{sync}', {plus(d, sync_h)})")
        if reply_h is not None:
            sql.append(f"INSERT INTO lead_updates (id, lead_id, content, occurred_at) "
                       f"VALUES ({8500 + lid}, {lid}, 'synthetic reply', {plus(d, reply_h)})")
    return sql


@contextlib.contextmanager
def synthetic_engine():
    """The Leads scenarios' private-temp-table machinery, with this file's four-cohort data, patched into the feed."""
    from desktop import sources_data as sx
    from tests import leads_scenarios as ls
    with ls.synthetic_engine(module=sx, fixture_sql=_fixture_sql) as fx:
        yield fx


def _check_sql(add) -> None:
    from desktop import sources_data as sx

    with synthetic_engine() as fx:
        store = sx.SourcesStore(flow=None, ttl=0)
        code, p = store.sources({}, fresh=True)
        add("sql: the synthetic four-cohort set answers 200 and every panel reads", code == 200 and p.get("ok"),
            (code, p.get("error"), {k: v.get("error") for k, v in (p.get("panels") or {}).items() if v.get("state") not in ("ok", "empty")}))
        if code != 200:
            return
        add("sql: the feed asked for a READ ONLY connection", any(o.get("postgresql_readonly") for o in fx.options), fx.options)
        S, C = p["panels"]["sources"], p["panels"]["cohorts"]
        add("sql: the ten synthetic leads are all in view", S["total_leads"] == 10, S["total_leads"])
        by = {r["name"]: r for r in S["rows"]}
        add("sql: the three sources are counted right", {k: v["n"] for k, v in by.items()} == {"web": 4, "ads": 3, "referral": 3},
            {k: v["n"] for k, v in by.items()})
        add("sql: the strict stage counts per source are right (ads: 3 assigned, 2 analysed, 2 ClickUp, 2 replied)",
            [by["ads"]["stages"][k]["n"] for k in ("assigned", "analyzed", "task", "update")] == [3, 2, 2, 2],
            [by["ads"]["stages"][k]["n"] for k in ("assigned", "analyzed", "task", "update")])
        add("sql: referral never reached a reply", [by["referral"]["stages"][k]["n"] for k in ("assigned", "analyzed", "task", "update")] == [3, 3, 2, 0],
            [by["referral"]["stages"][k]["n"] for k in ("assigned", "analyzed", "task", "update")])
        add("sql: the SLA outcomes across every source are 2 on time, 2 late, 5 past SLA, 1 waiting",
            {k: sum(r["counts"][k] for r in S["rows"]) for k in sx.W.KEYS} ==
            {"on_time": 2, "late": 2, "past_sla": 5, "pending": 1, "no_deadline": 0},
            {k: sum(r["counts"][k] for r in S["rows"]) for k in sx.W.KEYS})
        add("sql: the median first reply per source leaves out the reply stamped before arrival",
            round(by["web"]["median_h"], 2) == 24.0 and by["web"]["replied"] == 1 and round(by["ads"]["median_h"], 2) == 6.5,
            (by["web"]["median_h"], by["web"]["replied"], by["ads"]["median_h"]))
        add("sql: the rep spread comes from the current assignment", {x["name"] for x in by["web"]["reps"]} == {"Ann Rep", "Ben Rep"},
            by["web"]["reps"])
        add("sql: the bucket the server picked is 'day', and it says why", p["view"]["bucket"] == "day" and p["view"]["bucket_auto"]
            and "4 cohorts" in (p["view"]["bucket_why"] or ""), p["view"])
        add("sql: four arrival cohorts", len(C["cohorts"]) == 4, [(c["key"], c["n"]) for c in C["cohorts"]])
        sizes = [c["n"] for c in C["cohorts"]]
        add("sql: the cohorts hold 5, 2, 2 and 1 leads, oldest first", sizes == [5, 2, 2, 1], sizes)
        add("sql: each cohort is followed exactly to its own age (10, 7, 4 and 0 days)",
            [c["observed_age"] for c in C["cohorts"]] == [10, 7, 4, 0], [c["observed_age"] for c in C["cohorts"]])
        add("sql: today's cohort has a single age point, so it is drawn as points",
            C["cohorts"][-1]["single_point"] is True and len(C["cohorts"][-1]["points"]) == 1)
        add("sql: the oldest cohort's replied curve is 2 on day 0, then 3 from day 1",
            [pt["cum"] for pt in C["cohorts"][0]["points"]] == [2, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3],
            [pt["cum"] for pt in C["cohorts"][0]["points"]])
        add("sql: the reply stamped before its lead arrived counts on day 0 of its cohort",
            [pt["cum"] for pt in C["cohorts"][1]["points"]] == [1] * 8, [pt["cum"] for pt in C["cohorts"][1]["points"]])
        add("sql: ... and is reported as an anomaly", C["anomalies"] == 1, C["anomalies"])
        add("sql: the cohort where nothing happened is drawn at zero", [pt["cum"] for pt in C["cohorts"][2]["points"]] == [0] * 5)
        add("sql: two cohorts of 2 and one of 1 force the whole chart to counts", C["y_mode"] == "count", C["y_mode"])
        add("sql: the single-cohort banner is NOT shown when there are four", (p["banner"] or {}).get("level") != "single",
            (p["banner"] or {}).get("level"))

        # the ClickUp metric reads its own column on the same data
        code, p2 = store.sources({"metric": ["clickup"]}, fresh=True)
        C2 = p2["panels"]["cohorts"]
        add("sql: the ClickUp curve of the oldest cohort is 4 of 5 from day 0 (one lead never reached ClickUp)",
            [pt["cum"] for pt in C2["cohorts"][0]["points"]][:2] == [4, 4], [pt["cum"] for pt in C2["cohorts"][0]["points"]][:3])
        add("sql: nothing is undated in this fixture", C2["undated"] == 0)

        # the past-SLA metric, and the cohort filter
        code, p3 = store.sources({"metric": ["past_sla"], "cohort": [C["cohorts"][2]["key"]]}, fresh=True)
        S3, C3 = p3["panels"]["sources"], p3["panels"]["cohorts"]
        add("sql: the cohort filter narrows the TABLE to that cohort's leads", S3["total_leads"] == 2 and
            {r["name"] for r in S3["rows"]} == {"referral"}, (S3["total_leads"], [r["name"] for r in S3["rows"]]))
        add("sql: ... and the CHART deliberately keeps every cohort, so there is something to compare against",
            len(C3["cohorts"]) == 4, len(C3["cohorts"]))
        add("sql: the past-SLA curve of the quiet cohort is 2 of 2 from day 0",
            [pt["cum"] for pt in C3["cohorts"][2]["points"]] == [2] * 5, [pt["cum"] for pt in C3["cohorts"][2]["points"]])
        add("sql: the selected cohort is reported back as an active filter", p3["filters"]["cohort"] == C["cohorts"][2]["key"]
            and p3["filters"]["active"] == 1, p3["filters"])

        # the CSV of the same view
        code, body, headers = store.export_csv({"part": ["sources"], "cohort": [C["cohorts"][0]["key"]]})
        add("sql: the source CSV of a cohort answers 200 with only that cohort's three sources", code == 200
            and body.decode("utf-8-sig").count("\r\n") == 1 + 3, (code, body[:140] if isinstance(body, bytes) else body))
        code, whole, _h = store.export_csv({"part": ["sources"]})
        add("sql: no lead name reaches either file - they hold aggregate rows only",
            b"Fresh Today" not in whole and b"Old Late" not in whole and b"example.invalid" not in whole)
        code, body, headers = store.export_csv({"part": ["cohorts"]})
        add("sql: the cohort CSV holds one row per cohort and day (11 + 8 + 5 + 1)",
            code == 200 and body.decode("utf-8-sig").count("\r\n") == 1 + 11 + 8 + 5 + 1,
            body.decode("utf-8-sig").count("\r\n") if code == 200 else code)
        add("sql: the export says how many rows it wrote", headers.get("X-Row-Count") == "25", headers)

        # the whitelist, end to end, on the endpoint and on the export
        for params in ({"cohort": ["1999-01-01"]}, {"bucket": ["hour"]}, {"metric": ["money"]}, {"sort": ["; DROP"]},
                       {"source": ["nope"]}, {"nope": ["1"]}):
            code, _p = store.sources(params, fresh=True)
            add(f"sql: {list(params)[0]}={list(params.values())[0][0][:18]!r} is refused with a 400", code == 400, code)
            code, body, _h = store.export_csv({**params, "part": ["sources"]})
            add("sql: ... and the CSV refuses it too (an export must never silently widen)", code == 400, code)


def _check_real_db(add) -> None:
    """The real database, READ ONLY: this tab, the Funnel & SLA tab and the Today page must agree."""
    from desktop import leads_data as ld
    from desktop import sources_data as sx
    from desktop import today_data as td
    from erp.db import engine

    store = sx.SourcesStore(flow=None, ttl=0)
    code, p = store.sources({}, fresh=True)
    add("real db: the sources feed reads", code == 200 and p.get("ok"), (code, p.get("error")))
    if code != 200:
        return
    S = p["panels"]["sources"]
    past = sum(r["past_sla"] for r in S["rows"])
    code2, lp = ld.LeadsStore(flow=None, ttl=0).analysis({}, fresh=True)
    if code2 == 200 and lp["panels"]["sla"]["state"] == "ok":
        add("real db: 'past SLA now' here == the Funnel & SLA tab's", past == lp["panels"]["sla"]["past_sla_now"],
            (past, lp["panels"]["sla"]["past_sla_now"]))
        add("real db: the leads in view are the same on both tabs", S["total_leads"] == lp["panels"]["sla"]["total"],
            (S["total_leads"], lp["panels"]["sla"]["total"]))
        add("real db: the per-source lead counts match the Funnel & SLA tab's 'by source' panel",
            {r["name"]: r["n"] for r in S["rows"]} == {r["name"]: r["n"] for r in lp["panels"]["by_source"]["rows"]},
            ({r["name"]: r["n"] for r in S["rows"]}, {r["name"]: r["n"] for r in lp["panels"]["by_source"]["rows"]}))
    with engine.connect().execution_options(postgresql_readonly=True) as conn:
        today_raw = td.collect(conn)
        conn.rollback()
    if not td._failed(today_raw.get("leads")):
        add("real db: 'past SLA now' here == the Today page's tile", past == today_raw["leads"]["overdue"][-1],
            (past, today_raw["leads"]["overdue"][-1]))
        add("real db: the lead count here == the Today page's", S["total_leads"] == today_raw["leads"]["total"],
            (S["total_leads"], today_raw["leads"]["total"]))
    add("real db: the day-boundary time zone is read from the session, not assumed", bool(p.get("time_zone")), p.get("time_zone"))
    if p["options"].get("first_day") and p["options"].get("last_day"):
        span = (date.fromisoformat(p["options"]["last_day"]) - date.fromisoformat(p["options"]["first_day"])).days
        add("real db: the automatic bucket really is the finest one the span carries",
            p["view"]["bucket"] in sx.BUCKET_KEYS and (span > 0 or p["view"]["bucket"] == "day"), (span, p["view"]["bucket"]))
    code, _o = store.sources({"rep": ["x' OR '1'='1"]}, fresh=True)
    add("real db: a hostile filter is refused with a 400", code == 400, code)
    code, _o = store.sources({"statuss": ["past_sla"]}, fresh=True)
    add("real db: an unknown parameter NAME is refused even though the cache key ignores it (L-098)", code == 400, code)


def _check_failures(add) -> None:
    """Failure paths on purpose (L-043 / L-083): one broken query, a database that refuses, a busy service."""
    from desktop import sources_data as sx

    with synthetic_engine():
        store = sx.SourcesStore(flow=None, ttl=0)
        old = sx.REPS_SQL
        sx.REPS_SQL = "SELECT b.no_such_column AS name FROM b WHERE __W__"
        try:
            code, p = store.sources({}, fresh=True)
        finally:
            sx.REPS_SQL = old
        add("failure: one broken query leaves the table alive and only marks the rep spread unknown",
            code == 200 and p["panels"]["sources"]["state"] == "ok" and p["panels"]["sources"]["rows"][0]["reps_known"] is False,
            (code, p.get("panels", {}).get("sources", {}).get("state")))
        old = sx.COHORT_SQL
        sx.COHORT_SQL = "SELECT b.no_such_column FROM b WHERE __W__"
        try:
            code, p = store.sources({}, fresh=True)
        finally:
            sx.COHORT_SQL = old
        add("failure: a broken cohort query takes down only the cohort panel, with a reason",
            code == 200 and p["panels"]["cohorts"]["state"] == "unavailable" and p["panels"]["cohorts"].get("error")
            and p["panels"]["sources"]["state"] == "ok" and p["ok"] is False,
            (code, p["panels"]["cohorts"].get("state")))
        add("failure: ... and the cohort tile says unavailable instead of showing a zero",
            next(t for t in p["kpis"] if t["id"] == "cohorts")["state"] == "unavailable")

    # a database that is simply not there (L-108: no credentials in the URL)
    from sqlalchemy import create_engine
    dead = create_engine("postgresql+psycopg2://127.0.0.1:9/none", connect_args={"connect_timeout": 1})
    old_engine = sx.engine
    sx.engine = dead
    try:
        code, p = sx.SourcesStore(flow=None, ttl=0).sources({}, fresh=True)
    finally:
        sx.engine = old_engine
        dead.dispose()
    add("failure: a database that cannot be reached is a 503 with a sentence, never a traceback",
        code == 503 and p.get("unavailable") and "reach" in p.get("error", ""), (code, p.get("error", "")[:90]))

    store = sx.SourcesStore(flow=None, ttl=0)
    old_wait, sx.BUILD_WAIT_SECONDS = sx.BUILD_WAIT_SECONDS, 0.05
    taken = 0
    try:
        while store._slots.acquire(blocking=False):
            taken += 1
        code, p = store.sources({}, fresh=True)
        add("failure: every build slot busy gives a quick 503, never a parked thread", code == 503 and "busy" in p["error"], code)
    finally:
        sx.BUILD_WAIT_SECONDS = old_wait
        for _ in range(taken):
            store._slots.release()


# =========================================================================================== runner
def run(db_ok: bool = True) -> list[tuple[str, bool, object]]:
    """All scenarios. Returns [(name, passed, detail)]; never raises (a crash is one failed row)."""
    import logging
    rows: list[tuple[str, bool, object]] = []

    def add(name: str, ok, detail=None) -> None:
        rows.append((name, bool(ok), detail if not ok else ""))

    lg = logging.getLogger("erp_desk.sources")
    old_disabled, lg.disabled = lg.disabled, True
    lg2 = logging.getLogger("erp_desk.leads")
    old_disabled2, lg2.disabled = lg2.disabled, True
    try:
        fns = ([_check_cohorts, _check_buckets, _check_banner, _check_table, _check_filters, _check_csv, _check_text]
               + ([_check_sql, _check_real_db, _check_failures] if db_ok else []))
        for fn in fns:
            try:
                fn(add)
            except Exception as exc:  # noqa: BLE001
                import traceback
                add(f"{fn.__name__} crashed", False, f"{type(exc).__name__}: {exc} | {traceback.format_exc().splitlines()[-3][:160]}")
    finally:
        lg.disabled, lg2.disabled = old_disabled, old_disabled2
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
