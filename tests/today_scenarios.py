"""
Scenario table for the ERP Desk "Today" page feed (desktop/today_data.py), kept in the repo so the Reviewer, the next agent
and the merge gate (tests/smoke.py, check 4) can re-run it (lesson L-089).

  * status headline / caveat: pure functions with synthetic raw + health inputs - all green, blind (webhook offline, ClickUp
    pull not scheduled / unchecked / failing, ClickUp not connected, health feed not ready), needs attention, database down
  * "since yesterday": the clause list, the honest notes when a source is blind or its query failed, silence when nothing is
    on file from before today, the reply clause when a reply really came in, and that a clause is only clickable when the
    list behind it has rows
  * the detail drawer: the lists a tile opens, a lead with and without an AI analysis, with and without a ClickUp note,
    de-duplication across lists, per-panel degradation (the drawer query fails, the tiles keep their numbers), and - when the
    database is reachable - the real lead_updates row rendered through the real SQL
  * "How this is decided" text and tile hints follow erp.config.LEAD_SLA_HOURS and erp/business_hours.py (lesson L-085)
  * hang safety (lesson L-083): SET LOCAL statement timeout really cuts a slow read-only query and does not leak into the
    pooled connection; the query budget skips blocks instead of queueing; a database that accepts the TCP connection but
    never answers (hung, not refused) gives an honest "can't read" within the connect timeout; a request never parks behind
    another request's build; erp.db.engine carries its connect_timeout
  * pooled-connection freeze (lesson L-091, strengthened): a connection that is already sitting in the pool and then
    freezes mid-life (a TCP relay in front of the real database that forwards one good query, then goes silent) fails
    within erp.db.read_connect()'s wall-clock bound instead of hanging forever; erp.db.read_engine's connections carry
    the keepalive / tcp_user_timeout settings; a healthy request is not slowed down by any of this
  * checkout thread bound (audit fix, 2026-09-24): a sustained hang leaks at most erp.db.READ_CHECKOUT_MAX_INFLIGHT
    permanently-blocked threads, never one per read_connect() call - a further call once every slot is stuck fails
    fast on its own timeout instead of spawning another thread; healthy concurrent checkouts are unaffected; and
    (second audit pass, same date) once the real bound IS exhausted by frozen checkouts, a request against a
    database that then genuinely recovers still gets an honest fast failure first, then heals itself within a few
    seconds via the background recovery probe - no restart needed

Read-only: the only database use is SELECT / set_config on a READ ONLY transaction. Nothing is written or started.

Run (from the project root):
    venv\\Scripts\\python.exe -B -m tests.today_scenarios
"""
from __future__ import annotations

import contextlib
import copy
import socket
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from datetime import time as dtime
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SLA_LINE = "Numbers may be incomplete: "
WEBHOOK_CLAUSE = "new leads only arrive while the webhook is running (it is offline)"
PULL_CLAUSE = "replies are only read from ClickUp when the pull job is scheduled (it is not)"
STATUS_KEYS = {"level", "lead", "sep", "rest", "headline", "sub", "reasons", "blind", "caveat", "blind_inputs"}


# ------------------------------------------------------------------------------------------- synthetic inputs
def raw_ok(new: int = 3, overdue: int = 0, t_overdue: int = 0, *, total: int = 10, overdue_yesterday: int | None = None,
           replies: int = 0, replied_leads: int | None = None, with_reply: int | None = None, tz: str = "Asia/Bangkok",
           t_opened: int = 0, t_resolved: int = 0) -> dict:
    """A healthy set of query results. The keyword arguments move only what a scenario needs to move."""
    days = [f"2026-09-{15 + i:02d}" for i in range(7)]
    y = overdue if overdue_yesterday is None else overdue_yesterday
    return {
        "leads": {"days": days, "new": [0] * 6 + [new], "overdue": [0] * 5 + [y, overdue], "awaiting": [0] * 6 + [overdue],
                  "worst_late_s": 7200 if overdue else None, "oldest_wait_s": 7200 if overdue else None, "total": total, "today": days[-1]},
        "tickets": {"days": days, "opened": [0] * 6 + [t_opened], "resolved": [0] * 6 + [t_resolved], "open": [0] * 6 + [t_overdue],
                    "total": t_overdue + t_opened, "overdue": t_overdue, "stale": 0, "worst_late_s": 3600 if t_overdue else None},
        "attn_leads": [], "attn_tickets": [], "pull": {"at": None},
        "changes": {"replies_today": replies, "leads_replied_today": replies if replied_leads is None else replied_leads,
                    "leads_with_reply": replies if with_reply is None else with_reply, "tz": tz},
    }


# ------------------------------------------------------------------------- synthetic rows for the drawer
# Same column names as DETAIL_LEADS_SQL / DETAIL_TICKETS_SQL, so _lead_item / _ticket_item are exercised for real.
NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


def lead_row(list_: str, id_: int = 1, ord_: int = 1, *, late_h: float | None = 3.0, reply_h: float | None = None,
             ai: bool = True, note: bool = True, rep: str | None = "Mai Linh", sync: str | None = "synced") -> dict:
    """One row of DETAIL_LEADS_SQL. late_h = hours the deadline is already past (negative = still to come, None = no deadline)."""
    came = NOW - timedelta(hours=9)
    due = None if late_h is None else NOW - timedelta(hours=late_h)
    return {
        "list": list_, "ord": ord_, "id": id_, "full_name": f"Lead {id_}", "company": "Acme Ltd", "source": "web_form",
        "created_at": came, "sla_due_at": due, "rep": rep,
        "age_s": int((NOW - came).total_seconds()),
        "late_s": None if due is None else int((NOW - due).total_seconds()),
        "first_reply_at": None if reply_h is None else came + timedelta(hours=reply_h),
        "clickup_task_id": "abc123" if sync else None, "sync_status": sync,
        "last_synced_at": came if sync else None, "last_comment_pulled_at": NOW - timedelta(hours=1) if sync else None,
        "model_name": "deepseek-chat" if ai else None, "potential_score": 72 if ai else None,
        "scale_estimate": "mid-market" if ai else None, "organization_type": "manufacturer" if ai else None,
        "ai_notes": "Buys in volume, asked for a quote twice." if ai else None,
        "analyzed_at": came + timedelta(minutes=2) if ai else None,
        "note": "Called, will decide next week." if note else None, "note_by": "Mai Linh" if note else None,
        "note_source": "clickup_comment" if note else None,
        "note_at": NOW - timedelta(hours=2) if note else None, "note_at_synced": False,
    }


def ticket_row(list_: str, code: str = "TCK-1", ord_: int = 1, *, late_h: float | None = 2.0) -> dict:
    came = NOW - timedelta(hours=30)
    due = None if late_h is None else NOW - timedelta(hours=late_h)
    return {"list": list_, "ord": ord_, "ticket_code": code, "subject": "Printer is on fire", "status": "in_progress",
            "priority": "high", "channel": "email", "created_at": came, "sla_due_at": due, "resolved_at": None, "closed_at": None,
            "assignee": "Hoang", "customer": "Acme Ltd", "category": "Hardware",
            "age_s": int((NOW - came).total_seconds()), "late_s": None if due is None else int((NOW - due).total_seconds())}


