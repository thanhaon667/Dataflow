"""Marketing INBOX PROCESSOR: drop CSV exports in a folder, run one command, get them loaded and rolled up.

A one-shot job. For every *.csv in the inbox (default data_inbox/incoming, MARKETING_INBOX in .env or --inbox):

  1. skips a file that is still being written (modified in the last few seconds) and anything that is not a .csv;
  2. DETECTS the connector from the header row with the connectors' own strict header checks: ad_performance,
     email_campaign, then placement_performance (each header is a different exact column set, so a file matches at most one
     and no connector can take another's file). A placement file may carry the currency in its name
     (`<channel>__<currency>__whatever.csv`) for a Cost column written without one. The generic flat_file connector is used ONLY when the file carries a channel hint - a filename like
     `<channel>__<currency>__whatever.csv` (facebook__usd__march.csv) or a `channel` column. Otherwise nothing is guessed:
     the file goes to failed/ with a `<name>.reason.txt` saying exactly what was missing;
  3. loads it through the EXISTING pipeline (erp/marketing/pipeline.py via ingest.build_connector) - no parsing or
     validation is repeated here - and honours its status (ok / partial / failed) and failure budget;
  4. moves it to processed/ (ok, partial, or an exact duplicate) or failed/, with a timestamp prefix so a same-named file
     never overwrites another. The original bytes are only ever renamed, never rewritten or deleted;
  5. after all files, if at least one loaded rows, runs the incremental rollup refresh (erp.marketing.rollup) ONCE;
  6. prints and logs a summary table and exits 1 only if something failed.

Idempotence: the SHA-256 of every file that loaded is kept in `<inbox>/.autorun_index.json` (per schema). The same bytes
dropped again are moved to processed/ as a duplicate and NOT loaded again. The marketing_ingest_run journal has no hash
column and adding one would be a migration, so the small index file is used (delete it to force a re-load; the load itself
is an idempotent upsert anyway). The same file also remembers the last real run, which the Data Flow page shows.

--dry-run detects, validates and reports, moves nothing, writes nothing to the database or the inbox.
processed/ and failed/ are created next to the inbox folder (siblings of it), on demand.

Nothing schedules this job; Task Scheduler is the owner's decision (see run_marketing_autorun.bat).

Run:
  venv\\Scripts\\python.exe -m erp.marketing.autorun --dry-run
  venv\\Scripts\\python.exe -m erp.marketing.autorun
  venv\\Scripts\\python.exe -m erp.marketing.autorun --inbox D:\\exports\\marketing --schema public
Log: data_inbox\\marketing_autorun.log (git-ignored). Exit code: 0 nothing failed, 1 a file, the database or the rollup failed,
2 a bad argument.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import logging
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

logger = logging.getLogger("erp.marketing.autorun")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LOG_FILE = PROJECT_ROOT / "data_inbox" / "marketing_autorun.log"    # data_inbox/ is git-ignored (the smoke stray-file gate skips it)
INDEX_NAME = ".autorun_index.json"
LOCK_NAME = ".autorun.lock"
INDEX_VERSION = 1
GRACE_SECONDS = 5           # a file modified more recently than this may still be being written
LOCK_STALE_SECONDS = 2 * 3600
SNIFF_CHARS = 65536

# `<channel>__<currency>__whatever.csv`: the channel and the ISO currency of a generic export that names neither itself
FILENAME_HINT = re.compile(r"^(?P<channel>[A-Za-z0-9][\w\- ]*?)__(?P<currency>[A-Za-z]{3})__(?P<rest>.*)\.csv$", re.IGNORECASE)

FAILURE_STATUSES = ("failed", "deferred")


class DatabaseUnavailable(Exception):
    """The database (or the marketing tables) cannot be used: every remaining file would fail the same way, so the run
    stops and leaves the files in the inbox to be retried."""


# --------------------------------------------------------------------------- results
@dataclass
class FileOutcome:
    name: str
    connector: str = "-"
    rows_read: int | None = None
    rows_loaded: int | None = None
    rows_rejected: int | None = None
    status: str = "failed"          # ok | partial | failed | duplicate | skipped | deferred
    destination: str = ""
    detail: str = ""
    sha256: str = ""


@dataclass
class RunReport:
    inbox: str
    schema: str
    dry_run: bool
    outcomes: list = field(default_factory=list)
    rollup: str = "not needed"      # not needed | ok | failed | skipped (dry run)
    rollup_detail: str = ""
    problem: str = ""               # something that stopped the whole run (lock held, database down)
    ignored_non_csv: int = 0

    def failed(self) -> bool:
        return bool(self.problem) or self.rollup == "failed" or any(o.status in FAILURE_STATUSES for o in self.outcomes)

    def exit_code(self) -> int:
        return 1 if self.failed() else 0

    def count(self, *statuses: str) -> int:
        return sum(1 for o in self.outcomes if o.status in statuses)

    def rows_loaded(self) -> int:
        """Rows written by this run. Counts a file that failed AFTER some chunks committed too: the rollup must see them."""
        return sum(o.rows_loaded or 0 for o in self.outcomes)


# --------------------------------------------------------------------------- small helpers
def resolve_inbox(value: str | os.PathLike | None = None) -> Path:
    """--inbox, else MARKETING_INBOX (erp/config.py). A relative path is taken from the project root, not the cwd."""
    if value is None:
        from erp import config
        value = config.MARKETING_INBOX
    p = Path(value)
    return p if p.is_absolute() else PROJECT_ROOT / p


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _one_line(text: object, limit: int = 300) -> str:
    s = " ".join(str(text).split())
    return s if len(s) <= limit else s[: limit - 1] + "..."


# --------------------------------------------------------------------------- the index (hashes + last run)
class Index:
    """`<inbox>/.autorun_index.json`: {"version": 1, "schemas": {schema: {sha256: {...}}}, "last_run": {...}}."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict = {"version": INDEX_VERSION, "schemas": {}, "last_run": None}
        self.warning = ""
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("schemas"), dict):
                self.data.update({"schemas": raw["schemas"], "last_run": raw.get("last_run")})
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            self.warning = f"{path.name} is unreadable ({type(exc).__name__}); treating it as empty"
            logger.warning("%s", self.warning)

    def known(self, schema: str, sha: str) -> dict | None:
        entry = self.data["schemas"].get(schema, {}).get(sha)
        return entry if isinstance(entry, dict) else None

    def remember(self, schema: str, sha: str, name: str, connector: str, rows_loaded: int) -> None:
        self.data["schemas"].setdefault(schema, {})[sha] = {
            "file": name, "connector": connector, "rows_loaded": rows_loaded, "at": _now_iso()}
        self.save()

    def set_last_run(self, last: dict) -> None:
        self.data["last_run"] = last
        self.save()

    def save(self) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)


