"""One-off SCALE VERIFICATION of the marketing star schema and of the two new `leads` indexes.

It builds a few million synthetic rows in a THROWAWAY schema, runs EXPLAIN ANALYZE on the query shapes a
report page would really run, prints the plans and the timings, and drops the schema again. It exists so
the design can be judged on measured numbers instead of on confidence.

WHAT IT NEVER DOES: it never reads, writes, alters or analyses anything in `public`. The schema name must
match perf_[a-z0-9_]+ (so it can never be `public`), the session's search_path is set to that schema alone
- an unqualified `leads` inside this run is the synthetic copy, not the real table - and the schema is
dropped in a `finally`, even when a step fails or you interrupt it.

It is deliberately NOT part of the merge gate: it costs minutes and about 2 GB of disk. Run it when the
schema or the indexes change; the numbers of the last run are written up in
docs/marketing-data-architecture.md.

Run:
  venv\\Scripts\\python.exe -u -m erp.marketing.perf_check --yes
  venv\\Scripts\\python.exe -u -m erp.marketing.perf_check --yes --fact-rows 3000000 --lead-rows 1000000
  venv\\Scripts\\python.exe -u -m erp.marketing.perf_check --yes --keep --out perf_report.txt

(`-u` matters: the run prints as it goes, and a piped run would otherwise show nothing for minutes - L-011.)
"""
from __future__ import annotations

import argparse
import csv
import logging
import re
import shutil
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import text

logger = logging.getLogger("erp.marketing.perf_check")

SCHEMA_RE = re.compile(r"^perf_[a-z0-9_]{1,40}$")
MIN_FREE_GB = 1.5                 # abort before filling the machine's disk
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DDL_MARKETING = "db/sql/07_marketing_schema.sql"
DDL_CURRENCY = "db/sql/10_marketing_currency.sql"     # the fact's currency column; the pipeline writes it, the rollup groups by it
DDL_LEADS_INDEXES = "db/sql/08_leads_indexes.sql"

CHANNELS = 12
CAMPAIGNS = 400
CREATIVES = 2000
IDENTITIES = 100_000
LANDING_ROWS = 400_000
PIPELINE_ROWS = 20_000            # rows pushed through the REAL Python pipeline, to measure its throughput


class Report:
    """Collects everything printed, so the whole run can also be written to a file."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def say(self, line: str = "") -> None:
        self.lines.append(line)
        print(line, flush=True)

    def head(self, title: str) -> None:
        self.say("")
        self.say("=" * 78)
        self.say(title)
        self.say("=" * 78)


def free_gb() -> float:
    try:
        return shutil.disk_usage(PROJECT_ROOT.anchor or "C:\\").free / 1e9
    except OSError:
        return 99.0


def check_disk(rep: Report, where: str) -> None:
    gb = free_gb()
    if gb < MIN_FREE_GB:
        raise RuntimeError(f"only {gb:.1f} GB free after {where} - stopping before the disk fills")


def explain(conn, rep: Report, label: str, sql: str, params: dict | None = None, show: int = 14) -> dict:
    """EXPLAIN (ANALYZE, BUFFERS) one query, print the plan, and pull the numbers worth reporting."""
    rows = conn.execute(text("EXPLAIN (ANALYZE, BUFFERS, SUMMARY) " + sql), params or {}).fetchall()
    plan = [r[0] for r in rows]
    rep.say("")
    rep.say(f"--- {label}")
    for line in plan[:show]:
        rep.say("    " + line)
    if len(plan) > show:
        rep.say(f"    ... ({len(plan) - show} more plan lines)")
    joined = "\n".join(plan)
    exec_ms = _number(joined, r"Execution Time: ([\d.]+) ms")
    removed = _number(joined, r"Partitions removed by [A-Za-z ]*: (\d+)")
    for line in plan:
        if line.strip().startswith("Execution Time") or line.strip().startswith("Planning Time"):
            if line not in plan[:show]:
                rep.say("    " + line.strip())
    scans = sorted({m for m in re.findall(r"(Seq Scan|Index Scan|Index Only Scan|Bitmap Index Scan|Bitmap Heap Scan|Parallel Seq Scan)", joined)})
    parts = len(set(re.findall(r"on (?:\w+\.)?(interaction_fact_\d{4}_\d{2})", joined)))
    rep.say(f"    -> {exec_ms if exec_ms is not None else '?'} ms | scans: {', '.join(scans) or 'none'}"
            + (f" | fact partitions touched: {parts}" if parts else "")
            + (f" | partitions pruned: {int(removed)}" if removed is not None else ""))
    return {"label": label, "ms": exec_ms, "scans": scans, "partitions": parts, "plan": plan}


def _number(blob: str, pattern: str) -> float | None:
    m = re.search(pattern, blob)
    return float(m.group(1)) if m else None


def run_sql_file(conn, rel_path: str) -> None:
    """Execute one of the project's .sql files in the current session (so it lands in the perf schema)."""
    sql = (PROJECT_ROOT / rel_path).read_text(encoding="utf-8")
    conn.exec_driver_sql(sql)


