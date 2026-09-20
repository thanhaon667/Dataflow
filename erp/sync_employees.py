"""
Sync staff FROM the ClickUp workspace INTO the users table.
Matched by email: already exists -> update clickup_user_id; not found -> create new.

This is the standard flow for bringing real staff into the system (instead of
typing them in by hand) - run it whenever a new member is invited into the
ClickUp workspace.

Run:
    python -m erp.sync_employees --department SALES
"""
import argparse
import logging

from sqlalchemy import text

from erp import clickup_client
from erp.db import SessionLocal

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def sync_employees(department_code: str = "SALES") -> int:
    members = clickup_client.get_team_members()
    if not members:
        logger.warning("Could not fetch any members from ClickUp (check CLICKUP_API_TOKEN in .env).")
        return 0

    synced = 0
    with SessionLocal() as session:
        dept_id = session.execute(
            text("SELECT id FROM departments WHERE code = :code"),
            {"code": department_code},
        ).scalar_one_or_none()
        if dept_id is None:
            raise RuntimeError(f"No department found with code={department_code}")

        for m in members:
            email = (m.get("email") or "").strip()
            if not email:
                continue

            existing_id = session.execute(
                text("SELECT id FROM users WHERE email = :email"),
                {"email": email},
            ).scalar_one_or_none()

            if existing_id:
                session.execute(text("""
                    UPDATE users
                    SET clickup_user_id = :clickup_id, is_active = TRUE
                    WHERE id = :id
                """), {"clickup_id": str(m["id"]), "id": existing_id})
                logger.info(f"Updated clickup_user_id for existing user: {email}")
            else:
                session.execute(text("""
                    INSERT INTO users (full_name, email, department_id, role, clickup_user_id)
                    VALUES (:full_name, :email, :department_id, 'sales', :clickup_id)
                """), {
                    "full_name": m.get("username") or email,
                    "email": email,
                    "department_id": dept_id,
                    "clickup_id": str(m["id"]),
                })
                logger.info(f"Created new staff member from ClickUp: {email}")
            synced += 1

        session.commit()

    return synced


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sync ClickUp workspace members into DB staff records")
    parser.add_argument("--department", default="SALES", help="Department code (default: SALES)")
    args = parser.parse_args()

    count = sync_employees(args.department)
    print(f"Synced {count} staff member(s) from ClickUp into department {args.department}.")
