from __future__ import annotations

from concurrent.futures import Future
from threading import Event

from sqlalchemy import select

from app.models import Scan, ScanEvent
from app.utils.common import utcnow
from tests.conftest import create_host, create_project


def test_core_routes_return_success(client, session):
    host = create_host(session)
    project = create_project(session, host)

    routes = [
        "/health",
        "/web",
        "/web/hosts",
        "/web/hosts/new",
        f"/web/hosts/{host.id}",
        "/web/projects",
        "/web/projects/preview",
        f"/web/projects/{project.id}",
        "/web/scans",
        "/web/settings",
        "/web/lmstudio",
    ]

    for route in routes:
        response = client.get(route)
        assert response.status_code == 200, route


def test_connectivity_tab_renders_host_specific_commands(client, session):
    host = create_host(session, name="scan-vm", address="10.10.1.15", root_path="/opt/apps")
    response = client.get(f"/web/settings?tab=connectivity&host_id={host.id}")
    assert response.status_code == 200
    assert "ssh-copy-id" in response.text
    assert "/opt/apps" in response.text


def test_project_can_be_removed_from_ui_flow(client, session):
    host = create_host(session)
    project = create_project(session, host)
    project_id = project.id

    response = client.post(
        f"/api/projects/{project_id}/delete",
        data={"return_to": "/web/projects"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Removed project" in response.text
    session.expire_all()
    assert session.get(type(project), project_id) is None


def test_host_can_be_removed_from_ui_flow(client, session):
    host = create_host(session)
    project = create_project(session, host)
    host_id = host.id
    project_id = project.id

    response = client.post(
        f"/api/hosts/{host_id}/delete",
        data={"return_to": "/web/hosts"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert f"Removed host {host.name}." in response.text
    session.expire_all()
    assert session.get(type(host), host_id) is None
    assert session.get(type(project), project_id) is None


def test_project_context_can_be_saved_from_ui_flow(client, session):
    host = create_host(session)
    project = create_project(session, host)

    response = client.post(
        f"/api/projects/{project.id}/context",
        data={
            "added_info": "This VM runs my Golem provider and local watchdog scripts.",
            "extra_instructions": "Write this like an owner update, not a textbook definition.",
            "use_project_token_limit": "on",
            "project_max_tokens": 1536,
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Saved project notes, instructions, and profile settings." in response.text
    session.expire_all()
    saved = session.get(type(project), project.id)
    assert saved.metadata_json["operator_context"]["added_info"] == "This VM runs my Golem provider and local watchdog scripts."
    assert (
        saved.metadata_json["operator_context"]["extra_instructions"]
        == "Write this like an owner update, not a textbook definition."
    )
    assert saved.metadata_json["interpretation_preferences"]["max_output_tokens"] == 1536


def test_queued_scan_can_be_stopped_from_ui_flow(client, session, app):
    scan = Scan(scan_type="full", status="queued", started_at=utcnow())
    session.add(scan)
    session.commit()
    session.refresh(scan)

    with app.state.scan_lock:
        app.state.scan_controls[scan.id] = {"future": Future(), "stop_event": Event()}

    response = client.post(f"/api/scans/{scan.id}/stop", follow_redirects=True)

    assert response.status_code == 200
    assert "Cancelled queued scan" in response.text
    session.expire_all()
    saved_scan = session.get(Scan, scan.id)
    assert saved_scan is not None
    assert saved_scan.status == "cancelled"


def test_finished_scan_can_be_removed_from_ui_flow(client, session):
    scan = Scan(scan_type="full", status="completed", started_at=utcnow(), finished_at=utcnow())
    session.add(scan)
    session.flush()
    session.add(
        ScanEvent(
            scan_id=scan.id,
            level="info",
            event_type="scan_completed",
            message="Finished successfully.",
        )
    )
    session.commit()
    scan_id = scan.id

    response = client.post(f"/api/scans/{scan_id}/delete", follow_redirects=True)

    assert response.status_code == 200
    assert f"Removed scan #{scan_id} from history." in response.text
    session.expire_all()
    assert session.get(Scan, scan_id) is None
    assert session.execute(
        select(ScanEvent).where(ScanEvent.scan_id == scan_id)
    ).scalars().all() == []


def test_scan_status_api_repairs_orphaned_cancelling_scan(client, session):
    scan = Scan(scan_type="full", status="cancelling", started_at=utcnow())
    session.add(scan)
    session.commit()
    session.refresh(scan)

    response = client.get(f"/api/scans/{scan.id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["scan"]["status"] == "cancelled"
    session.refresh(scan)
    assert scan.status == "cancelled"


def test_active_scan_cannot_be_removed_from_ui_flow(client, session):
    scan = Scan(scan_type="full", status="running", started_at=utcnow())
    session.add(scan)
    session.commit()
    session.refresh(scan)

    response = client.post(f"/api/scans/{scan.id}/delete", follow_redirects=True)

    assert response.status_code == 200
    assert f"Stop scan #{scan.id} before removing it from history." in response.text
    session.expire_all()
    assert session.get(Scan, scan.id) is not None


def test_delete_all_scan_history_keeps_active_scans(client, session):
    completed = Scan(scan_type="full", status="completed", started_at=utcnow(), finished_at=utcnow())
    failed = Scan(scan_type="incremental", status="failed", started_at=utcnow(), finished_at=utcnow())
    active = Scan(scan_type="full", status="running", started_at=utcnow())
    session.add_all([completed, failed, active])
    session.commit()
    completed_id = completed.id
    failed_id = failed.id
    active_id = active.id

    response = client.post("/api/scans/delete-all", follow_redirects=True)

    assert response.status_code == 200
    assert "Removed 2 finished scan(s). Kept 1 active scan(s)." in response.text
    session.expire_all()
    assert session.get(Scan, completed_id) is None
    assert session.get(Scan, failed_id) is None
    assert session.get(Scan, active_id) is not None


def test_queued_interpretation_can_be_stopped_from_ui_flow(client, session, app):
    host = create_host(session)
    project = create_project(session, host)

    with app.state.interpretation_lock:
        app.state.interpretation_runs[project.id] = {"status": "queued", "output": "", "error": None}
        app.state.interpretation_controls[project.id] = {"future": Future(), "stop_event": Event()}

    response = client.post(f"/api/projects/{project.id}/interpret/stop", follow_redirects=True)

    assert response.status_code == 200
    assert "Cancelled queued profile refresh" in response.text
    with app.state.interpretation_lock:
        assert app.state.interpretation_runs[project.id]["status"] == "cancelled"


def test_projects_page_exposes_preview_entry_and_collection_preview(client, session):
    host = create_host(session, name="alpha", address="10.20.1.10")
    create_project(session, host, path="/srv/projects/a", marker_name="pyproject.toml")
    create_project(session, host, path="/srv/projects/b", marker_name="package.json")

    response = client.get("/web/projects")
    assert response.status_code == 200
    assert "Website Preview" in response.text

    preview = client.get("/web/projects/preview")
    assert preview.status_code == 200
    assert "Projects" in preview.text
    assert preview.text.count('class="project-card"') >= 2
