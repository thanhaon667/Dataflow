"""
Scenario table that proves the merge gate's own protections still work (run by tests/smoke.py check 9).

A gate is only worth its runtime while its protections are intact, and a protection that quietly stops working looks
exactly like a green gate. Every hardening this file covers was an Auditor finding on the 2026-09-21 cycle:

  bat          run_smoke.bat runs `python -B` (no tests/__pycache__, L-077), never waits for a key unless SMOKE_PAUSE
               is set (L-078, L-013), passes its arguments through, and the documented commands all carry -B
  merge-base   the diff is taken against `git merge-base <base> HEAD`, never the tip of the base branch, and every
               commit and commit MESSAGE of the branch is scanned, so a secret added and removed again is caught (L-074)
  allowlist    import safety is an allowlist: an unknown top-level call makes a module compile-only (L-075)
  placeholder  a STRONG secret name is only exempt for a clearly fake value, never for a real-looking one (L-072)
  git          every git helper and all gate set-up code turns a missing / hanging git, a non-repository, a detached
               HEAD, an unborn branch, a missing base branch and unrelated histories into a clean FAIL row (L-076)
  branch       "never edit on main" is a FAIL row, with the owner's own dirty files excluded (Law.md rule 1 / L-053)
  typography   only history / quotation LINES may name a retired font, and shell.css / shell.html / erp/typography.py
               are cross-checked so a font drift cannot fail silently into the fallback (L-124)
  numerals     every number that animates or stands in a column keeps tabular figures (L-112), no font weight is
               downloaded that no CSS rule paints (L-113), and the Streamlit --theme.font flag is proved by parsing
               it with Streamlit's own parser rather than by comparing two copies of a string (L-114 / L-124)
  cleanup      the self-test itself leaves nothing behind (L-042)

Everything is done in memory or in a throw-away git repository under the system temp folder, which is deleted again:
no probe file with a secret-looking value ever exists inside this project (L-070). Sample secret values are assembled
at run time, and so are the retired font names, so this file does not flag itself in check 5 / check 6 (L-073).

Run: it is run by the gate; stand-alone for a quick look use
    venv\\Scripts\\python.exe -B -m tests.smoke --only 9
"""
from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATE_COMMAND = r"venv\Scripts\python.exe -B -m tests.smoke"

# Assembled at run time so check 6 does not find a retired font name in this tracked file (L-073).
_OLD_FONT = "Fraun" + "ces"
_OLD_FONT_2 = "Public" + " Sans"

Row = tuple[str, bool, str]


def _rmtree(path: Path) -> None:
    """Delete a scratch git repository on Windows, where git marks its object files read-only (L-042)."""
    def force(func, target, _exc):
        try:
            os.chmod(target, 0o700)
            func(target)
        except OSError:
            pass

    try:                                     # onexc since 3.12, onerror before it
        shutil.rmtree(path, onexc=force)
    except TypeError:
        shutil.rmtree(path, onerror=lambda f, t, e: force(f, t, e))
    except OSError:
        pass


def _add(rows: list[Row], name: str, ok: bool, why: str = "") -> None:
    rows.append((name, bool(ok), "" if ok else (why or "did not hold")))


def _eq(rows: list[Row], name: str, got, want, note: str = "") -> None:
    _add(rows, name, got == want, f"got {got!r}, expected {want!r}{('; ' + note) if note else ''}")


def _section(rows: list[Row], label: str, fn, *args) -> None:
    """A crashing scenario group is a FAIL row, never an exception out of the gate."""
    try:
        fn(rows, *args)
    except Exception as exc:  # noqa: BLE001
        rows.append((f"{label}: the scenario group crashed", False, f"{type(exc).__name__}: {exc}"))


