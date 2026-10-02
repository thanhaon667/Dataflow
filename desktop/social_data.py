"""
Live data feed for the Social page of ERP Desk: social listening for a delivery brand.
  GET /api/social    every cube the page needs (JSON): mentions by day x brand x channel x sentiment, topic counts,
                     hour-of-week counts, top posts, top authors, keyword counts, the cleaning funnel and the event list.

WHAT IT READS. Only the `sl` schema (db/sql/12_social_listening.sql): the views `sl.v_mention` (spam already excluded),
`sl.mention_topic` + `sl.topic`, `sl.keyword`, `sl.import_batch`, `sl.reject`, `sl.event`. Nothing is written. The page does
its own slicing (date range, channel, brand focus) in the browser from these cubes, so one request serves every filter.

Page states, rendered without an error: not_installed (the `sl` schema or a view is missing -> the owner-run psql command),
empty (installed, no clean mention yet -> how to load) and ready.

Contract, like the other feeds: never raises into the caller; a few seconds of cache; one read-only snapshot per request;
the only accepted query parameter is `fresh` (any other NAME is a 400 that names it, checked before the cache, lesson L-098).
"""
from __future__ import annotations

import logging
import threading
import time

from sqlalchemy import text

from desktop.today_util import arm_timeouts
from erp.db import read_connect, read_engine as engine

logger = logging.getLogger("erp_desk.social")

CACHE_SECONDS = 10.0
KNOWN_PARAMS = ("fresh",)
SENTIMENTS = ("negative", "neutral", "positive")
TOP_POSTS = 40
TOP_AUTHORS = 12
LOCAL_TZ = "Asia/Ho_Chi_Minh"           # listening exports are Vietnamese: hour-of-week is read in that zone
INSTALL_CMD = "psql -U erp_app -h 127.0.0.1 -d erp_support -f db/sql/12_social_listening.sql"
SAMPLE_CMD = "python db/sample/sl_generate_sample.py"     # fake data, to try the page
NEEDED = ("v_mention", "mention_topic", "topic", "import_batch", "reject", "event", "keyword", "brand")


def unknown_params(params) -> list[str]:
    return [k for k in params if k not in KNOWN_PARAMS]


def _rows(conn, sql: str, **bind):
    return conn.execute(text(sql), bind).fetchall()


