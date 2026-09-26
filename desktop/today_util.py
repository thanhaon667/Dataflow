"""Today page, shared helpers: the logger, the statement / lock timeouts, number and span formatting, the log-once guard and the row reader (split out of desktop/today_data.py, unchanged).
"""
from __future__ import annotations

import logging
from datetime import date
from sqlalchemy import text


logger = logging.getLogger("erp_desk.today")

STATEMENT_TIMEOUT_MS = 4000    # every Today query is cut off after this long (SET LOCAL, so only this transaction is affected)
LOCK_TIMEOUT_MS = 2000         # ... and never waits longer than this for a lock (a read should not queue behind DDL)
QUERY_BUDGET_SECONDS = 5.0     # all queries together; once used up the remaining blocks are skipped ("too slow"), not run


# ============================================================================ small helpers
def _n(count: int, one: str, many: str | None = None) -> str:
    return f"{count} {one if count == 1 else (many or one + 's')}"


def _span(seconds: float | None) -> str:
    """4 days / 3 h 20 min / 25 min / under a minute - the size of a delay, in words."""
    if seconds is None:
        return "-"
    s = max(0, int(seconds))
    if s < 60:
        return "under a minute"
    m = s // 60
    if m < 60:
        return f"{m} min"
    h, mm = divmod(m, 60)
    if h < 6:
        return f"{h} h {mm:02d} min" if mm else f"{h} h"
    if h < 48:
        return f"{h} h"
    d = h // 24
    return _n(d, "day")


def _pretty_day(d: date) -> str:
    return f"{d.strftime('%A')} {d.day} {d.strftime('%B %Y')}"


def _i(v) -> int | None:
    return None if v is None else int(v)


def _delta(series: list[int], bad_when_up: bool | None) -> dict | None:
    """Change of a SNAPSHOT quantity (open now vs at the end of yesterday). None when there is no yesterday."""
    if len(series) < 2 or series[-1] is None or series[-2] is None:
        return None
    diff = series[-1] - series[-2]
    if diff == 0:
        return {"text": "same as yesterday", "dir": "flat", "good": None}
    up = diff > 0
    good = None if bad_when_up is None else (not up if bad_when_up else up)
    return {"text": f"{'+' if up else '-'}{abs(diff)} since yesterday", "dir": "up" if up else "down", "good": good}


def _rows(conn, sql: str) -> list[dict]:
    return [dict(r) for r in conn.execute(text(sql)).mappings().all()]


def arm_timeouts(conn, statement_ms: int = STATEMENT_TIMEOUT_MS, lock_ms: int = LOCK_TIMEOUT_MS) -> None:
    """Cap the queries of the CURRENT transaction. set_config(..., true) is SET LOCAL in one round trip: the setting dies with
    the transaction, so the pooled connection (and anything else sharing erp.db.read_engine) keeps the server defaults."""
    conn.execute(text("SELECT set_config('statement_timeout', :st, true), set_config('lock_timeout', :lk, true)"),
                 {"st": str(int(statement_ms)), "lk": str(int(lock_ms))})


_logged: dict[str, str] = {}


def _log_once(name: str, msg: str) -> None:
    if _logged.get(name) != msg:
        _logged[name] = msg
        logger.warning("Today feed: %s unavailable: %s", name, msg)


def _failed(block) -> bool:
    """A block is unusable when it is missing (None) or is the {"error": ...} marker collect() leaves for a failed query."""
    return block is None or (isinstance(block, dict) and "error" in block)
