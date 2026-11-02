from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock

from fastapi import FastAPI
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.config import AppConfig, get_config
from app.db import init_database, session_scope
from app.dependencies import format_datetime, json_pretty
from app.jobs.runner import JobRunner
from app.utils.settings_store import ensure_default_settings
from app.web.api import router as api_router
from app.web.routes import router as web_router


def create_app(config: AppConfig | None = None) -> FastAPI:
    config = config or get_config()
    session_factory = init_database(config)
    with session_scope(session_factory) as session:
        ensure_default_settings(session, config)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        app.state.job_runner.shutdown()

    app = FastAPI(title=config.app_name, lifespan=lifespan)
    app.add_middleware(SessionMiddleware, secret_key=config.secret_key)

    templates = Jinja2Templates(directory=str(config.templates_dir))
    templates.env.filters["dt"] = format_datetime
    templates.env.filters["json_pretty"] = json_pretty

    app.state.config = config
    app.state.session_factory = session_factory
    app.state.job_runner = JobRunner(sync=config.sync_jobs)
    app.state.templates = templates
    app.state.repo_path = Path(__file__).resolve().parent.parent
    app.state.scan_controls = {}
    app.state.scan_lock = Lock()
    app.state.profile_runs = {}
    app.state.profile_controls = {}
    app.state.profile_lock = Lock()
    app.state.interpretation_runs = app.state.profile_runs
    app.state.interpretation_controls = app.state.profile_controls
    app.state.interpretation_lock = app.state.profile_lock
    app.state.investigation_runs = {}
    app.state.investigation_controls = {}
    app.state.investigation_lock = Lock()

    app.mount("/static", StaticFiles(directory=str(config.static_dir)), name="static")
    app.include_router(web_router)
    app.include_router(api_router)

    @app.get("/")
    def root_redirect():
        return RedirectResponse("/web", status_code=302)

    @app.get("/health")
    def health():
        return JSONResponse({"status": "ok", "app": "AICMS"})

    return app


app = create_app()
