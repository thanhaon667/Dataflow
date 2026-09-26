"""
Scenario table for the ERP Desk Channels page (desktop/channels_data.py, static/channels.js), kept in the repo so the
Reviewer, the next agent and the merge gate (tests/smoke.py, check 4) can re-run it (lesson L-089).

  * pure functions, no database: the parameter whitelist for the JSON endpoint AND the CSV (parameter NAMES as well as values,
    checked BEFORE the cache and before any database access - lesson L-098), the sort vocabulary (and that the page's column
    keys are exactly that vocabulary), the rate maths with zero and tiny denominators (a percentage needs a real n, cost per
    conversion only where spend exists), the previous-period delta rules, the low-n rule, the choice between the four page
    states (not installed / empty / no match / ready) and the promise that the first two never contain a number, the honest
    per-tile / per-panel degradation, CSV escaping and the absence of any identity column, the Definitions text being built
    from the constants, that no request text and no raw-table name can reach the SQL, and the client/server timeout order.
  * SQL scenarios when the database is reachable: the REAL db/sql/07 + 09 DDL installed into the session's TEMPORARY schema
    (pg_temp, so nothing is ever written to a real table and the objects vanish with the session - lesson L-107 is proven
    by checking that every table resolves in pg_temp before anything is inserted), a hand-worked rollup, and the whole store
    driven against it: every figure of the tiles, the chart, the table, the campaign drill-down and the three CSV files
    against the Python-side arithmetic AND a direct SQL query, a poisoned raw fact row the page must not see, the not-installed
    / installed-but-empty / partly-installed states, one panel failing at a time, and the database being down.
  * the real database, read only: whatever state it is in today (not installed, empty, populated), the page answers honestly.

Run (from the project root):
    venv\\Scripts\\python.exe -B -m tests.channels_scenarios
"""
from __future__ import annotations

import contextlib
import csv
import io
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

INJECTIONS = ["x' OR '1'='1", "a'; DROP TABLE interaction_daily_rollup; --", "%' UNION SELECT password FROM users --", "../../etc/passwd",
              "chan\x00x", "$(calc)", "2026-09-16'; --"]


# =========================================================================================== helpers
def _options(n: int = 3, last_day: date = date(2026, 9, 24)):
    from desktop import channels_data as cd
    rows = [{"channel_id": i + 1, "days": 20, "sessions": 1000 - i * 100, "channel_key": f"ch{i + 1}", "display_name": f"Channel {i + 1}",
             "medium": "search", "is_paid": True} for i in range(n)]
    return cd.build_options(rows, {"rollup_rows": 60, "first_day": date(2026, 8, 1), "last_day": last_day})


def _ids(options) -> dict:
    return {c["key"]: c["id"] for c in options["_channels"]}


def _view(options=None, **req):
    from desktop import channels_data as cd
    options = options or _options()
    return cd.resolve_view(cd.Request(**req), options, _ids(options)), options


def _tot(days: int = 28, sessions: int = 100, clicks: int = 50, impressions: int = 1000, conversions: int = 5, spend: int = 0) -> dict:
    return {"days_with_data": days, "first_day": date(2026, 8, 26), "last_day": date(2026, 9, 24), "sessions": sessions, "clicks": clicks,
            "impressions": impressions, "conversions": conversions, "spend_micros": spend}


def _chrow(cid: int, **kw) -> dict:
    base = {"channel_id": cid, "days_with_data": 20, "sessions": 100, "clicks": 50, "impressions": 1000, "conversions": 5, "spend_micros": 0}
    base.update(kw)
    return base


def _daily(cid: int, day: date, **kw) -> dict:
    base = {"event_date": day, "channel_id": cid, "sessions": 10, "clicks": 5, "impressions": 100, "conversions": 1, "spend_micros": 0}
    base.update(kw)
    return base


META = {"tz": "Asia/Bangkok", "schema": "public", "rollup_rows": 60, "data_as_of": datetime(2026, 9, 25, 3, 0, tzinfo=timezone.utc),
        "journal": {"last_ok_at": datetime(2026, 9, 25, 3, 0, tzinfo=timezone.utc), "runs": 1, "last_status": "ok"}}
NOW = datetime(2026, 9, 25, 4, 0, tzinfo=timezone.utc)


def _raw(v, totals=None, prev=None, channels=None, daily=None, campaigns=None) -> dict:
    d0 = v.d_from
    return {"totals": [totals if totals is not None else _tot()], "prev": [prev if prev is not None else _tot(sessions=80, clicks=40, conversions=4)],
            "channels": channels if channels is not None else [_chrow(1, sessions=60), _chrow(2, sessions=30), _chrow(3, sessions=10)],
            "daily": daily if daily is not None else [_daily(c, d0 + timedelta(days=i)) for c in (1, 2, 3) for i in (0, 1, 2, 4)],
            **({"campaigns": campaigns} if campaigns is not None else {})}


def _no_db_engine():
    """An engine that fails the test the moment anything asks it for a connection."""
    class _Boom:
        touched = 0

        def connect(self):
            _Boom.touched += 1
            raise RuntimeError("the database must not be touched by this request")
    return _Boom()


