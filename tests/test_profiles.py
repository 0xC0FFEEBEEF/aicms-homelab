from __future__ import annotations

from app.lmstudio.client import LMStudioClient
from app.models import GuestInventory, HostFact, HostStorageFact, MachineProfile, ProjectFile, ServiceInventory
from app.profiles.classifier import classify_machine
from app.profiles.collector import collect_machine_facts
from app.profiles.inventory import sync_host_fact_inventory
from app.profiles.service import generate_machine_profile
from app.schemas import LMSTUDIO_SETTINGS_KEY, LMStudioSettingsPayload, PROFILE_SETTINGS_KEY, ProfileSettingsPayload
from app.utils.settings_store import set_settings
from app.utils.common import utcnow
from tests.conftest import create_host, create_project


def test_machine_classifier_detects_proxmox_hypervisor():
    classification = classify_machine(
        host_name="byteme2",
        metadata={
            "setup_kind": "proxmox-hypervisor",
            "grouped_roots": ["/etc/pve/nodes/byteme2/qemu-server", "/etc/pve/nodes/byteme2/lxc"],
            "inventory_summary": {"observed_names": ["proxmox", "vm"]},
            "child_projects": [],
            "project_counts": {"total": 1, "git_repos": 0, "by_type": {"virtualization": 1}},
            "marker_names": [],
        },
        host_facts={"proxmox_version": "8.4.1", "os_pretty_name": "Proxmox VE 8.4"},
        guest_inventory=[{"guest_type": "qemu"}, {"guest_type": "lxc"}],
        service_inventory=[],
    )

    assert classification.machine_kind == "hypervisor"
    assert classification.detected_platform == "Proxmox VE"
    assert classification.story_template_type == "proxmox_hypervisor"
    assert classification.confidence >= 0.88


def test_collect_machine_facts_parses_proxmox_guest_inventory():
    host = type("HostStub", (), {"name": "byteme2", "address": "10.20.1.2"})()

    def fake_run_lines(command, **kwargs):
        if "HOSTNAME=" in command:
            return (
                [
                    "HOSTNAME=byteme2",
                    "FQDN=byteme2.lan",
                    "KERNEL=6.8.0",
                    "ARCH=x86_64",
                    "OS_PRETTY_NAME=Debian GNU/Linux 13",
                    "CPU_CORES=8",
                    "CPU_THREADS=16",
                    "THREADS_PER_CORE=2",
                    "MEMORY_BYTES=34359738368",
                    "SWAP_BYTES=4294967296",
                    "ROOT_SIZE_BYTES=214748364800",
                    "ROOT_USED_BYTES=53687091200",
                    "UPTIME_SECONDS=7200",
                    "VIRTUALIZATION=none",
                    "IP_ADDRESSES=10.20.1.2 192.168.1.10",
                ],
                None,
            )
        if "df -P -B1" in command:
            return (["/dev/mapper/pve-root 214748364800 53687091200 161061273600 25% /"], None)
        if "systemctl list-units" in command:
            return (["sshd.service loaded active running OpenSSH server daemon"], None)
        if "pveversion" in command:
            return (["pve-manager/8.4.1/abcdef"], None)
        if "qm list" in command:
            return (["100 media running 4096 64.00 1234"], None)
        if "qm config 100" in command:
            return (
                [
                    "name: media",
                    "cores: 4",
                    "memory: 4096",
                    "scsi0: local-lvm:vm-100-disk-0,size=64G",
                ],
                None,
            )
        if "pct list" in command:
            return (["200 running tools"], None)
        if "pct config 200" in command:
            return (
                [
                    "hostname: tools",
                    "cores: 2",
                    "memory: 2048",
                    "rootfs: local-lvm:subvol-200-disk-0,size=8G",
                ],
                None,
            )
        if "pvesm status" in command:
            return (["local dir active 107374182400 53687091200 53687091200 50%"], None)
        return ([], None)

    facts = collect_machine_facts(
        host=host,
        run_lines=fake_run_lines,
        run_command=lambda command, **kwargs: (0, "", ""),
        max_services=20,
        max_guests=20,
    )

    assert facts["hostname"] == "byteme2"
    assert facts["proxmox_version"] == "pve-manager/8.4.1/abcdef"
    assert len(facts["guest_inventory"]) == 2
    assert facts["guest_inventory"][0]["guest_type"] == "qemu"
    assert facts["guest_inventory"][1]["guest_type"] == "lxc"
    assert facts["selected_running_services"][0]["service_name"] == "sshd.service"


