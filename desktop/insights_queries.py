"""Channels page, INSIGHTS database layer: the read-only queries the insights engine and the Excel report need (same style as
desktop/channels_queries.py: identifiers from schema.qualified(), every value a bound parameter, one statement per block, each
isolated so a failed block only takes its own part down).

It reads the two rollup tables only - never interaction_fact, never marketing_landing - plus the tiny campaign-name lookup:
  groups_sql   one row per (channel, campaign, currency) with the sums of the analysed window (c_*) and of the previous window
               of the same length (p_*), from the CAMPAIGN rollup, in ONE scan of the two windows (a FILTER per window)
  spans_sql    per channel and grain: first / last row day and number of row days, up to the window end (for the stale-channel rule)
  days_sql     the distinct row dates per channel and grain inside the window (for the holes rule)
  names_sql    campaign names for the ids in view

The request's filters (range, channel, campaign, currency) go through channels_queries.where_sql exactly like the page's own queries,
so the insights describe the same rows the tiles do. Money is grouped by currency in SQL: nothing here adds two currencies.
"""
from __future__ import annotations

from datetime import date

from desktop import leads_data as ld
from desktop.channels_queries import (CAMPAIGN_DIM, CAMPAIGN_TABLE, DAILY_ROWS_MAX, _t, source_table, where_sql)
from desktop.channels_request import View

INSIGHT_GROUPS_MAX = 5_000       # (channel, campaign, currency) groups read for the rules; beyond it the payload says the list was cut
SPAN_ROWS_MAX = 2_000            # (channel, grain) rows of the span query

GROUP_MEASURES = ("sessions", "clicks", "impressions", "conversions", "spend_micros", "revenue_micros")


def _split_columns() -> str:
    cols = []
    for m in GROUP_MEASURES:
        cols.append(f"COALESCE(sum({m}) FILTER (WHERE event_date >= :cur_from), 0)::bigint AS c_{m}")
        cols.append(f"COALESCE(sum({m}) FILTER (WHERE event_date < :cur_from), 0)::bigint AS p_{m}")
    cols.append("count(DISTINCT event_date) FILTER (WHERE event_date >= :cur_from) AS c_days")
    cols.append("count(DISTINCT event_date) FILTER (WHERE event_date < :cur_from) AS p_days")
    return ", ".join(cols)


def groups_sql(schema: str, v: View, limit: int) -> tuple[str, dict]:
    """The campaign rollup over the previous window AND the analysed window (they are adjacent), split with a FILTER on the first day
    of the analysed window. Ordered by clicks (a count, so no currency decides which groups survive the LIMIT)."""
    w, p = where_sql(v, v.prev_from)
    p["cur_from"] = v.d_from
    p["lim"] = limit
    return (f"SELECT channel_id, campaign_id, currency, {_split_columns()} FROM {_t(schema, CAMPAIGN_TABLE)} WHERE {w} "
            f"GROUP BY channel_id, campaign_id, currency ORDER BY sum(clicks) DESC, sum(conversions) DESC, channel_id, campaign_id, currency LIMIT :lim"), p


def spans_sql(schema: str, v: View) -> tuple[str, dict]:
    """Per channel and grain: the first and last row day and how many distinct row days, from the start of time up to the window end."""
    w, p = where_sql(v, ld.DATE_MIN)
    p["lim"] = SPAN_ROWS_MAX + 1
    return (f"SELECT channel_id, grain, min(event_date) AS first_day, max(event_date) AS last_day, count(DISTINCT event_date) AS history_days "
            f"FROM {source_table(schema, v)} WHERE {w} GROUP BY channel_id, grain ORDER BY channel_id, grain LIMIT :lim"), p


def days_sql(schema: str, v: View) -> tuple[str, dict]:
    """The distinct row dates per channel and grain inside the window (bounded like the chart's query)."""
    w, p = where_sql(v)
    p["lim"] = DAILY_ROWS_MAX + 1
    return (f"SELECT DISTINCT channel_id, grain, event_date FROM {source_table(schema, v)} WHERE {w} "
            f"ORDER BY channel_id, grain, event_date LIMIT :lim"), p


def names_sql(schema: str) -> str:
    return f"SELECT campaign_id, name, campaign_key FROM {_t(schema, CAMPAIGN_DIM)} WHERE campaign_id = ANY(CAST(:ids AS bigint[]))"


def collect_insights(rd, schema: str, v: View) -> dict:
    """The insights blocks, each isolated: groups (+ names), spans, days. A failure gives {"error": ...} for that block only."""
    sql, p = groups_sql(schema, v, INSIGHT_GROUPS_MAX + 1)

    def groups():
        rows = rd.rows(sql, p)
        ids = sorted({int(r["campaign_id"]) for r in rows[:INSIGHT_GROUPS_MAX] if int(r["campaign_id"]) != 0})
        names = rd.rows(names_sql(schema), {"ids": ids}) if ids else []
        return {"rows": rows, "names": names}
    rd.block("groups", groups)
    sql, p = spans_sql(schema, v)
    rd.block("spans", lambda: rd.rows(sql, p))
    sql, p = days_sql(schema, v)
    rd.block("days", lambda: rd.rows(sql, p))
    return rd.raw
