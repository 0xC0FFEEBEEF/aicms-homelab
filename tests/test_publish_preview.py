from __future__ import annotations

from app.models import HostProfileOverride, MachineIdentityResult, MachineProfile, ProjectMappingPreview
from app.publish_preview.service import build_project_website_preview, build_projects_website_previews
from app.utils.common import utcnow
from tests.conftest import create_host, create_project


def test_build_project_website_preview_uses_aicms_mapping_and_hides_internal_notes(session):
    host = create_host(session, name="wazuh", address="10.20.2.13", root_path="/var/ossec")
    project = create_project(session, host, path="/var/ossec", marker_name="wazuh-manager.service")

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
            identity_confidence=0.91,
            identity_evidence_json=["Observed /var/ossec", "Observed wazuh-manager service"],
            local_facts_evidence_json=["Observed Ubuntu 24.04"],
            last_resolved_at=utcnow(),
        )
    )
    session.add(
        MachineProfile(
            project_id=project.id,
            host_id=host.id,
            machine_kind="service_node",
            story_template_type="service_gateway",
            detected_platform="Wazuh stack",
            detected_role="security monitoring / SIEM node",
            confidence=0.9,
            lead_paragraph="This VM is for my Wazuh monitoring and security stack.",
            project_role_summary="It appears to host core Wazuh services used for monitoring and dashboard access.",
            setup_details_json=["Runs Wazuh manager services.", "Includes Filebeat integration."],
            why_it_matters_json=["It centralizes security visibility for my lab."],
            last_generated_at=utcnow(),
        )
    )
    session.add(
        ProjectMappingPreview(
            project_id=project.id,
            host_id=host.id,
            primary_signature_id="wazuh_stack",
            project_title_candidate="Wazuh Security Monitoring VM",
            project_summary_candidate="This VM is for my Wazuh monitoring and security stack.",
            card_title_candidate="Wazuh Security Monitoring VM",
            card_summary_candidate="My Wazuh monitoring and security VM.",
            card_tech_tags_candidate_json=["Wazuh", "Filebeat"],
            publish_title_candidate="Wazuh Security Monitoring VM",
            publish_summary_candidate="This VM is for my Wazuh monitoring and security stack.",
            publish_long_description_candidate="This VM is for my Wazuh monitoring and security stack.\n\n- Detected components: Wazuh, Filebeat",
            publish_sections_json=["Detected components: Wazuh, Filebeat"],
            tech_tags_candidate_json=["Wazuh", "Filebeat"],
            publishable_kind="project",
            recommended_status="needs_review",
            demo_url_candidate=None,
            repo_url_candidate="https://example.com/wazuh",
            publishability_reason="This machine looks like a coherent project but still needs review.",
            ready_for_cms=False,
            needs_review=True,
            last_generated_at=utcnow(),
        )
    )
    session.add(
        HostProfileOverride(
            host_id=host.id,
            public_title_override="Wazuh Lab Monitor",
            public_summary_hint="This VM is for my Wazuh monitoring and security stack.",
            publishable_project=True,
            notes_for_cms_export="Do not leak this note.",
        )
    )
    session.commit()
    session.refresh(project)

    preview = build_project_website_preview(session, project)

    assert preview.title == "Wazuh Lab Monitor"
    assert preview.summary == "This VM is for my Wazuh monitoring and security stack."
    assert preview.card.summary == "My Wazuh monitoring and security VM."
    assert preview.publish.long_description.startswith("This VM is for my Wazuh monitoring and security stack.")
    assert preview.publishability_status == "publishable"
    assert preview.card.repo_url == "https://example.com/wazuh"
    assert "Do not leak this note." not in preview.long_description
    assert all("Do not leak" not in warning.detail for warning in preview.warnings)


