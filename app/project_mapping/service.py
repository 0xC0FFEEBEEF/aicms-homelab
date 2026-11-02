from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from sqlalchemy import select

from app.models import (
    Host,
    HostProfileOverride,
    MachineIdentityResult,
    MachineProfile,
    Project,
    ProjectClarificationGuidance,
    ProjectMappingPreview,
)
from app.project_mapping.copy_builders import build_public_copy
from app.project_mapping.models import HostProfileOverridePayload, ProjectMappingPreviewPayload
from app.research.models import ResearchOutcome
from app.research.service import latest_research_outcome
from app.utils.common import utcnow


def _repo_candidate(project: Project) -> str | None:
    remote = (project.repo_remote or "").strip()
    if remote.startswith(("http://", "https://")):
        return remote
    return None


def _publishability_defaults(identity: MachineIdentityResult | None) -> tuple[str, str]:
    if identity is None:
        return "internal_only", "Low-confidence or unresolved machine identities default to internal-only."
    match identity.primary_signature_id:
        case "mcp_gateway" | "openwebui":
            return "publishable", "This machine resolves to an identifiable project or service that could become a later CMS draft."
        case "wazuh_stack" | "proxmox_hypervisor" | "proxmox_backup_server" | "docker_host":
            return "internal_only", "Infrastructure-heavy machines default to internal-only until explicitly reviewed."
        case _:
            if (identity.identity_confidence or 0) < 0.72:
                return "needs_review", "Identity confidence is not strong enough to mark this ready for publication."
            return "needs_review", "Signature defaults remain conservative until manual intent is set."


def persist_host_profile_override(session: Session, host: Host, payload: HostProfileOverridePayload) -> HostProfileOverride:
    record = host.profile_override
    if record is None:
        record = HostProfileOverride(host_id=host.id)
        session.add(record)

    record.project_label_override = payload.project_label_override
    record.public_title_override = payload.public_title_override
    record.public_summary_hint = payload.public_summary_hint
    record.publishable_project = payload.publishable_project
    record.internal_only = payload.internal_only
    record.related_host_ids_json = payload.related_host_ids
    record.public_tech_tags_override_json = payload.public_tech_tags_override
    record.project_group = payload.project_group
    record.notes_for_cms_export = payload.notes_for_cms_export
    session.flush()
    return record


def build_project_mapping_preview(
    session: Session,
    *,
    project: Project,
    identity: MachineIdentityResult | None,
    profile: MachineProfile | None,
    research_outcome: ResearchOutcome | None = None,
) -> ProjectMappingPreview:
    host = project.host
    override = host.profile_override or session.execute(
        select(HostProfileOverride).where(HostProfileOverride.host_id == host.id)
    ).scalar_one_or_none()
    guidance = project.clarification_guidance or session.execute(
        select(ProjectClarificationGuidance).where(ProjectClarificationGuidance.project_id == project.id)
    ).scalar_one_or_none()
    research = research_outcome or latest_research_outcome(session, project)
    public_copy = build_public_copy(
        host=host,
        project=project,
        identity=identity,
        profile=profile,
        override=override,
        guidance=guidance,
        research=research,
    )
    title_candidate = public_copy["publish_title"]
    summary_candidate = public_copy["publish_summary"]
    tech_tags = public_copy["tech_tags"]

    default_decision, default_reason = _publishability_defaults(identity)
    publishable_kind = (
        "project"
        if identity and identity.primary_signature_id in {"mcp_gateway", "openwebui", "nextcloud", "generic_project_vm", "golem_provider", "wazuh_stack", "grafana"}
        else "infrastructure"
    )
    recommended_status = default_decision
    publishability_reason = default_reason
    internal_only_reason: str | None = None

    if override:
        if override.internal_only is True:
            recommended_status = "internal_only"
            internal_only_reason = "Manually marked internal-only."
            publishability_reason = "Manual override keeps this machine out of future CMS export."
        elif override.publishable_project is True:
            recommended_status = "publishable"
            publishability_reason = "Manual override marks this machine as a publishable project candidate."
    elif guidance:
        if guidance.internal_only is True:
            recommended_status = "internal_only"
            internal_only_reason = "Clarification guidance marks this machine as internal-only."
            publishability_reason = "Answered AI clarification guidance keeps this machine out of future CMS export."
        elif guidance.publishable_project is True:
            recommended_status = "publishable"
            publishability_reason = "Answered AI clarification guidance marks this machine as a publishable project candidate."

    if identity is None or (identity.identity_confidence or 0) < 0.72:
        recommended_status = "needs_review" if not (override and override.internal_only is True) else "internal_only"
        publishability_reason = "Identity confidence is low, so this mapping stays conservative."

    ready_for_cms = recommended_status == "publishable"
    needs_review = recommended_status == "needs_review"
    record = project.mapping_preview or session.execute(
        select(ProjectMappingPreview).where(ProjectMappingPreview.project_id == project.id)
    ).scalar_one_or_none()
    if record is None:
        record = ProjectMappingPreview(project_id=project.id, host_id=host.id)
        session.add(record)

    preview = ProjectMappingPreviewPayload(
        project_title_candidate=title_candidate,
        project_summary_candidate=summary_candidate,
        tech_tags_candidate=tech_tags,
        card_title_candidate=public_copy["card_title"],
        card_summary_candidate=public_copy["card_summary"],
        card_tech_tags_candidate=public_copy["card_tags"],
        publish_title_candidate=public_copy["publish_title"],
        publish_summary_candidate=public_copy["publish_summary"],
        publish_long_description_candidate=public_copy["publish_long_description"],
        publish_sections=public_copy["publish_sections"],
        publishable_kind=publishable_kind,
        recommended_status=recommended_status,
        demo_url_candidate=None,
        repo_url_candidate=_repo_candidate(project),
        publishability_reason=publishability_reason,
        internal_only_reason=internal_only_reason,
        ready_for_cms=ready_for_cms,
        needs_review=needs_review,
    )

    record.host_id = host.id
    record.primary_signature_id = identity.primary_signature_id if identity else None
    record.project_title_candidate = preview.project_title_candidate
    record.project_summary_candidate = preview.project_summary_candidate
    record.tech_tags_candidate_json = preview.tech_tags_candidate
    record.card_title_candidate = preview.card_title_candidate
    record.card_summary_candidate = preview.card_summary_candidate
    record.card_tech_tags_candidate_json = preview.card_tech_tags_candidate
    record.publish_title_candidate = preview.publish_title_candidate
    record.publish_summary_candidate = preview.publish_summary_candidate
    record.publish_long_description_candidate = preview.publish_long_description_candidate
    record.publish_sections_json = preview.publish_sections
    record.publishable_kind = preview.publishable_kind
    record.recommended_status = preview.recommended_status
    record.demo_url_candidate = preview.demo_url_candidate
    record.repo_url_candidate = preview.repo_url_candidate
    record.publishability_reason = preview.publishability_reason
    record.internal_only_reason = preview.internal_only_reason
    record.related_hosts_json = override.related_host_ids_json if override else []
    record.ready_for_cms = preview.ready_for_cms
    record.needs_review = preview.needs_review
    record.last_generated_at = utcnow()
    session.flush()
    return record
