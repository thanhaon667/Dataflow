"""
Channels page, PLACEMENTS feed (display-network placements: WHERE the ads ran - a site, a marketplace app or a banner slot - what each place
cost and produced):
  GET /api/channels/placements       the Placements panel for one set of filters (JSON): a sortable table + totals per currency
  GET /api/channels/placements.csv   the same table as a CSV file (L-101: the link comes from the payload, so the file matches the screen)
and the helpers the Insights feed and the Excel report use to add the placement rules and the Placements sheet.

It reads ONLY the placement rollup (desktop/placements_queries.py) inside one read-only REPEATABLE READ snapshot - never the raw fact, never the
landing table - and it is OPTIONAL: without db/sql/11_marketing_placements.sql the feed answers the state `placements_not_installed` with the exact
command, and nothing else on the Channels page changes.

States (each rendered without an error, never a number that is not there):
    not_installed             the channel tables (07 + 09) are missing: same answer as the Channels page
    placements_not_installed  the placement rollup (11) is missing: the exact psql command
    empty                     installed, but no placement row has ever been loaded: the two commands to load and to roll up
    no_match                  placement rows exist, none in the filters / range
    outside_filter            the drill-down channel is one the channel filter excludes (L-209): said, not shown as 'no data'
    unavailable               the read failed (the rest of the page keeps working)
    ready                     numbers

Honesty (same rules as the Channels page): money is shown per currency and is never converted, added or ranked across currencies (a row belongs to
ONE currency; the totals are one line per currency); a percentage needs LOW_N or more in its denominator (below it the page shows counts);
an unknown ad size / position / viewability is BLANK, never 0; viewability is viewable / MEASURED impressions and its coverage is shown; a sort by a
rate puts rows the display suppresses with the unknown ones (L-210); the cardinality guard's "(other placements)" row is a bucket, not a place, and says how many
placements it folds.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Mapping

from desktop import channels_data as cd
from desktop import insights_rules as ir
from desktop import leads_data as ld
from desktop import placements_queries as pq
from desktop.flow_data import _iso, _redact
from erp import config as cfg
from erp.marketing import rollup_placements as rp
from erp.marketing import schema as mschema

logger = logging.getLogger("erp_desk.placements")

PARAMS_EXTRA = ("psort", "pdir")
KNOWN_PARAMS = cd.KNOWN_PARAMS + PARAMS_EXTRA
# the placements table's own sort keys: name -> (label, first direction). `sort` / `dir` (the channel table's order) are accepted, because the page's
# address carries them, and have no effect here.
PSORTS = {"spend": ("Spend", "desc"), "clicks": ("Clicks", "desc"), "impressions": ("Impressions", "desc"), "ctr": ("CTR", "desc"),
          "conversions": ("Conversions", "desc"), "cpa": ("Cost / conversion", "asc"), "viewability": ("Viewability", "desc"),
          "placement": ("Placement", "asc")}
DEFAULT_PSORT = "spend"
ROWS_SHOWN_MAX = 100             # rows of the on-screen table; the CSV / Excel carry every row up to EXPORT_MAX_ROWS
EXPORT_MAX_ROWS = 50_000
XLSX_ROWS_MAX = 20_000
CACHE_SECONDS = cd.CACHE_SECONDS

TYPE_LABEL = {"website": "Website", "app": "App", "video": "Video", "other": "Other"}
POSITION_LABEL = {"above_fold": "Above the fold", "below_fold": "Below the fold"}


# ============================================================================ request (pure)
def parse_psort(params: Mapping[str, list[str]]) -> tuple[str, str, list[dict]]:
    """(key, direction, problems) of the placements table's own order: psort must be one of PSORTS, pdir asc or desc; anything else is a 400
    that names the parameter, never a silent default."""
    problems: list[dict] = []
    P = ld._problem
    out = []
    for name in PARAMS_EXTRA:
        vals = [x for x in (params.get(name) or []) if x is not None]
        if len(vals) > 1:
            problems.append(P(name, vals[0], "give this parameter only once"))
        out.append(vals[-1] if vals else None)
    key, direction = out
    if key in (None, ""):
        key = DEFAULT_PSORT
    elif key not in PSORTS:
        problems.append(P("psort", key, "not one of: " + ", ".join(PSORTS)))
        key = DEFAULT_PSORT
    if direction in (None, ""):
        direction = PSORTS[key][1]
    elif direction.lower() not in ("asc", "desc"):
        problems.append(P("pdir", direction, "must be asc or desc"))
        direction = PSORTS[key][1]
    return key, direction.lower(), problems


# ============================================================================ aggregation (pure)
def _n(x) -> int:
    return int(x or 0)


def _creative_names(block: Mapping) -> dict[int, tuple[str, str]]:
    return {int(c["creative_id"]): (str(c.get("creative_key") or f"id:{c['creative_id']}"), str(c.get("name") or c.get("creative_key") or f"Placement #{c['creative_id']}"))
            for c in block.get("creatives") or []}


def _placement_identity(pid: int, names: Mapping) -> tuple[str, str]:
    if pid == 0:
        return "", ir.OTHER_PLACEMENTS
    return names.get(pid) or (f"id:{pid}", f"Placement #{pid}")


def build_slots(block: Mapping) -> tuple[list[dict], bool]:
    """Rollup groups -> table rows: one row per placement x type x ad size x position x currency, summed over the campaigns and channels in view.
    Returns (rows, truncated?). A row is plain numbers; the cells (rates, money) are made by slot_cells()."""
    names = _creative_names(block)
    rows = block.get("rows") or []
    truncated = len(rows) > pq.ROWS_MAX
    slots: dict[tuple, dict] = {}
    for r in rows[:pq.ROWS_MAX]:
        pid = _n(r["placement_id"])
        key, name = _placement_identity(pid, names)
        cur = str(r["currency"]).strip()
        k = (key, r["placement_type"], r["ad_size"] or "", r["position"] or "unknown", cur)
        s = slots.setdefault(k, {"placement": name, "key": key, "type": r["placement_type"], "ad_size": r["ad_size"] or "", "position": r["position"] or "unknown",
                                 "currency": cur, "campaign_ids": set(), "impressions": 0, "clicks": 0, "conversions": 0, "spend_micros": 0,
                                 "revenue_micros": 0, "viewable": 0, "viewable_base": 0})
        s["campaign_ids"].add(_n(r["campaign_id"]))
        for m in ("impressions", "clicks", "conversions", "spend_micros", "revenue_micros", "viewable", "viewable_base"):
            s[m] += _n(r[m])
    return list(slots.values()), truncated


def slot_cells(s: Mapping) -> dict:
    """One slot -> the row the page draws. Rates come from sums at read time; below LOW_N they read as counts, with no denominator as unknown."""
    cur = s["currency"]
    impr, clicks, conv, spend = s["impressions"], s["clicks"], s["conversions"], s["spend_micros"]
    ctr = cd.ratio(clicks, impr, "no impressions")
    base = s["viewable_base"]
    if base <= 0:
        view = {"state": "none", "pct": None, "value": None, "n": 0, "of": 0, "low_n": False, "why": "not measured", "coverage_pct": None}
    else:
        view = cd.ratio(s["viewable"], base, "not measured")
        view["state"] = "ok"
        view["coverage_pct"] = round(min(100.0, base / impr * 100), 1) if impr > 0 else None
    money = cd.money(spend, cur)
    return {"placement": s["placement"], "key": s["key"], "is_other": s["key"] == "", "type": s["type"], "ad_size": s["ad_size"] or None,
            "position": None if s["position"] == "unknown" else s["position"], "currency": cur if (spend > 0 or s["revenue_micros"] > 0) else None,
            "campaigns": len(s["campaign_ids"]), "impressions": impr, "clicks": clicks, "ctr": ctr, "conversions": conv, "spend": money,
            "cpa": cd.cost_per_conversion(spend, conv, cur), "viewability": view,
            "viewable_impressions": s["viewable"] if base > 0 else None, "viewable_base": base if base > 0 else None}


def _sort_value(row: Mapping, key: str):
    if key == "placement":
        return row["placement"].lower()
    if key == "spend":
        return row["spend"]["value"]
    if key == "cpa":
        return row["cpa"]["value"]
    if key == "ctr":
        return row["ctr"]["value"] if row["ctr"]["pct"] is not None else None      # L-210: a rate the display suppresses sorts with the unknown ones
    if key == "viewability":
        v = row["viewability"]
        return v["value"] if v.get("pct") is not None else None
    return row[key]


def sort_rows(rows: list[dict], key: str, direction: str) -> list[dict]:
    """Order rows by a whitelisted key; unknown values always last, ties by name. Money keys order WITHIN each currency (currencies in code
    order): an amount in one currency is never ranked against another's."""
    rows = sorted(rows, key=lambda r: (r["placement"].lower(), r["ad_size"] or "", r["position"] or "", r["type"], r["currency"] or ""))
    present = [r for r in rows if _sort_value(r, key) is not None]
    missing = [r for r in rows if _sort_value(r, key) is None]
    present.sort(key=lambda r: _sort_value(r, key), reverse=(direction != "asc"))
    if key in ("spend", "cpa"):
        present.sort(key=lambda r: r["currency"] or "")
    return present + missing


