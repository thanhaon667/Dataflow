-- =============================================================================
-- Create a dedicated database + user for the ERP project (fully separate from
-- any other database on the same server, e.g. another application's database).
--
-- RUN AS SUPERUSER 'postgres', passing the password via the -v pw variable:
--
--   & "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U postgres -h 127.0.0.1 -p 5432 -v pw="'NEW_PASSWORD'" -f db/sql/00_create_database.sql
--
-- Replace NEW_PASSWORD with the password you want for the erp_app user, then
-- put that exact password into the .env file (DB_PASSWORD).
-- =============================================================================

CREATE DATABASE erp_support ENCODING 'UTF8' TEMPLATE template0;

CREATE USER erp_app WITH PASSWORD :pw;

GRANT ALL PRIVILEGES ON DATABASE erp_support TO erp_app;

\c erp_support

GRANT ALL ON SCHEMA public TO erp_app;
