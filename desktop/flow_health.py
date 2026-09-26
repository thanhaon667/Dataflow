"""Data Flow feed, node health: the shared healthy / stale / error classification and one health function per node (split out of desktop/flow_data.py, unchanged).
"""
from __future__ import annotations

from desktop.flow_util import _age_hours, _ago, _latest, _local_iso, _redact
from desktop.flow_triggers import _Ctx


STALE_FACTOR = 1.5          # "stale" = older than 1.5x the expected cadence


# ============================================================================ health functions
def _h(state: str, detail: str, ok: str | None = None, err: str | None = None, notes: list[str] | None = None) -> dict:
    return {"state": state, "detail": detail, "last_ok_at": ok, "last_error_at": err, "notes": notes or []}


def _classify(ok: str | None, err: str | None, expected_h: float | None) -> str:
    if err and (not ok or err > ok):
        return "error"
    if not ok:
        return "never-run"
    if expected_h:
        age = _age_hours(ok)
        if age is not None and age > expected_h * STALE_FACTOR:
            return "stale"
    return "healthy"


def _script_h(c: _Ctx, node: dict, ok_extra: list | None = None, err_extra: list | None = None,
              no_record: str = "No run recorded anywhere (the .bat / command line leaves no trace; only Script Center runs are logged).",
              mocked: str | None = None) -> dict:
    rel = node.get("script") or ""
    s = c.v["center"]["per_script"].get(rel, {})
    ok = _latest(s.get("last_ok"), *(ok_extra or []))
    err = _latest(s.get("last_error"), *(err_extra or []))
    expected = node.get("expected_every_hours")
    state = _classify(ok, err, expected)
    notes = []
    if state == "never-run":
        detail = no_record
    elif state == "error":
        detail = f"Last failure {_ago(err)}, after the last success ({_ago(ok)})." if ok else f"Last failure {_ago(err)}; no success recorded."
    elif state == "stale":
        detail = f"Last run {_ago(ok)}; expected about every {_fmt_hours(expected)}."
    else:
        detail = f"Last run {_ago(ok)}."
    if err and ok and err < ok:
        notes.append(f"A failure was logged {_ago(err)}, followed by a successful run {_ago(ok)}.")
    if mocked and state in ("healthy", "never-run"):
        state = "mocked" if state == "healthy" else state
        notes.append(mocked)
    return _h(state, detail, ok, err, notes)


def _fmt_hours(h: float | None) -> str:
    if not h:
        return "-"
    if h < 1:
        return f"{round(h * 60)} minutes"
    if h < 48:
        return f"{h:g} hours"
    return f"{round(h / 24)} days"


def _db_guard(fn):
    def wrapper(c: _Ctx, node: dict) -> dict:
        if not c.v["db"]["ok"]:
            return _h("unavailable", "Database unreachable - " + (c.v["db"]["error"] or "no details"))
        return fn(c, node)
    return wrapper


def _hl_lead_form(c, n):
    L = c.v["leads"]
    if not L["total"]:
        return _h("never-run", "No lead has reached the database yet.")
    return _h("healthy", f"{L['total']} leads from {L['sources']} distinct sources; latest {_ago(L['last_at'])}.", L["last_at"])


def _hl_webhook(c, n):
    L = c.v["leads"] if c.v["db"]["ok"] else {"last_at": None, "total": None}
    up = c.v["probe"]["webhook"]
    if up:
        return _h("healthy", "The listener answers on 127.0.0.1:8000.", L["last_at"])
    tail = f" The last lead reached the database {_ago(L['last_at'])}." if L["last_at"] else " No lead has ever been received."
    return _h("offline", "Nothing is listening on 127.0.0.1:8000 - the webhook only runs while run_webhook.bat (or uvicorn) is open." + tail, L["last_at"])


def _hl_ticket_entry(c, n):
    T = c.v["tickets"]
    if not T["total"]:
        return _h("never-run", "No tickets in the database (no ingestion script exists; rows are added by SQL).")
    return _h("healthy", f"{T['total']} tickets, {T['open']} open.", T["last_at"])


