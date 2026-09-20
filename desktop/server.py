"""
ERP Desk shell server (FastAPI). Serves the app shell (navigation + splash), the
live Reporting page's assets and its JSON feed, the "today's briefing" feed, and
a few app-control endpoints. The Management page is the existing Script Center
(Streamlit) running on its own port and embedded in the shell as an iframe.

The Data Flow page (desktop/flow_definition.py + flow_data.py) is served by the read-only
GET /api/flow feed; its refresh button is one more fixed-action POST.

Security model - this is a local desktop app, but "local" still has attackers
(any web page you visit can send requests to 127.0.0.1):
  * binds to 127.0.0.1 only (see launcher) and rejects any Host header that is
    not 127.0.0.1 / localhost (DNS-rebinding protection);
  * every state-changing endpoint is POST-only, needs a per-launch random token
    that only the served shell page contains, and rejects foreign Origins;
  * none of those endpoints accepts a path, command or any free-form input: they
    trigger fixed actions (refresh, generate digest, quit);
  * static files are served from desktop/static only (Starlette StaticFiles,
    traversal-safe) plus one fixed vendored file (plotly.min.js from the
    installed plotly package).

Run standalone for development (no window, no Streamlit):
    venv\\Scripts\\python.exe -m desktop.server
"""
from __future__ import annotations

import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.staticfiles import StaticFiles

from desktop.digest_service import DigestService
from desktop.flow_data import FlowStore
from desktop.report_data import ReportStore

logger = logging.getLogger("erp_desk.server")

DESKTOP_DIR = Path(__file__).resolve().parent
STATIC_DIR = DESKTOP_DIR / "static"
APP_SIGNATURE = "erp-desk"
TOKEN_HEADER = "x-erp-desk-token"


@dataclass
class AppContext:
    shell_port: int
    streamlit_port: int
    report: ReportStore
    digest: DigestService
    flow: FlowStore | None = None     # the Data Flow page's live feed (None = not wired, e.g. in tests)
    token: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    started_at: float = field(default_factory=time.time)
    quit_event: threading.Event = field(default_factory=threading.Event)
    streamlit_state: Callable[[], str] = lambda: "unknown"      # starting | ready | restarting | down
    window_mode: Callable[[], str] = lambda: "unknown"          # app-window | browser-tab | none
    focus_window: Callable[[], str] = lambda: "unavailable"     # bring the app window forward / reopen it
    last_heartbeat: float = 0.0

    @property
    def origins(self) -> set[str]:
        return {f"http://127.0.0.1:{self.shell_port}", f"http://localhost:{self.shell_port}"}


