"""Data Flow feed, trigger state: is each trigger really armed, and the state text of every node trigger and edge (split out of desktop/flow_data.py, unchanged).
"""
from __future__ import annotations


# ============================================================================ automation
KIND_PRIORITY = ("event", "scheduled", "background")   # "real automation" kinds


class _Ctx:
    """Everything one refresh knows, handed to the health functions."""

    def __init__(self, v: dict, sched: dict, stream_state: str, report_alive: bool, digest_job: dict | None) -> None:
        self.v = v
        self.sched = sched
        self.stream_state = stream_state
        self.report_alive = report_alive
        self.digest_job = digest_job or {}

    def scheduled(self, key: str) -> bool | None:
        if not self.sched.get("available"):
            return None
        return key in self.sched.get("matches", {})


def _armed_one(spec: str, ctx: _Ctx) -> bool | None:
    if spec == "always":
        return True
    if spec == "external":
        return None
    if spec.startswith("probe:"):
        return bool(ctx.v["probe"].get(spec[6:]))
    if spec == "flag:digest_auto":
        return bool(ctx.v["digest"]["auto_generate"])
    if spec == "flag:report_refresher":
        return ctx.report_alive
    if spec.startswith("schedule:"):
        return ctx.scheduled(spec[9:])
    return None


def _armed(spec, ctx: _Ctx) -> bool | None:
    specs = spec if isinstance(spec, (list, tuple)) else [spec]
    vals = [_armed_one(s, ctx) for s in specs]
    if any(v is True for v in vals):
        return True
    if any(v is None for v in vals):
        return None
    return False


def _trigger_state_text(t: dict, armed: bool | None, ctx: _Ctx) -> str:
    spec = t.get("armed_by")
    specs = spec if isinstance(spec, (list, tuple)) else [spec]
    if armed is True:
        if any(str(s).startswith("schedule:") for s in specs):
            names = [n for s in specs if str(s).startswith("schedule:") for n in ctx.sched.get("matches", {}).get(str(s)[9:], [])]
            return "Scheduled on this machine" + (f": {names[0]}" if names else "")
        if "flag:digest_auto" in specs:
            return "Running: ERP Desk generates it once a day while open"
        if "flag:report_refresher" in specs:
            return "Running: refresher thread is alive"
        if any(str(s).startswith("probe:") for s in specs):
            return "Listener is answering"
        return "Available"
    if armed is False:
        if any(str(s).startswith("schedule:") for s in specs):
            return "Recommended, but NOT scheduled on this machine" if t.get("recommended") else "Not scheduled on this machine"
        if any(str(s).startswith("probe:") for s in specs):
            return "Not running right now - start it to arm this trigger"
        if "flag:digest_auto" in specs:
            return "Switched off (DESKTOP_AUTO_DIGEST=0)"
        return "Not armed"
    if any(str(s).startswith("schedule:") for s in specs):
        return "Could not check Task Scheduler" if ctx.sched.get("error") else "Checking Task Scheduler..."
    return "Happens outside this machine - cannot be verified here"


def _edge_state(e: dict, ctx: _Ctx) -> tuple[str, str]:
    """(mode, why) of one edge, from ITS OWN armed_by - never from the driver node's aggregate state.

    live = the trigger behind this very hop is armed right now; dormant = automated by design but the
    trigger is not armed; unknown = cannot be verified from here (e.g. Task Scheduler not readable).
    """
    if e["trigger"] == "passive":
        return "passive", "Nothing runs: it is read whenever something needs it."
    if e["trigger"] not in KIND_PRIORITY:
        return "manual", "Moves only when a person starts it."
    spec = e.get("armed_by")
    if spec is None:   # a missing armed_by must never look 'live'
        return "unknown", "No armed_by declared for this hop in flow_definition.EDGES."
    armed = _armed(spec, ctx)
    why = _trigger_state_text({"armed_by": spec, "recommended": bool(e.get("recommended"))}, armed, ctx)
    return {True: "live", False: "dormant"}.get(armed, "unknown"), why


def _resolve_triggers(node: dict, ctx: _Ctx) -> tuple[list[dict], str, str]:
    """(triggers with live state, automation, effective kind)."""
    trig = []
    for t in node["triggers"]:
        armed = _armed(t.get("armed_by", "always"), ctx)
        trig.append({"kind": t["kind"], "label": t["label"], "detail": t.get("detail", ""),
                     "recommended": bool(t.get("recommended")), "armed": armed,
                     "state": _trigger_state_text(t, armed, ctx)})
    auto_kinds = [t for t in trig if t["kind"] in KIND_PRIORITY]
    active = [t for t in auto_kinds if t["armed"] is True]
    if active:
        if node.get("roles") and len(active) < len(auto_kinds):
            return trig, "partial", active[0]["kind"]   # separate jobs: only some of them are running
        return trig, "active", active[0]["kind"]
    if auto_kinds:
        intended = auto_kinds[0]["kind"]
        return trig, "available", intended
    kinds = {t["kind"] for t in trig}
    if "manual" in kinds:
        return trig, "manual", "manual"
    if "passive" in kinds:
        return trig, "passive", "passive"
    return trig, "external", "external"
