"""
ERP Desk shell server (FastAPI). Serves the app shell (navigation + splash), the
live Reporting page's assets and its JSON feed, the "today's briefing" feed, and
a few app-control endpoints. The Management page is the existing Script Center
(Streamlit) running on its own port and embedded in the shell as an iframe.

The Leads page (funnel + SLA explorer for analysts, desktop/leads_data.py) is served by the read-only
GET /api/leads/analysis and GET /api/leads/export.csv (server-side filters, whitelisted, bound SQL parameters).
Its second tab, "Sources & cohorts" (desktop/sources_data.py), is served the same way by the read-only
GET /api/leads/sources and GET /api/leads/sources.csv; only one of the two tabs polls at a time, so the page
still has exactly one poller.

The Channels page (the marketing channel-performance report for managers and analysts, desktop/channels_data.py) is
served by the read-only GET /api/channels and GET /api/channels.csv. It reads only the two Phase 4 rollup tables (plus
channel/campaign name lookups), never the raw interaction table; in the owner's database today the honest answer is
"not installed" or "no data loaded yet", which the page renders as such.

The Data Flow page (desktop/flow_definition.py + flow_data.py) is served by the read-only
GET /api/flow feed; its refresh button is one more fixed-action POST. The Today page (the landing page:
"today at a glance" in plain English) is served by the read-only GET /api/today (desktop/today_data.py).

The Health page ("is this system actually working, and if not what do I do about it?", desktop/health_data.py) is
served by the read-only GET /api/health. It opens no connection and starts no thread of its own: it composes the
Data Flow snapshot and the Today payload (whose own on-demand build, shared with the Today page, is what may open
a connection once its few-second cache has expired), so the Today health strip and the Health page can never
disagree. `fresh` is the only query parameter it accepts; an unknown name is refused with a 400 that names it,
checked before the cache (lesson L-098), the same as /api/leads/analysis.

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

from desktop.channels_data import ChannelsStore
from desktop.insights_data import InsightsStore, XLSX_MIME
from desktop.placements_data import PlacementsStore
from desktop.digest_service import DigestService
from desktop.flow_data import FlowStore
from desktop.health_data import HealthStore
from desktop.health_data import unknown_params as health_unknown_params
from desktop.leads_data import LeadsStore
from desktop.report_data import ReportStore
from desktop.sources_data import SourcesStore
from desktop.today_data import TodayStore

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
    today: TodayStore | None = None   # the Today page's feed; built from `flow` in create_app when not given
    leads: LeadsStore | None = None   # the Leads page's feed (filters, funnel, SLA, CSV); built from `flow` in create_app when not given
    sources: SourcesStore | None = None   # the Leads page's "Sources & cohorts" tab; same, built from `flow` when not given
    health: HealthStore | None = None  # the Health page's feed; composed from `flow` + `today` in create_app when not given
    insights: InsightsStore | None = None   # the Channels page's Insights panel + Excel report (same rollup tables, own cache)
    placements: PlacementsStore | None = None   # the Channels page's Placements panel (the placement rollup only, own cache)
    channels: ChannelsStore | None = None   # the Channels page's feed (marketing rollup report); built in create_app when not given
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
    if ctx.today is None:
        ctx.today = TodayStore(flow=ctx.flow)      # on demand, cached a few seconds; no thread to stop
    if ctx.leads is None:
        ctx.leads = LeadsStore(flow=ctx.flow)      # same: on demand, a few seconds of cache, no thread
    if ctx.sources is None:
        ctx.sources = SourcesStore(flow=ctx.flow)  # same again: the Leads page's second tab
    if ctx.insights is None:
        ctx.insights = InsightsStore(flow=ctx.flow)
    if ctx.placements is None:
        ctx.placements = PlacementsStore(flow=ctx.flow)   # on demand, a few seconds of cache, no thread; reads the placement rollup only
    if ctx.channels is None:
        ctx.channels = ChannelsStore(flow=ctx.flow)   # on demand, a few seconds of cache, no thread; reads the two rollup tables only
    if ctx.health is None:
        # It feeds on the two stores above (their own caches), so opening the Health page adds no poller and no thread.
        ctx.health = HealthStore(flow=ctx.flow, today=ctx.today)
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

    @app.get("/api/today")
    def today(fresh: bool = False) -> JSONResponse:
        """Today page (the landing page): status sentence, KPI tiles, attention list, system health (read-only).
        `fresh=1` only skips the few-second cache; it never writes or triggers anything."""
        return JSONResponse(ctx.today.get(fresh=fresh), headers={"Cache-Control": "no-store"})

    def _query_lists(request: Request) -> dict[str, list[str]]:
        """The query string as {name: [values]}: repeated parameters (rep=A&rep=B) stay lists. Nothing is interpreted here."""
        out: dict[str, list[str]] = {}
        for k, v in request.query_params.multi_items():
            out.setdefault(k, []).append(v)
        return out

    @app.get("/api/health")
    def health(request: Request) -> JSONResponse:
        """Health page: one card per dependency - state, why, last checked / succeeded, what stops working without it
        and a fix hint to copy. Read-only and side-effect free: it runs nothing, changes no setting, and never puts a
        configuration VALUE in the payload (only "set" / "not set"). `fresh=1` only skips the few-second cache; any
        other parameter NAME is refused with a 400 that names it (lesson L-098), checked before the cache."""
        params = _query_lists(request)
        bad = health_unknown_params(params)
        if bad:
            first = bad[0]
            return JSONResponse({"ok": False, "error": f"Invalid parameter: {first['param']}", "problems": bad},
                                status_code=400, headers={"Cache-Control": "no-store"})
        fresh = (params.get("fresh") or ["0"])[-1] in ("1", "true")
        return JSONResponse(ctx.health.get(fresh=fresh), headers={"Cache-Control": "no-store"})

    @app.get("/api/leads/analysis")
    def leads_analysis(request: Request) -> JSONResponse:
        """Leads page: funnel, SLA outcomes, time to first reply, per rep / source, per day and a paged table for one set of
        server-side filters (read-only; every value is whitelisted and bound as a SQL parameter). `fresh=1` skips the cache."""
        params = _query_lists(request)
        code, payload = ctx.leads.analysis(params, fresh=(params.get("fresh") or ["0"])[-1] in ("1", "true"))
        return JSONResponse(payload, status_code=code, headers={"Cache-Control": "no-store"})

    @app.get("/api/leads/export.csv")
    def leads_export(request: Request) -> Response:
        """The leads behind the same filters as a CSV download (UTF-8 with BOM, formula-safe cells, no PII columns beyond the name)."""
        code, body, headers = ctx.leads.export_csv(_query_lists(request))
        if code != 200 or not isinstance(body, (bytes, bytearray)):
            return JSONResponse(body, status_code=code, headers={"Cache-Control": "no-store"})
        return Response(bytes(body), media_type="text/csv; charset=utf-8", headers={"Cache-Control": "no-store", **headers})

    @app.get("/api/leads/sources")
    def leads_sources(request: Request) -> JSONResponse:
        """Leads page, "Sources & cohorts" tab: one row per lead source (stages reached, SLA outcomes, median first reply,
        rep spread) and the arrival-cohort curves, for one set of server-side filters (read-only; every parameter NAME and
        value is whitelisted and bound as a SQL parameter). `fresh=1` skips the cache."""
        params = _query_lists(request)
        code, payload = ctx.sources.sources(params, fresh=(params.get("fresh") or ["0"])[-1] in ("1", "true"))
        return JSONResponse(payload, status_code=code, headers={"Cache-Control": "no-store"})

    @app.get("/api/leads/sources.csv")
    def leads_sources_export(request: Request) -> Response:
        """The same view as a CSV download (part=sources: one row per source, part=cohorts: one row per cohort and day).
        Aggregate rows only, so no per-lead column - and therefore no e-mail, phone or raw payload - can appear."""
        code, body, headers = ctx.sources.export_csv(_query_lists(request))
        if code != 200 or not isinstance(body, (bytes, bytearray)):
            return JSONResponse(body, status_code=code, headers={"Cache-Control": "no-store"})
        return Response(bytes(body), media_type="text/csv; charset=utf-8", headers={"Cache-Control": "no-store", **headers})

    @app.get("/api/channels")
    def channels(request: Request) -> JSONResponse:
        """Channels page (marketing rollup report): headline, KPI tiles, per-day chart, per-channel table and campaign
        drill-down for one set of server-side filters. Read-only; reads only the two rollup tables (desktop/channels_data.py).
        Every parameter NAME and value is whitelisted (unknown ones are a 400 that names the parameter, before the cache,
        lesson L-098) and bound as a SQL parameter. `fresh=1` skips the few-second cache."""
        params = _query_lists(request)
        code, payload = ctx.channels.get(params, fresh=(params.get("fresh") or ["0"])[-1] in ("1", "true"))
        return JSONResponse(payload, status_code=code, headers={"Cache-Control": "no-store"})

    @app.get("/api/channels.csv")
    def channels_export(request: Request) -> Response:
        """The same view as a CSV download (part=channels | daily | campaigns; UTF-8 with BOM, CRLF, formula-safe cells,
        aggregate rows only - no identity or personal column exists in the rollup)."""
        code, body, headers = ctx.channels.export_csv(_query_lists(request))
        if code != 200 or not isinstance(body, (bytes, bytearray)):
            return JSONResponse(body, status_code=code, headers={"Cache-Control": "no-store"})
        return Response(bytes(body), media_type="text/csv; charset=utf-8", headers={"Cache-Control": "no-store", **headers})

    @app.get("/api/channels/insights")
    def channels_insights(request: Request) -> JSONResponse:
        """Insights panel of the Channels page: severity-sorted findings from a fixed, explainable rule set, for the same filters as
        /api/channels (unknown parameter names are a 400 before the cache). Read-only; reads only the rollup tables."""
        params = _query_lists(request)
        code, payload = ctx.insights.get(params, fresh=(params.get("fresh") or ["0"])[-1] in ("1", "true"))
        return JSONResponse(payload, status_code=code, headers={"Cache-Control": "no-store"})

    @app.get("/api/channels/placements")
    def channels_placements(request: Request) -> JSONResponse:
        """Placements panel of the Channels page (display-network placements: a site, an app or a banner slot): a sortable table and totals per
        currency for the same filters as /api/channels plus psort / pdir. Unknown parameter names are a 400 before the cache. Read-only; reads only
        the placement rollup (desktop/placements_data.py), never the raw facts."""
        params = _query_lists(request)
        code, payload = ctx.placements.get(params, fresh=(params.get("fresh") or ["0"])[-1] in ("1", "true"))
        return JSONResponse(payload, status_code=code, headers={"Cache-Control": "no-store"})

    @app.get("/api/channels/placements.csv")
    def channels_placements_export(request: Request) -> Response:
        """The Placements table as a CSV download (UTF-8 with BOM, CRLF, formula-safe cells; every row up to the export cap)."""
        code, body, headers = ctx.placements.export_csv(_query_lists(request))
        if code != 200 or not isinstance(body, (bytes, bytearray)):
            return JSONResponse(body, status_code=code, headers={"Cache-Control": "no-store"})
        return Response(bytes(body), media_type="text/csv; charset=utf-8", headers={"Cache-Control": "no-store", **headers})

    @app.get("/api/channels/report.xlsx")
    def channels_report(request: Request) -> Response:
        """The same view as an Excel workbook (Summary, Channels, Campaigns, Insights, Definitions). Numeric cells, formula-safe text."""
        from starlette.responses import StreamingResponse
        code, body, headers = ctx.insights.export_xlsx(_query_lists(request))
        if code != 200 or not isinstance(body, (bytes, bytearray)):
            return JSONResponse(body, status_code=code, headers={"Cache-Control": "no-store"})
        data = bytes(body)

        def chunks():
            for i in range(0, len(data), 65536):
                yield data[i:i + 65536]
        return StreamingResponse(chunks(), media_type=XLSX_MIME, headers={"Cache-Control": "no-store", "Content-Length": str(len(data)), **headers})

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
