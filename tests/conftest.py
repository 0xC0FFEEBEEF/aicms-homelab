from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import AppConfig
from app.main import create_app
from app.models import Host, HostRoot, Project, ProjectMarker
from app.utils.common import utcnow


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        app_name="AICMS Test",
        secret_key="test-secret-key",
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        sync_jobs=True,
    )


@pytest.fixture
def app(config: AppConfig):
    app = create_app(config)
    app.state.repo_path = Path.cwd()
    return app


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def session(app):
    session = app.state.session_factory()
    try:
        yield session
    finally:
        session.close()


def create_host(session, *, name: str = "vm-1", address: str = "192.0.2.10", root_path: str = "/srv/projects") -> Host:
    host = Host(
        name=name,
        address=address,
        ssh_port=22,
        ssh_user="scanner",
        enabled=True,
        connection_status="unknown",
        roots=[HostRoot(root_path=root_path, enabled=True)],
    )
    session.add(host)
    session.commit()
    session.refresh(host)
    return host


def create_project(session, host: Host, *, path: str = "/srv/projects/demo", marker_name: str = "pyproject.toml") -> Project:
    project = Project(
        host_id=host.id,
        path=path,
        name_guess="demo",
        project_type="python",
        is_git_repo=True,
        repo_branch="main",
        head_commit="abc123",
        fingerprint="fingerprint",
        first_seen_at=utcnow(),
        last_seen_at=utcnow(),
        metadata_json={"marker_names": [marker_name]},
        shallow_tree_json=[{"type": "file", "path": marker_name}],
        markers=[ProjectMarker(marker_name=marker_name, marker_path=f"{path}/{marker_name}")],
    )
    session.add(project)
    session.commit()
    session.refresh(project)
    return project
