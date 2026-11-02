from __future__ import annotations

import json
import re
from pathlib import PurePosixPath
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.clarifications.service import mark_project_clarifications_used
from app.config import AppConfig
from app.lmstudio.client import LMStudioClient
from app.models import (
    Host,
    HostFact,
    LMStudioInteractionLog,
    MachineIdentityResult,
    MachineProfile,
    Project,
    ProjectFile,
    SignatureMatch,
)
from app.project_mapping.preferences import hypervisor_instruction_preferences
from app.project_mapping.service import build_project_mapping_preview
from app.profiles.collector import collect_machine_facts
from app.profiles.classifier import classify_machine
from app.profiles.inventory import sync_host_fact_inventory
from app.research.models import ResearchOutcome
from app.research.service import latest_research_outcome, run_identity_research
from app.scanner.ssh import SSHExecutor
from app.schemas import (
    LMSTUDIO_SETTINGS_KEY,
    PROFILE_SETTINGS_KEY,
    LMStudioSettingsPayload,
    MachineClassificationResult,
    MachineProfileNarrativeResult,
    ProfileSettingsPayload,
)
from app.signatures.models import MachineIdentityResolution
from app.signatures.resolver import resolve_machine_identity
from app.topology.service import build_host_topology_context, refresh_host_topology
from app.utils.common import utcnow
from app.utils.settings_store import get_settings


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _bytes_to_gib(value: int | None) -> float | None:
    if value is None:
        return None
    return round(value / (1024**3), 2)


def _record_lm_log(session: Session, action: str, status: str, message: str, details: dict[str, Any] | None = None) -> None:
    session.add(
        LMStudioInteractionLog(
            action=action,
            status=status,
            message=message,
            details_json=details,
        )
    )
    session.flush()


def _lmstudio_client(config: AppConfig, settings: LMStudioSettingsPayload) -> LMStudioClient:
    timeout = settings.request_timeout_seconds if settings.use_custom_timeout else config.lmstudio_timeout_seconds
    return LMStudioClient(base_url=settings.base_url, timeout=timeout, api_token=settings.api_token)


def _host_fact_payload(host: Host) -> dict[str, Any]:
    facts = (host.fact_snapshot.facts_json if host.fact_snapshot else {}) or {}
    if not facts:
        facts = {
            "hostname": host.name,
            "management_ip": host.address,
        }
    return facts


def _identity_user_hints(project: Project) -> list[str]:
    hints: list[str] = []
    metadata = project.metadata_json or {}
    operator_context = metadata.get("operator_context") or {}
    for field in ("added_info", "extra_instructions"):
        value = _clean_text(operator_context.get(field))
        if value:
            hints.append(value)
    if project.host and project.host.profile_override:
        override = project.host.profile_override
        for value in (
            override.project_label_override,
            override.public_title_override,
            override.public_summary_hint,
            override.project_group,
            override.notes_for_cms_export,
        ):
            cleaned = _clean_text(value)
            if cleaned:
                hints.append(cleaned)
    if project.clarification_guidance:
        guidance = project.clarification_guidance
        for value in (
            guidance.preferred_public_title,
            guidance.preferred_role_wording,
            guidance.preferred_project_group,
            guidance.preferred_summary_framing,
        ):
            cleaned = _clean_text(value)
            if cleaned:
                hints.append(cleaned)
        for item in guidance.guidance_notes_json or []:
            cleaned = _clean_text(item)
            if cleaned:
                hints.append(cleaned)
        if guidance.standalone_preference:
            hints.append(f"standalone preference: {guidance.standalone_preference}")
        if guidance.internal_only is True:
            hints.append("public intent: internal only")
        elif guidance.publishable_project is True:
            hints.append("public intent: publishable project")
    return hints


def _storage_payload(host: Host, *, max_items: int) -> list[dict[str, Any]]:
    rows = []
    for item in host.storage_facts[:max_items]:
        rows.append(
            {
                "storage_name": item.storage_name,
                "mount_point": item.mount_point,
                "storage_type": item.storage_type,
                "total_gib": _bytes_to_gib(item.total_bytes),
                "used_gib": _bytes_to_gib(item.used_bytes),
                "available_gib": _bytes_to_gib(item.available_bytes),
                "notes": item.notes,
            }
        )
    return rows


def _service_payload(host: Host, *, max_items: int) -> list[dict[str, Any]]:
    return [
        {
            "service_name": service.service_name,
            "status": service.status,
            "description": service.description,
            "role_guess": service.role_guess,
        }
        for service in host.service_inventory[:max_items]
    ]


def _guest_payload(host: Host, *, max_items: int) -> list[dict[str, Any]]:
    return [
        {
            "guest_id": item.guest_id,
            "guest_type": item.guest_type,
            "name": item.name,
            "status": item.status,
            "cores": item.cores,
            "ram_mb": item.ram_mb,
            "disk_summary": item.disk_summary,
            "role_guess": item.role_guess,
            "ip": item.ip,
            "config_path": item.config_path,
        }
        for item in host.guest_inventory[:max_items]
    ]


def _topology_payload(host: Host, *, max_items: int) -> dict[str, Any]:
    context = build_host_topology_context(host)

    def _guest_row_payload(guest: Any) -> dict[str, Any]:
        return {
            "guest_id": guest.guest_id,
            "guest_type": guest.guest_type,
            "name": guest.name,
            "status": guest.status,
            "cores": guest.cores,
            "ram_mb": guest.ram_mb,
            "disk_summary": guest.disk_summary,
            "role_guess": guest.role_guess,
            "ip": guest.ip,
            "config_path": guest.config_path,
            "linked_host_id": guest.linked_host_id,
            "linked_host_name": guest.linked_host.name if guest.linked_host else None,
            "management_state": guest.management_state,
        }

    return {
        "parent": context.get("parent"),
        "guest_counts": context.get("guest_counts") or {},
        "enrolled_guests": [_guest_row_payload(guest) for guest in context.get("enrolled_guests", [])[:max_items]],
        "discovered_guests": [_guest_row_payload(guest) for guest in context.get("discovered_guests", [])[:max_items]],
        "unmanaged_guests": [_guest_row_payload(guest) for guest in context.get("unmanaged_guests", [])[:max_items]],
    }


