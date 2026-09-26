"""Today page, SQL: the tuning constants the queries use and every query text, including the shared lead awaiting / past-SLA fragments the Leads and Sources pages reuse (split out of desktop/today_data.py, unchanged).
"""
from __future__ import annotations


DAYS = 7                       # sparkline length (today + 6 previous days)
TICKET_STALE_HOURS = 24        # an open ticket older than this shows up in the attention list even if not past SLA
ATTENTION_MAX = 5              # rows in the attention list (the page is meant to fit one screen)

OPEN_STATUS_LIST = ("open", "in_progress", "pending")       # same definition as flow_data / daily_check
OPEN_STATUSES = "(" + ", ".join(f"'{x}'" for x in OPEN_STATUS_LIST) + ")"

DETAIL_ROWS = 12               # rows in one drawer list (a tile opens "what it counts", worst first, capped)

# ============================================================================ SQL (read-only)
_DAYS_CTE = """
WITH d AS (
  SELECT n, date_trunc('day', now()) - n * interval '1 day' AS d0,
         LEAST(date_trunc('day', now()) - n * interval '1 day' + interval '1 day', now()) AS d1
  FROM generate_series(0, %d) AS n
)""" % (DAYS - 1)

# THE definition of "awaiting first reply" and "past SLA" for a lead, as SQL text, in one place. The Today page below and the
# Leads page (desktop/leads_data.py) both build their queries from these two helpers, so the two pages cannot disagree about
# which leads are late (a change here changes both).
#   reply evidence  a row in lead_updates (a ClickUp comment pulled by erp/clickup_pull.py); its time is occurred_at, or
#                   synced_at when ClickUp gave none
#   awaiting        no reply evidence (at the moment `at`; default: at any time)
#   past SLA        awaiting AND sla_due_at is before `at` (default: now)
REPLY_AT_SQL = "COALESCE(u.occurred_at, u.synced_at)"


def lead_awaiting_sql(lead_id: str = "l.id", at: str | None = None) -> str:
    when = f" AND {REPLY_AT_SQL} <= {at}" if at else ""
    return f"NOT EXISTS (SELECT 1 FROM lead_updates u WHERE u.lead_id = {lead_id}{when})"


def lead_past_sla_sql(lead: str = "l", lead_id: str = "l.id", at: str | None = None) -> str:
    return f"{lead}.sla_due_at < {at or 'now()'} AND {lead_awaiting_sql(lead_id, at)}"


# per day: leads that came in, leads past SLA and leads awaiting a first reply AS THEY WERE at the end of that day
# (for today: right now). The last element of each series is therefore exactly the number on the tile.
LEADS_SERIES_SQL = _DAYS_CTE + f"""
SELECT to_char(d.d0, 'YYYY-MM-DD') AS day,
  (SELECT count(*) FROM leads l WHERE l.created_at >= d.d0 AND l.created_at < d.d0 + interval '1 day') AS new_leads,
  (SELECT count(*) FROM leads l WHERE {lead_past_sla_sql(at="d.d1")}) AS overdue,
  (SELECT count(*) FROM leads l WHERE l.created_at <= d.d1 AND {lead_awaiting_sql(at="d.d1")}) AS awaiting
FROM d ORDER BY d.n DESC
"""

LEADS_NOW_SQL = f"""
SELECT
  (SELECT max(EXTRACT(EPOCH FROM (now() - l.sla_due_at)))::bigint FROM leads l
     WHERE {lead_past_sla_sql()}) AS worst_late_s,
  (SELECT max(EXTRACT(EPOCH FROM (now() - l.created_at)))::bigint FROM leads l
     WHERE {lead_awaiting_sql()}) AS oldest_wait_s,
  (SELECT count(*) FROM leads) AS total,
  to_char(now(), 'YYYY-MM-DD') AS today
"""

