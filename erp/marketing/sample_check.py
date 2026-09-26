"""One-off VERIFICATION of the two source-specific connectors on REAL sample files, in a THROWAWAY schema.

It proves, with direct SQL against the source files (parsed here with plain `csv` + regex, NOT with the connectors'
own parsers), that the ad_performance and email_campaign connectors load the owner's exports completely and correctly:

  1. installs the real DDL 07 + 10 + 09 into a throwaway schema (perf_...), and proves migration 10 is safe on an
     installed, POPULATED partitioned table (idempotent, no rewrite, existing rows read as the default);
  2. dry-run, then the real load of both files through `python -m erp.marketing.ingest` (the run summaries are printed);
  3. row counts, the sum of EVERY measure, spend and revenue PER CURRENCY, every row cell for cell (as a multiset), the 6
     duplicate-key pairs (both rows present), the scoped-GIN filter on brand / country / segment / device;
  4. builds the rollup with the real job code and compares it with a direct GROUP BY of the facts, per currency and grain;
  5. imports both files a SECOND time: 0 new rows, and the fact and rollup fingerprints do not change (idempotent).

WHAT IT NEVER DOES: touch `public`. The schema name must match perf_[a-z0-9_]+, every object is created inside it, the
schema is dropped in a `finally` (unless --keep), and the run compares the list of schemas and of `public` tables before
and after and says so. It never edits the data files.

Run:
  venv\\Scripts\\python.exe -u -m erp.marketing.sample_check --yes --data1 data_inbox\\data1.csv --data2 data_inbox\\data2.csv
  venv\\Scripts\\python.exe -u -m erp.marketing.sample_check --yes --data1 ... --data2 ... --keep      (leave the schema for a look)
  venv\\Scripts\\python.exe -u -m erp.marketing.sample_check --drop perf_samples                       (remove a kept schema)

`marketing_rollup.log` is not written (the job code is called in-process, not through its command line).
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import io
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import text

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_RE = re.compile(r"^perf_[a-z0-9_]{1,40}$")
DDL = ("db/sql/07_marketing_schema.sql", "db/sql/10_marketing_currency.sql", "db/sql/09_marketing_rollup.sql")


class Report:
    def __init__(self) -> None:
        self.checks: list[tuple[str, bool, str]] = []

    def head(self, title: str) -> None:
        print("\n" + "=" * 78 + f"\n{title}\n" + "=" * 78, flush=True)

    def say(self, line: str = "") -> None:
        print(line, flush=True)

    def check(self, name: str, ok: bool, detail: object = "") -> bool:
        self.checks.append((name, bool(ok), str(detail)))
        print(f"  {'ok  ' if ok else 'FAIL'} {name}" + ("" if ok or detail == "" else f"   <- {detail}"), flush=True)
        return bool(ok)


# --------------------------------------------------------------------------- the source files, read independently
def _digits(s: str) -> int:
    return int(re.sub(r"[^\d]", "", s))


def read_source1(path: Path) -> list[dict]:
    """data1 rows as plain typed dicts, parsed with csv + regex only."""
    out = []
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            purchases = int(r["Purchase"])
            per = _digits(r["Revenue/pur"])
            out.append({
                "day": datetime.strptime(r["Start"], "%m/%d/%Y").date(), "end": datetime.strptime(r["End"], "%m/%d/%Y").date(),
                "campaign": r["Campaign"], "adset": r["Adset"], "ad": r["Ad name"], "device": r["Device"],
                "impressions": int(r["Impression"]), "clicks": int(r["Link click"]), "sessions": int(r["LP view"]),
                "reactions": int(r["Eng"]), "conversions": purchases, "leads": int(r["Lead"]),
                "spend": _digits(r["Spent"]), "revenue": purchases * per, "reported_revenue_cell": r["Revenue"].strip()})
    return out


def read_source2(path: Path) -> list[dict]:
    out = []
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            out.append({
                "day": date.fromisoformat(r["send_date"]), "brand": r["brand"], "country": r["country"], "cid": r["campaign_id"],
                "segment": r["segment"], "impressions": int(r["delivered"]), "reactions": int(r["unique_opens"]),
                "clicks": int(r["unique_clicks"]), "conversions": int(r["orders"]), "revenue": Decimal(r["revenue_eur"]),
                "unsubs": int(r["unsubscribes"]), "spam": int(r["spam_complaints"])})
    return out


# --------------------------------------------------------------------------- database helpers
def scalar(conn, sql: str, params: dict | None = None):
    return conn.execute(text(sql), params or {}).scalar()


def install(conn, schema: str, rep: Report) -> None:
    conn.exec_driver_sql(f"CREATE SCHEMA {schema}")
    conn.exec_driver_sql(f"SET search_path TO {schema}")           # public is NOT on the path: an unqualified name cannot reach it
    conn.exec_driver_sql("CREATE TABLE leads (id SERIAL PRIMARY KEY)")   # the stub 07 references
    for rel in DDL:
        conn.exec_driver_sql((PROJECT_ROOT / rel).read_text(encoding="utf-8"))
    conn.exec_driver_sql("RESET search_path")                       # this connection goes back to the pool: leave it as it was found
    conn.exec_driver_sql("RESET lock_timeout")                      # (migration 10 sets one for the session)
    conn.commit()
    rep.say(f"  installed {', '.join(Path(d).name for d in DDL)} into schema '{schema}'")


def migration_proof(engine, schema: str, rep: Report) -> None:
    """Migration 10 on an installed, populated, partitioned table: idempotent, no rewrite, old rows read as the default."""
    mig = schema + "_mig"
    with engine.connect() as conn:
        try:
            conn.exec_driver_sql(f"CREATE SCHEMA {mig}")
            conn.exec_driver_sql(f"SET search_path TO {mig}")
            conn.exec_driver_sql("CREATE TABLE leads (id SERIAL PRIMARY KEY)")
            conn.exec_driver_sql((PROJECT_ROOT / DDL[0]).read_text(encoding="utf-8"))
            conn.exec_driver_sql(f"CREATE TABLE {mig}.interaction_fact_2026_09 PARTITION OF {mig}.interaction_fact "
                                 "FOR VALUES FROM ('2026-09-01') TO ('2026-10-01')")
            conn.exec_driver_sql(f"INSERT INTO {mig}.marketing_source (source_key) VALUES ('t')")
            conn.exec_driver_sql(f"INSERT INTO {mig}.marketing_channel (channel_key) VALUES ('c')")
            conn.exec_driver_sql(f"INSERT INTO {mig}.interaction_fact (event_date, dedupe_key, source_id, channel_id, event_type, clicks, spend_micros) "
                                 "SELECT DATE '2026-09-05', md5(g::text)::uuid, 1, 1, 'click', g, g * 1000000 FROM generate_series(1, 25) g")
            conn.commit()
            before = scalar(conn, f"SELECT pg_relation_filenode('{mig}.interaction_fact_2026_09')")
            has0 = scalar(conn, f"SELECT count(*) FROM pg_attribute WHERE attrelid = '{mig}.interaction_fact'::regclass AND attname = 'currency'")
            sql10 = (PROJECT_ROOT / DDL[1]).read_text(encoding="utf-8")
            conn.exec_driver_sql(sql10)
            conn.commit()
            after = scalar(conn, f"SELECT pg_relation_filenode('{mig}.interaction_fact_2026_09')")
            rep.check("migration 10 adds the column to the parent and to the existing partition",
                      has0 == 0 and scalar(conn, f"SELECT count(*) FROM pg_attribute WHERE attrelid = '{mig}.interaction_fact_2026_09'::regclass AND attname = 'currency' AND NOT attisdropped") == 1)
            rep.check("migration 10 did not rewrite the partition (same relfilenode: a constant default is catalog-only)", before == after, (before, after))
            rep.check("the 25 rows that existed before read as the default currency USD",
                      scalar(conn, f"SELECT count(*) FROM {mig}.interaction_fact WHERE currency = 'USD'") == 25)
            conn.exec_driver_sql(sql10)                 # the second run: IF NOT EXISTS, no error
            conn.commit()
            rep.check("running migration 10 a second time is a no-op (idempotent, no error)",
                      scalar(conn, f"SELECT count(*) FROM pg_attribute WHERE attrelid = '{mig}.interaction_fact'::regclass AND attname = 'currency'") == 1
                      and scalar(conn, f"SELECT pg_relation_filenode('{mig}.interaction_fact_2026_09')") == before)
            conn.exec_driver_sql(f"CREATE TABLE {mig}.interaction_fact_2026_10 PARTITION OF {mig}.interaction_fact FOR VALUES FROM ('2026-10-01') TO ('2026-11-01')")
            rep.check("a partition created AFTER the migration has the column too",
                      scalar(conn, f"SELECT count(*) FROM pg_attribute WHERE attrelid = '{mig}.interaction_fact_2026_10'::regclass AND attname = 'currency' AND NOT attisdropped") == 1)
            for bad in ("vnd", "VN", "VNDD", "V1D", ""):
                try:
                    conn.exec_driver_sql(f"INSERT INTO {mig}.interaction_fact (event_date, dedupe_key, source_id, channel_id, event_type, currency) "
                                         f"VALUES ('2026-09-06', md5('x{bad}')::uuid, 1, 1, 'click', '{bad}')")
                    conn.rollback()
                    rep.check(f"the CHECK refuses the currency {bad!r}", False, "it was accepted")
                except Exception:  # noqa: BLE001
                    conn.rollback()
                    rep.check(f"the CHECK refuses the currency {bad!r}", True)
        finally:
            with contextlib.suppress(Exception):
                conn.rollback()
            conn.exec_driver_sql("RESET search_path")
            conn.exec_driver_sql("RESET lock_timeout")
            conn.exec_driver_sql(f"DROP SCHEMA IF EXISTS {mig} CASCADE")
            conn.commit()


def run_ingest(argv: list[str], rep: Report) -> tuple[int, str]:
    """The real command line, in-process, output captured AND printed."""
    from erp.marketing import ingest
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = ingest.main(argv)
    out = buf.getvalue()
    print(out, end="", flush=True)
    return code, out


def summary_number(out: str, label: str) -> int:
    m = re.search(rf"^\s*{re.escape(label)}\s+(\d+)", out, re.M)
    return int(m.group(1)) if m else -1


def fact_fingerprint(conn, schema: str) -> tuple:
    """(row count, md5 of every fact row without the load-time columns) - equal fingerprints = identical facts."""
    return tuple(conn.execute(text(f"""
        SELECT count(*), coalesce(md5(string_agg(concat_ws('|', event_date, dedupe_key, source_id, channel_id, campaign_id, creative_id,
               event_type, impressions, clicks, reactions, sessions, conversions, spend_micros, revenue_micros, currency,
               attrs::text), ',' ORDER BY event_date, dedupe_key)), '') FROM {schema}.interaction_fact""")).one())


def verify(engine, schema: str, s1: list[dict], s2: list[dict], rep: Report) -> None:
    F = f"{schema}.interaction_fact"
    with engine.connect() as conn:
        rep.head("3. THE FACTS against the source files (direct SQL vs csv + regex)")
        src = {r[0]: r[1] for r in conn.execute(text(f"SELECT so.source_key, count(*) FROM {F} f JOIN {schema}.marketing_source so USING (source_id) GROUP BY 1"))}
        rep.check("data1: one fact row per source row", src.get("ad_performance") == len(s1), (src.get("ad_performance"), len(s1)))
        rep.check("data2: one fact row per source row", src.get("email_campaign") == len(s2), (src.get("email_campaign"), len(s2)))
        rep.check("nothing else was loaded", set(src) == {"ad_performance", "email_campaign"}, src)

        def sums(source: str) -> dict:
            row = conn.execute(text(f"""SELECT sum(impressions), sum(clicks), sum(reactions), sum(sessions), sum(conversions), sum(spend_micros), sum(revenue_micros),
                                               sum(COALESCE((attrs->>'leads')::bigint, 0))
                                          FROM {F} f JOIN {schema}.marketing_source so USING (source_id) WHERE so.source_key = :s"""), {"s": source}).one()
            return dict(zip(("impressions", "clicks", "reactions", "sessions", "conversions", "spend_micros", "revenue_micros", "leads"), (int(x or 0) for x in row)))
        db1, db2 = sums("ad_performance"), sums("email_campaign")
        want1 = {"impressions": sum(r["impressions"] for r in s1), "clicks": sum(r["clicks"] for r in s1), "reactions": sum(r["reactions"] for r in s1),
                 "sessions": sum(r["sessions"] for r in s1), "conversions": sum(r["conversions"] for r in s1), "spend_micros": sum(r["spend"] for r in s1) * 10 ** 6,
                 "revenue_micros": sum(r["revenue"] for r in s1) * 10 ** 6, "leads": sum(r["leads"] for r in s1)}
        want2 = {"impressions": sum(r["impressions"] for r in s2), "clicks": sum(r["clicks"] for r in s2), "reactions": sum(r["reactions"] for r in s2),
                 "sessions": 0, "conversions": sum(r["conversions"] for r in s2), "spend_micros": 0,
                 "revenue_micros": int(sum(r["revenue"] for r in s2) * 10 ** 6), "leads": 0}
        for name in want1:
            rep.check(f"data1 sum of {name}: fact {db1[name]:,} == source {want1[name]:,}", db1[name] == want1[name])
        for name in want2:
            rep.check(f"data2 sum of {name}: fact {db2[name]:,} == source {want2[name]:,}", db2[name] == want2[name])

        cur = {r[0]: (r[1], int(r[2]), int(r[3])) for r in conn.execute(text(f"SELECT currency, count(*), sum(spend_micros), sum(revenue_micros) FROM {F} GROUP BY 1"))}
        rep.check("currencies in the fact are exactly VND and EUR", set(cur) == {"VND", "EUR"}, cur)
        rep.check(f"VND: {cur.get('VND', (0,))[0]} rows, spend {cur.get('VND', (0, 0))[1] / 1e6:,.0f}, revenue {cur.get('VND', (0, 0, 0))[2] / 1e6:,.0f} == source",
                  cur.get("VND") == (len(s1), want1["spend_micros"], want1["revenue_micros"]))
        rep.check(f"EUR: {cur.get('EUR', (0,))[0]} rows, revenue {cur.get('EUR', (0, 0, 0))[2] / 1e6:,.2f} == source, no spend",
                  cur.get("EUR") == (len(s2), 0, want2["revenue_micros"]))

        got1 = Counter(tuple(r) for r in conn.execute(text(f"""
            SELECT f.event_date, ca.name, cr.name, f.attrs->>'adset', f.attrs->>'device', f.impressions, f.clicks, f.sessions, f.reactions,
                   f.conversions, f.spend_micros, f.revenue_micros
              FROM {F} f JOIN {schema}.marketing_source so USING (source_id)
              LEFT JOIN {schema}.marketing_campaign ca ON ca.campaign_id = f.campaign_id
              LEFT JOIN {schema}.marketing_creative cr ON cr.creative_id = f.creative_id WHERE so.source_key = 'ad_performance'""")))
        exp1 = Counter((r["day"], r["campaign"], r["ad"], r["adset"], r["device"], r["impressions"], r["clicks"], r["sessions"], r["reactions"],
                        r["conversions"], r["spend"] * 10 ** 6, r["revenue"] * 10 ** 6) for r in s1)
        rep.check("data1: every fact row equals its source row, cell for cell (multiset of 118 rows)", got1 == exp1, f"{sum((got1 - exp1).values())} differ")
        got2 = Counter((r[0], r[1], r[2], r[3], r[4], int(r[5]), int(r[6]), int(r[7]), int(r[8]), int(r[9])) for r in conn.execute(text(f"""
            SELECT f.event_date, ca.name, f.attrs->>'brand', f.attrs->>'country', f.attrs->>'segment', f.impressions, f.reactions, f.clicks, f.conversions, f.revenue_micros
              FROM {F} f JOIN {schema}.marketing_source so USING (source_id)
              LEFT JOIN {schema}.marketing_campaign ca ON ca.campaign_id = f.campaign_id WHERE so.source_key = 'email_campaign'""")))
        exp2 = Counter((r["day"], r["cid"].lower(), r["brand"], r["country"], r["segment"], r["impressions"], r["reactions"], r["clicks"], r["conversions"],
                        int(r["revenue"] * 10 ** 6)) for r in s2)
        got2 = Counter({(k[0], (k[1] or "").lower(), *k[2:]): v for k, v in got2.items()})
        rep.check("data2: every fact row equals its source row, cell for cell (multiset of 128 rows)", got2 == exp2, f"{sum((got2 - exp2).values())} differ")

        # the duplicate-key pairs
        groups: dict = defaultdict(list)
        for r in s1:
            groups[(r["day"], r["campaign"], r["adset"], r["ad"], r["device"])].append(r)
        dup = {k: v for k, v in groups.items() if len(v) > 1}
        rep.check(f"the source has {len(dup)} natural keys that repeat with different metrics (expected 6 in the owner's file)", len(dup) >= 1, len(dup))
        lost = 0
        for (day, camp, adset, ad, device), rows in dup.items():
            in_db = sorted(int(x[0]) for x in conn.execute(text(f"""
                SELECT f.impressions FROM {F} f JOIN {schema}.marketing_campaign ca ON ca.campaign_id = f.campaign_id
                  JOIN {schema}.marketing_creative cr ON cr.creative_id = f.creative_id
                 WHERE f.event_date = :d AND ca.name = :c AND cr.name = :a AND f.attrs->>'adset' = :s AND f.attrs->>'device' = :v"""),
                {"d": day, "c": camp, "a": ad, "s": adset, "v": device}))
            if in_db != sorted(r["impressions"] for r in rows):
                lost += 1
        rep.check("every duplicate-key pair is present in the fact with BOTH rows' metrics (none overwritten)", lost == 0, f"{lost} groups differ")
        rep.check("the duplicate rows carry distinct occurrence-based identities", scalar(conn, f"SELECT count(*) FROM (SELECT dedupe_key FROM {F} GROUP BY 1, event_date HAVING count(*) > 1) x") == 0)

        # grain and revenue markings in attrs
        rep.check("data1 rows all say grain=week with a period_end = start + 6 days",
                  scalar(conn, f"SELECT count(*) FROM {F} WHERE attrs->>'grain' = 'week' AND (attrs->>'period_end')::date = event_date + 6") == len(s1))
        rep.check("data1 revenue is marked derived (the source Revenue column is empty)",
                  all(r["reported_revenue_cell"] == "" for r in s1) and scalar(conn, f"SELECT count(*) FROM {F} WHERE (attrs->>'revenue_derived')::boolean") == len(s1))
        rep.check("data1 leads are in attrs, not in conversions", db1["leads"] == want1["leads"] and db1["conversions"] == want1["conversions"] != db1["conversions"] + db1["leads"])
        rep.check("reach and frequency are kept in attrs and are NOT columns of the rollup",
                  scalar(conn, f"SELECT count(*) FROM {F} WHERE attrs ? 'reach' AND attrs ? 'frequency'") == len(s1)
                  and not scalar(conn, f"SELECT count(*) FROM information_schema.columns WHERE table_schema = '{schema}' AND table_name LIKE 'interaction_daily%' AND column_name IN ('reach', 'frequency')"))
        rep.check("data2 rows say grain=day and document delivered / unique_opens",
                  scalar(conn, f"SELECT count(*) FROM {F} WHERE attrs->>'grain' = 'day' AND attrs->>'impressions_are' = 'delivered' AND attrs->>'reactions_are' = 'unique_opens'") == len(s2))

        # the scoped GIN: filter by brand / country / segment / device through attrs_idx
        for key, col, rows in (("brand", "brand", s2), ("country", "country", s2), ("segment", "segment", s2), ("device", "device", s1)):
            want = Counter(r[col].casefold() for r in rows)
            got = {v: scalar(conn, f"SELECT count(*) FROM {F} WHERE attrs_idx @> CAST(:j AS jsonb)", {"j": '{"%s": "%s"}' % (key, v)}) for v in want}
            rep.check(f"attrs_idx @> {{{key}: ...}} counts every value right ({len(want)} values)", got == dict(want), got)
        rep.check("landing kept every raw row", scalar(conn, f"SELECT count(*) FROM {schema}.marketing_landing") >= len(s1) + len(s2))
        conn.rollback()


def rollup_checks(engine, schema: str, s1: list[dict], s2: list[dict], rep: Report) -> tuple:
    from erp.marketing import rollup as mroll
    from erp.marketing import rollup_check as rc
    rep.head("4. THE ROLLUP (real job code) vs a direct GROUP BY of the facts")
    res = mroll.refresh(engine, schema, full=True)
    rep.say("  " + res.summary())
    with engine.connect() as conn:
        rep.check("rollup == direct GROUP BY of the fact, campaign table + channel table, both directions (differing rows)",
                  rc._mismatches(conn, schema) == 0)
        conn.rollback()
        direct = {(r[0], r[1]): (int(r[2]), int(r[3]), int(r[4])) for r in conn.execute(text(
            f"SELECT currency, {mroll.GRAIN_SQL} AS grain, sum(clicks), sum(spend_micros), sum(revenue_micros) FROM {schema}.interaction_fact GROUP BY 1, 2"))}
        chan = {(r[0], r[1]): (int(r[2]), int(r[3]), int(r[4])) for r in conn.execute(text(
            f"SELECT currency, grain, sum(clicks), sum(spend_micros), sum(revenue_micros) FROM {schema}.interaction_daily_channel_rollup GROUP BY 1, 2"))}
        camp = {(r[0], r[1]): (int(r[2]), int(r[3]), int(r[4])) for r in conn.execute(text(
            f"SELECT currency, grain, sum(clicks), sum(spend_micros), sum(revenue_micros) FROM {schema}.interaction_daily_rollup GROUP BY 1, 2"))}
        rep.check("per (currency, grain): channel rollup == campaign rollup == fact", direct == chan == camp, (direct, chan, camp))
        rep.check("the rollup keeps VND/week and EUR/day apart (no row mixes currencies)", set(chan) == {("VND", "week"), ("EUR", "day")}, set(chan))
        want_vnd = (sum(r["clicks"] for r in s1), sum(r["spend"] for r in s1) * 10 ** 6, sum(r["revenue"] for r in s1) * 10 ** 6)
        rep.check("rollup VND totals == source totals (clicks, spend, revenue)", chan.get(("VND", "week")) == want_vnd, (chan.get(("VND", "week")), want_vnd))
        want_eur = (sum(r["clicks"] for r in s2), 0, int(sum(r["revenue"] for r in s2) * 10 ** 6))
        rep.check("rollup EUR totals == source totals", chan.get(("EUR", "day")) == want_eur, (chan.get(("EUR", "day")), want_eur))
        weeks = sorted(r[0].isoformat() for r in conn.execute(text(f"SELECT DISTINCT event_date FROM {schema}.interaction_daily_channel_rollup WHERE grain = 'week'")))
        rep.check("weekly rows sit on their START days only (3 Mondays in the sample)", weeks == sorted({r["day"].isoformat() for r in s1}), weeks)
        derived = int(scalar(conn, f"SELECT sum(revenue_derived_micros) FROM {schema}.interaction_daily_channel_rollup"))
        rep.check("the rollup carries the DERIVED share of revenue (all of VND revenue, none of EUR)", derived == want_vnd[2], derived)
        fp = rc._fingerprint(conn, schema)
        conn.rollback()
    return fp


def snapshot(engine) -> tuple:
    with engine.connect() as conn:
        schemas = sorted(r[0] for r in conn.execute(text("SELECT nspname FROM pg_namespace WHERE nspname NOT LIKE 'pg\\_%' AND nspname <> 'information_schema'")))
        tables = sorted(r[0] for r in conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")))
        conn.rollback()
    return schemas, tables


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m erp.marketing.sample_check", description=__doc__.split("\n\n")[0])
    ap.add_argument("--yes", action="store_true", help="required: confirms that a throwaway schema may be created and dropped")
    ap.add_argument("--schema", default="perf_samples", help="throwaway schema name (must match perf_[a-z0-9_]+)")
    ap.add_argument("--data1", default="data_inbox/data1.csv", help="the ad_performance export")
    ap.add_argument("--data2", default="data_inbox/data2.csv", help="the email_campaign export")
    ap.add_argument("--keep", action="store_true", help="do NOT drop the schema at the end (drop it later with --drop)")
    ap.add_argument("--drop", metavar="SCHEMA", help="drop a kept throwaway schema and exit")
    args = ap.parse_args(argv)
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        with contextlib.suppress(Exception):
            sys.stdout.reconfigure(encoding="utf-8")       # L-011: the dong sign in the summaries
    from erp.db import engine
    target = args.drop or args.schema
    if not SCHEMA_RE.match(target):
        print(f"the schema name must match perf_[a-z0-9_]+ (so it can never be public): {target!r}")
        return 2
    if args.drop:
        with engine.connect() as conn:
            conn.exec_driver_sql(f"DROP SCHEMA IF EXISTS {target} CASCADE")
            conn.commit()
        print(f"schema '{target}' dropped")
        return 0
    if not args.yes:
        print("this creates and drops a throwaway schema in the database: add --yes to confirm.")
        return 2
    p1, p2 = Path(args.data1), Path(args.data2)
    for p in (p1, p2):
        if not p.is_file():
            print(f"no such file: {p}")
            return 2
    schema = args.schema
    rep = Report()
    before = snapshot(engine)
    if schema in before[0]:
        print(f"schema '{schema}' already exists - drop it first (--drop {schema}) or choose another name")
        return 2
    s1, s2 = read_source1(p1), read_source2(p2)
    ok_run = True
    try:
        rep.head("1. INSTALL the real DDL (07 + 10 + 09) into the throwaway schema, and prove migration 10 on a POPULATED partitioned table")
        with engine.connect() as conn:
            install(conn, schema, rep)
        migration_proof(engine, schema, rep)

        rep.head("2. LOAD: dry-run, then the real load, of both files through the real command line")
        base = ["--schema", schema, "--no-resolve-leads"]
        c, out = run_ingest(["--connector", "ad_performance", "--csv", str(p1), "--dry-run", *base], rep)
        rep.check("data1 dry run: exit 0, 118 valid rows, 6 key collisions kept, nothing rejected", c == 0 and summary_number(out, "would write") == len(s1) and summary_number(out, "rejected") == 0 and "6 kept as separate rows" in out)
        c, out = run_ingest(["--connector", "email_campaign", "--csv", str(p2), "--dry-run", *base], rep)
        rep.check("data2 dry run: exit 0, 128 valid rows, nothing rejected", c == 0 and summary_number(out, "would write") == len(s2) and summary_number(out, "rejected") == 0)
        with engine.connect() as conn:
            rep.check("the dry runs wrote nothing (no fact rows, no landing rows)", scalar(conn, f"SELECT count(*) FROM {schema}.interaction_fact") == 0 and scalar(conn, f"SELECT count(*) FROM {schema}.marketing_landing") == 0)
            conn.rollback()
        c1, out1 = run_ingest(["--connector", "ad_performance", "--csv", str(p1), *base], rep)
        c2, out2 = run_ingest(["--connector", "email_campaign", "--csv", str(p2), *base], rep)
        rep.check("data1 real load: exit 0, 118 new rows, 0 restated", c1 == 0 and "(118 new, 0 restated)" in out1, out1[-300:])
        rep.check("data2 real load: exit 0, 128 new rows, 0 restated", c2 == 0 and "(128 new, 0 restated)" in out2, out2[-300:])

        verify(engine, schema, s1, s2, rep)
        fp_roll = rollup_checks(engine, schema, s1, s2, rep)

        rep.head("5. IDEMPOTENCY: the same two files again")
        with engine.connect() as conn:
            fp_fact = fact_fingerprint(conn, schema)
            conn.rollback()
        d1, out1b = run_ingest(["--connector", "ad_performance", "--csv", str(p1), *base], rep)
        d2, out2b = run_ingest(["--connector", "email_campaign", "--csv", str(p2), *base], rep)
        rep.check("second import of data1: 0 new rows, 118 restated", d1 == 0 and "(0 new, 118 restated)" in out1b, out1b[-300:])
        rep.check("second import of data2: 0 new rows, 128 restated", d2 == 0 and "(0 new, 128 restated)" in out2b, out2b[-300:])
        from erp.marketing import rollup as mroll
        from erp.marketing import rollup_check as rc
        with engine.connect() as conn:
            rep.check("the fact table is identical after the second import (row count + md5 of every row)", fact_fingerprint(conn, schema) == fp_fact)
            conn.rollback()
        mroll.refresh(engine, schema, full=True)
        with engine.connect() as conn:
            rep.check("the rollup is identical after a second refresh (row count + md5, refreshed_at excluded)", rc._fingerprint(conn, schema) == fp_roll)
            conn.rollback()
    except Exception as exc:  # noqa: BLE001
        ok_run = False
        import traceback
        traceback.print_exc()
        rep.check(f"the run finished without an exception ({type(exc).__name__}: {exc})", False)
    finally:
        with engine.connect() as conn:
            conn.rollback()
            if args.keep:
                print(f"\n--keep: schema '{schema}' left in place. Point a scratch ERP Desk at it with MARKETING_SCHEMA={schema}, "
                      f"and remove it with:  venv\\Scripts\\python.exe -m erp.marketing.sample_check --drop {schema}")
            else:
                conn.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
                conn.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema}_mig CASCADE")
                conn.commit()
    after = snapshot(engine)
    rep.head("6. NOTHING ELSE CHANGED")
    expected_schemas = sorted(set(before[0]) | ({schema} if args.keep else set()))
    rep.check("the schemas of the database are what they were before" + (" (plus the kept one)" if args.keep else ""), after[0] == expected_schemas, (before[0], after[0]))
    rep.check("the tables of `public` are exactly what they were before (nothing added, nothing removed)", after[1] == before[1], (set(after[1]) ^ set(before[1])))
    bad = [c for c in rep.checks if not c[1]]
    print(f"\n{len(rep.checks) - len(bad)}/{len(rep.checks)} checks passed" + (" - FAILURES:" if bad else ""))
    for name, _ok, detail in bad:
        print(f"  FAIL {name}   <- {detail[:300]}")
    return 0 if ok_run and not bad else 1


if __name__ == "__main__":
    sys.exit(main())
