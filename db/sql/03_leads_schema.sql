-- =============================================================================
-- EXTENDED SCHEMA: Lead-to-Sale (Customer Support receives lead -> assigns
-- Sales rep -> AI analysis -> create ClickUp task -> pull updates back to DB
-- -> periodic AI report)
--
-- RUN WITH:
--   & "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U erp_app -h 127.0.0.1 -p 5432 -d erp_support -f db/sql/03_leads_schema.sql
-- =============================================================================

-- Allow assigning a ClickUp user id to a staff member (so tasks are assigned to the right person)
ALTER TABLE users ADD COLUMN IF NOT EXISTS clickup_user_id VARCHAR(30);

-- ---------------------------------------------------------------------------
-- Leads: raw data received from the lead form via the webhook
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS leads (
    id           SERIAL PRIMARY KEY,
    full_name    VARCHAR(150) NOT NULL,
    email        VARCHAR(150),
    phone        VARCHAR(30),
    company      VARCHAR(150),
    source       VARCHAR(50),          -- lead form / campaign name
    raw_payload  JSONB,                -- keep the raw payload as received, for later reconciliation
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_lead_contact CHECK (email IS NOT NULL OR phone IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_leads_email ON leads(email);
CREATE INDEX IF NOT EXISTS idx_leads_phone ON leads(phone);

-- ---------------------------------------------------------------------------
-- Lead assignment to a Sales rep (history; is_current marks who owns it now)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS lead_assignments (
    id                 SERIAL PRIMARY KEY,
    lead_id            INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    sales_rep_id       INTEGER NOT NULL REFERENCES users(id),
    assignment_reason  VARCHAR(30) NOT NULL,   -- 'existing_duplicate' or 'round_robin_new'
    is_current         BOOLEAN NOT NULL DEFAULT TRUE,
    assigned_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_lead_assignments_lead ON lead_assignments(lead_id);
CREATE INDEX IF NOT EXISTS idx_lead_assignments_rep_current
    ON lead_assignments(sales_rep_id) WHERE is_current;

-- ---------------------------------------------------------------------------
-- AI (DeepSeek) analysis/classification result before handing off to Sales
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS lead_ai_analysis (
    id                 SERIAL PRIMARY KEY,
    lead_id            INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    model_name         VARCHAR(50) NOT NULL,
    scale_estimate     VARCHAR(50),     -- scale: solo/small/medium/enterprise...
    potential_score    INTEGER,         -- 0-100
    organization_type  VARCHAR(100),    -- inferred organization type
    ai_notes           TEXT,            -- note for the Sales rep
    raw_response       JSONB,
    analyzed_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_lead_ai_analysis_lead ON lead_ai_analysis(lead_id);

-- ---------------------------------------------------------------------------
-- ClickUp task sync for each lead
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS lead_clickup_sync (
    id                SERIAL PRIMARY KEY,
    lead_id           INTEGER NOT NULL UNIQUE REFERENCES leads(id) ON DELETE CASCADE,
    clickup_task_id   VARCHAR(50),
    sync_status       VARCHAR(20) NOT NULL DEFAULT 'pending',  -- pending, synced, failed, mocked
    last_synced_at    TIMESTAMPTZ,
    last_comment_pulled_at TIMESTAMPTZ,   -- timestamp of the last comment pulled from ClickUp
    error_message     TEXT
);

-- ---------------------------------------------------------------------------
-- Sales rep notes entered on ClickUp, pulled back to the DB periodically
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS lead_updates (
    id               SERIAL PRIMARY KEY,
    lead_id          INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    source           VARCHAR(30) NOT NULL DEFAULT 'clickup_comment',
    content          TEXT NOT NULL,
    author_name      VARCHAR(150),
    occurred_at      TIMESTAMPTZ,        -- when the comment was created on ClickUp
    synced_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    raw_payload      JSONB
);

CREATE INDEX IF NOT EXISTS idx_lead_updates_lead ON lead_updates(lead_id);

-- ---------------------------------------------------------------------------
-- Summary view for the dashboard / weekly report / Power BI
-- ---------------------------------------------------------------------------
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
    cu.last_comment_pulled_at
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