def test_generate_machine_profile_deterministic_fallback_uses_my_wording(session, app):
    host = create_host(session, name="golem", address="10.20.0.15", root_path="/home/golem")
    project = create_project(session, host, path="host://1")
    project.project_type = "host"
    project.metadata_json = {
        "setup_kind": "host-summary",
        "grouped_roots": ["/home/golem"],
        "inventory_summary": {"observed_names": ["golem", "provider"]},
        "child_projects": [{"name": "golem", "path": "/home/golem", "project_type": "host-setup", "marker_names": ["README.md"]}],
        "project_counts": {"total": 1, "git_repos": 1, "by_type": {"host-setup": 1}},
        "marker_names": ["README.md"],
    }
    session.add(
        HostFact(
            host_id=host.id,
            machine_kind="virtual_machine",
            detected_platform="Ubuntu Server",
            detected_role="golem project VM",
            story_template_type="project_vm",
            confidence=0.8,
            facts_json={
                "hostname": "golem",
                "management_ip": "10.20.0.15",
                "os_pretty_name": "Ubuntu Server 24.04",
                "cpu_cores": 4,
                "cpu_threads": 4,
                "memory_gib": 8,
                "root_size_gib": 80,
                "root_used_gib": 32,
                "storage_summary": [],
                "selected_running_services": [],
                "guest_inventory": [],
            },
        )
    )
    session.commit()

    set_settings(
        session,
        PROFILE_SETTINGS_KEY,
        ProfileSettingsPayload(enabled=True, enable_lmstudio_formatting=False, fallback_to_deterministic_profile=True),
    )
    set_settings(
        session,
        LMSTUDIO_SETTINGS_KEY,
        LMStudioSettingsPayload(enabled=False, base_url="http://localhost:1234/v1"),
    )

    profile = generate_machine_profile(session, app.state.config, project.id)

    assert "my" in profile.lead_paragraph.lower()
    assert "your" not in profile.lead_paragraph.lower()
    assert profile.machine_kind in {"virtual_machine", "project_node"}
    assert profile.story_template_type == "project_vm"
    assert "golem" in profile.lead_paragraph.lower()