def _topology_evidence(topology: dict[str, Any]) -> list[str]:
    evidence: list[str] = []
    parent = topology.get("parent") or {}
    if parent.get("name"):
        guest_label = (parent.get("guest_type") or "guest").upper()
        guest_id = parent.get("guest_id")
        suffix = f" {guest_id}" if guest_id else ""
        evidence.append(
            f"Linked to parent host {parent['name']} as {guest_label}{suffix}."
        )

    guest_counts = topology.get("guest_counts") or {}
    enrolled_count = int(guest_counts.get("enrolled") or 0)
    discovered_count = int(guest_counts.get("discovered") or 0)
    unmanaged_count = int(guest_counts.get("unmanaged") or 0)
    total_count = enrolled_count + discovered_count
    if total_count:
        evidence.append(
            f"Topology inventory currently shows {enrolled_count} enrolled guest(s) and {discovered_count} discovered-only guest(s)."
        )
    if unmanaged_count:
        evidence.append(f"Topology inventory also has {unmanaged_count} unmanaged guest(s).")
    return evidence


def _enrichment_packet(result: dict[str, Any] | None) -> dict[str, Any]:
    if not result:
        return {"enabled": False, "summary": None, "evidence": []}
    return {
        "enabled": bool(result.get("enabled")),
        "summary": result.get("summary"),
        "evidence": [
            {
                "title": item.get("title"),
                "evidence_text": item.get("evidence_text"),
                "source_url": item.get("source_url"),
                "relevance": item.get("relevance"),
            }
            for item in (result.get("evidence") or [])
        ],
    }


def _safe_int(value: Any) -> int | None:
    if value in {None, ""}:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _parse_proxmox_config_text(content: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in content.splitlines():
        key, _, value = line.partition(":")
        key = key.strip().lower()
        if key:
            parsed[key] = value.strip()
    return parsed


def _summarize_guest_disk(config: dict[str, str]) -> str | None:
    disk_fragments: list[str] = []
    for key, value in config.items():
        if key.startswith(("scsi", "sata", "virtio", "ide", "rootfs", "mp")):
            size_match = re.search(r"size=([^,]+)", value)
            if size_match:
                disk_fragments.append(f"{key}:{size_match.group(1)}")
            else:
                disk_fragments.append(f"{key}:{value.split(',')[0]}")
    if not disk_fragments:
        return None
    return ", ".join(disk_fragments[:4])


def _extract_guest_ip(config: dict[str, str]) -> str | None:
    for value in config.values():
        ip_match = re.search(r"\bip=([^,]+)", value)
        if ip_match:
            ip = ip_match.group(1)
            if ip and ip.lower() != "dhcp":
                return ip
    return None


def _guess_guest_role(name: str, config_text: str) -> str | None:
    lowered = f"{name}\n{config_text}".lower()
    mapping = {
        "mcp": "AI control or gateway workload",
        "golem": "Golem project workload",
        "provider": "provider node",
        "cloud": "networking or edge service",
        "db": "database workload",
        "postgres": "database workload",
        "mysql": "database workload",
        "redis": "cache or queue workload",
        "media": "media workload",
        "plex": "media workload",
        "jellyfin": "media workload",
        "git": "code or SCM workload",
        "backup": "backup or maintenance workload",
        "vpn": "network gateway workload",
        "proxy": "proxy workload",
        "web": "web workload",
        "ai": "AI or model workload",
    }
    for token, role in mapping.items():
        if token in lowered:
            return role
    return None


def _sort_guest_key(item: dict[str, Any]) -> tuple[str, int | str]:
    guest_id = str(item.get("guest_id") or "")
    return item.get("guest_type") or "unknown", int(guest_id) if guest_id.isdigit() else guest_id


def _synthetic_proxmox_guest_inventory(project: Project, *, max_items: int) -> list[dict[str, Any]]:
    metadata = project.metadata_json or {}
    proxmox_inventory = metadata.get("proxmox_inventory") or {}
    guests: dict[tuple[str, str], dict[str, Any]] = {}

    for file_record in project.files:
        if file_record.entry_type != "file" or not file_record.path.endswith(".conf"):
            continue
        guest_type: str | None = None
        if "/qemu-server/" in file_record.path:
            guest_type = "qemu"
        elif "/lxc/" in file_record.path:
            guest_type = "lxc"
        if guest_type is None:
            continue

        guest_id = PurePosixPath(file_record.path).stem
        if not guest_id:
            continue

        content = file_record.content_sample or ""
        config = _parse_proxmox_config_text(content)
        name = config.get("name") if guest_type == "qemu" else config.get("hostname")
        role_guess = _guess_guest_role(name or guest_id, content)
        guests[(guest_type, guest_id)] = {
            "guest_id": guest_id,
            "guest_type": guest_type,
            "name": name,
            "status": None,
            "cores": _safe_int(config.get("cores")),
            "ram_mb": _safe_int(config.get("memory")),
            "disk_summary": _summarize_guest_disk(config),
            "role_guess": role_guess,
            "ip": _extract_guest_ip(config),
            "config_path": file_record.path,
        }

    for guest_id in proxmox_inventory.get("qemu_vm_ids") or []:
        guests.setdefault(
            ("qemu", str(guest_id)),
            {
                "guest_id": str(guest_id),
                "guest_type": "qemu",
                "name": None,
                "status": None,
                "cores": None,
                "ram_mb": None,
                "disk_summary": None,
                "role_guess": None,
                "ip": None,
                "config_path": f"/etc/pve/qemu-server/{guest_id}.conf",
            },
        )
    for guest_id in proxmox_inventory.get("lxc_container_ids") or []:
        guests.setdefault(
            ("lxc", str(guest_id)),
            {
                "guest_id": str(guest_id),
                "guest_type": "lxc",
                "name": None,
                "status": None,
                "cores": None,
                "ram_mb": None,
                "disk_summary": None,
                "role_guess": None,
                "ip": None,
                "config_path": f"/etc/pve/lxc/{guest_id}.conf",
            },
        )

    return sorted(guests.values(), key=_sort_guest_key)[:max_items]


def _node_scoped_proxmox_guest_keys(project: Project, fallback_inventory: list[dict[str, Any]]) -> set[tuple[str, str]]:
    metadata = project.metadata_json or {}
    proxmox_inventory = metadata.get("proxmox_inventory") or {}
    keys: set[tuple[str, str]] = set()
    for guest_id in proxmox_inventory.get("qemu_vm_ids") or []:
        keys.add(("qemu", str(guest_id)))
    for guest_id in proxmox_inventory.get("lxc_container_ids") or []:
        keys.add(("lxc", str(guest_id)))
    if not keys:
        for item in fallback_inventory:
            keys.add((str(item.get("guest_type") or "unknown"), str(item.get("guest_id") or "unknown")))
    return keys


def _merge_guest_inventory(
    direct_inventory: list[dict[str, Any]],
    fallback_inventory: list[dict[str, Any]],
    *,
    max_items: int,
    allowed_keys: set[tuple[str, str]] | None = None,
) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str], dict[str, Any]] = {}

    for item in fallback_inventory:
        key = (str(item.get("guest_type") or "unknown"), str(item.get("guest_id") or "unknown"))
        if allowed_keys and key not in allowed_keys:
            continue
        merged[key] = dict(item)

    for item in direct_inventory:
        key = (str(item.get("guest_type") or "unknown"), str(item.get("guest_id") or "unknown"))
        if allowed_keys and key not in allowed_keys:
            continue
        record = merged.setdefault(key, {})
        for field, value in item.items():
            if value is not None and value != "" and value != []:
                record[field] = value
        record.setdefault("guest_id", key[1])
        record.setdefault("guest_type", key[0])

    return sorted(merged.values(), key=_sort_guest_key)[:max_items]


