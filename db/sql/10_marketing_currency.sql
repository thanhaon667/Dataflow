-- =============================================================================
-- MARKETING / CHANNEL INTERACTION DATA - migration 10: a CURRENCY on every fact row
--
-- WHY. interaction_fact stores money as integer micros (1.00 = 1,000,000) but had no currency, so a table
-- holding one export in VND and another in EUR could only be summed into a meaningless number (1,000,000 VND
-- is about 35 EUR). This adds the missing column. Nothing is converted anywhere: a VND row stays VND, a EUR
-- row stays EUR, and every report groups or filters by currency instead of adding them (no exchange rates
-- exist in this project; see docs/marketing-data-architecture.md, "Currency").
--
-- ADDITIVE AND SAFE ON AN INSTALLED, POPULATED TABLE.
--   * ADD COLUMN IF NOT EXISTS: running it a second time does nothing (no error, no change).
--   * the default is a CONSTANT ('USD'), so PostgreSQL 11+ stores it in the catalog and does NOT rewrite any row
--     or partition; on the owner's database interaction_fact has 0 rows and no partitions anyway.
--   * on a partitioned table the new column (and its CHECK) is added to the parent and is inherited by every
--     partition that exists now or is created later (ensure_partitions creates them as PARTITION OF the parent).
--   * nothing is dropped, renamed or updated. Existing rows (if there were any) read as the default.
--
-- THE DEFAULT, 'USD'. Documented, deliberate and old: before this column existed the Channels page labelled
-- every money figure "USD". A connector that knows better (the ad_performance and email_campaign connectors
-- read VND / EUR from the file itself) writes its own code; the generic flat-file connector writes this default
-- unless it is given --currency. If you would rather have "unknown" than a guess, change the default in a
-- follow-up migration: the pipeline always writes the column explicitly, so the default only matters for rows
-- inserted by hand or by a tool that does not know about it.
--
-- RUN WITH (the owner runs it; nothing in this project runs it on the real database):
--   & "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U erp_app -h 127.0.0.1 -p 5432 -d erp_support -f db\sql\10_marketing_currency.sql
-- Order: 07 (already installed) -> 10 (this file) -> 09 (the rollup tables, which group by this column).
-- Like 07 it names the table WITHOUT a schema, so it can also be installed into a throwaway schema for testing
-- (SET search_path TO perf_x, public after 07 was installed there; see erp/marketing/sample_check.py).
-- =============================================================================

-- A partitioned table takes an ACCESS EXCLUSIVE lock for the ALTER: fail fast rather than queue every reader
-- behind a long-running query (the table is tiny or empty, so the lock is held for milliseconds).
SET lock_timeout = '15s';

ALTER TABLE interaction_fact
    ADD COLUMN IF NOT EXISTS currency CHAR(3) NOT NULL DEFAULT 'USD'
        CONSTRAINT chk_interaction_fact_currency CHECK (currency ~ '^[A-Z]{3}$');

COMMENT ON COLUMN interaction_fact.currency IS
    'ISO 4217 code (three capital letters) of spend_micros and revenue_micros on THIS row, exactly as the source '
    'reported it. Never converted. Default USD = the historical assumption of the Channels page; see 10_marketing_currency.sql.';
