from __future__ import annotations

from app.models import MachineIdentityResult, MachineProfile, ProjectMappingPreview
from app.project_mapping.models import HostProfileOverridePayload
from app.project_mapping.service import build_project_mapping_preview, persist_host_profile_override
from app.research.models import ResearchFindingPayload, ResearchOutcome
from app.research.service import run_identity_research
from app.signatures.resolver import resolve_machine_identity
from app.utils.common import utcnow
from tests.conftest import create_host, create_project


def _host_summary_metadata(*, grouped_roots: list[str], observed_names: list[str], marker_names: list[str] | None = None) -> dict:
    return {
        "setup_kind": "host-summary",
        "grouped_roots": grouped_roots,
        "inventory_summary": {"observed_names": observed_names},
        "child_projects": [],
        "project_counts": {"total": 1, "git_repos": 0, "by_type": {"host": 1}},
        "marker_names": marker_names or [],
        "project_paths": grouped_roots,
    }


def test_signature_resolver_recognizes_wazuh_stack():
    identity = resolve_machine_identity(
        host_name="wazuh",
        metadata=_host_summary_metadata(grouped_roots=["/var/ossec", "/etc/filebeat"], observed_names=["wazuh"]),
        host_facts={"os_pretty_name": "Ubuntu Server 24.04"},
        guest_inventory=[],
        service_inventory=[
            {"service_name": "wazuh-manager.service"},
            {"service_name": "wazuh-indexer.service"},
            {"service_name": "filebeat.service"},
        ],
        topology=None,
        user_hints=[],
    )

    assert identity.primary_signature_id == "wazuh_stack"
    assert identity.machine_kind == "service_node"
    assert identity.detected_platform == "Wazuh stack"
    assert "security monitoring" in identity.detected_role
    assert "Wazuh" in identity.stack_tags
    assert "Filebeat" in identity.stack_tags


def test_signature_resolver_prefers_postgresql_over_wazuh_agent_like_signals():
    identity = resolve_machine_identity(
        host_name="psql",
        metadata=_host_summary_metadata(
            grouped_roots=["/var/lib/postgresql", "/etc/filebeat"],
            observed_names=["psql", "postgres"],
        ),
        host_facts={"os_pretty_name": "Debian GNU/Linux 12"},
        guest_inventory=[],
        service_inventory=[
            {"service_name": "postgresql@16-main.service"},
            {"service_name": "filebeat.service"},
        ],
        topology=None,
        user_hints=[],
    )

    assert identity.primary_signature_id == "postgresql_database"
    assert identity.detected_platform == "PostgreSQL"
    assert identity.primary_signature_id != "wazuh_stack"


def test_signature_resolver_produces_alternate_matches_for_mixed_service_signals():
    identity = resolve_machine_identity(
        host_name="mcp-grafana",
        metadata=_host_summary_metadata(grouped_roots=["/srv/mcp"], observed_names=["mcp", "grafana"]),
        host_facts={"os_pretty_name": "Ubuntu Server 24.04"},
        guest_inventory=[],
        service_inventory=[
            {"service_name": "mcp-gateway.service"},
            {"service_name": "gateway-tool.service"},
            {"service_name": "grafana-server.service"},
        ],
        topology=None,
        user_hints=[],
    )

    assert identity.primary_signature_id == "mcp_gateway"
    assert identity.alternate_matches
    assert any(item["signature_id"] == "grafana" for item in identity.alternate_matches)
    assert identity.ambiguity_notes


def test_signature_resolver_does_not_let_hosted_golem_override_proxmox_hypervisor_identity():
    identity = resolve_machine_identity(
        host_name="Byteme2",
        metadata={
            "setup_kind": "host-summary",
            "grouped_roots": ["/mnt/hypervisor-config"],
            "inventory_summary": {"observed_names": ["golem", "yagna"]},
            "child_projects": [
                {"name": "golem", "path": "/srv/golem"},
                {"name": "mcp", "path": "/srv/mcp"},
            ],
            "project_paths": ["/srv/golem", "/srv/mcp"],
            "marker_names": [],
        },
        host_facts={"proxmox_version": "8.3.0", "os_pretty_name": "Debian GNU/Linux 13"},
        guest_inventory=[
            {"guest_type": "qemu", "guest_id": "100", "config_path": "/etc/pve/nodes/byteme2/qemu-server/100.conf"},
            {"guest_type": "lxc", "guest_id": "101", "config_path": "/etc/pve/nodes/byteme2/lxc/101.conf"},
        ],
        service_inventory=[],
        topology=None,
        user_hints=[],
    )

    assert identity.primary_signature_id == "proxmox_hypervisor"
    assert identity.detected_platform == "Proxmox VE"
    assert "virtualization host" in identity.detected_role


