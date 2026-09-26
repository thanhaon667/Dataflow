"""
Scenario table for the ERP Desk "Health" page feed (desktop/health_data.py), kept in the repo so the Reviewer, the
next agent and the merge gate (tests/smoke.py, check 4) can re-run it (lesson L-089).

  * wording / severity / fix-hint mapping across every state a card can be in: healthy, stale, error, never-run,
    mocked, offline, idle, unavailable - for PostgreSQL, the lead webhook, ClickUp, DeepSeek, SMTP, each scheduled
    job on its own, ERP Desk's own loops and the Script Center
  * the headline ("Everything that should be running is running" / "N things need attention") and that it is decided
    by the same facts as the Today headline - including the blind-input rule (lesson L-084), so the two pages cannot
    contradict each other about the same dependency, and the one place where they are ALLOWED to differ (a job this
    page counts and the Today page never looks at) is pinned down instead of being asserted away
  * "last checked": it follows the snapshot's `refreshed_at` (when the checks ran), not its `generated_at` (when the
    snapshot's content last changed, which on an idle machine never moves)
  * "What this means for the numbers": the blind inputs the Today caveat already names, with the pages they hit
  * configuration truth: no value from .env can reach the payload - proved with sentinel values, not by trusting the
    real .env (which may be empty here)
  * the page never acts: no fix hint claims to fix anything, and static/health.js contains no state-changing call
  * per-card degradation and store safety: a crashing card, a store whose sources raise, a build that never finishes

Read-only and offline: every input is synthetic, no database connection is opened, no thread is started and no file
outside the repository is read.

Run (from the project root):
    venv\\Scripts\\python.exe -B -m tests.health_scenarios
"""
from __future__ import annotations

import contextlib
import json
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

