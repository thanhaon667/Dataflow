"""The shared marketing ingestion pipeline: land -> validate/type -> dedupe -> load dimensions + fact.

One pipeline, any number of sources. A connector (erp/marketing/connectors/base.py) only maps its own raw
records to the common shape; everything below is the same for every source, so adding Facebook, Google or a
webhook later is one small connector module, not new pipeline logic.

What one run does, chunk by chunk (default 5000 records, so memory never depends on file size):

  1. LAND      every raw record is written to marketing_landing exactly as received, with the batch id and
               its position in the batch (except a row later isolated at the SQL stage: land and load share
               one transaction, so it rolls back with that row). If a mapping turns out to be wrong, the fact can be rebuilt from
               here without going back to the source.
  2. VALIDATE  each record goes through the connector's map_record() and then model.build_interaction(),
               which types every field and rejects what cannot be an interaction (including a magnitude
               bound on the numeric columns - model.MAX_COUNT / MAX_MONEY_MICROS - so a corrupted cell
               is caught here, not at the database). A rejected record is counted, its reason logged (at
               most LOG_INVALID_SAMPLES of them) and the run CONTINUES - one malformed row never ends a
               batch (lesson L-044). A record that passes validation but still fails at the database (an
               unexpected type coercion no bound anticipated) is caught by `_load_chunk_isolating`, which
               bisects the failing chunk to find and skip just that row instead of discarding the whole
               chunk - the same L-044 guarantee, one stage later. A broken database connection is the one
               exception: that propagates and ends the run, because isolating rows cannot fix a dead
               connection (see `_is_connection_error`).
  3. DEDUPE    inside the chunk by the dedupe key (exact, counted). Across chunks and across runs the
               database does it: the fact's primary key is (event_date, dedupe_key) and the insert is
               ON CONFLICT DO UPDATE, so re-importing the same export - or a restated version of it -
               refreshes those rows instead of adding a second copy. NOTHING IS OVERWRITTEN SILENTLY: two
               rows of ONE run that share an identity are REPORTED (BatchResult.collisions_*, with samples, printed
               by the command line): the later replaces the earlier ("replaced"), unless the connector gave them
               different identities on purpose because their metrics differ (a source-specific connector
               numbers rows within an identical natural key: "kept", see connectors/strict_csv.py).
  4. LOAD      the dimensions are upserted in bulk (one statement per dimension per chunk) and the fact
               rows are written with psycopg2's execute_values: one INSERT statement per page of 1000
               rows. Nothing in this pipeline ever inserts row by row in a loop.

Every run writes one row to marketing_ingest_run (started, finished, what it read, skipped, deduped and
loaded, and the first error if it failed), which is what the Data Flow page reads instead of counting
millions of fact rows.

Run: see erp/marketing/ingest.py (python -m erp.marketing.ingest).
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field

from sqlalchemy import text

from erp.config import MARKETING_BATCH_SIZE, MARKETING_MAX_ISOLATED_ROWS, MARKETING_SCHEMA
from erp.db import engine as default_engine
from erp.marketing import schema as mschema
from erp.marketing.model import InvalidRecord, build_interaction

logger = logging.getLogger(__name__)

PAGE_SIZE = 1000              # rows per INSERT statement inside a chunk (psycopg2 execute_values)
LOG_INVALID_SAMPLES = 10      # how many rejection reasons are kept for the report / the log
LOG_COLLISION_SAMPLES = 12    # how many key-collision descriptions are kept for the report
MAX_TRACKED_KEYS = 2_000_000  # the run-level collision check across chunks remembers at most this many identities
SYSTEMATIC_LEAF_STREAK = 8    # this many single-row failures with the parent's exact error and no success
                              # anywhere in that chunk's bisection = a systematic cause, not bad rows


class _RunAborted(Exception):
    """Internal: the run must stop because the SQL failure is systematic; run() turns it into status 'failed'."""


@dataclass
class BatchResult:
    """What one ingest run did. The same numbers go into marketing_ingest_run."""
    batch_id: str
    source: str
    schema: str
    rows_read: int = 0
    rows_landed: int = 0
    rows_invalid: int = 0
    rows_isolated: int = 0       # rows rejected at the SQL stage (a subset of rows_invalid); the run's failure budget
    rows_duplicate: int = 0
    rows_loaded: int = 0
    rows_inserted: int = 0
    rows_updated: int = 0
    rows_restated_changed: int = 0   # of rows_updated: those whose stored metrics or currency actually DIFFER from the incoming row
    partitions_created: list = field(default_factory=list)
    invalid_samples: list = field(default_factory=list)
    collisions_kept: int = 0          # rows whose NATURAL KEY was already seen in this run and that were kept as their own row
    collisions_replaced: int = 0      # rows whose whole identity was already seen in this run: the later one replaced the earlier
    collision_samples: list = field(default_factory=list)
    money: dict = field(default_factory=dict)      # currency -> {"spend_micros": n, "revenue_micros": n} of the rows written / to write
    chunk_keys: list = field(default_factory=list, repr=False)   # (event_date, dedupe_key, position) of ONE chunk, for the run-level check
    seen: dict = field(default_factory=dict, repr=False)         # run-level: identity -> position, only rows whose chunk committed
    tracking_stopped: bool = False
    seconds: float = 0.0
    status: str = "running"
    error: str | None = None

    def summary(self) -> str:
        return (f"{self.rows_read} read, {self.rows_landed} landed, {self.rows_invalid} invalid, "
                f"{self.rows_duplicate} duplicate in batch, {self.rows_loaded} written "
                f"({self.rows_inserted} new, {self.rows_updated} restated, {self.rows_restated_changed} of them changed) in {self.seconds:.1f}s"
                + (f"; key collisions: {self.collisions_kept} kept, {self.collisions_replaced} replaced"
                   if self.collisions_kept or self.collisions_replaced else ""))

    def note_collision(self, text: str) -> None:
        if len(self.collision_samples) < LOG_COLLISION_SAMPLES:
            self.collision_samples.append(text)


def _values(conn, sql: str, rows: list, template: str | None = None, fetch: bool = False):
    """Bulk statement through psycopg2's execute_values: ONE INSERT per page of PAGE_SIZE rows.

    Uses the DBAPI cursor of the SQLAlchemy connection we are already in, so it takes part in the same
    transaction as everything else in the chunk.
    """
    if not rows:
        return []
    from psycopg2.extras import execute_values      # local: keeps this module importable without a driver
    raw = conn.connection
    with raw.cursor() as cur:
        out = execute_values(cur, sql, rows, template=template, page_size=PAGE_SIZE, fetch=fetch)
        return list(out) if fetch else cur.rowcount


class Pipeline:
    """Loads any connector's records into the marketing star schema."""

    max_isolated = MARKETING_MAX_ISOLATED_ROWS      # class default; __init__ may override per instance

    def __init__(self, engine=None, schema: str | None = None, chunk_size: int | None = None,
                 resolve_leads: bool = True, max_isolated: int | None = None) -> None:
        self.engine = engine or default_engine
        self.schema = mschema.check_identifier(schema or MARKETING_SCHEMA)
        self.chunk_size = max(1, int(chunk_size or MARKETING_BATCH_SIZE))
        self.resolve_leads = resolve_leads
        self.max_isolated = max_isolated or MARKETING_MAX_ISOLATED_ROWS

    def t(self, table: str) -> str:
        return mschema.qualified(self.schema, table)

    # ---------------------------------------------------------------- the run
    def run(self, connector, limit: int | None = None, dry_run: bool = False) -> BatchResult:
        """Ingest everything `connector` yields. Never raises for a bad record (a value that fails
        validation or later fails at the database is isolated and counted, not raised); raises only if
        the database itself is unusable - either before any work started (wrong schema, tables not
        installed) or mid-run (the connection was lost - see `_is_connection_error`)."""
        batch_id = uuid.uuid4()
        started = time.monotonic()
        result = BatchResult(batch_id=str(batch_id), source=connector.source_key, schema=self.schema)

        if dry_run:
            self._dry_run(connector, result, limit)
            result.seconds = time.monotonic() - started
            result.status = "ok"
            return result

        with self.engine.connect() as conn:
            mschema.require_installed(conn, self.schema)
            mschema.require_currency_column(conn, self.schema)
            conn.rollback()          # end the transaction SQLAlchemy auto-began for that read
            self._open_run(conn, batch_id, connector)

            seq = 0
            try:
                for chunk in _chunks(connector.records(), self.chunk_size, limit):
                    self._load_chunk_isolating(conn, batch_id, seq, connector, chunk, result)
                    seq += len(chunk)
            except _RunAborted as stop:
                result.status = "failed"
                result.error = str(stop)
                logger.error("marketing ingest: run aborted - %s", stop)

            result.seconds = time.monotonic() - started
            if result.rows_isolated > LOG_INVALID_SAMPLES:
                logger.warning("marketing ingest: %d rows were rejected at SQL load in total (first %d logged above)",
                               result.rows_isolated, LOG_INVALID_SAMPLES)
            if result.status == "partial" and not result.rows_loaded:
                result.status = "failed"          # rows were isolated and nothing at all was written
            elif result.status == "running":
                result.status = "ok"
            self._close_run(conn, batch_id, result)
        return result

    # ------------------------------------------------------------- SQL-level row isolation
    def _load_chunk_isolating(self, conn, batch_id, seq: int, connector, chunk: list, result: BatchResult,
                               _depth: int = 0, _tree: dict | None = None, _parent_sig: tuple | None = None) -> None:
        """Load one chunk; if the bulk SQL itself fails, isolate the bad row(s) by bisection so the loss
        is that row, never the whole chunk.

        `_load_chunk`'s Python-side validation (model.build_interaction) already rejects what it can
        recognise as bad - but a value that PASSES validation can still fail at the database (an
        unexpected type coercion, a value one of the earlier bound checks did not anticipate). Before
        this method existed, ANY exception here - regardless of cause - discarded the entire chunk
        (up to MARKETING_BATCH_SIZE rows), which contradicted the row-level-isolation promise made by
        this module's own docstring, connectors/base.py and the architecture doc. Found by review: a
        single corrupted numeric cell (now also caught earlier, in model.py's MAX_COUNT/MAX_MONEY_MICROS
        bounds) discarded up to 4999 otherwise-good neighbours.

        A connection-level failure (the database itself went away) is NOT retried this way: isolating
        rows cannot fix a dead connection, and re-attempting it down to single-row chunks would just
        multiply one outage into thousands of slow, identical failures. That propagates immediately and
        ends the run (see `_is_connection_error`).

        A SYSTEMATIC failure (missing privilege, missing partition, schema mismatch) fails every row, so
        bisection would cost about 2N transactions per chunk and repeat for every chunk. Two guards abort
        the run (status 'failed', reason naming the cause) instead: a per-run budget of isolated rows
        (`max_isolated`, MARKETING_MAX_ISOLATED_ROWS), and a streak of SYSTEMATIC_LEAF_STREAK single-row
        failures carrying exactly their parent's error while nothing in the chunk has succeeded. (A
        genuinely poisoned row also repeats its parent's error, but its healthy siblings succeed, so it
        never builds that streak.) Note the row lost here also loses its landing row: land and load share
        one transaction.
        """
        tree = _tree if _tree is not None else {"ok": 0, "same": 0}
        scratch = BatchResult(batch_id=result.batch_id, source=result.source, schema=result.schema)
        try:
            with conn.begin():
                self._load_chunk(conn, batch_id, seq, connector, chunk, scratch)
        except Exception as exc:  # noqa: BLE001 - this attempt's SQL failed; the transaction rolled back
            if _is_connection_error(exc):
                raise
            sig = (type(exc).__name__, (str(exc).splitlines() or [""])[0][:300])
            if len(chunk) == 1:
                message = f"{sig[0]}: {sig[1]}"
                result.rows_read += 1
                result.rows_invalid += 1
                result.rows_isolated += 1
                result.status = "partial"       # run() turns this into 'failed' if nothing at all was loaded
                result.error = result.error or message
                self._note_invalid(result, seq, f"rejected at load (passed validation, failed at the database): {message}")
                if result.rows_isolated <= LOG_INVALID_SAMPLES:
                    logger.warning("marketing ingest: record #%d rejected at SQL load (%s)", seq, message)
                elif result.rows_isolated == LOG_INVALID_SAMPLES + 1:
                    logger.warning("marketing ingest: further SQL-load rejections are counted but not logged one by one")
                if sig == _parent_sig and not tree["ok"]:
                    tree["same"] += 1
                if result.rows_isolated > self.max_isolated:
                    raise _RunAborted(
                        f"aborted: more than {self.max_isolated} rows were rejected at the SQL stage "
                        f"(MARKETING_MAX_ISOLATED_ROWS) - this looks like a systematic cause, not bad rows. "
                        f"Last error: {message}")
                if tree["same"] >= SYSTEMATIC_LEAF_STREAK:
                    raise _RunAborted(
                        f"aborted: {tree['same']} single rows in a row failed with the same error as their whole chunk "
                        f"and none succeeded - a systematic cause (missing privilege, missing partition, schema "
                        f"mismatch), not bad rows. Error: {message}")
                return
            mid = len(chunk) // 2
            logger.info("marketing ingest: chunk at offset %d failed (%s) - isolating within %d records",
                        seq, sig[0], len(chunk))
            self._load_chunk_isolating(conn, batch_id, seq, connector, chunk[:mid], result, _depth + 1, tree, sig)
            self._load_chunk_isolating(conn, batch_id, seq + mid, connector, chunk[mid:], result, _depth + 1, tree, sig)
            return
        tree["ok"] += 1
        _merge_result(result, scratch)

    # ------------------------------------------------------------- the stages
    def _dry_run(self, connector, result: BatchResult, limit: int | None) -> None:
        """Map and validate only: no connection is opened and nothing is written anywhere."""
        seen: dict = {}
        base = 0
        for chunk in _chunks(connector.records(), self.chunk_size, limit):
            result.rows_read += len(chunk)
            for seq, item in self._validate(connector, chunk, base, result):
                key = (item.event_date, item.dedupe_key)
                if key in seen:
                    result.rows_duplicate += 1
                    result.collisions_replaced += 1
                    result.note_collision(_replaced_text(seq, seen[key], item))
                else:
                    _count_money(result.money, item)
                seen[key] = seq
            base += len(chunk)

    def _validate(self, connector, chunk: list, base: int, result: BatchResult) -> list:
        """Raw records -> [(position in the batch, Interaction)].

        A record that cannot become an interaction is counted and skipped, never raised (lesson L-044).
        The position is kept so a fact row can point back at the landing row it came from.
        """
        out = []
        for offset, raw in enumerate(chunk):
            seq = base + offset
            try:
                mapped = connector.map_record(raw)
            except InvalidRecord as exc:                 # a strict connector rejecting a row by name: the reason is the message
                self._note_invalid(result, seq, str(exc))
                result.rows_invalid += 1
                continue
            except Exception as exc:  # noqa: BLE001 - a connector bug on ONE record is that record's problem
                self._note_invalid(result, seq, f"{type(exc).__name__}: {exc}")
                result.rows_invalid += 1
                continue
            if mapped is None:
                result.rows_invalid += 1
                self._note_invalid(result, seq, "the connector did not recognise it as an interaction")
                continue
            try:
                item = build_interaction(mapped)
            except InvalidRecord as exc:
                result.rows_invalid += 1
                self._note_invalid(result, seq, str(exc))
                continue
            if item.occurrence > 0:
                # a second (third ...) row under one natural key: the connector gave it its own identity so it is KEPT,
                # and the run says so - the export holds rows that a plain dedupe would have merged
                result.collisions_kept += 1
                result.note_collision(f"record #{seq}{_line_of(raw)}: natural key {item.natural_key!r} was already seen "
                                      f"{item.occurrence}x in this file - kept as its own row (occurrence {item.occurrence}), "
                                      f"different metrics are not merged")
            out.append((seq, item))
        return out

    @staticmethod
    def _note_invalid(result: BatchResult, offset: int, why: str) -> None:
        if len(result.invalid_samples) < LOG_INVALID_SAMPLES:
            result.invalid_samples.append(f"record #{offset}: {why[:160]}")

    def _load_chunk(self, conn, batch_id, seq: int, connector, chunk: list, result: BatchResult) -> None:
        result.rows_read += len(chunk)

        # 1 - land the raw records, unchanged
        landing = [(batch_id, seq + i, connector.source_key,
                    _landing_external_id(raw), _landing_event_type(raw), json.dumps(raw, ensure_ascii=False, default=str))
                   for i, raw in enumerate(chunk)]
        landed = _values(conn, f"INSERT INTO {self.t(mschema.LANDING_TABLE)} "
                               "(batch_id, batch_seq, source, external_id, event_type, payload) VALUES %s "
                               "RETURNING landing_id, batch_seq",
                         landing, template="(%s, %s, %s, %s, %s, %s::jsonb)", fetch=True)
        result.rows_landed += len(landed)
        landing_by_seq = {int(r[1]): int(r[0]) for r in landed}

        # 2 - validate / type
        items = self._validate(connector, chunk, seq, result)
        if not items:
            return

        # 3 - dedupe inside the chunk (the database deduplicates across chunks and runs)
        unique: dict = {}
        for position, item in items:
            key = (item.event_date, item.dedupe_key)
            if key in unique:
                result.rows_duplicate += 1
                result.collisions_replaced += 1
                result.note_collision(_replaced_text(position, unique[key][1], item))
            unique[key] = (item, position)
        items = list(unique.values())
        for i, _pos in items:
            _count_money(result.money, i)
        result.chunk_keys = [(i.event_date, i.dedupe_key, pos) for i, pos in items]

        # 4 - dimensions, partitions, fact
        created = mschema.ensure_partitions(conn, self.schema, [i.event_date for i, _ in items])
        result.partitions_created.extend(created)
        ids = self._resolve_dimensions(conn, connector.source_key, [i for i, _ in items])
        self._load_fact(conn, items, ids, landing_by_seq, result)

    # ---------------------------------------------------------- the dimensions
    def _resolve_dimensions(self, conn, source_key: str, items: list) -> dict:
        """Upsert every dimension value this chunk mentions and return the key -> id maps."""
        source_id = self._upsert_source(conn, source_key)

        channels = {i.channel_key: (i.channel_key.replace("_", " ").title(), i.channel_medium) for i in items}
        channel_ids = self._upsert(
            conn, "marketing_channel", "(%s, %s, %s)",
            [(k, name, medium) for k, (name, medium) in channels.items()],
            columns="(channel_key, display_name, medium)", conflict="(channel_key)",
            update="display_name = COALESCE(marketing_channel.display_name, EXCLUDED.display_name), "
                   "medium = COALESCE(marketing_channel.medium, EXCLUDED.medium), last_seen_at = now()",
            returning="channel_id, channel_key", key_index=1)

        campaigns = {}
        for i in items:
            if i.campaign_key:
                campaigns[(channel_ids[i.channel_key], i.campaign_key)] = i.campaign_name
        campaign_ids = self._upsert(
            conn, "marketing_campaign", "(%s, %s, %s)",
            [(ch, key, name) for (ch, key), name in campaigns.items()],
            columns="(channel_id, campaign_key, name)", conflict="(channel_id, campaign_key)",
            update="name = COALESCE(marketing_campaign.name, EXCLUDED.name), last_seen_at = now()",
            returning="campaign_id, channel_id, campaign_key", key_index=(1, 2))

        creatives = {}
        for i in items:
            if i.creative_key and i.campaign_key:
                cid = campaign_ids[(channel_ids[i.channel_key], i.campaign_key)]
                creatives[(cid, i.creative_key)] = (i.creative_name, i.creative_format)
        creative_ids = self._upsert(
            conn, "marketing_creative", "(%s, %s, %s, %s)",
            [(c, key, name, fmt) for (c, key), (name, fmt) in creatives.items()],
            columns="(campaign_id, creative_key, name, format)", conflict="(campaign_id, creative_key)",
            update="name = COALESCE(marketing_creative.name, EXCLUDED.name), "
                   "format = COALESCE(marketing_creative.format, EXCLUDED.format), last_seen_at = now()",
            returning="creative_id, campaign_id, creative_key", key_index=(1, 2))

        identities = {(i.identity_type, i.identity_value) for i in items if i.identity_value}
        identity_ids = self._upsert(
            conn, "marketing_identity", "(%s, %s)", sorted(identities),
            columns="(identity_type, identity_value)", conflict="(identity_type, identity_value)",
            update="last_seen_at = now()", returning="identity_id, identity_type, identity_value", key_index=(1, 2))
        if identity_ids and self.resolve_leads:
            self._resolve_leads(conn, list(identity_ids.values()))

        return {"source_id": source_id, "channel": channel_ids, "campaign": campaign_ids,
                "creative": creative_ids, "identity": identity_ids}

    def _upsert(self, conn, table: str, template: str, rows: list, columns: str, conflict: str,
                update: str, returning: str, key_index) -> dict:
        """One bulk INSERT ... ON CONFLICT DO UPDATE ... RETURNING per dimension per chunk -> {key: id}."""
        if not rows:
            return {}
        sql = (f"INSERT INTO {self.t(table)} {columns} VALUES %s "
               f"ON CONFLICT {conflict} DO UPDATE SET {update} RETURNING {returning}")
        out = _values(conn, sql, rows, template=template, fetch=True)
        idx = key_index if isinstance(key_index, tuple) else (key_index,)
        return {(tuple(r[i] for i in idx) if len(idx) > 1 else r[idx[0]]): r[0] for r in out}

    def _upsert_source(self, conn, source_key: str) -> int:
        return conn.execute(text(
            f"INSERT INTO {self.t('marketing_source')} (source_key, display_name) VALUES (:k, :n) "
            "ON CONFLICT (source_key) DO UPDATE SET last_seen_at = now() RETURNING source_id"
        ), {"k": source_key, "n": source_key.replace("_", " ").title()}).scalar_one()

    def _resolve_leads(self, conn, identity_ids: list) -> int:
        """Point the identities of this chunk at an existing lead where one matches.

        The marketing tables store a sha256 of an e-mail / phone, never the value, so the match is done
        against the same hash computed from the `leads` row. This is a scan of `leads` per chunk, which is
        right at this project's lead volume; a functional index on leads would be the Phase 2 answer.
        """
        return conn.execute(text(f"""
            UPDATE {self.t('marketing_identity')} mi
               SET lead_id = l.id, resolved_at = now()
              FROM leads l
             WHERE mi.identity_id = ANY(CAST(:ids AS bigint[]))
               AND mi.lead_id IS NULL
               AND ((mi.identity_type = 'email_sha256' AND l.email IS NOT NULL
                     AND encode(sha256(CAST(lower(btrim(l.email)) AS bytea)), 'hex') = mi.identity_value)
                 OR (mi.identity_type = 'phone_sha256' AND l.phone IS NOT NULL
                     AND encode(sha256(CAST(regexp_replace(l.phone, '[^0-9+]', '', 'g') AS bytea)), 'hex') = mi.identity_value))
        """), {"ids": [int(i) for i in identity_ids]}).rowcount

    # ---------------------------------------------------------------- the fact
    def _load_fact(self, conn, items: list, ids: dict, landing_by_seq: dict, result: BatchResult) -> None:
        rows = []
        keys: list[tuple] = []
        metrics: dict[tuple, tuple] = {}
        for item, seq in items:
            channel_id = ids["channel"][item.channel_key]
            campaign_id = ids["campaign"].get((channel_id, item.campaign_key)) if item.campaign_key else None
            creative_id = (ids["creative"].get((campaign_id, item.creative_key))
                           if campaign_id and item.creative_key else None)
            identity_id = ids["identity"].get((item.identity_type, item.identity_value)) if item.identity_value else None
            rows.append((
                item.event_date, str(item.dedupe_key), item.event_at, ids["source_id"], channel_id,
                campaign_id, creative_id, identity_id, item.event_type,
                item.impressions, item.clicks, item.reactions, item.sessions, item.conversions,
                item.spend_micros, item.revenue_micros, landing_by_seq.get(seq),
                json.dumps(item.attrs, ensure_ascii=False), json.dumps(item.attrs_idx, ensure_ascii=False),
                item.currency,
            ))
            keys.append((item.event_date, str(item.dedupe_key)))
            metrics[keys[-1]] = _metrics(item.impressions, item.clicks, item.reactions, item.sessions, item.conversions,
                                         item.spend_micros, item.revenue_micros, item.currency)
        if not rows:
            return

        # Which of this chunk's keys are already in the fact table - a bulk lookup (never row by row) done BEFORE
        # the upsert, so "N new, M restated" can still be reported afterwards. This replaces an earlier
        # `RETURNING (xmax = 0) AS inserted` (works on a plain table) that PostgreSQL refuses on `interaction_fact`
        # because it is PARTITIONED: "cannot retrieve a system column in this context" - the parent relation has
        # no storage of its own, so xmax cannot be read from it, only from the child partition the tuple actually
        # lives in. Found by actually running this against the real partitioned schema (erp/marketing/perf_check.py
        # section 4) - the pure-Python scenario table never exercises real SQL, so this had never been caught.
        existing = self._existing_fact_keys(conn, keys)

        sql = (f"INSERT INTO {self.t(mschema.FACT_TABLE)} "
               "(event_date, dedupe_key, event_at, source_id, channel_id, campaign_id, creative_id, identity_id, "
               " event_type, impressions, clicks, reactions, sessions, conversions, spend_micros, revenue_micros, "
               " landing_id, attrs, attrs_idx, currency) VALUES %s "
               "ON CONFLICT (event_date, dedupe_key) DO UPDATE SET "
               "  event_at = EXCLUDED.event_at, campaign_id = EXCLUDED.campaign_id, creative_id = EXCLUDED.creative_id, "
               "  identity_id = EXCLUDED.identity_id, event_type = EXCLUDED.event_type, "
               "  impressions = EXCLUDED.impressions, clicks = EXCLUDED.clicks, reactions = EXCLUDED.reactions, "
               "  sessions = EXCLUDED.sessions, conversions = EXCLUDED.conversions, "
               "  spend_micros = EXCLUDED.spend_micros, revenue_micros = EXCLUDED.revenue_micros, "
               "  landing_id = EXCLUDED.landing_id, attrs = EXCLUDED.attrs, attrs_idx = EXCLUDED.attrs_idx, "
               "  currency = EXCLUDED.currency, loaded_at = now()")
        template = ("(%s, %s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)")
        _values(conn, sql, rows, template=template, fetch=False)
        new = sum(1 for k in keys if k not in existing)
        result.rows_loaded += len(rows)
        result.rows_inserted += new
        result.rows_updated += len(rows) - new
        # Free: `existing` already carries the stored metrics (same bulk query), so "restated" can be split into
        # identical re-imports and rows whose numbers really moved, without reading the data a second time.
        result.rows_restated_changed += sum(1 for k, stored in existing.items() if k in metrics and stored != metrics[k])

    def _existing_fact_keys(self, conn, keys: list[tuple]) -> dict[tuple, tuple]:
        """The (event_date, dedupe_key) pairs of this chunk already in the fact table -> their stored metrics, as one bulk query."""
        if not keys:
            return {}
        rows = _values(
            conn,
            f"SELECT v.event_date, v.dedupe_key, t.impressions, t.clicks, t.reactions, t.sessions, t.conversions, "
            f"t.spend_micros, t.revenue_micros, t.currency "
            f"FROM (VALUES %s) AS v(event_date, dedupe_key) "
            f"JOIN {self.t(mschema.FACT_TABLE)} t ON t.event_date = v.event_date AND t.dedupe_key = v.dedupe_key",
            keys, template="(%s::date, %s::uuid)", fetch=True,
        )
        return {(r[0], str(r[1])): _metrics(*r[2:]) for r in rows}

    # -------------------------------------------------------- the run journal
    def _open_run(self, conn, batch_id, connector) -> None:
        with conn.begin():
            conn.execute(text(
                f"INSERT INTO {self.t(mschema.RUN_TABLE)} (batch_id, source, connector, origin) "
                "VALUES (:b, :s, :c, :o)"
            ), {"b": batch_id, "s": connector.source_key, "c": connector.name, "o": (connector.origin() or "")[:500]})

    def _close_run(self, conn, batch_id, result: BatchResult) -> None:
        with conn.begin():
            conn.execute(text(
                f"UPDATE {self.t(mschema.RUN_TABLE)} SET finished_at = now(), rows_read = :read, rows_landed = :landed, "
                "rows_invalid = :invalid, rows_duplicate = :dup, rows_loaded = :loaded, status = :status, "
                "error_message = :err WHERE batch_id = :b"
            ), {"b": batch_id, "read": result.rows_read, "landed": result.rows_landed, "invalid": result.rows_invalid,
                "dup": result.rows_duplicate, "loaded": result.rows_loaded, "status": result.status,
                "err": result.error})


