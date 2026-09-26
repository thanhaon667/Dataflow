"""
Live data feed for the Channels page of ERP Desk: the channel-performance report, for MANAGERS (one sentence and five
tiles) and ANALYSTS (per-channel table, campaign drill-down, definitions, CSV).
  GET /api/channels        every panel of the page for one set of filters (JSON)
  GET /api/channels.csv    the same view as a CSV file (part=channels | daily | campaigns)

WHAT IT READS - and what it never reads. Aggregates come ONLY from the two Phase 4 rollup tables
(`interaction_daily_channel_rollup`, and `interaction_daily_rollup` for a campaign drill-down or a campaign filter),
never from `interaction_fact` and never from `marketing_landing`, so the cost of a request follows days x channels
(x campaigns), not the number of raw events (docs/marketing-data-architecture.md s.8; proven with EXPLAIN in the
verification notes of the README/PROJECT_NOTES). Three tiny LOOKUPS sit beside them and are named here so nobody is
surprised: `marketing_channel` (channel names), `marketing_campaign` (campaign names, and the campaign filter's
whitelist - one primary-key probe) and `marketing_rollup_run` (the refresh journal: "data as of"). None of them grows
with events. Every table lives in erp.config.MARKETING_SCHEMA and every identifier goes through
erp.marketing.schema.check_identifier / qualified; every VALUE (dates, channel ids, campaign id) is a bound parameter.

THE DATA REALITY. In the owner's database today the rollup tables are not installed (the owner runs
db/sql/09_marketing_rollup.sql) and no source is wired in, so the normal state of this page is one of four, chosen by
`page_state()` and rendered without a single error:
    not_installed   a marketing table or a rollup table is missing -> the exact owner-run psql command
    empty           installed, but the channel rollup has no row      -> "no data has been loaded yet" + the two commands
    no_match        data exists, the filters select none of it        -> say so, offer to loosen
    ready           numbers
Nothing is ever invented: an unknown is "unavailable" / "no spend field" / "no data in the previous period", never a 0.

CURRENCY AND GRAIN (docs/marketing-data-architecture.md, "Currency" and "Grain"). Money carries a currency and is NEVER
converted or added across currencies: 1,000,000 VND is about 35 EUR, so a sum of the two means nothing. The rollup keeps
currency in its key; this page adds money only within ONE currency. Counts (sessions, clicks, impressions, conversions) have
no currency and add freely. `money_cell()` is the ONE place that decides how a money figure is shown: one currency -> the
amount and its code; several -> the amounts per currency, no total; none -> "no spend field". A `currency` filter narrows the
view to one currency (it is a data filter like channel: it also narrows the counts). The rollup also keeps `grain`: 'week'
rows are a weekly source's rows, dated by their START day and carrying the whole week; they are drawn in their own per-week
chart and never as one day's activity.

Rules (the Definitions drawer is generated from the same constants, lesson L-085):
  * Additive columns only are read (sessions, clicks, impressions, conversions, spend_micros, revenue_micros). Every rate is
    computed at READ time from sums: CTR = clicks / impressions, conversion rate = conversions / clicks (the definition of
    docs/marketing-data-architecture.md s.8.1), cost per conversion = spend / conversions (of the same currency) - only where
    spend exists.
  * A percentage needs a real denominator: below LOW_N (5) the page shows counts ("2 of 3"). A denominator of 0 is
    "no clicks" / "no impressions", never a division.
  * Spend is "present" only when its sum is above 0 (the column is NOT NULL DEFAULT 0, so 0 cannot be told from "the
    source has no spend field"): otherwise "no spend field", never a $0.00.
  * A day with no rollup row is a GAP, not a zero (the refresh writes no zero rows, and "not loaded yet" looks the same
    as "no traffic"): sparklines and the chart break the line there, and the header says "N of M days have data".
  * The previous period is the same number of days immediately before the range. Its delta is a percentage only when
    the previous number is at least LOW_N; smaller ones read as counts; no data at all reads "no data in the previous
    period", never +0%.
  * Every figure carries its date range and the database session's time zone (lesson L-097): event_date is the day the
    source reports; `current_setting('TimeZone')` is read in the same request and printed with the range.
  * With no from/to the range is the last DEFAULT_DAYS days ENDING AT THE NEWEST DAY THE ROLLUP HOLDS (not today's date,
    so a report over old data still shows numbers) - `range.defaulted` says so.
  * A campaign filter cannot be answered by the channel rollup (no campaign column), so with one set the tiles, chart
    and table read the campaign rollup restricted to that campaign (`source` in the payload says which table).
    Without a campaign filter the chart reads the channel rollup only.

Contract, same as leads_data / sources_data: never raises into the caller. A query that fails degrades only its own
panel/tile; parameter NAMES are whitelisted before the cache (lesson L-098) and VALUES against what the database
holds; the sort key comes from a fixed dict; one read-only REPEATABLE READ snapshot per request so tiles, chart, table
and CSV describe the same rows even while a refresh commits.

CODE LAYOUT (clean-code pass 3: this module was split, behaviour unchanged). This file keeps the store, the payload
assembly and every function that reads a tuning constant a test patches (LOW_N, DEFAULT_DAYS, CHART_MAX_DAYS, CHART_MAX_WEEKS,
CAMPAIGN_ROWS_MAX, REFRESH_TEXT, engine and the *_sql builders collect() calls), so those patches still take effect. The rest
lives in siblings, lowest layer first, and is re-exported below:
  channels_request  the whitelisted parameters, Request / View, parse_request
  channels_money    measures, currency-aware money cells and deltas, sorting
  channels_text     date-range wording, page states, install / empty-state help
  channels_series   daily / weekly series, grain, coverage text, filter options
  channels_queries  table + SQL-fragment constants, query builders, read_state / lookups
  channels_csv      CSV cells, column sets and row builders
"""
from __future__ import annotations

import csv as csv
import dataclasses as dataclasses
import io as io
import logging
import math as math
import re as re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass as dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable as Callable, Collection, Mapping
from desktop import leads_data as ld
from desktop.flow_data import _iso, _redact
from erp import config as cfg
from erp.db import read_connect, read_engine as engine
from erp.marketing import rollup as mroll  # noqa: F401
from erp.marketing import schema as mschema
from erp.marketing.model import DEFAULT_CURRENCY as DEFAULT_CURRENCY
# Names that moved into the sibling modules; re-exported here (`x as x` = explicit re-export) so every importer of this module keeps working.
from desktop.channels_request import (
    MAX_SPAN_DAYS as MAX_SPAN_DAYS, MAX_VALUE_LEN as MAX_VALUE_LEN, MAX_CHANNEL_VALUES as MAX_CHANNEL_VALUES,
    KNOWN_PARAMS as KNOWN_PARAMS, CSV_PARAMS as CSV_PARAMS, CSV_PARTS as CSV_PARTS, CSV_FILE_LABEL as CSV_FILE_LABEL,
    _INT64_MAX as _INT64_MAX, _CURRENCY_RE as _CURRENCY_RE, _DIGITS as _DIGITS, _rate_key as _rate_key,
    SORTS as SORTS, MONEY_SORT_CURRENCY as MONEY_SORT_CURRENCY, DEFAULT_SORT as DEFAULT_SORT,
    DEFAULT_DIR as DEFAULT_DIR, Request as Request, View as View, _values as _values,
    unknown_params as unknown_params, parse_request as parse_request, focus_in_filter as focus_in_filter,
    view_query as view_query,
)
from desktop.channels_money import (
    MICROS as MICROS, MEASURES as MEASURES, ZERO_DECIMAL_CURRENCIES as ZERO_DECIMAL_CURRENCIES, measures as measures,
    money as money, money_text as money_text, _cur as _cur, legacy_money_rows as legacy_money_rows,
    group_money as group_money, money_mode as money_mode, derived_state as derived_state, MULTI_WHY as MULTI_WHY,
    money_cell as money_cell, unavailable_money as unavailable_money, sort_rows as sort_rows, _pct_text as _pct_text,
    money_delta as money_delta, spend_delta as spend_delta, money_series as money_series, td_failed as td_failed,
    _short_err as _short_err, money_summary as money_summary,
)
from desktop.channels_text import (
    PSQL_07 as PSQL_07, PSQL_09 as PSQL_09, PSQL_10 as PSQL_10, PSQL_NOTE as PSQL_NOTE, CMD_LOAD as CMD_LOAD,
    LOAD_NOTE as LOAD_NOTE, CMD_ROLLUP as CMD_ROLLUP, STATE_NOT_INSTALLED as STATE_NOT_INSTALLED,
    STATE_EMPTY as STATE_EMPTY, STATE_NO_MATCH as STATE_NO_MATCH, STATE_READY as STATE_READY, _MONTHS as _MONTHS,
    day_text as day_text, range_text as range_text, page_state as page_state, install_help as install_help,
    empty_help as empty_help, _metric_word as _metric_word,
)
from desktop.channels_series import (
    PALETTE_SLOTS as PALETTE_SLOTS, _dir as _dir, daily_series as daily_series, _spark as _spark,
    grain_of as grain_of, split_grain as split_grain, periods as periods, grain_mode as grain_mode,
    week_axis as week_axis, weekly_series as weekly_series, channel_lookup as channel_lookup,
    coverage_text as coverage_text, build_options as build_options, public_options as public_options,
)
from desktop.channels_queries import (
    DAILY_ROWS_MAX as DAILY_ROWS_MAX, CHANNEL_TABLE as CHANNEL_TABLE, CAMPAIGN_TABLE as CAMPAIGN_TABLE,
    RUN_TABLE as RUN_TABLE, CHANNEL_DIM as CHANNEL_DIM, CAMPAIGN_DIM as CAMPAIGN_DIM,
    REQUIRED_TABLES as REQUIRED_TABLES, SUMS_SQL as SUMS_SQL, MONEY_ONLY_SQL as MONEY_ONLY_SQL,
    MONEY_SUMS_SQL as MONEY_SUMS_SQL, PERIODS_SQL as PERIODS_SQL, CURRENCY_SQL as CURRENCY_SQL,
    INSTALL_SQL as INSTALL_SQL, _t as _t, where_sql as where_sql, source_table as source_table,
    totals_sql as totals_sql, channels_sql as channels_sql, daily_sql as daily_sql, MONEY_GROUPS as MONEY_GROUPS,
    money_sql as money_sql, campaigns_sql as campaigns_sql, read_state as read_state,
    campaign_lookup as campaign_lookup, campaign_name as campaign_name,
)
from desktop.channels_csv import (
    csv_cell as csv_cell, to_csv as to_csv, _pct as _pct, MIXED_CURRENCY as MIXED_CURRENCY,
    CHANNEL_CSV as CHANNEL_CSV, DAILY_CSV as DAILY_CSV, CAMPAIGN_CSV as CAMPAIGN_CSV, CSV_EXCLUDED as CSV_EXCLUDED,
    csv_money as csv_money, channel_csv_rows as channel_csv_rows, daily_csv_rows as daily_csv_rows,
    campaign_csv_rows as campaign_csv_rows,
)

