from __future__ import annotations

from app.models import HostRoot, Project, ProjectFile, ProjectInterpretation, Scan
from app.scanner.service import _build_grouped_setup_snapshots, _build_host_summary_snapshot, _interpret_project_with_review
from app.schemas import GeneralSettingsPayload, ScanSettingsPayload
from app.scanner.detection import guess_project_type
from app.schemas import ProjectSnapshot
from app.utils.common import utcnow

from tests.conftest import create_host


def test_project_detection_logic():
    assert guess_project_type(["package.json"]) == "node"
    assert guess_project_type(["Cargo.toml"]) == "rust"
    assert guess_project_type(["service.csproj"]) == "dotnet"
    assert guess_project_type([".git"]) == "generic-git"


def test_full_scan_persists_projects(client, session, monkeypatch):
    host = create_host(session)

    class DummyClient:
        def close(self):
            return None

    def fake_open_client(self, _host):
        return DummyClient()

    def fake_scan_host_full(executor, client, host_obj, general_settings, scan_settings, record_event=None):
        snapshot = ProjectSnapshot(
            host_id=host_obj.id,
            path="/srv/projects/demo",
            name_guess="demo",
            project_type="python",
            repo_remote="git@example/demo.git",
            repo_branch="main",
            head_commit="abc123",
            is_git_repo=True,
            last_modified_at=utcnow(),
            fingerprint="fp-demo",
            marker_files=[{"marker_name": "pyproject.toml", "marker_path": "/srv/projects/demo/pyproject.toml"}],
            shallow_tree_json=[{"type": "file", "path": "pyproject.toml"}],
            metadata_json={"marker_names": ["pyproject.toml"]},
        )
        return {"snapshots": [snapshot], "unchanged_project_ids": [], "warnings": []}

    monkeypatch.setattr("app.scanner.service.SSHExecutor.open_client", fake_open_client)
    monkeypatch.setattr("app.scanner.service.scan_host_full", fake_scan_host_full)

    response = client.post(f"/api/hosts/{host.id}/scan/full", follow_redirects=True)
    assert response.status_code == 200

    project = session.query(Project).one()
    scan = session.query(Scan).order_by(Scan.id.desc()).first()
    assert project.path == "/srv/projects/demo"
    assert scan.status == "completed"
    assert scan.projects_found == 1


