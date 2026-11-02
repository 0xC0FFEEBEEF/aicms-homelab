from __future__ import annotations

from typing import Iterable

from app.models import (
    GuestInventory,
    Host,
    HostProfileOverride,
    MachineIdentityResult,
    MachineProfile,
    Project,
    ProjectClarificationGuidance,
    ProjectMappingPreview,
)
from app.project_mapping.preferences import hypervisor_instruction_preferences
from app.project_mapping.title_rules import mapping_title_matches_identity, stable_identity_title
from app.publish_preview.models import (
    CMSFieldPreviewRow,
    PreviewContentBlock,
    PreviewGuestEntry,
    PreviewRelatedHost,
    PreviewWarning,
    WebsiteProjectDetailViewModel,
    WebsiteProjectCardViewModel,
    WebsitePreviewModel,
    WebsitePublishCopyViewModel,
)

PUBLIC_DETAIL_BLOCKLIST = (
    "deterministic identity resolved",
    "human verification recommended",
    "allowlisted root",
    "allowlisted roots",
    "observed file path",
    "observed config path",
    "identity evidence",
    "signature evidence",
    "topology evidence",
    "research evidence",
)


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    resolved: list[str] = []
    for item in items:
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        resolved.append(text)
    return resolved


def _is_public_detail_safe(value: str) -> bool:
    text = str(value).strip()
    if not text:
        return False
    lowered = text.lower()
    if any(token in lowered for token in PUBLIC_DETAIL_BLOCKLIST):
        return False
    if lowered.startswith(("observed ", "local fact:", "topology fact:", "identity:", "signature:", "research:")):
        return False
    if "/" in text and not text.startswith(("http://", "https://")):
        return False
    return True


def _public_detail_items(items: Iterable[str], *, max_items: int) -> list[str]:
    return _dedupe(text for text in (str(item).strip() for item in items) if _is_public_detail_safe(text))[:max_items]


def _public_title(
    host: Host,
    project: Project,
    identity: MachineIdentityResult | None,
    mapping: ProjectMappingPreview | None,
    override: HostProfileOverride | None,
    guidance: ProjectClarificationGuidance | None,
) -> tuple[str, bool]:
    if override and override.public_title_override:
        return override.public_title_override, False
    if guidance and guidance.preferred_public_title:
        return guidance.preferred_public_title, False
    if mapping and mapping.publish_title_candidate and mapping_title_matches_identity(
        host=host,
        project=project,
        identity=identity,
        candidate_title=mapping.publish_title_candidate,
    ):
        return mapping.publish_title_candidate, False
    if mapping and mapping.project_title_candidate and mapping_title_matches_identity(
        host=host,
        project=project,
        identity=identity,
        candidate_title=mapping.project_title_candidate,
    ):
        return mapping.project_title_candidate, False
    return stable_identity_title(host=host, project=project, identity=identity), True


def _card_summary(
    host: Host,
    mapping: ProjectMappingPreview | None,
    profile: MachineProfile | None,
    override: HostProfileOverride | None,
) -> tuple[str, bool]:
    if mapping and mapping.card_summary_candidate:
        return mapping.card_summary_candidate, False
    if override and override.public_summary_hint:
        return override.public_summary_hint, False
    if mapping and mapping.project_summary_candidate:
        return mapping.project_summary_candidate, False
    if profile and profile.lead_paragraph:
        return profile.lead_paragraph, False
    role = profile.detected_role if profile else None
    if role:
        return f"This is my {host.name} machine. It currently appears to be used for {role}.", True
    return f"This is my {host.name} machine in AICMS. It still needs a cleaner public-facing summary.", True


def _publish_summary(
    host: Host,
    mapping: ProjectMappingPreview | None,
    profile: MachineProfile | None,
    override: HostProfileOverride | None,
) -> tuple[str, bool]:
    if override and override.public_summary_hint:
        return override.public_summary_hint, False
    if mapping and mapping.publish_summary_candidate:
        return mapping.publish_summary_candidate, False
    if mapping and mapping.project_summary_candidate:
        return mapping.project_summary_candidate, False
    if profile and profile.lead_paragraph:
        return profile.lead_paragraph, False
    role = profile.detected_role if profile else None
    if role:
        return f"This is my {host.name} machine. It currently appears to be used for {role}.", True
    return f"This is my {host.name} machine in AICMS. It still needs a cleaner public-facing summary.", True


