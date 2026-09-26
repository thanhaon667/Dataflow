"""Where the marketing tables live, whether they are installed, and the monthly partitions of the fact.

The DDL itself is db/sql/07_marketing_schema.sql - this module only knows the names, checks that they
are there, and creates the `interaction_fact` partition a batch needs before the batch is loaded.

Run: imported by erp/marketing/pipeline.py; nothing here runs on import.
"""
from __future__ import annotations

import logging
import re
from datetime import date

from sqlalchemy import text

logger = logging.getLogger(__name__)

FACT_TABLE = "interaction_fact"
LANDING_TABLE = "marketing_landing"
RUN_TABLE = "marketing_ingest_run"

# Every object db/sql/07_marketing_schema.sql creates, in dependency order (used by the "is it
# installed?" check and by the docs; NOT used to create anything - the .sql file is the only DDL).
MARKETING_TABLES = (
    LANDING_TABLE, RUN_TABLE, "marketing_source", "marketing_channel",
    "marketing_campaign", "marketing_creative", "marketing_identity", FACT_TABLE,
)

_IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class SchemaError(RuntimeError):
    """The marketing tables are not usable in this database (not installed, or a bad schema name)."""


def check_identifier(name: str) -> str:
    """A schema name comes from .env or the command line, never from a request - but it is still
    formatted into SQL text (an identifier cannot be a bind parameter), so it is checked, not trusted."""
    n = (name or "").strip().casefold()
    if not _IDENT.match(n):
        raise SchemaError(f"{name!r} is not a usable schema name (lower case letters, digits and _ only)")
    return n


def qualified(schema: str, table: str) -> str:
    """'public', 'interaction_fact' -> 'public.interaction_fact' (both parts already checked)."""
    return f"{check_identifier(schema)}.{check_identifier(table)}"


def missing_tables(conn, schema: str) -> list[str]:
    """Which of MARKETING_TABLES this database does not have. Cheap: to_regclass never raises."""
    schema = check_identifier(schema)
    rows = conn.execute(
        text("SELECT t AS name, to_regclass(:s || '.' || t) IS NULL AS missing FROM unnest(CAST(:names AS text[])) AS t"),
        {"s": schema, "names": list(MARKETING_TABLES)},
    ).mappings().all()
    return [r["name"] for r in rows if r["missing"]]


def require_installed(conn, schema: str) -> None:
    missing = missing_tables(conn, schema)
    if missing:
        raise SchemaError(
            f"the marketing tables are not installed in schema '{schema}' (missing: {', '.join(missing)}). "
            f"Install them with: psql -U erp_app -d erp_support -f db/sql/07_marketing_schema.sql"
        )


PSQL_10 = (r'& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U erp_app -h 127.0.0.1 -p 5432 -d erp_support '
           r'-f db\sql\10_marketing_currency.sql')


def has_currency_column(conn, schema: str) -> bool:
    """Does the fact table of `schema` have the `currency` column that db/sql/10_marketing_currency.sql adds?"""
    schema = check_identifier(schema)
    return bool(conn.execute(text(
        "SELECT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass(:q) AND attname = 'currency' "
        "AND attnum > 0 AND NOT attisdropped)"), {"q": f"{schema}.{FACT_TABLE}"}).scalar())


def require_currency_column(conn, schema: str) -> None:
    """The pipeline writes, and the rollup job groups by, interaction_fact.currency: refuse to run without it, with the fix."""
    if not has_currency_column(conn, schema):
        raise SchemaError(
            f"the fact table in schema '{schema}' has no `currency` column (db/sql/10_marketing_currency.sql adds it; it is "
            f"additive and safe to run on an installed table). Run it once as the database owner, in PowerShell from the "
            f"project folder: {PSQL_10}")


def month_start(d: date) -> date:
    return date(d.year, d.month, 1)


def next_month(d: date) -> date:
    return date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1)


def partition_name(d: date) -> str:
    return f"{FACT_TABLE}_{d.year:04d}_{d.month:02d}"


def ensure_partitions(conn, schema: str, days) -> list[str]:
    """Create the monthly partitions the given event dates need. Returns the ones that were created.

    Called once per batch, BEFORE the fact rows are written. There is no DEFAULT partition on purpose
    (it would have to be scanned by every range query), so this is what keeps a real batch from ever
    hitting "no partition of relation ... found" halfway through a load.
    """
    schema = check_identifier(schema)
    created: list[str] = []
    for m in sorted({month_start(d) for d in days if d is not None}):
        name = partition_name(m)
        if conn.execute(text("SELECT to_regclass(:q) IS NOT NULL"), {"q": f"{schema}.{name}"}).scalar():
            continue
        conn.execute(text(
            f'CREATE TABLE IF NOT EXISTS {schema}.{name} PARTITION OF {schema}.{FACT_TABLE} '
            f"FOR VALUES FROM ('{m.isoformat()}') TO ('{next_month(m).isoformat()}')"
        ))
        created.append(name)
        logger.info("created fact partition %s.%s", schema, name)
    return created


def partitions(conn, schema: str) -> list[str]:
    """Every partition of the fact, oldest first (for the docs, the perf check and the Data Flow numbers)."""
    schema = check_identifier(schema)
    rows = conn.execute(text("""
        SELECT c.relname AS name
        FROM pg_inherits i
        JOIN pg_class c ON c.oid = i.inhrelid
        WHERE i.inhparent = to_regclass(:parent)
        ORDER BY c.relname
    """), {"parent": f"{schema}.{FACT_TABLE}"}).mappings().all()
    return [r["name"] for r in rows]
