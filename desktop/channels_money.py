"""Channels page, measures and money: the additive measures, currency-aware money cells (never added across currencies), money deltas, sorting and small failure helpers (split out of desktop/channels_data.py, unchanged).
"""
from __future__ import annotations

from typing import Mapping

from erp.marketing.model import DEFAULT_CURRENCY
from desktop.channels_request import DEFAULT_SORT, MONEY_SORT_CURRENCY, SORTS


MICROS = 1_000_000

MEASURES = ("sessions", "clicks", "impressions", "conversions")          # counts: no currency, they add across everything
ZERO_DECIMAL_CURRENCIES = frozenset({"BIF", "CLP", "DJF", "GNF", "ISK", "JPY", "KMF", "KRW", "PYG", "RWF", "UGX", "UYI", "VND", "VUV", "XAF", "XOF", "XPF"})


# ============================================================================ math (pure)
def measures(row: Mapping | None) -> dict:
    """A query row -> the four additive COUNTS as ints (a missing value is 0 only INSIDE a sum that is already 0-based).
    Money is not here: it is added per currency only, see group_money()."""
    row = row or {}
    return {m: int(row.get(m) or 0) for m in MEASURES}


def money(micros: int, currency: str | None = None) -> dict:
    """ONE currency's spend. 'Present' only when above 0: the column cannot tell 0 from 'this source has no spend field'."""
    micros = int(micros or 0)
    if micros <= 0:
        return {"state": "none", "value": None, "why": "no spend field"}
    out = {"state": "ok", "value": round(micros / MICROS, 2), "why": None}
    if currency:
        out["currency"] = currency
    return out


def money_text(value: float, currency: str) -> str:
    """'VND 4,440,453' / 'EUR 1,249.13' - the code is always written, so a figure never reads as a bare number."""
    return f"{currency} {value:,.0f}" if currency in ZERO_DECIMAL_CURRENCIES else f"{currency} {value:,.2f}"


def _cur(row: Mapping) -> str:
    return (row.get("currency") or DEFAULT_CURRENCY)


def legacy_money_rows(rows) -> list[dict]:
    """Rows that carry their own spend_micros (and maybe a currency) and were not given a money block: read them as money rows,
    a missing currency being DEFAULT_CURRENCY. Only a caller that did not pass the money queries reaches this."""
    return [dict(r, currency=_cur(r)) for r in (rows or []) if isinstance(r, dict)]


def group_money(rows) -> dict[str, dict]:
    """Money rows -> {currency: summed spend / revenue / derived revenue / conversions}, currencies in code order.
    THE rule of this page: money is added only inside one currency. A currency with neither spend nor revenue carries no
    money and is left out, so a currency-less source (a 'USD' default row with no spend field) never makes a view 'mixed'."""
    out: dict[str, dict] = {}
    for r in rows or []:
        g = out.setdefault(_cur(r), {"spend_micros": 0, "revenue_micros": 0, "revenue_derived_micros": 0, "conversions": 0})
        for k in g:
            g[k] += int(r.get(k) or 0)
    return {c: g for c, g in sorted(out.items()) if g["spend_micros"] or g["revenue_micros"]}


def money_mode(groups: Mapping) -> str:
    """none | single | multi: how many currencies carry money in this group of rows."""
    n = sum(1 for g in groups.values() if g["spend_micros"] > 0 or g["revenue_micros"] > 0)
    return "none" if n == 0 else ("single" if n == 1 else "multi")


def derived_state(derived: int, total: int) -> str | None:
    """How much of a currency's revenue a connector DERIVED (purchases x value per purchase) instead of the source reporting it."""
    derived, total = int(derived or 0), int(total or 0)
    if derived <= 0 or total <= 0:
        return None
    return "all" if derived >= total else "partly"


MULTI_WHY = "more than one currency - shown per currency, never added"


def money_cell(groups: Mapping, key: str) -> dict:
    """The ONE place that turns money into something to show. state: 'ok' (one currency: value + currency), 'multi' (several:
    `parts` per currency and no value, so nothing can be sorted or summed across them) or 'none' (no such field)."""
    what = "spend" if key == "spend_micros" else "revenue"
    parts = []
    for c, g in groups.items():
        if g.get(key, 0) > 0:
            p = {"currency": c, "value": round(g[key] / MICROS, 2)}
            if key == "revenue_micros":
                p["derived"] = derived_state(g.get("revenue_derived_micros"), g[key])
            parts.append(p)
    if not parts:
        return {"state": "none", "value": None, "currency": None, "parts": [], "why": f"no {what} field"}
    if len(parts) == 1:
        p = parts[0]
        cell = {"state": "ok", "value": p["value"], "currency": p["currency"], "parts": parts, "why": None}
        if "derived" in p:
            cell["derived"] = p["derived"]
        return cell
    return {"state": "multi", "value": None, "currency": None, "parts": parts, "why": MULTI_WHY}


