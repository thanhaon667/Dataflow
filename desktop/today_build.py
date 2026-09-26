"""Today page, panel builders: KPI tiles, last job, the attention list, the since-yesterday line and the detail drawer (split out of desktop/today_data.py, unchanged).
"""
from __future__ import annotations

from datetime import datetime, timezone

from desktop import sla_words as W
from desktop.flow_data import _ago, _iso, _redact
from erp import business_hours
from desktop.today_util import _delta, _failed, _i, _log_once, _n, _span
from desktop.today_sql import ATTENTION_MAX, DETAIL_ROWS, TICKET_STALE_HOURS


JOB_FRESH_HOURS = 26           # a daily job that last ran less than this long ago counts as "on time"

NOTE_MAX_CHARS = 700           # a ClickUp note longer than this is cut in the payload (the drawer says so)
AI_NOTES_MAX_CHARS = 700       # ... same for the AI summary text
CLICKUP_TASK_URL = "https://app.clickup.com/t/"           # + clickup_task_id; the only link the drawer offers

# ============================================================================ page model (pure functions)
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _sla_hours() -> float:
    """Read at call time from erp.config, exactly like erp/leads.py does when it stores sla_due_at (lesson L-085)."""
    from erp import config
    return float(config.LEAD_SLA_HOURS)


def _sla_window() -> str:
    """'8:00-17:00, Mon-Fri' from erp/business_hours.py - the module that really computes the deadline."""
    clock = lambda t: f"{t.hour}:{t.minute:02d}"  # noqa: E731
    days = sorted(business_hours.WORKDAYS)
    span = (f"{_WEEKDAYS[days[0]]}-{_WEEKDAYS[days[-1]]}" if len(days) > 1 and days == list(range(days[0], days[-1] + 1))
            else ", ".join(_WEEKDAYS[d] for d in days))
    return f"{clock(business_hours.BUSINESS_START)}-{clock(business_hours.BUSINESS_END)}, {span}"


def _tile(id_: str, label: str, hint: str, **kw) -> dict:
    t = {"id": id_, "label": label, "hint": hint, "state": "ok", "value": None, "value2": None, "label2": None, "text": None,
         "sub": "", "delta": None, "spark": None, "spark_days": None, "spark_kind": "line", "tone": "blue", "unit": None,
         "detail": None}    # "detail" = the id of the drawer list this tile opens, or None when it counts nothing clickable
    t.update(kw)
    return t


def _unavailable(id_: str, label: str, hint: str, why: str) -> dict:
    return _tile(id_, label, hint, state="unavailable", sub=why, tone="grey")


