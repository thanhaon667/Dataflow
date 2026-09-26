"""The ONE place the lead SLA outcomes are put into words, for report viewers and analysts alike.

Why this module exists: the Today page (desktop/today_data.py) and the Leads page (desktop/leads_data.py) already share
the SQL that decides who is late (today_data.lead_past_sla_sql / lead_awaiting_sql, lesson L-102), but each used to write
its own English for the very same state - "Leads past SLA now" against "Past SLA now", "Awaiting first contact" against
"Awaiting first reply". Two pages describing one state with two names is how a report viewer learns not to trust either.
Both pages now import the labels, hints and one-line meanings below, so a wording change happens once.

Nothing here touches the database or the configuration; the only moving part is the SLA text ("5 business hours,
8:00-17:00, Mon-Fri"), which the caller passes in because it is read from erp/config.py at call time (lesson L-085).

  Outcome         every lead is in exactly one of the five states of OUTCOMES, decided by two facts: is a reply on
                  record (a lead_updates row = a ClickUp comment pulled by erp/clickup_pull.py), and is the deadline
                  (leads.sla_due_at) in the past.
  Awaiting        "no reply on record at all" - the union of `past_sla` and `pending`. The Today page counts it,
                  the Leads page counts it, both call it the same thing now.
"""
from __future__ import annotations

# --------------------------------------------------------------------- the five outcomes (key, label, meaning)
OUTCOMES: list[tuple[str, str, str]] = [
    ("on_time", "Answered on time", "a reply is on record and it came at or before the deadline"),
    ("late", "Answered late", "a reply is on record, but it came after the deadline"),
    ("past_sla", "Past SLA, no reply", "no reply on record and the deadline has passed"),
    ("pending", "Waiting, inside deadline", "no reply yet, but the deadline has not passed"),
    ("no_deadline", "No deadline on record", "the lead has no sla_due_at, so it cannot be judged"),
]
KEYS: list[str] = [k for k, _l, _m in OUTCOMES]
LABEL: dict[str, str] = {k: label for k, label, _m in OUTCOMES}
MEANING: dict[str, str] = {k: meaning for k, _l, meaning in OUTCOMES}

# How ONE lead in that state is described in a row or a detail panel (a verb phrase, not a bucket name).
ROW_WHAT: dict[str, str] = {
    "on_time": "Answered on time",
    "late": "Answered, but after the deadline",
    "past_sla": "Waiting for a first reply, past the deadline",
    "pending": "Waiting for a first reply, still inside the deadline",
    "no_deadline": "No deadline on record",
}

# The compact form of LABEL for tight spaces (a filter pill, a table cell, a chip, a mini-bar segment) where the full
# label ("Past SLA, no reply", "Waiting, inside deadline") does not fit. Before 2026-09-24 the Leads page's two scripts
# (static/leads.js and static/sources.js) each kept their OWN copy of this same five-word map, hand-typed twice with no
# link back to this module - free to drift apart from each other or from LABEL above with nobody noticing. Both now read
# it from here: leads_data.build_options() puts it on every `options.statuses[i].short`, which both scripts already
# receive in their payload, so the words live in exactly one place for every SLA-outcome surface on both pages.
SHORT: dict[str, str] = {
    "on_time": "On time",
    "late": "Answered late",
    "past_sla": "Past SLA",
    "pending": "Waiting",
    "no_deadline": "No deadline",
}
# The colour/severity a row or badge gets for that outcome: late (red) | old (amber) | ok (green) | none (grey).
SEVERITY: dict[str, str] = {"on_time": "ok", "late": "old", "past_sla": "late", "pending": "old", "no_deadline": "none"}

# --------------------------------------------------------------------- KPI tile labels used by BOTH pages
TILE_PAST_SLA = "Leads past SLA now"
TILE_AWAITING = "Leads awaiting first reply"

# --------------------------------------------------------------------- shared phrases
REPLY_EVIDENCE = ("a reply on record - a row in lead_updates, which is a ClickUp comment pulled into the database by "
                  "erp/clickup_pull.py. That is the only evidence of a reply the database has")
SUB_NONE_PAST_SLA = "none - nobody is waiting too long"
NO_REPLY_YET = "no reply on record yet"


def hint_past_sla(sla_text: str) -> str:
    """The tooltip of the "Leads past SLA now" tile, on Today and on Leads."""
    return (f"No reply on record and the reply deadline has passed. The deadline is {sla_text} after the lead came in. "
            "Both pages read this from the same SQL, so they can never disagree about who is late. "
            "A lead that was answered, even late, is no longer counted here.")


def hint_awaiting() -> str:
    """The tooltip of the "Leads awaiting first reply" tile, on Today and on Leads."""
    return ("Leads with no reply on record at all, including the ones still inside their deadline. A rep's reply only "
            "becomes visible here after the ClickUp comment pull has run.")


def all_replied(in_view: bool = False) -> str:
    """The friendly empty state of the awaiting tile. `in_view` adds the Leads page's filter scope."""
    return "every lead" + (" in view" if in_view else "") + " has a reply on record"


def past_sla_phrase(n: int, sla_text: str) -> str:
    """The headline fragment: "1 lead past its 5-business-hour SLA" / "2 leads past their 5-business-hour SLA"."""
    if n == 1:
        return f"1 lead past its {sla_text} SLA"
    return f"{n} leads past their {sla_text} SLA"
