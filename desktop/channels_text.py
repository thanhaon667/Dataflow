"""Channels page, wording: date-range text, the page states and the install / empty-state help with the owner-run commands (split out of desktop/channels_data.py, unchanged).
"""
from __future__ import annotations

from datetime import date

from erp.marketing import schema as mschema
from desktop.channels_money import MEASURES


PSQL_07 = r'& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U erp_app -h 127.0.0.1 -p 5432 -d erp_support -f db\sql\07_marketing_schema.sql'
PSQL_09 = r'& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U erp_app -h 127.0.0.1 -p 5432 -d erp_support -f db\sql\09_marketing_rollup.sql'
PSQL_10 = mschema.PSQL_10
PSQL_NOTE = "Run it in PowerShell from the project folder (it asks for the erp_app password); change the psql path if PostgreSQL is installed elsewhere."
CMD_LOAD = r"venv\Scripts\python.exe -m erp.marketing.ingest --csv <file>"
LOAD_NOTE = ("Connectors (add --connector NAME): flat_file (the default, any CSV; --currency sets its currency), ad_performance (paid-social weekly export) and "
             "email_campaign (e-mail daily export). Add --dry-run first to see what would load. A real source (ads API, GA4) is a later phase.")
CMD_ROLLUP = r"venv\Scripts\python.exe -m erp.marketing.rollup --full --yes"

STATE_NOT_INSTALLED, STATE_EMPTY, STATE_NO_MATCH, STATE_READY = "not_installed", "empty", "no_match", "ready"

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def day_text(d: date) -> str:
    """26 Aug 2026 - month names are spelled here, not by strftime, so the sentence never depends on the machine's locale."""
    return f"{d.day} {_MONTHS[d.month - 1]} {d.year}"


def range_text(a: date, b: date) -> str:
    """'on 24 Sep 2026' / 'from 26 Aug to 24 Sep 2026' / 'from 26 Dec 2025 to 24 Jan 2026' (never an ISO date that wraps at its hyphens)."""
    if a == b:
        return f"on {day_text(a)}"
    if a.year == b.year:
        return f"from {a.day} {_MONTHS[a.month - 1]} to {day_text(b)}"
    return f"from {day_text(a)} to {day_text(b)}"


# ============================================================================ page state (pure)
def page_state(missing: list[str], rollup_rows: int | None, totals: dict | None) -> str:
    """not_installed | empty | no_match | ready - decided from what the database really holds."""
    if missing:
        return STATE_NOT_INSTALLED
    if not rollup_rows:
        return STATE_EMPTY
    if totals is not None and not any(totals.get(m) for m in MEASURES) and not totals.get("days_with_data"):
        return STATE_NO_MATCH
    return STATE_READY


def install_help(missing: list[str], schema: str, currency_ok: bool | None = None) -> dict:
    """The exact, owner-run fix for a missing table: 07 first when the marketing tables themselves are absent."""
    base_missing = [t for t in missing if t in mschema.MARKETING_TABLES]
    steps = []
    if base_missing:
        steps.append({"title": "Install the marketing tables (Phase 1)", "command": PSQL_07, "note": PSQL_NOTE})
    if currency_ok is not True:
        steps.append({"title": "Add the currency column to the fact table (migration 10)", "command": PSQL_10,
                      "note": ("" if steps else PSQL_NOTE + " ") + "Additive and safe to run again (skip it if it already ran). Nothing is converted: every money row keeps the currency "
                              "its source reported. Run it before the rollup tables and before loading data."})
    steps.append({"title": "Install the rollup tables (Phase 4)", "command": PSQL_09, "note": "" if steps else PSQL_NOTE})
    steps.append({"title": "Load data - nothing is wired in yet", "command": CMD_LOAD,
                  "note": LOAD_NOTE})
    steps.append({"title": "Build the rollup", "command": CMD_ROLLUP,
                  "note": "Then keep it current by scheduling run_marketing_rollup.bat - that is the owner's decision."})
    return {"missing": list(missing), "schema": schema, "steps": steps,
            "note": ("The page reads the schema named by MARKETING_SCHEMA (here '" + schema + "'). "
                     + ("" if schema == "public" else "The commands above install into 'public'; set the search_path first for another schema. "))
                    + "Nothing here is installed, run or changed by this page."}


def empty_help(schema: str, currency_ok: bool | None = None) -> dict:
    steps = []
    if currency_ok is False:
        steps.append({"title": "Add the currency column to the fact table (migration 10) - loading is refused without it", "command": PSQL_10,
                      "note": PSQL_NOTE + " Additive and safe to run again."})
    steps += [{"title": "Load data - no data has been loaded yet", "command": CMD_LOAD, "note": LOAD_NOTE},
              {"title": "Build the rollup", "command": CMD_ROLLUP,
               "note": "The page reads the rollup, not the raw table: a load shows up here after this runs."}]
    return {"missing": [], "schema": schema, "steps": steps, "note": "Nothing here is loaded, run or changed by this page."}


# ============================================================================ headline (pure)
def _metric_word(metric: str, n: int) -> str:
    return {"sessions": "session", "clicks": "click", "conversions": "conversion"}[metric] + ("" if n == 1 else "s")