class _NoCacheStatic(StaticFiles):
    """The UI files change between releases and are tiny + local: never cache."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-store"
        return response


def _plotly_js_path() -> Path | None:
    try:
        import plotly
        p = Path(plotly.__file__).parent / "package_data" / "plotly.min.js"
        return p if p.is_file() else None
    except Exception:
        return None


def create_app(ctx: AppContext) -> FastAPI:
    app = FastAPI(title="ERP Desk", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"          # nobody may embed the shell
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    def require_token(request: Request) -> None:
        """Guard for every state-changing endpoint."""
        supplied = request.headers.get(TOKEN_HEADER, "")
        if not secrets.compare_digest(supplied.encode(), ctx.token.encode()):
            raise HTTPException(status_code=403, detail="Missing or invalid app token")
        origin = request.headers.get("origin")
        if origin is not None and origin not in ctx.origins:
            raise HTTPException(status_code=403, detail="Cross-origin request refused")

    # -- pages & assets ----------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def shell() -> Response:
        html = (STATIC_DIR / "shell.html").read_text(encoding="utf-8")
        html = (html.replace("__TOKEN__", ctx.token)
                    .replace("__STREAMLIT_URL__", f"http://127.0.0.1:{ctx.streamlit_port}/")
                    .replace("__REFRESH_SECONDS__", str(ctx.report.interval)))
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    @app.get("/favicon.ico")
    def favicon() -> Response:
        return FileResponse(STATIC_DIR / "app.ico", media_type="image/x-icon")

    @app.get("/vendor/plotly.min.js")
    def plotly_js() -> Response:
        path = _plotly_js_path()
        if path is None:
            raise HTTPException(status_code=404, detail="plotly.min.js not found in the installed plotly package")
        return FileResponse(path, media_type="application/javascript", headers={"Cache-Control": "max-age=86400"})

    app.mount("/static", _NoCacheStatic(directory=str(STATIC_DIR)), name="static")

    # -- read-only JSON ----------------------------------------------------
    @app.get("/api/ping")
    def ping() -> dict:
        return {"app": APP_SIGNATURE, "pid": os.getpid()}

    @app.get("/api/status")
    def status() -> dict:
        return {
            "app": APP_SIGNATURE,
            "uptime_seconds": int(time.time() - ctx.started_at),
            "ports": {"shell": ctx.shell_port, "script_center": ctx.streamlit_port},
            "streamlit": {"state": ctx.streamlit_state()},
            "report": ctx.report.meta(),
            "digest_job": ctx.digest.job_state(),
            "window_mode": ctx.window_mode(),
        }

    @app.get("/api/report/data")
    def report_data(since: str | None = None) -> JSONResponse:
        return JSONResponse(ctx.report.get(since), headers={"Cache-Control": "no-store"})

    @app.get("/api/digest")
    def digest() -> JSONResponse:
        return JSONResponse(ctx.digest.view(), headers={"Cache-Control": "no-store"})

    @app.get("/api/flow")
    def flow_data(since: str | None = None) -> JSONResponse:
        """Data Flow page: the pipeline map + live numbers/health/automation state (read-only)."""
        if ctx.flow is None:
            return JSONResponse({"ok": False, "has_data": False, "changed": False, "data": None, "error": "flow feed not available"},
                                headers={"Cache-Control": "no-store"})
        return JSONResponse(ctx.flow.get(since), headers={"Cache-Control": "no-store"})

    # -- fixed-action endpoints (token protected) --------------------------
    @app.post("/api/report/refresh", dependencies=[Depends(require_token)])
    def report_refresh() -> JSONResponse:
        changed = ctx.report.refresh()
        payload = ctx.report.get()
        payload["forced"] = True
        payload["was_changed"] = changed
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})

    @app.post("/api/flow/refresh", dependencies=[Depends(require_token)])
    def flow_refresh() -> JSONResponse:
        if ctx.flow is None:
            raise HTTPException(status_code=503, detail="flow feed not available")
        changed = ctx.flow.refresh(force=True)   # the button also re-scans the project for the map sync check
        payload = ctx.flow.get()
        payload["forced"] = True
        payload["was_changed"] = changed
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})

    @app.post("/api/digest/generate", dependencies=[Depends(require_token)])
    def digest_generate() -> JSONResponse:
        started = ctx.digest.start_generation(trigger="manual")
        return JSONResponse({"started": started, "job": ctx.digest.job_state()}, status_code=202 if started else 200)

    @app.post("/api/heartbeat", dependencies=[Depends(require_token)])
    def heartbeat() -> dict:
        ctx.last_heartbeat = time.time()
        return {"ok": True}

    @app.post("/api/window/focus", dependencies=[Depends(require_token)])
    def window_focus() -> dict:
        """Used by a second launch: reuse this instance instead of starting another."""
        return {"result": ctx.focus_window()}

    @app.post("/api/quit", dependencies=[Depends(require_token)])
    def quit_app() -> dict:
        logger.info("Quit requested from the app")
        ctx.quit_event.set()
        return {"quitting": True}

    return app


# ---------------------------------------------------------------------------
# uvicorn embedded in a thread (so the launcher can stop it deterministically)
# ---------------------------------------------------------------------------
class ServerThread:
    def __init__(self, app: FastAPI, port: int) -> None:
        import uvicorn

        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, access_log=False,
                                log_level="warning", timeout_graceful_shutdown=2)
        self.server = uvicorn.Server(config)
        self.server.install_signal_handlers = lambda: None  # not the main thread
        self.thread = threading.Thread(target=self.server.run, name="shell-server", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self, timeout: float = 6.0) -> None:
        self.server.should_exit = True
        self.thread.join(timeout)


if __name__ == "__main__":  # dev helper: shell + report + digest, without Streamlit / window
    import sys

    from erp.config import DESKTOP_PORT, DESKTOP_REFRESH_SECONDS

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    root = DESKTOP_DIR.parent
    store = ReportStore(DESKTOP_REFRESH_SECONDS)
    store.refresh()
    store.start()
    digest_service = DigestService(root, sys.executable, auto_generate=False)
    flow_store = FlowStore(root, report=store, digest=digest_service, interval_seconds=DESKTOP_REFRESH_SECONDS)
    flow_store.start()
    context = AppContext(shell_port=DESKTOP_PORT, streamlit_port=DESKTOP_PORT + 1, report=store,
                         digest=digest_service, flow=flow_store)
    srv = ServerThread(create_app(context), DESKTOP_PORT)
    srv.start()
    print(f"ERP Desk dev server on http://127.0.0.1:{DESKTOP_PORT}/  (Ctrl+C to stop)")
    try:
        while not context.quit_event.wait(1):
            pass
    except KeyboardInterrupt:
        pass
    flow_store.stop()
    srv.stop()