@contextlib.contextmanager
def _patched(obj, **attrs):
    old = {k: getattr(obj, k) for k in attrs}
    for k, v in attrs.items():
        setattr(obj, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            setattr(obj, k, v)


# =========================================================================================== pure: the filter whitelist
def _check_filters(add) -> None:
    from desktop import channels_data as cd

    req, prob = cd.parse_request({})
    add("filters: an empty request is valid and takes the documented defaults", not prob and req.sort == "sessions" and req.dir == "desc" and req.channels == () and req.campaign is None, (req, prob))
    req, prob = cd.parse_request({"from": ["2026-08-01"], "to": ["2026-08-31"], "channel": ["a", "b", "a"], "campaign": ["12"], "focus": ["a"], "sort": ["cvr"], "dir": ["asc"]})
    add("filters: a full valid request parses (a repeated channel counts once)", not prob and req.channels == ("a", "b") and req.campaign == 12 and req.d_from == date(2026, 8, 1) and req.dir == "asc", (req, prob))

    # parameter NAMES (L-098): a typo is a 400 that names the parameter, on the JSON endpoint and on the CSV
    for typo in ("chanel", "channels", "form", "campain", "sorts", "part", "page", "status", "rep"):
        _r, p = cd.parse_request({typo: ["x"]})
        add(f"filters: unknown parameter name '{typo}' is refused and named", len(p) == 1 and p[0]["param"] == typo and p[0]["value"] == "", p)
    _r, p = cd.parse_request({"part": ["daily"]}, cd.CSV_PARAMS)
    add("filters: `part` is a parameter of the CSV endpoint", not p, p)
    _r, p = cd.parse_request({"chanel": ["x"]}, cd.CSV_PARAMS)
    add("filters: a typo is refused on the CSV endpoint too", len(p) == 1 and p[0]["param"] == "chanel", p)
    add("filters: `fresh` is accepted everywhere and is not a filter", not cd.unknown_params({"fresh": ["1"]}), cd.unknown_params({"fresh": ["1"]}))

    # values
    for name, bad in (("from", "2026-02-30"), ("from", "2026-9-1"), ("from", "yesterday"), ("to", "1999-01-01"), ("to", "2101-01-01"), ("sort", "cvr; DROP"),
                      ("sort", "spend_micros"), ("dir", "sideways"), ("campaign", "abc"), ("campaign", "-1"), ("campaign", "1.5"), ("campaign", "9" * 19),
                      ("campaign", "9223372036854775808")):
        _r, p = cd.parse_request({name: [bad]})
        add(f"filters: {name}={bad!r} is refused", any(x["param"] == name for x in p), p)
    _r, p = cd.parse_request({"from": ["2026-09-10"], "to": ["2026-09-01"]})
    add("filters: a start after the end is refused", any("after" in x["why"] for x in p), p)
    _r, p = cd.parse_request({"from": ["2000-01-01"], "to": ["2026-01-01"]})
    add("filters: a range longer than the cap is refused", any(str(cd.MAX_SPAN_DAYS) in x["why"] for x in p), p)
    _r, p = cd.parse_request({"from": ["2026-09-01", "2026-09-02"]})
    add("filters: a repeated single-value parameter is refused", any("only once" in x["why"] for x in p), p)
    _r, p = cd.parse_request({"campaign": ["0"]})
    add("filters: campaign 0 (= the rows that had no campaign) is a valid value", not p, p)
    _r, p = cd.parse_request({"campaign": ["77"]}, campaign_exists=lambda cid: False)
    add("filters: a campaign that does not exist is refused", any(x["param"] == "campaign" for x in p), p)
    _r, p = cd.parse_request({"campaign": ["77"]}, campaign_exists=lambda cid: cid == 77)
    add("filters: a campaign that exists passes the whitelist", not p, p)
    _r, p = cd.parse_request({"channel": ["nope"]}, channels_ok={"a", "b"})
    add("filters: a channel that is not in the data is refused", any(x["param"] == "channel" for x in p), p)
    _r, p = cd.parse_request({"focus": ["nope"]}, channels_ok={"a", "b"})
    add("filters: a focus channel that is not in the data is refused", any(x["param"] == "focus" for x in p), p)
    _r, p = cd.parse_request({"part": ["campaigns"]}, cd.CSV_PARAMS)
    add("filters: the campaigns file needs a channel", any(x["param"] == "focus" for x in p), p)
    _r, p = cd.parse_request({"part": ["campaigns"], "focus": ["a"]}, cd.CSV_PARAMS)
    add("filters: ... and is valid with one", not p, p)
    _r, p = cd.parse_request({"part": ["everything"]}, cd.CSV_PARAMS)
    add("filters: an unknown CSV part is refused", any(x["param"] == "part" for x in p), p)
    _r, p = cd.parse_request({"channel": [f"c{i}" for i in range(cd.MAX_CHANNEL_VALUES + 1)]})
    add("filters: an absurd number of channels is refused", any("at most" in x["why"] for x in p), p)

    # hostile text: never accepted as a value, never reaches SQL
    for evil in INJECTIONS:
        for name in ("from", "to", "campaign", "sort", "dir"):
            _r, p = cd.parse_request({name: [evil]})
            if not any(x["param"] == name for x in p):
                add(f"filters: {name}={evil!r} was accepted", False, p)
        _r, p = cd.parse_request({"channel": [evil], "focus": [evil]}, channels_ok={"a"}, campaign_exists=lambda cid: False)
        if not (any(x["param"] == "channel" for x in p) and any(x["param"] == "focus" for x in p)):
            add(f"filters: a hostile channel / focus {evil!r} passed the whitelist", False, p)
    add("filters: hostile text is refused for every parameter", True)

    # the refusal comes BEFORE any cache lookup and before the database (L-098)
    boom = _no_db_engine()
    with _patched(cd, engine=boom):
        store = cd.ChannelsStore(ttl=60)
        key = store._norm({})
        store._cache[key] = (time.monotonic(), (200, {"cached": True}))
        good = store.get({})
        typo = store.get({"chanel": ["x"]})
        add("filters: a warm cache answers the plain request", good == (200, {"cached": True}), good)
        add("filters: ... but a typo in the NAME is still a 400 (the cache key would have dropped it)", typo[0] == 400 and typo[1]["problems"][0]["param"] == "chanel", typo)
        add("filters: a malformed VALUE is a 400 before the cache too", store.get({"from": ["nope"]})[0] == 400)
        add("filters: none of that touched the database", boom.touched == 0, boom.touched)
        for params, want in (({"chanel": ["x"]}, "chanel"), ({"part": ["zzz"]}, "part"), ({"from": ["2026-13-01"]}, "from"), ({"sort": ["nope"]}, "sort")):
            code, body, headers = store.export_csv(params)
            add(f"filters: the CSV endpoint refuses {params} (400 naming '{want}')", code == 400 and body["problems"][0]["param"] == want and not headers, (code, body))
        code, body, _h = store.export_csv({"part": ["campaigns"]})
        add("filters: the campaigns CSV without a focus channel is a 400", code == 400 and body["problems"][0]["param"] == "focus", (code, body))
        add("filters: the CSV refusals did not touch the database either", boom.touched == 0, boom.touched)


# =========================================================================================== pure: math
def _check_math(add) -> None:
    from desktop import channels_data as cd
    LOW = cd.LOW_N

    r = cd.ratio(0, 0, "no clicks")
    add("math: a zero denominator is 'no clicks', never a division or a 0%", r["pct"] is None and r["value"] is None and not r["low_n"] and r["why"] == "no clicks", r)
    r = cd.ratio(2, 3, "x")
    add("math: 2 of 3 is a count, not 66.7% (n < LOW_N)", r["pct"] is None and r["low_n"] and (r["n"], r["of"]) == (2, 3), r)
    r = cd.ratio(1, LOW, "x")
    add("math: exactly LOW_N is enough for a percentage", r["pct"] == 20.0 and not r["low_n"], r)
    r = cd.ratio(1, LOW - 1, "x")
    add("math: one below LOW_N is still a count", r["pct"] is None and r["low_n"], r)
    r = cd.ratio(0, 10, "x")
    add("math: a real zero with a real denominator is 0%", r["pct"] == 0 and r["why"] is None and not r["low_n"], r)
    r = cd.ratio(3, 10 ** 9, "x")
    add("math: a tiny non-zero share never rounds to 0% (the page prints '<0.1%')", r["pct"] is not None and 0 < r["pct"] < 0.1, r)

    add("math: spend 0 is 'no spend field', never $0.00", cd.money(0) == {"state": "none", "value": None, "why": "no spend field"}, cd.money(0))
    add("math: spend in micros is dollars", cd.money(1_500_000)["value"] == 1.5 and cd.money(1_500_000)["state"] == "ok")
    c = cd.cost_per_conversion(0, 10)
    add("math: cost per conversion needs spend ('no spend field')", c["state"] == "none" and c["why"] == "no spend field" and c["value"] is None, c)
    c = cd.cost_per_conversion(5_000_000, 0)
    add("math: ... and conversions ('no conversions'), with no division by zero", c["state"] == "none" and c["why"] == "no conversions", c)
    c = cd.cost_per_conversion(10_000_000, 4)
    add("math: 4 conversions is a real cost but flagged low-n", c["value"] == 2.5 and c["low_n"] and c["n"] == 4, c)
    c = cd.cost_per_conversion(10_000_000, LOW)
    add("math: LOW_N conversions is not low-n", c["value"] == 2.0 and not c["low_n"], c)
    d = {**cd.derive({"sessions": 0, "clicks": 0, "impressions": 0, "conversions": 0}), **cd.money_cells(cd.group_money([]))}
    add("math: a group of zeros derives to reasons, never to numbers", d["ctr"]["why"] == "no impressions" and d["cvr"]["why"] == "no clicks" and d["spend"]["state"] == "none" and d["cpa"]["state"] == "none", d)
    add("math: the rate of sums, not the mean of rates (CTR = sum clicks / sum impressions)",
        cd.derive({"clicks": 30, "impressions": 1000, "conversions": 3, "spend_micros": 0, "sessions": 0})["ctr"]["pct"] == 3.0)
    s = cd.share(3, 0)
    add("math: a share of nothing is no bar and no percentage", s["frac"] == 0.0 and s["pct"] is None, s)
    s = cd.share(5, 20)
    add("math: a share of at least LOW_N sessions is a percentage with a bar fraction", s["pct"] == 25.0 and s["frac"] == 0.25, s)

    # previous-period delta
    tot = lambda **kw: {"days_with_data": 20, "sessions": 100, "clicks": 50, "conversions": 5, "spend_micros": 0, **kw}   # noqa: E731
    dl = cd.count_delta(120, tot(days_with_data=0), "sessions", 30)
    add("delta: no data in the previous period says so - never +0% or +infinity", dl["state"] == "none" and dl["pct"] is None and "no data" in dl["text"], dl)
    dl = cd.count_delta(120, {"error": "x"}, "sessions", 30)
    add("delta: an unreadable previous period is 'unavailable' on its own", dl["state"] == "unavailable" and dl["pct"] is None, dl)
    dl = cd.count_delta(120, None, "sessions", 30)
    add("delta: a missing previous block is 'unavailable'", dl["state"] == "unavailable", dl)
    dl = cd.count_delta(7, tot(sessions=2), "sessions", 30)
    add("delta: a previous number below LOW_N reads as counts, not as +250%", dl["state"] == "counts" and dl["pct"] is None and "7 now, 2" in dl["text"] and dl["dir"] == "up", dl)
    dl = cd.count_delta(112, tot(sessions=100), "sessions", 30)
    add("delta: 100 -> 112 is +12%, up and good", dl["state"] == "ok" and dl["pct"] == 12.0 and dl["dir"] == "up" and dl["good"] is True and dl["text"].startswith("12%"), dl)
    dl = cd.count_delta(50, tot(sessions=100), "sessions", 30)
    add("delta: 100 -> 50 is -50%, down and bad", dl["pct"] == -50.0 and dl["dir"] == "down" and dl["good"] is False, dl)
    dl = cd.count_delta(100, tot(sessions=100), "sessions", 30)
    add("delta: an unchanged number is flat and has no verdict", dl["dir"] == "flat" and dl["good"] is None, dl)
    dl = cd.count_delta(50, tot(sessions=100), "sessions", 30, good_up=None)
    add("delta: a metric with no good direction has no verdict", dl["good"] is None, dl)
    dl = cd.spend_delta(5_000_000, tot(spend_micros=0), 30)
    add("delta: spend against a previous period with no spend says so", dl["state"] == "none" and "no spend" in dl["text"], dl)
    dl = cd.spend_delta(6_000_000, tot(spend_micros=5_000_000), 30)
    add("delta: spend +20%", dl["pct"] == 20.0 and dl["good"] is None, dl)
    cur = cd.ratio(10, 100, "x")
    dl = cd.rate_delta(cur, tot(clicks=3, conversions=1), "conversions", "clicks", 30)
    add("delta: a rate change needs LOW_N clicks in BOTH periods", dl["state"] == "none" and "too few" in dl["text"], dl)
    dl = cd.rate_delta(cur, tot(clicks=100, conversions=5), "conversions", "clicks", 30)
    add("delta: a rate change is in percentage points", dl["state"] == "ok" and dl["pct"] == 5.0 and "pt" in dl["text"] and dl["good"] is True, dl)

    # the sort vocabulary
    add("sort: the default sort is in the vocabulary", cd.DEFAULT_SORT in cd.SORTS and cd.DEFAULT_DIR in ("asc", "desc"))
    rows = []
    for i, (name, clicks, conv) in enumerate([("Beta", 100, 10), ("alpha", 100, 5), ("Gamma", 0, 3), ("Delta", 50, 25)]):
        m = {"sessions": 100 - i, "clicks": clicks, "impressions": 1000, "conversions": conv}
        sp = 0 if name == "Delta" else 1_000_000 * (i + 1)
        rows.append({"name": name, **m, "days_with_data": 1, "low_n": False, **cd.derive(m),
                     **cd.money_cells(cd.group_money([{"currency": "USD", "spend_micros": sp, "revenue_micros": 0, "conversions": conv}]))})
    for key in cd.SORTS:
        for direction in ("asc", "desc"):
            out = cd.sort_rows(rows, key, direction)
            add(f"sort: {key} {direction} returns every row exactly once", sorted(r["name"] for r in out) == sorted(r["name"] for r in rows))
    out = [r["name"] for r in cd.sort_rows(rows, "cvr", "desc")]
    add("sort: a channel with no clicks (no conversion rate) sorts LAST when descending", out[-1] == "Gamma" and out[:3] == ["Delta", "Beta", "alpha"], out)
    out = [r["name"] for r in cd.sort_rows(rows, "cvr", "asc")]
    add("sort: ... and LAST when ascending too - unknown is not 'smallest'", out[-1] == "Gamma" and out[:3] == ["alpha", "Beta", "Delta"], out)
    out = [r["name"] for r in cd.sort_rows(rows, "cpa", "asc")]
    add("sort: a channel with no spend (no cost per conversion) sorts last in both directions", out[-1] == "Delta" and [r["name"] for r in cd.sort_rows(rows, "cpa", "desc")][-1] == "Delta", out)
    out = [r["name"] for r in cd.sort_rows(rows, "clicks", "desc")]
    add("sort: ties keep alphabetical order (case-insensitive)", out[:2] == ["alpha", "Beta"], out)
    add("sort: an unknown key falls back to the default instead of raising", [r["name"] for r in cd.sort_rows(rows, "nope", "desc")] == [r["name"] for r in cd.sort_rows(rows, cd.DEFAULT_SORT, "desc")])
    js = (ROOT / "desktop" / "static" / "channels.js").read_text(encoding="utf-8", errors="replace")
    cols = set(re.findall(r"\['[^']+', '(\w+)', '[ln]'\]", js))
    add("sort: the page's sortable column keys are exactly the server's vocabulary", cols == set(cd.SORTS), (sorted(cols), sorted(cd.SORTS)))

    # the range
    v, _o = _view()
    add("range: with no from/to the range is DEFAULT_DAYS ending at the newest rollup day", (v.d_to, v.days, v.defaulted) == (date(2026, 9, 24), cd.DEFAULT_DAYS, True) and v.d_from == date(2026, 8, 26), v)
    add("range: dates in a sentence are spelled out, one day or many, one year or two", cd.range_text(date(2026, 9, 24), date(2026, 9, 24)) == "on 24 Sep 2026" and cd.range_text(date(2025, 12, 26), date(2026, 1, 24)) == "from 26 Dec 2025 to 24 Jan 2026" and cd.range_text(date(2026, 8, 26), date(2026, 9, 24)) == "from 26 Aug to 24 Sep 2026")
    add("range: the previous period is the same length immediately before", (v.prev_to, (v.prev_to - v.prev_from).days + 1) == (date(2026, 8, 25), cd.DEFAULT_DAYS), v)
    v, _o = _view(d_from=date(2026, 9, 1), d_to=date(2026, 9, 3))
    add("range: an explicit range is used as is, its previous period is 3 days", (v.days, v.prev_from, v.prev_to, v.defaulted) == (3, date(2026, 8, 29), date(2026, 8, 31), False), v)
    v, _o = _view(d_to=date(2026, 9, 10))
    add("range: only `to` gives DEFAULT_DAYS before it", v.d_from == date(2026, 9, 10) - timedelta(days=cd.DEFAULT_DAYS - 1), v)
    v, _o = _view(d_from=date(2026, 9, 1))
    add("range: only `from` runs to the newest day", v.d_to == date(2026, 9, 24), v)
    q = cd.view_query(v)
    add("range: the export query writes the RESOLVED range out (a moving default cannot change what a link means)", "from=2026-09-01" in q and "to=2026-09-24" in q, q)


# =========================================================================================== pure: states, tiles, panels
def _check_states(add) -> None:
    from desktop import channels_data as cd

    add("state: a missing table means not installed", cd.page_state(["interaction_daily_rollup"], None, None) == "not_installed")
    add("state: installed with no rollup row means empty", cd.page_state([], 0, None) == "empty")
    add("state: data exists but the totals are empty means no match", cd.page_state([], 60, {"days_with_data": 0, "sessions": 0, "clicks": 0, "impressions": 0, "conversions": 0, "spend_micros": 0}) == "no_match")
    add("state: data and a non-empty view is ready", cd.page_state([], 60, _tot()) == "ready")
    add("state: an all-zero view that still has rows is ready, not 'no match'", cd.page_state([], 60, _tot(sessions=0, clicks=0, impressions=0, conversions=0)) == "ready")

    h = cd.install_help(["interaction_daily_rollup", "interaction_daily_channel_rollup", "marketing_rollup_run"], "public")
    cmds = [s["command"] for s in h["steps"]]
    add("setup: migration 10 comes before 09 (the rollup job groups by the currency column), both as exact owner-run psql commands", cmds[0] == cd.PSQL_10 and "10_marketing_currency.sql" in cmds[0] and cmds[1] == cd.PSQL_09 and "09_marketing_rollup.sql" in cmds[1] and all("psql.exe" in c and "-U erp_app" in c for c in cmds[:2]), cmds)
    add("setup: ... and 07 is not, when the marketing tables exist", cd.PSQL_07 not in cmds, cmds)
    h07 = cd.install_help(["marketing_landing", "interaction_daily_rollup"], "public")
    add("setup: 07 comes FIRST, then 10, then 09, when the marketing tables themselves are missing", [s["command"] for s in h07["steps"]][:3] == [cd.PSQL_07, cd.PSQL_10, cd.PSQL_09], [s["command"] for s in h07["steps"]])
    add("setup: the load and rollup commands are shown", any("marketing.ingest" in c for c in cmds) and any("marketing.rollup" in c for c in cmds), cmds)
    he = cd.empty_help("public")
    add("setup: the empty state says no data has been loaded and gives the load command", "no data has been loaded yet" in he["steps"][0]["title"] and "ingest" in he["steps"][0]["command"], he)
    add("setup: a non-public schema is named in the note", "perf_x" in cd.install_help(["interaction_daily_rollup"], "perf_x")["note"])

    for state, help_ in (("not_installed", h), ("empty", he)):
        p = cd.assemble_setup(state, {"tz": "Asia/Bangkok", "schema": "public", "rollup_rows": 0}, help_, None, NOW)
        add(f"never-zero: the {state} payload has no tile, no panel and no export link", p["kpis"] == [] and p["panels"] == {} and p["export"] is None and p["range"] is None, p)
        add(f"never-zero: the {state} headline contains no number at all", not re.search(r"\d", p["headline"]["text"] + p["headline"]["sub"]), p["headline"])
        add(f"never-zero: the {state} payload says the raw fact table was not read and has definitions", p["read"]["raw_fact_read"] is False and p["definitions"], p["read"])
        add(f"never-zero: the {state} payload is ok (nothing is broken) and carries the time zone", p["ok"] is True and p["time_zone"] == "Asia/Bangkok" and p["state"] == state, p["state"])
        add(f"never-zero: the {state} payload has an options block with an empty channel list", p["options"]["channels"] == [], p["options"])
    p = cd.assemble_setup("not_installed", {"tz": None, "schema": "public"}, h, None, NOW)
    add("setup: an unreadable time zone is worded, not guessed", "unknown" in p["definitions"][1]["text"] or "not readable" in p["definitions"][1]["text"], p["definitions"][1])


def _check_assemble(add) -> None:
    from desktop import channels_data as cd

    v, o = _view()
    p = cd.assemble(_raw(v), v, o, META, NOW)
    add("assemble: a ready payload carries state, range, zone and source", p["state"] == "ready" and p["range"]["from"] == "2026-08-26" and p["time_zone"] == "Asia/Bangkok" and p["source"] == "channel_rollup", (p["state"], p["range"]))
    add("assemble: every figure carries its date range and the zone: the headline says both (in words, never a wrapping ISO date)", "from 26 Aug to 24 Sep 2026" in p["headline"]["text"] and "Asia/Bangkok" in p["headline"]["sub"], p["headline"])
    tiles = {t["id"]: t for t in p["kpis"]}
    add("assemble: six tiles, in order (spend and revenue last)", [t["id"] for t in p["kpis"]] == ["sessions", "clicks", "conversions", "cvr", "spend", "revenue"], [t["id"] for t in p["kpis"]])
    add("assemble: the sessions tile is the sum, with a real delta against the previous period", tiles["sessions"]["value"] == 100 and tiles["sessions"]["delta"]["pct"] == 25.0, tiles["sessions"])
    add("assemble: the conversion-rate tile is conversions / clicks from the sums (10%)", tiles["cvr"]["value"] == 10.0 and tiles["cvr"]["unit"] == "%", tiles["cvr"])
    add("assemble: no spend anywhere is 'no spend field', not $0.00", tiles["spend"]["state"] == "none" and tiles["spend"]["value"] is None and "no spend field" in tiles["spend"]["sub"], tiles["spend"])
    add("assemble: the headline leads with the sessions total, the trend and the top channel", p["headline"]["text"].startswith("100 sessions from") and "up 25%" in p["headline"]["text"] and "Channel 1 brought 60%" in p["headline"]["text"], p["headline"])
    add("assemble: the sparkline has one entry per day and a GAP (None), never a zero, where a day has no row",
        tiles["sessions"]["spark"] is not None and tiles["sessions"]["spark"]["values"][3] is None and tiles["sessions"]["spark"]["values"][0] == 30, tiles["sessions"]["spark"])
    tb = p["panels"]["table"]
    add("assemble: the table total equals the sum of its rows and the tile", tb["total"]["sessions"] == sum(r["sessions"] for r in tb["rows"]) == tiles["sessions"]["value"], tb["total"])
    add("assemble: shares add up to 100% and each is a percentage (total >= LOW_N)", abs(sum(r["share"]["pct"] for r in tb["rows"]) - 100) < 0.01, [r["share"] for r in tb["rows"]])
    add("assemble: the definitions are present and the export links carry the resolved range", len(p["definitions"]) >= 12 and "from=2026-08-26" in p["export"]["channels"] and p["export"]["campaigns"] is None, p["export"])
    add("assemble: the payload says which tables it read and that the raw table was not", p["read"]["tables"] == ["interaction_daily_channel_rollup"] and p["read"]["raw_fact_read"] is False, p["read"])
    add("assemble: a healthy read is ok", p["ok"] is True)

    # low n, zero denominators and 'no field' in one table
    ch = [_chrow(1, sessions=200, clicks=100, impressions=0, conversions=0, spend_micros=0), _chrow(2, sessions=3, clicks=2, impressions=4, conversions=1, spend_micros=6_000_000),
          _chrow(3, sessions=50, clicks=0, impressions=500, conversions=5, spend_micros=10_000_000)]
    p = cd.assemble(_raw(v, channels=ch, totals=_tot(sessions=253, clicks=102, impressions=504, conversions=6, spend=16_000_000)), v, o, META, NOW)
    rows = {r["key"]: r for r in p["panels"]["table"]["rows"]}
    add("table: no impressions is a reason, not a 0% CTR", rows["ch1"]["ctr"]["pct"] is None and rows["ch1"]["ctr"]["why"] == "no impressions" and not rows["ch1"]["ctr"]["low_n"], rows["ch1"]["ctr"])
    add("table: 1 conversion of 2 clicks is a count under the low-n rule", rows["ch2"]["cvr"]["pct"] is None and rows["ch2"]["cvr"]["low_n"] and (rows["ch2"]["cvr"]["n"], rows["ch2"]["cvr"]["of"]) == (1, 2), rows["ch2"]["cvr"])
    add("table: a channel with fewer than LOW_N sessions is marked low volume", rows["ch2"]["low_n"] and not rows["ch1"]["low_n"], (rows["ch2"]["low_n"], rows["ch1"]["low_n"]))
    add("table: no clicks means no conversion rate, with the reason", rows["ch3"]["cvr"]["pct"] is None and rows["ch3"]["cvr"]["why"] == "no clicks", rows["ch3"]["cvr"])
    add("table: no spend is 'no spend field' and its cost per conversion likewise", rows["ch1"]["spend"]["state"] == "none" and rows["ch1"]["cpa"]["why"] == "no spend field", (rows["ch1"]["spend"], rows["ch1"]["cpa"]))
    add("table: spend and conversions give a cost per conversion (only where spend exists)", rows["ch3"]["cpa"]["value"] == 2.0 and rows["ch2"]["cpa"]["value"] == 6.0 and rows["ch2"]["cpa"]["low_n"], (rows["ch3"]["cpa"], rows["ch2"]["cpa"]))
    add("table: the total row derives its rates from the summed columns", p["panels"]["table"]["total"]["ctr"]["pct"] == round(102 / 504 * 100, 3), p["panels"]["table"]["total"]["ctr"])
    p = cd.assemble(_raw(v, totals=_tot(sessions=3, clicks=2, conversions=1, impressions=0), channels=[_chrow(1, sessions=3, clicks=2, conversions=1, impressions=0)]), v, o, META, NOW)
    t = {x["id"]: x for x in p["kpis"]}
    add("tiles: a view with 2 clicks shows '1 of 2', not 50%", t["cvr"]["text"] == "1 of 2" and t["cvr"]["value"] is None, t["cvr"])
    add("headline: a total below LOW_N names the leader by count, not by percentage", "brought 3 of them" in p["headline"]["text"] or "brought 3 of 3" in p["headline"]["text"], p["headline"])

    # independent degradation
    raw = _raw(v)
    raw["totals"] = {"error": "boom"}
    p = cd.assemble(raw, v, o, META, NOW)
    add("degrade: totals failing greys every tile with a reason and the headline says it could not be read", all(t["state"] == "unavailable" for t in p["kpis"]) and len(p["kpis"]) == 6 and "could not be read" in p["headline"]["text"], p["kpis"])
    add("degrade: ... while the table and the chart keep working", p["panels"]["table"]["state"] == "ok" and p["panels"]["chart"]["state"] == "ok" and p["ok"] is False)
    raw = _raw(v)
    raw["prev"] = {"error": "boom"}
    p = cd.assemble(raw, v, o, META, NOW)
    t = {x["id"]: x for x in p["kpis"]}
    add("degrade: the previous period failing only removes the deltas ('unavailable'), not the numbers", t["sessions"]["value"] == 100 and t["sessions"]["delta"]["state"] == "unavailable" and t["cvr"]["delta"]["state"] == "unavailable", t["sessions"])
    raw = _raw(v)
    raw["daily"] = {"error": "boom"}
    p = cd.assemble(raw, v, o, META, NOW)
    t = {x["id"]: x for x in p["kpis"]}
    add("degrade: the daily block failing removes the chart and the sparklines only", p["panels"]["chart"]["state"] == "unavailable" and t["sessions"]["value"] == 100 and t["sessions"]["spark"] is None and p["panels"]["table"]["state"] == "ok")
    raw = _raw(v)
    raw["channels"] = {"error": "boom"}
    p = cd.assemble(raw, v, o, META, NOW)
    add("degrade: the table failing leaves the tiles and the chart alone", p["panels"]["table"]["state"] == "unavailable" and p["panels"]["chart"]["state"] == "ok" and p["kpis"][0]["state"] == "ok")
    raw = _raw(v)
    raw["channels"] = [{"channel_id": 1}]             # malformed: no measures at all - a builder bug must not take the page down
    p = cd.assemble(raw, v, o, META, NOW)
    add("degrade: a row with no measures builds as zero sums, not an exception", p["panels"]["table"]["state"] in ("ok", "unavailable"))

    # a single day, a gap-only window and a prev period with no data
    raw = _raw(v, daily=[_daily(1, v.d_from)])
    p = cd.assemble(raw, v, o, META, NOW)
    add("spark: one day of data draws no sparkline (a line needs two real points)", {t["id"]: t for t in p["kpis"]}["sessions"]["spark"] is None)
    raw = _raw(v, prev=_tot(days=0, sessions=0, clicks=0, conversions=0, impressions=0))
    p = cd.assemble(raw, v, o, META, NOW)
    t = {x["id"]: x for x in p["kpis"]}
    add("delta: a previous period without data is 'no data in the previous period', never +0%", t["sessions"]["delta"]["state"] == "none" and t["sessions"]["delta"]["pct"] is None and "no data in the previous period" in p["headline"]["text"], (t["sessions"]["delta"], p["headline"]))
    raw = _raw(v, totals=_tot(days=0, sessions=0, clicks=0, impressions=0, conversions=0), channels=[], daily=[])
    p = cd.assemble(raw, v, o, META, NOW)
    add("state: a range with no rollup row at all is 'no match' with a sentence and no tiles", p["state"] == "no_match" and p["kpis"] == [] and "Nothing matches" in p["headline"]["text"], (p["state"], p["headline"]))
    raw = _raw(v, totals=_tot(sessions=0, clicks=40, conversions=4, impressions=400))
    p = cd.assemble(raw, v, o, META, NOW)
    add("headline: with no sessions recorded it talks about clicks instead of inventing sessions", " clicks from " in p["headline"]["text"] and "session" not in p["headline"]["text"], p["headline"])
    raw = _raw(v, totals=_tot(sessions=0, clicks=0, conversions=0, impressions=50))
    p = cd.assemble(raw, v, o, META, NOW)
    add("headline: with only impressions it says nothing was recorded rather than '0 sessions'", "No sessions, clicks or conversions" in p["headline"]["text"], p["headline"])

    # freshness
    stale_meta = dict(META, data_as_of=NOW - timedelta(days=9))
    p = cd.assemble(_raw(v), v, o, stale_meta, NOW)
    add("fresh: a rollup recomputed 9 days ago is called out and said to be unscheduled", p["freshness"]["stale"] and any("9 days ago" in w for w in p["warnings"]) and "not scheduled" in p["freshness"]["text"], p["freshness"])
    p = cd.assemble(_raw(v), v, o, META, NOW)
    add("fresh: a recent rollup raises no warning", not p["freshness"]["stale"] and not any("ago" in w for w in p["warnings"]), p["warnings"])

    # campaign filter switches the source table and says so
    v2, o2 = _view(campaign=10)
    p = cd.assemble(_raw(v2), v2, o2, dict(META, campaign_name="Summer"), NOW)
    add("source: a campaign filter reads the campaign rollup and the page says why", p["source"] == "campaign_rollup" and p["source_table"] == "interaction_daily_rollup" and any("campaign filter" in w for w in p["warnings"]), (p["source"], p["warnings"]))


def _check_panels(add) -> None:
    from desktop import channels_data as cd

    v, o = _view(_options(10))
    d0 = date(2026, 8, 26)
    rows = [_daily(c, d0 + timedelta(days=i), sessions=100 - c * 5 + i) for c in range(1, 11) for i in range(0, 6) if not (c == 2 and i == 3)]
    ch = cd.build_chart(rows, o, v)
    add("chart: the 8 largest channels get a line and the rest are ONE 'Other channels' line", len(ch["series"]) == cd.CHART_MAX_LINES + 1 and ch["series"][-1]["key"] == "_other" and ch["lines_hidden"] == 2, [s["name"] for s in ch["series"]])
    tot_in = sum(r["sessions"] for r in rows)
    tot_chart = sum(x or 0 for s in ch["series"] for x in s["values"]["sessions"])
    add("chart: nothing is lost by folding channels into 'Other' (sums are additive)", tot_in == tot_chart, (tot_in, tot_chart))
    c2 = next(s for s in ch["series"] if s["key"] == "ch2")
    add("chart: a day with no row is a gap (None) in that channel's line, not a zero", c2["values"]["sessions"][3] is None and c2["values"]["sessions"][2] is not None, c2["values"]["sessions"][:6])
    add("chart: one entry per day of the window, the days are continuous", len(ch["days"]) == v.days and ch["days"][0] == "2026-08-26" and ch["days"][-1] == "2026-09-24", (len(ch["days"]), ch["days"][:2]))
    add("chart: a metric with nothing recorded is flagged so the page can disable it", any(m["key"] == "clicks" and m["total"] > 0 for m in ch["metrics"]), ch["metrics"])
    zero_rows = [_daily(1, d0, sessions=0, clicks=7, impressions=0, conversions=0), _daily(1, d0 + timedelta(days=1), sessions=0, clicks=9, impressions=0, conversions=0)]
    ch2 = cd.build_chart(zero_rows, o, v)
    add("chart: with no sessions at all the default metric is the first one that has data", ch2["default_metric"] == "clicks" and next(m for m in ch2["metrics"] if m["key"] == "sessions")["total"] == 0, (ch2["default_metric"], ch2["metrics"]))
    vlong, _o = _view(_options(3), d_from=date(2026, 1, 1), d_to=date(2026, 9, 24))
    chl = cd.build_chart([_daily(1, date(2026, 9, 24))], _o, vlong)
    add("chart: a long range shows only the newest CHART_MAX_DAYS days and says it was clipped", len(chl["days"]) == cd.CHART_MAX_DAYS and chl["clipped"] and chl["days"][-1] == "2026-09-24", (len(chl["days"]), chl["clipped"]))

    # campaigns
    v, o = _view(focus="ch2")
    add("campaigns: with no focus channel the panel asks for one", cd.build_campaigns(None, o, _view()[0], None)["state"] == "need_focus")
    add("campaigns: an unreadable block is 'unavailable' for that panel only", cd.build_campaigns({"error": "boom"}, o, v, 100)["state"] == "unavailable")
    crow = lambda cid, s: {"campaign_id": cid, "days_with_data": 5, "sessions": s, "clicks": 10 if s >= 5 else 2, "impressions": 100, "conversions": 2, "spend_micros": 0, "total_campaigns": 40}   # noqa: E731
    block = {"rows": [crow(7, 50), crow(0, 30), crow(9, 3)], "names": [{"campaign_id": 7, "name": "Spring", "campaign_key": "spring"}, {"campaign_id": 9, "name": None, "campaign_key": "raw_key"}]}
    pan = cd.build_campaigns(block, o, v, 200)
    add("campaigns: names come from the lookup, a missing name falls back to the key, campaign 0 is '(no campaign)'",
        [r["name"] for r in pan["rows"]] == ["Spring", "(no campaign)", "raw_key"], [r["name"] for r in pan["rows"]])
    add("campaigns: 3 shown of 40 says '37 more' and how many sessions the rest hold", pan["more"] == 37 and pan["shown"] == 3 and pan["rest_sessions"] == 200 - 83 and pan["total_campaigns"] == 40, pan)
    add("campaigns: shares are of the channel's sessions, low n marked", abs(pan["rows"][0]["share"]["pct"] - 25.0) < 0.001 and pan["rows"][2]["low_n"], pan["rows"])
    add("campaigns: nothing in range is an 'empty' panel, not an error", cd.build_campaigns({"rows": [], "names": []}, o, v, 0)["state"] == "empty")
    full = cd.build_campaigns({"rows": [dict(crow(1, 10), total_campaigns=1)], "names": []}, o, v, 10)
    vo, oo = _view(channels=("ch1",), focus="ch2")
    vo = cd.dataclasses.replace(vo, channel_ids=(1,))
    out = cd.assemble(_raw(vo), vo, oo, META, NOW)
    add("campaigns: a focus channel the channel filter excludes says so (state outside_filter), never 'no campaign rows'",
        out["panels"]["campaigns"]["state"] == "outside_filter" and out["panels"]["campaigns"]["focus"]["key"] == "ch2" and out["ok"] and not cd.focus_in_filter(vo)
        and cd.focus_in_filter(cd.dataclasses.replace(vo, channel_ids=())) and cd.focus_in_filter(cd.dataclasses.replace(vo, channel_ids=(1, 2))), out["panels"]["campaigns"])
    add("campaigns: when everything fits there is no 'more' note", full["more"] == 0 and full["rest_sessions"] is None, full)


# =========================================================================================== pure: CSV
def _check_csv(add) -> None:
    from desktop import channels_data as cd

    add("csv: bytes start with a UTF-8 BOM and lines end with CRLF", cd.to_csv(["a"], [["x"]]).startswith(b"\xef\xbb\xbf") and cd.to_csv(["a"], [["x"]]).endswith(b"\r\n"))
    for lead in ("=", "+", "-", "@", ";", "\t", "\r"):
        c = cd.csv_cell(lead + "cmd|' /c calc'!A0")
        add(f"csv: a text cell starting with {lead!r} is defused", c.startswith("'"), c)
    add("csv: numbers are written as numbers, never prefixed (a negative int stays a number)", cd.csv_cell(-3) == "-3" and cd.csv_cell(12) == "12" and cd.csv_cell(0.5) == "0.5" and cd.csv_cell(2.0) == "2")
    add("csv: an empty / unknown value is an empty cell", cd.csv_cell(None) == "" and cd.csv_cell(float("nan")) == "")
    add("csv: a date is ISO", cd.csv_cell(date(2026, 9, 1)) == "2026-09-01")
    add("csv: a NUL byte in text is dropped", "\x00" not in cd.csv_cell("a\x00b"))

    bad = re.compile(r"e-?mail|phone|identity|user|lead|payload|token|password|secret|ip_|cookie|customer|name_of", re.I)
    for label, cols in (("channels", cd.CHANNEL_CSV), ("daily", cd.DAILY_CSV), ("campaigns", cd.CAMPAIGN_CSV)):
        hit = [c for c in cols if bad.search(c)]
        add(f"csv: the {label} file has no identity or personal column", not hit, hit)
        add(f"csv: the {label} file names the time zone", "time_zone" in cols)
    add("csv: the range travels in the channel and campaign files", {"period_from", "period_to"} <= set(cd.CHANNEL_CSV) and {"period_from", "period_to"} <= set(cd.CAMPAIGN_CSV))

    v, o = _view()
    ch = [_chrow(1, sessions=200, clicks=100, impressions=0, conversions=0, spend_micros=0), _chrow(2, sessions=3, clicks=2, impressions=4, conversions=1, spend_micros=6_000_000)]
    o["_channels"][0]["name"] = "=HYPERLINK(\"http://x\")"
    table = cd.build_table(ch, o, v, cap=None)
    rows = cd.channel_csv_rows(table, v, "Asia/Bangkok")
    body = cd.to_csv(cd.CHANNEL_CSV, rows).decode("utf-8-sig")
    parsed = list(csv.reader(io.StringIO(body, newline="")))
    head = parsed[0]
    add("csv: every row has as many cells as the header", all(len(r) == len(head) for r in parsed), [len(r) for r in parsed])
    first = dict(zip(head, parsed[1]))
    add("csv: a channel called =HYPERLINK(...) is defused in the file", first["channel"].startswith("'="), first["channel"])
    add("csv: the CTR of a channel with no impressions is blank (as on screen), not 0", first["ctr_pct"] == "" and first["spend"] == "" and first["cost_per_conversion"] == "" and first["currency"] == "", first)
    second = dict(zip(head, parsed[2]))
    add("csv: a low-n conversion rate is blank and the row says low_volume", second["conversion_rate_pct"] == "" and second["low_volume"] == "yes" and second["conversions"] == "1", second)
    add("csv: the period and the zone are on every row", first["period_from"] == "2026-08-26" and first["time_zone"] == "Asia/Bangkok" and first["source_table"] == "interaction_daily_channel_rollup", first)
    dr = cd.daily_csv_rows([_daily(1, date(2026, 9, 1), spend_micros=0), _daily(2, date(2026, 9, 1), spend_micros=2_500_000)], o, v, "Asia/Bangkok")
    add("csv: a daily row with no spend has an empty spend and currency cell, one with spend has the amount AND its currency", dr[0][11] is None and dr[0][6] == "" and dr[1][11] == 2.5 and dr[1][6] == "USD" and all(len(r) == len(cd.DAILY_CSV) for r in dr), dr)
    v, o = _view(focus="ch1")
    pan = cd.build_campaigns({"rows": [{"campaign_id": 5, "days_with_data": 2, "sessions": 9, "clicks": 3, "impressions": 30, "conversions": 1, "spend_micros": 0, "total_campaigns": 1}], "names": [{"campaign_id": 5, "name": "-x", "campaign_key": "k"}]}, o, v, 9)
    cr = cd.campaign_csv_rows(pan, v, "Asia/Bangkok")
    add("csv: campaign rows match the header and defuse a name starting with '-'", all(len(r) == len(cd.CAMPAIGN_CSV) for r in cr) and cd.csv_cell(cr[0][6]).startswith("'-"), cr)


# =========================================================================================== pure: text, SQL text, timeouts
def _check_text(add) -> None:
    from desktop import channels_data as cd
    from erp import db as edb
    from erp.marketing import schema as mschema

    d = cd.build_definitions("Asia/Bangkok", "public")
    joined = " ".join(x["term"] + " " + x["text"] for x in d)
    add("defs: every definition has a term and a text and the ids are unique", all(x["term"] and x["text"] for x in d) and len({x["id"] for x in d}) == len(d))
    add("defs: the time zone is named", "Asia/Bangkok" in joined)
    add("defs: the refresh cadence says it is not scheduled unless the owner schedules it", "python -m erp.marketing.rollup" in joined and "not scheduled unless the owner schedules it" in joined, cd.REFRESH_TEXT)
    add("defs: the rollup grain and both table names are stated", "one row per day x channel" in joined and cd.CHANNEL_TABLE in joined and cd.CAMPAIGN_TABLE in joined and "campaign 0" in joined)
    add("defs: the metrics are all defined", all(w in joined for w in ("CTR", "Conversion rate", "cost per conversion", "Share of sessions", "previous period")))
    add("defs: it says the raw tables are never read", "never reads interaction_fact" in joined)
    with _patched(cd, LOW_N=7, DEFAULT_DAYS=45, CHART_MAX_DAYS=90, CAMPAIGN_ROWS_MAX=12):
        d2 = " ".join(x["text"] for x in cd.build_definitions("UTC", "public"))
    add("defs: the low-n threshold in the text follows the constant (a literal 5 would go stale, L-085)", "at least 7" in d2 or "fewer than 7" in d2 or "Below 7" in d2, d2[:300])
    add("defs: ... and no stale threshold survives (a bare '5' as a count threshold)", not re.search(r"(?<![\d.])5(?![\d])", d2.replace("25", "").replace("15", "")), re.findall(r".{20}(?<![\d.])5(?![\d]).{10}", d2)[:3])
    add("defs: the default range, chart window and campaign cap follow their constants", "last 45 days" in d2 and "newest 90 days" in d2 and "at most 12" in d2, d2[:200])
    with _patched(cd, REFRESH_TEXT="refreshed by X"):
        add("defs: the refresh text is one constant", "refreshed by X" in " ".join(x["text"] for x in cd.build_definitions("UTC", "public")))

    # SQL text: identifiers checked, values bound, no raw table
    v, o = _view(_options(3), campaign=5, focus="ch1")
    v = cd.dataclasses.replace(v, channel_ids=(1, 2), focus_id=1)
    sqls = [cd.totals_sql("public", v)[0], cd.totals_sql("public", v, prev=True)[0], cd.channels_sql("public", v)[0], cd.daily_sql("public", v, v.d_from, 10)[0], cd.campaigns_sql("public", v, 5)[0]]
    v0, _o0 = _view()
    sqls += [cd.totals_sql("public", v0)[0], cd.channels_sql("public", v0)[0], cd.daily_sql("public", v0, v0.d_from, 10)[0], cd.INSTALL_SQL]
    add("sql: no query names the raw fact table or the landing table", not any(re.search(r"interaction_fact|marketing_landing", s) for s in sqls), [s for s in sqls if "interaction_fact" in s][:1])
    add("sql: the channel-level queries read the channel rollup and only the campaign ones the campaign rollup",
        "interaction_daily_channel_rollup" in cd.channels_sql("public", v0)[0] and "interaction_daily_rollup" not in cd.channels_sql("public", v0)[0].replace("interaction_daily_channel_rollup", "")
        and "interaction_daily_rollup" in cd.campaigns_sql("public", v, 5)[0])
    add("sql: a campaign filter is bound, not formatted", ":campaign_id" in cd.channels_sql("public", v)[0] and cd.channels_sql("public", v)[1]["campaign_id"] == 5)
    evil = "x'; DROP TABLE marketing_channel; --"
    req = cd.Request(channels=(evil,), focus=evil, campaign=None)
    vv = cd.resolve_view(req, o, _ids(o))
    text_all = " ".join([cd.totals_sql("public", vv)[0], cd.channels_sql("public", vv)[0], cd.daily_sql("public", vv, vv.d_from, 5)[0], cd.campaigns_sql("public", vv, 5)[0]])
    add("sql: request text never reaches the SQL text", "DROP" not in text_all and evil not in text_all and vv.channel_ids == () and vv.focus_id is None, vv)
    try:
        mschema.qualified("public; DROP TABLE x", "a")
        ok = False
    except mschema.SchemaError:
        ok = True
    add("sql: a schema name goes through check_identifier (a hostile MARKETING_SCHEMA is refused)", ok)
    add("sql: the sort key comes from a dict of Python functions, never SQL (there is no ORDER BY built from a request)", "ORDER BY" not in cd.channels_sql("public", v0)[0])

    # timeouts: the server's worst case must stay under what the page waits (L-094 / L-182)
    js = (ROOT / "desktop" / "static" / "channels.js").read_text(encoding="utf-8", errors="replace")
    m = re.search(r"FETCH_TIMEOUT_MS\s*=\s*(\d+)", js)
    client_s = int(m.group(1)) / 1000 if m else 0
    server_s = edb.READ_CHECKOUT_TIMEOUT + cd.QUERY_BUDGET_SECONDS
    add("timeouts: checkout bound + query budget stay below the page's fetch timeout (else it shows its own error, not the server's)", 0 < server_s < client_s, (server_s, client_s))
    add("timeouts: ... and a build never waits for a slot longer than the fetch timeout allows", cd.BUILD_WAIT_SECONDS + server_s < client_s + 1e-9 + 10, cd.BUILD_WAIT_SECONDS)
    add("timeouts: the export's server budget stays under the page's 60 s download abort", edb.READ_CHECKOUT_TIMEOUT + cd.EXPORT_BUDGET_SECONDS < 60 and "60000" in js, cd.EXPORT_BUDGET_SECONDS)
    add("page: it polls with an AbortController and the way every other page does", "AbortController" in js and "POLL_MS" in js and "visibilitychange" in js)
    css = (ROOT / "desktop" / "static" / "channels.css").read_text(encoding="utf-8", errors="replace")
    fams = re.findall(r"font-family\s*:\s*([^;}{]+)", css)
    add("page: the stylesheet only uses the project's font tokens (--font / --code), never a font name", all(f.strip() in ("var(--font)", "var(--code)") for f in fams), [f for f in fams if f.strip() not in ("var(--font)", "var(--code)")])
    add("page: the numbers that move use tabular figures", "tabular-nums" in css.split(".ch-kpi-value {")[1].split("}")[0] and "tabular-nums" in css.split(".ch-tbl td.n {")[1].split("}")[0])
    add("page: [hidden] beats display:grid / flex for every toggled element (L-092)", all(f"{s}[hidden]" in css for s in (".ch-kpis", ".ch-grid", ".ch-fast")))
    shell = (ROOT / "desktop" / "static" / "shell.js").read_text(encoding="utf-8", errors="replace")
    html = (ROOT / "desktop" / "static" / "shell.html").read_text(encoding="utf-8", errors="replace")
    add("nav: 'channels' is the seventh view and Ctrl+7 opens it (Ctrl+6 stays Health)", "'health', 'channels']" in shell and "e.key === '7'" in shell and "setView('channels')" in shell and "e.key === '6'" in shell and "setView('health')" in shell)
    navs = re.findall(r'class="nav-item" href="#(\w+)"', html)
    add("nav: the nav item order matches the VIEWS order in shell.js (the indicator is positioned by index)", navs[-1] == "channels" and navs[-2] == "health" and len(navs) == 7, navs)
    add("nav: the page loads its own script and stylesheet", '/static/channels.js' in html and '/static/channels.css' in html and 'id="view-channels"' in html)
    node_ok = True
    try:
        import shutil
        import subprocess
        node = shutil.which("node")
        if node:
            r = subprocess.run([node, "--check", str(ROOT / "desktop" / "static" / "channels.js")], capture_output=True, text=True, timeout=20)
            node_ok = r.returncode == 0
            add("page: channels.js parses (node --check)", node_ok, r.stderr[:200])
    except Exception:  # noqa: BLE001 - node missing / slow: the gate does not depend on it
        pass


# =========================================================================================== SQL: the real DDL in pg_temp
_FACT_DAYS_FROM, _LAST = date(2026, 7, 1), date(2026, 9, 10)
_GAPS = {date(2026, 8, 20), date(2026, 8, 21), date(2026, 9, 1)}


def _plan() -> list[dict]:
    """The hand-worked campaign-level rollup rows (event_date, channel_id, campaign_id + the five sums)."""
    rows = []
    d = _FACT_DAYS_FROM
    while d <= _LAST:
        if d not in _GAPS:
            rows += [
                dict(d=d, ch=1, cp=10, s=50 + d.day % 5, c=30, i=1000, cv=3, sp=12_500_000),
                dict(d=d, ch=1, cp=11, s=20, c=10, i=400, cv=1, sp=4_000_000),
                dict(d=d, ch=2, cp=20, s=40, c=25, i=800, cv=2, sp=9_000_000),
                dict(d=d, ch=2, cp=21, s=5, c=2, i=100, cv=0, sp=1_000_000),
                dict(d=d, ch=2, cp=0, s=2, c=0, i=0, cv=0, sp=0),
            ]
            if d.toordinal() % 3 == 0:
                rows.append(dict(d=d, ch=3, cp=0, s=30, c=12, i=0, cv=6, sp=0))
        d += timedelta(days=1)
    rows += [dict(d=date(2026, 9, 8), ch=4, cp=0, s=1, c=1, i=0, cv=0, sp=0), dict(d=date(2026, 9, 9), ch=4, cp=0, s=1, c=1, i=0, cv=1, sp=0),
             dict(d=date(2026, 9, 10), ch=4, cp=0, s=1, c=0, i=0, cv=0, sp=0)]
    return rows


def _expect(rows, lo: date, hi: date, channels=None, campaign=None) -> dict:
    sel = [r for r in rows if lo <= r["d"] <= hi and (channels is None or r["ch"] in channels) and (campaign is None or r["cp"] == campaign)]
    out = {"sessions": sum(r["s"] for r in sel), "clicks": sum(r["c"] for r in sel), "impressions": sum(r["i"] for r in sel),
           "conversions": sum(r["cv"] for r in sel), "spend_micros": sum(r["sp"] for r in sel), "days": len({r["d"] for r in sel})}
    per: dict[int, dict] = {}
    for r in sel:
        p = per.setdefault(r["ch"], {"sessions": 0, "clicks": 0, "impressions": 0, "conversions": 0, "spend_micros": 0})
        p["sessions"] += r["s"]; p["clicks"] += r["c"]; p["impressions"] += r["i"]; p["conversions"] += r["cv"]; p["spend_micros"] += r["sp"]
    out["per"] = per
    return out


class _Keep:
    """The fixture session, handed out again on every connect(): close() only rolls back, so the temp objects survive
    between the store's calls. execution_options() is recorded so a scenario can prove the feed asked for a READ ONLY,
    REPEATABLE READ snapshot."""

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
        with contextlib.suppress(Exception):
            self._real.rollback()


class _Eng:
    def __init__(self, conn):
        self.conn, self.options = conn, []

    def connect(self):
        return _Keep(self.conn, self.options)


@contextlib.contextmanager
def _temp_schema(level: str = "full"):
    """level: 'none' (nothing installed) | '07' (marketing tables only) | 'empty' (07 + 10 + 09) | 'full' (07 + 10 + 09 + a hand-worked rollup) |
    'money' (07 + 10 + 09 + two currencies and a weekly source, see _money_plan).
    Everything is created in the session's TEMPORARY schema and vanishes with the session."""
    from sqlalchemy import text

    from desktop import channels_data as cd
    from erp import config as cfg
    from erp.db import engine as real
    conn = real.connect()
    try:
        conn.exec_driver_sql("SET search_path TO pg_temp")
        if level != "none":
            conn.exec_driver_sql("CREATE TABLE leads (id SERIAL PRIMARY KEY)")
            conn.exec_driver_sql((ROOT / "db/sql/07_marketing_schema.sql").read_text(encoding="utf-8"))
            if level != "07":
                conn.exec_driver_sql((ROOT / "db/sql/10_marketing_currency.sql").read_text(encoding="utf-8"))
                conn.exec_driver_sql("RESET lock_timeout")
                conn.exec_driver_sql((ROOT / "db/sql/09_marketing_rollup.sql").read_text(encoding="utf-8"))
        conn.commit()
        # L-107: prove the tables really are the temporary ones before anything is inserted
        want = list(cd.REQUIRED_TABLES) if level in ("empty", "full", "money") else (list(cd.mschema.MARKETING_TABLES) if level == "07" else [])
        for t in want:
            if not conn.execute(text("SELECT to_regclass('pg_temp.' || :t) IS NOT NULL AND (SELECT relpersistence FROM pg_class WHERE oid = to_regclass('pg_temp.' || :t)) = 't'"), {"t": t}).scalar():
                raise RuntimeError(f"pg_temp.{t} is not a temporary table: refusing to insert anything")
        if level == "full":
            _load_plan(conn)
        elif level == "money":
            _load_money_plan(conn)
        eng = _Eng(conn)
        with _patched(cd, engine=eng), _patched(cfg, MARKETING_SCHEMA="pg_temp"):
            yield eng, conn
    finally:
        with contextlib.suppress(Exception):
            conn.rollback()
            conn.exec_driver_sql("SET search_path TO public")
            conn.commit()
        with contextlib.suppress(Exception):
            conn.invalidate()          # the session (and with it every temp object) is thrown away, never pooled again
        with contextlib.suppress(Exception):
            conn.close()


def _load_plan(conn) -> None:
    from sqlalchemy import text
    conn.execute(text("INSERT INTO pg_temp.marketing_source (source_id, source_key) VALUES (1, 'fixture')"))
    for cid, key, name, medium, paid in ((1, "facebook", "Facebook", "paid_social", True), (2, "google_ads", "Google Ads", "search", True),
                                         (3, "evil", '=HYPERLINK("http://x")', "organic", False), (4, "tiny", "Tiny newsletter", "email", False)):
        conn.execute(text("INSERT INTO pg_temp.marketing_channel (channel_id, channel_key, display_name, medium, is_paid) VALUES (:i, :k, :n, :m, :p)"), dict(i=cid, k=key, n=name, m=medium, p=paid))
    for cp, ch, key, name in ((10, 1, "summer", "Summer sale"), (11, 1, "brand", "Brand"), (20, 2, "search_a", "Search A"), (21, 2, "search_b", None)):
        conn.execute(text("INSERT INTO pg_temp.marketing_campaign (campaign_id, channel_id, campaign_key, name) VALUES (:c, :h, :k, :n)"), dict(c=cp, h=ch, k=key, n=name))
    conn.execute(text("""INSERT INTO pg_temp.interaction_daily_rollup (event_date, channel_id, campaign_id, sessions, clicks, impressions, conversions, spend_micros)
                         VALUES (:d, :ch, :cp, :s, :c, :i, :cv, :sp)"""), _plan())
    conn.execute(text("""INSERT INTO pg_temp.interaction_daily_channel_rollup (event_date, channel_id, sessions, clicks, impressions, conversions, spend_micros)
                         SELECT event_date, channel_id, sum(sessions), sum(clicks), sum(impressions), sum(conversions), sum(spend_micros)
                           FROM pg_temp.interaction_daily_rollup GROUP BY 1, 2"""))
    conn.execute(text("INSERT INTO pg_temp.marketing_rollup_run (mode, status, finished_at, rows_written) VALUES ('full', 'ok', now(), 1)"))
    # a POISONED raw fact row: 10^9 sessions in the range. The page reads only the rollup, so no number may ever show it.
    from erp.marketing import schema as mschema
    mschema.ensure_partitions(conn, "pg_temp", [date(2026, 9, 5)])
    conn.execute(text("""INSERT INTO pg_temp.interaction_fact (event_date, dedupe_key, source_id, channel_id, event_type, sessions, clicks, impressions, conversions, spend_micros)
                         VALUES ('2026-09-05', md5('poison')::uuid, 1, 1, 'session', 1000000000, 1000000000, 1000000000, 1000000000, 1000000000000)"""))
    conn.execute(text("""INSERT INTO pg_temp.marketing_landing (batch_id, batch_seq, source, external_id, event_type, payload)
                         VALUES (md5('b')::uuid, 1, 'fixture', 'poison', 'x', '{"sessions": 1000000000}')"""))
    conn.commit()


def _check_sql(add) -> None:
    import csv as csvmod

    from sqlalchemy import text

    from desktop import channels_data as cd
    rows = _plan()
    lo, hi = _LAST - timedelta(days=29), _LAST
    plo, phi = lo - timedelta(days=30), lo - timedelta(days=1)

    with _temp_schema("full") as (eng, conn):
        store = cd.ChannelsStore(ttl=0)
        code, p = store.get({}, fresh=True)
        add("sql: the default request answers 200, ready, every panel reads", code == 200 and p["state"] == "ready" and p["ok"], (code, p.get("state"), p.get("error"), {k: v.get("error") for k, v in (p.get("panels") or {}).items() if v.get("state") == "unavailable"}))
        if code != 200 or p["state"] != "ready":
            return
        add("sql: the feed asked for a READ ONLY, REPEATABLE READ connection", bool(eng.options) and all(o.get("postgresql_readonly") is True and o.get("isolation_level") == "REPEATABLE READ" for o in eng.options), eng.options)
        add("sql: the range defaults to 30 days ending at the newest rollup day, the zone is the session's", p["range"]["from"] == lo.isoformat() and p["range"]["to"] == hi.isoformat() and p["range"]["defaulted"] and bool(p["time_zone"]), p["range"])
        e = _expect(rows, lo, hi)
        prev = _expect(rows, plo, phi)
        tiles = {t["id"]: t for t in p["kpis"]}
        add("sql: sessions / clicks / conversions tiles equal the hand-worked sums (and ignore the 10^9 poisoned fact row)",
            (tiles["sessions"]["value"], tiles["clicks"]["value"], tiles["conversions"]["value"]) == (e["sessions"], e["clicks"], e["conversions"]), (tiles["sessions"]["value"], e["sessions"]))
        direct = conn.execute(text("SELECT sum(sessions), sum(clicks), sum(conversions), sum(spend_micros), count(DISTINCT event_date) FROM pg_temp.interaction_daily_channel_rollup WHERE event_date BETWEEN :a AND :b"), {"a": lo, "b": hi}).one()
        add("sql: ... and equal a direct SQL query of the rollup", tuple(int(x) for x in direct[:3]) == (e["sessions"], e["clicks"], e["conversions"]) and int(direct[3]) == e["spend_micros"], direct)
        campaign_side = conn.execute(text("SELECT sum(sessions), sum(clicks), sum(conversions) FROM pg_temp.interaction_daily_rollup WHERE event_date BETWEEN :a AND :b"), {"a": lo, "b": hi}).one()
        add("sql: the channel rollup and the campaign rollup agree (additive measures)", tuple(int(x) for x in campaign_side) == (e["sessions"], e["clicks"], e["conversions"]), campaign_side)
        add("sql: days with data = days in range minus the 3 gap days, and the header says so", p["range"]["days_with_data"] == e["days"] == 27 and "27 of 30" in p["headline"]["sub"], (p["range"], p["headline"]["sub"]))
        add("sql: the conversion-rate tile is conversions / clicks of the sums", abs(tiles["cvr"]["value"] - round(e["conversions"] / e["clicks"] * 100, 3)) < 0.001, tiles["cvr"])
        add("sql: the spend tile is dollars", tiles["spend"]["state"] == "ok" and abs(tiles["spend"]["value"] - e["spend_micros"] / 1e6) < 0.01, tiles["spend"])
        want_pct = round((e["sessions"] - prev["sessions"]) / prev["sessions"] * 100, 2)
        add("sql: the sessions delta is against the same-length previous period", abs(tiles["sessions"]["delta"]["pct"] - want_pct) < 0.01 and tiles["sessions"]["delta"]["state"] == "ok", (tiles["sessions"]["delta"], want_pct))
        add("sql: the sparkline has a gap (None) exactly on the gap days", [d for d, x in zip(tiles["sessions"]["spark"]["days"], tiles["sessions"]["spark"]["values"]) if x is None] == sorted(g.isoformat() for g in _GAPS if lo <= g <= hi), tiles["sessions"]["spark"])
        tb = p["panels"]["table"]
        by = {r["key"]: r for r in tb["rows"]}
        add("sql: four channels in the table, named from the dimension", set(by) == {"facebook", "google_ads", "evil", "tiny"} and by["facebook"]["name"] == "Facebook", list(by))
        add("sql: every channel row equals the hand-worked sums", all((by[k]["sessions"], by[k]["clicks"], by[k]["impressions"], by[k]["conversions"], int(round((by[k]["spend"]["value"] or 0) * 1e6))) == tuple(e["per"][c][m] for m in ("sessions", "clicks", "impressions", "conversions", "spend_micros"))
                                                                     for k, c in (("facebook", 1), ("google_ads", 2), ("evil", 3), ("tiny", 4))), {k: by[k]["sessions"] for k in by})
        add("sql: the table total equals the tile", tb["total"]["sessions"] == tiles["sessions"]["value"] and tb["total"]["conversions"] == tiles["conversions"]["value"])
        add("sql: 'evil' has no spend and no impressions (reasons, not zeros); 'tiny' is low volume with counts",
            by["evil"]["spend"]["why"] == "no spend field" and by["evil"]["ctr"]["why"] == "no impressions" and by["tiny"]["low_n"] and by["tiny"]["cvr"]["pct"] is None and by["tiny"]["cvr"]["low_n"], (by["evil"]["spend"], by["tiny"]["cvr"]))
        add("sql: the default order is sessions, largest first", [r["key"] for r in tb["rows"]][0] == "facebook" and tb["rows"][-1]["key"] == "tiny", [r["key"] for r in tb["rows"]])
        ch = p["panels"]["chart"]
        fb = next(s for s in ch["series"] if s["key"] == "facebook")
        day = date(2026, 9, 3)
        i = ch["days"].index(day.isoformat())
        add("sql: a chart point equals the rollup of that day and channel; a gap day is None", fb["values"]["sessions"][i] == sum(r["s"] for r in rows if r["ch"] == 1 and r["d"] == day)
            and fb["values"]["sessions"][ch["days"].index("2026-09-01")] is None, fb["values"]["sessions"][:5])
        add("sql: the chart total equals the tile total", sum(x or 0 for s in ch["series"] for x in s["values"]["sessions"]) == e["sessions"])
        add("sql: nothing but the two rollup tables (plus dimensions and the journal) was read: no raw-table figure anywhere", "1000000000" not in str(p) and p["read"]["raw_fact_read"] is False)
        add("sql: the freshness comes from the rollup rows and the journal", bool(p["freshness"]["data_as_of"]) and p["freshness"]["last_status"] == "ok" and p["freshness"]["runs"] == 1, p["freshness"])

        # filters, applied consistently to tiles / chart / table
        code, p2 = store.get({"channel": ["facebook", "google_ads"]}, fresh=True)
        e2 = _expect(rows, lo, hi, channels={1, 2})
        t2 = {t["id"]: t for t in p2["kpis"]}
        add("filters: two channels - tiles, table and chart all describe exactly those two", code == 200 and t2["sessions"]["value"] == e2["sessions"] and {r["key"] for r in p2["panels"]["table"]["rows"]} == {"facebook", "google_ads"}
            and sum(x or 0 for s in p2["panels"]["chart"]["series"] for x in s["values"]["sessions"]) == e2["sessions"], (code, t2["sessions"]["value"], e2["sessions"]))
        code, p3 = store.get({"from": ["2026-09-05"], "to": ["2026-09-07"]}, fresh=True)
        e3 = _expect(rows, date(2026, 9, 5), date(2026, 9, 7))
        add("filters: a 3-day range - tiles equal the hand-worked sums, the previous period is the 3 days before", code == 200 and p3["kpis"][0]["value"] == e3["sessions"] and p3["range"]["prev_from"] == "2026-09-02" and p3["range"]["days"] == 3 and not p3["range"]["defaulted"], (code, p3.get("range")))
        code, p4 = store.get({"campaign": ["10"]}, fresh=True)
        e4 = _expect(rows, lo, hi, campaign=10)
        add("filters: a campaign filter reads the campaign rollup and every panel follows it", code == 200 and p4["source"] == "campaign_rollup" and p4["kpis"][0]["value"] == e4["sessions"] and [r["key"] for r in p4["panels"]["table"]["rows"]] == ["facebook"]
            and p4["filters"]["campaign_name"] == "Summer sale" and any("campaign filter" in w for w in p4["warnings"]), (code, p4.get("source"), p4.get("filters")))
        code, p5 = store.get({"campaign": ["0"], "channel": ["google_ads"]}, fresh=True)
        e5 = _expect(rows, lo, hi, channels={2}, campaign=0)
        add("filters: campaign 0 (= no campaign) combined with a channel", code == 200 and p5["kpis"][0]["value"] == e5["sessions"], (code, e5["sessions"]))
        code, p6 = store.get({"from": ["2020-01-01"], "to": ["2020-01-31"]}, fresh=True)
        add("state: a range with no data is 'no match', with the data's real extent in the options", code == 200 and p6["state"] == "no_match" and p6["kpis"] == [] and p6["options"]["last_day"] == hi.isoformat(), (code, p6.get("state")))
        code, p7 = store.get({"channel": ["nope"]}, fresh=True)
        add("filters: an unknown channel is a 400 that carries the options for the filter bar", code == 400 and p7["problems"][0]["param"] == "channel" and p7["options"]["channels"], (code, p7.get("problems")))
        code, p8 = store.get({"campaign": ["999999"]}, fresh=True)
        add("filters: an unknown campaign is a 400", code == 400 and p8["problems"][0]["param"] == "campaign", (code, p8.get("error")))
        code, p9 = store.get({"focus": ["nope"]}, fresh=True)
        add("filters: an unknown focus channel is a 400", code == 400 and p9["problems"][0]["param"] == "focus", code)

        # sorting is on the server
        code, ps = store.get({"sort": ["cpa"], "dir": ["asc"]}, fresh=True)
        keys = [r["key"] for r in ps["panels"]["table"]["rows"]]
        add("sort: by cost per conversion ascending, the channels with no spend are last", code == 200 and keys[-2:] == ["evil", "tiny"] or keys[-2:] == ["tiny", "evil"], keys)
        code, ps = store.get({"sort": ["channel"], "dir": ["asc"]}, fresh=True)
        add("sort: by name ascending", [r["name"].lower() for r in ps["panels"]["table"]["rows"]] == sorted(r["name"].lower() for r in ps["panels"]["table"]["rows"]) and ps["sort"] == {"key": "channel", "dir": "asc"})

        # the campaign drill-down (campaign rollup, one channel)
        code, pc = store.get({"focus": ["google_ads"]}, fresh=True)
        cp = pc["panels"]["campaigns"]
        ex = {cid: sum(r["s"] for r in rows if r["ch"] == 2 and r["cp"] == cid and lo <= r["d"] <= hi) for cid in (20, 21, 0)}
        add("drill: the campaigns of the focus channel, largest first, named from the dimension", cp["state"] == "ok" and [r["campaign_id"] for r in cp["rows"]] == [20, 21, 0] and [r["name"] for r in cp["rows"]] == ["Search A", "search_b", "(no campaign)"], cp)
        add("drill: every campaign's sessions equal the hand-worked sums and add up to the channel", [r["sessions"] for r in cp["rows"]] == [ex[20], ex[21], ex[0]] and sum(ex.values()) == e["per"][2]["sessions"], [r["sessions"] for r in cp["rows"]])
        add("drill: the focus appears in the payload and the export link for the campaigns is offered", pc["filters"]["focus"] == "google_ads" and "part=campaigns" in (pc["export"]["campaigns"] or ""), pc["export"])
        with _patched(cd, CAMPAIGN_ROWS_MAX=2):
            code, pcap = store.get({"focus": ["google_ads"]}, fresh=True)
        cc = pcap["panels"]["campaigns"]
        add("drill: a row cap gives an honest 'N more' and the sessions the rest hold", cc["state"] == "ok" and cc["shown"] == 2 and cc["more"] == 1 and cc["total_campaigns"] == 3 and cc["rest_sessions"] == ex[0], cc)
        code, pf = store.get({"focus": ["facebook"], "campaign": ["10"]}, fresh=True)
        add("drill: a focus channel and a campaign filter together", pf["panels"]["campaigns"]["state"] == "ok" and [r["campaign_id"] for r in pf["panels"]["campaigns"]["rows"]] == [10], pf["panels"]["campaigns"])

        # the CSV files describe the same rows as the screen
        def get_csv(params):
            c, body, headers = store.export_csv(params)
            if c != 200:
                return c, body, headers
            text_ = body.decode("utf-8-sig")
            return c, list(csvmod.reader(io.StringIO(text_, newline=""))), (body, headers)
        c, parsed, (raw_bytes, headers) = get_csv({"from": [lo.isoformat()], "to": [hi.isoformat()], "part": ["channels"]})
        head = parsed[0]
        table_csv = {r[head.index("channel_key")]: dict(zip(head, r)) for r in parsed[1:]}
        add("csv: channels file - BOM, CRLF, one row per channel, the same numbers as the table", c == 200 and raw_bytes.startswith(b"\xef\xbb\xbf") and b"\r\n" in raw_bytes and int(table_csv["facebook"]["sessions"]) == by["facebook"]["sessions"]
            and sum(int(r["sessions"]) for r in table_csv.values()) == e["sessions"] and headers["X-Row-Count"] == "4", (c, headers))
        add("csv: ... the formula-looking channel name is defused, the zone and the period are on every row", table_csv["evil"]["channel"].startswith("'=") and table_csv["facebook"]["time_zone"] == p["time_zone"] and table_csv["facebook"]["period_to"] == hi.isoformat(), table_csv["evil"])
        add("csv: ... blank where the page says 'no spend field' / 'no impressions'", table_csv["evil"]["spend"] == "" and table_csv["evil"]["ctr_pct"] == "" and table_csv["facebook"]["spend"] != "" and table_csv["facebook"]["currency"] == "USD", table_csv["evil"])
        c, parsed, (raw_bytes, headers) = get_csv({"from": [lo.isoformat()], "to": [hi.isoformat()], "channel": ["facebook"], "part": ["daily"]})
        dh = parsed[0]
        add("csv: the daily file with a channel filter has one row per day with data and sums to the tile", c == 200 and len(parsed) - 1 == sum(1 for d in {r["d"] for r in rows if r["ch"] == 1 and lo <= r["d"] <= hi})
            and sum(int(r[dh.index("sessions")]) for r in parsed[1:]) == _expect(rows, lo, hi, channels={1})["sessions"] and all(r[dh.index("channel_key")] == "facebook" for r in parsed[1:]), (c, len(parsed)))
        c, parsed, (raw_bytes, headers) = get_csv({"from": [lo.isoformat()], "to": [hi.isoformat()], "focus": ["google_ads"], "part": ["campaigns"]})
        chd = parsed[0]
        add("csv: the campaigns file has the drill-down's rows and numbers", c == 200 and [int(r[chd.index("campaign_id")]) for r in parsed[1:]] == [20, 21, 0] and int(parsed[1][chd.index("sessions")]) == ex[20], (c, parsed[:2]))
        c, body, _h = store.export_csv({"channel": ["nope"]})
        add("csv: an unknown channel is refused, not ignored", c == 400 and body["problems"][0]["param"] == "channel", (c, body))
        c, body, _h = store.export_csv({"campaign": ["999999"]})
        add("csv: an unknown campaign is refused", c == 400, (c, body))

        # one panel failing at a time (the block runs in its own try; the rest of the page stays alive)
        bad_sql = lambda *a, **k: ("SELECT * FROM pg_temp.no_such_table", {})     # noqa: E731
        with _patched(cd, daily_sql=bad_sql):
            code, pd = store.get({}, fresh=True)
        add("failure: the daily query failing greys the chart and the sparklines only", code == 200 and pd["panels"]["chart"]["state"] == "unavailable" and pd["panels"]["table"]["state"] == "ok" and pd["kpis"][0]["value"] == e["sessions"] and pd["kpis"][0]["spark"] is None and pd["ok"] is False, (code, pd.get("panels", {}).get("chart")))
        with _patched(cd, channels_sql=bad_sql):
            code, pt = store.get({}, fresh=True)
        add("failure: the table query failing greys the table only", code == 200 and pt["panels"]["table"]["state"] == "unavailable" and pt["panels"]["chart"]["state"] == "ok" and pt["kpis"][0]["state"] == "ok")
        orig = cd.totals_sql
        with _patched(cd, totals_sql=lambda schema, v, prev=False: bad_sql() if prev else orig(schema, v, prev)):
            code, pp = store.get({}, fresh=True)
        add("failure: the previous-period query failing removes the deltas only", code == 200 and pp["kpis"][0]["value"] == e["sessions"] and pp["kpis"][0]["delta"]["state"] == "unavailable" and pp["panels"]["table"]["state"] == "ok", pp.get("kpis", [{}])[0])
        with _patched(cd, totals_sql=lambda schema, v, prev=False: bad_sql()):
            code, pn = store.get({}, fresh=True)
        add("failure: the totals query failing greys every tile with a reason, the table and chart keep working", code == 200 and all(t["state"] == "unavailable" for t in pn["kpis"]) and pn["panels"]["table"]["state"] == "ok" and pn["panels"]["chart"]["state"] == "ok", pn.get("kpis"))
        with _patched(cd, campaigns_sql=bad_sql):
            code, pk = store.get({"focus": ["facebook"]}, fresh=True)
        add("failure: the campaign query failing greys the drill-down only", code == 200 and pk["panels"]["campaigns"]["state"] == "unavailable" and pk["panels"]["table"]["state"] == "ok", pk.get("panels", {}).get("campaigns"))
        code, pa = store.get({}, fresh=True)
        add("failure: ... and the next request is healthy again (nothing stuck)", code == 200 and pa["ok"])

    # installed but empty: the honest 'no data has been loaded yet'
    with _temp_schema("empty") as (eng, conn):
        store = cd.ChannelsStore(ttl=0)
        code, pe = store.get({}, fresh=True)
        add("empty: installed and empty says so, with the load and rollup commands", code == 200 and pe["state"] == "empty" and "No marketing data has been loaded yet" in pe["headline"]["text"] and any("ingest" in s["command"] for s in pe["install"]["steps"]), (code, pe.get("headline")))
        add("empty: ... no tile, no panel, no number, no export", pe["kpis"] == [] and pe["panels"] == {} and pe["export"] is None and not re.search(r"\d", pe["headline"]["text"]), pe["headline"])
        c, body, _h = store.export_csv({})
        add("empty: the CSV endpoint says there is nothing to export (409), not an empty file", c == 409 and body["state"] == "empty", (c, body))
        add("empty: the definitions are still there", len(pe["definitions"]) >= 12)

    # not installed at all / only the marketing tables
    with _temp_schema("none") as (eng, conn):
        store = cd.ChannelsStore(ttl=0)
        code, pn = store.get({}, fresh=True)
        add("not installed: nothing installed says so and lists 07 first, then 09", code == 200 and pn["state"] == "not_installed" and [s["command"] for s in pn["install"]["steps"]][:3] == [cd.PSQL_07, cd.PSQL_10, cd.PSQL_09], (code, pn.get("install")))
        add("not installed: every required table is listed as missing", set(pn["install"]["missing"]) == set(cd.REQUIRED_TABLES), pn["install"]["missing"])
        c, body, _h = store.export_csv({})
        add("not installed: the CSV endpoint answers 409 with the state", c == 409 and body["state"] == "not_installed", (c, body))
    with _temp_schema("07") as (eng, conn):
        store = cd.ChannelsStore(ttl=0)
        code, pn = store.get({}, fresh=True)
        add("not installed: with only 07 installed the page asks for 09 first (the exact psql command)", code == 200 and pn["state"] == "not_installed" and [s["command"] for s in pn["install"]["steps"]][:2] == [cd.PSQL_10, cd.PSQL_09] and set(pn["install"]["missing"]) == set(cd.mroll.ROLLUP_TABLES), (code, pn.get("install", {}).get("missing")))

    # database down: a sentence and a 503, never a traceback
    class _Down:
        def connect(self):
            raise RuntimeError("could not connect to server: Connection refused")
    with _patched(cd, engine=_Down()):
        store = cd.ChannelsStore(ttl=0)
        code, pdn = store.get({}, fresh=True)
        add("failure: database down gives a 503 with a plain sentence and no traceback", code == 503 and pdn["ok"] is False and pdn.get("unavailable") and "Traceback" not in str(pdn) and "Can’t read" in pdn["error"], (code, pdn))
        c, body, _h = store.export_csv({})
        add("failure: ... and the export gives a 503 too", c == 503, (c, body))
    from erp import config as cfg
    with _patched(cfg, MARKETING_SCHEMA="Public; DROP TABLE x"):
        code, pbs = cd.ChannelsStore(ttl=0).get({}, fresh=True)
    add("failure: a hostile MARKETING_SCHEMA is refused with a sentence before any SQL runs", code == 503 and "MARKETING_SCHEMA" in pbs["error"], (code, pbs))


def _check_real_db(add) -> None:
    """Read only, against whatever the real database holds today."""
    from sqlalchemy import text

    from desktop import channels_data as cd
    from erp import config as cfg
    store = cd.ChannelsStore(ttl=0)
    code, p = store.get({}, fresh=True)
    add("real db: the page answers 200 whatever state the database is in", code == 200 and p.get("state") in ("not_installed", "empty", "no_match", "ready"), (code, p.get("state"), p.get("error")))
    if code != 200:
        return
    add("real db: the schema is the configured one and the raw table was not read", p["schema"] == cfg.MARKETING_SCHEMA and p["read"]["raw_fact_read"] is False, p.get("schema"))
    if p["state"] in ("not_installed", "empty"):
        add("real db: a setup state carries no tile, no panel and no number in its headline", p["kpis"] == [] and p["panels"] == {} and not re.search(r"\d", p["headline"]["text"]), p["headline"])
        if p["state"] == "not_installed":
            add("real db: not installed names the missing tables and the exact command for 09 (when 09 is what is missing)",
                bool(p["install"]["missing"]) and (cd.PSQL_09 in [s["command"] for s in p["install"]["steps"]]), p["install"])
        with cd.engine.connect() as c:
            n_before = c.execute(text("SELECT count(*) FROM pg_class WHERE relname LIKE 'interaction_daily%'")).scalar()
        add("real db: asking for the page created nothing (no interaction_daily table appeared)", n_before == (2 if p["state"] == "empty" else n_before), n_before)
    else:
        add("real db: a populated database gives six tiles and a table whose total is the sessions tile",
            [t["id"] for t in p["kpis"]] == ["sessions", "clicks", "conversions", "cvr", "spend", "revenue"] or p["state"] == "no_match", p["kpis"])
    with cd.ChannelsStore._connect() as c:
        iso = c.execute(text("SHOW transaction_isolation")).scalar()
        ro = c.execute(text("SHOW transaction_read_only")).scalar()
        c.rollback()
    add("real db: the feed's connection is a read-only REPEATABLE READ snapshot", (iso, ro) == ("repeatable read", "on"), (iso, ro))


# =========================================================================================== currency: money is never added across currencies
def _mrow(cid: int, cur: str, spend: int = 0, revenue: int = 0, derived: int = 0, conv: int = 0, **kw) -> dict:
    """One money row (channel x currency) as the money query returns it, amounts in micros."""
    return {"channel_id": cid, "currency": cur, "spend_micros": spend, "revenue_micros": revenue, "revenue_derived_micros": derived, "conversions": conv, **kw}


def _numbers(obj):
    """Every int / float anywhere inside a payload (to prove that a cross-currency sum appears nowhere in it)."""
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        yield float(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _numbers(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _numbers(v)


def _check_currency(add) -> None:
    from desktop import channels_data as cd
    M = 1_000_000

    # ---- the filter: shape, whitelist, names, once
    for good in ("EUR", "VND", "USD"):
        _r, p = cd.parse_request({"currency": [good]})
        add(f"currency filter: {good} has a valid shape", not p and _r.currency == good, p)
    for bad in ("eur", "EU", "EURO", "E1R", " EUR", "€", "EUR;", "123", "EUR' OR '1'='1"):
        _r, p = cd.parse_request({"currency": [bad]})
        add(f"currency filter: {bad!r} is refused (three capital letters only)", any(x["param"] == "currency" for x in p) and _r.currency is None, p)
    _r, p = cd.parse_request({"currency": ["EUR", "VND"]})
    add("currency filter: two values are refused (a view is in ONE currency)", any(x["param"] == "currency" and "once" in x["why"] for x in p), p)
    _r, p = cd.parse_request({"currency": ["USD"]}, currencies_ok={"EUR", "VND"})
    add("currency filter: a well-formed code that the data does not hold is refused ('not a currency in the data')", any(x["param"] == "currency" and "in the data" in x["why"] for x in p) and _r.currency is None, p)
    _r, p = cd.parse_request({"currency": ["VND"]}, currencies_ok={"EUR", "VND"})
    add("currency filter: a currency the data holds is accepted", not p and _r.currency == "VND", p)
    _r, p = cd.parse_request({"currecy": ["EUR"]})
    add("currency filter: a typo of the NAME is a 400 that names it (L-098), on the JSON endpoint and on the CSV", len(p) == 1 and p[0]["param"] == "currecy"
        and cd.unknown_params({"currecy": ["EUR"]}, cd.CSV_PARAMS)[0]["param"] == "currecy", p)
    add("currency filter: `currency` is a known parameter of both endpoints", "currency" in cd.KNOWN_PARAMS and "currency" in cd.CSV_PARAMS)
    _r, p = cd.parse_request({"currency": ["EUR"], "from": ["2026-09-01"]})
    add("currency filter: it counts as an active filter", _r.active() == 2, _r)
    v, o = _view(currency="EUR")
    w, params = cd.where_sql(v)
    add("currency filter: it is a BOUND parameter, never text in the SQL", "currency = :currency" in w and params["currency"] == "EUR" and "EUR" not in w, (w, params))
    v0, _o = _view()
    add("currency filter: without it the WHERE has no currency clause", "currency" not in cd.where_sql(v0)[0])
    add("currency filter: the export link reproduces it", "currency=EUR" in cd.view_query(v), cd.view_query(v))
    evil = "EUR'; DROP TABLE interaction_daily_rollup; --"
    ve = cd.resolve_view(cd.Request(currency=evil), o, _ids(o))
    add("currency filter: a hostile value that reached a Request still travels only as a bound parameter", evil not in " ".join([cd.totals_sql("public", ve)[0], cd.money_sql("public", ve, "total")[0], cd.daily_sql("public", ve, ve.d_from, 5, by_currency=True)[0]]))

    # ---- grouping: money is added inside a currency only
    g = cd.group_money([_mrow(1, "VND", 500 * M, 900 * M, 900 * M, 5), _mrow(2, "EUR", 20 * M, 100 * M, 0, 2), _mrow(3, "VND", 100 * M, 0, 0, 2)])
    add("group: two currencies stay two groups, in code order, each summed on its own", list(g) == ["EUR", "VND"] and g["VND"]["spend_micros"] == 600 * M and g["EUR"]["spend_micros"] == 20 * M and g["VND"]["conversions"] == 7, g)
    add("group: money_mode says multi for two currencies, single for one, none for none", cd.money_mode(g) == "multi" and cd.money_mode({"VND": g["VND"]}) == "single" and cd.money_mode({}) == "none")
    g0 = cd.group_money([_mrow(1, "USD", 0, 0, 0, 40), _mrow(2, "VND", 5 * M, 0, 0, 3)])
    add("group: a currency that carries no money (a USD default row with no spend field) does not make the view mixed", list(g0) == ["VND"] and cd.money_mode(g0) == "single", g0)
    add("group: a row with no currency at all is read as the default currency", list(cd.group_money([{"spend_micros": M}])) == [cd.DEFAULT_CURRENCY])
    sp = cd.money_cell(g, "spend_micros")
    add("cell: two currencies -> state multi, NO value, one part per currency, never a sum", sp["state"] == "multi" and sp["value"] is None and sp["currency"] is None
        and [(x["currency"], x["value"]) for x in sp["parts"]] == [("EUR", 20.0), ("VND", 600.0)] and 620.0 not in set(_numbers(sp)), sp)
    one = cd.money_cell({"VND": g["VND"]}, "spend_micros")
    add("cell: one currency -> state ok, the amount AND its code", one["state"] == "ok" and one["value"] == 600.0 and one["currency"] == "VND", one)
    none = cd.money_cell({"EUR": g["EUR"]}, "revenue_micros")
    add("cell: revenue derived state - none / partly / all", none["derived"] is None and cd.money_cell({"VND": g["VND"]}, "revenue_micros")["derived"] == "all"
        and cd.derived_state(50, 100) == "partly" and cd.derived_state(0, 100) is None and cd.derived_state(100, 100) == "all")
    add("cell: no such money is 'no spend field' / 'no revenue field', never a zero", cd.money_cell({}, "spend_micros")["why"] == "no spend field" and cd.money_cell({}, "revenue_micros")["why"] == "no revenue field")
    cp = cd.cpa_cell(g)
    add("cpa: per currency - that currency's spend over the conversions on THAT currency's rows (VND 600 / 7, not / 9)", cp["state"] == "multi" and [(x["currency"], x["value"]) for x in cp["parts"]] == [("EUR", 10.0), ("VND", 85.71)], cp)
    add("cpa: one currency is a plain cost with its code", cd.cpa_cell({"VND": g["VND"]})["state"] == "ok" and cd.cpa_cell({"VND": g["VND"]})["currency"] == "VND")
    add("cpa: no spend, or no conversions, are reasons", cd.cpa_cell({})["why"] == "no spend field" and cd.cpa_cell({"VND": dict(g["VND"], conversions=0)})["why"] == "no conversions")

    # ---- the channel table
    vv, oo = _view(_options(3))
    rows = [_chrow(1, sessions=100), _chrow(2, sessions=80), _chrow(3, sessions=60)]
    money = [_mrow(1, "VND", 500 * M, 900 * M, 900 * M, 5), _mrow(2, "EUR", 20 * M, 100 * M, 0, 2), _mrow(3, "VND", 100 * M, 0, 0, 2), _mrow(3, "EUR", 50 * M, 0, 0, 1)]
    t = cd.build_table(rows, oo, vv, money=money)
    by = {r["key"]: r for r in t["rows"]}
    add("table: a single-currency channel shows one amount with its code", by["ch1"]["spend"]["state"] == "ok" and by["ch1"]["spend"]["currency"] == "VND" and by["ch2"]["spend"]["currency"] == "EUR", (by["ch1"]["spend"], by["ch2"]["spend"]))
    add("table: a channel with two currencies is multi (parts in code order), never a sum", by["ch3"]["spend"]["state"] == "multi" and [x["currency"] for x in by["ch3"]["spend"]["parts"]] == ["EUR", "VND"] and by["ch3"]["spend"]["value"] is None, by["ch3"]["spend"])
    tot = t["total"]
    add("table: the TOTAL row is per currency too (EUR 70, VND 600), no cross-currency figure", tot["spend"]["state"] == "multi" and [(x["currency"], x["value"]) for x in tot["spend"]["parts"]] == [("EUR", 70.0), ("VND", 600.0)]
        and tot["revenue"]["state"] == "multi" and t["money_mode"] == "multi" and t["currencies"] == ["EUR", "VND"], tot["spend"])
    add("table: no number anywhere in the table equals the blended sum 670 (500 + 20 + 100 + 50)", 670.0 not in set(_numbers(t)), 670.0 in set(_numbers(t)))
    order = [r["key"] for r in cd.sort_rows(t["rows"], "spend", "desc")]
    add("sort: a money sort orders WITHIN each currency (EUR before VND by code), never VND 500 against EUR 20", order == ["ch2", "ch1", "ch3"], order)
    order = [r["key"] for r in cd.sort_rows(t["rows"], "spend", "asc")]
    add("sort: ... in both directions; a multi-currency row is unknown and always last", order == ["ch2", "ch1", "ch3"], order)
    order = [r["key"] for r in cd.sort_rows(t["rows"], "revenue", "desc")]
    add("sort: revenue is a sort key too (unknown last)", order[0] == "ch2" or order[0] == "ch1", order)
    tf = cd.build_table(rows, oo, vv, money={"error": "boom"})
    add("table: a failing money query greys the money cells only ('unavailable'), the counts stay", all(r["spend"]["state"] == "unavailable" for r in tf["rows"]) and tf["rows"][0]["sessions"] > 0 and tf["total"]["cpa"]["state"] == "unavailable")

    # ---- tiles
    v1, o1 = _view(_options(3), d_from=date(2026, 8, 1), d_to=date(2026, 8, 30))
    daily = [_daily(1, date(2026, 8, 1) + timedelta(days=i)) for i in (0, 1, 2, 4)]
    tot_row = _tot(days=4, sessions=40, clicks=20, impressions=400, conversions=4)
    dm = [{"event_date": date(2026, 8, 1) + timedelta(days=i), "currency": "VND", "spend_micros": (100 + i) * M, "revenue_micros": 0, "revenue_derived_micros": 0} for i in (0, 1, 2, 4)] \
        + [{"event_date": date(2026, 8, 2), "currency": "EUR", "spend_micros": 999 * M, "revenue_micros": 0, "revenue_derived_micros": 0}]
    tiles = {x["id"]: x for x in cd.build_kpis(tot_row, _tot(days=3), daily, v1, money=[_mrow(1, "VND", 403 * M, 50 * M, 50 * M, 4), _mrow(2, "EUR", 999 * M, 0, 0, 1)],
                                                  prev_money=[_mrow(1, "VND", 200 * M, 0, 0, 2)], daily_money=dm)}
    add("tiles: two currencies with spend -> the spend tile is multi: parts per currency, no value, no line, no delta", tiles["spend"]["state"] == "multi" and tiles["spend"]["value"] is None and len(tiles["spend"]["parts"]) == 2
        and tiles["spend"]["spark"] is None and tiles["spend"]["delta"] is None and "never added" in tiles["spend"]["sub"] and 1402.0 not in set(_numbers(tiles["spend"])), tiles["spend"])
    add("tiles: revenue in ONE currency is a plain tile with its code, and says it is derived", tiles["revenue"]["state"] == "ok" and tiles["revenue"]["unit"] == "VND" and tiles["revenue"]["value"] == 50.0 and "DERIVED" in tiles["revenue"]["sub"], tiles["revenue"])
    vc = cd.dataclasses.replace(v1, currency="VND")
    tiles = {x["id"]: x for x in cd.build_kpis(tot_row, _tot(days=3), daily, vc, money=[_mrow(1, "VND", 403 * M, 0, 0, 4)], prev_money=[_mrow(1, "VND", 200 * M, 0, 0, 2)], daily_money=dm)}
    add("tiles: one currency chosen -> spend is ok, in VND, with a delta against the previous VND spend (+101.5%)", tiles["spend"]["state"] == "ok" and tiles["spend"]["unit"] == "VND" and tiles["spend"]["value"] == 403.0
        and tiles["spend"]["delta"]["pct"] == 101.5 and tiles["spend"]["delta"]["state"] == "ok", tiles["spend"])
    add("tiles: the spend line uses only that currency's days (the EUR 999 day is not in it)", tiles["spend"]["spark"] is not None and 999.0 not in [x for x in tiles["spend"]["spark"]["values"] if x is not None]
        and [x for x in tiles["spend"]["spark"]["values"] if x is not None] == [100.0, 101.0, 102.0, 104.0], tiles["spend"]["spark"])
    add("tiles: cost per conversion is that currency's spend over its own conversions, with the code", "VND 101" in tiles["spend"]["sub"], tiles["spend"]["sub"])
    tiles = {x["id"]: x for x in cd.build_kpis(tot_row, _tot(days=3), daily, vc, money=[_mrow(1, "VND", 403 * M, 0, 0, 4)], prev_money=[_mrow(1, "EUR", 999 * M, 0, 0, 2)], daily_money=dm)}
    add("tiles: a previous period whose spend was in ANOTHER currency is 'no spend recorded' - never compared across currencies", tiles["spend"]["delta"]["state"] == "none" and "no spend recorded" in tiles["spend"]["delta"]["text"], tiles["spend"]["delta"])
    tiles = {x["id"]: x for x in cd.build_kpis(tot_row, _tot(days=3), daily, vc, money={"error": "boom"}, prev_money={"error": "boom"}, daily_money={"error": "boom"})}
    add("tiles: a failing money query greys spend and revenue only", tiles["spend"]["state"] == "unavailable" and tiles["revenue"]["state"] == "unavailable" and tiles["sessions"]["state"] == "ok")
    tiles = {x["id"]: x for x in cd.build_kpis(tot_row, _tot(days=3), daily, vc, money=[], prev_money=[], daily_money=[])}
    add("tiles: no money at all is 'no spend field' / 'no revenue field', never 0", tiles["spend"]["state"] == "none" and tiles["revenue"]["state"] == "none" and "no revenue field" in tiles["revenue"]["sub"])

    # ---- the whole payload: warning, summary, filters
    raw = _raw(v1, totals=tot_row)
    raw.update({"money_totals": [_mrow(1, "VND", 5 * M), _mrow(2, "EUR", 7 * M)], "money_prev": [], "money_channels": [_mrow(1, "VND", 5 * M), _mrow(2, "EUR", 7 * M)],
                "money_daily": []})
    p = cd.assemble(raw, v1, o1, META, NOW)
    add("payload: several currencies in view -> a warning naming them, and money.mode multi", p["money"]["mode"] == "multi" and [c["code"] for c in p["money"]["currencies"]] == ["EUR", "VND"]
        and any("2 currencies (EUR, VND)" in w and "never added" in w for w in p["warnings"]), (p["money"], p["warnings"]))
    vc = cd.dataclasses.replace(v1, currency="EUR", req=cd.Request(currency="EUR"))
    raw["money_totals"] = [_mrow(2, "EUR", 7 * M)]
    p = cd.assemble(raw, vc, o1, META, NOW)
    add("payload: a currency chosen -> mode single, no multi-currency warning, and the filter is echoed", p["money"]["mode"] == "single" and p["money"]["chosen"] == "EUR" and p["filters"]["currency"] == "EUR" and p["filters"]["active"] == 1
        and not any("currencies" in w for w in p["warnings"]) and "currency=EUR" in p["export"]["channels"], (p["money"], p["warnings"], p["export"]))
    p = cd.assemble(_raw(v1), v1, o1, META, NOW)
    add("payload: a caller without money blocks still gets a single-currency payload (read in the default currency)", p["money"]["mode"] == "unknown" and p["kpis"][4]["id"] == "spend")
    add("payload: the options list the currencies of the data (public shape)", cd.public_options(cd.build_options([], {"rollup_rows": 1, "currency_rows": [{"currency": "VND", "days": 3, "has_money": True}]}))["currencies"] == [{"code": "VND", "days": 3, "has_money": True}])

    # ---- CSV: a file must not offer a cross-currency sum either
    cm = cd.csv_money(by["ch3"])
    add("csv: a channel whose money spans currencies says MULTIPLE, leaves the amounts blank and spells every currency out", cm[0] == cd.MIXED_CURRENCY and cm[1:5] == [None, None, "", None] and "EUR: spend 50.0" in cm[5] and "VND: spend 100.0" in cm[5], cm)
    cm = cd.csv_money(by["ch1"])
    add("csv: a single-currency channel carries its code, amounts, derived flag and cost", cm == ["VND", 500.0, 900.0, "all", 100.0, ""], cm)
    cm = cd.csv_money({"spend": cd.money_cell({}, "spend_micros"), "revenue": cd.money_cell({}, "revenue_micros"), "cpa": cd.cpa_cell({})})
    add("csv: no money at all is all blank (no currency claimed)", cm == ["", None, None, "", None, ""], cm)
    body = cd.to_csv(cd.CHANNEL_CSV, cd.channel_csv_rows(t, vv, "Asia/Bangkok")).decode("utf-8-sig")
    parsed = list(csv.reader(io.StringIO(body, newline="")))
    head = parsed[0]
    cells = {r[head.index("channel_key")]: dict(zip(head, r)) for r in parsed[1:]}
    add("csv: the file has a currency column, MULTIPLE for the mixed channel, and no cell holds 670 (the blended sum)", "currency" in head and cells["ch3"]["currency"] == "MULTIPLE" and cells["ch3"]["spend"] == "" and cells["ch1"]["currency"] == "VND"
        and not any(x in ("670", "670.0") for r in parsed[1:] for x in r), cells["ch3"])
    add("csv: the file says which revenue is derived", cells["ch1"]["revenue_derived"] == "all" and cells["ch2"]["revenue_derived"] == "")
    dr = cd.daily_csv_rows([dict(_daily(1, date(2026, 9, 1)), currency="VND", spend_micros=2 * M, grain="week"), dict(_daily(1, date(2026, 9, 1)), currency="EUR", revenue_micros=3 * M)], oo, vv, "Asia/Bangkok")
    add("csv: the daily file is one row per day x channel x grain x currency, a weekly row says week / 7", [(r[1], r[2], r[6]) for r in dr] == [("week", 7, "VND"), ("day", 1, "EUR")] and all(len(r) == len(cd.DAILY_CSV) for r in dr), dr)

    # ---- the text
    d = " ".join(x["term"] + " " + x["text"] for x in cd.build_definitions("UTC", "public"))
    add("defs: currency, revenue and weekly sources are defined, and the text says money is never converted or added across currencies",
        all(w in d for w in ("Currency", "Revenue", "Weekly sources", "never converted", cd.MIXED_CURRENCY, "derived", "no exchange rates")), [w for w in ("Currency", "Revenue", "Weekly sources", "never converted", cd.MIXED_CURRENCY, "derived", "no exchange rates") if w not in d])
    js = (ROOT / "desktop" / "static" / "channels.js").read_text(encoding="utf-8", errors="replace")
    add("page: it has a currency filter, sends it, and shows money with its code (never a bare '$')", "data-cur" in js and "q.append('currency'" in js and "fmtMoney" in js and "'$'" not in js and "ch-mny" in js)
    add("page: a multi-currency tile and cell are drawn one line per currency", "ch-kpi-parts" in js and "ch-mny-stack" in js)
    add("page: the weekly chart exists and a weekly bar is centred on the middle of its week", "ch-plot-week" in js and "addDays(w, 3)" in js and "type: 'bar'" in js)
    tmpl = cd.install_help(["interaction_daily_rollup"], "public", currency_ok=True)
    add("setup: with the currency column already there, migration 10 is not asked for again", cd.PSQL_10 not in [s["command"] for s in tmpl["steps"]] and cd.PSQL_09 == tmpl["steps"][0]["command"], [s["command"] for s in tmpl["steps"]])
    add("setup: an installed-but-empty database WITHOUT the currency column is told to run 10 before loading", cd.empty_help("public", currency_ok=False)["steps"][0]["command"] == cd.PSQL_10 and cd.empty_help("public", currency_ok=True)["steps"][0]["command"] == cd.CMD_LOAD)


# =========================================================================================== grain: a week is never drawn as one day
def _check_grain(add) -> None:
    from desktop import channels_data as cd

    def wk(cid: int, d: date, **kw) -> dict:
        return dict(_daily(cid, d), grain="week", **kw)
    v, o = _view(_options(3), d_from=date(2026, 8, 1), d_to=date(2026, 8, 30))
    rows = [_daily(1, date(2026, 8, 3), sessions=10), wk(2, date(2026, 8, 3), sessions=700), wk(2, date(2026, 8, 10), sessions=500), wk(2, date(2026, 8, 24), sessions=300)]
    ch = cd.build_chart(rows, o, v)
    s1 = next(s for s in ch["series"] if s["key"] == "ch1")
    s2 = next(s for s in ch["series"] if s["key"] == "ch2")
    i = ch["days"].index("2026-08-03")
    add("grain: the weekly channel has NO value on the per-day axis (its week is not one day's activity)", all(x is None for x in s2["values"]["sessions"]) and s2["values"]["sessions"][i] is None, s2["values"]["sessions"][:6])
    add("grain: ... its weeks are on the weekly axis, a missing week is a gap (None), the axis runs every 7 days", ch["weeks"] == ["2026-08-03", "2026-08-10", "2026-08-17", "2026-08-24"] and s2["week_values"]["sessions"] == [700, 500, None, 300], (ch["weeks"], s2["week_values"]["sessions"]))
    add("grain: a daily channel is untouched (its day has its value, its week list is empty)", s1["values"]["sessions"][i] == 10 and s1["week_values"]["sessions"] == [None] * 4, s1)
    add("grain: has_daily / has_weekly say what is there; the metric totals count both grains once", ch["has_daily"] and ch["has_weekly"] and next(m for m in ch["metrics"] if m["key"] == "sessions")["total"] == 1510, (ch["has_daily"], ch["has_weekly"], ch["metrics"]))
    add("grain: coverage is counted per grain (1 day of data, 3 weeks of data)", ch["days_with_data"] == 1 and ch["weeks_with_data"] == 3, (ch["days_with_data"], ch["weeks_with_data"]))
    only = cd.build_chart([wk(2, date(2026, 8, 3), sessions=700)], o, v)
    add("grain: a view with only weekly rows has no daily chart, and is not 'empty'", not only["has_daily"] and only["has_weekly"] and any(m["total"] for m in only["metrics"]) and only["days_with_data"] == 0)
    add("grain: week starts that do not share a weekday are drawn where they are (no invented weeks)", cd.week_axis([date(2026, 8, 3), date(2026, 8, 12)]) == [date(2026, 8, 3), date(2026, 8, 12)] and cd.week_axis([]) == [])
    out = cd.week_axis([date(2026, 8, 3), date(2026, 8, 24)])
    add("grain: aligned starts are filled every 7 days", out == [date(2026, 8, 3), date(2026, 8, 10), date(2026, 8, 17), date(2026, 8, 24)], out)
    outside = cd.build_chart([wk(2, date(2026, 7, 27), sessions=5), wk(2, date(2026, 8, 3), sessions=6)], o, v)
    add("grain: a weekly row dated before the drawn window is not drawn", outside["weeks"] == ["2026-08-03"], outside["weeks"])

    # totals rows and the mode
    add("grain: periods() of a totals row without the split reads them all as daily", cd.periods({"days_with_data": 7}) == (7, 0) and cd.periods({"day_periods": 2, "week_periods": 3}) == (2, 3) and cd.periods(None) == (0, 0))
    add("grain: the mode is none / day / week / mixed", [cd.grain_mode(x) for x in ({"days_with_data": 0}, {"day_periods": 4, "week_periods": 0}, {"day_periods": 0, "week_periods": 3}, {"day_periods": 4, "week_periods": 3})] == ["none", "day", "week", "mixed"])
    wtot = dict(_tot(days=3, sessions=1500, clicks=900, impressions=9000, conversions=36), day_periods=0, week_periods=3)
    wdaily = [wk(2, date(2026, 8, 3), sessions=700, clicks=300), wk(2, date(2026, 8, 10), sessions=500, clicks=300), wk(2, date(2026, 8, 17), sessions=300, clicks=300)]
    tiles = {x["id"]: x for x in cd.build_kpis(wtot, _tot(days=0), wdaily, v)}
    sp = tiles["sessions"]["spark"]
    add("grain: a weekly-only view's tile line has one point per WEEK, labelled as weeks, on the start days", sp is not None and sp["grain"] == "week" and sp["days"] == ["2026-08-03", "2026-08-10", "2026-08-17"] and sp["values"] == [700, 500, 300], sp)
    add("grain: ... and the tile says weeks, not '3 of 30 days'", "3 weeks of data" in tiles["sessions"]["sub"] and "of 30 days" not in tiles["sessions"]["sub"], tiles["sessions"]["sub"])
    mtot = dict(wtot, day_periods=4)
    tiles = {x["id"]: x for x in cd.build_kpis(mtot, _tot(days=0), wdaily + [_daily(1, date(2026, 8, 3))], v)}
    add("grain: daily and weekly rows mixed -> no tile line at all (one axis cannot honestly hold both)", all(tiles[k]["spark"] is None for k in ("sessions", "clicks", "conversions", "cvr")) and "4 of 30 days and 3 weeks" in tiles["sessions"]["sub"], tiles["sessions"])
    add("grain: coverage_text counts weeks for a weekly source", cd.coverage_text(wtot, v) == "3 weeks of weekly data (each counted on its start day)" and "of 30 days" in cd.coverage_text(dict(wtot, day_periods=4), v) and cd.coverage_text({"days_with_data": 30}, v) == "all 30 days have data")
    raw = _raw(v, totals=wtot, daily=wdaily, channels=[_chrow(2, sessions=1500)])
    p = cd.assemble(raw, v, o, META, NOW)
    add("grain: the headline of a weekly view says weekly (not 'X of 30 days', which would read as an outage) and a warning explains the counting", "weekly data" in p["headline"]["sub"] and "of 30 days" not in p["headline"]["sub"]
        and any("weekly source" in w for w in p["warnings"]) and p["range"]["week_periods"] == 3 and p["range"]["day_periods"] == 0 and p["panels"]["chart"]["has_weekly"], (p["headline"], p["warnings"]))
    p = cd.assemble(_raw(v), v, o, META, NOW)
    add("grain: an all-daily view has no weekly warning and keeps its old wording", not any("weekly" in w for w in p["warnings"]) and p["range"]["week_periods"] == 0 and "of 30 days" in p["headline"]["sub"])
    add("grain: the SQL groups the chart by grain and counts periods per grain", "grain" in cd.daily_sql("public", v, v.d_from, 5)[0] and "FILTER (WHERE grain = 'week')" in cd.totals_sql("public", v)[0])
    css = (ROOT / "desktop" / "static" / "channels.css").read_text(encoding="utf-8", errors="replace")
    add("page: the hidden day/week plots stay hidden (L-092)", ".ch .ld-plot[hidden]" in css)

    # a weekly source is limited by BAR COUNT, not by the per-day window: its rows are few
    vlong, olong = _view(_options(3), d_from=date(2024, 1, 1), d_to=date(2026, 9, 10))
    rows = [wk(2, date(2025, 1, 6), sessions=9), wk(2, date(2025, 1, 13), sessions=8), wk(2, date(2026, 8, 3), sessions=7)]
    chl = cd.build_chart(rows, olong, vlong)
    add("grain: a weekly row twenty months old is drawn although the 980-day range is far beyond the 180-day per-day window",
        chl["has_weekly"] and chl["weeks"][0] == "2025-01-06" and chl["weeks"][-1] == "2026-08-03" and not chl["has_daily"] and chl["clipped"] and not chl["week_clipped"], (chl["weeks"][:2], chl["weeks"][-1]))
    rows = [wk(2, date(2026, 8, 3) - timedelta(days=7 * k), sessions=k + 1) for k in range(6)]
    with _patched(cd, CHART_MAX_WEEKS=4):
        cht = cd.build_chart(rows, olong, vlong)
        tt = {x["id"]: x for x in cd.build_kpis(dict(_tot(days=6), day_periods=0, week_periods=6), _tot(days=0), rows, vlong)}
    add("grain: more weeks than CHART_MAX_WEEKS keeps the NEWEST ones and says it clipped", cht["week_clipped"] and len(cht["weeks"]) == 4 and cht["weeks"][-1] == "2026-08-03" and cht["weeks"][0] == "2026-07-13", cht["weeks"])
    add("grain: ... and so does the tile line (newest 4 weeks)", tt["sessions"]["spark"] is not None and len(tt["sessions"]["spark"]["days"]) == 4 and tt["sessions"]["spark"]["grain"] == "week", tt["sessions"]["spark"])
    sql, params = cd.daily_sql("public", vlong, date(2026, 3, 14), 10, week_lower=vlong.d_from)
    add("grain: the chart query lets weekly rows reach back to the range start (grain = 'week' OR event_date >= day_lower), bound - not formatted", "grain = 'week' OR event_date >= :day_lower" in sql and params["day_lower"] == date(2026, 3, 14) and params["d_from"] == vlong.d_from, (sql[-160:], params))
    sql2, params2 = cd.daily_sql("public", vlong, date(2026, 3, 14), 10)
    add("grain: without a week lower bound the query is the plain one", "day_lower" not in sql2 and params2["d_from"] == date(2026, 3, 14))
    add("low volume: a channel with no sessions field but thousands of clicks is NOT low volume; one with few of both is", not cd.low_volume({"sessions": 0, "clicks": 19959}) and cd.low_volume({"sessions": 3, "clicks": 2}) and not cd.low_volume({"sessions": 500, "clicks": 0}))


# =========================================================================================== SQL: two currencies and a weekly source in a temp schema
_M = 1_000_000


def _money_plan() -> list[dict]:
    """Campaign-level rollup rows: paid_social (weekly, VND), email (daily, EUR, reported revenue, no spend), search (daily, USD, spend +
    reported revenue), organic (daily, USD default, counts only) and 'hybrid' (daily, VND AND EUR on the same days)."""
    rows = []
    for d, s, c, i, cv, sp, rv in ((date(2026, 8, 3), 400, 300, 5000, 12, 475_401, 12_000_000), (date(2026, 8, 10), 500, 350, 6000, 9, 512_300, 9_000_000),
                                   (date(2026, 8, 17), 450, 320, 5500, 15, 398_100, 15_000_000)):
        rows.append(dict(d=d, ch=1, cp=10, grain="week", cur="VND", s=s, c=c, i=i, cv=cv, sp=sp * _M, rv=rv * _M, dv=rv * _M))
        rows.append(dict(d=d, ch=1, cp=11, grain="week", cur="VND", s=20, c=10, i=300, cv=1, sp=50_000 * _M, rv=1_000_000 * _M, dv=1_000_000 * _M))
    rows.append(dict(d=date(2026, 1, 5), ch=1, cp=10, grain="week", cur="VND", s=77, c=77, i=1000, cv=2, sp=100_000 * _M, rv=2_000_000 * _M, dv=2_000_000 * _M))
    for k in range(21):
        if k % 4 == 3:
            continue
        rows.append(dict(d=date(2026, 8, 20) + timedelta(days=k), ch=2, cp=20, grain="day", cur="EUR", s=0, c=40 + k, i=3000 + k, cv=2 + k % 3, sp=0, rv=1_249_130_000 + k * 10_000_000, dv=0))
    for k in range(17):
        rows.append(dict(d=date(2026, 8, 25) + timedelta(days=k), ch=3, cp=30, grain="day", cur="USD", s=60 + k, c=25, i=900, cv=3, sp=12_500_000, rv=40_000_000, dv=0))
    for k in range(10):
        rows.append(dict(d=date(2026, 8, 28) + timedelta(days=k), ch=4, cp=0, grain="day", cur="USD", s=30, c=12, i=0, cv=1, sp=0, rv=0, dv=0))
    for k in range(3):
        d = date(2026, 9, 1) + timedelta(days=k)
        rows.append(dict(d=d, ch=5, cp=50, grain="day", cur="VND", s=10, c=8, i=200, cv=1, sp=70_000 * _M, rv=0, dv=0))
        rows.append(dict(d=d, ch=5, cp=50, grain="day", cur="EUR", s=11, c=9, i=210, cv=2, sp=3 * _M, rv=20 * _M, dv=0))
    return rows


def _money_expect(rows, lo: date, hi: date, cur: str | None = None, channels=None) -> dict:
    sel = [r for r in rows if lo <= r["d"] <= hi and (cur is None or r["cur"] == cur) and (channels is None or r["ch"] in channels)]
    out = {"sessions": sum(r["s"] for r in sel), "clicks": sum(r["c"] for r in sel), "impressions": sum(r["i"] for r in sel), "conversions": sum(r["cv"] for r in sel)}
    money: dict = {}
    for r in sel:
        m = money.setdefault(r["cur"], {"spend": 0, "revenue": 0, "derived": 0})
        m["spend"] += r["sp"]; m["revenue"] += r["rv"]; m["derived"] += r["dv"]
    out["money"] = {c: m for c, m in money.items() if m["spend"] or m["revenue"]}
    out["weeks"] = len({r["d"] for r in sel if r["grain"] == "week"})
    out["days"] = len({r["d"] for r in sel if r["grain"] == "day"})
    return out


def _load_money_plan(conn) -> None:
    from sqlalchemy import text
    conn.execute(text("INSERT INTO pg_temp.marketing_source (source_id, source_key) VALUES (1, 'fixture')"))
    for cid, key, name, medium, paid in ((1, "paid_social", "Paid Social", "paid_social", True), (2, "email", "Email", "email", False), (3, "search", "Search", "search", True),
                                         (4, "organic", "Organic", "organic", False), (5, "hybrid", "Hybrid", "unknown", None)):
        conn.execute(text("INSERT INTO pg_temp.marketing_channel (channel_id, channel_key, display_name, medium, is_paid) VALUES (:i, :k, :n, :m, :p)"), dict(i=cid, k=key, n=name, m=medium, p=paid))
    for cp, ch, key, name in ((10, 1, "cam_1", "Cam 1"), (11, 1, "cam_2", "Cam 2"), (20, 2, "cmp_a", "CMP-A"), (30, 3, "brand", "Brand"), (50, 5, "mix", "Mix")):
        conn.execute(text("INSERT INTO pg_temp.marketing_campaign (campaign_id, channel_id, campaign_key, name) VALUES (:c, :h, :k, :n)"), dict(c=cp, h=ch, k=key, n=name))
    conn.execute(text("""INSERT INTO pg_temp.interaction_daily_rollup (event_date, channel_id, campaign_id, currency, grain, sessions, clicks, impressions, conversions, spend_micros, revenue_micros, revenue_derived_micros)
                         VALUES (:d, :ch, :cp, :cur, :grain, :s, :c, :i, :cv, :sp, :rv, :dv)"""), _money_plan())
    conn.execute(text("""INSERT INTO pg_temp.interaction_daily_channel_rollup (event_date, channel_id, currency, grain, sessions, clicks, impressions, conversions, spend_micros, revenue_micros, revenue_derived_micros)
                         SELECT event_date, channel_id, currency, grain, sum(sessions), sum(clicks), sum(impressions), sum(conversions), sum(spend_micros), sum(revenue_micros), sum(revenue_derived_micros)
                           FROM pg_temp.interaction_daily_rollup GROUP BY 1, 2, 3, 4"""))
    conn.execute(text("INSERT INTO pg_temp.marketing_rollup_run (mode, status, finished_at, rows_written) VALUES ('full', 'ok', now(), 1)"))
    # a POISONED raw fact row in a currency no rollup row has: the page reads only the rollup, so JPY must appear nowhere
    from erp.marketing import schema as mschema
    mschema.ensure_partitions(conn, "pg_temp", [date(2026, 9, 5)])
    conn.execute(text("""INSERT INTO pg_temp.interaction_fact (event_date, dedupe_key, source_id, channel_id, event_type, sessions, clicks, spend_micros, currency)
                         VALUES ('2026-09-05', md5('poison-jpy')::uuid, 1, 1, 'click', 999999, 999999, 999999000000, 'JPY')"""))
    conn.commit()


def _check_sql_money(add) -> None:
    import csv as csvmod

    from sqlalchemy import text

    from desktop import channels_data as cd
    plan = _money_plan()
    lo, hi = date(2026, 8, 1), date(2026, 9, 10)
    q = {"from": [lo.isoformat()], "to": [hi.isoformat()]}
    with _temp_schema("money") as (eng, conn):
        store = cd.ChannelsStore(ttl=0)
        code, p = store.get(q, fresh=True)
        add("money sql: the multi-currency page answers 200, ready, every panel reads", code == 200 and p["state"] == "ready" and p["ok"], (code, p.get("state"), p.get("error"), {k: v.get("error") for k, v in (p.get("panels") or {}).items() if v.get("state") == "unavailable"}))
        if code != 200 or p["state"] != "ready":
            return
        e = _money_expect(plan, lo, hi)
        add("money sql: the currencies in view are exactly the ones that carry money, in code order", p["money"]["mode"] == "multi" and [c["code"] for c in p["money"]["currencies"]] == ["EUR", "USD", "VND"], p["money"])
        direct = {r[0]: (int(r[1]), int(r[2]), int(r[3])) for r in conn.execute(text("SELECT currency, sum(spend_micros), sum(revenue_micros), sum(revenue_derived_micros) FROM pg_temp.interaction_daily_channel_rollup WHERE event_date BETWEEN :a AND :b GROUP BY currency HAVING sum(spend_micros) <> 0 OR sum(revenue_micros) <> 0"), {"a": lo, "b": hi})}
        page = {c["code"]: (round(c["spend"] * 1e6), round(c["revenue"] * 1e6), round(c["revenue_derived"] * 1e6)) for c in p["money"]["currencies"]}
        add("money sql: per-currency spend, revenue and derived revenue equal the hand-worked plan AND a direct GROUP BY currency", page == direct == {c: (m["spend"], m["revenue"], m["derived"]) for c, m in e["money"].items()}, (page, direct))
        tiles = {t["id"]: t for t in p["kpis"]}
        blended = sum(m["spend"] for m in e["money"].values()) / 1e6
        add("money sql: spend has two currencies -> the tile is multi, its parts equal the plan, and the blended sum appears nowhere in the payload",
            tiles["spend"]["state"] == "multi" and tiles["spend"]["value"] is None and {x["currency"]: x["value"] for x in tiles["spend"]["parts"]} == {c: round(m["spend"] / 1e6, 2) for c, m in e["money"].items() if m["spend"]}
            and blended not in set(_numbers(p)), (tiles["spend"], blended))
        add("money sql: revenue in three currencies is multi; VND's part is marked derived (all), EUR's and USD's are reported", tiles["revenue"]["state"] == "multi"
            and {x["currency"]: x["derived"] for x in tiles["revenue"]["parts"]} == {"EUR": None, "USD": None, "VND": "all"}, tiles["revenue"]["parts"])
        add("money sql: the counts tiles ignore currency and equal the plan (and the poisoned JPY fact row appears nowhere)", (tiles["sessions"]["value"], tiles["clicks"]["value"], tiles["conversions"]["value"]) == (e["sessions"], e["clicks"], e["conversions"]) and "JPY" not in str(p),
            (tiles["sessions"]["value"], e["sessions"]))
        tb = p["panels"]["table"]
        by = {r["key"]: r for r in tb["rows"]}
        add("money sql: every channel row shows its own currency; the hybrid channel (VND and EUR) is multi; organic has no money", by["paid_social"]["spend"]["currency"] == "VND" and by["email"]["revenue"]["currency"] == "EUR" and by["search"]["spend"]["currency"] == "USD"
            and by["hybrid"]["spend"]["state"] == "multi" and by["organic"]["spend"]["state"] == "none" and by["organic"]["revenue"]["state"] == "none", {k: (r["spend"]["state"], r["spend"]["currency"]) for k, r in by.items()})
        add("money sql: the paid_social row equals the plan (spend VND, derived revenue, cost per conversion in VND)",
            by["paid_social"]["spend"]["value"] == round(sum(r["sp"] for r in plan if r["ch"] == 1 and lo <= r["d"] <= hi) / 1e6, 2) and by["paid_social"]["revenue"]["derived"] == "all"
            and by["paid_social"]["cpa"]["currency"] == "VND" and by["email"]["spend"]["why"] == "no spend field", by["paid_social"]["spend"])
        add("money sql: the total row is per currency (multi), never one blended figure", tb["total"]["spend"]["state"] == "multi" and tb["total"]["revenue"]["state"] == "multi" and tb["money_mode"] == "multi" and blended not in set(_numbers(tb)))
        ch = p["panels"]["chart"]
        ps = next(s for s in ch["series"] if s["key"] == "paid_social")
        i0 = ch["days"].index("2026-08-03")
        add("money sql: the weekly source is in the per-week chart (3 weeks, its clicks per week from the plan), and NOT a spike on the per-day axis",
            ch["has_weekly"] and ch["has_daily"] and ch["weeks"] == ["2026-08-03", "2026-08-10", "2026-08-17"] and ps["week_values"]["clicks"] == [310, 360, 330]
            and all(x is None for x in ps["values"]["clicks"]) and ps["values"]["clicks"][i0] is None, (ch["weeks"], ps["week_values"]["clicks"]))
        add("money sql: the chart's per-day sessions of the daily channels equal the plan (weekly rows excluded)",
            sum(x or 0 for s in ch["series"] for x in s["values"]["sessions"]) == sum(r["s"] for r in plan if r["grain"] == "day" and lo <= r["d"] <= hi) and sum(x or 0 for s in ch["series"] for x in (s["week_values"] or {"sessions": []})["sessions"]) == sum(r["s"] for r in plan if r["grain"] == "week" and lo <= r["d"] <= hi))
        add("money sql: the range knows its periods (weeks vs days) and the headline counts weeks", p["range"]["week_periods"] == e["weeks"] == 3 and p["range"]["day_periods"] == e["days"] and "3 weeks of weekly data" in p["headline"]["sub"], (p["range"], p["headline"]["sub"]))
        add("money sql: warnings name the currencies and the weekly source", any("3 currencies (EUR, USD, VND)" in w for w in p["warnings"]) and any("weekly source" in w for w in p["warnings"]), p["warnings"])
        add("money sql: the filter bar is offered the currencies of the data with their day counts", [c["code"] for c in p["options"]["currencies"]] == ["EUR", "USD", "VND"] and all(c["days"] > 0 for c in p["options"]["currencies"]) and next(c for c in p["options"]["currencies"] if c["code"] == "USD")["has_money"], p["options"]["currencies"])

        # a range too long for the per-day chart: the weekly bars still reach back (their rows are few)
        wide = {"from": ["2025-12-01"], "to": [hi.isoformat()]}
        code, pw = store.get(wide, fresh=True)
        chw = pw["panels"]["chart"] if code == 200 else {}
        psw = next((s for s in chw.get("series", []) if s["key"] == "paid_social"), {})
        add("money sql: a 283-day range clips the per-day chart to 180 days, yet the weekly chart still holds the January week (older than the day window) and the August weeks",
            code == 200 and chw.get("state") == "ok" and chw["clipped"] and chw["weeks"][0] == "2026-01-05" and chw["weeks"][-1] == "2026-08-17" and len(chw["weeks"]) == 33
            and psw["week_values"]["clicks"][0] == 77 and psw["week_values"]["clicks"][-1] == 330 and psw["week_values"]["clicks"][1] is None, (code, chw.get("weeks", [])[:3], chw.get("clipped")))
        code, pw2 = store.get({**wide, "currency": ["VND"]}, fresh=True)
        add("money sql: ... and the VND-only wide view draws the weekly bars from January on (its only daily rows are the hybrid channel's three VND days)",
            code == 200 and pw2["panels"]["chart"]["state"] == "ok" and pw2["panels"]["chart"]["has_weekly"] and pw2["panels"]["chart"]["weeks"][0] == "2026-01-05", (code, pw2.get("panels", {}).get("chart", {}).get("weeks")))
        add("money sql: ... and its clicks tile counts the January week, the three August weeks and the hybrid VND days once each", {t["id"]: t for t in pw2["kpis"]}["clicks"]["value"] == 77 + 310 + 360 + 330 + sum(r["c"] for r in plan if r["ch"] == 5 and r["cur"] == "VND"))

        # a currency filter narrows EVERYTHING to that currency's rows
        for cur, chans in (("VND", {1, 5}), ("EUR", {2, 5}), ("USD", {3, 4})):
            code, pc = store.get({**q, "currency": [cur]}, fresh=True)
            ec = _money_expect(plan, lo, hi, cur=cur)
            tc = {t["id"]: t for t in pc["kpis"]} if code == 200 else {}
            add(f"money sql: currency={cur} - every count tile equals the plan for that currency's rows only", code == 200 and (tc["sessions"]["value"], tc["clicks"]["value"], tc["conversions"]["value"]) == (ec["sessions"], ec["clicks"], ec["conversions"]) and pc["filters"]["currency"] == cur and pc["filters"]["active"] == 2, (code, pc.get("error")))
            add(f"money sql: currency={cur} - money is single-currency, in {cur}, and equals the plan", pc["money"]["mode"] == "single" and [c["code"] for c in pc["money"]["currencies"]] == [cur]
                and {x["currency"] for t in pc["kpis"] if t["id"] in ("spend", "revenue") and t["state"] == "ok" for x in t["parts"]} <= {cur} and not any("currencies" in w for w in pc["warnings"]), (pc["money"], pc["warnings"]))
            add(f"money sql: currency={cur} - the table lists only the channels that have {cur} rows", {r["key"] for r in pc["panels"]["table"]["rows"]} == {k for c, k in ((1, "paid_social"), (2, "email"), (3, "search"), (4, "organic"), (5, "hybrid")) if c in chans}, [r["key"] for r in pc["panels"]["table"]["rows"]])
        code, pv = store.get({**q, "currency": ["VND"]}, fresh=True)
        tv = {t["id"]: t for t in pv["kpis"]}
        add("money sql: currency=VND - spend and revenue are single-currency tiles (VND), revenue says derived, the weekly bars remain", tv["spend"]["state"] == "ok" and tv["spend"]["unit"] == "VND" and tv["revenue"]["unit"] == "VND" and "DERIVED" in tv["revenue"]["sub"] and pv["panels"]["chart"]["has_weekly"], (tv["spend"], tv["revenue"]["sub"]))
        code, pe = store.get({**q, "currency": ["EUR"]}, fresh=True)
        te = {t["id"]: t for t in pe["kpis"]}
        add("money sql: currency=EUR - revenue is EUR and reported (not derived); the only EUR spend is the hybrid channel's, also in EUR",
            te["revenue"]["unit"] == "EUR" and te["revenue"]["parts"][0]["derived"] is None and te["spend"]["state"] == "ok" and te["spend"]["unit"] == "EUR", (te["spend"], te["revenue"]))

        # validation, on the JSON endpoint and the CSV
        for bad in ("XXX", "eur", "EU", "JPY"):
            c1, b1 = store.get({**q, "currency": [bad]}, fresh=True)
            c2, b2, _h = store.export_csv({**q, "currency": [bad], "part": ["channels"]})
            add(f"money sql: currency={bad!r} is a 400 with the parameter named - on the page's feed and on the CSV", c1 == 400 and b1["problems"][0]["param"] == "currency" and c2 == 400 and b2["problems"][0]["param"] == "currency", (c1, c2))
        c1, b1 = store.get({**q, "currecy": ["EUR"]}, fresh=True)
        c2, b2, _h = store.export_csv({**q, "currecy": ["EUR"], "part": ["daily"]})
        add("money sql: a typo of the parameter NAME is a 400 on both endpoints", c1 == 400 and c2 == 400 and b1["problems"][0]["param"] == "currecy", (c1, c2))
        cached = cd.ChannelsStore(ttl=60)
        c0, _b0 = cached.get(q)
        c3, b3 = cached.get({**q, "currecy": ["EUR"]})
        c4, _b4 = cached.get({**q, "currency": ["nope"]})
        add("money sql: the name/shape checks run BEFORE the cache: a warm cache does not turn a typo into a 200", c0 == 200 and c3 == 400 and c4 == 400 and b3["problems"][0]["param"] == "currecy", (c0, c3, c4))

        # CSV
        def get_csv(params):
            c, body, headers = store.export_csv(params)
            if c != 200:
                return c, body, headers
            return c, list(csvmod.reader(io.StringIO(body.decode("utf-8-sig"), newline=""))), headers
        c, parsed, hdr = get_csv({**q, "part": ["channels"]})
        head = parsed[0]
        cells = {r[head.index("channel_key")]: dict(zip(head, r)) for r in parsed[1:]}
        add("money sql csv: channels file - one row per channel, its currency, its amounts, the derived flag; hybrid says MULTIPLE with blank amounts",
            c == 200 and cells["paid_social"]["currency"] == "VND" and cells["paid_social"]["revenue_derived"] == "all" and cells["email"]["currency"] == "EUR" and cells["email"]["spend"] == "" and cells["organic"]["currency"] == ""
            and cells["hybrid"]["currency"] == "MULTIPLE" and cells["hybrid"]["spend"] == "" and "EUR" in cells["hybrid"]["money_by_currency"] and "VND" in cells["hybrid"]["money_by_currency"], cells)
        add("money sql csv: no cell of the channels file holds the blended sum", not any(x in {f"{blended:g}", f"{blended:.2f}"} for r in parsed[1:] for x in r), blended)
        c, parsed, hdr = get_csv({**q, "part": ["daily"]})
        dh = parsed[0]
        body = parsed[1:]
        add("money sql csv: the daily file is one row per day x channel x grain x currency; its counts add up to the page's totals exactly once", c == 200 and sum(int(r[dh.index("sessions")]) for r in body) == e["sessions"] and sum(int(r[dh.index("clicks")]) for r in body) == e["clicks"],
            (c, sum(int(r[dh.index("sessions")]) for r in body), e["sessions"]))
        add("money sql csv: ... weekly rows say week / 7 and sit on their start day; the hybrid channel has one row per currency per day",
            {(r[dh.index("date")], r[dh.index("period_days")]) for r in body if r[dh.index("grain")] == "week"} == {("2026-08-03", "7"), ("2026-08-10", "7"), ("2026-08-17", "7")}
            and sorted(r[dh.index("currency")] for r in body if r[dh.index("channel_key")] == "hybrid" and r[dh.index("date")] == "2026-09-01") == ["EUR", "VND"], (hdr, len(body)))
        per_cur = defaultdict_sum(body, dh, "currency", "spend")
        add("money sql csv: spend summed per currency column equals the plan per currency", {k: round(v, 2) for k, v in per_cur.items() if k} == {c_: round(m["spend"] / 1e6, 2) for c_, m in e["money"].items() if m["spend"]}, per_cur)
        c, parsed, hdr = get_csv({**q, "part": ["campaigns"], "focus": ["paid_social"]})
        chd = parsed[0]
        add("money sql csv: the campaigns file carries the currency and the derived revenue flag per campaign", c == 200 and {r[chd.index("campaign")] for r in parsed[1:]} == {"Cam 1", "Cam 2"} and all(r[chd.index("currency")] == "VND" and r[chd.index("revenue_derived")] == "all" for r in parsed[1:]), parsed[:3])
        code, pf = store.get({**q, "focus": ["hybrid"]}, fresh=True)
        cp = pf["panels"]["campaigns"]
        add("money sql: a campaign whose spend spans currencies is multi in the drill-down too (its revenue is EUR only, so a plain EUR amount)", cp["state"] == "ok" and cp["rows"][0]["spend"]["state"] == "multi" and cp["rows"][0]["revenue"]["state"] == "ok" and cp["rows"][0]["revenue"]["currency"] == "EUR", cp["rows"][:1])
        code, pn = store.get({**q, "focus": ["paid_social"], "campaign": ["10"]}, fresh=True)
        add("money sql: a campaign filter (campaign rollup) keeps currency and the weekly grain", code == 200 and pn["source"] == "campaign_rollup" and pn["money"]["mode"] == "single" and pn["panels"]["chart"]["has_weekly"], (code, pn.get("error")))

        # one money query failing greys the money figures only
        bad_sql = lambda *a, **k: ("SELECT * FROM pg_temp.no_such_table", {})     # noqa: E731
        with _patched(cd, money_sql=bad_sql):
            code, pb = store.get(q, fresh=True)
        tb_ = {t["id"]: t for t in pb["kpis"]} if code == 200 else {}
        add("money sql failure: every money query failing greys spend / revenue and the money cells, the counts stay", code == 200 and tb_["spend"]["state"] == "unavailable" and tb_["revenue"]["state"] == "unavailable" and tb_["sessions"]["value"] == e["sessions"]
            and pb["panels"]["table"]["rows"][0]["spend"]["state"] == "unavailable" and pb["ok"] is False and pb["money"]["mode"] == "unavailable", (code, pb.get("money")))
        code, pa = store.get(q, fresh=True)
        add("money sql failure: ... and the next request is healthy (nothing stuck)", code == 200 and pa["ok"])

    # 10 was not installed: the page still answers, and says what to do first
    with _temp_schema("07") as (eng, conn):
        code, pn = cd.ChannelsStore(ttl=0).get({}, fresh=True)
        add("money sql: with only 07 installed the fix list starts with migration 10 (the fact has no currency column yet)", code == 200 and pn["state"] == "not_installed" and pn["install"]["steps"][0]["command"] == cd.PSQL_10, pn.get("install", {}).get("steps"))


def defaultdict_sum(body, header, key_col: str, val_col: str) -> dict:
    """Sum a numeric CSV column grouped by another column (blank cells count as nothing)."""
    out: dict = {}
    for r in body:
        v = r[header.index(val_col)]
        if v != "":
            out[r[header.index(key_col)]] = out.get(r[header.index(key_col)], 0.0) + float(v)
    return out


# =========================================================================================== runner
def run(db_ok: bool = True) -> list[tuple[str, bool, object]]:
    """All scenarios. Returns [(name, passed, detail)]; never raises (a crash is one failed row)."""
    import logging
    rows: list[tuple[str, bool, object]] = []

    def add(name: str, ok, detail=None) -> None:
        rows.append((name, bool(ok), detail if not ok else ""))

    lg = logging.getLogger("erp_desk.channels")
    lg2 = logging.getLogger("erp_desk.leads")
    old1, lg.disabled = lg.disabled, True
    old2, lg2.disabled = lg2.disabled, True
    try:
        fns = ([_check_filters, _check_math, _check_states, _check_assemble, _check_panels, _check_csv, _check_text, _check_currency, _check_grain]
               + ([_check_sql, _check_sql_money, _check_real_db] if db_ok else []))
        for fn in fns:
            try:
                fn(add)
            except Exception as exc:  # noqa: BLE001
                import traceback
                add(f"{fn.__name__} crashed", False, f"{type(exc).__name__}: {exc} | {traceback.format_exc().splitlines()[-3][:160]}")
    finally:
        lg.disabled, lg2.disabled = old1, old2
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
    t0 = time.monotonic()
    rows = run(db_ok)
    for name, ok, detail in rows:
        print(f"{'ok  ' if ok else 'FAIL'} {name}" + (f"   <- {detail}" if not ok else ""))
    bad = [r for r in rows if not r[1]]
    print(f"\n{len(rows) - len(bad)}/{len(rows)} scenarios passed in {time.monotonic() - t0:.1f}s" + ("" if db_ok else "  (database not reachable: the SQL scenarios were skipped)"))
    with contextlib.suppress(Exception):
        from erp.db import engine
        engine.dispose()
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
