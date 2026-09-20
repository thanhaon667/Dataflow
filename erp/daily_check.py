"""
Daily check: sync the latest ClickUp comments into the DB, then list anything
that needs attention (SLA breaches, leads not synced to ClickUp, leads not
yet analyzed by AI, leads with no follow-up/update in a while).

Run:
    python -m erp.daily_check
"""
import sys

from sqlalchemy import text

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from erp.clickup_pull import pull_all
from erp.db import SessionLocal


def run_daily_check() -> int:
    print("=== Syncing latest comments from ClickUp ===")
    new_comments = pull_all()

    with SessionLocal() as session:
        sla_breach = session.execute(text("""
            SELECT ticket_code, department_name, assignee_name, priority
            FROM v_tickets_summary
            WHERE sla_breached AND status NOT IN ('resolved', 'closed')
        """)).mappings().all()

        not_synced = session.execute(text("""
            SELECT lead_id, full_name, sync_status
            FROM v_leads_summary
            WHERE sync_status IS DISTINCT FROM 'synced'
        """)).mappings().all()

        lead_sla_breach = session.execute(text("""
            SELECT lead_id, full_name, assigned_sales_rep, sla_due_at
            FROM v_leads_summary
            WHERE sla_breached
        """)).mappings().all()

        no_analysis = session.execute(text("""
            SELECT l.id, l.full_name
            FROM leads l
            LEFT JOIN lead_ai_analysis a ON a.lead_id = l.id
            WHERE a.id IS NULL
        """)).mappings().all()

        stale_leads = session.execute(text("""
            SELECT l.id, l.full_name, l.created_at
            FROM leads l
            LEFT JOIN lead_updates u ON u.lead_id = l.id
            WHERE u.id IS NULL AND l.created_at < now() - INTERVAL '2 days'
        """)).mappings().all()

    print(f"Synced {new_comments} new comment(s) from ClickUp.\n")

    print(f"--- Open tickets breaching SLA: {len(sla_breach)} ---")
    for r in sla_breach:
        print(f"  {r['ticket_code']} | {r['department_name']} | assignee: {r['assignee_name']} | priority {r['priority']}")

    print(f"\n--- Leads not yet synced to ClickUp: {len(not_synced)} ---")
    for r in not_synced:
        print(f"  Lead #{r['lead_id']} {r['full_name']} - status: {r['sync_status']}")

    print(f"\n--- Unresolved leads breaching SLA (5 business hours): {len(lead_sla_breach)} ---")
    for r in lead_sla_breach:
        print(f"  Lead #{r['lead_id']} {r['full_name']} | Sales rep: {r['assigned_sales_rep']} | due: {r['sla_due_at']}")

    print(f"\n--- Leads not yet analyzed by AI: {len(no_analysis)} ---")
    for r in no_analysis:
        print(f"  Lead #{r['id']} {r['full_name']}")

    print(f"\n--- Leads with no update/follow-up in over 2 days: {len(stale_leads)} ---")
    for r in stale_leads:
        print(f"  Lead #{r['id']} {r['full_name']} (created at {r['created_at']})")

    total_issues = len(sla_breach) + len(not_synced) + len(no_analysis) + len(stale_leads) + len(lead_sla_breach)
    print(f"\n=== SUMMARY: {total_issues} item(s) needing attention ===")
    return total_issues


if __name__ == "__main__":
    run_daily_check()
