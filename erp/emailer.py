"""
Minimal SMTP email sender used for two things in this project:
  1) Script Center error alerts (dashboard/script_center.py) - "email me on failure"
  2) The daily digest (erp/daily_digest.py)

Uses only Python's stdlib (smtplib + email.mime) - no new pip dependency.
If SMTP_HOST isn't configured, logs a warning and skips sending instead of
raising (same fallback philosophy already used for ClickUp/DeepSeek elsewhere
in this codebase - never crash the caller just because email isn't set up).

Run directly for a quick manual test:
    venv\\Scripts\\python.exe -m erp.emailer
"""
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from erp.config import (
    ALERT_EMAIL_FROM,
    ALERT_EMAIL_TO,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_USER,
)

logger = logging.getLogger(__name__)


def is_configured() -> bool:
    return bool(SMTP_HOST and ALERT_EMAIL_FROM and ALERT_EMAIL_TO)


def send_email(subject: str, body_html: str) -> bool:
    """Send an HTML email. Returns True if sent, False if skipped/failed.
    Never raises - callers (Script Center, daily digest) must keep working
    even when email isn't configured or the SMTP call fails."""
    if not SMTP_HOST:
        logger.warning("SMTP_HOST is not configured - skipping email send. Subject: %s", subject)
        return False
    if not ALERT_EMAIL_FROM or not ALERT_EMAIL_TO:
        logger.warning("ALERT_EMAIL_FROM/ALERT_EMAIL_TO is not configured - skipping email send.")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = ALERT_EMAIL_FROM
    msg["To"] = ALERT_EMAIL_TO
    msg.attach(MIMEText(body_html, "html", "utf-8"))

    recipients = [addr.strip() for addr in ALERT_EMAIL_TO.split(",") if addr.strip()]

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
            server.ehlo()
            try:
                server.starttls()
                server.ehlo()
            except smtplib.SMTPNotSupportedError:
                pass  # some local/relay servers don't support STARTTLS - send in plaintext
            if SMTP_USER and SMTP_PASSWORD:
                server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(ALERT_EMAIL_FROM, recipients, msg.as_string())
        logger.info("Email sent: %s -> %s", subject, ALERT_EMAIL_TO)
        return True
    except Exception as e:
        logger.error("Failed to send email (%s): %s", subject, e)
        return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    if not is_configured():
        print("SMTP_HOST / ALERT_EMAIL_FROM / ALERT_EMAIL_TO not fully configured in .env - nothing to test with real sending.")
        print("Calling send_email() anyway to confirm it degrades gracefully...")
    ok = send_email("Script Center - test email", "<p>This is a test email from <b>erp/emailer.py</b>.</p>")
    print("Sent." if ok else "Not sent (see log above).")