def totals_by_currency(slots: list[dict]) -> list[dict]:
    """One line per currency (money never added across them); a currency with no money is 'counts only'. Counts of all rows go in `all`."""
    per: dict[str, dict] = {}
    for s in slots:
        g = per.setdefault(s["currency"], {"impressions": 0, "clicks": 0, "conversions": 0, "spend_micros": 0, "revenue_micros": 0, "viewable": 0, "viewable_base": 0,
                                           "keys": set()})
        for m in ("impressions", "clicks", "conversions", "spend_micros", "revenue_micros", "viewable", "viewable_base"):
            g[m] += s[m]
        if s["key"]:
            g["keys"].add(s["key"])
    out = []
    for cur in sorted(per):
        g = per[cur]
        cell = slot_cells({"placement": "", "key": "x", "type": "other", "ad_size": "", "position": "unknown", "currency": cur, "campaign_ids": set(), **{m: g[m] for m in
                           ("impressions", "clicks", "conversions", "spend_micros", "revenue_micros", "viewable", "viewable_base")}})
        out.append({"currency": cur if cell["currency"] else None, "code": cur, "placements": len(g["keys"]), "impressions": g["impressions"], "clicks": g["clicks"],
                    "ctr": cell["ctr"], "conversions": g["conversions"], "spend": cell["spend"], "cpa": cell["cpa"], "viewability": cell["viewability"]})
    return out