def _dominant_project_name(project: Project) -> str:
    metadata = project.metadata_json or {}
    inventory_summary = metadata.get("inventory_summary") or {}
    observed_names = inventory_summary.get("observed_names") or []
    for name in observed_names:
        cleaned = _clean_text(name).lower()
        if cleaned and cleaned != project.host.name.lower():
            return cleaned
    return _clean_text(project.name_guess) or project.host.name


def _build_machine_profile_seed(
    project: Project,
    profile_settings: ProfileSettingsPayload,
) -> tuple[MachineIdentityResolution, dict[str, Any]]:
    host = project.host
    metadata = project.metadata_json or {}
    host_facts = _host_fact_payload(host)
    hypervisor_preferences = hypervisor_instruction_preferences(project)
    guest_limit = profile_settings.max_guests_shown
    if metadata.get("proxmox_inventory") and hypervisor_preferences["include_all_guests"]:
        proxmox_inventory = metadata.get("proxmox_inventory") or {}
        configured_total = len(proxmox_inventory.get("qemu_vm_ids") or []) + len(proxmox_inventory.get("lxc_container_ids") or [])
        guest_limit = max(guest_limit, configured_total, len(host.guest_inventory or []))
    fallback_inventory = _synthetic_proxmox_guest_inventory(project, max_items=guest_limit)
    guest_inventory = _merge_guest_inventory(
        _guest_payload(host, max_items=guest_limit),
        fallback_inventory,
        max_items=guest_limit,
        allowed_keys=_node_scoped_proxmox_guest_keys(project, fallback_inventory) if metadata.get("proxmox_inventory") else None,
    )
    service_inventory = _service_payload(host, max_items=profile_settings.max_services_shown)
    topology = _topology_payload(host, max_items=guest_limit)
    identity = resolve_machine_identity(
        host_name=host.name,
        metadata=metadata,
        host_facts=host_facts,
        guest_inventory=guest_inventory,
        service_inventory=service_inventory,
        topology=topology,
        user_hints=_identity_user_hints(project),
    )
    return identity, {
        "host_facts": host_facts,
        "guest_inventory": guest_inventory,
        "service_inventory": service_inventory,
        "storage_summary": _storage_payload(host, max_items=guest_limit),
        "topology": topology,
        "topology_evidence": _topology_evidence(topology),
    }


def _build_host_resources(identity: MachineIdentityResolution, host_facts: dict[str, Any], storage_summary: list[dict[str, Any]]) -> dict[str, Any]:
    resources = {
        "cpu_cores": host_facts.get("cpu_cores"),
        "cpu_threads": host_facts.get("cpu_threads"),
        "memory_gib": host_facts.get("memory_gib"),
        "swap_gib": host_facts.get("swap_gib"),
        "os": host_facts.get("os_pretty_name"),
        "kernel": host_facts.get("kernel"),
        "management_ip": host_facts.get("management_ip") or host_facts.get("address"),
        "uptime_seconds": host_facts.get("uptime_seconds"),
        "platform": identity.detected_platform,
        "storage": storage_summary,
    }
    if identity.machine_kind != "hypervisor":
        resources["root_disk_gib"] = host_facts.get("root_size_gib")
        resources["root_used_gib"] = host_facts.get("root_used_gib")
        resources["virtualization"] = host_facts.get("virtualization")
    return resources


def _detected_components(project: Project, service_inventory: list[dict[str, Any]], *, max_items: int) -> list[dict[str, Any]]:
    metadata = project.metadata_json or {}
    components: list[dict[str, Any]] = []
    for marker in project.markers[:max_items]:
        components.append({"kind": "marker", "name": marker.marker_name, "path": marker.marker_path})
    for item in (metadata.get("child_projects") or [])[:max_items]:
        components.append(
            {
                "kind": item.get("project_type") or "contained-setup",
                "name": item.get("name"),
                "path": item.get("path"),
                "role": ", ".join(item.get("marker_names") or []),
            }
        )
    for service in service_inventory[:max_items]:
        components.append(
            {
                "kind": "service",
                "name": service.get("service_name"),
                "path": "",
                "role": service.get("role_guess") or service.get("description"),
            }
        )
    return components[:max_items]