def test_project_website_preview_route_supports_modes_and_excludes_internal_notes(client, session):
    host = create_host(session, name="mcp", address="10.20.2.40", root_path="/srv/mcp")
    project = create_project(session, host, path="/srv/mcp", marker_name="docker-compose.yml")

    session.add(
        MachineIdentityResult(
            project_id=project.id,
            host_id=host.id,
            primary_signature_id="mcp_gateway",
            primary_signature_name="MCP Gateway",
            machine_kind="service_node",
            story_template_type="service_gateway",
            detected_platform="MCP tooling",
            detected_role="tool access / gateway node",
            stack_tags_json=["MCP", "Gateway"],
            identity_confidence=0.86,
            identity_evidence_json=["Observed MCP service naming."],
            local_facts_evidence_json=["Observed Debian 13"],
            last_resolved_at=utcnow(),
        )
    )
    session.add(
        MachineProfile(
            project_id=project.id,
            host_id=host.id,
            machine_kind="service_node",
            story_template_type="service_gateway",
            detected_platform="MCP tooling",
            detected_role="tool access / gateway node",
            confidence=0.86,
            lead_paragraph="This VM is the control layer for my AI environment.",
            project_role_summary="Its role is to separate tool access from the main model runtime.",
            setup_details_json=["Runs the MCP gateway as the core workload."],
            why_it_matters_json=["It keeps tool access structured and separate."],
            last_generated_at=utcnow(),
        )
    )
    session.add(
        ProjectMappingPreview(
            project_id=project.id,
            host_id=host.id,
            primary_signature_id="mcp_gateway",
            project_title_candidate="MCP Gateway VM",
            project_summary_candidate="This VM is the control layer for my AI environment.",
            card_title_candidate="MCP Control Layer VM",
            card_summary_candidate="My control-layer VM for local AI tooling and structured tool access.",
            card_tech_tags_candidate_json=["MCP", "Gateway"],
            publish_title_candidate="MCP Control Layer VM",
            publish_summary_candidate="This VM is the control layer for my AI environment.",
            publish_long_description_candidate="This VM is the control layer for my AI environment.\n\n- Runs the MCP gateway as the core workload.",
            publish_sections_json=["Runs the MCP gateway as the core workload."],
            tech_tags_candidate_json=["MCP", "Gateway"],
            publishable_kind="project",
            recommended_status="publishable",
            demo_url_candidate=None,
            repo_url_candidate=None,
            publishability_reason="Manual publishable project intent is set.",
            ready_for_cms=True,
            needs_review=False,
            last_generated_at=utcnow(),
        )
    )
    session.add(
        HostProfileOverride(
            host_id=host.id,
            publishable_project=True,
            notes_for_cms_export="Internal export drafting note.",
        )
    )
    session.commit()

    response = client.get(f"/web/projects/{project.id}/preview")
    assert response.status_code == 200
    assert "Projects" in response.text
    assert "MCP Control Layer VM" in response.text
    assert "This VM is the control layer for my AI environment." in response.text
    assert "Runs the MCP gateway as the core workload." in response.text
    assert "Internal export drafting note." not in response.text
    assert "Public-facing copy" not in response.text
    assert "Why it matters in my environment" not in response.text

    response = client.get(f"/web/projects/{project.id}/preview?mode=card")
    assert response.status_code == 200
    assert "Project card" in response.text
    assert "MCP Control Layer VM" in response.text
    assert "Runs the MCP gateway as the core workload." in response.text
    assert "Internal export drafting note." not in response.text

    response = client.get(f"/web/projects/{project.id}/preview?mode=detail")
    assert response.status_code == 200
    assert "Project detail preview" in response.text
    assert "Build details" in response.text
    assert "This VM is the control layer for my AI environment." in response.text
    assert "Runs the MCP gateway as the core workload." in response.text
    assert "Internal export drafting note." not in response.text
    assert "Observed MCP service naming." not in response.text

    response = client.get(f"/web/projects/{project.id}/preview?mode=fields")
    assert response.status_code == 200
    assert "CMS field preview" in response.text
    assert "Publish-ready copy" in response.text
    assert "Repo URL" in response.text
    assert "What still needs attention" in response.text
    assert "Long Description" not in response.text
    assert "Summary" in response.text


