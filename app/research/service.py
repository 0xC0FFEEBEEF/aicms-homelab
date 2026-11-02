from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import AppConfig
from app.mcp.service import run_mcp_enrichment
from app.models import Project, ResearchFinding, ResearchRun, ResearchSource
from app.research.models import ResearchFindingPayload, ResearchOutcome
from app.websearch.service import run_web_search_enrichment
from app.utils.common import utcnow

GENERIC_SIGNATURE_IDS = {None, "generic_project_vm", "generic_service_node"}
GENERIC_TITLES = {
    "Generic Project VM",
    "Generic Service Node",
    "Generic Linux Node",
    "Fallback unknown profile",
}


@dataclass(frozen=True)
class _ResearchPattern:
    pattern_id: str
    display_name: str
    terms: tuple[str, ...]
    title_hint: str
    summary_hint: str
    long_description_hint: str
    detected_platform_hint: str
    detected_role_hint: str
    tech_tags: tuple[str, ...]


_PATTERNS: tuple[_ResearchPattern, ...] = (
    _ResearchPattern(
        pattern_id="golem_provider",
        display_name="Golem workload",
        terms=("golem", "yagna", "ya-provider", "ya-runtime", "golemsp"),
        title_hint="golem Workload VM",
        summary_hint="This VM is for my Golem project. It appears to run the provider-side components and runtime services tied to that workload.",
        long_description_hint=(
            "This VM appears to be the workload node for my Golem environment. "
            "Observed naming and service hints point to provider-side components such as yagna, ya-provider, or related runtime pieces."
        ),
        detected_platform_hint="Golem provider stack",
        detected_role_hint="distributed compute provider / workload VM",
        tech_tags=("Golem", "Yagna", "Provider"),
    ),
    _ResearchPattern(
        pattern_id="wazuh_stack",
        display_name="Wazuh stack",
        terms=("wazuh", "filebeat", "opensearch", "wazuh-manager", "wazuh-indexer", "wazuh-dashboard"),
        title_hint="Wazuh Security Monitoring VM",
        summary_hint="This VM is for my Wazuh monitoring and security stack.",
        long_description_hint=(
            "This machine appears to be part of my Wazuh security monitoring stack. "
            "Observed local evidence points to Wazuh, Filebeat, and related indexing or dashboard components."
        ),
        detected_platform_hint="Wazuh stack",
        detected_role_hint="security monitoring / SIEM node",
        tech_tags=("Wazuh", "Filebeat", "OpenSearch"),
    ),
    _ResearchPattern(
        pattern_id="mcp_gateway",
        display_name="MCP gateway",
        terms=("mcp", "gateway", "tool-access", "tooling"),
        title_hint="MCP Control Layer VM",
        summary_hint="This VM is the control layer for my AI environment.",
        long_description_hint=(
            "This machine appears to be the control and tool-access layer for my local AI environment. "
            "Observed naming suggests MCP or related gateway/tooling functions rather than a generic Linux VM."
        ),
        detected_platform_hint="MCP gateway",
        detected_role_hint="AI control / tool-access node",
        tech_tags=("MCP", "Gateway", "Tooling"),
    ),
    _ResearchPattern(
        pattern_id="grafana",
        display_name="Grafana",
        terms=("grafana", "grafana-server"),
        title_hint="Grafana Observability VM",
        summary_hint="This VM appears to host my Grafana-based dashboards or observability tooling.",
        long_description_hint=(
            "This machine appears to provide Grafana-oriented dashboards or observability functions in my environment."
        ),
        detected_platform_hint="Grafana",
        detected_role_hint="dashboard and observability node",
        tech_tags=("Grafana",),
    ),
)


def _normalize_text(items: Iterable[Any]) -> str:
    parts: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if text:
            parts.append(text.lower())
    return "\n".join(parts)