def _public_tags(
    mapping: ProjectMappingPreview | None,
    identity: MachineIdentityResult | None,
    override: HostProfileOverride | None,
    *,
    card: bool,
) -> list[str]:
    if override and override.public_tech_tags_override_json:
        return _dedupe(override.public_tech_tags_override_json)
    if mapping and card and mapping.card_tech_tags_candidate_json:
        return _dedupe(mapping.card_tech_tags_candidate_json)
    if mapping and mapping.tech_tags_candidate_json:
        return _dedupe(mapping.tech_tags_candidate_json)
    if identity and identity.stack_tags_json:
        return _dedupe(identity.stack_tags_json)
    if identity and identity.detected_platform:
        return [identity.detected_platform]
    return []


def _public_repo_url(project: Project, mapping: ProjectMappingPreview | None) -> str | None:
    if mapping and mapping.repo_url_candidate:
        return mapping.repo_url_candidate
    remote = (project.repo_remote or "").strip()
    if remote.startswith(("http://", "https://")):
        return remote
    return None


def _public_demo_url(mapping: ProjectMappingPreview | None) -> str | None:
    if mapping and mapping.demo_url_candidate:
        return mapping.demo_url_candidate
    return None


def _publishability_status(
    mapping: ProjectMappingPreview | None,
    override: HostProfileOverride | None,
) -> str:
    if override and override.internal_only is True:
        return "internal_only"
    if override and override.publishable_project is True:
        return "publishable"
    if mapping and mapping.recommended_status:
        return mapping.recommended_status
    return "needs_review"


def _preview_status(publishability_status: str) -> str:
    if publishability_status == "publishable":
        return "draft"
    return "draft"


def _related_hosts(hosts: list[Host]) -> list[PreviewRelatedHost]:
    return [
        PreviewRelatedHost(id=host.id, name=host.name, address=host.address)
        for host in hosts
    ]


def _ram_label(ram_mb: int | None) -> str | None:
    if ram_mb is None:
        return None
    return f"{round(float(ram_mb) / 1024, 1)} GiB"


def _guest_type_label(guest_type: str | None) -> str:
    if guest_type == "qemu":
        return "VM"
    if guest_type == "lxc":
        return "LXC"
    return str(guest_type or "guest").upper()


def _fallback_guest_role(guest: GuestInventory) -> str:
    if guest.guest_type == "qemu":
        return "project workload"
    if guest.guest_type == "lxc":
        return "infrastructure guest"
    return "service workload"


def _linked_guest_role(guest: GuestInventory, linked_projects_by_host_id: dict[int, Project]) -> str:
    if guest.linked_host_id and guest.linked_host_id in linked_projects_by_host_id:
        linked_project = linked_projects_by_host_id[guest.linked_host_id]
        if linked_project.identity_result and linked_project.identity_result.detected_role:
            return linked_project.identity_result.detected_role
        if linked_project.machine_profile and linked_project.machine_profile.detected_role:
            return linked_project.machine_profile.detected_role
    return guest.role_guess or _fallback_guest_role(guest)


def _guest_entry(guest: GuestInventory, linked_projects_by_host_id: dict[int, Project]) -> PreviewGuestEntry:
    return PreviewGuestEntry(
        guest_name=guest.name or f"{_guest_type_label(guest.guest_type)} {guest.guest_id}",
        guest_type=_guest_type_label(guest.guest_type),
        guest_id=guest.guest_id,
        status=guest.status,
        cpu=f"{guest.cores} core(s)" if guest.cores else None,
        ram=_ram_label(guest.ram_mb),
        disk=guest.disk_summary,
        role=_linked_guest_role(guest, linked_projects_by_host_id),
        linked_host_id=guest.linked_host_id,
        linked_host_name=guest.linked_host.name if guest.linked_host else None,
    )