def test_generate_machine_profile_with_mocked_lmstudio_response_uses_my_wording(session, app, monkeypatch):
    host = create_host(session, name="byteme2", address="10.20.1.2", root_path="/etc/pve/nodes/byteme2/qemu-server")
    project = create_project(session, host, path="host://2")
    project.project_type = "host"
    project.metadata_json = {
        "setup_kind": "host-summary",
        "grouped_roots": ["/etc/pve/nodes/byteme2/qemu-server", "/etc/pve/nodes/byteme2/lxc"],
        "inventory_summary": {"observed_names": ["proxmox", "vm"]},
        "child_projects": [],
        "project_counts": {"total": 1, "git_repos": 0, "by_type": {"virtualization": 1}},
        "marker_names": [],
    }
    session.add(
        HostFact(
            host_id=host.id,
            machine_kind="hypervisor",
            detected_platform="Proxmox VE",
            detected_role="virtualization node in my lab",
            story_template_type="proxmox_hypervisor",
            confidence=0.96,
            facts_json={
                "hostname": "byteme2",
                "management_ip": "10.20.1.2",
                "os_pretty_name": "Proxmox VE 8.4",
                "cpu_cores": 8,
                "cpu_threads": 16,
                "memory_gib": 32,
                "storage_summary": [],
                "selected_running_services": [],
                "guest_inventory": [],
                "proxmox_version": "8.4.1",
            },
        )
    )
    session.add(
        GuestInventory(
            host_id=host.id,
            guest_id="100",
            guest_type="qemu",
            name="media",
            status="running",
            cores=4,
            ram_mb=4096,
            disk_summary="scsi0:64G",
            role_guess="media workload",
            ip=None,
            config_path="/etc/pve/qemu-server/100.conf",
            raw_json={},
        )
    )
    session.commit()

    set_settings(
        session,
        PROFILE_SETTINGS_KEY,
        ProfileSettingsPayload(enabled=True, enable_lmstudio_formatting=True, fallback_to_deterministic_profile=True),
    )
    set_settings(
        session,
        LMSTUDIO_SETTINGS_KEY,
        LMStudioSettingsPayload(enabled=True, base_url="http://localhost:1234/v1", selected_model="local-model"),
    )

    class FakeResponse:
        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                "{"
                                '"lead_paragraph": "This is my Proxmox host called byteme2. It acts as a virtualization node in my lab and hosts guest workloads for my projects.", '
                                '"project_role_summary": "My main role for this machine is virtualization and guest management in the lab.", '
                                '"setup_summary_items": ["I can see Proxmox guest inventory and config-backed workload definitions."], '
                                '"why_it_matters_items": ["It carries multiple guest workloads that the rest of my lab depends on."], '
                                '"notes_items": ["Human review is still useful for guest role labels."], '
                                '"unknowns_items": ["Guest IP addresses were not directly observed in the available config."]'
                                "}"
                            )
                        }
                    }
                ]
            }

        def raise_for_status(self):
            return None

    monkeypatch.setattr(LMStudioClient, "_post_chat_completion", lambda self, payload, allow_plain_fallback=False: FakeResponse().json())

    profile = generate_machine_profile(session, app.state.config, project.id)

    assert "my" in profile.lead_paragraph.lower()
    assert "your" not in profile.lead_paragraph.lower()
    assert profile.llm_model_used == "local-model"
    assert profile.why_it_matters_json


def test_sync_host_fact_inventory_dedupes_storage_rows_and_updates_in_place(session):
    host = create_host(session, name="byteme2", address="10.20.1.2", root_path="/etc/pve/nodes/byteme2/qemu-server")
    seen_at = utcnow()
    host_facts = {
        "hostname": "byteme2",
        "storage_summary": [
            {
                "storage_name": "/dev/mapper/pve-root",
                "mount_point": "/",
                "storage_type": "filesystem",
                "total_bytes": 1000,
                "used_bytes": 500,
                "available_bytes": 500,
                "notes": None,
            },
            {
                "storage_name": "/dev/mapper/pve-root",
                "mount_point": "/",
                "storage_type": "filesystem",
                "total_bytes": 1000,
                "used_bytes": 500,
                "available_bytes": 500,
                "notes": None,
            },
        ],
        "guest_inventory": [
            {"guest_id": "100", "guest_type": "qemu", "name": "media", "status": "running"},
        ],
        "selected_running_services": [
            {"service_name": "sshd.service", "status": "running", "description": "OpenSSH", "role_guess": "remote access"},
        ],
    }

    sync_host_fact_inventory(
        session,
        host=host,
        host_facts=host_facts,
        classification={"machine_kind": "hypervisor", "detected_platform": "Proxmox VE", "evidence": ["pveversion"]},
        seen_at=seen_at,
    )
    session.commit()

    sync_host_fact_inventory(
        session,
        host=host,
        host_facts=host_facts,
        classification={"machine_kind": "hypervisor", "detected_platform": "Proxmox VE", "evidence": ["pveversion"]},
        seen_at=utcnow(),
    )
    session.commit()

    assert session.query(HostStorageFact).filter_by(host_id=host.id).count() == 1
    assert session.query(GuestInventory).filter_by(host_id=host.id).count() == 1
    assert session.query(ServiceInventory).filter_by(host_id=host.id).count() == 1