def test_signature_resolver_falls_back_conservatively_when_evidence_is_weak():
    identity = resolve_machine_identity(
        host_name="misc-node",
        metadata=_host_summary_metadata(grouped_roots=["/opt"], observed_names=["misc"]),
        host_facts={"os_pretty_name": "Debian GNU/Linux 13"},
        guest_inventory=[],
        service_inventory=[],
        topology=None,
        user_hints=[],
    )

    assert identity.primary_signature_id is None
    assert identity.machine_kind == "unknown"
    assert identity.identity_confidence < 0.5
    assert identity.ambiguity_notes


def test_project_mapping_preview_uses_publishable_signature_defaults(session):
    host = create_host(session, name="mcp", address="10.20.2.10", root_path="/srv/mcp")
    project = create_project(session, host, path="host://mcp")
    identity = MachineIdentityResult(
        project_id=project.id,
        host_id=host.id,
        primary_signature_id="mcp_gateway",
        primary_signature_name="MCP Gateway",
        machine_kind="service_node",
        story_template_type="service_gateway",
        detected_platform="MCP gateway",
        detected_role="AI control / tool-access node",
        stack_tags_json=["MCP", "Gateway", "Tooling"],
        identity_confidence=0.88,
        identity_evidence_json=["Observed MCP service naming."],
        alternate_matches_json=[],
        ambiguity_notes_json=[],
        local_facts_evidence_json=["Observed operating system: Ubuntu."],
        topology_evidence_json=[],
        last_resolved_at=utcnow(),
    )
    profile = MachineProfile(
        project_id=project.id,
        host_id=host.id,
        machine_kind="service_node",
        story_template_type="service_gateway",
        detected_platform="MCP gateway",
        detected_role="AI control / tool-access node",
        confidence=0.88,
        lead_paragraph="This VM is the control layer for my AI environment.",
        project_role_summary="It handles my tool-access and orchestration layer.",
        host_resources_json={},
        project_role_json={},
        setup_details_json=[],
        contained_workloads_json={},
        detected_components_json=[],
        why_it_matters_json=[],
        notes_json=[],
        unknowns_json=[],
        evidence_json=[],
        last_generated_at=utcnow(),
    )
    session.add(identity)
    session.add(profile)
    session.commit()

    preview = build_project_mapping_preview(session, project=project, identity=identity, profile=profile)

    assert preview.project_title_candidate == "MCP Control Layer VM"
    assert preview.card_summary_candidate == "My control-layer VM for local AI tooling and structured tool access."
    assert preview.publish_summary_candidate == "This VM is the control layer for my AI environment."
    assert preview.recommended_status == "publishable"
    assert preview.ready_for_cms is True
    assert "MCP" in (preview.tech_tags_candidate_json or [])


