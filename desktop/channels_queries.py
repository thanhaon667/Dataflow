"""Channels page, database layer: table and SQL-fragment constants, the query builders and the read-only state / lookup readers (split out of desktop/channels_data.py, unchanged).
"""
from __future__ import annotations

import dataclasses
from datetime import date
from typing import Callable

from desktop.flow_data import _redact
from erp.marketing import rollup as mroll
from erp.marketing import schema as mschema
from desktop.channels_request import View


DAILY_ROWS_MAX = 30_000          # (day, channel) rows read for the chart

CHANNEL_TABLE = mroll.CHANNEL_TABLE
CAMPAIGN_TABLE = mroll.ROLLUP_TABLE
RUN_TABLE = mroll.RUN_TABLE
CHANNEL_DIM, CAMPAIGN_DIM = "marketing_channel", "marketing_campaign"
REQUIRED_TABLES = tuple(mschema.MARKETING_TABLES) + tuple(mroll.ROLLUP_TABLES)   # 07 + 09: everything the page depends on

# ---- SQL: identifiers come from schema.qualified(), values are bound parameters -------------------------------------
SUMS_SQL = ("COALESCE(sum(sessions), 0)::bigint AS sessions, COALESCE(sum(clicks), 0)::bigint AS clicks, "
            "COALESCE(sum(impressions), 0)::bigint AS impressions, COALESCE(sum(conversions), 0)::bigint AS conversions")
MONEY_ONLY_SQL = ("COALESCE(sum(spend_micros), 0)::bigint AS spend_micros, COALESCE(sum(revenue_micros), 0)::bigint AS revenue_micros, "
                  "COALESCE(sum(revenue_derived_micros), 0)::bigint AS revenue_derived_micros")
MONEY_SUMS_SQL = MONEY_ONLY_SQL + ", COALESCE(sum(conversions), 0)::bigint AS conversions"     # conversions of the SAME currency's rows: the cost-per-conversion denominator
PERIODS_SQL = ("count(DISTINCT event_date) FILTER (WHERE grain = 'day') AS day_periods, "
               "count(DISTINCT event_date) FILTER (WHERE grain = 'week') AS week_periods")
CURRENCY_SQL = ("SELECT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass(:q) AND attname = 'currency' "
                "AND attnum > 0 AND NOT attisdropped) AS ok")
INSTALL_SQL = ("SELECT current_setting('TimeZone') AS tz, t AS name, to_regclass(:s || '.' || t) IS NULL AS missing "
               "FROM unnest(CAST(:names AS text[])) AS t")


# ============================================================================ SQL (read-only)
def _t(schema: str, table: str) -> str:
    return mschema.qualified(schema, table)


def where_sql(v: View, lower: date | None = None, *, use_channels: bool = True) -> tuple[str, dict]:
    """The WHERE text is assembled ONLY from the fixed fragments below; every value travels as a bound parameter."""
    clauses = ["event_date >= :d_from", "event_date <= :d_to"]
    params: dict = {"d_from": lower or v.d_from, "d_to": v.d_to}
    if use_channels and v.channel_ids:
        clauses.append("channel_id = ANY(CAST(:channel_ids AS smallint[]))")
        params["channel_ids"] = list(v.channel_ids)
    if v.campaign_id is not None:
        clauses.append("campaign_id = :campaign_id")
        params["campaign_id"] = v.campaign_id
    if v.currency:
        clauses.append("currency = :currency")
        params["currency"] = v.currency
    return " AND ".join(clauses), params


def source_table(schema: str, v: View) -> str:
    return _t(schema, CAMPAIGN_TABLE if v.campaign_id is not None else CHANNEL_TABLE)


def totals_sql(schema: str, v: View, prev: bool = False) -> tuple[str, dict]:
    w, p = where_sql(dataclasses.replace(v, d_from=v.prev_from, d_to=v.prev_to) if prev else v)
    return (f"SELECT count(DISTINCT event_date) AS days_with_data, {PERIODS_SQL}, min(event_date) AS first_day, max(event_date) AS last_day, {SUMS_SQL} "
            f"FROM {source_table(schema, v)} WHERE {w}"), p


