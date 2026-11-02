from __future__ import annotations

from app.models import HostProfileOverride, MachineIdentityResult, MachineProfile, ProjectClarificationGuidance
from app.project_mapping.copy_builders import build_public_copy, sanitize_public_copy_text
from app.project_mapping.preferences import project_public_copy_preferences
from app.project_mapping.service import build_project_mapping_preview
from app.publish_preview.service import build_project_website_preview
from app.utils.common import utcnow
from tests.conftest import create_host, create_project


def test_hypervisor_mapping_splits_card_and_publish_copy(session):
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
        identity_confidence=0.95,
        identity_evidence_json=["Observed Proxmox version.", "Observed /etc/pve."],
        local_facts_evidence_json=["Observed Debian GNU/Linux 13."],
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
        confidence=0.95,
        lead_paragraph="This is my Proxmox host called Byteme2.",
        project_role_summary="It hosts multiple VMs and LXCs in my lab.",
        host_resources_json={
            "cpu": "64 cores",
            "memory": "94 GiB",
            "storage": [
                {
                    "storage_name": "/dev/mapper/pve-root",
                    "mount_point": "/",
                    "storage_type": "filesystem",
                    "total_gib": 38.64,
                    "used_gib": 31.81,
                    "available_gib": 4.83,
                }
            ],
        },
        project_role_json={},
        setup_details_json=["Guest definitions are visible from Proxmox config files."],
        contained_workloads_json={
            "vms": [
                {"guest_id": "101", "guest_type": "qemu", "name": "MCP", "cores": 4, "ram_mb": 8192, "disk_summary": "scsi0:64G", "role_guess": "AI control or gateway workload"},
                {"guest_id": "102", "guest_type": "qemu", "name": "Wazuh", "cores": 4, "ram_mb": 8192, "disk_summary": "scsi0:128G", "role_guess": "security monitoring workload"},
            ],
            "lxcs": [
                {"guest_id": "201", "guest_type": "lxc", "name": "golem", "cores": 2, "ram_mb": 4096, "disk_summary": "rootfs:32G", "role_guess": "Golem project workload"},
            ],
        },
        detected_components_json=[],
        why_it_matters_json=["It hosts a large share of my lab workloads."],
        notes_json=[],
        unknowns_json=[],
        evidence_json=[],
        last_generated_at=utcnow(),
    )
    session.add_all([identity, profile])
    session.commit()

    preview = build_project_mapping_preview(session, project=project, identity=identity, profile=profile)

    assert preview.card_summary_candidate.startswith("My main Proxmox hypervisor")
    assert "Hosted workload summary:" in (preview.publish_long_description_candidate or "")
    assert "Featured guests:" in (preview.publish_long_description_candidate or "")
    assert "MCP" in (preview.publish_long_description_candidate or "")
    assert "Wazuh" in (preview.publish_long_description_candidate or "")
    assert "/ (filesystem) — 38.64 GiB total, 31.81 GiB used, 4.83 GiB available" in (preview.publish_long_description_candidate or "")
    assert "{'storage_name':" not in (preview.publish_long_description_candidate or "")


def test_website_preview_uses_card_copy_for_cards_and_publish_copy_for_fields(session):
    host = create_host(session, name="mcp", address="10.20.2.40", root_path="/srv/mcp")
    project = create_project(session, host, path="/srv/mcp")
    identity = MachineIdentityResult(
        project_id=project.id,
        host_id=host.id,
        primary_signature_id="mcp_gateway",
        primary_signature_name="MCP Gateway",
        machine_kind="service_node",
        story_template_type="service_gateway",
        detected_platform="MCP gateway",
        detected_role="AI control / tool-access node",
        stack_tags_json=["MCP", "Gateway"],
        identity_confidence=0.87,
        identity_evidence_json=["Observed mcp-gateway.service."],
        local_facts_evidence_json=[],
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
        confidence=0.87,
        lead_paragraph="This VM is the control layer for my AI environment.",
        project_role_summary="It separates tool access from the main model runtime.",
        host_resources_json={},
        project_role_json={},
        setup_details_json=["Runs the MCP gateway as the core workload."],
        contained_workloads_json={},
        detected_components_json=[],
        why_it_matters_json=[],
        notes_json=[],
        unknowns_json=[],
        evidence_json=[],
        last_generated_at=utcnow(),
    )
    session.add_all([identity, profile])
    session.commit()
    build_project_mapping_preview(session, project=project, identity=identity, profile=profile)
    session.commit()
    session.refresh(project)

    preview = build_project_website_preview(session, project)

    assert preview.card.summary == "My control-layer VM for local AI tooling and structured tool access."
    assert preview.publish.short_summary == "This VM is the control layer for my AI environment."
    assert any(
        row.label == "Summary"
        and "This VM is the control layer for my AI environment." in row.value
        and row.multiline is True
        for row in preview.cms_fields
    )