def test_hypervisor_preview_title_stays_with_current_project_even_if_mapping_titles_are_wrong(session):
    hypervisor = create_host(session, name="Byteme2", address="10.20.1.2", root_path="/etc/pve/nodes/byteme2/qemu-server")
    hypervisor_project = create_project(session, hypervisor, path="host://byteme2", marker_name="100.conf")
    golem_host = create_host(session, name="golem", address="10.20.2.20", root_path="/srv/golem")
    golem_project = create_project(session, golem_host, path="host://golem", marker_name="README.md")

    session.add(
        MachineIdentityResult(
            project_id=hypervisor_project.id,
            host_id=hypervisor.id,
            primary_signature_id="proxmox_hypervisor",
            primary_signature_name="Proxmox Hypervisor",
            machine_kind="hypervisor",
            story_template_type="proxmox_hypervisor",
            detected_platform="Proxmox VE",
            detected_role="virtualization host",
            stack_tags_json=["Proxmox", "QEMU", "LXC"],
            identity_confidence=0.96,
            identity_evidence_json=["Observed Proxmox version."],
            local_facts_evidence_json=["Observed Debian 13."],
            last_resolved_at=utcnow(),
        )
    )
    session.add(
        MachineProfile(
            project_id=hypervisor_project.id,
            host_id=hypervisor.id,
            machine_kind="hypervisor",
            story_template_type="proxmox_hypervisor",
            detected_platform="Proxmox VE",
            detected_role="virtualization host",
            confidence=0.96,
            lead_paragraph="This is my Proxmox host called Byteme2.",
            project_role_summary="It hosts multiple VMs and LXCs in my lab.",
            host_resources_json={},
            project_role_json={},
            setup_details_json=["Hosts my main lab workloads."],
            contained_workloads_json={},
            detected_components_json=[],
            why_it_matters_json=["It anchors multiple projects in my environment."],
            notes_json=[],
            unknowns_json=[],
            evidence_json=[],
            last_generated_at=utcnow(),
        )
    )
    session.add(
        ProjectMappingPreview(
            project_id=hypervisor_project.id,
            host_id=hypervisor.id,
            primary_signature_id="proxmox_hypervisor",
            project_title_candidate="golem Workload VM",
            project_summary_candidate="This is my main Proxmox hypervisor.",
            card_title_candidate="golem Workload VM",
            card_summary_candidate="My main Proxmox hypervisor for lab infrastructure and project workloads.",
            card_tech_tags_candidate_json=["Proxmox", "QEMU", "LXC"],
            publish_title_candidate="golem Workload VM",
            publish_summary_candidate="This is my main Proxmox hypervisor.",
            publish_long_description_candidate="This is my main Proxmox hypervisor.\n\nHosted workloads include golem and other lab systems.",
            publish_sections_json=["Hosted workload summary:", "Hosts multiple lab workloads."],
            tech_tags_candidate_json=["Proxmox", "QEMU", "LXC"],
            publishable_kind="infrastructure",
            recommended_status="internal_only",
            publishability_reason="Infrastructure-heavy machines default to internal-only until reviewed.",
            ready_for_cms=False,
            needs_review=False,
            last_generated_at=utcnow(),
        )
    )
    session.add(
        MachineIdentityResult(
            project_id=golem_project.id,
            host_id=golem_host.id,
            primary_signature_id="golem_provider",
            primary_signature_name="Golem Provider",
            machine_kind="project_node",
            story_template_type="project_vm",
            detected_platform="Golem provider stack",
            detected_role="provider-side compute workload",
            stack_tags_json=["Golem", "Yagna", "Provider"],
            identity_confidence=0.88,
            identity_evidence_json=["Observed provider stack markers."],
            local_facts_evidence_json=["Observed Ubuntu 24.04."],
            last_resolved_at=utcnow(),
        )
    )
    session.add(
        ProjectMappingPreview(
            project_id=golem_project.id,
            host_id=golem_host.id,
            primary_signature_id="golem_provider",
            project_title_candidate="golem Workload VM",
            project_summary_candidate="This VM is for my golem project.",
            card_title_candidate="golem Workload VM",
            card_summary_candidate="My golem workload VM for provider-side compute services.",
            card_tech_tags_candidate_json=["Golem", "Yagna", "Provider"],
            publish_title_candidate="golem Workload VM",
            publish_summary_candidate="This VM is for my golem project.",
            publish_long_description_candidate="This VM is for my golem project.\n\nIt handles provider-side compute services.",
            publish_sections_json=["How it is set up:", "Runs provider-side compute services."],
            tech_tags_candidate_json=["Golem", "Yagna", "Provider"],
            publishable_kind="project",
            recommended_status="publishable",
            publishability_reason="This machine resolves to a coherent project workload.",
            ready_for_cms=True,
            needs_review=False,
            last_generated_at=utcnow(),
        )
    )
    session.commit()

    hypervisor_preview = build_project_website_preview(session, hypervisor_project)
    golem_preview = build_project_website_preview(session, golem_project)

    assert hypervisor_preview.card.title == "Byteme2 Proxmox Hypervisor"
    assert hypervisor_preview.title == "Byteme2 Proxmox Hypervisor"
    assert "golem" in hypervisor_preview.publish.long_description.lower()
    assert golem_preview.card.title == "golem Workload VM"


