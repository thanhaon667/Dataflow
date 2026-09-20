-- =============================================================================
-- Sample data for the Customer Support module (Order ticket). The IT
-- department is also created (no tickets yet) so Phase 2 can start right
-- away without schema changes.
--
-- RUN WITH:
--   & "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U erp_app -h 127.0.0.1 -p 5432 -d erp_support -f db/sql/02_seed_cskh.sql
-- =============================================================================

-- ---------------------------------------------------------------------------
-- Departments
-- ---------------------------------------------------------------------------
INSERT INTO departments (code, name) VALUES
    ('CSKH', 'Customer Support'),
    ('IT',   'IT Support')
ON CONFLICT (code) DO NOTHING;

-- ---------------------------------------------------------------------------
-- Ticket categories
-- ---------------------------------------------------------------------------
INSERT INTO ticket_categories (department_id, name)
SELECT d.id, c.name
FROM departments d
JOIN (VALUES
    ('CSKH', 'Order Issue'),
    ('CSKH', 'Refund Request'),
    ('CSKH', 'Complaint'),
    ('CSKH', 'Shipping Delay'),
    ('CSKH', 'General Inquiry'),
    ('IT',   'Hardware'),
    ('IT',   'Software'),
    ('IT',   'Network Access')
) AS c(dept_code, name) ON c.dept_code = d.code
ON CONFLICT (department_id, name) DO NOTHING;

-- ---------------------------------------------------------------------------
-- SLA (minutes)
-- ---------------------------------------------------------------------------
INSERT INTO sla_policies (department_id, priority, response_time_minutes, resolution_time_minutes)
SELECT d.id, s.priority, s.response_time_minutes, s.resolution_time_minutes
FROM departments d
JOIN (VALUES
    ('CSKH', 'urgent', 15,   120),
    ('CSKH', 'high',   30,   480),
    ('CSKH', 'medium', 60,   1440),
    ('CSKH', 'low',    240,  4320),
    ('IT',   'urgent', 15,   240),
    ('IT',   'high',   30,   720),
    ('IT',   'medium', 120,  2880),
    ('IT',   'low',    480,  4320)
) AS s(dept_code, priority, response_time_minutes, resolution_time_minutes)
    ON s.dept_code = d.code
ON CONFLICT (department_id, priority) DO NOTHING;

-- ---------------------------------------------------------------------------
-- Customer Support staff
-- ---------------------------------------------------------------------------
INSERT INTO users (full_name, email, department_id, role)
SELECT u.full_name, u.email, d.id, u.role
FROM departments d
JOIN (VALUES
    ('Pham Thi Huong', 'huong.pham@company.vn',  'CSKH', 'agent'),
    ('Nguyen Van Khoa', 'khoa.nguyen@company.vn', 'CSKH', 'agent'),
    ('Tran Minh Duc',   'duc.tran@company.vn',    'CSKH', 'lead')
) AS u(full_name, email, dept_code, role) ON u.dept_code = d.code
ON CONFLICT (email) DO NOTHING;

-- ---------------------------------------------------------------------------
-- Customers
-- ---------------------------------------------------------------------------
INSERT INTO customers (full_name, email, phone, company) VALUES
    ('Nguyen Van A',  'a.nguyen@example.com',  '0901111111', NULL),
    ('Tran Thi B',    'b.tran@example.com',    '0902222222', 'ABC Trading'),
    ('Le Van C',      'c.le@example.com',      '0903333333', NULL),
    ('Pham Thi D',    'd.pham@example.com',    '0904444444', 'DEF Corp'),
    ('Hoang Van E',   'e.hoang@example.com',   '0905555555', NULL)
ON CONFLICT DO NOTHING;

-- ---------------------------------------------------------------------------
-- Orders
-- ---------------------------------------------------------------------------
INSERT INTO orders (order_code, customer_id, amount, order_status, created_at)
SELECT o.order_code, c.id, o.amount, o.order_status, now() - o.days_ago * INTERVAL '1 day'
FROM customers c
JOIN (VALUES
    ('ORD-1001', 'a.nguyen@example.com', 1250000, 'delivered', 10),
    ('ORD-1002', 'b.tran@example.com',   3400000, 'shipped',   6),
    ('ORD-1003', 'c.le@example.com',      780000, 'delivered', 9),
    ('ORD-1004', 'd.pham@example.com',   5200000, 'paid',      3),
    ('ORD-1005', 'e.hoang@example.com',  2100000, 'delivered', 12),
    ('ORD-1006', 'a.nguyen@example.com',  450000, 'cancelled', 2),
    ('ORD-1007', 'b.tran@example.com',   1980000, 'delivered', 15)
) AS o(order_code, customer_email, amount, order_status, days_ago)
    ON o.customer_email = c.email
