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


def database_url() -> str:
    return (
        f"postgresql+psycopg2://{DB_USER}:{DB_PASSWORD}"
        f"@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    )
