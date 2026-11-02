from __future__ import annotations

import httpx

from app.lmstudio.client import LMStudioClient
from app.models import LMStudioModel, MachineProfile, ProjectFile
from app.schemas import (
    LMSTUDIO_SETTINGS_KEY,
    LMStudioSettingsPayload,
    ProjectBatchReviewResult,
    ProjectFileReviewPlan,
    ProjectInterpretationResult,
)
from app.lmstudio.service import _client, build_project_packet, choose_files_for_review, interpret_project as interpret_project_service
from app.utils.settings_store import set_settings

from tests.conftest import create_host, create_project


def test_lmstudio_model_refresh(client, session, app, monkeypatch):
    set_settings(
        session,
        LMSTUDIO_SETTINGS_KEY,
        LMStudioSettingsPayload(
            enabled=True,
            base_url="http://localhost:1234/v1",
            api_token=None,
            selected_model=None,
            temperature=0.2,
            max_tokens=500,
        ),
    )

    monkeypatch.setattr(
        "app.lmstudio.client.LMStudioClient.list_models",
        lambda self: [{"id": "local-model"}, {"id": "backup-model"}],
    )

    response = client.post("/api/lmstudio/models/refresh", follow_redirects=True)
    assert response.status_code == 200
    assert session.query(LMStudioModel).count() == 2


def test_project_interpretation_parsing(client, session, app, monkeypatch):
    host = create_host(session)
    project = create_project(session, host)

    set_settings(
        session,
        LMSTUDIO_SETTINGS_KEY,
        LMStudioSettingsPayload(
            enabled=True,
            base_url="http://localhost:1234/v1",
            api_token=None,
            selected_model="local-model",
            temperature=0.2,
            max_tokens=500,
        ),
    )

    def fake_generate_machine_profile(session_obj, config_obj, project_id, stream_callback=None, include_enrichment=True):
        project_obj = session_obj.get(type(project), project_id)
        record = MachineProfile(
            project_id=project_obj.id,
            host_id=project_obj.host_id,
            machine_kind="virtual_machine",
            story_template_type="project_vm",
            detected_platform="Ubuntu Server",
            detected_role="demo project VM",
            confidence=0.91,
            lead_paragraph="This VM is for my demo project.",
            project_role_summary="My main role for this machine is running the demo workload.",
            host_resources_json={"cpu_cores": 2, "cpu_threads": 2, "memory_gib": 4},
            project_role_json={"primary_purpose": "demo workload", "environment_type": "project_vm", "detected_stack": "Python"},
            setup_details_json=["Observed marker: README.md"],
            contained_workloads_json={},
            detected_components_json=[{"kind": "marker", "name": "README.md", "path": "/srv/projects/demo/README.md"}],
            why_it_matters_json=[],
            notes_json=["None"],
            unknowns_json=["Deployment target is not shown in the scanned metadata."],
            evidence_json=["Observed marker: README.md"],
            llm_model_used="local-model",
        )
        session_obj.add(record)
        session_obj.flush()
        return record

    monkeypatch.setattr("app.scanner.service.generate_machine_profile", fake_generate_machine_profile)

    response = client.post(f"/api/projects/{project.id}/interpret", follow_redirects=True)
    assert response.status_code == 200

    profile = session.query(MachineProfile).order_by(MachineProfile.id.desc()).first()
    assert profile.machine_kind == "virtual_machine"
    assert profile.lead_paragraph == "This VM is for my demo project."

    status_response = client.get(f"/api/projects/{project.id}/interpret/status")
    assert status_response.status_code == 200
    assert status_response.json()["status"] == "completed"
    assert status_response.json()["latest_profile"]["machine_kind"] == "virtual_machine"