# --------------------------------------------------------------------------- synthetic data
def build_leads(conn, rep: Report, rows: int, days: int) -> None:
    rep.say(f"  building {rows:,} synthetic leads over {days} days ...")
    conn.exec_driver_sql("""
        CREATE TABLE leads (
            id          SERIAL PRIMARY KEY,
            full_name   VARCHAR(150) NOT NULL,
            email       VARCHAR(150),
            phone       VARCHAR(30),
            company     VARCHAR(150),
            source      VARCHAR(50),
            raw_payload JSONB,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            sla_due_at  TIMESTAMPTZ
        )""")
    t0 = time.monotonic()
    conn.execute(text("""
        INSERT INTO leads (full_name, email, phone, company, source, raw_payload, created_at, sla_due_at)
        SELECT 'Lead ' || g,
               'lead' || g || '@example.invalid',
               '+8490' || lpad((g % 9999999)::text, 7, '0'),
               'Company ' || (g % 5000),
               (ARRAY['landing_page','facebook','google_ads','zalo','referral','event','unknown'])[1 + (g % 7)],
               jsonb_build_object('n', g),
               now() - make_interval(secs => (g % (:days * 86400))),
               now() - make_interval(secs => (g % (:days * 86400))) + interval '5 hours'
          FROM generate_series(1, :rows) g
    """), {"rows": rows, "days": days})
    rep.say(f"  ... {time.monotonic() - t0:.1f}s")


def build_dimensions(conn, rep: Report, lead_rows: int) -> None:
    rep.say(f"  building dimensions: {CHANNELS} channels, {CAMPAIGNS} campaigns, {CREATIVES} creatives, {IDENTITIES:,} identities ...")
    conn.exec_driver_sql("INSERT INTO marketing_source (source_key, display_name) VALUES ('perf_synthetic', 'Perf synthetic')")
    conn.execute(text("""
        INSERT INTO marketing_channel (channel_key, display_name, medium, is_paid)
        SELECT 'channel_' || g, 'Channel ' || g,
               (ARRAY['paid_social','search','email','organic','referral'])[1 + (g % 5)],
               (g % 5) < 2
          FROM generate_series(1, :n) g
    """), {"n": CHANNELS})
    conn.execute(text("""
        INSERT INTO marketing_campaign (channel_id, campaign_key, name, objective)
        SELECT 1 + (g % :ch), 'campaign_' || g, 'Campaign ' || g,
               (ARRAY['awareness','traffic','leads','sales'])[1 + (g % 4)]
          FROM generate_series(1, :n) g
    """), {"n": CAMPAIGNS, "ch": CHANNELS})
    conn.execute(text("""
        INSERT INTO marketing_creative (campaign_id, creative_key, name, format)
        SELECT 1 + (g % :cp), 'creative_' || g, 'Creative ' || g,
               (ARRAY['image','video','carousel','text'])[1 + (g % 4)]
          FROM generate_series(1, :n) g
    """), {"n": CREATIVES, "cp": CAMPAIGNS})
    conn.execute(text("""
        INSERT INTO marketing_identity (identity_type, identity_value, lead_id, resolved_at)
        SELECT 'email_sha256', encode(sha256(CAST('lead' || g || '@example.invalid' AS bytea)), 'hex'),
               CASE WHEN g <= :leads THEN g END,
               CASE WHEN g <= :leads THEN now() END
          FROM generate_series(1, :n) g
    """), {"n": IDENTITIES, "leads": min(lead_rows, IDENTITIES)})


