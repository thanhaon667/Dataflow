-- =============================================================================
-- MARKETING / CHANNEL INTERACTION DATA - Phase 1 schema (star shape, partitioned fact)
--
-- ADDITIVE ONLY. This script creates new objects and touches nothing that already
-- exists in erp_support: no existing table, column, view, constraint or index is
-- altered or dropped. It is safe to run more than once (everything is IF NOT EXISTS).
--
-- WHY THIS SHAPE (the long version is docs/marketing-data-architecture.md):
--   * a raw LANDING layer keeps every record close to the shape it arrived in, so a
--     mapping bug is repairable by re-reading the landing rows (same idea as leads.raw_payload);
--   * a few small DIMENSION tables hold the things that repeat (source system, channel,
--     campaign, creative, identity), so the fact stays narrow and a rename is one UPDATE;
--   * one narrow FACT row per interaction with the handful of measures every channel
--     shares, plus a JSONB `attrs` for the source-specific extras that do not deserve
--     a column (Facebook alone can hand you 100+ fields, and they differ per source);
--   * `interaction_fact` is RANGE-partitioned by month on event_date, because every
--     report in this project filters by a date range first.
--
-- RUN WITH (installs into the public schema of erp_support):
--   $env:PGPASSWORD="<ERP_APP_PASSWORD>"
--   & "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U erp_app -h 127.0.0.1 -p 5432 -d erp_support -f db/sql/07_marketing_schema.sql
--
-- Every object below is named without a schema on purpose, so the same file can also be
-- installed into a throwaway schema for testing:
--   psql ... -c "CREATE SCHEMA perf_test" -c "SET search_path TO perf_test, public" -f db/sql/07_marketing_schema.sql
-- (that is exactly what erp/marketing/perf_check.py does, and it drops the schema again).
--
-- Requires PostgreSQL 11+ for declarative partitioning with a partitioned primary key;
-- developed and verified on PostgreSQL 18.3.
-- =============================================================================