def _guest_publish_bullet(entry: PreviewGuestEntry) -> str:
    parts = [f"{entry.guest_name} — {entry.role or 'role still needs review'}"]
    details: list[str] = []
    if entry.cpu:
        details.append(entry.cpu)
    if entry.ram:
        details.append(entry.ram)
    if entry.disk:
        details.append(entry.disk)
    if entry.status:
        details.append(entry.status)
    if details:
        parts.append(f"({'; '.join(details)})")
    return " ".join(parts)


def _preview_guest_name(guest: GuestInventory) -> str:
    return guest.name or f"{_guest_type_label(guest.guest_type)} {guest.guest_id}"


def _profile_guest_keys(profile: MachineProfile | None) -> set[tuple[str, str]]:
    if not profile:
        return set()
    workloads = profile.contained_workloads_json or {}
    keys: set[tuple[str, str]] = set()
    for item in list(workloads.get("lxcs") or []) + list(workloads.get("vms") or []):
        guest_id = str(item.get("guest_id") or "").strip()
        guest_type = str(item.get("guest_type") or "").strip()
        if guest_id and guest_type:
            keys.add((guest_type, guest_id))
    return keys


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
        blocks.append(
            PreviewContentBlock(
                kind="paragraph",
                text=" ".join(lines),
            )
        )
    return blocks


def _build_hypervisor_publish_details(
    guest_inventory: list[GuestInventory],
    linked_projects_by_host_id: dict[int, Project],
) -> tuple[str | None, list[str]]:
    vms = [_guest_entry(item, linked_projects_by_host_id) for item in guest_inventory if item.guest_type == "qemu"]
    lxcs = [_guest_entry(item, linked_projects_by_host_id) for item in guest_inventory if item.guest_type == "lxc"]
    if not vms and not lxcs:
        return None, []

    summary = f"Hosted workloads: {len(vms)} VM(s) and {len(lxcs)} LXC container(s) are currently visible in the inventory."
    sections: list[str] = ["Hosted workloads summary:", summary]
    if lxcs:
        sections.append("Hosted LXCs:")
        sections.extend(_guest_publish_bullet(item) for item in lxcs)
    if vms:
        sections.append("Hosted VMs:")
        sections.extend(_guest_publish_bullet(item) for item in vms)
    return summary, sections


def _build_overview_paragraphs(summary: str, profile: MachineProfile | None) -> list[str]:
    paragraphs = [summary]
    if profile and profile.project_role_summary:
        role_summary = profile.project_role_summary.strip()
        if role_summary and role_summary not in paragraphs:
            paragraphs.append(role_summary)
    return paragraphs


def _build_setup_highlights(profile: MachineProfile | None) -> list[str]:
    highlights: list[str] = []
    if profile and profile.setup_details_json:
        highlights.extend(str(item).strip() for item in profile.setup_details_json[:6] if str(item).strip())
    if profile and profile.detected_components_json and len(highlights) < 6:
        for item in profile.detected_components_json:
            label = str(item.get("name") or item.get("service") or item.get("label") or "").strip()
            if not label:
                continue
            highlights.append(f"Detected component: {label}")
            if len(highlights) >= 6:
                break
    return _dedupe(highlights)


def _build_why_it_matters(profile: MachineProfile | None) -> list[str]:
    if profile and profile.why_it_matters_json:
        return _dedupe(str(item).strip() for item in profile.why_it_matters_json[:5])
    return []


def _build_long_description(
    overview_paragraphs: list[str],
    setup_highlights: list[str],
    why_it_matters: list[str],
) -> str:
    blocks = list(overview_paragraphs)
    if setup_highlights:
        blocks.append("Setup highlights: " + "; ".join(setup_highlights[:4]) + ".")
    if why_it_matters:
        blocks.append("Why it matters in my environment: " + "; ".join(why_it_matters[:3]) + ".")
    return "\n\n".join(block for block in blocks if block)


def _build_missing_fields(
    *,
    fallback_title: bool,
    fallback_summary: bool,
    demo_url: str | None,
    repo_url: str | None,
    publishability_status: str,
) -> list[str]:
    missing: list[str] = []
    if fallback_title:
        missing.append("Title")
    if fallback_summary:
        missing.append("Summary")
    if not demo_url:
        missing.append("Demo URL")
    if not repo_url:
        missing.append("Repo URL")
    if publishability_status not in {"publishable", "internal_only"}:
        missing.append("Publishability approval")
    return missing