def build_landing(conn, rep: Report, rows: int, days: int) -> None:
    rep.say(f"  building {rows:,} landing rows over {days} days (for the BRIN index) ...")
    conn.execute(text("""
        INSERT INTO marketing_landing (batch_id, batch_seq, source, external_id, event_type, payload, received_at)
        SELECT ('00000000-0000-4000-8000-' || lpad(to_hex(g / 50000), 12, '0'))::uuid, g, 'perf_synthetic',
               'ext_' || g, 'rollup',
               jsonb_build_object('row', g, 'campaign', 'campaign_' || (g % :cp), 'clicks', g % 40),
               -- (:rows - g)::bigint forces bigint arithmetic for the whole expression: at 400,000 rows over 360
               -- days the plain-int product overflows int4 (~2.1e9) before the division ever runs (seen for real:
               -- "integer out of range" at g=1, (rows-g)*(days*86400) ~= 1.24e13) - never let generate_series' int
               -- default silently promote at only int4 volumes.
               now() - make_interval(secs => ((:rows - g)::bigint * (:days * 86400) / :rows))
          FROM generate_series(1, :rows) g
    """), {"rows": rows, "days": days, "cp": CAMPAIGNS})


def build_fact(conn, rep: Report, total_rows: int, months: int, end_month: date) -> tuple[date, date]:
    """Fill interaction_fact month by month (one transaction per month keeps WAL and memory bounded)."""
    first_month = date(end_month.year, end_month.month, 1)
    for _ in range(months - 1):
        first_month = (first_month - timedelta(days=1)).replace(day=1)
    per_month = total_rows // months
    rep.say(f"  building {total_rows:,} fact rows over {months} months ({per_month:,} per month) ...")
    offset = 0
    for i in range(months):
        m = first_month
        for _ in range(i):
            m = (m + timedelta(days=32)).replace(day=1)
        m_end = (m + timedelta(days=32)).replace(day=1)
        days_in_month = (m_end - m).days
        t0 = time.monotonic()
        conn.execute(text("""
            INSERT INTO interaction_fact
                (event_date, dedupe_key, event_at, source_id, channel_id, campaign_id, creative_id, identity_id,
                 event_type, impressions, clicks, reactions, sessions, conversions, spend_micros, revenue_micros,
                 landing_id, attrs, attrs_idx, loaded_at)
            SELECT CAST(:m AS date) + ((g + :off) % :dim),
                   md5((g + :off)::text)::uuid,
                   CAST(:m AS date) + ((g + :off) % :dim) + make_interval(secs => ((g * 37) % 86400)),
                   1,
                   1 + ((g + :off) % :ch),
                   1 + ((g + :off) % :cp),
                   1 + ((g + :off) % :cr),
                   CASE WHEN (g % 5) = 0 THEN 1 + ((g + :off) % :ident) END,
                   (ARRAY['impression','click','reaction','session','conversion','rollup'])[1 + ((g + :off) % 6)],
                   (g % 1000), (g % 37), (g % 11), (g % 7), (g % 3),
                   (g % 250000), (g % 900000),
                   1 + ((g + :off) % :landing),
                   jsonb_build_object(
                       'utm_source', 'src_' || (g % 7), 'utm_medium', 'med_' || (g % 4),
                       'utm_campaign', 'utm_' || (g % 53), 'placement', 'placement_' || (g % 9),
                       'device', (ARRAY['mobile','desktop','tablet'])[1 + (g % 3)],
                       'country', (ARRAY['vn','us','sg','jp'])[1 + (g % 4)],
                       'raw_note', 'synthetic row ' || (g + :off), 'ad_position', (g % 5)),
                   jsonb_build_object(
                       'utm_source', 'src_' || (g % 7), 'utm_medium', 'med_' || (g % 4),
                       'utm_campaign', 'utm_' || (g % 53), 'placement', 'placement_' || (g % 9),
                       'device', (ARRAY['mobile','desktop','tablet'])[1 + (g % 3)],
                       'country', (ARRAY['vn','us','sg','jp'])[1 + (g % 4)]),
                   now() - make_interval(days => :ago)
              FROM generate_series(1, :n) g
        """), {"m": m.isoformat(), "dim": days_in_month, "n": per_month, "off": offset, "ch": CHANNELS,
               "cp": CAMPAIGNS, "cr": CREATIVES, "ident": IDENTITIES, "landing": LANDING_ROWS,
               "ago": (months - i) * 30})
        offset += per_month
        rep.say(f"    {m.isoformat()[:7]}: {per_month:,} rows in {time.monotonic() - t0:.1f}s "
                f"(free disk {free_gb():.1f} GB)")
        check_disk(rep, f"month {m.isoformat()[:7]}")
    last_month_end = (first_month + timedelta(days=32 * months)).replace(day=1)
    return first_month, last_month_end