def test_project_detail_page_renders_machine_profile_sections(client, session):
    host = create_host(session, name="byteme2", address="10.20.1.2", root_path="/etc/pve/nodes/byteme2/qemu-server")
    project = create_project(session, host, path="host://2")
    project.project_type = "host"
    session.add(
        MachineProfile(
            project_id=project.id,
            host_id=host.id,
            machine_kind="hypervisor",
            story_template_type="proxmox_hypervisor",
            detected_platform="Proxmox VE",
            detected_role="virtualization node in my lab",
            confidence=0.55,
            lead_paragraph="This is my Proxmox host called byteme2. It acts as a virtualization node in my lab.",
            project_role_summary="My main role for this machine is virtualization and guest management in the lab.",
            host_resources_json={"cpu_cores": 8, "cpu_threads": 16, "memory_gib": 32, "management_ip": "10.20.1.2", "storage": []},
            project_role_json={"primary_purpose": "virtualization", "environment_type": "proxmox_hypervisor", "detected_stack": "Proxmox VE"},
            setup_details_json=["I can see Proxmox config roots and guest definitions."],
            contained_workloads_json={
                "lxcs": [{"guest_id": "200", "name": "tools", "cores": 2, "ram_mb": 2048, "disk_summary": "rootfs:8G", "role_guess": "tooling"}],
                "vms": [{"guest_id": "100", "name": "media", "cores": 4, "ram_mb": 4096, "disk_summary": "scsi0:64G", "role_guess": "media workload"}],
            },
            detected_components_json=[],
            why_it_matters_json=["It carries guest workloads for my lab."],
            notes_json=["Human review is still useful for guest role labels."],
            unknowns_json=["Guest IP addresses were not directly observed."],
            evidence_json=["Observed Proxmox guest inventory entries: 2."],
            llm_model_used="local-model",
        )
    )
    session.commit()

    response = client.get(f"/web/projects/{project.id}")
    assert response.status_code == 200
    assert "Machine Profile" in response.text
    assert "Contained LXCs" in response.text
    assert "Contained VMs" in response.text


def test_generate_machine_profile_ignores_off_node_proxmox_guest_inventory(session, app):
    host = create_host(session, name="Byteme2", address="10.20.1.2", root_path="/etc/pve/nodes/byteme2/qemu-server")
    project = create_project(session, host, path="host://byteme2")
    project.project_type = "host"
    project.metadata_json = {
        "setup_kind": "host-summary",
        "grouped_roots": ["/etc/pve/nodes/byteme2/qemu-server", "/etc/pve/nodes/byteme2/lxc"],
        "inventory_summary": {"observed_names": ["proxmox", "byteme2"]},
        "child_projects": [],
        "project_counts": {"total": 1, "git_repos": 0, "by_type": {"virtualization": 1}},
        "marker_names": [],
        "proxmox_inventory": {
            "qemu_vm_ids": ["101"],
            "qemu_vm_count": 1,
            "lxc_container_ids": ["201"],
            "lxc_container_count": 1,
        },
        "operator_context": {
            "extra_instructions": "List all my VMs and LXCs.",
        },
    }
    session.add(
        HostFact(
            host_id=host.id,
            machine_kind="hypervisor",
            detected_platform="Proxmox VE",
            detected_role="virtualization host",
            story_template_type="proxmox_hypervisor",
            confidence=0.95,
            facts_json={
                "hostname": "byteme2",
                "management_ip": "10.20.1.2",
                "os_pretty_name": "Proxmox VE 8.4",
                "cpu_cores": 16,
                "cpu_threads": 32,
                "memory_gib": 64,
                "storage_summary": [],
                "selected_running_services": [],
                "proxmox_version": "8.4.1",
            },
        )
    )
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
                ip=None,
                config_path="/etc/pve/qemu-server/101.conf",
                raw_json={},
            ),
            GuestInventory(
                host_id=host.id,
                guest_id="201",
                guest_type="lxc",
                name="Tools",
                status="running",
                cores=2,
                ram_mb=2048,
                disk_summary="rootfs:16G",
                role_guess="infrastructure guest",
                ip=None,
                config_path="/etc/pve/lxc/201.conf",
                raw_json={},
            ),
            GuestInventory(
                host_id=host.id,
                guest_id="999",
                guest_type="qemu",
                name="golem",
                status="running",
                cores=8,
                ram_mb=16384,
                disk_summary="scsi0:128G",
                role_guess="Golem project workload",
                ip=None,
                config_path="/etc/pve/qemu-server/999.conf",
                raw_json={},
            ),
        ]
    )
    session.commit()

    set_settings(
        session,
        PROFILE_SETTINGS_KEY,
        ProfileSettingsPayload(enabled=True, enable_lmstudio_formatting=False, fallback_to_deterministic_profile=True),
    )
    set_settings(
        session,
        LMSTUDIO_SETTINGS_KEY,
        LMStudioSettingsPayload(enabled=False, base_url="http://localhost:1234/v1"),
    )

    profile = generate_machine_profile(session, app.state.config, project.id)

    assert [item["name"] for item in profile.contained_workloads_json["vms"]] == ["MCP"]
    assert [item["name"] for item in profile.contained_workloads_json["lxcs"]] == ["Tools"]
    assert "golem" not in profile.project_role_summary.lower()


