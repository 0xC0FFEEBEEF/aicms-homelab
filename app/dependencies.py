from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Iterator

from fastapi import Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import AppConfig
from app.schemas import GENERAL_SETTINGS_KEY, GeneralSettingsPayload
from app.utils.settings_store import get_settings


def get_config_from_request(request: Request) -> AppConfig:
    return request.app.state.config


def get_db(request: Request) -> Iterator[Session]:
    session_factory = request.app.state.session_factory
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


def get_templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


def flash(request: Request, level: str, message: str) -> None:
    flashes = request.session.setdefault("_flashes", [])
    flashes.append({"level": level, "message": message})
    request.session["_flashes"] = flashes


def pop_flashes(request: Request) -> list[dict[str, str]]:
    return request.session.pop("_flashes", [])


def mask_secret(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) <= 4:
        return "*" * len(value)
    return f"{value[:2]}{'*' * (len(value) - 4)}{value[-2:]}"


def format_datetime(value: datetime | None) -> str:
    if value is None:
        return "Never"
    return value.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def json_pretty(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, default=str)


def base_template_context(request: Request, session: Session, **extra: Any) -> dict[str, Any]:
    config = get_config_from_request(request)
    general_settings = get_settings(session, config, GENERAL_SETTINGS_KEY, GeneralSettingsPayload)
    context = {
        "request": request,
        "app_name": general_settings.app_name,
        "timezone": general_settings.timezone,
        "flashes": pop_flashes(request),
        "active_path": request.url.path,
    }
    context.update(extra)
    return context
