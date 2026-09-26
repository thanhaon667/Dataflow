"""Channels page, CSV export: cell escaping, the three column sets and the row builders (split out of desktop/channels_data.py, unchanged).
"""
from __future__ import annotations

import csv
import io
import math
from datetime import date, datetime
from typing import Mapping

from desktop import leads_data as ld
from desktop.channels_request import View
from desktop.channels_money import MICROS, _cur
from desktop.channels_series import channel_lookup, grain_of
from desktop.channels_queries import CAMPAIGN_TABLE, CHANNEL_TABLE


# ============================================================================ CSV (pure)
def csv_cell(v) -> str:
    """One CSV cell. Text starting with = + - @ ; tab or CR gets a leading apostrophe (CSV injection); ints are numbers,
    floats keep up to 4 decimals (a rate) - never through the text rule, so a number is never prefixed."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return "" if math.isnan(v) else (f"{v:.4f}".rstrip("0").rstrip("."))
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    return ld.csv_cell(v)


def to_csv(header: list[str], rows: list[list]) -> bytes:
    """UTF-8 with a byte-order mark (Excel reads accents), CRLF line ends, every cell through csv_cell()."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(header)
    for r in rows:
        w.writerow([csv_cell(x) for x in r])
    return b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8")


def _pct(r: Mapping) -> float | None:
    return r["pct"]


MIXED_CURRENCY = "MULTIPLE"      # the `currency` cell of a row whose money spans several currencies: its amounts are blank, never a sum
CHANNEL_CSV = ["period_from", "period_to", "time_zone", "channel", "channel_key", "medium", "sessions", "share_of_sessions_pct", "clicks",
               "impressions", "ctr_pct", "conversions", "conversion_rate_pct", "currency", "spend", "revenue", "revenue_derived",
               "cost_per_conversion", "money_by_currency", "days_with_data", "low_volume", "source_table"]
DAILY_CSV = ["date", "grain", "period_days", "time_zone", "channel", "channel_key", "currency", "sessions", "clicks", "impressions", "conversions",
             "spend", "revenue", "revenue_derived_part", "source_table"]
CAMPAIGN_CSV = ["period_from", "period_to", "time_zone", "channel", "channel_key", "campaign_id", "campaign", "sessions", "share_of_channel_sessions_pct",
                "clicks", "impressions", "ctr_pct", "conversions", "conversion_rate_pct", "currency", "spend", "revenue", "revenue_derived",
                "cost_per_conversion", "money_by_currency", "days_with_data", "low_volume"]
CSV_EXCLUDED = "identity, e-mail, phone, lead or user ids and raw payloads: the rollup tables do not carry them and the file is aggregate rows only"


def csv_money(r: Mapping) -> list:
    """[currency, spend, revenue, revenue_derived, cost_per_conversion, money_by_currency] for one channel / campaign row.
    One currency: its code and its amounts. Several: currency MULTIPLE, the amounts left BLANK (a file must not offer a cross-currency
    sum either) and money_by_currency spelling every currency out. No money: all blank."""
    cells = (r["spend"], r["revenue"], r["cpa"])
    if any(c["state"] == "unavailable" for c in cells):
        return ["", None, None, "", None, ""]
    per: dict[str, dict] = {}
    for key in ("spend", "revenue"):
        for p in r[key]["parts"]:
            per.setdefault(p["currency"], {})[key] = p["value"]
    if len(per) > 1 or any(c["state"] == "multi" for c in cells):
        by = "; ".join(f"{c}: spend {x.get('spend', 'none')} revenue {x.get('revenue', 'none')}" for c, x in sorted(per.items()))
        return [MIXED_CURRENCY, None, None, "", None, by]
    if not per:
        return ["", None, None, "", None, ""]
    (code,) = per
    return [code, r["spend"]["value"], r["revenue"]["value"], r["revenue"].get("derived") or "", r["cpa"]["value"], ""]


def channel_csv_rows(table: dict, v: View, tz: str | None) -> list[list]:
    src = CHANNEL_TABLE if v.campaign_id is None else CAMPAIGN_TABLE
    return [[v.d_from, v.d_to, tz or "", r["name"], r["key"], r["medium"] or "", r["sessions"], _pct(r["share"]), r["clicks"], r["impressions"],
             _pct(r["ctr"]), r["conversions"], _pct(r["cvr"]), *csv_money(r), r["days_with_data"], r["low_n"], src]
            for r in table["rows"]]


def daily_csv_rows(rows: list[dict], options: Mapping, v: View, tz: str | None) -> list[list]:
    """One row per day x channel x grain x currency: every fact of a day belongs to exactly one currency, so the counts split cleanly
    and nothing is counted twice. A weekly row is dated by its start day and says grain 'week', period_days 7."""
    dims = channel_lookup(options)
    src = CHANNEL_TABLE if v.campaign_id is None else CAMPAIGN_TABLE
    out = []
    for r in rows:
        info = dims.get(r["channel_id"]) or {"key": f"id:{r['channel_id']}", "name": f"Channel #{r['channel_id']}"}
        sp, rv, dr = int(r.get("spend_micros") or 0), int(r.get("revenue_micros") or 0), int(r.get("revenue_derived_micros") or 0)
        week = grain_of(r) == "week"
        out.append([r["event_date"], "week" if week else "day", 7 if week else 1, tz or "", info["name"], info["key"], _cur(r) if (sp > 0 or rv > 0) else "",
                    int(r["sessions"]), int(r["clicks"]), int(r["impressions"]), int(r["conversions"]),
                    round(sp / MICROS, 2) if sp > 0 else None, round(rv / MICROS, 2) if rv > 0 else None, round(dr / MICROS, 2) if dr > 0 else None, src])
    return out


def campaign_csv_rows(panel: dict, v: View, tz: str | None) -> list[list]:
    f = panel.get("focus") or {}
    return [[v.d_from, v.d_to, tz or "", f.get("name", ""), f.get("key", ""), r["campaign_id"], r["name"], r["sessions"], _pct(r["share"]),
             r["clicks"], r["impressions"], _pct(r["ctr"]), r["conversions"], _pct(r["cvr"]), *csv_money(r),
             r["days_with_data"], r["low_n"]] for r in panel.get("rows", [])]