def build_tiles(raw: dict, job: dict | None) -> list[dict]:
    L, T = raw.get("leads"), raw.get("tickets")
    tiles: list[dict] = []
    h_new = "Leads that reached the database since midnight. The line is the last 7 days, newest on the right."
    h_over = W.hint_past_sla(f"{_sla_hours():g} business hours ({_sla_window()})")     # same text on the Leads page
    h_open = "Support tickets that are open, in progress or pending, now. The line is how many were open at the end of each of the last 7 days."
    h_flow = "Tickets that came in today and tickets that were resolved or closed today. The line is tickets opened per day."
    h_wait = W.hint_awaiting()                                                         # same text on the Leads page
    h_job = ("The most recent of: the daily digest being written, and comments being pulled from ClickUp. It says when a job last "
             "ran, not whether it will run again - see 'Scheduled jobs' below.")

    if _failed(L):
        why = "Could not read the leads table"
        tiles += [_unavailable("new_leads", "New leads today", h_new, why), _unavailable("overdue_leads", W.TILE_PAST_SLA, h_over, why)]
    else:
        new = L["new"]
        y = new[-2] if len(new) > 1 else None
        tiles.append(_tile("new_leads", "New leads today", h_new, value=new[-1], tone="blue", spark=new, spark_days=L["days"], spark_kind="bars",
                           sub="none yet today" if new[-1] == 0 else "since midnight", detail="new_leads" if new[-1] else None,
                           delta=None if y is None else {"text": f"yesterday {y}", "dir": "flat", "good": None}))
        od = L["overdue"]
        tiles.append(_tile("overdue_leads", W.TILE_PAST_SLA, h_over, value=od[-1], tone="red" if od[-1] else "green",
                           spark=od, spark_days=L["days"], delta=_delta(od, True), detail="overdue_leads" if od[-1] else None,
                           sub=f"worst: {_span(L['worst_late_s'])} late" if od[-1] else W.SUB_NONE_PAST_SLA))

    if _failed(T):
        why = "Could not read the tickets table"
        tiles += [_unavailable("open_tickets", "Open tickets", h_open, why), _unavailable("tickets_today", "Tickets today", h_flow, why)]
    else:
        empty = T["total"] == 0
        op = T["open"]
        tiles.append(_tile("open_tickets", "Open tickets", h_open, value=op[-1], spark=op, spark_days=T["days"],
                           tone="red" if T["overdue"] else "blue", delta=None if empty else _delta(op, True),
                           detail="open_tickets" if op[-1] else None,
                           sub="no tickets on file yet" if empty else (f"{_n(T['overdue'], 'ticket')} past SLA" if T["overdue"] else "none past SLA")))
        o, r = T["opened"][-1], T["resolved"][-1]
        net = r - o
        sub = ("nothing came in or went out" if o == r == 0 else "backlog is shrinking" if net > 0 else
               "backlog is growing" if net < 0 else "in and out are even")
        tiles.append(_tile("tickets_today", "Tickets today", h_flow, value=o, unit="opened", value2=r, label2="resolved",
                           tone="violet", spark=T["opened"], spark_days=T["days"], spark_kind="bars", detail="tickets_today" if o else None,
                           sub="no tickets on file yet" if empty else sub))

    if _failed(L):
        tiles.append(_unavailable("awaiting", W.TILE_AWAITING, h_wait, "Could not read the leads table"))
    else:
        aw = L["awaiting"]
        tiles.append(_tile("awaiting", W.TILE_AWAITING, h_wait, value=aw[-1], tone="amber" if aw[-1] else "green",
                           spark=aw, spark_days=L["days"], delta=_delta(aw, True), detail="awaiting" if aw[-1] else None,
                           sub=f"oldest has waited {_span(L['oldest_wait_s'])}" if aw[-1] else ("no leads on file yet" if not L["total"] else W.all_replied())))

    if job is None:
        tiles.append(_unavailable("last_job", "Last job run", h_job, "Could not read the job history"))
    else:
        tiles.append(_tile("last_job", "Last job run", h_job, text=job["ago"], tone=job["tone"], sub=job["sub"]))
    return tiles


def last_job(raw: dict, flow: dict | None) -> dict | None:
    """Newest sign of a background job: the digest file (via the flow snapshot) or the last ClickUp comment pull."""
    cands: list[tuple[str, str]] = []
    nodes = {n["id"]: n for n in (flow or {}).get("nodes", [])} if flow else {}
    d = nodes.get("digest_files", {}).get("health", {}).get("last_ok_at")
    if d:
        cands.append((d, "Daily digest written"))
    pull = raw.get("pull")
    if isinstance(pull, dict) and pull.get("at"):
        cands.append((pull["at"], "ClickUp comments pulled"))
    if _failed(pull) and not flow:
        return None
    if not cands:
        return {"ago": "never", "at": None, "tone": "grey", "sub": "no digest written and no comments pulled yet"}
    at, what = max(cands)
    age_h = None
    try:
        age_h = (datetime.now(timezone.utc) - datetime.strptime(at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)).total_seconds() / 3600
    except ValueError:
        pass
    sched = {n: nodes.get(n, {}).get("automation") for n in ("daily_digest", "clickup_pull")}
    armed = any(v in ("active", "partial") for v in sched.values())
    note = "" if armed or not flow else " · not on a schedule"
    return {"ago": _ago(at), "at": at, "tone": "green" if age_h is not None and age_h <= JOB_FRESH_HOURS else "amber",
            "sub": what + note}


