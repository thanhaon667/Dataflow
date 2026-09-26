-- =============================================================================
-- MARKETING - PLACEMENT ROLLUP (slice 3: where did the display ads run, and what did each place cost and produce?)
--
-- ADDITIVE AND IDEMPOTENT. This script creates ONE new table and its index and touches nothing that already exists: no
-- column, view, constraint or index of the star schema (07), the rollup (09) or the currency migration (10) is altered.
-- Safe to run more than once. It is optional: without it the marketing pages work exactly as before and the Placements
-- panel says "not installed" with this command.
--
-- WHY A ROLLUP AND NOT THE RAW FACT (measured, see docs/marketing-data-architecture.md s.11): a placement report has one row
-- per day x campaign x ad group x placement x size x position x device, so it is the widest grain in the system. A page that
-- scanned interaction_fact for it would cost as much as the raw table is big; this table holds the same additive measures
-- pre-summed at day x channel x campaign x placement x type x size x position x currency, refreshed by the SAME rollup job
-- (erp/marketing/rollup.py -> erp/marketing/rollup_placements.py) with the same incremental loaded_at watermark.
--
-- NO NEW DIMENSION TABLE. The placement (a site, an app id or a banner slot) is stored as a row of the existing
-- marketing_creative table (one creative per campaign x placement, key = readable slug + 8 hex digits of a hash so that two
-- placements that differ only in punctuation stay two rows). `placement_id` below is that creative_id. The other placement
-- attributes (type, ad size, position) stay in interaction_fact.attrs and are copied into this table's own columns.
--
-- CARDINALITY GUARD: a campaign can run on an unbounded number of sites and apps. The refresh keeps the first
-- MAX_PLACEMENTS_PER_CAMPAIGN placements of each campaign (by creative_id, i.e. by first appearance: a rule that does not
-- change when a later day is loaded) as their own rows and folds every further one into placement_id = 0 ("other placements").
-- Nothing is lost: the sums stay exact; only the naming stops. `folded_placements` counts the distinct placements folded into
-- that row on that day, so the page can say how many.
--
-- RUN WITH (installs into the public schema of erp_support; the owner runs it, the app role cannot be assumed to):
--   $env:PGPASSWORD="<ERP_APP_PASSWORD>"
--   & "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U erp_app -h 127.0.0.1 -p 5432 -d erp_support -f db/sql/11_marketing_placements.sql
-- (objects are unqualified on purpose: the same file installs into a throwaway schema for testing via search_path)
--
-- Requires 07 and 09 (and 10) to be installed first.
-- =============================================================================

CREATE TABLE IF NOT EXISTS interaction_placement_rollup (
    event_date          DATE        NOT NULL,
    channel_id          SMALLINT    NOT NULL,
    campaign_id         BIGINT      NOT NULL DEFAULT 0,          -- 0 = the facts had no campaign
    placement_id        BIGINT      NOT NULL DEFAULT 0,          -- marketing_creative.creative_id of the placement; 0 = "other placements" (folded by the cap)
    currency            CHAR(3)     NOT NULL DEFAULT 'USD',      -- currency of spend / revenue on these rows, never converted
    placement_type      VARCHAR(12) NOT NULL DEFAULT 'other',    -- website | app | video | other
    ad_size             VARCHAR(12) NOT NULL DEFAULT '',         -- '300x250'; '' = unknown
    position            VARCHAR(12) NOT NULL DEFAULT 'unknown',  -- above_fold | below_fold | unknown

    fact_rows           BIGINT      NOT NULL DEFAULT 0,          -- fact rows in the group
    impressions         BIGINT      NOT NULL DEFAULT 0,
    clicks              BIGINT      NOT NULL DEFAULT 0,
    conversions         BIGINT      NOT NULL DEFAULT 0,
    spend_micros        BIGINT      NOT NULL DEFAULT 0,
    revenue_micros      BIGINT      NOT NULL DEFAULT 0,
    viewable_impressions        BIGINT NOT NULL DEFAULT 0,       -- sum over the rows that REPORTED it (blank = unknown, not 0)
    viewability_base_impressions BIGINT NOT NULL DEFAULT 0,      -- impressions of exactly those rows: viewable / base is the viewability rate
    folded_placements   INTEGER     NOT NULL DEFAULT 0,          -- placement_id = 0 only: distinct placements folded into this row that day

    refreshed_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT pk_interaction_placement_rollup
        PRIMARY KEY (event_date, channel_id, campaign_id, placement_id, currency, placement_type, ad_size, position),
    CONSTRAINT chk_interaction_placement_rollup_currency CHECK (currency ~ '^[A-Z]{3}$'),
    CONSTRAINT chk_interaction_placement_rollup_type CHECK (placement_type IN ('website', 'app', 'video', 'other')),
    CONSTRAINT chk_interaction_placement_rollup_position CHECK (position IN ('above_fold', 'below_fold', 'unknown'))
);

-- the page filters by date first, then channel / campaign / currency
CREATE INDEX IF NOT EXISTS idx_interaction_placement_rollup_channel_date
    ON interaction_placement_rollup (channel_id, event_date);

COMMENT ON TABLE interaction_placement_rollup IS
    'Day x channel x campaign x placement x type x size x position x currency sums of the placement_performance facts (attrs.placement_row = true). '
    'Refreshed by erp/marketing/rollup.py with the other rollups; placement_id = 0 is the cardinality guard''s "other placements" bucket.';
