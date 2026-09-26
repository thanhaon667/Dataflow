"""The PLACEMENT ROLLUP: day x channel x campaign x placement x type x size x position x currency sums of the placement_performance facts.

A Placements page must never aggregate the raw fact (db/sql/11_marketing_placements.sql and docs/marketing-data-architecture.md s.11 give the
measurement). This module keeps `interaction_placement_rollup` current. It is called by erp/marketing/rollup.py inside the SAME job, for the SAME
date ranges (the last --days days plus every older day whose facts were loaded or restated since the last good run - the loaded_at watermark), so there
is no second job to schedule and no second watermark. Each range is one transaction: DELETE the range, INSERT ... SELECT ... GROUP BY from the fact.

Optional by design: when db/sql/11 is not installed the rollup job logs one line and carries on, and the existing rollup tables are untouched
either way (their numbers do not depend on this table).

WHICH FACTS. Rows whose attrs.placement_row is true (only the placement_performance connector writes it). Selecting by that flag, not by the source name,
means a source renamed with --source is still rolled up; the bootstrap below looks for the default source name only (see needs_bootstrap).

THE CARDINALITY GUARD. A campaign can run on an unbounded number of sites and apps. The first MAX_PLACEMENTS_PER_CAMPAIGN placements of each campaign
(by creative_id = by first appearance, a rule that does not move when a later day is loaded) keep their own rows; every further placement is folded into
placement_id = 0 ("other placements"). Sums stay exact, only the naming stops, and `folded_placements` records how many distinct placements were folded into
that row that day. The per-refresh report says how many placements are folded in total.

Run: imported by erp/marketing/rollup.py (python -m erp.marketing.rollup); nothing here runs on import.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from sqlalchemy import text

from erp.marketing import schema as mschema

logger = logging.getLogger("erp.marketing.rollup_placements")

PLACEMENT_TABLE = "interaction_placement_rollup"
CREATIVE_TABLE = "marketing_creative"
SOURCE_TABLE = "marketing_source"
DEFAULT_SOURCE = "placement_performance"
MAX_PLACEMENTS_PER_CAMPAIGN = 500
# the creative rows the placement connector writes end in "_" + 8 hex digits (connectors/placement_performance.creative_key_of). The cap ranks ONLY these, so a
# campaign's creatives from another source (an ad name, say) never use up placement slots.
PLACEMENT_KEY_RE = "_[0-9a-f]{8}$"

PSQL_11 = (r'& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U erp_app -h 127.0.0.1 -p 5432 -d erp_support '
           r'-f db\sql\11_marketing_placements.sql')

# the columns the rollup table carries beyond its key
SUM_COLUMNS = ("fact_rows", "impressions", "clicks", "conversions", "spend_micros", "revenue_micros", "viewable_impressions",
               "viewability_base_impressions")


@dataclass
class PlacementResult:
    installed: bool = False
    rows_deleted: int = 0
    rows_written: int = 0
    folded_placements: int = 0          # distinct placements that sit in the "other" bucket after this refresh (whole table)
    bootstrapped: bool = False          # the table was empty and placement facts existed: every day with such facts was built
    note: str = ""

    def summary(self) -> str:
        if not self.installed:
            return "placement rollup not installed (db/sql/11_marketing_placements.sql): skipped"
        return (f"placement rollup: {self.rows_deleted} row(s) replaced by {self.rows_written}"
                + (f", {self.folded_placements} placement(s) folded into 'other'" if self.folded_placements else "")
                + (" (built from scratch: the table was empty)" if self.bootstrapped else "")
                + (f" - {self.note}" if self.note else ""))


def _t(schema: str, table: str) -> str:
    return mschema.qualified(schema, table)


def is_installed(conn, schema: str) -> bool:
    """Does the placement rollup table exist in `schema`? (to_regclass never raises.)"""
    return not conn.execute(text("SELECT to_regclass(:q) IS NULL"), {"q": f"{mschema.check_identifier(schema)}.{PLACEMENT_TABLE}"}).scalar()


def _placement_source_id(conn, schema: str):
    return conn.execute(text(f"SELECT source_id FROM {_t(schema, SOURCE_TABLE)} WHERE source_key = :k"), {"k": DEFAULT_SOURCE}).scalar()


def needs_bootstrap(conn, schema: str) -> tuple[date, date] | None:
    """(first day, last day) of the default placement source's facts when the placement rollup is EMPTY but such facts exist, else None. This is
    what makes installing migration 11 AFTER placement data was loaded work without --full. It finds the facts through the indexed (source, date)
    columns, so it is cheap on a big fact table; a source renamed with --source is not found here (use `rollup --full --yes` for that)."""
    if conn.execute(text(f"SELECT EXISTS (SELECT 1 FROM {_t(schema, PLACEMENT_TABLE)})")).scalar():
        return None
    sid = _placement_source_id(conn, schema)
    if sid is None:
        return None
    lo, hi = conn.execute(text(f"SELECT min(event_date), max(event_date) FROM {_t(schema, mschema.FACT_TABLE)} WHERE source_id = :s"), {"s": sid}).one()
    return (lo, hi) if lo is not None and hi is not None else None


def refresh_range(conn, schema: str, rng, cap: int = MAX_PLACEMENTS_PER_CAMPAIGN) -> tuple[int, int]:
    """Recompute one date range of the placement rollup in the caller's transaction (which arms the timeouts and does not commit here).
    `rng` = (start, end_exclusive or None). Returns (rows deleted, rows written)."""
    from erp.marketing import rollup as mroll
    where, params = mroll._bounds(rng, "f.event_date")
    dwhere, dparams = mroll._bounds(rng)
    fact, creative, table = _t(schema, mschema.FACT_TABLE), _t(schema, CREATIVE_TABLE), _t(schema, PLACEMENT_TABLE)
    conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"marketing_rollup:{schema}"})
    deleted = conn.execute(text(f"DELETE FROM {table} WHERE {dwhere}"), dparams).rowcount
    written = conn.execute(text(f"""
        INSERT INTO {table}
            (event_date, channel_id, campaign_id, placement_id, currency, placement_type, ad_size, position,
             fact_rows, impressions, clicks, conversions, spend_micros, revenue_micros, viewable_impressions,
             viewability_base_impressions, folded_placements, refreshed_at)
        WITH scope AS (
            SELECT f.event_date, f.channel_id, f.campaign_id, f.creative_id, f.currency, f.impressions, f.clicks, f.conversions,
                   f.spend_micros, f.revenue_micros,
                   CASE WHEN f.attrs->>'placement_type' IN ('website', 'app', 'video') THEN f.attrs->>'placement_type' ELSE 'other' END AS ptype,
                   CASE WHEN f.attrs->>'ad_size' ~ '^[0-9]{{1,4}}x[0-9]{{1,4}}$' THEN f.attrs->>'ad_size' ELSE '' END AS asize,
                   CASE WHEN f.attrs->>'position' IN ('above_fold', 'below_fold') THEN f.attrs->>'position' ELSE 'unknown' END AS pos,
                   CASE WHEN f.attrs->>'viewable_impressions' ~ '^[0-9]{{1,15}}$'
                        THEN LEAST(CAST(f.attrs->>'viewable_impressions' AS bigint), f.impressions) END AS viewable
              FROM {fact} f
             WHERE {where} AND f.attrs->>'placement_row' = 'true'
        ), kept AS (
            SELECT creative_id FROM (
                SELECT c.creative_id, row_number() OVER (PARTITION BY c.campaign_id ORDER BY c.creative_id) AS rn
                  FROM {creative} c
                 WHERE c.creative_key ~ '_[0-9a-f]{{8}}$' AND c.campaign_id IN (SELECT DISTINCT campaign_id FROM scope WHERE campaign_id IS NOT NULL)
            ) ranked WHERE rn <= :cap
        )
        SELECT s.event_date, s.channel_id, COALESCE(s.campaign_id, 0),
               CASE WHEN k.creative_id IS NULL THEN 0 ELSE s.creative_id END,
               s.currency, s.ptype, s.asize, s.pos,
               count(*), sum(s.impressions), sum(s.clicks), sum(s.conversions), sum(s.spend_micros), sum(s.revenue_micros),
               COALESCE(sum(s.viewable), 0),
               COALESCE(sum(s.impressions) FILTER (WHERE s.viewable IS NOT NULL), 0),
               count(DISTINCT s.creative_id) FILTER (WHERE k.creative_id IS NULL AND s.creative_id IS NOT NULL),
               now()
          FROM scope s LEFT JOIN kept k ON k.creative_id = s.creative_id
         GROUP BY s.event_date, s.channel_id, COALESCE(s.campaign_id, 0),
                  CASE WHEN k.creative_id IS NULL THEN 0 ELSE s.creative_id END, s.currency, s.ptype, s.asize, s.pos
    """), {**params, "cap": int(cap)}).rowcount
    return deleted, written


def folded_total(conn, schema: str, cap: int = MAX_PLACEMENTS_PER_CAMPAIGN) -> int:
    """How many distinct placements sit beyond the cap of their campaign right now: read from the small creative dimension for the campaigns that have
    placement rollup rows, never from the fact."""
    return int(conn.execute(text(f"""
        SELECT count(*) FROM (
            SELECT row_number() OVER (PARTITION BY c.campaign_id ORDER BY c.creative_id) AS rn
              FROM {_t(schema, CREATIVE_TABLE)} c
             WHERE c.creative_key ~ '_[0-9a-f]{{8}}$' AND c.campaign_id IN (SELECT DISTINCT campaign_id FROM {_t(schema, PLACEMENT_TABLE)} WHERE campaign_id <> 0)
        ) ranked WHERE rn > :cap"""), {"cap": int(cap)}).scalar() or 0)
