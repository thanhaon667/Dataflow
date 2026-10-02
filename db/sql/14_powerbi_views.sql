-- =============================================================================
-- Power BI views: flat, personal-data-free tables for the three ERP Desk report templates in powerbi/
-- (Social listening, Leads and SLA, Channels). Additive: only CREATE OR REPLACE VIEW and GRANTs.
-- No email, phone or person name is exposed (a lead is its numeric id and company); authors of social posts are not exposed.
--
-- RUN WITH:
--   psql -U erp_app -h 127.0.0.1 -d erp_support -f db/sql/14_powerbi_views.sql
-- Needs 03/05 (leads), 07/09/11 (marketing) and 12 (social listening). The GRANTs go to powerbi_reader when that role exists
-- (db/sql/06_powerbi_readonly.sql creates it; run 06 first, or run this file again afterwards).
-- =============================================================================

-- The views are rebuilt on every run (a view cannot gain a column in the middle with CREATE OR REPLACE); only Power BI reads them.
DROP VIEW IF EXISTS pbi_social_batch, pbi_social_event, pbi_social_mention_topic, pbi_social_mention, pbi_channel_daily, pbi_leads, pbi_calendar;

-- One row per day, the shared date table of all three reports.
CREATE OR REPLACE VIEW pbi_calendar AS
SELECT d::date AS date,
       extract(year FROM d)::int AS year,
       extract(month FROM d)::int AS month_number,
       to_char(d, 'Mon') AS month_name,
       to_char(d, 'YYYY-MM') AS year_month,
       date_trunc('week', d)::date AS week_start,
       to_char(d, 'Dy') AS day_name,
       extract(isodow FROM d)::int AS day_of_week
FROM generate_series(date '2024-01-01', date '2030-12-31', interval '1 day') AS g(d);

-- ---------------------------------------------------------------------------------- Leads and SLA
-- first_reply_at = the first synced comment (lead_updates). reply_minutes is plain elapsed time; the 5-business-hour SLA itself is
-- stored in leads.sla_due_at by erp/business_hours.py, so "within SLA" simply compares with that deadline.
CREATE OR REPLACE VIEW pbi_leads AS
SELECT l.id AS lead_id,
       (l.created_at AT TIME ZONE 'Asia/Ho_Chi_Minh')::date AS created_date,
       l.created_at,
       coalesce(l.source, '(unknown)') AS source,
       coalesce(l.company, '(none)') AS company,
       coalesce(rep.full_name, '(unassigned)') AS sales_rep,
       coalesce(la.assignment_reason, '(none)') AS assignment_reason,
       ai.potential_score,
       coalesce(ai.organization_type, '(not analysed)') AS organization_type,
       coalesce(ai.scale_estimate, '(not analysed)') AS scale_estimate,
       coalesce(cu.sync_status, '(no task)') AS clickup_status,
       l.sla_due_at,
       fr.first_reply_at,
       CASE WHEN fr.first_reply_at IS NOT NULL THEN round(extract(epoch FROM fr.first_reply_at - l.created_at) / 60.0) END AS reply_minutes,
       CASE WHEN fr.first_reply_at IS NULL THEN 'No reply yet'
            WHEN fr.first_reply_at - l.created_at < interval '15 minutes' THEN '1. under 15 min'
            WHEN fr.first_reply_at - l.created_at < interval '1 hour' THEN '2. 15 min to 1 h'
            WHEN fr.first_reply_at - l.created_at < interval '5 hours' THEN '3. 1 to 5 h'
            WHEN fr.first_reply_at - l.created_at < interval '24 hours' THEN '4. 5 to 24 h'
            ELSE '5. over 24 h' END AS reply_bucket,
       CASE WHEN l.sla_due_at IS NULL THEN 'No SLA'
            WHEN fr.first_reply_at IS NOT NULL AND fr.first_reply_at <= l.sla_due_at THEN 'Replied in time'
            WHEN fr.first_reply_at IS NOT NULL THEN 'Replied late'
            WHEN now() > l.sla_due_at THEN 'Waiting, past SLA'
            ELSE 'Waiting, in time' END AS sla_status
FROM leads l
LEFT JOIN lead_assignments la ON la.lead_id = l.id AND la.is_current
LEFT JOIN users rep ON rep.id = la.sales_rep_id
LEFT JOIN LATERAL (SELECT a.scale_estimate, a.organization_type, a.potential_score FROM lead_ai_analysis a WHERE a.lead_id = l.id ORDER BY a.id DESC LIMIT 1) ai ON TRUE
LEFT JOIN LATERAL (SELECT c.sync_status FROM lead_clickup_sync c WHERE c.lead_id = l.id ORDER BY c.id DESC LIMIT 1) cu ON TRUE
LEFT JOIN LATERAL (SELECT min(coalesce(u.occurred_at, u.synced_at)) AS first_reply_at FROM lead_updates u WHERE u.lead_id = l.id) fr ON TRUE;

-- ---------------------------------------------------------------------------------- Channels
-- Daily channel rollup with names. Money in real units and ALWAYS with its currency: a measure must never add two currencies.
CREATE OR REPLACE VIEW pbi_channel_daily AS
SELECT r.event_date AS date,
       coalesce(c.display_name, c.channel_key) AS channel,
       coalesce(c.medium, '(unknown)') AS medium,
       CASE WHEN c.is_paid THEN 'Paid' WHEN c.is_paid IS FALSE THEN 'Organic' ELSE '(unknown)' END AS paid_or_organic,
       r.currency,
       r.grain,
       r.impressions, r.clicks, r.sessions, r.conversions,
       r.spend_micros / 1000000.0 AS spend,
       r.revenue_micros / 1000000.0 AS revenue
FROM interaction_daily_channel_rollup r
JOIN marketing_channel c ON c.channel_id = r.channel_id;

-- ---------------------------------------------------------------------------------- Social listening
CREATE OR REPLACE VIEW pbi_social_mention AS
SELECT id AS mention_id,
       posted_date AS date,
       posted_at,
       extract(hour FROM posted_at AT TIME ZONE 'Asia/Ho_Chi_Minh')::int AS hour_of_day,
       coalesce(brand, '(none)') AS brand,
       coalesce(is_own, FALSE) AS is_own_brand,
       platform,
       sentiment,
       sentiment_score,
       likes, comments, shares, views, engagement,
       left(content, 200) AS snippet
FROM sl.v_mention;

-- One row per (post, topic). Brand, channel, sentiment and date are copied onto it so a topic chart needs no cross-table filtering.
CREATE OR REPLACE VIEW pbi_social_mention_topic AS
SELECT mt.mention_id, t.label AS topic, t.theme,
       m.posted_date AS date, coalesce(m.brand, '(none)') AS brand, m.platform, m.sentiment, m.engagement
FROM sl.mention_topic mt
JOIN sl.topic t ON t.id = mt.topic_id
JOIN sl.v_mention m ON m.id = mt.mention_id;

CREATE OR REPLACE VIEW pbi_social_event AS
SELECT event_date AS date, label FROM sl.event;

CREATE OR REPLACE VIEW pbi_social_batch AS
SELECT id AS batch_id, source_file, loaded_at, rows_in, rows_bad, rows_dup, rows_spam, rows_kept FROM sl.import_batch;

-- ---------------------------------------------------------------------------------- permissions
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'powerbi_reader') THEN
    GRANT USAGE ON SCHEMA public TO powerbi_reader;
    GRANT SELECT ON pbi_calendar, pbi_leads, pbi_channel_daily, pbi_social_mention, pbi_social_mention_topic, pbi_social_event, pbi_social_batch TO powerbi_reader;
  END IF;
END $$;
