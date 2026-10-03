"""
Packs and restores the FAKE sample data (social listening + sample leads) as compressed CSV files kept in git, so a machine with
no network can get the same data set with one command and no generator run.

  python db/sample/sample_data.py load      replace the sample data in the database with db/sample/data/*.csv.gz
  python db/sample/sample_data.py export    rewrite db/sample/data/*.csv.gz from what the database holds now

What `load` needs: the schemas of db/sql/01..05, 07..12 (and 14 for Power BI) already run, and the Sales reps of db/sql/04_seed_sales.sql.
What `load` touches (in ONE transaction, all or nothing):
  * the whole `sl` social-listening DATA (brands, keywords, events, batches, raw and clean mentions, topics per mention, rejects). The
    dictionaries (sl.topic, sl.sentiment_term, sl.spam_pattern) are not replaced: the mentions are linked to topics by CODE.
  * the sample LEADS only: rows whose e-mail ends in @example.com (and their assignments and replies). Any other lead is left alone, and the
    sample leads get new ids, so they cannot collide with real ones.
Everything in the files is invented: brands, posts, people and numbers.
"""
from __future__ import annotations

import argparse
import gzip
import io
import sys
from pathlib import Path

import psycopg2

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from erp import config  # noqa: E402

DATA = Path(__file__).resolve().parent / "data"
SAMPLE_LEAD = "email LIKE '%@example.com'"

# file name -> COPY ... TO STDOUT select. Names (brand, topic code, rep e-mail) stand in for ids that differ between databases.
EXPORT = {
    "sl_brand": "SELECT name, is_own FROM sl.brand ORDER BY id",
    "sl_keyword": "SELECT k.keyword, k.keyword_group, b.name AS brand FROM sl.keyword k LEFT JOIN sl.brand b ON b.id = k.brand_id ORDER BY k.id",
    "sl_event": "SELECT event_date, label FROM sl.event ORDER BY id",
    "sl_import_batch": "SELECT id, source_file, loaded_at, cleaned_at, rows_in, rows_kept, rows_dup, rows_spam, rows_bad FROM sl.import_batch ORDER BY id",
    "sl_raw_mention": "SELECT id, batch_id, platform, url, author, content, posted_at, likes, comments, shares, views, matched_keyword, extra, processed_at FROM sl.raw_mention ORDER BY id",
    "sl_mention": ("SELECT m.id, m.first_raw_id, m.content_hash, m.platform, m.url, m.author, m.content, m.content_norm, m.posted_at, m.posted_date, m.likes, m.comments, "
                   "m.shares, m.views, b.name AS brand, m.keyword_group, m.is_spam, m.spam_reason, m.sentiment_score, m.sentiment, m.keywords "
                   "FROM sl.mention m LEFT JOIN sl.brand b ON b.id = m.brand_id ORDER BY m.id"),
    "sl_mention_topic": "SELECT mt.mention_id, t.code AS topic_code FROM sl.mention_topic mt JOIN sl.topic t ON t.id = mt.topic_id ORDER BY mt.mention_id, t.id",
    "sl_reject": "SELECT raw_id, reason FROM sl.reject ORDER BY raw_id",
    "leads": f"SELECT id AS sample_id, full_name, email, phone, company, source, created_at, sla_due_at FROM leads WHERE {SAMPLE_LEAD} ORDER BY id",
    "lead_assignments": (f"SELECT la.lead_id AS sample_id, u.email AS rep_email, la.assignment_reason, la.is_current, la.assigned_at FROM lead_assignments la "
                         f"JOIN users u ON u.id = la.sales_rep_id JOIN leads l ON l.id = la.lead_id WHERE l.{SAMPLE_LEAD} ORDER BY la.id"),
    "lead_updates": (f"SELECT u.lead_id AS sample_id, u.source, u.content, u.author_name, u.occurred_at, u.synced_at FROM lead_updates u "
                     f"JOIN leads l ON l.id = u.lead_id WHERE l.{SAMPLE_LEAD} ORDER BY u.id"),
}
LOAD_ORDER = list(EXPORT)


DBNAME = None      # set by --dbname; otherwise DB_NAME of .env


def connect():
    return psycopg2.connect(host=config.DB_HOST, port=config.DB_PORT, dbname=DBNAME or config.DB_NAME, user=config.DB_USER, password=config.DB_PASSWORD)


