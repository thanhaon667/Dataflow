"""
Scenario table for the marketing ingestion pipeline (erp/marketing/), kept in the repo so the Reviewer, the next
agent and the merge gate (tests/smoke.py, check 4) can re-run it (lesson L-089).

Everything here is PURE: no database, no file system, no network, so it costs the gate about a tenth of a second.

  * typing / coercion - dates in the formats real exports use, epoch seconds, thousands separators, a European
    decimal comma, money to integer micros, junk to a safe default;
  * validation - what makes a record unusable and the exact reason given for it, including the cases that must NOT
    be rejected;
  * malformed-row handling - a connector that raises on one record, a record that maps to None, a record that fails
    validation: each is counted and skipped and the batch carries on (lesson L-044);
  * dedupe - the key is stable for the same interaction, different for a different one, exact when the source gives
    an external id, and the grain-based key groups a restatement onto the same row;
  * privacy - an e-mail or phone identity is stored as a sha256 digest, never as the value;
  * the scoped GIN allowlist - which attrs keys are projected into attrs_idx, and that the rest still survive in attrs;
  * the flat-file connector - column recognition by name, the rollup-vs-single-metric decision, unknown columns going
    to attrs and nothing being silently dropped;
  * the schema helpers - partition naming and month arithmetic (including December), and the identifier guard that
    keeps a schema name out of SQL text unless it is a plain identifier.

What is NOT here, on purpose: the scale verification (a few million synthetic rows, EXPLAIN ANALYZE, partition
pruning and index usage). It costs minutes and about 2 GB of disk, so it is a one-off run of
erp/marketing/perf_check.py, and its measured numbers are written up in docs/marketing-data-architecture.md.

Run (from the project root):
    venv\\Scripts\\python.exe -B -m tests.marketing_scenarios
"""
from __future__ import annotations

import contextlib
import io
import sys
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _flat(rows, **kw):
    from erp.marketing.connectors.flat_file import FlatFileConnector
    return FlatFileConnector(rows, **kw)


def _one(row: dict, **kw):
    """One raw flat-file row -> the validated Interaction (or an exception)."""
    from erp.marketing.model import build_interaction
    c = _flat([row], **kw)
    return build_interaction(c.map_record(row))


# =========================================================================================== typing / coercion
def _check_types(add) -> None:
    from erp.marketing import model as m

    for raw, want in [("2026-09-24", date(2026, 9, 24)), ("24/09/2026", date(2026, 9, 24)),
                      ("2026/09/24", date(2026, 9, 24)), ("20260924", date(2026, 9, 24)),
                      ("2026-09-24T18:30:00Z", date(2026, 9, 24)), ("2026-09-24 18:30:00", date(2026, 9, 24)),
                      (date(2026, 9, 24), date(2026, 9, 24)),
                      (datetime(2026, 9, 24, 7, 0, tzinfo=timezone.utc), date(2026, 9, 24))]:
        add(f"to_date parses {raw!r}", m.to_date(raw) == want, m.to_date(raw))
    for raw in ("", "   ", None, "not a date", "n/a", "2026-13-45"):
        add(f"to_date refuses {raw!r}", m.to_date(raw) is None, m.to_date(raw))
    add("to_date reads epoch seconds", m.to_date(1790000000) is not None, m.to_date(1790000000))

    for raw, want in [("1,234", 1234), ("1 234", 1234), ("12.0", 12), ("", 0), (None, 0), ("abc", 0),
                      (-5, 0), ("3.7", 4), (True, 0)]:
        add(f"to_count({raw!r}) == {want}", m.to_count(raw) == want, m.to_count(raw))
    for raw, want in [("1,234", 1234), ("1,234,567", 1234567), ("1.234.567", 1234567), ("1,5", 2)]:
        add(f"thousands vs decimal separator: {raw!r} -> {want}", m.to_count(raw) == want, m.to_count(raw))
    for raw, want in [("12.34", 12_340_000), ("$1,234.56", 1_234_560_000), ("1234,56", 1_234_560_000),
                      ("1.234,56", 1_234_560_000), ("", 0), (None, 0), ("-2.5", -2_500_000)]:
        add(f"to_micros({raw!r}) == {want}", m.to_micros(raw) == want, m.to_micros(raw))

    add("slug normalises a display name", m.slug("Summer Sale 2026!") == "summer_sale_2026", m.slug("Summer Sale 2026!"))
    add("slug of an empty value is None", m.slug("  ") is None, m.slug("  "))
    add("clean_text drops export null words", m.clean_text("N/A") is None and m.clean_text("null") is None)
    add("clean_text collapses whitespace and NULs", m.clean_text("a\x00 b\n c") == "a b c", m.clean_text("a\x00 b\n c"))
    add("clean_text truncates to the column width",
        len(m.clean_text("x" * 500)) == m.MAX_TEXT, len(m.clean_text("x" * 500)))


# =========================================================================================== identity / privacy
def _check_identity(add) -> None:
    from erp.marketing import model as m

    t, v = m.hash_identity("email", "  Person@Example.COM ")
    add("an e-mail identity is hashed, not stored", t == "email_sha256" and len(v) == 64 and "@" not in v, (t, v[:8]))
    add("the e-mail hash is case- and space-insensitive",
        m.hash_identity("email", "person@example.com") == (t, v))
    t2, v2 = m.hash_identity("phone", "+84 (90) 123-4567")
    add("a phone identity is normalised then hashed", t2 == "phone_sha256" and len(v2) == 64, (t2, v2[:8]))
    add("the same phone written differently gives the same hash",
        m.hash_identity("phone", "+84901234567") == (t2, v2))
    add("a click id is kept as itself (not personal)",
        m.hash_identity("click_id", "abc123") == ("click_id", "abc123"))
    add("an unknown identity type is labelled, not thrown away",
        m.hash_identity("whatever", "x9") == ("external_user_id", "x9"))
    add("an empty identity is None", m.hash_identity("email", "  ") is None)
    samples = {"email": "a@b.co", "phone": "+84901234567", "click_id": "ck1", "session_id": "s1", "nonsense": "x"}
    add("every identity type used is one the schema allows",
        all(m.hash_identity(k, v)[0] in m.IDENTITY_TYPES for k, v in samples.items()),
        {k: m.hash_identity(k, v) for k, v in samples.items()})
    add("a phone with no digits at all is not an identity", m.hash_identity("phone", "n.a.") is None)

    i = _one({"date": "2026-09-24", "channel": "facebook", "clicks": "2", "email": "Person@Example.com"})
    add("an interaction never carries the raw e-mail",
        i.identity_type == "email_sha256" and "@" not in (i.identity_value or "") and "person" not in str(i.attrs).lower(),
        (i.identity_type, i.attrs))


