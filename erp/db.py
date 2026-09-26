"""
The project's two database engines, plus read_connect() - the wall-clock bound that closes the gap plain
connect_timeout leaves open (lesson L-091, strengthened).

  engine / SessionLocal    the plain recipe: writes and every pipeline script (erp/leads.py, erp/clickup_pull.py,
                           erp/daily_check.py, erp/daily_digest.py, erp/sync_employees.py, erp/weekly_report.py).
                           Unchanged by the fix below.
  read_engine              the SAME recipe plus READ_KEEPALIVE_ARGS, used only by the read-only ERP Desk feeds
                           (desktop/today_data.py, leads_data.py, sources_data.py, report_data.py, flow_data.py).
  read_connect(eng)        how those feeds get a connection: a hard wall-clock bound on the checkout itself.

Why a connection needs a SECOND bound on top of connect_timeout: connect_timeout only bounds OPENING a brand new
connection. A connection that is already sitting in the pool and then freezes mid-life - the remote host or the
network path between here and it stops answering without ever closing the TCP session - is bounded by NOTHING
today: `pool_pre_ping`'s own liveness probe (a bare "SELECT 1" run on every checkout, before the caller gets a
chance to run anything, let alone a SET LOCAL statement_timeout - see desktop/today_data.arm_timeouts) can block
forever on a dead-but-unclosed socket, and so can a real query issued right after. Two independent, complementary
bounds close this:

  1. READ_KEEPALIVE_ARGS (libpq TCP keepalives + tcp_user_timeout): the standard PostgreSQL-recommended fix for a
     connection whose PEER genuinely stops answering at the network level - a crashed host, a dropped VPN/NAT
     mapping, a firewall that silently swallows an idle session. The OS gives up on the socket within a bounded
     time instead of waiting on it forever. tcp_user_timeout is Linux-only (PG 12+); libpq documents it as a
     silent no-op on platforms that do not support the socket option (this project's dev/owner machines are
     Windows), so it is safe to set unconditionally.

  2. read_connect() (a wall-clock bound, independent of the peer's OS): TCP keepalives cannot detect every hang.
     A peer whose own operating system stays up and keeps acknowledging packets - a Postgres backend stuck on
     something, or a proxy/load-balancer in front of it that stops relaying bytes without ever closing the
     socket - never trips a keepalive, because a keepalive probe only proves the peer's KERNEL is alive, not that
     its application ever answers (verified empirically while building this fix: a TCP relay in front of the real
     database that forwards one good query and then goes silent - exactly tests/today_scenarios.py's
     `_FrozenProxy` - keeps acknowledging keepalive probes forever, so keepalives alone never fire against it).
     read_connect() checks out the connection on a background thread and gives the caller a clean TimeoutError
     after `timeout` seconds if the checkout - including pool_pre_ping's own probe - has not returned, so a caller
     never blocks past that wall-clock bound regardless of what the peer's OS is doing.

     There is no safe way to interrupt a blocking C-level socket call from another Python thread, so a checkout
     thread whose peer is GENUINELY and permanently frozen is abandoned together with its socket: that part of the
     original design was sound. What was NOT bounded is how MANY such threads could pile up: the earlier version
     spawned a fresh `threading.Thread` on every single call with no cap, so a background poller that keeps calling
     read_connect() every DESKTOP_REFRESH_SECONDS during a multi-hour outage leaked one new permanently-blocked
     thread (holding an open socket) per call, forever, for as long as nobody restarted the process - unbounded
     thread/handle growth, not "one pool slot". READ_CHECKOUT_MAX_INFLIGHT closes that: a bounded semaphore caps how
     many checkout threads may be in flight (including permanently stuck ones) at once. Once that many are stuck, a
     new read_connect() call never spawns another thread at all - it just waits up to `timeout` for a slot and then
     raises TimeoutError, exactly like a normal hang. Total leaked threads/sockets over an arbitrarily long outage
     are capped at READ_CHECKOUT_MAX_INFLIGHT, not one per poll. The bound is sized generously above the number of
     read-only feeds that can plausibly build concurrently (today/leads/sources/report/flow, each single-flight
     locked per store - see desktop/today_data.TodayStore._lock and its siblings) so a healthy burst is never
     queued behind another feed's build.

  3. Self-healing once the queue is full (audit fix, 2026-09-24 - confirmed a real gap in the fix above): a fixed
     semaphore that permanently loses a permit to every genuinely-stuck checkout thread turns a temporary outage
     into a RESTART-ONLY one. Once all READ_CHECKOUT_MAX_INFLIGHT permits are held by threads that will never
     return, no NEW checkout can ever complete through that same semaphore to prove the database is reachable
     again - however long ago it actually recovered, every read-only page stays on "unavailable" forever, and each
     request first burns the full `timeout` budget finding that out. There is no way to reclaim a permit from a
     thread genuinely stuck in a blocking C-level socket call (same limitation as above), so instead of trying to
     reclaim one, `_maybe_start_recovery_probe()` below spends at most a HANDFUL of extra background threads (capped
     by `_probe_slots`, sized `READ_CHECKOUT_PROBE_MAX_STUCK`, independent of and much smaller than the main pool)
     trying a single fresh connection OUTSIDE the exhausted semaphore. The moment one such probe succeeds, the
     database is provably reachable again, so a BRAND NEW semaphore is swapped in for every future checkout - the
     old, exhausted instance and its stuck threads are simply abandoned. This is safe because read_connect() below
     captures whichever semaphore instance was current at the start of each call into a local variable and every
     release (both the checkout's own and a stuck thread's, whenever/if it ever unblocks) always targets that same
     captured instance, never whatever the module-level name happens to point to later - so a late release from an
     old, abandoned instance can never corrupt the new one's count. A dedicated warning is logged the first time the
     queue is found full (so the exhaustion is its own visible event, not just a generic per-caller "unavailable"),
     and again when a probe proves the database has recovered; the message on the TimeoutError itself says plainly
     that no restart is needed - it clears on its own once the database answers again.

Nothing here changes queries or writes once a connection is in hand: statement timeouts are still set per query by
whoever needs one (desktop/today_data.arm_timeouts and its callers).
"""
from __future__ import annotations