logger = logging.getLogger("erp_desk.channels")

LOW_N = ld.LOW_N                 # a percentage needs a denominator of at least this many; below it the page shows "2 of 3"
DEFAULT_DAYS = 30                # the range when the request gives none: this many days ending at the newest rollup day

CHART_MAX_DAYS = 180             # the chart and the tile sparklines show at most the newest this-many days of the range
CHART_MAX_WEEKS = 104            # a weekly source's per-week chart shows at most the newest this-many weeks (bars): its rows are few, so it is limited by COUNT, not by the per-day window
CHART_MAX_LINES = 8              # channels drawn as their own line; the rest are summed into one "Other channels" line

CHANNEL_ROWS_MAX = 60            # rows of the per-channel table
CAMPAIGN_ROWS_MAX = 25           # rows of the campaign drill-down; "N more, narrow the filters" beyond it
EXPORT_MAX_ROWS = 50_000
CACHE_SECONDS = 3.0
CACHE_KEYS_MAX = 24
QUERY_BUDGET_SECONDS = 6.0       # all queries of one payload together (L-083, L-090)
EXPORT_BUDGET_SECONDS = 20.0
BUILD_WAIT_SECONDS = 6.0         # a request never parks longer than this waiting for a free build slot (L-091)
MAX_CONCURRENT_BUILDS = 3

STALE_AFTER_DAYS = 2             # the rollup counts as "not refreshed lately" beyond this many days

REFRESH_TEXT = ("refreshed by python -m erp.marketing.rollup (run_marketing_rollup.bat), not scheduled unless the owner "
                "schedules it")

CHART_METRICS = (("sessions", "Sessions"), ("clicks", "Clicks"), ("conversions", "Conversions"), ("impressions", "Impressions"))


def resolve_view(req: Request, options: dict, ids: Mapping[str, int]) -> View:
    """Fill in the range. With no from/to: DEFAULT_DAYS days ending at the newest day the rollup holds."""
    anchor = date.fromisoformat(options["last_day"]) if options.get("last_day") else date.today()
    defaulted = False
    d_from, d_to = req.d_from, req.d_to
    if d_from and d_to:
        pass
    elif d_from:
        d_to = max(anchor, d_from)
    elif d_to:
        d_from = d_to - timedelta(days=DEFAULT_DAYS - 1)
    else:
        d_to, d_from, defaulted = anchor, anchor - timedelta(days=DEFAULT_DAYS - 1), True
    days = (d_to - d_from).days + 1
    prev_to = d_from - timedelta(days=1)
    return View(req=req, d_from=d_from, d_to=d_to, days=days, defaulted=defaulted, prev_from=prev_to - timedelta(days=days - 1),
                prev_to=prev_to, channel_ids=tuple(ids[k] for k in req.channels if k in ids), campaign_id=req.campaign,
                focus_id=ids.get(req.focus) if req.focus else None, currency=req.currency)


def ratio(part: int, whole: int, why: str) -> dict:
    """part / whole computed from sums. `pct` is None when the whole is below LOW_N (the page then shows "2 of 3") and
    `value` is None when it is 0 (`why` says what is missing). `value` is kept for sorting only."""
    part, whole = int(part or 0), int(whole or 0)
    if whole <= 0:
        return {"n": part, "of": whole, "pct": None, "value": None, "low_n": False, "why": why}
    pct = None if whole < LOW_N else round(part / whole * 100, 3)
    if pct == 0 and part > 0:
        pct = 0.001                 # a real but tiny share must not read as "0%": the page prints it as "<0.1%"
    return {"n": part, "of": whole, "pct": pct, "value": part / whole, "low_n": whole < LOW_N, "why": None}


def cost_per_conversion(spend_micros: int, conversions: int, currency: str | None = None) -> dict:
    spend_micros, conversions = int(spend_micros or 0), int(conversions or 0)
    if spend_micros <= 0:
        return {"state": "none", "value": None, "n": conversions, "low_n": False, "why": "no spend field"}
    if conversions <= 0:
        return {"state": "none", "value": None, "n": 0, "low_n": False, "why": "no conversions"}
    out = {"state": "ok", "value": round(spend_micros / MICROS / conversions, 2), "n": conversions,
           "low_n": conversions < LOW_N, "why": None}
    if currency:
        out["currency"] = currency
    return out


def cpa_cell(groups: Mapping) -> dict:
    """Cost per conversion, per currency: that currency's spend divided by the conversions on that currency's rows."""
    parts = []
    for c, g in groups.items():
        if g["spend_micros"] > 0:
            one = cost_per_conversion(g["spend_micros"], g["conversions"], c)
            parts.append({"currency": c, "value": one["value"], "n": one["n"], "low_n": one["low_n"], "why": one["why"]})
    if not parts:
        return {"state": "none", "value": None, "currency": None, "n": 0, "low_n": False, "parts": [], "why": "no spend field"}
    if len(parts) == 1:
        p = parts[0]
        if p["value"] is None:
            return {"state": "none", "value": None, "currency": p["currency"], "n": 0, "low_n": False, "parts": parts, "why": p["why"]}
        return {"state": "ok", "value": p["value"], "currency": p["currency"], "n": p["n"], "low_n": p["low_n"], "parts": parts, "why": None}
    return {"state": "multi", "value": None, "currency": None, "n": 0, "low_n": False, "parts": parts, "why": MULTI_WHY}


def money_cells(groups: Mapping) -> dict:
    return {"spend": money_cell(groups, "spend_micros"), "revenue": money_cell(groups, "revenue_micros"), "cpa": cpa_cell(groups)}


def derive(m: Mapping) -> dict:
    """The rates of one group of COUNTS - the SAME function for a channel row, a campaign row, the total row and the tiles.
    (Money is derived per currency by money_cells(), never here.)"""
    return {"ctr": ratio(m["clicks"], m["impressions"], "no impressions"),
            "cvr": ratio(m["conversions"], m["clicks"], "no clicks")}


def share(part: int, whole: int) -> dict:
    r = ratio(part, whole, "no sessions")
    r["frac"] = (part / whole) if whole > 0 else 0.0
    return r


def low_volume(m: Mapping) -> bool:
    """'Low volume' = too few sessions AND too few clicks to trust a rate. A source that reports no sessions at all (an e-mail export)
    but has thousands of clicks is not low volume: judging it by sessions alone would flag a 150,000-click channel."""
    return m["sessions"] < LOW_N and m["clicks"] < LOW_N


def count_delta(cur: int, prev: Mapping | None | dict, metric: str, days: int, good_up: bool | None = True) -> dict:
    """Change against the previous period of the same length. `prev` is the previous period's totals row, {"error": ..}
    when it could not be read, or None. Percent only when the previous number is at least LOW_N."""
    label = f"the previous {days} day{'s' if days != 1 else ''}"
    if prev is None or (isinstance(prev, dict) and "error" in prev):
        return {"state": "unavailable", "dir": None, "text": "previous period could not be read", "pct": None, "good": None}
    if not int(prev.get("days_with_data") or 0):
        return {"state": "none", "dir": None, "text": "no data in the previous period", "pct": None, "good": None}
    p = int(prev[metric] or 0)
    d = _dir(cur, p)

    def good() -> bool | None:
        return None if (good_up is None or d == "flat") else ((d == "up") == good_up)

    if p < LOW_N:
        return {"state": "counts", "dir": d, "text": f"{cur:,} now, {p:,} in {label}", "pct": None, "good": good()}
    pct = (cur - p) / p * 100
    return {"state": "ok", "dir": "flat" if abs(pct) < 0.05 else d, "text": f"{_pct_text(abs(pct))} vs {label}" if abs(pct) >= 0.05 else f"unchanged vs {label}",
            "pct": round(pct, 2), "good": None if abs(pct) < 0.05 else good()}