# --------------------------------------------------------------------------- the pipeline, for real
def run_real_pipeline(conn_schema: str, rep: Report, rows: int) -> None:
    """Push a generated CSV through the REAL connector + pipeline, to measure end-to-end throughput and to
    prove the dedupe and malformed-row behaviour outside a unit test."""
    from erp.marketing.connectors.flat_file import FlatFileConnector
    from erp.marketing.pipeline import Pipeline

    tmp = Path(tempfile.mkdtemp(prefix="erp_perf_")) / "channel_export.csv"
    header = ["Date", "Channel", "Campaign name", "Ad name", "Impressions", "Clicks", "Conversions",
              "Amount spent (USD)", "utm_source", "device", "country", "Weird extra column", "Email"]
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        day0 = date.today() - timedelta(days=30)
        for g in range(rows):
            broken = (g % 500 == 0)
            w.writerow([
                "" if broken else (day0 + timedelta(days=g % 30)).isoformat(),
                f"channel_{g % CHANNELS}", f"Campaign {g % 50}", f"Creative {g % 200}",
                g % 900, g % 30, g % 4, f"{(g % 400) / 10:.2f}",
                f"src_{g % 7}", ["mobile", "desktop", "tablet"][g % 3], ["vn", "us", "sg"][g % 3],
                f"something-{g}", f"lead{g}@example.invalid",
            ])
    try:
        pipe = Pipeline(schema=conn_schema, chunk_size=5000)
        t0 = time.monotonic()
        first = pipe.run(FlatFileConnector.from_csv(tmp, source_key="perf_flat_file"))
        rep.say(f"  first load : {first.summary()}")
        rate = first.rows_loaded / max(0.001, time.monotonic() - t0)
        rep.say(f"  throughput : {rate:,.0f} fact rows/s end to end (read CSV -> land -> validate -> dedupe -> load)")
        second = pipe.run(FlatFileConnector.from_csv(tmp, source_key="perf_flat_file"))
        rep.say(f"  same file again: {second.summary()}")
        rep.say(f"  -> dedupe holds: {second.rows_inserted} NEW rows on the second load "
                f"({second.rows_updated} restated in place)")
        rep.say(f"  -> malformed rows: {first.rows_invalid} of {first.rows_read} skipped and counted, batch still ok")
        for sample in first.invalid_samples[:3]:
            rep.say(f"     {sample}")
    finally:
        try:
            shutil.rmtree(tmp.parent, ignore_errors=True)
        except OSError:
            pass