def flow_green() -> dict:
    """A Data Flow snapshot in which every door is open: webhook answering, pull scheduled, ClickUp / DeepSeek / email connected."""
    def node(id_, state="ok", triggers=None, automation="active"):
        return {"id": id_, "label": id_, "automation": automation, "triggers": triggers or [],
                "health": {"state": state, "detail": "", "last_ok_at": "2026-09-21T01:00:00Z"}}
    return {
        "db": {"ok": True, "error": None}, "probe": {"webhook": True},
        "scheduler": {"available": True, "error": None, "pending": False, "checked_at": None,
                      "tasks": [{"name": "erp pull", "keys": ["clickup_pull"], "enabled": True, "last_result": "0"}]},
        "coverage": {"not_set_up": []},
        "nodes": [node("webhook"), node("clickup_push"), node("deepseek"), node("emailer"), node("digest_files"),
                  node("clickup_pull", triggers=[{"kind": "scheduled", "armed": True}, {"kind": "manual", "armed": True}])],
    }


def flow_with(**kw) -> dict:
    """flow_green() with one door closed. webhook_down, pull=<not_scheduled|checking|unreadable|failing>, clickup_off."""
    f = copy.deepcopy(flow_green())
    nodes = {n["id"]: n for n in f["nodes"]}
    if kw.get("webhook_down"):
        f["probe"]["webhook"] = False
    pull = kw.get("pull")
    if pull == "not_scheduled":
        f["scheduler"]["tasks"] = []
        for t in nodes["clickup_pull"]["triggers"]:
            if t["kind"] == "scheduled":
                t["armed"] = False
    elif pull == "checking":
        f["scheduler"].update(available=False, pending=True, tasks=[])
        for t in nodes["clickup_pull"]["triggers"]:
            if t["kind"] == "scheduled":
                t["armed"] = None
    elif pull == "unreadable":
        f["scheduler"].update(available=False, pending=False, error="schtasks is not available on this system", tasks=[])
        for t in nodes["clickup_pull"]["triggers"]:
            if t["kind"] == "scheduled":
                t["armed"] = None
    elif pull == "failing":
        f["scheduler"]["tasks"][0]["last_result"] = "1"
    if kw.get("clickup_off"):
        nodes["clickup_push"]["health"]["state"] = "mocked"
    if kw.get("clickup_red"):
        nodes["clickup_push"]["health"]["state"] = "error"
    return f


def assemble(raw, flow, db_error=None):
    from desktop import today_data as td
    return td.assemble(raw, flow, db_error)


# ------------------------------------------------------------------------------------------- the table
def _check_status_scenarios(add) -> None:
    from erp import config
    sla = f"{config.LEAD_SLA_HOURS:g}-business-hour"

    p = assemble(raw_ok(3), flow_green())["status"]
    add("all green -> level ok, 'All good', no caveat, not blind",
        p["level"] == "ok" and p["headline"] == "All good - 3 new leads today, none overdue" and p["caveat"] is None and p["blind"] is False, p["headline"])
    add("status has one fixed set of keys", set(p) == STATUS_KEYS, sorted(set(p) ^ STATUS_KEYS))

    p = assemble(raw_ok(0), flow_green())["status"]
    add("all green, no leads -> 'no new leads yet today'", p["headline"] == "All good - no new leads yet today, none overdue", p["headline"])

    p = assemble(raw_ok(3), flow_with(webhook_down=True))["status"]
    add("blind: webhook offline -> 'No problems found, but ...', level watch, caveat names the webhook",
        p["level"] == "watch" and p["headline"].startswith("No problems found, but the numbers may be incomplete (3 new leads today, none overdue)")
        and p["caveat"] == SLA_LINE + WEBHOOK_CLAUSE + "." and p["blind"] and p["blind_inputs"] == ["webhook"] and "All good" not in p["headline"],
        (p["headline"], p["caveat"]))

    p = assemble(raw_ok(3), flow_with(pull="not_scheduled"))["status"]
    add("blind: pull not scheduled -> caveat names the pull job", p["level"] == "watch" and p["caveat"] == SLA_LINE + PULL_CLAUSE + ".", p["caveat"])

    p = assemble(raw_ok(3), flow_with(webhook_down=True, pull="not_scheduled"))["status"]
    add("blind: both -> the owner's example sentence, word for word",
        p["caveat"] == "Numbers may be incomplete: new leads only arrive while the webhook is running (it is offline) and replies are only "
                       "read from ClickUp when the pull job is scheduled (it is not).", p["caveat"])

    for kind, frag in (("checking", "still being checked"), ("unreadable", "could not be read"), ("failing", "last scheduled run reported an error")):
        p = assemble(raw_ok(3), flow_with(pull=kind))["status"]
        add(f"blind: pull {kind} -> level watch and the caveat says why", p["level"] == "watch" and frag in (p["caveat"] or ""), p["caveat"])

    p = assemble(raw_ok(3), flow_with(clickup_off=True))["status"]
    add("blind: ClickUp not connected -> caveat about replies, no duplicate pull clause",
        p["level"] == "watch" and "once it is connected" in (p["caveat"] or "") and "pull job" not in (p["caveat"] or ""), p["caveat"])

    p = assemble(raw_ok(3), None)["status"]
    add("blind: health feed not ready (no flow) -> never a bare 'All good'",
        p["level"] == "watch" and p["headline"].startswith("No problems found, but") and p["blind_inputs"] == ["checks"], p["headline"])

    good = assemble(raw_ok(3, overdue=2), flow_green())["status"]
    p = assemble(raw_ok(3, overdue=2), flow_with(webhook_down=True, pull="not_scheduled"))["status"]
    add("needs attention keeps its exact text and level, blind or not",
        good["headline"] == p["headline"] == f"Needs attention - 2 leads past their {sla} SLA" and good["level"] == p["level"] == "attention", (good["headline"], p["headline"]))
    add("needs attention + healthy inputs has no caveat; blind adds it and says leads may already be answered",
        good["caveat"] is None and p["caveat"] and "may already have been answered in ClickUp" in p["caveat"], p["caveat"])
    p = assemble(raw_ok(3, overdue=1, t_overdue=1), flow_with(webhook_down=True))["status"]
    add("attention with only the webhook blind: caveat has no 'already answered' claim", "already" not in (p["caveat"] or ""), p["caveat"])

    p = assemble({}, flow_green(), "OperationalError: could not connect")["status"]
    add("database down -> level unknown, never 'All good', no caveat (the sub-line already explains)",
        p["level"] == "unknown" and p["headline"].startswith("Can’t read today’s numbers") and p["caveat"] is None and p["blind"], p["headline"])
    p = assemble({"leads": {"error": "x"}, "tickets": {"error": "y"}}, flow_green())["status"]
    add("both DB blocks failed (no db_error) -> level unknown", p["level"] == "unknown", p["level"])

    raw = raw_ok(3)
    raw["tickets"] = {"error": "relation missing"}
    p = assemble(raw, flow_with(webhook_down=True))["status"]
    add("partly unavailable keeps its text, gets the caveat", p["level"] == "watch" and p["lead"] == "Partly unavailable" and p["caveat"], p["headline"])

    p = assemble(raw_ok(3), flow_with(clickup_red=True))["status"]
    add("red health keeps 'Mostly fine' when the doors are open", p["lead"] == "Mostly fine" and p["caveat"] is None, p["headline"])
    p = assemble(raw_ok(3), flow_with(clickup_red=True, webhook_down=True))["status"]
    add("red health + blind: still 'Mostly fine' and the caveat is added", p["lead"] == "Mostly fine" and bool(p["caveat"]), p["headline"])

    p = assemble(raw_ok(3), flow_with(webhook_down=True))
    add("payload: caveat lives at status.caveat and health jobs cell carries the pull state",
        "caveat" in p["status"] and any(c["id"] == "jobs" and "pull" in c for c in p["health"]), [c["id"] for c in p["health"]])


