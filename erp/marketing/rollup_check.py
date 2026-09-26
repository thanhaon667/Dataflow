"""One-off SCALE VERIFICATION of the Phase 4 rollup layer and partition maintenance, in a THROWAWAY schema.

It installs db/sql/07 + 09 into `perf_rollup`, fills `interaction_fact` with a few million synthetic rows, builds the
rollup with the real job code (erp/marketing/rollup.py), and measures: the report-shaped queries against the rollup
versus the raw fact (EXPLAIN ANALYZE), that an incremental `--days 3` refresh touches only those days, that a
refresh is idempotent, that the rollup equals a direct GROUP BY of the facts, and that partition maintenance
creates months ahead and only REPORTS old ones. It drops the schema again.

WHAT IT NEVER DOES: touch `public`. The schema name must match perf_[a-z0-9_]+, every statement is schema-qualified
or runs with search_path set to that schema, and the schema is dropped in a `finally` (also when a step fails).
Not part of the merge gate (minutes and about 2 GB of disk); the numbers of the last run are in
docs/marketing-data-architecture.md s.8.

Run:
  venv\\Scripts\\python.exe -u -m erp.marketing.rollup_check --yes
  venv\\Scripts\\python.exe -u -m erp.marketing.rollup_check --yes --fact-rows 500000 --out rollup_report.txt
"""
from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time
from datetime import date, timedelta

from sqlalchemy import text

from erp.marketing import partitions as mpart
from erp.marketing import perf_check as pc
from erp.marketing import rollup as mroll
from erp.marketing import schema as mschema

logger = logging.getLogger("erp.marketing.rollup_check")

DDL_ROLLUP = "db/sql/09_marketing_rollup.sql"
LEADS_STUB = 1000


class _Quiet(pc.Report):
    """A Report that prints nothing: for the warm-up run of a query whose plan is not reported."""

    def say(self, line: str = "") -> None:
        pass


def _scalar(conn, sql: str, params: dict | None = None):
    return conn.execute(text(sql), params or {}).scalar()


def _fingerprint(conn, schema: str) -> tuple:
    """(campaign rows, md5 of them, channel rows, md5 of them) with refreshed_at excluded, in key order:
    equal fingerprints = identical rollups."""
    cols = ", ".join(mroll.MEASURE_COLUMNS)
    out = []
    for table, key in (("interaction_daily_rollup", "event_date, channel_id, campaign_id, currency, grain"),
                       ("interaction_daily_channel_rollup", "event_date, channel_id, currency, grain")):
        out.extend(conn.execute(text(f"""
            SELECT count(*), coalesce(md5(string_agg(concat_ws('|', {key}, {cols}), ',' ORDER BY {key})), '')
              FROM {schema}.{table}""")).one())
    return tuple(out)


def _mismatches(conn, schema: str, lo: date | None = None, hi: date | None = None) -> int:
    """Rows where the rollup differs from a direct GROUP BY of the facts (both directions), optionally for a day range."""
    rng = "" if lo is None else "WHERE event_date >= :lo AND event_date < :hi"
    params = {} if lo is None else {"lo": lo, "hi": hi}
    direct = f"""
        SELECT event_date, channel_id, COALESCE(campaign_id, 0) AS campaign_id, currency,
               {mroll.GRAIN_SQL} AS grain, count(*) AS events_total,
               count(*) FILTER (WHERE event_type = 'impression') AS events_impression,
               count(*) FILTER (WHERE event_type = 'click') AS events_click,
               count(*) FILTER (WHERE event_type = 'reaction') AS events_reaction,
               count(*) FILTER (WHERE event_type = 'session') AS events_session,
               count(*) FILTER (WHERE event_type = 'conversion') AS events_conversion,
               count(*) FILTER (WHERE event_type NOT IN ('impression','click','reaction','session','conversion')) AS events_other,
               sum(impressions)::bigint AS impressions, sum(clicks)::bigint AS clicks, sum(reactions)::bigint AS reactions,
               sum(sessions)::bigint AS sessions, sum(conversions)::bigint AS conversions,
               sum(spend_micros)::bigint AS spend_micros, sum(revenue_micros)::bigint AS revenue_micros,
               COALESCE(sum(revenue_micros) FILTER (WHERE attrs->>'revenue_derived' = 'true'), 0)::bigint AS revenue_derived_micros
          FROM {schema}.interaction_fact {rng} GROUP BY 1, 2, 3, 4, 5"""
    stored = f"""SELECT event_date, channel_id, campaign_id, currency, grain, events_total, events_impression, events_click,
                        events_reaction, events_session, events_conversion, events_other, impressions, clicks, reactions,
                        sessions, conversions, spend_micros, revenue_micros, revenue_derived_micros
                   FROM {schema}.interaction_daily_rollup {rng}"""
    a = _scalar(conn, f"SELECT count(*) FROM (({direct}) EXCEPT ({stored})) x", params)
    b = _scalar(conn, f"SELECT count(*) FROM (({stored}) EXCEPT ({direct})) x", params)
    # the per-channel table must equal the fact grouped by (day, channel) too
    cols = ", ".join(mroll.MEASURE_COLUMNS)
    direct_ch = (f"SELECT event_date, channel_id, currency, grain, {cols} FROM (" + direct.replace("COALESCE(campaign_id, 0) AS campaign_id, ", "")
                 .replace("GROUP BY 1, 2, 3, 4, 5", "GROUP BY 1, 2, 3, 4") + ") d")
    stored_ch = f"SELECT event_date, channel_id, currency, grain, {cols} FROM {schema}.interaction_daily_channel_rollup {rng}"
    c = _scalar(conn, f"SELECT count(*) FROM (({direct_ch}) EXCEPT ({stored_ch})) x", params)
    d = _scalar(conn, f"SELECT count(*) FROM (({stored_ch}) EXCEPT ({direct_ch})) x", params)
    return int(a) + int(b) + int(c) + int(d)