def test_projects_page_preview_titles_do_not_leak_between_cards(session):
    wazuh_host = create_host(session, name="wazuh", address="10.20.2.13", root_path="/var/ossec")
    wazuh_project = create_project(session, wazuh_host, path="host://wazuh", marker_name="wazuh-manager.service")
    mcp_host = create_host(session, name="mcp", address="10.20.2.40", root_path="/srv/mcp")
    mcp_project = create_project(session, mcp_host, path="host://mcp", marker_name="docker-compose.yml")

    session.add_all(
        [
            ProjectMappingPreview(
                project_id=wazuh_project.id,
                host_id=wazuh_host.id,
                primary_signature_id="wazuh_stack",
                project_title_candidate="Wazuh Security Monitoring VM",
                project_summary_candidate="This VM is for my Wazuh monitoring and security stack.",
                card_title_candidate="Wazuh Security Monitoring VM",
                card_summary_candidate="My Wazuh monitoring and security VM.",
                card_tech_tags_candidate_json=["Wazuh", "Filebeat"],
                publish_title_candidate="Wazuh Security Monitoring VM",
                publish_summary_candidate="This VM is for my Wazuh monitoring and security stack.",
                publish_long_description_candidate="This VM is for my Wazuh monitoring and security stack.",
                tech_tags_candidate_json=["Wazuh", "Filebeat"],
                publishable_kind="project",
                recommended_status="needs_review",
                publishability_reason="Needs review.",
                ready_for_cms=False,
                needs_review=True,
                last_generated_at=utcnow(),
            ),
            ProjectMappingPreview(
                project_id=mcp_project.id,
                host_id=mcp_host.id,
                primary_signature_id="mcp_gateway",
                project_title_candidate="MCP Control Layer VM",
                project_summary_candidate="This VM is the control layer for my AI environment.",
                card_title_candidate="MCP Control Layer VM",
                card_summary_candidate="My control-layer VM for local AI tooling and structured tool access.",
                card_tech_tags_candidate_json=["MCP", "Gateway"],
                publish_title_candidate="MCP Control Layer VM",
                publish_summary_candidate="This VM is the control layer for my AI environment.",
                publish_long_description_candidate="This VM is the control layer for my AI environment.",
                tech_tags_candidate_json=["MCP", "Gateway"],
                publishable_kind="project",
                recommended_status="publishable",
                publishability_reason="Manual publishable project intent is set.",
                ready_for_cms=True,
                needs_review=False,
                last_generated_at=utcnow(),
            ),
        ]
    )
    session.commit()

    previews = build_projects_website_previews(session, [wazuh_project, mcp_project])
    titles = [preview.card.title for preview in previews]

    assert titles == ["Wazuh Security Monitoring VM", "MCP Control Layer VM"]