# ------------------------------------------------------------------------------------------- "since yesterday"
def _check_since_scenarios(add) -> None:
    s = assemble(raw_ok(2, replies=1, with_reply=1), flow_green())["since"]
    add("since: the owner's example shape - new leads, the reply, and the SLA verdict in one plain line",
        s["available"] and s["text"] == "Since yesterday: 2 new leads, 1 reply came in from ClickUp, nothing new went past its SLA."
        and s["note"] is None, (s["text"], s["note"]))
    add("since: names the database's own time zone, so 'today' is not ambiguous (L-097)",
        s["tz"] == "Asia/Bangkok" and "Asia/Bangkok" in s["day_note"], s["day_note"])

    s = assemble(raw_ok(2, replies=1), flow_with(pull="not_scheduled"))["since"]
    add("since: a blind ClickUp pull replaces the reply clause with a note - never a zero",
        all(p["id"] != "replies" for p in s["parts"]) and "can't be counted" in (s["note"] or "")
        and "no replies" not in (s["text"] or ""), (s["text"], s["note"]))

    s = assemble(raw_ok(2), flow_with(clickup_off=True))["since"]
    add("since: ClickUp not connected at all is the same honest note", "can't be counted" in (s["note"] or ""), s["note"])

    raw = raw_ok(2)
    raw["changes"] = {"error": "QueryCanceled"}
    s = assemble(raw, flow_green())["since"]
    add("since: only the replies query failed -> its clause becomes 'could not be counted', the others survive",
        "could not be counted" in (s["note"] or "") and any(p["id"] == "new_leads" for p in s["parts"]), (s["text"], s["note"]))

    s = assemble(raw_ok(4, total=4), flow_green())["since"]
    add("since: nothing on file from before today -> silent, with a reason, rather than a guess",
        s["available"] is False and s["text"] is None and "nothing to compare" in (s["reason"] or ""), (s["available"], s["reason"]))

    s = assemble(raw_ok(3, overdue=3, overdue_yesterday=1), flow_green())["since"]
    add("since: leads that went past SLA today are counted against the end of yesterday, and the clause is clickable",
        any(p["id"] == "past_sla" and p["text"] == "2 more leads went past their SLA" and p["tone"] == "bad" for p in s["parts"]),
        [p["text"] for p in s["parts"]])
    s = assemble(raw_ok(3, overdue=1, overdue_yesterday=3), flow_green())["since"]
    add("since: fewer past SLA than last night reads as good news, in words",
        any(p["id"] == "past_sla" and p["text"] == "2 leads fewer are past SLA than last night" and p["tone"] == "good" for p in s["parts"]),
        [p["text"] for p in s["parts"]])

    s = assemble(raw_ok(1, t_opened=2, t_resolved=1), flow_green())["since"]
    add("since: tickets appear only when tickets are on file, and read as plain English",
        any(p["id"] == "tickets" and p["text"] == "2 tickets came in and 1 was resolved" for p in s["parts"]), [p["text"] for p in s["parts"]])
    s = assemble(raw_ok(1), flow_green())["since"]
    add("since: an empty tickets table adds no clause at all", all(p["id"] != "tickets" for p in s["parts"]), [p["id"] for p in s["parts"]])

    p = assemble({}, flow_green(), "OperationalError: could not connect")
    add("since: database down -> no line, and no crash", p["since"]["available"] is False and p["since"]["text"] is None, p["since"]["reason"])

    # a clause is only clickable when the list behind it really came back with rows (no dead ends)
    raw = raw_ok(2, replies=1, with_reply=1)
    s = assemble(raw, flow_green())["since"]
    add("since: without drawer rows no clause claims a list to open",
        all(p["detail"] is None for p in s["parts"]), [(p["id"], p["detail"]) for p in s["parts"]])
    raw["detail_leads"] = [lead_row("new_leads", 1), lead_row("replied_today", 7, reply_h=1)]
    s = assemble(raw, flow_green())["since"]
    add("since: with rows behind them, the new-leads and reply clauses become clickable",
        {p["id"]: p["detail"] for p in s["parts"]}.get("new_leads") == "new_leads"
        and {p["id"]: p["detail"] for p in s["parts"]}.get("replies") == "replied_today",
        [(p["id"], p["detail"]) for p in s["parts"]])


# ------------------------------------------------------------------------------------------- "Needs a human" repeated-phrase hoist
def _check_attention_hoist(add) -> None:
    """build_attention()'s all_same_what path (lesson L-145): when every row in the attention list needs the same
    thing, the phrase is hoisted once above the list instead of being repeated on every row - a mixed list or a
    single row keeps each row's own phrase in place, since there is nothing to hoist."""
    from desktop import today_data as td
    rows = [{"id": i, "full_name": f"Lead {i}", "company": None, "source": "web_form", "rep": "Mai Linh",
             "late_s": 3600 * i} for i in (1, 2, 3)]
    a = td.build_attention({"leads": {"overdue": [3], "new": [0]}, "tickets": {"overdue": 0, "stale": 0},
                            "attn_leads": rows, "attn_tickets": []})
    add("every row sharing the same phrase -> it is hoisted once",
        a["all_same_what"] == a["items"][0]["what"] and len({i["what"] for i in a["items"]}) == 1, a["all_same_what"])
    mixed = td.build_attention({"leads": {"overdue": [1], "new": [0]}, "tickets": {"overdue": 1, "stale": 0},
                                "attn_leads": rows[:1],
                                "attn_tickets": [{"ticket_code": "T-1", "subject": "x", "assignee": "Nam",
                                                  "priority": "high", "customer": None, "late_s": 60, "age_s": 9000}]})
    add("a mixed list (rows in different states) -> nothing is hoisted, each row keeps its own phrase",
        mixed["all_same_what"] is None, mixed["all_same_what"])
    one = td.build_attention({"leads": {"overdue": [1], "new": [0]}, "tickets": {"overdue": 0, "stale": 0},
                              "attn_leads": rows[:1], "attn_tickets": []})
    add("a single-row list -> nothing to hoist, the row keeps its own phrase", one["all_same_what"] is None, one["all_same_what"])
    js = (ROOT / "desktop" / "static" / "today.js").read_text(encoding="utf-8")
    add("today.js uses the server's verdict instead of deciding it again", "a.all_same_what" in js, "missing")