def test_generate_machine_profile_uses_scanned_proxmox_config_files_when_guest_inventory_is_empty(session, app):
    host = create_host(session, name="byteme2", address="10.20.1.2", root_path="/etc/pve/nodes/byteme2/qemu-server")
    project = create_project(session, host, path="host://2")
    project.project_type = "host"
    project.metadata_json = {
        "setup_kind": "host-summary",
        "grouped_roots": ["/etc/pve/nodes/byteme2/qemu-server", "/etc/pve/nodes/byteme2/lxc"],
        "inventory_summary": {"observed_names": ["proxmox", "vm"]},
        "child_projects": [],
        "project_counts": {"total": 1, "git_repos": 0, "by_type": {"virtualization": 1}},
        "marker_names": [],
        "proxmox_inventory": {
            "qemu_vm_ids": ["100"],
            "qemu_vm_count": 1,
            "lxc_container_ids": ["200"],
            "lxc_container_count": 1,
        },
    }
    session.add(
        HostFact(
            host_id=host.id,
            machine_kind="hypervisor",
            detected_platform="Proxmox VE",
            detected_role="virtualization node in my lab",
            story_template_type="proxmox_hypervisor",
            confidence=0.9,
            facts_json={
                "hostname": "byteme2",
                "management_ip": "10.20.1.2",
                "os_pretty_name": "Proxmox VE 8.4",
                "cpu_cores": 8,
                "cpu_threads": 16,
                "memory_gib": 32,
                "storage_summary": [],
                "selected_running_services": [],
                "guest_inventory": [],
                "proxmox_version": "8.4.1",
            },
        )
    )
    session.add_all(
        [
            ProjectFile(
                project_id=project.id,
                host_id=host.id,
                root_path="/etc/pve/nodes/byteme2/qemu-server",
                path="/etc/pve/nodes/byteme2/qemu-server/100.conf",
                relative_path="qemu-server/100.conf",
                entry_type="file",
                size_bytes=128,
                fingerprint="vm-100",
                content_sample="name: media\ncores: 4\nmemory: 4096\nscsi0: local-lvm:vm-100-disk-0,size=64G\n",
            ),
            ProjectFile(
                project_id=project.id,
                host_id=host.id,
                root_path="/etc/pve/nodes/byteme2/lxc",
                path="/etc/pve/nodes/byteme2/lxc/200.conf",
                relative_path="lxc/200.conf",
                entry_type="file",
                size_bytes=128,
                fingerprint="ct-200",
                content_sample="hostname: tools\ncores: 2\nmemory: 2048\nrootfs: local-lvm:subvol-200-disk-0,size=8G\n",
            ),
        ]
    )
    session.commit()

    set_settings(
        session,
        PROFILE_SETTINGS_KEY,
        ProfileSettingsPayload(enabled=True, enable_lmstudio_formatting=False, fallback_to_deterministic_profile=True),
    )
    set_settings(
        session,
        LMSTUDIO_SETTINGS_KEY,
        LMStudioSettingsPayload(enabled=False, base_url="http://localhost:1234/v1"),
    )

    profile = generate_machine_profile(session, app.state.config, project.id)

    assert "1 LXC container(s) and 1 VM(s)" in profile.lead_paragraph
    assert profile.contained_workloads_json["vms"][0]["name"] == "media"
    assert profile.contained_workloads_json["lxcs"][0]["name"] == "tools"