def test_full_scan_persists_file_inventory_and_scan_api(client, session, monkeypatch):
    host = create_host(session, root_path="/home/golem")

    class DummyClient:
        def close(self):
            return None

    def fake_open_client(self, _host):
        return DummyClient()

    def fake_scan_host_full(executor, client, host_obj, general_settings, scan_settings, record_event=None):
        snapshot = ProjectSnapshot(
            host_id=host_obj.id,
            path="/home/golem",
            name_guess="golem",
            project_type="workspace",
            repo_remote=None,
            repo_branch=None,
            head_commit=None,
            is_git_repo=False,
            last_modified_at=utcnow(),
            fingerprint="fp-home",
            marker_files=[],
            shallow_tree_json=[{"type": "dir", "path": "src"}],
            metadata_json={"discovered_via": "root_inventory"},
        )
        return {
            "snapshots": [snapshot],
            "unchanged_project_ids": [],
            "warnings": [],
            "root_inventories": [
                {
                    "root_path": "/home/golem",
                    "entries": [
                        {
                            "path": "/home/golem/src",
                            "relative_path": "src",
                            "entry_type": "dir",
                            "size_bytes": 0,
                            "last_modified_at": utcnow(),
                            "fingerprint": "dir-fp",
                            "metadata_json": {"name": "src"},
                        },
                        {
                            "path": "/home/golem/src/app.py",
                            "relative_path": "src/app.py",
                            "entry_type": "file",
                            "size_bytes": 128,
                            "last_modified_at": utcnow(),
                            "fingerprint": "file-fp",
                            "metadata_json": {"name": "app.py"},
                        },
                    ],
                }
            ],
        }

    monkeypatch.setattr("app.scanner.service.SSHExecutor.open_client", fake_open_client)
    monkeypatch.setattr("app.scanner.service.scan_host_full", fake_scan_host_full)
    monkeypatch.setattr("app.scanner.service._maybe_interpret_project_during_scan", lambda *args, **kwargs: None)

    response = client.post(f"/api/hosts/{host.id}/scan/full", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/web/scans?scan_id=")

    scan = session.query(Scan).order_by(Scan.id.desc()).first()
    api_response = client.get(f"/api/scans/{scan.id}")
    assert api_response.status_code == 200
    payload = api_response.json()
    assert payload["scan"]["status"] == "completed"

    file_records = session.query(ProjectFile).order_by(ProjectFile.path.asc()).all()
    assert [record.path for record in file_records] == ["/home/golem/src", "/home/golem/src/app.py"]


def test_grouped_proxmox_roots_collapse_into_virtualization_snapshot(session):
    host = create_host(session, name="Byteme3", root_path="/etc/pve/nodes/byteme3/qemu-server")
    host.roots.append(HostRoot(root_path="/etc/pve/nodes/byteme3/lxc", enabled=True))
    host.roots.append(HostRoot(root_path="/usr/local/bin", enabled=True))
    session.commit()

    now = utcnow()
    scan_settings = ScanSettingsPayload(
        ignore_patterns=["node_modules", ".git"],
        allowed_marker_files=[".git", "README.md", "Dockerfile", "compose.yaml"],
    )
    snapshots, consumed_roots, suppressed_roots = _build_grouped_setup_snapshots(
        host,
        root_inventories=[
            {
                "root_path": "/etc/pve/nodes/byteme3/qemu-server",
                "entries": [
                    {
                        "path": "/etc/pve/nodes/byteme3/qemu-server/100.conf",
                        "relative_path": "100.conf",
                        "entry_type": "file",
                        "size_bytes": 128,
                        "last_modified_at": now,
                        "fingerprint": "vm-100",
                        "metadata_json": {"name": "100.conf"},
                    },
                    {
                        "path": "/etc/pve/nodes/byteme3/qemu-server/101.conf",
                        "relative_path": "101.conf",
                        "entry_type": "file",
                        "size_bytes": 128,
                        "last_modified_at": now,
                        "fingerprint": "vm-101",
                        "metadata_json": {"name": "101.conf"},
                    },
                ],
            },
            {
                "root_path": "/etc/pve/nodes/byteme3/lxc",
                "entries": [
                    {
                        "path": "/etc/pve/nodes/byteme3/lxc/200.conf",
                        "relative_path": "200.conf",
                        "entry_type": "file",
                        "size_bytes": 64,
                        "last_modified_at": now,
                        "fingerprint": "ct-200",
                        "metadata_json": {"name": "200.conf"},
                    }
                ],
            },
            {
                "root_path": "/usr/local/bin",
                "entries": [
                    {
                        "path": "/usr/local/bin/cloudflared",
                        "relative_path": "cloudflared",
                        "entry_type": "file",
                        "size_bytes": 64,
                        "last_modified_at": now,
                        "fingerprint": "cloudflared",
                        "metadata_json": {"name": "cloudflared"},
                    }
                ],
            },
        ],
        general_settings=GeneralSettingsPayload(),
        scan_settings=scan_settings,
        host_facts={"os_pretty_name": "Debian GNU/Linux 12", "cpu_count": 8},
    )

    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert snapshot.path == "/etc/pve/nodes/byteme3"
    assert snapshot.project_type == "virtualization"
    assert snapshot.metadata_json["proxmox_inventory"]["qemu_vm_count"] == 2
    assert snapshot.metadata_json["proxmox_inventory"]["lxc_container_count"] == 1
    assert "/etc/pve/nodes/byteme3/qemu-server" in consumed_roots
    assert "/etc/pve/nodes/byteme3/lxc" in consumed_roots
    assert "/usr/local/bin" in suppressed_roots


def test_host_summary_snapshot_wraps_child_setups(session):
    host = create_host(session, name="golem", address="10.20.0.15", root_path="/home/golem")
    child_snapshot = ProjectSnapshot(
        host_id=host.id,
        path="/home/golem",
        name_guess="golem host setup",
        project_type="host-setup",
        repo_remote=None,
        repo_branch=None,
        head_commit=None,
        is_git_repo=False,
        last_modified_at=utcnow(),
        fingerprint="child-fingerprint",
        marker_files=[],
        shallow_tree_json=[{"type": "dir", "path": "golem-data"}],
        metadata_json={
            "setup_kind": "home-root",
            "discovered_via": "root_inventory",
            "inventory_summary": {"observed_names": ["golem", "provider"]},
            "file_inventory_count": 18,
            "safe_text_candidates": 5,
        },
    )

    snapshot = _build_host_summary_snapshot(
        host,
        child_snapshots=[child_snapshot],
        root_inventories=[
            {
                "root_path": "/home/golem",
                "entries": [
                    {
                        "path": "/home/golem/golem-data",
                        "relative_path": "golem-data",
                        "entry_type": "dir",
                        "size_bytes": 0,
                        "last_modified_at": utcnow(),
                        "fingerprint": "dir-fingerprint",
                        "metadata_json": {"name": "golem-data"},
                    }
                ],
            }
        ],
        host_facts={"os_pretty_name": "Ubuntu Server 24.04", "cpu_count": 4, "memory_gib": 16.0},
    )

    assert snapshot.project_type == "host"
    assert snapshot.path == f"host://{host.id}"
    assert snapshot.metadata_json["setup_kind"] == "host-summary"
    assert snapshot.metadata_json["child_projects"][0]["name"] == "golem host setup"
    assert snapshot.metadata_json["grouped_roots"] == ["/home/golem"]
    assert snapshot.metadata_json["host_profile"]["cpu_count"] == 4


def test_full_scan_replaces_stale_root_project_with_grouped_project(client, session, monkeypatch):
    host = create_host(session, name="Byteme3", root_path="/etc/pve/nodes/byteme3/qemu-server")
    stale_project = Project(
        host_id=host.id,
        path="/etc/pve/nodes/byteme3/qemu-server",
        name_guess="qemu-server",
        project_type="workspace",
        is_git_repo=False,
        fingerprint="stale",
        first_seen_at=utcnow(),
        last_seen_at=utcnow(),
        metadata_json={"discovered_via": "root_inventory"},
    )
    session.add(stale_project)
    session.commit()

    class DummyClient:
        def close(self):
            return None

    monkeypatch.setattr("app.scanner.service.SSHExecutor.open_client", lambda self, _host: DummyClient())
    monkeypatch.setattr(
        "app.scanner.service.scan_host_full",
        lambda executor, client, host_obj, general_settings, scan_settings, record_event=None: {
            "snapshots": [
                ProjectSnapshot(
                    host_id=host_obj.id,
                    path="/etc/pve/nodes/byteme3",
                    name_guess="byteme3 VM host",
                    project_type="virtualization",
                    repo_remote=None,
                    repo_branch=None,
                    head_commit=None,
                    is_git_repo=False,
                    last_modified_at=utcnow(),
                    fingerprint="fresh-grouped",
                    marker_files=[],
                    shallow_tree_json=[{"type": "file", "path": "qemu-server/100.conf"}],
                    metadata_json={"discovered_via": "host_grouping", "setup_kind": "proxmox-hypervisor"},
                )
            ],
            "unchanged_project_ids": [],
            "warnings": [],
            "root_inventories": [{"root_path": "/etc/pve/nodes/byteme3/qemu-server", "entries": []}],
        },
    )
    monkeypatch.setattr("app.scanner.service._maybe_interpret_project_during_scan", lambda *args, **kwargs: None)

    response = client.post(f"/api/hosts/{host.id}/scan/full", follow_redirects=True)
    assert response.status_code == 200

    project_paths = [project.path for project in session.query(Project).order_by(Project.path.asc()).all()]
    assert project_paths == ["/etc/pve/nodes/byteme3"]


def test_incremental_scan_queue_runs(client, session, monkeypatch):
    host = create_host(session)

    class DummyClient:
        def close(self):
            return None

    monkeypatch.setattr("app.scanner.service.SSHExecutor.open_client", lambda self, _host: DummyClient())
    monkeypatch.setattr(
        "app.scanner.service.scan_host_incremental",
        lambda executor, client, host_obj, general_settings, scan_settings, last_scan_at: {
            "snapshots": [],
            "unchanged_project_ids": [],
            "warnings": [],
        },
    )

    response = client.post(f"/api/hosts/{host.id}/scan/incremental", follow_redirects=True)
    assert response.status_code == 200
    scan = session.query(Scan).order_by(Scan.id.desc()).first()
    assert scan.scan_type == "incremental"


def test_project_interpretation_reviews_all_safe_files_in_batches(session, config, monkeypatch):
    host = create_host(session, name="Byteme2", root_path="/etc/pve/nodes/byteme2/qemu-server")
    project = Project(
        host_id=host.id,
        path=f"host://{host.id}",
        name_guess="Byteme2",
        project_type="host",
        is_git_repo=False,
        fingerprint="host-fingerprint",
        first_seen_at=utcnow(),
        last_seen_at=utcnow(),
        metadata_json={"setup_kind": "host-summary"},
    )
    session.add(project)
    session.commit()
    session.refresh(project)

    files: list[ProjectFile] = []
    for index in range(9):
        file_record = ProjectFile(
            project_id=project.id,
            host_id=host.id,
            root_path="/etc/pve/nodes/byteme2/qemu-server",
            path=f"/etc/pve/nodes/byteme2/qemu-server/{100 + index}.conf",
            relative_path=f"{100 + index}.conf",
            entry_type="file",
            size_bytes=256,
            fingerprint=f"fp-{index}",
            content_sample=None,
        )
        session.add(file_record)
        files.append(file_record)
    session.commit()
    session.refresh(project)
    project.files = files

    monkeypatch.setattr(
        "app.scanner.service._read_file_sample",
        lambda *args, **kwargs: f"sample for {kwargs['file_record'].relative_path}",
    )

    batch_calls = []
    final_packets = []

    def fake_review_batch(session_obj, config_obj, *, project, batch_files, batch_index, batch_count):
        batch_calls.append([file_record.relative_path for file_record in batch_files])
        return type(
            "BatchResult",
            (),
            {
                "batch_summary": f"batch {batch_index}",
                "notable_points": [f"{file_record.relative_path} reviewed" for file_record in batch_files],
            },
        )()

    def fake_interpret(session_obj, config_obj, project_id, *, project_packet_override=None, stream_callback=None):
        final_packets.append(project_packet_override)
        return ProjectInterpretation(project_id=project_id, status="completed", summary="done", prompt_version="test")

    monkeypatch.setattr("app.lmstudio.service.review_project_batch", fake_review_batch)
    monkeypatch.setattr("app.lmstudio.service.interpret_project", fake_interpret)

    scan_settings = ScanSettingsPayload(
        ignore_patterns=[".git"],
        allowed_marker_files=["README.md"],
        max_read_bytes=512,
        collect_shallow_file_trees=True,
        collect_recent_git_metadata=True,
    )

    interpretation = _interpret_project_with_review(
        session,
        config,
        executor=None,
        client=None,
        host=host,
        project=project,
        scan_settings=scan_settings,
    )

    assert interpretation.status == "completed"
    assert batch_calls == [
        ["100.conf", "101.conf", "102.conf", "103.conf"],
        ["104.conf", "105.conf", "106.conf", "107.conf"],
        ["108.conf"],
    ]
    assert len(final_packets) == 1
    assert len(final_packets[0]["batch_reviews"]) == 3
    assert final_packets[0]["review_coverage"]["reviewed_file_count"] == 9
    assert final_packets[0]["review_coverage"]["batch_count"] == 3
    assert final_packets[0]["reviewed_files"] == []
    session.expire_all()
    refreshed = (
        session.query(ProjectFile)
        .filter(ProjectFile.project_id == project.id)
        .order_by(ProjectFile.relative_path.asc())
        .all()
    )
    assert all(file_record.last_ai_reviewed_at is not None for file_record in refreshed)
    assert all(file_record.content_sample for file_record in refreshed)