def _hl_marketing_ingest(c, n):
    M = c.v["marketing"]
    if not M["installed"]:
        return _h("never-run", "The marketing tables are not installed in this database - "
                                "run db/sql/07_marketing_schema.sql (Phase 1 has no scheduled or event trigger yet).")
    if not M["runs"]:
        return _h("never-run", "Tables installed, but no batch has been run yet - "
                                "python -m erp.marketing.ingest --csv <file> (Phase 1 has no scheduled or event trigger yet).")
    if M["failed"]:
        return _h("error", f"{M['failed']} of {M['runs']} run(s) failed - see marketing_ingest_run.error_message.", M["last_run_at"], M["last_run_at"])
    if M["partial"]:
        return _h("error", f"{M['partial']} of {M['runs']} run(s) finished 'partial' - some rows were rejected at load, see marketing_ingest_run.error_message.", M["last_run_at"], M["last_run_at"])
    return _h("healthy", f"{M['runs']} manual run(s), {M['fact_rows']} interaction row(s) loaded. "
                          "No connector is scheduled or event-driven yet - Phase 2 wires a real source.", M["last_run_at"])


def _hl_marketing_rollup(c, n):
    R = c.v["rollup"]
    if not R["installed"]:
        return _h("never-run", "The rollup tables are not installed - run db/sql/09_marketing_rollup.sql. "
                                "Nothing schedules this job (Task Scheduler is the owner's to set up).")
    if not R["runs"]:
        return _h("never-run", "Tables installed, but the refresh has not run yet - python -m erp.marketing.rollup --full --yes "
                                "for the first build. Nothing schedules it.")
    if R["last_status"] == "failed":
        return _h("error", "The last rollup refresh failed - see marketing_rollup.log / marketing_rollup_run.error_message.",
                  R["last_ok_at"], R["last_ok_at"] or "failed")
    return _h("healthy", f"{R['runs']} manual run(s), newest day in the rollup {R['newest_day'] or 'none'}, "
                          f"{R['channel_days']} day x channel row(s). Not scheduled: a person (or a Task Scheduler entry the owner creates) runs it.",
              R["last_ok_at"])


def _hl_marketing_autorun(c, n):
    A = c.v["autorun"]
    if not A["ran"]:
        return _h("never-run", "Never run - drop CSV exports in data_inbox/incoming and run python -m erp.marketing.autorun "
                                "(--dry-run first to see what it would do). UNSCHEDULED: nothing runs it but a person.")
    if A["exit_code"]:
        why = A["problem"] or f"{A['failed']} file(s) failed" + (", rollup failed" if A["rollup"] == "failed" else "")
        return _h("error", f"The last run failed: {why} - see data_inbox/marketing_autorun.log and the failed/ folder's .reason.txt files.",
                  None, A["at"])
    return _h("healthy", f"Last run: {A['ok']} file(s) loaded ({A['rows_loaded']} rows), {A['duplicate']} duplicate, rollup {A['rollup']}. "
                          "UNSCHEDULED: a person (or a Task Scheduler entry the owner creates) runs it.", A["at"])


def _hl_leads_logic(c, n):
    A, G = c.v["assign"], c.v["gaps"]
    if not A["total"]:
        return _h("never-run", "No lead has been assigned yet.")
    if G.get("no_assignment"):
        return _h("error", f"{G['no_assignment']} lead(s) have no assignment - the pipeline stopped after the insert.", A["last_at"], A["last_at"])
    return _h("healthy", f"Every lead has a Sales rep ({A['rr']} round-robin, {A['dup']} duplicate re-routes).", A["last_at"])


def _hl_postgres(c, n):
    if not c.v["db"]["ok"]:
        return _h("error", "Cannot connect: " + (c.v["db"]["error"] or "unknown error"))
    rows = sum((c.v[k].get("total") or 0) for k in ("leads", "assign", "ai", "clickup", "updates", "users", "tickets"))
    return _h("healthy", f"Connected. {rows} rows across the 7 tracked tables.", c.v["leads"]["last_at"])


def _hl_views(c, n):
    V = c.v["views"]
    if V["leads_rows"] is None or V["tickets_rows"] is None:
        return _h("error", "A reporting view could not be queried (was 05_leads_sla.sql / 01_schema.sql applied?).")
    if not V["role_ok"]:
        return _h("never-run", "Views are fine, but the read-only role powerbi_reader has not been created (06_powerbi_readonly.sql).")
    return _h("healthy", "Both views answer; role powerbi_reader exists.")


