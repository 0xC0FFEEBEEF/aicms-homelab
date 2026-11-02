from __future__ import annotations

import fnmatch
import json
from typing import Any

from sqlalchemy.orm import Session

from app.config import AppConfig
from app.mcp.client import MCPClient
from app.models import EnrichmentEvidence, EnrichmentRun, EnrichmentSource, Project
from app.schemas import MCP_SETTINGS_KEY, MCPSettingsPayload
from app.utils.common import utcnow
from app.utils.settings_store import get_settings, set_settings

SAFE_TOOL_TOKENS = ("search", "query", "read", "get", "list", "fetch")
UNSAFE_TOOL_TOKENS = ("write", "create", "delete", "update", "patch", "shell", "exec", "run", "command")


def _client(config: AppConfig, settings: MCPSettingsPayload) -> MCPClient:
    return MCPClient(base_url=settings.server_url or "", timeout=settings.timeout_seconds, api_token=settings.api_token)


def _tool_allowed(tool_name: str, settings: MCPSettingsPayload) -> bool:
    lowered = tool_name.lower()
    if settings.allowed_tools:
        if not any(fnmatch.fnmatch(tool_name, pattern) or fnmatch.fnmatch(lowered, pattern.lower()) for pattern in settings.allowed_tools):
            return False
    if settings.read_only_search_only:
        if any(token in lowered for token in UNSAFE_TOOL_TOKENS):
            return False
        if not any(token in lowered for token in SAFE_TOOL_TOKENS):
            return False
    return True


def _pick_arguments(tool: dict[str, Any], query: str, settings: MCPSettingsPayload) -> dict[str, Any] | None:
    schema = tool.get("inputSchema") or tool.get("input_schema") or {}
    properties = schema.get("properties") or {}
    arguments: dict[str, Any] = {}
    if "query" in properties:
        arguments["query"] = query
    elif "q" in properties:
        arguments["q"] = query
    elif "search" in properties:
        arguments["search"] = query
    else:
        return None

    if "limit" in properties:
        arguments["limit"] = settings.max_results_items
    elif "count" in properties:
        arguments["count"] = settings.max_results_items
    elif "max_results" in properties:
        arguments["max_results"] = settings.max_results_items
    return arguments


def _summarize_payload(payload: Any, *, limit: int = 800) -> str:
    if isinstance(payload, str):
        return payload[:limit]
    try:
        return json.dumps(payload, sort_keys=True, default=str)[:limit]
    except TypeError:
        return str(payload)[:limit]


