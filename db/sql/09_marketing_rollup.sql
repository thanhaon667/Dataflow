-- =============================================================================
-- MARKETING / CHANNEL INTERACTION DATA - Phase 4: the daily ROLLUP layer
--
-- ADDITIVE ONLY. Creates two new tables and their indexes; touches nothing that exists
-- (needs db/sql/07_marketing_schema.sql first, and db/sql/10_marketing_currency.sql before the REFRESH JOB runs:
-- the job groups by interaction_fact.currency). Safe to run more than once (IF NOT EXISTS).
--
-- CURRENCY AND GRAIN ARE PART OF THE KEY. Money is never converted and never added across currencies, so both rollup
-- tables carry `currency` in their key and the refresh groups by it: a day x channel that has VND rows and EUR rows is
-- TWO rollup rows, and a report adds only rows of one currency (the counts - clicks, sessions ... - have no currency
-- and add freely). `grain` says what period a row's numbers cover: 'day' (the normal case) or 'week' (a weekly export
-- whose rows sit on the week's START date and carry the whole week). Reports must not draw a week as one day's
-- activity, so they keep the two grains apart. Both come from the fact: currency from its column, grain from
-- attrs->>'grain' ('week', anything else = 'day'). See docs/marketing-data-architecture.md ("Currency", "Grain").
--
-- WHY A REAL TABLE AND NOT A MATERIALIZED VIEW (the long version: docs/marketing-data-architecture.md s.8):
--   REFRESH MATERIALIZED VIEW re-runs the whole defining query over every partition of
--   interaction_fact, every time (3.36 s at 3M rows and growing linearly with history), and
--   even REFRESH ... CONCURRENTLY still recomputes everything and then diffs it. A plain table
--   refreshed by erp/marketing/rollup.py recomputes only the DAYS that are recent or whose facts
--   changed: DELETE that day range + INSERT ... SELECT ... GROUP BY inside one transaction, which
--   partition pruning confines to one or two monthly partitions. The unique key below is what
--   makes that refresh idempotent, and the table outlives a dropped fact partition.
--
-- RUN WITH (installs into the public schema of erp_support):
--   & "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U erp_app -h 127.0.0.1 -p 5432 -d erp_support -f db/sql/09_marketing_rollup.sql
-- Like 07 it names every object WITHOUT a schema, so erp/marketing/rollup_check.py can install it into a
-- throwaway schema (SET search_path TO perf_x, public) and remove that schema afterwards.
-- =============================================================================


-- ---------------------------------------------------------------------------
-- The rollup: one row per (day, channel, campaign).
--
-- Grain: event_date x channel_id x campaign_id. campaign_id is NOT NULL with 0 meaning "the fact
-- had no campaign" (COALESCE at refresh time): a nullable column cannot be part of a primary key,
-- and a UNIQUE index treats NULLs as distinct, which would break idempotency. There is no foreign
-- key on it for the same reason (0 is not a campaign) and because a per-row FK check on a table a
-- refresh rewrites daily protects nothing: the values come straight from the fact, which has the FK.
--
-- Only ADDITIVE measures are stored (counts and sums). Every ratio - CTR, CPC, CPM, conversion rate,
-- ROAS - is computed at READ time as sum(numerator) / nullif(sum(denominator), 0), so it stays correct
-- when the rows are re-aggregated to a week, a month or a channel. A stored ratio would be an average
-- of averages and wrong the moment two rows are combined.
--
-- The per-type event counts (events_click ...) count fact ROWS by their event_type; the measure columns
-- (clicks, impressions ...) sum the numbers carried on those rows. They differ on purpose: a daily export
-- row of event_type 'rollup' carries 40 clicks in one row.
-- events_other is every event_type that is not one of the five named ones, so
-- events_total = events_impression + events_click + events_reaction + events_session + events_conversion + events_other.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS interaction_daily_rollup (
    event_date          DATE     NOT NULL,
    channel_id          SMALLINT NOT NULL,
    campaign_id         BIGINT   NOT NULL DEFAULT 0,        -- 0 = the facts of that day had no campaign
    currency            CHAR(3)     NOT NULL DEFAULT 'USD',   -- currency of spend / revenue on these rows (interaction_fact.currency), never converted
    grain               VARCHAR(8)  NOT NULL DEFAULT 'day',   -- 'day' | 'week': the period the numbers cover (a week is dated by its START day)

    events_total        BIGINT   NOT NULL DEFAULT 0,        -- fact rows in the group
    events_impression   BIGINT   NOT NULL DEFAULT 0,        -- fact rows by event_type ...
    events_click        BIGINT   NOT NULL DEFAULT 0,
    events_reaction     BIGINT   NOT NULL DEFAULT 0,
    events_session      BIGINT   NOT NULL DEFAULT 0,
    events_conversion   BIGINT   NOT NULL DEFAULT 0,
    events_other        BIGINT   NOT NULL DEFAULT 0,

    impressions         BIGINT   NOT NULL DEFAULT 0,        -- sums of the fact measures
    clicks              BIGINT   NOT NULL DEFAULT 0,
    reactions           BIGINT   NOT NULL DEFAULT 0,
    sessions            BIGINT   NOT NULL DEFAULT 0,
    conversions         BIGINT   NOT NULL DEFAULT 0,
    spend_micros        BIGINT   NOT NULL DEFAULT 0,        -- 1 USD = 1000000, exactly like the fact
    revenue_micros      BIGINT   NOT NULL DEFAULT 0,
    revenue_derived_micros BIGINT NOT NULL DEFAULT 0,       -- the part of revenue_micros the connector DERIVED (attrs.revenue_derived = true), never reported by the source

    refreshed_at        TIMESTAMPTZ NOT NULL DEFAULT now(), -- when this row was last recomputed from the fact
    CONSTRAINT pk_interaction_daily_rollup PRIMARY KEY (event_date, channel_id, campaign_id, currency, grain),
    CONSTRAINT chk_interaction_daily_rollup_currency CHECK (currency ~ '^[A-Z]{3}$'),
    CONSTRAINT chk_interaction_daily_rollup_grain CHECK (grain IN ('day', 'week'))
);