def _totals(conn, schema: str, table: str) -> tuple:
    cols = "sum(impressions), sum(clicks), sum(conversions), sum(spend_micros), sum(revenue_micros)"
    return tuple(int(v or 0) for v in conn.execute(text(f"SELECT {cols} FROM {schema}.{table}")).one())


def _detect_probe(engine, conn, rep: pc.Report, schema: str, today: date, label: str) -> dict:
    """EXPLAIN ANALYZE the changed-day detection query with the real watermark (nothing has changed: 0 rows)."""
    wm = mroll.watermark(mroll.last_ok_start(conn, schema))
    conn.rollback()
    return pc.explain(conn, rep, label,
                      f"SELECT DISTINCT event_date FROM {schema}.interaction_fact "
                      f"WHERE event_date < CAST(:ws AS date) AND loaded_at >= :wm ORDER BY 1",
                      {"ws": mroll.window_start(today, 3).isoformat(), "wm": wm}, show=4)


def _vacuum(engine, schema: str) -> float:
    """VACUUM (ANALYZE) the fact on its own autocommit connection (VACUUM cannot run inside a transaction). This is
    what autovacuum does on a real, loaded table: it summarises the BRIN ranges the bulk load left unsummarised."""
    t0 = time.monotonic()
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
        c.exec_driver_sql(f"VACUUM (ANALYZE) {schema}.interaction_fact")
    return time.monotonic() - t0


