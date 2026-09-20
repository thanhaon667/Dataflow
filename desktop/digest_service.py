"""
"Today's briefing" back end for ERP Desk: reads what erp/daily_digest.py wrote
(daily_digest_latest.json + daily_digest_history.json), turns the AI narrative
into structured sections the page can lay out as cards, and can run the digest
generator on demand (and, if enabled, once per day automatically).

The generator itself is NOT reimplemented - it is run as
`python -m erp.daily_digest` in a child process (fixed argv, nothing from the
browser reaches the command line), so the digest logic, its history file and its
AI call stay in exactly one place. It is always run with `--no-email`: opening
the app or clicking the button must never email the team (nor send a failure
alert) - the emailed digest stays the job of run_daily_digest.bat / Task
Scheduler. The child is put into the launcher's job object so quitting the app
also stops a digest that is running.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
from datetime import date, datetime
from pathlib import Path

from desktop import winutil

logger = logging.getLogger("erp_desk.digest")

# Same keys/labels as erp.daily_digest.METRIC_LABELS. Duplicated on purpose:
# importing erp.daily_digest configures root logging + opens daily_digest.log as
# a side effect, which a long-running app must not trigger just to read labels.
METRIC_LABELS = {
    "total_leads": "Total leads",
    "sla_breached_leads": "Leads breaching SLA",
    "sla_breached_tickets": "Tickets breaching SLA",
    "clickup_unsynced_leads": "Not synced to ClickUp",
    "leads_not_analyzed": "Not yet AI-analyzed",
    "open_tickets": "Open tickets",
    "avg_potential_score": "Avg. potential score",
}
# +1: a higher number is good news, -1: a higher number is bad news.
METRIC_POLARITY = {
    "total_leads": 1, "sla_breached_leads": -1, "sla_breached_tickets": -1,
    "clickup_unsynced_leads": -1, "leads_not_analyzed": -1, "open_tickets": -1,
    "avg_potential_score": 1,
}

DIGEST_TIMEOUT_SECONDS = 240

_SECTION_KEYS = (
    ("status", ("status", "overview", "summary")),
    ("opportunities", ("opportunit", "wins", "positives")),
    ("risks", ("risk", "concern", "threat")),
    ("actions", ("action", "recommend", "next step")),
)
_BULLET = re.compile(r"^\s*(?:[-*•●]|\d+[.)])\s+(.*\S)\s*$")


def _section_of(line: str) -> str | None:
    """Return the section key if `line` looks like a heading (short, not a bullet)."""
    stripped = line.strip()
    if not stripped or len(stripped) > 90 or _BULLET.match(line):
        return None
    plain = re.sub(r"^[#\s]+|[*_:\s]+$", "", stripped).lower()
    plain = re.sub(r"^\**\s*\d+[.)]?\s*", "", plain)
    if not plain or len(plain.split()) > 7:
        return None
    for key, needles in _SECTION_KEYS:
        if any(n in plain for n in needles):
            return key
    return None


def parse_narrative(narrative: str) -> dict:
    """Split the AI narrative into {status, opportunities[], risks[], actions[]}.
    Anything unrecognised ends up in `status`; if nothing at all is recognised
    the caller falls back to showing the raw text."""
    sections: dict = {"status": "", "opportunities": [], "risks": [], "actions": []}
    current = None
    found = False
    for line in (narrative or "").splitlines():
        heading = _section_of(line)
        if heading:
            current, found = heading, True
            continue
        if not line.strip():
            continue
        if current is None:
            continue  # title line ("Daily Digest - ...") before the first section
        bullet = _BULLET.match(line)
        if current == "status":
            text = bullet.group(1) if bullet else line.strip()
            sections["status"] = (sections["status"] + " " + text).strip()
        else:
            text = bullet.group(1) if bullet else line.strip()
            if bullet or not sections[current]:
                sections[current].append(text)
            else:
                sections[current][-1] += " " + text  # wrapped continuation of the previous bullet
    sections["recognised"] = found
    return sections


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read %s: %s", path.name, exc)
        return None


class DigestService:
    def __init__(self, project_root: Path, python_exe: str, job: "winutil.JobObject | None" = None,
                 auto_generate: bool = True) -> None:
        self.root = project_root
        self.latest_file = project_root / "daily_digest_latest.json"
        self.history_file = project_root / "daily_digest_history.json"
        self.python_exe = python_exe
        self.job = job
        self.auto_generate = auto_generate
        self._lock = threading.Lock()
        self._job = {"state": "idle", "started_at": None, "finished_at": None, "message": "", "trigger": None}
        self._auto_attempt_date: str | None = None
        self._proc: subprocess.Popen | None = None

    # -- read --------------------------------------------------------------
    def _history(self) -> list[dict]:
        rows = []
        if self.history_file.exists():
            for line in self.history_file.read_text(encoding="utf-8").splitlines():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        rows.sort(key=lambda r: r.get("date", ""))
        return rows[-14:]

    def view(self) -> dict:
        digest = _read_json(self.latest_file)
        out: dict = {
            "exists": False, "today": date.today().isoformat(),
            "labels": METRIC_LABELS, "polarity": METRIC_POLARITY,
            "history": self._history(), "job": self.job_state(),
            "auto_generate": self.auto_generate,
            "emails": False,  # in-app runs never send email (see module docstring)
        }
        if not isinstance(digest, dict) or "metrics" not in digest:
            return out
        narrative = str(digest.get("narrative") or "")
        parsed = parse_narrative(narrative)
        out.update({
            "exists": True,
            "date": digest.get("date"),
            "is_today": digest.get("date") == out["today"],
            "generated_at": digest.get("generated_at"),
            "previous_date": digest.get("previous_date"),
            "metrics": digest.get("metrics") or {},
            "deltas": digest.get("deltas") or {},
            "sections": parsed,
            "narrative": narrative,
            # ai_client.chat returns "[... not configured ...]" / "[Error ...]" instead of raising
            "ai_note": narrative.strip().startswith("[") and narrative.strip().endswith("]"),
            "ai_configured": bool(digest.get("ai_configured")),
            "email_sent": bool(digest.get("email_sent")),
        })
        return out

    # -- generate ----------------------------------------------------------
    def job_state(self) -> dict:
        with self._lock:
            return dict(self._job)

    def start_generation(self, trigger: str = "manual") -> bool:
        """Kick off `python -m erp.daily_digest --no-email` in the background. Returns False
        if one is already running."""
        with self._lock:
            if self._job["state"] == "running":
                return False
            self._job = {"state": "running", "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                         "finished_at": None, "message": "", "trigger": trigger}
        threading.Thread(target=self._run, name="digest-run", daemon=True).start()
        return True

    def _finish(self, state: str, message: str) -> None:
        with self._lock:
            self._job.update(state=state, message=message,
                             finished_at=datetime.now().astimezone().isoformat(timespec="seconds"))
        self._proc = None

    def _run(self) -> None:
        logger.info("Digest generation started (%s)", self._job.get("trigger"))
        try:
            proc = subprocess.Popen(
                [self.python_exe, "-m", "erp.daily_digest", "--no-email"], cwd=str(self.root),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                creationflags=winutil.CREATE_NO_WINDOW,
            )
            self._proc = proc
            if self.job:
                self.job.add(proc)
            try:
                out, _ = proc.communicate(timeout=DIGEST_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                winutil.kill_tree(proc.pid)
                proc.communicate()
                self._finish("error", f"Timed out after {DIGEST_TIMEOUT_SECONDS}s")
                logger.warning("Digest generation timed out")
                return
        except Exception as exc:
            logger.exception("Could not start the digest generator")
            self._finish("error", f"Could not start: {type(exc).__name__}")
            return

        if proc.returncode == 0:
            self._finish("done", "Digest generated")
            logger.info("Digest generation finished OK")
        else:
            tail = (out or b"").decode("utf-8", "replace").strip().splitlines()[-12:]
            logger.warning("Digest generation failed (exit %s). Last output:\n%s", proc.returncode, "\n".join(tail))
            self._finish("error", f"Generator exited with code {proc.returncode} - see daily_digest.log")

    def terminate(self) -> None:
        proc = self._proc
        if proc and proc.poll() is None:
            winutil.kill_tree(proc.pid)

    # -- daily automation --------------------------------------------------
    def run_scheduler(self, stop: threading.Event, initial_delay: float = 20.0) -> None:
        """Once per calendar day, if today has no digest yet, generate it. At most
        one automatic attempt per day (a failure is not retried in a loop; the
        Generate button is always available)."""
        if not self.auto_generate:
            logger.info("Automatic daily digest is disabled (DESKTOP_AUTO_DIGEST=0)")
            return
        if stop.wait(initial_delay):
            return
        while not stop.is_set():
            today = date.today().isoformat()
            digest = _read_json(self.latest_file)
            has_today = isinstance(digest, dict) and digest.get("date") == today
            if not has_today and self._auto_attempt_date != today:
                self._auto_attempt_date = today
                logger.info("No digest for %s yet - generating it automatically", today)
                self.start_generation(trigger="auto")
            stop.wait(60)
