"""Today page, assembly: the status sentence, the rules text and the payload assembly (split out of desktop/today_data.py, unchanged).
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from desktop import sla_words as W
from desktop.flow_data import _iso
from desktop.today_util import QUERY_BUDGET_SECONDS, STATEMENT_TIMEOUT_MS, _failed, _n, _pretty_day, _span
from desktop.today_sql import ATTENTION_MAX, DAYS, DETAIL_ROWS, OPEN_STATUS_LIST, TICKET_STALE_HOURS
from desktop.today_build import (
    JOB_FRESH_HOURS, PRIVACY_NOTE, _sla_hours, _sla_window, build_attention, build_details, build_since, build_tiles,
    last_job,
)
from desktop.today_health import blind_inputs, build_health


def _caveat(items: list[dict], overdue_leads: int) -> str | None:
    """The plain-English sub-line under the headline, or None when every input is open."""
    if not items:
        return None
    out = "Numbers may be incomplete: " + " and ".join(i["clause"] for i in items) + "."
    if overdue_leads and any(i["id"] in ("pull", "clickup") for i in items):    # the blind spot inflates exactly this number
        out += " Some leads listed as past their SLA may already have been answered in ClickUp."
    return out


def _status(level: str, lead: str, rest: str, sub: str, reasons: list[str], sep: str = " - ", caveat: str | None = None,
            items: list[dict] | None = None) -> dict:
    """The one shape every status has. `sep` is what joins lead and rest (the page shows ' - ' as an en dash)."""
    return {"level": level, "lead": lead, "sep": sep, "rest": rest, "headline": lead + sep + rest if rest else lead, "sub": sub,
            "reasons": reasons, "blind": bool(items), "caveat": caveat, "blind_inputs": [i["id"] for i in items or []]}


def build_status(raw: dict, health: list[dict], db_error: str | None) -> dict:
    """The one big sentence. See the module docstring for the rule order."""
    L, T = raw.get("leads"), raw.get("tickets")
    if db_error or (_failed(L) and _failed(T)):
        return _status("unknown", "Can’t read today’s numbers", "the database is not answering right now",
                       "Check that PostgreSQL is running, then press Refresh.", ["The database could not be queried."],
                       items=[{"id": "database", "clause": "the database is not answering"}])
    od = L["overdue"][-1] if not _failed(L) else 0
    ot = T["overdue"] if not _failed(T) else 0
    items = blind_inputs(health)
    caveat = _caveat(items, od)
    if od or ot:
        sla = f"{_sla_hours():g}-business-hour"
        parts = []
        if od:
            parts.append(W.past_sla_phrase(od, sla))      # same wording as the Leads page (desktop/sla_words.py)
        if ot:
            parts.append(f"{_n(ot, 'ticket')} past their SLA" if ot != 1 else "1 ticket past its SLA")
        rest = " and ".join(parts)
        waits = [b["worst_late_s"] for b in (L, T) if not _failed(b) and b.get("worst_late_s") is not None]
        worst = max(waits) if waits else None
        sub = (f"The longest wait is {_span(worst)}. " if worst else "") + "The list below shows who to chase first."
        return _status("attention", "Needs attention", rest, sub, list(parts), caveat=caveat, items=items)
    new = L["new"][-1] if not _failed(L) else None
    if _failed(L) or _failed(T):
        which = "lead" if _failed(L) else "ticket"
        return _status("watch", "Partly unavailable", f"the {which} numbers could not be read; nothing is overdue in the rest",
                       "Some numbers could not be read - the tiles marked unavailable say why.", [f"The {which} numbers are unavailable."],
                       caveat=caveat, items=items)
    red = [c for c in health if c["state"] == "bad"]
    if red:
        rest = "nothing is overdue, but " + red[0]["text"][0].lower() + red[0]["text"][1:] + (f" (+{len(red) - 1} more)" if len(red) > 1 else "")
        return _status("watch", "Mostly fine", rest, "Everything is on time, but something behind the scenes needs a look - see System health below.",
                       [f"{c['label']}: {c['text']}" for c in red], caveat=caveat, items=items)
    summary = f"{_n(new, 'new lead')} today, none overdue" if new else "no new leads yet today, none overdue"
    if items:     # nothing is late in what the page can see, but it cannot see everything: never a bare "All good"
        return _status("watch", "No problems found", f"but the numbers may be incomplete ({summary})", "This turns to All good by itself once those inputs are back.",
                       [i["clause"][0].upper() + i["clause"][1:] for i in items], sep=", ", caveat=caveat, items=items)
    return _status("ok", "All good", summary, "Nothing is late right now. This page updates by itself.", [])


def assemble(raw: dict, flow: dict | None, db_error: str | None = None) -> dict:
    """Raw query results + the flow snapshot -> the JSON the page draws. Pure: no I/O."""
    job = last_job(raw, flow)
    health = build_health(flow, db_error)
    L = raw.get("leads")
    day = None
    if not _failed(L):
        try:
            day = date.fromisoformat(L["today"])
        except (KeyError, ValueError):
            day = None
    sources = {k: ("unavailable" if _failed(raw.get(k)) else "ok")
               for k in ("leads", "tickets", "attn_leads", "attn_tickets", "pull", "changes", "detail_leads", "detail_tickets")}
    sources["health"] = "ok" if flow else "unavailable"
    status = build_status(raw, health, db_error)
    tiles = build_tiles(raw, job)
    details = build_details(raw)
    since = build_since(raw, status.get("blind_inputs") or [])
    # A tile / a clause only invites a click when the list behind it really came back with rows (never a dead end).
    def _drillable(holder: dict) -> None:
        spec = details["lists"].get(holder.get("detail") or "")
        if spec is None or not spec["available"] or not spec["rows"]:
            holder["detail"] = None
    for t in tiles:
        _drillable(t)
    for part in since["parts"]:
        _drillable(part)
    changes = raw.get("changes")
    tz = changes.get("tz") if isinstance(changes, dict) and not _failed(changes) else None
    return {
        "ok": not db_error and sources["leads"] == "ok" and sources["tickets"] == "ok",
        "generated_at": _iso(datetime.now(timezone.utc)),
        "day": day.isoformat() if day else None,
        "day_label": _pretty_day(day) if day else datetime.now().strftime("%A"),
        "tz": tz,
        "status": status,
        "since": since,
        "kpis": tiles,
        "attention": build_attention(raw),
        "details": details,
        "health": health,
        "job": job,
        "sources": sources,
        "rules": build_rules(tz),
    }


def build_rules(tz: str | None = None) -> list[str]:
    """The text under "How this is decided". Built from the same constants and config the computations use, on every call, so
    it cannot drift from the code (lesson L-085): change LEAD_SLA_HOURS in .env, restart, and this text follows."""
    stat = [x.replace("_", " ") for x in OPEN_STATUS_LIST]
    open_txt = ", ".join(stat[:-1]) + " or " + stat[-1]
    return [
        f"{W.LABEL['past_sla']} ('{W.TILE_PAST_SLA}'): {W.MEANING['past_sla']} - the deadline being {_sla_hours():g} business hours, "
        f"{_sla_window()}, counted from when the lead came in. The Leads page (Ctrl+5) uses these very words and the very same SQL.",
        f"'{W.TILE_AWAITING}': no reply is on record yet at all, deadline or not. A reply on record means {W.REPLY_EVIDENCE}.",
        f"Tickets: open = {open_txt}. The attention list adds tickets open for more than {TICKET_STALE_HOURS} hours and shows at most "
        f"{ATTENTION_MAX} rows, worst first.",
        "Status, first match wins: 'Can't read today's numbers' when the database does not answer; 'Needs attention' when anything is past "
        "its SLA; 'Partly unavailable' when a number cannot be read; 'Mostly fine' when nothing is overdue but a system check is red; "
        "'No problems found, but ...' when nothing is overdue but an input is blind (next line); otherwise 'All good'.",
        "Blind inputs: new web-form leads only arrive while the lead webhook is running, and a rep's reply only reaches the database when "
        "the ClickUp comment pull is scheduled (and ClickUp is connected). If either is not the case, or could not be checked, the page says "
        "'Numbers may be incomplete' under the headline and never shows a bare 'All good'.",
        f"Charts: the last {DAYS} days; the arrow compares now with the end of yesterday; a day is the database's calendar day"
        + (f" (time zone {tz})" if tz else "") + ", midnight to now. "
        f"Last job run is green when a job ran in the last {JOB_FRESH_HOURS} hours.",
        "Since yesterday: the line under the headline counts what happened during today - leads that came in, ClickUp replies that "
        "came in, whether the number of leads past SLA grew since the end of yesterday, and ticket movement when there are tickets "
        "on file. It uses the same day boundary and time zone as everything else on this page. A clause whose source is blind or "
        "unreadable is replaced by a short note instead of a zero, and with nothing on file from before today the line is left out.",
        "Needs a human: when every row is in the same state, that state is printed once above the list instead of on "
        "every row, so the rows differ where they really differ (who owns it, how late it is).",
        f"Detail panel: clicking a tile (or a clause of the line above) lists what that number counts, worst first, at most "
        f"{DETAIL_ROWS} rows; clicking a row there or in 'Needs a human' shows the lead or ticket in full - owner, arrival, "
        "deadline, the AI summary from lead_ai_analysis if there is one, the ClickUp task and state, and the newest note pulled "
        f"from ClickUp. The '{W.TILE_AWAITING}' list also offers the other half of the question, the leads that already have a "
        "reply. It is all part of the same payload as the numbers, so opening it reads nothing extra. " + PRIVACY_NOTE,
        "Green / amber / grey / red under System health come from the Data Flow page's live checks; grey means not set up or not checkable, never 'running'. "
        "The Health page (Ctrl+6) shows the same verdicts in full - why, when it was last checked, what stops working and what to do about it.",
        f"Speed: each query is cut off after {STATEMENT_TIMEOUT_MS / 1000:g} s and all of them together after {QUERY_BUDGET_SECONDS:g} s; "
        "a number that could not be read in time shows as unavailable instead of freezing the page.",
    ]