NOW = datetime(2026, 9, 23, 9, 0, tzinfo=timezone.utc)
FRESH = (NOW - timedelta(minutes=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
OLD = (NOW - timedelta(days=9)).strftime("%Y-%m-%dT%H:%M:%SZ")
LOCAL_NOW = "2026-09-23T16:00:00+07:00"

JOB_KEYS = ("clickup_pull", "digest_daily", "weekly_report")


# ------------------------------------------------------------------------------------------- synthetic inputs
def _node(id_: str, state: str = "healthy", detail: str = "It ran and it worked.", ok_at: str | None = FRESH,
          triggers: list | None = None) -> dict:
    return {"id": id_, "label": id_,
            "health": {"state": state, "detail": detail, "last_ok_at": ok_at, "last_error_at": None, "notes": []},
            "triggers": triggers or []}


def _task(name: str, key: str, *, enabled: bool = True, last_result: str = "0", next_run: str = "24/09/2026 08:00") -> dict:
    return {"name": name, "keys": [key], "enabled": enabled, "next_run": next_run,
            "last_run": "23/09/2026 08:00", "last_result": last_result, "schedule": "Daily"}


def flow(*, webhook_up: bool = True, db_ok: bool = True, db_error: str | None = None,
         clickup_push: str = "healthy", deepseek: str = "healthy", emailer: str = "healthy",
         clickup_pull: str = "healthy", daily_digest: str = "healthy", weekly_report: str = "healthy",
         live_report: str = "healthy", script_center: str = "healthy",
         scheduled: tuple = JOB_KEYS, tasks: list | None = None, sched_available: bool = True,
         sched_pending: bool = False, sched_error: str | None = None,
         report_armed: bool | None = True, digest_auto: bool | None = True,
         clickup_configured: bool = True, not_set_up: list | None = None) -> dict:
    """One Data Flow snapshot body, in exactly the shape flow_data.FlowStore puts in `data`."""
    if tasks is None:
        tasks = [_task(f"\\ERP {k}", k) for k in scheduled]
    sched_trigger = lambda key: [{"kind": "scheduled", "label": "Task Scheduler", "detail": "", "recommended": True,
                                  "armed": (None if not sched_available or sched_pending else any(key in t["keys"] and t["enabled"] for t in tasks)),
                                  "state": "Scheduled on this machine" if any(key in t["keys"] and t["enabled"] for t in tasks) else "Recommended, but NOT scheduled on this machine"},
                                 {"kind": "manual", "label": "run it by hand", "detail": "", "recommended": False,
                                  "armed": True, "state": "Available"}]
    nodes = [
        _node("postgres", "healthy" if db_ok else "error", "Connected." if db_ok else "Cannot connect."),
        _node("webhook", "healthy" if webhook_up else "offline",
              "The listener answers on 127.0.0.1:8000." if webhook_up else "Nothing is listening on 127.0.0.1:8000."),
        _node("clickup_push", clickup_push, "ClickUp task state."),
        _node("deepseek", deepseek, "AI analysis state."),
        _node("emailer", emailer, "SMTP state."),
        _node("clickup_pull", clickup_pull, "Comment pull state.", triggers=sched_trigger("clickup_pull")),
        _node("daily_digest", daily_digest, "Digest state.",
              triggers=[{"kind": "background", "label": "ERP Desk auto-digest", "detail": "", "recommended": False,
                         "armed": digest_auto, "state": "Running: ERP Desk generates it once a day while open" if digest_auto else "Switched off (DESKTOP_AUTO_DIGEST=0)"}]
                       + sched_trigger("digest_daily")),
        _node("weekly_report", weekly_report, "Weekly report state.", triggers=sched_trigger("weekly_report")),
        _node("live_report", live_report, "Re-queried 12 time(s), every 10 s.",
              triggers=[{"kind": "background", "label": "ERP Desk refresher thread", "detail": "", "recommended": False,
                         "armed": report_armed, "state": "Running: refresher thread is alive" if report_armed else "Not armed"},
                        {"kind": "manual", "label": "read on demand", "detail": "", "recommended": False, "armed": True, "state": "Available"}]),
        # the detail follows the state, exactly as flow_data._hl_script_center writes it - a fixture that always says
        # "the process is up" would put a green sentence on a red card and hide a real wording bug
        _node("script_center", script_center,
              {"healthy": "The Streamlit process is up (supervised by ERP Desk).",
               "stale": "Starting up...",
               "error": "Stopped several times in a row - see erp_desktop_streamlit.log."}.get(
                  script_center, "Nothing is supervising a Script Center process here, so whether it is up is not known.")),
    ]
    return {
        "nodes": nodes, "edges": [], "lanes": [], "summary": {},
        "coverage": {"total": 10, "active": 6, "partial": 0, "available": 2, "manual": 2, "not_set_up": not_set_up or []},
        "scheduler": {"available": (None if sched_pending else sched_available), "error": sched_error,
                      "pending": sched_pending, "tasks": tasks, "checked_at": FRESH},
        "db": {"ok": db_ok, "error": db_error or (None if db_ok else "OperationalError: connection refused")},
        "env": {"deepseek": deepseek != "mocked", "clickup": clickup_configured, "smtp": emailer != "mocked"},
        "probe": {"webhook": webhook_up, "cskh_dashboard": False},
        "map_check": {"available": True, "in_sync": True},
    }


def snap(data: dict | None, error: str | None = None) -> dict:
    return {"ok": data is not None, "has_data": data is not None, "version": "abc", "generated_at": LOCAL_NOW,
            "refreshed_at": LOCAL_NOW, "error": error, "interval_seconds": 10, "changed": True, "data": data}


def today_payload(data: dict | None, db_error: str | None = None, *, overdue: int = 0) -> dict:
    """The real Today payload pieces the Health page reuses - built by the real functions, not by a copy of them."""
    from desktop import today_data as td
    from tests import today_scenarios as TS
    cells = td.build_health(data, db_error)
    raw = {} if db_error else TS.raw_ok(overdue=overdue)
    return {"health": cells, "status": td.build_status(raw, cells, db_error)}


def build(data: dict | None, *, db_error: str | None = None, overdue: int = 0, error: str | None = None) -> dict:
    """The real assemble() on a synthetic snapshot. With no snapshot there is no Today payload either: the Today page
    cannot have built one without the very feed that is missing."""
    from desktop import health_data as hd
    return hd.assemble(snap(data, error), today_payload(data, db_error, overdue=overdue) if data is not None else None)


def card(payload: dict, id_: str) -> dict | None:
    return next((c for c in payload.get("cards", []) if c["id"] == id_), None)


# ------------------------------------------------------------------------------------------- scenarios
def _check_shape(add) -> None:
    from desktop import health_data as hd
    p = build(flow())
    add("every declared card is present in an all-green payload",
        {c["id"] for c in p["cards"]} >= {"database", "webhook", "clickup", "deepseek", "email",
                                          "job_clickup_pull", "job_digest_daily", "job_weekly_report",
                                          "desk_report", "desk_flow", "desk_digest", "script_center"},
        sorted(c["id"] for c in p["cards"]))
    ids = [c["id"] for c in p["cards"]]
    add("card ids are unique", len(ids) == len(set(ids)), ids)
    bad_state = [c["id"] for c in p["cards"] if c["state"] not in hd.STATES]
    bad_word = [c["id"] for c in p["cards"] if c["word"] not in hd.WORDS]
    add("every card uses the declared colour vocabulary", not bad_state, bad_state)
    add("every card uses the declared state word (healthy/stale/error/never-run/mocked/offline/idle/unavailable)",
        not bad_word, bad_word)
    thin = [c["id"] for c in p["cards"] if not c["why"] or not c["impact"] or not c["fix"]]
    add("every card says why, what depends on it, and at least one thing to do", not thin, thin)
    kinds = {f["kind"] for c in p["cards"] for f in c["fix"]}
    add("every fix hint has a known kind", kinds <= {"command", "click", "config", "owner"}, sorted(kinds))
    empty = [c["id"] for c in p["cards"] for f in c["fix"] if not str(f.get("text", "")).strip()]
    add("no fix hint is empty", not empty, empty)
    copyable = [c["id"] for c in p["cards"] if any(f.get("copy") for f in c["fix"])]
    titles = {c["state"]: c["fix_title"] for c in p["cards"]}
    add("a green card's hints are headed 'If it ever stops', not 'What to do'",
        titles.get("ok") == "If it ever stops" and all(c["fix_title"] == "What to do" for c in p["cards"] if c["state"] != "ok"),
        titles)
    add("every card offers at least one copyable fix hint",
        len(copyable) == len(p["cards"]), sorted(set(c["id"] for c in p["cards"]) - set(copyable)))
    # the consequence block must not be phrased as a hypothesis on a card that is already amber / red / grey
    mixed = build(flow(webhook_up=False, deepseek="mocked", clickup_push="error", sched_pending=True))
    titles = {c["state"]: c["impact_title"] for c in mixed["cards"]}
    add("a working card says 'If this stops', a broken one says what it costs right now",
        titles.get("ok") == "If this stops"
        and all(c["impact_title"] == "What this is costing you right now"
                for c in mixed["cards"] if c["state"] in ("warn", "bad", "off"))
        and all(c["impact_title"] == "If it is not working" for c in mixed["cards"] if c["state"] == "unknown"),
        titles)
    # the severity badge is printed only when it adds something the state word does not
    tags = {(c["state"], c["word"]): c["tag"] for c in mixed["cards"]}
    add("the severity badge never repeats the state word",
        all(t is None or t != w.upper() for (s, w), t in tags.items())
        and tags.get(("bad", "error")) is None and tags.get(("unknown", "unavailable")) is None
        and tags.get(("off", "mocked")) == "ON PURPOSE" and tags.get(("warn", "offline")) == "CHECK THIS",
        tags)
    groups = {g["id"] for g in p["groups"]}
    stray = [c["id"] for c in p["cards"] if c["group"] not in groups]
    add("every card belongs to a group that is rendered", not stray, stray)
    listed = [i for g in p["groups"] for i in g["cards"]]
    add("every card appears exactly once in exactly one group", sorted(listed) == sorted(ids), (sorted(listed), sorted(ids)))
    add("last checked / last succeeded are worded, not raw timestamps",
        all(c["checked_text"] and c["ok_text"] for c in p["cards"]),
        [c["id"] for c in p["cards"] if not (c["checked_text"] and c["ok_text"])])


def _check_headline(add) -> None:
    p = build(flow())
    add("all green -> 'Everything that should be running is running'",
        p["headline"]["level"] == "ok" and p["headline"]["text"] == "Everything that should be running is running"
        and p["headline"]["attention"] == 0, p["headline"])
    add("... and nothing is marked as needing attention",
        not [c["id"] for c in p["cards"] if c["attention"]], [c["id"] for c in p["cards"] if c["attention"]])

    p = build(flow(webhook_up=False))
    add("webhook offline -> the headline counts it and names it",
        p["headline"]["level"] in ("watch", "attention") and p["headline"]["attention"] >= 1
        and "Lead webhook listener" in p["headline"]["names"], p["headline"])
    add("... and 'N things need attention' is plural-correct",
        p["headline"]["text"] == f"{p['headline']['attention']} thing"
        + ("" if p["headline"]["attention"] == 1 else "s") + " need"
        + ("s" if p["headline"]["attention"] == 1 else "") + " attention", p["headline"]["text"])

    p = build(flow(db_ok=False), db_error="OperationalError: connection refused")
    add("database down -> the database card is red and the headline is 'attention'",
        card(p, "database")["state"] == "bad" and card(p, "database")["word"] == "error"
        and p["headline"]["level"] == "attention", (card(p, "database"), p["headline"]))
    add("... and every other card still renders (the page is useful when the DB is down)",
        len(p["cards"]) >= 12 and card(p, "script_center") is not None and card(p, "job_weekly_report") is not None,
        [c["id"] for c in p["cards"]])
    add("... and the database card still offers something to do", bool(card(p, "database")["fix"]), card(p, "database")["fix"])

    # the case the page exists for: PostgreSQL is refusing, and the system checks have not finished either
    from desktop import health_data as hd
    p = hd.assemble(snap(None, "the feed has not produced a snapshot yet"),
                    today_payload(None, db_error="OperationalError: connection refused"))
    add("database down + no system-check snapshot -> the red card is in the headline, not hidden behind 'Still checking'",
        p["headline"]["level"] == "attention" and "need" in p["headline"]["text"]
        and "PostgreSQL" in p["headline"]["names"], p["headline"])
    add("... and the headline admits the other checks have not finished",
        "not finished" in p["headline"]["sub"], p["headline"]["sub"])
    add("... and every other card still renders, each saying it is not known",
        len(p["cards"]) >= 12 and all(c["state"] == "unknown" for c in p["cards"] if c["id"] not in ("database", "desk_flow")),
        [(c["id"], c["state"]) for c in p["cards"]])
    add("... and the numbers block names the database as the reason",
        any(i["id"] == "database" for i in p["numbers"]["items"]) and not p["numbers"]["complete"], p["numbers"]["items"])

    p = build(None, error="the feed has not produced a snapshot yet")
    add("no snapshot at all -> 'Still checking', never a green verdict",
        p["headline"]["level"] == "unknown" and "checking" in p["headline"]["text"].lower(), p["headline"])
    add("... and the database is not claimed to be fine just because nobody asked",
        card(p, "database")["state"] == "unknown" and card(p, "database")["word"] == "unavailable", card(p, "database"))


def _check_states(add) -> None:
    """The wording / severity / fix-hint mapping, one case per state a card can be in."""
    cases = [
        # (kwargs, card id, expected state, expected word, a phrase that must appear in `why`)
        (dict(), "database", "ok", "healthy", "Connected"),
        (dict(db_ok=False), "database", "bad", "error", "Cannot connect"),
        (dict(), "webhook", "ok", "healthy", "Listening"),
        (dict(webhook_up=False), "webhook", "warn", "offline", "run_webhook.bat"),
        (dict(), "clickup", "ok", "healthy", "Connected"),
        (dict(clickup_push="mocked", clickup_configured=False), "clickup", "off", "mocked", "Not connected"),
        (dict(clickup_push="error"), "clickup", "bad", "error", "failed"),
        (dict(clickup_push="never-run"), "clickup", "warn", "never-run", "no task has been sent yet"),
        (dict(), "deepseek", "ok", "healthy", "Connected"),
        (dict(deepseek="mocked"), "deepseek", "off", "mocked", "No API key"),
        (dict(deepseek="never-run"), "deepseek", "warn", "never-run", "no lead has been analysed yet"),
        (dict(deepseek="error"), "deepseek", "bad", "error", "failed"),
        (dict(), "email", "ok", "healthy", "Set up"),
        (dict(emailer="mocked"), "email", "off", "mocked", "Not set up"),
        (dict(emailer="never-run"), "email", "warn", "never-run", "no digest email is on record"),
        (dict(), "job_clickup_pull", "ok", "healthy", "Scheduled as"),
        (dict(scheduled=("digest_daily", "weekly_report")), "job_clickup_pull", "warn", "offline", "Not scheduled on this machine"),
        (dict(clickup_pull="stale"), "job_clickup_pull", "warn", "stale", "not produced anything recently"),
        (dict(clickup_pull="never-run"), "job_clickup_pull", "warn", "never-run", "never produced anything yet"),
        (dict(clickup_pull="error"), "job_clickup_pull", "bad", "error", "last run failed"),
        (dict(clickup_pull="mocked"), "job_clickup_pull", "off", "mocked", "nothing real to do"),
        (dict(clickup_pull="unavailable"), "job_clickup_pull", "unknown", "unavailable", "could not be read"),
        (dict(sched_pending=True), "job_digest_daily", "unknown", "unavailable", "still being read"),
        (dict(sched_available=False, sched_error="schtasks timed out"), "job_weekly_report", "unknown", "unavailable", "could not be read"),
        (dict(), "desk_report", "ok", "healthy", "Re-queried"),
        (dict(report_armed=False), "desk_report", "warn", "offline", "not alive"),
        (dict(live_report="error"), "desk_report", "bad", "error", "Re-queried"),
        # both wrong at once: the red is the failed re-query, and the dead thread is named as the second fact
        (dict(live_report="error", report_armed=False), "desk_report", "bad", "error", "not alive either"),
        (dict(), "desk_digest", "ok", "healthy", "once a day"),
        (dict(digest_auto=False), "desk_digest", "off", "idle", "Switched off"),
        (dict(), "script_center", "ok", "healthy", "Streamlit process is up"),
        (dict(script_center="error"), "script_center", "bad", "error", ""),
        (dict(script_center="stale"), "script_center", "warn", "stale", ""),
        (dict(), "desk_flow", "ok", "healthy", "Running"),
    ]
    for kw, cid, want_state, want_word, phrase in cases:
        db_err = "OperationalError: connection refused" if kw.get("db_ok") is False else None
        p = build(flow(**kw), db_error=db_err)
        c = card(p, cid)
        name = f"{cid} / {want_word}: {json.dumps(kw, sort_keys=True, default=str)[:60]}"
        if c is None:
            add(name, False, "card missing")
            continue
        ok = c["state"] == want_state and c["word"] == want_word and (not phrase or phrase.lower() in c["why"].lower())
        add(name, ok, {"state": c["state"], "word": c["word"], "why": c["why"][:130]})

    # completeness: the table above must really exercise every colour and every state word the page can print,
    # otherwise "every state at least once" is a claim nobody re-checks when a new word is added
    from desktop import health_data as hd
    seen_states, seen_words = set(), set()
    for kw, cid, _s, _w, _p in cases:
        db_err = "OperationalError: connection refused" if kw.get("db_ok") is False else None
        for c in build(flow(**kw), db_error=db_err)["cards"]:
            seen_states.add(c["state"])
            seen_words.add(c["word"])
    add("the scenario table exercises every colour at least once", seen_states >= set(hd.STATES),
        sorted(set(hd.STATES) - seen_states))
    add("the scenario table exercises every state word at least once", seen_words >= set(hd.WORDS),
        sorted(set(hd.WORDS) - seen_words))

    # a card must never print the same fact twice: the Data Flow node's detail line is written by another module
    # and routinely repeats the sentence above it
    p = build(flow())
    dupes = [c["id"] for c in p["cards"] if c["detail"] and hd._norm(c["detail"]) in hd._norm(c["why"])]
    add("no card repeats its own sentence in the detail line", not dupes, dupes)
    add("a detail that only restates the sentence above it is dropped",
        hd._dedup_detail("Switched off (DESKTOP_AUTO_DIGEST=0).",
                         "Switched off (DESKTOP_AUTO_DIGEST=0) - a digest is only written when you ask.") == "",
        hd._dedup_detail("Switched off (DESKTOP_AUTO_DIGEST=0).", "Switched off (DESKTOP_AUTO_DIGEST=0) - x"))
    add("... but a detail that adds a fact keeps that fact and loses only the repeat",
        hd._dedup_detail("Connected. 47 rows across the 7 tracked tables.", "Connected and answering.")
        == "47 rows across the 7 tracked tables.",
        hd._dedup_detail("Connected. 47 rows across the 7 tracked tables.", "Connected and answering."))
    jobs = [c for c in build(flow(scheduled=()))["cards"] if c["id"].startswith("job_")]
    add("an unscheduled job says 'not scheduled' once, not three times",
        jobs and all(not c["detail"] and c["why"].lower().count("scheduled") == 1 for c in jobs),
        [(c["id"], c["why"], c["detail"]) for c in jobs])

    # a scheduled task that exists but reported an error on its last run is red, whatever the job's own log says
    p = build(flow(tasks=[_task("\\ERP pull", "clickup_pull", last_result="1"),
                          _task("\\ERP digest", "digest_daily"), _task("\\ERP weekly", "weekly_report")]))
    c = card(p, "job_clickup_pull")
    add("a scheduled task whose last run returned a non-zero result is red",
        c["state"] == "bad" and c["word"] == "error" and "error" in c["why"].lower(), c["why"])

    # a disabled task is not "scheduled"
    p = build(flow(tasks=[_task("\\ERP pull", "clickup_pull", enabled=False),
                          _task("\\ERP digest", "digest_daily"), _task("\\ERP weekly", "weekly_report")]))
    add("a DISABLED scheduled task does not count as scheduled",
        card(p, "job_clickup_pull")["state"] == "warn" and card(p, "job_clickup_pull")["word"] == "offline",
        card(p, "job_clickup_pull")["why"])

    # a task registered for a job the map does not require is shown honestly, as a bonus
    p = build(flow(tasks=[_task(f"\\ERP {k}", k) for k in JOB_KEYS] + [_task("\\ERP daily check", "daily_check")]))
    extra = card(p, "job_daily_check")
    add("a scheduled task the map does not require is still shown, and does not raise an alarm",
        extra is not None and extra["state"] == "ok" and not extra["attention"], extra)
    add("... and it says plainly that nothing depends on it",
        extra is not None and "Nothing depends on it" in extra["impact"], extra and extra["impact"])


def _check_fix_hints(add) -> None:
    p = build(flow(webhook_up=False, deepseek="mocked", emailer="mocked", clickup_push="mocked",
                   clickup_configured=False, scheduled=()))
    texts = {c["id"]: [f["text"] for f in c["fix"]] for c in p["cards"]}
    add("the offline webhook hints at run_webhook.bat", "run_webhook.bat" in texts["webhook"], texts["webhook"])
    add("the unconfigured ClickUp hints at the .env keys, never at a value",
        "CLICKUP_API_TOKEN" in texts["clickup"] and "CLICKUP_LIST_ID" in texts["clickup"], texts["clickup"])
    add("the unconfigured DeepSeek hints at DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY" in texts["deepseek"], texts["deepseek"])
    add("the unconfigured email hints at SMTP_HOST and the two addresses",
        {"SMTP_HOST", "ALERT_EMAIL_FROM", "ALERT_EMAIL_TO"} <= set(texts["email"]), texts["email"])
    add("the unscheduled comment pull hints at the command to run it now",
        any("erp.clickup_pull" in t for t in texts["job_clickup_pull"]), texts["job_clickup_pull"])
    add("... and says registering it in Task Scheduler needs the owner",
        any(f["kind"] == "owner" and "Task Scheduler" in f["text"] for f in card(p, "job_clickup_pull")["fix"]),
        card(p, "job_clickup_pull")["fix"])
    add("the unscheduled digest hints at run_daily_digest.bat",
        "run_daily_digest.bat" in texts["job_digest_daily"], texts["job_digest_daily"])
    owner_copy = [f for c in p["cards"] for f in c["fix"] if f["kind"] == "owner" and f["copy"]]
    add("an 'owner' hint is a sentence to read, not a command to paste", not owner_copy, owner_copy)
    # the page must never claim to act
    acting = [f["text"] for c in p["cards"] for f in c["fix"]
              if any(w in f["text"].lower() for w in ("click here to fix", "we will", "this page will start", "press to restart"))]
    add("no fix hint claims this page will do it for you", not acting, acting)
    add("the payload states plainly that the page runs nothing",
        "never runs anything" in p["never_runs"].lower() and p["never_runs"] in p["rules"], p["never_runs"])
    js = (ROOT / "desktop" / "static" / "health.js").read_text(encoding="utf-8")
    forbidden = [w for w in ("api.post", "X-ERP-Desk-Token", "/api/quit", "/api/flow/refresh", "/api/digest/generate",
                             "/api/report/refresh", "method: 'POST'") if w in js]
    add("static/health.js makes no state-changing call at all", not forbidden, forbidden)


def _check_numbers_block(add) -> None:
    p = build(flow())
    add("all green -> the numbers block says nothing is holding a figure back",
        p["numbers"]["complete"] and not p["numbers"]["items"], p["numbers"])

    p = build(flow(webhook_up=False))
    ids = [i["id"] for i in p["numbers"]["items"]]
    add("webhook offline -> the numbers block names it and lists the pages it hits",
        not p["numbers"]["complete"] and "webhook" in ids
        and all(i["affects"] for i in p["numbers"]["items"]), p["numbers"])
    add("... and its clause is the Today page's own clause",
        any("new leads only arrive while the webhook is running" in i["clause"] for i in p["numbers"]["items"]),
        p["numbers"]["items"])

    p = build(flow(scheduled=("digest_daily", "weekly_report")))
    ids = [i["id"] for i in p["numbers"]["items"]]
    add("the comment pull not scheduled -> the numbers block names the reply-based figures",
        "pull" in ids and any("awaiting first reply" in a for i in p["numbers"]["items"] for a in i["affects"]),
        p["numbers"]["items"])

    p = build(flow(clickup_push="mocked", clickup_configured=False))
    add("ClickUp not connected -> the numbers block names it",
        "clickup" in [i["id"] for i in p["numbers"]["items"]], p["numbers"]["items"])

    p = build(flow(db_ok=False), db_error="OperationalError: connection refused")
    add("database down -> the numbers block says every page is affected",
        not p["numbers"]["complete"] and p["numbers"]["items"][0]["id"] == "database", p["numbers"]["items"][:1])


def _check_agreement_with_today(add) -> None:
    """The Today strip is the summary and this page the detail: they may never tell different stories (L-084, L-102).

    Exactly ONE cross-page direction is an invariant, and it is the one health_data.mark_attention() enforces:
    if this page says all clear, the Today page may not be reporting a blind input. The opposite ("Today says All
    good, so this page must be green") is deliberately NOT asserted - see _check_different_questions below for why.
    """
    from desktop import today_data as td
    from tests import today_scenarios as TS
    cases = {
        "all green": dict(),
        "webhook offline": dict(webhook_up=False),
        "pull not scheduled": dict(scheduled=("digest_daily", "weekly_report")),
        "ClickUp not connected": dict(clickup_push="mocked", clickup_configured=False),
        "Task Scheduler unreadable": dict(sched_available=False, sched_error="schtasks is not available on this system"),
        "DeepSeek mocked only": dict(deepseek="mocked"),
        "email not set up only": dict(emailer="mocked"),
    }
    for name, kw in cases.items():
        data = flow(**kw)
        cells = td.build_health(data, None)
        p = build(data)
        blind = {b["id"] for b in td.blind_inputs(cells)}
        want = {"webhook": "webhook", "clickup": "clickup", "pull": "job_clickup_pull"}
        missing = [want[b] for b in blind if b in want and not card(p, want[b])["attention"]]
        add(f"agreement / {name}: every blind input the Today page names needs attention here", not missing, missing)
        # the strip's own verdicts are reused verbatim for the five summary cards
        drift = [c["cell"] for c in __import__("desktop.health_data", fromlist=["x"]).SUMMARY_CARDS
                 if card(p, c["id"])["state"] != next(x["state"] for x in cells if x["id"] == c["cell"])]
        add(f"agreement / {name}: the five summary cards carry the strip's own state", not drift, drift)
        status = td.build_status(TS.raw_ok(), cells, None)
        if p["headline"]["level"] == "ok":
            add(f"agreement / {name}: this page says all clear -> Today may not be blind",
                not status["blind"], status["caveat"])


def _check_different_questions(add) -> None:
    """The two pages answer different questions, and the gate must not assert an invariant that contradicts that.

    This page counts EVERY recommended job Windows Task Scheduler does not have; today_data.blind_inputs() only knows
    the webhook, ClickUp and the ClickUp comment pull, because those are the only three that decide whether a number
    CAN exist. So a correctly set-up machine - the comment pull registered (exactly what this page's own fix hint
    tells the owner to do) and run_webhook.bat running - legitimately reads "All good" on Today and "2 things need
    attention" here. An earlier version of the merge gate asserted Today-ok => Health-ok and would have turned red on
    that machine, which is why the case is pinned down here.
    """
    from desktop import health_data as hd
    from desktop import today_data as td
    from tests import today_scenarios as TS

    data = flow(scheduled=("clickup_pull",), not_set_up=["daily_digest", "weekly_report"])
    cells = td.build_health(data, None)
    status = td.build_status(TS.raw_ok(), cells, None)
    p = build(data)
    add("only the comment pull is scheduled: Today says 'All good' and is not blind",
        status["level"] == "ok" and not status["blind"], (status["headline"], status["blind_inputs"]))
    add("... while this page asks for attention about the two jobs Today never looks at",
        p["headline"]["level"] == "watch"
        and {"Daily digest (the emailed one)", "Weekly AI report"} <= set(p["headline"]["names"]),
        (p["headline"]["text"], p["headline"]["names"]))
    add("... and the two sentences are shown side by side, so the difference is visible, not hidden",
        (p["today"]["headline"] or "").startswith("All good") and p["today"]["level"] == "ok", p["today"])
    add("... and the invariant that DOES hold is still true here (not all clear, so nothing to promise)",
        not (p["headline"]["level"] == "ok" and status["blind"]), (p["headline"]["level"], status["blind"]))

    # the same the other way round: the machinery is perfect and the workload is not
    data = flow()
    p = hd.assemble(snap(data), today_payload(data, overdue=2))
    add("leads past SLA with healthy machinery: this page stays green and still shows the Today sentence",
        p["headline"]["level"] == "ok" and "Needs attention" in (p["today"]["headline"] or ""), p["today"])


def _check_last_checked(add) -> None:
    """"Last checked" must be when the checks last RAN, never when the snapshot's CONTENT last changed.

    FlowStore.refresh() only writes a new `generated_at` when the snapshot's version hash changes, and on an idle
    machine it never does (the db block is {ok, error}, the probe block is booleans). `refreshed_at` is the real
    "the checks just ran" time, and it is what the Data Flow page itself prints. Taking `generated_at` instead made
    every card claim "checked 9h ago" after a quiet night, and made the "System checks" card contradict itself
    (healthy, yet last read 9h ago).
    """
    from desktop import health_data as hd
    data = flow()
    s = snap(data)
    s["generated_at"] = "2026-09-23T16:00:00+07:00"      # the content last changed here...
    s["refreshed_at"] = "2026-09-23T16:01:40+07:00"      # ... but the checks ran again 100 s later
    p = hd.assemble(s, today_payload(data))
    add("'last checked' is the snapshot's refreshed_at, not its sticky generated_at",
        p["checked_at"] == "2026-09-23T09:01:40Z", p["checked_at"])
    live = [c for c in p["cards"] if not c["id"].startswith("job_")]
    add("... and every card whose check is that feed carries the same time",
        live and all(c["checked_at"] == p["checked_at"] for c in live),
        [(c["id"], c["checked_at"]) for c in live if c["checked_at"] != p["checked_at"]])
    add("... and the 'System checks' card does not claim a success older than its last run",
        card(p, "desk_flow")["ok_at"] == p["checked_at"], card(p, "desk_flow")["ok_at"])
    jobs = [c for c in p["cards"] if c["id"].startswith("job_")]
    add("a job card keeps the Task Scheduler scan's own time instead",
        jobs and all(c["checked_at"] == FRESH for c in jobs), [(c["id"], c["checked_at"]) for c in jobs])
    s2 = snap(data)
    s2.pop("refreshed_at")
    p2 = hd.assemble(s2, today_payload(data))
    add("a snapshot without refreshed_at falls back to generated_at instead of 'not checked yet'",
        p2["checked_at"] == "2026-09-23T09:00:00Z", p2["checked_at"])


def _check_config_truth(add) -> None:
    """Configuration is 'set' / 'not set' only - proved with sentinel values, so the real .env is irrelevant."""
    from erp import config
    from desktop import health_data as hd
    names = [k for k, _l, _g, _w in hd.CONFIG_KEYS]
    sentinels = {n: f"SENTINEL-{n}-7Qx4Zt9w" for n in names}
    old = {n: getattr(config, n, "") for n in names}
    try:
        for n, v in sentinels.items():
            setattr(config, n, v)
        p = build(flow())
        blob = json.dumps(p, sort_keys=True, default=str)
        leaked = [n for n, v in sentinels.items() if v in blob or v[9:] in blob]
        add("no configuration VALUE reaches the payload (sentinel scan over the whole JSON)", not leaked, leaked)
        cfg = p["config"]
        add("every configured setting is reported as 'set'",
            all(r["state"] == "set" for r in cfg["rows"]), [r for r in cfg["rows"] if r["state"] != "set"])
        add("with every key set, ClickUp / DeepSeek / email read as configured",
            all(i["mode"] == "configured" for i in cfg["integrations"]), cfg["integrations"])
        extra_keys = {k for r in cfg["rows"] for k in r} - {"key", "label", "integration", "why", "state"}
        add("a configuration row carries no field that could hold a value", not extra_keys, sorted(extra_keys))
        add("a configuration row's state is only 'set' or 'not set'",
            all(r["state"] in ("set", "not set") for r in cfg["rows"]), [r["state"] for r in cfg["rows"]])
        for n in names:
            setattr(config, n, "")
        p2 = build(flow(clickup_push="mocked", clickup_configured=False, deepseek="mocked", emailer="mocked"))
        cfg2 = p2["config"]
        add("with nothing set, every setting is reported as 'not set'",
            all(r["state"] == "not set" for r in cfg2["rows"]), [r for r in cfg2["rows"] if r["state"] != "not set"])
        modes = {i["id"]: i["mode"] for i in cfg2["integrations"]}
        add("with nothing set, ClickUp and DeepSeek read as 'mocked' and email as 'not set up'",
            modes == {"clickup": "mocked", "deepseek": "mocked", "email": "not set up"}, modes)
        setattr(config, "CLICKUP_API_TOKEN", "SENTINEL-partial-1")
        p3 = build(flow())
        modes3 = {i["id"]: i["mode"] for i in p3["config"]["integrations"]}
        add("one key of two -> 'partly configured', and the missing key is named by NAME only",
            modes3["clickup"] == "partly configured"
            and next(i for i in p3["config"]["integrations"] if i["id"] == "clickup")["missing"] == ["CLICKUP_LIST_ID"],
            p3["config"]["integrations"][0])
        add("... and that partial value still does not reach the payload",
            "SENTINEL-partial-1" not in json.dumps(p3, sort_keys=True, default=str), "leaked")
    finally:
        for n, v in old.items():
            setattr(config, n, v)


def _check_degradation(add) -> None:
    from desktop import health_data as hd
    # one card builder crashing must not take the page down
    real = hd._node_health
    try:
        def boom(nodes, node_id):
            if node_id == "script_center":
                raise RuntimeError("synthetic")
            return real(nodes, node_id)
        hd._node_health = boom
        p = build(flow())
        c = card(p, "script_center")
        add("a card whose check crashes degrades to 'unknown' and the rest of the page survives",
            c is not None and c["state"] == "unknown" and c["word"] == "unavailable" and len(p["cards"]) >= 12, c)
        add("... and it says only that card is affected", c is not None and "Only this card" in c["impact"], c and c["impact"])
        # a crashed card must not lose its own useful fix hints down to just "press Refresh" (they are still the
        # dependency's real hints, plus Refresh on top - Refresh alone is not a useful hint for THIS dependency)
        add("... and it still offers its own real fix hints, not only 'press Refresh'",
            c is not None and any("stop_erp_desktop.bat" in f["text"] for f in c["fix"]), c and c["fix"])
        add("... and at least one of those real hints is copyable, not just the generic Refresh click",
            c is not None and any(f.get("copy") and "Refresh" not in f["text"] for f in c["fix"]), c and c["fix"])
    finally:
        hd._node_health = real

    p = hd.assemble(None, None)
    add("assemble(None, None) still returns a complete, honest payload",
        p["headline"]["level"] == "unknown" and isinstance(p["cards"], list) and p["rules"] and p["config"]["available"],
        p["headline"])
    add("... and it never claims the feed is ok", p["ok"] is False and p["feed_ok"] is False, (p["ok"], p["feed_ok"]))

    # a Today payload that could not be built: the cells are rebuilt from the flow snapshot instead of guessing
    p = hd.assemble(snap(flow(webhook_up=False)), None)
    add("no Today payload -> the cells are rebuilt from the flow snapshot, not invented",
        card(p, "webhook")["state"] == "warn" and card(p, "webhook")["word"] == "offline", card(p, "webhook"))


def _check_store(add) -> None:
    from desktop import health_data as hd

    class _S:
        def __init__(self, value=None, raise_=False):
            self.value, self.raise_, self.calls = value, raise_, 0

        def get(self, *a, **k):
            self.calls += 1
            if self.raise_:
                raise RuntimeError("synthetic")
            return self.value

    data = flow()
    fs, ts = _S(snap(data)), _S(today_payload(data))
    before = threading.active_count()
    store = hd.HealthStore(flow=fs, today=ts)
    a = store.get()
    b = store.get()
    add("the store starts no thread", threading.active_count() == before, (before, threading.active_count()))
    add("a second read within the cache window does not re-read the sources", fs.calls == 1 and ts.calls == 1, (fs.calls, ts.calls))
    add("... and returns the same payload", a is b, "different objects")
    add("?fresh=1 is still rate-limited", store.get(fresh=True) is a, "a fresh read got through inside MIN_FRESH_SECONDS")
    store._at = 0.0
    store.get()
    add("after the cache expires the sources are read again", fs.calls == 2, fs.calls)

    broken = hd.HealthStore(flow=_S(raise_=True), today=_S(raise_=True))
    import logging
    lg = logging.getLogger("erp_desk.health")
    was, lg.disabled = lg.disabled, True
    try:
        p = broken.get()
    finally:
        lg.disabled = was
    add("a store whose sources raise still answers with an honest payload, never an exception",
        isinstance(p, dict) and p["headline"]["level"] == "unknown", p.get("headline"))

    # a build that never finishes must not park the next request for ever
    slow = hd.HealthStore(flow=_S(snap(data)), today=_S(today_payload(data)))
    slow._lock.acquire()
    hd.BUILD_WAIT_SECONDS, keep = 0.2, hd.BUILD_WAIT_SECONDS
    t0 = time.monotonic()
    try:
        was, lg.disabled = lg.disabled, True
        try:
            out = slow.get()
        finally:
            lg.disabled = was
    finally:
        hd.BUILD_WAIT_SECONDS = keep
        slow._lock.release()
    took = time.monotonic() - t0
    add("a request never parks behind a stuck build", took < 2.0 and isinstance(out, dict) and out["headline"]["level"] == "unknown",
        f"{took:.2f}s")


def _check_rules_text(add) -> None:
    p = build(flow())
    joined = " ".join(p["rules"])
    for word in ("healthy", "stale", "error", "never-run", "mocked", "offline", "idle", "unavailable"):
        if word not in joined:
            add(f"the rules text lists the state word '{word}'", False, joined[:200])
            break
    else:
        add("the rules text lists every state word the cards can use", True)
    from desktop import health_data as hd
    add("the rules text quotes the real cache / wait constants, not a literal",
        f"{hd.CACHE_SECONDS:g} s" in joined and f"{hd.BUILD_WAIT_SECONDS:g} s" in joined, joined[-320:])
    add("the rules text quotes the real number of cards",
        f"is {len(p['cards'])}" in joined, [r for r in p["rules"] if "Needs attention" in r])
    add("the rules text says where the armed-trigger logic lives (L-030)",
        "desktop/flow_data.py" in joined, "missing")


def _check_unscheduled_ok_at(add) -> None:
    """An unscheduled job must never be credited with a 'last succeeded' it did not earn from its OWN trigger.

    The fixture's `flow()` gives every job node a healthy `last_ok_at` by default, even when nothing schedules
    that job - exactly the shape of the real bug: job_digest_daily's Data Flow node ("daily_digest") is also fed
    by ERP Desk's own in-app auto-digest, which is not the scheduled, emailing job at all. Any job left off the
    schedule must show "no success on record" here, whatever the shared node claims.
    """
    for kw, cid in ((dict(scheduled=("digest_daily", "weekly_report")), "job_clickup_pull"),
                   (dict(scheduled=("clickup_pull", "weekly_report")), "job_digest_daily"),
                   (dict(scheduled=("clickup_pull", "digest_daily")), "job_weekly_report")):
        p = build(flow(**kw))
        c = card(p, cid)
        add(f"{cid}: not scheduled -> no 'last succeeded' is claimed, even though its shared node has one",
            c is not None and c["state"] == "warn" and c["word"] == "offline"
            and c["ok_at"] is None and c["ok_text"] == "no success on record",
            c and (c["state"], c["ok_at"], c["ok_text"]))


def _check_unknown_params(add) -> None:
    """GET /api/health accepts only `fresh` (lesson L-098): an unknown parameter NAME is a 400 that names it,
    the same shape as desktop/leads_data.unknown_params, never echoing the value it held."""
    from desktop import health_data as hd
    add("no problem for the one parameter this feed accepts", hd.unknown_params({"fresh": ["1"]}) == [], hd.unknown_params({"fresh": ["1"]}))
    add("no problem for no parameters at all", hd.unknown_params({}) == [], hd.unknown_params({}))
    bad = hd.unknown_params({"fresh": ["1"], "bogus": ["1"]})
    add("an unknown parameter name is reported, naming the parameter and never echoing its value",
        len(bad) == 1 and bad[0]["param"] == "bogus" and bad[0]["value"] == "" and "fresh" in bad[0]["why"], bad)
    bad2 = hd.unknown_params({"statuss": ["past_sla"]})
    add("a typo of a real-looking name is still refused, not silently accepted", len(bad2) == 1 and bad2[0]["param"] == "statuss", bad2)


def run(db_ok: bool = True) -> list[tuple[str, bool, object]]:
    """All scenarios. Returns [(name, passed, detail)]; never raises (a crash is one failed row).

    `db_ok` is accepted for symmetry with the other scenario tables; nothing here needs a database.
    """
    import logging
    rows: list[tuple[str, bool, object]] = []

    def add(name: str, ok, detail=None) -> None:
        rows.append((name, bool(ok), detail if not ok else ""))

    lg = logging.getLogger("erp_desk.health")
    old_disabled, lg.disabled = lg.disabled, True
    try:
        for fn in (_check_shape, _check_headline, _check_states, _check_fix_hints, _check_numbers_block,
                   _check_agreement_with_today, _check_different_questions, _check_last_checked,
                   _check_config_truth, _check_degradation, _check_store,
                   _check_rules_text, _check_unscheduled_ok_at, _check_unknown_params):
            try:
                fn(add)
            except Exception as exc:  # noqa: BLE001
                add(f"{fn.__name__} crashed", False, f"{type(exc).__name__}: {exc}")
    finally:
        lg.disabled = old_disabled
    return rows


def main() -> int:
    rows = run(True)
    for name, ok, detail in rows:
        print(f"{'ok  ' if ok else 'FAIL'} {name}" + (f"   <- {detail}" if not ok else ""))
    bad = [r for r in rows if not r[1]]
    print(f"\n{len(rows) - len(bad)}/{len(rows)} scenarios passed")
    with contextlib.suppress(Exception):
        from erp.db import engine
        engine.dispose()
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