def test_hypervisor_detail_preview_contains_structured_guest_inventory(session):
    host = create_host(session, name="Byteme3", address="10.20.0.37", root_path="/etc/pve/nodes/byteme3/qemu-server")
    project = create_project(session, host, path="host://byteme3")
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
        identity_confidence=0.95,
        identity_evidence_json=["Observed Proxmox version."],
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
        confidence=0.95,
        lead_paragraph="This is my Proxmox host called Byteme3.",
        project_role_summary="It hosts multiple VMs and LXCs in my lab.",
        host_resources_json={"cpu": "32 cores", "memory": "64 GiB", "management_ip": "10.20.0.37"},
        project_role_json={},
        setup_details_json=["Guest definitions are visible from Proxmox config files."],
        contained_workloads_json={
            "vms": [],
            "lxcs": [],
        },
        detected_components_json=[],
        why_it_matters_json=["It carries several project workloads."],
        notes_json=[],
        unknowns_json=[],
        evidence_json=[],
        last_generated_at=utcnow(),
    )
    session.add_all([identity, profile])
    session.commit()
    build_project_mapping_preview(session, project=project, identity=identity, profile=profile)
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
                guest_id="201",
                guest_type="lxc",
                name="golem",
                status="running",
                cores=2,
                ram_mb=4096,
                disk_summary="rootfs:32G",
                role_guess="workload VM",
            ),
        ]
    )
    session.commit()
    session.refresh(project)

    preview = build_project_website_preview(session, project)

    assert preview.detail.hosted_vms
    assert preview.detail.hosted_lxcs
    assert preview.detail.hosted_vms[0].cpu == "4 core(s)"
    assert preview.detail.hosted_vms[0].ram == "8.0 GiB"
    assert preview.detail.hosted_vms[0].disk == "scsi0:64G"
    assert preview.detail.hosted_vms[0].status == "running"
    assert preview.detail.hosted_vms[0].role == "AI control layer"
    assert preview.card.summary.startswith("My main Proxmox hypervisor")
    assert "Hosted workloads" in preview.publish.long_description


def test_cms_fields_include_richer_long_form_content(session):
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
        identity_confidence=0.95,
        identity_evidence_json=["Observed Proxmox version."],
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
        confidence=0.95,
        lead_paragraph="This is my Proxmox host called Byteme2.",
        project_role_summary="It hosts multiple VMs and LXCs in my lab.",
        host_resources_json={},
        project_role_json={},
        setup_details_json=[],
        contained_workloads_json={"vms": [{"guest_id": "101", "guest_type": "qemu", "name": "Wazuh", "cores": 4, "ram_mb": 8192, "disk_summary": "scsi0:64G", "role_guess": "security monitoring"}], "lxcs": []},
        detected_components_json=[],
        why_it_matters_json=[],
        notes_json=[],
        unknowns_json=[],
        evidence_json=[],
        last_generated_at=utcnow(),
    )
    session.add_all([identity, profile])
    session.commit()
    build_project_mapping_preview(session, project=project, identity=identity, profile=profile)
    session.commit()
    session.refresh(project)

    preview = build_project_website_preview(session, project)

    summary_row = next(row for row in preview.cms_fields if row.label == "Summary")
    assert "Hosted workload summary:" in summary_row.value
    assert "Featured guests:" in summary_row.value
    assert summary_row.multiline is True