def _build_warnings(
    *,
    identity: MachineIdentityResult | None,
    mapping: ProjectMappingPreview | None,
    override: HostProfileOverride | None,
    fallback_title: bool,
    fallback_summary: bool,
    missing_fields: list[str],
) -> list[PreviewWarning]:
    warnings: list[PreviewWarning] = []
    publishability_status = _publishability_status(mapping, override)
    if publishability_status == "internal_only":
        reason = (
            mapping.internal_only_reason
            if mapping and mapping.internal_only_reason
            else "This machine is currently marked internal-only."
        )
        warnings.append(
            PreviewWarning(
                code="internal_only",
                label="Internal only",
                detail=reason,
                level="error",
            )
        )
    if publishability_status == "needs_review":
        reason = (
            mapping.publishability_reason
            if mapping and mapping.publishability_reason
            else "This preview still needs review before it would be suitable for a public project page."
        )
        warnings.append(
            PreviewWarning(
                code="needs_review",
                label="Needs review",
                detail=reason,
                level="warning",
            )
        )
    if identity is None or (identity.identity_confidence or 0) < 0.70:
        warnings.append(
            PreviewWarning(
                code="low_confidence_identity",
                label="Low confidence identity",
                detail="The machine identity is low-confidence or unresolved, so public-facing wording may still be generic.",
                level="warning",
            )
        )
    if fallback_title or fallback_summary:
        warnings.append(
            PreviewWarning(
                code="fallback_wording",
                label="Generated fallback wording in use",
                detail="The preview is using fallback wording because a stronger public title or summary has not been curated yet.",
                level="warning",
            )
        )
    for field in missing_fields:
        warnings.append(
            PreviewWarning(
                code=f"missing_{field.lower().replace(' ', '_')}",
                label=f"Missing {field}",
                detail=f"{field} is not set yet for the future CMS-style preview.",
                level="warning",
            )
        )
    return warnings


def _build_cms_fields(
    *,
    title: str,
    long_description: str,
    structured_sections: list[str],
    tech_tags: list[str],
    demo_url: str | None,
    repo_url: str | None,
    status: str,
    publish_at: str | None,
    missing_fields: list[str],
) -> list[CMSFieldPreviewRow]:
    missing_set = set(missing_fields)
    return [
        CMSFieldPreviewRow(label="Title", value=title or "Not set", missing="Title" in missing_set),
        CMSFieldPreviewRow(label="Summary", value=long_description or "Not set", missing="Summary" in missing_set, multiline=True),
        CMSFieldPreviewRow(
            label="Structured Sections",
            value="\n".join(f"- {item}" for item in structured_sections) if structured_sections else "Not set",
            missing=False,
            multiline=True,
        ),
        CMSFieldPreviewRow(
            label="Tech",
            value=", ".join(tech_tags) if tech_tags else "Not set",
            missing=False,
        ),
        CMSFieldPreviewRow(label="Demo URL", value=demo_url or "Not set", missing="Demo URL" in missing_set),
        CMSFieldPreviewRow(label="Repo URL", value=repo_url or "Not set", missing="Repo URL" in missing_set),
        CMSFieldPreviewRow(label="Status", value=status, missing=False),
        CMSFieldPreviewRow(label="Publish At", value=publish_at or "Not scheduled", missing=False),
    ]