# ------------------------------------------------------------------------------------------- the detail drawer
def _check_drawer_scenarios(add) -> None:
    from desktop import sla_words as W
    from desktop import today_data as td

    raw = raw_ok(1, overdue=2, total=12, with_reply=3)
    raw["detail_leads"] = [lead_row("overdue_leads", 1, 1), lead_row("overdue_leads", 2, 2, ai=False, note=False, rep=None, sync=None),
                           lead_row("awaiting", 1, 1), lead_row("new_leads", 3, 1, late_h=-2.0),
                           lead_row("replied", 4, 1, late_h=6.0, reply_h=5.0, note=True)]
    raw["detail_tickets"] = [ticket_row("attention_tickets", "TCK-1", 1)]
    d = assemble(raw, flow_green())
    lists, items = d["details"]["lists"], d["details"]["items"]

    # backwards compatibility: the page gained keys, it never lost or renamed one (the only consumers are static/today.js
    # and smoke check 4, and both read these names)
    old_keys = {"ok", "generated_at", "day", "day_label", "status", "kpis", "attention", "health", "job", "sources", "rules"}
    add("payload: every key the feed had before is still there, with the same shapes",
        old_keys <= set(d) and isinstance(d["kpis"], list) and isinstance(d["rules"], list)
        and {"details", "since", "tz"} <= set(d), sorted(old_keys - set(d)))
    add("payload: a tile still has every field the renderer reads, plus the new 'detail'",
        all({"id", "label", "hint", "state", "value", "sub", "delta", "spark", "spark_kind", "tone", "detail"} <= set(t)
            for t in d["kpis"]), [sorted(t) for t in d["kpis"][:1]])

    add("drawer: travels in the same payload as the tiles (one poller, nothing fetched on open)",
        "details" in d and set(lists) >= {"new_leads", "overdue_leads", "awaiting", "replied", "replied_today",
                                          "open_tickets", "tickets_today", "attention_tickets"}, sorted(lists))
    add("drawer: a lead in two lists is stored once and pointed at twice",
        len(items) == 5 and [r["ref"] for r in lists["awaiting"]["rows"]] == ["lead-1"]
        and [r["ref"] for r in lists["overdue_leads"]["rows"]] == ["lead-1", "lead-2"], (len(items), sorted(items)))
    add("drawer: a tile only invites a click when its list really has rows",
        {t["id"]: t["detail"] for t in d["kpis"]}["overdue_leads"] == "overdue_leads"
        and {t["id"]: t["detail"] for t in d["kpis"]}["open_tickets"] is None, [(t["id"], t["detail"]) for t in d["kpis"]])

    it = items["lead-1"]
    facts = {f["k"]: f for f in it["facts"]}
    add("drawer, lead WITH an AI analysis: score, model and summary are all there",
        it["ai"] and it["ai"]["score"] == 72 and it["ai"]["model"] == "deepseek-chat" and "volume" in it["ai"]["notes"], it["ai"])
    add("drawer, lead with a ClickUp note: the note, who wrote it and which stamp it is",
        it["note"] and it["note"]["by"] == "Mai Linh" and "written in ClickUp" in it["note"]["stamp"], it["note"])
    add("drawer: the ClickUp task links out only when the task really exists and is synced",
        it["clickup"]["url"] == "https://app.clickup.com/t/abc123" and it["clickup"]["tone"] == "ok", it["clickup"])
    add("drawer: who owns it, when it arrived, the deadline and how late - in words, not timestamps",
        facts["rep"]["value"] == "Mai Linh" and facts["due"]["value"] == "passed 3 h ago" and facts["due"]["tone"] == "bad"
        and facts["reply"]["value"] == W.NO_REPLY_YET, {k: v["value"] for k, v in facts.items()})
    add("drawer: the SLA outcome is worded by the shared module, so Today and Leads agree",
        it["outcome"]["label"] == W.LABEL["past_sla"] and it["outcome"]["what"] == W.ROW_WHAT["past_sla"], it["outcome"])
    sql_text = td.DETAIL_LEADS_SQL.lower()
    add("drawer: no private column is even selected (no e-mail, phone or raw payload) and the panel says so",
        not any(c in sql_text for c in ("l.email", "l.phone", "raw_payload")) and "privacy" in it
        and set(it) == {"ref", "kind", "eyebrow", "title", "outcome", "badge", "facts", "ai", "ai_none", "clickup",
                        "clickup_none", "note", "note_none", "privacy"}, sorted(set(it)))

    it2 = items["lead-2"]
    add("drawer, lead WITHOUT an AI analysis: an honest sentence instead of an empty card",
        it2["ai"] is None and "No AI analysis is on record" in it2["ai_none"], it2["ai_none"])
    add("drawer, lead with no note: says only what the database proves (L-086)",
        it2["note"] is None and "not proof that nobody called" in it2["note_none"], it2["note_none"])
    add("drawer, lead with no ClickUp row and no rep: both are stated, not left blank",
        it2["clickup"] is None and {f["k"]: f for f in it2["facts"]}["rep"]["tone"] == "warn", it2["clickup_none"])

    it3 = items["lead-3"]
    add("drawer: a lead still inside its deadline says when the deadline is, not how late it is",
        it3["outcome"]["key"] == "pending" and it3["badge"] == "deadline in 2 h", (it3["outcome"]["key"], it3["badge"]))
    it4 = items["lead-4"]
    add("drawer: a lead answered after the deadline is 'answered late', not 'past SLA'",
        it4["outcome"]["key"] == "late" and it4["outcome"]["label"] == W.LABEL["late"], it4["outcome"])

    tk = items["ticket-TCK-1"]
    add("drawer, ticket: assignee, customer, status and how late, and no AI / ClickUp claim",
        tk["kind"] == "ticket" and tk["ai"] is None and "only done for leads" in tk["ai_none"]
        and {f["k"]: f["value"] for f in tk["facts"]}["who"] == "Acme Ltd", [f["k"] for f in tk["facts"]])

    add("drawer: the awaiting list offers the other half of the question (leads already answered)",
        lists["awaiting"]["related"] == {"id": "replied", "text": "3 other leads already have a reply on record"},
        lists["awaiting"]["related"])
    add("drawer: a list says how many rows it is not showing", lists["overdue_leads"]["total"] == 2 and lists["overdue_leads"]["more"] == 0
        and lists["overdue_leads"]["cap"] == td.DETAIL_ROWS, lists["overdue_leads"])

    # per-panel degradation: the drawer query fails, the numbers above it do not
    bad = copy.deepcopy(raw)
    bad["detail_leads"] = {"error": "QueryCanceled: canceling statement due to statement timeout"}
    d2 = assemble(bad, flow_green())
    add("drawer: a failed drawer query takes down only the lead lists, with the reason",
        d2["details"]["lists"]["overdue_leads"]["available"] is False and "QueryCanceled" in d2["details"]["lists"]["overdue_leads"]["error"]
        and d2["details"]["lists"]["attention_tickets"]["available"] is True, d2["details"]["lists"]["overdue_leads"]["error"])
    add("drawer: ... the tiles keep their numbers and simply stop offering a click",
        {t["id"]: t["value"] for t in d2["kpis"]}["overdue_leads"] == 2
        and all(t["detail"] is None for t in d2["kpis"] if t["id"] in ("overdue_leads", "awaiting", "new_leads")),
        [(t["id"], t["value"], t["detail"]) for t in d2["kpis"]])
    add("drawer: a related link is never offered to a list that could not be read",
        d2["details"]["lists"]["awaiting"]["related"] is None, d2["details"]["lists"]["awaiting"]["related"])

    # one malformed row must not empty the whole drawer (L-044)
    broken = copy.deepcopy(raw)
    broken["detail_leads"] = [{"list": "overdue_leads", "ord": 1}] + [lead_row("overdue_leads", 9, 2)]
    d3 = assemble(broken, flow_green())
    add("drawer: one malformed row is skipped, the rest of the list still renders (L-044)",
        [r["ref"] for r in d3["details"]["lists"]["overdue_leads"]["rows"]] == ["lead-9"],
        [r["ref"] for r in d3["details"]["lists"]["overdue_leads"]["rows"]])

    # a long AI summary / note is cut in the payload, not in the browser
    long_row = lead_row("overdue_leads", 5, 1)
    long_row["ai_notes"] = "x" * (td.AI_NOTES_MAX_CHARS + 50)
    long_row["note"] = "y" * (td.NOTE_MAX_CHARS + 50)
    d4 = assemble({**raw_ok(1, overdue=1), "detail_leads": [long_row]}, flow_green())
    cut = d4["details"]["items"]["lead-5"]
    add("drawer: an over-long AI summary and note are cut server-side and say so",
        cut["ai"]["notes_cut"] and cut["note"]["cut"] and len(cut["ai"]["notes"]) <= td.AI_NOTES_MAX_CHARS + 3, len(cut["ai"]["notes"]))

    empty = assemble({}, flow_green(), "OperationalError")["details"]
    add("drawer: database down -> every list unavailable with a reason, no crash",
        all(not spec["available"] for spec in empty["lists"].values()) and not empty["items"], list(empty["lists"])[:2])