def _merge_result(dst: BatchResult, src: BatchResult) -> None:
    """Fold a successful isolation attempt's scratch counters into the real result. Called only when
    `src`'s transaction actually committed - a failed attempt's scratch is discarded by its caller
    instead, so a row's counts are never attributed twice (once as part of a failed larger attempt,
    once again when a smaller retry isolates it)."""
    dst.rows_read += src.rows_read
    dst.rows_landed += src.rows_landed
    dst.rows_invalid += src.rows_invalid
    dst.rows_duplicate += src.rows_duplicate
    dst.rows_loaded += src.rows_loaded
    dst.rows_inserted += src.rows_inserted
    dst.rows_updated += src.rows_updated
    dst.rows_restated_changed += src.rows_restated_changed
    dst.partitions_created.extend(src.partitions_created)
    for event_date, key, position in src.chunk_keys:
        ident = (event_date, key)
        if ident in dst.seen:                       # the same identity in an EARLIER chunk of this run: the database will call it
            dst.rows_duplicate += 1                 # 'restated', but inside one run it is a collision and must be said
            dst.collisions_replaced += 1
            dst.note_collision(f"record #{position} has the same identity as record #{dst.seen[ident]} in an earlier part of "
                               f"this run ({event_date.isoformat()}) - the later one REPLACED the earlier")
        elif len(dst.seen) < MAX_TRACKED_KEYS:
            dst.seen[ident] = position
        elif not dst.tracking_stopped:
            dst.tracking_stopped = True
            dst.note_collision(f"(run-level collision tracking stopped after {MAX_TRACKED_KEYS:,} identities: a repeat beyond "
                               f"that shows up only as 'restated')")
    src.chunk_keys = []
    dst.collisions_kept += src.collisions_kept
    dst.collisions_replaced += src.collisions_replaced
    for cur, m in src.money.items():
        into = dst.money.setdefault(cur, {"spend_micros": 0, "revenue_micros": 0})
        for k, v in m.items():
            into[k] = into.get(k, 0) + v
    for sample in src.invalid_samples:
        if len(dst.invalid_samples) >= LOG_INVALID_SAMPLES:
            break
        dst.invalid_samples.append(sample)
    for sample in src.collision_samples:
        dst.note_collision(sample)