def rate_delta(cur: dict, prev_totals: Mapping | None | dict, num: str, den: str, days: int) -> dict:
    """Conversion-rate change in percentage POINTS, only when both periods have LOW_N or more clicks behind them."""
    label = f"the previous {days} day{'s' if days != 1 else ''}"
    if prev_totals is None or (isinstance(prev_totals, dict) and "error" in prev_totals):
        return {"state": "unavailable", "dir": None, "text": "previous period could not be read", "pct": None, "good": None}
    if not int(prev_totals.get("days_with_data") or 0):
        return {"state": "none", "dir": None, "text": "no data in the previous period", "pct": None, "good": None}
    p = ratio(prev_totals[num], prev_totals[den], "")
    if cur["pct"] is None or p["pct"] is None:
        return {"state": "none", "dir": None, "text": f"too few {den} (under {LOW_N}) to compare rates", "pct": None, "good": None}
    diff = cur["pct"] - p["pct"]
    d = "flat" if abs(diff) < 0.05 else ("up" if diff > 0 else "down")
    return {"state": "ok", "dir": d, "text": (f"{abs(diff):.1f} pt vs {label}" if d != "flat" else f"unchanged vs {label}"), "pct": round(diff, 2),
            "good": None if d == "flat" else d == "up"}


def chart_window(v: View) -> tuple[date, bool]:
    """(first day drawn, clipped?): the newest CHART_MAX_DAYS days of the range."""
    if v.days > CHART_MAX_DAYS:
        return v.d_to - timedelta(days=CHART_MAX_DAYS - 1), True
    return v.d_from, False


def build_kpis(totals, prev, daily, v: View, money=None, prev_money=None, daily_money=None) -> list[dict]:
    """Six tiles. Each degrades to 'unavailable' on its own: the value needs `totals`, the delta needs `prev`, the sparkline
    needs `daily`; losing the last two only removes the delta or the line. The two money tiles (spend, revenue) never add
    across currencies: one currency -> a value with its code, several -> `parts` per currency and no value, none -> "no field".
    `money` / `prev_money` / `daily_money` are the money blocks (rows or the {"error": ...} marker); a caller that passes none of
    them gets the totals rows' own spend_micros read in DEFAULT_CURRENCY."""
    def tile(id_, label, hint, **kw):
        t = {"id": id_, "label": label, "hint": hint, "state": "ok", "value": None, "text": None, "sub": "", "tone": "blue",
             "unit": None, "delta": None, "spark": None, "spark_kind": "line", "parts": []}
        t.update(kw)
        return t

    h = {"sessions": "Sum of the sessions column of the rollup for the days and channels in view.",
         "clicks": "Sum of the clicks column of the rollup for the days and channels in view.",
         "conversions": "Sum of the conversions column of the rollup for the days and channels in view.",
         "cvr": f"Conversions divided by clicks, from the summed columns. A percentage appears only with at least {LOW_N} clicks behind it.",
         "spend": "Sum of spend in the view, in the currency the source reported it (spend_micros / 1,000,000). Never converted and never added across currencies: with several currencies in view each is shown on its own line. Shown only when the source carries a spend field.",
         "revenue": "Sum of revenue in the view, in the currency the source reported it (revenue_micros / 1,000,000). Never converted and never added across currencies. Some connectors DERIVE revenue (purchases x value per purchase) because the export's own revenue column is empty: the tile says how much of it is derived."}
    labels = {"sessions": "Sessions", "clicks": "Clicks", "conversions": "Conversions", "cvr": "Conversion rate", "spend": "Spend", "revenue": "Revenue"}
    if td_failed(totals):
        bad = (totals or {}).get("error") if isinstance(totals, dict) else None
        return [tile(k, labels[k], h[k], state="unavailable", sub=_short_err(bad) or "could not be read", tone="grey") for k in labels]
    m = measures(totals)
    has_data = int(totals.get("days_with_data") or 0) > 0
    mode = grain_mode(totals)
    day_p, week_p = periods(totals)
    grain, days, series = "day", None, None
    if not td_failed(daily) and mode in ("day", "week"):
        lo, _clipped = chart_window(v)
        day_rows, week_rows = split_grain(daily)
        if mode == "day":
            days, series = daily_series(day_rows, lo, v.d_to)
        else:
            axis = week_axis([r["event_date"] for r in week_rows])[-CHART_MAX_WEEKS:]
            days, series, grain = [d.isoformat() for d in axis], weekly_series([r for r in week_rows if axis and r["event_date"] >= axis[0]], axis), "week"
    weeks = f"{week_p} week{'s' if week_p != 1 else ''}"
    span_note = (f"{weeks} of data, each counted on its start day" if mode == "week"
                 else f"{day_p} of {v.days} days and {weeks} have data" if mode == "mixed"
                 else f"{totals.get('days_with_data', 0)} of {v.days} days have data")
    tiles = []
    for k in ("sessions", "clicks", "conversions"):
        sp = _spark(days, series[k], grain) if series else None
        tiles.append(tile(k, labels[k], h[k], value=m[k], tone={"sessions": "blue", "clicks": "violet", "conversions": "green"}[k],
                          sub=(span_note if has_data else "no data in this range"), spark=sp,
                          delta=count_delta(m[k], prev, k, v.days) if has_data else None))
    cvr = ratio(m["conversions"], m["clicks"], "no clicks")
    rate_spark = None
    if series:
        vals = [None if (c is None or c < LOW_N or n is None) else round(n / c * 100, 2) for n, c in zip(series["conversions"], series["clicks"])]
        rate_spark = _spark(days, vals, grain)
    if cvr["pct"] is not None:
        tiles.append(tile("cvr", labels["cvr"], h["cvr"], value=cvr["pct"], unit="%", tone="amber", sub=f"{m['conversions']:,} conversions of {m['clicks']:,} clicks",
                          spark=rate_spark, delta=rate_delta(cvr, prev, "conversions", "clicks", v.days)))
    elif cvr["low_n"]:
        tiles.append(tile("cvr", labels["cvr"], h["cvr"], text=f"{m['conversions']} of {m['clicks']}", tone="amber",
                          sub=f"conversions of clicks - too few clicks (under {LOW_N}) for a percentage"))
    else:
        tiles.append(tile("cvr", labels["cvr"], h["cvr"], state="none", text="-", tone="grey",
                          sub="no clicks in this view, so no rate" if has_data else "no data in this range"))

    # ---- money: never added across currencies
    if money is None:
        money = legacy_money_rows([totals])
        prev_money = prev if td_failed(prev) else legacy_money_rows([prev])
        daily_money = daily
    if td_failed(money):
        for key in ("spend", "revenue"):
            tiles.append(tile(key, labels[key], h[key], state="unavailable", tone="grey", sub="the money figures could not be read"))
        return tiles
    groups = group_money(money)
    prev_groups = None if td_failed(prev_money) else group_money(prev_money)
    dm = None if (td_failed(daily_money) or mode not in ("day", "week")) else daily_money
    for key, col, tone in (("spend", "spend_micros", "coral"), ("revenue", "revenue_micros", "ink")):
        cell = money_cell(groups, col)
        if cell["state"] == "ok":
            cur = cell["currency"]
            if td_failed(prev) or prev_groups is None:
                pm = prev if td_failed(prev) else {"error": "the previous period's money could not be read"}
            else:
                pm = {"days_with_data": prev.get("days_with_data"), col: prev_groups.get(cur, {}).get(col, 0)}
            spark = None
            if dm is not None and days:
                spark = _spark(days, money_series(dm, cur, col, days), grain)
            if key == "spend":
                one = cost_per_conversion(groups[cur]["spend_micros"], groups[cur]["conversions"], cur)
                sub = (f"cost per conversion {money_text(one['value'], cur)}" + (f" (n = {one['n']})" if one["low_n"] else "")
                       if one["state"] == "ok" else "no conversions to divide by")
            else:
                d = cell.get("derived")
                sub = ("DERIVED from purchases x value per purchase - the source did not report it" if d == "all"
                       else "partly derived from purchases x value per purchase" if d == "partly" else f"as loaded from the source, in {cur}")
            tiles.append(tile(key, labels[key], h[key], value=cell["value"], unit=cur, tone=tone, sub=sub, spark=spark,
                              delta=money_delta(groups[cur][col], pm, v.days, col, key) if has_data else None, parts=cell["parts"]))
        elif cell["state"] == "multi":
            codes = [p["currency"] for p in cell["parts"]]
            note = f"{len(codes)} currencies ({', '.join(codes)}): never added or converted. Choose one in the filter bar for a trend."
            if key == "revenue" and any(p.get("derived") for p in cell["parts"]):
                note += " Derived = computed from purchases x value per purchase."
            tiles.append(tile(key, labels[key], h[key], state="multi", tone=tone, parts=cell["parts"], sub=note))
        else:
            tiles.append(tile(key, labels[key], h[key], state="none", text="-", tone="grey",
                              sub=(f"{cell['why']}: every row carries 0 {key}, so this source does not report it" if has_data else "no data in this range")))
    return tiles