def _build_detail_view(
    *,
    title: str,
    short_summary: str,
    long_description: str,
    tech_tags: list[str],
    demo_url: str | None,
    repo_url: str | None,
    profile: MachineProfile | None,
    related_hosts: list[Host],
    publish_sections: list[str],
    guest_inventory: list[GuestInventory],
    linked_projects_by_host_id: dict[int, Project],
    identity: MachineIdentityResult | None,
) -> WebsiteProjectDetailViewModel:
    host_resources = []
    if profile and profile.host_resources_json:
        if profile.host_resources_json.get("cpu"):
            host_resources.append(f"CPU: {profile.host_resources_json['cpu']}")
        if profile.host_resources_json.get("memory"):
            host_resources.append(f"RAM: {profile.host_resources_json['memory']}")
        for item in profile.host_resources_json.get("storage") or []:
            if isinstance(item, dict):
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
                host_resources.append(line)
            else:
                host_resources.append(str(item))
        if profile.host_resources_json.get("operating_system"):
            host_resources.append(f"OS: {profile.host_resources_json['operating_system']}")
        if profile.host_resources_json.get("management_ip"):
            host_resources.append(f"Management IP: {profile.host_resources_json['management_ip']}")

    setup_summary = _public_detail_items((profile.setup_details_json or []) if profile else [], max_items=8)
    notes_or_highlights = []
    if profile and profile.why_it_matters_json:
        notes_or_highlights.extend(_public_detail_items(profile.why_it_matters_json, max_items=8))
    if profile and profile.notes_json:
        notes_or_highlights.extend(_public_detail_items(profile.notes_json, max_items=8))
    notes_or_highlights = _dedupe(notes_or_highlights)[:8]

    vms = [_guest_entry(item, linked_projects_by_host_id) for item in guest_inventory if item.guest_type == "qemu"]
    lxcs = [_guest_entry(item, linked_projects_by_host_id) for item in guest_inventory if item.guest_type == "lxc"]

    hosted_workload_summary: list[str] = []
    if vms or lxcs:
        hosted_workload_summary.append(f"{len(vms)} VM(s) and {len(lxcs)} LXC container(s) are currently visible in the inventory.")
        featured_names = [item.guest_name for item in (vms + lxcs)[:6] if item.guest_name]
        if featured_names:
            hosted_workload_summary.append("Featured workloads: " + ", ".join(featured_names) + ".")

    return WebsiteProjectDetailViewModel(
        title=title,
        short_summary=short_summary,
        long_description=long_description,
        tech=tech_tags,
        demo_url=demo_url,
        repo_url=repo_url,
        role_in_lab=profile.project_role_summary if profile else (identity.detected_role if identity else None),
        host_resources=host_resources,
        setup_summary=setup_summary,
        detail_sections=publish_sections,
        related_hosts=_related_hosts(related_hosts),
        hosted_workload_summary=hosted_workload_summary,
        hosted_lxcs=lxcs,
        hosted_vms=vms,
        notes_or_highlights=notes_or_highlights,
        machine_kind=identity.machine_kind if identity else (profile.machine_kind if profile else None),
        detected_role=identity.detected_role if identity else (profile.detected_role if profile else None),
        detected_platform=identity.detected_platform if identity else (profile.detected_platform if profile else None),
    )