def _check_drawer_against_real_row(add, db_ok: bool) -> None:
    """The one real lead_updates row in this database, rendered through the real SQL (read-only)."""
    if not db_ok:
        add("SKIPPED (database unreachable): the real ClickUp note renders in the drawer", True, "")
        return
    from sqlalchemy import text as sql
    from desktop import today_data as td
    from erp.db import engine
    with engine.connect().execution_options(postgresql_readonly=True) as conn:
        td.arm_timeouts(conn)
        n = conn.execute(sql("SELECT count(*) FROM lead_updates")).scalar()
        rows = td._rows(conn, td.DETAIL_LEADS_SQL)
        conn.rollback()
    if not n:
        add("SKIPPED (no lead_updates row on file): the real ClickUp note renders in the drawer", True, "")
        return
    replied = [r for r in rows if r["list"] == "replied"]
    add("real data: the leads that have a reply are reachable through the 'replied' list", bool(replied), len(replied))
    if not replied:
        return
    it = td._lead_item(replied[0])
    add("real data: the real ClickUp comment is shown as the last note, with its author and its age",
        it["note"] is not None and it["note"]["text"].strip() != "" and it["note"]["by"] and it["note"]["ago"],
        {k: it["note"][k] for k in ("by", "ago", "source")} if it["note"] else None)
    add("real data: a lead with a reply on record is never counted as 'past SLA'",
        it["outcome"]["key"] in ("on_time", "late") and {f["k"]: f["value"] for f in it["facts"]}["reply"] != "no reply on record yet",
        it["outcome"]["key"])


def _check_rules_follow_config(add) -> None:
    from desktop import today_data as td
    from erp import business_hours as bh
    from erp import config
    old = (config.LEAD_SLA_HOURS, bh.BUSINESS_START, bh.BUSINESS_END, set(bh.WORKDAYS))
    try:
        config.LEAD_SLA_HOURS = 3.5
        bh.BUSINESS_START, bh.BUSINESS_END = dtime(9, 30), dtime(18, 0)
        bh.WORKDAYS.clear()
        bh.WORKDAYS.update({0, 1, 2, 3, 4, 5})
        rules = " ".join(td.build_rules())
        hint = next(t["hint"] for t in td.build_tiles(raw_ok(3), None) if t["id"] == "overdue_leads")
        add("rules text follows LEAD_SLA_HOURS, business hours and work days", "3.5 business hours, 9:30-18:00, Mon-Sat" in rules, rules[:140])
        add("overdue tile hint follows the same config", "3.5 business hours (9:30-18:00, Mon-Sat)" in hint, hint)
        add("headline follows the same config", "3.5-business-hour" in assemble(raw_ok(3, overdue=1), flow_green())["status"]["headline"], "")
        add("rules text follows the other constants", f"more than {td.TICKET_STALE_HOURS} hours" in rules and f"last {td.DAYS} days" in rules
            and f"{td.JOB_FRESH_HOURS} hours" in rules and f"at most {td.ATTENTION_MAX} rows" in rules, "")
    finally:
        config.LEAD_SLA_HOURS, bh.BUSINESS_START, bh.BUSINESS_END = old[0], old[1], old[2]
        bh.WORKDAYS.clear()
        bh.WORKDAYS.update(old[3])
    rules = " ".join(td.build_rules())
    add("rules text is restored with the config (5 business hours, 8:00-17:00, Mon-Fri by default)",
        f"{config.LEAD_SLA_HOURS:g} business hours, 8:00-17:00, Mon-Fri" in rules or config.LEAD_SLA_HOURS != 5, rules[:100])