def _hl_deepseek(c, n):
    A = c.v["ai"]
    if not c.v["env"]["deepseek"]:
        return _h("mocked", "DEEPSEEK_API_KEY is not set: the fixed heuristic fallback runs and every score is a placeholder.", A["last_at"])
    if not A["total"]:
        return _h("never-run", "API key is configured, but no lead has been analyzed yet.")
    notes = []
    if A["fallback"]:
        notes.append(f"{A['fallback']} of {A['total']} analyses came from the fallback (no raw AI response stored: API error or earlier mock mode).")
    return _h("healthy", f"{A['real']} real AI analyses stored; latest {_ago(A['last_at'])}.", A["last_at"], None, notes)


def _hl_clickup_push(c, n):
    K = c.v["clickup"]
    if K.get("failed"):
        return _h("error", f"{K['failed']} task(s) failed to sync (see lead_clickup_sync.error_message).", K["last_at"], K["last_at"])
    if not c.v["env"]["clickup"]:
        return _h("mocked", "CLICKUP_API_TOKEN / CLICKUP_LIST_ID are not set: tasks are created in mock mode (MOCK-xxxx ids).", K.get("last_at"))
    if not K.get("created"):
        return _h("never-run", "ClickUp is configured, but no task has been created yet.")
    notes = []
    if K.get("mocked"):
        notes.append(f"{K['mocked']} earlier task(s) were mock tasks.")
    if K.get("pending"):
        notes.append(f"{K['pending']} task(s) still pending.")
    return _h("healthy", f"{K['synced']} real ClickUp tasks created; latest {_ago(K['last_at'])}.", K["last_at"], None, notes)


def _hl_clickup(c, n):
    K = c.v["clickup"]
    if not c.v["env"]["clickup"]:
        return _h("mocked", "ClickUp is not configured - the workspace is never contacted.")
    if not K.get("synced"):
        return _h("never-run", "ClickUp is configured, but no task exists there yet.")
    return _h("healthy", f"{K['synced']} tasks live in the workspace.", K["last_at"])


def _hl_clickup_pull(c, n):
    K = c.v["clickup"]
    scheduled = c.scheduled("clickup_pull")
    if not K.get("synced"):
        if not c.v["env"]["clickup"]:
            return _h("mocked", "ClickUp is not configured. Only 'synced' tasks are pulled, so mock tasks are skipped.")
        return _h("never-run", "No synced ClickUp task exists yet, so there is nothing to pull.")
    h = _script_h(c, n, ok_extra=[K.get("last_pull_at")], no_record="No pull recorded yet.")
    if scheduled is False:
        h["notes"].insert(0, "Not scheduled: comments are pulled only when someone runs it (README recommends every ~15 min).")
    elif scheduled is None:
        h["notes"].insert(0, "Task Scheduler could not be checked.")
    if h["state"] == "stale":
        h["detail"] = f"Last pull {_ago(h['last_ok_at'])} - it should run about every 15 minutes." + (
            " No scheduled task exists." if scheduled is False else "")
    return h


def _hl_sync_employees(c, n):
    U = c.v["users"]
    h = _script_h(c, n, no_record="No run recorded (this script keeps no log).")
    if U["linked"]:
        state = "healthy" if h["state"] in ("healthy", "never-run") else h["state"]
        return _h(state, f"{U['linked']} of {U['total']} staff carry a ClickUp id, so it has run at least once (no run log is kept).", h["last_ok_at"], h["last_error_at"], h["notes"])
    if not c.v["env"]["clickup"]:
        return _h("mocked", "ClickUp is not configured, so there are no members to sync.")
    return h


def _hl_daily_check(c, n):
    h = _script_h(c, n)
    A = c.v["attention"]
    if A.get("total") is not None:
        h["notes"].append(f"Right now it would list {A['total']} item(s) needing attention.")
    return h


