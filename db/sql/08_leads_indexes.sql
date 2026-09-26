-- =============================================================================
-- Two missing indexes on the EXISTING `leads` table.
--
-- ADDITIVE ONLY: `CREATE INDEX` adds an access path. It changes no row, no column,
-- no constraint and no query RESULT - only the plan the planner may choose.
--
-- Why: every Leads / Sources & cohorts / Today query filters `leads` by a date range
-- and/or by `source`, and `leads` had an index on `email` and `phone` only:
--
--   desktop/today_data.py    "new leads today"       WHERE created_at >= date_trunc('day', now())
--   desktop/leads_data.py    the filter bar          WHERE created_at >= :from AND created_at < :to [AND source = ANY(:sources)]
--   desktop/sources_data.py  cohorts + source table  GROUP BY source, date_trunc(...) over the same range
--
-- At the current size of this database (a handful of leads) PostgreSQL will still pick a
-- sequential scan, and it is right to: reading one page beats reading an index plus a page.
-- These indexes are for the shape the table is growing into; proven on a 1,000,000-row copy
-- in a throwaway schema (see erp/marketing/perf_check.py and
-- docs/marketing-data-architecture.md for the measured plans).
--
-- RUN WITH:
--   $env:PGPASSWORD="<ERP_APP_PASSWORD>"
--   & "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U erp_app -h 127.0.0.1 -p 5432 -d erp_support -f db/sql/08_leads_indexes.sql
--
-- On a live, busy table use CREATE INDEX CONCURRENTLY instead (it cannot run inside a
-- transaction block, so it is not written that way here).
-- =============================================================================

-- Date-range filters, "leads today", "leads per day", the arrival cohorts.
-- DESC because the pages that do not filter at all still want the newest first.
CREATE INDEX IF NOT EXISTS idx_leads_created_at ON leads (created_at DESC);

-- The SOURCE filter pills and the per-source tables. Source first, then the date, so one
-- index serves both "everything from this source" and "this source in this range".
CREATE INDEX IF NOT EXISTS idx_leads_source_created_at ON leads (source, created_at DESC);

ANALYZE leads;
