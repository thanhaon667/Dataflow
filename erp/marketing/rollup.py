"""Refresh the daily channel/campaign ROLLUP of `interaction_fact`, and keep its monthly partitions ahead of the data.

A report page must never aggregate the raw fact table (3.36 s at 3M rows, growing with history - see
docs/marketing-data-architecture.md s.5 and s.8). It reads `interaction_daily_rollup` instead: one row per
(day, channel, campaign) carrying additive counts and sums only. This job keeps that table current.

What one run does, in order (each step is independent: one failing does not stop the next, the exit code says so):

  1. PARTITIONS  create this month and the next few months of `interaction_fact` (additive), so a scheduled load
                 never hits "no partition found".
  2. ROLLUP      recompute the days that need it - the last `--days` days, plus any older day whose facts were
                 loaded or restated since the last successful run - as DELETE that day range + INSERT ... SELECT
                 ... GROUP BY inside one transaction per range. Partition pruning confines each range to one or
                 two monthly partitions, so the cost follows the days recomputed, not the history.
  2b. PLACEMENTS the same ranges again for `interaction_placement_rollup` (db/sql/11, OPTIONAL: skipped with one log line when it is not
                 installed; see erp/marketing/rollup_placements.py). Nothing above depends on it.
  3. RETENTION   list the fact partitions older than MARKETING_RETENTION_MONTHS and print the exact commands that
                 would detach/drop them. NOTHING is ever detached or dropped by this job.

Safe to run twice in a row (the primary key + recompute-by-range make the second run rewrite identical numbers),
safe when the fact table is empty, and it honours MARKETING_SCHEMA / --schema so it can run in a throwaway schema.

Unattended-job rules (L-013): everything is caught at the top, the run is logged to marketing_rollup.log next to
the console, the exit code is non-zero on failure, and nothing ever prompts. NOT armed anywhere: nothing
schedules this job (Task Scheduler is a system setting only the owner changes) - see the doc for the command.

Run:
  venv\\Scripts\\python.exe -m erp.marketing.rollup                      (last 3 days + any changed older day)
  venv\\Scripts\\python.exe -m erp.marketing.rollup --days 7
  venv\\Scripts\\python.exe -m erp.marketing.rollup --full --yes         (rebuild every day that still has facts)
  venv\\Scripts\\python.exe -m erp.marketing.rollup --partitions-only
  venv\\Scripts\\python.exe -m erp.marketing.rollup --schema perf_check  (a throwaway schema)
  run_marketing_rollup.bat                                              (the same thing, for Task Scheduler)

Exit code: 0 = every step finished, 1 = a step failed (see marketing_rollup.log), 2 = bad command line.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import text

from erp.marketing import partitions as mpart
from erp.marketing import schema as mschema

logger = logging.getLogger("erp.marketing.rollup")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LOG_FILE = PROJECT_ROOT / "marketing_rollup.log"

ROLLUP_TABLE = "interaction_daily_rollup"
RUN_TABLE = "marketing_rollup_run"
CHANNEL_TABLE = "interaction_daily_channel_rollup"
ROLLUP_TABLES = (ROLLUP_TABLE, CHANNEL_TABLE, RUN_TABLE)

# The additive columns, in the order both rollup tables carry them.
EVENT_COLUMNS = ("events_total", "events_impression", "events_click", "events_reaction", "events_session",
                 "events_conversion", "events_other")
SUM_COLUMNS = ("impressions", "clicks", "reactions", "sessions", "conversions", "spend_micros", "revenue_micros",
               "revenue_derived_micros")
MEASURE_COLUMNS = EVENT_COLUMNS + SUM_COLUMNS

DEFAULT_DAYS = 3
MAX_DAYS = 3660                   # ten years: a bigger number is a typo, and --full exists for "everything"
TOUCHED_OVERLAP = timedelta(hours=1)   # safety margin under the watermark: a load that started before the last run
                                       # but committed after it carries a loaded_at older than that run's start
LOCK_TIMEOUT_MS = 60_000          # a second refresh waits at most a minute for the first
STATEMENT_TIMEOUT_MS = 30 * 60_000

# The rollup's `grain` comes from the fact's attrs: a weekly export row carries attrs.grain = 'week' and sits on the week's START
# day, so its numbers cover 7 days; every other row (no grain key, 'day', anything else) is a day. Deliberately a two-value
# CASE, never the raw attrs text, so a stray value cannot invent a third grain (the DDL has a CHECK on it too).
GRAIN_SQL = "CASE WHEN attrs->>'grain' = 'week' THEN 'week' ELSE 'day' END"

# event_type values that get their own column; everything else counts as events_other
NAMED_EVENT_TYPES = ("impression", "click", "reaction", "session", "conversion")

# Ranges are (start, end_exclusive); end None = open ended (everything from start onwards, including future dates).
Range = tuple[date, "date | None"]


# --------------------------------------------------------------------------- pure logic
def window_start(today: date, days: int) -> date:
    """The first day of a `days`-day window that includes today (days=3 on the 25th -> the 23rd)."""
    if days < 1:
        raise ValueError("days must be 1 or more")
    return today - timedelta(days=days - 1)


def contiguous_ranges(days: list[date]) -> list[tuple[date, date]]:
    """Sorted, de-duplicated days -> closed-open ranges of consecutive days: [3,4,5,9] -> [(3,6),(9,10)]."""
    out: list[tuple[date, date]] = []
    for d in sorted(set(days)):
        if out and out[-1][1] == d:
            out[-1] = (out[-1][0], d + timedelta(days=1))
        else:
            out.append((d, d + timedelta(days=1)))
    return out


def split_at_months(start: date, end: date) -> list[tuple[date, date]]:
    """Cut a closed-open range at month boundaries so no single transaction spans more than one monthly partition."""
    out = []
    cur = start
    while cur < end:
        nxt = min(end, mschema.next_month(cur))
        out.append((cur, nxt))
        cur = nxt
    return out


def plan_ranges(today: date, days: int, touched_days: list[date] | None = None) -> list[Range]:
    """What a window-mode run recomputes: the last `days` days (open ended) plus the older days that changed.

    Touched days at or after the window start are already inside the window and are ignored; a touched run that
    ends right where the window starts is merged into it. Closed ranges are split at month boundaries."""
    ws = window_start(today, days)
    older = contiguous_ranges([d for d in (touched_days or []) if d < ws])
    plan: list[Range] = []
    for a, b in older:
        if b == ws:                       # touches the window: one range, no gap
            ws = a
        else:
            plan.extend(split_at_months(a, b))
    plan.append((ws, None))
    return plan


def full_ranges(min_day: date | None, max_day: date | None) -> list[Range]:
    """Every month from the first day that has facts onwards; the last range is open ended so a rollup row
    left behind for a day after the newest fact is cleared too. Days BEFORE min_day are deliberately left alone:
    they are the history of fact partitions that were dropped for retention, and the rollup outlives them."""
    if min_day is None or max_day is None:
        return []
    out: list[Range] = []
    cur = min_day
    last_month = mschema.month_start(max_day)
    while mschema.month_start(cur) < last_month:
        nxt = mschema.next_month(cur)
        out.append((cur, nxt))
        cur = nxt
    out.append((cur, None))
    return out


def describe_ranges(ranges: list[Range], limit: int = 6) -> str:
    def one(r: Range) -> str:
        return f"{r[0]}..{r[1] - timedelta(days=1)}" if r[1] else f"{r[0]}.. (open)"
    shown = ", ".join(one(r) for r in ranges[:limit])
    return shown + (f" (+{len(ranges) - limit} more)" if len(ranges) > limit else "")


def count_days(ranges: list[Range], today: date) -> int:
    total = 0
    for a, b in ranges:
        total += ((b - a).days if b else max(1, (today - a).days + 1))
    return total


def watermark(last_ok_started_at, overlap: timedelta = TOUCHED_OVERLAP):
    """Facts loaded at or after this instant count as 'changed since the last good run'; None = never ran."""
    return None if last_ok_started_at is None else last_ok_started_at - overlap


# --------------------------------------------------------------------------- database side
@dataclass
class RollupResult:
    schema: str
    mode: str                                   # window | window_only | full
    ranges: list = field(default_factory=list)
    days_recomputed: int = 0
    touched_days: int = 0                       # older days picked up because their facts changed
    rows_deleted: int = 0                       # campaign-level rows replaced ...
    rows_written: int = 0                       # ... by this many
    channel_rows_written: int = 0               # rows of the per-channel table written from them
    detect_seconds: float = 0.0                 # time spent finding older days whose facts changed
    seconds: float = 0.0
    note: str = ""
    placement: object = None                    # rollup_placements.PlacementResult once the optional placement step ran (None: never looked)

    def summary(self) -> str:
        return (f"{self.mode}: {self.days_recomputed} day(s) recomputed ({describe_ranges(self.ranges)}), "
                f"{self.rows_deleted} rollup row(s) replaced by {self.rows_written} "
                f"(+ {self.channel_rows_written} per-channel) in {self.seconds:.2f}s"
                + (f" (changed-day detection {self.detect_seconds:.2f}s)" if self.detect_seconds else "")
                + (f" - {self.note}" if self.note else "")
                + (f"; {self.placement.summary()}" if self.placement is not None and self.placement.installed else ""))


def _t(schema: str, table: str) -> str:
    return mschema.qualified(schema, table)


def require_rollup_installed(conn, schema: str) -> None:
    mschema.require_installed(conn, schema)
    mschema.require_currency_column(conn, schema)
    schema = mschema.check_identifier(schema)
    missing = [t for t in ROLLUP_TABLES
               if conn.execute(text("SELECT to_regclass(:q) IS NULL"), {"q": f"{schema}.{t}"}).scalar()]
    if missing:
        raise mschema.SchemaError(
            f"the rollup tables are not installed in schema '{schema}' (missing: {', '.join(missing)}). "
            f"Install them with: psql -U erp_app -d erp_support -f db/sql/09_marketing_rollup.sql")


def _arm_timeouts(conn) -> None:
    """Re-armed at the start of EVERY transaction: SET LOCAL dies with the transaction that set it (L-090)."""
    conn.execute(text("SELECT set_config('lock_timeout', :l, true), set_config('statement_timeout', :s, true)"),
                 {"l": str(LOCK_TIMEOUT_MS), "s": str(STATEMENT_TIMEOUT_MS)})


def _bounds(rng: Range, col: str = "event_date") -> tuple[str, dict]:
    sql = f"{col} >= :a"
    params: dict = {"a": rng[0]}
    if rng[1] is not None:
        sql += f" AND {col} < :b"
        params["b"] = rng[1]
    return sql, params


def refresh_range(conn, schema: str, rng: Range) -> tuple[int, int, int]:
    """Recompute one date range: DELETE the rollup rows of the range, INSERT them again from the facts, then
    rebuild the per-channel rows of the same range from the campaign rows just written.
    All statements are date-bounded, so partition pruning keeps the fact read on the partitions of that range, and
    they run in the caller's transaction: readers see the old rows or the new ones, never a half-empty range.
    Returns (campaign rows deleted, campaign rows written, channel rows written). Does not commit."""
    where, params = _bounds(rng)
    rollup, fact, chan = _t(schema, ROLLUP_TABLE), _t(schema, mschema.FACT_TABLE), _t(schema, CHANNEL_TABLE)
    # one advisory lock per schema: two refreshes of the same schema queue instead of deleting each other's rows
    conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"marketing_rollup:{schema}"})
    deleted = conn.execute(text(f"DELETE FROM {rollup} WHERE {where}"), params).rowcount
    named = ", ".join(f"'{t}'" for t in NAMED_EVENT_TYPES)
    written = conn.execute(text(f"""
        INSERT INTO {rollup}
            (event_date, channel_id, campaign_id, currency, grain,
             events_total, events_impression, events_click, events_reaction, events_session, events_conversion,
             events_other, impressions, clicks, reactions, sessions, conversions, spend_micros, revenue_micros,
             revenue_derived_micros, refreshed_at)
        SELECT event_date, channel_id, COALESCE(campaign_id, 0), currency,
               {GRAIN_SQL},
               count(*),
               count(*) FILTER (WHERE event_type = 'impression'),
               count(*) FILTER (WHERE event_type = 'click'),
               count(*) FILTER (WHERE event_type = 'reaction'),
               count(*) FILTER (WHERE event_type = 'session'),
               count(*) FILTER (WHERE event_type = 'conversion'),
               count(*) FILTER (WHERE event_type NOT IN ({named})),
               sum(impressions), sum(clicks), sum(reactions), sum(sessions), sum(conversions),
               sum(spend_micros), sum(revenue_micros),
               COALESCE(sum(revenue_micros) FILTER (WHERE attrs->>'revenue_derived' = 'true'), 0),
               now()
          FROM {fact}
         WHERE {where}
         GROUP BY event_date, channel_id, COALESCE(campaign_id, 0), currency, {GRAIN_SQL}
    """), params).rowcount
    # the channel level is summed from the campaign level: additive, so exact, and it reads the small table
    conn.execute(text(f"DELETE FROM {chan} WHERE {where}"), params)
    cols = ", ".join(MEASURE_COLUMNS)
    sums = ", ".join(f"sum({c})" for c in MEASURE_COLUMNS)
    channel_written = conn.execute(text(f"""
        INSERT INTO {chan} (event_date, channel_id, currency, grain, {cols}, refreshed_at)
        SELECT event_date, channel_id, currency, grain, {sums}, now()
          FROM {rollup}
         WHERE {where}
         GROUP BY event_date, channel_id, currency, grain
    """), params).rowcount
    return deleted, written, channel_written


def refresh_placements(conn, schema: str, ranges: list[Range]):
    """Step 2b: the optional placement rollup over the ranges the main rollup just recomputed (plus, once, every day with placement facts when the table
    is installed but empty). Returns a PlacementResult; `installed` False means db/sql/11 is not there and nothing was done."""
    from erp.marketing import rollup_placements as rp
    out = rp.PlacementResult()
    out.installed = rp.is_installed(conn, schema)
    conn.rollback()
    if not out.installed:
        logger.info("%s", out.summary())
        return out
    todo = list(ranges)
    boot = rp.needs_bootstrap(conn, schema)
    conn.rollback()
    if boot:
        todo, out.bootstrapped = full_ranges(*boot), True
    for rng in todo:
        with conn.begin():
            _arm_timeouts(conn)
            d, w = rp.refresh_range(conn, schema, rng)
        out.rows_deleted += d
        out.rows_written += w
    out.folded_placements = rp.folded_total(conn, schema)
    conn.rollback()
    return out


def last_ok_start(conn, schema: str):
    """Start of the newest successful run that looked for changed older days (the watermark's source). A
    --window-only run did not look, so it must not move the watermark: it would hide changes made before it."""
    return conn.execute(text(
        f"SELECT max(started_at) FROM {_t(schema, RUN_TABLE)} WHERE status = 'ok' AND mode <> 'window_only'")).scalar()


def touched_days(conn, schema: str, since, before: date) -> list[date]:
    """Older days (before the window) that have a fact loaded or restated since `since` (None = ever: the
    initial build). The BRIN index on loaded_at keeps this from reading every heap page."""
    fact = _t(schema, mschema.FACT_TABLE)
    sql = f"SELECT DISTINCT event_date FROM {fact} WHERE event_date < :before"
    params: dict = {"before": before}
    if since is not None:
        sql += " AND loaded_at >= :since"
        params["since"] = since
    return [r[0] for r in conn.execute(text(sql + " ORDER BY 1"), params)]


def _open_run(engine, schema: str, mode: str, days: int | None) -> int:
    with engine.begin() as conn:
        return conn.execute(text(
            f"INSERT INTO {_t(schema, RUN_TABLE)} (mode, days_requested) VALUES (:m, :d) RETURNING run_id"),
            {"m": mode, "d": days}).scalar_one()


def _close_run(engine, schema: str, run_id: int, status: str, res: RollupResult | None, error: str | None) -> None:
    """Best effort: a journal failure must not hide the real outcome, so it only logs."""
    try:
        with engine.begin() as conn:
            conn.execute(text(f"""
                UPDATE {_t(schema, RUN_TABLE)}
                   SET finished_at = now(), status = :s, error_message = :e, ranges = :r,
                       days_recomputed = :dr, rows_deleted = :rd, rows_written = :rw
                 WHERE run_id = :id"""),
                {"s": status, "e": (error or None) and error[:1000], "id": run_id,
                 "r": describe_ranges(res.ranges, 12)[:1000] if res else None,
                 "dr": res.days_recomputed if res else 0, "rd": res.rows_deleted if res else 0,
                 "rw": res.rows_written if res else 0})
    except Exception:  # noqa: BLE001
        logger.warning("could not close journal row %s", run_id, exc_info=True)


def refresh(engine, schema: str, days: int = DEFAULT_DAYS, full: bool = False,
            today: date | None = None, use_touched: bool = True) -> RollupResult:
    """Run the rollup refresh and journal it. Window mode by default; `full` rebuilds every month that has facts."""
    schema = mschema.check_identifier(schema)
    today = today or date.today()
    t0 = time.monotonic()
    mode = "full" if full else ("window" if use_touched else "window_only")
    with engine.connect() as conn:
        require_rollup_installed(conn, schema)
        since_ok = last_ok_start(conn, schema)
        conn.rollback()
    run_id = _open_run(engine, schema, mode, None if full else days)
    res = RollupResult(schema=schema, mode=mode)
    try:
        with engine.connect() as conn:
            if full:
                lo, hi = conn.execute(text(
                    f"SELECT min(event_date), max(event_date) FROM {_t(schema, mschema.FACT_TABLE)}")).one()
                conn.rollback()
                res.ranges = full_ranges(lo, hi)
                if not res.ranges:
                    res.note = "the fact table is empty: nothing to rebuild, existing rollup rows left as they are"
            else:
                older: list[date] = []
                if use_touched:
                    ws = window_start(today, days)
                    wm = watermark(since_ok)
                    t_detect = time.monotonic()
                    older = touched_days(conn, schema, wm, ws)
                    conn.rollback()
                    res.detect_seconds = time.monotonic() - t_detect
                    res.touched_days = len(older)
                    if since_ok is None:
                        res.note = "no earlier successful run: every day with facts counted as changed (initial build)"
                res.ranges = plan_ranges(today, days, older)
            res.days_recomputed = count_days(res.ranges, today)
            for rng in res.ranges:
                with conn.begin():
                    _arm_timeouts(conn)
                    t1 = time.monotonic()
                    d, w, cw = refresh_range(conn, schema, rng)
                res.rows_deleted += d
                res.rows_written += w
                res.channel_rows_written += cw
                logger.debug("range %s: %s rollup rows replaced by %s (+%s per-channel) in %.2fs",
                             describe_ranges([rng]), d, w, cw, time.monotonic() - t1)
            res.placement = refresh_placements(conn, schema, res.ranges)
        res.seconds = time.monotonic() - t0
        _close_run(engine, schema, run_id, "ok", res, None)
        return res
    except BaseException as exc:
        res.seconds = time.monotonic() - t0
        _close_run(engine, schema, run_id, "failed", res, f"{type(exc).__name__}: {exc}")
        raise


# --------------------------------------------------------------------------- steps + command line
def build_parser() -> argparse.ArgumentParser:
    from erp import config
    p = argparse.ArgumentParser(
        prog="python -m erp.marketing.rollup",
        description="Refresh the daily channel/campaign rollup of interaction_fact, pre-create the next monthly "
                    "partitions, and report (never drop) the partitions past retention.",
    )
    p.add_argument("--days", type=int, default=DEFAULT_DAYS,
                   help=f"recompute the last N days, today included (default {DEFAULT_DAYS}); older days whose facts "
                        "changed since the last good run are recomputed as well")
    p.add_argument("--window-only", action="store_true",
                   help="recompute ONLY the last --days days, do not look for changed older days")
    p.add_argument("--full", action="store_true",
                   help="rebuild the rollup for every month that still has facts (slow on a big table; needs --yes)")
    p.add_argument("--yes", action="store_true", help="confirms --full (the job never prompts)")
    p.add_argument("--schema", default=None, help=f"schema holding the marketing tables (default MARKETING_SCHEMA = {config.MARKETING_SCHEMA})")
    p.add_argument("--partitions-ahead", type=int, default=config.MARKETING_PARTITIONS_AHEAD,
                   help=f"create this month plus N more months of interaction_fact partitions (default {config.MARKETING_PARTITIONS_AHEAD})")
    p.add_argument("--retention-months", type=int, default=config.MARKETING_RETENTION_MONTHS,
                   help=f"report fact partitions older than N months (default {config.MARKETING_RETENTION_MONTHS}); never drops them")
    p.add_argument("--partitions-only", action="store_true", help="only the partition step (create ahead + retention report)")
    p.add_argument("--skip-partitions", action="store_true", help="only refresh the rollup")
    p.add_argument("--verbose", action="store_true", help="debug logging")
    return p


def check_args(args: argparse.Namespace) -> str | None:
    """A usage error message, or None when the arguments are fine. Pure."""
    if not 1 <= args.days <= MAX_DAYS:
        return f"--days must be between 1 and {MAX_DAYS}"
    if not 0 <= args.partitions_ahead <= mpart.MAX_AHEAD_MONTHS:
        return f"--partitions-ahead must be between 0 and {mpart.MAX_AHEAD_MONTHS}"
    if args.retention_months < 1:
        return "--retention-months must be 1 or more"
    if args.full and not args.yes:
        return "--full rebuilds the whole rollup and can take minutes on a large fact table: add --yes to confirm (this job never prompts)"
    if args.full and args.window_only:
        return "--full and --window-only contradict each other"
    if args.partitions_only and (args.skip_partitions or args.full):
        return "--partitions-only cannot be combined with --skip-partitions or --full"
    return None


def step_partitions(engine, schema: str, args, today: date) -> None:
    with engine.connect() as conn:
        mschema.require_installed(conn, schema)
        conn.rollback()
        report = mpart.maintain(conn, schema, today=today, ahead=args.partitions_ahead,
                                retention_months=args.retention_months)
    for line in report.lines():
        logger.info("%s", line)


def step_rollup(engine, schema: str, args, today: date) -> None:
    if args.full:
        logger.warning("FULL REBUILD requested (--full --yes): recomputing every month that has facts in schema '%s'", schema)
    res = refresh(engine, schema, days=args.days, full=args.full, today=today, use_touched=not args.window_only)
    logger.info("rollup %s", res.summary())
    if res.touched_days:
        logger.info("rollup: %d older day(s) were recomputed because their facts changed since the last good run", res.touched_days)


def _setup_logging(verbose: bool) -> None:
    """Console + marketing_rollup.log. Only when this file is RUN as a job, never on import (L-010)."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(LOG_FILE, encoding="utf-8")],
    )


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:                      # argparse's own usage error / --help
        return int(exc.code or 0)
    problem = check_args(args)
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return 2
    try:
        _setup_logging(args.verbose)
    except OSError:                                # a read-only folder must not stop the job
        logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    try:
        return run_job(args)
    except BaseException as exc:  # noqa: BLE001 - an unattended job logs and exits non-zero, never a bare traceback (L-013)
        logger.exception("marketing rollup job failed: %s", type(exc).__name__)
        return 1


def run_job(args, engine=None, today: date | None = None) -> int:
    """The steps, each guarded on its own. Returns the exit code."""
    from erp import config
    schema = mschema.check_identifier(args.schema or config.MARKETING_SCHEMA)
    today = today or date.today()
    if engine is None:
        from erp.db import engine as default_engine
        engine = default_engine
    steps = []
    if not args.skip_partitions:
        steps.append(("partitions", step_partitions))
    if not args.partitions_only:
        steps.append(("rollup", step_rollup))
    failed = []
    logger.info("marketing rollup job starting: schema=%s today=%s steps=%s", schema, today, ",".join(n for n, _ in steps))
    for name, fn in steps:
        try:
            fn(engine, schema, args, today)
        except mschema.SchemaError as exc:
            logger.error("step '%s' cannot run: %s", name, exc)
            failed.append(name)
        except Exception as exc:  # noqa: BLE001
            logger.exception("step '%s' failed: %s", name, type(exc).__name__)
            failed.append(name)
    if failed:
        logger.error("marketing rollup job finished WITH FAILURES in: %s", ", ".join(failed))
        return 1
    logger.info("marketing rollup job finished ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
