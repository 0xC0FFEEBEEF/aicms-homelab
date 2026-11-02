from __future__ import annotations

from app.schemas import (
    GENERAL_SETTINGS_KEY,
    GITHUB_SETTINGS_KEY,
    LMSTUDIO_SETTINGS_KEY,
    MCP_SETTINGS_KEY,
    WEB_SEARCH_SETTINGS_KEY,
    GeneralSettingsPayload,
    GitHubSettingsPayload,
    LMStudioSettingsPayload,
    MCPSettingsPayload,
    WebSearchSettingsPayload,
)
from app.utils.settings_store import get_settings
from app.websearch.config import parse_domain_filters


def test_settings_seeded(session, app):
    general = get_settings(session, app.state.config, GENERAL_SETTINGS_KEY, GeneralSettingsPayload)
    assert general.app_name == "AICMS Test"
    lmstudio = get_settings(session, app.state.config, LMSTUDIO_SETTINGS_KEY, LMStudioSettingsPayload)
    assert lmstudio.use_max_output_tokens is True


def test_general_and_github_settings_persist(client, session, app):
    response = client.post(
        "/api/settings/general",
        data={
            "app_name": "Inventory HQ",
            "timezone": "UTC",
            "default_max_tree_depth": 3,
            "default_scanner_ssh_key_path": "~/.ssh/inventory-key",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200

    general = get_settings(session, app.state.config, GENERAL_SETTINGS_KEY, GeneralSettingsPayload)
    assert general.app_name == "Inventory HQ"
    assert general.timezone == "UTC"
    assert general.default_max_tree_depth == 3
    assert general.default_scanner_ssh_key_path == "~/.ssh/inventory-key"

    response = client.post(
        "/api/settings/github",
        data={"repo_owner": "octo", "repo_name": "aicms", "branch": "main", "pat": "token-123"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    github = get_settings(session, app.state.config, GITHUB_SETTINGS_KEY, GitHubSettingsPayload)
    assert github.repo_owner == "octo"
    assert github.repo_name == "aicms"
    assert github.pat == "token-123"

    response = client.post(
        "/api/settings/lmstudio",
        data={
            "enabled": "on",
            "base_url": "http://localhost:1234/v1",
            "api_token": "",
            "selected_model": "",
            "temperature": 0.2,
            "use_max_output_tokens": "on",
            "max_tokens": 500,
            "use_custom_timeout": "on",
            "request_timeout_seconds": 180,
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    lmstudio = get_settings(session, app.state.config, LMSTUDIO_SETTINGS_KEY, LMStudioSettingsPayload)
    assert lmstudio.use_max_output_tokens is True
    assert lmstudio.use_custom_timeout is True
    assert lmstudio.request_timeout_seconds == 180


def test_mcp_and_web_search_settings_persist(client, session, app):
    response = client.post(
        "/api/settings/mcp",
        data={
            "enabled": "on",
            "server_url": "http://mcp.internal/rpc",
            "api_token": "mcp-token",
            "allowed_tools": "search*\ndocs.read",
            "read_only_search_only": "on",
            "auto_enrich_after_scan": "on",
            "max_searches_per_profile_refresh": 4,
            "timeout_seconds": 20,
            "max_results_items": 12,
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    mcp = get_settings(session, app.state.config, MCP_SETTINGS_KEY, MCPSettingsPayload)
    assert mcp.enabled is True
    assert mcp.server_url == "http://mcp.internal/rpc"
    assert mcp.allowed_tools == ["search*", "docs.read"]
    assert mcp.auto_enrich_after_scan is True

    response = client.post(
        "/api/settings/web-search",
        data={
            "enabled": "on",
            "engine": "searxng",
            "query_url_template": "http://10.20.2.8:8080/search?q=<query>",
            "language": "all",
            "result_count": 10,
            "concurrent_requests": 8,
            "domain_filters": "example.com\ndocs.example.org",
            "bypass_embedding_and_retrieval": "",
            "bypass_web_loader": "on",
            "trust_proxy_environment": "",
            "loader_engine": "default",
            "timeout": 15,
            "verify_ssl_certificate": "on",
            "loader_concurrent_requests": 6,
            "youtube_language": "en",
            "youtube_proxy_url": "",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    web_search = get_settings(session, app.state.config, WEB_SEARCH_SETTINGS_KEY, WebSearchSettingsPayload)
    assert web_search.enabled is True
    assert web_search.query_url_template == "http://10.20.2.8:8080/search?q=<query>"
    assert web_search.domain_filters == ["example.com", "docs.example.org"]
    assert web_search.bypass_web_loader is True


def test_web_search_query_template_requires_query_placeholder():
    try:
        WebSearchSettingsPayload(enabled=True, engine="searxng", query_url_template="http://10.20.2.8:8080/search?q=test")
    except Exception as exc:
        assert "<query>" in str(exc)
    else:
        raise AssertionError("Expected validation error for missing <query> placeholder")


def test_parse_domain_filters_normalizes_values():
    assert parse_domain_filters(["https://example.com/", "docs.example.org", "example.com"]) == [
        "example.com",
        "docs.example.org",
    ]