TICKETS_SERIES_SQL = _DAYS_CTE + """
SELECT to_char(d.d0, 'YYYY-MM-DD') AS day,
  (SELECT count(*) FROM tickets t WHERE t.created_at >= d.d0 AND t.created_at < d.d0 + interval '1 day') AS opened,
  (SELECT count(*) FROM tickets t WHERE COALESCE(t.resolved_at, t.closed_at) >= d.d0
                                    AND COALESCE(t.resolved_at, t.closed_at) < d.d0 + interval '1 day') AS resolved,
  (SELECT count(*) FROM tickets t WHERE t.created_at <= d.d1 AND (t.status IN %(open)s
                                    OR COALESCE(t.resolved_at, t.closed_at, t.updated_at) > d.d1)) AS open_end
FROM d ORDER BY d.n DESC
""" % {"open": OPEN_STATUSES}

TICKETS_NOW_SQL = """
SELECT count(*) AS total,
  count(*) FILTER (WHERE status IN %(open)s AND sla_due_at < now()) AS overdue,
  count(*) FILTER (WHERE status IN %(open)s AND (sla_due_at IS NULL OR sla_due_at >= now())
                     AND created_at < now() - interval '%(stale)d hours') AS stale,
  max(EXTRACT(EPOCH FROM (now() - sla_due_at))) FILTER (WHERE status IN %(open)s AND sla_due_at < now()) AS worst_late_s
FROM tickets
""" % {"open": OPEN_STATUSES, "stale": TICKET_STALE_HOURS}

ATTN_LEADS_SQL = f"""
SELECT l.id, l.full_name, l.company, l.source, rep.full_name AS rep,
       EXTRACT(EPOCH FROM (now() - l.sla_due_at))::bigint AS late_s
FROM leads l
LEFT JOIN lead_assignments la ON la.lead_id = l.id AND la.is_current
LEFT JOIN users rep ON rep.id = la.sales_rep_id
WHERE {lead_past_sla_sql()}
ORDER BY l.sla_due_at ASC, l.id
LIMIT {ATTENTION_MAX}
"""

ATTN_TICKETS_SQL = """
SELECT t.ticket_code, t.subject, t.priority, t.status, ag.full_name AS assignee, c.full_name AS customer,
       EXTRACT(EPOCH FROM (now() - t.created_at))::bigint AS age_s,
       CASE WHEN t.sla_due_at < now() THEN EXTRACT(EPOCH FROM (now() - t.sla_due_at))::bigint END AS late_s
FROM tickets t
LEFT JOIN users ag ON ag.id = t.assignee_id
LEFT JOIN customers c ON c.id = t.customer_id
WHERE t.status IN %(open)s AND (t.sla_due_at < now() OR t.created_at < now() - interval '%(stale)d hours')
ORDER BY late_s DESC NULLS LAST, age_s DESC
LIMIT %(lim)d
""" % {"open": OPEN_STATUSES, "stale": TICKET_STALE_HOURS, "lim": ATTENTION_MAX}

LAST_PULL_SQL = "SELECT max(last_comment_pulled_at) AS at FROM lead_clickup_sync"

# ---- "since yesterday" -----------------------------------------------------------------------------------------------
# Everything here uses date_trunc('day', now()) - the SAME day boundary and the same session time zone as the sparkline
# series above (lesson L-097), so the line can never describe a different day than the tiles. `tz` is the database session's
# zone name, printed on the page so a reader knows which midnight is meant. Whether there IS comparable history is derived
# from the numbers already read (leads on file minus the ones that arrived today): with nothing on file from before today
# the line stays silent instead of inventing a comparison.
CHANGES_SQL = """
SELECT
  (SELECT count(*) FROM lead_updates u
     WHERE COALESCE(u.occurred_at, u.synced_at) >= date_trunc('day', now())
       AND COALESCE(u.occurred_at, u.synced_at) <= now())                       AS replies_today,
  (SELECT count(DISTINCT u.lead_id) FROM lead_updates u
     WHERE COALESCE(u.occurred_at, u.synced_at) >= date_trunc('day', now())
       AND COALESCE(u.occurred_at, u.synced_at) <= now())                       AS leads_replied_today,
  (SELECT count(DISTINCT u.lead_id) FROM lead_updates u)                        AS leads_with_reply,
  current_setting('TimeZone')                                                   AS tz
"""

