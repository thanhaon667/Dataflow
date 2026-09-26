"""Data Flow feed, map sync check: compares the hand-declared map (desktop/flow_definition.py) with the project's scripts and checks its own consistency (split out of desktop/flow_data.py, unchanged).
"""
from __future__ import annotations

import ast
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from desktop import winutil
from desktop.flow_definition import EDGES, IGNORE, LANES, NODES, PROBES, SCAN, SCHEDULE_MARKERS, TRACE
from desktop.flow_util import _iso, _redact
from desktop.flow_triggers import KIND_PRIORITY


MAP_SCAN_LIMIT = 20000      # give up ("check unavailable") when a folder tree is absurdly large

# ============================================================================ map sync check
# The map (flow_definition.py) is declared by hand. These checks tell the user when it has drifted from
# the project, so a forgotten script / a deleted file / a typo in an edge can never go unnoticed.
# Everything here is read-only, takes no input from the browser, and degrades to "check unavailable".
FIX_HINT = ("Add a node (with its edges) to NODES / EDGES in desktop/flow_definition.py, or add the file to IGNORE "
            "there with a one-line reason, then restart ERP Desk.")
NODE_TRIGGER_KINDS = ("event", "scheduled", "background", "manual", "passive", "external")
EDGE_TRIGGER_KINDS = ("event", "scheduled", "background", "manual", "passive")
ARMED_FLAGS = ("flag:digest_auto", "flag:report_refresher")
COUNTER_KEYS = ("leads", "assign", "ai", "clickup", "updates", "staff", "tickets", "digests", "refreshes", "marketing", "rollup", "autorun")


def _file_mtime(path: str | None) -> float | None:
    try:
        return os.stat(path).st_mtime if path else None
    except OSError:
        return None


def _key(rel: str) -> str:
    """Comparison key of a project-relative path (Windows paths are case-insensitive)."""
    return rel.casefold() if winutil.IS_WINDOWS else rel


def _clean_rel(p) -> str | None:
    """A project-relative posix path, or None when it is empty, absolute or climbs out of the project."""
    if not isinstance(p, str):
        return None
    s = p.strip().replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    if not s or s.startswith("/") or re.match(r"^[A-Za-z]:", s) or ".." in s.split("/"):
        return None
    return s


def declared_files(node: dict) -> list[str]:
    """The source files a node stands for: its explicit "files" list, else the paths in "script" / "file"."""
    raw = node.get("files")
    if raw is None:
        raw = []
        for k in ("script", "file"):
            v = node.get(k)
            if isinstance(v, str):
                raw.extend(v.split(","))
    seen: set[str] = set()
    out = []
    for x in raw:
        if isinstance(x, str) and x.strip() and x.strip() not in seen:
            seen.add(x.strip())
            out.append(x.strip())
    return out


def scan_scripts(root: Path) -> list[str]:
    """Every project .py script (relative posix paths), minus the standard exclusions in flow_definition.SCAN.

    Walks the whole project tree but prunes hidden folders, virtualenvs and SCAN["skip_dirs"], so it is cheap
    (a few ms here). Does not follow symlinks.
    """
    skip_dirs = {d.casefold() for d in SCAN.get("skip_dirs", [])}
    skip_files = {f.casefold() for f in SCAN.get("skip_files", [])}
    prefixes = tuple(p.casefold() for p in SCAN.get("skip_prefixes", []))
    suffixes = tuple(x.casefold() for x in SCAN.get("skip_suffixes", []))
    out: list[str] = []
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d.casefold() not in skip_dirs
                             and not os.path.exists(os.path.join(dirpath, d, "pyvenv.cfg")))
        for fn in sorted(filenames):
            seen += 1
            if seen > MAP_SCAN_LIMIT:
                raise RuntimeError("the project tree is too large to scan")
            low = fn.casefold()
            if not low.endswith(".py") or low in skip_files or low.startswith(prefixes) or low.endswith(suffixes):
                continue
            out.append(Path(dirpath, fn).relative_to(root).as_posix())
    out.sort(key=str.casefold)
    return out


def _first_doc_line(path: Path) -> str | None:
    """First meaningful line of a script's module docstring; None when there is none or the file cannot be parsed."""
    try:
        if path.stat().st_size > 512_000:
            return None
        doc = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8-sig", errors="replace")))
    except Exception:  # noqa: BLE001 - a malformed / unreadable script must never break the feed
        return None
    for line in (doc or "").splitlines():
        if re.search(r"[A-Za-z0-9]", line):
            return _redact(line.strip(), 160) or None
    return None