def build_attention(raw: dict) -> dict:
    """The people-facing list: what needs a human today. Worst first."""
    L, T = raw.get("leads"), raw.get("tickets")
    al, at = raw.get("attn_leads"), raw.get("attn_tickets")
    if _failed(al) and _failed(at):
        return {"available": False, "total": None, "items": [], "shown": 0, "note": "Could not read leads or tickets", "all_same_what": None}
    items: list[dict] = []
    if not _failed(al):
        for r in al:
            who = f"With {r['rep']}" if r.get("rep") else "Not assigned to a rep"
            src = f" · came in via {r['source']}" if r.get("source") else ""
            name = r.get("full_name") or "Unnamed lead"
            items.append({"kind": "lead", "severity": "late", "sort": r["late_s"] or 0,
                          "title": name + (f" ({r['company']})" if r.get("company") else ""),
                          "what": W.ROW_WHAT["past_sla"], "who": who + src,
                          "badge": f"{_span(r['late_s'])} late", "id": f"lead-{r['id']}", "ref": f"lead-{r['id']}"})
    if not _failed(at):
        for r in at:
            late = r.get("late_s")
            who = f"With {r['assignee']}" if r.get("assignee") else "Not assigned to anyone"
            prio = f"{r['priority']} priority" if r.get("priority") else ""
            title = f"{r['ticket_code']} - {r['subject']}" if r.get("subject") else str(r["ticket_code"])
            items.append({"kind": "ticket", "severity": "late" if late is not None else "old", "sort": late if late is not None else (r["age_s"] or 0),
                          "title": title, "what": "Past its support deadline" if late is not None else f"Open for {_span(r['age_s'])}",
                          "who": ", ".join(x for x in (who, prio, f"customer {r['customer']}" if r.get("customer") else "") if x),
                          "badge": f"{_span(late)} late" if late is not None else f"open {_span(r['age_s'])}",
                          "id": f"ticket-{r['ticket_code']}", "ref": f"ticket-{r['ticket_code']}"})
    items.sort(key=lambda x: (x["severity"] != "late", -x["sort"]))
    lead_items = [i for i in items if i["kind"] == "lead"]
    tick_items = [i for i in items if i["kind"] == "ticket"]
    n_t = min(len(tick_items), max(2, ATTENTION_MAX - len(lead_items)))
    n_l = min(len(lead_items), ATTENTION_MAX - n_t)
    items = sorted(lead_items[:n_l] + tick_items[:n_t], key=lambda x: (x["severity"] != "late", -x["sort"]))
    total = ((L["overdue"][-1] if not _failed(L) else len(al) if not _failed(al) else 0)
             + ((T["overdue"] + T["stale"]) if not _failed(T) else len(at) if not _failed(at) else 0))
    shown = items[:ATTENTION_MAX]
    for it in shown:
        it.pop("sort", None)
    note = None
    if _failed(al) or _failed(at):
        note = "Part of this list could not be read (" + ("leads" if _failed(al) else "tickets") + " unavailable)"
    # Lesson L-145: when every row says the same thing ("Past its 5-business-hour deadline"), printing it N times is
    # noise. The server decides it here (so a scenario can check it) and the page prints it once above the list.
    same = shown[0]["what"] if len(shown) > 1 and all(i["what"] == shown[0]["what"] for i in shown) else None
    return {"available": True, "total": total, "items": shown, "shown": len(shown), "note": note,
            "all_same_what": same}


# ============================================================================ "since yesterday" (pure)
def _n_or_no(count: int, one: str, many: str | None = None) -> str:
    """'2 new leads' / '1 new lead' / 'no new leads' - a count in words, zero included."""
    return f"no {many or one + 's'}" if count == 0 else _n(count, one, many)


