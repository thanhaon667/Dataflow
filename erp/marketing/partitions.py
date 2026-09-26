"""Partition maintenance for `interaction_fact`: create the next months ahead of time, and REPORT (never act on) old ones.

Two jobs, both deliberately small:

  * LOOK-AHEAD   make sure the current month and the next N months have a partition, so a scheduled load never
                 hits "no partition of relation interaction_fact found for row" (there is no DEFAULT partition on
                 purpose, see db/sql/07_marketing_schema.sql). Creating an empty partition is additive and cheap.
  * RETENTION    list the partitions older than the documented retention threshold (MARKETING_RETENTION_MONTHS,
                 default 24 months) together with their size, and print the EXACT commands that would detach and
                 drop them. It never runs them: removing fact data is destructive, needs a backup/archive decision,
                 and that decision belongs to the owner. This module has no DROP / DETACH execution path at all.

The month arithmetic and the candidate selection are pure functions (tests/marketing_scenarios.py covers them
without a database); only ensure_lookahead() and list_partitions() touch one.

Run: called by erp/marketing/rollup.py (python -m erp.marketing.rollup, or --partitions-only for just this step).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import text

from erp.marketing import schema as mschema

logger = logging.getLogger(__name__)

DEFAULT_AHEAD_MONTHS = 3
DEFAULT_RETENTION_MONTHS = 24
MAX_AHEAD_MONTHS = 60          # a typo (--partitions-ahead 3000) must not create 250 years of tables

_PARTITION_RE = re.compile(rf"^{mschema.FACT_TABLE}_(\d{{4}})_(\d{{2}})$")


# --------------------------------------------------------------------------- pure logic
def add_months(d: date, n: int) -> date:
    """First day of the month n months after (or, for negative n, before) d's month."""
    idx = d.year * 12 + (d.month - 1) + n
    return date(idx // 12, idx % 12 + 1, 1)


def lookahead_months(today: date, ahead: int) -> list[date]:
    """The month starts that must have a partition: this month plus the next `ahead` months."""
    if ahead < 0:
        raise ValueError("ahead must be 0 or more")
    return [add_months(today, i) for i in range(ahead + 1)]


def parse_partition(name: str) -> date | None:
    """'interaction_fact_2026_09' -> date(2026, 9, 1); anything that is not a monthly partition name -> None."""
    m = _PARTITION_RE.match(name or "")
    if not m:
        return None
    year, month = int(m.group(1)), int(m.group(2))
    if not 1 <= month <= 12:
        return None
    return date(year, month, 1)


def retention_cutoff(today: date, retention_months: int) -> date:
    """Data strictly before this day is past retention: the current month and the `retention_months` full
    months before it are kept. (today 2026-09-25, 24 months -> 2024-09-01.)"""
    if retention_months < 1:
        raise ValueError("retention_months must be 1 or more")
    return add_months(today, -retention_months)


def retention_candidates(names: list[str], today: date, retention_months: int) -> list[tuple[str, date]]:
    """The monthly partitions that lie ENTIRELY before the cutoff, oldest first. A partition whose month is not
    fully past the cutoff is never a candidate, and a name that is not a monthly partition is ignored."""
    cutoff = retention_cutoff(today, retention_months)
    out = []
    for name in names:
        month = parse_partition(name)
        if month is not None and mschema.next_month(month) <= cutoff:
            out.append((name, month))
    return sorted(out, key=lambda x: x[1])


def detach_commands(schema: str, name: str) -> list[str]:
    """The exact statements an owner would run - returned as text, never executed by this module."""
    schema = mschema.check_identifier(schema)
    name = mschema.check_identifier(name)
    return [
        f"ALTER TABLE {schema}.{mschema.FACT_TABLE} DETACH PARTITION {schema}.{name} CONCURRENTLY;",
        f"DROP TABLE {schema}.{name};   -- only after the detached table has been archived or you accept losing it",
    ]


# --------------------------------------------------------------------------- database side
@dataclass
class PartitionReport:
    schema: str
    today: date
    ahead: int
    retention_months: int
    created: list[str] = field(default_factory=list)
    existing: list[str] = field(default_factory=list)
    candidates: list[dict] = field(default_factory=list)   # name, month, rows_estimate, bytes, commands

    def lines(self) -> list[str]:
        out = [f"partitions: {len(self.existing)} present, {len(self.created)} created now "
               f"(this month + {self.ahead} ahead)"
               + (f": {', '.join(self.created)}" if self.created else "")]
        cutoff = retention_cutoff(self.today, self.retention_months)
        if not self.candidates:
            out.append(f"retention: no partition is older than {self.retention_months} months (cutoff {cutoff}); nothing to do")
            return out
        out.append(f"retention: {len(self.candidates)} partition(s) lie entirely before {cutoff} "
                   f"(retention = {self.retention_months} months). NOTHING WAS DETACHED OR DROPPED - "
                   f"that is the owner's decision. To remove one, after archiving it if needed:")
        for c in self.candidates:
            out.append(f"  {c['name']}  ~{c['rows_estimate']:,} rows, {c['bytes'] / 1e6:,.0f} MB")
            for cmd in c["commands"]:
                out.append(f"      {cmd}")
        out.append("  (the rollup rows for those days are kept: a fact partition being dropped does not touch "
                   "interaction_daily_rollup, and --full never deletes rollup days that have no fact partition any more)")
        return out


def list_partitions(conn, schema: str) -> list[dict]:
    """Every partition of the fact with its estimated row count and total size. reltuples is the planner's
    estimate (kept fresh by autovacuum/ANALYZE): a report line does not deserve a count(*) over millions of rows."""
    schema = mschema.check_identifier(schema)
    rows = conn.execute(text("""
        SELECT c.relname AS name, GREATEST(c.reltuples, 0)::bigint AS rows_estimate,
               pg_total_relation_size(c.oid) AS bytes
          FROM pg_inherits i
          JOIN pg_class c ON c.oid = i.inhrelid
         WHERE i.inhparent = to_regclass(:parent)
         ORDER BY c.relname
    """), {"parent": f"{schema}.{mschema.FACT_TABLE}"}).mappings().all()
    return [dict(r) for r in rows]


def ensure_lookahead(conn, schema: str, today: date, ahead: int) -> list[str]:
    """Create the missing partitions of this month and the next `ahead` months; returns the names created.
    Idempotent (mschema.ensure_partitions checks first), additive, and does not commit - the caller does."""
    if ahead > MAX_AHEAD_MONTHS:
        raise ValueError(f"--partitions-ahead {ahead} is more than the {MAX_AHEAD_MONTHS}-month sanity limit")
    return mschema.ensure_partitions(conn, schema, lookahead_months(today, ahead))


def maintain(conn, schema: str, today: date | None = None, ahead: int = DEFAULT_AHEAD_MONTHS,
             retention_months: int = DEFAULT_RETENTION_MONTHS) -> PartitionReport:
    """Look-ahead creation + retention report, in that order. Never detaches or drops anything."""
    today = today or date.today()
    schema = mschema.check_identifier(schema)
    report = PartitionReport(schema=schema, today=today, ahead=ahead, retention_months=retention_months)
    report.created = ensure_lookahead(conn, schema, today, ahead)
    conn.commit()
    infos = list_partitions(conn, schema)
    report.existing = [i["name"] for i in infos]
    by_name = {i["name"]: i for i in infos}
    for name, month in retention_candidates(report.existing, today, retention_months):
        info = by_name[name]
        report.candidates.append({"name": name, "month": month, "rows_estimate": int(info["rows_estimate"]),
                                  "bytes": int(info["bytes"]), "commands": detach_commands(schema, name)})
    return report