def _deterministic_narrative(
    project: Project,
    identity: MachineIdentityResolution,
    *,
    host_facts: dict[str, Any],
    guest_inventory: list[dict[str, Any]],
    service_inventory: list[dict[str, Any]],
    topology: dict[str, Any],
    profile_settings: ProfileSettingsPayload,
) -> dict[str, Any]:
    metadata = project.metadata_json or {}
    dominant_name = _dominant_project_name(project)
    child_projects = metadata.get("child_projects") or []
    setup_items: list[str] = []
    why_items: list[str] = []
    notes: list[str] = []
    unknowns: list[str] = []

    if host_facts.get("fqdn"):
        setup_items.append(f"My inventory also sees the FQDN {host_facts.get('fqdn')}.")
    if metadata.get("grouped_roots"):
        setup_items.append(f"I am scanning these allowlisted roots on this machine: {', '.join(metadata.get('grouped_roots', [])[:6])}.")
    if service_inventory:
        top_services = ", ".join(service["service_name"] for service in service_inventory[:6] if service.get("service_name"))
        setup_items.append(f"Observed running services include {top_services}.")
    parent = topology.get("parent") or {}
    if parent.get("name"):
        guest_label = "VM" if parent.get("guest_type") == "qemu" else str(parent.get("guest_type") or "guest").upper()
        guest_suffix = f" {parent.get('guest_id')}" if parent.get("guest_id") else ""
        setup_items.append(
            f"This machine is linked to my parent host {parent['name']} as {guest_label}{guest_suffix}."
        )

    if identity.primary_signature_id == "proxmox_hypervisor" or identity.story_template_type == "proxmox_hypervisor":
        lxc_count = sum(1 for item in guest_inventory if item.get("guest_type") == "lxc")
        vm_count = sum(1 for item in guest_inventory if item.get("guest_type") == "qemu")
        lead = (
            f"This is my Proxmox host called {project.host.name}. It acts as a virtualization node in my lab and hosts "
            f"{lxc_count} LXC container(s) and {vm_count} VM(s) for my projects and infrastructure."
        )
        project_role_summary = "My main role for this machine is virtualization and guest management inside the lab."
        if guest_inventory:
            setup_items.append("I can see guest definitions directly from the Proxmox inventory and config data.")
        if host_facts.get("proxmox_version"):
            setup_items.append(f"The observed Proxmox version is {host_facts.get('proxmox_version')}.")
        guest_counts = topology.get("guest_counts") or {}
        if guest_counts.get("enrolled") or guest_counts.get("discovered"):
            setup_items.append(
                f"My topology inventory currently tracks {guest_counts.get('enrolled', 0)} enrolled guest(s) and {guest_counts.get('discovered', 0)} discovered-only guest(s) here."
            )
        why_items.append("It holds a large share of the workloads that the rest of my lab depends on.")
        why_items.append("Changes on this machine can affect multiple VMs and containers at once.")
    elif identity.primary_signature_id == "wazuh_stack":
        lead = (
            "This VM is for my Wazuh monitoring and security stack. It appears to host the core services I use for "
            "security visibility, log handling, indexing, or dashboard access in my environment."
        )
        project_role_summary = "The likely role for this machine is security monitoring and SIEM-related work inside my lab."
        why_items.append("It looks like part of the security and monitoring layer for the rest of my environment.")
        why_items.append("I should treat changes here carefully because they can affect visibility into other machines.")
    elif identity.primary_signature_id == "mcp_gateway" or identity.story_template_type == "service_gateway":
        lead = (
            f"This VM is the control layer for my AI environment on {project.host.name}. "
            "It appears to handle service or tooling functions rather than acting as a general project workspace."
        )
        project_role_summary = f"The likely role for this machine is {identity.detected_role}."
        why_items.append("It sits between other systems in my lab and the services or tools they depend on.")
        why_items.append("If this machine changes, the surrounding control flow in my environment may change with it.")
    elif identity.primary_signature_id == "golem_provider":
        lead = (
            "This VM is for my golem project. It appears to run the provider-side components and runtime services tied to that workload."
        )
        project_role_summary = "The likely role for this machine is a Golem provider or workload VM in my lab."
        why_items.append("It looks like one of the main workload machines tied to my Golem setup.")
        why_items.append("Changes here could affect provider-side runtime behavior or project capacity.")
    elif identity.story_template_type == "project_vm":
        lead = (
            f"This VM is for my {dominant_name} project. It appears to be the main workload node for that environment and "
            "contains the application files, runtime components, and supporting services tied to it."
        )
        project_role_summary = f"The likely role for this machine is {identity.detected_role}."
        if child_projects:
            setup_items.append(f"I can see {len(child_projects)} contained setup summary item(s) inside this host profile.")
    else:
        lead = (
            f"This is my Linux node called {project.host.name}. Based on the observed facts, it looks like a "
            f"{identity.detected_role}."
        )
        project_role_summary = f"The likely role for this machine is {identity.detected_role}."

    if not host_facts.get("memory_gib"):
        unknowns.append("RAM size was not available from the scanned host facts.")
    if not host_facts.get("cpu_threads") and not host_facts.get("cpu_cores"):
        unknowns.append("CPU sizing details were not available from the scanned host facts.")
    if identity.identity_confidence < 0.7:
        notes.append("Human verification recommended before treating this profile as fully settled.")
    if not guest_inventory and identity.story_template_type == "proxmox_hypervisor":
        unknowns.append("Guest inventory was not available even though Proxmox indicators were present.")
    if identity.primary_signature_name:
        notes.append(f"Deterministic identity resolved this machine as {identity.primary_signature_name}.")
    if identity.ambiguity_notes:
        unknowns.extend(item for item in identity.ambiguity_notes if item not in unknowns)

    return {
        "lead_paragraph": lead,
        "project_role_summary": project_role_summary,
        "setup_summary_items": setup_items[: profile_settings.max_setup_bullets_shown],
        "why_it_matters_items": why_items[: profile_settings.max_setup_bullets_shown],
        "notes_items": notes[: profile_settings.max_setup_bullets_shown],
        "unknowns_items": unknowns[: profile_settings.max_setup_bullets_shown],
    }