-- ---------------------------------------------------------------------------
-- PART 1 - LANDING: append-only, one row per record as it arrived
--
-- Nothing here is interpreted. The connector's own record goes into `payload`
-- unchanged, next to the handful of universal columns every source can supply.
-- If a mapping turns out to be wrong, the fact rows of a batch can be deleted and
-- rebuilt from these rows without going back to the external API.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS marketing_landing (
    landing_id   BIGSERIAL PRIMARY KEY,
    batch_id     UUID        NOT NULL,              -- the ingest run that brought this row in
    batch_seq    INTEGER     NOT NULL,              -- position inside the batch (used to map a fact row back here)
    source       VARCHAR(60) NOT NULL,              -- connector key: 'flat_file', later 'facebook_ads', ...
    external_id  VARCHAR(200),                      -- the source's own id for this record, when it has one
    event_type   VARCHAR(80),                       -- as the SOURCE called it, before any mapping
    payload      JSONB       NOT NULL,              -- the record exactly as received
    received_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- BRIN, not btree: received_at only ever grows, so consecutive heap pages hold
-- consecutive timestamps. A BRIN index over that is a few dozen kB instead of tens of
-- MB and is all "give me what landed between X and Y" / retention deletes ever need.
CREATE INDEX IF NOT EXISTS idx_marketing_landing_received_brin
    ON marketing_landing USING brin (received_at) WITH (pages_per_range = 32);

-- "show me everything one ingest run brought in" (the operational question after a bad batch)
CREATE INDEX IF NOT EXISTS idx_marketing_landing_batch ON marketing_landing (batch_id, batch_seq);

-- "have we already seen this record from this source?" - the cheap reconciliation path
CREATE INDEX IF NOT EXISTS idx_marketing_landing_source_ext
    ON marketing_landing (source, external_id) WHERE external_id IS NOT NULL;


-- ---------------------------------------------------------------------------
-- PART 2 - INGEST JOURNAL: one row per batch, so "did it run, and what happened"
-- is a 5-row query instead of a count over millions of rows.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS marketing_ingest_run (
    run_id          BIGSERIAL PRIMARY KEY,
    batch_id        UUID        NOT NULL UNIQUE,
    source          VARCHAR(60) NOT NULL,
    connector       VARCHAR(120),                   -- which connector class produced the common shape
    origin          TEXT,                           -- file name / API window - never a credential
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    rows_read       BIGINT NOT NULL DEFAULT 0,
    rows_landed     BIGINT NOT NULL DEFAULT 0,
    rows_invalid    BIGINT NOT NULL DEFAULT 0,      -- malformed rows: counted and skipped, never fatal (lesson L-044)
    rows_duplicate  BIGINT NOT NULL DEFAULT 0,      -- same interaction seen twice (inside the batch or already in the fact)
    rows_loaded     BIGINT NOT NULL DEFAULT 0,      -- new fact rows
    status          VARCHAR(20) NOT NULL DEFAULT 'running',   -- running | ok | partial | failed
    error_message   TEXT
);

CREATE INDEX IF NOT EXISTS idx_marketing_ingest_run_started ON marketing_ingest_run (started_at DESC);


-- ---------------------------------------------------------------------------
-- PART 3 - DIMENSIONS (small, slowly changing)
--
-- These are SCD type 1 (the row is updated in place) plus first_seen_at / last_seen_at,
-- which is what this data actually needs today: a campaign's display name changing does
-- not invalidate yesterday's clicks. Keeping history of a dimension row (SCD type 2) is a
-- Phase 4 decision and is described in docs/marketing-data-architecture.md; it would add
-- valid_from / valid_to + a surrogate per version, and the fact would point at the version.
-- ---------------------------------------------------------------------------

-- WHERE the data came from (the system, not the marketing channel)
CREATE TABLE IF NOT EXISTS marketing_source (
    source_id     SMALLSERIAL PRIMARY KEY,
    source_key    VARCHAR(60) NOT NULL UNIQUE,      -- 'flat_file', 'facebook_ads', 'ga4', ...
    display_name  VARCHAR(120),
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- WHICH marketing channel the interaction belongs to
CREATE TABLE IF NOT EXISTS marketing_channel (
    channel_id    SMALLSERIAL PRIMARY KEY,
    channel_key   VARCHAR(80) NOT NULL UNIQUE,      -- normalised: lower case, spaces -> '_' ('facebook', 'google_ads', 'organic')
    display_name  VARCHAR(120),
    medium        VARCHAR(40),                      -- paid_social | search | email | organic | referral | unknown
    is_paid       BOOLEAN,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    attrs         JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS marketing_campaign (
    campaign_id   BIGSERIAL PRIMARY KEY,
    channel_id    SMALLINT NOT NULL REFERENCES marketing_channel(channel_id),
    campaign_key  VARCHAR(200) NOT NULL,            -- the source's campaign id, else a slug of its name
    name          VARCHAR(300),
    objective     VARCHAR(80),
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    attrs         JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT uq_marketing_campaign UNIQUE (channel_id, campaign_key)
);

CREATE TABLE IF NOT EXISTS marketing_creative (
    creative_id   BIGSERIAL PRIMARY KEY,
    campaign_id   BIGINT NOT NULL REFERENCES marketing_campaign(campaign_id),
    creative_key  VARCHAR(200) NOT NULL,            -- the source's ad/creative id, else a slug of its name
    name          VARCHAR(300),
    format        VARCHAR(40),                      -- image | video | carousel | text | unknown
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    attrs         JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT uq_marketing_creative UNIQUE (campaign_id, creative_key)
);

-- WHO the interaction belongs to, when the source says anything about that.
--
-- This is the ONLY place that knows about erp_support's existing identities: it REFERENCES
-- leads(id) instead of copying a name, an e-mail or a phone number. `identity_value` for a
-- personal identifier (e-mail, phone) is a sha256 hex digest written by the pipeline, never
-- the value itself, so this table can not become a second copy of the customer list; for a
-- non-personal identifier (a click id, a source-side user id, a session id) it is the value.
CREATE TABLE IF NOT EXISTS marketing_identity (
    identity_id    BIGSERIAL PRIMARY KEY,
    identity_type  VARCHAR(30)  NOT NULL,           -- email_sha256 | phone_sha256 | click_id | external_user_id | session_id
    identity_value VARCHAR(200) NOT NULL,
    lead_id        INTEGER REFERENCES leads(id) ON DELETE SET NULL,   -- filled in when the identity matches a known lead
    resolved_at    TIMESTAMPTZ,
    first_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_marketing_identity UNIQUE (identity_type, identity_value)
);

CREATE INDEX IF NOT EXISTS idx_marketing_identity_lead
    ON marketing_identity (lead_id) WHERE lead_id IS NOT NULL;


-- ---------------------------------------------------------------------------
-- PART 4 - THE FACT: one row per interaction, monthly partitions
--
-- Columns are deliberately few. A measure gets a column only when EVERY channel can
-- mean something by it (an impression, a click, a reaction, a session, a conversion, money
-- in, money back). Everything else a source hands us goes to `attrs`.
--
-- No surrogate id: the natural key (event_date, dedupe_key) IS the identity of an
-- interaction, and it has to be the primary key anyway because a partitioned table's
-- primary key must contain the partition column. A BIGSERIAL on top would cost 8 bytes a
-- row plus sequence traffic on every bulk load, and nothing references the fact.
--
-- Money is stored in MICROS (integer), never float: 1 USD = 1000000. There is NO currency column in this file:
-- db/sql/10_marketing_currency.sql adds `currency` (ISO 4217, NOT NULL, default 'USD') to the fact. Nothing is ever
-- CONVERTED - a VND row stays VND, a EUR row stays EUR - and reports group by currency instead of adding it up.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS interaction_fact (
    event_date     DATE        NOT NULL,            -- PARTITION KEY: the day the interaction happened, in the source's own day
    dedupe_key     UUID        NOT NULL,            -- blake2b-128 of the record's identity (see erp/marketing/model.py)
    event_at       TIMESTAMPTZ,                     -- the exact moment, when the source knows it (daily exports do not)
    source_id      SMALLINT    NOT NULL REFERENCES marketing_source(source_id),
    channel_id     SMALLINT    NOT NULL REFERENCES marketing_channel(channel_id),
    campaign_id    BIGINT      REFERENCES marketing_campaign(campaign_id),
    creative_id    BIGINT      REFERENCES marketing_creative(creative_id),
    identity_id    BIGINT      REFERENCES marketing_identity(identity_id),
    event_type     VARCHAR(40) NOT NULL,            -- impression | click | reaction | session | conversion | other
    impressions    BIGINT      NOT NULL DEFAULT 0,
    clicks         BIGINT      NOT NULL DEFAULT 0,
    reactions      BIGINT      NOT NULL DEFAULT 0,
    sessions       BIGINT      NOT NULL DEFAULT 0,
    conversions    BIGINT      NOT NULL DEFAULT 0,
    spend_micros   BIGINT      NOT NULL DEFAULT 0,
    revenue_micros BIGINT      NOT NULL DEFAULT 0,
    landing_id     BIGINT,                          -- which raw row this came from. Deliberately NOT a foreign key:
                                                    -- a per-row FK check on a multi-million-row bulk load costs more
                                                    -- than it protects, and landing is append-only so it cannot dangle.
    attrs          JSONB       NOT NULL DEFAULT '{}'::jsonb,   -- everything source-specific, in full
    attrs_idx      JSONB       NOT NULL DEFAULT '{}'::jsonb,   -- ONLY the indexed keys (see below)
    loaded_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT pk_interaction_fact PRIMARY KEY (event_date, dedupe_key)
) PARTITION BY RANGE (event_date);

-- Monthly partitions are created by the pipeline before a batch is loaded
-- (erp/marketing/schema.py ensure_partitions) and look like:
--
--   CREATE TABLE IF NOT EXISTS interaction_fact_2026_09
--       PARTITION OF interaction_fact FOR VALUES FROM ('2026-09-01') TO ('2026-10-01');
--
-- There is deliberately NO DEFAULT partition: a default partition has to be scanned by any
-- query whose range the planner cannot prove it excludes, which is exactly the partition
-- pruning this design exists for. A row with no partition raises an error instead of
-- silently landing somewhere that makes every report slower, and the pipeline creates the
-- months of a batch up front so a real batch never hits that error mid-load.

-- ---- indexes on the fact (they propagate to every partition automatically) ----

-- The filter every report page in this project starts with: a channel (or several) inside a
-- date range. Channel first, date second: the channel is an equality and the date a range.
CREATE INDEX IF NOT EXISTS idx_interaction_fact_channel_date
    ON interaction_fact (channel_id, event_date);

-- The same question asked per source system ("what did the Facebook connector bring in?").
CREATE INDEX IF NOT EXISTS idx_interaction_fact_source_date
    ON interaction_fact (source_id, event_date);

-- "which interactions belong to this lead" - via the identity dimension. Partial, because
-- most interactions are anonymous and NULLs would only make the index bigger.
CREATE INDEX IF NOT EXISTS idx_interaction_fact_identity
    ON interaction_fact (identity_id) WHERE identity_id IS NOT NULL;

-- BRIN on loaded_at: rows are appended in load order, so this answers "what did the last
-- ingest add / what arrived since yesterday" for a few dozen kB per partition.
CREATE INDEX IF NOT EXISTS idx_interaction_fact_loaded_brin
    ON interaction_fact USING brin (loaded_at) WITH (pages_per_range = 32);

-- The SCOPED GIN index. It is on `attrs_idx`, NOT on `attrs`.
--
-- `attrs` is the full source-specific blob and may hold 100+ keys per row; GIN-indexing all
-- of it would index every key of every row (large, slow to write, and almost all of it never
-- queried). `attrs_idx` is written by the pipeline and contains ONLY the keys on the
-- documented allowlist INDEXED_ATTR_KEYS in erp/marketing/model.py:
--
--     utm_source, utm_medium, utm_campaign, placement, device, country, brand, segment
--
-- Why those eight (the last two, brand and segment, were added with the e-mail connector: an e-mail export is
-- filtered by exactly them, and the fact table held no rows yet, so no backfill was needed and this index, which
-- covers the whole attrs_idx column, did not change): they are the cross-source "slice by" attributes a channel-performance
-- report filters on, they exist (under some spelling) in nearly every ad/analytics export,
-- and none of them deserves its own column yet because they are sparse and their meaning is
-- still source-specific. Anything else in `attrs` is readable but not indexed - which is the
-- honest trade: a filter on an unlisted key is a scan of the date range, not of the table.
--
-- jsonb_path_ops (not the default jsonb_ops): about half the size, and it supports exactly
-- the containment query this is for - attrs_idx @> '{"utm_source": "facebook"}'.
--
-- To add a key later: add it to INDEXED_ATTR_KEYS, backfill attrs_idx for the partitions you
-- care about, and say so here. The index itself does not change.
CREATE INDEX IF NOT EXISTS idx_interaction_fact_attrs_gin
    ON interaction_fact USING gin (attrs_idx jsonb_path_ops);


-- ---------------------------------------------------------------------------
-- What is deliberately NOT here
--   * no report view / materialised rollup yet - Phase 4 (docs/marketing-data-architecture.md)
--   * no campaign_id index - campaign filters always arrive together with channel + date,
--     which idx_interaction_fact_channel_date already serves. Add it when a real workload asks.
--   * no partition of marketing_landing - it is append-only and only ever read by batch_id or
--     by a BRIN range. Partition it (or add a retention drop) when it outgrows the disk.
-- ---------------------------------------------------------------------------