def test_build_project_packet_includes_file_inventory(session):
    host = create_host(session)
    project = create_project(session, host)
    project.metadata_json = {
        "host_profile": {"os_pretty_name": "Ubuntu Server", "cpu_count": 4},
        "setup_kind": "home-root",
        "inventory_summary": {"observed_names": ["golem"], "entry_count": 12},
        "operator_context": {
            "added_info": "This VM runs my Golem provider.",
            "extra_instructions": "Describe the setup like an owner update.",
        },
    }
    file_record = ProjectFile(
        project_id=project.id,
        host_id=host.id,
        root_path=project.path,
        path=f"{project.path}/README.md",
        relative_path="README.md",
        entry_type="file",
        size_bytes=32,
        fingerprint="file-fingerprint",
        content_sample="# Demo",
    )
    session.add(file_record)
    session.commit()
    session.refresh(project)
    project.files = [file_record]

    packet = build_project_packet(project, files=[file_record], requested_paths=["README.md"])
    assert packet["file_inventory"][0]["relative_path"] == "README.md"
    assert packet["reviewed_files"][0]["content_sample"] == "# Demo"
    assert packet["host_profile"]["cpu_count"] == 4
    assert packet["setup_kind"] == "home-root"
    assert packet["inventory_summary"]["observed_names"] == ["golem"]
    assert packet["operator_context"]["added_info"] == "This VM runs my Golem provider."
    assert packet["operator_context"]["extra_instructions"] == "Describe the setup like an owner update."


def test_choose_files_for_review_filters_unknown_paths(session, app, monkeypatch):
    host = create_host(session)
    project = create_project(session, host)
    file_record = ProjectFile(
        project_id=project.id,
        host_id=host.id,
        root_path=project.path,
        path=f"{project.path}/README.md",
        relative_path="README.md",
        entry_type="file",
        size_bytes=32,
        fingerprint="file-fingerprint",
    )
    session.add(file_record)
    session.commit()
    session.refresh(project)
    project.files = [file_record]

    set_settings(
        session,
        LMSTUDIO_SETTINGS_KEY,
        LMStudioSettingsPayload(
            enabled=True,
            base_url="http://localhost:1234/v1",
            api_token=None,
            selected_model="local-model",
            temperature=0.2,
            max_tokens=500,
        ),
    )

    monkeypatch.setattr(
        "app.lmstudio.client.LMStudioClient.plan_file_review",
        lambda self, **kwargs: ProjectFileReviewPlan(requested_paths=["README.md", "missing.txt"], notes=["reason"]),
    )

    plan = choose_files_for_review(session, app.state.config, project=project, candidate_files=[file_record])
    assert plan.requested_paths == ["README.md"]


def test_plan_file_review_recovers_paths_from_plain_text(monkeypatch):
    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": "Review README.md and compose.yaml next. README.md explains the setup and compose.yaml shows the services."
                        }
                    }
                ]
            }

    monkeypatch.setattr("app.lmstudio.client.httpx.post", lambda *args, **kwargs: FakeResponse())

    client = LMStudioClient(base_url="http://localhost:1234/v1", timeout=10)
    plan = client.plan_file_review(
        model_id="local-model",
        project_packet={
            "file_inventory": [
                {"relative_path": "README.md"},
                {"relative_path": "compose.yaml"},
                {"relative_path": "notes.txt"},
            ]
        },
    )

    assert plan.requested_paths == ["README.md", "compose.yaml"]


def test_prompt_test_uses_simple_chat_payload(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "LM Studio is responding."}}]}

    def fake_post(url, headers, json, timeout):
        captured["payload"] = json
        return FakeResponse()

    monkeypatch.setattr("app.lmstudio.client.httpx.post", fake_post)

    client = LMStudioClient(base_url="http://localhost:1234/v1", timeout=10)
    result = client.prompt_test("local-model")

    assert "response_format" not in captured["payload"]
    assert result["content"] == "LM Studio is responding."