def export() -> int:
    DATA.mkdir(exist_ok=True)
    with connect() as conn, conn.cursor() as cur:
        conn.set_session(readonly=True)
        for name, sql in EXPORT.items():
            buf = io.BytesIO()
            cur.copy_expert(f"COPY ({sql}) TO STDOUT WITH (FORMAT csv, HEADER true)", buf)
            raw = buf.getvalue()
            with open(DATA / f"{name}.csv.gz", "wb") as fh, gzip.GzipFile(fileobj=fh, mode="wb", mtime=0, compresslevel=9) as gz:   # mtime=0: identical bytes for identical data
                gz.write(raw)
            rows = raw.count(b"\n") - 1
            kb = (DATA / (name + ".csv.gz")).stat().st_size / 1024
            print(f"{name:18} {rows:>7} rows  {kb:>8.0f} KB")
    return 0


def _copy_in(cur, table: str, name: str) -> None:
    with gzip.open(DATA / f"{name}.csv.gz", "rb") as fh:
        cur.copy_expert(f"COPY {table} FROM STDIN WITH (FORMAT csv, HEADER true)", fh)


def load() -> int:
    missing = [n for n in LOAD_ORDER if not (DATA / f"{n}.csv.gz").exists()]
    if missing:
        print("missing files in db/sample/data: " + ", ".join(missing))
        return 1
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('sl.mention') IS NOT NULL, to_regclass('sl.raw_mention') IS NOT NULL")
            if not all(cur.fetchone()):
                print("The sl schema is not installed. Run db/sql/12_social_listening.sql first.")
                return 1
            cur.execute("SELECT count(*) FROM users WHERE role = 'sales'")
            if cur.fetchone()[0] == 0:
                print("No Sales reps in users. Run db/sql/04_seed_sales.sql first.")
                return 1
            # ---- social listening: replace the data, keep the dictionaries
            cur.execute("TRUNCATE sl.mention_topic, sl.mention, sl.reject, sl.raw_mention, sl.import_batch, sl.keyword, sl.brand, sl.event RESTART IDENTITY CASCADE")
            cur.execute("CREATE TEMP TABLE t_brand (name text, is_own boolean) ON COMMIT DROP")
            _copy_in(cur, "t_brand", "sl_brand")
            cur.execute("INSERT INTO sl.brand(name, is_own) SELECT name, is_own FROM t_brand")
            cur.execute("CREATE TEMP TABLE t_kw (keyword text, keyword_group text, brand text) ON COMMIT DROP")
            _copy_in(cur, "t_kw", "sl_keyword")
            cur.execute("INSERT INTO sl.keyword(keyword, keyword_group, brand_id) SELECT k.keyword, k.keyword_group, b.id FROM t_kw k LEFT JOIN sl.brand b ON b.name = k.brand")
            _copy_in(cur, "sl.event(event_date, label)", "sl_event")
            _copy_in(cur, "sl.import_batch(id, source_file, loaded_at, cleaned_at, rows_in, rows_kept, rows_dup, rows_spam, rows_bad)", "sl_import_batch")
            _copy_in(cur, "sl.raw_mention(id, batch_id, platform, url, author, content, posted_at, likes, comments, shares, views, matched_keyword, extra, processed_at)", "sl_raw_mention")
            cur.execute("CREATE TEMP TABLE t_m (id bigint, first_raw_id bigint, content_hash char(32), platform text, url text, author text, content text, content_norm text, "
                        "posted_at timestamptz, posted_date date, likes int, comments int, shares int, views bigint, brand text, keyword_group text, is_spam boolean, "
                        "spam_reason text, sentiment_score smallint, sentiment text, keywords text[]) ON COMMIT DROP")
            _copy_in(cur, "t_m", "sl_mention")
            cur.execute("INSERT INTO sl.mention(id, first_raw_id, content_hash, platform, url, author, content, content_norm, posted_at, posted_date, likes, comments, shares, views, "
                        "brand_id, keyword_group, is_spam, spam_reason, sentiment_score, sentiment, keywords) "
                        "SELECT m.id, m.first_raw_id, m.content_hash, m.platform, m.url, m.author, m.content, m.content_norm, m.posted_at, m.posted_date, m.likes, m.comments, m.shares, "
                        "m.views, b.id, m.keyword_group, m.is_spam, m.spam_reason, m.sentiment_score, m.sentiment, m.keywords FROM t_m m LEFT JOIN sl.brand b ON b.name = m.brand")
            cur.execute("CREATE TEMP TABLE t_mt (mention_id bigint, topic_code text) ON COMMIT DROP")
            _copy_in(cur, "t_mt", "sl_mention_topic")
            cur.execute("SELECT count(*) FROM t_mt x LEFT JOIN sl.topic t ON t.code = x.topic_code WHERE t.id IS NULL")
            if cur.fetchone()[0]:
                conn.rollback()
                print("The data uses topic codes that sl.topic does not have. Run db/sql/12_social_listening.sql (it seeds the topics) and try again.")
                return 1
            cur.execute("INSERT INTO sl.mention_topic(mention_id, topic_id) SELECT x.mention_id, t.id FROM t_mt x JOIN sl.topic t ON t.code = x.topic_code")
            _copy_in(cur, "sl.reject(raw_id, reason)", "sl_reject")
            for seq, tbl in (("sl.raw_mention_id_seq", "sl.raw_mention"), ("sl.mention_id_seq", "sl.mention"), ("sl.import_batch_id_seq", "sl.import_batch")):
                cur.execute(f"SELECT setval('{seq}', coalesce((SELECT max(id) FROM {tbl}), 1))")
            # ---- sample leads: replace only the @example.com ones; new ids, old id kept in raw_payload to link the children
            cur.execute(f"DELETE FROM leads WHERE {SAMPLE_LEAD}")
            cur.execute("CREATE TEMP TABLE t_l (sample_id int, full_name text, email text, phone text, company text, source text, created_at timestamptz, sla_due_at timestamptz) ON COMMIT DROP")
            _copy_in(cur, "t_l", "leads")
            cur.execute("INSERT INTO leads(full_name, email, phone, company, source, raw_payload, created_at, sla_due_at) "
                        "SELECT full_name, email, phone, company, source, jsonb_build_object('sample_id', sample_id), created_at, sla_due_at FROM t_l ORDER BY sample_id")
            cur.execute("CREATE TEMP TABLE t_la (sample_id int, rep_email text, assignment_reason text, is_current boolean, assigned_at timestamptz) ON COMMIT DROP")
            _copy_in(cur, "t_la", "lead_assignments")
            cur.execute("SELECT count(*) FROM t_la a LEFT JOIN users u ON u.email = a.rep_email WHERE u.id IS NULL")
            if cur.fetchone()[0]:
                conn.rollback()
                print("Some Sales reps of the sample (by e-mail) are not in users. Run db/sql/04_seed_sales.sql and try again.")
                return 1
            cur.execute("INSERT INTO lead_assignments(lead_id, sales_rep_id, assignment_reason, is_current, assigned_at) "
                        "SELECT l.id, u.id, a.assignment_reason, a.is_current, a.assigned_at FROM t_la a "
                        "JOIN leads l ON (l.raw_payload->>'sample_id')::int = a.sample_id AND l.email LIKE '%@example.com' JOIN users u ON u.email = a.rep_email")
            cur.execute("CREATE TEMP TABLE t_lu (sample_id int, source text, content text, author_name text, occurred_at timestamptz, synced_at timestamptz) ON COMMIT DROP")
            _copy_in(cur, "t_lu", "lead_updates")
            cur.execute("INSERT INTO lead_updates(lead_id, source, content, author_name, occurred_at, synced_at) "
                        "SELECT l.id, u.source, u.content, u.author_name, u.occurred_at, u.synced_at FROM t_lu u "
                        "JOIN leads l ON (l.raw_payload->>'sample_id')::int = u.sample_id AND l.email LIKE '%@example.com'")
            for label, sql in (("social mentions", "SELECT count(*) FROM sl.mention"), ("raw rows", "SELECT count(*) FROM sl.raw_mention"),
                               ("mention-topic links", "SELECT count(*) FROM sl.mention_topic"), ("sample leads", f"SELECT count(*) FROM leads WHERE {SAMPLE_LEAD}"),
                               ("lead replies", "SELECT count(*) FROM lead_updates")):
                cur.execute(sql)
                print(f"{label:20} {cur.fetchone()[0]:>8}")
    print("sample data loaded")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["load", "export"])
    ap.add_argument("--dbname", help="database to use instead of DB_NAME in .env (for a trial run on a scratch database)")
    a = ap.parse_args()
    global DBNAME
    DBNAME = a.dbname
    return {"load": load, "export": export}[a.action]()


if __name__ == "__main__":
    sys.exit(main())