# ---- the detail drawer -------------------------------------------------------------------------------------------------
# One statement builds every lead list the drawer can show, because a tile opens "the rows behind this number" and each list
# wants its own order (newest first for "new today", worst first for the two late lists). `picks` takes the top DETAIL_ROWS
# of each list, then one join adds everything the panel shows. A lead that is in two lists comes back twice (at most
# 3 x DETAIL_ROWS rows); assemble() de-duplicates it into one item the lists point at.
# Never selected: e-mail, phone, raw_payload - the drawer shows only what the page already shows plus the AI summary.
DETAIL_LEADS_SQL = f"""
WITH picks AS (
  SELECT 'new_leads' AS list, id, ord FROM (
    SELECT l.id, row_number() OVER (ORDER BY l.created_at DESC, l.id DESC) AS ord
    FROM leads l WHERE l.created_at >= date_trunc('day', now())) a WHERE ord <= {DETAIL_ROWS}
  UNION ALL
  SELECT 'overdue_leads', id, ord FROM (
    SELECT l.id, row_number() OVER (ORDER BY l.sla_due_at ASC, l.id) AS ord
    FROM leads l WHERE {lead_past_sla_sql()}) b WHERE ord <= {DETAIL_ROWS}
  UNION ALL
  SELECT 'awaiting', id, ord FROM (
    SELECT l.id, row_number() OVER (ORDER BY l.created_at ASC, l.id) AS ord
    FROM leads l WHERE l.created_at <= now() AND {lead_awaiting_sql()}) c WHERE ord <= {DETAIL_ROWS}
  UNION ALL
  -- the drill-down behind "1 reply came in from ClickUp" in the "since yesterday" line: which leads those were
  SELECT 'replied_today', id, ord FROM (
    SELECT l.id, row_number() OVER (ORDER BY r.at DESC, l.id DESC) AS ord
    FROM leads l
    JOIN LATERAL (SELECT max({REPLY_AT_SQL}) AS at FROM lead_updates u WHERE u.lead_id = l.id) r ON TRUE
    WHERE r.at >= date_trunc('day', now()) AND r.at <= now()) d WHERE ord <= {DETAIL_ROWS}
  UNION ALL
  -- the other side of "awaiting first reply": the leads that DID get one, whenever it was. The awaiting list offers it as
  -- a follow-on ("... already have a reply on record"), so a reader can see the answered leads without leaving the drawer.
  SELECT 'replied', id, ord FROM (
    SELECT l.id, row_number() OVER (ORDER BY r.at DESC NULLS LAST, l.id DESC) AS ord
    FROM leads l
    JOIN LATERAL (SELECT max({REPLY_AT_SQL}) AS at FROM lead_updates u WHERE u.lead_id = l.id) r ON TRUE
    WHERE r.at IS NOT NULL) e WHERE ord <= {DETAIL_ROWS}
)
SELECT p.list, p.ord, l.id, l.full_name, l.company, l.source, l.created_at, l.sla_due_at,
       rep.rep,
       EXTRACT(EPOCH FROM (now() - l.created_at))::bigint AS age_s,
       CASE WHEN l.sla_due_at IS NOT NULL THEN EXTRACT(EPOCH FROM (now() - l.sla_due_at))::bigint END AS late_s,
       fr.at AS first_reply_at,
       cs.clickup_task_id, cs.sync_status, cs.last_synced_at, cs.last_comment_pulled_at,
       ai.model_name, ai.potential_score, ai.scale_estimate, ai.organization_type, ai.ai_notes, ai.analyzed_at,
       nu.content AS note, nu.author_name AS note_by, nu.source AS note_source,
       COALESCE(nu.occurred_at, nu.synced_at) AS note_at, (nu.occurred_at IS NULL) AS note_at_synced
FROM picks p
JOIN leads l ON l.id = p.id
LEFT JOIN LATERAL (SELECT ru.full_name AS rep FROM lead_assignments la JOIN users ru ON ru.id = la.sales_rep_id
                   WHERE la.lead_id = l.id AND la.is_current ORDER BY la.assigned_at DESC, la.id DESC LIMIT 1) rep ON TRUE
LEFT JOIN lead_clickup_sync cs ON cs.lead_id = l.id
LEFT JOIN LATERAL (SELECT min({REPLY_AT_SQL}) AS at FROM lead_updates u WHERE u.lead_id = l.id) fr ON TRUE
LEFT JOIN LATERAL (SELECT a.model_name, a.potential_score, a.scale_estimate, a.organization_type, a.ai_notes, a.analyzed_at
                   FROM lead_ai_analysis a WHERE a.lead_id = l.id
                   ORDER BY a.analyzed_at DESC, a.id DESC LIMIT 1) ai ON TRUE
LEFT JOIN LATERAL (SELECT u.content, u.author_name, u.source, u.occurred_at, u.synced_at
                   FROM lead_updates u WHERE u.lead_id = l.id
                   ORDER BY COALESCE(u.occurred_at, u.synced_at) DESC, u.id DESC LIMIT 1) nu ON TRUE
ORDER BY p.list, p.ord
"""