# --------------------------------------------------------------------------- 1. run_smoke.bat and the -B flag
def _bat_rows(rows: list[Row], smoke) -> None:
    bat = ROOT / "run_smoke.bat"
    text = bat.read_text(encoding="utf-8", errors="replace") if bat.is_file() else ""
    _add(rows, "bat: run_smoke.bat exists", bool(text), "run_smoke.bat is missing or empty")
    if not text:
        return
    _add(rows, "bat: launches python -B -m tests.smoke", bool(re.search(r"python\s+-B\s+-m\s+tests\.smoke", text)),
         "the launcher must use -B, or runpy writes tests/__pycache__ before the module can forbid it (L-077)")
    code_lines = [ln for ln in text.splitlines() if not ln.strip().upper().startswith("REM")]
    pause_lines = [ln for ln in code_lines if re.search(r"(?<![A-Za-z])pause(?![A-Za-z])", ln)]
    _add(rows, "bat: pause is opt-in (SMOKE_PAUSE) only",
         all("SMOKE_PAUSE" in ln for ln in pause_lines),
         f"a pause is not guarded by SMOKE_PAUSE: {[ln.strip() for ln in pause_lines if 'SMOKE_PAUSE' not in ln][:1]}")
    _add(rows, "bat: no %cmdcmdline% double-click guess",
         not any("cmdcmdline" in ln.lower() for ln in code_lines),
         "L-078: %cmdcmdline% cannot tell Explorer from `cmd /c`, PowerShell or Task Scheduler - the pause must be opt-in")
    _add(rows, "bat: the comment states what really happens", "SMOKE_PAUSE" in text and "double-click" in text.lower(),
         "the REM comment must name SMOKE_PAUSE and say that nothing waits for a key otherwise")

    pyc = ROOT / "tests" / "__pycache__"
    _add(rows, "bat: no tests/__pycache__ before the run", not pyc.exists(),
         f"{pyc} exists: somebody ran the gate without -B. Delete it and always use `{GATE_COMMAND}` (L-077)")
    env = {k: v for k, v in os.environ.items() if k.upper() != "SMOKE_PAUSE"}
    try:
        cp = subprocess.run(["cmd", "/c", str(bat), "--help"], cwd=ROOT, env=env, capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=25, check=False)
    except subprocess.TimeoutExpired:
        _add(rows, "bat: returns non-interactively with the gate's exit code", False,
             "run_smoke.bat did not return within 25 s although SMOKE_PAUSE is unset")
        return
    except OSError as exc:
        _add(rows, "bat: returns non-interactively with the gate's exit code", False, f"{type(exc).__name__}: {exc}")
        return
    out = (cp.stdout or "") + (cp.stderr or "")
    # NOTE: this run cannot prove "it does not pause" - cmd's `pause` returns at once when stdin is a pipe. That the
    # pause is opt-in is proven by reading the file above; this proves the launcher runs, forwards and exits cleanly.
    _add(rows, "bat: returns non-interactively with the gate's exit code", cp.returncode == 0,
         f"exit code {cp.returncode}: {out[:200]}")
    _add(rows, "bat: passes its arguments through", "--only" in out and "--base" in out,
         "`run_smoke.bat --help` did not print the gate's own help text")
    _add(rows, "bat: -B kept tests/__pycache__ from appearing", not pyc.exists(),
         "running run_smoke.bat created tests/__pycache__ - the -B flag is missing or ineffective (L-077)")


def _docs_rows(rows: list[Row], smoke) -> None:
    stale = re.compile(r"python(?:\.exe)?\s+-m\s+tests\.smoke")
    sources = [("README.md", (ROOT / "README.md").read_text(encoding="utf-8", errors="replace")),
               (".claude/AUTONOMY.md", (ROOT / ".claude" / "AUTONOMY.md").read_text(encoding="utf-8", errors="replace")),
               ("tests/smoke.py docstring", smoke.__doc__ or "")]
    for label, text in sources:
        _add(rows, f"bat: {label} documents `{GATE_COMMAND}`", GATE_COMMAND in text,
             f"{label} does not show the exact command with -B")
        bad = [ln.strip() for ln in text.splitlines() if stale.search(ln)]
        _add(rows, f"bat: {label} has no command without -B", not bad, f"without -B: {bad[:2]}")


