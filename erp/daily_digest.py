"""
Daily digest: reads today's key pipeline metrics from the live DB, compares
them against yesterday's numbers (a small local JSON history file - no DB
migration needed), asks the AI agent (erp.ai_client.chat) for a short
narrative (status / opportunities / risks / suggested actions), emails it
via erp.emailer, and writes it to daily_digest_latest.json so
dashboard/script_center.py can render the latest digest on the page.

Meant to run once a day (see run_daily_digest.bat - schedule it with Windows
Task Scheduler, e.g. every morning at 07:00).

Run:
    venv\\Scripts\\python.exe -m erp.daily_digest             (generates + emails)
    venv\\Scripts\\python.exe -m erp.daily_digest --no-email  (generates only; what the ERP Desk app uses)
"""
import argparse
import json
import logging
import sys
import traceback
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import text

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from erp import ai_client, emailer
from erp.db import SessionLocal

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HISTORY_FILE = PROJECT_ROOT / "daily_digest_history.json"
LATEST_FILE = PROJECT_ROOT / "daily_digest_latest.json"
LOG_FILE = PROJECT_ROOT / "daily_digest.log"

# A FileHandler (not just the console) matters here: this job is meant to run
# unattended via run_daily_digest.bat under Windows Task Scheduler, where
# nobody is watching a console. Without a persistent log file, a failure that
# happens before the failure email can be sent (see send_failure_alert below)
# would leave zero trace anywhere.
def _setup_logging() -> None:
    """Attach the console + daily_digest.log handlers. Only done when this file is RUN as a job:
    doing it at import time redirected the root logger of every importing process (e.g. the
    Script Center's Streamlit server) into daily_digest.log and polluted it with unrelated errors."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(LOG_FILE, encoding="utf-8")],
    )


logger = logging.getLogger(__name__)

METRIC_LABELS = {
    "total_leads": "Total leads",
    "sla_breached_leads": "Leads breaching SLA",
    "sla_breached_tickets": "Tickets breaching SLA",
    "clickup_unsynced_leads": "Leads not synced to ClickUp",
    "leads_not_analyzed": "Leads not yet analyzed by AI",
    "open_tickets": "Open/in-progress tickets",
    "avg_potential_score": "Avg. lead potential score",
}


# ---------------------------------------------------------------------------
# 1) Gather today's metrics from the live DB (same spirit as erp/daily_check.py
#    and erp/html_report.py - reused/adapted queries, read-only).
# ---------------------------------------------------------------------------
def gather_metrics() -> dict:
    with SessionLocal() as session:
        total_leads = session.execute(text("SELECT count(*) FROM leads")).scalar_one()

        sla_breached_leads = session.execute(text(
            "SELECT count(*) FROM v_leads_summary WHERE sla_breached"
        )).scalar_one()

        sla_breached_tickets = session.execute(text(
            "SELECT count(*) FROM v_tickets_summary WHERE sla_breached AND status NOT IN ('resolved', 'closed')"
        )).scalar_one()

        clickup_unsynced_leads = session.execute(text(
            "SELECT count(*) FROM v_leads_summary WHERE sync_status IS DISTINCT FROM 'synced'"
        )).scalar_one()

        leads_not_analyzed = session.execute(text("""
            SELECT count(*) FROM leads l
            LEFT JOIN lead_ai_analysis a ON a.lead_id = l.id
            WHERE a.id IS NULL
        """)).scalar_one()

        open_tickets = session.execute(text(
            "SELECT count(*) FROM v_tickets_summary WHERE status IN ('open', 'in_progress', 'pending')"
        )).scalar_one()

        avg_potential_score = session.execute(text(
            "SELECT avg(potential_score) FROM v_leads_summary WHERE potential_score IS NOT NULL"
        )).scalar_one()

    return {
        "total_leads": int(total_leads or 0),
        "sla_breached_leads": int(sla_breached_leads or 0),
        "sla_breached_tickets": int(sla_breached_tickets or 0),
        "clickup_unsynced_leads": int(clickup_unsynced_leads or 0),
        "leads_not_analyzed": int(leads_not_analyzed or 0),
        "open_tickets": int(open_tickets or 0),
        "avg_potential_score": round(float(avg_potential_score), 1) if avg_potential_score is not None else None,
    }


# ---------------------------------------------------------------------------
# 2) Local one-line-per-day history file (JSON Lines: one JSON object per
#    line, keyed by date) - lets us compute day-over-day deltas without a DB
#    migration.
# ---------------------------------------------------------------------------
def load_history() -> list[dict]:
    if not HISTORY_FILE.exists():
        return []
    rows = []
    for line in HISTORY_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def save_history_entry(entry: dict) -> None:
    rows = [r for r in load_history() if r.get("date") != entry["date"]]  # upsert: replace same-day entry
    rows.append(entry)
    rows.sort(key=lambda r: r.get("date", ""))
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def find_previous_entry(rows: list[dict], today_str: str) -> dict | None:
    prior = [r for r in rows if r.get("date") and r["date"] < today_str]
    if not prior:
        return None
    return max(prior, key=lambda r: r["date"])


def compute_deltas(today_metrics: dict, previous: dict | None) -> dict:
    deltas = {}
    for key, val in today_metrics.items():
        prev_val = (previous or {}).get("metrics", {}).get(key)
        if val is None or prev_val is None:
            deltas[key] = None
        else:
            deltas[key] = round(val - prev_val, 2)
    return deltas


# ---------------------------------------------------------------------------
# 3) AI narrative: status / opportunities / risks / suggested actions
# ---------------------------------------------------------------------------
def build_narrative(today_metrics: dict, deltas: dict, previous: dict | None) -> str:
    lines = ["Today's metrics:"]
    for key, label in METRIC_LABELS.items():
        val = today_metrics.get(key)
        delta = deltas.get(key)
        delta_str = "n/a (no data from yesterday)" if delta is None else f"{'+' if delta >= 0 else ''}{delta} vs. yesterday"
        lines.append(f"- {label}: {val} ({delta_str})")

    prompt = (
        "You are an operations lead for a B2B Customer Support + Lead-to-Sale team. "
        "Based on today's pipeline metrics below (compared to yesterday), write a short "
        "daily digest with exactly these sections:\n"
        "1) Overall status (1-2 sentences)\n"
        "2) Notable opportunities (bullet points)\n"
        "3) Notable risks (bullet points)\n"
        "4) 2-4 concrete suggested actions (bullet points)\n\n"
        + "\n".join(lines)
    )
    return ai_client.chat(prompt)


# ---------------------------------------------------------------------------
# 4) Orchestration
# ---------------------------------------------------------------------------
def run_daily_digest(send_email: bool = True) -> dict:
    """Build today's digest. `send_email=False` (CLI: --no-email) still gathers
    metrics, calls the AI and writes daily_digest_latest.json, but sends nothing;
    the ERP Desk app uses that so opening the app / clicking its button never
    emails the team (the scheduled run_daily_digest.bat keeps emailing)."""
    today_str = date.today().isoformat()
    metrics = gather_metrics()

    history = load_history()
    previous = find_previous_entry(history, today_str)
    deltas = compute_deltas(metrics, previous)

    narrative = build_narrative(metrics, deltas, previous)

    save_history_entry({"date": today_str, "metrics": metrics})

    digest = {
        "date": today_str,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "metrics": metrics,
        "deltas": deltas,
        "previous_date": previous.get("date") if previous else None,
        "narrative": narrative,
        "ai_configured": ai_client.is_configured(),
    }

    with open(LATEST_FILE, "w", encoding="utf-8") as f:
        json.dump(digest, f, ensure_ascii=False, indent=2)

    body_html_rows = "".join(
        f"<tr><td style='padding:4px 10px'>{METRIC_LABELS[k]}</td>"
        f"<td style='padding:4px 10px;text-align:right'><b>{v}</b></td>"
        f"<td style='padding:4px 10px;text-align:right;color:#6b6f76'>"
        f"{'n/a' if deltas[k] is None else (('+' if deltas[k] >= 0 else '') + str(deltas[k]))}</td></tr>"
        for k, v in metrics.items()
    )
    body_html = f"""
    <div style="font-family:sans-serif">
      <h2 style="font-family:Georgia,serif">Daily Digest - erp_support ({today_str})</h2>
      <table style="border-collapse:collapse">{body_html_rows}</table>
      <pre style="white-space:pre-wrap;font-family:inherit;margin-top:16px">{narrative}</pre>
    </div>
    """
    if send_email:
        sent = emailer.send_email(f"[ERP] Daily Digest - {today_str}", body_html)
    else:
        sent = False
        logger.info("--no-email: digest written locally, no email sent.")
    digest["email_sent"] = sent
    digest["email_skipped"] = not send_email
    with open(LATEST_FILE, "w", encoding="utf-8") as f:
        json.dump(digest, f, ensure_ascii=False, indent=2)

    return digest


# ---------------------------------------------------------------------------
# 5) Failure alert - this is the path that actually matters for "email me on
#    error", because it is the one nobody is watching live: the unattended
#    run fired by Windows Task Scheduler via run_daily_digest.bat. If the DB
#    is unreachable, the AI call fails, or anything else in run_daily_digest()
#    raises, we still want a human notified by email instead of a silent
#    crash with only an unread traceback in the Task Scheduler history.
# ---------------------------------------------------------------------------
def send_failure_alert(exc: BaseException) -> bool:
    """Best-effort failure email. Must never raise - this already runs from
    inside an except block, and it is the last line of defense before the
    process exits, so a bug here must not turn into a second unhandled crash."""
    try:
        today_str = date.today().isoformat()
        tb = traceback.format_exc()
        subject = f"[ERP] Daily Digest FAILED - {today_str}"
        body_html = f"""
        <div style="font-family:sans-serif">
          <h2 style="font-family:Georgia,serif;color:#e0393e">Daily digest job failed</h2>
          <p>The scheduled daily digest (<code>erp/daily_digest.py</code>, run via
          <code>run_daily_digest.bat</code> / Windows Task Scheduler) raised an unhandled
          exception and did not complete. No metrics were gathered or, if the failure
          happened later, the digest may be incomplete.</p>
          <p><b>Error:</b> {type(exc).__name__}: {exc}</p>
          <pre style="white-space:pre-wrap;background:#f5f3ef;padding:10px;border-radius:8px;font-size:12px">{tb}</pre>
          <p>See {LOG_FILE.name} on the host for the full log.</p>
        </div>
        """
        return emailer.send_email(subject, body_html)
    except Exception:
        logger.exception("send_failure_alert itself raised - giving up on notifying by email.")
        return False


if __name__ == "__main__":
    _setup_logging()
    parser = argparse.ArgumentParser(description="Generate today's daily digest (metrics + AI narrative).")
    parser.add_argument("--no-email", action="store_true",
                        help="Do not send the digest email (or the failure-alert email); only write "
                             "daily_digest_latest.json. Used by the ERP Desk app.")
    cli_args = parser.parse_args()
    try:
        result = run_daily_digest(send_email=not cli_args.no_email)
    except Exception as exc:
        # Catches everything in the call chain: gather_metrics()'s raw
        # SQLAlchemy queries (e.g. DB unreachable), history file I/O, the
        # AI narrative call, and the digest/email writeback - so an
        # unattended run always ends with a log entry and a best-effort
        # email instead of a bare unhandled traceback.
        logger.exception("Daily digest failed with an unhandled exception.")
        if cli_args.no_email:
            logger.error("Failure alert email skipped (--no-email).")
        else:
            alert_sent = send_failure_alert(exc)
            logger.error(
                "Failure alert email %s.",
                "sent" if alert_sent else "NOT sent (SMTP likely not configured - see error above)",
            )
        sys.exit(1)

    print("\n" + "=" * 60)
    print(f"DAILY DIGEST - {result['date']}")
    print("=" * 60)
    for k, v in result["metrics"].items():
        print(f"  {METRIC_LABELS[k]}: {v} (delta: {result['deltas'][k]})")
    print("\n--- Narrative ---")
    print(result["narrative"])
    print(f"\nEmail sent: {result['email_sent']}" + (" (skipped: --no-email)" if result.get("email_skipped") else ""))
    print(f"Written to: {LATEST_FILE}")
