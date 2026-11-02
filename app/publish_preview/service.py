from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.investigations.service import investigation_public_preview_draft, investigation_visible_draft
from app.models import (
    GuestInventory,
    Host,
    InvestigationSession,
    MachineIdentityResult,
    MachineProfile,
    Project,
)
from app.publish_preview.adapters import build_preview_model
from app.publish_preview.models import (
    CMSFieldPreviewRow,
    PreviewContentBlock,
    PreviewGuestEntry,
    PreviewRelatedHost,
    WebsiteProjectCardViewModel,
    WebsiteProjectDetailViewModel,
    WebsitePublishCopyViewModel,
    WebsitePreviewModel,
)


def _project_preview_dependencies(
    session: Session,
    host: Host | None,
) -> tuple[list[Host], list[GuestInventory], dict[int, Project]]:
    related_hosts: list[Host] = []
    guest_inventory: list[GuestInventory] = []
    linked_projects_by_host_id: dict[int, Project] = {}
    if host and host.profile_override and host.profile_override.related_host_ids_json:
        related_hosts = (
            session.execute(
                select(Host)
                .where(Host.id.in_(host.profile_override.related_host_ids_json))
                .order_by(Host.name.asc())
            )
            .scalars()
            .all()
        )
    if host is not None:
        guest_inventory = (
            session.execute(
                select(GuestInventory)
                .where(GuestInventory.host_id == host.id)
                .options(selectinload(GuestInventory.linked_host))
                .order_by(GuestInventory.guest_type.asc(), GuestInventory.guest_id.asc())
            )
            .scalars()
            .all()
        )
        linked_host_ids = sorted({item.linked_host_id for item in guest_inventory if item.linked_host_id})
        if linked_host_ids:
            linked_projects = (
                session.execute(
                    select(Project)
                    .where(Project.host_id.in_(linked_host_ids))
                    .options(
                        selectinload(Project.identity_result),
                        selectinload(Project.machine_profile),
                    )
                    .order_by(Project.id.asc())
                )
                .scalars()
                .all()
            )
            for candidate in linked_projects:
                existing = linked_projects_by_host_id.get(candidate.host_id)
                if existing is None or ((candidate.metadata_json or {}).get("setup_kind") == "host-summary"):
                    linked_projects_by_host_id[candidate.host_id] = candidate
    return related_hosts, guest_inventory, linked_projects_by_host_id


def _investigation_draft_text(
    investigation: InvestigationSession,
    preview: WebsitePreviewModel | None = None,
    *,
    draft_scope: str = "public",
) -> tuple[str, str, str, str]:
    if draft_scope == "visible":
        preview_source, draft = investigation_visible_draft(investigation)
    else:
        preview_source, draft = investigation_public_preview_draft(investigation)
    draft = draft or {}
    fallback_title = (
        preview.title
        if preview is not None
        else investigation.target_name or investigation.target_hostname or investigation.target_address or f"Investigation {investigation.id}"
    )
    fallback_summary = (
        preview.publish.short_summary
        if preview is not None
        else "This investigation preview is still being drafted."
    )
    fallback_long = (
        preview.publish.long_description
        if preview is not None
        else investigation.draft_report_text or fallback_summary
    )
    title = str(draft.get("publish_title") or draft.get("title") or fallback_title).strip() or fallback_title
    short_summary = (
        str(draft.get("publish_summary") or draft.get("overview") or fallback_summary).strip() or fallback_summary
    )
    preferred_long = draft.get("publish_long_description")
    if draft_scope == "visible" and str(draft.get("report_text") or "").strip():
        preferred_long = draft.get("report_text")
    long_description = str(preferred_long or draft.get("report_text") or fallback_long).strip() or fallback_long
    return title, short_summary, long_description, preview_source


def _build_content_blocks(long_description: str) -> list[PreviewContentBlock]:
    blocks: list[PreviewContentBlock] = []
    for chunk in [item.strip() for item in long_description.split("\n\n") if item.strip()]:
        lines = [line.strip() for line in chunk.splitlines() if line.strip()]
        if not lines:
            continue
        if lines[0].endswith(":") and any(line.startswith("- ") for line in lines[1:]):
            blocks.append(
                PreviewContentBlock(
                    kind="group",
                    heading=lines[0].rstrip(":"),
                    items=[line[2:].strip() for line in lines[1:] if line.startswith("- ")],
                )
            )
            continue
        if all(line.startswith("- ") for line in lines):
            blocks.append(
                PreviewContentBlock(
                    kind="group",
                    items=[line[2:].strip() for line in lines],
                )
            )
            continue
        blocks.append(PreviewContentBlock(kind="paragraph", text=" ".join(lines)))
    return blocks