def _is_connection_error(exc: Exception) -> bool:
    """True for a broken database/connection - isolating rows by bisection cannot fix that, so the run
    should fail loudly and stop instead of re-attempting every remaining row down to single-row chunks.
    False for a bad VALUE in one row (a numeric overflow, an unexpected type), which bisection CAN
    isolate and skip. psycopg2.OperationalError/InterfaceError (raw, or wrapped in a SQLAlchemy DBAPIError
    that is invalidated or whose .orig is one of them) cover a dropped connection, a server
    restart, a timeout; IntegrityError/DataError (constraint violations, 'integer out of range') are
    per-row problems and fall through to isolation."""
    try:
        import psycopg2  # local: keeps this module importable without a driver, matches _values()
    except ImportError:
        return False
    conn_types = (psycopg2.OperationalError, psycopg2.InterfaceError)
    if isinstance(exc, conn_types):
        return True
    # Calls made through conn.execute(text(...)) (_upsert_source, _resolve_leads, the schema.py partition
    # helpers) raise SQLAlchemy-WRAPPED errors: unwrap them, or a dead connection would be bisected.
    from sqlalchemy.exc import DBAPIError
    if isinstance(exc, DBAPIError):
        return bool(exc.connection_invalidated) or isinstance(exc.orig, conn_types)
    return False