def test_golem_publish_copy_is_richer_than_card_copy(session):
    host = create_host(session, name="golem", address="10.20.2.21", root_path="/srv/golem")
    project = create_project(session, host, path="/srv/golem")
    identity = MachineIdentityResult(
        project_id=project.id,
        host_id=host.id,
        primary_signature_id="golem_provider",
        primary_signature_name="Golem Provider",
        machine_kind="project_node",
        story_template_type="project_vm",
        detected_platform="Golem provider stack",
        detected_role="provider-side compute node",
        stack_tags_json=["Golem", "Yagna", "Provider"],
        identity_confidence=0.9,
        identity_evidence_json=["Observed yagna service."],
        local_facts_evidence_json=[],
        topology_evidence_json=[],
        last_resolved_at=utcnow(),
    )
    profile = MachineProfile(
        project_id=project.id,
        host_id=host.id,
        machine_kind="project_node",
        story_template_type="project_vm",
        detected_platform="Golem provider stack",
        detected_role="provider-side compute node",
        confidence=0.9,
        lead_paragraph="This VM is for my golem project.",
        project_role_summary="It appears to be the main provider workload VM for that environment.",
        host_resources_json={"cpu": "4 cores", "memory": "8 GiB", "operating_system": "Ubuntu 24.04"},
        project_role_json={},
        setup_details_json=["Runs the provider runtime and supporting services for my golem environment."],
        contained_workloads_json={},
        detected_components_json=[
            {"name": "yagna.service"},
            {"name": "ya-provider.service"},
            {"name": "modemmanager.service"},
        ],
        why_it_matters_json=["It is the main compute-facing VM for my golem setup."],
        notes_json=[],
        unknowns_json=[],
        evidence_json=[],
        last_generated_at=utcnow(),
    )
    session.add_all([identity, profile])
    session.commit()

    preview = build_project_mapping_preview(session, project=project, identity=identity, profile=profile)

    assert preview.card_summary_candidate == "My golem workload VM for provider-side compute services and supporting runtime components."
    assert "This VM is for my golem project." in (preview.publish_long_description_candidate or "")
    assert "The main stack indicators currently point to Golem, Yagna, Provider" in (preview.publish_long_description_candidate or "")
    assert "How it is set up:" in (preview.publish_long_description_candidate or "")


def test_sanitize_public_copy_filters_plain_editorial_instructions():
    preferences = project_public_copy_preferences(None)

    assert sanitize_public_copy_text("Focus on the hosted LXCs.", preferences) == ""
    assert sanitize_public_copy_text("Exclude the networking detail.", preferences) == ""


def test_public_copy_summary_falls_back_to_safe_text_when_filtered(session):
    host = create_host(session, name="nextcloud", address="10.20.2.12", root_path="/var/www/nextcloud")
    project = create_project(session, host, path="/var/www/nextcloud")
    identity = MachineIdentityResult(
        project_id=project.id,
        host_id=host.id,
        primary_signature_id="generic_service_node",
        primary_signature_name="Generic Service Node",
        machine_kind="service_node",
        story_template_type="service_gateway",
        detected_platform="Linux",
        detected_role="service workload node",
        stack_tags_json=["Linux"],
        identity_confidence=0.72,
        identity_evidence_json=["Observed apache2.service."],
        local_facts_evidence_json=[],
        topology_evidence_json=[],
        last_resolved_at=utcnow(),
    )
    profile = MachineProfile(
        project_id=project.id,
        host_id=host.id,
        machine_kind="service_node",
        story_template_type="service_gateway",
        detected_platform="Linux",
        detected_role="service workload node",
        confidence=0.72,
        lead_paragraph="This machine appears to run a service workload in my lab.",
        project_role_summary="This machine stores my personal cloud files.",
        host_resources_json={},
        project_role_json={},
        setup_details_json=["My inventory also sees the FQDN nextcloud."],
        contained_workloads_json={},
        detected_components_json=[],
        why_it_matters_json=[],
        notes_json=[],
        unknowns_json=[],
        evidence_json=[],
        last_generated_at=utcnow(),
    )
    override = HostProfileOverride(
        host_id=host.id,
        public_summary_hint="My inventory also sees the FQDN nextcloud.",
    )
    guidance = ProjectClarificationGuidance(
        project_id=project.id,
        host_id=host.id,
        preferred_summary_framing="Remove talk of FQDN.",
    )

    public_copy = build_public_copy(
        host=host,
        project=project,
        identity=identity,
        profile=profile,
        override=override,
        guidance=guidance,
        research=None,
    )

    assert public_copy["publish_summary"] == "This machine stores my personal cloud files."
    assert public_copy["card_summary"] == "This machine stores my personal cloud files."
    assert "fqdn" not in public_copy["publish_summary"].lower()
    assert "fqdn" not in public_copy["card_summary"].lower()