def _hl_daily_digest(c, n):
    d, dl = c.v["digest"], c.v["dlog"]
    h = _script_h(c, n, ok_extra=[d["generated_at"]], err_extra=[dl["last_error_at"]],
                  no_record="No digest has been generated yet.")
    if dl["last_error"] and dl["last_error_at"] and (not h["last_ok_at"] or dl["last_error_at"] > h["last_ok_at"]):
        h["detail"] = f"Last run failed {_ago(dl['last_error_at'])}: {dl['last_error']}"
    if d["exists"] and d["ai_configured"] is False and h["state"] == "healthy":
        h["state"] = "mocked"
        h["notes"].append("The digest was written without an AI key: the narrative is a placeholder note.")
    job = c.digest_job
    if job.get("started_at"):
        when = _ago(_local_iso((job.get("finished_at") or job["started_at"])[:19]))
        h["notes"].append(f"In-app run ({job.get('trigger')}) {when}: {job.get('state')}" + (f" - {job['message']}" if job.get("message") else "") + ".")
    if d["exists"]:
        h["notes"].append("Emailed on its last run." if d["email_sent"] else "Its last run did not email (in-app runs never email; SMTP is also unconfigured)." if not c.v["env"]["smtp"] else "Its last run did not email (in-app runs never email).")
    return h


def _hl_weekly_report(c, n):
    return _script_h(c, n, no_record="No run recorded anywhere (it prints to the console and keeps no log; only Script Center runs are logged).",
                     mocked=None if c.v["env"]["deepseek"] else "DEEPSEEK_API_KEY is not set: it would dump raw JSON instead of an AI report.")


def _hl_emailer(c, n):
    d = c.v["digest"]
    if not c.v["env"]["smtp"]:
        return _h("mocked", "SMTP_HOST / ALERT_EMAIL_FROM / ALERT_EMAIL_TO are not all set: every send is skipped with a warning.")
    if d["exists"] and d["email_sent"]:
        return _h("healthy", "SMTP configured; the last digest was emailed.", d["generated_at"])
    return _h("never-run", "SMTP is configured, but no emailed digest is recorded yet.")


def _hl_inbox(c, n):
    if not c.v["env"]["smtp"]:
        return _h("mocked", "ALERT_EMAIL_TO / SMTP are not configured - nothing is delivered.")
    return _h("healthy", "Recipient configured (delivery is up to your mail server).")


def _hl_digest_files(c, n):
    d = c.v["digest"]
    if not d["exists"]:
        return _h("never-run", "daily_digest_latest.json does not exist yet.")
    if d["is_today"]:
        return _h("healthy", f"Today's digest is on disk ({d['days']} day(s) of history).", d["generated_at"])
    return _h("stale", f"The latest digest is from {d['date']}, not today.", d["generated_at"])


def _hl_live_report(c, n):
    R = c.v["report"]
    if R.get("error"):
        return _h("error", f"Last DB re-query failed: {_redact(R['error'])}. The page keeps showing the last good data.", R.get("refreshed_at"), R.get("refreshed_at"))
    if not R.get("has_data"):
        return _h("never-run", "The first snapshot is still loading.")
    return _h("healthy", f"Re-queried {R['refresh_count']} time(s), every {R['interval']} s.", R.get("refreshed_at"))


def _hl_script_center(c, n):
    C = c.v["center"]
    st = c.stream_state
    # L-030: with no supervisor to ask (a scratch server, the smoke gate) the state is genuinely unknown - say so
    # instead of calling it healthy. The Health page renders that as grey "unavailable", not a green tick.
    base = {"ready": "healthy", "starting": "stale", "restarting": "stale", "down": "error"}.get(st, "unavailable")
    detail = {"ready": "The Streamlit process is up (supervised by ERP Desk).", "starting": "Starting up...",
              "restarting": "Restarting after an unexpected exit...",
              "down": "Stopped several times in a row - see erp_desktop_streamlit.log."}.get(
        st, "Nothing is supervising a Script Center process here, so whether it is up is not known.")
    if C["entries"]:
        detail += f" {C['entries']} logged action(s), {C['errors']} failed; last activity {_ago(C['last_at'])}."
    return _h(base, detail, C["last_at"], C["last_error_at"] if C["errors"] else None)


def _hl_powerbi(c, n):
    if c.v["views"]["role_ok"]:
        return _h("healthy", "The read-only role powerbi_reader exists; Power BI itself is outside this app (cannot see whether it is open).")
    return _h("never-run", "The role powerbi_reader has not been created yet.")
