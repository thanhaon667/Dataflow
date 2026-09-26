"""
ERP Desk launcher - starts everything the desktop app needs, opens a chrome-less
app window, and tears everything down again.

What "the app" is: a local web app that looks like a desktop app (no Electron, no
.exe). This process is the supervisor:

    launcher (pythonw, no console)
      |- shell server (FastAPI/uvicorn thread)  -> navigation, live Reporting page, briefing feed
      |- report refresher + Data Flow feed + daily-digest scheduler (threads)
      |- Script Center (Streamlit child process) -> the Management page, embedded in the shell
      '- Edge / Chrome `--app=` window            -> no tabs, no address bar, own profile + taskbar icon

Guarantees:
  * everything binds to 127.0.0.1 only; preferred ports are fixed uncommon ones and a
    free port is picked automatically if another program already uses them;
  * single instance: launching again focuses (or reopens) the running app's window;
  * every child process lives in one Windows Job Object with KILL_ON_JOB_CLOSE, so
    closing the window, the in-app Quit button, `--stop`, Ctrl+C, or even killing this
    process leaves no orphaned Streamlit / uvicorn / browser process behind;
  * logs go to erp_desktop.log (this process) and erp_desktop_streamlit.log (Streamlit).

Run:
    venv\\Scripts\\pythonw.exe -m desktop            (normal, no console window)
    venv\\Scripts\\python.exe  -m desktop --console  (debug: logs to the console too)
    venv\\Scripts\\python.exe  -m desktop --stop     (ask a running instance to quit)
    venv\\Scripts\\python.exe  -m desktop --status   (print what is running)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
os.chdir(PROJECT_ROOT)
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from desktop import winutil  # noqa: E402
from erp import typography  # noqa: E402  (stdlib only: the project's font tokens)

STATE_DIR = PROJECT_ROOT / ".erp_desktop"
STATE_FILE = STATE_DIR / "instance.json"
PROFILE_DIR = STATE_DIR / "browser_profile"
LOG_FILE = PROJECT_ROOT / "erp_desktop.log"
STREAMLIT_LOG = PROJECT_ROOT / "erp_desktop_streamlit.log"
WINDOW_TITLE = "ERP Desk"          # must match <title> in desktop/static/shell.html
APP_SIGNATURE = "erp-desk"

STREAMLIT_START_TIMEOUT = 90       # seconds to wait for Streamlit's health endpoint
REPORT_START_TIMEOUT = 25          # seconds to wait for the first report snapshot
WINDOW_GONE_GRACE = 4              # seconds without a window (after seeing one) before quitting
HEARTBEAT_GRACE = 45               # browser-tab fallback: silence before quitting
MAX_STREAMLIT_RESTARTS = 3

logger = logging.getLogger("erp_desk")

# Same brand theme as run_script_center.bat. Passed on the command line (not
# .streamlit/config.toml) so the theme belongs to this launch only.
THEME_ARGS = [
    "--theme.base", "light",
    "--theme.primaryColor", "#3452eb",
    "--theme.backgroundColor", "#f5f3ef",
    "--theme.secondaryBackgroundColor", "#ffffff",
    "--theme.textColor", "#1c1d22",
    "--theme.borderColor", "#e7e2d8",
    "--theme.redColor", "#e0393e",
    "--theme.greenColor", "#12b886",
    "--theme.blueColor", "#3452eb",
    "--theme.violetColor", "#7c5cff",
    "--theme.orangeColor", "#f2b705",
    "--theme.baseRadius", "16px",
    "--theme.buttonRadius", "12px",
    "--theme.showWidgetBorder", "true",
    *typography.streamlit_theme_args(),     # Montserrat for text and headings, a system monospace for code (erp/typography.py)
]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def setup_logging(console: bool) -> None:
    # pythonw has no stdout/stderr: point them somewhere so a stray print()
    # (or a library warning) cannot crash the process.
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
    handlers: list[logging.Handler] = [RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8")]
    if console:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                        handlers=handlers, force=True)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    def _hook(exc_type, exc, tb):
        logger.critical("Unhandled exception", exc_info=(exc_type, exc, tb))
    sys.excepthook = _hook


def is_port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def pick_port(preferred: int, avoid: set[int]) -> int:
    if preferred not in avoid and 1024 <= preferred < 65536 and is_port_free(preferred):
        return preferred
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        chosen = s.getsockname()[1]
    logger.warning("Preferred port %s is in use by another program - using free port %s instead", preferred, chosen)
    return chosen


# Loopback calls must never go through a system/corporate proxy.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_json(url: str, method: str = "GET", token: str | None = None, timeout: float = 2.0):
    req = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None)
    if token:
        req.add_header("X-ERP-Desk-Token", token)
    with _OPENER.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def http_ok(url: str, timeout: float = 1.0) -> bool:
    try:
        with _OPENER.open(url, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def read_state() -> dict | None:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def write_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def is_running_instance(state: dict | None) -> bool:
    """True if `state` describes a live ERP Desk shell server."""
    if not state:
        return False
    try:
        return http_json(f"http://127.0.0.1:{state['shell_port']}/api/ping", timeout=1.5).get("app") == APP_SIGNATURE
    except Exception:
        return False


def find_browser() -> tuple[str, str] | None:
    """(kind, path) of Microsoft Edge (preferred - always on Windows 10/11) or Chrome."""
    candidates: list[tuple[str, str]] = []
    if winutil.IS_WINDOWS:
        try:
            import winreg
            for exe, kind in (("msedge.exe", "edge"), ("chrome.exe", "chrome")):
                for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                    try:
                        with winreg.OpenKey(hive, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}") as key:
                            candidates.append((kind, winreg.QueryValue(key, None)))
                    except OSError:
                        pass
        except ImportError:
            pass
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    local = os.environ.get("LOCALAPPDATA", "")
    candidates += [
        ("edge", rf"{pf86}\Microsoft\Edge\Application\msedge.exe"),
        ("edge", rf"{pf}\Microsoft\Edge\Application\msedge.exe"),
        ("chrome", rf"{pf}\Google\Chrome\Application\chrome.exe"),
        ("chrome", rf"{pf86}\Google\Chrome\Application\chrome.exe"),
        ("chrome", rf"{local}\Google\Chrome\Application\chrome.exe"),
    ]
    for kind in ("edge", "chrome"):  # Edge first, whatever order the registry gave
        for k, path in candidates:
            if k == kind and path and Path(path).is_file():
                return k, path
    return None


def python_exe() -> str:
    """Console python.exe next to the (possibly pythonw.exe) interpreter running us."""
    exe = Path(sys.executable)
    console = exe.with_name("python.exe")
    return str(console if console.is_file() else exe)


# ---------------------------------------------------------------------------
# The supervisor
# ---------------------------------------------------------------------------
class Supervisor:
    def __init__(self, open_window: bool = True) -> None:
        from erp import config  # after sys.path/cwd are set; loads .env

        self.config = config
        self.want_window = open_window
        self.job = winutil.JobObject()
        self.quit_event = threading.Event()
        self.stop_reason = ""
        self.ready = False
        self.window_mode = "none"
        self._window_lock = threading.Lock()
        self._browser_proc: subprocess.Popen | None = None
        self._window_launched_at = 0.0
        self._window_seen = False
        self._window_missing_since: float | None = None

        self.streamlit_proc: subprocess.Popen | None = None
        self.streamlit_state = "starting"
        self._st_restarts: list[float] = []
        self._st_log = None

        self.shell_port = pick_port(config.DESKTOP_PORT, set())
        self.streamlit_port = pick_port(config.DESKTOP_STREAMLIT_PORT, {self.shell_port})
        self.server = None
        self.store = None
        self.flow = None
        self.digest = None
        self.ctx = None
        self._threads_stop = threading.Event()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.shell_port}/"

    # -- Streamlit (Management page) --------------------------------------
    def _streamlit_cmd(self) -> list[str]:
        return [
            python_exe(), "-m", "streamlit", "run", "dashboard/script_center.py",
            "--server.port", str(self.streamlit_port),
            "--server.address", "127.0.0.1",
            "--server.headless", "true",
            "--browser.gatherUsageStats", "false",
            "--client.toolbarMode", "minimal",
            *THEME_ARGS,
        ]

    def start_streamlit(self) -> None:
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8",
                   STREAMLIT_BROWSER_GATHER_USAGE_STATS="false")
        if self._st_log is None:
            self._st_log = open(STREAMLIT_LOG, "ab")
        self._st_log.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} starting Script Center on :{self.streamlit_port}\n".encode())
        self._st_log.flush()
        self.streamlit_proc = subprocess.Popen(
            self._streamlit_cmd(), cwd=str(PROJECT_ROOT), env=env, stdin=subprocess.DEVNULL,
            stdout=self._st_log, stderr=subprocess.STDOUT,
            creationflags=winutil.CREATE_NO_WINDOW | winutil.CREATE_NEW_PROCESS_GROUP,
        )
        self.job.add(self.streamlit_proc)
        self.streamlit_state = "starting"
        logger.info("Script Center (Streamlit) started, pid %s, port %s", self.streamlit_proc.pid, self.streamlit_port)

    def _streamlit_healthy(self) -> bool:
        return http_ok(f"http://127.0.0.1:{self.streamlit_port}/_stcore/health", timeout=1.0)

    def _check_streamlit(self) -> None:
        proc = self.streamlit_proc
        if proc is None:
            return
        if proc.poll() is not None:
            now = time.time()
            self._st_restarts = [t for t in self._st_restarts if now - t < 300]
            if len(self._st_restarts) >= MAX_STREAMLIT_RESTARTS:
                if self.streamlit_state != "down":
                    logger.error("Script Center exited (code %s) too many times - giving up; see %s",
                                 proc.returncode, STREAMLIT_LOG.name)
                self.streamlit_state = "down"
                return
            logger.warning("Script Center exited unexpectedly (code %s) - restarting", proc.returncode)
            self._st_restarts.append(now)
            self.streamlit_state = "restarting"
            self.start_streamlit()
            self.streamlit_state = "restarting"
            return
        if self.streamlit_state in ("starting", "restarting") and self._streamlit_healthy():
            self.streamlit_state = "ready"
            logger.info("Script Center is healthy")

    # -- browser window ----------------------------------------------------
    def open_window(self) -> str:
        """Open the chrome-less app window (or a plain browser tab if neither Edge
        nor Chrome is installed). Returns the resulting window mode."""
        with self._window_lock:
            browser = find_browser()
            self._window_launched_at = time.time()
            self._window_seen = False
            self._window_missing_since = None
            if browser is None:
                logger.warning("Neither Edge nor Chrome found - opening the default browser instead (tab mode)")
                webbrowser.open(self.url)
                self.window_mode = "browser-tab"
                return self.window_mode
            kind, path = browser
            PROFILE_DIR.mkdir(parents=True, exist_ok=True)
            args = [
                path, f"--app={self.url}", f"--user-data-dir={PROFILE_DIR}",
                "--no-first-run", "--no-default-browser-check",
                "--window-size=1440,920",
                "--disable-features=Translate", "--disable-sync", "--disable-background-mode",
            ]
            try:
                proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                logger.exception("Could not start %s", kind)
                webbrowser.open(self.url)
                self.window_mode = "browser-tab"
                return self.window_mode
            self.job.add(proc)
            self._browser_proc = proc
            self.window_mode = "app-window"
            logger.info("Opened %s app window (%s)", kind, path)
            return self.window_mode

    def focus_or_open_window(self) -> str:
        """Called for a second launch: reuse this instance."""
        if not self.ready:
            return "starting"  # the first launch will open the window itself in a moment
        windows = winutil.find_app_windows(WINDOW_TITLE)
        if windows:
            winutil.focus_window(windows[0])
            return "focused"
        self.open_window()
        return "opened"

    def _watch_window(self) -> None:
        now = time.time()
        if self.window_mode == "app-window":
            if winutil.find_app_windows(WINDOW_TITLE):
                self._window_seen = True
                self._window_missing_since = None
            elif self._window_seen:
                self._window_missing_since = self._window_missing_since or now
                if now - self._window_missing_since >= WINDOW_GONE_GRACE:
                    self.request_quit("app window closed")
            elif now - self._window_launched_at > 60:
                logger.warning("No app window appeared within 60 s - falling back to heartbeat-based close detection")
                self.window_mode = "browser-tab"
        elif self.window_mode == "browser-tab":
            beat = self.ctx.last_heartbeat
            if beat and now - beat > HEARTBEAT_GRACE:
                self.request_quit("browser page stopped responding (closed)")

    # -- lifecycle ---------------------------------------------------------
    def request_quit(self, reason: str) -> None:
        if not self.quit_event.is_set():
            self.stop_reason = reason
            logger.info("Quit requested: %s", reason)
            self.quit_event.set()

    def run(self) -> int:
        from desktop.digest_service import DigestService
        from desktop.flow_data import FlowStore
        from desktop.report_data import ReportStore
        from desktop.server import AppContext, ServerThread, create_app

        logger.info("ERP Desk starting (pid %s, python %s, job object %s)", os.getpid(),
                    sys.version.split()[0], "on" if self.job.active else "unavailable")
        write_state(self._state("starting"))

        self.start_streamlit()
        self.store = ReportStore(self.config.DESKTOP_REFRESH_SECONDS)
        self.digest = DigestService(PROJECT_ROOT, python_exe(), self.job, auto_generate=self.config.DESKTOP_AUTO_DIGEST)
        self.flow = FlowStore(PROJECT_ROOT, report=self.store, digest=self.digest,
                              streamlit_state=lambda: self.streamlit_state,
                              interval_seconds=self.config.DESKTOP_REFRESH_SECONDS, job=self.job)
        self.ctx = AppContext(shell_port=self.shell_port, streamlit_port=self.streamlit_port,
                              report=self.store, digest=self.digest, flow=self.flow, quit_event=self.quit_event)
        self.ctx.streamlit_state = lambda: self.streamlit_state
        self.ctx.window_mode = lambda: self.window_mode
        self.ctx.focus_window = self.focus_or_open_window
        write_state(self._state("starting"))  # now includes the token so `--stop` works during startup

        self.store.start()
        self.flow.start()
        self.server = ServerThread(create_app(self.ctx), self.shell_port)
        self.server.start()

        if not self._wait(lambda: http_ok(f"http://127.0.0.1:{self.shell_port}/api/ping"), 20, "shell server"):
            logger.critical("Shell server did not come up on port %s", self.shell_port)
            self.stop_reason = "startup failure"
            winutil.message_box("ERP Desk", f"The app server could not start on port {self.shell_port}.\n\nSee {LOG_FILE.name} for details.", error=True)
            self.shutdown()
            return 1

        self._wait(lambda: self._poll_streamlit_ready(), STREAMLIT_START_TIMEOUT, "Script Center")
        if self.streamlit_state != "ready":
            logger.error("Script Center is not healthy yet - opening the app anyway (see %s)", STREAMLIT_LOG.name)
        self._wait(lambda: self.store.meta()["has_data"] or self.store.meta()["error"], REPORT_START_TIMEOUT, "first report snapshot")

        threading.Thread(target=self.digest.run_scheduler, args=(self._threads_stop,), name="digest-scheduler", daemon=True).start()

        self.ready = True
        write_state(self._state("ready"))
        logger.info("ERP Desk is ready at %s  (Script Center on :%s)", self.url, self.streamlit_port)
        if self.want_window:
            self.open_window()
        else:
            self.window_mode = "none"
            logger.info("--no-window: not opening a window; stop with `python -m desktop --stop`")

        signal.signal(signal.SIGINT, lambda *_: self.request_quit("Ctrl+C"))
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, lambda *_: self.request_quit("Ctrl+Break"))

        while not self.quit_event.wait(1.0):
            self._check_streamlit()
            self._watch_window()
        return self.shutdown()

    def _poll_streamlit_ready(self) -> bool:
        self._check_streamlit()
        return self.streamlit_state == "ready" or self.streamlit_state == "down"

    def _wait(self, predicate, timeout: float, what: str) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline and not self.quit_event.is_set():
            try:
                if predicate():
                    return True
            except Exception:
                logger.exception("waiting for %s", what)
            time.sleep(0.4)
        return False

    def _state(self, phase: str) -> dict:
        return {
            "app": APP_SIGNATURE, "pid": os.getpid(), "phase": phase,
            "shell_port": self.shell_port, "streamlit_port": self.streamlit_port,
            "token": self.ctx.token if self.ctx else "", "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }

    def shutdown(self) -> int:
        logger.info("Shutting down (%s)", self.stop_reason or "quit requested from the app")
        self._threads_stop.set()
        # 1. close the app window politely so the browser exits cleanly
        for hwnd in winutil.find_app_windows(WINDOW_TITLE):
            winutil.close_window(hwnd)
        deadline = time.time() + 2.5
        while time.time() < deadline and winutil.find_app_windows(WINDOW_TITLE):
            time.sleep(0.2)
        # 2. app server + refresher
        if self.store:
            self.store.stop()
        if self.flow:
            self.flow.stop()
        if self.server:
            self.server.stop()
        # 3. Script Center + any running digest
        if self.digest:
            self.digest.terminate()
        if self.streamlit_proc and self.streamlit_proc.poll() is None:
            winutil.kill_tree(self.streamlit_proc.pid)
            try:
                self.streamlit_proc.wait(5)
            except subprocess.TimeoutExpired:
                pass
        # 4. anything left in the job (browser processes, stragglers) dies with the handle
        self.job.close()
        if self._st_log:
            self._st_log.close()
        try:
            STATE_FILE.unlink()
        except OSError:
            pass
        logger.info("ERP Desk stopped cleanly")
        return 0


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------
def cmd_stop() -> int:
    state = read_state()
    if not is_running_instance(state):
        print("ERP Desk is not running.")
        return 0
    try:
        http_json(f"http://127.0.0.1:{state['shell_port']}/api/quit", "POST", state.get("token"), timeout=5)
    except Exception as exc:
        print(f"Could not ask ERP Desk to quit ({exc}); killing process {state['pid']}.")
        winutil.kill_tree(int(state["pid"]))
        return 1
    for _ in range(50):
        if not is_running_instance(read_state()):
            print("ERP Desk stopped.")
            return 0
        time.sleep(0.3)
    print("ERP Desk did not stop in time; killing it.")
    winutil.kill_tree(int(state["pid"]))
    return 1


def cmd_status() -> int:
    state = read_state()
    if not is_running_instance(state):
        print("ERP Desk is not running.")
        return 1
    print(json.dumps(http_json(f"http://127.0.0.1:{state['shell_port']}/api/status"), indent=2))
    return 0


def reuse_running_instance() -> int:
    """Another launcher owns the mutex: focus its window (or reopen it) and exit."""
    deadline = time.time() + 60  # the first instance may still be starting up
    while time.time() < deadline:
        state = read_state()
        if state and state.get("token") and is_running_instance(state):
            try:
                result = http_json(f"http://127.0.0.1:{state['shell_port']}/api/window/focus", "POST", state["token"], timeout=5)
                logger.info("Already running - second launch handled: %s", result.get("result"))
            except Exception:
                logger.exception("Could not reach the running instance")
                return 1
            return 0
        time.sleep(0.5)
    winutil.message_box(
        "ERP Desk", "ERP Desk seems to be running but is not responding.\n\n"
        f"Close it from the Task Manager (python) or run:\n  venv\\Scripts\\python.exe -m desktop --stop\n\nLog: {LOG_FILE.name}",
        error=True)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="desktop", description="ERP Desk - local desktop app launcher")
    parser.add_argument("--console", action="store_true", help="also log to the console (debugging)")
    parser.add_argument("--no-window", action="store_true", help="start the services but do not open a window")
    parser.add_argument("--stop", action="store_true", help="ask the running instance to quit")
    parser.add_argument("--status", action="store_true", help="print the running instance's status")
    args = parser.parse_args(argv)

    setup_logging(args.console or args.stop or args.status)
    if args.stop:
        return cmd_stop()
    if args.status:
        return cmd_status()

    mutex_name = "Local\\ERPDesk-" + hashlib.sha1(str(PROJECT_ROOT).lower().encode()).hexdigest()[:12]
    instance = winutil.SingleInstance(mutex_name)
    if not instance.acquired:
        return reuse_running_instance()

    supervisor = None
    try:
        supervisor = Supervisor(open_window=not args.no_window)
        return supervisor.run()
    except Exception:
        logger.exception("ERP Desk crashed")
        if supervisor is not None:
            supervisor.stop_reason = "crash"
            try:
                supervisor.shutdown()   # stop Streamlit / server / browser even on a crash (the job object is the backstop)
            except Exception:
                logger.exception("shutdown after crash also failed")
        winutil.message_box("ERP Desk", f"ERP Desk could not start.\n\nSee {LOG_FILE.name} for details.", error=True)
        return 1
    finally:
        instance.release()


if __name__ == "__main__":
    sys.exit(main())