# ---- the rules' input ----
def groups_from_block(block: Mapping, options: Mapping) -> tuple[list[ir.PlacementGroup], bool]:
    """Rollup groups -> one PlacementGroup per (channel, campaign, currency), each holding its placements summed over type / size / position."""
    names = _creative_names(block)
    campaign_names = {_n(c["campaign_id"]): (c.get("name") or c.get("campaign_key") or f"Campaign #{c['campaign_id']}") for c in block.get("campaigns") or []}
    dims = cd.channel_lookup(options)
    rows = block.get("rows") or []
    truncated = len(rows) > pq.ROWS_MAX
    acc: dict[tuple, dict[str, dict]] = {}
    for r in rows[:pq.ROWS_MAX]:
        pid = _n(r["placement_id"])
        key, name = _placement_identity(pid, names)
        gk = (_n(r["channel_id"]), _n(r["campaign_id"]), str(r["currency"]).strip())
        p = acc.setdefault(gk, {}).setdefault(key, {"name": name, "key": key, "type": r["placement_type"], "spend": -1, "impressions": 0, "clicks": 0, "conversions": 0,
                                                     "spend_micros": 0, "viewable": 0, "viewable_base": 0})
        if _n(r["spend_micros"]) > p["spend"]:
            p["spend"], p["type"] = _n(r["spend_micros"]), r["placement_type"]          # the type of the placement's biggest slot
        for m in ("impressions", "clicks", "conversions", "spend_micros", "viewable", "viewable_base"):
            p[m] += _n(r[m])
    out = []
    for (cid_ch, camp, cur), ps in sorted(acc.items()):
        info = dims.get(cid_ch) or {"key": f"id:{cid_ch}", "name": f"Channel #{cid_ch}"}
        stats = tuple(ir.PlacementStat(name=p["name"], key=p["key"], ptype=p["type"], impressions=p["impressions"], clicks=p["clicks"], conversions=p["conversions"],
                                       spend_micros=p["spend_micros"], viewable=p["viewable"], viewable_base=p["viewable_base"]) for p in ps.values())
        out.append(ir.PlacementGroup(channel_key=info["key"], channel_name=info["name"], campaign_id=camp,
                                     campaign_name="(no campaign)" if camp == 0 else campaign_names.get(camp, f"Campaign #{camp}"), currency=cur, placements=stats))
    return out, truncated