def build_since(raw: dict, blind_ids: list[str]) -> dict:
    """The plain line under the headline: what changed during today, compared with where things stood last night.

    Same day boundary, same time zone and the same 7-day series as the tiles (lesson L-097): "today" is
    date_trunc('day', now()) in the database session's zone, and the SLA comparison is `now` against the last point of
    yesterday in the very series the sparkline draws, so the line and the tiles can never tell different stories.

    Honest, never a guess:
      * a clause whose source is blind (the ClickUp pull is not running, so a reply cannot reach the database) is
        replaced by a short "can't be counted" note - it is never rendered as "no replies";
      * a clause whose query failed is replaced by "could not be read";
      * with nothing on file from before today there is nothing to compare, so the whole line is left out
        (`available: False` with a reason) rather than printed with invented context.
    """
    L, T, C = raw.get("leads"), raw.get("tickets"), raw.get("changes")
    parts: list[dict] = []
    unknown: list[str] = []
    lead_history = (not _failed(L)) and (int(L["total"]) - int(L["new"][-1])) > 0
    tick_history = (not _failed(T)) and (int(T["total"]) - int(T["opened"][-1])) > 0
    tz = C.get("tz") if isinstance(C, dict) and not _failed(C) else None

    def out(available: bool, reason: str | None = None) -> dict:
        text = None
        if available:
            text = "Since yesterday: " + ", ".join(p["text"] for p in parts) + "."
        return {"available": available, "reason": reason, "text": text, "label": "Since yesterday",
                "parts": parts, "unknown": unknown,
                "note": (" ".join(u[0].upper() + u[1:] + "." for u in unknown) or None),
                "tz": tz, "day_note": "Compared with the end of yesterday, in the database's calendar day"
                                      + (f" (time zone {tz})" if tz else "") + "."}

    if _failed(L) and _failed(T):
        return out(False, "the numbers behind the comparison could not be read")
    if not lead_history and not tick_history:
        return out(False, "nothing is on file from before today, so there is nothing to compare with")

    # 1. leads that came in today
    if _failed(L):
        unknown.append("new leads could not be counted (the leads table could not be read)")
    else:
        n = int(L["new"][-1])
        parts.append({"id": "new_leads", "text": _n_or_no(n, "new lead"), "tone": "flat", "detail": "new_leads" if n else None})

    # 2. replies that came in today - only countable while the ClickUp pull really reaches the database (L-086)
    if any(b in blind_ids for b in ("pull", "clickup", "checks")):
        unknown.append("replies can't be counted while the ClickUp comment pull is not known to be running")
    elif _failed(C):
        unknown.append("replies could not be counted (that query did not answer)")
    else:
        n = int(C["replies_today"])
        parts.append({"id": "replies", "text": (_n_or_no(n, "reply", "replies")) + " came in from ClickUp",
                      "tone": "good" if n else "flat", "detail": "replied_today" if n else None})

    # 3. did anything NEW go past its SLA since last night?
    if _failed(L):
        pass                                   # already reported by clause 1
    elif len(L["overdue"]) < 2:
        unknown.append("there is no yesterday in the series to compare the SLA count with")
    else:
        diff = int(L["overdue"][-1]) - int(L["overdue"][-2])
        if diff > 0:
            parts.append({"id": "past_sla", "text": f"{_n(diff, 'more lead')} went past its SLA" if diff == 1
                                                    else f"{diff} more leads went past their SLA",
                          "tone": "bad", "detail": "overdue_leads"})
        elif diff == 0:
            parts.append({"id": "past_sla", "text": "nothing new went past its SLA", "tone": "good", "detail": None})
        else:
            parts.append({"id": "past_sla", "text": f"{_n(-diff, 'lead')} fewer {'is' if -diff == 1 else 'are'} past SLA than last night",
                          "tone": "good", "detail": None})

    # 4. tickets - only when the company actually has tickets on file, so an empty table adds no noise
    if not _failed(T) and int(T["total"]) > 0:
        o, r = int(T["opened"][-1]), int(T["resolved"][-1])
        if o or r:
            parts.append({"id": "tickets", "text": f"{_n_or_no(o, 'ticket')} came in and {r} {'was' if r == 1 else 'were'} resolved",
                          "tone": "good" if r >= o else "flat", "detail": "tickets_today" if o else None})
        else:
            parts.append({"id": "tickets", "text": "no ticket came in or was resolved", "tone": "flat", "detail": None})

    if not parts:
        return out(False, "every source behind the comparison is blind or unreadable")
    return out(True)


