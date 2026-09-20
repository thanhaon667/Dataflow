-- =============================================================================
-- Create a dedicated READ-ONLY role for Power BI. It can only SELECT from the
-- summary views (v_tickets_summary, v_leads_summary) - no access to raw
-- tables, and no write access anywhere.
--
-- RUN WITH:
--   & "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U erp_app -h 127.0.0.1 -p 5432 -d erp_support -v pw="'NEW_PASSWORD'" -f db/sql/06_powerbi_readonly.sql
-- =============================================================================

-- Create the role only if it doesn't exist yet (idempotent via \gexec)
SELECT format('CREATE ROLE powerbi_reader LOGIN PASSWORD %L', :'pw')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'powerbi_reader')
\gexec

-- Always (re)apply the password, whether the role is new or already existed
ALTER ROLE powerbi_reader WITH PASSWORD :'pw';

GRANT CONNECT ON DATABASE erp_support TO powerbi_reader;
GRANT USAGE ON SCHEMA public TO powerbi_reader;

-- Only the summary views, never the raw tables
GRANT SELECT ON v_tickets_summary, v_leads_summary TO powerbi_reader;

-- Any future view/table created by erp_app is auto-granted SELECT too,
-- so new summary views don't require a manual GRANT later.
ALTER DEFAULT PRIVILEGES FOR ROLE erp_app IN SCHEMA public
    GRANT SELECT ON TABLES TO powerbi_reader;