def test_public_copy_treats_editorial_instructions_as_constraints_not_content(session):
    host = create_host(session, name="nextcloud", address="10.20.2.12", root_path="/var/www/nextcloud")
    project = create_project(session, host, path="/var/www/nextcloud")
    identity = MachineIdentityResult(
        project_id=project.id,
        host_id=host.id,
        primary_signature_id="generic_service_node",
        primary_signature_name="Generic Service Node",
        machine_kind="service_node",
        story_template_type="service_gateway",
        detected_platform="Linux",
        detected_role="service workload node",
        stack_tags_json=["Linux", "apache2.service", "console-getty.service"],
        identity_confidence=0.72,
        identity_evidence_json=["Observed apache2.service."],
        local_facts_evidence_json=["Observed FQDN nextcloud."],
        topology_evidence_json=[],
        last_resolved_at=utcnow(),
    )
    profile = MachineProfile(
        project_id=project.id,
        host_id=host.id,
        machine_kind="service_node",
        story_template_type="service_gateway",
        detected_platform="Linux",
        detected_role="service workload node",
        confidence=0.72,
        lead_paragraph="This machine appears to run a service workload in my lab.",
        project_role_summary="The likely role for this machine is service workload node.",
        host_resources_json={
            "storage": [
                {
                    "mount_point": "/",
                    "storage_type": "filesystem",
                    "total_gib": 491.84,
                    "used_gib": 312.45,
                    "available_gib": 154.37,
                }
            ]
        },
        project_role_json={},
        setup_details_json=[
            "My inventory also sees the FQDN nextcloud.",
            "This machine is linked to my parent host Byteme2 as LXC 122.",
        ],
        contained_workloads_json={},
        detected_components_json=[
            {"name": "apache-htcacheclean.service"},
            {"name": "apache2.service"},
            {"name": "console-getty.service"},
            {"name": "container-getty@1.service"},
        ],
        why_it_matters_json=["It stores my personal cloud data and is monitored as part of the service workload stack."],
        notes_json=[],
        unknowns_json=[],
        evidence_json=[],
        last_generated_at=utcnow(),
    )
    guidance = ProjectClarificationGuidance(
        project_id=project.id,
        host_id=host.id,
        preferred_summary_framing='Remove talk like "The likely role for this machine is service workload node." also remove talk of FQDN and remove the .service crap under tech and stack.',
    )
    session.add_all([identity, profile, guidance])
    session.commit()
    build_project_mapping_preview(session, project=project, identity=identity, profile=profile)
    session.commit()
    session.refresh(project)

    preview = build_project_website_preview(session, project)
    public_text = "\n".join(
        [
            preview.card.summary,
            preview.publish.long_description,
            preview.detail.long_description,
            "\n".join(row.value for row in preview.cms_fields),
        ]
    )

    assert "Remove talk like" not in public_text
    assert "service workload node" not in public_text.lower()
    assert "fqdn" not in public_text.lower()
    assert ".service" not in public_text