def _build_profile_packet(
    project: Project,
    identity: MachineIdentityResolution,
    *,
    host_facts: dict[str, Any],
    storage_summary: list[dict[str, Any]],
    guest_inventory: list[dict[str, Any]],
    service_inventory: list[dict[str, Any]],
    topology: dict[str, Any],
    topology_evidence: list[str],
    deterministic_narrative: dict[str, Any],
    mcp_enrichment: dict[str, Any] | None = None,
    web_search_enrichment: dict[str, Any] | None = None,
    mapping_preview: dict[str, Any] | None = None,
    research_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = project.metadata_json or {}
    operator_context = metadata.get("operator_context") or {}
    return {
        "machine_name": project.host.name,
        "machine_kind": identity.machine_kind,
        "story_template_type": identity.story_template_type,
        "detected_platform": identity.detected_platform,
        "detected_role": identity.detected_role,
        "identity_result": {
            "primary_signature_id": identity.primary_signature_id,
            "primary_signature_name": identity.primary_signature_name,
            "machine_kind": identity.machine_kind,
            "detected_platform": identity.detected_platform,
            "detected_role": identity.detected_role,
            "stack_tags": identity.stack_tags,
            "identity_confidence": identity.identity_confidence,
            "identity_evidence": identity.identity_evidence,
            "alternate_matches": identity.alternate_matches,
            "ambiguity_notes": identity.ambiguity_notes,
            "local_facts_evidence": identity.local_facts_evidence,
            "topology_evidence": identity.topology_evidence,
        },
        "host_facts": host_facts,
        "storage_summary": storage_summary,
        "guest_inventory": guest_inventory,
        "service_inventory": service_inventory,
        "child_projects": metadata.get("child_projects") or [],
        "inventory_summary": metadata.get("inventory_summary") or {},
        "project_counts": metadata.get("project_counts") or {},
        "marker_names": metadata.get("marker_names") or [],
        "grouped_roots": metadata.get("grouped_roots") or [],
        "operator_context": operator_context,
        "deterministic_narrative": deterministic_narrative,
        "topology_facts": topology,
        "topology_evidence": topology_evidence,
        "local_evidence": identity.local_facts_evidence,
        "signature_evidence": identity.identity_evidence,
        "research_context": research_context or {},
        "mcp_enrichment": _enrichment_packet(mcp_enrichment),
        "web_search_enrichment": _enrichment_packet(web_search_enrichment),
        "project_mapping_preview": mapping_preview or {},
    }


def _normalize_narrative_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    for text_field in ("lead_paragraph", "project_role_summary"):
        value = normalized.get(text_field)
        if value is None:
            normalized[text_field] = ""
        elif not isinstance(value, str):
            normalized[text_field] = str(value)
    for list_field in ("setup_summary_items", "why_it_matters_items", "notes_items", "unknowns_items"):
        value = normalized.get(list_field)
        if isinstance(value, str):
            normalized[list_field] = [value]
        elif isinstance(value, list):
            normalized[list_field] = [str(item).strip() for item in value if str(item).strip()]
        else:
            normalized[list_field] = []
    return normalized


def _format_with_lmstudio(
    session: Session,
    config: AppConfig,
    *,
    project: Project,
    profile_packet: dict[str, Any],
    max_tokens: int | None,
    stream_callback: Callable[[str], None] | None,
) -> tuple[MachineProfileNarrativeResult | None, str | None]:
    lm_settings = get_settings(session, config, LMSTUDIO_SETTINGS_KEY, LMStudioSettingsPayload)
    if not lm_settings.enabled or not lm_settings.selected_model:
        return None, None

    client = _lmstudio_client(config, lm_settings)
    payload = {
        "model": lm_settings.selected_model,
        "temperature": lm_settings.temperature,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": (
                    "You format machine profile narratives for an internal homelab inventory. "
                    "Use first-person ownership language with 'my'. Never use 'your'. Never rename the machine. "
                "Do not invent cores, RAM, storage, guests, services, or roles. "
                "Only narrate facts already supplied. If a field is missing, say less rather than guessing. "
                "Treat local facts and topology facts as the source of truth. "
                "Treat the supplied deterministic identity result as authoritative for the machine identity. "
                "Treat MCP and web-search context as optional supplemental context only. "
                "If supplemental context conflicts with local facts, follow local facts. "
                "Keep the lead paragraph concise, practical, and grounded. "
                "Do not use marketing language or fluffy phrasing. "
                "Return JSON only."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Format this machine profile packet into narrative fields. "
                    "Return JSON with these keys: lead_paragraph, project_role_summary, setup_summary_items, "
                    "why_it_matters_items, notes_items, unknowns_items.\n\n"
                    "Additional instructions:\n"
                    "- Use 'my', never 'your'.\n"
                    "- Keep the wording practical, like an internal CMDB or lab inventory note written back to me.\n"
                    "- Do not over-brand or embellish names.\n"
                    "- Reuse the deterministic facts and evidence already provided.\n"
                    "- Use the deterministic signature result to describe what this machine is for when it is specific enough.\n"
                    "- If the identity result points to Wazuh, describe it as my Wazuh monitoring or security stack rather than a generic node.\n"
                    "- Clearly respect the distinction between local facts, topology facts, MCP enrichment, and web-search enrichment.\n"
                    "- Do not let enrichment evidence override directly observed machine facts.\n\n"
                    f"{json.dumps(profile_packet, indent=2, sort_keys=True)}"
                ),
            },
        ],
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens

    if stream_callback:
        try:
            content = client._stream_chat_completion(payload, allow_plain_fallback=True, chunk_callback=stream_callback)
        except Exception:
            response_payload = client._post_chat_completion(payload, allow_plain_fallback=True)
            content = client._extract_message_content(response_payload)
            stream_callback(content)
    else:
        response_payload = client._post_chat_completion(payload, allow_plain_fallback=True)
        content = client._extract_message_content(response_payload)

    parsed = client._extract_json_content(content)
    narrative = MachineProfileNarrativeResult.model_validate(_normalize_narrative_payload(parsed))
    _record_lm_log(
        session,
        "format_machine_profile",
        "success",
        f"Generated machine profile narrative for {project.path}.",
        {"project_id": project.id, "machine_name": project.host.name},
    )
    return narrative, lm_settings.selected_model