def test_interpret_project_omits_max_tokens_when_unlimited(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                "{"
                                '"asset_kind": "application", '
                                '"canonical_name": "Demo asset", '
                                '"one_line_summary": "Observed files suggest a small demo application.", '
                                '"operator_summary": "Observed files suggest a small demo application with API-oriented markers and a straightforward runtime layout.", '
                                '"evidence": ["Observed marker: README.md"], '
                                '"likely_role": "Likely role is a small internal API demo.", '
                                '"primary_stack": "Python", '
                                '"tags": ["api"], '
                                '"confidence": 0.9, '
                                '"risks_or_notes": ["None"], '
                                '"unknowns": ["Deployment target is not shown in the scanned metadata."]'
                                "}"
                            )
                        }
                    }
                ]
            }

    def fake_post(url, headers, json, timeout):
        captured["payload"] = json
        return FakeResponse()

    monkeypatch.setattr("app.lmstudio.client.httpx.post", fake_post)

    client = LMStudioClient(base_url="http://localhost:1234/v1", timeout=10)
    client.interpret_project(
        model_id="local-model",
        project_packet={"path": "/demo"},
        temperature=0.2,
        max_tokens=None,
    )

    assert "max_tokens" not in captured["payload"]


def test_lmstudio_client_uses_custom_timeout(config):
    settings = LMStudioSettingsPayload(
        enabled=True,
        base_url="http://localhost:1234/v1",
        selected_model="local-model",
        use_custom_timeout=True,
        request_timeout_seconds=180,
    )
    client = _client(config, settings)
    assert client.timeout == 180


def test_interpret_project_parses_thinking_output(monkeypatch):
    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                "<think>internal reasoning</think>\n"
                                "{"
                                '"asset_kind": "application", '
                                '"canonical_name": "Demo asset", '
                                '"one_line_summary": "Observed files suggest a small demo application.", '
                                '"operator_summary": "Observed files suggest a small demo application with API-oriented markers and a straightforward runtime layout.", '
                                '"evidence": ["Observed marker: README.md"], '
                                '"likely_role": "Likely role is a small internal API demo.", '
                                '"primary_stack": "Python", '
                                '"tags": ["api"], '
                                '"confidence": 0.9, '
                                '"risks_or_notes": "None", '
                                '"unknowns": ["Deployment target is not shown in the scanned metadata."]'
                                "}"
                            )
                        }
                    }
                ]
            }

    monkeypatch.setattr("app.lmstudio.client.httpx.post", lambda *args, **kwargs: FakeResponse())

    client = LMStudioClient(base_url="http://localhost:1234/v1", timeout=10)
    result = client.interpret_project(
        model_id="local-model",
        project_packet={"path": "/demo"},
        temperature=0.2,
        max_tokens=500,
    )

    assert result.canonical_name == "Demo asset"
    assert result.risks_or_notes == ["None"]
    assert result.evidence == ["Observed marker: README.md"]


def test_review_project_batch_parses_json(monkeypatch):
    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                "{"
                                '"batch_summary": "This batch covers VM configs and a cloudflared tunnel.", '
                                '"notable_points": ["VM 100 has 8 vCPUs", "cloudflared exposes internal services"]'
                                "}"
                            )
                        }
                    }
                ]
            }

    monkeypatch.setattr("app.lmstudio.client.httpx.post", lambda *args, **kwargs: FakeResponse())

    client = LMStudioClient(base_url="http://localhost:1234/v1", timeout=10)
    result = client.review_project_batch(
        model_id="local-model",
        project_packet={"path": "host://2"},
        batch_index=1,
        batch_count=3,
    )

    assert isinstance(result, ProjectBatchReviewResult)
    assert result.batch_summary == "This batch covers VM configs and a cloudflared tunnel."
    assert result.notable_points == ["VM 100 has 8 vCPUs", "cloudflared exposes internal services"]


