-- =============================================================================
-- Add an SLA for leads: 5 business hours (8:00-17:00, Mon-Fri) counted from
-- when the lead was created. The actual calculation lives in
-- erp/business_hours.py (Python); this column just stores the computed
-- result for fast querying/reporting.
--
-- RUN WITH:
--   & "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U erp_app -h 127.0.0.1 -p 5432 -d erp_support -f db/sql/05_leads_sla.sql
-- =============================================================================

ALTER TABLE leads ADD COLUMN IF NOT EXISTS sla_due_at TIMESTAMPTZ;

CREATE OR REPLACE VIEW v_leads_summary AS
SELECT
    l.id AS lead_id,
    l.full_name,
    l.email,
    l.phone,
    l.company,
    l.source,
    l.created_at,
    rep.full_name   AS assigned_sales_rep,
    la.assignment_reason,
    la.assigned_at,
    ai.scale_estimate,
    ai.potential_score,
    ai.organization_type,
    ai.ai_notes,
    cu.clickup_task_id,
    cu.sync_status,
    cu.last_comment_pulled_at,
    l.sla_due_at,
    CASE
        WHEN l.sla_due_at IS NULL THEN FALSE
        ELSE now() > l.sla_due_at
    END AS sla_breached
FROM leads l
LEFT JOIN lead_assignments la ON la.lead_id = l.id AND la.is_current
LEFT JOIN users rep           ON rep.id = la.sales_rep_id
LEFT JOIN LATERAL (
    SELECT * FROM lead_ai_analysis a
    WHERE a.lead_id = l.id
    ORDER BY a.analyzed_at DESC
    LIMIT 1
) ai ON TRUE
LEFT JOIN lead_clickup_sync cu ON cu.lead_id = l.id;
