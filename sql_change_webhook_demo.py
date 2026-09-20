"""
=============================================================================
EXAMPLE: AUTOMATICALLY SENDING A WEBHOOK WHEN NEW DATA APPEARS IN THE
DATABASE (POLLING PATTERN)
=============================================================================
Core idea: MySQL/SQL CANNOT call an HTTP webhook directly by itself.
So the most common approach (and the easiest to explain in an interview) is
to use PYTHON to "poll" - i.e. periodically check the DB for any new
records since the last check. If there are any, POST each one to the webhook.

Workflow:
    1. Read the previous "checkpoint" (last_processed_id) from a file/DB
    2. Query the DB: fetch records with id > last_processed_id
    3. For each new record -> POST it to the webhook
    4. If the send succeeds -> update the "checkpoint"
    5. Repeat periodically (every X seconds), or run on a schedule
       (Task Scheduler/cron)

This file RUNS OUT OF THE BOX using mock data (no real DB needed) so you
can read and follow the flow at the same time.
=============================================================================
"""

import time
import logging
from pathlib import Path
from typing import Optional

import requests

# For a real MySQL setup, you'd also need:
# import mysql.connector
# DB_CONFIG = {"host": "...", "user": "...", "password": "...", "database": "..."}


# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

WEBHOOK_URL = "https://example.com/webhooks/YOUR_WEBHOOK_TOKEN"
STATE_FILE = Path("last_processed_id.txt")   # tracks which id we've already sent a webhook for
POLL_INTERVAL_SECONDS = 30                   # check the DB every 30 seconds


# ---------------------------------------------------------------------------
# STEP 1: READ / WRITE THE "CHECKPOINT" (state) - to know how far we've processed
# ---------------------------------------------------------------------------
def get_last_processed_id() -> int:
    """Read the last successfully processed id. Returns 0 if the file doesn't exist yet."""
    if STATE_FILE.exists():
        content = STATE_FILE.read_text().strip()
        return int(content) if content else 0
    return 0


def save_last_processed_id(order_id: int) -> None:
    """Save the id we just finished processing - the next poll won't resend it."""
    STATE_FILE.write_text(str(order_id))
    logger.info(f"State updated: last_processed_id = {order_id}")


# ---------------------------------------------------------------------------
# STEP 2: QUERY THE DB FOR NEW RECORDS (mocked here for the demo)
# ---------------------------------------------------------------------------
def fetch_new_orders(last_id: int) -> list[dict]:
    """
    In a real scenario you'd write:

        conn = mysql.connector.connect(**DB_CONFIG)
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT id, customer, amount, status "
            "FROM orders WHERE id > %s ORDER BY id ASC",
            (last_id,)
        )
        rows = cursor.fetchall()
        conn.close()
        return rows

    MOCKED here: simulate a DB that already has 2 new orders with id > last_id.
    Once everything has been processed (last_id >= 2), there's nothing new left.
    """
    if last_id >= 2:
        return []
    mock_new_rows = [
        {"id": 1, "customer": "Nguyen Van A", "amount": 150.0, "status": "completed"},
        {"id": 2, "customer": "Tran Thi B", "amount": 89.5, "status": "completed"},
    ]
    # only return records with id > last_id (equivalent to the WHERE id > %s condition)
    return [row for row in mock_new_rows if row["id"] > last_id]


# ---------------------------------------------------------------------------
# STEP 3: SEND A WEBHOOK FOR 1 RECORD
# ---------------------------------------------------------------------------
def send_webhook(order: dict) -> bool:
    """Send 1 order to the webhook. Returns True on success, False on failure."""
    payload = {
        "event": "order_created",
        "order_id": order["id"],
        "customer": order["customer"],
        "amount": order["amount"],
        "status": order["status"],
    }
    headers = {"Content-Type": "application/json"}

    try:
        response = requests.post(WEBHOOK_URL, json=payload, headers=headers, timeout=10)
        response.raise_for_status()
        logger.info(f"Webhook sent OK for order_id={order['id']} (status {response.status_code})")
        return True
    except requests.exceptions.RequestException as e:
        logger.error(f"Webhook FAILED for order_id={order['id']}: {e}")
        return False


# ---------------------------------------------------------------------------
# STEP 4: RUN ONCE - check, send, update state
# ---------------------------------------------------------------------------
def run_once() -> None:
    last_id = get_last_processed_id()
    new_orders = fetch_new_orders(last_id)

    if not new_orders:
        logger.info("No new data since the last check.")
        return

    logger.info(f"Found {len(new_orders)} new record(s), sending webhooks...")

    for order in new_orders:
        success = send_webhook(order)
        if success:
            # ONLY update the "checkpoint" after a SUCCESSFUL send
            # -> if it fails, the next poll retries this record, no data is lost
            save_last_processed_id(order["id"])
        else:
            logger.warning(f"Stopping at order_id={order['id']}, will retry on the next poll.")
            break  # do NOT skip the failed record, to avoid losing data


# ---------------------------------------------------------------------------
# STEP 5 (extension): RUN CONTINUOUSLY ON A SCHEDULE - for real deployment
# ---------------------------------------------------------------------------
def run_forever() -> None:
    """
    Infinite polling loop - use this to run as a background service.
    In practice, Windows Task Scheduler / cron is usually preferred to call
    run_once() periodically instead of a while True loop (to avoid the
    script silently "dying" without anyone noticing).
    """
    logger.info(f"Starting polling, checking the DB every {POLL_INTERVAL_SECONDS} second(s)...")
    while True:
        run_once()
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    # Demo: run once only. Switch to run_forever() to run continuously.
    run_once()