def test_review_project_batch_recovers_from_plain_text(monkeypatch):
    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                "This batch covers several Proxmox VM config files for media and utility guests.\n"
                                "- VM 120 appears sized for a heavier workload.\n"
                                "- VM 121 and 122 look like lighter service guests.\n"
                                "- The batch is focused on QEMU configs rather than containers."
                            )
                        }
                    }
                ]
            }

    monkeypatch.setattr("app.lmstudio.client.httpx.post", lambda *args, **kwargs: FakeResponse())

    client = LMStudioClient(base_url="http://localhost:1234/v1", timeout=10)
    result = client.review_project_batch(
        model_id="local-model",
        project_packet={"path": "host://2"},
        batch_index=1,
        batch_count=3,
    )

    assert result.batch_summary == "This batch covers several Proxmox VM config files for media and utility guests."
    assert "VM 120 appears sized for a heavier workload." in result.notable_points


def test_interpret_project_uses_host_specific_prompt(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                "{"
                                '"asset_kind": "hypervisor", '
                                '"canonical_name": "Byteme3 hypervisor", '
                                '"one_line_summary": "This appears to be a Proxmox host with guest VM inventory under /etc/pve/nodes/byteme3.", '
                                '"operator_summary": "Observed files include Proxmox VM configuration paths and host sizing metadata. This appears to be infrastructure rather than an application project.", '
                                '"evidence": ["/etc/pve/nodes/byteme3/qemu-server", "host_profile.cpu_count"], '
                                '"likely_role": "Likely role is a Proxmox hypervisor managing guest workloads.", '
                                '"primary_stack": "Python", '
                                '"tags": ["api"], '
                                '"confidence": 0.9, '
                                '"risks_or_notes": ["None"], '
                                '"unknowns": ["Guest purpose still needs human review."]'
                                "}"
                            )
                        }
                    }
                ]
            }

    def fake_post(url, headers, json, timeout):
        captured["payload"] = json
        return FakeResponse()

    monkeypatch.setattr("app.lmstudio.client.httpx.post", fake_post)

    client = LMStudioClient(base_url="http://localhost:1234/v1", timeout=10)
    client.interpret_project(
        model_id="local-model",
        project_packet={
            "path": "/etc/pve/nodes/byteme3",
            "project_type": "virtualization",
            "host_profile": {"os_pretty_name": "Debian GNU/Linux 12", "cpu_count": 8},
            "proxmox_inventory": {"qemu_vm_count": 3},
        },
        temperature=0.2,
        max_tokens=500,
    )

    system_message = captured["payload"]["messages"][0]["content"]
    user_message = captured["payload"]["messages"][1]["content"]
    assert "internal infrastructure inventory assistant" in system_message
    assert "Do not invent services" in system_message
    assert "hypervisor" in user_message
    assert "Keep summaries useful for an admin reviewing inventory in a dashboard." in user_message
    assert "host sizing or platform details" in user_message
    assert "asset_kind" in user_message


def test_interpret_project_normalizes_list_stack_and_text_confidence(monkeypatch):
    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                "{"
                                '"asset_kind": "virtualization", '
                                '"canonical_name": "Byteme3 host", '
                                '"one_line_summary": "This appears to be a virtualization host.", '
                                '"operator_summary": "Observed files suggest a virtualization-focused host with guest configuration present.", '
                                '"evidence": ["/etc/pve/nodes/byteme3/qemu-server/100.conf"], '
                                '"likely_role": "Likely role is virtualization management.", '
                                '"primary_stack": ["proxmox", "qemu"], '
                                '"tags": ["proxmox"], '
                                '"confidence": "high", '
                                '"risks_or_notes": ["Review backups"], '
                                '"unknowns": ["Guest ownership is not shown in config alone."]'
                                "}"
                            )
                        }
                    }
                ]
            }

    monkeypatch.setattr("app.lmstudio.client.httpx.post", lambda *args, **kwargs: FakeResponse())

    client = LMStudioClient(base_url="http://localhost:1234/v1", timeout=10)
    result = client.interpret_project(
        model_id="local-model",
        project_packet={"path": "host://2", "setup_kind": "host-summary"},
        temperature=0.2,
        max_tokens=500,
    )

    assert result.primary_stack == "proxmox, qemu"
    assert result.confidence == 0.85


