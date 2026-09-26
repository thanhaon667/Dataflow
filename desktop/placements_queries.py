"""Channels page, PLACEMENTS database layer: the read-only queries behind the Placements panel, the Insights placement rules and the Excel
Placements sheet (same style as desktop/insights_queries.py: identifiers from schema.qualified(), every value a bound parameter, one statement
per block, each isolated so a failed block only takes its own part down).

It reads ONE rollup table - `interaction_placement_rollup` (db/sql/11_marketing_placements.sql, kept current by erp/marketing/rollup_placements.py) -
plus two tiny dimension lookups (the creative rows that name the placements, the campaign names). It never reads `interaction_fact` and never
`marketing_landing`, so the cost of a request follows days x campaigns x placements (bounded by the rollup's cardinality guard), not the number
of raw rows. The table is OPTIONAL: `installed()` says whether it exists, and every caller answers an honest "not installed" state without it.

The request's filters (range, channel, campaign, currency, and the drill-down channel `focus`) go through the same
channels_queries.where_sql the page's other queries use, so the placements describe the same rows the tiles do.
"""
from __future__ import annotations

from desktop.channels_queries import CAMPAIGN_DIM, _t, where_sql
from desktop.channels_request import View
from erp.marketing import rollup_placements as rp

PLACEMENT_TABLE = rp.PLACEMENT_TABLE
CREATIVE_DIM = rp.CREATIVE_TABLE
ROWS_MAX = 50_000            # (channel, campaign, placement, currency, type, size, position) groups read for one payload; beyond it the page says the list was cut
NAMES_MAX = 60_000

SUMS = ("COALESCE(sum(impressions), 0)::bigint AS impressions, COALESCE(sum(clicks), 0)::bigint AS clicks, "
        "COALESCE(sum(conversions), 0)::bigint AS conversions, COALESCE(sum(spend_micros), 0)::bigint AS spend_micros, "
        "COALESCE(sum(revenue_micros), 0)::bigint AS revenue_micros, COALESCE(sum(viewable_impressions), 0)::bigint AS viewable, "
        "COALESCE(sum(viewability_base_impressions), 0)::bigint AS viewable_base")
GROUP = "channel_id, campaign_id, placement_id, currency, placement_type, ad_size, position"


def installed(rd, schema: str) -> bool:
    """Is db/sql/11 installed in this schema? (to_regclass never raises; a failure of the probe itself propagates to the caller's block.)"""
    return bool(rd.rows("SELECT to_regclass(:q) IS NOT NULL AS ok", {"q": f"{schema}.{PLACEMENT_TABLE}"})[0]["ok"])


def has_rows(rd, schema: str) -> dict:
    """Does the placement rollup hold anything at all, and from / to which day? min / max of the leading key column: no scan."""
    return rd.rows(f"SELECT min(event_date) AS first_day, max(event_date) AS last_day FROM {_t(schema, PLACEMENT_TABLE)}")[0]


def rows_sql(schema: str, v: View, limit: int) -> tuple[str, dict]:
    """The window's placement groups. Ordered by clicks (a count) so that no currency decides which groups survive the LIMIT: money is never
    ranked across currencies. `focus` (the channel drill-down of the page) narrows to that channel."""
    w, p = where_sql(v)
    if v.focus_id is not None:
        w += " AND channel_id = :focus_id"
        p["focus_id"] = v.focus_id
    p["lim"] = limit
    return (f"SELECT {GROUP}, {SUMS} FROM {_t(schema, PLACEMENT_TABLE)} WHERE {w} GROUP BY {GROUP} "
            f"ORDER BY sum(clicks) DESC, sum(impressions) DESC, channel_id, campaign_id, placement_id, currency, placement_type, ad_size, position LIMIT :lim"), p


def creatives_sql(schema: str) -> str:
    return f"SELECT creative_id, creative_key, name FROM {_t(schema, CREATIVE_DIM)} WHERE creative_id = ANY(CAST(:ids AS bigint[]))"


def campaign_names_sql(schema: str) -> str:
    return f"SELECT campaign_id, name, campaign_key FROM {_t(schema, CAMPAIGN_DIM)} WHERE campaign_id = ANY(CAST(:ids AS bigint[]))"


def folded_sql(schema: str) -> str:
    """Distinct placements beyond the cardinality cap of the given campaigns (read from the small creative dimension, never the fact)."""
    return (f"SELECT count(*) AS n FROM (SELECT row_number() OVER (PARTITION BY campaign_id ORDER BY creative_id) AS rn FROM {_t(schema, CREATIVE_DIM)} "
            f"WHERE creative_key ~ '{rp.PLACEMENT_KEY_RE}' AND campaign_id = ANY(CAST(:ids AS bigint[]))) ranked WHERE rn > :cap")


def collect(rd, schema: str, v: View) -> dict:
    """{"rows": [...], "creatives": [...], "campaigns": [...], "folded": int} - each part isolated by the block()."""
    sql, p = rows_sql(schema, v, ROWS_MAX + 1)

    def read():
        rows = rd.rows(sql, p)
        kept = rows[:ROWS_MAX]
        pids = sorted({int(r["placement_id"]) for r in kept if int(r["placement_id"]) != 0})[:NAMES_MAX]
        cids = sorted({int(r["campaign_id"]) for r in kept if int(r["campaign_id"]) != 0})
        return {"rows": rows,
                "creatives": rd.rows(creatives_sql(schema), {"ids": pids}) if pids else [],
                "campaigns": rd.rows(campaign_names_sql(schema), {"ids": cids}) if cids else [],
                "folded": int(rd.rows(folded_sql(schema), {"ids": cids, "cap": rp.MAX_PLACEMENTS_PER_CAMPAIGN})[0]["n"]) if cids else 0}
    rd.block("placements", read)
    return rd.raw