def build_table(rows: list[dict], options: Mapping, v: View, cap: int | None = CHANNEL_ROWS_MAX, money=None) -> dict:
    """[{channel_id, days_with_data, <counts>}] -> one row per channel + a total row, sorted by the whitelisted key.
    `money` is [{channel_id, currency, spend_micros, revenue_micros, revenue_derived_micros, conversions}] (one row per channel
    and currency) or the {"error": ...} marker of a failed query; money is added within a currency only, so a channel row shows
    ONE amount with its code, or the amounts per currency, and the total row does the same across the table."""
    dims = channel_lookup(options)
    ms = [(r, measures(r)) for r in rows]
    if money is None:
        money = legacy_money_rows(rows)
    failed = td_failed(money)
    by_channel: dict[int, list] = {}
    for mr in ([] if failed else money):
        by_channel.setdefault(int(mr["channel_id"]), []).append(mr)
    total_sessions = sum(m["sessions"] for _r, m in ms)
    out = []
    for r, m in ms:
        info = dims.get(r["channel_id"]) or {"key": f"id:{r['channel_id']}", "name": f"Channel #{r['channel_id']}", "medium": None, "is_paid": None, "slot": 0}
        row = {"key": info["key"], "name": info["name"], "medium": info.get("medium"), "is_paid": info.get("is_paid"), "slot": info["slot"],
               "days_with_data": int(r.get("days_with_data") or 0), "low_n": low_volume(m), **m,
               "share": share(m["sessions"], total_sessions)}
        row.update(derive(m))
        row.update(unavailable_money() if failed else money_cells(group_money(by_channel.get(int(r["channel_id"]), []))))
        out.append(row)
    ordered = sort_rows(out, v.req.sort, v.req.dir)
    shown = ordered if cap is None else ordered[:cap]
    more = len(ordered) - len(shown)
    tot = {m: sum(r[m] for r in out) for m in MEASURES}
    all_groups = {} if failed else group_money([mr for mrs in by_channel.values() for mr in mrs])
    total = {"name": "All channels in view", "channels": len(out), **tot, "share": share(total_sessions, total_sessions), **derive(tot),
             **(unavailable_money() if failed else money_cells(all_groups))}
    return {"rows": shown, "more": more, "total": total, "channels": len(out),
            "sort": v.req.sort, "dir": v.req.dir, "money_mode": "unavailable" if failed else money_mode(all_groups),
            "currencies": [] if failed else list(all_groups)}


def build_chart(rows: list[dict], options: Mapping, v: View) -> dict:
    """[{event_date, channel_id, grain, <counts>}] -> per-day series per channel for the metric switch, PLUS per-week series for a
    weekly source. Reads the channel rollup only (unless a campaign filter forces the campaign table). The CHART_MAX_LINES
    largest channels get their own line; the rest are one summed 'Other channels' line. A day without a row is None: the line
    breaks, it does not drop to zero.

    GRAIN. A weekly source's row carries a whole week and sits on the week's START day. Drawn on the per-day axis it would be a
    false one-day spike, so weekly rows never touch `values` (per day): they go to `week_values`, one entry per week start along
    `weeks`, which the page draws as bars spanning their 7 days in a separate chart."""
    lo, clipped = chart_window(v)
    days, _ = daily_series([], lo, v.d_to)
    idx = {d: i for i, d in enumerate(days)}
    dims = channel_lookup(options)
    day_rows, week_rows = split_grain(rows)
    week_rows = [r for r in week_rows if v.d_from <= r["event_date"] <= v.d_to]           # the query already bounds them; a stray row is never drawn
    all_weeks = week_axis([r["event_date"] for r in week_rows])
    week_clipped = len(all_weeks) > CHART_MAX_WEEKS
    weeks = all_weeks[-CHART_MAX_WEEKS:]
    week_rows = [r for r in week_rows if weeks and weeks[0] <= r["event_date"] <= v.d_to]
    widx = {d.isoformat(): i for i, d in enumerate(weeks)}
    per: dict[int, dict[str, list]] = {}
    perw: dict[int, dict[str, list]] = {}
    for r in day_rows:
        i = idx.get(r["event_date"].isoformat())
        if i is None:
            continue
        ch = per.setdefault(r["channel_id"], {m: [None] * len(days) for m in MEASURES})
        for m in MEASURES:
            ch[m][i] = (ch[m][i] or 0) + int(r.get(m) or 0)
    for r in week_rows:
        j = widx.get(r["event_date"].isoformat())
        if j is None:
            continue
        ch = perw.setdefault(r["channel_id"], {m: [None] * len(weeks) for m in MEASURES})
        for m in MEASURES:
            ch[m][j] = (ch[m][j] or 0) + int(r.get(m) or 0)
    chans = set(per) | set(perw)

    def vals(store: dict, cid: int, metric: str, n: int) -> list:
        return store[cid][metric] if cid in store else [None] * n

    def total(cid: int, metric: str) -> int:
        return sum(x or 0 for x in vals(per, cid, metric, len(days))) + sum(x or 0 for x in vals(perw, cid, metric, len(weeks)))
    rank = sorted(chans, key=lambda c: (-total(c, "sessions"), -total(c, "clicks"), -total(c, "impressions"), c))
    top, rest = rank[:CHART_MAX_LINES], rank[CHART_MAX_LINES:]

    def line(cid: int) -> dict:
        info = dims.get(cid) or {"key": f"id:{cid}", "name": f"Channel #{cid}", "slot": 0}
        return {"key": info["key"], "name": info["name"], "slot": info["slot"], "values": {k: vals(per, cid, k, len(days)) for k, _l in CHART_METRICS},
                "week_values": {k: vals(perw, cid, k, len(weeks)) for k, _l in CHART_METRICS} if weeks else None}
    series = [line(c) for c in top]
    if rest:
        other = {k: [None] * len(days) for k, _l in CHART_METRICS}
        other_w = {k: [None] * len(weeks) for k, _l in CHART_METRICS}
        for c in rest:
            for k, _l in CHART_METRICS:
                for store, into, n in ((per, other, len(days)), (perw, other_w, len(weeks))):
                    for i, x in enumerate(vals(store, c, k, n)):
                        if x is not None:
                            into[k][i] = (into[k][i] or 0) + x
        series.append({"key": "_other", "name": f"Other channels ({len(rest)})", "slot": -1, "values": other, "week_values": other_w if weeks else None})
    metrics = [{"key": k, "label": lab, "total": sum(total(c, k) for c in chans)} for k, lab in CHART_METRICS]
    # the metric a viewer sees first: sessions, else the first one that has anything
    first = next((m["key"] for m in metrics if m["total"] > 0), "sessions")
    return {"days": days, "series": series, "metrics": metrics, "default_metric": first, "clipped": clipped, "from": lo.isoformat(),
            "to": v.d_to.isoformat(), "lines_hidden": len(rest), "channels": len(rank),
            "days_with_data": sum(1 for i in range(len(days)) if any(per[c]["sessions"][i] is not None for c in per)),
            "has_daily": bool(day_rows), "has_weekly": bool(week_rows), "weeks": [d.isoformat() for d in weeks], "week_days": 7, "week_clipped": week_clipped,
            "weeks_with_data": sum(1 for j in range(len(weeks)) if any(perw[c]["sessions"][j] is not None for c in perw))}


def build_campaigns(block, options: Mapping, v: View, chan_sessions: int | None, money=None) -> dict:
    """The drill-down for ONE focus channel. `block` = {"rows": [...], "names": [...]} from the campaign rollup; `money` = the
    money rows of those campaigns per currency (or the {"error": ...} marker), handled exactly like the channel table's."""
    focus = next((c for c in options.get("_channels", []) if c["id"] == v.focus_id), None)
    if v.focus_id is None or focus is None:
        return {"state": "need_focus", "rows": [], "focus": None}
    if td_failed(block):
        return {"state": "unavailable", "error": (block or {}).get("error", "could not be read") if isinstance(block, dict) else "could not be read",
                "focus": {"key": focus["key"], "name": focus["name"]}, "rows": []}
    names = {int(n["campaign_id"]): (n.get("name") or n.get("campaign_key") or f"Campaign #{n['campaign_id']}") for n in block["names"]}
    rows_in = block["rows"]
    if money is None:
        money = legacy_money_rows(rows_in)
    failed = td_failed(money)
    by_campaign: dict[int, list] = {}
    for mr in ([] if failed else money):
        by_campaign.setdefault(int(mr["campaign_id"]), []).append(mr)
    total_n = int(rows_in[0]["total_campaigns"]) if rows_in else 0
    out = []
    for r in rows_in:
        m = measures(r)
        cid = int(r["campaign_id"])
        row = {"campaign_id": cid, "name": "(no campaign)" if cid == 0 else names.get(cid, f"Campaign #{cid}"), "days_with_data": int(r.get("days_with_data") or 0),
               "low_n": low_volume(m), **m,
               "share": share(m["sessions"], chan_sessions or 0)}
        row.update(derive(m))
        row.update(unavailable_money() if failed else money_cells(group_money(by_campaign.get(cid, []))))
        out.append(row)
    shown = len(out)
    rest = None
    if chan_sessions is not None and total_n > shown:
        rest = max(0, chan_sessions - sum(r["sessions"] for r in out))
    return {"state": "ok" if out else "empty", "focus": {"key": focus["key"], "name": focus["name"]}, "rows": out, "total_campaigns": total_n,
            "shown": shown, "more": max(0, total_n - shown), "cap": CAMPAIGN_ROWS_MAX, "rest_sessions": rest,
            "channel_sessions": chan_sessions}


