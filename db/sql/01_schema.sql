-- =============================================================================
-- ERP SCHEMA: IT Support + Customer Support (Order ticket) share 1 tickets model
--
-- RUN WITH:
--   & "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U erp_app -h 127.0.0.1 -p 5432 -d erp_support -f db/sql/01_schema.sql
-- =============================================================================

-- ---------------------------------------------------------------------------
-- Shared tables
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS departments (
    id   SERIAL PRIMARY KEY,
    code VARCHAR(20) UNIQUE NOT NULL,      -- 'IT', 'CSKH'
    name VARCHAR(100) NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id             SERIAL PRIMARY KEY,
    full_name      VARCHAR(150) NOT NULL,
    email          VARCHAR(150) UNIQUE NOT NULL,
    department_id  INTEGER REFERENCES departments(id),
    role           VARCHAR(50) NOT NULL DEFAULT 'agent',  -- agent, team lead, manager
    is_active      BOOLEAN NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS customers (
    id         SERIAL PRIMARY KEY,
    full_name  VARCHAR(150) NOT NULL,
    email      VARCHAR(150),
    phone      VARCHAR(30),
    company    VARCHAR(150),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ticket_categories (
    id            SERIAL PRIMARY KEY,
    department_id INTEGER NOT NULL REFERENCES departments(id),
    name          VARCHAR(100) NOT NULL,
    UNIQUE (department_id, name)
);

CREATE TABLE IF NOT EXISTS sla_policies (
    id                       SERIAL PRIMARY KEY,
    department_id            INTEGER NOT NULL REFERENCES departments(id),
    priority                 VARCHAR(20) NOT NULL,   -- low, medium, high, urgent
    response_time_minutes    INTEGER NOT NULL,
    resolution_time_minutes  INTEGER NOT NULL,
    UNIQUE (department_id, priority)
);

-- ---------------------------------------------------------------------------
-- Customer Support only: orders linked to a ticket
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS orders (
    id           SERIAL PRIMARY KEY,
    order_code   VARCHAR(30) UNIQUE NOT NULL,
    customer_id  INTEGER NOT NULL REFERENCES customers(id),
    amount       NUMERIC(14,2) NOT NULL,
    currency     VARCHAR(10) NOT NULL DEFAULT 'VND',
    order_status VARCHAR(30) NOT NULL DEFAULT 'pending',  -- pending, paid, shipped, delivered, cancelled, refunded
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Core table: tickets (shared by IT and Customer Support, split by department_id)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tickets (
    id                 SERIAL PRIMARY KEY,
    ticket_code        VARCHAR(30) UNIQUE NOT NULL,
    department_id      INTEGER NOT NULL REFERENCES departments(id),
    category_id        INTEGER REFERENCES ticket_categories(id),
    customer_id        INTEGER REFERENCES customers(id),        -- NULL for internal IT tickets
    requester_user_id  INTEGER REFERENCES users(id),            -- NULL for Customer Support tickets (submitted by a customer)
    order_id           INTEGER REFERENCES orders(id),           -- only used for Customer Support tickets tied to an order
    assignee_id        INTEGER REFERENCES users(id),
    subject            VARCHAR(255) NOT NULL,
    description        TEXT,
    priority           VARCHAR(20) NOT NULL DEFAULT 'medium',   -- low, medium, high, urgent
    status             VARCHAR(30) NOT NULL DEFAULT 'open',     -- open, in_progress, pending, resolved, closed
    channel            VARCHAR(30) DEFAULT 'portal',            -- email, phone, portal, chat
    sla_due_at         TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at        TIMESTAMPTZ,
    closed_at          TIMESTAMPTZ,
    CONSTRAINT chk_ticket_origin CHECK (
        (customer_id IS NOT NULL AND requester_user_id IS NULL) OR
        (customer_id IS NULL AND requester_user_id IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_tickets_department ON tickets(department_id);
CREATE INDEX IF NOT EXISTS idx_tickets_status      ON tickets(status);
CREATE INDEX IF NOT EXISTS idx_tickets_customer    ON tickets(customer_id);
CREATE INDEX IF NOT EXISTS idx_tickets_created_at  ON tickets(created_at);

CREATE TABLE IF NOT EXISTS ticket_status_history (
    id          SERIAL PRIMARY KEY,
    ticket_id   INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    old_status  VARCHAR(30),
    new_status  VARCHAR(30) NOT NULL,
    changed_by  INTEGER REFERENCES users(id),
    changed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ticket_comments (
    id                  SERIAL PRIMARY KEY,
    ticket_id           INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    author_type         VARCHAR(20) NOT NULL,   -- 'agent' or 'customer'
    author_user_id      INTEGER REFERENCES users(id),
    author_customer_id  INTEGER REFERENCES customers(id),
    message             TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS clickup_sync (
    id               SERIAL PRIMARY KEY,
    ticket_id        INTEGER NOT NULL UNIQUE REFERENCES tickets(id) ON DELETE CASCADE,
    clickup_task_id  VARCHAR(50),
    sync_status      VARCHAR(20) NOT NULL DEFAULT 'pending',  -- pending, synced, failed
    last_synced_at   TIMESTAMPTZ,
    error_message    TEXT
);

-- ---------------------------------------------------------------------------
-- Trigger: automatically update updated_at + log history on status change
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_tickets_before_update() RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at := now();

    IF NEW.status IS DISTINCT FROM OLD.status THEN
        INSERT INTO ticket_status_history(ticket_id, old_status, new_status, changed_by)
        VALUES (OLD.id, OLD.status, NEW.status, NEW.assignee_id);

        IF NEW.status = 'resolved' AND OLD.status <> 'resolved' THEN
            NEW.resolved_at := now();
        END IF;
        IF NEW.status = 'closed' AND OLD.status <> 'closed' THEN
            NEW.closed_at := now();
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_tickets_before_update ON tickets;
CREATE TRIGGER trg_tickets_before_update
    BEFORE UPDATE ON tickets
    FOR EACH ROW EXECUTE FUNCTION fn_tickets_before_update();

-- ---------------------------------------------------------------------------
-- Summary view: used by the Streamlit dashboard and later by Power BI
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_tickets_summary AS
SELECT
    t.id,
    t.ticket_code,
    d.code            AS department_code,
    d.name            AS department_name,
    cat.name          AS category,
    t.priority,
    t.status,
    t.channel,
    cust.full_name    AS customer_name,
    cust.company,
    o.order_code,
    o.amount          AS order_amount,
    o.order_status,
    ag.full_name      AS assignee_name,
    t.created_at,
    t.sla_due_at,
    t.resolved_at,
    t.closed_at,
    CASE
        WHEN t.sla_due_at IS NULL THEN FALSE
        WHEN t.resolved_at IS NOT NULL THEN t.resolved_at > t.sla_due_at
        ELSE now() > t.sla_due_at
    END AS sla_breached
FROM tickets t
JOIN departments d        ON d.id = t.department_id
LEFT JOIN ticket_categories cat ON cat.id = t.category_id
LEFT JOIN customers cust  ON cust.id = t.customer_id
LEFT JOIN orders o        ON o.id = t.order_id
LEFT JOIN users ag        ON ag.id = t.assignee_id;