# The same idea for tickets. "attention_tickets" repeats the order of ATTN_TICKETS_SQL, so every row of "Needs a human"
# has a detail panel even when it is not among the first DETAIL_ROWS open tickets.
DETAIL_TICKETS_SQL = """
WITH picks AS (
  SELECT 'open_tickets' AS list, id, ord FROM (
    SELECT t.id, row_number() OVER (ORDER BY (t.sla_due_at < now()) DESC NULLS LAST, t.sla_due_at ASC NULLS LAST,
                                             t.created_at ASC, t.id) AS ord
    FROM tickets t WHERE t.status IN %(open)s) a WHERE ord <= %(cap)d
  UNION ALL
  SELECT 'tickets_today', id, ord FROM (
    SELECT t.id, row_number() OVER (ORDER BY t.created_at DESC, t.id DESC) AS ord
    FROM tickets t WHERE t.created_at >= date_trunc('day', now())) b WHERE ord <= %(cap)d
  UNION ALL
  SELECT 'attention_tickets', id, ord FROM (
    SELECT t.id, row_number() OVER (
             ORDER BY (CASE WHEN t.sla_due_at < now() THEN EXTRACT(EPOCH FROM (now() - t.sla_due_at)) END) DESC NULLS LAST,
                      EXTRACT(EPOCH FROM (now() - t.created_at)) DESC, t.id) AS ord
    FROM tickets t WHERE t.status IN %(open)s
      AND (t.sla_due_at < now() OR t.created_at < now() - interval '%(stale)d hours')) c WHERE ord <= %(cap)d
)
SELECT p.list, p.ord, t.ticket_code, t.subject, t.status, t.priority, t.channel, t.created_at, t.sla_due_at,
       t.resolved_at, t.closed_at, ag.full_name AS assignee, cu.full_name AS customer, cat.name AS category,
       EXTRACT(EPOCH FROM (now() - t.created_at))::bigint AS age_s,
       CASE WHEN t.sla_due_at < now() THEN EXTRACT(EPOCH FROM (now() - t.sla_due_at))::bigint END AS late_s
FROM picks p
JOIN tickets t ON t.id = p.id
LEFT JOIN users ag ON ag.id = t.assignee_id
LEFT JOIN customers cu ON cu.id = t.customer_id
LEFT JOIN ticket_categories cat ON cat.id = t.category_id
ORDER BY p.list, p.ord
""" % {"open": OPEN_STATUSES, "stale": TICKET_STALE_HOURS, "cap": DETAIL_ROWS}