-- The other half of the report filter: one channel (or a few) over a date range. The primary key already
-- serves "every channel, this date range" (leading column event_date); this serves "channel X, this range"
-- without walking every channel's rows first - the same (channel, date) order the fact's own index uses.
CREATE INDEX IF NOT EXISTS idx_interaction_daily_rollup_channel_date
    ON interaction_daily_rollup (channel_id, event_date);


-- ---------------------------------------------------------------------------
-- The same rollup one level up: one row per (day, channel), summed from the campaign-level rows above
-- by the same refresh, in the same transaction, for the same date range - so the two can never disagree.
--
-- Why a second table: the campaign grain only compresses the fact by the average number of interactions per
-- (day, channel, campaign). A channel report that reads "all history, per channel" (a headline tile, a trend
-- chart) should read days x channels rows (about 4,000 for a year of 12 channels), not days x channels x
-- campaigns (hundreds of thousands). Additive measures make this exact: the channel row is the sum of its
-- campaign rows (of the same currency and grain: those two stay in the key, so the sum never crosses them).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS interaction_daily_channel_rollup (
    event_date          DATE     NOT NULL,
    channel_id          SMALLINT NOT NULL,
    currency            CHAR(3)     NOT NULL DEFAULT 'USD',
    grain               VARCHAR(8)  NOT NULL DEFAULT 'day',

    events_total        BIGINT   NOT NULL DEFAULT 0,
    events_impression   BIGINT   NOT NULL DEFAULT 0,
    events_click        BIGINT   NOT NULL DEFAULT 0,
    events_reaction     BIGINT   NOT NULL DEFAULT 0,
    events_session      BIGINT   NOT NULL DEFAULT 0,
    events_conversion   BIGINT   NOT NULL DEFAULT 0,
    events_other        BIGINT   NOT NULL DEFAULT 0,

    impressions         BIGINT   NOT NULL DEFAULT 0,
    clicks              BIGINT   NOT NULL DEFAULT 0,
    reactions           BIGINT   NOT NULL DEFAULT 0,
    sessions            BIGINT   NOT NULL DEFAULT 0,
    conversions         BIGINT   NOT NULL DEFAULT 0,
    spend_micros        BIGINT   NOT NULL DEFAULT 0,
    revenue_micros      BIGINT   NOT NULL DEFAULT 0,
    revenue_derived_micros BIGINT NOT NULL DEFAULT 0,

    refreshed_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT pk_interaction_daily_channel_rollup PRIMARY KEY (event_date, channel_id, currency, grain),
    CONSTRAINT chk_interaction_daily_channel_rollup_currency CHECK (currency ~ '^[A-Z]{3}$'),
    CONSTRAINT chk_interaction_daily_channel_rollup_grain CHECK (grain IN ('day', 'week'))
);

CREATE INDEX IF NOT EXISTS idx_interaction_daily_channel_rollup_channel_date
    ON interaction_daily_channel_rollup (channel_id, event_date);


-- ---------------------------------------------------------------------------
-- The refresh journal: one row per run of `python -m erp.marketing.rollup`, so "did it run, what did it
-- touch" is a 5-row query - and so the next run knows which days changed since the last good one
-- (started_at of the newest 'ok' window/full row is the watermark compared with interaction_fact.loaded_at).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS marketing_rollup_run (
    run_id           BIGSERIAL PRIMARY KEY,
    started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at      TIMESTAMPTZ,
    mode             VARCHAR(20) NOT NULL,                  -- window | window_only | full (window_only never moves the watermark)
    days_requested   INTEGER,                               -- the --days value (window mode)
    ranges           TEXT,                                  -- the date ranges that were recomputed, human readable
    days_recomputed  INTEGER NOT NULL DEFAULT 0,            -- calendar days inside those ranges (touched-day ranges included)
    rows_deleted     BIGINT  NOT NULL DEFAULT 0,
    rows_written     BIGINT  NOT NULL DEFAULT 0,
    status           VARCHAR(20) NOT NULL DEFAULT 'running',-- running | ok | failed
    error_message    TEXT
);

CREATE INDEX IF NOT EXISTS idx_marketing_rollup_run_started ON marketing_rollup_run (started_at DESC);
