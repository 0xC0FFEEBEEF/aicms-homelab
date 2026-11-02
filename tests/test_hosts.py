from __future__ import annotations

import html
import socket
from threading import Event

import pytest

from app.jobs.cancellation import JobCancelled
from app.scanner.ssh import SSHExecutor
from app.models import Host
from app.schemas import GENERAL_SETTINGS_KEY, ConnectionTestResult, GeneralSettingsPayload, RootCheckResult
from app.utils.settings_store import set_settings

from tests.conftest import create_host


def test_host_creation(client, session):
    response = client.post(
        "/api/hosts",
        data={
            "name": "app-vm",
            "address": "10.20.0.12",
            "ssh_port": 22,
            "ssh_user": "scanner",
            "ssh_key_path": "~/.ssh/aicms",
            "enabled": "on",
            "roots_text": "/srv/projects\n/opt/apps",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    body = response.text
    assert "app-vm" in body
    host = session.query(Host).one()
    assert len(host.roots) == 2


def test_host_creation_deduplicates_duplicate_roots(client, session):
    response = client.post(
        "/api/hosts",
        data={
            "name": "app-vm",
            "address": "10.20.0.12",
            "ssh_port": 22,
            "ssh_user": "scanner",
            "ssh_key_path": "~/.ssh/aicms",
            "enabled": "on",
            "roots_text": "/home/golem\n/home/golem\n/home/golem",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    host = session.query(Host).one()
    assert [root.root_path for root in host.roots] == ["/home/golem"]


def test_host_creation_rejects_placeholder_roots(client, session):
    response = client.post(
        "/api/hosts",
        data={
            "name": "pve-1",
            "address": "192.0.2.20",
            "ssh_port": 22,
            "ssh_user": "aicms",
            "ssh_key_path": "~/.ssh/aicms",
            "enabled": "on",
            "roots_text": "/etc/pve/nodes/<your-node>/qemu-server",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "Replace placeholder roots like <your-node> with the real path for this host." in html.unescape(response.text)
    assert session.query(Host).count() == 0


def test_host_creation_normalizes_public_key_path(client, session):
    response = client.post(
        "/api/hosts",
        data={
            "name": "app-vm",
            "address": "10.20.0.12",
            "ssh_port": 22,
            "ssh_user": "scanner",
            "ssh_key_path": "~/.ssh/aicms_scanner_ed25519.pub",
            "enabled": "on",
            "roots_text": "/srv/projects",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    host = session.query(Host).one()
    assert host.ssh_key_path == "~/.ssh/aicms_scanner_ed25519"


def test_host_creation_autofills_default_scanner_key_path(client, session, app):
    set_settings(
        session,
        GENERAL_SETTINGS_KEY,
        GeneralSettingsPayload(
            app_name="AICMS Test",
            timezone="US/Central",
            default_max_tree_depth=2,
            default_scanner_ssh_key_path="~/.ssh/custom_aicms_key",
        ),
    )

    response = client.post(
        "/api/hosts",
        data={
            "name": "app-vm",
            "address": "10.20.0.12",
            "ssh_port": 22,
            "ssh_user": "scanner",
            "ssh_key_path": "",
            "enabled": "on",
            "roots_text": "/srv/projects",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    host = session.query(Host).one()
    assert host.ssh_key_path == "~/.ssh/custom_aicms_key"


def test_host_update_reuses_existing_root_rows(client, session):
    host = create_host(session, root_path="/home/golem")

    response = client.post(
        "/api/hosts",
        data={
            "host_id": host.id,
            "name": "vm-1-renamed",
            "address": host.address,
            "ssh_port": host.ssh_port,
            "ssh_user": host.ssh_user,
            "ssh_key_path": "",
            "enabled": "on",
            "roots_text": "/home/golem\n/opt/apps",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    session.refresh(host)
    assert host.name == "vm-1-renamed"
    assert sorted(root.root_path for root in host.roots) == ["/home/golem", "/opt/apps"]


def test_bootstrap_host_route_updates_connection_status(client, session, monkeypatch):
    def fake_bootstrap_host_access(self, host, *, bootstrap_user, bootstrap_password, sudo_password, roots):
        return ConnectionTestResult(
            success=True,
            message="Bootstrap completed. SSH connectivity test succeeded.",
            root_results=[RootCheckResult(root_path="/srv/projects", exists=True, message="ok")],
        )

    monkeypatch.setattr("app.web.api.SSHExecutor.bootstrap_host_access", fake_bootstrap_host_access)

    response = client.post(
        "/api/hosts/bootstrap",
        data={
            "name": "golem",
            "address": "golem.lan",
            "ssh_port": 22,
            "ssh_user": "aicms",
            "ssh_key_path": "~/.ssh/aicms_scanner_ed25519.pub",
            "enabled": "on",
            "roots_text": "/home/golem\n/opt/apps",
            "bootstrap_user": "root",
            "bootstrap_password": "secret",
            "sudo_password": "",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    host = session.query(Host).one()
    assert host.connection_status == "reachable"


def test_bootstrap_host_route_shows_hostname_resolution_error(client, session, monkeypatch):
    def fake_bootstrap_host_access(self, host, *, bootstrap_user, bootstrap_password, sudo_password, roots):
        raise RuntimeError("Could not resolve SSH hostname 'byteme3.lan'. Use a reachable IP address or a hostname your AICMS VM can resolve.")

    monkeypatch.setattr("app.web.api.SSHExecutor.bootstrap_host_access", fake_bootstrap_host_access)

    response = client.post(
        "/api/hosts/bootstrap",
        data={
            "name": "Byteme3",
            "address": "byteme3.lan",
            "ssh_port": 22,
            "ssh_user": "aicms",
            "ssh_key_path": "~/.ssh/aicms_scanner_ed25519",
            "enabled": "on",
            "roots_text": "/opt",
            "bootstrap_user": "root",
            "bootstrap_password": "secret",
            "sudo_password": "",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Could not resolve SSH hostname 'byteme3.lan'" in html.unescape(response.text)


def test_host_connection_test_flashes_missing_root_details(client, session, monkeypatch):
    host = create_host(session, root_path="/home/golem")

    def fake_test_connection(self, _host, _roots):
        return ConnectionTestResult(
            success=False,
            message="SSH connectivity succeeded, but one or more roots are missing or inaccessible.",
            root_results=[
                RootCheckResult(
                    root_path="/home/golem",
                    exists=False,
                    message="Root exists but is not readable/traversable by the SSH user.",
                )
            ],
        )

    monkeypatch.setattr("app.web.api.SSHExecutor.test_connection", fake_test_connection)

    response = client.post(f"/api/hosts/{host.id}/test", follow_redirects=True)
    assert response.status_code == 200
    assert "/home/golem: Root exists but is not readable/traversable by the SSH user." in response.text


def test_bootstrap_script_grants_readonly_access_for_allowlisted_roots():
    executor = SSHExecutor(connect_timeout=10, command_timeout=30)
    script = executor._build_bootstrap_setup_script(
        target_user="aicms",
        public_key="ssh-ed25519 AAAA example",
        roots=["/home/golem", "/srv/projects"],
    )

    assert "setfacl -m u:aicms:--x / >/dev/null 2>&1 || true;" in script
    assert "setfacl -m u:aicms:--x /home >/dev/null 2>&1 || true;" in script
    assert "setfacl -R -m u:aicms:rX /home/golem >/dev/null 2>&1 || true;" in script
    assert "setfacl -m u:aicms:--x /srv >/dev/null 2>&1 || true;" in script
    assert "setfacl -R -m u:aicms:rX /srv/projects >/dev/null 2>&1 || true;" in script
    assert "|| true" in script


def test_bootstrap_script_handles_proxmox_pmxcfs_roots():
    executor = SSHExecutor(connect_timeout=10, command_timeout=30)
    script = executor._build_bootstrap_setup_script(
        target_user="aicms",
        public_key="ssh-ed25519 AAAA example",
        roots=["/etc/pve/nodes/byteme3/qemu-server", "/var/lib/vz/snippets"],
    )

    assert "usermod -aG www-data aicms" in script
    assert "setfacl -R -m u:aicms:rX /etc/pve/nodes/byteme3/qemu-server >/dev/null 2>&1 || true;" in script


def test_host_connection_test_success_updates_status(client, session, monkeypatch):
    host = create_host(session)

    def fake_test_connection(self, _host, _roots):
        return ConnectionTestResult(
            success=True,
            message="SSH connectivity test succeeded.",
            root_results=[RootCheckResult(root_path="/srv/projects", exists=True, message="ok")],
        )

    monkeypatch.setattr("app.web.api.SSHExecutor.test_connection", fake_test_connection)

    response = client.post(f"/api/hosts/{host.id}/test", follow_redirects=True)
    assert response.status_code == 200
    session.refresh(host)
    assert host.connection_status == "reachable"


def test_host_connection_test_failure_updates_status(client, session, monkeypatch):
    host = create_host(session)

    def fake_test_connection(self, _host, _roots):
        raise RuntimeError("auth failed")

    monkeypatch.setattr("app.web.api.SSHExecutor.test_connection", fake_test_connection)

    response = client.post(f"/api/hosts/{host.id}/test", follow_redirects=True)
    assert response.status_code == 200
    session.refresh(host)
    assert host.connection_status == "unreachable"


def test_open_client_formats_dns_resolution_error(monkeypatch):
    executor = SSHExecutor(connect_timeout=10, command_timeout=30)
    host = Host(name="pve-1", address="byteme3.lan", ssh_port=22, ssh_user="aicms")

    class FakeClient:
        def set_missing_host_key_policy(self, _policy):
            return None

        def connect(self, **kwargs):
            raise socket.gaierror(-2, "Name or service not known")

    monkeypatch.setattr("app.scanner.ssh.paramiko.SSHClient", FakeClient)
    try:
        executor.open_client(host)
    except RuntimeError as exc:
        assert "Could not resolve SSH hostname 'byteme3.lan'" in str(exc)
    else:
        raise AssertionError("Expected RuntimeError")


def test_ssh_executor_run_honors_stop_event():
    executor = SSHExecutor(connect_timeout=10, command_timeout=30)
    stop_event = Event()
    stop_event.set()

    class FakeChannel:
        def __init__(self):
            self.closed = False

        def exit_status_ready(self):
            return False

        def close(self):
            self.closed = True

    class FakeStream:
        def __init__(self, channel):
            self.channel = channel

        def read(self):
            return b""

    class FakeClient:
        def __init__(self):
            self.channel = FakeChannel()

        def exec_command(self, _command, timeout=None):
            stream = FakeStream(self.channel)
            return None, stream, stream

    client = FakeClient()

    with pytest.raises(JobCancelled):
        executor.run(client, "sleep 60", stop_event=stop_event)

    assert client.channel.closed is True