def _replace_cms_fields(
    preview: WebsitePreviewModel,
    *,
    title: str,
    long_description: str,
) -> None:
    updated_fields: list[CMSFieldPreviewRow] = []
    for row in preview.cms_fields:
        if row.label == "Title":
            updated_fields.append(CMSFieldPreviewRow(label=row.label, value=title, missing=False, multiline=row.multiline))
        elif row.label == "Summary":
            updated_fields.append(
                CMSFieldPreviewRow(label=row.label, value=long_description, missing=False, multiline=True)
            )
        else:
            updated_fields.append(row)
    preview.cms_fields = updated_fields


def _host_resources_from_facts(facts: dict) -> list[str]:
    if not facts:
        return []
    lines: list[str] = []
    if facts.get("cpu_cores") or facts.get("cpu_threads"):
        cpu = facts.get("cpu_cores") or "?"
        threads = facts.get("cpu_threads") or "?"
        lines.append(f"CPU: {cpu} cores / {threads} threads")
    if facts.get("memory_gib") is not None:
        lines.append(f"RAM: {facts.get('memory_gib')} GiB")
    if facts.get("root_size_gib") is not None:
        lines.append(f"Root disk: {facts.get('root_size_gib')} GiB")
    if facts.get("os_pretty_name"):
        lines.append(f"OS: {facts.get('os_pretty_name')}")
    if facts.get("management_ip"):
        lines.append(f"Management IP: {facts.get('management_ip')}")
    storage_items = []
    for item in facts.get("storage_facts") or []:
        if not isinstance(item, dict):
            continue
        mount = str(item.get("mount_point") or item.get("storage_name") or "storage").strip()
        storage_type = str(item.get("storage_type") or "").strip()
        details: list[str] = []
        if item.get("total_gib") is not None:
            details.append(f"{item.get('total_gib')} GiB total")
        if item.get("used_gib") is not None:
            details.append(f"{item.get('used_gib')} GiB used")
        if item.get("available_gib") is not None:
            details.append(f"{item.get('available_gib')} GiB available")
        line = mount
        if storage_type:
            line += f" ({storage_type})"
        if details:
            line += " — " + ", ".join(details)
        storage_items.append(line)
    return lines + storage_items


def _preview_guest_entry(item: dict) -> PreviewGuestEntry:
    guest_type = str(item.get("guest_type") or "guest").strip()
    guest_label = "VM" if guest_type == "qemu" else ("LXC" if guest_type == "lxc" else guest_type.upper())
    cpu = item.get("cores")
    ram_mb = item.get("ram_mb")
    return PreviewGuestEntry(
        guest_name=str(item.get("name") or f"{guest_label} {item.get('guest_id') or '?'}").strip(),
        guest_type=guest_label,
        guest_id=str(item.get("guest_id")) if item.get("guest_id") is not None else None,
        status=str(item.get("status")).strip() if item.get("status") else None,
        cpu=f"{cpu} core(s)" if cpu else None,
        ram=f"{round(float(ram_mb) / 1024, 1)} GiB" if ram_mb else None,
        disk=str(item.get("disk_summary")).strip() if item.get("disk_summary") else None,
        role=str(item.get("role_guess")).strip() if item.get("role_guess") else None,
    )


def _related_hosts_for_investigation(
    session: Session | None,
    investigation: InvestigationSession,
    host: Host | None,
) -> list[PreviewRelatedHost]:
    if session is not None and host is not None and host.profile_override and host.profile_override.related_host_ids_json:
        related_hosts = (
            session.execute(
                select(Host)
                .where(Host.id.in_(host.profile_override.related_host_ids_json))
                .order_by(Host.name.asc())
            )
            .scalars()
            .all()
        )
        return [PreviewRelatedHost(id=item.id, name=item.name, address=item.address) for item in related_hosts]
    if investigation.linked_guest and investigation.linked_guest.host:
        parent = investigation.linked_guest.host
        return [PreviewRelatedHost(id=parent.id, name=parent.name, address=parent.address)]
    return []


def _overlay_draft_detail_fields(preview: WebsitePreviewModel, draft: dict[str, Any] | None) -> None:
    from app.publish_preview.models import PreviewCustomSection

    if not draft:
        return
    if draft.get("detail_sections"):
        preview.detail.detail_sections = draft["detail_sections"]
    if draft.get("role_in_lab"):
        preview.detail.role_in_lab = draft["role_in_lab"]
    if draft.get("setup_summary"):
        preview.detail.setup_summary = draft["setup_summary"]
    if draft.get("notes_or_highlights"):
        preview.detail.notes_or_highlights = draft["notes_or_highlights"]
    custom = draft.get("custom_sections")
    if isinstance(custom, list) and custom:
        preview.detail.custom_sections = [
            PreviewCustomSection(heading=sec["heading"], items=sec.get("items", []))
            for sec in custom
            if isinstance(sec, dict) and sec.get("heading")
        ]