def collect_if_installed(rd, schema: str, v: cd.View) -> None:
    """For the Insights feed and the Excel report: read the placement rows into rd.raw['placements'] when db/sql/11 is installed; leave the key
    absent when it is not (the placement rules then simply do not exist), or an {"error"} marker when the probe or the read failed."""
    try:
        ok = pq.installed(rd, schema)
    except Exception as exc:  # noqa: BLE001
        try:
            rd.conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        rd.raw["placements"] = {"error": f"{type(exc).__name__}: {_redact(str(exc))}"}
        return
    if ok:
        pq.collect(rd, schema, v)


# ============================================================================ payload (pure)
def view_query(v: cd.View, psort: str | None = None, pdir: str | None = None) -> str:
    q = cd.view_query(v)
    if psort:
        q += f"&psort={psort}&pdir={pdir or PSORTS[psort][1]}"
    return q


def _headline(state: str, totals: list, n_places: int, folded: int, v: cd.View | None) -> dict:
    if state == "not_installed":
        return {"text": "Placements need the channel rollup, which is not installed yet.", "sub": "Nothing is shown and nothing is invented.", "tone": "grey"}
    if state == "placements_not_installed":
        return {"text": "The placement rollup is not installed yet.", "sub": "It is one optional table; the rest of this page works without it.", "tone": "grey"}
    if state == "empty":
        return {"text": "No placement report has been loaded yet.", "sub": "Load a placement export with the placement_performance connector, then refresh the rollup.", "tone": "grey"}
    if state == "no_match":
        return {"text": "No placement rows match these filters.", "sub": "Loosen the range, channel, campaign or currency filter.", "tone": "amber"}
    if state == "outside_filter":
        return {"text": "The channel you drilled into is excluded by the channel filter.", "sub": "Clear the channel filter or pick another drill-down channel.", "tone": "amber"}
    if state == "unavailable":
        return {"text": "The placements could not be read right now.", "sub": "The rest of the page keeps working.", "tone": "red"}
    bits = []
    for t in totals:
        if t["spend"]["state"] == "ok":
            bits.append(f"{t['code']} {t['spend']['value']:,.0f}" if t["code"] in cd.ZERO_DECIMAL_CURRENCIES else f"{t['code']} {t['spend']['value']:,.2f}")
    spend = " and ".join(bits) if bits else "no spend field"
    clicks = sum(t["clicks"] for t in totals)
    conv = sum(t["conversions"] for t in totals)
    return {"text": f"{n_places:,} placement{'s' if n_places != 1 else ''} ran in this view: spend {spend}, {clicks:,} clicks, {conv:,} conversions.",
            "sub": (f"{folded:,} more placements are folded into '(other placements)' by the per-campaign limit; their numbers are still in the totals." if folded else
                    "Where the ads ran, what each place cost and produced. Suggestions to exclude or scale are in the Insights panel above; nothing is changed for you."),
            "tone": "blue"}


