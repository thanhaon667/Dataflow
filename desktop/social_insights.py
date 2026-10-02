"""
Social page, INSIGHTS: a fixed, explainable rule set that reads the cubes of desktop/social_data.py and says what to look at, plus an
OPTIONAL AI summary (DeepSeek, through erp.ai_client) that the reader asks for with a button.
  GET /api/social/insights?days=90&brand=0[&ai=1]

The rules (pure functions, no database, no I/O; every threshold is a named constant printed in the evidence of the finding):
  volume_change     the focus brand's mentions moved by at least VOLUME_CHANGE_PCT against the previous equal window
  sentiment_shift   net sentiment (% positive - % negative) moved by at least SENTIMENT_SHIFT_PTS points
  spike_days        days at least SPIKE_Z standard deviations above the window average, with the top negative topic of the day
  negative_topic    the topic with most negative posts, and how its negative share compares with the competitors'
  competitor_gap    the focus brand's net sentiment against the best competitor's
  channel_risk      the channel with the highest negative share (at least CHANNEL_MIN_POSTS posts) and its share of engagement
Below MIN_POSTS no rule speaks at all; between MIN_POSTS and ENOUGH_POSTS a finding is only info with confidence 'thin data'.

AI SUMMARY. Only AGGREGATE numbers leave this machine (counts, shares, topic and channel names): never a post text, an author or a
URL. It is requested explicitly (ai=1), cached for AI_CACHE_SECONDS per set of facts, and never blocks the rules: with no
DEEPSEEK_API_KEY, or when the call fails, the payload says so and the page keeps the rule findings. The text is shown as an
AI-written note, not as a fact: the findings above it carry the evidence.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time

logger = logging.getLogger("erp_desk.social")

MIN_POSTS = 50
ENOUGH_POSTS = 200
VOLUME_CHANGE_PCT = 25.0
SENTIMENT_SHIFT_PTS = 5.0
SPIKE_Z = 2.0
SPIKE_MAX = 3
CHANNEL_MIN_POSTS = 100
TOPIC_MIN_POSTS = 20
AI_CACHE_SECONDS = 3600.0
KNOWN_PARAMS = ("days", "brand", "ai", "fresh")


def unknown_params(params) -> list[str]:
    return [k for k in params if k not in KNOWN_PARAMS]


def _agg(cube, lo, hi, brand=None):
    r = {"n": 0, "eng": 0, "negative": 0, "neutral": 0, "positive": 0}
    names = ("negative", "neutral", "positive")
    for a in cube:
        if a[0] < lo or a[0] > hi or (brand is not None and a[1] != brand):
            continue
        r["n"] += a[4]; r["eng"] += a[5]; r[names[a[3]]] += a[4]
    return r


def _net(r):
    return 100.0 * (r["positive"] - r["negative"]) / r["n"] if r["n"] else 0.0


def facts(data: dict, days: int, brand: int) -> dict:
    """Every number the rules and the AI prompt use, for one window (days=0: the whole data set) and one focus brand."""
    nday = int(data["_nday"])
    hi = nday - 1
    lo = max(0, nday - days) if days else 0
    ln = hi - lo + 1
    pl, ph = lo - ln, lo - 1
    have_prev = pl >= 0
    brands, cube = data["brands"], data["cube"]
    cur, prv = _agg(cube, lo, hi, brand), (_agg(cube, pl, ph, brand) if have_prev else None)
    allc = _agg(cube, lo, hi)
    day = [0] * nday
    for a in cube:
        if a[1] == brand and lo <= a[0] <= hi:
            day[a[0]] += a[4]
    dv = day[lo:hi + 1]
    mean = sum(dv) / len(dv) if dv else 0.0
    sd = (sum((x - mean) ** 2 for x in dv) / len(dv)) ** 0.5 if dv else 0.0
    neg_by_day_topic: dict[tuple[int, int], int] = {}
    for b in data["by_topic"]:
        if b[1] == brand and b[3] == 0 and lo <= b[0] <= hi:
            neg_by_day_topic[(b[0], b[2])] = neg_by_day_topic.get((b[0], b[2]), 0) + b[4]
    spikes = []
    for d in range(lo, hi + 1):
        z = (day[d] - mean) / sd if sd else 0.0
        if z >= SPIKE_Z:
            top = max(((neg_by_day_topic.get((d, t), 0), t) for t in range(len(data["topics"]))), default=(0, 0))
            spikes.append({"day": d, "n": day[d], "z": round(z, 1), "topic": data["topics"][top[1]][1] if top[0] else None, "topic_negative": top[0]})
    spikes.sort(key=lambda s: -s["z"])
    tops = [[0, 0, 0] for _ in data["topics"]]            # per topic: negative, total (focus brand), competitors' [neg, total] kept apart
    comp = [[0, 0] for _ in data["topics"]]
    for b in data["by_topic"]:
        if not lo <= b[0] <= hi:
            continue
        if b[1] == brand:
            tops[b[2]][1] += b[4]
            if b[3] == 0:
                tops[b[2]][0] += b[4]
        else:
            comp[b[2]][1] += b[4]
            if b[3] == 0:
                comp[b[2]][0] += b[4]
    topics = [{"label": data["topics"][i][1], "negative": tops[i][0], "total": tops[i][1],
               "neg_pct": round(100.0 * tops[i][0] / tops[i][1], 1) if tops[i][1] else None,
               "comp_neg_pct": round(100.0 * comp[i][0] / comp[i][1], 1) if comp[i][1] >= TOPIC_MIN_POSTS else None} for i in range(len(tops))]
    pc = [[0, 0, 0] for _ in data["plats"]]               # negative, total, engagement
    for a in cube:
        if a[1] == brand and lo <= a[0] <= hi:
            pc[a[2]][1] += a[4]; pc[a[2]][2] += a[5]
            if a[3] == 0:
                pc[a[2]][0] += a[4]
    eng_total = sum(p[2] for p in pc) or 1
    plats = [{"platform": data["plats"][i], "negative": pc[i][0], "total": pc[i][1], "neg_pct": round(100.0 * pc[i][0] / pc[i][1], 1) if pc[i][1] else None,
              "engagement_share": round(100.0 * pc[i][2] / eng_total, 1)} for i in range(len(pc))]
    rivals = []
    for i, name in enumerate(brands):
        if i != brand:
            r = _agg(cube, lo, hi, i)
            rivals.append({"brand": name, "n": r["n"], "net": round(_net(r), 1)})
    return {"brand": brands[brand], "days": ln, "from": lo, "to": hi, "has_prev": have_prev, "cur": cur, "prev": prv,
            "net": round(_net(cur), 1), "net_prev": round(_net(prv), 1) if prv and prv["n"] else None,
            "sov": round(100.0 * cur["n"] / allc["n"], 1) if allc["n"] else 0.0, "mean_day": round(mean, 1), "spikes": spikes[:SPIKE_MAX],
            "topics": topics, "channels": plats, "rivals": rivals}


def _ev(label, value, text, met=True):
    return {"label": label, "value": value, "text": text, "met": met}


def rules(f: dict, day_label) -> list[dict]:
    """The findings: [{id, severity (info|warn|crit), confidence, what, evidence, action}]. day_label(i) -> 'd Mon' for a day index."""
    out: list[dict] = []
    cur, prv, n = f["cur"], f["prev"], f["cur"]["n"]
    if n < MIN_POSTS:
        return [{"id": "too_little_data", "severity": "info", "confidence": "thin data", "what": f"Only {n} posts for {f['brand']} in this range, too few for any finding.",
                 "evidence": [_ev("posts", n, f"{n} posts < {MIN_POSTS} minimum", False)], "action": "Widen the date range or add keywords."}]
    conf = "enough data" if n >= ENOUGH_POSTS else "thin data"

    def sev(s):
        return s if conf == "enough data" else "info"

    if prv and prv["n"] >= MIN_POSTS:
        pct = 100.0 * (n - prv["n"]) / prv["n"]
        if abs(pct) >= VOLUME_CHANGE_PCT:
            out.append({"id": "volume_change", "severity": sev("warn" if pct > 0 else "info"), "confidence": conf,
                        "what": f"Mentions of {f['brand']} {'rose' if pct > 0 else 'fell'} {abs(pct):.0f}% against the previous {f['days']} days.",
                        "evidence": [_ev("mentions", n, f"{n:,} now vs {prv['n']:,} before"), _ev("change", f"{pct:+.0f}%", f"threshold ±{VOLUME_CHANGE_PCT:.0f}%")],
                        "action": "Check the spike days below: a single event usually explains most of the move." if f["spikes"] else "Look at which topic grew in the topic table."})
    if f["net_prev"] is not None:
        d = f["net"] - f["net_prev"]
        if abs(d) >= SENTIMENT_SHIFT_PTS:
            out.append({"id": "sentiment_shift", "severity": sev("warn" if d < 0 else "info"), "confidence": conf,
                        "what": f"Net sentiment {'dropped' if d < 0 else 'improved'} {abs(d):.1f} points to {f['net']:+.1f}.",
                        "evidence": [_ev("net sentiment", f"{f['net']:+.1f}", f"{f['net_prev']:+.1f} in the previous window"), _ev("shift", f"{d:+.1f} pts", f"threshold ±{SENTIMENT_SHIFT_PTS:.0f} pts")],
                        "action": "Read the top negative topic below and the posts behind it." if d < 0 else "Find what changed (a campaign? a fix?) and keep doing it."})
    for s in f["spikes"]:
        out.append({"id": "spike_days", "severity": sev("warn"), "confidence": conf,
                    "what": f"{day_label(s['day'])}: {s['n']:,} mentions, {s['n'] / f['mean_day']:.1f}× the daily average" + (f", driven by {s['topic']} complaints." if s["topic"] else "."),
                    "evidence": [_ev("mentions", s["n"], f"{s['n']:,} vs average {f['mean_day']:.0f} a day"), _ev("z-score", s["z"], f"threshold {SPIKE_Z:g}"),
                                 _ev("top negative topic", s["topic_negative"], s["topic"] or "none")],
                    "action": "Find the event behind this day (an outage, a viral post, a sale) and log it in sl.event so it is marked on the charts."})
    worst = [t for t in f["topics"] if t["total"] >= TOPIC_MIN_POSTS and t["negative"]]
    if worst:
        t = max(worst, key=lambda x: x["negative"])
        gap = (t["neg_pct"] - t["comp_neg_pct"]) if t["comp_neg_pct"] is not None else None
        text = f"{t['label']} has the most negative posts ({t['negative']:,}, {t['neg_pct']:.0f}% of its posts)"
        if gap is not None and abs(gap) >= 5:
            text += f", {abs(gap):.0f} points {'worse' if gap > 0 else 'better'} than competitors"
        out.append({"id": "negative_topic", "severity": sev("warn" if (gap or 0) >= 5 else "info"), "confidence": conf, "what": text + ".",
                    "evidence": [_ev("negative posts", t["negative"], f"{t['negative']:,} of {t['total']:,} posts on the topic"),
                                 _ev("competitors", t["comp_neg_pct"], "negative share on the same topic" if t["comp_neg_pct"] is not None else "not enough competitor posts to compare")],
                    "action": "Hand this topic to the owning team with three example posts."})
    if f["rivals"]:
        best = max(f["rivals"], key=lambda r: r["net"])
        if best["n"] >= MIN_POSTS and best["net"] - f["net"] >= SENTIMENT_SHIFT_PTS:
            out.append({"id": "competitor_gap", "severity": "info", "confidence": conf,
                        "what": f"{best['brand']} has better net sentiment ({best['net']:+.1f} vs {f['net']:+.1f}).",
                        "evidence": [_ev("gap", f"{best['net'] - f['net']:.1f} pts", f"threshold {SENTIMENT_SHIFT_PTS:.0f} pts")],
                        "action": "Compare the topics where they score better; that is where customers see the difference."})
    chans = [c for c in f["channels"] if c["total"] >= CHANNEL_MIN_POSTS and c["neg_pct"] is not None]
    if chans:
        c = max(chans, key=lambda x: x["neg_pct"])
        avg = 100.0 * sum(x["negative"] for x in chans) / (sum(x["total"] for x in chans) or 1)
        if c["neg_pct"] - avg >= 5:
            out.append({"id": "channel_risk", "severity": "info", "confidence": conf,
                        "what": f"{c['platform']} is the most negative channel ({c['neg_pct']:.0f}% negative vs {avg:.0f}% overall) and carries {c['engagement_share']:.0f}% of engagement.",
                        "evidence": [_ev("negative share", f"{c['neg_pct']:.0f}%", f"{c['negative']:,} of {c['total']:,} posts"), _ev("engagement share", f"{c['engagement_share']:.0f}%", "of all engagement")],
                        "action": "Answer the most-engaged negative posts there first."})
    if not out:
        out.append({"id": "all_quiet", "severity": "info", "confidence": conf, "what": f"No rule fired for {f['brand']} in this range: volume and sentiment are within their normal band.",
                    "evidence": [_ev("mentions", n, f"within ±{VOLUME_CHANGE_PCT:.0f}% of the previous window or no previous window")], "action": "Nothing needed."})
    return out


def ai_prompt(f: dict, findings: list[dict], day_label) -> str:
    """The prompt: aggregate numbers and the rule findings only (no post text, authors or URLs)."""
    lines = [f"Brand: {f['brand']} (a delivery company). Window: {f['days']} days. Mentions {f['cur']['n']}, net sentiment {f['net']:+.1f}, share of voice {f['sov']}%.",
             "Competitors (mentions, net sentiment): " + "; ".join(f"{r['brand']} {r['n']}, {r['net']:+.1f}" for r in f["rivals"]),
             "Topics (negative posts / total): " + "; ".join(f"{t['label']} {t['negative']}/{t['total']}" for t in f["topics"] if t["total"]),
             "Channels (negative %): " + "; ".join(f"{c['platform']} {c['neg_pct']}%" for c in f["channels"] if c["neg_pct"] is not None),
             "Spike days: " + ("; ".join(f"{day_label(s['day'])} {s['n']} mentions ({s['topic'] or 'unclear'})" for s in f["spikes"]) or "none"),
             "Rule findings: " + " | ".join(x["what"] for x in findings)]
    return ("You are a social listening analyst for a delivery brand. Using ONLY the numbers below, write a short note in English: "
            "(1) what happened, (2) the likely causes, (3) three concrete actions, most urgent first. At most 150 words, plain text, "
            "no markdown, and say so where a number is too small to conclude anything.\n\n" + "\n".join(lines))


class InsightStore:
    """Rules on every request (cheap); the AI note only when asked for, cached per set of facts. Never raises."""

    def __init__(self, chat=None, configured=None) -> None:
        self._chat, self._configured = chat, configured
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, dict]] = {}

    def _ai(self):
        if self._chat is None:
            from erp import ai_client
            self._chat, self._configured = ai_client.chat, ai_client.is_configured
        return self._chat, self._configured

    def build(self, data: dict, days: int, brand: int, want_ai: bool, day_label, fresh: bool = False) -> dict:
        f = facts(data, days, brand)
        findings = rules(f, day_label)
        out = {"ok": True, "brand": f["brand"], "days": f["days"], "findings": findings,
               "thresholds": {"min_posts": MIN_POSTS, "enough_posts": ENOUGH_POSTS, "volume_change_pct": VOLUME_CHANGE_PCT, "sentiment_shift_pts": SENTIMENT_SHIFT_PTS, "spike_z": SPIKE_Z},
               "ai": {"requested": want_ai}}
        chat, configured = self._ai()
        out["ai"]["configured"] = bool(configured())
        if not want_ai:
            return out
        if not out["ai"]["configured"]:
            out["ai"]["error"] = "DEEPSEEK_API_KEY is not set in .env, so no AI note can be written. The rule findings above need no key."
            return out
        prompt = ai_prompt(f, findings, day_label)
        key = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(key)
        if hit and not fresh and now - hit[0] < AI_CACHE_SECONDS:
            return {**out, "ai": {**out["ai"], **hit[1], "cached": True}}
        text = chat(prompt)
        if not isinstance(text, str) or text.startswith("[") or not text.strip():
            logger.warning("Social AI note failed: %s", str(text)[:120])
            out["ai"]["error"] = "The AI service did not answer. The rule findings above are unaffected."
            return out
        res = {"text": text.strip(), "chars_sent": len(prompt), "note": "Written by an AI from the aggregate numbers above (no post text was sent); check it against the evidence."}
        with self._lock:
            self._cache[key] = (now, res)
            while len(self._cache) > 32:
                self._cache.pop(next(iter(self._cache)))
        out["ai"].update(res)
        return out
