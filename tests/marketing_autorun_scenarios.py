"""
Scenario table for the marketing inbox processor (erp/marketing/autorun.py), dump-and-count style like the other
scenario files: every scenario is one line "ok / FAIL <name>", and the run ends with "N/M scenarios passed".

Two layers, both on a temp folder (never data_inbox/, never the owner's files):

  * file-handling scenarios with a FAKE pipeline and a fake rollup (no database): detection of the two real header
    shapes and an unknown one, the filename / channel-column hint for flat_file, the partial-file (still being written)
    skip, duplicate-by-hash, processed / failed routing, the reason sidecar, dry run touching nothing, the rollup running
    exactly once, unreadable / empty / binary files, a dead database leaving the files alone, the lock, the run record, and
    that no original file is ever modified;
  * an end-to-end run against a THROWAWAY schema (perf_autorun_<hex>, the real DDL 07 + 10 + 09 installed by
    erp/marketing/sample_check.py, dropped in a `finally`): the real pipeline, the real rollup, small fixture files and -
    when data_inbox/data1.csv and data2.csv exist - COPIES of the owner's two real exports. `public` is never written:
    its table list and its interaction_fact row count are compared before and after.

Run (from the project root):
    venv\\Scripts\\python.exe -B -m tests.marketing_autorun_scenarios
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

AD_HEADER = ("Start,End,Month,Period,Campaign,Adset,Ad name,Device,Impression,Reach,Freq,Spent,Link click,LP view,Eng,Lead,"
             "Purchase,Revenue/pur,Revenue")
AD_ROWS = ['6/10/2024,6/16/2024,6,10-Jun,Cam 1,Group 1,Dynamic_ad_02,desktop,16092,7797,2.06,"475,401 ₫",100,80,74,2,1,"1,000,000 ₫",',
           '6/10/2024,6/16/2024,6,10-Jun,Cam 1,Group 1,Dynamic_ad_01,desktop,17038,8738,1.95,"256,855 ₫",100,80,74,10,5,"1,000,000 ₫",',
           '6/10/2024,6/16/2024,6,10-Jun,Cam 2,Group 2,Ad 2,iphone,2750,1548,1.78,"208,110 ₫",100,80,74,10,8,"1,000,000 ₫",']
EMAIL_HEADER = "send_date,brand,country,campaign_id,segment,delivered,unique_opens,unique_clicks,orders,revenue_eur,unsubscribes,spam_complaints"
EMAIL_ROWS = ["2025-11-01,GiftLoom,DE,CMP-GIF-20251101,Engaged,35768,6472,847,42,1249.13,37,3",
              "2025-11-01,GiftLoom,DE,CMP-GIF-20251103,Lapsed,34539,6444,787,41,1269.28,39,4",
              "2025-11-02,FotoNest,US,CMP-FOT-20251104,Prospects,41317,7809,1038,48,2037.17,51,4"]
AD_CSV = "\n".join([AD_HEADER] + AD_ROWS) + "\n"
EMAIL_CSV = "\n".join([EMAIL_HEADER] + EMAIL_ROWS) + "\n"
FLAT_CSV = "date,clicks,spend\n2026-09-01,10,5.50\n2026-09-02,20,7.25\n2026-09-03,30,9\n"
FLAT_CHANNEL_CSV = "date,channel,clicks\n2026-09-01,tiktok,10\n2026-09-02,youtube,20\n"
UNKNOWN_CSV = "foo,bar,baz\n1,2,3\n4,5,6\n"


# ------------------------------------------------------------------------------------------ helpers
class Box:
    """A temp folder with an inbox inside it (processed/ and failed/ appear next to the inbox)."""

    def __init__(self, tmp: Path, name: str = "b") -> None:
        self.root = tmp / name
        self.inbox = self.root / "incoming"
        self.inbox.mkdir(parents=True)
        self.processed = self.root / "processed"
        self.failed = self.root / "failed"

    def put(self, name: str, content, age: float = 60.0) -> Path:
        p = self.inbox / name
        p.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
        t = time.time() - age
        os.utime(p, (t, t))
        return p

    def listing(self, folder: Path | None = None) -> list[str]:
        folder = folder or self.root
        return sorted(str(p.relative_to(folder)).replace("\\", "/") for p in folder.rglob("*"))

    def files(self, folder: Path) -> list[Path]:
        return sorted(p for p in folder.glob("*") if p.is_file() and not p.name.endswith(".reason.txt")) if folder.is_dir() else []


def sha(path_or_bytes) -> str:
    data = path_or_bytes if isinstance(path_or_bytes, bytes) else Path(path_or_bytes).read_bytes()
    return hashlib.sha256(data).hexdigest()


class FakePipeline:
    """Stands in for erp.marketing.pipeline.Pipeline: records the calls, answers with a scripted BatchResult or error."""
    calls: list = []
    script = None            # callable(connector, dry_run) -> BatchResult | raises

    def __init__(self, engine=None, schema=None, **kw) -> None:
        self.schema = schema

    def run(self, connector, limit=None, dry_run=False):
        FakePipeline.calls.append((connector.origin(), type(connector).__name__, bool(dry_run)))
        if FakePipeline.script:
            return FakePipeline.script(connector, dry_run)
        return result(rows_read=3, loaded=3)


def result(rows_read=3, loaded=3, invalid=0, status="ok", error=None, dup=0):
    from erp.marketing import pipeline as pl
    r = pl.BatchResult(batch_id=str(uuid.uuid4()), source="s", schema="x")
    r.rows_read, r.rows_loaded, r.rows_invalid, r.rows_duplicate, r.status, r.error = rows_read, loaded, invalid, dup, status, error
    r.invalid_samples = [f"line {i + 2}: bad cell" for i in range(min(invalid, 3))]
    return r


@contextlib.contextmanager
def fake_pipeline(script=None):
    from erp.marketing import pipeline as pl
    real = pl.Pipeline
    FakePipeline.calls, FakePipeline.script = [], script
    pl.Pipeline = FakePipeline
    try:
        yield FakePipeline
    finally:
        pl.Pipeline = real
        FakePipeline.script = None


class Rollup:
    def __init__(self, fail: bool = False) -> None:
        self.calls, self.fail = [], fail

    def __call__(self, engine, schema):
        self.calls.append(schema)
        if self.fail:
            raise RuntimeError("rollup boom")
        return "fake rollup summary"


def go(box: Box, rollup: Rollup | None = None, **kw):
    from erp.marketing import autorun
    rollup = rollup if rollup is not None else Rollup()
    kw.setdefault("schema", "perf_fake")
    return autorun.run_inbox(box.inbox, rollup_fn=rollup, **kw), rollup


def outcome(report, name: str):
    return next((o for o in report.outcomes if o.name == name), None)


def _quiet(fn, *a, **k):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return fn(*a, **k)


@contextlib.contextmanager
def _logging_kept():
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    try:
        yield
    finally:
        for h in list(root.handlers):
            if h not in handlers:
                root.removeHandler(h)
                h.close()
        root.setLevel(level)


# ------------------------------------------------------------------------------------------ detection
def _check_detection(add, tmp: Path) -> None:
    from erp.marketing import autorun
    b = Box(tmp, "det")

    def det(name, content):
        return autorun.detect(b.put(name, content))

    d = det("a.csv", AD_CSV)
    add("detect: the paid-social header (data1 shape) -> ad_performance", d.connector == "ad_performance", d)
    d = det("e.csv", EMAIL_CSV)
    add("detect: the e-mail header (data2 shape) -> email_campaign", d.connector == "email_campaign", d)
    d = det("u.csv", UNKNOWN_CSV)
    add("detect: an unknown header with no hint is REFUSED, not guessed", d.connector is None and "nothing was guessed" in d.reason, d)
    add("detect: the refusal names the missing hint (filename convention and channel column)",
        "<channel>__<currency>__whatever.csv" in d.reason and "'channel' column" in d.reason, d.reason[:200])
    add("detect: the refusal quotes what each strict connector said (its own header check)",
        "ad_performance:" in d.reason and "email_campaign:" in d.reason and "missing:" in d.reason, d.reason[:300])
    add("detect: the refusal shows the header that was found", "'foo'" in d.reason and "'baz'" in d.reason)
    d = det("facebook__usd__march.csv", FLAT_CSV)
    add("detect: filename <channel>__<currency>__x.csv -> flat_file with that channel and currency",
        (d.connector, d.channel, d.currency) == ("flat_file", "facebook", "USD"), d)
    d = det("Tik Tok__vnd__x.csv", FLAT_CSV)
    add("detect: the filename channel is normalised (slug) and the currency upper-cased",
        (d.connector, d.channel, d.currency) == ("flat_file", "tik_tok", "VND"), d)
    d = det("tiktok.csv", FLAT_CHANNEL_CSV)
    add("detect: a `channel` column is enough for flat_file", d.connector == "flat_file" and d.channel is None and "channel column" in d.how, d)
    d = det("Report.csv", "Date,CHANNEL,Clicks\n2026-09-01,x,1\n")
    add("detect: the channel column is found whatever its case", d.connector == "flat_file", d)
    d = det("facebook__dollars__x.csv", FLAT_CSV)
    add("detect: a filename whose currency part is not a 3-letter code is no hint", d.connector is None, d)
    d = det("facebook__usd.csv", FLAT_CSV)
    add("detect: a filename with only two parts is no hint", d.connector is None, d)
    d = det("nochannel.csv", "date,clicks,spend\n2026-09-01,1,2\n")
    add("detect: a generic file with neither hint is refused", d.connector is None, d)
    d = det("facebook__usd__a.csv", AD_CSV)
    add("detect: the strict header wins over a filename hint", d.connector == "ad_performance", d)
    d = det("x.csv", AD_HEADER.replace("Eng", "Eng ") + "\n" + AD_ROWS[0] + "\n")
    add("detect: a padded strict header is refused (no guessing) and the reason says so", d.connector is None and "leading or trailing spaces" in d.reason, d.reason[:200])
    d = det("x2.csv", AD_HEADER + ",Extra\n" + AD_ROWS[0] + ",1\n")
    add("detect: a strict header with one extra column is refused and the reason names it", d.connector is None and "not part of this export: 'Extra'" in d.reason, d.reason[:200])
    d = det("empty.csv", b"")
    add("detect: an empty (0 byte) file is refused with that reason", d.connector is None and "empty (0 bytes)" in d.reason, d)
    d = det("bin.csv", b"PK\x03\x04\x00\x00\x00binary\x00\x01\x02" * 30)
    add("detect: a binary file is refused as binary", d.connector is None and "binary" in d.reason, d)
    d = det("blank.csv", "\n\n")
    add("detect: a file that is only blank lines has no usable header", d.connector is None, d)
    d = det("late_bad_byte.csv", b"date,channel,clicks\n" + b"2026-09-01,web,1\n" * 3000 + b"2026-09-02,caf\xe9,1\n")
    add("detect: a bad byte far below the header does not stop detection (flat_file decodes with replacement)", d.connector == "flat_file", d)
    d = det("bad.csv", b"\xff\xfe\xfa,date\n1,2\n")
    add("detect: an invalid UTF-8 header is refused with the byte position", d.connector is None and "not valid UTF-8" in d.reason, d)
    add("filename_hint: parses both parts", autorun.filename_hint("meta__eur__q3 report.csv") == ("meta", "EUR"))
    add("filename_hint: none for a plain name", autorun.filename_hint("export.csv") is None)

    det_flat = autorun.Detection("flat_file", channel="facebook", currency="VND")
    p = b.put("facebook__vnd__c.csv", FLAT_CSV)
    c = autorun.build_connector(det_flat, p)
    add("build_connector: flat_file gets the filename channel and currency through ingest.build_connector",
        type(c).__name__ == "FlatFileConnector" and c._currency_default == "VND" and c._channel_default == "facebook", (c._currency_default, c._channel_default))
    c = autorun.build_connector(autorun.Detection("ad_performance"), b.put("ad.csv", AD_CSV))
    add("build_connector: ad_performance is built by the existing strict from_csv", type(c).__name__ == "AdPerformanceConnector")


# ------------------------------------------------------------------------------------------ routing / files
def _check_routing(add, tmp: Path) -> None:
    from erp.marketing import autorun

    b = Box(tmp, "route")
    src = b.put("data1.csv", AD_CSV)
    before = src.read_bytes()
    with fake_pipeline() as fp:
        rep, roll = go(b)
    o = outcome(rep, "data1.csv")
    dest = b.files(b.processed)
    add("route: an ok file leaves the inbox and lands in processed/ next to the inbox", not src.exists() and len(dest) == 1, b.listing())
    add("route: the processed name carries a timestamp prefix and the original name",
        dest and dest[0].name.endswith("_data1.csv") and dest[0].name[:8].isdigit() and dest[0].name[8] == "-", dest and dest[0].name)
    add("route: the original bytes are unchanged (sha256 equal)", dest and dest[0].read_bytes() == before)
    add("route: the outcome reports connector, rows and status", o.connector == "ad_performance" and (o.rows_read, o.rows_loaded, o.rows_rejected) == (3, 3, 0) and o.status == "ok", o)
    add("route: the pipeline was called once, for real (not a dry run)", fp.calls == [("data1.csv", "AdPerformanceConnector", False)], fp.calls)
    add("route: exit code 0 and one rollup call", rep.exit_code() == 0 and len(roll.calls) == 1, (rep.exit_code(), roll.calls))
    add("route: nothing is created in the inbox but the index and last-run record", sorted(p.name for p in b.inbox.iterdir()) == [autorun.INDEX_NAME], b.listing(b.inbox))

    b = Box(tmp, "route2")
    u = b.put("mystery.csv", UNKNOWN_CSV)
    orig = u.read_bytes()
    with fake_pipeline() as fp:
        rep, roll = go(b)
    fails = b.files(b.failed)
    side = list(b.failed.glob("*.reason.txt"))
    add("route: an undetectable file goes to failed/, not the pipeline", len(fails) == 1 and not fp.calls and outcome(rep, "mystery.csv").status == "failed", b.listing())
    add("route: the failed file's bytes are untouched", fails and fails[0].read_bytes() == orig)
    add("route: a reason sidecar sits next to it, named <moved name>.reason.txt", len(side) == 1 and side[0].name == fails[0].name + ".reason.txt", [s.name for s in side])
    text = side[0].read_text(encoding="utf-8") if side else ""
    add("route: the sidecar says exactly what was missing and how to fix it", "no connector matched" in text and "'channel' column" in text and "rename the file" in text, text[:200])
    add("route: the sidecar records the file name and its sha256", "mystery.csv" in text and sha(orig) in text)
    add("route: a failed file makes the exit code 1 and needs no rollup", rep.exit_code() == 1 and roll.calls == [], (rep.exit_code(), roll.calls))

    b = Box(tmp, "ignore")
    b.put("notes.txt", "hello")
    b.put("sheet.xlsx", b"PK\x03\x04")
    b.put(".hidden.csv", AD_CSV)
    b.put("~$lock.csv", AD_CSV)
    (b.inbox / "sub").mkdir()
    (b.inbox / "sub" / "inner.csv").write_text(AD_CSV, encoding="utf-8")
    b.put("UPPER.CSV", AD_CSV)
    with fake_pipeline() as fp:
        rep, _ = go(b)
    add("ignore: only *.csv files are processed - .txt, .xlsx, hidden, Excel lock files and subfolders are left alone",
        [o.name for o in rep.outcomes] == ["UPPER.CSV"] and (b.inbox / "notes.txt").exists() and (b.inbox / "sheet.xlsx").exists()
        and (b.inbox / ".hidden.csv").exists() and (b.inbox / "sub" / "inner.csv").exists(), [o.name for o in rep.outcomes])
    add("ignore: an upper-case .CSV extension counts", outcome(rep, "UPPER.CSV").status == "ok")
    add("ignore: the report counts the non-CSV files it ignored", rep.ignored_non_csv == 2, rep.ignored_non_csv)

    b = Box(tmp, "same")
    with fake_pipeline():
        b.put("data.csv", AD_CSV)
        go(b)
        b.put("data.csv", AD_CSV + AD_ROWS[0] + "\n")          # same name, different bytes
        go(b)
        b.put("data.csv", EMAIL_CSV)
        go(b)
    names = [p.name for p in b.files(b.processed)]
    add("route: a same-named file never overwrites an earlier one (3 drops, 3 files in processed/)", len(names) == 3 and len(set(names)) == 3, names)
    b = Box(tmp, "same2")
    with fake_pipeline():
        for i in range(3):
            b.put("q.csv", UNKNOWN_CSV + "9,9,%d\n" % i)
            go(b)
    add("route: same-second failed collisions get distinct names and paired sidecars",
        len(b.files(b.failed)) == 3 and len(list(b.failed.glob("*.reason.txt"))) == 3, b.listing())

    b = Box(tmp, "empty")
    with fake_pipeline() as fp:
        rep, roll = go(b)
    add("route: an empty inbox is a clean no-op (exit 0, no pipeline, no rollup, processed/ and failed/ not created)",
        rep.exit_code() == 0 and not rep.outcomes and not fp.calls and not roll.calls and not b.processed.exists() and not b.failed.exists())
    add("route: an empty inbox still gets a run record", autorun.read_last_run(b.inbox) is not None and autorun.read_last_run(b.inbox)["files"] == 0)
    missing = tmp / "nowhere" / "incoming"
    with fake_pipeline():
        rep = autorun.run_inbox(missing, schema="perf_fake", dry_run=True, rollup_fn=Rollup())
    add("route: a dry run on a missing inbox creates nothing", rep.exit_code() == 0 and not (tmp / "nowhere").exists())
    with fake_pipeline():
        rep = autorun.run_inbox(missing, schema="perf_fake", rollup_fn=Rollup())
    add("route: a real run creates the missing inbox on demand", missing.is_dir() and rep.exit_code() == 0)


# ------------------------------------------------------------------------------------------ partial files
def _check_partial(add, tmp: Path) -> None:
    from erp.marketing import autorun

    b = Box(tmp, "part")
    fresh = b.put("growing.csv", AD_CSV, age=1.0)
    old = b.put("done.csv", EMAIL_CSV, age=60.0)
    with fake_pipeline() as fp:
        rep, roll = go(b)
    add("partial: a file modified 1s ago is skipped and stays in the inbox, untouched",
        outcome(rep, "growing.csv").status == "skipped" and fresh.exists() and fresh.read_text(encoding="utf-8") == AD_CSV, outcome(rep, "growing.csv"))
    add("partial: the skip is explained (modified Ns ago, waits 5s)", "modified 1s ago" in outcome(rep, "growing.csv").detail and "5s" in outcome(rep, "growing.csv").detail)
    add("partial: the settled file next to it is still processed", outcome(rep, "done.csv").status == "ok" and not old.exists())
    add("partial: a skipped file is not a failure (exit 0) and the pipeline never saw it", rep.exit_code() == 0 and [c[0] for c in fp.calls] == ["done.csv"], fp.calls)

    b = Box(tmp, "edge")
    p = b.put("edge.csv", AD_CSV, age=0)
    m = p.stat().st_mtime
    with fake_pipeline():
        r1, _ = go(b, now=m + 4.9)
        s1 = outcome(r1, "edge.csv").status
        r2, _ = go(b, now=m + 5.1)
    add("partial: 4.9s old is skipped, 5.1s old is processed (default 5s grace)", s1 == "skipped" and outcome(r2, "edge.csv").status == "ok", (s1, outcome(r2, "edge.csv").status))
    b = Box(tmp, "grace0")
    b.put("now.csv", AD_CSV, age=0)
    with fake_pipeline():
        r, _ = go(b, grace=0)
    add("partial: --grace-seconds 0 processes a file that was just written", outcome(r, "now.csv").status == "ok")
    b = Box(tmp, "future")
    b.put("future.csv", AD_CSV, age=-3600)
    with fake_pipeline():
        r, _ = go(b)
    add("partial: a file dated in the future (clock skew) is treated as still being written, not loaded", outcome(r, "future.csv").status == "skipped")

    b = Box(tmp, "grow")
    p = b.put("grow.csv", AD_CSV)
    real_sha = autorun.sha256_file

    def growing(path):
        h = real_sha(path)
        with open(path, "ab") as fh:
            fh.write(b"9,9,9\n")               # the writer appended while we were reading
        return h

    autorun.sha256_file = growing
    try:
        with fake_pipeline() as fp:
            r, _ = go(b, grace=0)
    finally:
        autorun.sha256_file = real_sha
    add("partial: a file whose size changed while it was being read is skipped, not loaded", outcome(r, "grow.csv").status == "skipped" and not fp.calls and p.exists(), outcome(r, "grow.csv"))


# ------------------------------------------------------------------------------------------ duplicates / index
def _check_duplicates(add, tmp: Path) -> None:
    from erp.marketing import autorun

    b = Box(tmp, "dup")
    with fake_pipeline() as fp:
        b.put("first.csv", AD_CSV)
        r1, roll1 = go(b)
        b.put("renamed_copy.csv", AD_CSV)            # same bytes, another name
        r2, roll2 = go(b)
    o = outcome(r2, "renamed_copy.csv")
    add("dup: identical bytes dropped again are a duplicate, moved to processed/, NOT loaded again",
        o.status == "duplicate" and len(fp.calls) == 1 and not (b.inbox / "renamed_copy.csv").exists() and len(b.files(b.processed)) == 2, (o, fp.calls))
    add("dup: the duplicate is explained with the first file's name", "first.csv" in o.detail and "not loaded again" in o.detail, o.detail)
    add("dup: a duplicate is not a failure and needs no rollup (nothing new)", r2.exit_code() == 0 and roll2.calls == [] and len(roll1.calls) == 1, (r2.exit_code(), roll2.calls))
    idx = json.loads((b.inbox / autorun.INDEX_NAME).read_text(encoding="utf-8"))
    add("dup: the sha256 is stored per schema in the inbox's index file", sha(AD_CSV.encode()) in idx["schemas"]["perf_fake"], list(idx["schemas"]))

    b = Box(tmp, "dup2")
    b.put("a.csv", AD_CSV)
    b.put("b.csv", AD_CSV)
    b.put("c.csv", EMAIL_CSV)
    with fake_pipeline() as fp:
        rep, roll = go(b)
    add("dup: two identical files in ONE run load once and the second is a duplicate", [o.status for o in rep.outcomes] == ["ok", "duplicate", "ok"] and len(fp.calls) == 2, [o.status for o in rep.outcomes])
    add("dup: three files, two loaded -> the rollup ran exactly once", len(roll.calls) == 1, roll.calls)

    b = Box(tmp, "dup3")
    with fake_pipeline() as fp:
        b.put("a.csv", AD_CSV)
        go(b, schema="perf_one")
        b.put("a.csv", AD_CSV)
        rep, _ = go(b, schema="perf_two")
    add("dup: the index is per schema - the same file is new for another schema", outcome(rep, "a.csv").status == "ok" and len(fp.calls) == 2)

    b = Box(tmp, "dup4")
    with fake_pipeline(lambda c, d: result(status="failed", loaded=0, error="boom")) as fp:
        b.put("a.csv", AD_CSV)
        go(b)
    with fake_pipeline() as fp:
        b.put("a.csv", AD_CSV)
        rep, _ = go(b)
    add("dup: a file that FAILED is not remembered - dropping it again retries the load", outcome(rep, "a.csv").status == "ok" and len(fp.calls) == 1)

    b = Box(tmp, "dup5")
    (b.inbox / autorun.INDEX_NAME).write_text("{ not json", encoding="utf-8")
    b.put("a.csv", AD_CSV)
    with fake_pipeline():
        rep, _ = go(b)
    idx = json.loads((b.inbox / autorun.INDEX_NAME).read_text(encoding="utf-8"))
    add("dup: a corrupt index file is treated as empty (the file loads) and rewritten valid", outcome(rep, "a.csv").status == "ok" and idx["schemas"]["perf_fake"], idx)

    b = Box(tmp, "dup6")
    b.put("a.csv", AD_CSV)
    with fake_pipeline():
        go(b, dry_run=True)
        add("dup: a dry run never records a hash (the same file is new for the real run)", not (b.inbox / autorun.INDEX_NAME).exists())
        rep, _ = go(b)
    add("dup: ... and the real run after it loads the file", outcome(rep, "a.csv").status == "ok")

    b = Box(tmp, "dup7")
    with fake_pipeline():
        b.put("a.csv", AD_CSV)
        go(b)
        b.put("a2.csv", AD_CSV)
        rep, _ = go(b, dry_run=True)
    add("dup: a dry run reports a known file as duplicate and leaves it in place", outcome(rep, "a2.csv").status == "duplicate" and (b.inbox / "a2.csv").exists())


# ------------------------------------------------------------------------------------------ pipeline statuses
def _check_pipeline_status(add, tmp: Path) -> None:
    from sqlalchemy.exc import OperationalError

    from erp.marketing.schema import SchemaError

    b = Box(tmp, "st1")
    b.put("a.csv", AD_CSV)
    with fake_pipeline(lambda c, d: result(rows_read=3, loaded=2, invalid=1, status="partial", error="one row failed")):
        rep, roll = go(b)
    o = outcome(rep, "a.csv")
    add("status: 'partial' is honoured - processed/, status partial, exit 0, rollup runs", o.status == "partial" and len(b.files(b.processed)) == 1 and rep.exit_code() == 0 and len(roll.calls) == 1, o)
    b = Box(tmp, "st3")
    b.put("a.csv", AD_CSV)
    with fake_pipeline(lambda c, d: result(rows_read=3, loaded=2, invalid=1, status="ok")):
        rep, _ = go(b)
    o = outcome(rep, "a.csv")
    add("status: rejected rows are counted in the outcome (rows_rejected) and named in the detail", o.rows_rejected == 1 and "1 row(s) rejected" in o.detail, o)

    b = Box(tmp, "st4")
    b.put("a.csv", AD_CSV)
    with fake_pipeline(lambda c, d: result(rows_read=3, loaded=0, invalid=0, status="failed", error="aborted: budget exceeded")):
        rep, roll = go(b)
    side = list(b.failed.glob("*.reason.txt"))
    add("status: a pipeline 'failed' (failure budget) -> failed/ with the pipeline's own error in the sidecar",
        outcome(rep, "a.csv").status == "failed" and side and "budget exceeded" in side[0].read_text(encoding="utf-8"), b.listing())
    add("status: ... exit 1, and no rollup when nothing loaded", rep.exit_code() == 1 and roll.calls == [])
    b = Box(tmp, "st5")
    b.put("a.csv", AD_CSV)
    with fake_pipeline(lambda c, d: result(rows_read=3, loaded=1, invalid=0, status="failed", error="aborted mid-way")):
        rep, roll = go(b)
    add("status: a file that failed AFTER some chunks committed still triggers the rollup (rows are in the fact)", outcome(rep, "a.csv").status == "failed" and len(roll.calls) == 1)

    b = Box(tmp, "st6")
    b.put("a.csv", AD_CSV)
    with fake_pipeline(lambda c, d: result(rows_read=3, loaded=0, invalid=3, status="ok")):
        rep, _ = go(b)
    side = list(b.failed.glob("*.reason.txt"))
    add("status: 'ok' but every row rejected -> failed/ with the rejected-row samples", outcome(rep, "a.csv").status == "failed" and side and "line 2: bad cell" in side[0].read_text(encoding="utf-8"))
    b = Box(tmp, "st7")
    b.put("a.csv", AD_HEADER + "\n")
    with fake_pipeline(lambda c, d: result(rows_read=0, loaded=0, status="ok")):
        rep, _ = go(b)
    side = list(b.failed.glob("*.reason.txt"))
    add("status: a header-only file (no data rows) -> failed/ saying so", outcome(rep, "a.csv").status == "failed" and side and "no data rows" in side[0].read_text(encoding="utf-8"))

    def boom_first(c, d):
        if c.origin() == "a.csv":
            raise ValueError("kaboom")
        return result()

    b = Box(tmp, "st8")
    b.put("a.csv", AD_CSV)
    b.put("b.csv", EMAIL_CSV)
    with fake_pipeline(boom_first):
        rep, roll = go(b)
    add("status: an unexpected pipeline exception fails that file only; the next file still loads",
        outcome(rep, "a.csv").status == "failed" and outcome(rep, "b.csv").status == "ok" and rep.exit_code() == 1 and len(roll.calls) == 1, [o.status for o in rep.outcomes])

    def db_down(c, d):
        raise SchemaError("the marketing tables are not installed")

    b = Box(tmp, "st9")
    b.put("a.csv", AD_CSV)
    b.put("b.csv", EMAIL_CSV)
    with fake_pipeline(db_down) as fp:
        rep, roll = go(b)
    add("status: marketing tables missing -> the run stops, every file stays in the inbox (nothing moved), exit 1",
        [o.status for o in rep.outcomes] == ["deferred", "deferred"] and len(fp.calls) == 1 and (b.inbox / "a.csv").exists() and (b.inbox / "b.csv").exists()
        and not b.processed.exists() and not b.failed.exists() and rep.exit_code() == 1, [o.status for o in rep.outcomes])
    add("status: ... no rollup after a database problem, and the problem is stated", roll.calls == [] and "database unavailable" in rep.problem, rep.problem)

    def conn_lost(c, d):
        raise OperationalError("SELECT 1", {}, __import__("psycopg2").OperationalError("server closed the connection"))

    b = Box(tmp, "st10")
    b.put("a.csv", AD_CSV)
    with fake_pipeline(conn_lost):
        rep, _ = go(b)
    add("status: a lost database connection is treated the same way (deferred, file kept)", outcome(rep, "a.csv").status == "deferred" and (b.inbox / "a.csv").exists())

    b = Box(tmp, "st11")
    b.put("a.csv", AD_CSV)
    roll = Rollup(fail=True)
    with fake_pipeline():
        rep, roll = go(b, rollup=roll)
    add("rollup: a failing refresh is reported, exit 1, and the loaded file is still in processed/",
        rep.rollup == "failed" and "rollup boom" in rep.rollup_detail and rep.exit_code() == 1 and len(b.files(b.processed)) == 1, (rep.rollup, rep.rollup_detail))

    b = Box(tmp, "st12")
    b.put("a.csv", AD_CSV)
    b.put("b.csv", EMAIL_CSV)
    b.put("c.csv", FLAT_CSV.replace("date", "date"), age=0)     # skipped (fresh)
    with fake_pipeline():
        rep, roll = go(b)
    add("rollup: called exactly once for two loaded files (and a skipped one)", len(roll.calls) == 1 and roll.calls == ["perf_fake"], roll.calls)

    b = Box(tmp, "st13")
    b.put("a.csv", UNKNOWN_CSV)
    b.put("b.csv", b"")
    with fake_pipeline():
        rep, roll = go(b)
    add("rollup: never called when every file failed", roll.calls == [] and rep.rollup == "not needed")


# ------------------------------------------------------------------------------------------ dry run
def _check_dry_run(add, tmp: Path) -> None:
    from erp.marketing import autorun

    b = Box(tmp, "dry")
    b.put("data1.csv", AD_CSV)
    b.put("data2.csv", EMAIL_CSV)
    b.put("mystery.csv", UNKNOWN_CSV)
    before = b.listing()
    hashes = {p.name: sha(p) for p in b.inbox.glob("*.csv")}
    mtimes = {p.name: p.stat().st_mtime_ns for p in b.inbox.glob("*.csv")}
    with fake_pipeline() as fp:
        rep, roll = go(b, dry_run=True)
    add("dry run: nothing is moved or created (folder listing identical before and after)", b.listing() == before, (before, b.listing()))
    add("dry run: file bytes and mtimes are unchanged", hashes == {p.name: sha(p) for p in b.inbox.glob("*.csv")} and mtimes == {p.name: p.stat().st_mtime_ns for p in b.inbox.glob("*.csv")})
    add("dry run: no processed/ or failed/ folder, no index, no lock, no sidecar", not b.processed.exists() and not b.failed.exists() and not (b.inbox / autorun.INDEX_NAME).exists() and not (b.inbox / autorun.LOCK_NAME).exists())
    add("dry run: the pipeline was asked for a dry run only", fp.calls and all(c[2] for c in fp.calls), fp.calls)
    add("dry run: the rollup is NOT run", roll.calls == [] and rep.rollup == "skipped")
    add("dry run: detection and validation are still reported per file", [(o.name, o.connector, o.status) for o in rep.outcomes] ==
        [("data1.csv", "ad_performance", "ok"), ("data2.csv", "email_campaign", "ok"), ("mystery.csv", "-", "failed")], [(o.name, o.connector, o.status) for o in rep.outcomes])
    add("dry run: the destination says it was not moved", all("dry run" in o.destination for o in rep.outcomes), [o.destination for o in rep.outcomes])
    add("dry run: a refusal still fails the exit code (the analyst must see it)", rep.exit_code() == 1)
    add("dry run: no last-run record is written", autorun.read_last_run(b.inbox) is None)

    # the REAL pipeline in dry-run mode opens no database connection at all: give it an engine that would explode
    class Boom:
        def connect(self, *a, **k):
            raise AssertionError("a dry run opened a database connection")

    b = Box(tmp, "dry2")
    b.put("data1.csv", AD_CSV)
    b.put("data2.csv", EMAIL_CSV)
    b.put("facebook__usd__x.csv", FLAT_CSV)
    rep = autorun.run_inbox(b.inbox, schema="perf_fake", dry_run=True, engine=Boom(), rollup_fn=Rollup())
    got = {o.name: (o.rows_read, o.rows_loaded, o.status) for o in rep.outcomes}
    add("dry run: the REAL pipeline validates the three file shapes without opening a database connection",
        got == {"data1.csv": (3, 3, "ok"), "data2.csv": (3, 3, "ok"), "facebook__usd__x.csv": (3, 3, "ok")}, got)


# ------------------------------------------------------------------------------------------ unreadable / lock / record
def _check_robustness(add, tmp: Path) -> None:
    from erp.marketing import autorun

    b = Box(tmp, "unread")
    p = b.put("a.csv", AD_CSV)
    real = autorun.sha256_file

    def denied(path):
        raise PermissionError(13, "Permission denied")

    autorun.sha256_file = denied
    try:
        with fake_pipeline() as fp:
            rep, roll = go(b)
    finally:
        autorun.sha256_file = real
    add("unreadable: a file that cannot be read fails cleanly (still in the inbox, exit 1, never traceback)",
        outcome(rep, "a.csv").status == "failed" and p.exists() and not fp.calls and rep.exit_code() == 1, outcome(rep, "a.csv"))

    b = Box(tmp, "movefail")
    p = b.put("a.csv", AD_CSV)
    real_move = autorun._move

    def cannot(path, folder):
        raise PermissionError(13, "in use by another process")

    autorun._move = cannot
    try:
        with fake_pipeline():
            rep, _ = go(b)
    finally:
        autorun._move = real_move
    o = outcome(rep, "a.csv")
    add("move: a move that fails is reported (file stays, exit 1) instead of silently losing track", o.status == "failed" and "could not move" in o.detail and p.exists() and rep.exit_code() == 1, o)
    with fake_pipeline() as fp:
        rep, _ = go(b)
    add("move: ... and the next run sees it as a duplicate (it was loaded) and moves it, no second load", outcome(rep, "a.csv").status == "duplicate" and not fp.calls and not p.exists())

    b = Box(tmp, "lock")
    lock = b.inbox / autorun.LOCK_NAME
    lock.write_text("pid 1", encoding="utf-8")
    b.put("a.csv", AD_CSV)
    with fake_pipeline() as fp:
        rep, _ = go(b)
    add("lock: a fresh lock file means another run is active: nothing is processed, exit 1", rep.problem and not rep.outcomes and not fp.calls and rep.exit_code() == 1 and (b.inbox / "a.csv").exists(), rep.problem)
    old = time.time() - autorun.LOCK_STALE_SECONDS - 60
    os.utime(lock, (old, old))
    with fake_pipeline():
        rep, _ = go(b)
    add("lock: a stale lock (a dead run's) is removed and the run proceeds", outcome(rep, "a.csv") and outcome(rep, "a.csv").status == "ok" and not rep.problem)
    add("lock: the lock is released after every run", not lock.exists())
    b = Box(tmp, "lock2")
    b.put("a.csv", AD_CSV)
    with fake_pipeline(lambda c, d: (_ for _ in ()).throw(KeyError("x"))):
        go(b)
    add("lock: the lock is released even when a file blew up", not (b.inbox / autorun.LOCK_NAME).exists())

    b = Box(tmp, "record")
    add("record: read_last_run is None before any run", autorun.read_last_run(b.inbox) is None)
    b.put("a.csv", AD_CSV)
    b.put("b.csv", UNKNOWN_CSV)
    with fake_pipeline():
        rep, _ = go(b)
    last = autorun.read_last_run(b.inbox)
    add("record: the last real run is recorded (files, ok, failed, rows, rollup, exit code)",
        last and (last["files"], last["ok"], last["failed"], last["rows_loaded"], last["rollup"], last["exit_code"]) == (2, 1, 1, 3, "ok", 1), last)
    (b.inbox / autorun.INDEX_NAME).write_text("[1,2", encoding="utf-8")
    add("record: a damaged index reads as 'never run', never raises", autorun.read_last_run(b.inbox) is None)
    add("record: an unknown inbox path reads as 'never run'", autorun.read_last_run(tmp / "does-not-exist") is None)

    from erp import config
    add("config: MARKETING_INBOX defaults to data_inbox/incoming under the project root",
        autorun.resolve_inbox(None) == (ROOT / config.MARKETING_INBOX) and str(autorun.resolve_inbox(None)).endswith(os.path.join("data_inbox", "incoming")) or bool(os.getenv("MARKETING_INBOX")),
        autorun.resolve_inbox(None))
    add("config: a relative --inbox is taken from the project root, an absolute one as given",
        autorun.resolve_inbox("x/y") == ROOT / "x" / "y" and autorun.resolve_inbox(tmp) == tmp)

    b = Box(tmp, "fmt")
    b.put("a.csv", AD_CSV)
    b.put("u.csv", UNKNOWN_CSV)
    with fake_pipeline():
        rep, _ = go(b)
    text = autorun.format_report(rep)
    add("report: the summary table has file, connector, read, loaded, rejected, status, destination and both files",
        all(w in text for w in ("file", "connector", "read", "loaded", "rejected", "status", "destination", "a.csv", "u.csv", "ad_performance")) and "FAILED" in text, text[:300])


# ------------------------------------------------------------------------------------------ command line and hygiene
def _check_cli(add, tmp: Path) -> None:
    from erp.marketing import autorun

    b = Box(tmp, "cli")
    b.put("a.csv", AD_CSV)
    with _logging_kept(), fake_pipeline():
        code = _quiet(autorun.main, ["--inbox", str(b.inbox), "--schema", "perf_fake", "--dry-run"])
    add("cli: --dry-run on a valid inbox exits 0", code == 0, code)
    with _logging_kept():
        code_bad = _quiet(autorun.main, ["--grace-seconds", "-1"])
        code_arg = _quiet(autorun.main, ["--no-such-flag"])
    add("cli: a bad argument exits 2 (never a traceback)", code_bad == 2 and code_arg == 2, (code_bad, code_arg))
    with _logging_kept():
        code_help = _quiet(autorun.main, ["--help"])
    add("cli: --help exits 0", code_help == 0)
    with _logging_kept(), fake_pipeline():
        code_bad_schema = _quiet(autorun.main, ["--inbox", str(b.inbox), "--schema", "x; drop table y"])
    add("cli: a schema name that is not a plain identifier is refused (exit 1, nothing run)", code_bad_schema == 1 and (b.inbox / "a.csv").exists(), code_bad_schema)

    root = logging.getLogger()
    before = list(root.handlers)
    import importlib
    importlib.reload(autorun)
    add("hygiene: importing the module opens no log file and adds no logging handler (L-010)", list(root.handlers) == before)
    ignored = [subprocess.run(["git", "check-ignore", "-q", f], cwd=ROOT).returncode == 0
               for f in ("data_inbox/incoming/x.csv", "data_inbox/processed/y.csv", "data_inbox/failed/z.csv.reason.txt", "data_inbox/marketing_autorun.log")]
    add("hygiene: data_inbox/ (incoming, processed, failed, log) is git-ignored", all(ignored), ignored)
    bat = (ROOT / "run_marketing_autorun.bat").read_text(encoding="utf-8")
    add("hygiene: the launcher runs the module, passes arguments through and returns its exit code, no pause",
        "python -m erp.marketing.autorun %*" in bat and "exit /b %ERRORLEVEL%" in bat and not any(ln.strip().lower() == "pause" for ln in bat.splitlines()))
    log_rel = str(autorun.LOG_FILE.relative_to(ROOT)).replace("\\", "/")
    add("hygiene: the log lives inside the git-ignored data_inbox/ folder, which the stray-file gate skips", log_rel == "data_inbox/marketing_autorun.log")


def _check_flow_map(add) -> None:
    from desktop import flow_definition as fd
    from desktop.flow_mapcheck import map_integrity
    ids = {n["id"] for n in fd.NODES}
    edges = [e for e in fd.EDGES if e["driver"] == "marketing_autorun"]
    node = next((n for n in fd.NODES if n["id"] == "marketing_autorun"), {})
    add("flow map: the marketing_autorun node exists with its files and a recommended-but-unscheduled trigger",
        "marketing_autorun" in ids and "erp/marketing/autorun.py" in node.get("files", []) and any(t["kind"] == "scheduled" and t.get("recommended") for t in node.get("triggers", [])), node.get("id"))
    add("flow map: its two automated edges (to ingest, to rollup) declare armed_by", {(e["to"], e["armed_by"]) for e in edges} == {("marketing_ingest", "schedule:marketing_autorun"), ("marketing_rollup", "schedule:marketing_autorun")}, edges)
    add("flow map: the schedule marker is declared, so the page says 'not set up' until the owner schedules it", "marketing_autorun" in fd.SCHEDULE_MARKERS)
    add("flow map: no inconsistent node or edge anywhere in the map", map_integrity() == [], map_integrity())


# ------------------------------------------------------------------------------------------ end to end (throwaway schema)
def _check_end_to_end(add, tmp: Path) -> None:
    from sqlalchemy import text

    from erp.db import engine
    from erp.marketing import autorun, sample_check

    schema = "perf_autorun_" + uuid.uuid4().hex[:8]
    if not sample_check.SCHEMA_RE.match(schema):
        add("e2e: the throwaway schema name is one sample_check accepts", False, schema)
        return
    try:
        with engine.connect() as c:
            public_tables = sorted(r[0] for r in c.execute(text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")))
            public_facts = c.execute(text("SELECT count(*) FROM public.interaction_fact")).scalar() if "interaction_fact" in public_tables else None
    except Exception as exc:  # noqa: BLE001
        add("e2e: the database is reachable for the end-to-end scenarios", False, f"{type(exc).__name__}: {exc}")
        return

    def one(sql: str, **p):
        with engine.connect() as c:
            return c.execute(text(sql), p).scalar()

    try:
        with engine.connect() as c, contextlib.redirect_stdout(io.StringIO()):
            sample_check.install(c, schema, sample_check.Report())
        real1, real2 = ROOT / "data_inbox" / "data1.csv", ROOT / "data_inbox" / "data2.csv"
        have_real = real1.is_file() and real2.is_file()
        real_hashes = {p.name: sha(p) for p in (real1, real2) if p.is_file()}

        b = Box(tmp, "e2e")
        if have_real:
            b.put("data1.csv", real1.read_bytes())
            b.put("data2.csv", real2.read_bytes())
        else:
            b.put("data1.csv", AD_CSV)
            b.put("data2.csv", EMAIL_CSV)
        b.put("facebook__usd__q3.csv", FLAT_CSV)
        b.put("mystery.csv", UNKNOWN_CSV)
        b.put("still_writing.csv", AD_CSV.replace("Cam 1", "Cam 9"), age=1.0)
        expect1, expect2 = (118, 128) if have_real else (3, 3)
        put_hash = {p.name: sha(p) for p in b.inbox.glob("*.csv")}

        rep_dry = autorun.run_inbox(b.inbox, schema=schema, dry_run=True)
        got = {o.name: (o.connector, o.rows_read, o.rows_loaded, o.status) for o in rep_dry.outcomes}
        add("e2e dry run: both real header shapes and the filename-hint file are detected and validated",
            got["data1.csv"] == ("ad_performance", expect1, expect1, "ok") and got["data2.csv"] == ("email_campaign", expect2, expect2, "ok")
            and got["facebook__usd__q3.csv"] == ("flat_file", 3, 3, "ok") and got["mystery.csv"][3] == "failed" and got["still_writing.csv"][3] == "skipped", got)
        add("e2e dry run: the database is untouched (0 fact rows, 0 ingest runs) and every file is still in the inbox",
            one(f"SELECT count(*) FROM {schema}.interaction_fact") == 0 and one(f"SELECT count(*) FROM {schema}.marketing_ingest_run") == 0
            and len(list(b.inbox.glob("*.csv"))) == 5 and not b.processed.exists(), b.listing())

        rep = autorun.run_inbox(b.inbox, schema=schema)
        st = {o.name: o.status for o in rep.outcomes}
        loaded = sum(o.rows_loaded or 0 for o in rep.outcomes if o.status in ("ok", "partial"))
        add("e2e: data1, data2 and the flat file load; the unknown file fails; the fresh file is skipped",
            st == {"data1.csv": "ok", "data2.csv": "ok", "facebook__usd__q3.csv": "ok", "mystery.csv": "failed", "still_writing.csv": "skipped"}, st)
        add("e2e: the exit code is 1 because one file failed", rep.exit_code() == 1)
        facts = one(f"SELECT count(*) FROM {schema}.interaction_fact")
        add("e2e: the fact table holds exactly the rows the summary reported as loaded", facts == loaded == expect1 + expect2 + 3, (facts, loaded))
        add("e2e: the ingest journal has one ok run per loaded file", one(f"SELECT count(*) FROM {schema}.marketing_ingest_run WHERE status = 'ok'") == 3)
        add("e2e: the flat-file channel and currency came from the filename",
            one(f"SELECT count(*) FROM {schema}.marketing_channel WHERE channel_key = 'facebook'") == 1
            and one(f"SELECT count(DISTINCT currency) FROM {schema}.interaction_fact f JOIN {schema}.marketing_channel c ON c.channel_id = f.channel_id WHERE c.channel_key = 'facebook'") == 1
            and one(f"SELECT max(currency) FROM {schema}.interaction_fact f JOIN {schema}.marketing_channel c ON c.channel_id = f.channel_id WHERE c.channel_key = 'facebook'") == "USD")
        add("e2e: the rollup ran once and wrote rows", rep.rollup == "ok" and one(f"SELECT count(*) FROM {schema}.marketing_rollup_run WHERE status = 'ok'") == 1
            and one(f"SELECT count(*) FROM {schema}.interaction_daily_channel_rollup") > 0, (rep.rollup, rep.rollup_detail))
        spend_fact = one(f"SELECT COALESCE(sum(spend_micros), 0) FROM {schema}.interaction_fact WHERE currency = 'VND'")
        spend_src = sum(r["spend"] for r in sample_check.read_source1(b.processed.glob("*_data1.csv").__next__())) * 1_000_000
        add("e2e: VND spend in the fact equals an independent sum of the source file", spend_fact == spend_src, (spend_fact, spend_src))
        moved = {p.name.split("_", 1)[1]: sha(p) for p in b.files(b.processed)}
        add("e2e: the processed files are byte-identical to what was dropped (never modified)", moved == {k: v for k, v in put_hash.items() if k in moved} and len(moved) == 3, moved)
        add("e2e: the failed file has its reason sidecar", len(list(b.failed.glob("*_mystery.csv.reason.txt"))) == 1)
        add("e2e: the fresh file is still in the inbox, byte-identical", (b.inbox / "still_writing.csv").is_file() and sha(b.inbox / "still_writing.csv") == put_hash["still_writing.csv"])
        add("e2e: the run record shows 3 loaded, 1 failed, rollup ok", (autorun.read_last_run(b.inbox) or {}).get("ok") == 3 and autorun.read_last_run(b.inbox)["failed"] == 1)

        # ---- again: the same bytes under new names are duplicates
        b.put("data1_again.csv", (b.processed.glob("*_data1.csv").__next__()).read_bytes())
        b.put("data2_again.csv", (b.processed.glob("*_data2.csv").__next__()).read_bytes())
        (b.inbox / "still_writing.csv").unlink()
        os.remove(next(b.failed.glob("*_mystery.csv")))
        runs_before = one(f"SELECT count(*) FROM {schema}.marketing_ingest_run")
        rollups_before = one(f"SELECT count(*) FROM {schema}.marketing_rollup_run")
        rep2 = autorun.run_inbox(b.inbox, schema=schema)
        add("e2e again: the same two exports dropped again are duplicates, moved to processed/, and load nothing",
            [o.status for o in rep2.outcomes] == ["duplicate", "duplicate"] and one(f"SELECT count(*) FROM {schema}.interaction_fact") == facts, [o.status for o in rep2.outcomes])
        add("e2e again: no new ingest run and no new rollup run were recorded (idempotent)",
            one(f"SELECT count(*) FROM {schema}.marketing_ingest_run") == runs_before and one(f"SELECT count(*) FROM {schema}.marketing_rollup_run") == rollups_before and rep2.exit_code() == 0)

        # ---- a file with one bad row: the pipeline's rejection budget is honoured, the good rows load
        b2 = Box(tmp, "e2e2")
        bad = "date,channel,clicks\n2026-09-01,tiktok,10\nnot-a-date,tiktok,5\n2026-09-03,tiktok,7\n"
        b2.put("tiktok_export.csv", bad)
        b2.put("all_bad.csv", "date,channel,clicks\nnope,tiktok,1\nnever,tiktok,2\n")
        rep3 = autorun.run_inbox(b2.inbox, schema=schema)
        o_bad = outcome(rep3, "tiktok_export.csv")
        add("e2e bad rows: 1 rejected of 3 -> the 2 good rows load, file processed, rejection counted",
            o_bad.status in ("ok", "partial") and (o_bad.rows_read, o_bad.rows_loaded, o_bad.rows_rejected) == (3, 2, 1) and len(b2.files(b2.processed)) == 1, o_bad)
        o_all = outcome(rep3, "all_bad.csv")
        add("e2e bad rows: a file whose every row is rejected goes to failed/ with the pipeline's reasons",
            o_all.status == "failed" and len(list(b2.failed.glob("*.reason.txt"))) == 1 and "rejected" in next(b2.failed.glob("*.reason.txt")).read_text(encoding="utf-8"), o_all)
        add("e2e bad rows: the channel column of the file was used (no filename hint needed)", one(f"SELECT count(*) FROM {schema}.marketing_channel WHERE channel_key = 'tiktok'") == 1)

        add("e2e: the owner's real files in data_inbox/ were never touched (sha256 before = after)", all(sha(p) == real_hashes[p.name] for p in (real1, real2) if p.is_file()))
        with engine.connect() as c:
            public_after = sorted(r[0] for r in c.execute(text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")))
            facts_after = c.execute(text("SELECT count(*) FROM public.interaction_fact")).scalar() if "interaction_fact" in public_after else None
        add("e2e: `public` is untouched (same tables, same interaction_fact row count)", public_after == public_tables and facts_after == public_facts, (public_facts, facts_after))
        add("e2e: the real files were exercised" if have_real else "e2e: data_inbox/data1.csv + data2.csv are absent on this machine - fixture files were used instead", True)
    finally:
        with engine.connect() as c:
            c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            c.commit()
        with engine.connect() as c:
            left = c.execute(text("SELECT count(*) FROM information_schema.schemata WHERE schema_name = :s"), {"s": schema}).scalar()
        add("e2e: the throwaway schema was dropped", left == 0, left)


# ------------------------------------------------------------------------------------------ runner
def run(db_ok: bool = True) -> list[tuple[str, bool, object]]:
    rows: list[tuple[str, bool, object]] = []

    def add(name: str, ok: bool, why: object = "") -> None:
        rows.append((name, bool(ok), why))

    logging.disable(logging.CRITICAL)            # the job logs failures loudly on purpose; the scenario dump stays readable
    with tempfile.TemporaryDirectory(prefix="autorun_scn_") as t:
        tmp = Path(t)
        for fn in (_check_detection, _check_routing, _check_partial, _check_duplicates, _check_pipeline_status,
                   _check_dry_run, _check_robustness, _check_cli, _check_flow_map, _check_end_to_end):
            if fn is _check_end_to_end and not db_ok:
                add("e2e: skipped (no database)", True)
                continue
            try:
                with _logging_kept():
                    if fn is _check_flow_map:
                        fn(add)
                    else:
                        fn(add, tmp)
            except Exception as exc:  # noqa: BLE001
                import traceback
                add(f"{fn.__name__} crashed", False, f"{type(exc).__name__}: {exc} | {traceback.format_exc().splitlines()[-3][:160]}")
    logging.disable(logging.NOTSET)
    return rows


def main() -> int:
    rows = run(True)
    for name, ok, detail in rows:
        print(f"{'ok  ' if ok else 'FAIL'} {name}" + (f"   <- {detail}" if not ok else ""))
    bad = [r for r in rows if not r[1]]
    print(f"\n{len(rows) - len(bad)}/{len(rows)} scenarios passed")
    return 1 if bad else 0


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