def _observed_text(project: Project, identity, collected: dict[str, Any]) -> str:
    metadata = project.metadata_json or {}
    inventory_summary = metadata.get("inventory_summary") or {}
    observed_names = inventory_summary.get("observed_names") or []
    service_names = [item.get("service_name") for item in collected.get("service_inventory") or []]
    role_guesses = [item.get("role_guess") for item in collected.get("guest_inventory") or []]
    project_paths = metadata.get("project_paths") or []
    grouped_roots = metadata.get("grouped_roots") or []
    marker_names = metadata.get("marker_names") or []
    stack_tags = identity.stack_tags if identity else []
    host_facts = collected.get("host_facts") or {}
    return _normalize_text(
        [
            project.host.name,
            project.host.address,
            *(observed_names or []),
            *(service_names or []),
            *(role_guesses or []),
            *(project_paths or []),
            *(grouped_roots or []),
            *(marker_names or []),
            *(stack_tags or []),
            host_facts.get("hostname"),
            host_facts.get("fqdn"),
            host_facts.get("os_pretty_name"),
        ]
    )


def _weak_title(title: str | None) -> bool:
    text = str(title or "").strip()
    if not text:
        return True
    if text in GENERIC_TITLES:
        return True
    lowered = text.lower()
    return lowered.startswith("generic ") or lowered.startswith("fallback ")


def _weak_summary(summary: str | None) -> bool:
    text = str(summary or "").strip().lower()
    if not text:
        return True
    return any(
        fragment in text
        for fragment in (
            "generic linux node",
            "generic service node",
            "generic project vm",
            "still needs a cleaner public-facing summary",
            "part of my lab as",
        )
    )


def should_run_identity_research(
    *,
    identity,
    title_candidate: str | None = None,
    summary_candidate: str | None = None,
    force: bool = False,
) -> tuple[bool, str]:
    if force:
        return True, "Manual research refresh requested."
    if identity is None:
        return True, "No identity result exists yet."
    if identity.primary_signature_id in GENERIC_SIGNATURE_IDS:
        return True, "Identity is generic or unresolved."
    if (identity.identity_confidence or 0.0) < 0.78:
        return True, "Identity confidence is below the research threshold."
    if _weak_title(title_candidate):
        return True, "Title candidate is weak or generic."
    if _weak_summary(summary_candidate):
        return True, "Summary candidate is weak or generic."
    return False, "Identity and public copy are already specific enough."


def _pattern_finding(
    pattern: _ResearchPattern,
    *,
    source_type: str,
    observed_text: str,
    query_used: str | None,
    response_summary: str | None = None,
) -> ResearchFindingPayload | None:
    hits = [term for term in pattern.terms if term in observed_text]
    if not hits:
        return None
    confidence = min(0.58 + (0.1 * len(hits)), 0.92)
    summary = response_summary or f"{pattern.display_name} indicators matched local or enriched evidence."
    return ResearchFindingPayload(
        source_type=source_type,
        finding_kind=pattern.pattern_id,
        result_summary=summary,
        confidence=confidence,
        relevance=confidence,
        evidence=[f"Matched indicator: {term}" for term in hits[:6]],
        query_used=query_used,
        title_hint=pattern.title_hint,
        summary_hint=pattern.summary_hint,
        long_description_hint=pattern.long_description_hint,
        detected_platform_hint=pattern.detected_platform_hint,
        detected_role_hint=pattern.detected_role_hint,
        tech_tags=list(pattern.tech_tags),
        affected_identity=True,
        affected_title=True,
        affected_summary=True,
        affected_tags=True,
        used_in_generation=True,
        raw_payload={"matched_terms": hits, "pattern_id": pattern.pattern_id},
    )


def _local_findings(project: Project, identity, collected: dict[str, Any]) -> list[ResearchFindingPayload]:
    observed_text = _observed_text(project, identity, collected)
    findings: list[ResearchFindingPayload] = []
    for pattern in _PATTERNS:
        finding = _pattern_finding(pattern, source_type="local", observed_text=observed_text, query_used=None)
        if finding is not None:
            findings.append(finding)
    findings.sort(key=lambda item: item.confidence, reverse=True)
    return findings


