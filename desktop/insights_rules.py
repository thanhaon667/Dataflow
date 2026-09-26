"""
Channels page, INSIGHTS ENGINE: a fixed, explainable rule set that reads the rollup's numbers and says what to look at (pure
functions, no database, no I/O). Fed by desktop/insights_data.py with per-campaign sums for the analysed window and for the
previous window of the same length, plus per-channel day coverage.

Six rules, nothing learned, nothing hidden - every threshold below is a named constant that the page prints in its
"How these are decided" drawer (generated from these constants by thresholds(), lesson L-085):
  spend_no_conversions   a campaign holds a real share of the spend and has NO conversion in the window
  cpa_jump               cost per conversion rose by X% against the previous equal window
  cvr_drop               conversion rate (conversions / clicks) fell against the previous equal window
  stale_channel          a channel stopped delivering rows (no row in the last N days before the window end) / holes inside the window
  concentration_risk     one campaign carries more than X% of a currency's spend
  scale_candidates       the campaigns with the best cost per conversion, or the biggest improvers - worth a bigger budget

Every finding is {id, key, severity (info|warn|crit), confidence ('enough data'|'thin data'), channel, campaign, currency, what,
evidence [{label, value, compare, threshold, text, met}], action, magnitude}. The evidence carries the exact comparison that made the
rule fire ("412 clicks >= 50 minimum"), so a reader can check the rule against the numbers.

HONESTY (lesson L-030, docs of the Channels page):
  * MONEY IS NEVER MIXED. Money rules run once per currency on the rows of that currency alone (rules_for_currency() REFUSES a
    group of another currency with MixedCurrencyError); a finding names its currency; nothing is summed or compared across them.
  * A 0 that means "unknown" is not a result. Spend without conversions is only judged when the same channel reports conversions
    somewhere (otherwise the source has no conversion field and the campaign is listed in `notes`, not accused). A zero spend
    is "no spend field", never a bargain.
  * MINIMUM VOLUME. Below a rule's floor nothing is said at all; between the floor and the "enough" level a finding may appear
    but only as info with confidence 'thin data' - a tiny sample is never a warning or a critical alarm.
  * Comparisons need the previous window: with no rows in it, cpa_jump, cvr_drop and the improver half of scale_candidates say
    so (status skipped) instead of comparing with nothing.
  * The engine only READS numbers and returns text. It never schedules, runs or changes anything: the automation ideas are plain
    suggestions.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Iterable, Sequence

from desktop.channels_money import MICROS, money_text

# ============================================================================ thresholds (named; shown to the user)
NOCONV_MIN_SHARE_PCT = 5.0        # spend_no_conversions: the campaign holds at least this share of its currency's spend in the window ...
NOCONV_FLOOR_CLICKS = 10          # ... and had at least this many clicks (below it: nothing is said)
NOCONV_ENOUGH_CLICKS = 50         # ... at least this many clicks make it 'enough data' (10-49 = thin data, info only)
NOCONV_CRIT_SHARE_PCT = 25.0      # ... a share at or above this is critical (with enough data), a smaller one a warning

CPA_JUMP_WARN_PCT = 30.0          # cpa_jump: cost per conversion up by at least this much against the previous window = warning
CPA_JUMP_CRIT_PCT = 75.0          # ... up by at least this much = critical
CPA_FLOOR_CONV = 2                # ... needs at least this many conversions in EACH window (below it: nothing is said)
CPA_ENOUGH_CONV = 5               # ... at least this many in each window make it 'enough data' (2-4 = thin data, info only)

CVR_DROP_WARN_PCT = 25.0          # cvr_drop: conversion rate down by at least this much (relative) = warning
CVR_DROP_CRIT_PCT = 50.0          # ... down by at least this much = critical
CVR_DROP_MIN_POINTS = 0.3         # ... and by at least this many percentage points (a fall from 0.5% to 0.3% is noise)
CVR_FLOOR_CLICKS = 20             # ... needs at least this many clicks in EACH window (below it: nothing is said)
CVR_ENOUGH_CLICKS = 100           # ... at least this many in each window make it 'enough data' (20-99 = thin data, info only)

STALE_DAYS = 7                    # stale_channel: a daily channel whose newest row is more than this many days before the window end
STALE_DAYS_WEEKLY = 14            # ... a weekly channel (one row a week): more than this many days
STALE_HISTORICAL_DAYS = 180       # ... silent for MORE than this many days it is 'historical / no longer reporting' (an ended or archived feed): info, not a warning
STALE_ENOUGH_ROWS = 7             # ... a channel with at least this many day rows of history (weekly: STALE_ENOUGH_WEEKS) is 'enough data'
STALE_ENOUGH_WEEKS = 3
HOLE_MIN_DAYS = 3                 # holes inside the window: this many missing days in a row between two rows of a daily channel (weekly: one missing week)
HOLE_WARN_SHARE_PCT = 20.0        # ... missing days at or above this share of the window make it a warning, fewer an info
HOLE_ENOUGH_ROWS = 7              # ... a channel with at least this many rows in the window (weekly: HOLE_ENOUGH_WEEKS) is 'enough data'
HOLE_ENOUGH_WEEKS = 3
HOLE_LISTED = 3                   # gaps written out in the evidence

CONC_WARN_PCT = 50.0              # concentration_risk: one campaign carries more than this share of a currency's spend = warning
CONC_CRIT_PCT = 80.0              # ... more than this share = critical
CONC_MIN_CAMPAIGNS = 2            # ... needs at least this many campaigns with spend in that currency (one campaign is always 100%)
CONC_ENOUGH_CLICKS = 100          # ... at least this many clicks across those campaigns make it 'enough data'

SCALE_BETTER_PCT = 25.0           # scale_candidates: cost per conversion at least this much better than the currency's pooled cost per conversion ...
SCALE_IMPROVE_PCT = 20.0          # ... or improved by at least this much against the previous window (and not worse than the pooled cost)
SCALE_MIN_CONV = 5                # ... needs at least this many conversions in the window (and in the previous one for the improver test)
SCALE_MIN_CAMPAIGNS = 2           # ... needs at least this many campaigns with spend and conversions in that currency to compare
SCALE_MAX = 3                     # ... at most this many candidates per currency

RECURRING_MIN = 2                 # an automation idea appears when a rule fired this many times (warn or crit), or once as critical

SEVERITIES = ("crit", "warn", "info")
_SEV_RANK = {"crit": 0, "warn": 1, "info": 2}
ENOUGH, THIN = "enough data", "thin data"

RULES = (
    ("spend_no_conversions", "Spend without conversions"),
    ("cpa_jump", "Cost per conversion jumped"),
    ("cvr_drop", "Conversion rate dropped"),
    ("stale_channel", "Stale channel or data gap"),
    ("concentration_risk", "Spend concentrated in one campaign"),
    ("scale_candidates", "Campaigns worth scaling"),
)
RULE_TITLE = dict(RULES)


class MixedCurrencyError(ValueError):
    """A money rule was handed rows of more than one currency: the engine refuses instead of adding VND to EUR."""


# ============================================================================ inputs
@dataclass(frozen=True)
class Period:
    """The sums of one campaign (in one currency) over one window. `days` = distinct days that carry a rollup row."""
    sessions: int = 0
    clicks: int = 0
    impressions: int = 0
    conversions: int = 0
    spend_micros: int = 0
    revenue_micros: int = 0
    days: int = 0

    @property
    def spend(self) -> float:
        return self.spend_micros / MICROS

    def cpa(self) -> float | None:
        """Cost per conversion in currency units - only where spend exists AND there is a conversion; otherwise unknown (None)."""
        if self.spend_micros <= 0 or self.conversions <= 0:
            return None
        return self.spend_micros / MICROS / self.conversions

    def cvr(self) -> float | None:
        """Conversion rate in percent (conversions / clicks); unknown without clicks."""
        return None if self.clicks <= 0 else self.conversions / self.clicks * 100


@dataclass(frozen=True)
class Group:
    """One campaign of one channel in one currency: the current window and the previous window of the same length."""
    channel_key: str
    channel_name: str
    campaign_id: int
    campaign_name: str
    currency: str
    cur: Period
    prev: Period

    @property
    def has_money(self) -> bool:
        return bool(self.cur.spend_micros or self.cur.revenue_micros or self.prev.spend_micros or self.prev.revenue_micros)


@dataclass(frozen=True)
class Span:
    """Day coverage of one channel and grain: `window_days` are the distinct dates with a row inside the window, the rest describes
    all history up to the window end (first / last row, number of distinct row dates)."""
    channel_key: str
    channel_name: str
    grain: str
    first_day: date
    last_day: date
    history_days: int
    window_days: tuple = ()


@dataclass(frozen=True)
class Window:
    d_from: date
    d_to: date
    prev_from: date
    prev_to: date

    @property
    def days(self) -> int:
        return (self.d_to - self.d_from).days + 1


# ============================================================================ small helpers (pure)
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def day_text(d: date) -> str:
    return f"{d.day} {_MONTHS[d.month - 1]} {d.year}"


def _n(x: int | float) -> str:
    return f"{int(x):,}"


def _pct(x: float) -> str:
    return f"{x:.1f}%"


def _money(value: float, currency: str | None) -> str:
    return money_text(value, currency) if currency else f"{value:,.2f}"


def _ccy(g: Group) -> str | None:
    """A group's currency is shown only when it carries money: a counts-only source has a default code in its key but no money."""
    return g.currency if g.has_money else None


def _channel(g: Group) -> dict:
    return {"key": g.channel_key, "name": g.channel_name}


def _campaign(g: Group) -> dict:
    return {"id": g.campaign_id, "name": g.campaign_name}


def _label(g: Group) -> str:
    return g.channel_name if g.campaign_id == 0 else f"{g.campaign_name} ({g.channel_name})"


def cmp_item(label: str, value, op: str, threshold, text: str, met: bool = True) -> dict:
    """One line of evidence: the number, the comparison operator and the threshold it was held against, plus the sentence."""
    return {"label": label, "value": value, "compare": op, "threshold": threshold, "text": text, "met": bool(met)}


def note_item(label: str, value, text: str) -> dict:
    """Evidence that is context, not a test (the previous number, the window)."""
    return {"label": label, "value": value, "compare": None, "threshold": None, "text": text, "met": True}


