"""Channels page, request layer: the whitelisted query parameters, the Request / View value objects, and the parsing and validation of a request (split out of desktop/channels_data.py, unchanged).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Callable, Collection, Mapping

from desktop import leads_data as ld


MAX_SPAN_DAYS = 3660             # ten years: a longer range is a typo

MAX_VALUE_LEN = 150
MAX_CHANNEL_VALUES = 50

# ---- fixed vocabularies (whitelists) -------------------------------------------------------------------------------
KNOWN_PARAMS = ("from", "to", "channel", "campaign", "currency", "focus", "sort", "dir")
CSV_PARAMS = KNOWN_PARAMS + ("part",)
CSV_PARTS = ("channels", "daily", "campaigns")
CSV_FILE_LABEL = {"channels": "by_channel", "daily": "daily", "campaigns": "campaigns"}     # channels_by_channel_20260925_1023.csv
_INT64_MAX = 2 ** 63 - 1
_CURRENCY_RE = re.compile(r"[A-Z]{3}")
_DIGITS = re.compile(r"\d{1,18}")


def _rate_key(r: dict, k: str):
    return r[k]["value"]


# Sort key -> (column label, value a channel row is ordered by, first direction). The ONLY place a sort order comes from:
# the request names a key, never a column. `dir` decides the direction; a row whose value is unknown (no clicks, so no
# conversion rate) always lands at the end, whichever direction is asked for - it is "unknown", not "smallest".
SORTS = {
    "sessions": ("Sessions", lambda r: r["sessions"], "desc"),
    "channel": ("Channel", lambda r: r["name"].lower(), "asc"),
    "clicks": ("Clicks", lambda r: r["clicks"], "desc"),
    "impressions": ("Impressions", lambda r: r["impressions"], "desc"),
    "ctr": ("CTR", lambda r: _rate_key(r, "ctr"), "desc"),
    "conversions": ("Conversions", lambda r: r["conversions"], "desc"),
    "cvr": ("Conv. rate", lambda r: _rate_key(r, "cvr"), "desc"),
    "spend": ("Spend", lambda r: r["spend"]["value"], "desc"),
    "revenue": ("Revenue", lambda r: r["revenue"]["value"], "desc"),
    "cpa": ("Cost / conversion", lambda r: r["cpa"]["value"], "asc"),
}
# Money is only comparable within one currency, so a money sort GROUPS the rows by currency code first (a row whose figure is
# unknown, or spans several currencies, is unknown and goes last like any other unknown).
MONEY_SORT_CURRENCY = {"spend": lambda r: r["spend"]["currency"] or "", "revenue": lambda r: r["revenue"]["currency"] or "",
                       "cpa": lambda r: r["cpa"]["currency"] or ""}
DEFAULT_SORT, DEFAULT_DIR = "sessions", "desc"


# ============================================================================ request (pure)
@dataclass(frozen=True)
class Request:
    """What the query string asked for, checked for shape but not yet resolved against the data."""
    d_from: date | None = None
    d_to: date | None = None
    channels: tuple = ()
    campaign: int | None = None
    focus: str | None = None
    sort: str = DEFAULT_SORT
    dir: str = DEFAULT_DIR
    part: str = "channels"
    currency: str | None = None

    def active(self) -> int:
        return sum(1 for x in (self.d_from or self.d_to, self.channels, self.campaign is not None, self.currency) if x)


@dataclass(frozen=True)
class View:
    """A Request resolved against the data: the concrete range, the previous period, the ids behind the keys."""
    req: Request
    d_from: date
    d_to: date
    days: int
    defaulted: bool
    prev_from: date
    prev_to: date
    channel_ids: tuple = ()
    campaign_id: int | None = None
    focus_id: int | None = None
    currency: str | None = None

    @property
    def source_kind(self) -> str:
        return "campaign_rollup" if self.campaign_id is not None else "channel_rollup"


def _values(params: Mapping[str, list[str]], name: str) -> list[str]:
    return [v for v in (params.get(name) or []) if v is not None]


def unknown_params(params: Mapping[str, list[str]], known: Collection[str] = KNOWN_PARAMS) -> list[dict]:
    """Parameter NAMES this endpoint does not understand (lesson L-098): a typo such as ?chanel=x must be a 400 that
    names the parameter, never a 200 with every channel in it. `fresh` is the one name every feed accepts."""
    return [ld._problem(name, "", "not a parameter of this view; known: " + ", ".join(known))
            for name in sorted(params) if name not in known and name != "fresh"]


def parse_request(params: Mapping[str, list[str]], known: Collection[str] = KNOWN_PARAMS,
                  channels_ok: Collection[str] | None = None,
                  campaign_exists: Callable[[int], bool] | None = None,
                  currencies_ok: Collection[str] | None = None) -> tuple[Request, list[dict]]:
    """Request parameters -> (Request, problems). With `channels_ok` / `campaign_exists` / `currencies_ok` given, channel,
    campaign and currency values are also checked against the data (a whitelist of what exists right now); without them only the SHAPE is checked, which
    is what runs before the cache. A value that fails is reported and never reaches SQL, and it is never silently
    dropped: an export that quietly ignored a filter would hand the analyst the wrong rows."""
    problems: list[dict] = unknown_params(params, known)
    out: dict = {}
    P = ld._problem

    def one(name: str) -> str | None:
        vals = _values(params, name)
        if len(vals) > 1:
            problems.append(P(name, vals[0], "give this parameter only once"))
        return vals[-1] if vals else None

    for name, key in (("from", "d_from"), ("to", "d_to")):
        raw = one(name)
        if raw in (None, ""):
            continue
        try:
            if not ld._ISO_DAY.match(raw):
                raise ValueError
            d = date.fromisoformat(raw)
            if not ld.DATE_MIN <= d <= ld.DATE_MAX:
                raise ValueError
            out[key] = d
        except ValueError:
            problems.append(P(name, raw, "not a date between 2000-01-01 and 2100-12-31 (use YYYY-MM-DD)"))
    if out.get("d_from") and out.get("d_to"):
        if out["d_from"] > out["d_to"]:
            problems.append(P("from", out["d_from"].isoformat(), "the start date is after the end date"))
        elif (out["d_to"] - out["d_from"]).days + 1 > MAX_SPAN_DAYS:
            problems.append(P("from", out["d_from"].isoformat(), f"a range is at most {MAX_SPAN_DAYS} days"))

    chans: list[str] = []
    for v in _values(params, "channel"):
        if v == "" or v in chans:
            continue
        if len(v) > MAX_VALUE_LEN or (channels_ok is not None and v not in channels_ok):
            problems.append(P("channel", v, "not a known channel"))
        else:
            chans.append(v)
    if len(chans) > MAX_CHANNEL_VALUES:
        problems.append(P("channel", f"{len(chans)} values", f"at most {MAX_CHANNEL_VALUES} channels"))
        chans = chans[:MAX_CHANNEL_VALUES]
    out["channels"] = tuple(chans)

    raw = one("campaign")
    if raw not in (None, ""):
        if not _DIGITS.fullmatch(raw) or int(raw) > _INT64_MAX:
            problems.append(P("campaign", raw, "must be a campaign number (0 = the rows that had no campaign)"))
        elif campaign_exists is not None and not campaign_exists(int(raw)):
            problems.append(P("campaign", raw, "not a known campaign"))
        else:
            out["campaign"] = int(raw)

    raw = one("currency")
    if raw not in (None, ""):
        if not _CURRENCY_RE.fullmatch(raw):
            problems.append(P("currency", raw, "not a currency code (three capital letters, for example EUR)"))
        elif currencies_ok is not None and raw not in currencies_ok:
            problems.append(P("currency", raw, "not a currency in the data"))
        else:
            out["currency"] = raw

    raw = one("focus")
    if raw not in (None, ""):
        if len(raw) > MAX_VALUE_LEN or (channels_ok is not None and raw not in channels_ok):
            problems.append(P("focus", raw, "not a known channel"))
        else:
            out["focus"] = raw

    sort = one("sort") or DEFAULT_SORT
    if sort not in SORTS:
        problems.append(P("sort", sort, "not one of: " + ", ".join(SORTS)))
        sort = DEFAULT_SORT
    direction = (one("dir") or SORTS[sort][2]).lower()
    if direction not in ("asc", "desc"):
        problems.append(P("dir", direction, "must be asc or desc"))
        direction = SORTS[sort][2]
    out["sort"], out["dir"] = sort, direction

    if "part" in known:
        part = one("part") or "channels"
        if part not in CSV_PARTS:
            problems.append(P("part", part, "not one of: " + ", ".join(CSV_PARTS)))
            part = "channels"
        out["part"] = part
        if part == "campaigns" and not out.get("focus") and not any(p["param"] == "focus" for p in problems):
            problems.append(P("focus", "", "the campaigns file needs a channel: add focus=<channel key>"))
    return Request(**out), problems


def focus_in_filter(v: View) -> bool:
    """The drill-down channel must be one the channel filter still lets through (no channel filter = every channel)."""
    return not v.channel_ids or v.focus_id in v.channel_ids


def view_query(v: View, part: str | None = None) -> str:
    """The query string that reproduces exactly this view (the export link uses it, so the file matches the screen: L-101).
    The RESOLVED range is written out, so a default that moves when new data arrives cannot change what a link means."""
    from urllib.parse import urlencode
    pairs: list[tuple[str, str]] = [("from", v.d_from.isoformat()), ("to", v.d_to.isoformat())]
    pairs += [("channel", k) for k in v.req.channels]
    if v.campaign_id is not None:
        pairs.append(("campaign", str(v.campaign_id)))
    if v.currency:
        pairs.append(("currency", v.currency))
    if v.req.focus:
        pairs.append(("focus", v.req.focus))
    pairs += [("sort", v.req.sort), ("dir", v.req.dir)]
    if part:
        pairs.append(("part", part))
    return urlencode(pairs)
