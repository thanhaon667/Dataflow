"""Today page, system health: the six plain-English health cells and the blind-input list, translated from the Data Flow snapshot (split out of desktop/today_data.py, unchanged).
"""
from __future__ import annotations

from desktop.flow_data import _ago
from desktop.today_util import _n


_TASK_NOT_RUN = {"", "0", "267009", "267010", "267011"}   # schtasks "last result": ok / running / disabled / never ran


# ---- system health (translation of the Data Flow snapshot into six plain cells) --------------------------------
def _cell(id_: str, label: str, state: str, text_: str, detail: str = "", **extra) -> dict:
    return {"id": id_, "label": label, "state": state, "text": text_, "detail": detail, **extra}


def _pull_state(nodes: dict, sched: dict) -> str:
    """Is the ClickUp comment pull (the only way replies reach the database) really going to run? Taken from the Data Flow
    node's own triggers, so the "armed?" logic exists once (L-030). One of:
      scheduled  a Task Scheduler entry for erp.clickup_pull exists and its last run did not fail
      failing    it exists but reported an error on its last run
      not_scheduled  nothing schedules it (it runs only when a person starts it)
      checking / unreadable  Task Scheduler has not been read yet / could not be read, so nobody knows"""
    node = nodes.get("clickup_pull")
    autos = [t for t in (node or {}).get("triggers", []) if t.get("kind") in ("event", "scheduled", "background")]
    if node is None:
        return "unreadable"
    if any(t.get("armed") is True for t in autos):
        bad = [t for t in sched.get("tasks", []) if t.get("enabled") and "clickup_pull" in (t.get("keys") or [])
               and str(t.get("last_result", "")).strip() not in _TASK_NOT_RUN]
        return "failing" if bad else "scheduled"
    if any(t.get("armed") is None for t in autos):
        return "checking" if sched.get("pending") else "unreadable"
    return "not_scheduled"