# --------------------------------------------------------------------------- 2. merge-base and the history scan
def _run_git(smoke, root: Path, *args: str):
    ident = ("-c", "user.email=smoke@selftest.invalid", "-c", "user.name=smoke selftest", "-c", "commit.gpgsign=false")
    r = smoke.git(*ident, *args, cwd=root, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in the scratch repo: {(r.stderr or r.stdout).strip()[:200]}")
    return r.stdout.strip()


def _scratch_repo(smoke, root: Path) -> dict:
    """A throw-away repository: main -> (branch point) -> feature adds a secret, then removes it; main then moves on."""
    _run_git(smoke, root, "-c", "init.defaultBranch=main", "init", "--quiet")
    (root / "app.py").write_text("SAFE = 1\n", encoding="utf-8")
    _run_git(smoke, root, "add", "-A")
    _run_git(smoke, root, "commit", "--quiet", "-m", "base")
    base_sha = _run_git(smoke, root, "rev-parse", "HEAD")

    _run_git(smoke, root, "checkout", "--quiet", "-b", "feature")
    planted = "DB_PASS" + "WORD = 'hunter" + "22xyz'"          # assembled at run time, temp folder only
    (root / "app.py").write_text(f"SAFE = 1\n{planted}\n", encoding="utf-8")
    _run_git(smoke, root, "add", "-A")
    _run_git(smoke, root, "commit", "--quiet", "-m", "wire up the config")
    (root / "app.py").write_text("SAFE = 1\n", encoding="utf-8")   # the branch tree is clean again
    _run_git(smoke, root, "add", "-A")
    msg_token = "?to" + "ken=" + "A1b2C3d4E5f6G7h8"
    _run_git(smoke, root, "commit", "--quiet", "-m", "read it from the environment instead of " + msg_token)
    feature_sha = _run_git(smoke, root, "rev-parse", "HEAD")

    _run_git(smoke, root, "checkout", "--quiet", "main")
    (root / "only_on_main.txt").write_text("a change that landed on main after the branch was cut\n", encoding="utf-8")
    _run_git(smoke, root, "add", "-A")
    _run_git(smoke, root, "commit", "--quiet", "-m", "main moves on")
    main_tip = _run_git(smoke, root, "rev-parse", "HEAD")

    _run_git(smoke, root, "checkout", "--quiet", "-b", "clean-branch", base_sha)
    (root / "note.md").write_text("a harmless change\n", encoding="utf-8")
    _run_git(smoke, root, "add", "-A")
    _run_git(smoke, root, "commit", "--quiet", "-m", "a harmless change")

    _run_git(smoke, root, "checkout", "--quiet", "feature")
    return {"base": base_sha, "feature": feature_sha, "main_tip": main_tip}


def _mergebase_rows(rows: list[Row], smoke, repo: Path, shas: dict) -> None:
    mb = smoke.merge_base("main", repo)
    _eq(rows, "merge-base: diffs against the branch point, not the tip of main", mb, shas["base"],
        "a main that moved on must not be part of this branch's diff (L-074)")
    _add(rows, "merge-base: the branch point is not the tip of main", mb != shas["main_tip"],
         "the scratch repository did not actually move main on - the scenario proves nothing")

    vs_mb = set(_run_git(smoke, repo, "diff", "--name-only", mb).splitlines())
    vs_tip = set(_run_git(smoke, repo, "diff", "--name-only", "main").splitlines())
    _add(rows, "merge-base: a moved main shows no reversed change", "only_on_main.txt" not in vs_mb,
         f"the merge-base diff wrongly contains main's own file: {sorted(vs_mb)}")
    _add(rows, "merge-base: diffing against the tip WOULD show one (the trap)", "only_on_main.txt" in vs_tip,
         "diffing against the tip of main no longer shows the reversed change - the scenario no longer proves the fix")

    status, reason, details = smoke.check_secrets_and_strays("main", root=repo)
    found = [d for d in details if "commit " in d]
    _eq(rows, "merge-base: a secret added then removed still fails the gate", status, smoke.FAIL, reason)
    _add(rows, "merge-base: the finding names the branch history",
         any("branch history" in d for d in found), f"no history finding in {details[:3]}")
    _add(rows, "merge-base: commit MESSAGES are scanned too",
         any("message line" in d for d in found), f"no commit-message finding in {details[:3]}")
    _add(rows, "merge-base: the working-tree diff alone is clean",
         not any(d.startswith("app.py:") for d in details),
         "app.py is reported as a current change, so the history scan is not what caught it")

    _run_git(smoke, repo, "checkout", "--quiet", "clean-branch")
    try:
        status2, reason2, _d2 = smoke.check_secrets_and_strays("main", root=repo)
        _eq(rows, "merge-base: a clean branch still passes (no false positive)", status2, smoke.PASS, reason2)
    finally:
        _run_git(smoke, repo, "checkout", "--quiet", "feature")


# --------------------------------------------------------------------------- 3. import safety is an allowlist
SAFE_MODULE_SRC = '''\
"""A docstring."""
from __future__ import annotations
import logging, os
from pathlib import Path

LOG = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]
LIMIT = int(os.environ.get("LIMIT", "5"))
NAMES = "a,b".split(",")
LABEL = f"{ROOT.name} ({LIMIT})"

class Thing:
    field: int = 3
    def go(self, n: int = LIMIT) -> str:
        return str(n)

def main() -> None:
    LOG.info("hello")

if __name__ == "__main__":
    main()
'''


def _allowlist_rows(rows: list[Row], smoke) -> None:
    def verdict(src: str, extra: dict | None = None, name: str = "probe.py") -> str | None:
        return smoke.import_hazard(ast.parse(src), Path("erp") / name, extra=extra or {})

    _eq(rows, "allowlist: a plain module of imports / defs / constants is importable", verdict(SAFE_MODULE_SRC), None)
    _add(rows, "allowlist: a guarded __main__ does not make it compile-only",
         verdict("def main():\n    pass\nif __name__ == '__main__':\n    main()\n") is None, "a guarded main() was rejected")
    for label, src in [
        ("a known entry point", "import sys\nsys.exit(0)\n"),
        ("a brand-new entry point name nobody listed", "def run():\n    pass\nrun_the_whole_pipeline()\n"),
        ("a network call", "import requests\nrequests.get('http://example.invalid')\n"),
        ("a subprocess call", "import subprocess\nsubprocess.run(['x'])\n"),
        ("an unreviewed call in a class body", "class C:\n    data = build_everything()\n"),
        ("an unguarded call hidden in a try block", "try:\n    boot()\nexcept Exception:\n    pass\n"),
        ("a top-level while loop", "while True:\n    break\n"),
        ("a decorator that registers a route", "@app.get('/x')\ndef h():\n    pass\n"),
    ]:
        _add(rows, f"allowlist: {label} is compile-only", verdict(src) is not None,
             "a denylist would have let this through - the allowlist must reject it (L-075)")
    # deliberately conservative: a method call on a bare NAME could be a module function (os.replace, shutil.copy),
    # so only a method of a value (a literal, an f-string, the result of a safe call) is allowed
    _add(rows, "allowlist: a string method on a literal is allowed",
         verdict('PARTS = "a,b".split(",")\n') is None, "a method of a literal value must stay importable")
    _add(rows, "allowlist: a method on a bare name is compile-only (on purpose)",
         verdict("import os\nNAME = os.replace('a', 'b')\n") is not None,
         "a bare name could be a module, so a method call on it is never assumed safe")
    _add(rows, "allowlist: a Streamlit page is compile-only",
         "Streamlit" in (verdict("import streamlit as st\nst.title('x')\n") or ""), "a Streamlit import was not detected")
    _add(rows, "allowlist: a reviewed IMPORT_SAFE_EXTRA call is allowed",
         verdict("engine = make_engine()\n", extra={"calls": {"make_engine"}}) is None,
         "the per-module reviewed exception does not work")
    _add(rows, "allowlist: the same call is rejected in any other module",
         verdict("engine = make_engine()\n") is not None,
         "an IMPORT_SAFE_EXTRA entry must apply to its own module only")
    _add(rows, "allowlist: every IMPORT_SAFE_EXTRA entry states a reason",
         all(str(v.get("why", "")).strip() for v in smoke.IMPORT_SAFE_EXTRA.values()),
         "an entry without a 'why' is an unreviewed exception")

    mods = {
        "pkg": (ROOT / "pkg" / "__init__.py", ast.parse("")),
        "pkg.risky": (ROOT / "pkg" / "risky.py", ast.parse("start_the_server()\n")),
        "pkg.user": (ROOT / "pkg" / "user.py", ast.parse("from pkg import risky\n")),
    }
    verdicts = smoke.classify_imports(mods)
    _add(rows, "allowlist: importing a compile-only module makes the importer compile-only too",
         verdicts["pkg.user"] is not None and "compile-only" in verdicts["pkg.user"],
         f"pkg.user verdict was {verdicts['pkg.user']!r}")
    _add(rows, "allowlist: a clean sibling stays importable", verdicts["pkg"] is None, f"pkg verdict was {verdicts['pkg']!r}")


# --------------------------------------------------------------------------- 4. strict placeholder exemptions
def _placeholder_rows(rows: list[Row], smoke) -> None:
    real = ["hunter" + "22xyz", "ADMIN" + "2026", "my-real" + "-pass1", "ERP_APP" + "_PASSWORD", "Passw" + "0rd!",
            "S3cr3t" + "Value", "the" + "Quick1"]
    fake = ["YOUR_PASS" + "WORD", "your-api-key" + "-here", "change" + "me", "CHANGE_ME", "xxxx" + "xxxx", "<to" + "ken>",
            "${SEC" + "RET}", "{{pass" + "word}}", "%PG" + "PASS%", "exam" + "ple", "***" + "*", "..." + "."]
    for v in real:
        _add(rows, f"placeholder: a real-looking value is never exempt ({v[:4]}...)", not smoke._is_placeholder(v),
             "a strong secret name assigned this value must always be reported (L-072)")
    for v in fake:
        _add(rows, f"placeholder: a clearly fake value is exempt ({v[:6]}...)", smoke._is_placeholder(v),
             "documentation templates must not turn the gate red")
    _add(rows, "placeholder: a real-looking password literal is a finding",
         bool(smoke.scan_line("x.py", "DB_PASS" + "WORD = '" + "hunter" + "22xyz'")), "the scanner missed it")
    _add(rows, "placeholder: an ALL_CAPS value is a finding too",
         bool(smoke.scan_line("x.py", "SMTP_PASS" + "WORD = '" + "ADMIN" + "2026'")),
         "an ALL_CAPS word used to be exempt and hid a real password (L-072)")
    _add(rows, "placeholder: a template value is not a finding",
         not smoke.scan_line("x.py", "DB_PASS" + "WORD = '<ERP_APP" + "_PASSWORD>'"), "a <template> must stay exempt")
    _add(rows, "placeholder: the scanner's own sample table still holds", smoke._selftest_scanner() is None,
         str(smoke._selftest_scanner()))


# --------------------------------------------------------------------------- 5. git / OSError handling
class _WhichShim:
    """shutil with which() forced to None, so the "git is not installed" branch can be proven."""

    def __init__(self, real):
        self._real = real

    def which(self, *_a, **_k):
        return None

    def __getattr__(self, name):
        return getattr(self._real, name)


def _fail_run(exc):
    def runner(*_a, **_k):
        raise exc
    return runner


def _git_failure_rows(rows: list[Row], smoke, repo: Path, tmp: Path) -> None:
    real_run, real_shutil = smoke._RUN, smoke.shutil
    try:
        smoke._RUN = _fail_run(FileNotFoundError(2, "The system cannot find the file specified"))
        smoke.shutil = _WhichShim(real_shutil)
        try:
            smoke.git("status")
            _add(rows, "git: a missing git raises a clean GateError", False, "no exception at all")
        except smoke.GateError as exc:
            _add(rows, "git: a missing git raises a clean GateError", "not on PATH" in str(exc), str(exc))
        _add(rows, "git: the branch name degrades instead of raising",
             smoke.current_branch().startswith("(unknown"), smoke.current_branch())

        smoke._RUN = _fail_run(subprocess.TimeoutExpired(cmd="git", timeout=30))
        try:
            smoke.git("log")
            _add(rows, "git: a hanging git raises a clean GateError", False, "no exception at all")
        except smoke.GateError as exc:
            _add(rows, "git: a hanging git raises a clean GateError", "did not finish" in str(exc), str(exc))

        smoke._RUN = _fail_run(PermissionError(13, "Permission denied"))
        try:
            smoke.git("log")
            _add(rows, "git: any other OSError raises a clean GateError", False, "no exception at all")
        except smoke.GateError as exc:
            _add(rows, "git: any other OSError raises a clean GateError", "could not be started" in str(exc), str(exc))
    finally:
        smoke._RUN, smoke.shutil = real_run, real_shutil

    # a check that raises GateError becomes a FAIL row with the plain message (no traceback, no "crashed")
    scratch: list = []
    real_results, smoke.RESULTS = smoke.RESULTS, scratch
    try:
        def boom(_a, _s):
            raise smoke.GateError("git is not installed / not on PATH")
        res = smoke.run_check(0, "probe", boom, None, {})
    finally:
        smoke.RESULTS = real_results
    _add(rows, "git: a GateError becomes a clean FAIL row",
         res.status == smoke.FAIL and res.reason == "git is not installed / not on PATH" and not res.details,
         f"status={res.status!r} reason={res.reason!r} details={res.details!r}")

    not_a_repo = tmp / "not_a_repo"
    not_a_repo.mkdir(exist_ok=True)
    status, reason, _d = smoke.check_secrets_and_strays("main", root=not_a_repo)
    _add(rows, "git: outside a repository the gate FAILs cleanly",
         status == smoke.FAIL and "work tree" in reason, f"{status}: {reason}")
    try:
        smoke.worktree_files(not_a_repo)
        _add(rows, "git: a failed ls-files never looks like 'no files'", False,
             "worktree_files returned a value outside a repository - check 7 would pass for the wrong reason")
    except smoke.GateError as exc:
        _add(rows, "git: a failed ls-files never looks like 'no files'", "ls-files" in str(exc), str(exc))

    status, reason, _d = smoke.check_secrets_and_strays("no-such-base-branch", root=repo)
    _add(rows, "git: a missing base branch FAILs cleanly", status == smoke.FAIL and "does not exist" in reason,
         f"{status}: {reason}")
    status, reason, _d = smoke.check_secrets_and_strays("main; echo hi", root=repo)
    _add(rows, "git: a --base value that is not a ref name is refused",
         status == smoke.FAIL and "plain branch" in reason, f"{status}: {reason}")

    head = _run_git(smoke, repo, "rev-parse", "HEAD")
    _run_git(smoke, repo, "checkout", "--quiet", "--detach", head)
    try:
        _eq(rows, "git: a detached HEAD is named, not crashed at", smoke.current_branch(repo), "(detached HEAD)")
        status, reason, _d = smoke.check_secrets_and_strays("main", root=repo)
        _add(rows, "git: a detached HEAD still produces a row", status in (smoke.PASS, smoke.FAIL), f"{status}: {reason}")
    finally:
        _run_git(smoke, repo, "checkout", "--quiet", "feature")

    # LAST in this group: these two leave the scratch repository on an orphan branch, so nothing may follow them.
    _run_git(smoke, repo, "checkout", "--quiet", "--orphan", "unrelated")
    _run_git(smoke, repo, "commit", "--quiet", "-m", "a history of its own")
    try:
        smoke.merge_base("main", repo)
        _add(rows, "git: unrelated histories FAIL cleanly", False, "merge_base returned a value for unrelated histories")
    except smoke.GateError as exc:
        _add(rows, "git: unrelated histories FAIL cleanly", "common ancestor" in str(exc), str(exc))

    # a real unborn branch: the base branch exists, but HEAD points at a branch that has no commit yet
    _run_git(smoke, repo, "checkout", "--quiet", "--orphan", "fresh")
    status, reason, _d = smoke.check_secrets_and_strays("main", root=repo)
    _add(rows, "git: an unborn branch FAILs cleanly", status == smoke.FAIL and "no commit yet" in reason,
         f"{status}: {reason}")


# --------------------------------------------------------------------------- 6. branch discipline
def _branch_rows(rows: list[Row], smoke) -> None:
    owner = sorted(smoke.OWNER_FILES)
    cases = [
        ("main", {"erp/config.py"}, True, "an agent edited a source file while on main"),
        ("master", {"desktop/server.py", "README.md"}, True, "the same rule holds for master"),
        ("main", set(owner), False, "the owner's own dirty files are not agent work (Law.md rule 9)"),
        ("main", set(), False, "a clean main is fine"),
        ("main", set(owner) | {"tests/smoke.py"}, True, "one agent edit next to the owner's files still counts"),
        ("agent/20260923-gate-hardening", {"tests/smoke.py"}, False, "a branch is where work belongs"),
        ("(detached HEAD)", {"tests/smoke.py"}, False, "a detached HEAD is not main"),
        ("maintenance", {"erp/config.py"}, False, "only main / master are protected, not every name starting with 'main'"),
        ("MAIN", {"erp/config.py"}, True, "the branch name is compared case-insensitively"),
        ("", {"erp/config.py"}, False, "an unknown branch name must not crash the rule"),
        ("(not a git repository)", {"erp/config.py"}, False, "a non-repository is reported by the check, not by this rule"),
    ]
    for branch, modified, should_fail, note in cases:
        why = smoke.branch_discipline_problem(branch, set(modified))
        _add(rows, f"branch: {branch} + {sorted(modified)[:2] or 'nothing'} -> {'FAIL' if should_fail else 'ok'}",
             bool(why) == should_fail, f"{note}; the rule said {why!r}")
    status, reason, _d = smoke.check_branch_discipline()
    _add(rows, "branch: the real check produces a row", status in (smoke.PASS, smoke.FAIL), f"{status}: {reason}")


# --------------------------------------------------------------------------- 7. typography lines and font cross-check
def _typography_rows(rows: list[Row], smoke) -> None:
    cases = [
        ("desktop/static/shell.css", f"  --font: '{_OLD_FONT}', serif;", True, "live source must never name a retired font"),
        ("dashboard/script_center.py", f"FONT = \"{_OLD_FONT_2}\"", True, "live source, no exemption"),
        (".claude/lessons.md", f"font-family: '{_OLD_FONT}';", True,
         "a whole document is never exempt - only a history LINE is (L-124)"),
        ("README.md", f"body {{ font-family: '{_OLD_FONT}'; }}", True, "a change-log document may still hold live CSS"),
        (".claude/lessons.md", f"the old {_OLD_FONT} trio was retired on 2026-09-23", False, "a history line is exempt"),
        (".claude/journal.md", f"{_OLD_FONT} was replaced by Montserrat", False, "a history line is exempt"),
        ("PROJECT_NOTES.md", f"History: before 2026-09-21 the design used {_OLD_FONT}.", False, "a history line is exempt"),
        (".claude/skills/dashboard-craft/SKILL.md", f"Formerly {_OLD_FONT_2}; now Montserrat.", False,
         "a skill file quoting the old value is exempt on that line"),
        ("PROJECT_NOTES.md", f"font-family: {_OLD_FONT}, serif;   /* history of the old look */", True,
         "a line that SETS a font gets no exemption, however much history prose surrounds it (L-124)"),
        (".claude/journal.md", f"  --font: '{_OLD_FONT}';  /* before 2026-09-21 */", True,
         "a pasted CSS token in a journal entry is still a live rule"),
        ("README.md", f"the old trio was {_OLD_FONT}, retired on 2026-09-23", False,
         "prose about the change is what the change log is for"),
        ("desktop/static/shell.css", "  --font: 'Montserrat', 'Segoe UI', sans-serif;", False, "the current font is fine"),
    ]
    for path, line, should_flag, note in cases:
        why = smoke.old_font_problem(path, line)
        _add(rows, f"typography: {path} / {'flagged' if should_flag else 'exempt'} ({line[:26].strip()}...)",
             bool(why) == should_flag, f"{note}; the rule said {why!r}")

    tracked = {ln.strip() for ln in smoke.git("ls-files").stdout.splitlines() if ln.strip()}
    _add(rows, "typography: the content scan covers TRACKED files only", set(smoke.typography_files()) == tracked,
         "an untracked scratch note must never be able to turn the gate red (L-124)")

    from erp import typography as ty
    good_css = f"  --font: {ty.FONT_STACK};\n  --code: {ty.CODE_STACK};\n"
    good_html = f'<link rel="stylesheet" href="{ty.GOOGLE_FONTS_CSS_URL}">'
    args = (ty.FONT_NAME, ty.FONT_STACK, ty.CODE_STACK, ty.GOOGLE_FONTS_CSS_URL)
    _add(rows, "typography: the real shell.css / shell.html / typography.py agree",
         not smoke.font_consistency_problems(good_css, good_html, *args), "the synthetic 'all agree' case must be silent")
    drifts = [
        ("the <link> loads another family", good_css, good_html.replace(ty.FONT_NAME, "Inter")),
        ("shell.css asks for another family", good_css.replace(f"'{ty.FONT_NAME}'", "'Inter'"), good_html),
        ("the weights drift apart", good_css, good_html.replace("wght@400", "wght@300")),
        ("the <link> is gone", good_css, "<title>ERP Desk</title>"),
        ("the --code token is gone", f"  --font: {ty.FONT_STACK};\n", good_html),
    ]
    for label, css, html in drifts:
        _add(rows, f"typography: drift is caught ({label})", bool(smoke.font_consistency_problems(css, html, *args)),
             "this drift used to fail silently into the fallback font (L-124)")
    _add(rows, "typography: the files on disk pass the cross-check",
         not smoke.font_consistency_problems((ROOT / "desktop" / "static" / "shell.css").read_text(encoding="utf-8", errors="replace"),
                                             (ROOT / "desktop" / "static" / "shell.html").read_text(encoding="utf-8", errors="replace"),
                                             *args),
         "shell.css, shell.html and erp/typography.py do not agree right now")


# --------------------------------------------------------------------------- 8. numerals, weights, --theme flags
def _numerals_rows(rows: list[Row], smoke) -> None:
    """L-112 (a moving number keeps tabular figures), L-113 (no weight is downloaded for nothing) and
    L-114 / L-124 (the Streamlit font flag is proved through Streamlit's own parser, not by string equality)."""
    from erp import typography as ty

    # --- the rule-block reader the two scans are built on (a .py file writes its CSS with doubled braces) ---
    _eq(rows, "numerals: a plain CSS rule block is read",
        smoke.css_declaration_block(".kpi-value { font-size: 12px; }", ".kpi-value").strip(), "font-size: 12px;")
    _eq(rows, "numerals: a CSS rule inside an f-string is read",
        smoke.css_declaration_block(smoke.css_source("x.py", ".a {{ color:{INK}; font-variant-numeric:tabular-nums; }}"),
                                    ".a").strip(), "color:{INK}; font-variant-numeric:tabular-nums;")
    _add(rows, "numerals: a longer selector is not matched by a shorter one",
         smoke.css_declaration_block(".kpi-values { font-size: 9px; }", ".kpi-value") is None,
         "`.kpi-value` must not read the `.kpi-values` rule")

    # --- the tabular-figures table (L-112) ---
    good: dict[str, str] = {}                      # several selectors can live in one file: build it up, do not overwrite
    for path, sel, _why in smoke.TABULAR_NUMBER_RULES:
        good[path] = good.get(path, "") + f"{sel} {{ font-variant-numeric: tabular-nums; }}\n"
    _add(rows, "numerals: a table where every rule is tabular is silent", not smoke.tabular_number_problems(good))
    for path, sel, _why in smoke.TABULAR_NUMBER_RULES[:1] + smoke.TABULAR_NUMBER_RULES[-1:]:
        broken = dict(good, **{path: good[path].replace(f"{sel} {{ font-variant-numeric: tabular-nums; }}",
                                                        f"{sel} {{ font-variant-numeric: normal; }}")})
        _add(rows, f"numerals: losing tabular figures on `{sel}` is caught",
             any(sel in p for p in smoke.tabular_number_problems(broken)),
             "this is exactly how the Montserrat cycle left the KPI tiles jittering (L-112)")
        gone = dict(good); gone.pop(path)
        _add(rows, f"numerals: an unreadable {path} is reported, not skipped", bool(smoke.tabular_number_problems(gone)))
    _add(rows, "numerals: the real files pass the tabular-figure table",
         not smoke.tabular_number_problems({p: (ROOT / p).read_text(encoding="utf-8", errors="replace")
                                            for p in smoke.FONT_CSS_SOURCES if (ROOT / p).is_file()}),
         "a number that animates or sits in a column lost its tabular figures")

    # --- the downloaded weights (L-113) ---
    js_ok = "    var faces = ['400', '500', '600', '700'].map(function (w) {"
    css_ok = {"a.css": "h1 { font-weight: 600; } b { font-weight: 700; }"}
    _add(rows, "weights: every downloaded weight is painted -> silent",
         not smoke.font_weight_problems("600;700", css_ok, "var faces = ['600', '700'].map"))
    _add(rows, "weights: a weight nobody paints is caught",
         any("800" in p for p in smoke.font_weight_problems("600;700;800", css_ok, "var faces = ['600', '700', '800'].map")),
         "weight 800 was downloaded and painted nowhere - the exact debt this check exists for (L-113)")
    _add(rows, "weights: a CSS rule asking for a weight that is not downloaded is caught",
         bool(smoke.font_weight_problems("600", {"a.css": "h1 { font-weight: 900; }"}, "var faces = ['600'].map")))
    _add(rows, "weights: the shell.js font gate drifting from FONT_WEIGHTS is caught",
         any("shell.js" in p for p in smoke.font_weight_problems("600;700", css_ok, "var faces = ['600'].map")),
         "charts wait on Promise.all of that list, so a weight too many delays every first draw")
    _add(rows, "weights: an unreadable shell.js gate list is reported",
         any("shell.js" in p for p in smoke.font_weight_problems("600;700", css_ok, "// no gate here")))
    _add(rows, "weights: the real project passes", not smoke.font_weight_problems(
        ty.FONT_WEIGHTS,
        {p: (ROOT / p).read_text(encoding="utf-8", errors="replace") for p in smoke.FONT_CSS_SOURCES if (ROOT / p).is_file()},
        (ROOT / "desktop" / "static" / "shell.js").read_text(encoding="utf-8", errors="replace")),
        f"FONT_WEIGHTS is {ty.FONT_WEIGHTS} - every one of them must be painted by a CSS rule, and {js_ok.strip()} must match")

    # --- the Streamlit --theme.font flag, through Streamlit's own parser (L-114 / L-124) ---
    real, note = smoke.streamlit_font_flag_problems(ty.streamlit_theme_args(), ty.FONT_NAME,
                                                    ty.GOOGLE_FONTS_CSS_URL, ty.CODE_STACK)
    if note:
        _add(rows, "theme flags: streamlit is importable", False, note)
        return
    _add(rows, "theme flags: the real streamlit_theme_args() parse to the project's own font URL", not real,
         "; ".join(real))
    swallowed = ["--theme.font", f"'{ty.FONT_NAME}':{ty.GOOGLE_FONTS_CSS_URL},sans-serif",
                 "--theme.headingFont", f"'{ty.FONT_NAME}':{ty.GOOGLE_FONTS_CSS_URL}",
                 "--theme.codeFont", ty.CODE_STACK]
    _add(rows, "theme flags: a fallback stack swallowed into the URL is caught",
         bool(smoke.streamlit_font_flag_problems(swallowed, ty.FONT_NAME, ty.GOOGLE_FONTS_CSS_URL, ty.CODE_STACK)[0]),
         "Streamlit splits on the FIRST colon, so ',sans-serif' lands inside the URL and Google Fonts answers "
         "without display=swap (L-114) - two byte-equal copies of the flag would never have shown it (L-124)")
    no_swap = ty.GOOGLE_FONTS_CSS_URL.replace("&display=swap", "")
    _add(rows, "theme flags: losing display=swap is caught",
         bool(smoke.streamlit_font_flag_problems(["--theme.font", f"'{ty.FONT_NAME}':{no_swap}",
                                                  "--theme.headingFont", f"'{ty.FONT_NAME}':{no_swap}",
                                                  "--theme.codeFont", ty.CODE_STACK],
                                                 ty.FONT_NAME, ty.GOOGLE_FONTS_CSS_URL, ty.CODE_STACK)[0]))
    _add(rows, "theme flags: a code font that looks like a URL is caught",
         bool(smoke.streamlit_font_flag_problems(["--theme.font", f"'{ty.FONT_NAME}':{ty.GOOGLE_FONTS_CSS_URL}",
                                                  "--theme.headingFont", f"'{ty.FONT_NAME}':{ty.GOOGLE_FONTS_CSS_URL}",
                                                  "--theme.codeFont", "Consolas, monospace, other"],
                                                 ty.FONT_NAME, ty.GOOGLE_FONTS_CSS_URL, ty.CODE_STACK)[0]))


# --------------------------------------------------------------------------- entry point
def _skills_rows(rows: list[Row], smoke) -> None:
    """Skill files must not name repository paths that do not exist (check 6): a fake path is caught, a real one passes."""
    real = "desktop/static/shell.css"
    cases = [
        ("a real path passes", f"Tokens live in `{real}`.", []),
        ("a bare real file name passes (abbreviated path)", "See `shell.css` and `static/flow.js`.", []),
        ("a fake path is caught", "Copy from `desktop/static/no_such_page.js`.", ["desktop/static/no_such_page.js"]),
        ("a deleted module is caught", "Reuse `erp/html_report.py`.", ["erp/html_report.py"]),
        ("a fenced example is skipped", "```\nopen `desktop/static/no_such_page.js`\n```", []),
        ("text after a fence is checked again", "```\nx\n```\nSee `erp/no_such_module.py`.", ["erp/no_such_module.py"]),
        ("a placeholder path is not a path", "Feed `desktop/<x>_data.py`, style `static/*.css`.", []),
        ("a non-path token is ignored", "Call `Flow3D.mount` and set `--font`.", []),
        ("the allowlist exempts an illustrative name", "The feed is `x_data.py`.", []),
    ]
    for name, text, want in cases:
        got = smoke.skill_path_problems(text)
        _eq(rows, f"skills: {name}", got, want)
    _eq(rows, "skills: an explicit allowlist entry is honoured", smoke.skill_path_problems("`made_up_example.py`", allow=frozenset({"made_up_example.py"})), [])
    for f in smoke.SKILL_FILES:
        ghosts = smoke.skill_path_problems((ROOT / f).read_text(encoding="utf-8", errors="replace"))
        _add(rows, f"skills: {f} names only real paths", not ghosts, f"missing on disk: {ghosts[:5]}")


def run(smoke, base_ref: str = "main") -> list[Row]:
    """Return [(scenario name, ok, why not)] - run by tests/smoke.py check 9.

    `smoke` is the running gate module itself (it runs as __main__ under `-m`, so importing it by name would execute a
    second copy). Everything that needs a repository uses a throw-away one under the system temp folder.
    """
    rows: list[Row] = []
    before = smoke.worktree_files()
    tmp = Path(tempfile.mkdtemp(prefix="erp_smoke_selftest_"))
    repo = tmp / "repo"
    repo.mkdir()
    try:
        _section(rows, "bat", _bat_rows, smoke)
        _section(rows, "bat", _docs_rows, smoke)
        _section(rows, "allowlist", _allowlist_rows, smoke)
        _section(rows, "placeholder", _placeholder_rows, smoke)
        _section(rows, "branch", _branch_rows, smoke)
        _section(rows, "typography", _typography_rows, smoke)
        _section(rows, "numerals", _numerals_rows, smoke)
        _section(rows, "skills", _skills_rows, smoke)
        try:
            shas = _scratch_repo(smoke, repo)
        except Exception as exc:  # noqa: BLE001 - no scratch repo: say so instead of pretending the checks passed
            rows.append(("merge-base: the scratch repository could not be built", False, f"{type(exc).__name__}: {exc}"))
        else:
            _section(rows, "merge-base", _mergebase_rows, smoke, repo, shas)
            _section(rows, "git", _git_failure_rows, smoke, repo, tmp)
    finally:
        _rmtree(tmp)
    _add(rows, "cleanup: the scratch repository is gone", not tmp.exists(), f"{tmp} was left behind (L-042)")
    _add(rows, "cleanup: nothing new in the project work tree", smoke.worktree_files() == before,
         f"new: {sorted(smoke.worktree_files() - before)[:4]}")
    return rows