# ============================================================================ the detail drawer (pure)
# id -> (title, how it is ordered, what kind of row). The title is what the drawer header says when a tile is clicked.
DETAIL_LISTS: dict[str, tuple[str, str, str]] = {
    "new_leads": ("Leads that came in today", "Newest first.", "lead"),
    "overdue_leads": (W.TILE_PAST_SLA, "Worst first: the longest past its deadline at the top.", "lead"),
    "awaiting": (W.TILE_AWAITING, "Longest wait first.", "lead"),
    "replied_today": ("Leads that got a reply today", "Newest reply first.", "lead"),
    "replied": ("Leads with a reply on record", "Newest reply first. A reply on record is a ClickUp comment that the pull job "
                "has already brought into the database.", "lead"),
    "open_tickets": ("Open tickets", "Past their deadline first, then the oldest.", "ticket"),
    "tickets_today": ("Tickets opened today", "Newest first.", "ticket"),
    "attention_tickets": ("Tickets that need a human", "Worst first: past the deadline, then open the longest.", "ticket"),
}
PRIVACY_NOTE = ("Only what this page already shows: name, company and source. E-mail, phone and the raw form payload are "
                "never sent to the browser.")


def _clip(value, limit: int) -> tuple[str | None, bool]:
    """(text, was_cut). Empty / whitespace-only text becomes None so the drawer can say 'nothing on record'."""
    s = str(value if value is not None else "").strip()
    if not s:
        return None, False
    if len(s) <= limit:
        return s, False
    return s[:limit].rstrip() + "...", True


def _fact(key: str, label: str, value: str, at: str | None = None, tone: str = "") -> dict:
    return {"k": key, "label": label, "value": value, "at": at, "tone": tone}


def _when(seconds: float | None, past: str, future: str, none: str) -> str:
    """'3 h 20 min late' / 'in 2 h' / 'no deadline on record', from a signed 'now - then' in seconds."""
    if seconds is None:
        return none
    return past.format(_span(seconds)) if seconds >= 0 else future.format(_span(-seconds))


def _lead_outcome(row: dict) -> str:
    """The lead's SLA outcome, by the same two facts the Leads page uses: is a reply on record, is the deadline past."""
    due, first = row.get("sla_due_at"), row.get("first_reply_at")
    if due is None:
        return "no_deadline"
    if first is not None:
        return "on_time" if first <= due else "late"
    return "past_sla" if (row.get("late_s") is not None and int(row["late_s"]) > 0) else "pending"


def _lead_item(row: dict) -> dict:
    key = _lead_outcome(row)
    late = _i(row.get("late_s"))
    name = (row.get("full_name") or "").strip() or "Unnamed lead"
    company = (row.get("company") or "").strip()
    facts = [
        _fact("rep", "Owned by", row.get("rep") or "Nobody - this lead has no current rep", tone="" if row.get("rep") else "warn"),
        _fact("came", "Came in", _ago(_iso(row.get("created_at"))), _iso(row.get("created_at"))),
        _fact("due", "Reply deadline", _when(late, "passed {} ago", "in {}", "no deadline on record"),
              _iso(row.get("sla_due_at")), tone="bad" if key == "past_sla" else ""),
        _fact("source", "Came in via", row.get("source") or "source not recorded"),
    ]
    first = row.get("first_reply_at")
    if first is not None and row.get("created_at") is not None:
        wait = (first - row["created_at"]).total_seconds()
        facts.append(_fact("reply", "First reply", ("stamped before the lead arrived (data anomaly)" if wait < 0
                                                    else f"{_span(wait)} after it arrived"), _iso(first),
                           tone="good" if key == "on_time" else "warn"))
    else:
        facts.append(_fact("reply", "First reply", W.NO_REPLY_YET, None, tone="warn"))

    ai = None
    notes, cut = _clip(row.get("ai_notes"), AI_NOTES_MAX_CHARS)
    if row.get("model_name") or row.get("potential_score") is not None or notes:
        ai = {"score": _i(row.get("potential_score")), "scale": row.get("scale_estimate") or None,
              "org": row.get("organization_type") or None, "notes": notes, "notes_cut": cut,
              "model": row.get("model_name") or None, "at": _iso(row.get("analyzed_at")),
              "ago": _ago(_iso(row.get("analyzed_at")))}

    clickup = None
    task = (row.get("clickup_task_id") or "").strip()
    state = (row.get("sync_status") or "").strip()
    if task or state:
        pretty = {"synced": "Task created in ClickUp", "mocked": "Test mode - no real ClickUp task was created",
                  "pending": "Not sent to ClickUp yet", "failed": "Sending to ClickUp failed"}.get(state, f"ClickUp sync status: {state}" if state else "No ClickUp sync row")
        clickup = {"state": state or "none", "text": pretty, "task_id": task or None,
                   "url": (CLICKUP_TASK_URL + task) if (task and state == "synced") else None,
                   "sent_ago": _ago(_iso(row.get("last_synced_at"))), "sent_at": _iso(row.get("last_synced_at")),
                   "pulled_ago": _ago(_iso(row.get("last_comment_pulled_at"))), "pulled_at": _iso(row.get("last_comment_pulled_at")),
                   "tone": "ok" if state == "synced" else "warn" if state in ("pending", "failed") else "off"}

    note = None
    text, note_cut = _clip(row.get("note"), NOTE_MAX_CHARS)
    if text:
        note = {"text": text, "cut": note_cut, "by": (row.get("note_by") or "").strip() or "an unnamed ClickUp user",
                "at": _iso(row.get("note_at")), "ago": _ago(_iso(row.get("note_at"))),
                "source": (row.get("note_source") or "clickup_comment").replace("_", " "),
                "stamp": "when it was pulled (ClickUp gave no time)" if row.get("note_at_synced") else "when it was written in ClickUp"}

    return {
        "ref": f"lead-{row['id']}", "kind": "lead", "eyebrow": f"LEAD #{row['id']}",
        "title": name + (f" ({company})" if company else ""),
        "outcome": {"key": key, "label": W.LABEL[key], "what": W.ROW_WHAT[key], "meaning": W.MEANING[key],
                    "severity": W.SEVERITY[key]},
        "badge": (f"{_span(late)} late" if key == "past_sla" else
                  _when(late, "{} late", "deadline in {}", "no deadline") if key == "pending" else W.LABEL[key]),
        "facts": facts, "ai": ai,
        "ai_none": "No AI analysis is on record for this lead (lead_ai_analysis is empty for it).",
        "clickup": clickup, "clickup_none": "No ClickUp task is on record for this lead.",
        "note": note, "note_none": "No ClickUp comment has been pulled for this lead yet - which is exactly what the "
                                   "database knows, not proof that nobody called.",
        "privacy": PRIVACY_NOTE,
    }


