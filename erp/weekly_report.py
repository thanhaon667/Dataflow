"""
Weekly report: summarize the last 7 days of lead activity + call DeepSeek for
insights/recommended actions.

Run:
    python -m erp.weekly_report
"""
import json
import logging
import sys

from sqlalchemy import text

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # ensure non-ASCII characters print correctly on Windows consoles

from erp import ai_client
from erp.db import SessionLocal

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def gather_weekly_data(session) -> dict:
    leads = session.execute(text("""
        SELECT l.id, l.full_name, l.company, l.created_at,
               rep.full_name AS sales_rep, la.assignment_reason,
               ai.potential_score, ai.scale_estimate,
               cu.sync_status
        FROM leads l
        LEFT JOIN lead_assignments la ON la.lead_id = l.id AND la.is_current
        LEFT JOIN users rep ON rep.id = la.sales_rep_id
        LEFT JOIN LATERAL (
            SELECT * FROM lead_ai_analysis a WHERE a.lead_id = l.id
            ORDER BY analyzed_at DESC LIMIT 1
        ) ai ON TRUE
        LEFT JOIN lead_clickup_sync cu ON cu.lead_id = l.id
        WHERE l.created_at >= now() - INTERVAL '7 days'
        ORDER BY l.created_at
    """)).mappings().all()

    updates = session.execute(text("""
        SELECT lead_id, content, author_name, occurred_at
        FROM lead_updates
        WHERE occurred_at >= now() - INTERVAL '7 days'
        ORDER BY occurred_at
    """)).mappings().all()

    return {
        "leads": [dict(r) for r in leads],
        "updates": [dict(r) for r in updates],
    }


def build_report(data: dict) -> str:
    if not data["leads"] and not data["updates"]:
        return "No leads or updates in the last 7 days."

    if not ai_client.is_configured():
        logger.warning("DeepSeek is not configured - dumping raw data only, no AI report.")
        return json.dumps(data, ensure_ascii=False, indent=2, default=str)

    prompt = (
        "You are a Sales team lead. Based on the lead and update data from the past "
        "week below, write a concise weekly report covering: "
        "(1) an overview of new lead volume and the breakdown per sales rep, "
        "(2) a list of high-potential leads (high potential_score) to prioritize, "
        "(3) observations from the customer updates/interactions logged, "
        "(4) recommended actions for next week.\n\n"
        f"Data (JSON): {json.dumps(data, ensure_ascii=False, default=str)}"
    )
    return ai_client.chat(prompt)


if __name__ == "__main__":
    with SessionLocal() as session:
        weekly_data = gather_weekly_data(session)

    report_text = build_report(weekly_data)
    print("\n" + "=" * 60)
    print("WEEKLY REPORT - LEAD & SALE")
    print("=" * 60)
    print(report_text)
