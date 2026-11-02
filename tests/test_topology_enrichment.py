from __future__ import annotations

from app.mcp.service import run_mcp_enrichment
from app.models import EnrichmentEvidence, EnrichmentRun, EnrichmentSource, GuestInventory, Host
from app.schemas import (
    MCP_SETTINGS_KEY,
    WEB_SEARCH_SETTINGS_KEY,
    ConnectionTestResult,
    MCPSettingsPayload,
    RootCheckResult,
    WebSearchSettingsPayload,
)
from app.utils.settings_store import set_settings
from app.websearch.schemas import WebSearchResultItem
from app.websearch.service import run_web_search_enrichment
from tests.conftest import create_host, create_project


def test_discovered_guest_enrollment_links_to_parent_host(client, session):
    hypervisor = create_host(session, name="byteme2", address="10.20.1.2", root_path="/etc/pve/nodes/byteme2/qemu-server")
    guest = GuestInventory(
        host_id=hypervisor.id,
        guest_id="100",
        guest_type="qemu",
        name="golem",
        status="running",
        cores=4,
        ram_mb=4096,
        disk_summary="scsi0:64G",
        management_state="discovered",
        relationship_source="proxmox_discovery",
    )
    session.add(guest)
    session.commit()
    session.refresh(guest)

    response = client.post(
        f"/api/guests/{guest.id}/enroll",
        data={
            "name": "golem",
            "address": "10.20.1.50",
            "ssh_port": 22,
            "ssh_user": "aicms",
            "ssh_key_path": "~/.ssh/aicms_scanner_ed25519",
            "enabled": "on",
            "roots_text": "/home/golem\n/opt",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    session.expire_all()
    child_host = session.query(Host).filter(Host.id != hypervisor.id).one()
    linked_guest = session.get(GuestInventory, guest.id)
    assert child_host.parent_host_id == hypervisor.id
    assert child_host.parent_name == hypervisor.name
    assert child_host.guest_type == "qemu"
    assert child_host.guest_id == "100"
    assert linked_guest.linked_host_id == child_host.id
    assert linked_guest.management_state == "enrolled"


def test_discovered_guest_bootstrap_triggers_connectivity_and_fact_refresh(client, session, monkeypatch):
    hypervisor = create_host(session, name="byteme2", address="10.20.1.2", root_path="/etc/pve/nodes/byteme2/qemu-server")
    guest = GuestInventory(
        host_id=hypervisor.id,
        guest_id="200",
        guest_type="lxc",
        name="tools",
        status="running",
        management_state="discovered",
        relationship_source="proxmox_discovery",
    )
    session.add(guest)
    session.commit()
    session.refresh(guest)

    refreshed_hosts: list[int] = []

    def fake_bootstrap(self, host, *, bootstrap_user, bootstrap_password, sudo_password, roots):
        return ConnectionTestResult(
            success=True,
            message="Bootstrap completed. SSH connectivity test succeeded.",
            root_results=[RootCheckResult(root_path="/srv/projects", exists=True, message="ok")],
        )

    def fake_test_connection(self, host, roots):
        return ConnectionTestResult(
            success=True,
            message="SSH connectivity test succeeded.",
            root_results=[RootCheckResult(root_path=roots[0], exists=True, message="ok")],
        )

    def fake_refresh_host_facts(session_obj, config_obj, host_id):
        refreshed_hosts.append(host_id)
        host = session_obj.get(Host, host_id)
        host.connection_status = "reachable"
        return None

    monkeypatch.setattr("app.web.api.SSHExecutor.bootstrap_host_access", fake_bootstrap)
    monkeypatch.setattr("app.web.api.SSHExecutor.test_connection", fake_test_connection)
    monkeypatch.setattr("app.web.api.refresh_host_facts", fake_refresh_host_facts)

    response = client.post(
        f"/api/guests/{guest.id}/bootstrap",
        data={
            "name": "tools",
            "address": "10.20.1.60",
            "ssh_port": 22,
            "ssh_user": "aicms",
            "ssh_key_path": "~/.ssh/aicms_scanner_ed25519",
            "enabled": "on",
            "roots_text": "/srv/projects",
            "bootstrap_user": "root",
            "bootstrap_password": "secret",
            "sudo_password": "",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    session.expire_all()
    child_host = session.query(Host).filter(Host.id != hypervisor.id).one()
    linked_guest = session.get(GuestInventory, guest.id)
    assert refreshed_hosts == [child_host.id]
    assert child_host.parent_host_id == hypervisor.id
    assert linked_guest.linked_host_id == child_host.id
    assert linked_guest.management_state == "enrolled"


def test_host_detail_page_renders_discovered_guest_actions(client, session):
    hypervisor = create_host(session, name="byteme2", address="10.20.1.2", root_path="/etc/pve/nodes/byteme2/qemu-server")
    session.add(
        GuestInventory(
            host_id=hypervisor.id,
            guest_id="100",
            guest_type="qemu",
            name="golem",
            status="running",
            management_state="discovered",
            relationship_source="proxmox_discovery",
        )
    )
    session.commit()

    response = client.get(f"/web/hosts/{hypervisor.id}")
    assert response.status_code == 200
    assert "Discovered-only guests" in response.text
    assert "Add as host" in response.text
    assert "Mark unmanaged" in response.text


def test_mcp_enrichment_persists_evidence(session, app, monkeypatch):
    host = create_host(session)
    project = create_project(session, host)
    set_settings(
        session,
        MCP_SETTINGS_KEY,
        MCPSettingsPayload(
            enabled=True,
            server_url="http://mcp.internal/rpc",
            allowed_tools=["search*"],
            read_only_search_only=True,
            max_searches_per_profile_refresh=2,
            max_results_items=5,
        ),
    )

    monkeypatch.setattr("app.mcp.client.MCPClient.initialize", lambda self: {"server": "ok"})
    monkeypatch.setattr(
        "app.mcp.client.MCPClient.list_tools",
        lambda self: [
            {"name": "search.docs", "inputSchema": {"properties": {"query": {}, "limit": {}}}},
            {"name": "write.file", "inputSchema": {"properties": {"query": {}}}},
        ],
    )
    monkeypatch.setattr(
        "app.mcp.client.MCPClient.call_tool",
        lambda self, tool_name, arguments: {
            "results": [
                {"title": "Doc", "content": "Observed upstream context for this machine.", "url": "https://example.com/doc", "score": 0.9}
            ]
        },
    )

    result = run_mcp_enrichment(session, app.state.config, project=project, profile_packet={"detected_platform": "Ubuntu Server", "detected_role": "service node"})

    assert result["enabled"] is True
    assert session.query(EnrichmentRun).count() == 1
    assert session.query(EnrichmentSource).count() == 1
    assert session.query(EnrichmentEvidence).count() == 1
    assert session.query(EnrichmentSource).one().tool_name == "search.docs"


def test_web_search_enrichment_persists_evidence(session, app, monkeypatch):
    host = create_host(session)
    project = create_project(session, host)
    set_settings(
        session,
        WEB_SEARCH_SETTINGS_KEY,
        WebSearchSettingsPayload(
            enabled=True,
            engine="searxng",
            query_url_template="http://10.20.2.8:8080/search?q=<query>",
            result_count=3,
        ),
    )

    monkeypatch.setattr(
        "app.websearch.client.WebSearchClient.search",
        lambda self, query: [
            WebSearchResultItem(
                title="Inventory note",
                url="https://example.com/inventory",
                snippet="Observed external note for this node.",
                relevance=0.8,
                raw={"title": "Inventory note"},
            )
        ],
    )
    monkeypatch.setattr("app.websearch.client.WebSearchClient.load_result", lambda self, item: "Loaded external context.")

    result = run_web_search_enrichment(session, app.state.config, project=project, profile_packet={"detected_platform": "Ubuntu Server", "detected_role": "service node"})

    assert result["enabled"] is True
    assert session.query(EnrichmentRun).count() == 1
    assert session.query(EnrichmentEvidence).count() == 1
    assert session.query(EnrichmentEvidence).one().source_type == "web_search"