def _measure(engine, conn, rep: pc.Report, schema: str, args) -> list[dict]:
    results: list[dict] = []
    today = date.today()

    rep.head("1. INSTALL the real DDL (07 + 09) into the throwaway schema")
    conn.exec_driver_sql("CREATE TABLE leads (id SERIAL PRIMARY KEY)")
    conn.execute(text("INSERT INTO leads (id) SELECT g FROM generate_series(1, :n) g"), {"n": LEADS_STUB})
    pc.run_sql_file(conn, pc.DDL_MARKETING)
    pc.run_sql_file(conn, pc.DDL_CURRENCY)
    pc.run_sql_file(conn, DDL_ROLLUP)
    conn.commit()
    rep.say(f"  {pc.DDL_MARKETING} and {DDL_ROLLUP} applied")
    rep.say(f"  installed check: 07 missing={mschema.missing_tables(conn, schema)}; 09 -> "
            f"{[t for t in mroll.ROLLUP_TABLES if _scalar(conn, 'SELECT to_regclass(:q) IS NULL', {'q': f'{schema}.{t}'})] or 'both present'}")

    rep.head("2. PARTITION MAINTENANCE on an empty fact table")
    rep.say("  retention 24 months, look-ahead 3 months, today " + str(today))
    report = mpart.maintain(conn, schema, today=today, ahead=3, retention_months=24)
    for line in report.lines():
        rep.say("  " + line)
    again = mpart.maintain(conn, schema, today=today, ahead=3, retention_months=24)
    rep.say(f"  second call created {len(again.created)} (idempotent: {not again.created})")
    # the loop below creates the past months build_fact needs
    end_month = today.replace(day=1)
    months = [end_month]
    for _ in range(args.months):
        months.append((months[-1] - timedelta(days=1)).replace(day=1))
    mschema.ensure_partitions(conn, schema, months)
    conn.commit()

    rep.head("3. EMPTY fact table: the refresh must be a clean no-op")
    r1 = mroll.refresh(engine, schema, days=3, today=today)
    r2 = mroll.refresh(engine, schema, full=True, today=today)
    rep.say(f"  window: {r1.summary()}")
    rep.say(f"  full  : {r2.summary()}")
    n_empty = _scalar(conn, f"SELECT count(*) FROM {schema}.interaction_daily_rollup")
    conn.rollback()
    rep.say(f"  rollup rows after both: {n_empty} (expected 0)")

    rep.head(f"4. BUILD {args.fact_rows:,} synthetic fact rows")
    pc.build_dimensions(conn, rep, LEADS_STUB)
    conn.commit()
    pc.build_fact(conn, rep, args.fact_rows, args.months, end_month)
    conn.commit()
    t0 = time.monotonic()
    conn.exec_driver_sql("ANALYZE")
    conn.commit()
    rep.say(f"  ANALYZE {time.monotonic() - t0:.1f}s; fact rows: {_scalar(conn, f'SELECT count(*) FROM {schema}.interaction_fact'):,}")
    rep.say(pc._sizes(conn, rep, schema))

    rep.head("5. FULL REBUILD of the rollup with the real job code")
    t0 = time.monotonic()
    full = mroll.refresh(engine, schema, full=True, today=today)
    full_s = time.monotonic() - t0
    rep.say(f"  {full.summary()}")
    conn.exec_driver_sql(f"ANALYZE {schema}.interaction_daily_rollup")
    conn.exec_driver_sql(f"ANALYZE {schema}.interaction_daily_channel_rollup")
    conn.commit()
    n_roll = _scalar(conn, f"SELECT count(*) FROM {schema}.interaction_daily_rollup")
    size = _scalar(conn, f"SELECT pg_total_relation_size('{schema}.interaction_daily_rollup')")
    n_ch = _scalar(conn, f"SELECT count(*) FROM {schema}.interaction_daily_channel_rollup")
    size_ch = _scalar(conn, f"SELECT pg_total_relation_size('{schema}.interaction_daily_channel_rollup')")
    rep.say(f"  campaign rollup: {n_roll:,} rows, {size / 1e6:,.1f} MB (table + indexes) for {args.fact_rows:,} fact rows "
            f"({args.fact_rows / max(1, n_roll):.1f} fact rows per rollup row)")
    rep.say(f"  channel rollup : {n_ch:,} rows, {size_ch / 1e6:,.2f} MB")
    conn.rollback()
    results.append({"label": f"full rebuild ({args.fact_rows:,} facts -> {n_roll:,} rollup rows)", "ms": full_s * 1000, "scans": [], "partitions": 0})

    rep.head("6. CORRECTNESS: rollup vs a direct GROUP BY of the facts")
    mm = _mismatches(conn, schema)
    conn.rollback()
    rep.say(f"  differing rows in either direction, campaign table + channel table: {mm} (expected 0)")
    ft, rt = _totals(conn, schema, "interaction_fact"), _totals(conn, schema, "interaction_daily_rollup")
    ct = _totals(conn, schema, "interaction_daily_channel_rollup")
    conn.rollback()
    rep.say(f"  grand totals (impressions, clicks, conversions, spend, revenue) fact  : {ft}")
    rep.say(f"                                                                  rollup: {rt}  equal: {ft == rt}")
    rep.say(f"                                                                  channel table: {ct}  equal: {ft == ct}")

    rep.head("6b. CHANGED-DAY DETECTION cost: BRIN on loaded_at, before and after a VACUUM")
    rep.say("  the query finds older days whose facts were loaded since the last good run; nothing changed, so it must return 0 rows")
    before = _detect_probe(engine, conn, rep, schema, today, "detection, BRIN NOT summarised (bulk load, no VACUUM yet)")
    conn.rollback()
    vs = _vacuum(engine, schema)
    rep.say(f"  VACUUM (ANALYZE) of the fact: {vs:.1f}s (autovacuum does this on a real table)")
    after = _detect_probe(engine, conn, rep, schema, today, "detection, BRIN summarised (after VACUUM)")
    conn.rollback()
    results.append({"label": "changed-day detection, BRIN unsummarised", "ms": before["ms"], "scans": before["scans"], "partitions": before["partitions"]})
    results.append({"label": "changed-day detection, BRIN summarised", "ms": after["ms"], "scans": after["scans"], "partitions": after["partitions"]})

    rep.head("7. REPORT-SHAPED QUERIES: rollup vs raw fact (each run twice, the second timing is reported)")
    d1 = (today - timedelta(days=45)).isoformat()
    d2 = (today - timedelta(days=15)).isoformat()
    pairs = [
        ("Phase 1 baseline, verbatim shape: full table, no date filter, per channel sum(clicks)",
         "SELECT channel_id, sum(clicks) FROM {t} GROUP BY 1", {}),
        ("date range (30 d) + 3 channels, per channel per day",
         "SELECT channel_id, event_date, {ev} AS events, sum(impressions) AS imp, sum(clicks) AS clk, "
         "sum(conversions) AS conv, sum(spend_micros) / 1000000.0 AS spend FROM {t} "
         "WHERE event_date >= CAST(:d1 AS date) AND event_date < CAST(:d2 AS date) "
         "AND channel_id = ANY(CAST(:ch AS smallint[])) GROUP BY 1, 2 ORDER BY 1, 2", {"d1": d1, "d2": d2, "ch": [1, 2, 3]}),
        ("FULL range, per channel totals + derived rates (CTR, CPC, CVR at read time)",
         "SELECT channel_id, {ev} AS events, sum(impressions) AS imp, sum(clicks) AS clk, sum(conversions) AS conv, "
         "sum(spend_micros) / 1000000.0 AS spend, sum(clicks)::numeric / nullif(sum(impressions), 0) AS ctr, "
         "sum(spend_micros) / 1000000.0 / nullif(sum(clicks), 0) AS cpc, "
         "sum(conversions)::numeric / nullif(sum(clicks), 0) AS cvr FROM {t} GROUP BY 1 ORDER BY 1", {}),
        ("FULL range, per channel per day (the whole-history trend chart)",
         "SELECT channel_id, event_date, sum(clicks) AS clk, sum(spend_micros) / 1000000.0 AS spend FROM {t} "
         "GROUP BY 1, 2 ORDER BY 1, 2", {}),
        ("top 10 campaigns by clicks, 30-day range",
         "SELECT campaign_id, sum(clicks) AS clk, sum(spend_micros) / 1000000.0 AS spend FROM {t} "
         "WHERE event_date >= CAST(:d1 AS date) AND event_date < CAST(:d2 AS date) "
         "GROUP BY 1 ORDER BY 2 DESC LIMIT 10", {"d1": d1, "d2": d2}),
    ]
    rep.say("  NOTE: the raw 'events' column is count(*); the rollup's is sum(events_total) - the same number.")
    for label, tpl, params in pairs:
        variants = [("raw fact           ", f"{schema}.interaction_fact", "count(*)"),
                    ("rollup (campaign)  ", f"{schema}.interaction_daily_rollup", "sum(events_total)")]
        if "campaign_id" not in tpl:
            variants.append(("rollup (channel)   ", f"{schema}.interaction_daily_channel_rollup", "sum(events_total)"))
        for kind, table, ev in variants:
            sql = tpl.format(t=table, ev=ev)
            pc.explain(conn, _Quiet(), "warm-up", sql, params, show=0)     # discard: cache warm-up, not reported
            res = pc.explain(conn, rep, f"{kind} | {label}", sql, params, show=10)
            res["label"] = f"{kind} | {label}"
            results.append(res)
        conn.rollback()

    rep.head("8. INCREMENTAL REFRESH: `--days 3` touches only those days")
    snap_before = conn.execute(text(f"""
        SELECT count(*) FILTER (WHERE event_date >= :ws) AS in_window, count(*) FILTER (WHERE event_date < :ws) AS older,
               max(refreshed_at) FILTER (WHERE event_date < :ws) AS older_refreshed
          FROM {schema}.interaction_daily_rollup"""), {"ws": mroll.window_start(today, 3)}).one()
    conn.rollback()
    t0 = time.monotonic()
    inc = mroll.refresh(engine, schema, days=3, today=today)
    inc_s = time.monotonic() - t0
    rep.say(f"  {inc.summary()}")
    rep.say(f"  touched (older) days picked up from changed facts: {inc.touched_days} (expected 0: nothing changed since the full rebuild)")
    after = conn.execute(text(f"""
        SELECT count(*) FILTER (WHERE refreshed_at >= now() - interval '5 minutes') AS recomputed,
               count(*) FILTER (WHERE event_date < :ws AND refreshed_at >= :t) AS older_rewritten,
               count(*) FILTER (WHERE event_date >= :ws AND refreshed_at >= :t) AS window_rewritten,
               max(refreshed_at) FILTER (WHERE event_date < :ws) AS older_refreshed
          FROM {schema}.interaction_daily_rollup"""),
        {"ws": mroll.window_start(today, 3), "t": _scalar(conn, f"SELECT max(started_at) FROM {schema}.marketing_rollup_run WHERE mode = 'window' AND status = 'ok'")}).one()
    conn.rollback()
    rep.say(f"  rollup rows in the 3-day window (and after, open ended): {snap_before[0]}; older rows: {snap_before[1]}")
    rep.say(f"  rows rewritten by this run: in window {after[2]}, OLDER THAN THE WINDOW {after[1]} (expected 0)")
    rep.say(f"  newest refreshed_at among older days is still the full rebuild's: {after[3] == snap_before[2]}")
    where, params = mroll._bounds((mroll.window_start(today, 3), None))
    conn.rollback()
    ex = pc.explain(conn, rep, "the refresh's own SELECT for that window (which fact partitions does it read?)",
                    f"SELECT event_date, channel_id, COALESCE(campaign_id, 0), count(*), sum(clicks) "
                    f"FROM {schema}.interaction_fact WHERE event_date >= CAST(:a AS date) GROUP BY 1, 2, 3",
                    {"a": params["a"].isoformat()}, show=6)
    conn.rollback()
    results.append({"label": f"incremental --days 3 refresh ({inc.rows_deleted} rows replaced by {inc.rows_written})", "ms": inc_s * 1000, "scans": ex["scans"], "partitions": ex["partitions"]})
    rep.say(f"  fact partitions read by the refresh: {ex['partitions']} of {len(mpart.list_partitions(conn, schema))}")
    conn.rollback()

    rep.head("9. A RESTATED OLD DAY is picked up by the next refresh (changed-facts detection)")
    old_day = today - timedelta(days=100)
    fp_pre = _fingerprint(conn, schema)
    conn.rollback()
    conn.execute(text(f"UPDATE {schema}.interaction_fact SET clicks = clicks + 5, loaded_at = now() WHERE event_date = :d"), {"d": old_day})
    conn.commit()
    rep.say(f"  simulated a restatement of {old_day} (clicks + 5 on its facts, loaded_at = now(), like the pipeline's ON CONFLICT DO UPDATE)")
    rep.say(f"  mismatch before the refresh for that day: {_mismatches(conn, schema, old_day, old_day + timedelta(days=1))} rows differ")
    conn.rollback()
    ch = mroll.refresh(engine, schema, days=3, today=today)
    rep.say(f"  {ch.summary()}")
    rep.say(f"  touched older days: {ch.touched_days} (expected 1)")
    rep.say(f"  mismatch after the refresh for that day: {_mismatches(conn, schema, old_day, old_day + timedelta(days=1))} rows differ (expected 0)")
    conn.rollback()
    fp_post = _fingerprint(conn, schema)
    conn.rollback()
    rep.say(f"  rollup fingerprint changed only because of that day: {fp_pre != fp_post}")

    rep.head("10. IDEMPOTENCY: refresh twice in a row -> identical rollup (refreshed_at excluded)")
    mroll.refresh(engine, schema, days=3, today=today)
    fp1 = _fingerprint(conn, schema)
    conn.rollback()
    mroll.refresh(engine, schema, days=3, today=today)
    fp2 = _fingerprint(conn, schema)
    conn.rollback()
    rep.say(f"  window x2 : {fp1[0]:,}+{fp1[2]:,} rows md5 {fp1[1][:8]}/{fp1[3][:8]}  ==  {fp2[0]:,}+{fp2[2]:,} rows md5 {fp2[1][:8]}/{fp2[3][:8]}  -> identical: {fp1 == fp2}")
    mroll.refresh(engine, schema, full=True, today=today)
    fp3 = _fingerprint(conn, schema)
    conn.rollback()
    rep.say(f"  then FULL : {fp3[0]:,}+{fp3[2]:,} rows md5 {fp3[1][:8]}/{fp3[3][:8]}  -> identical to the incremental result: {fp1 == fp3}")
    rep.say(f"  final mismatches vs a direct GROUP BY over ALL days: {_mismatches(conn, schema)} (expected 0)")
    conn.rollback()

    rep.head("11. THE REAL CLI in a subprocess (exit code, log file, --full guard)")
    py = sys.executable
    def cli(*extra):
        p = subprocess.run([py, "-B", "-m", "erp.marketing.rollup", "--schema", schema, *extra],
                           cwd=str(pc.PROJECT_ROOT), capture_output=True, text=True, timeout=900)
        return p.returncode, (p.stderr or "").strip().splitlines()[-4:]
    rc, tail = cli("--days", "3", "--partitions-ahead", "2", "--retention-months", "6")
    rep.say(f"  --days 3 --retention-months 6 -> exit {rc}")
    for line in tail:
        rep.say("     " + line[:170])
    rc2, tail2 = cli("--full")
    rep.say(f"  --full without --yes -> exit {rc2} (expected 2): {tail2[-1] if tail2 else ''}"[:220])
    rc3, _ = cli("--schema", "perf_does_not_exist_x")
    rep.say(f"  a schema with no marketing tables -> exit {rc3} (expected 1)")
    names_before = [p["name"] for p in mpart.list_partitions(conn, schema)]
    conn.rollback()
    rep.say(f"  partitions still present after the retention report (nothing may be dropped): {len(names_before)}")

    rep.head("12. RETENTION REPORT (documented threshold, report only)")
    r = mpart.maintain(conn, schema, today=today, ahead=3, retention_months=6)
    for line in r.lines():
        rep.say("  " + line)
    after_names = [p["name"] for p in mpart.list_partitions(conn, schema)]
    conn.rollback()
    rep.say(f"  partitions before {len(names_before)}, after {len(after_names)}: only additions from the look-ahead, none removed: "
            f"{set(names_before) <= set(after_names)}")
    return results


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m erp.marketing.rollup_check",
                                description="Scale verification of the rollup layer in a throwaway schema (never touches public).")
    p.add_argument("--yes", action="store_true", help="required: confirms that a throwaway schema may be created and dropped")
    p.add_argument("--schema", default="perf_rollup", help="throwaway schema name (must match perf_[a-z0-9_]+)")
    p.add_argument("--fact-rows", type=int, default=3_000_000)
    p.add_argument("--months", type=int, default=12)
    p.add_argument("--keep", action="store_true", help="do NOT drop the schema at the end")
    p.add_argument("--out", default=None, help="also write the whole report to this file")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    schema = (args.schema or "").strip().lower()
    if not pc.SCHEMA_RE.match(schema):
        print(f"refusing to use schema {args.schema!r}: it must match perf_[a-z0-9_]+ so it can never be a real one")
        return 2
    if not args.yes:
        print(f"This creates schema '{schema}' with about {args.fact_rows:,} synthetic rows and drops it again. Re-run with --yes.")
        return 2
    if pc.free_gb() < 3.0:
        print(f"only {pc.free_gb():.1f} GB free - this run needs about 2 GB.")
        return 2

    from erp.db import engine
    rep = pc.Report()
    results: list[dict] = []
    started = time.monotonic()
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            conn.exec_driver_sql(f"CREATE SCHEMA {schema}")
            conn.exec_driver_sql(f"SET search_path TO {schema}")
            conn.commit()
            try:
                results = _measure(engine, conn, rep, schema, args)
            finally:
                conn.rollback()
                if args.keep:
                    rep.say(f"--keep: schema '{schema}' left in place. Drop it with: DROP SCHEMA {schema} CASCADE;")
                else:
                    conn.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
                    conn.exec_driver_sql("SET search_path TO public")
                    conn.commit()
                    rep.say("")
                    rep.say(f"throwaway schema '{schema}' dropped. Free disk now {pc.free_gb():.1f} GB.")
    except Exception as exc:  # noqa: BLE001
        rep.say("")
        rep.say(f"FAILED: {type(exc).__name__}: {exc}")
        logger.exception("rollup check failed")
        pc._write(args.out, rep)
        return 1
    rep.head("SUMMARY")
    for r in results:
        rep.say(f"  {r['ms'] if r['ms'] is None else round(r['ms'], 1):>10} ms  {r['label']}")
    rep.say(f"\n  total run time {time.monotonic() - started:.0f}s")
    pc._write(args.out, rep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