def _line_of(raw) -> str:
    line = getattr(raw, "line", 0)
    return f" (file line {line})" if line else ""


# A connector whose row has no revenue at all writes attrs.revenue_source starting with this word (ad_performance does).
NO_REVENUE_BASIS = "none"


def _metrics(*values) -> tuple:
    """The stored measures of a fact row in one comparable shape (None counts as 0; the currency is text)."""
    *numbers, currency = values
    return (*(int(v or 0) for v in numbers), (currency or "").strip())


def _replaced_text(position: int, earlier: int, item) -> str:
    return (f"record #{position} has the same identity as record #{earlier} in this run - the later one REPLACED the earlier "
            f"({item.event_date.isoformat()}, {item.channel_key}"
            + (f", campaign {item.campaign_key}" if item.campaign_key else "") + "). If they are different rows, the "
            "connector must give them different identities")


def _count_money(into: dict, item) -> None:
    m = into.setdefault(item.currency, {"spend_micros": 0, "revenue_micros": 0})
    m["spend_micros"] += item.spend_micros
    m["revenue_micros"] += item.revenue_micros
    if str(item.attrs.get("revenue_source", "")).startswith(NO_REVENUE_BASIS):
        m["revenue_unknown_rows"] = m.get("revenue_unknown_rows", 0) + 1     # 0 stored, but nothing was reported: UNKNOWN, not zero


def _chunks(iterable, size: int, limit: int | None):
    """Yield lists of at most `size` records, stopping after `limit` records in total."""
    chunk: list = []
    taken = 0
    for item in iterable:
        if limit is not None and taken >= limit:
            break
        chunk.append(item)
        taken += 1
        if len(chunk) >= size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def _landing_external_id(raw) -> str | None:
    if not isinstance(raw, dict):
        return None
    for key in ("external_id", "event_id", "id", "record_id", "row_id"):
        for candidate in (key, key.upper(), key.title()):
            value = raw.get(candidate)
            if value is not None and str(value).strip():
                return str(value).strip()[:200]
    return None


def _landing_event_type(raw) -> str | None:
    if not isinstance(raw, dict):
        return None
    for key in ("event_type", "event", "action", "action_type"):
        for candidate in (key, key.upper(), key.title()):
            value = raw.get(candidate)
            if value is not None and str(value).strip():
                return str(value).strip()[:80]
    return None