def build_definitions() -> list[dict]:
    """The text of the Placements drawer, built from the same constants the numbers and the rules use."""
    return [
        {"id": "placement", "term": "Placement", "text": "Where a display ad ran: a website (domain), a marketplace app (app id) or a banner slot. The table has one row per placement x ad size x position in the chosen range, summed over the campaigns and channels in view."},
        {"id": "ctr", "term": "CTR", "text": f"Clicks divided by impressions, from the summed columns. A percentage appears only with at least {cd.LOW_N} impressions behind it."},
        {"id": "cpa", "term": "Cost / conversion", "text": "Spend divided by conversions, in the row's own currency; blank when there is no spend or no conversion (unknown, not 0)."},
        {"id": "viewability", "term": "Viewability", "text": "Viewable impressions divided by the impressions that were MEASURED for viewability. A blank cell in the export means 'not measured' and is never counted as 0%. The 'measured' figure says what share of the placement's impressions the rate is based on."},
        {"id": "unknown", "term": "Blank means unknown", "text": "An ad size, position or viewability the export did not carry is left blank, never shown as 0 or as a guessed value."},
        {"id": "currency", "term": "Currencies", "text": "Every row belongs to one currency. Money is never converted, added or ranked across currencies: the totals are one line per currency and a money sort orders within each currency."},
        {"id": "other", "term": "(other placements)", "text": f"A campaign keeps its first {rp.MAX_PLACEMENTS_PER_CAMPAIGN} placements as their own rows (by first appearance); every further placement is folded into this bucket so the summary stays small. Sums stay exact; only the naming stops. It is a bucket, not a place, and is never judged by the rules."},
        {"id": "read", "term": "What is read", "text": f"Only the placement rollup table ({pq.PLACEMENT_TABLE}) and two small name lookups - never the raw interaction rows. Ad group and device are not kept in the rollup."},
        {"id": "suggest", "term": "Suggestions", "text": "Exclusion and budget suggestions come from fixed, explainable rules in the Insights panel (thresholds are printed in its 'How these are decided' drawer). Nothing on this page changes a campaign."},
    ]


def assemble_placements(raw: dict, v: cd.View, options: dict, meta: dict, psort: str, pdir: str, now: datetime | None = None) -> dict:
    """Raw query blocks -> the JSON of the Placements panel. Pure: no I/O."""
    now = now or datetime.now(timezone.utc)
    base = {"ok": True, "generated_at": _iso(now), "time_zone": meta.get("tz"), "schema": meta.get("schema"),
            "range": {"from": v.d_from.isoformat(), "to": v.d_to.isoformat(), "days": v.days, "defaulted": v.defaulted},
            "filters": {"channel": list(v.req.channels), "campaign": v.campaign_id, "currency": v.currency, "focus": v.req.focus},
            "read": {"tables": [pq.PLACEMENT_TABLE], "raw_fact_read": False},
            "config": {"low_n": cd.LOW_N, "rows_shown_max": ROWS_SHOWN_MAX, "export_max_rows": EXPORT_MAX_ROWS, "max_placements_per_campaign": rp.MAX_PLACEMENTS_PER_CAMPAIGN},
            "sorts": [{"value": k, "label": s[0], "dir": s[1]} for k, s in PSORTS.items()], "sort": {"key": psort, "dir": pdir},
            "definitions": build_definitions(), "export": {"csv": "/api/channels/placements.csv?" + view_query(v, psort, pdir), "xlsx": "/api/channels/report.xlsx?" + cd.view_query(v)},
            "table": {"rows": [], "total_rows": 0, "shown": 0, "more": 0, "truncated": False}, "totals": [], "folded": 0, "notes": []}
    block = raw.get("placements")
    if cd.td_failed(block):
        err = (block or {}).get("error", "could not be read") if isinstance(block, dict) else "could not be read"
        return {**base, "ok": False, "state": "unavailable", "error": err, "headline": _headline("unavailable", [], 0, 0, v), "export": None}
    slots, truncated = build_slots(block)
    if not slots:
        state = "no_match" if meta.get("placement_first_day") else "empty"
        return {**base, "state": state, "headline": _headline(state, [], 0, 0, v), "export": None if state == "empty" else base["export"]}
    cells = [slot_cells(s) for s in slots]
    ordered = sort_rows(cells, psort, pdir)
    shown = ordered[:ROWS_SHOWN_MAX]
    totals = totals_by_currency(slots)
    named = {s["key"] for s in slots if s["key"]}
    folded = int(block.get("folded") or 0)
    notes = []
    if truncated:
        notes.append(f"More than {pq.ROWS_MAX:,} placement groups are in view: the {pq.ROWS_MAX:,} with the most clicks were read. Narrow the range or the campaign.")
    if len(ordered) > ROWS_SHOWN_MAX:
        notes.append(f"The table shows the first {ROWS_SHOWN_MAX} of {len(ordered):,} rows in the chosen order; the CSV and the Excel report carry all of them.")
    if len(totals) > 1:
        notes.append(f"This view holds money in {len(totals)} currencies ({', '.join(t['code'] for t in totals)}): each has its own total and rows, and none are added together or converted.")
    return {**base, "state": "ready", "headline": _headline("ready", totals, len(named), folded, v),
            "table": {"rows": shown, "total_rows": len(ordered), "shown": len(shown), "more": max(0, len(ordered) - len(shown)), "truncated": truncated},
            "totals": totals, "folded": folded, "notes": notes}