import logging
import threading
import time

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from erp.config import DB_CONNECT_TIMEOUT, database_url

logger = logging.getLogger(__name__)


def make_engine(url: str | None = None, connect_timeout: int = DB_CONNECT_TIMEOUT, extra_connect_args: dict | None = None):
    """The project's engine recipe. `connect_timeout` (seconds, libpq) bounds only the time to OPEN a connection: a hung
    PostgreSQL then raises OperationalError instead of blocking the caller forever. It changes nothing for queries or writes
    (statement timeouts are set per query by whoever needs one, see desktop/today_data.py). `extra_connect_args` layers
    additional libpq connect_args on top (see READ_KEEPALIVE_ARGS) without touching the plain recipe above."""
    connect_args = {"connect_timeout": int(connect_timeout), **(extra_connect_args or {})}
    return create_engine(url or database_url(), pool_pre_ping=True, connect_args=connect_args)


engine = make_engine()
SessionLocal = sessionmaker(bind=engine)

# See the module docstring for why these exist and what each half catches.
READ_KEEPALIVE_ARGS = {
    "keepalives": 1,
    "keepalives_idle": 2,       # start probing after 2 s of silence on the connection
    "keepalives_interval": 2,   # ... every 2 s after that
    "keepalives_count": 3,      # ... give up after 3 missed probes (platform-dependent; Windows honours idle/interval,
                                 # not count, per libpq's win32 keepalive code - see read_connect() for the real bound)
    "tcp_user_timeout": 8000,   # ms; Linux/PG12+ only, a documented no-op elsewhere - same ~8 s order of magnitude
}

READ_CHECKOUT_TIMEOUT = 8.0  # seconds; wall-clock bound for read_connect(), independent of the peer's OS (see above)

# Bound on simultaneously in-flight checkout threads (see module docstring): more than the 5 read-only feeds could
# plausibly need at once (each is single-flight locked per store), so a healthy burst is never queued behind
# another feed's build - but a sustained hang can never leak more than this many threads/sockets, for any outage
# length, instead of one per call.
READ_CHECKOUT_MAX_INFLIGHT = 8

# Recovery probe bound (module docstring, point 3): independent of and much smaller than the main pool, so a still
# -hung database can leak at most a handful of extra probe threads total, never one per exhausted call.
READ_CHECKOUT_PROBE_MAX_STUCK = 4

_read_checkout_slots = threading.Semaphore(READ_CHECKOUT_MAX_INFLIGHT)
_probe_slots = threading.Semaphore(READ_CHECKOUT_PROBE_MAX_STUCK)
_slots_lock = threading.Lock()       # guards the two module vars below so concurrent callers agree on one exhaustion event
_slots_exhausted_since: float | None = None


def make_read_engine(url: str | None = None, connect_timeout: int = DB_CONNECT_TIMEOUT):
    """Same recipe as make_engine(), plus READ_KEEPALIVE_ARGS. Used only by the read-only ERP Desk feeds."""
    return make_engine(url, connect_timeout, extra_connect_args=READ_KEEPALIVE_ARGS)


read_engine = make_read_engine()