def _persist_machine_identity(session: Session, project: Project, identity: MachineIdentityResolution) -> MachineIdentityResult:
    record = project.identity_result
    if record is None:
        record = MachineIdentityResult(project_id=project.id, host_id=project.host.id)
        session.add(record)

    record.host_id = project.host.id
    record.primary_signature_id = identity.primary_signature_id
    record.primary_signature_name = identity.primary_signature_name
    record.machine_kind = identity.machine_kind
    record.story_template_type = identity.story_template_type
    record.detected_platform = identity.detected_platform
    record.detected_role = identity.detected_role
    record.stack_tags_json = identity.stack_tags
    record.identity_confidence = identity.identity_confidence
    record.identity_evidence_json = identity.identity_evidence
    record.alternate_matches_json = identity.alternate_matches
    record.ambiguity_notes_json = identity.ambiguity_notes
    record.local_facts_evidence_json = identity.local_facts_evidence
    record.topology_evidence_json = identity.topology_evidence
    record.last_resolved_at = utcnow()

    for row in list(record.signature_matches):
        session.delete(row)
    session.flush()
    for rank, match in enumerate(identity.signature_matches, start=1):
        session.add(
            SignatureMatch(
                identity_result_id=record.id,
                project_id=project.id,
                host_id=project.host.id,
                signature_id=match.signature_id,
                display_name=match.display_name,
                category=match.category,
                rank=rank,
                confidence=match.confidence,
                machine_kind=match.machine_kind,
                detected_platform=match.detected_platform,
                detected_role=match.detected_role,
                project_mapping_hint=match.project_mapping_hint,
                default_publishability=match.default_publishability,
                evidence_json=match.evidence,
                required_matches_json=match.required_matches,
                optional_matches_json=match.optional_matches,
                forbidden_matches_json=match.forbidden_matches,
                ambiguity_notes_json=match.ambiguity_notes,
            )
        )
    session.flush()
    return record


def refresh_machine_identity(
    session: Session,
    config: AppConfig,
    project_id: int,
) -> MachineIdentityResult:
    profile_settings = get_settings(session, config, PROFILE_SETTINGS_KEY, ProfileSettingsPayload)
    project = session.execute(
        select(Project)
        .where(Project.id == project_id)
        .options(
            selectinload(Project.host).selectinload(Host.fact_snapshot),
            selectinload(Project.host).selectinload(Host.storage_facts),
            selectinload(Project.host).selectinload(Host.guest_inventory),
            selectinload(Project.host).selectinload(Host.service_inventory),
            selectinload(Project.host).selectinload(Host.parent_host),
            selectinload(Project.host).selectinload(Host.profile_override),
            selectinload(Project.files),
            selectinload(Project.markers),
            selectinload(Project.identity_result).selectinload(MachineIdentityResult.signature_matches),
            selectinload(Project.clarification_guidance),
            selectinload(Project.clarification_threads),
        )
    ).scalar_one()
    identity, _collected = _build_machine_profile_seed(project, profile_settings)
    record = _persist_machine_identity(session, project, identity)
    mark_project_clarifications_used(session, project)
    session.flush()
    return record


def refresh_project_mapping_preview(
    session: Session,
    config: AppConfig,
    project_id: int,
):
    project = session.execute(
        select(Project)
        .where(Project.id == project_id)
        .options(
            selectinload(Project.host).selectinload(Host.profile_override),
            selectinload(Project.machine_profile),
            selectinload(Project.identity_result),
            selectinload(Project.clarification_guidance),
            selectinload(Project.clarification_threads),
        )
    ).scalar_one()
    preview = build_project_mapping_preview(
        session,
        project=project,
        identity=project.identity_result,
        profile=project.machine_profile,
    )
    mark_project_clarifications_used(session, project)
    session.flush()
    return preview


def refresh_project_research(
    session: Session,
    config: AppConfig,
    project_id: int,
    *,
    force: bool = True,
) -> ResearchOutcome:
    profile_settings = get_settings(session, config, PROFILE_SETTINGS_KEY, ProfileSettingsPayload)
    project = session.execute(
        select(Project)
        .where(Project.id == project_id)
        .options(
            selectinload(Project.host).selectinload(Host.fact_snapshot),
            selectinload(Project.host).selectinload(Host.storage_facts),
            selectinload(Project.host).selectinload(Host.guest_inventory),
            selectinload(Project.host).selectinload(Host.service_inventory),
            selectinload(Project.host).selectinload(Host.parent_host),
            selectinload(Project.host).selectinload(Host.profile_override),
            selectinload(Project.files),
            selectinload(Project.markers),
            selectinload(Project.mapping_preview),
            selectinload(Project.clarification_guidance),
            selectinload(Project.clarification_threads),
        )
    ).scalar_one()
    identity, collected = _build_machine_profile_seed(project, profile_settings)
    identity_record = _persist_machine_identity(session, project, identity)
    base_profile_packet = _build_profile_packet(
        project,
        identity,
        host_facts=collected["host_facts"],
        storage_summary=collected["storage_summary"],
        guest_inventory=collected["guest_inventory"],
        service_inventory=collected["service_inventory"],
        topology=collected["topology"],
        topology_evidence=collected["topology_evidence"],
        deterministic_narrative=_deterministic_narrative(
            project,
            identity,
            host_facts=collected["host_facts"],
            guest_inventory=collected["guest_inventory"],
            service_inventory=collected["service_inventory"],
            topology=collected["topology"],
            profile_settings=profile_settings,
        ),
    )
    outcome = run_identity_research(
        session,
        config,
        project=project,
        identity=identity,
        collected=collected,
        base_profile_packet=base_profile_packet,
        title_candidate=project.mapping_preview.project_title_candidate if project.mapping_preview else None,
        summary_candidate=project.mapping_preview.project_summary_candidate if project.mapping_preview else None,
        include_external=True,
        force=force,
    )
    build_project_mapping_preview(
        session,
        project=project,
        identity=identity_record,
        profile=project.machine_profile,
        research_outcome=outcome,
    )
    mark_project_clarifications_used(session, project)
    session.flush()
    return outcome