# --------------------------------------------------------------------------- the run
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m erp.marketing.perf_check",
                                description="Scale verification in a throwaway schema (never touches public).")
    p.add_argument("--yes", action="store_true", required=False,
                   help="required: confirms that a throwaway schema may be created and dropped")
    p.add_argument("--schema", default="perf_test", help="throwaway schema name (must match perf_[a-z0-9_]+)")
    p.add_argument("--fact-rows", type=int, default=3_000_000)
    p.add_argument("--lead-rows", type=int, default=1_000_000)
    p.add_argument("--months", type=int, default=12)
    p.add_argument("--keep", action="store_true", help="do NOT drop the schema at the end (for manual digging)")
    p.add_argument("--out", default=None, help="also write the whole report to this file")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    schema = (args.schema or "").strip().lower()
    if not SCHEMA_RE.match(schema):
        print(f"refusing to use schema {args.schema!r}: it must match perf_[a-z0-9_]+ so it can never be a real one")
        return 2
    if not args.yes:
        print(f"This creates schema '{schema}' with about {args.fact_rows:,} synthetic rows and drops it again.")
        print("Re-run with --yes to go ahead. Nothing in public is ever read, written or analysed.")
        return 2
    if free_gb() < 3.0:
        print(f"only {free_gb():.1f} GB free - this run needs about 2 GB. Free some space first.")
        return 2

    from erp.db import engine
    rep = Report()
    results: list[dict] = []
    started = time.monotonic()
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            conn.exec_driver_sql(f"CREATE SCHEMA {schema}")
            conn.exec_driver_sql(f"SET search_path TO {schema}")   # an unqualified name can no longer reach public
            conn.commit()
            try:
                results = _measure(conn, rep, schema, args)
            finally:
                conn.rollback()
                if args.keep:
                    rep.say("")
                    rep.say(f"--keep: schema '{schema}' was left in place. Drop it with: DROP SCHEMA {schema} CASCADE;")
                else:
                    conn.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
                    conn.commit()
                    rep.say("")
                    rep.say(f"throwaway schema '{schema}' dropped. Free disk now {free_gb():.1f} GB.")
    except Exception as exc:  # noqa: BLE001 - report it, do not traceback at the user
        rep.say("")
        rep.say(f"FAILED: {type(exc).__name__}: {exc}")
        logger.exception("perf check failed")
        _write(args.out, rep)
        return 1

    rep.head("SUMMARY")
    for r in results:
        rep.say(f"  {r['ms'] if r['ms'] is not None else '?':>10} ms  {r['label']}")
    rep.say("")
    rep.say(f"  total run time {time.monotonic() - started:.0f}s")
    _write(args.out, rep)
    return 0


def _write(out: str | None, rep: Report) -> None:
    if out:
        Path(out).write_text("\n".join(rep.lines) + "\n", encoding="utf-8")
        print(f"report written to {out}")


