"""Data Flow feed, shared helpers: the logger, time formatting and age arithmetic, secret redaction and log tailing (split out of desktop/flow_data.py, unchanged).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path


logger = logging.getLogger("erp_desk.flow")


# ============================================================================ tiny helpers
def _iso(value) -> str | None:
    """datetime -> 'YYYY-MM-DDTHH:MM:SSZ' (UTC). Naive datetimes are taken as local time."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.astimezone()
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return None


def _local_iso(s: str | None) -> str | None:
    """'2026-09-20T10:12:59' / '2026-09-20 10:12:59' (naive local) -> UTC iso."""
    if not s or not isinstance(s, str):  # a malformed log/digest field must not take down the whole feed
        return None
    try:
        return _iso(datetime.fromisoformat(s.strip().replace(" ", "T")))
    except ValueError:
        return None


def _parse(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _age_hours(iso: str | None) -> float | None:
    dt = _parse(iso)
    return None if dt is None else max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 3600)


def _ago(iso: str | None) -> str:
    h = _age_hours(iso)
    if h is None:
        return "never"
    minutes = h * 60
    if minutes < 1:
        return "just now"
    if minutes < 90:
        return f"{round(minutes)}m ago"
    if h < 48:
        return f"{round(h)}h ago"
    return f"{round(h / 24)}d ago"


def _latest(*isos: str | None) -> str | None:
    vals = [i for i in isos if i]
    return max(vals) if vals else None


_REDACT = [
    (re.compile(r"(?i)(://[^:/\s]+:)[^@\s]+@"), r"\1***@"),                          # user:password@host
    (re.compile(r"(?i)\b(bearer|token|api[_-]?key|password|passwd|secret)\b\s*[:=]?\s*\S+"), r"\1 ***"),
    (re.compile(r"\b[A-Za-z0-9_\-]{32,}\b"), "***"),                               # long opaque tokens
]


def _redact(msg: str | None, limit: int = 180) -> str:
    """Log / exception text is shown in the UI: strip anything credential-shaped and cap the length."""
    s = (msg or "").strip().splitlines()[0] if (msg or "").strip() else ""
    for rx, rep in _REDACT:
        s = rx.sub(rep, s)
    return s[:limit]


def _read_tail(path: Path, max_bytes: int) -> str:
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()   # drop a partial first line
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""