def _aggregate_outcome(
    findings: list[ResearchFindingPayload],
    *,
    triggered: bool,
    reason: str,
    mcp_enrichment: dict[str, Any] | None = None,
    web_search_enrichment: dict[str, Any] | None = None,
) -> ResearchOutcome:
    if not findings:
        return ResearchOutcome(
            triggered=triggered,
            reason=reason,
            findings=[],
            mcp_enrichment=mcp_enrichment,
            web_search_enrichment=web_search_enrichment,
        )
    best = max(findings, key=lambda item: item.confidence)
    tags: list[str] = []
    for finding in findings:
        for tag in finding.tech_tags:
            if tag not in tags:
                tags.append(tag)
    return ResearchOutcome(
        triggered=triggered,
        reason=reason,
        findings=findings,
        title_hint=best.title_hint,
        summary_hint=best.summary_hint,
        long_description_hint=best.long_description_hint,
        detected_platform_hint=best.detected_platform_hint,
        detected_role_hint=best.detected_role_hint,
        tech_tags=tags,
        affected_identity=any(item.affected_identity for item in findings),
        affected_title=any(item.affected_title for item in findings),
        affected_summary=any(item.affected_summary for item in findings),
        mcp_enrichment=mcp_enrichment,
        web_search_enrichment=web_search_enrichment,
    )


def _persist_research_outcome(
    session: Session,
    *,
    project: Project,
    reason: str,
    outcome: ResearchOutcome,
) -> ResearchRun:
    run = ResearchRun(
        host_id=project.host.id if project.host else None,
        project_id=project.id,
        status="completed",
        run_kind="identity_enrichment",
        trigger_reason=reason,
        query_summary=reason,
        result_summary=outcome.summary_hint or outcome.long_description_hint or outcome.reason,
        affected_identity=outcome.affected_identity,
        affected_title=outcome.affected_title,
        affected_summary=outcome.affected_summary,
        started_at=utcnow(),
        finished_at=utcnow(),
    )
    session.add(run)
    session.flush()

    source_rows: dict[tuple[str, str | None], ResearchSource] = {}
    for finding in outcome.findings:
        key = (finding.source_type, finding.query_used)
        source = source_rows.get(key)
        if source is None:
            source = ResearchSource(
                run_id=run.id,
                source_type=finding.source_type,
                tool_name=finding.source_type,
                query_summary=finding.query_used,
                response_summary=finding.result_summary,
                relevance=finding.relevance,
                used_in_generation=finding.used_in_generation,
                raw_json=finding.raw_payload,
            )
            session.add(source)
            session.flush()
            source_rows[key] = source
        session.add(
            ResearchFinding(
                run_id=run.id,
                source_id=source.id,
                host_id=project.host.id if project.host else None,
                project_id=project.id,
                source_type=finding.source_type,
                query_used=finding.query_used,
                finding_kind=finding.finding_kind,
                result_summary=finding.result_summary,
                confidence=finding.confidence,
                relevance=finding.relevance,
                affected_identity=finding.affected_identity,
                affected_title=finding.affected_title,
                affected_summary=finding.affected_summary,
                affected_tags=finding.affected_tags,
                used_in_generation=finding.used_in_generation,
                evidence_json=finding.evidence,
                raw_json={
                    "title_hint": finding.title_hint,
                    "summary_hint": finding.summary_hint,
                    "long_description_hint": finding.long_description_hint,
                    "detected_platform_hint": finding.detected_platform_hint,
                    "detected_role_hint": finding.detected_role_hint,
                    "tech_tags": finding.tech_tags,
                    "raw_payload": finding.raw_payload,
                },
            )
        )
    session.flush()
    return run