def build_headline(state: str, v: View | None, totals, prev, table: dict | None, tz: str | None) -> dict:
    """One plain sentence + one line under it, computed on the server. First rule that matches wins."""
    if state == STATE_NOT_INSTALLED:
        return {"text": "The channel report is not installed yet.", "sub": "The rollup tables it reads are missing from this database, so there is nothing to show - and nothing is being made up.", "tone": "grey"}
    if state == STATE_EMPTY:
        return {"text": "No marketing data has been loaded yet.", "sub": "The tables are installed but hold no rows. The numbers appear after data is loaded and the rollup is built.", "tone": "grey"}
    span = range_text(v.d_from, v.d_to)
    zone = f"database time zone {tz}" if tz else "database time zone unknown"
    if state == STATE_NO_MATCH:
        return {"text": "Nothing matches these filters.", "sub": f"No channel has a rollup row {span}. Loosen a filter to bring data back ({zone}).", "tone": "amber"}
    if td_failed(totals):
        return {"text": "The channel numbers could not be read right now.", "sub": "The other panels keep working where they can; this page keeps trying by itself.", "tone": "red"}
    m = measures(totals)
    metric = next((k for k in ("sessions", "clicks", "conversions") if m[k] > 0), None)
    coverage = coverage_text(totals, v)
    if metric is None:
        return {"text": f"No sessions, clicks or conversions are recorded {span}.", "sub": f"{coverage}; impressions and spend may still be present below ({zone}).", "tone": "amber"}
    n = m[metric]
    delta = count_delta(n, prev, metric, v.days)
    if delta["state"] == "ok":
        trend = "level with" if delta["dir"] == "flat" else f"{'up' if delta['dir'] == 'up' else 'down'} {_pct_text(abs(delta['pct']))} on"
        trend_txt = f", {trend} the previous {v.days} day{'s' if v.days != 1 else ''}"
    elif delta["state"] == "counts":
        trend_txt = f", against {delta['text'].split(', ')[1]}"
    elif delta["state"] == "none":
        trend_txt = " (no data in the previous period to compare with)"
    else:
        trend_txt = ""
    lead = ""
    rows = sorted((table or {}).get("rows") or [], key=lambda r: (-r[metric], r["name"].lower()))
    if rows:
        top = rows[0]
        if top[metric] > 0:
            s = ratio(top[metric], n, "")
            lead = f" {top['name']} brought {_pct_text(s['pct'])} of them." if s["pct"] is not None else f" {top['name']} brought {top[metric]:,} of them."
    return {"text": f"{n:,} {_metric_word(metric, n)} {span}{trend_txt}.{lead}",
            "sub": f"{coverage} - dates as reported by the sources, {zone}.", "tone": "blue"}


# ============================================================================ freshness (pure)
def build_freshness(data_as_of, journal, now: datetime) -> dict:
    """When the ROLLUP was last recomputed (max refreshed_at) and what the journal says - both from the small tables."""
    last_ok = journal.get("last_ok_at") if isinstance(journal, dict) and "error" not in journal else None
    age = None
    note = None
    if isinstance(data_as_of, datetime):
        ref = data_as_of if data_as_of.tzinfo else data_as_of.astimezone()
        age = max(0.0, (now - ref.astimezone(timezone.utc)).total_seconds() / 86400)
        if age > STALE_AFTER_DAYS:
            note = (f"The rollup was last recomputed {int(age)} days ago. Anything loaded since then is not in these numbers until "
                    f"{CMD_ROLLUP.replace(' --full --yes', '')} runs (it is not scheduled unless the owner schedules it).")
    return {"data_as_of": _iso(data_as_of), "last_ok_run_at": _iso(last_ok), "runs": (journal or {}).get("runs") if isinstance(journal, dict) and "error" not in journal else None,
            "last_status": (journal or {}).get("last_status") if isinstance(journal, dict) and "error" not in journal else None,
            "age_days": None if age is None else round(age, 1), "stale": bool(note), "note": note, "text": REFRESH_TEXT}