ON CONFLICT (order_code) DO NOTHING;

-- ---------------------------------------------------------------------------
-- Customer Support tickets linked to orders, varied status / priority / SLA
-- ---------------------------------------------------------------------------
INSERT INTO tickets (
    ticket_code, department_id, category_id, customer_id, order_id,
    assignee_id, subject, description, priority, status, channel,
    sla_due_at, created_at, resolved_at, closed_at
)
SELECT
    t.ticket_code,
    d.id,
    cat.id,
    cust.id,
    ord.id,
    ag.id,
    t.subject,
    t.description,
    t.priority,
    t.status,
    t.channel,
    (now() - t.created_days_ago * INTERVAL '1 day') + t.sla_minutes * INTERVAL '1 minute',
    now() - t.created_days_ago * INTERVAL '1 day',
    CASE WHEN t.status IN ('resolved', 'closed')
         THEN now() - t.resolved_days_ago * INTERVAL '1 day' END,
    CASE WHEN t.status = 'closed'
         THEN now() - t.closed_days_ago * INTERVAL '1 day' END
FROM (VALUES
    ('TCK-CS-0001', 'Order Issue',      'a.nguyen@example.com', 'ORD-1001', 'huong.pham@company.vn',
        'Order delivered with a missing item', 'Customer received order ORD-1001 with 1 item missing.',
        'high', 'resolved', 'email', 10, 480, 8, 8),
    ('TCK-CS-0002', 'Shipping Delay',   'b.tran@example.com',   'ORD-1002', 'khoa.nguyen@company.vn',
        'Order delivered later than expected', 'Order ORD-1002 is 3 days behind the expected delivery date.',
        'medium', 'in_progress', 'chat', 6, 1440, NULL, NULL),
    ('TCK-CS-0003', 'Refund Request',   'c.le@example.com',     'ORD-1003', 'huong.pham@company.vn',
        'Refund request for a defective item', 'The item was defective; the customer is requesting a refund.',
        'high', 'resolved', 'portal', 9, 480, 7, 7),
    ('TCK-CS-0004', 'Order Issue',      'd.pham@example.com',   'ORD-1004', 'khoa.nguyen@company.vn',
        'Wrong shipping address', 'Customer wants to change the shipping address before the order ships.',
        'urgent', 'open', 'phone', 3, 120, NULL, NULL),
    ('TCK-CS-0005', 'Complaint',        'e.hoang@example.com',  'ORD-1005', 'duc.tran@company.vn',
        'Complaint about delivery staff attitude', 'Customer reports the courier was rude.',
        'medium', 'pending', 'email', 5, 1440, NULL, NULL),
    ('TCK-CS-0006', 'General Inquiry',  'a.nguyen@example.com', NULL,       'khoa.nguyen@company.vn',
        'Question about the return policy', 'Customer is asking about the 7-day return policy.',
        'low', 'closed', 'chat', 20, 4320, 18, 17),
    ('TCK-CS-0007', 'Order Issue',      'b.tran@example.com',   'ORD-1006', 'huong.pham@company.vn',
        'Order was cancelled but card was still charged', 'ORD-1006 was cancelled but the customer still sees a charge on their card.',
        'urgent', 'open', 'portal', 1, 120, NULL, NULL),
    ('TCK-CS-0008', 'Shipping Delay',   'b.tran@example.com',   'ORD-1007', 'khoa.nguyen@company.vn',
        'Checking on a slow-moving order', 'Customer is asking about the shipping status of ORD-1007.',
        'low', 'in_progress', 'email', 4, 4320, NULL, NULL)
) AS t(ticket_code, category_name, customer_email, order_code, assignee_email,
       subject, description, priority, status, channel, created_days_ago, sla_minutes,
       resolved_days_ago, closed_days_ago)
JOIN departments d          ON d.code = 'CSKH'
JOIN ticket_categories cat  ON cat.name = t.category_name AND cat.department_id = d.id
JOIN customers cust         ON cust.email = t.customer_email
LEFT JOIN orders ord        ON ord.order_code = t.order_code
JOIN users ag                ON ag.email = t.assignee_email
ON CONFLICT (ticket_code) DO NOTHING;