def test_hypervisor_preview_filters_excluded_guest_from_public_preview(session):
    host = create_host(session, name="Byteme2", address="10.20.1.2", root_path="/etc/pve/nodes/byteme2/qemu-server")
    project = create_project(session, host, path="host://byteme2")
    project.metadata_json = {
        "operator_context": {
            "extra_instructions": "List all my VMs and LXCs, exclude CSC-306-NB-VM",
        }
    }
    session.add(
        MachineIdentityResult(
            project_id=project.id,
            host_id=host.id,
            primary_signature_id="proxmox_hypervisor",
            primary_signature_name="Proxmox Hypervisor",
            machine_kind="hypervisor",
            story_template_type="proxmox_hypervisor",
            detected_platform="Proxmox VE",
            detected_role="virtualization host",
            stack_tags_json=["Proxmox", "QEMU", "LXC"],
            identity_confidence=0.95,
            identity_evidence_json=["Observed Proxmox version."],
            local_facts_evidence_json=[],
            topology_evidence_json=[],
            last_resolved_at=utcnow(),
        )
    )
    session.add(
        MachineProfile(
            project_id=project.id,
            host_id=host.id,
            machine_kind="hypervisor",
            story_template_type="proxmox_hypervisor",
            detected_platform="Proxmox VE",
            detected_role="virtualization host",
            confidence=0.95,
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
    )
    session.add(
        ProjectMappingPreview(
            project_id=project.id,
            host_id=host.id,
            primary_signature_id="proxmox_hypervisor",
            project_title_candidate="Byteme2 Proxmox Hypervisor",
            project_summary_candidate="This is my main Proxmox hypervisor.",
            card_title_candidate="Byteme2 Proxmox Hypervisor",
            card_summary_candidate="My main Proxmox hypervisor for lab infrastructure and project workloads.",
            card_tech_tags_candidate_json=["Proxmox", "QEMU", "LXC"],
            publish_title_candidate="Byteme2 Proxmox Hypervisor",
            publish_summary_candidate="This is my main Proxmox hypervisor.",
            publish_long_description_candidate="This is my main Proxmox hypervisor.",
            publish_sections_json=[],
            tech_tags_candidate_json=["Proxmox", "QEMU", "LXC"],
            publishable_kind="infrastructure",
            recommended_status="internal_only",
            publishability_reason="Infrastructure-heavy machines default to internal-only until reviewed.",
            ready_for_cms=False,
            needs_review=False,
            last_generated_at=utcnow(),
        )
    )
    from app.models import GuestInventory

    session.add_all(
        [
            GuestInventory(
                host_id=host.id,
                guest_id="101",
                guest_type="qemu",
                name="MCP",
                status="running",
                cores=4,
                ram_mb=8192,
                disk_summary="scsi0:64G",
                role_guess="AI control layer",
            ),
            GuestInventory(
                host_id=host.id,
                guest_id="102",
                guest_type="qemu",
                name="CSC-306-NB-VM",
                status="running",
                cores=8,
                ram_mb=16384,
                disk_summary="scsi0:256G",
                role_guess="project workload",
            ),
            GuestInventory(
                host_id=host.id,
                guest_id="201",
                guest_type="lxc",
                name="Gitea",
                status="running",
                cores=2,
                ram_mb=4096,
                disk_summary="rootfs:32G",
                role_guess="code or SCM workload",
            ),
        ]
    )
    session.commit()

    preview = build_project_website_preview(session, project)

    assert "CSC-306-NB-VM" not in preview.publish.long_description
    assert all(entry.guest_name != "CSC-306-NB-VM" for entry in preview.detail.hosted_vms)
    assert any(entry.guest_name == "MCP" for entry in preview.detail.hosted_vms)
    assert any(entry.guest_name == "Gitea" for entry in preview.detail.hosted_lxcs)