# ============================================================================ definitions (generated from the constants)
def build_definitions(tz: str | None, schema: str) -> list[dict]:
    """The text of the "Definitions" drawer, built from the same constants the numbers use (lesson L-085)."""
    zone = tz or "the database session's time zone (not readable right now)"
    return [
        {"id": "view", "term": "The data in view", "text": f"Rows of the daily rollup whose day falls in the chosen range (both ends included) for the chosen channels, campaign and currency. Different filters combine with AND; several channels combine with OR. With no range set the page shows the last {DEFAULT_DAYS} days ending at the newest day the rollup holds, so a report over older data still shows numbers. A range is at most {MAX_SPAN_DAYS:,} days."},
        {"id": "zone", "term": "Dates and time zone", "text": f"Every day on this page is an event_date exactly as the source reported it - a calendar date, not a moment, so it is not converted. The database session runs in {zone}; that zone decides what 'newest' and the refresh times below mean, and it is printed under the headline next to the range. The CSV files carry the range and the zone in columns."},
        {"id": "rollup", "term": "Where the numbers come from", "text": f"Two small tables, refreshed by a job, never the raw events. {CHANNEL_TABLE}: one row per day x channel x currency x grain. {CAMPAIGN_TABLE}: one row per day x channel x campaign x currency x grain (campaign 0 = the rows that had no campaign). Both hold additive counts and sums only, so any slice can be re-added exactly - counts across everything, money within one currency. The page reads them in schema '{schema}'; it also looks up channel and campaign names ({CHANNEL_DIM}, {CAMPAIGN_DIM}) and the refresh journal ({RUN_TABLE}), all tiny. It never reads interaction_fact or marketing_landing, so opening it costs the same at ten thousand events as at ten billion."},
        {"id": "source", "term": "Which table a panel reads", "text": f"Tiles, chart and table read {CHANNEL_TABLE}. The campaign drill-down reads {CAMPAIGN_TABLE} for the one channel you selected. A channel table cannot be filtered by campaign, so when a campaign filter is set the tiles, chart and table read {CAMPAIGN_TABLE} restricted to that campaign instead; the header line says which table is in use."},
        {"id": "sessions", "term": "Sessions, clicks, impressions, conversions", "text": "Sums of the columns of the same names: numbers carried ON the source's rows (a daily export row can carry 40 clicks). They are not counts of rows. A source that does not report a column contributes 0 to it, so an all-zero column means 'not reported', and the page then shows 'no <thing>' rather than a 0% rate. These counts have no currency and add across every source. What a source means by them can differ (an e-mail 'delivered' is filed under impressions, a unique open under reactions): the connector's documentation says so."},
        {"id": "ctr", "term": "CTR", "text": f"Clicks divided by impressions, computed from the sums at read time (never an average of daily rates). Below {LOW_N} impressions the page shows the counts ('2 of 3'); with none it says 'no impressions'."},
        {"id": "cvr", "term": "Conversion rate", "text": f"Conversions divided by clicks, from the sums (the definition of docs/marketing-data-architecture.md section 8). Below {LOW_N} clicks the page shows the counts; with no clicks it says 'no clicks', because a rate of zero clicks does not exist."},
        {"id": "currency", "term": "Currency", "text": f"Money is shown in the currency the source reported it in and is never converted: this project has no exchange rates, and 1,000,000 VND is about 35 EUR, so a total of the two would mean nothing. The rollup keeps the currency in its key and this page adds money only inside one currency. When the view holds money in more than one currency, every spend and revenue figure is shown per currency with its code and nothing is added across them: a tile lists each currency on its own line, a table cell does the same, sorting by a money column orders within each currency (currencies in code order), and the CSV writes {MIXED_CURRENCY} with the amounts left blank. Choose a currency in the filter bar to narrow the whole view to it - the counts too, because a currency belongs to the rows of one source - and spend, revenue, cost per conversion, the trend and the change against the previous period are then all in that one currency. A currency that carries no money (a source with no spend field) does not make a view mixed."},
        {"id": "spend", "term": "Spend and cost per conversion", "text": f"Spend is in the source's own currency (the source's amount x 1,000,000 is what the database stores); see Currency. The column cannot tell 'nothing was spent' from 'this source has no spend field', so a channel with 0 spend shows 'no spend field', never 0.00, and cost per conversion (spend divided by the conversions on the rows of the same currency) is shown only where spend exists. A cost based on fewer than {LOW_N} conversions is marked with its n."},
        {"id": "revenue", "term": "Revenue", "text": "Summed like spend, per currency, and shown only where a source carries a revenue field ('no revenue field' otherwise). It is whatever the connector loaded: some sources report it, and the ad_performance connector DERIVES it (purchases x value per purchase) because that export's own Revenue column is empty. The rollup keeps the derived part of the sum in its own column, so a derived figure is labelled 'derived' (or 'partly derived') on the tile and in the table and is written in the CSV column revenue_derived - it is never presented as a reported number."},
        {"id": "grain", "term": "Weekly sources", "text": "A source that exports a whole week per row is stored on the week's START day and is the rollup's grain 'week'. Its numbers cover 7 days, so the per-day chart never draws them (that would be a false one-day spike): they get their own per-week chart, one bar per week spanning its 7 days. A tile sparkline uses the one grain the view has, says so, and is left out when daily and weekly rows are mixed. Coverage counts weeks ('3 weeks of weekly data, each counted on its start day'), not days. A range that starts or ends inside a week includes or excludes that whole week by its start day: a week is never split over days, because the source did not give the split."},
        {"id": "share", "term": "Share of sessions", "text": f"A channel's sessions divided by all sessions in view. It is a percentage only when at least {LOW_N} sessions are in view; below that the bar still draws but the text is a count. Rows with fewer than {LOW_N} sessions AND fewer than {LOW_N} clicks are marked 'low volume' (a source that reports no sessions but has clicks is not): read their rates as counts."},
        {"id": "delta", "term": "Change against the previous period", "text": f"The previous period is the same number of days immediately before the range. A change is a percentage only when the previous number was at least {LOW_N}; smaller ones read as counts ('3 now, 1 before'); when the previous period has no data the tile says so instead of showing +0%. A conversion-rate change is in percentage points and needs {LOW_N}+ clicks in both periods. Spend and revenue are compared only inside one currency, and only when the previous period recorded that money in that currency."},
        {"id": "gaps", "term": "Days without data", "text": "The refresh writes no zero rows, so a day with no row means either no activity or nothing loaded yet - the page cannot tell which. Sparklines and the chart therefore break the line at such a day instead of dropping to zero, and the header says 'N of M days in the range have data'."},
        {"id": "chart", "term": "Per-day chart", "text": f"One line per channel for the metric selected above it (the {CHART_MAX_LINES} largest channels of the range; the rest are added into one 'Other channels' line), at most the newest {CHART_MAX_DAYS} days of the range, from the sources that report daily. Sources that report weekly are drawn in the per-week chart below it, at most the newest {CHART_MAX_WEEKS} weeks (see 'Weekly sources'). Reads the channel rollup only (see 'Which table a panel reads'). The chart shows counts only, so it needs no currency."},
        {"id": "table", "term": "Channel table", "text": f"One row per channel in view plus a total row, sortable by clicking a heading (the sort happens on the server from a fixed list; a value that is unknown, such as the conversion rate of a channel with no clicks, always sorts last). Click a row to see its campaigns. At most {CHANNEL_ROWS_MAX} channels are listed. Money columns show an amount with its currency code, or one line per currency, and the total row never adds across currencies."},
        {"id": "campaigns", "term": "Campaign drill-down", "text": f"The campaigns of the selected channel, largest by sessions first, at most {CAMPAIGN_ROWS_MAX}; beyond that the page says how many more there are and asks you to narrow the filters (a shorter range, another channel). Shares are of that channel's sessions."},
        {"id": "refresh", "term": "How fresh is this?", "text": f"The rollup is {REFRESH_TEXT}. The page shows when the rollup rows were last recomputed and what the journal ({RUN_TABLE}) says, and warns when that is more than {STALE_AFTER_DAYS} days ago. It reads the database when you open it, change a filter or press Refresh (results are reused for {CACHE_SECONDS:g} s) and again every 30 s while it is open."},
        {"id": "csv", "term": "Download CSV", "text": f"The same filters as the screen. Three files: channels (the table), daily (one row per day, channel, grain and currency: the chart's data, with the money) and campaigns (the drill-down, needs a selected channel). Money columns carry a currency column; a row whose money spans several currencies says {MIXED_CURRENCY} and leaves its amounts blank, with money_by_currency spelling them out. UTF-8 with a byte-order mark, CRLF line ends, text cells starting with = + - @ ; tab or CR get a leading apostrophe. Rates are blank where the screen shows counts; spend and revenue are blank where the screen says 'no spend field'. Aggregate rows only: {CSV_EXCLUDED}. At most {EXPORT_MAX_ROWS:,} rows."},
        {"id": "limits", "term": "Time limits", "text": f"Every query is cut off by the database's statement timeout and all of them together after {QUERY_BUDGET_SECONDS:g} s; a panel that cannot be read says 'unavailable' while the rest keep working."},
    ]