def unavailable_money() -> dict:
    cell = {"state": "unavailable", "value": None, "currency": None, "parts": [], "why": "could not be read", "n": 0, "low_n": False}
    return {"spend": dict(cell), "revenue": dict(cell), "cpa": dict(cell)}


def sort_rows(rows: list[dict], key: str, direction: str) -> list[dict]:
    """Order channel rows by a whitelisted key; unknown values always last, ties alphabetical. A money key (spend, revenue,
    cost per conversion) orders WITHIN each currency and lists the currencies in code order: an amount in VND is never
    ranked against an amount in EUR."""
    fn = SORTS.get(key, SORTS[DEFAULT_SORT])[1]
    rows = sorted(rows, key=lambda r: r["name"].lower())
    present = [r for r in rows if fn(r) is not None]
    missing = [r for r in rows if fn(r) is None]
    present.sort(key=fn, reverse=(direction != "asc"))
    by_currency = MONEY_SORT_CURRENCY.get(key)
    if by_currency:
        present.sort(key=by_currency)                 # stable: keeps the value order inside each currency
    return present + missing


def _pct_text(pct: float) -> str:
    return f"{pct:.1f}%".replace(".0%", "%") if abs(pct) < 100 else f"{pct:.0f}%"


def money_delta(cur_micros: int, prev: Mapping | None | dict, days: int, key: str = "spend_micros", what: str = "spend") -> dict:
    """Change of ONE currency's money against the previous period. `prev` carries days_with_data and `key` for that same currency."""
    label = f"the previous {days} day{'s' if days != 1 else ''}"
    if prev is None or (isinstance(prev, dict) and "error" in prev):
        return {"state": "unavailable", "dir": None, "text": "previous period could not be read", "pct": None, "good": None}
    if not int(prev.get("days_with_data") or 0):
        return {"state": "none", "dir": None, "text": "no data in the previous period", "pct": None, "good": None}
    p = int(prev[key] or 0)
    if p <= 0:
        return {"state": "none", "dir": None, "text": f"no {what} recorded in the previous period", "pct": None, "good": None}
    pct = (cur_micros - p) / p * 100
    d = "flat" if abs(pct) < 0.05 else ("up" if pct > 0 else "down")
    return {"state": "ok", "dir": d, "text": f"{_pct_text(abs(pct))} vs {label}" if d != "flat" else f"unchanged vs {label}", "pct": round(pct, 2), "good": None}


def spend_delta(cur_micros: int, prev: Mapping | None | dict, days: int) -> dict:
    return money_delta(cur_micros, prev, days, "spend_micros", "spend")


def money_series(rows, currency: str, key: str, labels: list[str]) -> list:
    """One currency's money per label (a 'YYYY-MM-DD'), in currency units; None where that currency has nothing (a gap, not a zero)."""
    per: dict[str, int] = {}
    for r in rows or []:
        if _cur(r) == currency:
            k = r["event_date"].isoformat()
            per[k] = per.get(k, 0) + int(r.get(key) or 0)
    return [None if per.get(k, 0) <= 0 else round(per[k] / MICROS, 2) for k in labels]


def td_failed(block) -> bool:
    """A block is unusable when it is missing (None) or is the {"error": ...} marker a failed query leaves."""
    return block is None or (isinstance(block, dict) and "error" in block)


def _short_err(msg) -> str | None:
    return None if not msg else str(msg)[:120]


# ============================================================================ assembly (pure)
def money_summary(rows, chosen: str | None) -> dict:
    """What the money in view looks like, for the page and its warnings: mode none | single | multi (| unavailable | unknown),
    the currency the viewer chose (if any) and each currency's spend and revenue in currency units. Never a total across them."""
    if rows is None:
        return {"mode": "unknown", "chosen": chosen, "currencies": []}
    if td_failed(rows):
        return {"mode": "unavailable", "chosen": chosen, "currencies": []}
    g = group_money(rows)
    return {"mode": money_mode(g), "chosen": chosen,
            "currencies": [{"code": c, "spend": round(x["spend_micros"] / MICROS, 2), "revenue": round(x["revenue_micros"] / MICROS, 2),
                            "revenue_derived": round(x["revenue_derived_micros"] / MICROS, 2)} for c, x in g.items()]}