def read_last_run(inbox: str | os.PathLike | None = None) -> dict | None:
    """The last real (non dry-run) run recorded in the inbox's index, or None. Never raises: the Data Flow page calls it."""
    try:
        raw = json.loads((resolve_inbox(inbox) / INDEX_NAME).read_text(encoding="utf-8"))
        last = raw.get("last_run") if isinstance(raw, dict) else None
        return last if isinstance(last, dict) else None
    except Exception:  # noqa: BLE001 - a missing or damaged index is "never run", not an error
        return None


class Lock:
    """One run at a time per inbox: a lock file made with O_EXCL. A lock older than LOCK_STALE_SECONDS is a dead run's."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.held = False

    def acquire(self) -> bool:
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                except OSError:
                    continue
                if age < LOCK_STALE_SECONDS:
                    return False
                logger.warning("removing a stale lock (%d minutes old): %s", age // 60, self.path.name)
                try:
                    self.path.unlink()
                except OSError:
                    return False
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(f"pid {os.getpid()} started {_now_iso()}\n")
            self.held = True
            return True
        return False

    def release(self) -> None:
        if self.held:
            try:
                self.path.unlink()
            except OSError:
                pass
            self.held = False


# --------------------------------------------------------------------------- detection
@dataclass
class Detection:
    connector: str | None           # ad_performance | email_campaign | placement_performance | flat_file | None (= refused)
    channel: str | None = None      # flat_file / placement_performance: default channel from the filename
    currency: str | None = None     # flat_file / placement_performance: currency from the filename
    how: str = ""                   # why this connector
    reason: str = ""                # when connector is None: exactly what was missing


def _sniff_header(path: Path) -> list[str]:
    with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as fh:
        sample = fh.read(SNIFF_CHARS)
    try:
        delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        delimiter = ","
    try:
        return next(csv.reader(io.StringIO(sample), delimiter=delimiter))
    except StopIteration:
        return []


def filename_hint(name: str) -> tuple[str, str] | None:
    """`facebook__usd__march.csv` -> ('facebook', 'USD'); None when the name does not follow the convention."""
    from erp.marketing.model import slug, to_currency
    m = FILENAME_HINT.match(name)
    if not m:
        return None
    channel, currency = slug(m.group("channel"), 80), to_currency(m.group("currency"))
    return (channel, currency) if channel and currency else None


def detect(path: Path) -> Detection:
    """Which connector reads this file. Refuses (connector None, reason set) rather than guess."""
    from erp.marketing import parse
    from erp.marketing.connectors.ad_performance import AdPerformanceConnector
    from erp.marketing.connectors.base import ConnectorError
    from erp.marketing.connectors.email_campaign import EmailCampaignConnector
    from erp.marketing.connectors.placement_performance import PlacementPerformanceConnector
    from erp.marketing.model import slug

    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            head = fh.read(8192)
    except OSError as exc:
        return Detection(None, reason=f"the file cannot be read: {exc.strerror or type(exc).__name__}")
    if size == 0:
        return Detection(None, reason="the file is empty (0 bytes): there is no header row to detect a connector from")
    if b"\x00" in head:
        return Detection(None, reason="the file looks binary (it contains NUL bytes): it is not a text CSV export")
    try:
        header = parse.read_header(path, ",")
    except UnicodeDecodeError as exc:
        return Detection(None, reason=f"the file is not valid UTF-8 text ({exc.reason} at byte {exc.start}): save the export as CSV UTF-8")
    except ValueError:
        return Detection(None, reason="the file has no header row (it is blank)")
    except OSError as exc:
        return Detection(None, reason=f"the file cannot be read: {exc.strerror or type(exc).__name__}")

    refusals = []
    for key, cls in (("ad_performance", AdPerformanceConnector), ("email_campaign", EmailCampaignConnector),
                     ("placement_performance", PlacementPerformanceConnector)):
        try:
            cls.check_header(header)
            if key == "placement_performance":
                named = filename_hint(path.name)
                return Detection(key, channel=named[0] if named else None, currency=named[1] if named else None,
                                 how="header matches the exact placement_performance export"
                                     + (f" (filename says channel '{named[0]}', currency {named[1]})" if named else ""))
            return Detection(key, how="header matches the exact " + key + " export")
        except ConnectorError as exc:
            refusals.append(f"{key}: {exc}")

    hint = filename_hint(path.name)
    try:
        flat_header = _sniff_header(path)
    except OSError:
        flat_header = header
    has_channel_column = any(slug(h, 120) == "channel" for h in flat_header)
    if hint or has_channel_column:
        parts = []
        if hint:
            parts.append(f"filename says channel '{hint[0]}', currency {hint[1]}")
        if has_channel_column:
            parts.append("the header has a channel column")
        return Detection("flat_file", channel=hint[0] if hint else None, currency=hint[1] if hint else None,
                         how="generic flat_file: " + " and ".join(parts))

    lines = ["no connector matched and the file carries no channel hint, so nothing was guessed.",
             "Detected header: " + ", ".join(repr(h) for h in header[:30]) + (" ..." if len(header) > 30 else ""),
             "Strict connectors refused it:"]
    lines += [f"  - {r}" for r in refusals]
    lines += ["The generic flat_file connector needs a channel hint and there is none:",
              "  - the filename does not follow <channel>__<currency>__whatever.csv (for example facebook__usd__march.csv)",
              "  - the header has no 'channel' column",
              "To load it: rename the file to <channel>__<currency>__<anything>.csv, or add a 'channel' column, then drop it in the inbox again."]
    return Detection(None, reason="\n".join(lines))


def build_connector(det: Detection, path: Path, date_format: str = "auto"):
    """The connector object, built by the existing ingest.build_connector (so flags and refusals behave exactly as on the command line)."""
    from erp.marketing import ingest
    args = SimpleNamespace(connector=det.connector, channel=det.channel, channel_default=None, currency=det.currency,
                           source=None, date_format=date_format, delimiter=None)
    return ingest.build_connector(args, path)


def _release(connector) -> None:
    """Close the connector's lazy CSV generators: on Windows an open file handle would stop the move."""
    rows = getattr(connector, "_rows", None)
    close = getattr(rows, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- moving files
def _unique_dest(folder: Path, name: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = folder / f"{stamp}_{name}"
    n = 1
    while dest.exists() or dest.with_name(dest.name + ".reason.txt").exists():
        n += 1
        dest = folder / f"{stamp}_{n}_{name}"
    return dest


def _move(path: Path, folder: Path) -> Path:
    """Rename (never rewrite) `path` into `folder` under a timestamp-prefixed name that does not exist yet."""
    folder.mkdir(parents=True, exist_ok=True)
    dest = _unique_dest(folder, path.name)
    shutil.move(str(path), str(dest))          # a rename on the same drive; bytes are never rewritten
    return dest


def _write_reason(dest: Path, out: FileOutcome, reason: str) -> None:
    sidecar = dest.with_name(dest.name + ".reason.txt")
    body = [f"File:       {out.name}", f"Time:       {_now_iso()}", f"SHA-256:    {out.sha256}",
            f"Connector:  {out.connector}", "", "Reason:", reason.rstrip(), ""]
    sidecar.write_text("\n".join(body), encoding="utf-8")


# --------------------------------------------------------------------------- one file
@dataclass
class _Ctx:
    schema: str
    dry_run: bool
    index: Index | None
    pipeline: object | None
    processed: Path
    failed: Path
    date_format: str
    grace: float
    now: float
    seen_in_run: set = field(default_factory=set)


def _finish(ctx: _Ctx, path: Path, out: FileOutcome, folder: Path, label: str, reason: str = "") -> FileOutcome:
    """Route a file: report where it would go (dry run) or move it, with the reason sidecar for a failure."""
    if ctx.dry_run:
        out.destination = f"{label}/ (dry run: not moved)"
        return out
    try:
        dest = _move(path, folder)
        if reason:
            _write_reason(dest, out, reason)
        out.destination = f"{label}/{dest.name}"
    except OSError as exc:
        out.status = "failed" if out.status in ("ok", "partial", "duplicate") else out.status
        out.destination = "(still in the inbox)"
        out.detail = _one_line(f"{out.detail} | could not move the file: {exc.strerror or type(exc).__name__}")
        logger.error("%s: could not move the file to %s/: %s", out.name, label, exc)
    return out


def _fail(ctx: _Ctx, path: Path, out: FileOutcome, reason: str) -> FileOutcome:
    out.status = "failed"
    out.detail = _one_line(reason.splitlines()[0] if reason.strip() else "failed")
    logger.error("%s: FAILED - %s", out.name, out.detail)
    return _finish(ctx, path, out, ctx.failed, "failed", reason)


def process_file(path: Path, ctx: _Ctx) -> FileOutcome:
    from erp.marketing.connectors.base import ConnectorError

    out = FileOutcome(name=path.name)
    try:
        before = path.stat()
    except OSError as exc:
        out.status, out.detail, out.destination = "failed", f"cannot read the file: {exc.strerror or type(exc).__name__}", "(still in the inbox)"
        return out
    age = ctx.now - before.st_mtime
    if age < ctx.grace:
        out.status, out.destination = "skipped", "(left in the inbox)"
        out.detail = f"still being written? modified {max(age, 0):.0f}s ago (waits {ctx.grace:g}s)"
        logger.info("%s: skipped - %s", out.name, out.detail)
        return out
    try:
        out.sha256 = sha256_file(path)
        after = path.stat()
    except OSError as exc:
        out.status, out.detail, out.destination = "failed", f"cannot read the file: {exc.strerror or type(exc).__name__}", "(still in the inbox)"
        return out
    if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
        out.status, out.destination = "skipped", "(left in the inbox)"
        out.detail = "the file changed while it was being read (still being written)"
        logger.info("%s: skipped - %s", out.name, out.detail)
        return out

    known = ctx.index.known(ctx.schema, out.sha256) if ctx.index else None
    if known or out.sha256 in ctx.seen_in_run:
        out.status = "duplicate"
        out.connector = (known or {}).get("connector", "-")
        out.detail = ("identical content was already loaded" + (f" from {known['file']} on {known['at'][:10]}" if known else " earlier in this run")
                      + ": not loaded again")
        logger.info("%s: duplicate (sha256 %s...) - %s", out.name, out.sha256[:12], out.detail)
        return _finish(ctx, path, out, ctx.processed, "processed")

    det = detect(path)
    if det.connector is None:
        return _fail(ctx, path, out, det.reason)
    out.connector = det.connector
    logger.info("%s: %s", out.name, det.how)

    connector = None
    try:
        try:
            connector = build_connector(det, path, ctx.date_format)
        except ConnectorError as exc:
            return _fail(ctx, path, out, f"the {det.connector} connector refused the file: {exc}")
        except ValueError as exc:
            return _fail(ctx, path, out, f"the file cannot be loaded with these settings: {exc}")
        try:
            result = ctx.pipeline.run(connector, dry_run=ctx.dry_run)
        except Exception as exc:  # noqa: BLE001
            from erp.marketing.pipeline import _is_connection_error
            from erp.marketing.schema import SchemaError
            if isinstance(exc, SchemaError) or _is_connection_error(exc):
                out.status, out.destination = "deferred", "(left in the inbox)"
                out.detail = _one_line(f"database unusable: {type(exc).__name__}: {exc}")
                raise DatabaseUnavailable(out.detail) from exc
            logger.exception("%s: the pipeline raised %s", out.name, type(exc).__name__)
            return _fail(ctx, path, out, f"the ingest pipeline raised {type(exc).__name__}: {_one_line(exc)}")
    finally:
        _release(connector)

    out.rows_read, out.rows_rejected = result.rows_read, result.rows_invalid
    usable = result.rows_read - result.rows_invalid - result.rows_duplicate
    out.rows_loaded = usable if ctx.dry_run else result.rows_loaded
    samples = "\n".join(f"  - {s}" for s in result.invalid_samples)
    if result.status == "failed" and not ctx.dry_run:
        return _fail(ctx, path, out, f"the pipeline reported status 'failed' (batch {result.batch_id}): {result.error or 'nothing was loaded'}"
                                     + (f"\nRejected rows:\n{samples}" if samples else ""))
    if result.rows_read == 0:
        return _fail(ctx, path, out, "the file has a header but no data rows: nothing to load")
    if out.rows_loaded == 0:
        return _fail(ctx, path, out, f"every one of the {result.rows_read} row(s) was rejected or a duplicate, nothing would load"
                                     + (f"\nRejected rows:\n{samples}" if samples else ""))
    out.status = "partial" if (result.status == "partial" and not ctx.dry_run) else "ok"
    if result.rows_invalid:
        out.detail = f"{result.rows_invalid} row(s) rejected"
        for s in result.invalid_samples[:3]:
            logger.warning("%s: rejected row - %s", out.name, s)
    ctx.seen_in_run.add(out.sha256)
    if not ctx.dry_run and ctx.index is not None:
        try:
            ctx.index.remember(ctx.schema, out.sha256, out.name, out.connector, out.rows_loaded)
        except OSError as exc:
            logger.error("could not update %s (%s): the file may be treated as new next time", INDEX_NAME, exc)
    return _finish(ctx, path, out, ctx.processed, "processed")


# --------------------------------------------------------------------------- the run
def refresh_rollup(engine, schema: str):
    """The existing incremental rollup refresh (last days + every older day whose facts changed since the last good run)."""
    from erp.marketing import rollup
    return rollup.refresh(engine, schema)


def run_inbox(inbox, schema: str | None = None, dry_run: bool = False, engine=None, rollup_fn=None,
              grace: float = GRACE_SECONDS, date_format: str = "auto", now: float | None = None) -> RunReport:
    """Process every CSV in `inbox` once. `engine` / `rollup_fn` exist so the tests can point it at a throwaway schema."""
    from erp import config
    from erp.marketing import schema as mschema

    inbox = resolve_inbox(inbox)
    schema = mschema.check_identifier(schema or config.MARKETING_SCHEMA)
    report = RunReport(inbox=str(inbox), schema=schema, dry_run=dry_run)
    processed, failed = inbox.parent / "processed", inbox.parent / "failed"

    if not inbox.is_dir():
        if dry_run:
            logger.info("the inbox %s does not exist: nothing to do", inbox)
            return report
        try:
            inbox.mkdir(parents=True, exist_ok=True)
            logger.info("created the inbox %s", inbox)
        except OSError as exc:
            report.problem = f"cannot create the inbox {inbox}: {exc.strerror or type(exc).__name__}"
            return report

    lock = Lock(inbox / LOCK_NAME)
    if not dry_run and not lock.acquire():
        report.problem = f"another run holds {LOCK_NAME} in {inbox} (delete it if no run is active)"
        return report
    try:
        entries = sorted(inbox.iterdir(), key=lambda p: p.name.lower())
        files = [p for p in entries if p.is_file() and p.suffix.lower() == ".csv" and not p.name.startswith((".", "~$"))]
        report.ignored_non_csv = sum(1 for p in entries if p.is_file() and p not in files and not p.name.startswith(".") and p.suffix.lower() != ".csv")
        index = Index(inbox / INDEX_NAME)
        pipeline = None
        if files:
            from erp.marketing.pipeline import Pipeline
            pipeline = Pipeline(engine=engine, schema=schema)
        ctx = _Ctx(schema=schema, dry_run=dry_run, index=index, pipeline=pipeline, processed=processed, failed=failed,
                   date_format=date_format, grace=grace, now=time.time() if now is None else now)
        for i, path in enumerate(files):
            try:
                report.outcomes.append(process_file(path, ctx))
            except DatabaseUnavailable as exc:
                report.outcomes.append(_last_outcome_or(path, str(exc)))
                report.problem = f"database unavailable - the run stopped, files stay in the inbox to be retried: {exc}"
                for rest in files[i + 1:]:
                    report.outcomes.append(FileOutcome(name=rest.name, status="deferred", destination="(left in the inbox)",
                                                       detail="not tried: the database was unavailable"))
                break
            except Exception as exc:  # noqa: BLE001 - one bad file must not stop the others
                logger.exception("%s: unexpected failure", path.name)
                report.outcomes.append(FileOutcome(name=path.name, status="failed", destination="(still in the inbox)",
                                                   detail=_one_line(f"unexpected {type(exc).__name__}: {exc}")))

        if report.rows_loaded() > 0 and not report.problem:
            if dry_run:
                report.rollup, report.rollup_detail = "skipped", "dry run: the rollup would be refreshed once"
            else:
                try:
                    if rollup_fn is not None:
                        res = rollup_fn(engine, schema)
                    else:
                        from erp.db import engine as default_engine
                        res = refresh_rollup(engine or default_engine, schema)
                    report.rollup, report.rollup_detail = "ok", _one_line(res.summary() if hasattr(res, "summary") else res)
                except Exception as exc:  # noqa: BLE001
                    report.rollup, report.rollup_detail = "failed", _one_line(f"{type(exc).__name__}: {exc}")
                    logger.error("rollup refresh failed: %s", report.rollup_detail)

        if not dry_run:
            try:
                index.set_last_run({
                    "at": _now_iso(), "schema": schema, "files": len(report.outcomes), "ok": report.count("ok", "partial"),
                    "duplicate": report.count("duplicate"), "failed": report.count(*FAILURE_STATUSES),
                    "skipped": report.count("skipped"), "rows_loaded": report.rows_loaded(),
                    "rollup": report.rollup, "exit_code": report.exit_code(), "problem": report.problem or None})
            except OSError as exc:
                logger.error("could not record the run in %s: %s", INDEX_NAME, exc)
    finally:
        lock.release()
    return report


def _last_outcome_or(path: Path, detail: str) -> FileOutcome:
    return FileOutcome(name=path.name, status="deferred", destination="(left in the inbox)", detail=_one_line(detail))


# --------------------------------------------------------------------------- output
def format_report(report: RunReport) -> str:
    rows = [("file", "connector", "read", "loaded", "rejected", "status", "destination")]
    num = lambda v: "-" if v is None else str(v)  # noqa: E731
    for o in report.outcomes:
        rows.append((o.name, o.connector, num(o.rows_read), num(o.rows_loaded), num(o.rows_rejected), o.status, o.destination))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    widths[0] = min(widths[0], 44)
    widths[6] = min(widths[6], 60)

    def fit(s: str, w: int) -> str:
        return s if len(s) <= w else s[: w - 3] + "..."

    lines = [f"  marketing autorun - inbox {report.inbox}   schema {report.schema}" + ("   (DRY RUN: nothing moved, nothing written)" if report.dry_run else "")]
    if report.outcomes:
        lines.append("")
        for n, r in enumerate(rows):
            lines.append("  " + "  ".join(fit(c, widths[i]).ljust(widths[i]) if i in (0, 1, 5, 6) else fit(c, widths[i]).rjust(widths[i]) for i, c in enumerate(r)))
            if n == 0:
                lines.append("  " + "  ".join("-" * w for w in widths))
        notes = [f"  {o.name}: {o.detail}" for o in report.outcomes if o.detail]
        if notes:
            lines += [""] + notes
        if report.dry_run:
            lines.append("  (dry run: 'loaded' is what WOULD be loaded)")
    else:
        lines.append("  no CSV files waiting in the inbox")
    if report.ignored_non_csv:
        lines.append(f"  ignored {report.ignored_non_csv} non-CSV file(s) in the inbox")
    if report.rollup != "not needed":
        lines.append(f"  rollup: {report.rollup}" + (f" - {report.rollup_detail}" if report.rollup_detail else ""))
    if report.problem:
        lines.append(f"  PROBLEM: {report.problem}")
    lines.append(f"  result: {'FAILED' if report.failed() else 'ok'} - {report.count('ok', 'partial')} loaded, {report.count('duplicate')} duplicate, "
                 f"{report.count(*FAILURE_STATUSES)} failed, {report.count('skipped')} skipped")
    return "\n".join(lines)


# --------------------------------------------------------------------------- command line
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m erp.marketing.autorun",
                                description="Process every CSV in the marketing inbox once: detect, load, move, refresh the rollup.")
    p.add_argument("--inbox", default=None, help="the folder to process (default: MARKETING_INBOX in .env, else data_inbox/incoming)")
    p.add_argument("--schema", default=None, help="database schema of the marketing tables (default: MARKETING_SCHEMA, else public)")
    p.add_argument("--dry-run", action="store_true", help="detect, validate and report only: nothing is moved, nothing is written")
    p.add_argument("--date-format", choices=("auto", "mdy", "dmy"), default="auto",
                   help="slash-date order for the strict connectors; auto refuses an ambiguous file (it then goes to failed/)")
    p.add_argument("--grace-seconds", type=float, default=GRACE_SECONDS,
                   help=f"skip a file modified less than this many seconds ago (default {GRACE_SECONDS})")
    p.add_argument("--verbose", action="store_true", help="debug logging")
    return p


def _setup_logging(verbose: bool) -> None:
    """Console + data_inbox/marketing_autorun.log. Only when run as a job, never on import (L-010)."""
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(LOG_FILE, encoding="utf-8"))
    except OSError:
        pass                                    # a read-only folder must not stop the job
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", handlers=handlers)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):      # quoted cells may not fit the console code page
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    if args.grace_seconds < 0:
        print("error: --grace-seconds cannot be negative", file=sys.stderr)
        return 2
    _setup_logging(args.verbose)
    try:
        report = run_inbox(args.inbox, schema=args.schema, dry_run=args.dry_run, date_format=args.date_format, grace=args.grace_seconds)
    except BaseException as exc:  # noqa: BLE001 - an unattended job logs and exits non-zero, never a bare traceback (L-013)
        logger.exception("marketing autorun failed: %s", type(exc).__name__)
        return 1
    text = format_report(report)
    print()
    print(text)
    print()
    for o in report.outcomes:        # the table above is for the console; the log gets one line per file
        logger.info("summary: %s | %s | read %s loaded %s rejected %s | %s | %s", o.name, o.connector, o.rows_read, o.rows_loaded,
                    o.rows_rejected, o.status, o.destination)
    logger.info("summary: %s", text.splitlines()[-1].strip())
    return report.exit_code()


if __name__ == "__main__":
    sys.exit(main())
