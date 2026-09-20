"""
Pull new comments from ClickUp tasks into the DB (lead_updates), with a
timestamp of when they were synced. Run periodically via Windows Task
Scheduler / cron.

Run once:
    python -m erp.clickup_pull
"""
import json
import logging
from datetime import datetime, timezone

from sqlalchemy import text

from erp import clickup_client
from erp.db import SessionLocal

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def _parse_clickup_time(value) -> datetime | None:
    if not value:
        return None
    return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)


def pull_all() -> int:
    total_new = 0
    with SessionLocal() as session:
        rows = session.execute(text("""
            SELECT lead_id, clickup_task_id, last_comment_pulled_at
            FROM lead_clickup_sync
            WHERE sync_status = 'synced'
        """)).mappings().all()

        for row in rows:
            comments = clickup_client.get_task_comments(row["clickup_task_id"])
            new_count = 0

            for c in comments:
                occurred_at = _parse_clickup_time(c.get("date"))
                if row["last_comment_pulled_at"] and occurred_at and occurred_at <= row["last_comment_pulled_at"]:
                    continue

                session.execute(text("""
                    INSERT INTO lead_updates (lead_id, source, content, author_name, occurred_at, raw_payload)
                    VALUES (:lead_id, 'clickup_comment', :content, :author_name, :occurred_at, CAST(:raw AS jsonb))
                """), {
                    "lead_id": row["lead_id"],
                    "content": c.get("comment_text", ""),
                    "author_name": (c.get("user") or {}).get("username"),
                    "occurred_at": occurred_at,
                    "raw": json.dumps(c, ensure_ascii=False),
                })
                new_count += 1

            session.execute(text("""
                UPDATE lead_clickup_sync
                SET last_comment_pulled_at = now()
                WHERE lead_id = :lead_id
            """), {"lead_id": row["lead_id"]})

            if new_count:
                logger.info(f"Lead {row['lead_id']}: pulled {new_count} new comment(s) from ClickUp.")
            total_new += new_count

        session.commit()

    logger.info(f"Done: {total_new} new comment(s) synced in total.")
    return total_new


if __name__ == "__main__":
    pull_all()