def channels_sql(schema: str, v: View) -> tuple[str, dict]:
    w, p = where_sql(v)
    return (f"SELECT channel_id, count(DISTINCT event_date) AS days_with_data, {SUMS_SQL} FROM {source_table(schema, v)} "
            f"WHERE {w} GROUP BY channel_id"), p


def daily_sql(schema: str, v: View, lower: date, limit: int, by_currency: bool = False, week_lower: date | None = None) -> tuple[str, dict]:
    """One row per day x channel x grain (the chart). `by_currency` (the CSV) adds the currency and the money columns and groups by
    it too: every fact belongs to one currency, so the counts split cleanly and no money is ever added across currencies.
    `week_lower` (the chart) lets WEEKLY rows reach back further than daily ones: daily rows start at `lower`, weekly rows at `week_lower`
    (the range start): a weekly source has one row per week, so the chart limits it by bar count instead (CHART_MAX_WEEKS)."""
    if week_lower is not None and week_lower < lower:
        w, p = where_sql(v, week_lower)
        w += " AND (grain = 'week' OR event_date >= :day_lower)"
        p["day_lower"] = lower
    else:
        w, p = where_sql(v, lower)
    p["lim"] = limit
    cur = ", currency" if by_currency else ""
    money = f", {MONEY_ONLY_SQL}" if by_currency else ""
    return (f"SELECT event_date, channel_id, grain{cur}, {SUMS_SQL}{money} FROM {source_table(schema, v)} WHERE {w} "
            f"GROUP BY event_date, channel_id, grain{cur} ORDER BY event_date, channel_id, grain{cur} LIMIT :lim"), p


MONEY_GROUPS = {"total": "currency", "channel": "channel_id, currency", "daily": "event_date, currency", "campaign": "campaign_id, currency"}


def money_sql(schema: str, v: View, group: str, prev: bool = False, lower: date | None = None, campaign_ids=None, week_lower: date | None = None) -> tuple[str, dict]:
    """The money of the view, one row per currency (and per channel / day / campaign): spend, revenue, the derived part of the
    revenue and the conversions on those same rows. Groups with no spend and no revenue are dropped in SQL. The addition happens
    HERE, per currency, and nowhere else: no query of this page ever sums money across currencies."""
    cols = MONEY_GROUPS[group]
    if week_lower is not None and lower is not None and week_lower < lower:
        w, p = where_sql(v, week_lower)
        w += " AND (grain = 'week' OR event_date >= :day_lower)"
        p["day_lower"] = lower
    else:
        w, p = where_sql(dataclasses.replace(v, d_from=v.prev_from, d_to=v.prev_to) if prev else v, lower)
    extra = ""
    if group == "campaign":
        extra = " AND channel_id = :focus_id AND campaign_id = ANY(CAST(:campaign_ids AS bigint[]))"
        p.update({"focus_id": v.focus_id, "campaign_ids": [int(c) for c in (campaign_ids or [])]})
    p["lim"] = DAILY_ROWS_MAX + 1
    src = _t(schema, CAMPAIGN_TABLE) if group == "campaign" else source_table(schema, v)      # only the campaign rollup has a campaign column
    return (f"SELECT {cols}, {MONEY_SUMS_SQL} FROM {src} WHERE {w}{extra} GROUP BY {cols} "
            f"HAVING (COALESCE(sum(spend_micros), 0) <> 0 OR COALESCE(sum(revenue_micros), 0) <> 0) ORDER BY {cols} LIMIT :lim"), p


def campaigns_sql(schema: str, v: View, limit: int) -> tuple[str, dict]:
    w, p = where_sql(v)
    p.update({"focus_id": v.focus_id, "lim": limit})
    return (f"SELECT campaign_id, count(DISTINCT event_date) AS days_with_data, {SUMS_SQL}, count(*) OVER () AS total_campaigns "
            f"FROM {_t(schema, CAMPAIGN_TABLE)} WHERE {w} AND channel_id = :focus_id GROUP BY campaign_id "
            f"ORDER BY sum(sessions) DESC, sum(clicks) DESC, campaign_id LIMIT :lim"), p


