from __future__ import annotations

import secrets
from dataclasses import replace
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import text

from ..config import get_settings
from ..db import Base, make_engine, make_session_factory
from .. import models  # noqa: F401  (register tables on Base.metadata)
from .routes import router
from .evals_routes import router as evals_router
from .drift_routes import router as drift_router
from .regress_routes import router as regress_router
from .rootcause_routes import router as rootcause_router
from .canary_routes import router as canary_router
from .release_routes import router as release_router
from .otlp_routes import router as otlp_router
from .assistant_routes import router as assistant_router
from .fixes_routes import router as fixes_router
from .insight_routes import feedback_router, router as insight_router
from ..assistant import RateLimiter

_DASH_DIR = Path(__file__).resolve().parent.parent / "dashboard"


def _dashboard_page() -> str:
    """The page is stored as a shell, one stylesheet and several script parts (kept apart so each is easy to edit); join them here."""
    shell = (_DASH_DIR / "index.html").read_text(encoding="utf-8")
    css = (_DASH_DIR / "app.css").read_text(encoding="utf-8")
    js = "\n".join(p.read_text(encoding="utf-8") for p in sorted((_DASH_DIR / "js").glob("*.js")))
    return shell.replace("/*__CSS__*/", css).replace("/*__JS__*/", js)


def create_app(database_url: str | None = None, api_key: str | None = None,
               anthropic_api_key: str | None = None) -> FastAPI:
    settings = get_settings()
    if database_url is not None:
        settings = replace(settings, database_url=database_url)
    if api_key is not None:
        settings = replace(settings, api_key=api_key)

    if anthropic_api_key is not None:
        settings = replace(settings, anthropic_api_key=anthropic_api_key)

    engine = make_engine(settings.database_url)
    if settings.auto_create_tables:  # with Alembic (Docker Compose) set AUDITOR_AUTO_CREATE_TABLES=false
        Base.metadata.create_all(engine)

    app = FastAPI(title="Agent Auditor", version="0.13.0")
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    app.state.assistant_transport = None  # tests inject an httpx transport here
    app.state.rate_limiter = RateLimiter()
    app.state.assistant_dead = {}  # AI services that failed recently, skipped for a few minutes

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse(url="/docs")

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict:
        """Liveness: the process is up. No database access, no authentication."""
        return {"status": "ok", "version": app.version}

    @app.get("/readyz", include_in_schema=False)
    def readyz():
        """Readiness: the database answers. Used by Docker Compose and load balancers."""
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception:  # noqa: BLE001 - report not-ready instead of crashing
            return JSONResponse({"status": "database unavailable"}, status_code=503)
        return {"status": "ok"}

    @app.get("/dashboard", include_in_schema=False)
    def dashboard() -> HTMLResponse:
        """Read-only dashboard. The page itself is public; every data call it makes
        carries the API key the person types in, so it exposes nothing by itself."""
        nonce = secrets.token_urlsafe(16)
        page = _dashboard_page().replace("__NONCE__", nonce)
        csp = (
            "default-src 'none'; "
            f"script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
            "connect-src 'self'; img-src 'self' data:; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
        )
        return HTMLResponse(page, headers={
            "Content-Security-Policy": csp,
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        })

    app.include_router(router)
    app.include_router(otlp_router)
    app.include_router(evals_router)
    app.include_router(drift_router)
    app.include_router(regress_router)
    app.include_router(rootcause_router)
    app.include_router(canary_router)
    app.include_router(release_router)
    app.include_router(assistant_router)
    app.include_router(fixes_router)
    app.include_router(insight_router)
    app.include_router(feedback_router)
    return app
