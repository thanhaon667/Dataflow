-- =============================================================================
-- Sample data: Sales department + sales reps, for testing round-robin lead assignment
--
-- RUN WITH:
--   & "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U erp_app -h 127.0.0.1 -p 5432 -d erp_support -f db/sql/04_seed_sales.sql
-- =============================================================================

INSERT INTO departments (code, name) VALUES
    ('SALES', 'Sales')
ON CONFLICT (code) DO NOTHING;

-- Fill in the real clickup_user_id here once you have it (left blank -> the
-- task is still created, just without a ClickUp assignee).
INSERT INTO users (full_name, email, department_id, role, clickup_user_id)
SELECT u.full_name, u.email, d.id, 'sales', u.clickup_user_id
FROM departments d
JOIN (VALUES
    ('Do Thi Lan',    'lan.do@company.vn',    NULL::VARCHAR),
    ('Vu Minh Tuan',  'tuan.vu@company.vn',   NULL::VARCHAR),
    ('Bui Thi Nga',   'nga.bui@company.vn',   NULL::VARCHAR)
) AS u(full_name, email, clickup_user_id) ON TRUE
WHERE d.code = 'SALES'
ON CONFLICT (email) DO NOTHING;