def generate_machine_profile(
    session: Session,
    config: AppConfig,
    project_id: int,
    *,
    stream_callback: Callable[[str], None] | None = None,
    include_enrichment: bool = True,
) -> MachineProfile:
    profile_settings = get_settings(session, config, PROFILE_SETTINGS_KEY, ProfileSettingsPayload)
    project = session.execute(
        select(Project)
        .where(Project.id == project_id)
        .options(
            selectinload(Project.host).selectinload(Host.fact_snapshot),
            selectinload(Project.host).selectinload(Host.storage_facts),
            selectinload(Project.host).selectinload(Host.guest_inventory),
            selectinload(Project.host).selectinload(Host.service_inventory),
            selectinload(Project.host).selectinload(Host.parent_host),
            selectinload(Project.host).selectinload(Host.profile_override),
            selectinload(Project.files),
            selectinload(Project.markers),
            selectinload(Project.enrichment_evidence),
            selectinload(Project.identity_result).selectinload(MachineIdentityResult.signature_matches),
            selectinload(Project.mapping_preview),
            selectinload(Project.machine_profile),
        )
    ).scalar_one()

    if project.host is None:
        raise RuntimeError("Project host is missing.")
    if not profile_settings.enabled:
        raise RuntimeError("Machine profiles are disabled.")

    identity, collected = _build_machine_profile_seed(project, profile_settings)
    identity_record = _persist_machine_identity(session, project, identity)
    deterministic_narrative = _deterministic_narrative(
        project,
        identity,
        host_facts=collected["host_facts"],
        guest_inventory=collected["guest_inventory"],
        service_inventory=collected["service_inventory"],
        topology=collected["topology"],
        profile_settings=profile_settings,
    )

    lead_paragraph = deterministic_narrative["lead_paragraph"]
    project_role_summary = deterministic_narrative["project_role_summary"]
    setup_summary_items = deterministic_narrative["setup_summary_items"]
    why_it_matters_items = deterministic_narrative["why_it_matters_items"]
    notes_items = deterministic_narrative["notes_items"]
    unknowns_items = deterministic_narrative["unknowns_items"]
    llm_model_used: str | None = None

    lm_settings = get_settings(session, config, LMSTUDIO_SETTINGS_KEY, LMStudioSettingsPayload)
    max_tokens = None if lm_settings.use_max_output_tokens else lm_settings.max_tokens
    base_profile_packet = _build_profile_packet(
        project,
        identity,
        host_facts=collected["host_facts"],
        storage_summary=collected["storage_summary"],
        guest_inventory=collected["guest_inventory"],
        service_inventory=collected["service_inventory"],
        topology=collected["topology"],
        topology_evidence=collected["topology_evidence"],
        deterministic_narrative=deterministic_narrative,
    )
    research_outcome = run_identity_research(
        session,
        config,
        project=project,
        identity=identity,
        collected=collected,
        base_profile_packet=base_profile_packet,
        title_candidate=project.mapping_preview.project_title_candidate if project.mapping_preview else None,
        summary_candidate=project.mapping_preview.project_summary_candidate if project.mapping_preview else None,
        include_external=include_enrichment,
        force=False,
        stream_callback=stream_callback,
    )
    mcp_enrichment: dict[str, Any] | None = research_outcome.mcp_enrichment
    web_search_enrichment: dict[str, Any] | None = research_outcome.web_search_enrichment
    mapping_preview = build_project_mapping_preview(
        session,
        project=project,
        identity=identity_record,
        profile=project.machine_profile,
        research_outcome=research_outcome,
    )
    profile_packet = _build_profile_packet(
        project,
        identity,
        host_facts=collected["host_facts"],
        storage_summary=collected["storage_summary"],
        guest_inventory=collected["guest_inventory"],
        service_inventory=collected["service_inventory"],
        topology=collected["topology"],
        topology_evidence=collected["topology_evidence"],
        deterministic_narrative=deterministic_narrative,
        mcp_enrichment=mcp_enrichment,
        web_search_enrichment=web_search_enrichment,
        mapping_preview={
            "project_title_candidate": mapping_preview.project_title_candidate,
            "project_summary_candidate": mapping_preview.project_summary_candidate,
            "card_title_candidate": mapping_preview.card_title_candidate,
            "card_summary_candidate": mapping_preview.card_summary_candidate,
            "publish_title_candidate": mapping_preview.publish_title_candidate,
            "publish_summary_candidate": mapping_preview.publish_summary_candidate,
        },
        research_context=research_outcome.as_payload(),
    )
    if profile_settings.enable_lmstudio_formatting:
        try:
            narrative, llm_model_used = _format_with_lmstudio(
                session,
                config,
                project=project,
                profile_packet=profile_packet,
                max_tokens=max_tokens,
                stream_callback=stream_callback,
            )
            if narrative is not None:
                lead_paragraph = narrative.lead_paragraph or lead_paragraph
                project_role_summary = narrative.project_role_summary or project_role_summary
                setup_summary_items = narrative.setup_summary_items or setup_summary_items
                why_it_matters_items = narrative.why_it_matters_items or why_it_matters_items
                notes_items = narrative.notes_items or notes_items
                unknowns_items = narrative.unknowns_items or unknowns_items
        except Exception as exc:
            if not profile_settings.fallback_to_deterministic_profile:
                raise
            _record_lm_log(
                session,
                "format_machine_profile",
                "error",
                str(exc),
                {"project_id": project.id},
            )
            if stream_callback:
                stream_callback(f"\n[AICMS] LM Studio formatting failed; using deterministic machine profile content instead.\n")

    host_resources = _build_host_resources(identity, collected["host_facts"], collected["storage_summary"])
    components = _detected_components(
        project,
        collected["service_inventory"],
        max_items=max(profile_settings.max_services_shown, profile_settings.max_setup_bullets_shown),
    )
    hypervisor_preferences = hypervisor_instruction_preferences(project)
    workload_limit = len(collected["guest_inventory"]) if hypervisor_preferences["include_all_guests"] else profile_settings.max_guests_shown
    contained_workloads = {
        "lxcs": [item for item in collected["guest_inventory"] if item.get("guest_type") == "lxc"][:workload_limit],
        "vms": [item for item in collected["guest_inventory"] if item.get("guest_type") == "qemu"][:workload_limit],
        "services": collected["service_inventory"][: profile_settings.max_services_shown],
        "child_projects": (project.metadata_json or {}).get("child_projects", [])[:workload_limit],
        "topology": collected["topology"],
    }
    profile_role_json = {
        "primary_purpose": identity.detected_role,
        "environment_type": identity.story_template_type,
        "detected_stack": ", ".join(identity.stack_tags) or ", ".join(component.get("name") for component in components[:6] if component.get("kind") == "marker") or identity.detected_platform,
    }

    record = project.machine_profile
    if record is None:
        record = MachineProfile(project_id=project.id, host_id=project.host.id)
        session.add(record)

    record.host_id = project.host.id
    record.machine_kind = identity.machine_kind
    record.story_template_type = identity.story_template_type
    record.detected_platform = identity.detected_platform
    record.detected_role = identity.detected_role
    record.confidence = identity.identity_confidence
    record.lead_paragraph = lead_paragraph
    record.project_role_summary = project_role_summary
    record.host_resources_json = host_resources
    record.project_role_json = profile_role_json
    record.setup_details_json = setup_summary_items
    record.contained_workloads_json = contained_workloads
    record.detected_components_json = components
    record.why_it_matters_json = why_it_matters_items
    record.notes_json = notes_items
    record.unknowns_json = unknowns_items
    record.evidence_json = identity.identity_evidence
    record.last_generated_at = utcnow()
    record.llm_model_used = llm_model_used
    session.flush()
    mapping_preview = build_project_mapping_preview(
        session,
        project=project,
        identity=identity_record,
        profile=record,
        research_outcome=research_outcome if research_outcome.triggered else latest_research_outcome(session, project),
    )
    profile_packet["project_mapping_preview"] = {
        "project_title_candidate": mapping_preview.project_title_candidate,
        "project_summary_candidate": mapping_preview.project_summary_candidate,
        "tech_tags_candidate": mapping_preview.tech_tags_candidate_json or [],
        "card_title_candidate": mapping_preview.card_title_candidate,
        "card_summary_candidate": mapping_preview.card_summary_candidate,
        "publish_title_candidate": mapping_preview.publish_title_candidate,
        "publish_summary_candidate": mapping_preview.publish_summary_candidate,
        "publish_long_description_candidate": mapping_preview.publish_long_description_candidate,
        "publishable_kind": mapping_preview.publishable_kind,
        "recommended_status": mapping_preview.recommended_status,
        "publishability_reason": mapping_preview.publishability_reason,
        "internal_only_reason": mapping_preview.internal_only_reason,
    }
    return record