class _Hung:
    """A TCP listener that accepts connections and never says a word: a hung PostgreSQL, not a refused one."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.held: list[socket.socket] = []
        self._stop = False
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()

    def _loop(self) -> None:
        self.sock.settimeout(0.2)
        while not self._stop:
            try:
                c, _ = self.sock.accept()
                self.held.append(c)
            except OSError:
                continue

    def close(self) -> None:
        self._stop = True
        self._t.join(2)
        for c in self.held + [self.sock]:
            with contextlib.suppress(OSError):
                c.close()


class _FrozenProxy:
    """A TCP relay in front of the REAL database (lesson L-095): one connection does ONE GOOD round trip through it -
    so it becomes a genuinely established, previously-good, now-POOLED connection, not a fresh connect to a dead
    port (which connect_timeout already covers via `_Hung` above, and which the earlier attempt at this fix tested
    exclusively - that distinction is what made it miss the real gap). Then the proxy goes silent: it keeps BOTH
    legs' sockets open but stops relaying bytes in either direction, exactly like a hung backend or a silent
    NAT/firewall drop.

    Empirically (built and measured while writing this fix): going silent this way does NOT trip TCP keepalives on
    this machine even after 60+ seconds, because the proxy's own OS keeps acknowledging at the TCP layer regardless
    of whether its forwarding thread ever calls recv() - a keepalive probe only proves the peer's KERNEL is alive,
    not that its application ever answers back. That is exactly why erp.db.read_connect()'s wall-clock bound exists
    as the second, independent line of defence (see erp/db.py's module docstring) - it is what this scenario
    actually proves; the keepalive/tcp_user_timeout settings are checked separately, for what they are: the right
    fix for a genuinely dead network peer, which this in-process test cannot simulate without firewall access
    (Law.md rule 5)."""

    def __init__(self, real_host: str, real_port: int) -> None:
        self.real_host, self.real_port = real_host, real_port
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(8)
        self.port = self.listener.getsockname()[1]
        self.frozen = threading.Event()
        self._stop = False
        self._sockets: list[socket.socket] = []
        self._accept_t = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_t.start()

    def _accept_loop(self) -> None:
        self.listener.settimeout(0.2)
        while not self._stop:
            try:
                client, _ = self.listener.accept()
            except OSError:
                continue
            try:
                backend = socket.create_connection((self.real_host, self.real_port), timeout=3)
            except OSError:
                client.close()
                continue
            self._sockets += [client, backend]
            for a, b in ((client, backend), (backend, client)):
                threading.Thread(target=self._pump, args=(a, b), daemon=True).start()

    def _pump(self, src: socket.socket, dst: socket.socket) -> None:
        src.settimeout(0.2)
        while not self._stop:
            if self.frozen.is_set():
                time.sleep(0.2)   # simulate a hung peer: TCP session stays open, nothing is ever forwarded again
                continue
            try:
                data = src.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            if not data:
                return
            try:
                dst.sendall(data)
            except OSError:
                return

    def close(self) -> None:
        self._stop = True
        self._accept_t.join(2)
        for s in self._sockets + [self.listener]:
            with contextlib.suppress(OSError):
                s.close()


def _check_checkout_thread_bound(add) -> None:
    """Confirmed-by-audit fix (2026-09-24, strengthening L-091): the ORIGINAL read_connect() spawned a fresh,
    unbounded `threading.Thread` on every call with no cap and no reuse. A checkout thread whose peer genuinely
    never answers can never be reclaimed (there is no safe way to interrupt a blocking C-level socket call from
    another Python thread), so during a sustained outage the old code leaked one new permanently-blocked thread
    (holding an open socket) per call, forever - not "one pool slot", growing without bound for as long as nobody
    restarted the process. This is exactly the unattended scenario L-013 is about: ReportStore/FlowStore refresh on
    an always-running background timer (DESKTOP_REFRESH_SECONDS) with no browser open to notice.

    A SECOND audit pass (2026-09-24) found the fixed-size semaphore this introduced had its own gap: once every
    permit is held by a checkout that will never return, no NEW checkout can ever complete through that SAME
    semaphore to prove the database has recovered - a self-healing outage became a restart-only one. This check's
    third block proves the fix for that: with the bound genuinely exhausted by frozen checkouts (never swapped out
    for a fresh one), a request against a database that then becomes healthy still gets a real answer, without a
    restart, because of the background recovery probe in erp.db (`_maybe_start_recovery_probe`).

    In-process only, no real database or network touched: fake engines whose connect() blocks forever (or blocks
    until told to answer) stand in for a peer that never answers / a peer that recovers. Dedicated, small semaphores
    are swapped in for the module's real ones for the duration of this check (`_read_checkout_slots` AND
    `_probe_slots`), so the threads this test deliberately leaks can never consume real checkout or probe slots the
    rest of the app needs, and so a leftover probe from an earlier real outage in this same process cannot skew the
    result."""
    import erp.db as erp_db

    class _NeverAnswers:
        def connect(self):
            threading.Event().wait()  # never returns - a permanently frozen checkout

    class _Instant:
        def connect(self):
            return object()

    class _RecoversWhenTold:
        """Blocks like `_NeverAnswers` until `.healthy` is set, then answers instantly - a database that is
        genuinely hung and then genuinely recovers, without a test-side semaphore swap standing in for it."""
        def __init__(self):
            self.healthy = threading.Event()

        def connect(self):
            self.healthy.wait()
            return object()

    bound = 3  # small and dedicated to this check; independent of the real erp_db.READ_CHECKOUT_MAX_INFLIGHT
    saved_slots = erp_db._read_checkout_slots
    saved_probe_slots = erp_db._probe_slots
    saved_exhausted_since = erp_db._slots_exhausted_since
    erp_db._read_checkout_slots = threading.Semaphore(bound)
    erp_db._probe_slots = threading.Semaphore(2)
    erp_db._slots_exhausted_since = None
    try:
        eng = _NeverAnswers()
        before = sum(1 for t in threading.enumerate() if t.name == "db-read-checkout")
        calls = 5 * bound
        for _ in range(calls):
            try:
                erp_db.read_connect(eng, timeout=0.05)
            except TimeoutError:
                pass
        time.sleep(0.3)
        stuck = sum(1 for t in threading.enumerate() if t.name == "db-read-checkout") - before
        add(f"a sustained hang leaks at most the bound ({bound}) checkout threads across {calls} calls, never one per call",
            stuck == bound, f"{stuck} threads still alive and stuck")

        t0 = time.monotonic()
        err = None
        try:
            erp_db.read_connect(eng, timeout=0.3)
        except Exception as exc:  # noqa: BLE001
            err = type(exc).__name__
        took = time.monotonic() - t0
        after_extra = sum(1 for t in threading.enumerate() if t.name == "db-read-checkout") - before
        add("once every slot is stuck, one more call fails fast on its OWN timeout, without spawning yet another thread",
            err == "TimeoutError" and took < 1.0 and after_extra == bound, f"{err!r} after {took:.2f}s, {after_extra} threads total")
    finally:
        erp_db._read_checkout_slots = saved_slots  # the real global slots (and this test's own leaked ones) are left alone
        erp_db._probe_slots = saved_probe_slots
        erp_db._slots_exhausted_since = saved_exhausted_since

    # on a FRESH bound (not the one just permanently exhausted above), `bound` concurrent HEALTHY checkouts all
    # complete quickly and are not queued behind each other - proves the semaphore itself does not serialise
    # healthy traffic. This does NOT exercise the exhausted case above; the next block does that honestly.
    erp_db._read_checkout_slots = threading.Semaphore(bound)
    try:
        ok_eng = _Instant()
        results: list[float] = []

        def _worker() -> None:
            t0 = time.monotonic()
            erp_db.read_connect(ok_eng, timeout=2.0)
            results.append(time.monotonic() - t0)

        workers = [threading.Thread(target=_worker) for _ in range(bound)]
        for w in workers:
            w.start()
        for w in workers:
            w.join(3)
        add(f"on a fresh bound, {bound} concurrent HEALTHY checkouts complete quickly and are not queued behind each other",
            len(results) == bound and max(results) < 1.0, f"{len(results)} completed, max {max(results, default=0):.2f}s")
    finally:
        erp_db._read_checkout_slots = saved_slots

    # the honest case the fresh-bound check above cannot show (audit finding, 2026-09-24): with the REAL bound
    # exhausted by frozen checkouts (no test-side swap), what does a request actually get? First: a fast, honest
    # failure, never a silent hang. Then: once the database genuinely recovers, the SAME exhausted queue heals
    # itself - via the background recovery probe read_connect() starts on the exhaustion above - without a restart.
    erp_db._read_checkout_slots = threading.Semaphore(bound)
    erp_db._probe_slots = threading.Semaphore(2)
    erp_db._slots_exhausted_since = None
    try:
        stuck_eng = _NeverAnswers()
        for _ in range(bound):
            try:
                erp_db.read_connect(stuck_eng, timeout=0.05)
            except TimeoutError:
                pass
        # every one of the `bound` permits is now permanently held by a stuck checkout against THIS instance

        recovering = _RecoversWhenTold()
        t0 = time.monotonic()
        err = None
        try:
            erp_db.read_connect(recovering, timeout=0.2)  # also starts the background recovery probe (queue is full)
        except Exception as exc:  # noqa: BLE001
            err = type(exc).__name__
        took = time.monotonic() - t0
        add("with the bound exhausted by frozen checkouts, a request against what is (unknown to it) a healthy "
            "database still fails fast on its own timeout, not a silent hang",
            err == "TimeoutError" and took < 1.0, f"{err!r} after {took:.2f}s")

        recovering.healthy.set()  # the database "comes back"; the recovery probe above is waiting on exactly this
        healed = False
        t0 = time.monotonic()
        while time.monotonic() - t0 < 3.0:
            try:
                erp_db.read_connect(recovering, timeout=0.3)
                healed = True
                break
            except TimeoutError:
                time.sleep(0.05)
        add("once the database answers again, the SAME exhausted queue heals itself within a few seconds - no restart needed",
            healed, f"healed={healed} after {time.monotonic() - t0:.2f}s")
    finally:
        erp_db._read_checkout_slots = saved_slots
        erp_db._probe_slots = saved_probe_slots
        erp_db._slots_exhausted_since = saved_exhausted_since


def _check_thread_start_failure(add) -> None:
    """Auditor fix (2026-09-24, item 1 of a second, independent read-only-pool audit pass): BOTH
    `threading.Thread(...).start()` call sites in erp/db.py acquire a checkout/probe slot BEFORE starting the
    thread that releases it in a `finally` (the main checkout path at line ~225, the recovery-probe path at line
    ~156). If `.start()` itself raises - exactly the `RuntimeError: can't start new thread` exhaustion scenario
    this whole mechanism exists to survive, L-180 rule 2 - the never-started thread's `finally` never runs, so the
    just-acquired slot would leak permanently without the try/except this fix wraps around each `.start()`.

    Proven by monkeypatching `threading.Thread` itself (module-global; scoped tightly around one call each and
    always restored in `finally`, so nothing else in this single-threaded test process is affected) so `.start()`
    always raises, then directly draining each semaphore's real capacity with non-blocking acquires (releasing
    them straight back - see `_capacity()`) to prove NO permit was lost, and finally that a later real
    checkout/probe-start through the SAME semaphore still succeeds - a leaked slot would otherwise decay
    READ_CHECKOUT_MAX_INFLIGHT/READ_CHECKOUT_PROBE_MAX_STUCK one failure at a time, forever."""
    import erp.db as erp_db

    class _StartFails(threading.Thread):
        def start(self):  # never actually starts an OS thread - the exact shape of "can't start new thread"
            raise RuntimeError("can't start new thread (simulated)")

    class _Instant:
        def connect(self):
            return object()

    class _Never:
        def connect(self):
            threading.Event().wait()  # never returns - stays alive so a later enumerate() check can see the thread

    real_thread_cls = erp_db.threading.Thread

    def _capacity(sem: threading.Semaphore, cap: int) -> int:
        """How many permits `sem` actually holds right now, up to `cap` (drains them with non-blocking acquires,
        then releases every one straight back - the semaphore's own count is not otherwise inspectable)."""
        held = 0
        for _ in range(cap):
            if sem.acquire(blocking=False):
                held += 1
            else:
                break
        for _ in range(held):
            sem.release()
        return held

    # --- main checkout path: read_connect() -> the `_checkout` thread ---
    bound = 3
    saved_slots = erp_db._read_checkout_slots
    erp_db._read_checkout_slots = threading.Semaphore(bound)
    try:
        before = _capacity(erp_db._read_checkout_slots, bound)
        erp_db.threading.Thread = _StartFails
        raised = None
        try:
            erp_db.read_connect(_Instant(), timeout=0.2)
        except Exception as exc:  # noqa: BLE001
            raised = type(exc).__name__
        finally:
            erp_db.threading.Thread = real_thread_cls
        add("a .start() failure on the main checkout path raises rather than being swallowed",
            raised == "RuntimeError", raised)
        after = _capacity(erp_db._read_checkout_slots, bound)
        add(f"the checkout slot is NOT leaked when .start() raises: capacity is {bound} both before and after",
            before == bound and after == bound, f"before={before} after={after}")

        # end-to-end, real threading.Thread restored: `bound` real checkouts still all succeed - proves the pool
        # was not silently shrunk by the induced failure above (a leaked permit would strand one of these).
        ok = 0
        for _ in range(bound):
            try:
                erp_db.read_connect(_Instant(), timeout=0.5)
                ok += 1
            except Exception:  # noqa: BLE001
                pass
        add(f"a later checkout can still get all {bound} slots after the induced .start() failure",
            ok == bound, f"{ok}/{bound} succeeded")
    finally:
        erp_db.threading.Thread = real_thread_cls
        erp_db._read_checkout_slots = saved_slots

    # --- recovery-probe path: _maybe_start_recovery_probe() -> the `_run_recovery_probe` thread ---
    probe_bound = 2
    saved_probe_slots = erp_db._probe_slots
    saved_exhausted_since = erp_db._slots_exhausted_since
    erp_db._probe_slots = threading.Semaphore(probe_bound)
    erp_db._slots_exhausted_since = None
    try:
        before = _capacity(erp_db._probe_slots, probe_bound)
        erp_db.threading.Thread = _StartFails
        crashed = None
        try:
            erp_db._maybe_start_recovery_probe(_Instant())  # acquires a probe slot, then .start() raises
        except Exception as exc:  # noqa: BLE001
            crashed = f"{type(exc).__name__}: {exc}"
        finally:
            erp_db.threading.Thread = real_thread_cls
        add("a .start() failure on the recovery-probe path does not crash its caller (best-effort, logged)",
            crashed is None, crashed)
        after = _capacity(erp_db._probe_slots, probe_bound)
        add(f"the probe slot is NOT leaked when .start() raises: capacity is {probe_bound} both before and after",
            before == probe_bound and after == probe_bound, f"before={before} after={after}")

        # a later exhaustion event can still spawn a real recovery probe (real threading.Thread restored) - proves
        # the probe pool was not silently shrunk by the induced failure above. Uses a permanently-frozen fake
        # connect() so the spawned thread is still alive (and stuck holding its own real permit, deliberately
        # leaked like `_check_checkout_thread_bound`'s threads - harmless daemon threads, process exits after) when
        # checked, instead of racing a fast-completing one that could finish before the enumerate() below runs.
        before_threads = sum(1 for t in threading.enumerate() if t.name == "db-read-recovery-probe")
        erp_db._maybe_start_recovery_probe(_Never())
        time.sleep(0.2)
        after_threads = sum(1 for t in threading.enumerate() if t.name == "db-read-recovery-probe")
        add("a later exhaustion event can still spawn a real recovery-probe thread after the induced .start() failure",
            after_threads > before_threads, f"{before_threads} -> {after_threads} probe threads")
    finally:
        erp_db.threading.Thread = real_thread_cls
        erp_db._probe_slots = saved_probe_slots
        erp_db._slots_exhausted_since = saved_exhausted_since


def _check_pooled_connection_freeze(add, db_ok: bool) -> None:
    """Lesson L-091 (strengthened): the one thing a fresh-connect hang test cannot show - a connection ALREADY
    sitting in the pool, previously good, then frozen mid-life - and the read_connect() wall-clock bound that
    closes it (erp/db.py). Needs the real database; skipped otherwise."""
    if not db_ok:
        add("SKIPPED (database unreachable): pooled-connection-freeze safety (L-091)", True, "")
        return
    from sqlalchemy import text
    from erp import config as erp_config
    from erp import db as erp_db

    proxy = _FrozenProxy("127.0.0.1", int(erp_config.DB_PORT))
    try:
        url = f"postgresql+psycopg2://{erp_config.DB_USER}:{erp_config.DB_PASSWORD}@127.0.0.1:{proxy.port}/{erp_config.DB_NAME}"
        eng = erp_db.make_read_engine(url, connect_timeout=3)
        try:
            with eng.connect() as c:
                c.execute(text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001 - the proxy itself could not reach the real DB; nothing to prove here
            add("SKIPPED (could not open a connection through the proxy): pooled-connection-freeze safety", True, f"{type(exc).__name__}: {exc}")
            eng.dispose()
            return

        # the connection above is now genuinely pooled (checked in, previously good) - NOW freeze the peer. The pump
        # threads poll `frozen` with a 0.2 s recv() timeout each, so a query sent immediately after `.set()` can
        # still race one in-flight recv() through before both directions have actually stopped relaying; give that
        # up to 0.2 s a full 5x margin to drain before proving anything, or this scenario is flaky by construction.
        proxy.frozen.set()
        time.sleep(1.0)
        t0 = time.monotonic()
        err = None
        try:
            erp_db.read_connect(eng, timeout=1.5)
        except Exception as exc:  # noqa: BLE001
            err = type(exc).__name__
        took = time.monotonic() - t0
        add("an ALREADY-POOLED connection that freezes mid-life fails within read_connect()'s bound, not never",
            err == "TimeoutError" and took < 3.0, f"{err!r} after {took:.2f}s")
        eng.dispose()
    finally:
        proxy.close()

    # the keepalive / tcp_user_timeout settings really are attached to the read engine's connections (the right fix
    # for a genuinely dead network peer, which this in-process test cannot simulate - see _FrozenProxy's docstring)
    with erp_db.read_engine.connect() as c:
        dsn = c.connection.dbapi_connection.info.dsn_parameters
    add("erp.db.read_engine's connections carry the keepalive / tcp_user_timeout settings",
        all(dsn.get(k) == str(v) for k, v in erp_db.READ_KEEPALIVE_ARGS.items()), dsn)

    # a normal request against the real, healthy engine is unaffected by any of the above
    t0 = time.monotonic()
    with erp_db.read_connect(erp_db.read_engine) as c:
        c.execute(text("SELECT 1"))
    took = time.monotonic() - t0
    add("a normal (healthy) request through read_connect() is fast - the bound never slows down a working database",
        took < 2.0, f"{took:.2f}s")


def _check_hang_safety(add, db_ok: bool) -> None:
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError
    from desktop import today_data as td
    from erp import db as erp_db

    # a hung (not refused) database: connect_timeout must turn it into an honest "can't read", not a stuck request
    hung = _Hung()
    try:
        eng = erp_db.make_engine(f"postgresql+psycopg2://x:y@127.0.0.1:{hung.port}/none", connect_timeout=1)
        store = td.TodayStore(flow=None)
        saved = td.engine
        td.engine = eng
        try:
            t0 = time.monotonic()
            payload = store._build()
            took = time.monotonic() - t0
        finally:
            td.engine = saved
            eng.dispose()
        add("hung database (accepts, never answers) -> 'Can't read today's numbers' within the connect timeout",
            payload["status"]["level"] == "unknown" and payload["ok"] is False and took < 4.0, f"level={payload['status']['level']} took {took:.1f}s")
    finally:
        hung.close()

    # a request never parks behind another request's build
    store = td.TodayStore(flow=None)
    saved_wait = td.BUILD_WAIT_SECONDS
    td.BUILD_WAIT_SECONDS = 0.3
    store._lock.acquire()
    try:
        t0 = time.monotonic()
        out = store.get()
        took = time.monotonic() - t0
    finally:
        store._lock.release()
        td.BUILD_WAIT_SECONDS = saved_wait
    add("a request waiting behind a stuck build gets an honest 'busy' payload, not a parked thread",
        took < 1.5 and out["status"]["level"] == "unknown" and "busy" in out["status"]["headline"], f"took {took:.1f}s: {out['status']['headline']}")

    if not db_ok:
        add("SKIPPED (database unreachable): statement timeout / budget / connect_timeout on the shared engine", True, "")
        return

    with erp_db.engine.connect().execution_options(postgresql_readonly=True) as conn:
        info = conn.connection.dbapi_connection.info.dsn_parameters
        add("erp.db.engine opens connections with a connect_timeout", info.get("connect_timeout") == str(erp_db.DB_CONNECT_TIMEOUT), info.get("connect_timeout"))
        default_st = conn.execute(text("SHOW statement_timeout")).scalar()
        conn.rollback()
        td.arm_timeouts(conn, 150, 100)
        t0 = time.monotonic()
        err = None
        try:
            conn.execute(text("SELECT pg_sleep(3)"))
        except DBAPIError as exc:
            err = type(exc.orig).__name__
        took = time.monotonic() - t0
        conn.rollback()
        add("SET LOCAL statement_timeout cuts a slow read-only query (150 ms cap on pg_sleep(3))", err == "QueryCanceled" and took < 1.5, f"{err} after {took:.2f}s")
        after = conn.execute(text("SHOW statement_timeout")).scalar()
        conn.rollback()
        add("the timeout does not leak: it ends with the transaction (pooled connection keeps the server default)", after == default_st, f"{default_st} -> {after}")

        # after a timeout the next block must still run (rollback dropped the SET LOCAL, so collect re-arms it)
        raw = td.collect(conn, budget_seconds=5.0)
        conn.rollback()
        add("collect() still answers after a rolled-back timeout", not td._failed(raw.get("leads")) and not td._failed(raw.get("tickets")), {k: ("err" if td._failed(v) else "ok") for k, v in raw.items()})
        starved = td.collect(conn, budget_seconds=0.0)
        conn.rollback()
        add("an used-up query budget skips every block ('too slow') instead of queueing",
            all(td._failed(v) and "too slow" in v["error"] for v in starved.values()), {k: str(v)[:40] for k, v in starved.items()})
        st = td.build_status(starved, td.build_health(None, None), None)
        add("... and the headline is then 'Can't read today's numbers'", st["level"] == "unknown", st["headline"])


def run(db_ok: bool = True) -> list[tuple[str, bool, object]]:
    """All scenarios. Returns [(name, passed, detail)]; never raises (a crash is one failed row)."""
    import logging
    rows: list[tuple[str, bool, object]] = []

    def add(name: str, ok, detail=None) -> None:
        rows.append((name, bool(ok), detail if not ok else ""))

    lg = logging.getLogger("erp_desk.today")           # the scenarios provoke warnings on purpose: keep them off the console
    old_disabled, lg.disabled = lg.disabled, True
    try:
        for fn in (_check_status_scenarios, _check_since_scenarios, _check_attention_hoist, _check_drawer_scenarios,
                   _check_rules_follow_config, _check_checkout_thread_bound, _check_thread_start_failure):
            try:
                fn(add)
            except Exception as exc:  # noqa: BLE001
                add(f"{fn.__name__} crashed", False, f"{type(exc).__name__}: {exc}")
        for fn2 in (_check_drawer_against_real_row, _check_hang_safety, _check_pooled_connection_freeze):
            try:
                fn2(add, db_ok)
            except Exception as exc:  # noqa: BLE001
                add(f"{fn2.__name__} crashed", False, f"{type(exc).__name__}: {exc}")
    finally:
        lg.disabled = old_disabled
        from desktop import today_data as td
        td._logged.clear()                             # forget the provoked errors so the real feed logs its own first failure
    return rows


def main() -> int:
    try:
        from erp.db import engine
        from sqlalchemy import text
        with engine.connect() as c:
            c.execute(text("SELECT 1"))
        db_ok = True
    except Exception:  # noqa: BLE001
        db_ok = False
    rows = run(db_ok)
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
