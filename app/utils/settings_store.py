from __future__ import annotations

from typing import Any, TypeVar

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import AppConfig
from app.models import AppSetting
from app.redaction.rules import DEFAULT_IGNORE_PATTERNS
from app.schemas import (
    GENERAL_SETTINGS_KEY,
    GITHUB_SETTINGS_KEY,
    LMSTUDIO_SETTINGS_KEY,
    MCP_SETTINGS_KEY,
    PROFILE_SETTINGS_KEY,
    SCAN_SETTINGS_KEY,
    WEB_SEARCH_SETTINGS_KEY,
)


SettingsModel = TypeVar("SettingsModel", bound=BaseModel)


def build_default_settings(config: AppConfig) -> dict[str, dict[str, Any]]:
    return {
        GENERAL_SETTINGS_KEY: {
            "app_name": config.app_name,
            "timezone": config.timezone,
            "default_max_tree_depth": 2,
            "default_scanner_ssh_key_path": "~/.ssh/aicms_scanner_ed25519",
        },
        SCAN_SETTINGS_KEY: {
            "ignore_patterns": DEFAULT_IGNORE_PATTERNS,
            "allowed_marker_files": [
                ".git",
                "README.md",
                "Dockerfile",
                "docker-compose.yml",
                "compose.yaml",
                "package.json",
                "pyproject.toml",
                "requirements.txt",
                ".csproj",
                ".sln",
                "go.mod",
                "Cargo.toml",
            ],
            "max_read_bytes": 4096,
            "collect_shallow_file_trees": True,
            "collect_recent_git_metadata": True,
        },
        LMSTUDIO_SETTINGS_KEY: {
            "enabled": False,
            "base_url": config.default_lmstudio_base_url.rstrip("/"),
            "api_token": None,
            "selected_model": None,
            "temperature": 0.2,
            "use_max_output_tokens": True,
            "max_tokens": 500,
            "use_custom_timeout": False,
            "request_timeout_seconds": config.lmstudio_timeout_seconds,
            "last_successful_contact": None,
            "last_error": None,
            "last_refresh_time": None,
        },
        GITHUB_SETTINGS_KEY: {
            "repo_owner": None,
            "repo_name": None,
            "branch": "main",
            "pat": None,
            "last_checked_at": None,
            "local_revision": None,
            "remote_revision": None,
            "update_available": False,
            "last_error": None,
        },
        PROFILE_SETTINGS_KEY: {
            "enabled": True,
            "enable_lmstudio_formatting": True,
            "fallback_to_deterministic_profile": True,
            "max_guests_shown": 24,
            "max_services_shown": 20,
            "max_setup_bullets_shown": 10,
        },
        MCP_SETTINGS_KEY: {
            "enabled": False,
            "server_url": None,
            "api_token": None,
            "allowed_tools": [],
            "read_only_search_only": True,
            "auto_enrich_after_scan": False,
            "max_searches_per_profile_refresh": 3,
            "timeout_seconds": 15,
            "max_results_items": 10,
            "last_successful_contact": None,
            "last_error": None,
        },
        WEB_SEARCH_SETTINGS_KEY: {
            "enabled": False,
            "engine": "searxng",
            "query_url_template": "http://10.20.2.8:8080/search?q=<query>",
            "language": "all",
            "result_count": 10,
            "concurrent_requests": 10,
            "domain_filters": [],
            "bypass_embedding_and_retrieval": False,
            "bypass_web_loader": False,
            "trust_proxy_environment": False,
            "loader_engine": "default",
            "timeout": 15,
            "verify_ssl_certificate": True,
            "loader_concurrent_requests": 10,
            "youtube_language": "en",
            "youtube_proxy_url": None,
            "last_successful_connection": None,
            "last_error": None,
        },
    }


def ensure_default_settings(session: Session, config: AppConfig) -> None:
    defaults = build_default_settings(config)
    existing = {
        row.key: row
        for row in session.execute(select(AppSetting)).scalars().all()
    }

    dirty = False
    for key, payload in defaults.items():
        if key not in existing:
            session.add(AppSetting(key=key, value_json=payload))
            dirty = True

    if dirty:
        session.commit()


def _merged_setting_payload(session: Session, config: AppConfig, key: str) -> dict[str, Any]:
    defaults = build_default_settings(config)[key]
    record = session.execute(select(AppSetting).where(AppSetting.key == key)).scalar_one_or_none()
    if not record:
        return defaults
    payload = dict(defaults)
    payload.update(record.value_json or {})
    return payload


def get_settings(session: Session, config: AppConfig, key: str, schema: type[SettingsModel]) -> SettingsModel:
    payload = _merged_setting_payload(session, config, key)
    return schema.model_validate(payload)


def set_settings(session: Session, key: str, payload: BaseModel) -> AppSetting:
    record = session.execute(select(AppSetting).where(AppSetting.key == key)).scalar_one_or_none()
    value_json = payload.model_dump(mode="json")

    if record is None:
        record = AppSetting(key=key, value_json=value_json)
        session.add(record)
    else:
        record.value_json = value_json

    session.commit()
    session.refresh(record)
    return record


def get_all_settings(session: Session, config: AppConfig) -> dict[str, dict[str, Any]]:
    defaults = build_default_settings(config)
    response: dict[str, dict[str, Any]] = {}
    for key in defaults:
        response[key] = _merged_setting_payload(session, config, key)
    return response