def refresh_host_facts(session: Session, config: AppConfig, host_id: int) -> HostFact:
    profile_settings = get_settings(session, config, PROFILE_SETTINGS_KEY, ProfileSettingsPayload)
    host = session.execute(
        select(Host)
        .where(Host.id == host_id)
        .options(
            selectinload(Host.roots),
            selectinload(Host.fact_snapshot),
            selectinload(Host.storage_facts),
            selectinload(Host.guest_inventory),
            selectinload(Host.service_inventory),
            selectinload(Host.projects),
        )
    ).scalar_one()
    executor = SSHExecutor(
        connect_timeout=config.ssh_connect_timeout_seconds,
        command_timeout=config.ssh_command_timeout_seconds,
    )
    client = executor.open_client(host)
    try:
        facts = collect_machine_facts(
            host=host,
            run_lines=lambda command, **kwargs: (
                [line.strip() for line in executor.run(client, command, timeout=kwargs.get("timeout") or executor.command_timeout)[1].splitlines() if line.strip()],
                None,
            ),
            run_command=lambda command, **kwargs: executor.run(
                client,
                command,
                timeout=kwargs.get("timeout") or executor.command_timeout,
            ),
            max_services=profile_settings.max_services_shown,
            max_guests=profile_settings.max_guests_shown,
        )
    finally:
        client.close()

    metadata = {}
    host_summary_project = next(
        (project for project in host.projects if (project.metadata_json or {}).get("setup_kind") == "host-summary"),
        None,
    )
    if host_summary_project is not None:
        metadata = dict(host_summary_project.metadata_json or {})

    classification = classify_machine(
        host_name=host.name,
        metadata=metadata,
        host_facts=facts,
        guest_inventory=facts.get("guest_inventory") or [],
        service_inventory=facts.get("selected_running_services") or [],
    )
    sync_host_fact_inventory(session, host=host, host_facts=facts, classification=classification, seen_at=utcnow())
    refresh_host_topology(session, host.id)
    host.connection_status = "reachable"
    host.last_seen_at = utcnow()
    session.flush()
    return host.fact_snapshot
