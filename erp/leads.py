"""
Business logic for the Lead-to-Sale flow:
  1) Save the new lead to the DB
  2) Check for a duplicate (email OR phone) against existing leads -> assign to the rep already handling it
     If it never existed before -> assign to the rep with the fewest active leads
  3) Call AI (DeepSeek) to analyze/classify the lead
  4) Create a ClickUp task (assigned to the rep, with AI notes) + store the sync link
"""
import json
import logging
from datetime import datetime, timezone

from sqlalchemy import text

from erp import ai_client, clickup_client
from erp.business_hours import add_business_hours
from erp.config import LEAD_SLA_HOURS
from erp.db import SessionLocal

logger = logging.getLogger(__name__)


def find_existing_assignment(session, email: str | None, phone: str | None):
    """Find the rep currently handling (is_current) an existing lead matching this email or phone."""
    query = text("""
        SELECT la.sales_rep_id, u.full_name AS rep_name
        FROM leads l
        JOIN lead_assignments la ON la.lead_id = l.id AND la.is_current
        JOIN users u ON u.id = la.sales_rep_id
        WHERE (:email IS NOT NULL AND l.email = :email)
           OR (:phone IS NOT NULL AND l.phone = :phone)
        ORDER BY la.assigned_at DESC
        LIMIT 1
    """)
    return session.execute(query, {"email": email, "phone": phone}).mappings().first()


def pick_least_loaded_sales_rep(session):
    """Pick the active rep currently handling (is_current) the fewest leads."""
    query = text("""
        SELECT u.id, u.full_name,
               COALESCE(cnt.active_leads, 0) AS active_leads
        FROM users u
        JOIN departments d ON d.id = u.department_id AND d.code = 'SALES'
        LEFT JOIN (
            SELECT sales_rep_id, COUNT(*) AS active_leads
            FROM lead_assignments
            WHERE is_current
            GROUP BY sales_rep_id
        ) cnt ON cnt.sales_rep_id = u.id
        WHERE u.is_active
        ORDER BY active_leads ASC, u.id ASC
        LIMIT 1
    """)
    return session.execute(query).mappings().first()


def insert_lead(session, payload: dict) -> int:
    sla_due_at = add_business_hours(datetime.now(timezone.utc), LEAD_SLA_HOURS)

    query = text("""
        INSERT INTO leads (full_name, email, phone, company, source, raw_payload, sla_due_at)
        VALUES (:full_name, :email, :phone, :company, :source, CAST(:raw_payload AS jsonb), :sla_due_at)
        RETURNING id
    """)
    result = session.execute(query, {
        "full_name": payload["full_name"],
        "email": payload.get("email"),
        "phone": payload.get("phone"),
        "company": payload.get("company"),
        "source": payload.get("source") or "unknown",
        "raw_payload": json.dumps(payload, ensure_ascii=False),
        "sla_due_at": sla_due_at,
    })
    return result.scalar_one()


def assign_lead(session, lead_id: int, sales_rep_id: int, reason: str) -> None:
    session.execute(text("""
        INSERT INTO lead_assignments (lead_id, sales_rep_id, assignment_reason)
        VALUES (:lead_id, :sales_rep_id, :reason)
    """), {"lead_id": lead_id, "sales_rep_id": sales_rep_id, "reason": reason})


def save_ai_analysis(session, lead_id: int, analysis: dict, model_name: str = "deepseek-chat") -> None:
    raw = analysis.get("raw_response")
    session.execute(text("""
        INSERT INTO lead_ai_analysis
            (lead_id, model_name, scale_estimate, potential_score, organization_type, ai_notes, raw_response)
        VALUES
            (:lead_id, :model_name, :scale_estimate, :potential_score, :organization_type, :ai_notes, CAST(:raw AS jsonb))
    """), {
        "lead_id": lead_id,
        "model_name": model_name,
        "scale_estimate": analysis.get("scale_estimate"),
        "potential_score": analysis.get("potential_score"),
        "organization_type": analysis.get("organization_type"),
        "ai_notes": analysis.get("ai_notes"),
        "raw": json.dumps(raw, ensure_ascii=False) if raw is not None else None,
    })


def save_clickup_sync(session, lead_id: int, task: dict) -> None:
    status = "mocked" if task.get("mocked") else "synced"
    session.execute(text("""
        INSERT INTO lead_clickup_sync (lead_id, clickup_task_id, sync_status, last_synced_at)
        VALUES (:lead_id, :task_id, :status, now())
        ON CONFLICT (lead_id) DO UPDATE
        SET clickup_task_id = EXCLUDED.clickup_task_id,
            sync_status = EXCLUDED.sync_status,
            last_synced_at = now()
    """), {"lead_id": lead_id, "task_id": task.get("id"), "status": status})


def build_task_description(payload: dict, analysis: dict) -> str:
    return (
        f"Lead: {payload.get('full_name')}\n"
        f"Email: {payload.get('email') or '-'} | Phone: {payload.get('phone') or '-'}\n"
        f"Company: {payload.get('company') or '-'}\n"
        f"Source: {payload.get('source') or 'unknown'}\n\n"
        f"--- AI analysis ---\n"
        f"Scale: {analysis.get('scale_estimate')}\n"
        f"Potential: {analysis.get('potential_score')}/100\n"
        f"Organization type: {analysis.get('organization_type')}\n"
        f"Note: {analysis.get('ai_notes')}"
    )


def process_new_lead(payload: dict) -> dict:
    """Run the full pipeline for one new lead from the webhook. Returns a result summary."""
    with SessionLocal() as session:
        lead_id = insert_lead(session, payload)

        existing = find_existing_assignment(session, payload.get("email"), payload.get("phone"))
        if existing:
            sales_rep_id, rep_name = existing["sales_rep_id"], existing["rep_name"]
            reason = "existing_duplicate"
        else:
            rep = pick_least_loaded_sales_rep(session)
            if rep is None:
                raise RuntimeError("No active sales rep found in the system (dept SALES).")
            sales_rep_id, rep_name = rep["id"], rep["full_name"]
            reason = "round_robin_new"

        assign_lead(session, lead_id, sales_rep_id, reason)

        analysis = ai_client.analyze_lead(payload)
        save_ai_analysis(session, lead_id, analysis)

        rep_clickup_id = session.execute(
            text("SELECT clickup_user_id FROM users WHERE id = :id"),
            {"id": sales_rep_id},
        ).scalar_one_or_none()

        task = clickup_client.create_task(
            name=f"[Lead] {payload.get('full_name')}",
            description=build_task_description(payload, analysis),
            assignee_clickup_id=rep_clickup_id,
        )
        save_clickup_sync(session, lead_id, task)

        session.commit()

        logger.info(
            f"Lead {lead_id} ({payload.get('full_name')}) -> assigned to {rep_name} "
            f"(reason: {reason}), ClickUp task: {task.get('id')}"
        )

        return {
            "lead_id": lead_id,
            "assigned_sales_rep": rep_name,
            "assignment_reason": reason,
            "ai_analysis": analysis,
            "clickup_task": task,
        }