def assemble(raw: dict, v: View, options: dict, meta: dict, now: datetime | None = None) -> dict:
    """Raw query blocks (each rows or {"error"}) -> the JSON the page draws. Pure: no I/O."""
    now = now or datetime.now(timezone.utc)
    tz = meta.get("tz")
    totals_rows = raw.get("totals")
    totals = None if td_failed(totals_rows) else (totals_rows[0] if totals_rows else None)
    prev_rows = raw.get("prev")
    prev = prev_rows if td_failed(prev_rows) else (prev_rows[0] if prev_rows else None)
    state = STATE_READY if td_failed(totals_rows) else page_state([], meta.get("rollup_rows"), totals)
    panels: dict[str, dict] = {}

    def panel(name: str, block, build):
        if td_failed(block):
            panels[name] = {"state": "unavailable", "error": (block or {}).get("error", "could not be read") if isinstance(block, dict) else "could not be read"}
            return
        try:
            panels[name] = {"state": "ok", **build(block)}
        except Exception as exc:  # noqa: BLE001 - a bug in one builder must not take the whole page down
            logger.exception("Channels feed: panel %s failed to build", name)
            panels[name] = {"state": "unavailable", "error": f"{type(exc).__name__}: {_redact(str(exc))}"}

    panel("table", raw.get("channels"), lambda b: build_table(b, options, v, money=raw.get("money_channels")))
    panel("chart", raw.get("daily"), lambda b: build_chart(b, options, v))
    if panels["table"]["state"] == "ok" and not panels["table"]["rows"]:
        panels["table"]["state"] = "empty"
    if panels["chart"]["state"] == "ok" and not any(m["total"] for m in panels["chart"]["metrics"]):
        panels["chart"]["state"] = "empty"
    table_ok = panels["table"] if panels["table"]["state"] in ("ok", "empty") else None
    if v.focus_id is None:
        panels["campaigns"] = {"state": "need_focus", "rows": [], "focus": None}
    elif not focus_in_filter(v):
        # a focus channel the channel filter has left out would answer "no campaign rows" - a wrong reason: say the real one
        f = next((c for c in options.get("_channels", []) if c["id"] == v.focus_id), None)
        panels["campaigns"] = {"state": "outside_filter", "rows": [], "focus": {"key": f["key"], "name": f["name"]} if f else None}
    else:
        chan_sessions = None if td_failed(raw.get("channels")) else sum(int(r.get("sessions") or 0) for r in raw["channels"] if r["channel_id"] == v.focus_id)
        panels["campaigns"] = build_campaigns(raw.get("campaigns"), options, v, chan_sessions, money=raw.get("money_campaigns"))

    tot_arg = totals_rows if td_failed(totals_rows) else (totals or {"days_with_data": 0, **{m: 0 for m in MEASURES}})
    kpis = build_kpis(tot_arg, prev, raw.get("daily"), v, money=raw.get("money_totals"), prev_money=raw.get("money_prev"),
                      daily_money=raw.get("money_daily")) if state == STATE_READY else []
    warnings: list[str] = []
    ch = panels["chart"]
    if ch["state"] == "ok" and ch.get("clipped"):
        warnings.append(f"The chart and sparklines show the newest {CHART_MAX_DAYS} days of the {v.days}-day range.")
    if ch["state"] == "ok" and ch.get("lines_hidden"):
        warnings.append(f"{ch['lines_hidden']} smaller channel{'s are' if ch['lines_hidden'] != 1 else ' is'} added into one 'Other channels' line; the table lists them all.")
    tb = panels["table"]
    if tb["state"] == "ok" and tb.get("more"):
        warnings.append(f"{tb['more']} more channel{'s' if tb['more'] != 1 else ''} not listed (the {CHANNEL_ROWS_MAX} largest are shown) - narrow the filters to see them.")
    if isinstance(raw.get("daily"), list) and len(raw["daily"]) > DAILY_ROWS_MAX:
        warnings.append(f"The chart was cut at {DAILY_ROWS_MAX:,} day/channel rows; narrow the range or the channels.")
    fresh = build_freshness(meta.get("data_as_of"), meta.get("journal"), now)
    if fresh["note"]:
        warnings.append(fresh["note"])
    if v.campaign_id is not None:
        warnings.append(f"A campaign filter is set ({meta.get('campaign_name') or '#' + str(v.campaign_id)}): {CHANNEL_TABLE} has no campaign column, so the tiles, "
                        f"chart and table read {CAMPAIGN_TABLE} restricted to that campaign instead.")
    money_info = money_summary(raw.get("money_totals"), v.currency)
    if money_info["mode"] == "multi" and not v.currency:
        codes = [c["code"] for c in money_info["currencies"]]
        warnings.append(f"This view holds money in {len(codes)} currencies ({', '.join(codes)}). Spend and revenue are shown per currency and are never added "
                        f"together or converted; choose a currency in the filter bar to narrow the whole view to one.")
    day_p, week_p = periods(totals)
    if week_p:
        warnings.append(f"This view includes a weekly source ({week_p} week start date{'s' if week_p != 1 else ''}). A week's numbers are counted on its start day and "
                        f"drawn in the per-week chart, never as a single day; a range that starts or ends inside a week includes or excludes that whole week.")
    headline = build_headline(state, v, tot_arg, prev, table_ok, tz)
    days_with = int(totals["days_with_data"]) if totals else 0
    all_ok = all(p["state"] in ("ok", "empty", "need_focus", "outside_filter") for p in panels.values()) and not td_failed(totals_rows) and not any(t["state"] == "unavailable" for t in kpis)
    return {
        "ok": all_ok, "state": state, "generated_at": _iso(now), "time_zone": tz, "schema": meta.get("schema"),
        "headline": headline, "install": None, "warnings": warnings, "freshness": fresh,
        "range": {"from": v.d_from.isoformat(), "to": v.d_to.isoformat(), "days": v.days, "days_with_data": days_with, "defaulted": v.defaulted,
                  "prev_from": v.prev_from.isoformat(), "prev_to": v.prev_to.isoformat(), "day_periods": day_p, "week_periods": week_p},
        "source": v.source_kind, "source_table": CHANNEL_TABLE if v.campaign_id is None else CAMPAIGN_TABLE,
        "filters": {"from": v.req.d_from.isoformat() if v.req.d_from else None, "to": v.req.d_to.isoformat() if v.req.d_to else None,
                    "channel": list(v.req.channels), "campaign": v.campaign_id, "campaign_name": meta.get("campaign_name"),
                    "currency": v.currency, "focus": v.req.focus, "active": v.req.active()},
        "money": money_info,
        "sort": {"key": v.req.sort, "dir": v.req.dir}, "sorts": [{"value": k, "label": s[0], "dir": s[2]} for k, s in SORTS.items()],
        "options": public_options(options), "kpis": kpis, "panels": panels,
        "definitions": build_definitions(tz, meta.get("schema") or cfg.MARKETING_SCHEMA),
        "export": {"channels": "/api/channels.csv?" + view_query(v, "channels"), "daily": "/api/channels.csv?" + view_query(v, "daily"),
                   "campaigns": ("/api/channels.csv?" + view_query(v, "campaigns")) if v.req.focus else None,
                   "xlsx": "/api/channels/report.xlsx?" + view_query(v), "insights": "/api/channels/insights?" + view_query(v),
                   "placements": "/api/channels/placements?" + view_query(v)},
        "read": {"tables": [CHANNEL_TABLE] + ([CAMPAIGN_TABLE] if (v.campaign_id is not None or v.focus_id is not None) else []),
                 "rollup_rows": meta.get("rollup_rows"), "raw_fact_read": False},
        "config": {"low_n": LOW_N, "default_days": DEFAULT_DAYS, "chart_max_days": CHART_MAX_DAYS, "chart_max_weeks": CHART_MAX_WEEKS, "chart_max_lines": CHART_MAX_LINES,
                   "campaign_rows_max": CAMPAIGN_ROWS_MAX, "channel_rows_max": CHANNEL_ROWS_MAX, "export_max_rows": EXPORT_MAX_ROWS,
                   "palette_slots": PALETTE_SLOTS, "refresh": REFRESH_TEXT},
    }


def assemble_setup(state: str, meta: dict, help_: dict, options: dict | None, now: datetime | None = None) -> dict:
    """The not_installed / empty payloads: a headline, the exact fix, the definitions - and no number anywhere."""
    now = now or datetime.now(timezone.utc)
    tz, schema = meta.get("tz"), meta.get("schema")
    fresh = build_freshness(meta.get("data_as_of"), meta.get("journal"), now)
    return {
        "ok": True, "state": state, "generated_at": _iso(now), "time_zone": tz, "schema": schema,
        "headline": build_headline(state, None, None, None, None, tz), "install": help_, "warnings": [], "freshness": fresh,
        "range": None, "source": None, "source_table": None,
        "filters": {"from": None, "to": None, "channel": [], "campaign": None, "campaign_name": None, "currency": None, "focus": None, "active": 0},
        "money": {"mode": "unknown", "chosen": None, "currencies": []},
        "sort": {"key": DEFAULT_SORT, "dir": DEFAULT_DIR}, "sorts": [{"value": k, "label": s[0], "dir": s[2]} for k, s in SORTS.items()],
        "options": public_options(options) if options else {"channels": [], "currencies": [], "first_day": None, "last_day": None, "rollup_rows": 0},
        "kpis": [], "panels": {}, "definitions": build_definitions(tz, schema or cfg.MARKETING_SCHEMA), "export": None,
        "read": {"tables": [], "rollup_rows": meta.get("rollup_rows"), "raw_fact_read": False},
        "config": {"low_n": LOW_N, "default_days": DEFAULT_DAYS, "chart_max_days": CHART_MAX_DAYS, "chart_max_weeks": CHART_MAX_WEEKS, "chart_max_lines": CHART_MAX_LINES,
                   "campaign_rows_max": CAMPAIGN_ROWS_MAX, "channel_rows_max": CHANNEL_ROWS_MAX, "export_max_rows": EXPORT_MAX_ROWS,
                   "palette_slots": PALETTE_SLOTS, "refresh": REFRESH_TEXT},
    }


def collect(rd, schema: str, v: View, options: dict, part: str | None = None) -> dict:
    """Every panel's query, each isolated (a failure gives {"error": ...} for that block only)."""
    lo, _clipped = chart_window(v)
    sql, p = totals_sql(schema, v)
    rd.block("totals", lambda: rd.rows(sql, p))
    sql, p = totals_sql(schema, v, prev=True)
    rd.block("prev", lambda: rd.rows(sql, p))
    sql, p = channels_sql(schema, v)
    rd.block("channels", lambda: rd.rows(sql, p))
    sql, p = daily_sql(schema, v, lo, DAILY_ROWS_MAX + 1, week_lower=v.d_from)
    rd.block("daily", lambda: rd.rows(sql, p))
    for name, kw in (("money_totals", {"group": "total"}), ("money_prev", {"group": "total", "prev": True}),
                     ("money_channels", {"group": "channel"}), ("money_daily", {"group": "daily", "lower": lo, "week_lower": v.d_from})):
        sql, p = money_sql(schema, v, **kw)
        rd.block(name, lambda sql=sql, p=p: rd.rows(sql, p))
    if v.focus_id is not None and focus_in_filter(v):
        def campaigns():
            q, params = campaigns_sql(schema, v, CAMPAIGN_ROWS_MAX)
            rows = rd.rows(q, params)
            ids = [int(r["campaign_id"]) for r in rows if int(r["campaign_id"]) != 0]
            names = rd.rows(f"SELECT campaign_id, name, campaign_key FROM {_t(schema, CAMPAIGN_DIM)} WHERE campaign_id = ANY(CAST(:ids AS bigint[]))",
                            {"ids": ids}) if ids else []
            return {"rows": rows, "names": names}
        rd.block("campaigns", campaigns)
        if not td_failed(rd.raw.get("campaigns")):
            shown = [int(r["campaign_id"]) for r in rd.raw["campaigns"]["rows"]]
            sql, p = money_sql(schema, v, "campaign", campaign_ids=shown)
            rd.block("money_campaigns", lambda: rd.rows(sql, p))
    return rd.raw


def build_view(rd, schema: str, params: Mapping[str, list[str]], known: Collection[str]):
    """State + validation + view, on one Reader. Returns (kind, ...):
       ("down",  {..})                        the database could not answer
       ("setup", state, meta, help)           not_installed or empty
       ("bad",   problems, options)           a value that is not in the data
       ("ok",    view, options, meta, req)"""
    try:
        meta = read_state(rd, schema)
    except Exception as exc:  # noqa: BLE001
        return ("down", f"{type(exc).__name__}: {_redact(str(exc))}")
    if meta["missing"]:
        return ("setup", STATE_NOT_INSTALLED, meta, install_help(meta["missing"], schema, meta.get("currency_ok")))
    if not meta["rollup_rows"]:
        return ("setup", STATE_EMPTY, meta, empty_help(schema, meta.get("currency_ok")))
    options = build_options(meta["channel_rows"], meta)
    ids = {c["key"]: c["id"] for c in options["_channels"]}
    exists = campaign_lookup(rd, schema)
    try:
        req, problems = parse_request(params, known, channels_ok=set(ids), campaign_exists=exists,
                                      currencies_ok={c["code"] for c in options["currencies"]})
    except Exception as exc:  # noqa: BLE001 - the campaign probe failed: say the database is the problem, not the value
        return ("down", f"{type(exc).__name__}: {_redact(str(exc))}")
    if problems:
        return ("bad", problems, public_options(options))
    return ("ok", resolve_view(req, options, ids), options, meta, req)