def map_integrity(nodes: list | None = None, edges: list | None = None, lanes: list | None = None,
                  trace: list | None = None) -> list[dict]:
    """Inconsistencies inside the definition itself (each would otherwise show up as a silent 'unknown' or a blank)."""
    nodes = NODES if nodes is None else nodes
    edges = EDGES if edges is None else edges
    lanes = LANES if lanes is None else lanes
    trace = TRACE if trace is None else trace
    issues: list[dict] = []

    def add(kind: str, where, message: str) -> None:
        issues.append({"kind": kind, "where": str(where)[:80], "message": message})

    def bad_spec(spec) -> str | None:
        for sp in (spec if isinstance(spec, (list, tuple)) else [spec]):
            ok = (sp in ("always", "external") or sp in ARMED_FLAGS
                  or (isinstance(sp, str) and sp.startswith("probe:") and sp[6:] in PROBES)
                  or (isinstance(sp, str) and sp.startswith("schedule:") and sp[9:] in SCHEDULE_MARKERS))
            if not ok:
                return str(sp)
        return None

    ids: set = set()
    lane_ids = {ln.get("id") for ln in lanes}
    for n in nodes:
        nid = n.get("id")
        if nid in ids:
            add("duplicate_id", nid, f"Node id '{nid}' is used more than once.")
        ids.add(nid)
        if n.get("lane") not in lane_ids:
            add("unknown_lane", nid, f"Node '{nid}' is on lane '{n.get('lane')}', which is not in LANES.")
        for t in n.get("triggers") or []:
            if t.get("kind") not in NODE_TRIGGER_KINDS:
                add("bad_trigger", nid, f"Node '{nid}' has a trigger of unknown kind '{t.get('kind')}'.")
            bad = bad_spec(t.get("armed_by", "always"))
            if bad:
                add("bad_armed_by", nid, f"Node '{nid}' has a trigger with the unknown armed_by '{bad}'.")
    edge_ids: set = set()
    for e in edges:
        eid = e.get("id")
        if eid in edge_ids:
            add("duplicate_id", eid, f"Edge id '{eid}' is used more than once.")
        edge_ids.add(eid)
        for k in ("from", "to", "driver"):
            if e.get(k) not in ids:
                add("unknown_node", eid, f"Edge '{eid}' refers to the unknown node '{e.get(k)}' ({k}).")
        if e.get("trigger") not in EDGE_TRIGGER_KINDS:
            add("bad_trigger", eid, f"Edge '{eid}' has the unknown trigger '{e.get('trigger')}'.")
        elif e["trigger"] in KIND_PRIORITY and e.get("armed_by") is None:
            add("missing_armed_by", eid, f"Automated edge '{eid}' has no armed_by, so it can only ever show as 'unknown'.")
        if e.get("armed_by") is not None:
            bad = bad_spec(e["armed_by"])
            if bad:
                add("bad_armed_by", eid, f"Edge '{eid}' has the unknown armed_by '{bad}'.")
        if e.get("counter") and e["counter"] not in COUNTER_KEYS:
            add("bad_counter", eid, f"Edge '{eid}' names the unknown counter '{e['counter']}'.")
    for st in trace:
        if st.get("edge") not in edge_ids:
            add("unknown_edge", st.get("edge"), f"TRACE refers to the unknown edge '{st.get('edge')}'.")
    return issues


def check_map(root: Path, nodes: list | None = None, ignore: dict | None = None) -> dict:
    """Compare the scripts on disk with the map. Raises on an unexpected failure (the caller degrades)."""
    nodes = NODES if nodes is None else nodes
    ignore = IGNORE if ignore is None else ignore
    scripts = scan_scripts(root)

    covered: dict[str, str] = {}          # comparison key -> node id
    stale_nodes: list[dict] = []
    for n in nodes:
        for raw in declared_files(n):
            rel = _clean_rel(raw)
            if rel is None:
                reason = "not a project-relative path"
            elif not (root / rel).is_file():
                reason = "file not found (moved, renamed or deleted?)"
            else:
                covered.setdefault(_key(rel), n.get("id"))
                continue
            stale_nodes.append({"node": n.get("id"), "label": n.get("label"), "path": str(raw)[:200], "reason": reason})

    ignored: dict[str, tuple[str, str]] = {}   # key -> (relative path, reason)
    stale_ignores: list[dict] = []
    for raw, why in ignore.items():
        rel = _clean_rel(raw)
        why = why.strip() if isinstance(why, str) else ""
        if rel is None:
            stale_ignores.append({"path": str(raw)[:200], "reason": "not a project-relative path"})
        elif not (root / rel).is_file():
            stale_ignores.append({"path": rel, "reason": "the file no longer exists - remove this IGNORE entry"})
        elif _key(rel) in covered:
            stale_ignores.append({"path": rel, "reason": f"node '{covered[_key(rel)]}' covers it now - remove this IGNORE entry"})
        elif not why:
            stale_ignores.append({"path": rel, "reason": "no reason given - say in a few words why it is not part of the flow"})
        else:
            ignored[_key(rel)] = (rel, why)

    unmapped: list[dict] = []
    mapped = 0
    ignored_seen: list[dict] = []
    for rel in scripts:
        k = _key(rel)
        if k in covered:
            mapped += 1
        elif k in ignored:
            ignored_seen.append({"path": ignored[k][0], "reason": ignored[k][1][:240]})
        else:
            unmapped.append({"path": rel, "doc": _first_doc_line(root / rel)})

    try:
        integrity = map_integrity()
    except Exception as exc:  # noqa: BLE001 - a malformed definition entry: report it, keep the rest of the check
        integrity = [{"kind": "check_failed", "where": "definition", "message": f"Could not validate the definition: {type(exc).__name__}: {_redact(str(exc))}"}]
    problems = len(unmapped) + len(stale_nodes) + len(stale_ignores) + len(integrity)
    return {"available": True, "error": None, "in_sync": problems == 0, "scanned": len(scripts), "mapped": mapped,
            "ignored": ignored_seen, "unmapped": unmapped, "stale_nodes": stale_nodes, "stale_ignores": stale_ignores,
            "integrity": integrity, "hint": FIX_HINT, "checked_at": _iso(datetime.now(timezone.utc))}


def map_check_unavailable(exc: BaseException) -> dict:
    return {"available": False, "error": f"{type(exc).__name__}: {_redact(str(exc))}", "in_sync": None, "scanned": 0, "mapped": 0,
            "ignored": [], "unmapped": [], "stale_nodes": [], "stale_ignores": [], "integrity": [], "hint": FIX_HINT,
            "checked_at": _iso(datetime.now(timezone.utc))}