def build(conn) -> dict:
    """The whole payload from one open connection. Pure SQL reads; raises only if the database does."""
    have = {r[0] for r in _rows(conn, "SELECT table_name FROM information_schema.tables WHERE table_schema = 'sl'")}
    missing = [t for t in NEEDED if t not in have]
    if missing:
        return {"ok": True, "state": "not_installed", "missing": missing, "install": INSTALL_CMD, "sample": SAMPLE_CMD}
    span = _rows(conn, "SELECT min(posted_date), max(posted_date), count(*) FROM sl.v_mention WHERE brand_id IS NOT NULL")[0]
    if not span[2]:
        return {"ok": True, "state": "empty", "install": INSTALL_CMD, "sample": SAMPLE_CMD}
    d0, d1 = span[0], span[1]
    brands = [r[0] for r in _rows(conn, "SELECT name FROM sl.brand ORDER BY is_own DESC, id")]
    plats = [r[0] for r in _rows(conn, "SELECT platform FROM sl.v_mention GROUP BY 1 ORDER BY count(*) DESC, 1")]
    topics = [list(r) for r in _rows(conn, "SELECT code, label, theme FROM sl.topic ORDER BY id")]
    bi = {b: i for i, b in enumerate(brands)}
    pi = {p: i for i, p in enumerate(plats)}
    ti = {t[0]: i for i, t in enumerate(topics)}
    cube = [[(r[0] - d0).days, bi[r[1]], pi[r[2]], SENTIMENTS.index(r[3]), int(r[4]), int(r[5]), int(r[6])] for r in _rows(conn, """
        SELECT posted_date, brand, platform, sentiment, count(*), coalesce(sum(engagement), 0), coalesce(sum(views), 0)
        FROM sl.v_mention WHERE brand IS NOT NULL GROUP BY 1, 2, 3, 4""")]
    by_topic = [[(r[0] - d0).days, bi[r[1]], ti[r[2]], SENTIMENTS.index(r[3]), int(r[4])] for r in _rows(conn, """
        SELECT m.posted_date, m.brand, t.code, m.sentiment, count(*)
        FROM sl.v_mention m JOIN sl.mention_topic mt ON mt.mention_id = m.id JOIN sl.topic t ON t.id = mt.topic_id
        WHERE m.brand IS NOT NULL GROUP BY 1, 2, 3, 4""") if r[2] in ti]
    hours = [[bi[r[0]], int(r[1]), int(r[2]), int(r[3])] for r in _rows(conn, """
        SELECT brand, extract(isodow FROM posted_at AT TIME ZONE :tz) - 1, extract(hour FROM posted_at AT TIME ZONE :tz), count(*)
        FROM sl.v_mention WHERE brand IS NOT NULL GROUP BY 1, 2, 3""", tz=LOCAL_TZ)]
    top = [[str(r[0]), r[1], r[2], r[3], int(r[4]), int(r[5]), r[6]] for r in _rows(conn, """
        SELECT posted_date, platform, brand, sentiment, engagement, views, left(content, 140)
        FROM sl.v_mention WHERE brand IS NOT NULL ORDER BY engagement DESC, id LIMIT :n""", n=TOP_POSTS)]
    own = _rows(conn, "SELECT name FROM sl.brand WHERE is_own ORDER BY id LIMIT 1")
    authors = [[r[0], int(r[1]), int(r[2]), int(r[3])] for r in _rows(conn, """
        SELECT author, count(*), sum(engagement), round(100.0 * avg((sentiment = 'negative')::int))
        FROM sl.v_mention WHERE brand = :b AND author IS NOT NULL GROUP BY 1 HAVING count(*) >= 5
        ORDER BY 3 DESC LIMIT :n""", b=own[0][0] if own else "", n=TOP_AUTHORS)]
    keywords = [[r[0], int(r[1])] for r in _rows(conn, "SELECT k, count(*) FROM sl.v_mention m, unnest(m.keywords) k GROUP BY 1 ORDER BY 2 DESC LIMIT 15")]
    qual = _rows(conn, """SELECT coalesce(sum(rows_in), 0), coalesce(sum(rows_kept), 0), coalesce(sum(rows_dup), 0),
                                 coalesce(sum(rows_spam), 0), coalesce(sum(rows_bad), 0) FROM sl.import_batch""")[0]
    events = [[(r[0] - d0).days, r[1]] for r in _rows(conn, "SELECT event_date, label FROM sl.event WHERE event_date BETWEEN :a AND :b ORDER BY 1", a=d0, b=d1)]
    return {"ok": True, "state": "ready", "d0": str(d0), "d1": str(d1), "brands": brands, "own": 0 if own else None, "plats": plats,
            "sents": list(SENTIMENTS), "topics": topics, "cube": cube, "by_topic": by_topic, "hours": hours, "top": top,
            "authors": authors, "keywords": keywords, "events": events,
            "quality": dict(zip(("rows_in", "rows_kept", "rows_dup", "rows_spam", "rows_bad"), map(int, qual))),
            "tz": LOCAL_TZ}


class SocialStore:
    """Builds the /api/social payload on demand with a few seconds of cache. No thread, nothing to stop. Never raises."""

    def __init__(self, ttl: float = CACHE_SECONDS) -> None:
        self.ttl = ttl
        self._lock = threading.Lock()
        self._hit: tuple[float, tuple[int, dict]] | None = None

    def get(self, params, fresh: bool = False) -> tuple[int, dict]:
        bad = unknown_params(params)
        if bad:
            return 400, {"ok": False, "error": f"Invalid {bad[0]}: unknown parameter", "problems": [{"param": bad[0], "why": "unknown parameter"}]}
        now = time.monotonic()
        with self._lock:
            if not fresh and self._hit and now - self._hit[0] < self.ttl:
                return self._hit[1]
        try:
            with read_connect(engine).execution_options(postgresql_readonly=True, isolation_level="REPEATABLE READ") as conn:
                arm_timeouts(conn, 8000)
                payload = build(conn)
                conn.rollback()
            result = (200, payload)
        except Exception as exc:  # noqa: BLE001 - never a 500 for the page
            logger.warning("Social feed unavailable: %s: %s", type(exc).__name__, str(exc).splitlines()[0][:160])
            return 503, {"ok": False, "unavailable": True, "error": "Can't read the social listening data right now. Is PostgreSQL running?"}
        with self._lock:
            self._hit = (now, result)
        return result