def _ticket_item(row: dict) -> dict:
    late = _i(row.get("late_s"))
    code = str(row.get("ticket_code"))
    subject = (row.get("subject") or "").strip()
    closed = row.get("resolved_at") or row.get("closed_at")
    facts = [
        _fact("rep", "Assigned to", row.get("assignee") or "Nobody - this ticket has no assignee", tone="" if row.get("assignee") else "warn"),
        _fact("came", "Opened", _ago(_iso(row.get("created_at"))), _iso(row.get("created_at"))),
        _fact("due", "Support deadline", _when(late, "passed {} ago", "in {}", "no deadline on record"),
              _iso(row.get("sla_due_at")), tone="bad" if (late is not None and late > 0) else ""),
        _fact("who", "Customer", row.get("customer") or "no customer on the ticket (internal)"),
        _fact("state", "Status", f"{str(row.get('status') or '').replace('_', ' ')}"
                                 + (f" · {row['priority']} priority" if row.get("priority") else "")
                                 + (f" · came in by {row['channel']}" if row.get("channel") else "")),
    ]
    if row.get("category"):
        facts.append(_fact("cat", "Category", row["category"]))
    if closed is not None:
        facts.append(_fact("done", "Resolved / closed", _ago(_iso(closed)), _iso(closed), tone="good"))
    sev = "late" if (late is not None and late > 0) else ("old" if (row.get("age_s") or 0) >= TICKET_STALE_HOURS * 3600 else "ok")
    return {
        "ref": f"ticket-{code}", "kind": "ticket", "eyebrow": "TICKET " + code,
        "title": f"{code} - {subject}" if subject else code,
        "outcome": {"key": sev, "label": "Past its support deadline" if sev == "late" else
                    (f"Open for more than {TICKET_STALE_HOURS} hours" if sev == "old" else "Inside its deadline"),
                    "what": "Past its support deadline" if sev == "late" else f"Open for {_span(_i(row.get('age_s')))}",
                    "meaning": "an open ticket is late when its sla_due_at is in the past; it is 'stuck' when it has been "
                               f"open for more than {TICKET_STALE_HOURS} hours without being past the deadline",
                    "severity": sev},
        "badge": f"{_span(late)} late" if sev == "late" else f"open {_span(_i(row.get('age_s')))}",
        "facts": facts, "ai": None, "ai_none": "AI analysis is only done for leads, not for support tickets.",
        "clickup": None, "clickup_none": "Support tickets are not pushed to ClickUp by this pipeline.",
        "note": None, "note_none": "Ticket comments are not pulled into this page.",
        "privacy": PRIVACY_NOTE,
    }