def _external_findings(
    *,
    source_type: str,
    payload: dict[str, Any] | None,
) -> list[ResearchFindingPayload]:
    if not payload or not payload.get("enabled"):
        return []
    summary_text = _normalize_text(
        [
            payload.get("summary"),
            *(
                item.get("evidence_text") or item.get("title") or ""
                for item in payload.get("evidence") or []
            ),
        ]
    )
    findings: list[ResearchFindingPayload] = []
    for pattern in _PATTERNS:
        finding = _pattern_finding(
            pattern,
            source_type=source_type,
            observed_text=summary_text,
            query_used=payload.get("summary"),
            response_summary=f"Bounded {source_type} research matched {pattern.display_name} indicators.",
        )
        if finding is not None:
            findings.append(finding)
    findings.sort(key=lambda item: item.confidence, reverse=True)
    return findings


def run_identity_research(
    session: Session,
    config: AppConfig,
    *,
    project: Project,
    identity,
    collected: dict[str, Any],
    base_profile_packet: dict[str, Any],
    title_candidate: str | None = None,
    summary_candidate: str | None = None,
    include_external: bool = True,
    force: bool = False,
    stream_callback: Callable[[str], None] | None = None,
) -> ResearchOutcome:
    should_run, reason = should_run_identity_research(
        identity=identity,
        title_candidate=title_candidate,
        summary_candidate=summary_candidate,
        force=force,
    )
    if not should_run:
        return ResearchOutcome(triggered=False, reason=reason)

    if stream_callback:
        stream_callback(f"[AICMS] Running bounded research: {reason}\n")

    local_findings = _local_findings(project, identity, collected)
    mcp_enrichment: dict[str, Any] | None = None
    web_search_enrichment: dict[str, Any] | None = None
    external_findings: list[ResearchFindingPayload] = []
    if include_external:
        mcp_enrichment = run_mcp_enrichment(session, config, project=project, profile_packet=base_profile_packet)
        web_search_enrichment = run_web_search_enrichment(session, config, project=project, profile_packet=base_profile_packet)
        external_findings.extend(_external_findings(source_type="mcp", payload=mcp_enrichment))
        external_findings.extend(_external_findings(source_type="web_search", payload=web_search_enrichment))

    findings = sorted(local_findings + external_findings, key=lambda item: item.confidence, reverse=True)
    outcome = _aggregate_outcome(
        findings,
        triggered=True,
        reason=reason,
        mcp_enrichment=mcp_enrichment,
        web_search_enrichment=web_search_enrichment,
    )
    _persist_research_outcome(session, project=project, reason=reason, outcome=outcome)
    return outcome


def latest_research_outcome(session: Session, project: Project) -> ResearchOutcome:
    latest_run = session.execute(
        select(ResearchRun)
        .where(ResearchRun.project_id == project.id)
        .options(selectinload(ResearchRun.findings))
        .order_by(ResearchRun.started_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if latest_run is None:
        return ResearchOutcome(triggered=False, reason="No research has run yet.")

    findings: list[ResearchFindingPayload] = []
    for row in latest_run.findings:
        payload = row.raw_json or {}
        findings.append(
            ResearchFindingPayload(
                source_type=row.source_type,
                finding_kind=row.finding_kind,
                result_summary=row.result_summary,
                confidence=row.confidence or 0.0,
                relevance=row.relevance or 0.0,
                evidence=payload.get("evidence") or row.evidence_json or [],
                query_used=row.query_used,
                title_hint=payload.get("title_hint"),
                summary_hint=payload.get("summary_hint"),
                long_description_hint=payload.get("long_description_hint"),
                detected_platform_hint=payload.get("detected_platform_hint"),
                detected_role_hint=payload.get("detected_role_hint"),
                tech_tags=payload.get("tech_tags") or [],
                affected_identity=row.affected_identity,
                affected_title=row.affected_title,
                affected_summary=row.affected_summary,
                affected_tags=row.affected_tags,
                used_in_generation=row.used_in_generation,
                raw_payload=payload.get("raw_payload"),
            )
        )
    findings.sort(key=lambda item: item.confidence, reverse=True)
    return _aggregate_outcome(findings, triggered=True, reason=latest_run.trigger_reason or "Loaded latest research.")
