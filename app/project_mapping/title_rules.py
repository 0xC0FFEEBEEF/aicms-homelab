from __future__ import annotations

from app.models import Host, HostProfileOverride, MachineIdentityResult, Project, ProjectClarificationGuidance
from app.research.models import ResearchOutcome

GENERIC_SIGNATURE_IDS = {None, "generic_project_vm", "generic_service_node"}
GENERIC_TITLE_LABELS = {"Generic Project VM", "Generic Service Node", "Generic Linux Node", "Fallback unknown profile"}


def safe_host_title(host_name: str | None) -> str | None:
    name = (host_name or "").strip()
    if not name:
        return None
    lowered = name.lower()
    if lowered in {"vm", "node", "server", "linux"}:
        return None
    if lowered.startswith(("vm-", "node-", "srv-")):
        return None
    return name


def stable_identity_title(
    *,
    host: Host | None,
    project: Project | None,
    identity: MachineIdentityResult | None,
) -> str:
    host_title = safe_host_title(host.name if host else None)
    project_name = (project.name_guess or "").strip() if project else ""
    signature_id = identity.primary_signature_id if identity else None
    signature_name = identity.primary_signature_name if identity and identity.primary_signature_name else None

    if signature_id == "wazuh_stack":
        return "Wazuh Security Monitoring VM"
    if signature_id == "mcp_gateway":
        return "MCP Control Layer VM"
    if signature_id == "postgresql_database":
        return f"{host_title} PostgreSQL Database VM" if host_title else "PostgreSQL Database VM"
    if signature_id == "proxmox_hypervisor":
        return f"{host.name} Proxmox Hypervisor" if host and host.name else "Proxmox Hypervisor"
    if signature_id == "proxmox_backup_server":
        return f"{host.name} Proxmox Backup Server" if host and host.name else "Proxmox Backup Server"
    if signature_id == "golem_provider":
        return f"{host_title} Workload VM" if host_title else "golem Workload VM"
    if signature_id == "grafana":
        return "Grafana Observability VM"
    if signature_name and signature_name not in GENERIC_TITLE_LABELS and host_title:
        return f"{host_title} {signature_name}"
    if signature_id == "generic_project_vm":
        return f"{host_title} Workload VM" if host_title else "Unresolved Project VM"
    if signature_id == "generic_service_node":
        return f"{host_title} Service Workload" if host_title else "Unresolved Service Workload"
    if host_title:
        return host_title
    if project_name:
        return project_name
    return "Unresolved Machine"


def research_matches_identity(
    *,
    identity: MachineIdentityResult | None,
    research: ResearchOutcome | None,
) -> bool:
    if not research or not research.title_hint or not research.affected_title:
        return False
    if identity is None or identity.primary_signature_id in GENERIC_SIGNATURE_IDS:
        return True
    signature_id = identity.primary_signature_id
    if signature_id in {"proxmox_hypervisor", "proxmox_backup_server"}:
        return any(item.finding_kind == signature_id for item in research.findings)
    if any(item.finding_kind == signature_id for item in research.findings):
        return True

    lowered = research.title_hint.lower()
    expected_markers = {
        "wazuh_stack": ("wazuh",),
        "mcp_gateway": ("mcp",),
        "postgresql_database": ("postgres", "postgresql", "psql"),
        "golem_provider": ("golem",),
        "grafana": ("grafana",),
        "nextcloud": ("nextcloud",),
        "openwebui": ("openwebui", "open webui"),
    }
    markers = expected_markers.get(signature_id)
    if markers:
        return any(marker in lowered for marker in markers)
    return False


def resolved_public_title(
    *,
    host: Host,
    project: Project,
    identity: MachineIdentityResult | None,
    override: HostProfileOverride | None,
    guidance: ProjectClarificationGuidance | None,
    research: ResearchOutcome | None,
) -> str:
    if override and override.public_title_override:
        return override.public_title_override
    if guidance and guidance.preferred_public_title:
        return guidance.preferred_public_title
    if research_matches_identity(identity=identity, research=research):
        return research.title_hint  # type: ignore[return-value]
    return stable_identity_title(host=host, project=project, identity=identity)


def mapping_title_matches_identity(
    *,
    host: Host,
    project: Project,
    identity: MachineIdentityResult | None,
    candidate_title: str | None,
) -> bool:
    title = (candidate_title or "").strip()
    if not title:
        return False
    if identity is None or identity.primary_signature_id in GENERIC_SIGNATURE_IDS:
        return True

    expected = stable_identity_title(host=host, project=project, identity=identity).strip().lower()
    lowered = title.lower()
    if lowered == expected:
        return True

    signature_id = identity.primary_signature_id
    if signature_id == "proxmox_hypervisor":
        host_name = (host.name or "").strip().lower()
        return bool(host_name) and host_name in lowered and ("proxmox" in lowered or "hypervisor" in lowered)
    if signature_id == "proxmox_backup_server":
        host_name = (host.name or "").strip().lower()
        return bool(host_name) and host_name in lowered and "backup" in lowered
    if signature_id == "wazuh_stack":
        return "wazuh" in lowered
    if signature_id == "mcp_gateway":
        return "mcp" in lowered
    if signature_id == "postgresql_database":
        return any(marker in lowered for marker in ("postgres", "postgresql", "psql"))
    if signature_id == "golem_provider":
        host_name = (host.name or "").strip().lower()
        return "golem" in lowered or (bool(host_name) and host_name in lowered)
    if signature_id == "grafana":
        return "grafana" in lowered
    return True