def test_project_mapping_preview_respects_internal_only_override(session):
    host = create_host(session, name="byteme2", address="10.20.1.2", root_path="/etc/pve/nodes/byteme2/qemu-server")
    project = create_project(session, host, path="host://byteme2")
    identity = MachineIdentityResult(
        project_id=project.id,
        host_id=host.id,
        primary_signature_id="proxmox_hypervisor",
        primary_signature_name="Proxmox Hypervisor",
        machine_kind="hypervisor",
        story_template_type="proxmox_hypervisor",
        detected_platform="Proxmox VE",
        detected_role="virtualization host",
        stack_tags_json=["Proxmox", "QEMU", "LXC"],
        identity_confidence=0.96,
        identity_evidence_json=["Observed Proxmox version."],
        alternate_matches_json=[],
        ambiguity_notes_json=[],
        local_facts_evidence_json=[],
        topology_evidence_json=[],
        last_resolved_at=utcnow(),
    )
    profile = MachineProfile(
        project_id=project.id,
        host_id=host.id,
        machine_kind="hypervisor",
        story_template_type="proxmox_hypervisor",
        detected_platform="Proxmox VE",
        detected_role="virtualization host",
        confidence=0.96,
        lead_paragraph="This is my Proxmox host called byteme2.",
        project_role_summary="It hosts multiple VMs and LXCs in my lab.",
        host_resources_json={},
        project_role_json={},
        setup_details_json=[],
        contained_workloads_json={},
        detected_components_json=[],
        why_it_matters_json=[],
        notes_json=[],
        unknowns_json=[],
        evidence_json=[],
        last_generated_at=utcnow(),
    )
    session.add(identity)
    session.add(profile)
    session.commit()

    persist_host_profile_override(
        session,
        host,
        HostProfileOverridePayload(
            internal_only=True,
            public_title_override="My private hypervisor",
            notes_for_cms_export="Keep this out of public export.",
        ),
    )
    session.commit()

    preview = build_project_mapping_preview(session, project=project, identity=identity, profile=profile)

    assert preview.recommended_status == "internal_only"
    assert preview.internal_only_reason == "Manually marked internal-only."
    assert preview.ready_for_cms is False


def test_hypervisor_mapping_title_does_not_take_guest_research_title_hint(session):
    host = create_host(session, name="Byteme2", address="10.20.1.2", root_path="/etc/pve/nodes/byteme2/qemu-server")
    project = create_project(session, host, path="host://byteme2")
    identity = MachineIdentityResult(
        project_id=project.id,
        host_id=host.id,
        primary_signature_id="proxmox_hypervisor",
        primary_signature_name="Proxmox Hypervisor",
        machine_kind="hypervisor",
        story_template_type="proxmox_hypervisor",
        detected_platform="Proxmox VE",
        detected_role="virtualization host",
        stack_tags_json=["Proxmox", "QEMU", "LXC"],
        identity_confidence=0.96,
        identity_evidence_json=["Observed Proxmox version."],
        alternate_matches_json=[],
        ambiguity_notes_json=[],
        local_facts_evidence_json=[],
        topology_evidence_json=[],
        last_resolved_at=utcnow(),
    )
    profile = MachineProfile(
        project_id=project.id,
        host_id=host.id,
        machine_kind="hypervisor",
        story_template_type="proxmox_hypervisor",
        detected_platform="Proxmox VE",
        detected_role="virtualization host",
        confidence=0.96,
        lead_paragraph="This is my Proxmox host called Byteme2.",
        project_role_summary="It hosts multiple VMs and LXCs in my lab.",
        host_resources_json={},
        project_role_json={},
        setup_details_json=[],
        contained_workloads_json={},
        detected_components_json=[],
        why_it_matters_json=[],
        notes_json=[],
        unknowns_json=[],
        evidence_json=[],
        last_generated_at=utcnow(),
    )
    research = ResearchOutcome(
        triggered=True,
        reason="Identity title was weak.",
        title_hint="golem Workload VM",
        affected_title=True,
        findings=[
            ResearchFindingPayload(
                source_type="local",
                finding_kind="golem_provider",
                result_summary="Matched golem-related indicators from hosted guest context.",
                confidence=0.88,
                relevance=0.88,
                evidence=["Matched indicator: golem"],
                title_hint="golem Workload VM",
                affected_title=True,
            )
        ],
    )
    session.add(identity)
    session.add(profile)
    session.commit()

    preview = build_project_mapping_preview(
        session,
        project=project,
        identity=identity,
        profile=profile,
        research_outcome=research,
    )

    assert preview.project_title_candidate == "Byteme2 Proxmox Hypervisor"
    assert preview.publish_title_candidate == "Byteme2 Proxmox Hypervisor"
    assert preview.project_summary_candidate == "This is my main Proxmox hypervisor for lab infrastructure and project workloads."
    assert preview.publish_summary_candidate == "This is my main Proxmox hypervisor for lab infrastructure and project workloads."
    assert "golem project" not in (preview.publish_long_description_candidate or "").lower()
    assert "Golem" not in (preview.tech_tags_candidate_json or [])