# ============================================================================ store
class ChannelsStore:
    """Builds the /api/channels payloads on demand. A few seconds of cache (one entry per distinct request) and a bounded
    number of concurrent builds keep a burst from re-reading the database; no thread, nothing to stop. Never raises."""

    def __init__(self, flow=None, ttl: float = CACHE_SECONDS) -> None:
        self.flow = flow
        self.ttl = ttl
        self._cache: OrderedDict[tuple, tuple[float, tuple[int, dict]]] = OrderedDict()
        self._cache_lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(MAX_CONCURRENT_BUILDS)

    @staticmethod
    def _norm(params: Mapping[str, list[str]]) -> tuple:
        return tuple(sorted((k, tuple(v)) for k, v in params.items() if k in KNOWN_PARAMS))

    @staticmethod
    def _bad_request(problems: list[dict], options: dict | None = None) -> tuple[int, dict]:
        first = problems[0]
        return 400, {"ok": False, "error": f"Invalid {first['param']}: {first['why']}", "problems": problems, "options": options}

    @staticmethod
    def _busy() -> tuple[int, dict]:
        return 503, {"ok": False, "error": "The data service is busy reading the database. Try again in a moment."}

    @staticmethod
    def _down(err: str) -> tuple[int, dict]:
        ld._log_once("channels_database", err)
        return 503, {"ok": False, "error": "Can’t read the channel data right now - " + err, "unavailable": True}

    @staticmethod
    def _connect():
        # one read-only REPEATABLE READ snapshot: tiles, chart, table and drill-down describe the same rows even when a
        # refresh commits in between (each of its ranges is one transaction, so a snapshot sees old or new, never half)
        return read_connect(engine).execution_options(postgresql_readonly=True, isolation_level="REPEATABLE READ")

    def get(self, params: Mapping[str, list[str]], fresh: bool = False) -> tuple[int, dict]:
        bad = unknown_params(params)                  # before the cache: _norm() drops unknown names, so a hit would hide the typo (L-098)
        _req, shape = parse_request(params)          # ... and so does a malformed value: reject the SHAPE before any cache lookup too
        if bad or shape:
            return self._bad_request(bad or shape)
        key = self._norm(params)
        now = time.monotonic()
        if not fresh:
            with self._cache_lock:
                hit = self._cache.get(key)
                if hit and now - hit[0] < self.ttl:
                    return hit[1]
        if not self._slots.acquire(timeout=BUILD_WAIT_SECONDS):
            logger.warning("Channels feed: all %d build slots are busy for more than %.0f s", MAX_CONCURRENT_BUILDS, BUILD_WAIT_SECONDS)
            with self._cache_lock:
                hit = self._cache.get(key)
            return hit[1] if hit else self._busy()
        try:
            try:
                result = self._build(params)
            except Exception as exc:  # noqa: BLE001 - last line of defence: never a 500 for the page
                logger.exception("Channels feed failed")
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
            schema = mschema.check_identifier(cfg.MARKETING_SCHEMA)
        except mschema.SchemaError as exc:
            return 503, {"ok": False, "error": f"MARKETING_SCHEMA is not usable: {exc}", "unavailable": True}
        try:
            with self._connect() as conn:
                rd = ld._Reader(conn, QUERY_BUDGET_SECONDS)
                built = build_view(rd, schema, params, KNOWN_PARAMS)
                if built[0] == "down":
                    conn.rollback()
                    return self._down(built[1])
                if built[0] == "bad":
                    conn.rollback()
                    return self._bad_request(built[1], built[2])
                if built[0] == "setup":
                    conn.rollback()
                    _k, state, meta, help_ = built
                    ld._logged.pop("channels_database", None)
                    return 200, assemble_setup(state, meta, help_, build_options(meta.get("channel_rows") or [], meta) if meta.get("rollup_rows") else None)
                _k, v, options, meta, _req = built
                raw = collect(rd, schema, v, options)
                meta["campaign_name"] = campaign_name(rd, schema, v.campaign_id) if not td_failed(raw.get("totals")) else None
                conn.rollback()
        except Exception as exc:  # noqa: BLE001 - DB down: the connect itself failed
            return self._down(f"{type(exc).__name__}: {_redact(str(exc))}")
        ld._logged.pop("channels_database", None)
        return 200, assemble(raw, v, options, meta)

    # -- the export ----------------------------------------------------------
    def export_csv(self, params: Mapping[str, list[str]]) -> tuple[int, bytes | dict, dict]:
        """(status, csv bytes | error dict, extra headers). Same filters, same whitelists, same SQL as the page."""
        bad = unknown_params(params, CSV_PARAMS)
        _req, shape = parse_request(params, CSV_PARAMS)
        if bad or shape:
            code, body = self._bad_request(bad or shape)
            return code, body, {}
        if not self._slots.acquire(timeout=BUILD_WAIT_SECONDS):
            code, body = self._busy()
            return code, body, {}
        try:
            schema = mschema.check_identifier(cfg.MARKETING_SCHEMA)
            with self._connect() as conn:
                rd = ld._Reader(conn, EXPORT_BUDGET_SECONDS, statement_ms=int(EXPORT_BUDGET_SECONDS * 1000))
                built = build_view(rd, schema, params, CSV_PARAMS)
                if built[0] == "down":
                    conn.rollback()
                    return 503, {"ok": False, "error": "Can’t read the channel data right now - " + built[1]}, {}
                if built[0] == "bad":
                    conn.rollback()
                    code, body = self._bad_request(built[1])
                    return code, body, {}
                if built[0] == "setup":
                    conn.rollback()
                    what = "installed" if built[1] == STATE_NOT_INSTALLED else "loaded"
                    return 409, {"ok": False, "error": f"There is nothing to export: the channel data is not {what} yet.", "state": built[1]}, {}
                _k, v, options, meta, req = built
                tz = meta.get("tz")
                if req.part == "channels":
                    sql, p = channels_sql(schema, v)
                    rows = rd.rows(sql, p)
                    msql, mp = money_sql(schema, v, "channel")
                    body_rows = channel_csv_rows(build_table(rows, options, v, cap=None, money=rd.rows(msql, mp)), v, tz)
                    header, truncated = CHANNEL_CSV, False
                elif req.part == "daily":
                    sql, p = daily_sql(schema, v, v.d_from, EXPORT_MAX_ROWS + 1, by_currency=True)
                    rows = rd.rows(sql, p)
                    truncated = len(rows) > EXPORT_MAX_ROWS
                    body_rows = daily_csv_rows(rows[:EXPORT_MAX_ROWS], options, v, tz)
                    header = DAILY_CSV
                else:
                    sql, p = channels_sql(schema, v)
                    chan_sessions = sum(int(r.get("sessions") or 0) for r in rd.rows(sql, p) if r["channel_id"] == v.focus_id)
                    q, params_ = campaigns_sql(schema, v, EXPORT_MAX_ROWS + 1)
                    rows = rd.rows(q, params_)
                    ids = [int(r["campaign_id"]) for r in rows if int(r["campaign_id"]) != 0]
                    names = rd.rows(f"SELECT campaign_id, name, campaign_key FROM {_t(schema, CAMPAIGN_DIM)} WHERE campaign_id = ANY(CAST(:ids AS bigint[]))",
                                    {"ids": ids}) if ids else []
                    truncated = len(rows) > EXPORT_MAX_ROWS
                    msql, mp = money_sql(schema, v, "campaign", campaign_ids=[int(r["campaign_id"]) for r in rows[:EXPORT_MAX_ROWS]])
                    panel = build_campaigns({"rows": rows[:EXPORT_MAX_ROWS], "names": names}, options, v, chan_sessions, money=rd.rows(msql, mp))
                    body_rows = campaign_csv_rows(panel, v, tz)
                    header = CAMPAIGN_CSV
                conn.rollback()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Channels export failed: %s: %s", type(exc).__name__, _redact(str(exc)))
            return 503, {"ok": False, "error": f"The export could not be read ({type(exc).__name__}). Try again in a moment."}, {}
        finally:
            self._slots.release()
        name = f"channels_{CSV_FILE_LABEL[req.part]}_" + datetime.now().strftime("%Y%m%d_%H%M") + ("_filtered" if req.active() else "") + ".csv"
        return 200, to_csv(header, body_rows), {"Content-Disposition": f'attachment; filename="{name}"', "X-Row-Count": str(len(body_rows)),
                                                 "X-Export-Truncated": "true" if truncated else "false"}