def test_interpret_project_service_retries_with_compact_packet(session, app, monkeypatch):
    host = create_host(session)
    project = create_project(session, host, path="host://1")
    project.project_type = "host"
    project.metadata_json = {
        "setup_kind": "host-summary",
        "child_projects": [{"name": f"child-{index}", "path": f"/srv/{index}"} for index in range(20)],
        "inventory_summary": {"root_summaries": [{"root_path": f"/srv/{index}"} for index in range(12)]},
        "grouped_roots": ["/srv/projects"],
        "host_profile": {"cpu_count": 8, "memory_gib": 32},
    }
    session.commit()

    set_settings(
        session,
        LMSTUDIO_SETTINGS_KEY,
        LMStudioSettingsPayload(
            enabled=True,
            base_url="http://localhost:1234/v1",
            api_token=None,
            selected_model="local-model",
            temperature=0.2,
            max_tokens=500,
        ),
    )

    packets = []

    def fake_interpret(self, **kwargs):
        packets.append(kwargs["project_packet"])
        if len(packets) == 1:
            request = httpx.Request("POST", "http://localhost:1234/v1/chat/completions")
            response = httpx.Response(400, request=request)
            raise httpx.HTTPStatusError("bad request", request=request, response=response)
        return ProjectInterpretationResult(
            asset_kind="host",
            canonical_name="Host summary",
            one_line_summary="This appears to be a scanned host summary.",
            operator_summary="Observed metadata suggests a host-level inventory summary built from grouped child projects and root summaries.",
            evidence=["grouped_roots", "child_projects"],
            likely_role="Likely role is a host-level inventory summary.",
            primary_stack="Infrastructure",
            tags=["host"],
            confidence=0.8,
            risks_or_notes=["None"],
            unknowns=["Exact child workload purpose still requires review."],
        )

    monkeypatch.setattr("app.lmstudio.client.LMStudioClient.interpret_project", fake_interpret)

    result = interpret_project_service(session, app.state.config, project.id)

    assert result.status == "completed"
    assert len(packets) == 2
    assert len(packets[1]["child_projects"]) < len(packets[0]["child_projects"])


def test_interpret_project_service_uses_project_token_override(session, app, monkeypatch):
    host = create_host(session)
    project = create_project(session, host, path="host://1")
    project.project_type = "host"
    project.metadata_json = {
        "setup_kind": "host-summary",
        "interpretation_preferences": {"max_output_tokens": 1792},
    }
    session.commit()

    set_settings(
        session,
        LMSTUDIO_SETTINGS_KEY,
        LMStudioSettingsPayload(
            enabled=True,
            base_url="http://localhost:1234/v1",
            api_token=None,
            selected_model="local-model",
            temperature=0.2,
            use_max_output_tokens=True,
            max_tokens=500,
        ),
    )

    captured = {}

    def fake_interpret(self, **kwargs):
        captured["max_tokens"] = kwargs["max_tokens"]
        return ProjectInterpretationResult(
            asset_kind="host",
            canonical_name="Host summary",
            one_line_summary="This appears to be a scanned host summary.",
            operator_summary="Observed metadata suggests a host-level inventory summary built from grouped child projects and root summaries.",
            evidence=["grouped_roots", "child_projects"],
            likely_role="Likely role is a host-level inventory summary.",
            primary_stack="Infrastructure",
            tags=["host"],
            confidence=0.8,
            risks_or_notes=["None"],
            unknowns=["Exact child workload purpose still requires review."],
        )

    monkeypatch.setattr("app.lmstudio.client.LMStudioClient.interpret_project", fake_interpret)

    result = interpret_project_service(session, app.state.config, project.id)

    assert result.status == "completed"
    assert captured["max_tokens"] == 1792