def test_project_detail_page_renders_identity_and_mapping_sections(client, session):
    host = create_host(session, name="wazuh", address="10.20.3.10", root_path="/var/ossec")
    project = create_project(session, host, path="host://wazuh")
    session.add(
        MachineIdentityResult(
            project_id=project.id,
            host_id=host.id,
            primary_signature_id="wazuh_stack",
            primary_signature_name="Wazuh Stack",
            machine_kind="service_node",
            story_template_type="service_gateway",
            detected_platform="Wazuh stack",
            detected_role="security monitoring / SIEM node",
            stack_tags_json=["Wazuh", "Filebeat"],
            identity_confidence=0.82,
            identity_evidence_json=["Observed wazuh-manager service", "Observed /var/ossec"],
            alternate_matches_json=[{"signature_id": "grafana", "display_name": "Grafana", "confidence": 0.74, "detected_role": "dashboard and observability node"}],
            ambiguity_notes_json=[],
            local_facts_evidence_json=["Observed operating system: Ubuntu Server 24.04."],
            topology_evidence_json=[],
            last_resolved_at=utcnow(),
        )
    )
    session.add(
        ProjectMappingPreview(
            project_id=project.id,
            host_id=host.id,
            primary_signature_id="wazuh_stack",
            project_title_candidate="Wazuh Security Monitoring VM",
            project_summary_candidate="This VM is for my Wazuh monitoring and security stack.",
            tech_tags_candidate_json=["Wazuh", "Filebeat"],
            publishable_kind="project",
            recommended_status="needs_review",
            demo_url_candidate=None,
            repo_url_candidate=None,
            publishability_reason="Signature defaults remain conservative until manual intent is set.",
            internal_only_reason=None,
            related_hosts_json=[],
            ready_for_cms=False,
            needs_review=True,
            last_generated_at=utcnow(),
        )
    )
    session.commit()

    response = client.get(f"/web/projects/{project.id}")

    assert response.status_code == 200
    assert "Identity" in response.text
    assert "Project Mapping Preview" in response.text
    assert "Wazuh Stack" in response.text
    assert "Wazuh Security Monitoring VM" in response.text


def test_signature_resolver_recognizes_golem_workload():
    identity = resolve_machine_identity(
        host_name="golem",
        metadata=_host_summary_metadata(
            grouped_roots=["/home/golem"],
            observed_names=["golem", "yagna", "ya-provider"],
        ),
        host_facts={"os_pretty_name": "Ubuntu Server 24.04"},
        guest_inventory=[],
        service_inventory=[
            {"service_name": "yagna.service"},
            {"service_name": "ya-provider.service"},
        ],
        topology=None,
        user_hints=[],
    )

    assert identity.primary_signature_id == "golem_provider"
    assert identity.detected_platform == "Golem provider stack"
    assert "Golem" in identity.stack_tags


def test_research_can_improve_generic_project_identity_for_golem(session, config):
    host = create_host(session, name="golem", address="10.20.2.20", root_path="/home/golem")
    project = create_project(session, host, path="host://golem")
    project.metadata_json = {
        "setup_kind": "host-summary",
        "grouped_roots": ["/home/golem"],
        "project_paths": ["/home/golem/.local/bin/yagna", "/home/golem/.config/ya-provider"],
        "inventory_summary": {"observed_names": ["golem", "yagna", "ya-provider"]},
    }
    session.commit()

    identity = resolve_machine_identity(
        host_name="misc-workload",
        metadata=project.metadata_json,
        host_facts={"os_pretty_name": "Ubuntu Server 24.04"},
        guest_inventory=[],
        service_inventory=[],
        topology=None,
        user_hints=[],
    )

    outcome = run_identity_research(
        session,
        config,
        project=project,
        identity=identity,
        collected={
            "host_facts": {"os_pretty_name": "Ubuntu Server 24.04"},
            "service_inventory": [{"service_name": "yagna.service"}],
            "guest_inventory": [],
        },
        base_profile_packet={},
        title_candidate="Generic Project VM",
        summary_candidate="This machine is part of my lab as golem.",
        include_external=False,
        force=True,
    )

    assert outcome.triggered is True
    assert outcome.title_hint == "golem Workload VM"
    assert "Golem" in outcome.tech_tags