def read_state(rd, schema: str) -> dict:
    """The install check and, when installed, what the page needs before it can even validate a filter: how much the rollup
    holds, which channels and currencies, when it was refreshed. Raises when the database cannot answer (the route says 503)."""
    rows = rd.rows(INSTALL_SQL, {"s": schema, "names": list(REQUIRED_TABLES)})
    meta: dict = {"schema": schema, "tz": rows[0]["tz"] if rows else None, "missing": [r["name"] for r in rows if r["missing"]]}
    try:
        meta["currency_ok"] = bool(rd.rows(CURRENCY_SQL, {"q": f"{schema}.{mschema.FACT_TABLE}"})[0]["ok"])      # does the FACT have migration 10's column?
    except Exception:  # noqa: BLE001 - only the help text depends on it
        try:
            rd.conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        meta["currency_ok"] = None
    if meta["missing"]:
        return meta
    ch = _t(schema, CHANNEL_TABLE)
    agg = rd.rows(f"SELECT count(*) AS n, min(event_date) AS first_day, max(event_date) AS last_day, max(refreshed_at) AS data_as_of FROM {ch}")[0]
    meta.update({"rollup_rows": int(agg["n"]), "first_day": agg["first_day"], "last_day": agg["last_day"], "data_as_of": agg["data_as_of"]})
    meta["channel_rows"] = []
    meta["currency_rows"] = []
    if meta["rollup_rows"]:
        meta["channel_rows"] = rd.rows(
            f"SELECT r.channel_id, r.days, r.sessions, c.channel_key, c.display_name, c.medium, c.is_paid FROM "
            f"(SELECT channel_id, count(*) AS days, COALESCE(sum(sessions), 0) AS sessions FROM {ch} GROUP BY channel_id) r "
            f"LEFT JOIN {_t(schema, CHANNEL_DIM)} c ON c.channel_id = r.channel_id ORDER BY r.sessions DESC, r.channel_id")
        meta["currency_rows"] = rd.rows(
            f"SELECT currency, count(DISTINCT event_date) AS days, bool_or(spend_micros <> 0 OR revenue_micros <> 0) AS has_money "
            f"FROM {ch} GROUP BY currency ORDER BY currency")
    try:
        meta["journal"] = rd.rows(
            f"SELECT max(finished_at) FILTER (WHERE status = 'ok') AS last_ok_at, count(*) AS runs, "
            f"(SELECT status FROM {_t(schema, RUN_TABLE)} ORDER BY started_at DESC LIMIT 1) AS last_status FROM {_t(schema, RUN_TABLE)}")[0]
    except Exception as exc:  # noqa: BLE001 - the journal only adds a line; the numbers do not need it
        try:
            rd.conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        meta["journal"] = {"error": f"{type(exc).__name__}: {_redact(str(exc))}"}
    return meta


def campaign_lookup(rd, schema: str) -> Callable[[int], bool]:
    """Whitelist for the campaign filter: 0 (= no campaign) or a row of marketing_campaign - one primary-key probe."""
    def exists(cid: int) -> bool:
        if cid == 0:
            return True
        return bool(rd.rows(f"SELECT 1 AS x FROM {_t(schema, CAMPAIGN_DIM)} WHERE campaign_id = :c", {"c": cid}))
    return exists


def campaign_name(rd, schema: str, cid: int | None) -> str | None:
    if cid is None:
        return None
    if cid == 0:
        return "(no campaign)"
    try:
        rows = rd.rows(f"SELECT name, campaign_key FROM {_t(schema, CAMPAIGN_DIM)} WHERE campaign_id = :c", {"c": cid})
        return (rows[0]["name"] or rows[0]["campaign_key"]) if rows else None
    except Exception:  # noqa: BLE001
        return None