def _row_of(item: dict) -> dict:
    """The compact row a drawer list shows for an item (the detail panel is one click further)."""
    who = next((f["value"] for f in item["facts"] if f["k"] == "rep"), "")
    return {"ref": item["ref"], "title": item["title"], "what": item["outcome"]["what"], "who": who,
            "badge": item["badge"], "severity": item["outcome"]["severity"], "kind": item["kind"]}


def build_details(raw: dict) -> dict:
    """{"lists": {...}, "items": {...}} - everything a drawer can show, in the same payload as the tiles (one poller).

    A lead that is in two lists is stored once and pointed at twice. A block whose query failed makes only its own lists
    unavailable (with the reason); the tiles above keep their numbers."""
    L, T, C = raw.get("leads"), raw.get("tickets"), raw.get("changes")
    dl, dt = raw.get("detail_leads"), raw.get("detail_tickets")
    items: dict[str, dict] = {}
    lists: dict[str, dict] = {}
    totals = {
        "new_leads": None if _failed(L) else int(L["new"][-1]),
        "overdue_leads": None if _failed(L) else int(L["overdue"][-1]),
        "awaiting": None if _failed(L) else int(L["awaiting"][-1]),
        "replied_today": None if _failed(C) else int(C["leads_replied_today"]),
        "replied": None if _failed(C) else int(C.get("leads_with_reply") or 0),
        "open_tickets": None if _failed(T) else int(T["open"][-1]),
        "tickets_today": None if _failed(T) else int(T["opened"][-1]),
        "attention_tickets": None if _failed(T) else int(T["overdue"]) + int(T["stale"]),
    }
    for list_id, (title, order, kind) in DETAIL_LISTS.items():
        block = dl if kind == "lead" else dt
        why = None
        if _failed(block):
            why = (block or {}).get("error", "could not be read") if isinstance(block, dict) else "could not be read"
        lists[list_id] = {"id": list_id, "title": title, "order": order, "kind": kind, "rows": [],
                          "total": totals.get(list_id), "available": why is None, "error": _redact(why) if why else None,
                          "cap": DETAIL_ROWS, "more": 0, "related": None}
    for block, builder, kind in ((dl, _lead_item, "lead"), (dt, _ticket_item, "ticket")):
        if _failed(block):
            continue
        for row in block:
            try:
                item = builder(row)
            except Exception as exc:  # noqa: BLE001 - one malformed row must not empty the whole drawer (L-044)
                _log_once(f"detail_{kind}", f"{type(exc).__name__}: {_redact(str(exc))}")
                continue
            items.setdefault(item["ref"], item)
            target = lists.get(row["list"])
            if target is not None:
                target["rows"].append(_row_of(items[item["ref"]]))
    for spec in lists.values():
        if spec["total"] is not None:
            spec["more"] = max(0, spec["total"] - len(spec["rows"]))

    # A list may offer the other half of the same question - "who is still waiting" vs "who was already answered" - but only
    # when that other list really came back with rows, so a follow-on link is never a dead end.
    def _link(source: str, target: str, phrase) -> None:
        a, b = lists.get(source), lists.get(target)
        if a and b and a["available"] and b["available"] and b["rows"]:
            n = b["total"] if b["total"] is not None else len(b["rows"])
            a["related"] = {"id": target, "text": phrase(n)}
    _link("awaiting", "replied", lambda n: f"{_n(n, 'other lead')} already " + ("has" if n == 1 else "have") + " a reply on record")
    _link("replied", "awaiting", lambda n: f"{_n(n, 'lead')} " + ("is" if n == 1 else "are") + " still waiting for a first reply")
    return {"lists": lists, "items": items, "cap": DETAIL_ROWS, "privacy": PRIVACY_NOTE}