def _severity(wanted: str, confidence: str) -> str:
    """Thin data never alarms: whatever the rule wanted, a thin finding is info."""
    return "info" if confidence == THIN else wanted


def _finding(rule: str, severity: str, confidence: str, *, channel: dict | None, campaign: dict | None, currency: str | None,
             what: str, evidence: list, action: str, magnitude: float, extra: str = "") -> dict:
    return {"id": rule, "key": f"{rule}:{(channel or {}).get('key', '')}:{(campaign or {}).get('id', '')}:{currency or ''}{':' + extra if extra else ''}",
            "severity": _severity(severity, confidence), "confidence": confidence, "channel": channel, "campaign": campaign,
            "currency": currency, "what": what, "evidence": evidence, "action": action, "magnitude": round(float(magnitude), 4)}


def _result(findings: list, notes: list | None = None, ran: bool = True, why: str | None = None) -> dict:
    return {"findings": findings, "notes": notes or [], "ran": ran, "why": why}


def _skipped(why: str) -> dict:
    return _result([], ran=False, why=why)


# ============================================================================ rule 1: spend without conversions
def rule_spend_no_conversions(groups: Sequence[Group], currency: str, window: Window) -> dict:
    spending = [g for g in groups if g.cur.spend_micros > 0]
    if not spending:
        return _skipped("no campaign has spend in this window")
    total = sum(g.cur.spend_micros for g in spending)
    tracked: dict[str, bool] = {}
    for g in groups:                                                       # does the source of this channel report conversions at all?
        tracked[g.channel_key] = tracked.get(g.channel_key, False) or (g.cur.conversions + g.prev.conversions) > 0
    out, notes = [], []
    for g in spending:
        if g.cur.conversions > 0:
            continue
        if not tracked.get(g.channel_key):
            notes.append(f"{_label(g)} has spend ({_money(g.cur.spend, currency)}) but its channel reports no conversions at all in either "
                         f"window, so a zero here means 'unknown', not 'none': no verdict.")
            continue
        share = g.cur.spend_micros / total * 100
        if share < NOCONV_MIN_SHARE_PCT or g.cur.clicks < NOCONV_FLOOR_CLICKS:
            continue
        enough = g.cur.clicks >= NOCONV_ENOUGH_CLICKS
        sev = "crit" if share >= NOCONV_CRIT_SHARE_PCT else "warn"
        ev = [cmp_item("Spend share", round(share, 2), ">=", NOCONV_MIN_SHARE_PCT,
                       f"{currency} spend {_money(g.cur.spend, currency)} = {_pct(share)} of all {currency} spend ({_money(total / MICROS, currency)}), threshold {_pct(NOCONV_MIN_SHARE_PCT)}"),
              cmp_item("Conversions", 0, "==", 0, f"0 conversions in {day_text(window.d_from)} to {day_text(window.d_to)}"),
              cmp_item("Clicks", g.cur.clicks, ">=", NOCONV_ENOUGH_CLICKS if enough else NOCONV_FLOOR_CLICKS,
                       f"{_n(g.cur.clicks)} clicks {'>=' if enough else '<'} {NOCONV_ENOUGH_CLICKS} needed for 'enough data'", met=enough)]
        if g.prev.conversions:
            ev.append(note_item("Previous window", g.prev.conversions, f"it had {_n(g.prev.conversions)} conversion{'s' if g.prev.conversions != 1 else ''} in the previous {window.days} days"))
        out.append(_finding("spend_no_conversions", sev, ENOUGH if enough else THIN, channel=_channel(g), campaign=_campaign(g), currency=currency,
                            what=f"{_label(g)} spent {_money(g.cur.spend, currency)} and produced no conversions.",
                            evidence=ev, magnitude=share,
                            action="Pause it or cap its budget for now, then check the conversion tracking (tag, goal, landing page) before spending more."))
    return _result(out, notes)


# ============================================================================ rule 2: cost per conversion jump
def rule_cpa_jump(groups: Sequence[Group], currency: str, window: Window) -> dict:
    comparable = [g for g in groups if g.cur.spend_micros > 0 and g.prev.spend_micros > 0]
    if not comparable:
        return _skipped("no campaign has spend in both this window and the previous one")
    out = []
    for g in comparable:
        c, p = g.cur, g.prev
        if min(c.conversions, p.conversions) < CPA_FLOOR_CONV:
            continue
        now, before = c.cpa(), p.cpa()
        if now is None or before is None or before <= 0:
            continue
        change = (now - before) / before * 100
        if change < CPA_JUMP_WARN_PCT:
            continue
        enough = min(c.conversions, p.conversions) >= CPA_ENOUGH_CONV
        sev = "crit" if change >= CPA_JUMP_CRIT_PCT else "warn"
        ev = [cmp_item("Cost per conversion change", round(change, 2), ">=", CPA_JUMP_WARN_PCT,
                       f"{_money(now, currency)} now vs {_money(before, currency)} before = +{_pct(change)}, warning at +{_pct(CPA_JUMP_WARN_PCT)}, critical at +{_pct(CPA_JUMP_CRIT_PCT)}"),
              cmp_item("Conversions (fewer of the two windows)", min(c.conversions, p.conversions), ">=", CPA_ENOUGH_CONV,
                       f"{_n(c.conversions)} now, {_n(p.conversions)} before; {_n(min(c.conversions, p.conversions))} {'>=' if enough else '<'} {CPA_ENOUGH_CONV} needed for 'enough data'", met=enough),
              note_item("Spend", round(c.spend, 2), f"spend {_money(c.spend, currency)} now, {_money(p.spend, currency)} before"),
              note_item("Windows", None, f"{day_text(window.d_from)} to {day_text(window.d_to)} against {day_text(window.prev_from)} to {day_text(window.prev_to)}")]
        out.append(_finding("cpa_jump", sev, ENOUGH if enough else THIN, channel=_channel(g), campaign=_campaign(g), currency=currency,
                            what=f"{_label(g)}: each conversion now costs {_pct(change)} more than in the previous {window.days} days.",
                            evidence=ev, magnitude=change,
                            action="Look at what changed between the two windows (audience, bids, creative, landing page) and consider lowering the budget until the cost per conversion recovers."))
    return _result(out)


# ============================================================================ rule 3: conversion rate drop
def rule_cvr_drop(groups: Sequence[Group], currency: str, window: Window) -> dict:
    comparable = [g for g in groups if g.cur.clicks > 0 and g.prev.clicks > 0]
    if not comparable:
        return _skipped("no campaign has clicks in both this window and the previous one")
    out = []
    for g in comparable:
        c, p = g.cur, g.prev
        if min(c.clicks, p.clicks) < CVR_FLOOR_CLICKS:
            continue
        now, before = c.cvr(), p.cvr()
        if now is None or before is None or before <= 0:
            continue
        rel = (before - now) / before * 100
        points = before - now
        if rel < CVR_DROP_WARN_PCT or points < CVR_DROP_MIN_POINTS:
            continue
        enough = min(c.clicks, p.clicks) >= CVR_ENOUGH_CLICKS
        sev = "crit" if rel >= CVR_DROP_CRIT_PCT else "warn"
        ev = [cmp_item("Conversion rate drop", round(rel, 2), ">=", CVR_DROP_WARN_PCT,
                       f"{now:.2f}% now ({_n(c.conversions)} of {_n(c.clicks)} clicks) vs {before:.2f}% before ({_n(p.conversions)} of {_n(p.clicks)}) = -{_pct(rel)} (-{points:.2f} points); warning at -{_pct(CVR_DROP_WARN_PCT)}, critical at -{_pct(CVR_DROP_CRIT_PCT)}"),
              cmp_item("Drop in points", round(points, 3), ">=", CVR_DROP_MIN_POINTS, f"-{points:.2f} percentage points >= {CVR_DROP_MIN_POINTS} points (smaller falls are treated as noise)"),
              cmp_item("Clicks (fewer of the two windows)", min(c.clicks, p.clicks), ">=", CVR_ENOUGH_CLICKS,
                       f"{_n(min(c.clicks, p.clicks))} {'>=' if enough else '<'} {CVR_ENOUGH_CLICKS} needed for 'enough data'", met=enough),
              note_item("Windows", None, f"{day_text(window.d_from)} to {day_text(window.d_to)} against {day_text(window.prev_from)} to {day_text(window.prev_to)}")]
        out.append(_finding("cvr_drop", sev, ENOUGH if enough else THIN, channel=_channel(g), campaign=_campaign(g), currency=_ccy(g),
                            what=f"{_label(g)}: the conversion rate fell from {before:.2f}% to {now:.2f}%.",
                            evidence=ev, magnitude=rel,
                            action="Check the landing page and the tracking first (a rate that halves while clicks hold steady is often a broken form or tag), then review the audience or the creative."))
    return _result(out)