# =========================================================================================== validation (L-044)
def _check_validation(add) -> None:
    from erp.marketing.model import InvalidRecord, build_interaction

    ok = build_interaction({"source_key": "flat_file", "event_date": "2026-09-24", "channel_key": "Facebook",
                            "event_type": "click", "clicks": 1})
    add("a minimal valid record is accepted", ok.event_date == date(2026, 9, 24) and ok.channel_key == "facebook", ok)
    add("a missing channel becomes 'unknown', not a rejection",
        build_interaction({"source_key": "s", "event_date": "2026-09-24", "event_type": "click"}).channel_key == "unknown")
    add("an unknown event word becomes 'other'",
        build_interaction({"source_key": "s", "event_date": "2026-09-24", "event_type": "wiggle", "clicks": 1}).event_type == "other")

    for name, record, expect in [
        ("no source_key", {"event_date": "2026-09-24", "clicks": 1}, "source_key"),
        ("no usable date", {"source_key": "s", "event_date": "yesterday", "clicks": 1}, "date"),
        ("a date outside 2000-2100", {"source_key": "s", "event_date": "1970-01-05", "clicks": 1}, "2000-2100"),
        ("no event type and no measure", {"source_key": "s", "event_date": "2026-09-24"}, "nothing to record"),
        ("not a record at all", ["not", "a", "dict"], "not a record"),
    ]:
        try:
            build_interaction(record)
            add(f"rejects: {name}", False, "it was accepted")
        except InvalidRecord as exc:
            add(f"rejects: {name}", expect in str(exc), f"reason was {exc!r}")

    i = build_interaction({"source_key": "s", "event_date": "2026-09-24", "event_at": "2026-09-01T10:00:00Z",
                           "event_type": "click", "clicks": 1})
    add("an event_at that disagrees with the day is dropped, not trusted", i.event_at is None, i.event_at)

    # A corrupted numeric cell (a real-world CSV/Excel export glitch) must be rejected here, at the
    # row-level Python validation stage, rather than silently reaching the database as a value that
    # overflows BIGINT and takes its whole chunk down with it (the confirmed gap from review).
    from erp.marketing.model import MAX_COUNT, MAX_MONEY_MICROS
    huge = "999999999999999999999999999999"
    add("to_count has no bound of its own (the record-level check below is what protects the batch)",
        m_to_count(huge) > MAX_COUNT, m_to_count(huge))
    for field in ("impressions", "clicks", "reactions", "sessions", "conversions"):
        try:
            build_interaction({"source_key": "s", "event_date": "2026-09-24", "event_type": "click", field: huge})
            add(f"rejects a corrupted {field} cell instead of overflowing BIGINT at the database", False, "it was accepted")
        except InvalidRecord as exc:
            add(f"rejects a corrupted {field} cell instead of overflowing BIGINT at the database",
                "beyond any real value" in str(exc), str(exc))
    for field in ("spend", "revenue"):
        try:
            build_interaction({"source_key": "s", "event_date": "2026-09-24", "event_type": "click", field: huge})
            add(f"rejects a corrupted {field} cell instead of overflowing BIGINT at the database", False, "it was accepted")
        except InvalidRecord as exc:
            add(f"rejects a corrupted {field} cell instead of overflowing BIGINT at the database",
                "beyond any real value" in str(exc), str(exc))
    add("a value right at the bound is still accepted (the bound rejects corruption, not real scale)",
        build_interaction({"source_key": "s", "event_date": "2026-09-24", "event_type": "click",
                           "clicks": MAX_COUNT}).clicks == MAX_COUNT)
    add("a large negative revenue (a big refund) is judged by magnitude, not sign",
        _rejects_money(str(-(MAX_MONEY_MICROS // 1_000_000 + 1))))


def _rejects_money(spend_value) -> bool:
    from erp.marketing.model import InvalidRecord, build_interaction
    try:
        build_interaction({"source_key": "s", "event_date": "2026-09-24", "event_type": "click", "spend": spend_value})
        return False
    except InvalidRecord:
        return True


def m_to_count(value):
    from erp.marketing.model import to_count
    return to_count(value)


def _check_batch_survives(add) -> None:
    """A malformed row costs that row, never the batch (L-044) - proven on the pipeline's own validate stage."""
    from erp.marketing.pipeline import BatchResult, Pipeline

    class Exploding:
        source_key = "test"

        def map_record(self, raw):
            if raw.get("boom"):
                raise ValueError("this connector is broken for this record")
            if raw.get("skip"):
                return None
            return {"source_key": "test", "event_date": raw.get("d"), "channel_key": "c",
                    "event_type": "click", "clicks": 1}

    chunk = [{"d": "2026-09-24"}, {"boom": 1}, {"skip": 1}, {"d": "rubbish"}, {"d": "2026-09-25"}]
    result = BatchResult(batch_id="b", source="test", schema="public")
    items = Pipeline(schema="public")._validate(Exploding(), chunk, 100, result)
    add("a good record survives a connector crash on its neighbour", len(items) == 2, len(items))
    add("every bad record is counted", result.rows_invalid == 3, result.rows_invalid)
    add("the reasons are kept for the report", len(result.invalid_samples) == 3, result.invalid_samples)
    add("the reasons carry the record's position in the batch",
        all(s.startswith("record #10") for s in result.invalid_samples), result.invalid_samples)
    add("a connector crash names the exception, not a traceback",
        "ValueError" in result.invalid_samples[0] and "\n" not in result.invalid_samples[0], result.invalid_samples[0])


def _check_sql_failure_isolation(add) -> None:
    """A row that PASSES Python validation but fails at the database (an unexpected type coercion, or -
    before the model.py bound existed - a numeric overflow) must still cost only that row, never the
    rest of its chunk. This is the SQL-level twin of `_check_batch_survives`: that one proves isolation
    at the earlier `_validate()` stage; this one proves it at `_load_chunk_isolating`, the stage the
    confirmed review finding was actually about (pipeline.py's old chunk-wide try/except discarded up
    to MARKETING_BATCH_SIZE good rows for one bad one). Pure Python: `_load_chunk` is monkeypatched so
    no database is involved, only the bisection/accounting logic in pipeline.py itself."""
    import contextlib
    from erp.marketing.pipeline import LOG_INVALID_SAMPLES, BatchResult, Pipeline, _is_connection_error

    class FakeConn:
        def begin(self):
            return contextlib.nullcontext()

    pipeline = Pipeline.__new__(Pipeline)  # skip __init__: this test needs no engine/schema

    poison = {13}

    def fake_load_chunk(conn, batch_id, seq, connector, chunk, result):
        result.rows_read += len(chunk)
        if any(v in poison for v in chunk):
            raise ValueError("simulated SQL failure (e.g. 'integer out of range')")
        result.rows_loaded += len(chunk)

    pipeline._load_chunk = fake_load_chunk
    chunk = list(range(20))                    # one poisoned value among nineteen good ones
    result = BatchResult(batch_id="b", source="test", schema="public")
    pipeline._load_chunk_isolating(FakeConn(), "b", 0, None, chunk, result)

    add("every good row in the chunk still loads despite one poisoned neighbour",
        result.rows_loaded == 19, result.rows_loaded)
    add("exactly the poisoned row is counted invalid, not the whole chunk",
        result.rows_invalid == 1, result.rows_invalid)
    add("the run is marked partial (some loaded, one isolated) so the ingest journal shows it happened",
        result.status == "partial", result.status)
    add("the isolated row is counted against the failure budget", result.rows_isolated == 1, result.rows_isolated)
    add("the failure reason is recorded for the journal",
        bool(result.error) and "simulated SQL failure" in result.error, result.error)

    # A poisoned row at the very START of the chunk (worst case for the systematic-failure streak: no
    # sibling has succeeded yet when its leaf fails) must still isolate exactly as before.
    poison.clear()
    poison.add(0)
    result1b = BatchResult(batch_id="b", source="test", schema="public")
    pipeline._load_chunk_isolating(FakeConn(), "b", 0, None, list(range(20)), result1b)
    add("regression: a poisoned first row isolates alone and the other 19 load",
        result1b.rows_invalid == 1 and result1b.rows_loaded == 19, (result1b.rows_invalid, result1b.rows_loaded))

    # A SYSTEMATIC failure (every row fails: missing privilege / partition / schema) must ABORT after a
    # bounded number of attempts - asserting the count, not just that it terminates - and not flood the log.
    import logging
    import psycopg2
    from erp.marketing.pipeline import _RunAborted

    attempts = []

    def fake_load_chunk_always_fails(conn, batch_id, seq, connector, chunk, result):
        attempts.append(len(chunk))
        result.rows_read += len(chunk)
        raise psycopg2.errors.InsufficientPrivilege("permission denied for table marketing_landing")

    class Capture(logging.Handler):
        def __init__(self):
            super().__init__()
            self.records = []

        def emit(self, record):
            self.records.append(record)

    pipeline._load_chunk = fake_load_chunk_always_fails
    pipeline.max_isolated = 50
    result2 = BatchResult(batch_id="b", source="test", schema="public")
    cap = Capture()
    plog = logging.getLogger("erp.marketing.pipeline")
    plog.addHandler(cap)
    old_level = plog.level
    plog.setLevel(logging.DEBUG)
    aborted = None
    try:
        pipeline._load_chunk_isolating(FakeConn(), "b", 0, None, list(range(5000)), result2)
    except _RunAborted as stop:
        aborted = str(stop)
    finally:
        plog.removeHandler(cap)
        plog.setLevel(old_level)
    warnings = [r for r in cap.records if r.levelno >= logging.WARNING]
    add("a systematic failure over a 5000-row chunk aborts the run", aborted is not None, aborted)
    add("... and the reason names the cause", bool(aborted) and "systematic" in aborted and "permission denied" in aborted, aborted)
    add("... after a BOUNDED number of attempts (not ~2N = 10,000)", len(attempts) <= 40, len(attempts))
    add("... having rejected far fewer rows than the chunk holds", result2.rows_invalid <= 10, result2.rows_invalid)
    add("... without one warning line per row", len(warnings) <= LOG_INVALID_SAMPLES + 2, len(warnings))

    # The per-run budget catches a systematic failure whose messages differ row by row.
    counter = {"n": 0}

    def fake_load_chunk_varying(conn, batch_id, seq, connector, chunk, result):
        counter["n"] += 1
        raise ValueError(f"failure #{counter['n']}")

    pipeline._load_chunk = fake_load_chunk_varying
    pipeline.max_isolated = 5
    result2b = BatchResult(batch_id="b", source="test", schema="public")
    budget_msg = None
    try:
        pipeline._load_chunk_isolating(FakeConn(), "b", 0, None, list(range(200)), result2b)
    except _RunAborted as stop:
        budget_msg = str(stop)
    add("exceeding the isolated-row budget aborts the run", budget_msg is not None and "MARKETING_MAX_ISOLATED_ROWS" in budget_msg, budget_msg)
    add("... with the budget respected to within one row", result2b.rows_isolated == 6, result2b.rows_isolated)
    pipeline.max_isolated = 50

    # A broken CONNECTION (not a bad value) must propagate instead of being bisected down to
    # single-row chunks - isolating rows cannot fix a dead database, and trying anyway would turn one
    # outage into thousands of slow, identical failures.
    def fake_load_chunk_conn_dead(conn, batch_id, seq, connector, chunk, result):
        raise psycopg2.OperationalError("server closed the connection unexpectedly")

    pipeline._load_chunk = fake_load_chunk_conn_dead
    result3 = BatchResult(batch_id="b", source="test", schema="public")
    raised = False
    try:
        pipeline._load_chunk_isolating(FakeConn(), "b", 0, None, list(range(5)), result3)
    except psycopg2.OperationalError:
        raised = True
    add("a broken database connection propagates instead of being treated as bad rows",
        raised, raised)
    # The same outage seen through conn.execute(text(...)) arrives wrapped by SQLAlchemy.
    import sqlalchemy.exc as sa_exc

    def fake_load_chunk_wrapped_dead(conn, batch_id, seq, connector, chunk, result):
        result.rows_read += len(chunk)
        raise sa_exc.OperationalError("INSERT ...", {}, psycopg2.OperationalError("server closed the connection"))

    pipeline._load_chunk = fake_load_chunk_wrapped_dead
    result4 = BatchResult(batch_id="b", source="test", schema="public")
    raised4 = False
    try:
        pipeline._load_chunk_isolating(FakeConn(), "b", 0, None, list(range(5)), result4)
    except sa_exc.OperationalError:
        raised4 = True
    add("a SQLAlchemy-wrapped OperationalError propagates instead of being bisected", raised4, raised4)
    add("... and no row was counted invalid", result4.rows_invalid == 0, result4.rows_invalid)
    invalidated = sa_exc.DBAPIError("SELECT 1", {}, RuntimeError("gone"), connection_invalidated=True)
    add("a wrapped error flagged connection_invalidated is a connection error", _is_connection_error(invalidated))
    add("a wrapped DataError (a bad value) is NOT a connection error",
        not _is_connection_error(sa_exc.DataError("INSERT", {}, psycopg2.DataError("integer out of range"))))
    add("a wrapped QueryCanceled/Deadlock stays conservative (aborts, no data loss)",
        _is_connection_error(sa_exc.OperationalError("x", {}, psycopg2.errors.QueryCanceled("cancel")))
        and _is_connection_error(sa_exc.OperationalError("x", {}, psycopg2.errors.DeadlockDetected("dl"))))
    add("_is_connection_error tells a dropped connection from a bad value",
        _is_connection_error(psycopg2.OperationalError("x")) and not _is_connection_error(ValueError("x")))


def _check_chunks(add) -> None:
    from erp.marketing.pipeline import _chunks

    add("chunks are capped at the chunk size",
        [len(c) for c in _chunks(range(10), 4, None)] == [4, 4, 2], [len(c) for c in _chunks(range(10), 4, None)])
    add("a limit stops the run early",
        sum(len(c) for c in _chunks(range(100), 7, 10)) == 10)
    add("an empty source yields no chunk", list(_chunks([], 5, None)) == [])


# =========================================================================================== dedupe
def _check_dedupe(add) -> None:
    from erp.marketing.model import dedupe_key

    grain = ("2026-09-24", "facebook", "camp", "creative", "click", None)
    a = dedupe_key("flat_file", None, grain)
    add("the grain key is a UUID", isinstance(a, uuid.UUID), a)
    add("the same interaction gives the same key", dedupe_key("flat_file", None, grain) == a)
    add("a different day gives a different key",
        dedupe_key("flat_file", None, ("2026-09-25", *grain[1:])) != a)
    add("a different source gives a different key", dedupe_key("other", None, grain) != a)
    add("an external id wins over the grain",
        dedupe_key("flat_file", "abc", grain) == dedupe_key("flat_file", "abc", ("anything", "else", None, None, "x", None)))
    add("two different external ids differ", dedupe_key("s", "a", grain) != dedupe_key("s", "b", grain))

    row = {"date": "2026-09-24", "channel": "facebook", "campaign": "Summer", "clicks": "5", "impressions": "100"}
    restated = dict(row, clicks="9", impressions="140")
    add("a restated row keeps the same key, so it updates instead of duplicating",
        _one(row).dedupe_key == _one(restated).dedupe_key)
    add("a different campaign is a different row",
        _one(row).dedupe_key != _one(dict(row, campaign="Winter")).dedupe_key)


# =========================================================================================== the scoped GIN allowlist
def _check_attrs(add) -> None:
    from erp.marketing.model import INDEXED_ATTR_KEYS, split_attrs

    full, idx = split_attrs({"UTM Source": "Facebook", "device": "Mobile", "raw_note": "keep me",
                             "ad_position": 3, "empty": None})
    add("attrs keeps every extra the source sent",
        set(full) == {"utm_source", "device", "raw_note", "ad_position"}, full)
    add("attrs drops a value the source left empty", "empty" not in full, full)
    add("attrs_idx holds only the allowlisted keys", set(idx) == {"utm_source", "device"}, idx)
    add("attrs_idx values are lower-cased so a containment query is predictable",
        idx == {"utm_source": "facebook", "device": "mobile"}, idx)
    add("a key is copied, not moved (attrs stays complete)", full["device"] == "Mobile", full["device"])
    add("the allowlist is small and documented", 1 <= len(INDEXED_ATTR_KEYS) <= 10, INDEXED_ATTR_KEYS)
    add("every allowlisted key is a plain slug",
        all(k == k.lower() and k.replace("_", "").isalnum() for k in INDEXED_ATTR_KEYS), INDEXED_ATTR_KEYS)
    add("a non-dict attrs is handled", split_attrs("nonsense") == ({}, {}))
    add("a huge record is capped", len(split_attrs({f"k{i}": i for i in range(1000)})[0]) <= 300)


# =========================================================================================== the flat-file connector
def _check_connector(add) -> None:
    from erp.marketing.connectors.flat_file import FlatFileConnector, normalise_row

    add("headers are matched on their slug",
        normalise_row({"Amount spent (USD)": "1"}) == {"amount_spent_usd": "1"}, normalise_row({"Amount spent (USD)": "1"}))

    row = {"Date": "2026-09-24", "Channel": "Facebook", "Campaign name": "Summer Sale", "Ad name": "Video A",
           "Impressions": "1,000", "Clicks": "42", "Conversions": "3", "Amount spent (USD)": "12.50",
           "utm_source": "fb", "device": "Mobile", "Some vendor column": "keep me"}
    i = _one(row)
    add("a multi-metric export line is a rollup, not one interaction", i.event_type == "rollup", i.event_type)
    add("metrics are typed", (i.impressions, i.clicks, i.conversions) == (1000, 42, 3), (i.impressions, i.clicks, i.conversions))
    add("money becomes micros", i.spend_micros == 12_500_000, i.spend_micros)
    add("the channel becomes a slug key", i.channel_key == "facebook", i.channel_key)
    add("the campaign gets a key from its name", i.campaign_key == "summer_sale" and i.campaign_name == "Summer Sale")
    add("the creative gets a key from its name", i.creative_key == "video_a", i.creative_key)
    add("an unrecognised column is kept in attrs", i.attrs.get("some_vendor_column") == "keep me", i.attrs)
    add("a recognised column is not repeated in attrs",
        not {"date", "channel", "clicks", "impressions"} & set(i.attrs), i.attrs)
    add("an indexed extra reaches attrs_idx", i.attrs_idx.get("utm_source") == "fb", i.attrs_idx)

    single = _one({"day": "2026-09-24", "platform": "google", "clicks": "7"})
    add("a single-metric line is that metric's own event", single.event_type == "click", single.event_type)
    named = _one({"day": "2026-09-24", "action": "Purchase", "conversions": "1", "revenue": "9.99"})
    add("the source's own word for the event wins", named.event_type == "conversion", named.event_type)
    add("revenue is read as money", named.revenue_micros == 9_990_000, named.revenue_micros)

    c = _flat([{"Date": "2026-09-24", "clicks": "1"}])
    add("a row with no date is not an interaction", c.map_record({"total": "9999"}) is None)
    add("an empty row is not an interaction", c.map_record({}) is None)
    add("a non-dict is not an interaction", c.map_record("nonsense") is None)
    add("records() ignores anything that is not a record",
        len(list(_flat([{"a": 1}, "junk", None, {"b": 2}]).records())) == 2)

    default = FlatFileConnector([], source_key="my_export", channel_default="facebook")
    m = default.map_record({"date": "2026-09-24", "clicks": "1"})
    add("a file with no channel column can be given one", m["channel_key"] == "facebook", m["channel_key"])
    add("the connector names its own source", default.source_key == "my_export", default.source_key)
    add("a source key is slugged like everything else",
        FlatFileConnector([], source_key="Facebook Ads!").source_key == "facebook_ads")


# =========================================================================================== schema helpers
def _check_schema(add) -> None:
    from erp.marketing import schema as s

    add("a partition is named after its month",
        s.partition_name(date(2026, 9, 14)) == "interaction_fact_2026_09", s.partition_name(date(2026, 9, 14)))
    add("month_start is the first of the month", s.month_start(date(2026, 9, 30)) == date(2026, 9, 1))
    add("next_month rolls over December", s.next_month(date(2026, 12, 3)) == date(2027, 1, 1))
    add("next_month is otherwise the next first", s.next_month(date(2026, 1, 31)) == date(2026, 2, 1))

    for bad in ("public; DROP TABLE leads", "pg_catalog.x", "a b", "", "1abc", "x" * 80, "Public'"):
        try:
            s.check_identifier(bad)
            add(f"refuses the schema name {bad[:24]!r}", False, "it was accepted")
        except s.SchemaError:
            add(f"refuses the schema name {bad[:24]!r}", True)
    add("accepts a plain identifier", s.check_identifier("Perf_Test") == "perf_test")
    add("qualified() builds schema.table", s.qualified("public", "interaction_fact") == "public.interaction_fact")
    add("every table the DDL creates is listed",
        set(s.MARKETING_TABLES) >= {"interaction_fact", "marketing_landing", "marketing_ingest_run",
                                    "marketing_source", "marketing_channel", "marketing_campaign",
                                    "marketing_creative", "marketing_identity"}, s.MARKETING_TABLES)


def _check_ddl_matches_code(add) -> None:
    """The DDL file and the Python that writes into it must not drift apart."""
    sql = (ROOT / "db" / "sql" / "07_marketing_schema.sql").read_text(encoding="utf-8")
    from erp.marketing import schema as s
    from erp.marketing.model import INDEXED_ATTR_KEYS

    for table in s.MARKETING_TABLES:
        add(f"the DDL creates {table}", f"CREATE TABLE IF NOT EXISTS {table}" in sql)
    add("the fact is partitioned by month on event_date", "PARTITION BY RANGE (event_date)" in sql)
    add("the fact's key is (event_date, dedupe_key)", "PRIMARY KEY (event_date, dedupe_key)" in sql)
    add("there is a btree on (channel_id, event_date)", "(channel_id, event_date)" in sql)
    add("there is a BRIN on the append-only received_at", "brin (received_at)" in sql)
    add("the GIN index is on attrs_idx, never on the whole attrs blob",
        "gin (attrs_idx jsonb_path_ops)" in sql and "gin (attrs " not in sql)
    add("the DDL names the same GIN allowlist as the code",
        all(k in sql for k in INDEXED_ATTR_KEYS), INDEXED_ATTR_KEYS)
    add("the DDL only ever creates (it alters and drops nothing that exists)",
        " DROP " not in sql.upper() and "ALTER TABLE" not in sql.upper())
    add("the identity dimension references leads instead of copying it",
        "REFERENCES leads(id)" in sql and "email" not in sql.split("marketing_identity")[1].split("CREATE TABLE")[0].replace("email_sha256", ""))

    idx = (ROOT / "db" / "sql" / "08_leads_indexes.sql").read_text(encoding="utf-8")
    add("the leads fix adds an index on created_at", "idx_leads_created_at" in idx and "(created_at" in idx)
    add("the leads fix adds an index on source", "idx_leads_source_created_at" in idx and "(source," in idx)
    add("the leads fix alters and drops nothing",
        " DROP " not in idx.upper() and "ALTER TABLE" not in idx.upper())


# =========================================================================================== rollup + partitions (Phase 4)
def _raises(fn, exc) -> bool:
    try:
        fn()
    except exc:
        return True
    return False


def _check_rollup_logic(add) -> None:
    """Day-window arithmetic, range planning and the rollup DDL's shape: all pure."""
    from erp.marketing import rollup as r

    d = date
    today = d(2026, 9, 25)
    add("a 3-day window includes today", r.window_start(today, 3) == d(2026, 9, 23))
    add("a 1-day window is just today", r.window_start(today, 1) == today)
    add("the window crosses a month boundary", r.window_start(d(2026, 3, 2), 5) == d(2026, 2, 26))
    add("the window crosses a year boundary", r.window_start(d(2027, 1, 1), 3) == d(2026, 12, 30))
    add("a 0-day window is refused", _raises(lambda: r.window_start(today, 0), ValueError))

    add("consecutive days merge into one closed-open range",
        r.contiguous_ranges([d(2026, 9, 3), d(2026, 9, 4), d(2026, 9, 5)]) == [(d(2026, 9, 3), d(2026, 9, 6))])
    add("a gap makes two ranges, duplicates and order do not matter",
        r.contiguous_ranges([d(2026, 9, 9), d(2026, 9, 3), d(2026, 9, 4), d(2026, 9, 4)])
        == [(d(2026, 9, 3), d(2026, 9, 5)), (d(2026, 9, 9), d(2026, 9, 10))])
    add("no days, no ranges", r.contiguous_ranges([]) == [])
    add("a December range ends on the 1st of January",
        r.contiguous_ranges([d(2026, 12, 31)]) == [(d(2026, 12, 31), d(2027, 1, 1))])
    add("a range is split at month boundaries",
        r.split_at_months(d(2026, 1, 20), d(2026, 3, 5))
        == [(d(2026, 1, 20), d(2026, 2, 1)), (d(2026, 2, 1), d(2026, 3, 1)), (d(2026, 3, 1), d(2026, 3, 5))])

    add("nothing changed: the plan is just the open-ended window",
        r.plan_ranges(today, 3, []) == [(d(2026, 9, 23), None)])
    add("a changed older day is added before the window",
        r.plan_ranges(today, 3, [d(2026, 6, 17)]) == [(d(2026, 6, 17), d(2026, 6, 18)), (d(2026, 9, 23), None)])
    add("a changed day inside the window is not planned twice",
        r.plan_ranges(today, 3, [d(2026, 9, 24), d(2026, 9, 23)]) == [(d(2026, 9, 23), None)])
    add("changed days that end right at the window merge into it",
        r.plan_ranges(today, 3, [d(2026, 9, 22), d(2026, 9, 21)]) == [(d(2026, 9, 21), None)])
    add("a changed run spanning two months is split at the boundary",
        r.plan_ranges(today, 3, [d(2026, 7, 30), d(2026, 7, 31), d(2026, 8, 1)])
        == [(d(2026, 7, 30), d(2026, 8, 1)), (d(2026, 8, 1), d(2026, 8, 2)), (d(2026, 9, 23), None)])

    add("a full rebuild is one range per month, the last one open ended",
        r.full_ranges(d(2026, 7, 14), d(2026, 9, 3))
        == [(d(2026, 7, 14), d(2026, 8, 1)), (d(2026, 8, 1), d(2026, 9, 1)), (d(2026, 9, 1), None)])
    add("a full rebuild with facts in one month is one open range",
        r.full_ranges(d(2026, 9, 3), d(2026, 9, 20)) == [(d(2026, 9, 3), None)])
    add("a full rebuild of an empty fact table plans nothing (history is never wiped)", r.full_ranges(None, None) == [])
    add("a full rebuild starts at the first fact day, leaving older rollup history alone",
        r.full_ranges(d(2025, 1, 10), d(2025, 1, 12))[0][0] == d(2025, 1, 10))
    add("days counted for a closed range and an open one",
        r.count_days([(d(2026, 9, 1), d(2026, 9, 4)), (d(2026, 9, 23), None)], today) == 3 + 3)
    add("the watermark is the last good start minus the overlap, None when it never ran",
        r.watermark(None) is None and r.watermark(datetime(2026, 9, 25, 10, 0)) == datetime(2026, 9, 25, 9, 0))

    import inspect
    add("a --window-only run must not move the changed-days watermark (it never looked for changes)",
        "mode <> 'window_only'" in inspect.getsource(r.last_ok_start))

    sql = (ROOT / "db" / "sql" / "09_marketing_rollup.sql").read_text(encoding="utf-8")
    add("the rollup DDL names every measure the job writes",
        all(c in sql for c in r.MEASURE_COLUMNS), [c for c in r.MEASURE_COLUMNS if c not in sql])
    add("the campaign rollup's unique key is the day/channel/campaign grain (plus currency and grain)",
        "PRIMARY KEY (event_date, channel_id, campaign_id, currency, grain)" in sql)
    add("the channel rollup's unique key is the day/channel grain (plus currency and grain)",
        "pk_interaction_daily_channel_rollup PRIMARY KEY (event_date, channel_id, currency, grain)" in sql)
    add("both rollups are indexed for the channel + date-range filter",
        sql.count("(channel_id, event_date)") >= 2)
    tables_sql = sql.lower().split("create table")[1:]
    add("no ratio is stored in the rollup tables (they are computed at read time)",
        not any(w in t for t in tables_sql for w in ("ctr ", "cpc ", "cvr ", "roas ", "_rate")))
    add("the rollup DDL only creates (it alters and drops nothing that exists)",
        " DROP " not in sql.upper() and "ALTER TABLE" not in sql.upper())
    add("every table the job needs is listed", set(r.ROLLUP_TABLES) >= {
        "interaction_daily_rollup", "interaction_daily_channel_rollup", "marketing_rollup_run"}, r.ROLLUP_TABLES)
    add("the DDL and the journal insert agree on the journal's columns",
        all(c in sql for c in ("mode", "days_requested", "ranges", "days_recomputed", "rows_deleted", "rows_written",
                               "status", "error_message")))


def _check_partition_maintenance(add) -> None:
    from erp.marketing import partitions as p
    from erp.marketing import schema as mschema

    d = date
    add("add_months rolls over a year end", p.add_months(d(2026, 11, 15), 3) == d(2027, 2, 1))
    add("add_months goes back over a year start", p.add_months(d(2026, 2, 10), -3) == d(2025, 11, 1))
    add("add_months(0) is the month start", p.add_months(d(2026, 9, 25), 0) == d(2026, 9, 1))
    add("the look-ahead is this month plus N",
        p.lookahead_months(d(2026, 11, 25), 3) == [d(2026, 11, 1), d(2026, 12, 1), d(2027, 1, 1), d(2027, 2, 1)])
    add("a look-ahead of 0 is just this month", p.lookahead_months(d(2026, 9, 25), 0) == [d(2026, 9, 1)])
    add("a negative look-ahead is refused", _raises(lambda: p.lookahead_months(d(2026, 9, 25), -1), ValueError))
    add("a silly look-ahead (typo) is refused before any table is created",
        _raises(lambda: p.ensure_lookahead(None, "public", d(2026, 9, 25), p.MAX_AHEAD_MONTHS + 1), ValueError))
    add("partition names parse back to their month", p.parse_partition("interaction_fact_2026_09") == d(2026, 9, 1))
    add("a name that is not a monthly partition parses to None",
        all(p.parse_partition(x) is None for x in
            ("interaction_fact", "interaction_fact_2026_13", "leads_2026_09", "interaction_fact_2026_9", "", None)))
    add("partition_name and parse_partition are inverses",
        all(p.parse_partition(mschema.partition_name(m)) == m for m in (d(2026, 1, 1), d(2027, 12, 1))))

    today = d(2026, 9, 25)
    names = [f"interaction_fact_{y}_{m:02d}" for y, m in
             [(2024, 7), (2024, 8), (2024, 9), (2024, 10), (2026, 9), (2026, 12)]] + ["interaction_fact", "noise"]
    add("the retention cutoff keeps the current month and N full months before it",
        p.retention_cutoff(today, 24) == d(2024, 9, 1))
    cand = p.retention_candidates(names, today, 24)
    add("a partition entirely before the cutoff is a candidate, oldest first",
        [c[0] for c in cand] == ["interaction_fact_2024_07", "interaction_fact_2024_08"], cand)
    add("the partition ending exactly at the cutoff is a candidate, the one starting at it is not",
        "interaction_fact_2024_08" in [c[0] for c in cand] and "interaction_fact_2024_09" not in [c[0] for c in cand])
    add("recent, future and non-partition names are never candidates",
        not any(n in [c[0] for c in cand] for n in ("interaction_fact_2026_09", "interaction_fact_2026_12",
                                                    "interaction_fact", "noise")))
    add("a longer retention selects fewer partitions", p.retention_candidates(names, today, 36) == [])
    add("retention below 1 month is refused", _raises(lambda: p.retention_cutoff(today, 0), ValueError))

    cmds = p.detach_commands("public", "interaction_fact_2024_07")
    add("the detach command is text naming the exact partition, CONCURRENTLY",
        cmds[0] == "ALTER TABLE public.interaction_fact DETACH PARTITION public.interaction_fact_2024_07 CONCURRENTLY;", cmds)
    add("the drop command is only text, and comes second", cmds[1].startswith("DROP TABLE public.interaction_fact_2024_07;"))
    add("a bad identifier cannot reach a command", _raises(lambda: p.detach_commands("public", "x; DROP TABLE leads"), Exception))

    rep = p.PartitionReport(schema="public", today=today, ahead=3, retention_months=24,
                            created=["interaction_fact_2026_10"], existing=["interaction_fact_2024_07"],
                            candidates=[{"name": "interaction_fact_2024_07", "month": d(2024, 7, 1), "rows_estimate": 5,
                                         "bytes": 2_000_000, "commands": cmds}])
    out = "\n".join(rep.lines())
    add("the report says nothing was detached or dropped and shows the exact commands",
        "NOTHING WAS DETACHED OR DROPPED" in out and "DETACH PARTITION" in out and "DROP TABLE" in out)
    empty = p.PartitionReport(schema="public", today=today, ahead=3, retention_months=24)
    add("no candidates: the report says there is nothing to do", "nothing to do" in "\n".join(empty.lines()))

    # structural guard: the module must have no way to run a destructive statement (they only exist as text)
    import ast
    tree = ast.parse((ROOT / "erp" / "marketing" / "partitions.py").read_text(encoding="utf-8"))
    executed = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") in ("execute", "exec_driver_sql"):
            executed.append(ast.unparse(node).upper())
    add("no statement partitions.py executes is a DROP or a DETACH",
        executed and not any("DROP" in e or "DETACH" in e for e in executed), executed)


def _check_rollup_cli(add) -> None:
    """Argument handling, exit codes and failure behaviour of `python -m erp.marketing.rollup` - no database."""
    import logging
    from erp.marketing import rollup as r
    from erp.marketing import schema as mschema

    def parse(*argv):
        return r.build_parser().parse_args(list(argv))

    a = parse()
    add("the defaults: 3 days, no full rebuild", a.days == 3 and not a.full and not a.yes and not a.partitions_only)
    add("--days is read", parse("--days", "7").days == 7)
    add("valid arguments give no complaint", r.check_args(parse("--days", "30")) is None)
    add("--days 0 is a usage error", r.check_args(parse("--days", "0")) is not None)
    add("--days beyond the sanity limit is a usage error", r.check_args(parse("--days", str(r.MAX_DAYS + 1))) is not None)
    add("--full without --yes is refused (the job never prompts)", "--yes" in (r.check_args(parse("--full")) or ""))
    add("--full --yes is accepted", r.check_args(parse("--full", "--yes")) is None)
    add("--full and --window-only contradict", r.check_args(parse("--full", "--yes", "--window-only")) is not None)
    add("--partitions-only cannot be combined with --full", r.check_args(parse("--partitions-only", "--full", "--yes")) is not None)
    add("a negative --partitions-ahead is a usage error", r.check_args(parse("--partitions-ahead", "-1")) is not None)
    add("--retention-months 0 is a usage error", r.check_args(parse("--retention-months", "0")) is not None)

    logging.disable(logging.CRITICAL)
    import contextlib
    import io
    saved = (r.step_partitions, r.step_rollup, r._setup_logging, r.run_job)
    calls: list[str] = []
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            add("a non-numeric --days: argparse's usage error becomes exit code 2", r.main(["--days", "abc"]) == 2)
            add("main returns 2 for --full without --yes, before touching the database", r.main(["--full"]) == 2)
        with contextlib.redirect_stdout(io.StringIO()):
            add("--help exits cleanly (0)", r.main(["--help"]) == 0)

        args = parse("--schema", "perf_none")
        r.step_partitions = lambda *x: calls.append("partitions")
        r.step_rollup = lambda *x: calls.append("rollup")
        code = r.run_job(args, engine=object(), today=date(2026, 9, 25))
        add("both steps ok -> exit 0 and both ran, partitions first", code == 0 and calls == ["partitions", "rollup"], (code, calls))

        calls.clear()

        def boom(*x):
            calls.append("partitions")
            raise RuntimeError("boom")
        r.step_partitions = boom
        code = r.run_job(args, engine=object(), today=date(2026, 9, 25))
        add("a failing partition step -> exit 1, but the rollup step still ran", code == 1 and calls == ["partitions", "rollup"], (code, calls))

        calls.clear()
        r.step_partitions = lambda *x: calls.append("partitions")

        def not_installed(*x):
            calls.append("rollup")
            raise mschema.SchemaError("not installed")
        r.step_rollup = not_installed
        add("tables not installed -> exit 1 (logged, no traceback out of run_job)",
            r.run_job(args, engine=object(), today=date(2026, 9, 25)) == 1)

        calls.clear()
        r.step_rollup = lambda *x: calls.append("rollup")
        code = r.run_job(parse("--schema", "perf_none", "--partitions-only"), engine=object())
        add("--partitions-only runs just the partition step", code == 0 and calls == ["partitions"], calls)
        calls.clear()
        code = r.run_job(parse("--schema", "perf_none", "--skip-partitions"), engine=object())
        add("--skip-partitions runs just the rollup", code == 0 and calls == ["rollup"], calls)
        add("a hostile --schema is refused before any SQL",
            _raises(lambda: r.run_job(parse("--schema", "x; drop schema public"), engine=object()), mschema.SchemaError))

        # main() catches EVERYTHING at the top (L-013): even a non-Exception escaping the job becomes exit 1
        r._setup_logging = lambda verbose: None

        def explode(*a, **k):
            raise MemoryError("worse than an Exception")
        r.run_job = explode
        add("main() turns any error escaping the job into exit code 1", r.main(["--days", "2"]) == 1)
    finally:
        r.step_partitions, r.step_rollup, r._setup_logging, r.run_job = saved
        logging.disable(logging.NOTSET)


# =========================================================================================== the two source-specific connectors (real-file work)
DONG = "₫"          # the dong sign: the currency symbol of the owner's paid-social export
AD_HEADER = ["Start", "End", "Month", "Period", "Campaign", "Adset", "Ad name", "Device", "Impression", "Reach", "Freq", "Spent",
             "Link click", "LP view", "Eng", "Lead", "Purchase", "Revenue/pur", "Revenue"]
EMAIL_HEADER = ["send_date", "brand", "country", "campaign_id", "segment", "delivered", "unique_opens", "unique_clicks", "orders",
                "revenue_eur", "unsubscribes", "spam_complaints"]


def _ad_row(**over) -> dict:
    """One synthetic paid-social row in the owner's export shape (values invented, shape exact)."""
    row = {"Start": "6/10/2024", "End": "6/16/2024", "Month": "6", "Period": "10-Jun", "Campaign": "Cam 1", "Adset": "Group 1", "Ad name": "Dynamic_ad_02",
           "Device": "desktop", "Impression": "16,092", "Reach": "7797", "Freq": "2.06", "Spent": f"475,401 {DONG}", "Link click": "100", "LP view": "80",
           "Eng": "74", "Lead": "10", "Purchase": "5", "Revenue/pur": f"1,000,000 {DONG}", "Revenue": ""}
    row.update(over)
    return row


def _email_row(**over) -> dict:
    row = {"send_date": "2025-11-01", "brand": "GiftLoom", "country": "DE", "campaign_id": "CMP-GIF-20251101", "segment": "Engaged", "delivered": "35768",
           "unique_opens": "6472", "unique_clicks": "847", "orders": "42", "revenue_eur": "1249.13", "unsubscribes": "37", "spam_complaints": "3"}
    row.update(over)
    return row


def _ad(rows, **kw):
    from erp.marketing.connectors.ad_performance import AdPerformanceConnector
    return AdPerformanceConnector.from_rows(list(rows), **kw)


def _email(rows, **kw):
    from erp.marketing.connectors.email_campaign import EmailCampaignConnector
    return EmailCampaignConnector.from_rows(list(rows), **kw)


def _validate(connector):
    """Run a connector's records through the pipeline's validation stage (no database): (interactions, result)."""
    from erp.marketing.pipeline import BatchResult, Pipeline
    res = BatchResult(batch_id="t", source=connector.source_key, schema="perf_x")
    items = Pipeline(schema="perf_x")._validate(connector, list(connector.records()), 0, res)
    return [i for _s, i in items], res


def _refused(fn) -> str | None:
    from erp.marketing.connectors.base import ConnectorError
    try:
        fn()
    except ConnectorError as exc:
        return str(exc)
    return None


@contextlib.contextmanager
def _root_logging_kept():
    """ingest.main() calls logging.basicConfig: snapshot and restore the root logger so the gate's own log is untouched (L-010)."""
    import logging
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    try:
        yield
    finally:
        for h in list(root.handlers):
            if h not in handlers:
                root.removeHandler(h)
        for h in handlers:
            if h not in root.handlers:
                root.addHandler(h)
        root.setLevel(level)


def _check_strict_parsers(add) -> None:
    from decimal import Decimal

    from erp.marketing import parse as p

    for raw, want in [(f"475,401 {DONG}", (Decimal("475401"), "VND")), (f"1,000,000 {DONG}", (Decimal("1000000"), "VND")), (f"23,316 {DONG}", (Decimal("23316"), "VND")),
                      (f"{DONG}475,401", (Decimal("475401"), "VND")), (f"475,401 {DONG}", (Decimal("475401"), "VND")), ("VND 475,401", (Decimal("475401"), "VND")),
                      ("475,401 VND", (Decimal("475401"), "VND")), ("1,249.13 €", (Decimal("1249.13"), "EUR")), ("1249.13", (Decimal("1249.13"), None)),
                      ("0", (Decimal("0"), None)), ("1,234,567.5", (Decimal("1234567.5"), None))]:
        try:
            got = p.parse_money(raw)
        except ValueError as exc:
            got = f"ValueError: {exc}"
        add(f"parse_money({raw!r}) == {want}", got == want, got)
    for raw in ("$5", "1.234,56", "1,5", "-5", "", "abc", "12,34,56", "1,23", f"475,401 {DONG}{DONG}", "vnd 5", "5 %", "1e5", "¥100", "0x10"):
        try:
            got = p.parse_money(raw)
            add(f"parse_money refuses {raw!r} (no guessing)", False, got)
        except ValueError:
            add(f"parse_money refuses {raw!r} (no guessing)", True)
    add("parse_money says WHY it refuses '$'", "ambiguous" in _msg(p.parse_money, "$5"), _msg(p.parse_money, "$5"))
    add("to_micros_exact has no float in it (475401 -> 475,401,000,000 exactly; 1249.13 -> 1,249,130,000)", p.to_micros_exact(Decimal("475401")) == 475_401_000_000 and p.to_micros_exact(Decimal("1249.13")) == 1_249_130_000
        and p.to_micros_exact(Decimal("9999999999.99")) == 9_999_999_999_990_000)
    for raw, want in [("1,234", 1234), ("1234", 1234), ("12.0", 12), ("0", 0), ("16,092", 16092)]:
        add(f"parse_int({raw!r}) == {want}", p.parse_int(raw) == want)
    for raw in ("1.5", "-3", "1,23", "abc", "", "12.5", "1e3", "₫5"):
        try:
            p.parse_int(raw)
            add(f"parse_int refuses {raw!r}", False)
        except ValueError:
            add(f"parse_int refuses {raw!r}", True)
    add("parse_decimal reads a frequency and refuses junk", p.parse_decimal("2.06") == Decimal("2.06") and _raises(lambda: p.parse_decimal("2,06"), ValueError) and _raises(lambda: p.parse_decimal("-1"), ValueError))

    # dates: the ambiguity rule
    add("date order: 6/16/2024 proves month-first (16 cannot be a month)", p.detect_slash_order(["6/10/2024", "6/16/2024"]) == "mdy")
    add("date order: 16/6/2024 proves day-first", p.detect_slash_order(["16/6/2024", "3/4/2024"]) == "dmy")
    add("date order: ISO dates are ignored and a file with only ISO dates needs no order", p.detect_slash_order(["2025-11-01", "2025-11-02"]) is None and p.detect_slash_order([]) is None)
    add("date order: an ISO date next to slash dates does not disturb the decision", p.detect_slash_order(["2025-11-01", "6/16/2024"]) == "mdy")
    for vals, why in ((["3/4/2024", "5/6/2024"], "both parts 12 or below"), (["16/6/2024", "6/16/2024"], "contradict")):
        try:
            p.detect_slash_order(vals)
            add(f"date order: {vals} is REFUSED, never guessed ({why})", False, "it decided")
        except p.AmbiguousDates as exc:
            add(f"date order: {vals} is REFUSED, never guessed ({why})", why in str(exc), str(exc)[:120])
    add("date order: a date that is no date in either order is an error", _raises(lambda: p.detect_slash_order(["30/30/2024"]), ValueError))
    add("parse_date: ISO always works, a slash date needs the decided order", p.parse_date("2025-11-01", None) == date(2025, 11, 1) and p.parse_date("6/16/2024", "mdy") == date(2024, 6, 16)
        and p.parse_date("16/6/2024", "dmy") == date(2024, 6, 16) and _raises(lambda: p.parse_date("6/16/2024", None), ValueError))
    add("parse_slash_date: the wrong order for the value is an error, not a swap", _raises(lambda: p.parse_slash_date("6/16/2024", "dmy"), ValueError) and _raises(lambda: p.parse_slash_date("2/30/2024", "mdy"), ValueError))
    add("parse_iso_date: refuses non-ISO and impossible dates", _raises(lambda: p.parse_iso_date("2025-13-01"), ValueError) and _raises(lambda: p.parse_iso_date("1/2/2025"), ValueError))
    add("the generic to_date is left as it was (day-first first): the strict parsers exist because of exactly this", __import__("erp.marketing.model", fromlist=["x"]).to_date("6/10/2024") == date(2024, 10, 6))

    # the CSV reader: BOM, CRLF, the line number of every record
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "x.csv"
        f.write_bytes(b"\xef\xbb\xbf" + "Start,End\r\n6/10/2024,6/16/2024\r\n\r\n6/17/2024,6/23/2024,extra\r\n6/24/2024\r\n".encode("utf-8"))
        rows = list(p.read_csv(f))
        add("csv: a UTF-8 byte-order mark is dropped from the first header cell (with it 'Start' would not be 'Start')", p.read_header(f) == ["Start", "End"] and list(rows[0]) == ["Start", "End"], p.read_header(f))
        add("csv: CRLF and a blank line are handled; every record knows its file line", [r.line for r in rows] == [2, 4, 5] and rows[0]["End"] == "6/16/2024", [r.line for r in rows])
        add("csv: a record with too many / too few cells is flagged, never padded", "_extra_columns" in rows[1] and "_missing_columns" in rows[2], rows[1:])
        f.write_bytes("Start\n6/10/2024\n".encode("utf-16"))
        add("csv: a file that is not UTF-8 is an error the connector turns into a refusal, not a silent mojibake", _raises(lambda: list(p.read_csv(f)), UnicodeDecodeError) or _raises(lambda: p.read_header(f), (UnicodeDecodeError, ValueError)))
        f.write_bytes(b"")
        add("csv: an empty file has no header (an error, not an empty success)", _raises(lambda: p.read_header(f), ValueError))


def _quiet(fn):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn()


def _msg(fn, *a) -> str:
    try:
        fn(*a)
    except ValueError as exc:
        return str(exc)
    return ""


def _check_ad_performance(add) -> None:
    from erp.marketing.connectors import strict_csv
    from erp.marketing.connectors.ad_performance import AdPerformanceConnector
    from erp.marketing.model import build_interaction

    conn = _ad([_ad_row()])
    (i,), res = _validate(conn)
    add("ad_performance: the row sits on its START date (M/D/YYYY read month-first), event type rollup, channel paid_social by default", i.event_date == date(2024, 6, 10) and i.event_type == "rollup" and i.channel_key == "paid_social", (i.event_date, i.event_type, i.channel_key))
    add("ad_performance: Impression -> impressions, Link click -> clicks, Eng -> reactions, LP view -> sessions, Purchase -> conversions", (i.impressions, i.clicks, i.reactions, i.sessions, i.conversions) == (16092, 100, 74, 80, 5),
        (i.impressions, i.clicks, i.reactions, i.sessions, i.conversions))
    add("ad_performance: Spent `475,401 <dong>` -> 475401 VND exactly, in integer micros, currency VND", i.spend_micros == 475_401_000_000 and i.currency == "VND", (i.spend_micros, i.currency))
    add("ad_performance: Campaign and Ad name become the campaign and creative dimensions", (i.campaign_key, i.campaign_name, i.creative_key, i.creative_name) == ("cam_1", "Cam 1", "dynamic_ad_02", "Dynamic_ad_02"), (i.campaign_key, i.creative_key))
    a = i.attrs
    add("ad_performance: End -> attrs.period_end, grain=week, 7 days; Adset, Device, Period, Month -> attrs", (a["grain"], a["period_end"], a["period_days"], a["adset"], a["device"], a["period"], a["month"]) == ("week", "2024-06-16", 7, "Group 1", "desktop", "10-Jun", 6), a)
    add("ad_performance: Lead -> attrs.leads and is NOT added to conversions (10 leads, 5 purchases -> 5 conversions)", a["leads"] == 10 and i.conversions == 5)
    add("ad_performance: Reach and Freq are kept in attrs (reach 7797, frequency 2.06) and are not in any measure", a["reach"] == 7797 and a["frequency"] == 2.06 and i.impressions == 16092 and i.sessions == 80)
    add("ad_performance: revenue is DERIVED (Purchase x Revenue/pur = 5,000,000 VND) and MARKED derived in attrs", i.revenue_micros == 5_000_000_000_000 and a["revenue_derived"] is True and "derived" in a["revenue_source"] and a["revenue_per_purchase"] == 1_000_000,
        (i.revenue_micros, a["revenue_derived"], a["revenue_source"]))
    add("ad_performance: the device is filterable through attrs_idx (the scoped GIN), the file line is kept for tracing", i.attrs_idx.get("device") == "desktop" and "source_line" not in a)
    (j,), _r = _validate(_ad([_ad_row(Revenue=f"3,000,000 {DONG}")]))
    add("ad_performance: a filled Revenue column is used as REPORTED (revenue_derived false) instead of the derivation", j.revenue_micros == 3_000_000_000_000 and j.attrs["revenue_derived"] is False and "reported" in j.attrs["revenue_source"], j.attrs)
    (k,), _r = _validate(_ad([_ad_row(**{"Revenue/pur": "", "Purchase": "0"})]))
    add("ad_performance: nothing to derive from means revenue 0 and NOT marked derived", k.revenue_micros == 0 and k.attrs["revenue_derived"] is False and "none" in k.attrs["revenue_source"], k.attrs)
    (m,), _r = _validate(_ad([_ad_row()], channel="Facebook Ads"))
    add("ad_performance: the channel is a parameter (--channel), slugged", m.channel_key == "facebook_ads")
    add("ad_performance: the connector documents its own source key", AdPerformanceConnector.source_key == "ad_performance" and AdPerformanceConnector.DEFAULT_CHANNEL == "paid_social")

    # rows that must be rejected, each by name
    bad_rows = [
        ("End is not Start + 6 days", _ad_row(End="6/20/2024"), "not one 7-day week"),
        ("Spent has no currency", _ad_row(Spent="475,401"), "names no currency"),
        ("Spent in another currency than Revenue/pur", _ad_row(**{"Revenue/pur": "1,000,000 €"}), "one row, one currency"),
        ("a European decimal in Spent", _ad_row(Spent=f"475.401,50 {DONG}"), "Spent"),
        ("the ambiguous '$' symbol", _ad_row(Spent="$475"), "ambiguous"),
        ("a fractional impression count", _ad_row(Impression="16,092.5"), "Impression"),
        ("an empty campaign", _ad_row(Campaign=""), "Campaign is empty"),
        ("a negative purchase count", _ad_row(Purchase="-1"), "Purchase"),
        ("an unreadable frequency", _ad_row(Freq="2,06"), "Freq"),
        ("a non-date Start", _ad_row(Start="tomorrow"), "Start"),
    ]
    for name, row, why in bad_rows:
        items, r = _validate(_ad([_ad_row(), row]))
        add(f"ad_performance rejects a row with {name} (the good row still loads)", len(items) == 1 and r.rows_invalid == 1 and why in " ".join(r.invalid_samples), (len(items), r.invalid_samples))
    items, r = _validate(_ad([_ad_row(), dict(_ad_row(), _extra_columns=["x"])]))
    add("ad_performance rejects a row with the wrong number of cells, by name", len(items) == 1 and r.rows_invalid == 1 and "cells" in " ".join(r.invalid_samples), r.invalid_samples)

    # strict header: exact, no fuzzy mapping
    for name, header in (("a missing column", [h for h in AD_HEADER if h != "Spent"]), ("an extra column", AD_HEADER + ["Currency"]), ("a renamed column", ["Impressions" if h == "Impression" else h for h in AD_HEADER]),
                         ("a wrongly-cased column", ["start" if h == "Start" else h for h in AD_HEADER]), ("a repeated column", AD_HEADER + ["Spent"])):
        msg = _refused(lambda: AdPerformanceConnector.check_header(header))
        add(f"ad_performance REFUSES a file with {name}, naming the difference", msg is not None and ("missing" in msg or "not part of this export" in msg or "repeated" in msg), msg)
    add("ad_performance accepts its exact header in any column order", _refused(lambda: AdPerformanceConnector.check_header(list(reversed(AD_HEADER)))) is None)
    add("ad_performance: the flat_file connector's fuzzy names are NOT used (a 'Date'/'Cost' file is refused, not guessed)", _refused(lambda: AdPerformanceConnector.check_header(["Date", "Cost", "Clicks"])) is not None)

    # dates: refuse to guess
    msg = _refused(lambda: _ad([_ad_row(Start="3/4/2024", End="3/10/2024")]))
    add("ad_performance REFUSES a file whose slash dates could be month-first or day-first, and names the flag", msg is not None and "--date-format" in msg, msg)
    add("ad_performance: ... and --date-format mdy settles it (the operator's decision, not a guess)", _refused(lambda: _ad([_ad_row(Start="3/4/2024", End="3/10/2024")], date_format="mdy")) is None)
    msg = _refused(lambda: _ad([_ad_row()], date_format="dmy"))
    add("ad_performance: --date-format dmy on a file that can only be month-first is refused too", msg is not None and "can only be read mdy" in msg, msg)
    (d1,), _r = _validate(_ad([_ad_row(Start="3/4/2024", End="9/4/2024")], date_format="dmy"))
    add("ad_performance: an ambiguous file read day-first (3/4 = 3 April) is honoured when the operator says so", d1.event_date == date(2024, 4, 3), d1.event_date)
    (iso,), _r = _validate(_ad([_ad_row(Start="2024-06-10", End="2024-06-16")]))
    add("ad_performance: ISO dates need no order at all", iso.event_date == date(2024, 6, 10))

    # duplicates: keep both, report, idempotent
    one, two = _ad_row(Impression="768", Spent=f"81,005 {DONG}"), _ad_row(Impression="341", Spent=f"869 {DONG}")
    other = _ad_row(Device="iphone")
    items, r = _validate(_ad([one, two, other]))
    add("duplicates: two rows under ONE natural key with different metrics are BOTH kept (3 rows in, 3 rows out)", len(items) == 3 and len({i.dedupe_key for i in items}) == 3 and r.rows_duplicate == 0, (len(items), r.rows_duplicate))
    add("duplicates: the occurrence index is 0 for the first, 1 for the second; a different device is a different key (no collision)", [i.occurrence for i in items] == [0, 1, 0] and r.collisions_kept == 1, ([i.occurrence for i in items], r.collisions_kept))
    add("duplicates: the run REPORTS the collision, naming the natural key and the file position", r.collisions_kept == 1 and "natural key" in r.collision_samples[0] and "kept as its own row" in r.collision_samples[0] and "occurrence 1" in r.collision_samples[0], r.collision_samples)
    items2, r2 = _validate(_ad([one, two, other]))
    add("duplicates: the same file again has the SAME identities (a re-import updates, adds nothing - idempotent)", [i.dedupe_key for i in items] == [i.dedupe_key for i in items2] and r2.collisions_kept == 1)
    items3, _r3 = _validate(_ad([two, one, other]))
    by_key = {i.dedupe_key: i.impressions for i in items}
    swapped = {i.dedupe_key: i.impressions for i in items3}
    add("duplicates: the stated trade-off - the SAME rows in another ORDER keep the same identities and totals, but the pairing of the two rows swaps",
        set(by_key) == set(swapped) and sum(by_key.values()) == sum(swapped.values()) and by_key != swapped, (by_key, swapped))
    (pair_a, pair_b), _r = _validate(_ad([_ad_row(Adset="Group 1"), _ad_row(Adset="Group 2")]))
    add("duplicates: rows that differ only by Adset (or Device, or Ad name) are different rows, not collisions", pair_a.dedupe_key != pair_b.dedupe_key and pair_b.occurrence == 0)
    (c1, c2), _r = _validate(_ad([_ad_row(), _ad_row(Campaign="cam 1")]))
    add("duplicates: the natural key ignores case and spacing exactly like the dimensions do (Cam 1 == cam 1 is the same key)", c2.occurrence == 1 and c1.dedupe_key != c2.dedupe_key)
    (f1,), _r = _validate(_ad([_ad_row()]))
    (f2,), _r = _validate(_ad([_ad_row()], channel="facebook"))
    add("duplicates: the channel is part of the identity (the same rows from another platform never collide)", f1.dedupe_key != f2.dedupe_key)
    long_id = strict_csv.natural_external_id("x" * 400, occurrence=1)
    add("duplicates: an identity longer than the column is replaced by its digest, never truncated", len(long_id) <= 200 and long_id.startswith("h:") and long_id != strict_csv.natural_external_id("x" * 400 + "y", occurrence=1))
    add("duplicates: without the connector the generic grain would MERGE the pair (why the occurrence index exists)",
        build_interaction({"source_key": "s", "event_date": "2024-06-17", "channel_key": "paid_social", "campaign_key": "cam_2", "creative_name": "Ad 5", "event_type": "rollup", "impressions": 768}).dedupe_key
        == build_interaction({"source_key": "s", "event_date": "2024-06-17", "channel_key": "paid_social", "campaign_key": "cam_2", "creative_name": "Ad 5", "event_type": "rollup", "impressions": 341}).dedupe_key)

    # the run summary, no database
    items, r = _validate(_ad([_ad_row(), _ad_row(Device="iphone", Spent=f"1,000 {DONG}")]))
    from erp.marketing.pipeline import Pipeline
    res = Pipeline(schema="perf_x").run(_ad([_ad_row(), _ad_row(Device="iphone", Spent=f"1,000 {DONG}")]), dry_run=True)
    add("summary: a dry run reports spend PER CURRENCY (VND 476,401 - never a blended figure)", res.money == {"VND": {"spend_micros": 476_401_000_000, "revenue_micros": 10_000_000_000_000}} and res.status == "ok" and res.rows_read == 2, res.money)
    from erp.marketing.ingest import money_lines
    add("summary: the money lines print one line per currency", money_lines({"VND": {"spend_micros": 5_000_000, "revenue_micros": 0}, "EUR": {"spend_micros": 0, "revenue_micros": 2_000_000}}) == ["  money EUR      spend 0.00   revenue 2.00", "  money VND      spend 5.00   revenue 0.00"])


def _check_email_campaign(add) -> None:
    from erp.marketing.connectors.email_campaign import EmailCampaignConnector

    (i,), res = _validate(_email([_email_row()]))
    add("email_campaign: send_date -> event_date (ISO), channel email by default, event type rollup", i.event_date == date(2025, 11, 1) and i.channel_key == "email" and i.event_type == "rollup", (i.event_date, i.channel_key))
    add("email_campaign: delivered -> impressions, unique_opens -> reactions, unique_clicks -> clicks, orders -> conversions", (i.impressions, i.reactions, i.clicks, i.conversions) == (35768, 6472, 847, 42), (i.impressions, i.reactions, i.clicks, i.conversions))
    add("email_campaign: revenue_eur -> revenue in EUR (1249.13 -> 1,249,130,000 micros), currency EUR, no spend, no sessions", i.revenue_micros == 1_249_130_000 and i.currency == "EUR" and i.spend_micros == 0 and i.sessions == 0, (i.revenue_micros, i.currency))
    add("email_campaign: campaign_id -> the campaign dimension (key = slug, name = the id)", (i.campaign_key, i.campaign_name) == ("cmp_gif_20251101", "CMP-GIF-20251101") and i.creative_key is None, (i.campaign_key, i.campaign_name))
    a = i.attrs
    add("email_campaign: brand, country, segment, unsubscribes, spam_complaints go to attrs; unique_opens and delivered keep their exact values", (a["brand"], a["country"], a["segment"], a["unsubscribes"], a["spam_complaints"], a["unique_opens"], a["delivered"]) == ("GiftLoom", "DE", "Engaged", 37, 3, 6472, 35768), a)
    add("email_campaign: 'delivered' is DOCUMENTED as not an ad impression, and unique_opens as not a social reaction, in attrs", a["impressions_are"] == "delivered" and a["reactions_are"] == "unique_opens" and a["grain"] == "day", a)
    add("email_campaign: brand, country and segment are filterable through attrs_idx (the scoped GIN), lower-cased", i.attrs_idx.get("brand") == "giftloom" and i.attrs_idx.get("country") == "de" and i.attrs_idx.get("segment") == "engaged", i.attrs_idx)
    from erp.marketing.model import INDEXED_ATTR_KEYS
    add("email_campaign: the GIN allowlist holds brand, country, segment and device (and stays short)", {"brand", "country", "segment", "device"} <= set(INDEXED_ATTR_KEYS) and len(INDEXED_ATTR_KEYS) <= 10, INDEXED_ATTR_KEYS)

    # sanity rules: rejected, never loaded quietly
    for name, row, why in (("unique_opens above delivered", _email_row(unique_opens="40,000"), "unique_opens (40,000) is more than delivered"),
                           ("unique_clicks above unique_opens", _email_row(unique_clicks="7000"), "unique_clicks (7,000) is more than unique_opens"),
                           ("a negative order count", _email_row(orders="-1"), "orders"), ("an unreadable revenue", _email_row(revenue_eur="1.249,13"), "revenue_eur"),
                           ("revenue written in dollars", _email_row(revenue_eur="USD 12.5"), "EUR"), ("no campaign_id", _email_row(campaign_id=""), "campaign_id is empty")):
        items, r = _validate(_email([_email_row(), row]))
        add(f"email_campaign REJECTS a row with {name} (counted, reason given, the good row loads)", len(items) == 1 and r.rows_invalid == 1 and why in " ".join(r.invalid_samples), (len(items), r.invalid_samples))
    add("email_campaign REFUSES an ISO file that also holds an ambiguous slash date (11/1/2025), rather than guessing", _refused(lambda: _email([_email_row(), _email_row(send_date="11/1/2025")])) is not None)
    items, r = _validate(_email([_email_row(unique_opens="35768", unique_clicks="35768")]))
    add("email_campaign: the boundaries themselves are fine (opens == delivered, clicks == opens)", len(items) == 1 and r.rows_invalid == 0)

    for name, header in (("a missing column", [h for h in EMAIL_HEADER if h != "segment"]), ("an extra column", EMAIL_HEADER + ["revenue_usd"]), ("another currency's revenue column", ["revenue_usd" if h == "revenue_eur" else h for h in EMAIL_HEADER])):
        msg = _refused(lambda: EmailCampaignConnector.check_header(header))
        add(f"email_campaign REFUSES a file with {name}", msg is not None, msg)
    # keys: unique campaign_id in the sample; a repeat is kept and reported
    items, r = _validate(_email([_email_row(), _email_row(campaign_id="CMP-GIF-20251103")]))
    add("email_campaign: distinct campaign ids are distinct rows, no collision", len({i.dedupe_key for i in items}) == 2 and r.collisions_kept == 0)
    items, r = _validate(_email([_email_row(), _email_row(unique_opens="6000")]))
    add("email_campaign: the same campaign / day / brand / country / segment twice is KEPT (occurrence 1) and REPORTED, never merged", len({i.dedupe_key for i in items}) == 2 and r.collisions_kept == 1 and r.rows_duplicate == 0, (r.collisions_kept, r.rows_duplicate))
    (a1,), _r = _validate(_email([_email_row(segment="VIP")]))
    add("email_campaign: another segment is another row", a1.dedupe_key != i.dedupe_key)
    res = __import__("erp.marketing.pipeline", fromlist=["x"]).Pipeline(schema="perf_x").run(_email([_email_row(), _email_row(campaign_id="CMP-GIF-20251103", revenue_eur="10.5")]), dry_run=True)
    add("email_campaign: a dry run reports EUR revenue per currency, no spend", res.money == {"EUR": {"spend_micros": 0, "revenue_micros": 1_259_630_000}}, res.money)


def _check_currency_model(add) -> None:
    from erp.marketing import model as m
    from erp.marketing.connectors.flat_file import FlatFileConnector

    base = {"source_key": "s", "event_date": "2026-09-24", "event_type": "click", "clicks": 1}
    add("currency: a record without one is the DEFAULT currency (USD), documented", m.build_interaction(base).currency == "USD" == m.DEFAULT_CURRENCY)
    add("currency: 'vnd' / ' eur ' are read as VND / EUR", m.build_interaction(dict(base, currency="vnd")).currency == "VND" and m.build_interaction(dict(base, currency=" eur ")).currency == "EUR")
    for bad in ("US", "dollars", "US1", "€", "1234"):
        try:
            m.build_interaction(dict(base, currency=bad))
            add(f"currency: {bad!r} rejects the row (three letters only)", False, "accepted")
        except m.InvalidRecord as exc:
            add(f"currency: {bad!r} rejects the row (three letters only)", "ISO 4217" in str(exc), str(exc))
    add("currency: it is NOT part of the identity - the same row restated in another currency updates that row instead of adding one",
        m.build_interaction(dict(base, currency="VND")).dedupe_key == m.build_interaction(dict(base, currency="EUR")).dedupe_key)
    add("currency: to_currency is strict", m.to_currency("vnd") == "VND" and m.to_currency("VN") is None and m.to_currency(None) is None and m.to_currency("") is None)
    ex = m.build_interaction(dict(base, spend="1", spend_micros=475_401_000_000))
    add("money: an exact `spend_micros` (a strict connector's Decimal arithmetic) wins over the float path", ex.spend_micros == 475_401_000_000)
    big = m.build_interaction(dict(base, spend_micros=5_000_000_000 * 10 ** 6))
    add("money: 5,000,000,000 VND in one row is accepted (the bound was 1e15 micros = 1 billion units, too small for VND; now 1e16)", big.spend_micros == 5_000_000_000_000_000 and m.MAX_MONEY_MICROS == 10 ** 16, big.spend_micros)
    add("money: a value beyond even that is still refused as a corrupted cell", _rejects_money(str(10 ** 10 + 1)))
    fc = FlatFileConnector([], source_key="x", currency_default="vnd")
    mapped = fc.map_record({"Date": "2026-09-24", "Clicks": "3", "Spend": "100"})
    add("flat_file: --currency sets the currency of a file with no currency column (VND), nothing converted", mapped["currency"] == "VND" and m.build_interaction(mapped).currency == "VND", mapped["currency"])
    mapped = FlatFileConnector([]).map_record({"Date": "2026-09-24", "Clicks": "3"})
    add("flat_file: with neither a column nor a flag the currency is left to the model's default (USD)", mapped["currency"] is None and m.build_interaction(mapped).currency == "USD")
    mapped = FlatFileConnector([], currency_default="USD").map_record({"Date": "2026-09-24", "Clicks": "3", "Currency": "EUR", "Spend": "5"})
    i = m.build_interaction(mapped)
    add("flat_file: a `currency` column wins over the flag and is not repeated in attrs", i.currency == "EUR" and "currency" not in i.attrs, (i.currency, i.attrs))
    try:
        m.build_interaction(FlatFileConnector([]).map_record({"Date": "2026-09-24", "Clicks": "3", "Currency": "US Dollar"}))
        add("flat_file: a currency column that is not a code rejects the row", False)
    except m.InvalidRecord:
        add("flat_file: a currency column that is not a code rejects the row", True)


def _check_pipeline_currency(add) -> None:
    """The pipeline writes the currency and reports collisions: proved against a recording stand-in for the bulk writer (no database)."""
    from erp.marketing import pipeline as pl
    from erp.marketing.model import build_interaction

    captured: dict = {}

    def fake_values(conn, sql, rows, template=None, fetch=False):
        captured.setdefault("calls", []).append((sql, rows, template))
        return []
    items = [(build_interaction({"source_key": "s", "event_date": "2026-09-24", "event_type": "click", "clicks": 1, "currency": "VND", "spend": "5"}), 0)]
    ids = {"source_id": 1, "channel": {"unknown": 1}, "campaign": {}, "creative": {}, "identity": {}}
    old = pl._values
    pl._values = fake_values
    try:
        res = pl.BatchResult(batch_id="t", source="s", schema="perf_x")
        pl.Pipeline(schema="perf_x")._load_fact(object(), items, ids, {0: 1}, res)
    finally:
        pl._values = old
    sql, rows, template = captured["calls"][-1]
    add("pipeline: the fact INSERT names the currency column and the restatement (ON CONFLICT) updates it", "currency) VALUES" in sql and "currency = EXCLUDED.currency" in sql, sql[-260:])
    add("pipeline: the row carries the currency as its LAST value and the template has exactly one placeholder per value", rows[0][-1] == "VND" and template.count("%s") == len(rows[0]) == 20, (rows[0][-1], template.count("%s"), len(rows[0])))

    # the run-level collision check across chunks: same identity in two committed chunks -> replaced, reported
    dst = pl.BatchResult(batch_id="t", source="s", schema="perf_x")
    src1 = pl.BatchResult(batch_id="t", source="s", schema="perf_x")
    src2 = pl.BatchResult(batch_id="t", source="s", schema="perf_x")
    key = uuid.uuid4()
    src1.chunk_keys = [(date(2026, 9, 24), key, 3)]
    src2.chunk_keys = [(date(2026, 9, 24), key, 9), (date(2026, 9, 25), uuid.uuid4(), 10)]
    pl._merge_result(dst, src1)
    pl._merge_result(dst, src2)
    add("pipeline: the same identity in two chunks of one run is counted as a collision (replaced) and reported with both positions", dst.collisions_replaced == 1 and dst.rows_duplicate == 1 and "record #9" in dst.collision_samples[0] and "#3" in dst.collision_samples[0], (dst.collisions_replaced, dst.collision_samples))
    a, b = pl.BatchResult(batch_id="t", source="s", schema="x"), pl.BatchResult(batch_id="t", source="s", schema="x")
    a.money, b.money = {"VND": {"spend_micros": 5, "revenue_micros": 1}}, {"VND": {"spend_micros": 7, "revenue_micros": 0}, "EUR": {"spend_micros": 0, "revenue_micros": 2}}
    pl._merge_result(a, b)
    add("pipeline: money merges per currency (VND 12, EUR 2) and never across", a.money == {"VND": {"spend_micros": 12, "revenue_micros": 1}, "EUR": {"spend_micros": 0, "revenue_micros": 2}}, a.money)
    from erp.marketing.connectors.flat_file import FlatFileConnector
    rows = [{"Date": "2026-09-24", "Channel": "facebook", "Campaign": "A", "Clicks": "1"}, {"Date": "2026-09-24", "Channel": "facebook", "Campaign": "A", "Clicks": "9"}]
    res = pl.Pipeline(schema="perf_x").run(FlatFileConnector(rows), dry_run=True)
    add("pipeline: two generic rows with ONE identity are reported as REPLACED (the old silent overwrite), not hidden", res.rows_duplicate == 1 and res.collisions_replaced == 1 and "REPLACED" in res.collision_samples[0], (res.rows_duplicate, res.collision_samples))
    add("pipeline: the summary line mentions collisions only when there are some", "collisions" not in pl.BatchResult(batch_id="t", source="s", schema="x").summary() and "1 replaced" in res.summary(), res.summary())

    # a fake connection: no currency column -> a refusal that names the fix; with it -> fine
    from erp.marketing import schema as ms

    class _Conn:
        def __init__(self, ok):
            self.ok = ok

        def execute(self, *a, **k):
            class _R:
                def scalar(_s):
                    return self.ok
            return _R()
    try:
        ms.require_currency_column(_Conn(False), "public")
        refused = ""
    except ms.SchemaError as exc:
        refused = str(exc)
    add("schema: a fact table without the currency column is refused with the exact migration command", "10_marketing_currency.sql" in refused and "psql.exe" in refused and "currency" in refused, refused[:200])
    add("schema: with the column the check passes", ms.require_currency_column(_Conn(True), "public") is None and ms.has_currency_column(_Conn(True), "public") is True)


def _check_migration_and_rollup_ddl(add) -> None:
    from erp.marketing import rollup as r

    sql10 = (ROOT / "db" / "sql" / "10_marketing_currency.sql").read_text(encoding="utf-8")
    code10 = "\n".join(line for line in sql10.splitlines() if not line.strip().startswith("--"))
    add("10: the migration is ADDITIVE and idempotent (ADD COLUMN IF NOT EXISTS), on the fact table only", "ADD COLUMN IF NOT EXISTS currency" in code10 and code10.upper().count("ALTER TABLE") == 1 and "ALTER TABLE interaction_fact" in code10)
    add("10: a NOT NULL 3-letter column with a CONSTANT default (no table rewrite) and a CHECK on the shape", "CHAR(3) NOT NULL DEFAULT 'USD'" in code10 and "CHECK (currency ~ '^[A-Z]{3}$')" in code10)
    add("10: it drops, updates, deletes and renames nothing", not any(w in code10.upper() for w in (" DROP ", "UPDATE ", "DELETE ", "RENAME", "TRUNCATE")))
    add("10: it documents the default, the order (07 -> 10 -> 09) and that nothing is converted", "'USD'" in sql10 and "07" in sql10 and "09" in sql10 and "nothing is converted" in sql10.lower() and "exchange rate" in sql10)
    add("10: it arms a lock_timeout so it can never queue every reader behind a lock", "lock_timeout" in code10)
    sql09 = (ROOT / "db" / "sql" / "09_marketing_rollup.sql").read_text(encoding="utf-8")
    add("09: both rollup tables carry currency and grain in their key (campaign and channel level)", "PRIMARY KEY (event_date, channel_id, campaign_id, currency, grain)" in sql09
        and "pk_interaction_daily_channel_rollup PRIMARY KEY (event_date, channel_id, currency, grain)" in sql09)
    add("09: currency and grain have CHECKs (a 3-letter code; day or week) and the derived-revenue column exists on both", sql09.count("CHECK (currency ~ '^[A-Z]{3}$')") == 2 and sql09.count("CHECK (grain IN ('day', 'week'))") == 2 and sql09.count("revenue_derived_micros") >= 2)
    add("09: it is still create-only (the edit did not add an ALTER or DROP: 09 is not installed anywhere yet)", "ALTER TABLE" not in sql09.upper() and " DROP " not in sql09.upper())
    add("09: the job's constants agree with the DDL (revenue_derived_micros is a measure the job writes)", "revenue_derived_micros" in r.MEASURE_COLUMNS and "revenue_derived_micros" in r.SUM_COLUMNS)
    add("09: the grain expression is a two-value CASE (a stray attrs value cannot invent a third grain)", r.GRAIN_SQL == "CASE WHEN attrs->>'grain' = 'week' THEN 'week' ELSE 'day' END")

    # refresh_range against a recording connection: what does it really group by?
    class _Rec:
        def __init__(self):
            self.sql: list[str] = []

        def execute(self, stmt, params=None):
            self.sql.append(str(stmt))

            class _R:
                rowcount = 1
            return _R()
    rec = _Rec()
    r.refresh_range(rec, "perf_x", (date(2026, 9, 1), date(2026, 10, 1)))
    ins_campaign = next(s for s in rec.sql if "INSERT INTO perf_x.interaction_daily_rollup" in s)
    ins_channel = next(s for s in rec.sql if "INSERT INTO perf_x.interaction_daily_channel_rollup" in s)
    add("refresh: the campaign level inserts and GROUPS BY currency and grain (a VND row and a EUR row can never share a rollup row)", "currency, grain" in " ".join(ins_campaign.split()) and "GROUP BY event_date, channel_id, COALESCE(campaign_id, 0), currency, CASE WHEN" in " ".join(ins_campaign.split()), " ".join(ins_campaign.split())[-300:])
    add("refresh: the channel level is summed from the campaign level by (day, channel, currency, grain) - the sum never crosses them", "GROUP BY event_date, channel_id, currency, grain" in " ".join(ins_channel.split()) and "FROM perf_x.interaction_daily_rollup" in ins_channel, " ".join(ins_channel.split())[-260:])
    add("refresh: the derived part of revenue is summed separately from attrs.revenue_derived", "revenue_derived_micros" in ins_campaign and "attrs->>'revenue_derived' = 'true'" in ins_campaign)
    add("refresh: the job needs the fact's currency column (require_currency_column is in its install check)", "require_currency_column" in __import__("inspect").getsource(r.require_rollup_installed))
    add("the rollup verification scripts install migration 10 too (else the job could not run there)", "DDL_CURRENCY" in (ROOT / "erp" / "marketing" / "perf_check.py").read_text(encoding="utf-8") and "DDL_CURRENCY" in (ROOT / "erp" / "marketing" / "rollup_check.py").read_text(encoding="utf-8"))
    add("the rollup verification compares currency and grain too (its direct GROUP BY)", "currency" in (ROOT / "erp" / "marketing" / "rollup_check.py").read_text(encoding="utf-8").split("def _mismatches")[1].split("def _totals")[0])


def _check_ingest_cli(add) -> None:
    import tempfile

    from erp.marketing import ingest

    p = ingest.build_parser()
    a = p.parse_args(["--connector", "ad_performance", "--csv", "x.csv", "--channel", "facebook", "--date-format", "mdy", "--dry-run", "--schema", "perf_x"])
    add("cli: --connector, --csv, --channel, --schema, --dry-run and --date-format are understood", (a.connector, a.channel, a.date_format, a.dry_run, a.schema) == ("ad_performance", "facebook", "mdy", True, "perf_x"))
    add("cli: the connector defaults to the generic flat_file (existing commands keep working)", p.parse_args(["--csv", "x.csv"]).connector == "flat_file" and set(ingest.CONNECTORS) == {"flat_file", "ad_performance", "email_campaign", "placement_performance"})
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            p.parse_args(["--csv", "x.csv", "--connector", "nope"])
        add("cli: an unknown connector is refused by argparse", False)
    except SystemExit:
        add("cli: an unknown connector is refused by argparse", True)
    add("cli: --currency is refused for the strict connectors (they read it from the file) and checked for the generic one",
        _raises(lambda: ingest.build_connector(p.parse_args(["--csv", "x", "--connector", "ad_performance", "--currency", "VND"]), Path("x")), ValueError)
        and _raises(lambda: ingest.build_connector(p.parse_args(["--csv", "x", "--currency", "dollars"]), Path("x")), ValueError))
    with tempfile.TemporaryDirectory() as d:
        good = Path(d) / "ad.csv"
        good.write_bytes(b"\xef\xbb\xbf" + ",".join(f'"{h}"' for h in AD_HEADER).encode("utf-8") + b"\r\n" + ",".join(f'"{_ad_row()[h]}"' for h in AD_HEADER).encode("utf-8") + b"\r\n")
        conn = ingest.build_connector(p.parse_args(["--csv", str(good), "--connector", "ad_performance"]), good)
        items, r = _validate(conn)
        add("cli: a real BOM + CRLF file with quoted thousands separators loads through the connector end to end (no database)", len(items) == 1 and items[0].spend_micros == 475_401_000_000 and items[0].currency == "VND" and items[0].attrs["source_line"] == 2, (len(items), r.invalid_samples))
        conn = ingest.build_connector(p.parse_args(["--csv", str(good), "--connector", "ad_performance", "--source", "Meta Export"]), good)
        add("cli: --source overrides the source key (slugged)", conn.source_key == "meta_export")
        bad = Path(d) / "bad.csv"
        bad.write_text("Date,Cost\n2024-06-10,5\n", encoding="utf-8")
        amb = Path(d) / "amb.csv"
        amb.write_text(",".join(f'"{h}"' for h in AD_HEADER) + "\n" + ",".join(f'"{_ad_row(Start="3/4/2024", End="3/10/2024")[h]}"' for h in AD_HEADER) + "\n", encoding="utf-8")
        with _root_logging_kept(), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code_bad = ingest.main(["--connector", "ad_performance", "--csv", str(bad), "--dry-run"])
            code_amb = ingest.main(["--connector", "ad_performance", "--csv", str(amb), "--dry-run"])
            code_ok = ingest.main(["--connector", "ad_performance", "--csv", str(amb), "--dry-run", "--date-format", "mdy"])
            code_missing = ingest.main(["--csv", str(Path(d) / "nope.csv"), "--dry-run"])
            code_cur = ingest.main(["--connector", "email_campaign", "--csv", str(good), "--currency", "EUR", "--dry-run"])
        add("cli: a wrong header is exit 1, before anything is read or any database touched", code_bad == 1)
        add("cli: an ambiguous-date file is exit 1 (refused), and exit 0 once --date-format settles it", code_amb == 1 and code_ok == 0, (code_amb, code_ok))
        add("cli: a missing file / a flag combination that makes no sense is exit 1", code_missing == 1 and code_cur == 1)
        # a console code page that cannot show a quoted cell must not turn a finished run into a traceback + exit 1
        from erp.marketing import pipeline as pl
        real_pipeline = pl.Pipeline

        class _FakePipeline:
            def __init__(self, **kw): pass

            def run(self, connector, limit=None, dry_run=False):
                r = pl.BatchResult(batch_id="b", source="s", schema="perf_x", rows_read=1, rows_invalid=1)
                r.invalid_samples = ["line 2: bad value '16ạ092' / Việt"]
                r.collision_samples = ["line 3: campaign Đà Nẵng"]
                r.money = {"VND": {"spend_micros": 1_000_000, "revenue_micros": 0}}
                r.status = "ok"
                return r

        raw = io.BytesIO()
        cp = io.TextIOWrapper(raw, encoding="cp1252", errors="strict", newline="")
        try:
            pl.Pipeline = _FakePipeline
            with _root_logging_kept(), contextlib.redirect_stdout(cp), contextlib.redirect_stderr(io.StringIO()):
                code_enc = ingest.main(["--connector", "ad_performance", "--csv", str(good), "--dry-run"])
            cp.flush()
        finally:
            pl.Pipeline = real_pipeline
        out = raw.getvalue().decode("cp1252")
        add("cli: the run summary survives a console code page that cannot show a quoted cell (exit 0, status line printed)",
            code_enc == 0 and "status          ok" in out and "line 2" in out, (code_enc, out[-120:]))
    from erp.marketing import sample_check as sc
    add("sample_check: refuses a schema name that could be public (only perf_...), and needs --yes", sc.SCHEMA_RE.match("perf_samples") is not None and sc.SCHEMA_RE.match("public") is None and sc.SCHEMA_RE.match("perf_x; drop") is None
        and _quiet(lambda: sc.main(["--drop", "public"])) == 2 and _quiet(lambda: sc.main([])) == 2)
    add("sample_check: it installs 07, 10 and 09 in that order and never touches the data files (it only reads them)", [Path(x).name for x in sc.DDL] == ["07_marketing_schema.sql", "10_marketing_currency.sql", "09_marketing_rollup.sql"])


# =========================================================================================== runner
def run(db_ok: bool = False) -> list[tuple[str, bool, object]]:
    """Every scenario. `db_ok` is accepted for a uniform signature with the other tables; nothing here needs a
    database - the SQL side of this feature is verified once by erp/marketing/perf_check.py, not on every gate run."""
    rows: list[tuple[str, bool, object]] = []

    def add(name: str, ok: bool, why: object = "") -> None:
        rows.append((name, bool(ok), why))

    for fn in (_check_types, _check_identity, _check_validation, _check_batch_survives,
               _check_sql_failure_isolation, _check_chunks,
               _check_dedupe, _check_attrs, _check_connector, _check_schema, _check_ddl_matches_code,
               _check_rollup_logic, _check_partition_maintenance, _check_rollup_cli,
               _check_strict_parsers, _check_ad_performance, _check_email_campaign, _check_currency_model, _check_pipeline_currency,
               _check_migration_and_rollup_ddl, _check_ingest_cli, _check_audit_fixes, _check_generic_currency_column):
        try:
            fn(add)
        except Exception as exc:  # noqa: BLE001
            import traceback
            add(f"{fn.__name__} crashed", False,
                f"{type(exc).__name__}: {exc} | {traceback.format_exc().splitlines()[-3][:160]}")
    return rows


def _check_audit_fixes(add) -> None:
    """The audit round: locale-proof money, ASCII-only digits, padded headers, restated-changed count, unknown revenue."""
    import tempfile
    from decimal import Decimal

    from erp.marketing import ingest, parse as p
    from erp.marketing import pipeline as pl

    D = Decimal
    # (a) zero-decimal currencies: a decimal part is refused, three-digit groups are thousands only in the grouped form
    for raw, want in [(f"475.401 {DONG}", (D("475401"), "VND")), (f"475,401 {DONG}", (D("475401"), "VND")), (f"1.234.567 {DONG}", (D("1234567"), "VND")),
                      (f"1,234,567 {DONG}", (D("1234567"), "VND")), ("475401 VND", (D("475401"), "VND")), ("VND 1.000", (D("1000"), "VND")),
                      ("1.234.567 JPY", (D("1234567"), "JPY")), ("5000 KRW", (D("5000"), "KRW"))]:
        add(f"locale: parse_money({raw!r}) == {want} (thousands of a zero-decimal currency)", _try(p.parse_money, raw) == want, _msg(p.parse_money, raw))
    for raw in (f"475.40 {DONG}", f"475,4 {DONG}", f"475.4 {DONG}", f"1,234.56 {DONG}", f"1.234,56 {DONG}", f"12.0 {DONG}", f"1,23,456 {DONG}", f"1.234,567 {DONG}",
                f"1,234.567 {DONG}", "100.5 JPY", "1,5 KRW", f"0.401 {DONG}"):
        m = _msg(p.parse_money, raw)
        add(f"locale: parse_money refuses {raw!r} (zero-decimal currency: no decimal part)", bool(m), m[:100])
    add("locale: the refusal for a zero-decimal currency says which forms are accepted", "no decimals" in _msg(p.parse_money, f"475.40 {DONG}") and "Accepted" in _msg(p.parse_money, f"475.40 {DONG}"))
    # (b) currencies with decimals: ambiguity is refused with the accepted forms named
    for raw, want in [("1249.13", (D("1249.13"), None)), ("1,249.13", (D("1249.13"), None)), ("1,249.13 EUR", (D("1249.13"), "EUR")), ("1249", (D("1249"), None)),
                      ("0.125", (D("0.125"), None)), ("1,234,567", (D("1234567"), None)), ("12.5 EUR", (D("12.5"), "EUR")), ("1,249.130", (D("1249.130"), None))]:
        add(f"locale: parse_money({raw!r}) == {want}", _try(p.parse_money, raw) == want, _msg(p.parse_money, raw))
    for raw in ("1.249", "1,249", "249,130", "249.130 EUR", "1.249,13", "1.249,13 EUR", "1249,13", "1.249.130", "12,5", "1,23,456"):
        m = _msg(p.parse_money, raw)
        add(f"locale: parse_money refuses {raw!r} (ambiguous or European) and says what is accepted", "Accepted" in m or "not a plain amount" in m, m[:120])
    add("locale: '1.249' is called AMBIGUOUS and both readings are named", "ambiguous" in _msg(p.parse_money, "1.249") and "1.249 or 1249" in _msg(p.parse_money, "1.249"), _msg(p.parse_money, "1.249"))
    add("locale: '1.249,13' is refused explicitly as the European form", "European" in _msg(p.parse_money, "1.249,13"), _msg(p.parse_money, "1.249,13"))
    add("locale: the same '475.401' is 475401 under VND (hint) but refused as ambiguous under EUR", p.parse_money("475.401", currency_hint="VND") == (D("475401"), None)
        and "ambiguous" in _msg(lambda x: p.parse_money(x, currency_hint="EUR"), "475.401"))
    add("locale: a symbol-less cell with no currency to go by follows the strict decimal rules ('1,249' refused)", "ambiguous" in _msg(p.parse_money, "1,249"))
    for raw in ("12 eur", "-5", "$12", "-475,401 " + DONG, "eur 12", "EUR-5"):
        m = _msg(p.parse_money, raw)
        add(f"locale: parse_money refuses {raw!r}", bool(m), m[:100])
    add("locale: '12 eur' is refused for the lowercase code, '$12' for the ambiguous symbol", "capital" in _msg(p.parse_money, "12 eur") and "ambiguous" in _msg(p.parse_money, "$12"))
    # (c) ASCII digits only
    fullwidth, arabic = "１２３", "١٢٣"
    for label, raw in (("fullwidth", fullwidth), ("Arabic-Indic", arabic), ("fullwidth with EUR", fullwidth + " EUR"), ("fullwidth with dong", fullwidth + DONG), ("mixed", "1２")):
        add(f"digits: parse_money refuses {label} digits", bool(_msg(p.parse_money, raw)), raw)
    add("digits: parse_int refuses fullwidth and Arabic-Indic digits", bool(_msg(p.parse_int, fullwidth)) and bool(_msg(p.parse_int, arabic)))
    add("digits: parse_decimal refuses fullwidth digits", bool(_msg(p.parse_decimal, "１.５")))
    add("digits: ISO and slash dates refuse fullwidth and Arabic-Indic digits",
        bool(_msg(p.parse_iso_date, "２０２５-01-01")) and bool(_msg(p.parse_slash_date, "٦/١٦/2024", "mdy"))
        and bool(_msg(p.parse_date, "２０２５-01-01", None)) and p.detect_slash_order(["٦/١٦/2024"]) is None)
    # the connectors: a locale-variant Spent cell is a rejected row, not a silent factor of 1000
    items, res = _validate(_ad([_ad_row(Spent=f"475.401 {DONG}", Device="a"), _ad_row(Spent=f"475.40 {DONG}", Device="b"), _ad_row(Spent=f"1.249,13 {DONG}", Device="c")]))
    add("ad_performance: `475.401 <dong>` is read as 475,401 VND (dot thousands), never 475.401; a decimal or European Spent cell is rejected",
        len(items) == 1 and items[0].spend_micros == 475_401_000_000 and res.rows_invalid == 2 and "Spent" in res.invalid_samples[0], (len(items), res.rows_invalid, res.invalid_samples[:1]))
    items, res = _validate(_ad([_ad_row(**{"Revenue/pur": "1.249", "Purchase": "2"})]))
    add("ad_performance: a symbol-less Revenue/pur cell is parsed under the row's currency: '1.249' under VND is 1,249", len(items) == 1 and items[0].revenue_micros == 2_498_000_000, (res.invalid_samples, [i.revenue_micros for i in items]))
    items, res = _validate(_email([_email_row(revenue_eur="1.249"), _email_row(campaign_id="C2", revenue_eur="1.249,13"), _email_row(campaign_id="C3", revenue_eur="1,249.13")]))
    add("email_campaign: revenue_eur '1.249' and '1.249,13' are rejected rows, '1,249.13' loads as 1249.13 EUR", len(items) == 1 and items[0].revenue_micros == 1_249_130_000 and res.rows_invalid == 2, (len(items), res.invalid_samples))

    # (2) a padded header name is refused (the trailing-space Revenue case), from a real CSV and from rows
    from erp.marketing.connectors.ad_performance import AdPerformanceConnector
    padded = [("Revenue " if h == "Revenue" else h) for h in AD_HEADER]
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "padded.csv"
        row = _ad_row(Revenue=f"9,000,000 {DONG}")
        f.write_bytes(("﻿" + ",".join(f'"{h}"' for h in padded) + "\r\n" + ",".join(f'"{row[h]}"' for h in AD_HEADER) + "\r\n").encode("utf-8"))
        why = _refused(lambda: AdPerformanceConnector.from_csv(f))
        add("header: a data1 copy whose header is `Revenue ` (trailing space) is REFUSED, naming the padded column (before: header passed, reported Revenue ignored)",
            bool(why) and "'Revenue '" in why and "spaces" in why, why)
        f.write_bytes(("﻿" + ",".join(f'"{h}"' for h in AD_HEADER) + "\r\n" + ",".join(f'"{row[h]}"' for h in AD_HEADER) + "\r\n").encode("utf-8"))
        (i,), _r = _validate(AdPerformanceConnector.from_csv(f))
        add("header: the same file with a clean header loads and the reported Revenue is used (9,000,000 VND, not the derived 5,000,000)", i.revenue_micros == 9_000_000_000_000, i.revenue_micros)
    bad = _ad_row()
    bad["Revenue "] = bad.pop("Revenue")
    add("header: padded names are refused for in-memory rows too", bool(_refused(lambda: _ad([bad]))))
    add("header: a leading space is refused as well", bool(_refused(lambda: _ad([{(" " + k if k == "Eng" else k): v for k, v in _ad_row().items()}]))))

    # (3) restated rows whose stored numbers changed: counted from the lookup that already exists, no second pass
    calls = []
    stored: dict = {}

    def fake_values(conn, sql, rows, template=None, fetch=False):
        calls.append(sql)
        if fetch:
            return [(k[0], k[1], *stored[k]) for k in (tuple(r) for r in rows) if k in stored]
        return []
    ids = {"source_id": 1, "channel": {"paid_social": 1}, "campaign": {}, "creative": {}, "identity": {}}
    three = _validate(_ad([_ad_row(Device="a"), _ad_row(Device="b"), _ad_row(Device="c")]))[0]
    three_items = [(i, n) for n, i in enumerate(three)]
    i0 = three[0]
    same = (i0.impressions, i0.clicks, i0.reactions, i0.sessions, i0.conversions, i0.spend_micros, i0.revenue_micros, "VND")
    changed = (same[0] + 1,) + same[1:]
    stored[(three[0].event_date, str(three[0].dedupe_key))] = same                # identical restatement
    stored[(three[1].event_date, str(three[1].dedupe_key))] = changed             # restated with different numbers
    old = pl._values
    pl._values = fake_values
    try:
        res = pl.BatchResult(batch_id="t", source="s", schema="perf_x")
        pl.Pipeline(schema="perf_x")._load_fact(object(), three_items, ids, {0: 1, 1: 2, 2: 3}, res)
    finally:
        pl._values = old
    add("restated: 3 rows, one new, one identical restatement, one restated with changed numbers -> 1 new, 2 restated, 1 changed",
        (res.rows_inserted, res.rows_updated, res.rows_restated_changed) == (1, 2, 1), (res.rows_inserted, res.rows_updated, res.rows_restated_changed))
    add("restated: no second pass - one lookup query and one upsert for the chunk, and the lookup itself returns the stored numbers",
        len(calls) == 2 and "t.spend_micros" in calls[0] and calls[1].startswith("INSERT"), calls[:1])
    dst = pl.BatchResult(batch_id="t", source="s", schema="x")
    pl._merge_result(dst, res)
    add("restated: the changed count survives the merge of an isolation attempt and is part of the summary line", dst.rows_restated_changed == 1 and "1 of them changed" in dst.summary(), dst.summary())
    add("restated: the CLI prints the count on a real run (the line sits under 'written to fact')", "restated changed" in Path(ingest.__file__).read_text(encoding="utf-8"))

    # (5) revenue UNKNOWN is not revenue 0
    no_rev = {"Revenue/pur": "", "Revenue": ""}
    items, _r = _validate(_ad([_ad_row(**no_rev)]))
    add("revenue: a row with neither Revenue nor Revenue/pur is still stored as 0 with basis 'none' ...", items[0].revenue_micros == 0 and items[0].attrs["revenue_source"].startswith("none"), items[0].attrs)
    money = _validate_money(_ad([_ad_row(Device="a", **no_rev), _ad_row(Device="b")]))
    lines = ingest.money_lines(money)
    add("revenue: ... and the summary counts it: 'revenue 5,000,000.00 from the other rows (UNKNOWN for 1 rows ...)', never a bare 0.00",
        money["VND"]["revenue_unknown_rows"] == 1 and "UNKNOWN for 1 rows" in lines[0] and "5,000,000.00" in lines[0], lines)
    lines = ingest.money_lines(_validate_money(_ad([_ad_row(**no_rev)])))
    add("revenue: when EVERY row lacks a basis the line says revenue UNKNOWN and prints no revenue amount", "revenue UNKNOWN (1 rows have no revenue basis)" in lines[0] and "revenue 0.00" not in lines[0], lines)
    add("revenue: rows with a reported or derived revenue carry no unknown count", "revenue_unknown_rows" not in _validate_money(_ad([_ad_row()]))["VND"])


def _try(fn, *a):
    try:
        return fn(*a)
    except ValueError as exc:
        return f"ValueError: {exc}"


def _validate_money(connector) -> dict:
    from erp.marketing.pipeline import Pipeline
    return Pipeline(schema="perf_x").run(connector, dry_run=True).money


def _check_generic_currency_column(add) -> None:
    """Documented behaviour change (audit nit): the generic flat file now consumes a `currency` column and rejects an invalid value."""
    rows = [{"Date": "2026-09-24", "Channel": "facebook", "Campaign": "A", "Clicks": "1", "Currency": "vnd", "Spend": "5"},
            {"Date": "2026-09-24", "Channel": "facebook", "Campaign": "B", "Clicks": "1", "Currency": "dollars", "Spend": "5"}]
    items, res = _validate(_flat(rows))
    add("flat_file: a `currency` column is CONSUMED (row currency VND, not left in attrs) and an invalid value rejects the row",
        len(items) == 1 and items[0].currency == "VND" and "currency" not in {k.lower() for k in items[0].attrs} and res.rows_invalid == 1, (len(items), res.rows_invalid, items and items[0].attrs))
    doc = (ROOT / "erp" / "marketing" / "connectors" / "flat_file.py").read_text(encoding="utf-8")
    md = (ROOT / "docs" / "marketing-data-architecture.md").read_text(encoding="utf-8")
    add("flat_file: the behaviour change is stated in the connector docstring and in the docs' connector section", "BEHAVIOUR CHANGE" in doc and "consumes** a `currency`" in md)


def main() -> int:
    rows = run(False)
    for name, ok, detail in rows:
        print(f"{'ok  ' if ok else 'FAIL'} {name}" + (f"   <- {detail}" if not ok else ""))
    bad = [r for r in rows if not r[1]]
    print(f"\n{len(rows) - len(bad)}/{len(rows)} scenarios passed")
    return 1 if bad else 0


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):        # scenario names contain the dong sign: a cp1252 console must not crash the runner
        try:
            _stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
