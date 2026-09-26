import os

from dotenv import load_dotenv

load_dotenv(override=True)  # the project's .env always takes precedence over system env vars

DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "erp_support")
DB_USER = os.getenv("DB_USER", "erp_app")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")

CLICKUP_API_TOKEN = os.getenv("CLICKUP_API_TOKEN", "")
CLICKUP_LIST_ID = os.getenv("CLICKUP_LIST_ID", "")

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_API_URL = os.getenv("DEEPSEEK_API_URL", "https://api.deepseek.com")

LEAD_SLA_HOURS = float(os.getenv("LEAD_SLA_HOURS", "5"))  # lead handling SLA, counted in business hours

# Email alerts (Script Center error alerts + daily digest). Leave SMTP_HOST blank
# to disable sending - callers must log a warning and skip instead of crashing
# (same fallback philosophy as ClickUp/DeepSeek above).
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587") or "587")
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
ALERT_EMAIL_FROM = os.getenv("ALERT_EMAIL_FROM", "")
ALERT_EMAIL_TO = os.getenv("ALERT_EMAIL_TO", "")

# ERP Desk - the local "desktop app" shell (desktop/). Everything binds to
# 127.0.0.1 only. If a preferred port is taken by another program the launcher
# falls back to any free port automatically, so these are preferences.
def _int_env(name: str, default: int) -> int:
    """A typo in an optional .env value must never break unrelated scripts that import this module."""
    try:
        return int((os.getenv(name) or "").strip() or default)
    except ValueError:
        return default


DESKTOP_PORT = _int_env("DESKTOP_PORT", 47650)                        # shell + live report server
DESKTOP_STREAMLIT_PORT = _int_env("DESKTOP_STREAMLIT_PORT", 47651)    # embedded Script Center
DESKTOP_REFRESH_SECONDS = max(3, _int_env("DESKTOP_REFRESH_SECONDS", 10))  # live report DB re-query interval
DESKTOP_AUTO_DIGEST = os.getenv("DESKTOP_AUTO_DIGEST", "1").strip().lower() in ("1", "true", "yes", "on")  # generate today's digest once per day

# Marketing / channel interaction data (erp/marketing/, db/sql/07_marketing_schema.sql).
# MARKETING_SCHEMA is normally "public"; the scale verification points it at a throwaway schema.
MARKETING_SCHEMA = (os.getenv("MARKETING_SCHEMA", "public") or "public").strip().lower() or "public"
MARKETING_BATCH_SIZE = max(100, _int_env("MARKETING_BATCH_SIZE", 5000))   # records per transaction while ingesting
# Per-run budget of rows that may be isolated at the SQL stage before the run aborts as "systematic" (bad privilege,
# missing partition, schema mismatch) instead of bisecting every remaining chunk down to single rows.
MARKETING_MAX_ISOLATED_ROWS = max(1, _int_env("MARKETING_MAX_ISOLATED_ROWS", 50))
# Phase 4 partition maintenance (erp/marketing/partitions.py): how many months ahead of today the job pre-creates
# interaction_fact partitions, and how old a monthly partition must be before it is REPORTED as a retention candidate
# (it is never dropped by the job - that is the owner's decision).
MARKETING_PARTITIONS_AHEAD = min(60, max(0, _int_env("MARKETING_PARTITIONS_AHEAD", 3)))
MARKETING_RETENTION_MONTHS = max(1, _int_env("MARKETING_RETENTION_MONTHS", 24))

# Marketing inbox processor (erp/marketing/autorun.py): the folder of CSV exports it processes, with processed/ and failed/
# created next to it on demand. A relative path is taken from the project root. Git-ignored (data_inbox/); unset = default.
MARKETING_INBOX = (os.getenv("MARKETING_INBOX", "") or "").strip() or os.path.join("data_inbox", "incoming")

# Seconds a NEW database connection may take before erp/db.py gives up (libpq connect_timeout). Without it a hung (not refused)
# PostgreSQL, e.g. a stalled port forward or a frozen host, would block a caller forever. Existing connections are unaffected.
DB_CONNECT_TIMEOUT = max(1, _int_env("DB_CONNECT_TIMEOUT", 5))


def database_url() -> str:
    return (
        f"postgresql+psycopg2://{DB_USER}:{DB_PASSWORD}"
        f"@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    )