def assemble_setup(state: str, meta: dict, now: datetime | None = None) -> dict:
    """The not_installed / placements_not_installed payloads: a headline, the exact fix and no number."""
    now = now or datetime.now(timezone.utc)
    help_ = None
    if state == "placements_not_installed":
        help_ = {"title": "Install the placement rollup (one optional table)",
                 "steps": [{"label": "Run once as the database owner, in PowerShell from the project folder", "command": rp.PSQL_11},
                           {"label": "Then load a placement export and refresh the rollup", "command": "venv\\Scripts\\python.exe -m erp.marketing.ingest --connector placement_performance --csv <file> --currency <ISO code>"}]}
    elif state == "empty":
        help_ = {"title": "Load a placement report",
                 "steps": [{"label": "Read a placement export (add --dry-run first to see how it maps)", "command": "venv\\Scripts\\python.exe -m erp.marketing.ingest --connector placement_performance --csv <file> --currency <ISO code>"},
                           {"label": "Refresh the rollup", "command": "venv\\Scripts\\python.exe -m erp.marketing.rollup"},
                           {"label": "Or generate a synthetic file to try it (loads nothing)", "command": "venv\\Scripts\\python.exe -m erp.marketing.sample_placements --rows 5000"}]}
    return {"ok": True, "state": state, "generated_at": _iso(now), "time_zone": meta.get("tz"), "schema": meta.get("schema"), "range": None, "filters": {},
            "read": {"tables": [], "raw_fact_read": False}, "config": {"low_n": cd.LOW_N}, "sorts": [{"value": k, "label": s[0], "dir": s[1]} for k, s in PSORTS.items()],
            "sort": {"key": DEFAULT_PSORT, "dir": PSORTS[DEFAULT_PSORT][1]}, "definitions": build_definitions(), "export": None, "install": help_,
            "table": {"rows": [], "total_rows": 0, "shown": 0, "more": 0, "truncated": False}, "totals": [], "folded": 0, "notes": [],
            "headline": _headline(state, [], 0, 0, None)}


# ============================================================================ CSV / Excel rows (pure)
PLACEMENT_CSV = ["period_from", "period_to", "time_zone", "placement", "placement_type", "ad_size", "position", "currency", "campaigns", "impressions", "clicks",
                 "ctr_pct", "conversions", "spend", "cost_per_conversion", "viewable_impressions", "viewability_measured_impressions", "viewability_pct",
                 "viewability_measured_share_pct", "other_placements_bucket", "source_table"]


def csv_rows(rows: list[dict], v: cd.View, tz: str | None) -> list[list]:
    out = []
    for r in rows:
        vw = r["viewability"]
        out.append([v.d_from, v.d_to, tz or "", r["placement"], r["type"], r["ad_size"] or "", r["position"] or "", r["currency"] or "", r["campaigns"], r["impressions"],
                    r["clicks"], r["ctr"]["pct"], r["conversions"], r["spend"]["value"], r["cpa"]["value"], r["viewable_impressions"], r["viewable_base"],
                    vw["pct"] if vw.get("state") == "ok" else None, vw.get("coverage_pct"), r["is_other"], pq.PLACEMENT_TABLE])
    return out