def build_preview_model(
    *,
    host: Host,
    project: Project,
    identity: MachineIdentityResult | None,
    profile: MachineProfile | None,
    mapping: ProjectMappingPreview | None,
    override: HostProfileOverride | None,
    guidance: ProjectClarificationGuidance | None,
    related_hosts: list[Host],
    guest_inventory: list[GuestInventory] | None = None,
    linked_projects_by_host_id: dict[int, Project] | None = None,
) -> WebsitePreviewModel:
    title, fallback_title = _public_title(host, project, identity, mapping, override, guidance)
    card_summary, fallback_summary = _card_summary(host, mapping, profile, override)
    publish_summary, fallback_publish_summary = _publish_summary(host, mapping, profile, override)
    card_tags = _public_tags(mapping, identity, override, card=True)
    tech_tags = _public_tags(mapping, identity, override, card=False)
    guest_preferences = hypervisor_instruction_preferences(project)
    excluded_guest_names = guest_preferences["excluded_guest_names"]
    preferred_guest_keys = _profile_guest_keys(profile)
    resolved_guest_inventory = [
        item
        for item in (guest_inventory or [])
        if (not preferred_guest_keys or (str(item.guest_type or ""), str(item.guest_id or "")) in preferred_guest_keys)
        and _preview_guest_name(item).lower() not in excluded_guest_names
    ]
    resolved_linked_projects = linked_projects_by_host_id or {}
    demo_url = _public_demo_url(mapping)
    repo_url = _public_repo_url(project, mapping)
    publishability_status = _publishability_status(mapping, override)
    status = _preview_status(publishability_status)
    publish_at = None
    overview_paragraphs = _build_overview_paragraphs(publish_summary, profile)
    setup_highlights = _build_setup_highlights(profile)
    why_it_matters = _build_why_it_matters(profile)
    long_description = (
        mapping.publish_long_description_candidate
        if mapping and mapping.publish_long_description_candidate
        else _build_long_description(overview_paragraphs, setup_highlights, why_it_matters)
    )
    publish_sections = list(mapping.publish_sections_json or []) if mapping and mapping.publish_sections_json else []
    if identity and identity.primary_signature_id == "proxmox_hypervisor":
        inventory_summary, inventory_sections = _build_hypervisor_publish_details(
            resolved_guest_inventory,
            resolved_linked_projects,
        )
        if inventory_summary and inventory_summary not in long_description:
            long_description = f"{long_description}\n\n{inventory_summary}".strip()
        if guest_preferences["include_all_guests"] and inventory_sections:
            inventory_block = "\n\n".join(
                section
                if section.endswith(":")
                else f"- {section}"
                for section in inventory_sections
            )
            if inventory_block and inventory_block not in long_description:
                long_description = f"{long_description}\n\n{inventory_block}".strip()
        for item in inventory_sections:
            if item not in publish_sections:
                publish_sections.append(item)
    missing_fields = _build_missing_fields(
        fallback_title=fallback_title,
        fallback_summary=fallback_publish_summary,
        demo_url=demo_url,
        repo_url=repo_url,
        publishability_status=publishability_status,
    )
    warnings = _build_warnings(
        identity=identity,
        mapping=mapping,
        override=override,
        fallback_title=fallback_title,
        fallback_summary=fallback_publish_summary,
        missing_fields=missing_fields,
    )
    card = WebsiteProjectCardViewModel(
        title=title,
        summary=card_summary,
        tech=card_tags,
        demo_url=demo_url,
        repo_url=repo_url,
        status=status,
    )
    publish = WebsitePublishCopyViewModel(
        title=title,
        short_summary=publish_summary,
        long_description=long_description,
        content_blocks=_build_content_blocks(long_description),
        tech=tech_tags,
        sections=publish_sections,
        demo_url=demo_url,
        repo_url=repo_url,
        status=status,
        publish_at=publish_at,
    )
    detail = _build_detail_view(
        title=title,
        short_summary=publish.short_summary,
        long_description=publish.long_description,
        tech_tags=tech_tags,
        demo_url=demo_url,
        repo_url=repo_url,
        profile=profile,
        related_hosts=related_hosts,
        publish_sections=publish_sections,
        guest_inventory=resolved_guest_inventory,
        linked_projects_by_host_id=resolved_linked_projects,
        identity=identity,
    )
    cms_fields = _build_cms_fields(
        title=title,
        long_description=publish.long_description,
        structured_sections=publish.sections,
        tech_tags=tech_tags,
        demo_url=demo_url,
        repo_url=repo_url,
        status=status,
        publish_at=publish_at,
        missing_fields=missing_fields,
    )
    return WebsitePreviewModel(
        title=title,
        summary=publish_summary,
        long_description=long_description,
        overview_paragraphs=overview_paragraphs,
        setup_highlights=setup_highlights,
        why_it_matters=why_it_matters,
        tech_tags=tech_tags,
        demo_url=demo_url,
        repo_url=repo_url,
        status=status,
        publish_at=publish_at,
        related_hosts=_related_hosts(related_hosts),
        machine_kind=identity.machine_kind if identity else (profile.machine_kind if profile else None),
        detected_role=identity.detected_role if identity else (profile.detected_role if profile else None),
        detected_platform=identity.detected_platform if identity else (profile.detected_platform if profile else None),
        publishability_status=publishability_status,
        warnings=warnings,
        missing_fields=missing_fields,
        fallback_title_in_use=fallback_title,
        fallback_summary_in_use=fallback_publish_summary,
        card=card,
        publish=publish,
        detail=detail,
        cms_fields=cms_fields,
    )
