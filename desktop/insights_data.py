"""
Channels page, INSIGHTS feed + EXCEL report:
  GET /api/channels/insights      the rule findings for one set of filters (JSON), same filters and the same 400-before-cache
                                   rule as GET /api/channels
  GET /api/channels/report.xlsx   the same view as an Excel workbook (Summary, Channels, Campaigns, Insights, Definitions)

It reads ONLY the rollup tables (desktop/insights_queries.py) inside one read-only REPEATABLE READ snapshot, hands plain numbers to
the pure engine desktop/insights_rules.py, and wraps the answer. States, like the page: not_installed / empty (no number, no
finding), no_data_in_window (the analysed window holds no rows: it says which window and where the data is), ready. Money is
judged per currency and never mixed; a 0 that means 'unknown' is a note, not a finding (see insights_rules).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Mapping

from desktop import channels_data as cd
from desktop import insights_rules as ir
from desktop import insights_queries as iq
from desktop import leads_data as ld
from desktop import placements_data as pld
from desktop.flow_data import _iso, _redact
from erp import config as cfg
from erp.marketing import schema as mschema

logger = logging.getLogger("erp_desk.insights")

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
XLSX_MAX_BYTES = 12 * 1024 * 1024
XLSX_CAMPAIGN_ROWS_MAX = 20_000     # keep equal to insights_xlsx.ROWS_MAX (the writer enforces it again, before it creates cells)
XLSX_BUDGET_SECONDS = 20.0


# ============================================================================ rows -> engine inputs (pure)
def _period(row: Mapping, p: str) -> ir.Period:
    return ir.Period(**{m.replace("_micros", "_micros"): int(row.get(f"{p}_{m}") or 0) for m in iq.GROUP_MEASURES}, days=int(row.get(f"{p}_days") or 0))


def groups_from_rows(block: Mapping, options: Mapping) -> tuple[list[ir.Group], bool]:
    """{"rows": [...], "names": [...]} -> ([Group], truncated?). Groups with no rows in either window are dropped."""
    dims = cd.channel_lookup(options)
    names = {int(n["campaign_id"]): (n.get("name") or n.get("campaign_key") or f"Campaign #{n['campaign_id']}") for n in block["names"]}
    rows = block["rows"]
    truncated = len(rows) > iq.INSIGHT_GROUPS_MAX
    out = []
    for r in rows[:iq.INSIGHT_GROUPS_MAX]:
        cid = int(r["campaign_id"])
        info = dims.get(int(r["channel_id"])) or {"key": f"id:{r['channel_id']}", "name": f"Channel #{r['channel_id']}"}
        cur, prev = _period(r, "c"), _period(r, "p")
        if not cur.days and not prev.days:
            continue
        out.append(ir.Group(channel_key=info["key"], channel_name=info["name"], campaign_id=cid,
                            campaign_name="(no campaign)" if cid == 0 else names.get(cid, f"Campaign #{cid}"),
                            currency=str(r["currency"]).strip(), cur=cur, prev=prev))
    return out, truncated


def spans_from_rows(span_rows: list, day_rows: list, options: Mapping) -> list[ir.Span]:
    dims = cd.channel_lookup(options)
    days: dict[tuple, list] = {}
    for r in day_rows:
        days.setdefault((int(r["channel_id"]), r["grain"]), []).append(r["event_date"])
    out = []
    for r in span_rows:
        cid = int(r["channel_id"])
        info = dims.get(cid) or {"key": f"id:{cid}", "name": f"Channel #{cid}"}
        grain = "week" if r["grain"] == "week" else "day"
        out.append(ir.Span(channel_key=info["key"], channel_name=info["name"], grain=grain, first_day=r["first_day"], last_day=r["last_day"],
                           history_days=int(r["history_days"]), window_days=tuple(sorted(set(days.get((cid, r["grain"]), []))))))
    return out


def window_of(v: cd.View) -> ir.Window:
    return ir.Window(d_from=v.d_from, d_to=v.d_to, prev_from=v.prev_from, prev_to=v.prev_to)


# ============================================================================ payload (pure)
def build_thresholds_block() -> dict:
    return {"thresholds": ir.thresholds(), "rules": ir.rule_texts(),
            "notes": [f"Findings are computed per currency: each currency's rows are judged alone and money is never converted, added or compared across currencies.",
                      f"The previous window is the same number of days immediately before the analysed one; with no rows in it the comparison rules are skipped.",
                      f"Below a rule's floor nothing is said; between the floor and 'enough' a finding is info with confidence 'thin data', never a warning.",
                      "Everything is read from the daily rollup tables, never from the raw events. The engine only reads numbers: it never schedules, runs or changes anything."]}


def _headline(state: str, counts: dict, window: dict | None, currencies: list) -> dict:
    if state == "not_installed":
        return {"text": "Insights need the channel rollup, which is not installed yet.", "sub": "Nothing is analysed and nothing is invented.", "tone": "grey"}
    if state == "empty":
        return {"text": "There is no marketing data to analyse yet.", "sub": "The tables are installed but hold no rows.", "tone": "grey"}
    if state == "no_data_in_window":
        return {"text": "The rollup has no rows in the analysed window.", "sub": f"Analysed {ir.day_text(date.fromisoformat(window['from']))} to {ir.day_text(date.fromisoformat(window['to']))}; the rollup holds data from {window['data_first_day']} to {window['data_last_day']}. Widen the range to analyse it.", "tone": "amber"}
    n = counts["crit"] + counts["warn"]
    if n == 0 and not counts["info"]:
        return {"text": "No rule fired: nothing needs attention in this window.", "sub": "Rules that had too little data to judge are listed under 'rules checked'.", "tone": "green"}
    if n == 0:
        return {"text": f"Nothing urgent: {counts['info']} note{'s' if counts['info'] != 1 else ''} to read.", "sub": "Only info-level findings (including thin-data observations).", "tone": "blue"}
    parts = []
    if counts["crit"]:
        parts.append(f"{counts['crit']} critical")
    if counts["warn"]:
        parts.append(f"{counts['warn']} warning{'s' if counts['warn'] != 1 else ''}")
    return {"text": f"{n} thing{'s' if n != 1 else ''} to look at: " + ", ".join(parts) + ".", "sub": f"{counts['info']} more info-level." if counts["info"] else "", "tone": "red" if counts["crit"] else "amber"}


def _merge_placements(block, options: dict, win: ir.Window, base: dict, findings: list, rules: list, notes: list) -> tuple[list, list, list]:
    """Add the placement rules' findings, their rule statuses and their drawer text to an Insights payload. A block that could not be read costs the
    placement rules only (a note says so): the six campaign rules keep their answer."""
    if cd.td_failed(block):
        return findings, rules, notes + [f"The placement data could not be read ({(block or {}).get('error', 'unknown error') if isinstance(block, dict) else 'unknown error'}): the placement rules were not run."]
    groups, cut = pld.groups_from_block(block, options)
    base["how"]["thresholds"] = base["how"]["thresholds"] + ir.placement_thresholds()
    base["how"]["rules"] = base["how"]["rules"] + ir.placement_rule_texts()
    base["read"]["tables"] = base["read"]["tables"] + [pld.pq.PLACEMENT_TABLE]
    extra_notes = []
    if cut:
        extra_notes.append(f"More than {pld.pq.ROWS_MAX:,} placement groups are in view: the {pld.pq.ROWS_MAX:,} with the most clicks were analysed.")
    pres = ir.analyse_placements(groups, win)
    merged = sorted(findings + pres["findings"], key=ir._sort_key)
    return merged, rules + pres["rules"], notes + extra_notes + pres["notes"]


def assemble_insights(raw: dict, v: cd.View, options: dict, meta: dict, now: datetime | None = None) -> dict:
    """Raw query blocks -> the JSON of the Insights panel. Pure: no I/O."""
    now = now or datetime.now(timezone.utc)
    win = window_of(v)
    window = {"from": v.d_from.isoformat(), "to": v.d_to.isoformat(), "days": v.days, "prev_from": v.prev_from.isoformat(), "prev_to": v.prev_to.isoformat(),
              "defaulted": v.defaulted, "data_first_day": options.get("first_day"), "data_last_day": options.get("last_day")}
    base = {"ok": True, "generated_at": _iso(now), "time_zone": meta.get("tz"), "schema": meta.get("schema"), "window": window,
            "filters": {"channel": list(v.req.channels), "campaign": v.campaign_id, "currency": v.currency},
            "export": {"xlsx": "/api/channels/report.xlsx?" + cd.view_query(v), "insights": "/api/channels/insights?" + cd.view_query(v)},
            "read": {"tables": [cd.CAMPAIGN_TABLE, cd.CHANNEL_TABLE], "raw_fact_read": False},
            "how": build_thresholds_block(), "config": {"low_n": cd.LOW_N}}
    blocks = [raw.get(k) for k in ("groups", "spans", "days")]
    if any(cd.td_failed(b) for b in blocks):
        err = next((b.get("error") for b in blocks if isinstance(b, dict) and "error" in b), "could not be read")
        return {**base, "ok": False, "state": "unavailable", "error": err, "headline": {"text": "The insights could not be read right now.", "sub": "The rest of the page keeps working.", "tone": "red"},
                "findings": [], "counts": ir.counts([]), "notes": [], "rules": [], "automation": [], "currencies": []}
    groups, truncated = groups_from_rows(raw["groups"], options)
    spans = spans_from_rows(raw["spans"], raw["days"], options)
    notes = []
    days_cut = len(raw["days"]) > cd.DAILY_ROWS_MAX
    if days_cut:
        spans = [ir.Span(s.channel_key, s.channel_name, s.grain, s.first_day, s.last_day, s.history_days, ()) for s in spans]
        notes.append(f"The window holds more than {cd.DAILY_ROWS_MAX:,} day rows: holes inside the window were not checked. Narrow the range.")
    if truncated:
        notes.append(f"More than {iq.INSIGHT_GROUPS_MAX:,} campaign groups are in view: the {iq.INSIGHT_GROUPS_MAX:,} with the most clicks were analysed.")
    in_window = any(g.cur.days for g in groups) or any(s.window_days for s in spans)
    if not in_window:
        return {**base, "state": "no_data_in_window", "headline": _headline("no_data_in_window", ir.counts([]), window, []), "findings": [], "counts": ir.counts([]),
                "notes": notes, "rules": [], "automation": [], "currencies": []}
    res = ir.analyse(groups, spans, win)
    notes = notes + res["notes"]
    if not res["has_previous"]:
        notes.append(f"No rollup rows in the previous window ({ir.day_text(v.prev_from)} to {ir.day_text(v.prev_to)}), so comparisons (cost jump, conversion-rate drop, improvers) were not run.")
    findings, rules = res["findings"], res["rules"]
    placement = raw.get("placements")                    # only present when db/sql/11 is installed (the placement rules are optional)
    if placement is not None:
        findings, rules, notes = _merge_placements(placement, options, win, base, findings, rules, notes)
    counts = ir.counts(findings)
    return {**base, "state": "ready", "headline": _headline("ready", counts, window, res["currencies"]), "findings": findings, "counts": counts, "notes": notes,
            "rules": rules, "automation": ir.automation_ideas(findings), "currencies": res["currencies"],
            "campaign_groups": len(groups)}


def assemble_insights_setup(state: str, meta: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    return {"ok": True, "state": state, "generated_at": _iso(now), "time_zone": meta.get("tz"), "schema": meta.get("schema"), "window": None, "filters": {},
            "export": None, "read": {"tables": [], "raw_fact_read": False}, "how": build_thresholds_block(), "config": {"low_n": cd.LOW_N},
            "headline": _headline(state, ir.counts([]), None, []), "findings": [], "counts": ir.counts([]), "notes": [], "rules": [], "automation": [], "currencies": []}


# ============================================================================ Excel sheets (pure: spec -> insights_xlsx renders)
def _pct_frac(r) -> float | None:
    return None if r is None or r.get("pct") is None else r["pct"] / 100


def campaign_rows_for_sheet(groups: list[ir.Group]) -> list[list]:
    out = []
    for g in groups:
        c, p = g.cur, g.prev
        cvr = cd.ratio(c.conversions, c.clicks, "no clicks")
        ctr = cd.ratio(c.clicks, c.impressions, "no impressions")
        now, before = c.cpa(), p.cpa()
        chg = None if (now is None or before is None or before <= 0) else (now - before) / before
        cur = g.currency if g.has_money else ""
        out.append([g.channel_name, g.campaign_name, g.campaign_id, cur, c.sessions, c.clicks, c.impressions, c.conversions, _pct_frac(ctr), _pct_frac(cvr),
                    round(c.spend, 2) if c.spend_micros > 0 else None, round(c.revenue_micros / cd.MICROS, 2) if c.revenue_micros > 0 else None,
                    None if now is None else round(now, 2), p.conversions, round(p.spend, 2) if p.spend_micros > 0 else None,
                    None if before is None else round(before, 2), chg, c.days, p.days])
    return out


CAMPAIGN_HEADER = [("Channel", "text"), ("Campaign", "text"), ("Campaign id", "int_plain"), ("Currency", "text"), ("Sessions", "int"), ("Clicks", "int"),
                   ("Impressions", "int"), ("Conversions", "int"), ("CTR", "pct"), ("Conversion rate", "pct"), ("Spend", "money"), ("Revenue", "money"),
                   ("Cost per conversion", "money"), ("Conversions (previous window)", "int"), ("Spend (previous window)", "money"),
                   ("Cost per conversion (previous window)", "money"), ("Cost per conversion change", "pct"), ("Days with data", "int"),
                   ("Days with data (previous window)", "int")]
CAMPAIGN_CURRENCY_COL = 3

INSIGHT_HEADER = [("Severity", "text"), ("Rule", "text"), ("Channel", "text"), ("Campaign", "text"), ("Currency", "text"), ("What", "text_wide"),
                  ("Evidence", "text_wide"), ("Suggested action", "text_wide"), ("Confidence", "text")]


def insight_rows(findings: list[dict]) -> list[list]:
    return [[f["severity"], ir.RULE_TITLE.get(f["id"], f["id"]), (f["channel"] or {}).get("name", ""), (f["campaign"] or {}).get("name", ""), f["currency"] or "",
             f["what"], " | ".join(e["text"] for e in f["evidence"]), f["action"], f["confidence"]] for f in findings]


DEFINITION_HEADER = [("Section", "text"), ("Name", "text"), ("Value", "num"), ("Unit", "text"), ("Description", "text_wide")]


def definition_rows(defs: list[dict], how: dict) -> list[list]:
    rows = [["Threshold", t["name"], t["value"], t["unit"], f"{ir.RULE_TITLE.get(t['rule'], t['rule'])}: {t['meaning']}"] for t in how["thresholds"]]
    rows += [["Rule", r["title"], None, "", r["text"]] for r in how["rules"]]
    rows += [["How decided", "", None, "", n] for n in how["notes"]]
    rows += [["Definition", d["term"], None, "", d["text"]] for d in defs]
    rows.append(["Excel file", "Text cells", None, "", "A text value that starts with = + - @ (or a tab / carriage return) is written with a leading apostrophe so a spreadsheet never runs it as a formula. Numbers are real numeric cells."])
    rows.append(["Excel file", "Percentages", None, "", "Rates are stored as fractions with a percent format (0.123 shows as 12.3%). A blank rate means the denominator was below the low-n limit or zero: unknown, not 0%."])
    return rows


def summary_rows(v: cd.View, meta: dict, options: dict, totals: Mapping | None, money_rows: list, insights: dict, generated: str, truncated_note: str | None) -> list[list]:
    rows: list[list] = []

    def add(item, value=None, currency="", note=""):
        rows.append([item, value, currency, note])
    add("Report", "ERP Desk - Channels report", "", "Aggregates of the daily rollup only; no personal data.")
    add("Generated", generated, "", "")
    add("Time zone (database session)", meta.get("tz") or "unknown", "", "Dates are event_date as the source reported them.")
    add("Window from", v.d_from, "", "Both ends included." + (" Default window: the last %d days ending at the newest day the rollup holds." % cd.DEFAULT_DAYS if v.defaulted else ""))
    add("Window to", v.d_to)
    add("Days in window", v.days)
    add("Previous window from", v.prev_from, "", "Same length, immediately before the window (used for comparisons).")
    add("Previous window to", v.prev_to)
    add("Rollup holds data from", options.get("first_day") or "")
    add("Rollup holds data to", options.get("last_day") or "")
    add("Filter: channels", ", ".join(v.req.channels) or "all")
    add("Filter: campaign", "all" if v.campaign_id is None else str(v.campaign_id))
    add("Filter: currency", v.currency or "all", "", "" if v.currency else "Money is never converted or added across currencies.")
    if totals:
        m = cd.measures(totals)
        anything = any(m.values())
        for label, key in (("Sessions", "sessions"), ("Clicks", "clicks"), ("Impressions", "impressions"), ("Conversions", "conversions")):
            zero = anything and m[key] == 0
            add(label, m[key], "", "0 here can mean 'none happened' or 'the sources in view do not report it': the rollup cannot tell (L-030)" if zero else "")
        for label, num, den, why in (("CTR", m["clicks"], m["impressions"], "no impressions"), ("Conversion rate", m["conversions"], m["clicks"], "no clicks")):
            r = cd.ratio(num, den, why)
            add(label, _pct_frac(r), "", "" if r["pct"] is not None else (f"unknown: {r['why']}" if r["why"] else f"unknown: fewer than {cd.LOW_N} in the denominator ({num:,} of {den:,})"))
    else:
        add("Numbers", None, "", "no rollup rows in this window")
    groups = cd.group_money(money_rows)
    if not groups:
        add("Spend", None, "", "no spend field in this view (a 0 is not shown as a value)")
        add("Revenue", None, "", "no revenue field in this view")
    for cur, g in groups.items():
        add("Spend", round(g["spend_micros"] / cd.MICROS, 2) if g["spend_micros"] > 0 else None, cur, "" if g["spend_micros"] > 0 else "no spend field in this currency")
        add("Revenue", round(g["revenue_micros"] / cd.MICROS, 2) if g["revenue_micros"] > 0 else None, cur,
            "" if g["revenue_micros"] <= 0 else ("derived from purchases x value per purchase" if g["revenue_derived_micros"] >= g["revenue_micros"] else "partly derived" if g["revenue_derived_micros"] else ""))
        one = cd.cost_per_conversion(g["spend_micros"], g["conversions"], cur)
        add("Cost per conversion", one["value"], cur, "" if one["value"] is not None else one["why"])
    c = insights.get("counts") or {}
    add("Insights: critical", c.get("crit", 0))
    add("Insights: warnings", c.get("warn", 0))
    add("Insights: info", c.get("info", 0), "", f"{c.get('thin', 0)} of all findings are thin data" if c.get("thin") else "")
    for n in (insights.get("notes") or []):
        add("Note", n)
    if truncated_note:
        add("Note", truncated_note)
    return rows


# ============================================================================ store
class InsightsStore(cd.ChannelsStore):
    """The /api/channels/insights and /api/channels/report.xlsx builders. Same cache, same build slots and same whitelist as the
    Channels store (it IS one: get() validates names and shapes BEFORE the cache, _build() differs)."""

    def _build(self, params: Mapping[str, list[str]]) -> tuple[int, dict]:
        try:
            schema = mschema.check_identifier(cfg.MARKETING_SCHEMA)
        except mschema.SchemaError as exc:
            return 503, {"ok": False, "error": f"MARKETING_SCHEMA is not usable: {exc}", "unavailable": True}
        try:
            with self._connect() as conn:
                rd = ld._Reader(conn, cd.QUERY_BUDGET_SECONDS)
                built = cd.build_view(rd, schema, params, cd.KNOWN_PARAMS)
                if built[0] == "down":
                    conn.rollback()
                    return self._down(built[1])
                if built[0] == "bad":
                    conn.rollback()
                    return self._bad_request(built[1], built[2])
                if built[0] == "setup":
                    conn.rollback()
                    return 200, assemble_insights_setup(built[1], built[2])
                _k, v, options, meta, _req = built
                raw = iq.collect_insights(rd, schema, v)
                pld.collect_if_installed(rd, schema, v)
                conn.rollback()
        except Exception as exc:  # noqa: BLE001
            return self._down(f"{type(exc).__name__}: {_redact(str(exc))}")
        ld._logged.pop("channels_database", None)
        return 200, assemble_insights(raw, v, options, meta)

    def export_xlsx(self, params: Mapping[str, list[str]]) -> tuple[int, bytes | dict, dict]:
        """(status, xlsx bytes | error dict, extra headers). Same filters and whitelists as the page."""
        from desktop import insights_xlsx as ix
        bad = cd.unknown_params(params)
        _req, shape = cd.parse_request(params)
        if bad or shape:
            code, body = self._bad_request(bad or shape)
            return code, body, {}
        if not self._slots.acquire(timeout=cd.BUILD_WAIT_SECONDS):
            code, body = self._busy()
            return code, body, {}
        try:
            schema = mschema.check_identifier(cfg.MARKETING_SCHEMA)
            with self._connect() as conn:
                rd = ld._Reader(conn, XLSX_BUDGET_SECONDS, statement_ms=int(XLSX_BUDGET_SECONDS * 1000))
                built = cd.build_view(rd, schema, params, cd.KNOWN_PARAMS)
                if built[0] == "down":
                    conn.rollback()
                    return 503, {"ok": False, "error": "Can’t read the channel data right now - " + built[1]}, {}
                if built[0] == "bad":
                    conn.rollback()
                    code, body = self._bad_request(built[1])
                    return code, body, {}
                if built[0] == "setup":
                    conn.rollback()
                    what = "installed" if built[1] == cd.STATE_NOT_INSTALLED else "loaded"
                    return 409, {"ok": False, "error": f"There is nothing to export: the channel data is not {what} yet.", "state": built[1]}, {}
                _k, v, options, meta, _req = built
                sql, p = cd.totals_sql(schema, v)
                totals_rows = rd.rows(sql, p)
                sql, p = cd.channels_sql(schema, v)
                ch_rows = rd.rows(sql, p)
                msql, mp = cd.money_sql(schema, v, "channel")
                ch_money = rd.rows(msql, mp)
                msql, mp = cd.money_sql(schema, v, "total")
                tot_money = rd.rows(msql, mp)
                raw = iq.collect_insights(rd, schema, v)
                pld.collect_if_installed(rd, schema, v)
                conn.rollback()
            insights = assemble_insights(raw, v, options, meta)
            if insights.get("state") == "unavailable":
                return 503, {"ok": False, "error": "The insights part could not be read: " + str(insights.get("error"))}, {}
            groups, _cut = groups_from_rows(raw["groups"], options)
            table = cd.build_table(ch_rows, options, v, cap=None, money=ch_money)
            pct_cols = [i for i, h in enumerate(cd.CHANNEL_CSV) if ix.kind_of_channel_column(h) == "pct"]
            chan_rows = [[(x / 100 if (i in pct_cols and x is not None) else x) for i, x in enumerate(r)] for r in cd.channel_csv_rows(table, v, meta.get("tz"))]   # the CSV holds percent numbers, the sheet fractions
            cut = len(groups) > XLSX_CAMPAIGN_ROWS_MAX
            camp = campaign_rows_for_sheet(groups[:XLSX_CAMPAIGN_ROWS_MAX])
            generated = datetime.now().strftime("%Y-%m-%d %H:%M")
            tnote = f"The Campaigns sheet was cut at {XLSX_CAMPAIGN_ROWS_MAX:,} rows; narrow the filters." if cut else None
            defs = cd.build_definitions(meta.get("tz"), meta.get("schema") or cfg.MARKETING_SCHEMA)
            place_sheet, place_rows = None, 0
            pblock = raw.get("placements")
            if pblock is not None and not cd.td_failed(pblock):                    # db/sql/11 installed: a Placements sheet (all rows in the view, bounded BEFORE building it: L-239)
                slots, _cutp = pld.build_slots(pblock)
                cells = pld.sort_rows([pld.slot_cells(s) for s in slots], pld.DEFAULT_PSORT, pld.PSORTS[pld.DEFAULT_PSORT][1])
                if len(cells) > pld.XLSX_ROWS_MAX:
                    insights["notes"] = list(insights.get("notes") or []) + [f"The Placements sheet was cut at {pld.XLSX_ROWS_MAX:,} rows (largest spend first); narrow the filters or use the placements CSV."]
                    cells = cells[:pld.XLSX_ROWS_MAX]
                if cells:
                    prow = pld.sheet_rows(cells, v, meta.get("tz"))
                    place_sheet = ix.Sheet("Placements", [(h, pld.sheet_kind(h)) for h in pld.PLACEMENT_CSV], prow, currency_col=pld.PLACEMENT_CSV.index("currency"))
                    place_rows = len(prow)
            sheets = [
                ix.Sheet("Summary", [("Item", "text"), ("Value", "auto"), ("Currency", "text"), ("Note", "text_wide")],
                         summary_rows(v, meta, options, totals_rows[0] if totals_rows else None, tot_money, insights, generated, tnote), currency_col=2),
                ix.Sheet("Channels", [(h, ix.kind_of_channel_column(h)) for h in cd.CHANNEL_CSV], chan_rows, currency_col=cd.CHANNEL_CSV.index("currency")),
                ix.Sheet("Campaigns", CAMPAIGN_HEADER, camp, currency_col=CAMPAIGN_CURRENCY_COL),
                *([place_sheet] if place_sheet else []),
                ix.Sheet("Insights", INSIGHT_HEADER, insight_rows(insights["findings"])),
                ix.Sheet("Definitions", DEFINITION_HEADER, definition_rows(defs, insights["how"])),
            ]
            body = ix.build_workbook(sheets)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Channels Excel report failed: %s: %s", type(exc).__name__, _redact(str(exc)))
            return 503, {"ok": False, "error": f"The report could not be built ({type(exc).__name__}). Try again in a moment."}, {}
        finally:
            self._slots.release()
        if len(body) > XLSX_MAX_BYTES:
            return 413, {"ok": False, "error": f"The report would be larger than {XLSX_MAX_BYTES // (1024 * 1024)} MB: narrow the range or the channels."}, {}
        name = "channels_report_" + datetime.now().strftime("%Y%m%d_%H%M") + ("_filtered" if v.req.active() else "") + ".xlsx"
        return 200, body, {"Content-Disposition": f'attachment; filename="{name}"', "X-Row-Count": str(len(chan_rows) + len(camp)),
                           "X-Export-Truncated": "true" if cut else "false"}