def sheet_kind(h: str) -> str:
    if h in ("period_from", "period_to"):
        return "date"
    if h in ("campaigns", "impressions", "clicks", "conversions", "viewable_impressions", "viewability_measured_impressions"):
        return "int"
    if h.endswith("_pct"):
        return "pct"
    if h in ("spend", "cost_per_conversion"):
        return "money"
    if h == "other_placements_bucket":
        return "bool"
    return "text"


def sheet_rows(rows: list[dict], v: cd.View, tz: str | None) -> list[list]:
    """The same rows as the CSV, with percentages as fractions (an Excel 0.0% format needs 0.123, not 12.3: L-236)."""
    pct_cols = [i for i, h in enumerate(PLACEMENT_CSV) if sheet_kind(h) == "pct"]
    return [[(x / 100 if (i in pct_cols and x is not None) else x) for i, x in enumerate(r)] for r in csv_rows(rows, v, tz)]


# ============================================================================ store
class PlacementsStore(cd.ChannelsStore):
    """The /api/channels/placements builders. Same cache, same build slots and the same whitelist as the Channels store (it IS one); get() is
    repeated here only because the placements feed knows two more parameter names (psort, pdir) and they must be in the cache key and the
    400-before-cache check (L-098, L-240)."""

    @staticmethod
    def _norm(params: Mapping[str, list[str]]) -> tuple:
        return tuple(sorted((k, tuple(v)) for k, v in params.items() if k in KNOWN_PARAMS))

    def get(self, params: Mapping[str, list[str]], fresh: bool = False) -> tuple[int, dict]:
        bad = cd.unknown_params(params, KNOWN_PARAMS)
        _req, shape = cd.parse_request(params, KNOWN_PARAMS)
        _k, _d, sort_problems = parse_psort(params)
        if bad or shape or sort_problems:
            return self._bad_request(bad or shape or sort_problems)
        key = self._norm(params)
        now = time.monotonic()
        if not fresh:
            with self._cache_lock:
                hit = self._cache.get(key)
                if hit and now - hit[0] < self.ttl:
                    return hit[1]
        if not self._slots.acquire(timeout=cd.BUILD_WAIT_SECONDS):
            with self._cache_lock:
                hit = self._cache.get(key)
            return hit[1] if hit else self._busy()
        try:
            try:
                result = self._build(params)
            except Exception as exc:  # noqa: BLE001 - never a 500 for the page
                logger.exception("Placements feed failed")
                result = 500, {"ok": False, "error": f"Unexpected error ({type(exc).__name__}). See erp_desktop.log."}
        finally:
            self._slots.release()
        if result[0] == 200:
            with self._cache_lock:
                self._cache[key] = (time.monotonic(), result)
                self._cache.move_to_end(key)
                while len(self._cache) > cd.CACHE_KEYS_MAX:
                    self._cache.popitem(last=False)
        return result

    def _read(self, rd, schema: str, params, known, sort_pair=None):
        """(kind, ...) like channels_data.build_view plus the placement probe: ("down", err) | ("bad", problems, options) |
        ("setup", state, meta) | ("ok", view, options, meta, raw)"""
        built = cd.build_view(rd, schema, params, known)
        if built[0] in ("down", "bad"):
            return built
        if built[0] == "setup":
            return ("setup", built[1], built[2])
        _k, v, options, meta, _req = built
        if not pq.installed(rd, schema):
            return ("setup", "placements_not_installed", meta)
        span = pq.has_rows(rd, schema)
        meta["placement_first_day"], meta["placement_last_day"] = span["first_day"], span["last_day"]
        if v.req.focus and not cd.focus_in_filter(v):
            return ("setup", "outside_filter", meta)
        pq.collect(rd, schema, v)
        return ("ok", v, options, meta, rd.raw)

    def _build(self, params: Mapping[str, list[str]]) -> tuple[int, dict]:
        try:
            schema = mschema.check_identifier(cfg.MARKETING_SCHEMA)
        except mschema.SchemaError as exc:
            return 503, {"ok": False, "error": f"MARKETING_SCHEMA is not usable: {exc}", "unavailable": True}
        psort, pdir, _p = parse_psort(params)
        try:
            with self._connect() as conn:
                rd = ld._Reader(conn, cd.QUERY_BUDGET_SECONDS)
                built = self._read(rd, schema, params, KNOWN_PARAMS)
                conn.rollback()
        except Exception as exc:  # noqa: BLE001
            return self._down(f"{type(exc).__name__}: {_redact(str(exc))}")
        if built[0] == "down":
            return self._down(built[1])
        if built[0] == "bad":
            return self._bad_request(built[1], built[2])
        ld._logged.pop("channels_database", None)
        if built[0] == "setup":
            return 200, assemble_setup(built[1], built[2])
        _k, v, options, meta, raw = built
        return 200, assemble_placements(raw, v, options, meta, psort, pdir)

    def export_csv(self, params: Mapping[str, list[str]]) -> tuple[int, bytes | dict, dict]:
        """(status, csv bytes | error dict, extra headers). Same filters, whitelists and rows as the panel; every row up to EXPORT_MAX_ROWS."""
        bad = cd.unknown_params(params, KNOWN_PARAMS)
        _req, shape = cd.parse_request(params, KNOWN_PARAMS)
        psort, pdir, sort_problems = parse_psort(params)
        if bad or shape or sort_problems:
            code, body = self._bad_request(bad or shape or sort_problems)
            return code, body, {}
        if not self._slots.acquire(timeout=cd.BUILD_WAIT_SECONDS):
            code, body = self._busy()
            return code, body, {}
        try:
            schema = mschema.check_identifier(cfg.MARKETING_SCHEMA)
            with self._connect() as conn:
                rd = ld._Reader(conn, cd.EXPORT_BUDGET_SECONDS, statement_ms=int(cd.EXPORT_BUDGET_SECONDS * 1000))
                built = self._read(rd, schema, params, KNOWN_PARAMS)
                conn.rollback()
            if built[0] == "down":
                return 503, {"ok": False, "error": "Can’t read the channel data right now - " + built[1]}, {}
            if built[0] == "bad":
                code, body = self._bad_request(built[1])
                return code, body, {}
            if built[0] == "setup":
                what = {"not_installed": "the channel data is not installed yet", "placements_not_installed": "the placement rollup is not installed yet",
                        "outside_filter": "the drill-down channel is excluded by the channel filter"}.get(built[1], "the channel data is not loaded yet")
                return 409, {"ok": False, "error": f"There is nothing to export: {what}.", "state": built[1]}, {}
            _k, v, options, meta, raw = built
            if cd.td_failed(raw.get("placements")):
                return 503, {"ok": False, "error": "The placements could not be read: " + str((raw.get("placements") or {}).get("error"))}, {}
            slots, truncated = build_slots(raw["placements"])
            ordered = sort_rows([slot_cells(s) for s in slots], psort, pdir)
            cut = len(ordered) > EXPORT_MAX_ROWS
            body_rows = csv_rows(ordered[:EXPORT_MAX_ROWS], v, meta.get("tz"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Placements export failed: %s: %s", type(exc).__name__, _redact(str(exc)))
            return 503, {"ok": False, "error": f"The export could not be read ({type(exc).__name__}). Try again in a moment."}, {}
        finally:
            self._slots.release()
        if not body_rows:
            return 409, {"ok": False, "error": "There is nothing to export: no placement row matches these filters.", "state": "no_match"}, {}
        name = "channels_placements_" + datetime.now().strftime("%Y%m%d_%H%M") + ("_filtered" if v.req.active() else "") + ".csv"
        return 200, cd.to_csv(PLACEMENT_CSV, body_rows), {"Content-Disposition": f'attachment; filename="{name}"', "X-Row-Count": str(len(body_rows)),
                                                           "X-Export-Truncated": "true" if (cut or truncated) else "false"}