def _measure(conn, rep: Report, schema: str, args) -> list[dict]:
    results: list[dict] = []
    days = max(30, args.months * 30)

    rep.head(f"1. LEADS at {args.lead_rows:,} rows - do the two new indexes get used?")
    build_leads(conn, rep, args.lead_rows, days)
    conn.exec_driver_sql("ANALYZE leads")
    conn.commit()
    check_disk(rep, "the leads table")

    lead_queries = [
        ("Today: new leads today",
         "SELECT count(*) FROM leads WHERE created_at >= date_trunc('day', now())", {}),
        ("Leads page: one page of a 30-day range, newest first",
         "SELECT id, full_name, source, created_at FROM leads "
         "WHERE created_at >= now() - interval '30 days' AND created_at < now() "
         "ORDER BY created_at DESC LIMIT 25", {}),
        ("Sources tab: per-source counts inside a 30-day range",
         "SELECT source, count(*) FROM leads WHERE created_at >= now() - interval '30 days' "
         "AND source = ANY(CAST(:src AS text[])) GROUP BY source",
         {"src": ["facebook", "google_ads"]}),
        ("Leads page shape: the same filter inside a CTE over a view (does the predicate push down?)",
         "WITH b AS (SELECT v.id AS lead_id, v.source AS src, v.created_at FROM v_leads_summary v) "
         "SELECT count(*) FROM b WHERE b.created_at >= now() - interval '30 days'", {}),
    ]
    conn.exec_driver_sql(
        "CREATE VIEW v_leads_summary AS SELECT l.id, l.full_name, l.email, l.phone, l.company, l.source, "
        "l.created_at, l.sla_due_at FROM leads l")
    conn.commit()

    rep.say("")
    rep.say("  BEFORE the new indexes (leads has only the email / phone / pkey indexes it has today):")
    for label, sql, params in lead_queries:
        results.append(explain(conn, rep, "before | " + label, sql, params, show=8))

    rep.say("")
    rep.say(f"  applying {DDL_LEADS_INDEXES} ...")
    t0 = time.monotonic()
    run_sql_file(conn, DDL_LEADS_INDEXES)
    conn.commit()
    rep.say(f"  ... both indexes built on {args.lead_rows:,} rows in {time.monotonic() - t0:.1f}s")
    rep.say("")
    rep.say("  AFTER the new indexes:")
    for label, sql, params in lead_queries:
        results.append(explain(conn, rep, "after  | " + label, sql, params, show=8))

    rep.head("2. MARKETING SCHEMA - installing the real DDL into the throwaway schema")
    t0 = time.monotonic()
    run_sql_file(conn, DDL_MARKETING)
    run_sql_file(conn, DDL_CURRENCY)
    conn.commit()
    rep.say(f"  {DDL_MARKETING} applied in {time.monotonic() - t0:.1f}s (it is the same file that installs into public)")

    from erp.marketing import schema as mschema
    missing = mschema.missing_tables(conn, schema)
    rep.say(f"  tables present: {len(mschema.MARKETING_TABLES) - len(missing)}/{len(mschema.MARKETING_TABLES)}"
            + (f" MISSING: {missing}" if missing else ""))

    end_month = date.today().replace(day=1)
    months = [end_month]
    for _ in range(args.months):
        months.append((months[-1] - timedelta(days=1)).replace(day=1))
    created = mschema.ensure_partitions(conn, schema, months)
    conn.commit()
    rep.say(f"  monthly partitions created by the pipeline's own ensure_partitions(): {len(created)}")

    build_dimensions(conn, rep, args.lead_rows)
    build_landing(conn, rep, LANDING_ROWS, days)
    conn.commit()
    build_fact(conn, rep, args.fact_rows, args.months, end_month)
    conn.commit()

    rep.say("  ANALYZE ...")
    t0 = time.monotonic()
    conn.exec_driver_sql("ANALYZE")
    conn.commit()
    rep.say(f"  ... {time.monotonic() - t0:.1f}s")
    rep.say(_sizes(conn, rep, schema))

    rep.head("3. THE QUERY SHAPES A REPORT PAGE WOULD RUN, at this size")
    d1 = (end_month - timedelta(days=45)).isoformat()
    d2 = (end_month - timedelta(days=15)).isoformat()
    fact_queries = [
        ("date range + channel filter (the filter every page starts with)",
         "SELECT count(*) AS n, sum(clicks) AS clicks, sum(impressions) AS impressions, "
         "sum(spend_micros) / 1000000.0 AS spend FROM interaction_fact "
         "WHERE event_date >= CAST(:d1 AS date) AND event_date < CAST(:d2 AS date) "
         "AND channel_id = ANY(CAST(:ch AS smallint[]))", {"d1": d1, "d2": d2, "ch": [1, 2, 3]}),
        ("rollup: per channel per day over the same range (the chart behind a channel report)",
         "SELECT c.channel_key, f.event_date, sum(f.impressions) AS imp, sum(f.clicks) AS clk, "
         "sum(f.conversions) AS conv, sum(f.spend_micros) / 1000000.0 AS spend "
         "FROM interaction_fact f JOIN marketing_channel c ON c.channel_id = f.channel_id "
         "WHERE f.event_date >= CAST(:d1 AS date) AND f.event_date < CAST(:d2 AS date) "
         "GROUP BY 1, 2 ORDER BY 1, 2", {"d1": d1, "d2": d2}),
        ("one day, one channel (the narrowest slice a drill-down asks for)",
         "SELECT count(*) FROM interaction_fact WHERE event_date = CAST(:d1 AS date) AND channel_id = 3",
         {"d1": d1}),
        ("scoped GIN: an indexed attrs key inside a date range",
         "SELECT count(*) FROM interaction_fact WHERE event_date >= CAST(:d1 AS date) "
         "AND event_date < CAST(:d2 AS date) AND attrs_idx @> CAST(:j AS jsonb)",
         {"d1": d1, "d2": d2, "j": '{"device": "mobile", "country": "vn"}'}),
        ("an attrs key that is NOT on the allowlist (the honest cost of not indexing everything)",
         "SELECT count(*) FROM interaction_fact WHERE event_date >= CAST(:d1 AS date) "
         "AND event_date < CAST(:d2 AS date) AND attrs ->> 'ad_position' = '3'", {"d1": d1, "d2": d2}),
        ("drill-down: every interaction of one known lead, all history (no date filter to prune with)",
         "SELECT f.event_date, f.event_type, f.clicks FROM interaction_fact f "
         "JOIN marketing_identity mi ON mi.identity_id = f.identity_id "
         "WHERE mi.lead_id = 42 ORDER BY f.event_date DESC LIMIT 50", {}),
        ("BRIN on the append-only landing table: what arrived in the last two days",
         "SELECT count(*) FROM marketing_landing WHERE received_at >= now() - interval '2 days'", {}),
        ("full-table rollup with NO date filter (what a report must never do at this size)",
         "SELECT channel_id, sum(clicks) FROM interaction_fact GROUP BY 1", {}),
    ]
    for label, sql, params in fact_queries:
        results.append(explain(conn, rep, label, sql, params))

    rep.head("4. THE REAL PYTHON PIPELINE, end to end")
    run_real_pipeline(schema, rep, PIPELINE_ROWS)

    rep.head("5. COUNTS (proof the data is really there)")
    for table in ("leads", "marketing_landing", "interaction_fact", "marketing_channel",
                  "marketing_campaign", "marketing_creative", "marketing_identity", "marketing_ingest_run"):
        n = conn.execute(text(f"SELECT count(*) FROM {schema}.{table}")).scalar()
        rep.say(f"  {table:<24} {n:>12,}")
    return results


def _sizes(conn, rep: Report, schema: str) -> str:
    rows = conn.execute(text("""
        SELECT c.relname AS name, pg_total_relation_size(c.oid) AS bytes
          FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = :s AND c.relkind IN ('r', 'p')
         ORDER BY 2 DESC LIMIT 8
    """), {"s": schema}).mappings().all()
    # sum(bigint) is PostgreSQL `numeric` -> psycopg2 hands back a decimal.Decimal (unlike the plain bigint rows
    # above, which arrive as int) - dividing that by the float literal below raised TypeError, only ever hit here
    # because every earlier run of this script crashed before reaching this line.
    total = int(conn.execute(text("""
        SELECT sum(pg_total_relation_size(c.oid)) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = :s"""), {"s": schema}).scalar() or 0)
    rep.say("")
    rep.say("  on-disk size (table + indexes + toast):")
    for r in rows:
        rep.say(f"    {r['name']:<28} {r['bytes'] / 1e6:>10,.0f} MB")
    return f"    {'TOTAL of the schema':<28} {total / 1e6:>10,.0f} MB"


if __name__ == "__main__":
    sys.exit(main())
