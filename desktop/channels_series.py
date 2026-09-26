"""Channels page, time series: daily and weekly series, grain handling, week axis, coverage text and the filter options (split out of desktop/channels_data.py, unchanged).
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Mapping

from desktop.channels_request import View
from desktop.channels_money import MEASURES


PALETTE_SLOTS = 7                # colour slots; the page maps a slot to a colour of the design language


# ============================================================================ delta, sparkline, tiles (pure)
def _dir(cur: float, prev: float, eps: float = 1e-9) -> str:
    return "flat" if abs(cur - prev) <= eps else ("up" if cur > prev else "down")


def daily_series(rows: list[dict], d_from: date, d_to: date) -> tuple[list[str], dict[str, list]]:
    """[{event_date, channel_id, <sums>}] -> (every day of the window as 'YYYY-MM-DD', {measure: [sum per day | None]}).
    A day with no rollup row is None - a gap, never a zero."""
    per: dict[date, dict] = {}
    for r in rows:
        d = r["event_date"]
        cur = per.setdefault(d, {m: 0 for m in MEASURES})
        for m in MEASURES:
            cur[m] += int(r.get(m) or 0)
    days, series = [], {m: [] for m in MEASURES}
    d = d_from
    while d <= d_to:
        days.append(d.isoformat())
        row = per.get(d)
        for m in MEASURES:
            series[m].append(None if row is None else row[m])
        d += timedelta(days=1)
    return days, series


def _spark(days: list[str], values: list, grain: str = "day") -> dict | None:
    """A sparkline needs two real points; otherwise the tile shows none rather than a line drawn from one. `grain` says what a
    point is: a day, or a week (a weekly source's row, dated by its start day)."""
    return {"days": days, "values": values, "grain": grain} if sum(1 for v in values if v is not None) >= 2 else None


def grain_of(row: Mapping) -> str:
    return "week" if row.get("grain") == "week" else "day"


def split_grain(rows) -> tuple[list, list]:
    """Rows -> (daily-grain rows, weekly-grain rows). A row with no grain is a day (the rollup default)."""
    day, week = [], []
    for r in rows or []:
        (week if grain_of(r) == "week" else day).append(r)
    return day, week


def periods(totals) -> tuple[int, int]:
    """(distinct days that carry a daily-grain row, distinct week START days that carry a weekly-grain row) of a totals row.
    A totals row without the split is all daily."""
    t = totals or {}
    d, w = t.get("day_periods"), t.get("week_periods")
    if d is None and w is None:
        return int(t.get("days_with_data") or 0), 0
    return int(d or 0), int(w or 0)


def grain_mode(totals) -> str:
    """none | day | week | mixed: which grains the rows in view have."""
    d, w = periods(totals)
    return "none" if not d and not w else ("week" if not d else ("day" if not w else "mixed"))


def week_axis(dates) -> list[date]:
    """The week-start dates to draw. Starts that all fall on one weekday are laid out every 7 days, so a missing week is a GAP
    (None) and not a missing bar; starts that do not line up are drawn where they are."""
    ds = sorted(set(dates))
    if not ds:
        return []
    if all((d - ds[0]).days % 7 == 0 for d in ds):
        out, d = [], ds[0]
        while d <= ds[-1]:
            out.append(d)
            d += timedelta(days=7)
        return out
    return ds


def weekly_series(rows: list[dict], axis: list[date]) -> dict[str, list]:
    """Weekly-grain rows -> {measure: [sum per week start | None]} along `axis`."""
    per: dict[date, dict] = {}
    for r in rows:
        cur = per.setdefault(r["event_date"], {m: 0 for m in MEASURES})
        for m in MEASURES:
            cur[m] += int(r.get(m) or 0)
    return {m: [None if d not in per else per[d][m] for d in axis] for m in MEASURES}


# ============================================================================ channel table, chart, campaigns (pure)
def channel_lookup(options: Mapping) -> dict[int, dict]:
    return {c["id"]: c for c in options.get("_channels", [])}


def coverage_text(totals, v: View) -> str:
    """'27 of 30 days in the range have data' - or, for a weekly source, the WEEKS it has (a week is counted on its start day, so
    counting 'days with data' would say 3 of 30 and read as a data outage)."""
    day_p, week_p = periods(totals)
    if not week_p:
        days_with = int(totals.get("days_with_data") or 0)
        return f"{days_with} of {v.days} day{'s' if v.days != 1 else ''} in the range have data" if days_with < v.days else f"all {v.days} day{'s' if v.days != 1 else ''} have data"
    wk = f"{week_p} week{'s' if week_p != 1 else ''} of weekly data (each counted on its start day)"
    return wk if not day_p else f"{day_p} of {v.days} days have daily data and {wk}"


def build_options(rows: list[dict], meta: dict) -> dict:
    """The filter bar's choices and the whitelist behind them: the channels that really have rollup rows, largest first.
    `_channels` (with the ids) stays on the server; public_options() strips it."""
    chans = []
    for i, r in enumerate(rows):
        cid = int(r["channel_id"])
        key = r.get("channel_key") or f"id:{cid}"
        chans.append({"id": cid, "key": key, "name": r.get("display_name") or r.get("channel_key") or f"Channel #{cid}",
                      "medium": r.get("medium"), "is_paid": r.get("is_paid"), "days": int(r.get("days") or 0),
                      "sessions": int(r.get("sessions") or 0), "slot": i % PALETTE_SLOTS})
    currencies = [{"code": r["currency"], "days": int(r.get("days") or 0), "has_money": bool(r.get("has_money"))} for r in (meta.get("currency_rows") or [])]
    fd, ld_ = meta.get("first_day"), meta.get("last_day")
    return {"_channels": chans, "currencies": currencies, "first_day": fd.isoformat() if isinstance(fd, date) else fd, "last_day": ld_.isoformat() if isinstance(ld_, date) else ld_,
            "rollup_rows": int(meta.get("rollup_rows") or 0)}


def public_options(options: Mapping) -> dict:
    return {"channels": [{k: c[k] for k in ("key", "name", "medium", "is_paid", "days", "sessions", "slot")} for c in options.get("_channels", [])],
            "currencies": list(options.get("currencies", [])), "first_day": options.get("first_day"), "last_day": options.get("last_day"), "rollup_rows": options.get("rollup_rows", 0)}