def _apply_investigation_draft_to_preview(
    preview: WebsitePreviewModel,
    investigation: InvestigationSession,
    *,
    draft_scope: str = "public",
) -> WebsitePreviewModel:
    title, short_summary, long_description, preview_source = _investigation_draft_text(
        investigation,
        preview,
        draft_scope=draft_scope,
    )
    content_blocks = _build_content_blocks(long_description)
    preview.title = title
    preview.summary = short_summary
    preview.long_description = long_description
    preview.card.title = title
    preview.card.summary = short_summary
    preview.publish.title = title
    preview.publish.short_summary = short_summary
    preview.publish.long_description = long_description
    preview.publish.content_blocks = content_blocks
    preview.detail.title = title
    preview.detail.short_summary = short_summary
    preview.detail.long_description = long_description
    preview.fallback_title_in_use = preview_source != "assistant"
    preview.fallback_summary_in_use = preview_source != "assistant"
    _replace_cms_fields(preview, title=title, long_description=long_description)

    facts = investigation.raw_facts_json or {}
    resources = _host_resources_from_facts(facts)
    if resources:
        preview.detail.host_resources = resources
    guests = facts.get("guest_inventory") or []
    if guests:
        preview.detail.hosted_vms = [_preview_guest_entry(item) for item in guests if item.get("guest_type") == "qemu"]
        preview.detail.hosted_lxcs = [_preview_guest_entry(item) for item in guests if item.get("guest_type") == "lxc"]
        preview.detail.hosted_workload_summary = [
            f"{len(preview.detail.hosted_vms)} VM(s) and {len(preview.detail.hosted_lxcs)} LXC container(s) are currently visible in the investigation facts."
        ]
    related_hosts = _related_hosts_for_investigation(None, investigation, None)
    if related_hosts:
        preview.detail.related_hosts = related_hosts
    normalized = investigation.normalized_findings_json or {}
    identity = normalized.get("identity") or {}
    if identity.get("machine_kind"):
        preview.machine_kind = identity["machine_kind"]
        preview.detail.machine_kind = identity["machine_kind"]
    if identity.get("detected_role"):
        preview.detected_role = identity["detected_role"]
        preview.detail.detected_role = identity["detected_role"]
    if identity.get("detected_platform"):
        preview.detected_platform = identity["detected_platform"]
        preview.detail.detected_platform = identity["detected_platform"]
    if identity.get("stack_tags"):
        tags = [str(item).strip() for item in identity.get("stack_tags") or [] if str(item).strip()]
        if tags:
            preview.tech_tags = tags
            preview.card.tech = tags
            preview.publish.tech = tags
            preview.detail.tech = tags

    if draft_scope == "visible":
        _, visible_draft = investigation_visible_draft(investigation)
    else:
        _, visible_draft = investigation_public_preview_draft(investigation)
    _overlay_draft_detail_fields(preview, visible_draft)
    return preview


