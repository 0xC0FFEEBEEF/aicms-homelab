from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.config import AppConfig
from app.models import EnrichmentEvidence, EnrichmentRun, EnrichmentSource, Project
from app.schemas import WEB_SEARCH_SETTINGS_KEY, WebSearchSettingsPayload
from app.utils.common import utcnow
from app.utils.settings_store import get_settings, set_settings
from app.websearch.client import WebSearchClient
from app.websearch.config import parse_domain_filters, render_request_preview


def _client(config: AppConfig, settings: WebSearchSettingsPayload) -> WebSearchClient:
    normalized = settings.model_copy(update={"domain_filters": parse_domain_filters(settings.domain_filters)})
    return WebSearchClient(normalized)


def test_web_search_connection(session: Session, config: AppConfig) -> str:
    settings = get_settings(session, config, WEB_SEARCH_SETTINGS_KEY, WebSearchSettingsPayload)
    client = _client(config, settings)
    try:
        preview = client.test_connection()
        settings.last_successful_connection = utcnow()
        settings.last_error = None
        set_settings(session, WEB_SEARCH_SETTINGS_KEY, settings)
        return preview
    except Exception as exc:
        settings.last_error = str(exc)
        set_settings(session, WEB_SEARCH_SETTINGS_KEY, settings)
        raise


def test_web_search_search(session: Session, config: AppConfig, *, query: str) -> dict[str, Any]:
    settings = get_settings(session, config, WEB_SEARCH_SETTINGS_KEY, WebSearchSettingsPayload)
    client = _client(config, settings)
    try:
        results = client.search(query)
        settings.last_successful_connection = utcnow()
        settings.last_error = None
        set_settings(session, WEB_SEARCH_SETTINGS_KEY, settings)
        return {
            "preview_url": render_request_preview(settings.query_url_template, query),
            "results": [result.model_dump(mode="json") for result in results],
        }
    except Exception as exc:
        settings.last_error = str(exc)
        set_settings(session, WEB_SEARCH_SETTINGS_KEY, settings)
        raise


def run_web_search_enrichment(
    session: Session,
    config: AppConfig,
    *,
    project: Project,
    profile_packet: dict[str, Any],
) -> dict[str, Any]:
    settings = get_settings(session, config, WEB_SEARCH_SETTINGS_KEY, WebSearchSettingsPayload)
    if not settings.enabled or settings.engine == "disabled":
        return {"enabled": False, "evidence": [], "summary": None}

    client = _client(config, settings)
    query_parts = [project.host.name, profile_packet.get("detected_platform"), profile_packet.get("detected_role")]
    query = " ".join(part for part in query_parts if part)

    run = EnrichmentRun(
        host_id=project.host_id,
        project_id=project.id,
        source_type="web_search",
        status="running",
        run_kind="profile_refresh",
        query_summary=query,
        used_in_profile=False,
    )
    session.add(run)
    session.flush()

    try:
        results = client.search(query)
        evidence_items: list[dict[str, Any]] = []
        for result in results[: settings.result_count]:
            snippet = client.load_result(result)
            source = EnrichmentSource(
                run_id=run.id,
                source_type="web_search",
                tool_name=settings.engine,
                query_summary=query,
                response_summary=(snippet or result.snippet or result.title)[:1000],
                relevance=result.relevance,
                used_in_profile=True,
                raw_json=result.raw,
            )
            session.add(source)
            session.flush()
            evidence_items.append(
                {
                    "source_id": source.id,
                    "title": result.title,
                    "evidence_text": snippet or result.snippet or result.title,
                    "source_url": result.url,
                    "relevance": result.relevance,
                    "raw_json": result.raw,
                }
            )

        for evidence in evidence_items:
            session.add(
                EnrichmentEvidence(
                    run_id=run.id,
                    source_id=evidence["source_id"],
                    host_id=project.host_id,
                    project_id=project.id,
                    source_type="web_search",
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
        run.response_summary = f"Collected {len(evidence_items)} web evidence item(s) for {query}."
        settings.last_successful_connection = utcnow()
        settings.last_error = None
        set_settings(session, WEB_SEARCH_SETTINGS_KEY, settings)
        session.flush()
        return {"enabled": True, "run_id": run.id, "evidence": evidence_items, "summary": run.response_summary}
    except Exception as exc:
        run.status = "error"
        run.finished_at = utcnow()
        run.response_summary = str(exc)
        settings.last_error = str(exc)
        set_settings(session, WEB_SEARCH_SETTINGS_KEY, settings)
        session.flush()
        return {"enabled": True, "run_id": run.id, "evidence": [], "summary": str(exc), "error": str(exc)}