def _maybe_start_recovery_probe(eng) -> None:
    """Called (cheap, synchronously) every time a checkout could not get a free slot at all - see the module
    docstring, point 3. Logs the exhaustion as its own distinct event exactly once per outage, then - if a probe
    slot is free - spends one background thread proving whether the database has actually recovered, without
    adding to the caller's own already-exhausted wait."""
    global _slots_exhausted_since
    with _slots_lock:
        first_time = _slots_exhausted_since is None
        if first_time:
            _slots_exhausted_since = time.monotonic()
            logger.warning(
                "read-only DB checkout queue is full (%d checkouts already in flight, at least one likely frozen "
                "on a hung database) - ERP Desk read pages will show 'unavailable' until it answers again; no "
                "restart is needed, this clears itself automatically once the database responds",
                READ_CHECKOUT_MAX_INFLIGHT,
            )
    if not _probe_slots.acquire(blocking=False):
        return  # every probe slot is itself stuck on a still-hung database - do not spawn another
    try:
        threading.Thread(target=_run_recovery_probe, args=(eng,), name="db-read-recovery-probe", daemon=True).start()
    except BaseException:  # noqa: BLE001 - e.g. RuntimeError: can't start new thread (L-180 rule 2): the thread
        # that would have released this slot in its own finally never ran, so release it here instead. Do not
        # re-raise: this is a best-effort background probe called from the exhausted-checkout path, and the
        # caller's own TimeoutError (already being raised there) must not be masked by a probe-spawn failure.
        _probe_slots.release()
        logger.warning("could not start the DB recovery-probe thread; will retry on the next exhausted checkout", exc_info=True)


def _run_recovery_probe(eng) -> None:
    """Runs on its own thread, holding one of `_probe_slots`. A genuinely still-hung database blocks this thread
    forever in `eng.connect()` exactly like a stuck checkout does (see module docstring) - that is fine, it is
    bounded by READ_CHECKOUT_PROBE_MAX_STUCK, not by outage duration. If the connection succeeds, the database is
    provably reachable again: swap in a brand-new semaphore for every future checkout (see point 3 above for why
    this is safe) and log the recovery."""
    global _read_checkout_slots, _slots_exhausted_since
    try:
        try:
            conn = eng.connect()
        except BaseException:  # noqa: BLE001 - still down/refused; the next exhausted call tries again
            return
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        with _slots_lock:
            _read_checkout_slots = threading.Semaphore(READ_CHECKOUT_MAX_INFLIGHT)
            was_exhausted = _slots_exhausted_since is not None
            _slots_exhausted_since = None
        if was_exhausted:
            logger.warning(
                "read-only DB checkout queue recovered - a probe connection reached the database again; new "
                "requests get a fresh checkout pool without restarting ERP Desk"
            )
    finally:
        _probe_slots.release()


def read_connect(eng, timeout: float = READ_CHECKOUT_TIMEOUT):
    """Check out a connection from `eng` with a hard wall-clock bound (see the module docstring for why).

    Returns a live Connection, exactly as `eng.connect()` would - callers chain `.execution_options(...)` on it
    exactly as before. Raises TimeoutError if a checkout slot could not be had, or the checkout itself (including
    pool_pre_ping's own probe) has not completed, within `timeout` seconds; every caller already has "DB error
    during connect -> this part of the page says unavailable" handling (lesson L-044), so no caller needs to change
    to benefit from this.

    The `timeout` budget covers BOTH waiting for a free checkout slot (READ_CHECKOUT_MAX_INFLIGHT) and the checkout
    itself, so a caller's total wait never exceeds `timeout` even when every slot is currently held by a stuck
    checkout from an earlier, still-ongoing outage. Once every slot IS stuck, a background recovery probe (module
    docstring, point 3) keeps checking behind the scenes so the queue clears itself the moment the database
    answers again - no restart required.
    """
    slots = _read_checkout_slots  # capture the instance current NOW: a later recovery swap must never affect a
                                   # checkout already counted against this one (see point 3 in the module docstring)
    deadline = time.monotonic() + timeout
    if not slots.acquire(timeout=timeout):
        _maybe_start_recovery_probe(eng)
        raise TimeoutError(
            f"database checkout queue is full ({READ_CHECKOUT_MAX_INFLIGHT} checkouts already in flight, at least "
            f"one likely frozen) - no free slot within {timeout:g}s; no restart needed, it recovers automatically "
            f"once the database answers again"
        )
    box: dict = {}
    done = threading.Event()

    def _checkout() -> None:
        try:
            box["conn"] = eng.connect()
        except BaseException as exc:  # noqa: BLE001 - re-raised in the CALLING thread below, never swallowed here
            box["error"] = exc
        finally:
            done.set()
            slots.release()  # always the instance THIS call captured, never a later swapped-in one - see above

    try:
        threading.Thread(target=_checkout, name="db-read-checkout", daemon=True).start()
    except BaseException:  # noqa: BLE001 - e.g. RuntimeError: can't start new thread (L-180 rule 2): the thread
        # that would have released `slots` in its own finally never ran, so release it here instead, then re-raise -
        # every caller already has "DB error during connect -> this part of the page says unavailable" handling.
        slots.release()
        raise
    remaining = max(0.0, deadline - time.monotonic())
    if not done.wait(remaining):
        raise TimeoutError(f"database checkout did not complete within {timeout:g}s (a pooled connection may be frozen)")
    if "error" in box:
        raise box["error"]
    return box["conn"]