def build_health(flow: dict | None, db_error: str | None) -> list[dict]:
    """state: ok (green) | warn (amber) | off (grey: not connected / not set up) | bad (red) | unknown (checking / unreadable)."""
    if not flow:
        soon = "Still checking - the system-health feed has not finished its first pass"
        db = (_cell("database", "Database", "bad", "Cannot connect - numbers on this page are unavailable", db_error or "")
              if db_error else _cell("database", "Database", "ok", "Connected and answering"))
        return [db] + [_cell(i, l, "unknown", soon) for i, l in (("webhook", "Lead webhook"), ("clickup", "ClickUp"), ("deepseek", "DeepSeek AI"),
                                                                  ("email", "Email"), ("jobs", "Scheduled jobs"))]
    nodes = {n["id"]: n for n in flow.get("nodes", [])}

    def hs(node: str) -> tuple[str, str, str | None]:
        h = nodes.get(node, {}).get("health") or {}
        return h.get("state", "unavailable"), h.get("detail", ""), h.get("last_ok_at")

    cells: list[dict] = []

    # database
    fdb = flow.get("db") or {}
    if db_error or not fdb.get("ok", True):
        cells.append(_cell("database", "Database", "bad", "Cannot connect - numbers on this page are unavailable", db_error or fdb.get("error") or ""))
    else:
        cells.append(_cell("database", "Database", "ok", "Connected and answering"))

    # webhook: the only door for web-form leads; it runs only while run_webhook.bat / uvicorn is open
    if (flow.get("probe") or {}).get("webhook"):
        cells.append(_cell("webhook", "Lead webhook", "ok", "Listening - new web-form leads are received automatically"))
    else:
        last = nodes.get("webhook", {}).get("health", {}).get("last_ok_at")
        cells.append(_cell("webhook", "Lead webhook", "warn", "Not running - web-form leads cannot arrive until run_webhook.bat is started",
                           f"The last lead reached the database {_ago(last)}." if last else "No lead has ever been received."))

    # ClickUp
    st, det, last = hs("clickup_push")
    if st == "error":
        cells.append(_cell("clickup", "ClickUp", "bad", "Some tasks failed to send to ClickUp", det))
    elif st == "mocked":
        cells.append(_cell("clickup", "ClickUp", "off", "Not connected - tasks are created in test mode only", det))
    elif st == "never-run":
        cells.append(_cell("clickup", "ClickUp", "warn", "Connected, but no task has been sent yet", det))
    elif st == "unavailable":
        cells.append(_cell("clickup", "ClickUp", "unknown", "Could not check - database unreachable", det))
    else:
        cells.append(_cell("clickup", "ClickUp", "ok", "Connected - " + (f"last task sent {_ago(last)}" if last else "tasks are sent"), det))

    # DeepSeek
    st, det, last = hs("deepseek")
    if st == "mocked":
        cells.append(_cell("deepseek", "DeepSeek AI", "off", "No API key - leads get a simple fallback score, not an AI one", det))
    elif st == "never-run":
        cells.append(_cell("deepseek", "DeepSeek AI", "warn", "Key is set, but no lead has been analysed yet", det))
    elif st == "unavailable":
        cells.append(_cell("deepseek", "DeepSeek AI", "unknown", "Could not check - database unreachable", det))
    elif st == "error":
        cells.append(_cell("deepseek", "DeepSeek AI", "bad", "The last AI analysis failed", det))
    else:
        cells.append(_cell("deepseek", "DeepSeek AI", "ok", "Connected - " + (f"last lead analysed {_ago(last)}" if last else "leads are scored"), det))

    # email
    st, det, _last = hs("emailer")
    if st == "mocked":
        cells.append(_cell("email", "Email", "off", "Not set up - nothing is emailed", det))
    elif st == "never-run":
        cells.append(_cell("email", "Email", "warn", "Set up, but no digest email is on record yet", det))
    else:
        cells.append(_cell("email", "Email", "ok", "Set up - the last digest was emailed", det))

    # scheduled jobs (Windows Task Scheduler, read-only scan done by flow_data)
    sch = flow.get("scheduler") or {}
    tasks = [t for t in sch.get("tasks", []) if t.get("enabled")]
    missing = (flow.get("coverage") or {}).get("not_set_up") or []
    pull = _pull_state(nodes, sch)          # extra key "pull": what build_status needs to know, without re-deriving it
    if sch.get("pending"):
        cells.append(_cell("jobs", "Scheduled jobs", "unknown", "Checking Windows Task Scheduler...", pull=pull))
    elif not sch.get("available"):
        cells.append(_cell("jobs", "Scheduled jobs", "unknown", "Could not read Windows Task Scheduler", sch.get("error") or "", pull="unreadable"))
    elif not tasks:
        cells.append(_cell("jobs", "Scheduled jobs", "warn", "Nothing is scheduled - the digest and the ClickUp pull run only when someone starts them", pull=pull))
    else:
        errs = [t for t in tasks if str(t.get("last_result", "")).strip() not in _TASK_NOT_RUN]
        names = ", ".join(t["name"] for t in tasks[:2]) + (f" +{len(tasks) - 2} more" if len(tasks) > 2 else "")
        if errs:
            cells.append(_cell("jobs", "Scheduled jobs", "warn", f"{_n(len(errs), 'scheduled job')} reported an error on the last run: {errs[0]['name']}", pull=pull))
        elif missing:
            cells.append(_cell("jobs", "Scheduled jobs", "warn", f"{_n(len(tasks), 'job')} scheduled ({names}); {len(missing)} recommended job(s) are not", pull=pull))
        else:
            cells.append(_cell("jobs", "Scheduled jobs", "ok", f"{_n(len(tasks), 'job')} scheduled: {names}", pull=pull))
    return cells


def blind_inputs(health: list[dict]) -> list[dict]:
    """Which doors the numbers arrive through are closed (or unchecked)? [{"id", "clause"}], empty when all are open.

    Pure: reads only the health cells build_health() made from the Data Flow snapshot, so the verdict can never differ from
    the strip on the same page (lesson L-084). Only the two doors that decide whether a number CAN exist are considered:
    web-form leads come in through the webhook; a rep's reply gets into the database only through the scheduled
    ClickUp comment pull (which is moot when ClickUp is not connected at all)."""
    by = {c["id"]: c for c in health}
    wh = by.get("webhook")
    if wh is None or wh["state"] == "unknown":     # the health feed has not produced a verdict yet: nothing can be vouched for
        return [{"id": "checks", "clause": "the system checks have not finished yet, so it is not known whether the webhook is running "
                                           "or the ClickUp pull job is scheduled"}]
    items: list[dict] = []
    if wh["state"] == "warn":
        items.append({"id": "webhook", "clause": "new leads only arrive while the webhook is running (it is offline)"})
    pull = (by.get("jobs") or {}).get("pull", "unreadable")
    reply = "replies are only read from ClickUp when the pull job "
    if (by.get("clickup") or {}).get("state") == "off":
        items.append({"id": "clickup", "clause": "replies can only be read from ClickUp once it is connected (it is not set up)"})
    elif pull == "not_scheduled":
        items.append({"id": "pull", "clause": reply + "is scheduled (it is not)"})
    elif pull == "checking":
        items.append({"id": "pull", "clause": reply + "is scheduled (Task Scheduler is still being checked)"})
    elif pull == "unreadable":
        items.append({"id": "pull", "clause": reply + "is scheduled (Task Scheduler could not be read)"})
    elif pull == "failing":
        items.append({"id": "pull", "clause": reply + "runs (its last scheduled run reported an error)"})
    return items