# ============================================================================ rule 4: stale channel / data gaps (currency-free)
def _gaps(days: Sequence[date], grain: str) -> list[tuple[date, date, int]]:
    """Runs of missing days between two rows of a daily channel (>= HOLE_MIN_DAYS), or missing weeks of a weekly channel (>= 1).
    Returns [(first missing day, last missing day, missing days)]. Leading and trailing gaps are not holes: stale_channel says them."""
    ds = sorted(set(days))
    out = []
    for a, b in zip(ds, ds[1:]):
        step = (b - a).days
        if grain == "week":
            if step > 7:
                missing_weeks = -(-step // 7) - 1
                if missing_weeks >= 1:
                    out.append((a + timedelta(days=7), b - timedelta(days=1), missing_weeks * 7))
        elif step - 1 >= HOLE_MIN_DAYS:
            out.append((a + timedelta(days=1), b - timedelta(days=1), step - 1))
    return out


def rule_channel_data(spans: Sequence[Span], window: Window) -> dict:
    if not spans:
        return _skipped("no channel has a rollup row up to the end of this window")
    by_channel: dict[str, list[Span]] = {}
    for s in spans:
        by_channel.setdefault(s.channel_key, []).append(s)
    out = []
    for key in sorted(by_channel):
        group = by_channel[key]
        name = group[0].channel_name
        ch = {"key": key, "name": name}
        # ---- stale: every grain the channel has is silent for longer than its cadence allows
        latest = max(group, key=lambda s: s.last_day)
        limits = [(s, STALE_DAYS_WEEKLY if s.grain == "week" else STALE_DAYS) for s in group]
        silent = {id(s): (window.d_to - s.last_day).days for s in group}
        if all(silent[id(s)] > lim for s, lim in limits):
            lim = STALE_DAYS_WEEKLY if latest.grain == "week" else STALE_DAYS
            gap = silent[id(latest)]
            weekly = latest.grain == "week"
            enough = latest.history_days >= (STALE_ENOUGH_WEEKS if weekly else STALE_ENOUGH_ROWS)
            outside = latest.last_day < window.d_from
            ev = [cmp_item("Days without a row", gap, ">", lim,
                           f"newest row {day_text(latest.last_day)}, window ends {day_text(window.d_to)}: {_n(gap)} days without a row > {lim} days allowed for a {'weekly' if weekly else 'daily'} channel"),
                  cmp_item("History", latest.history_days, ">=", STALE_ENOUGH_WEEKS if weekly else STALE_ENOUGH_ROWS,
                           f"{_n(latest.history_days)} {'week' if weekly else 'day'} rows before it stopped ({'>=' if enough else '<'} {STALE_ENOUGH_WEEKS if weekly else STALE_ENOUGH_ROWS} needed for 'enough data')", met=enough),
                  note_item("First row", latest.first_day.isoformat(), f"first row {day_text(latest.first_day)}")]
            historical = gap > STALE_HISTORICAL_DAYS
            ev.append(cmp_item("Historical cut-off", gap, ">", STALE_HISTORICAL_DAYS,
                               f"{_n(gap)} days without a row {'>' if historical else '<='} {STALE_HISTORICAL_DAYS} days: " + ("historical / no longer reporting, so info" if historical else "a recent gap, so a warning"), met=historical))
            if historical:
                out.append(_finding("stale_channel", "info", ENOUGH if enough else THIN, channel=ch, campaign=None, currency=None,
                                    what=f"{name} looks like an ended or archived feed (historical, no longer reporting): its newest row is {day_text(latest.last_day)}, {_n(gap)} days before the end of this window - not a fresh outage.",
                                    evidence=ev, magnitude=gap, extra="stale:historical",
                                    action="Confirm the feed is retired or re-connect it. If it was retired, filter it out of the view."))
            else:
                out.append(_finding("stale_channel", "warn", ENOUGH if enough else THIN, channel=ch, campaign=None, currency=None,
                                    what=f"{name} has sent nothing for {_n(gap)} days" + (" - not a single row in this window." if outside else " before the end of this window."),
                                    evidence=ev, magnitude=gap, extra="stale",
                                    action="Check whether the source still delivers (the export, the inbox folder, the last load and rollup run). If the channel was retired, filter it out of the view."))
        # ---- holes inside the window
        for s in group:
            if len(s.window_days) < 2:
                continue
            gaps = _gaps(s.window_days, s.grain)
            if not gaps:
                continue
            weekly = s.grain == "week"
            missing = sum(g[2] for g in gaps)
            share = missing / window.days * 100
            enough = len(s.window_days) >= (HOLE_ENOUGH_WEEKS if weekly else HOLE_ENOUGH_ROWS)
            listed = ", ".join(f"{day_text(a) if a == b else day_text(a) + ' to ' + day_text(b)} ({_n(n)} day{'s' if n != 1 else ''})" for a, b, n in gaps[:HOLE_LISTED])
            more = f" and {len(gaps) - HOLE_LISTED} more" if len(gaps) > HOLE_LISTED else ""
            ev = [cmp_item("Missing days", missing, ">=", 1, f"{_n(missing)} day{'s' if missing != 1 else ''} without a row between the first and last row in the window = {_pct(share)} of its {window.days} days"),
                  cmp_item("Gaps", len(gaps), ">=", 1, f"{len(gaps)} gap{'s' if len(gaps) != 1 else ''}: {listed}{more}; a gap is {'at least one missing week' if weekly else f'{HOLE_MIN_DAYS} or more days in a row'}"),
                  cmp_item("Rows in the window", len(s.window_days), ">=", HOLE_ENOUGH_WEEKS if weekly else HOLE_ENOUGH_ROWS,
                           f"{len(s.window_days)} {'week' if weekly else 'day'} rows ({'>=' if enough else '<'} {HOLE_ENOUGH_WEEKS if weekly else HOLE_ENOUGH_ROWS} needed for 'enough data')", met=enough)]
            out.append(_finding("stale_channel", "warn" if share >= HOLE_WARN_SHARE_PCT else "info", ENOUGH if enough else THIN, channel=ch, campaign=None, currency=None,
                                what=f"{name} has {len(gaps)} hole{'s' if len(gaps) != 1 else ''} in this window: {_n(missing)} missing day{'s' if missing != 1 else ''}.",
                                evidence=ev, magnitude=share, extra="holes:" + s.grain,
                                action="Look for a missed export or a paused campaign on those days; load the missing files and run the rollup again. Until then the trend lines break there instead of showing a zero."))
    return _result(out)


# ============================================================================ rule 5: concentration risk
def rule_concentration(groups: Sequence[Group], currency: str, window: Window) -> dict:
    spending = [g for g in groups if g.cur.spend_micros > 0]
    if len(spending) < CONC_MIN_CAMPAIGNS:
        return _skipped(f"needs at least {CONC_MIN_CAMPAIGNS} campaigns with spend in {currency}")
    total = sum(g.cur.spend_micros for g in spending)
    top = max(spending, key=lambda g: (g.cur.spend_micros, g.campaign_id))
    share = top.cur.spend_micros / total * 100
    if share <= CONC_WARN_PCT:
        return _result([])
    clicks = sum(g.cur.clicks for g in spending)
    enough = clicks >= CONC_ENOUGH_CLICKS
    ev = [cmp_item("Share of spend", round(share, 2), ">", CONC_WARN_PCT,
                   f"{_label(top)} = {_money(top.cur.spend, currency)} of {_money(total / MICROS, currency)} = {_pct(share)} of all {currency} spend, warning above {_pct(CONC_WARN_PCT)}, critical above {_pct(CONC_CRIT_PCT)}"),
          cmp_item("Campaigns with spend", len(spending), ">=", CONC_MIN_CAMPAIGNS, f"{len(spending)} campaigns share the {currency} spend"),
          cmp_item("Clicks", clicks, ">=", CONC_ENOUGH_CLICKS, f"{_n(clicks)} clicks across them ({'>=' if enough else '<'} {CONC_ENOUGH_CLICKS} needed for 'enough data')", met=enough)]
    return _result([_finding("concentration_risk", "crit" if share > CONC_CRIT_PCT else "warn", ENOUGH if enough else THIN, channel=_channel(top), campaign=_campaign(top), currency=currency,
                             what=f"{_label(top)} carries {_pct(share)} of the {currency} spend.",
                             evidence=ev, magnitude=share,
                             action="Consider moving part of the budget to a second campaign that already converts, so one campaign's bad week cannot take out most of the results. Check the others convert first.")])


# ============================================================================ rule 6: campaigns worth scaling
def rule_scale_candidates(groups: Sequence[Group], currency: str, window: Window) -> dict:
    judged = [g for g in groups if g.cur.spend_micros > 0 and g.cur.conversions > 0]
    if len(judged) < SCALE_MIN_CAMPAIGNS:
        return _skipped(f"needs at least {SCALE_MIN_CAMPAIGNS} campaigns with spend and conversions in {currency}")
    pooled = sum(g.cur.spend_micros for g in judged) / MICROS / sum(g.cur.conversions for g in judged)
    picks = []
    for g in judged:
        c = g.cur
        if c.conversions < SCALE_MIN_CONV:
            continue                                                       # thin data: never advise scaling on it
        now = c.cpa()
        better = (pooled - now) / pooled * 100
        before = g.prev.cpa() if g.prev.conversions >= SCALE_MIN_CONV else None
        improve = (before - now) / before * 100 if before else None
        if better >= SCALE_BETTER_PCT or (improve is not None and improve >= SCALE_IMPROVE_PCT and now <= pooled):
            picks.append((now, g, better, improve, before))
    picks.sort(key=lambda t: (t[0], t[1].campaign_id))
    out = []
    for now, g, better, improve, before in picks[:SCALE_MAX]:
        c = g.cur
        ev = [cmp_item("Cost per conversion vs the pooled cost", round(better, 2), ">=", SCALE_BETTER_PCT,
                       f"{_money(now, currency)} per conversion vs {_money(pooled, currency)} pooled over the {len(judged)} {currency} campaigns that convert = {_pct(abs(better))} {'better' if better >= 0 else 'worse'}; needs {_pct(SCALE_BETTER_PCT)} better", met=better >= SCALE_BETTER_PCT),
              cmp_item("Conversions", c.conversions, ">=", SCALE_MIN_CONV, f"{_n(c.conversions)} conversions >= {SCALE_MIN_CONV} minimum")]
        if improve is not None:
            ev.append(cmp_item("Improvement vs the previous window", round(improve, 2), ">=", SCALE_IMPROVE_PCT,
                               f"{_money(before, currency)} before, {_money(now, currency)} now = {_pct(abs(improve))} {'better' if improve >= 0 else 'worse'}; the improver test needs {_pct(SCALE_IMPROVE_PCT)} and a cost no worse than the pooled one", met=improve >= SCALE_IMPROVE_PCT))
        why = f"{_pct(better)} cheaper per conversion than the {currency} average" if better >= SCALE_BETTER_PCT else f"its cost per conversion improved {_pct(improve or 0)}"
        out.append(_finding("scale_candidates", "info", ENOUGH, channel=_channel(g), campaign=_campaign(g), currency=currency,
                            what=f"{_label(g)} is worth a bigger budget: {_money(now, currency)} per conversion, {why}.",
                            evidence=ev, magnitude=max(better, improve or 0),
                            action="A candidate for a larger budget: raise it in small steps (for example 20%) and watch that the cost per conversion holds."))
    return _result(out)


CURRENCY_RULES = (("spend_no_conversions", rule_spend_no_conversions), ("cpa_jump", rule_cpa_jump), ("cvr_drop", rule_cvr_drop),
                  ("concentration_risk", rule_concentration), ("scale_candidates", rule_scale_candidates))


def rules_for_currency(groups: Sequence[Group], currency: str, window: Window) -> dict[str, dict]:
    """Every money rule on the rows of ONE currency. Refuses (MixedCurrencyError) a group of another currency: the engine never
    adds or compares money across currencies. Returns {rule id: result}."""
    bad = sorted({g.currency for g in groups if g.currency != currency})
    if bad:
        raise MixedCurrencyError(f"these rows are in {', '.join(bad)} but the rules are running for {currency}: money of different currencies is never mixed")
    return {rid: fn(groups, currency, window) for rid, fn in CURRENCY_RULES}


# ============================================================================ the whole engine
def _sort_key(f: dict):
    return (_SEV_RANK[f["severity"]], 1 if f["confidence"] == THIN else 0, -f["magnitude"], f["id"], (f["channel"] or {}).get("name", ""), (f["campaign"] or {}).get("name", ""), f["currency"] or "")


def analyse(groups: Iterable[Group], spans: Sequence[Span], window: Window) -> dict:
    """All rules over the rows in view. Money rules run once per currency (the rows are split by currency first and nothing crosses);
    the channel-data rule is currency-free. Returns {findings (sorted: critical first), notes, rules [{id, title, status, why, fired}],
    currencies, has_previous}."""
    groups = list(groups)
    by_cur: dict[str, list[Group]] = {}
    for g in groups:
        by_cur.setdefault(g.currency, []).append(g)
    results: dict[str, list[tuple[str, dict]]] = {rid: [] for rid, _t in RULES}
    for cur in sorted(by_cur):
        for rid, res in rules_for_currency(by_cur[cur], cur, window).items():
            results[rid].append((cur, res))
    results["stale_channel"].append(("", rule_channel_data(spans, window)))
    findings, notes, statuses = [], [], []
    for rid, title in RULES:
        rs = results[rid]
        ran = [r for _c, r in rs if r["ran"]]
        why = next((r["why"] for _c, r in rs if not r["ran"] and r["why"]), "no data in view") if not ran else None
        fired = sum(len(r["findings"]) for _c, r in rs)
        for _c, r in rs:
            findings.extend(r["findings"])
            notes.extend(r["notes"])
        statuses.append({"id": rid, "title": title, "status": "ran" if ran else "skipped", "why": why, "fired": fired})
    findings.sort(key=_sort_key)
    return {"findings": findings, "notes": notes, "rules": statuses, "currencies": sorted(c for c, gs in by_cur.items() if any(g.has_money for g in gs)),
            "has_previous": any(g.prev.days > 0 for g in groups)}


def counts(findings: Iterable[dict]) -> dict:
    out = {"crit": 0, "warn": 0, "info": 0, "thin": 0}
    for f in findings:
        out[f["severity"]] += 1
        out["thin"] += 1 if f["confidence"] == THIN else 0
    return out


# ============================================================================ automation ideas (text only)
IDEAS = {
    "spend_no_conversions": ("Weekly spend-without-conversions alert",
                             "Every Monday, list the campaigns whose spend share is at least {share}% and that had {clicks}+ clicks and 0 conversions in the last 7 days, and send the list to the marketing owner."),
    "cpa_jump": ("Weekly cost-per-conversion check",
                 "Every Monday, compare each campaign's cost per conversion for the last 7 days with the 7 days before and flag a rise of {pct}% or more (only with {conv}+ conversions in each week)."),
    "cvr_drop": ("Weekly conversion-rate watch",
                 "Every Monday, compare the conversion rate of each campaign with the week before and flag a fall of {pct}% or more (only with {clicks}+ clicks in each week)."),
    "stale_channel": ("Daily stale-channel check",
                      "Every morning, list the channels whose newest rollup row is more than {days} days old (weekly sources: {wdays}) so a stopped export is noticed the same day."),
    "concentration_risk": ("Monthly budget-concentration review",
                           "On the first of the month, list every currency in which one campaign holds more than {pct}% of the spend, with the runner-up campaigns that already convert."),
    "scale_candidates": ("Monthly scale-up shortlist",
                         "On the first of the month, list the campaigns whose cost per conversion is {pct}%+ better than their currency's average, as candidates for a budget increase."),
}


def _idea_text(rid: str) -> str:
    fill = {"share": f"{NOCONV_MIN_SHARE_PCT:g}", "clicks": str(NOCONV_ENOUGH_CLICKS if rid == "spend_no_conversions" else CVR_ENOUGH_CLICKS), "pct": f"{CPA_JUMP_WARN_PCT:g}",
            "conv": str(CPA_ENOUGH_CONV), "days": str(STALE_DAYS), "wdays": str(STALE_DAYS_WEEKLY)}
    if rid == "cvr_drop":
        fill["pct"] = f"{CVR_DROP_WARN_PCT:g}"
    elif rid == "concentration_risk":
        fill["pct"] = f"{CONC_WARN_PCT:g}"
    elif rid == "scale_candidates":
        fill["pct"] = f"{SCALE_BETTER_PCT:g}"
    return IDEAS[rid][1].format(**fill)


def automation_ideas(findings: Iterable[dict]) -> list[dict]:
    """Recurring findings -> concrete automation candidates, as plain text. Recurring = the rule fired for at least RECURRING_MIN
    different campaigns / channels as warn or crit, or once as critical. Nothing here schedules or runs anything."""
    by_rule: dict[str, list[dict]] = {}
    for f in findings:
        if f["severity"] in ("warn", "crit") and f["id"] in IDEAS:
            by_rule.setdefault(f["id"], []).append(f)
    out = []
    for rid, _t in RULES:
        fs = by_rule.get(rid) or []
        if len(fs) >= RECURRING_MIN or any(f["severity"] == "crit" for f in fs):
            names = sorted({(f["campaign"] or f["channel"] or {}).get("name", "") for f in fs})
            out.append({"id": rid, "title": IDEAS[rid][0], "text": _idea_text(rid), "based_on": f"{len(fs)} finding{'s' if len(fs) != 1 else ''}: " + ", ".join(names[:4]) + (" ..." if len(names) > 4 else ""),
                        "count": len(fs)})
    return out


# ============================================================================ the "How these are decided" text (generated from the constants)
def thresholds() -> list[dict]:
    """Every named threshold with its value and meaning: the page's drawer and the Excel Definitions sheet are built from THIS list."""
    def t(rule, name, value, unit, meaning):
        return {"rule": rule, "name": name, "value": value, "unit": unit, "meaning": meaning}
    return [
        t("spend_no_conversions", "NOCONV_MIN_SHARE_PCT", NOCONV_MIN_SHARE_PCT, "% of spend", "the campaign holds at least this share of its currency's spend in the window"),
        t("spend_no_conversions", "NOCONV_FLOOR_CLICKS", NOCONV_FLOOR_CLICKS, "clicks", "below this many clicks nothing is said"),
        t("spend_no_conversions", "NOCONV_ENOUGH_CLICKS", NOCONV_ENOUGH_CLICKS, "clicks", "this many clicks make it 'enough data' (fewer = thin data, info only)"),
        t("spend_no_conversions", "NOCONV_CRIT_SHARE_PCT", NOCONV_CRIT_SHARE_PCT, "% of spend", "a share at or above this is critical, a smaller one a warning"),
        t("cpa_jump", "CPA_JUMP_WARN_PCT", CPA_JUMP_WARN_PCT, "% rise", "cost per conversion up by at least this much against the previous equal window = warning"),
        t("cpa_jump", "CPA_JUMP_CRIT_PCT", CPA_JUMP_CRIT_PCT, "% rise", "up by at least this much = critical"),
        t("cpa_jump", "CPA_FLOOR_CONV", CPA_FLOOR_CONV, "conversions", "needs this many conversions in each window, else nothing is said"),
        t("cpa_jump", "CPA_ENOUGH_CONV", CPA_ENOUGH_CONV, "conversions", "this many in each window make it 'enough data' (fewer = thin data, info only)"),
        t("cvr_drop", "CVR_DROP_WARN_PCT", CVR_DROP_WARN_PCT, "% fall", "conversion rate down by at least this much (relative) = warning"),
        t("cvr_drop", "CVR_DROP_CRIT_PCT", CVR_DROP_CRIT_PCT, "% fall", "down by at least this much = critical"),
        t("cvr_drop", "CVR_DROP_MIN_POINTS", CVR_DROP_MIN_POINTS, "percentage points", "and by at least this many points (a smaller fall is noise)"),
        t("cvr_drop", "CVR_FLOOR_CLICKS", CVR_FLOOR_CLICKS, "clicks", "needs this many clicks in each window, else nothing is said"),
        t("cvr_drop", "CVR_ENOUGH_CLICKS", CVR_ENOUGH_CLICKS, "clicks", "this many in each window make it 'enough data' (fewer = thin data, info only)"),
        t("stale_channel", "STALE_DAYS", STALE_DAYS, "days", "a daily channel whose newest row is more than this many days before the window end is stale"),
        t("stale_channel", "STALE_DAYS_WEEKLY", STALE_DAYS_WEEKLY, "days", "the same for a weekly channel (one row a week)"),
        t("stale_channel", "STALE_HISTORICAL_DAYS", STALE_HISTORICAL_DAYS, "days", "a channel silent for more than this many days is 'historical / no longer reporting' (an ended or archived feed): an info, not a warning"),
        t("stale_channel", "STALE_ENOUGH_ROWS", STALE_ENOUGH_ROWS, "day rows", "a channel with this much history is 'enough data' (weekly: STALE_ENOUGH_WEEKS = %d weeks)" % STALE_ENOUGH_WEEKS),
        t("stale_channel", "HOLE_MIN_DAYS", HOLE_MIN_DAYS, "days in a row", "a hole inside the window: this many missing days between two rows of a daily channel (weekly: one missing week)"),
        t("stale_channel", "HOLE_WARN_SHARE_PCT", HOLE_WARN_SHARE_PCT, "% of the window", "holes covering at least this share of the window are a warning, less an info"),
        t("stale_channel", "HOLE_ENOUGH_ROWS", HOLE_ENOUGH_ROWS, "rows", "rows in the window for 'enough data' (weekly: HOLE_ENOUGH_WEEKS = %d weeks)" % HOLE_ENOUGH_WEEKS),
        t("concentration_risk", "CONC_WARN_PCT", CONC_WARN_PCT, "% of spend", "one campaign above this share of a currency's spend = warning"),
        t("concentration_risk", "CONC_CRIT_PCT", CONC_CRIT_PCT, "% of spend", "above this share = critical"),
        t("concentration_risk", "CONC_MIN_CAMPAIGNS", CONC_MIN_CAMPAIGNS, "campaigns", "needs this many campaigns with spend in the currency (one campaign is always 100%)"),
        t("concentration_risk", "CONC_ENOUGH_CLICKS", CONC_ENOUGH_CLICKS, "clicks", "clicks across those campaigns for 'enough data'"),
        t("scale_candidates", "SCALE_BETTER_PCT", SCALE_BETTER_PCT, "% cheaper", "cost per conversion at least this much better than the currency's pooled cost per conversion"),
        t("scale_candidates", "SCALE_IMPROVE_PCT", SCALE_IMPROVE_PCT, "% cheaper", "or improved by at least this much against the previous window (and not worse than the pooled cost)"),
        t("scale_candidates", "SCALE_MIN_CONV", SCALE_MIN_CONV, "conversions", "needs this many conversions; with fewer the engine never advises scaling"),
        t("scale_candidates", "SCALE_MIN_CAMPAIGNS", SCALE_MIN_CAMPAIGNS, "campaigns", "needs this many campaigns with spend and conversions in the currency to compare"),
        t("scale_candidates", "SCALE_MAX", SCALE_MAX, "campaigns", "at most this many candidates per currency"),
        t("automation", "RECURRING_MIN", RECURRING_MIN, "findings", "an automation idea appears when a rule fired this many times as warn or crit, or once as critical"),
    ]


def rule_texts() -> list[dict]:
    """One plain sentence per rule: what it looks at and when it stays quiet (for the drawer and the Definitions sheet)."""
    return [
        {"id": "spend_no_conversions", "title": RULE_TITLE["spend_no_conversions"],
         "text": f"A campaign that holds at least {NOCONV_MIN_SHARE_PCT:g}% of its currency's spend in the window, had at least {NOCONV_FLOOR_CLICKS} clicks and 0 conversions. Only judged when its channel reports conversions somewhere (otherwise a 0 means the source has no conversion field: the campaign is listed as a note, not accused). Critical from {NOCONV_CRIT_SHARE_PCT:g}% of spend, a warning below; with fewer than {NOCONV_ENOUGH_CLICKS} clicks it is 'thin data' and only info."},
        {"id": "cpa_jump", "title": RULE_TITLE["cpa_jump"],
         "text": f"Cost per conversion (spend / conversions, in the campaign's own currency) is compared with the previous window of the same length. A rise of {CPA_JUMP_WARN_PCT:g}% is a warning, {CPA_JUMP_CRIT_PCT:g}% critical. Needs spend in both windows and at least {CPA_FLOOR_CONV} conversions in each; under {CPA_ENOUGH_CONV} in either it is 'thin data' (info only)."},
        {"id": "cvr_drop", "title": RULE_TITLE["cvr_drop"],
         "text": f"Conversion rate (conversions / clicks) is compared with the previous window. A relative fall of {CVR_DROP_WARN_PCT:g}% (and at least {CVR_DROP_MIN_POINTS:g} points) is a warning, {CVR_DROP_CRIT_PCT:g}% critical. Needs at least {CVR_FLOOR_CLICKS} clicks in each window; under {CVR_ENOUGH_CLICKS} in either it is 'thin data' (info only)."},
        {"id": "stale_channel", "title": RULE_TITLE["stale_channel"],
         "text": f"A channel whose newest rollup row (up to the window end) is more than {STALE_DAYS} days old ({STALE_DAYS_WEEKLY} for a weekly source) is a warning, unless it is silent for more than {STALE_HISTORICAL_DAYS} days: then it looks like an ended or archived feed (historical, no longer reporting) and is only an info that suggests confirming it is retired or re-connecting it. The rule also flags a run of {HOLE_MIN_DAYS}+ missing days between two rows inside the window (weekly: a missing week). The rollup writes no zero rows, so a missing day means 'no activity or nothing loaded' - the rule says which days, it cannot tell why. Channels with little history are 'thin data'."},
        {"id": "concentration_risk", "title": RULE_TITLE["concentration_risk"],
         "text": f"One campaign carries more than {CONC_WARN_PCT:g}% of a currency's spend ({CONC_CRIT_PCT:g}% critical) when at least {CONC_MIN_CAMPAIGNS} campaigns spend in that currency. Each currency is judged alone."},
        {"id": "scale_candidates", "title": RULE_TITLE["scale_candidates"],
         "text": f"Campaigns with at least {SCALE_MIN_CONV} conversions whose cost per conversion is {SCALE_BETTER_PCT:g}% better than their currency's pooled cost per conversion, or that improved {SCALE_IMPROVE_PCT:g}% against the previous window without being worse than the pool. At most {SCALE_MAX} per currency, always info, never on thin data."},
    ]


# ============================================================================ PLACEMENT RULES (display-network placements: a site, an app or a banner slot)
# Five more explainable rules that read the PLACEMENT rollup of one campaign in one currency over the analysed window. They live beside the six
# campaign rules but run through their own entry point, analyse_placements(): the six above and their outputs are unchanged, and a database
# without the placement rollup simply never calls this. Same honesty rules: money is judged per currency, a placement never counts as
# guilty on a sample too small to tell (a floor says nothing, 'enough' makes it a real finding, in between it is info + 'thin data'), a blank
# is unknown and never 0, and NOTHING here changes anything: every finding is a suggestion with the numbers behind it.
PLC_EXCL_MIN_SHARE_PCT = 5.0          # placement_exclude (no conversions): the placement holds at least this share of its campaign's spend in the currency ...
PLC_EXCL_CRIT_SHARE_PCT = 20.0        # ... a share at or above this (with enough data) is critical, a smaller one a warning
PLC_EXCL_FLOOR_CLICKS = 20            # ... needs at least this many clicks (below it: nothing is said)
PLC_EXCL_ENOUGH_CLICKS = 100          # ... at least this many clicks make it 'enough data' (fewer = thin data, info only)
PLC_EXCL_MAX_LUCK_PCT = 5.0           # ... and the chance of ZERO conversions from that many clicks, at the campaign's own conversion rate without it, must be at most this
PLC_EXCL_MIN_CAMPAIGN_CONV = 5        # ... and the campaign itself needs at least this many conversions (otherwise a 0 means 'the tracking may be missing', not 'wasteful')
PLC_CTR_FACTOR_PCT = 25.0             # placement_exclude (low CTR): the placement's CTR is at most this share of the campaign's CTR without it ...
PLC_CTR_MIN_SHARE_PCT = 2.0           # ... it holds at least this share of the campaign's spend ...
PLC_CTR_FLOOR_IMPRESSIONS = 5000      # ... and has at least this many impressions (below it: nothing is said)
PLC_CTR_ENOUGH_IMPRESSIONS = 20000    # ... at least this many make it 'enough data' (fewer = thin data, info only)
PLC_SCALE_BETTER_PCT = 30.0           # placement_scale: cost per conversion at least this much lower than the campaign's cost per conversion without the placement ...
PLC_SCALE_FLOOR_CONV = 3              # ... needs at least this many conversions (below it: nothing is said)
PLC_SCALE_MIN_CONV = 5                # ... at least this many make it 'enough data' (3-4 = thin data, info only)
PLC_SCALE_MAX = 3                     # ... at most this many candidates per campaign and currency
PLC_VIEW_LOW_PCT = 40.0               # placement_viewability: viewable / measured impressions below this ...
PLC_VIEW_MIN_SHARE_PCT = 2.0          # ... on a placement holding at least this share of the campaign's spend ...
PLC_VIEW_FLOOR_IMPRESSIONS = 5000     # ... with at least this many MEASURED impressions (below it: nothing is said)
PLC_VIEW_ENOUGH_IMPRESSIONS = 20000   # ... at least this many make it 'enough data' (fewer = thin data, info only)
PLC_VIEW_MIN_COVERAGE_PCT = 50.0      # ... and the measured impressions must be at least this share of all the placement's impressions (else the rate is not representative)
PLC_CONC_TOP_N = 3                    # placement_concentration: the largest N placements together carry at least ...
PLC_CONC_WARN_PCT = 80.0              # ... this share of the campaign's spend = warning ...
PLC_CONC_CRIT_PCT = 95.0              # ... this share = critical
PLC_CONC_MIN_PLACEMENTS = 6           # ... needs at least this many named placements with spend (few placements are always concentrated)
PLC_CONC_ENOUGH_CLICKS = 200          # ... at least this many clicks in the campaign make it 'enough data'
PLC_REALLOC_MIN_CONV = 10             # placement_reallocation (estimate): the receiving group needs at least this many conversions (the sending group needs 'enough' clicks: PLC_EXCL_ENOUGH_CLICKS)
PLC_REALLOC_MAX_GROWTH = 1.0          # ... and the receiving group's spend is assumed to grow at most by this factor (1.0 = it can at most double): a cap, not saturation modelling

PLACEMENT_RULES = (
    ("placement_exclude", "Placements to consider excluding"),
    ("placement_scale", "Placements worth more budget"),
    ("placement_viewability", "Low viewability"),
    ("placement_concentration", "Spend concentrated in few placements"),
    ("placement_reallocation", "Budget reallocation estimate"),
)
RULE_TITLE.update(dict(PLACEMENT_RULES))
OTHER_PLACEMENTS = "(other placements)"


@dataclass(frozen=True)
class PlacementStat:
    """One placement (a site, app or banner slot) of one campaign in one currency over the analysed window. `viewable` counts only the
    rows that REPORTED viewability and `viewable_base` is the impressions of exactly those rows (blank = not measured, never 0)."""
    name: str
    key: str = ""                # '' = the cardinality guard's "other placements" bucket: counted in the totals, never judged on its own
    ptype: str = "other"
    impressions: int = 0
    clicks: int = 0
    conversions: int = 0
    spend_micros: int = 0
    viewable: int = 0
    viewable_base: int = 0

    @property
    def is_other(self) -> bool:
        return self.key == ""

    @property
    def spend(self) -> float:
        return self.spend_micros / MICROS

    def ctr(self) -> float | None:
        return None if self.impressions <= 0 else self.clicks / self.impressions * 100

    def cpa(self) -> float | None:
        return None if self.spend_micros <= 0 or self.conversions <= 0 else self.spend_micros / MICROS / self.conversions

    def viewability(self) -> float | None:
        return None if self.viewable_base <= 0 else self.viewable / self.viewable_base * 100


@dataclass(frozen=True)
class PlacementGroup:
    """All the placements of one campaign of one channel in one currency."""
    channel_key: str
    channel_name: str
    campaign_id: int
    campaign_name: str
    currency: str
    placements: tuple = ()
    folded: int = 0              # distinct placements folded into the "other" bucket by the cardinality guard (whole campaign)

    def totals(self) -> dict:
        p = self.placements
        return {"impressions": sum(x.impressions for x in p), "clicks": sum(x.clicks for x in p), "conversions": sum(x.conversions for x in p),
                "spend_micros": sum(x.spend_micros for x in p)}


def _pch(g: PlacementGroup) -> dict:
    return {"key": g.channel_key, "name": g.channel_name}


def _pcamp(g: PlacementGroup) -> dict:
    return {"id": g.campaign_id, "name": g.campaign_name}


def _plabel(g: PlacementGroup, p: PlacementStat) -> str:
    return f"{p.name} ({'no campaign' if g.campaign_id == 0 else g.campaign_name})"


def _pfinding(rule: str, severity: str, confidence: str, g: PlacementGroup, p: PlacementStat | None, *, what: str, evidence: list, action: str,
              magnitude: float, extra: str = "", estimate: bool = False) -> dict:
    f = _finding(rule, severity, confidence, channel=_pch(g), campaign=_pcamp(g), currency=g.currency, what=what, evidence=evidence, action=action,
                 magnitude=magnitude, extra=(p.key if p else "") + (":" + extra if extra else ""))
    f["placement"] = None if p is None else {"name": p.name, "key": p.key, "type": p.ptype}
    f["estimate"] = estimate
    return f


def _named(g: PlacementGroup) -> list[PlacementStat]:
    return [p for p in g.placements if not p.is_other]


def _exclusion_candidates(g: PlacementGroup) -> tuple[list, list]:
    """([(placement, share %, luck %, enough)] with no conversion, [notes]). Shared by placement_exclude and the reallocation estimate."""
    tot = g.totals()
    if tot["spend_micros"] <= 0:
        return [], []
    out, notes = [], []
    campaign_convs_ok = tot["conversions"] >= PLC_EXCL_MIN_CAMPAIGN_CONV
    for p in _named(g):
        if p.conversions > 0 or p.spend_micros <= 0 or p.clicks < PLC_EXCL_FLOOR_CLICKS:
            continue
        share = p.spend_micros / tot["spend_micros"] * 100
        if share < PLC_EXCL_MIN_SHARE_PCT:
            continue
        if not campaign_convs_ok:
            notes.append(f"{_plabel(g, p)} spent {_money(p.spend, g.currency)} with no conversions, but its whole campaign has only {_n(tot['conversions'])} "
                         f"conversion{'s' if tot['conversions'] != 1 else ''} (fewer than {PLC_EXCL_MIN_CAMPAIGN_CONV}), so a zero here may mean missing tracking, not a wasteful placement: no verdict.")
            continue
        other_clicks = tot["clicks"] - p.clicks
        if other_clicks <= 0:
            continue
        rate = tot["conversions"] / other_clicks
        luck = (max(0.0, 1.0 - rate)) ** p.clicks * 100 if rate < 1 else 0.0
        if luck > PLC_EXCL_MAX_LUCK_PCT:
            continue
        out.append((p, share, luck, p.clicks >= PLC_EXCL_ENOUGH_CLICKS))
    return out, notes


def _scale_candidates(g: PlacementGroup) -> list:
    """[(placement, cost per conversion, the campaign's cost per conversion without it, % better, enough)], best first."""
    tot = g.totals()
    named = [p for p in _named(g) if p.spend_micros > 0 and p.conversions > 0]
    if len(named) < 2:
        return []
    out = []
    for p in named:
        if p.conversions < PLC_SCALE_FLOOR_CONV:
            continue
        o_conv, o_spend = tot["conversions"] - p.conversions, tot["spend_micros"] - p.spend_micros
        if o_conv <= 0 or o_spend <= 0:
            continue
        others = o_spend / MICROS / o_conv
        better = (others - p.cpa()) / others * 100
        if better >= PLC_SCALE_BETTER_PCT:
            out.append((p, p.cpa(), others, better, p.conversions >= PLC_SCALE_MIN_CONV))
    out.sort(key=lambda t: (t[1], t[0].name))
    return out[:PLC_SCALE_MAX]


def rule_placement_exclude(g: PlacementGroup, window: Window) -> dict:
    tot = g.totals()
    if tot["spend_micros"] <= 0 or not _named(g):
        return _skipped("no placement has spend in this campaign and window")
    out = []
    cands, notes = _exclusion_candidates(g)
    for p, share, luck, enough in cands:
        sev = "crit" if (share >= PLC_EXCL_CRIT_SHARE_PCT and enough) else "warn"
        other_clicks = tot["clicks"] - p.clicks
        rate = tot["conversions"] / other_clicks
        ev = [cmp_item("Spend share", round(share, 2), ">=", PLC_EXCL_MIN_SHARE_PCT,
                       f"{_money(p.spend, g.currency)} = {_pct(share)} of the campaign's {g.currency} spend ({_money(tot['spend_micros'] / MICROS, g.currency)}), threshold {_pct(PLC_EXCL_MIN_SHARE_PCT)}, critical from {_pct(PLC_EXCL_CRIT_SHARE_PCT)}"),
              cmp_item("Conversions", 0, "==", 0, f"0 conversions from {_n(p.clicks)} clicks in {day_text(window.d_from)} to {day_text(window.d_to)}"),
              cmp_item("Chance of zero by luck", round(luck, 3), "<=", PLC_EXCL_MAX_LUCK_PCT,
                       f"the rest of the campaign converts {rate * 100:.2f}% of its clicks ({_n(tot['conversions'])} of {_n(other_clicks)}); at that rate {_n(p.clicks)} clicks would give about {p.clicks * rate:.1f} conversions and the chance of none is {luck:.2f}%, limit {_pct(PLC_EXCL_MAX_LUCK_PCT)}"),
              cmp_item("Clicks", p.clicks, ">=", PLC_EXCL_ENOUGH_CLICKS if enough else PLC_EXCL_FLOOR_CLICKS,
                       f"{_n(p.clicks)} clicks {'>=' if enough else '<'} {PLC_EXCL_ENOUGH_CLICKS} needed for 'enough data'", met=enough)]
        out.append(_pfinding("placement_exclude", sev, ENOUGH if enough else THIN, g, p,
                             what=f"{p.name} took {_money(p.spend, g.currency)} ({_pct(share)} of the campaign's spend) and produced no conversions.",
                             evidence=ev, magnitude=share, extra="noconv",
                             action="Consider adding it to the campaign's placement exclusions, after checking the conversion tracking on that traffic. Nothing has been excluded: this is a suggestion."))
    for p in _named(g):                                                    # ---- CTR far below the rest of the campaign
        if p.impressions < PLC_CTR_FLOOR_IMPRESSIONS or p.spend_micros <= 0:
            continue
        share = p.spend_micros / tot["spend_micros"] * 100
        o_impr, o_clicks = tot["impressions"] - p.impressions, tot["clicks"] - p.clicks
        if share < PLC_CTR_MIN_SHARE_PCT or o_impr < PLC_CTR_FLOOR_IMPRESSIONS or o_clicks <= 0:
            continue
        others, mine = o_clicks / o_impr * 100, p.clicks / p.impressions * 100
        ratio_pct = mine / others * 100
        if ratio_pct > PLC_CTR_FACTOR_PCT:
            continue
        enough = p.impressions >= PLC_CTR_ENOUGH_IMPRESSIONS
        ev = [cmp_item("CTR versus the rest of the campaign", round(ratio_pct, 2), "<=", PLC_CTR_FACTOR_PCT,
                       f"CTR {mine:.3f}% ({_n(p.clicks)} clicks of {_n(p.impressions)} impressions) against {others:.3f}% for the rest of the campaign = {_pct(ratio_pct)} of it, limit {_pct(PLC_CTR_FACTOR_PCT)}"),
              cmp_item("Spend share", round(share, 2), ">=", PLC_CTR_MIN_SHARE_PCT, f"{_money(p.spend, g.currency)} = {_pct(share)} of the campaign's {g.currency} spend, threshold {_pct(PLC_CTR_MIN_SHARE_PCT)}"),
              cmp_item("Impressions", p.impressions, ">=", PLC_CTR_ENOUGH_IMPRESSIONS if enough else PLC_CTR_FLOOR_IMPRESSIONS,
                       f"{_n(p.impressions)} impressions {'>=' if enough else '<'} {_n(PLC_CTR_ENOUGH_IMPRESSIONS)} needed for 'enough data'", met=enough)]
        out.append(_pfinding("placement_exclude", "warn", ENOUGH if enough else THIN, g, p,
                             what=f"{p.name} is clicked far less than the rest of the campaign ({mine:.2f}% against {others:.2f}%).",
                             evidence=ev, magnitude=100 - ratio_pct, extra="lowctr",
                             action="Look at whether the banner size or position suits that place, or consider excluding it. Nothing has been changed: this is a suggestion."))
    return _result(out, notes)


def rule_placement_scale(g: PlacementGroup, window: Window) -> dict:
    tot = g.totals()
    with_conv = [p for p in _named(g) if p.spend_micros > 0 and p.conversions > 0]
    if len(with_conv) < 2:
        return _skipped("needs at least 2 placements with spend and conversions in the campaign to compare")
    out = []
    for p, now, others, better, enough in _scale_candidates(g):
        ev = [cmp_item("Cost per conversion versus the rest", round(better, 2), ">=", PLC_SCALE_BETTER_PCT,
                       f"{_money(now, g.currency)} per conversion against {_money(others, g.currency)} for the rest of the campaign = {_pct(better)} cheaper, needs {_pct(PLC_SCALE_BETTER_PCT)}"),
              cmp_item("Conversions", p.conversions, ">=", PLC_SCALE_MIN_CONV if enough else PLC_SCALE_FLOOR_CONV,
                       f"{_n(p.conversions)} conversions {'>=' if enough else '<'} {PLC_SCALE_MIN_CONV} needed for 'enough data'", met=enough),
              note_item("Spend", round(p.spend, 2), f"{_money(p.spend, g.currency)} of the campaign's {_money(tot['spend_micros'] / MICROS, g.currency)}")]
        out.append(_pfinding("placement_scale", "info", ENOUGH if enough else THIN, g, p,
                             what=f"{p.name} converts cheaply: {_money(now, g.currency)} per conversion, {_pct(better)} below the rest of the campaign.",
                             evidence=ev, magnitude=better,
                             action="A candidate for more budget or a higher bid: raise it in small steps and watch that the cost per conversion holds (a small placement may not absorb much more)."))
    return _result(out)


def rule_placement_viewability(g: PlacementGroup, window: Window) -> dict:
    tot = g.totals()
    measured = [p for p in _named(g) if p.viewable_base > 0]
    if not measured:
        return _skipped("no placement of this campaign reports viewability (a blank is unknown, not 0%)")
    out, notes = [], []
    for p in measured:
        if p.spend_micros <= 0 or tot["spend_micros"] <= 0 or p.viewable_base < PLC_VIEW_FLOOR_IMPRESSIONS:
            continue
        share = p.spend_micros / tot["spend_micros"] * 100
        rate = p.viewable / p.viewable_base * 100
        if share < PLC_VIEW_MIN_SHARE_PCT or rate >= PLC_VIEW_LOW_PCT:
            continue
        coverage = p.viewable_base / p.impressions * 100 if p.impressions else 0.0
        if coverage < PLC_VIEW_MIN_COVERAGE_PCT:
            notes.append(f"{_plabel(g, p)}: only {coverage:.0f}% of its impressions were measured for viewability (limit {PLC_VIEW_MIN_COVERAGE_PCT:g}%), so its {rate:.1f}% is not judged.")
            continue
        enough = p.viewable_base >= PLC_VIEW_ENOUGH_IMPRESSIONS
        ev = [cmp_item("Viewability", round(rate, 2), "<", PLC_VIEW_LOW_PCT,
                       f"{_n(p.viewable)} of {_n(p.viewable_base)} measured impressions were viewable = {rate:.1f}%, threshold {_pct(PLC_VIEW_LOW_PCT)}"),
              cmp_item("Measured impressions", p.viewable_base, ">=", PLC_VIEW_ENOUGH_IMPRESSIONS if enough else PLC_VIEW_FLOOR_IMPRESSIONS,
                       f"{_n(p.viewable_base)} {'>=' if enough else '<'} {_n(PLC_VIEW_ENOUGH_IMPRESSIONS)} needed for 'enough data'", met=enough),
              cmp_item("Measured share of its impressions", round(coverage, 1), ">=", PLC_VIEW_MIN_COVERAGE_PCT, f"{coverage:.0f}% of the placement's {_n(p.impressions)} impressions were measured"),
              cmp_item("Spend share", round(share, 2), ">=", PLC_VIEW_MIN_SHARE_PCT, f"{_money(p.spend, g.currency)} = {_pct(share)} of the campaign's {g.currency} spend")]
        out.append(_pfinding("placement_viewability", "warn", ENOUGH if enough else THIN, g, p,
                             what=f"Only {rate:.0f}% of the measured impressions on {p.name} were viewable.",
                             evidence=ev, magnitude=PLC_VIEW_LOW_PCT - rate,
                             action="Ads that are not seen still cost money: try a position above the fold or another size on that placement, or lower its bid. Nothing has been changed."))
    return _result(out, notes)


def rule_placement_concentration(g: PlacementGroup, window: Window) -> dict:
    tot = g.totals()
    spending = sorted((p for p in _named(g) if p.spend_micros > 0), key=lambda p: (-p.spend_micros, p.name))
    if len(spending) < PLC_CONC_MIN_PLACEMENTS or tot["spend_micros"] <= 0:
        return _skipped(f"needs at least {PLC_CONC_MIN_PLACEMENTS} named placements with spend in the campaign")
    top = spending[:PLC_CONC_TOP_N]
    share = sum(p.spend_micros for p in top) / tot["spend_micros"] * 100
    if share < PLC_CONC_WARN_PCT:
        return _result([])
    enough = tot["clicks"] >= PLC_CONC_ENOUGH_CLICKS
    listed = ", ".join(f"{p.name} {_pct(p.spend_micros / tot['spend_micros'] * 100)}" for p in top)
    ev = [cmp_item("Share of spend in the top placements", round(share, 2), ">=", PLC_CONC_WARN_PCT,
                   f"the {PLC_CONC_TOP_N} largest placements carry {_pct(share)} of the campaign's {g.currency} spend ({listed}); warning from {_pct(PLC_CONC_WARN_PCT)}, critical from {_pct(PLC_CONC_CRIT_PCT)}"),
          cmp_item("Placements with spend", len(spending), ">=", PLC_CONC_MIN_PLACEMENTS, f"{len(spending)} named placements share the spend"),
          cmp_item("Clicks", tot["clicks"], ">=", PLC_CONC_ENOUGH_CLICKS, f"{_n(tot['clicks'])} clicks in the campaign ({'>=' if enough else '<'} {PLC_CONC_ENOUGH_CLICKS} needed for 'enough data')", met=enough)]
    return _result([_pfinding("placement_concentration", "crit" if share >= PLC_CONC_CRIT_PCT else "warn", ENOUGH if enough else THIN, g, None,
                              what=f"{PLC_CONC_TOP_N} placements carry {_pct(share)} of the {g.currency} spend of {'the rows with no campaign' if g.campaign_id == 0 else g.campaign_name}.",
                              evidence=ev, magnitude=share,
                              action="Check whether those few placements deserve that much: if one of them is wasteful (see the other findings) most of the money is at risk; if they convert well, test a second tier of placements so the campaign does not depend on them.")])


def _fmt_estimate(x: float) -> str:
    return f"{x:.1f}" if x < 10 else f"{x:,.0f}"


def rule_placement_reallocation(g: PlacementGroup, window: Window) -> dict:
    """The ESTIMATE: what moving the spend of the clear waste to the clearly efficient placements would change at their CURRENT cost per conversion.
    Arithmetic on the past with one stated assumption, never a forecast, and shown only when BOTH groups have enough data."""
    worst = [p for p, _s, _l, enough in _exclusion_candidates(g)[0] if enough]
    # "efficient" is judged against the campaign WITHOUT the wasteful placements: their spend inflates the rest's cost per conversion and would make
    # ordinary placements look efficient (and the estimate too rosy)
    best = [t for t in _scale_candidates(replace(g, placements=tuple(p for p in g.placements if p not in worst))) if t[4]]
    if not worst or not best:
        return _skipped("needs both a wasteful placement and an efficient placement with enough data in the same campaign")
    spend_w = sum(p.spend_micros for p in worst)
    clicks_w = sum(p.clicks for p in worst)
    spend_b = sum(t[0].spend_micros for t in best)
    conv_b = sum(t[0].conversions for t in best)
    if clicks_w < PLC_EXCL_ENOUGH_CLICKS or conv_b < PLC_REALLOC_MIN_CONV or spend_b <= 0:
        return _skipped(f"the sending group needs {PLC_EXCL_ENOUGH_CLICKS}+ clicks and the receiving group {PLC_REALLOC_MIN_CONV}+ conversions")
    cpa_b = spend_b / MICROS / conv_b
    moved_micros = min(spend_w, int(spend_b * PLC_REALLOC_MAX_GROWTH))
    moved = moved_micros / MICROS
    gain = moved / cpa_b
    capped = moved_micros < spend_w
    wn, bn = ", ".join(p.name for p in worst), ", ".join(t[0].name for t in best)
    ev = [note_item("Sending group", round(spend_w / MICROS, 2), f"{len(worst)} placement{'s' if len(worst) != 1 else ''} with no conversions ({wn}): {_money(spend_w / MICROS, g.currency)} from {_n(clicks_w)} clicks, 0 conversions"),
          note_item("Receiving group", round(cpa_b, 2), f"{len(best)} placement{'s' if len(best) != 1 else ''} ({bn}): {_n(conv_b)} conversions for {_money(spend_b / MICROS, g.currency)} = {_money(cpa_b, g.currency)} per conversion"),
          note_item("Amount moved", round(moved, 2), f"{_money(moved, g.currency)}" + (f" - limited to {PLC_REALLOC_MAX_GROWTH * 100:.0f}% of the receiving group's own spend, not the whole {_money(spend_w / MICROS, g.currency)}" if capped else " - the whole spend of the sending group")),
          note_item("Assumption", None, "the receiving group keeps converting at its CURRENT cost per conversion however much it is given (constant cost per conversion, no saturation, no change in audience or price) and the sending group keeps producing 0. Real results usually get worse as budget grows.")]
    return _result([_pfinding("placement_reallocation", "info", ENOUGH, g, None,
                              what=f"Estimate: moving {_money(moved, g.currency)} from the wasteful placements to the efficient ones would add about {_fmt_estimate(gain)} conversions.",
                              evidence=ev, magnitude=gain, estimate=True,
                              action="Read this as an order of magnitude to prioritise the work, not as a promise: change the budget in small steps and measure. It changes nothing by itself.")])


PLACEMENT_RULE_FUNCS = (("placement_exclude", rule_placement_exclude), ("placement_scale", rule_placement_scale),
                        ("placement_viewability", rule_placement_viewability), ("placement_concentration", rule_placement_concentration),
                        ("placement_reallocation", rule_placement_reallocation))


def analyse_placements(groups: Iterable[PlacementGroup], window: Window) -> dict:
    """All placement rules over the campaigns in view. A group holds ONE campaign in ONE currency (the currency is a field of the group), so a
    finding never mixes currencies. Returns {findings (sorted), notes, rules [{id, title, status, why, fired}]}."""
    groups = list(groups)
    results: dict[str, list[dict]] = {rid: [] for rid, _t in PLACEMENT_RULES}
    for g in sorted(groups, key=lambda x: (x.currency, x.channel_key, x.campaign_id)):
        for rid, fn in PLACEMENT_RULE_FUNCS:
            results[rid].append(fn(g, window))
    findings, notes, statuses = [], [], []
    for rid, title in PLACEMENT_RULES:
        rs = results[rid]
        ran = [r for r in rs if r["ran"]]
        why = next((r["why"] for r in rs if not r["ran"] and r["why"]), "no placement data in view") if not ran else None
        for r in rs:
            findings.extend(r["findings"])
            notes.extend(r["notes"])
        statuses.append({"id": rid, "title": title, "status": "ran" if ran else "skipped", "why": why, "fired": sum(len(r["findings"]) for r in rs)})
    findings.sort(key=_sort_key)
    return {"findings": findings, "notes": notes, "rules": statuses}


def placement_thresholds() -> list[dict]:
    """Every named placement threshold with its value and meaning: the drawer and the Excel Definitions sheet are built from THIS list."""
    def t(rule, name, value, unit, meaning):
        return {"rule": rule, "name": name, "value": value, "unit": unit, "meaning": meaning}
    return [
        t("placement_exclude", "PLC_EXCL_MIN_SHARE_PCT", PLC_EXCL_MIN_SHARE_PCT, "% of campaign spend", "a placement with no conversions must hold at least this share of its campaign's spend in the currency"),
        t("placement_exclude", "PLC_EXCL_CRIT_SHARE_PCT", PLC_EXCL_CRIT_SHARE_PCT, "% of campaign spend", "a share at or above this (with enough data) is critical, a smaller one a warning"),
        t("placement_exclude", "PLC_EXCL_FLOOR_CLICKS", PLC_EXCL_FLOOR_CLICKS, "clicks", "below this many clicks nothing is said"),
        t("placement_exclude", "PLC_EXCL_ENOUGH_CLICKS", PLC_EXCL_ENOUGH_CLICKS, "clicks", "this many clicks make it 'enough data' (fewer = thin data, info only)"),
        t("placement_exclude", "PLC_EXCL_MAX_LUCK_PCT", PLC_EXCL_MAX_LUCK_PCT, "% chance", "the chance of zero conversions from that many clicks, at the campaign's own conversion rate without the placement, must be at most this"),
        t("placement_exclude", "PLC_EXCL_MIN_CAMPAIGN_CONV", PLC_EXCL_MIN_CAMPAIGN_CONV, "conversions", "the campaign itself needs this many conversions, else a zero may only mean missing tracking (a note is shown instead)"),
        t("placement_exclude", "PLC_CTR_FACTOR_PCT", PLC_CTR_FACTOR_PCT, "% of the campaign CTR", "low-CTR test: the placement's CTR is at most this share of the CTR of the rest of the campaign"),
        t("placement_exclude", "PLC_CTR_MIN_SHARE_PCT", PLC_CTR_MIN_SHARE_PCT, "% of campaign spend", "low-CTR test: the placement holds at least this share of the spend"),
        t("placement_exclude", "PLC_CTR_FLOOR_IMPRESSIONS", PLC_CTR_FLOOR_IMPRESSIONS, "impressions", "low-CTR test: below this many impressions nothing is said"),
        t("placement_exclude", "PLC_CTR_ENOUGH_IMPRESSIONS", PLC_CTR_ENOUGH_IMPRESSIONS, "impressions", "low-CTR test: this many make it 'enough data' (fewer = thin data)"),
        t("placement_scale", "PLC_SCALE_BETTER_PCT", PLC_SCALE_BETTER_PCT, "% cheaper", "cost per conversion at least this much lower than the rest of the campaign's"),
        t("placement_scale", "PLC_SCALE_FLOOR_CONV", PLC_SCALE_FLOOR_CONV, "conversions", "below this many conversions nothing is said"),
        t("placement_scale", "PLC_SCALE_MIN_CONV", PLC_SCALE_MIN_CONV, "conversions", "this many make it 'enough data' (fewer = thin data, info only)"),
        t("placement_scale", "PLC_SCALE_MAX", PLC_SCALE_MAX, "placements", "at most this many candidates per campaign and currency"),
        t("placement_viewability", "PLC_VIEW_LOW_PCT", PLC_VIEW_LOW_PCT, "% viewable", "viewable / measured impressions below this is low"),
        t("placement_viewability", "PLC_VIEW_MIN_SHARE_PCT", PLC_VIEW_MIN_SHARE_PCT, "% of campaign spend", "only placements holding at least this share of the spend are judged"),
        t("placement_viewability", "PLC_VIEW_FLOOR_IMPRESSIONS", PLC_VIEW_FLOOR_IMPRESSIONS, "measured impressions", "below this many measured impressions nothing is said"),
        t("placement_viewability", "PLC_VIEW_ENOUGH_IMPRESSIONS", PLC_VIEW_ENOUGH_IMPRESSIONS, "measured impressions", "this many make it 'enough data' (fewer = thin data, info only)"),
        t("placement_viewability", "PLC_VIEW_MIN_COVERAGE_PCT", PLC_VIEW_MIN_COVERAGE_PCT, "% of impressions", "the measured impressions must be at least this share of the placement's impressions, else the rate is not representative (a note is shown)"),
        t("placement_concentration", "PLC_CONC_TOP_N", PLC_CONC_TOP_N, "placements", "how many of the largest placements are added up"),
        t("placement_concentration", "PLC_CONC_WARN_PCT", PLC_CONC_WARN_PCT, "% of campaign spend", "the top placements together carry at least this share = warning"),
        t("placement_concentration", "PLC_CONC_CRIT_PCT", PLC_CONC_CRIT_PCT, "% of campaign spend", "at least this share = critical"),
        t("placement_concentration", "PLC_CONC_MIN_PLACEMENTS", PLC_CONC_MIN_PLACEMENTS, "placements", "needs this many named placements with spend (a few placements are always concentrated)"),
        t("placement_concentration", "PLC_CONC_ENOUGH_CLICKS", PLC_CONC_ENOUGH_CLICKS, "clicks", "clicks in the campaign for 'enough data'"),
        t("placement_reallocation", "PLC_REALLOC_MIN_CONV", PLC_REALLOC_MIN_CONV, "conversions", "the receiving group needs this many conversions (the sending group needs PLC_EXCL_ENOUGH_CLICKS clicks); otherwise no estimate is shown"),
        t("placement_reallocation", "PLC_REALLOC_MAX_GROWTH", PLC_REALLOC_MAX_GROWTH, "x own spend", "the estimate never moves more than this multiple of the receiving group's own spend (1 = it can at most double)"),
    ]


def placement_rule_texts() -> list[dict]:
    """One plain sentence (or three) per placement rule for the drawer and the Definitions sheet, built from the constants."""
    return [
        {"id": "placement_exclude", "title": RULE_TITLE["placement_exclude"],
         "text": f"Two tests per placement of a campaign. (1) It holds at least {PLC_EXCL_MIN_SHARE_PCT:g}% of the campaign's spend, had at least {PLC_EXCL_FLOOR_CLICKS} clicks and no conversion, and at the conversion rate of the rest of the campaign the chance of such a zero is at most {PLC_EXCL_MAX_LUCK_PCT:g}% (so a small placement that simply had no luck is not accused); critical from {PLC_EXCL_CRIT_SHARE_PCT:g}% of spend with {PLC_EXCL_ENOUGH_CLICKS}+ clicks. Only judged when the campaign itself has {PLC_EXCL_MIN_CAMPAIGN_CONV}+ conversions. (2) Its CTR is at most {PLC_CTR_FACTOR_PCT:g}% of the CTR of the rest of the campaign, with {PLC_CTR_FLOOR_IMPRESSIONS:,}+ impressions and {PLC_CTR_MIN_SHARE_PCT:g}%+ of the spend. Fewer clicks or impressions than 'enough' = thin data, info only. The finding suggests a review or an exclusion; nothing is excluded for you."},
        {"id": "placement_scale", "title": RULE_TITLE["placement_scale"],
         "text": f"Placements with at least {PLC_SCALE_FLOOR_CONV} conversions ({PLC_SCALE_MIN_CONV}+ for 'enough data') whose cost per conversion is at least {PLC_SCALE_BETTER_PCT:g}% lower than the rest of the same campaign's. At most {PLC_SCALE_MAX} per campaign and currency, always info."},
        {"id": "placement_viewability", "title": RULE_TITLE["placement_viewability"],
         "text": f"Viewable impressions divided by the impressions that were MEASURED (the blank rows are unknown, never counted as 0%) is below {PLC_VIEW_LOW_PCT:g}% on a placement holding {PLC_VIEW_MIN_SHARE_PCT:g}%+ of the campaign's spend, with {PLC_VIEW_FLOOR_IMPRESSIONS:,}+ measured impressions ({PLC_VIEW_ENOUGH_IMPRESSIONS:,}+ for 'enough data'). Skipped, with a note, when fewer than {PLC_VIEW_MIN_COVERAGE_PCT:g}% of the placement's impressions were measured."},
        {"id": "placement_concentration", "title": RULE_TITLE["placement_concentration"],
         "text": f"The {PLC_CONC_TOP_N} largest placements together carry {PLC_CONC_WARN_PCT:g}% or more of a campaign's spend ({PLC_CONC_CRIT_PCT:g}% critical), when at least {PLC_CONC_MIN_PLACEMENTS} named placements have spend. Judged per campaign and currency."},
        {"id": "placement_reallocation", "title": RULE_TITLE["placement_reallocation"],
         "text": f"An ESTIMATE, shown only when the same campaign has both a wasteful placement and an efficient one with enough data ({PLC_EXCL_ENOUGH_CLICKS}+ clicks and no conversion; {PLC_REALLOC_MIN_CONV}+ conversions in the efficient group). It moves the wasteful group's spend to the efficient group at the efficient group's CURRENT cost per conversion and reports the extra conversions. The assumption is written on the finding: a constant cost per conversion with no saturation, capped at {PLC_REALLOC_MAX_GROWTH:g}x the receiving group's own spend. It is arithmetic on the past, not a forecast."},
    ]