def test_hypervisor_public_copy_honors_list_all_guests_and_exclusions(session):
    host = create_host(session, name="Byteme2", address="10.20.1.2", root_path="/etc/pve/nodes/byteme2/qemu-server")
    project = create_project(session, host, path="host://byteme2")
    project.metadata_json = {
        "operator_context": {
            "extra_instructions": "Do a web search for each of the vms and lxc's names so that you know what they are used for, exclude CSC-306-NB-VM",
        }
    }
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
        identity_confidence=0.95,
        identity_evidence_json=["Observed Proxmox version."],
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
        confidence=0.95,
        lead_paragraph="This is my Proxmox host called Byteme2.",
        project_role_summary="It hosts multiple VMs and LXCs in my lab.",
        host_resources_json={},
        project_role_json={},
        setup_details_json=["Guest definitions are visible from Proxmox config files."],
        contained_workloads_json={
            "vms": [
                {"guest_id": "101", "guest_type": "qemu", "name": "MCP", "cores": 4, "ram_mb": 8192, "disk_summary": "scsi0:64G", "role_guess": "AI control layer"},
                {"guest_id": "102", "guest_type": "qemu", "name": "Wazuh", "cores": 4, "ram_mb": 8192, "disk_summary": "scsi0:128G", "role_guess": "security monitoring"},
                {"guest_id": "103", "guest_type": "qemu", "name": "CSC-306-NB-VM", "cores": 8, "ram_mb": 16384, "disk_summary": "scsi0:256G", "role_guess": "project workload"},
            ],
            "lxcs": [
                {"guest_id": "201", "guest_type": "lxc", "name": "AICMS", "cores": 2, "ram_mb": 4096, "disk_summary": "rootfs:32G", "role_guess": "infrastructure guest"},
                {"guest_id": "202", "guest_type": "lxc", "name": "Gitea", "cores": 2, "ram_mb": 4096, "disk_summary": "rootfs:32G", "role_guess": "code or SCM workload"},
            ],
        },
        detected_components_json=[],
        why_it_matters_json=["It hosts a large share of my lab workloads."],
        notes_json=[],
        unknowns_json=[],
        evidence_json=[],
        last_generated_at=utcnow(),
    )
    session.add_all([identity, profile])
    session.commit()

    preview = build_project_mapping_preview(session, project=project, identity=identity, profile=profile)

    assert "Hosted LXCs:" in (preview.publish_long_description_candidate or "")
    assert "Hosted VMs:" in (preview.publish_long_description_candidate or "")
    assert "MCP — AI control layer" in (preview.publish_long_description_candidate or "")
    assert "Wazuh — security monitoring" in (preview.publish_long_description_candidate or "")
    assert "AICMS — infrastructure guest" in (preview.publish_long_description_candidate or "")
    assert "Gitea — code or SCM workload" in (preview.publish_long_description_candidate or "")
    assert "CSC-306-NB-VM" not in (preview.publish_long_description_candidate or "")


def test_guidance_instructions_are_sanitized_and_not_duplicated_in_public_copy(session):
    host = create_host(session, name="nextcloud", address="10.20.2.22", root_path="/var/www/nextcloud")
    project = create_project(session, host, path="/var/www/nextcloud")
    identity = MachineIdentityResult(
        project_id=project.id,
        host_id=host.id,
        primary_signature_id="nextcloud",
        primary_signature_name="Nextcloud",
        machine_kind="service_node",
        story_template_type="project_vm",
        detected_platform="Nextcloud",
        detected_role="self-hosted cloud storage node",
        stack_tags_json=["Nextcloud", "Linux"],
        identity_confidence=0.9,
        identity_evidence_json=["Observed nextcloud config."],
        local_facts_evidence_json=[],
        topology_evidence_json=[],
        last_resolved_at=utcnow(),
    )
    profile = MachineProfile(
        project_id=project.id,
        host_id=host.id,
        machine_kind="service_node",
        story_template_type="project_vm",
        detected_platform="Nextcloud",
        detected_role="self-hosted cloud storage node",
        confidence=0.9,
        lead_paragraph="This VM appears to host my Nextcloud deployment.",
        project_role_summary="It stores and serves personal cloud data in my lab.",
        host_resources_json={"operating_system": "Debian GNU/Linux 12"},
        project_role_json={},
        setup_details_json=["Runs Apache and the main Nextcloud application stack."],
        contained_workloads_json={},
        detected_components_json=[
            {"name": "apache2.service"},
            {"name": "php-fpm.service"},
        ],
        why_it_matters_json=["It provides my self-hosted cloud storage."],
        notes_json=[],
        unknowns_json=[],
        evidence_json=[],
        last_generated_at=utcnow(),
    )
    guidance = ProjectClarificationGuidance(
        project_id=project.id,
        host_id=host.id,
        preferred_summary_framing="Frame it as my own self hosted cloud storage, using cloudflare zerotrust to remotely access it",
        preferred_role_wording="Frame it as my own self hosted cloud storage, using cloudflare zerotrust to remotely access it",
    )
    session.add_all([identity, profile, guidance])
    session.commit()

    preview = build_project_mapping_preview(session, project=project, identity=identity, profile=profile)
    public_copy = preview.publish_long_description_candidate or ""

    assert "Frame it as" not in public_copy
    assert "This is my own self-hosted cloud storage, using Cloudflare Zero Trust to remotely access it." in public_copy
    assert "My own self-hosted cloud storage, using Cloudflare Zero Trust to remotely access it." not in public_copy
    assert public_copy.count("This is my own self-hosted cloud storage, using Cloudflare Zero Trust to remotely access it.") == 1