def _minimal_investigation_preview(
    session: Session,
    investigation: InvestigationSession,
    *,
    draft_scope: str = "public",
) -> WebsitePreviewModel:
    title, short_summary, long_description, preview_source = _investigation_draft_text(
        investigation,
        draft_scope=draft_scope,
    )
    facts = investigation.raw_facts_json or {}
    normalized = investigation.normalized_findings_json or {}
    identity = normalized.get("identity") or {}
    related_hosts = _related_hosts_for_investigation(session, investigation, investigation.linked_host)
    tech_tags = [str(item).strip() for item in identity.get("stack_tags") or [] if str(item).strip()]
    hosted_vms = [_preview_guest_entry(item) for item in facts.get("guest_inventory") or [] if item.get("guest_type") == "qemu"]
    hosted_lxcs = [_preview_guest_entry(item) for item in facts.get("guest_inventory") or [] if item.get("guest_type") == "lxc"]
    hosted_summary: list[str] = []
    if hosted_vms or hosted_lxcs:
        hosted_summary.append(
            f"{len(hosted_vms)} VM(s) and {len(hosted_lxcs)} LXC container(s) are currently visible in the investigation facts."
        )
    content_blocks = _build_content_blocks(long_description)
    cms_fields = [
        CMSFieldPreviewRow(label="Title", value=title, missing=False),
        CMSFieldPreviewRow(label="Summary", value=long_description or "Not set", missing=not bool(long_description), multiline=True),
        CMSFieldPreviewRow(label="Structured Sections", value="Not set", missing=False, multiline=True),
        CMSFieldPreviewRow(label="Tech", value=", ".join(tech_tags) if tech_tags else "Not set", missing=False),
        CMSFieldPreviewRow(label="Demo URL", value="Not set", missing=True),
        CMSFieldPreviewRow(label="Repo URL", value="Not set", missing=True),
        CMSFieldPreviewRow(label="Status", value="draft", missing=False),
        CMSFieldPreviewRow(label="Publish At", value="Not scheduled", missing=False),
    ]
    preview = WebsitePreviewModel(
        title=title,
        summary=short_summary,
        long_description=long_description,
        overview_paragraphs=[short_summary] if short_summary else [],
        setup_highlights=[],
        why_it_matters=[],
        tech_tags=tech_tags,
        demo_url=None,
        repo_url=None,
        status="draft",
        publish_at=None,
        related_hosts=related_hosts,
        machine_kind=identity.get("machine_kind"),
        detected_role=identity.get("detected_role"),
        detected_platform=identity.get("detected_platform"),
        publishability_status="needs_review",
        warnings=[],
        missing_fields=["Demo URL", "Repo URL", "Publishability approval"],
        fallback_title_in_use=preview_source != "assistant",
        fallback_summary_in_use=preview_source != "assistant",
        card=WebsiteProjectCardViewModel(
            title=title,
            summary=short_summary,
            tech=tech_tags,
            demo_url=None,
            repo_url=None,
            status="draft",
        ),
        publish=WebsitePublishCopyViewModel(
            title=title,
            short_summary=short_summary,
            long_description=long_description,
            content_blocks=content_blocks,
            tech=tech_tags,
            sections=[],
            demo_url=None,
            repo_url=None,
            status="draft",
            publish_at=None,
        ),
        detail=WebsiteProjectDetailViewModel(
            title=title,
            short_summary=short_summary,
            long_description=long_description,
            tech=tech_tags,
            demo_url=None,
            repo_url=None,
            role_in_lab=identity.get("detected_role"),
            host_resources=_host_resources_from_facts(facts),
            setup_summary=[],
            detail_sections=[],
            related_hosts=related_hosts,
            hosted_workload_summary=hosted_summary,
            hosted_lxcs=hosted_lxcs,
            hosted_vms=hosted_vms,
            notes_or_highlights=[],
            machine_kind=identity.get("machine_kind"),
            detected_role=identity.get("detected_role"),
            detected_platform=identity.get("detected_platform"),
        ),
        cms_fields=cms_fields,
    )
    if draft_scope == "visible":
        _, visible_draft = investigation_visible_draft(investigation)
    else:
        _, visible_draft = investigation_public_preview_draft(investigation)
    _overlay_draft_detail_fields(preview, visible_draft)
    return preview


def build_project_website_preview(session: Session, project: Project) -> WebsitePreviewModel:
    host = project.host
    related_hosts, guest_inventory, linked_projects_by_host_id = _project_preview_dependencies(session, host)

    return build_preview_model(
        host=host,
        project=project,
        identity=project.identity_result,
        profile=project.machine_profile,
        mapping=project.mapping_preview,
        override=host.profile_override if host else None,
        guidance=project.clarification_guidance,
        related_hosts=related_hosts,
        guest_inventory=guest_inventory,
        linked_projects_by_host_id=linked_projects_by_host_id,
    )


def build_projects_website_previews(session: Session, projects: list[Project]) -> list[WebsitePreviewModel]:
    return [build_project_website_preview(session, project) for project in projects]


def build_investigation_preview_model(
    session: Session,
    investigation: InvestigationSession,
    *,
    draft_scope: str = "public",
) -> WebsitePreviewModel:
    project = investigation.linked_project
    if project is not None:
        preview = build_project_website_preview(session, project)
        return _apply_investigation_draft_to_preview(preview, investigation, draft_scope=draft_scope)
    return _minimal_investigation_preview(session, investigation, draft_scope=draft_scope)


def build_investigation_website_preview(session: Session, investigation: InvestigationSession) -> WebsitePreviewModel:
    return build_investigation_preview_model(session, investigation, draft_scope="public")


def build_investigation_working_preview(session: Session, investigation: InvestigationSession) -> WebsitePreviewModel:
    return build_investigation_preview_model(session, investigation, draft_scope="visible")