def _extract_evidence(payload: Any, *, source_type: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        candidates = payload.get("results") or payload.get("items") or payload.get("content") or payload.get("documents")
        if isinstance(candidates, list):
            for item in candidates[:10]:
                if isinstance(item, dict):
                    items.append(
                        {
                            "title": item.get("title") or item.get("name"),
                            "evidence_text": item.get("content") or item.get("text") or item.get("snippet") or _summarize_payload(item),
                            "source_url": item.get("url") or item.get("href"),
                            "relevance": item.get("score") or item.get("relevance"),
                            "source_type": source_type,
                            "raw_json": item,
                        }
                    )
        elif payload:
            items.append(
                {
                    "title": None,
                    "evidence_text": _summarize_payload(payload),
                    "source_url": None,
                    "relevance": None,
                    "source_type": source_type,
                    "raw_json": payload if isinstance(payload, dict) else None,
                }
            )
    elif isinstance(payload, list):
        for item in payload[:10]:
            items.append(
                {
                    "title": None,
                    "evidence_text": _summarize_payload(item),
                    "source_url": None,
                    "relevance": None,
                    "source_type": source_type,
                    "raw_json": item if isinstance(item, dict) else None,
                }
            )
    elif payload:
        items.append(
            {
                "title": None,
                "evidence_text": _summarize_payload(payload),
                "source_url": None,
                "relevance": None,
                "source_type": source_type,
                "raw_json": None,
            }
        )
    return items


def test_mcp_connection(session: Session, config: AppConfig) -> tuple[str, list[dict[str, Any]]]:
    settings = get_settings(session, config, MCP_SETTINGS_KEY, MCPSettingsPayload)
    client = _client(config, settings)
    try:
        info = client.initialize()
        tools = client.list_tools()
        settings.last_successful_contact = utcnow()
        settings.last_error = None
        set_settings(session, MCP_SETTINGS_KEY, settings)
        return _summarize_payload(info), tools
    except Exception as exc:
        settings.last_error = str(exc)
        set_settings(session, MCP_SETTINGS_KEY, settings)
        raise


def run_mcp_enrichment(
    session: Session,
    config: AppConfig,
    *,
    project: Project,
    profile_packet: dict[str, Any],
) -> dict[str, Any]:
    settings = get_settings(session, config, MCP_SETTINGS_KEY, MCPSettingsPayload)
    if not settings.enabled or not settings.server_url:
        return {"enabled": False, "evidence": [], "summary": None}

    client = _client(config, settings)
    run = EnrichmentRun(
        host_id=project.host_id,
        project_id=project.id,
        source_type="mcp",
        status="running",
        run_kind="profile_refresh",
        query_summary=f"MCP enrichment for {project.host.name}",
        used_in_profile=False,
    )
    session.add(run)
    session.flush()

    try:
        client.initialize()
        tools = [tool for tool in client.list_tools() if _tool_allowed(str(tool.get("name") or ""), settings)]
        query_parts = [project.host.name, profile_packet.get("detected_platform"), profile_packet.get("detected_role")]
        base_query = " ".join(part for part in query_parts if part)
        evidence_items: list[dict[str, Any]] = []
        source_summaries: list[str] = []

        for tool in tools[: settings.max_searches_per_profile_refresh]:
            tool_name = str(tool.get("name") or "")
            arguments = _pick_arguments(tool, base_query, settings)
            if arguments is None:
                continue
            result = client.call_tool(tool_name, arguments)
            source = EnrichmentSource(
                run_id=run.id,
                source_type="mcp",
                tool_name=tool_name,
                query_summary=base_query,
                response_summary=_summarize_payload(result),
                relevance=None,
                used_in_profile=True,
                raw_json=result if isinstance(result, dict) else None,
            )
            session.add(source)
            session.flush()
            source_summaries.append(f"{tool_name}: {_summarize_payload(result, limit=240)}")
            for evidence in _extract_evidence(result, source_type="mcp")[: settings.max_results_items]:
                evidence_items.append(evidence | {"source_id": source.id})

        for evidence in evidence_items[: settings.max_results_items]:
            session.add(
                EnrichmentEvidence(
                    run_id=run.id,
                    source_id=evidence.get("source_id"),
                    host_id=project.host_id,
                    project_id=project.id,
                    source_type="mcp",
                    title=evidence.get("title"),
                    evidence_text=evidence["evidence_text"],
                    source_url=evidence.get("source_url"),
                    relevance=evidence.get("relevance"),
                    used_in_profile=True,
                    raw_json=evidence.get("raw_json"),
                )
            )

        run.status = "completed"
        run.finished_at = utcnow()
        run.used_in_profile = bool(evidence_items)
        run.response_summary = "\n".join(source_summaries)[:4000] if source_summaries else "No usable MCP evidence."
        settings.last_successful_contact = utcnow()
        settings.last_error = None
        set_settings(session, MCP_SETTINGS_KEY, settings)
        session.flush()
        return {
            "enabled": True,
            "run_id": run.id,
            "evidence": evidence_items[: settings.max_results_items],
            "summary": run.response_summary,
        }
    except Exception as exc:
        run.status = "error"
        run.finished_at = utcnow()
        run.response_summary = str(exc)
        settings.last_error = str(exc)
        set_settings(session, MCP_SETTINGS_KEY, settings)
        session.flush()
        return {"enabled": True, "run_id": run.id, "evidence": [], "summary": str(exc), "error": str(exc)}
