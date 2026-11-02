from __future__ import annotations

from threading import Event
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

import app.investigations.service as investigation_service
from app.publish_preview.service import build_investigation_working_preview
from app.models import (
    Host,
    InvestigationMessage,
    InvestigationSession,
    MachineIdentityResult,
    Project,
    ProjectClarificationGuidance,
    ProjectMappingPreview,
)
from app.utils.common import utcnow
from tests.conftest import create_host, create_project


def _load_investigation(session, investigation_id: int) -> InvestigationSession:
    return investigation_service.load_investigation_session(session, investigation_id)


def _identity(project: Project, host: Host, *, signature_id: str = "generic_project_vm", display_name: str = "Generic Project VM", confidence: float = 0.55) -> MachineIdentityResult:
    return MachineIdentityResult(
        project_id=project.id,
        host_id=host.id,
        primary_signature_id=signature_id,
        primary_signature_name=display_name,
        machine_kind="project_node",
        story_template_type="project_vm",
        detected_platform="Linux VM",
        detected_role="project workload",
        stack_tags_json=["Linux"],
        identity_confidence=confidence,
        identity_evidence_json=["Observed README.md"],
        alternate_matches_json=[],
        ambiguity_notes_json=["Identity still needs clarification."] if confidence < 0.78 else [],
        local_facts_evidence_json=["Observed Ubuntu 24.04."],
        topology_evidence_json=[],
        last_resolved_at=utcnow(),
    )


def _mapping(project: Project, host: Host, *, publish_title: str | None = None) -> ProjectMappingPreview:
    return ProjectMappingPreview(
        project_id=project.id,
        host_id=host.id,
        primary_signature_id="generic_project_vm",
        project_title_candidate=publish_title,
        project_summary_candidate="Current draft summary.",
        tech_tags_candidate_json=["Linux"],
        card_title_candidate=publish_title,
        card_summary_candidate="Short card summary.",
        card_tech_tags_candidate_json=["Linux"],
        publish_title_candidate=publish_title,
        publish_summary_candidate="Publish summary.",
        publish_long_description_candidate="Long publish description.",
        publish_sections_json=["Overview"],
        publishable_kind="project",
        recommended_status="needs-review",
        ready_for_cms=False,
        needs_review=True,
        last_generated_at=utcnow(),
    )


def test_investigation_routes_render(client, session, app):
    host = create_host(session, name="alpha", address="10.20.2.20")
    create_project(session, host, path="host://alpha", marker_name="README.md")
    investigation = investigation_service.create_investigation_session(
        session,
        app.state.config,
        target_type="existing_host",
        existing_host_id=host.id,
    )
    session.commit()

    routes = [
        "/web/investigations",
        "/web/investigations/new",
        f"/web/investigations/{investigation.id}",
    ]

    for route in routes:
        response = client.get(route)
        assert response.status_code == 200, route

    detail = client.get(f"/web/investigations/{investigation.id}")
    assert "Assistant Conversation" in detail.text
    assert "Working Draft Preview" in detail.text
    assert "Proposal Review" in detail.text
    assert "Website Preview" in detail.text


def test_create_manual_investigation_session_uses_default_key_and_initial_message(session, config):
    investigation = investigation_service.create_investigation_session(
        session,
        config,
        target_type="manual_ssh_target",
        name="pgsql",
        address="10.20.2.44",
        ssh_port=2222,
        ssh_user="aicms",
        roots_text="/srv/postgres\n/opt/ops",
    )
    session.commit()

    loaded = _load_investigation(session, investigation.id)
    assert loaded.target_type == "manual_ssh_target"
    assert loaded.target_config_json["ssh_key_path"] == "~/.ssh/aicms_scanner_ed25519"
    assert loaded.target_config_json["roots"] == ["/srv/postgres", "/opt/ops"]
    assert loaded.messages
    assert all(message.hidden_internal for message in loaded.messages)


def test_investigation_preview_route_uses_session_draft_over_saved_project_mapping(client, session, app):
    host = create_host(session, name="alpha", address="10.20.2.20")
    project = create_project(session, host, path="host://alpha", marker_name="README.md")
    session.add(_identity(project, host, signature_id="mcp_gateway", display_name="MCP Gateway", confidence=0.88))
    session.add(
        ProjectMappingPreview(
            project_id=project.id,
            host_id=host.id,
            primary_signature_id="mcp_gateway",
            project_title_candidate="Saved Project Title",
            project_summary_candidate="Saved project summary.",
            tech_tags_candidate_json=["MCP"],
            card_title_candidate="Saved Card Title",
            card_summary_candidate="Saved card summary.",
            card_tech_tags_candidate_json=["MCP"],
            publish_title_candidate="Saved Publish Title",
            publish_summary_candidate="Saved publish summary.",
            publish_long_description_candidate="Saved publish long description.",
            publish_sections_json=["Saved section"],
            publishable_kind="project",
            recommended_status="needs-review",
            ready_for_cms=False,
            needs_review=True,
            last_generated_at=utcnow(),
        )
    )
    investigation = investigation_service.create_investigation_session(
        session,
        app.state.config,
        target_type="existing_host",
        existing_host_id=host.id,
    )
    investigation.draft_report_json = {
        "title": "Investigation Alpha",
        "overview": "My live draft overview for alpha.",
        "report_text": "Investigation draft body for alpha.",
        "publish_title": "Investigation Preview Title",
        "publish_summary": "Investigation publish summary.",
        "publish_long_description": "Investigation publish long description.\n\n- Draft bullet",
    }
    investigation.draft_report_text = "Investigation draft body for alpha."
    session.commit()

    response = client.get(f"/web/investigations/{investigation.id}/preview")
    assert response.status_code == 200
    assert "Projects" in response.text
    assert "Investigation Preview Title" in response.text
    assert "Investigation publish long description." in response.text
    assert "Saved Publish Title" not in response.text
    assert "Saved publish long description." not in response.text

    detail_response = client.get(f"/web/investigations/{investigation.id}/preview?mode=detail")
    assert detail_response.status_code == 200
    assert "Project detail preview" in detail_response.text
    assert "Investigation Preview Title" in detail_response.text
    assert "Draft bullet" in detail_response.text


def test_investigation_preview_route_supports_manual_target_without_linked_project(client, session, app):
    investigation = investigation_service.create_investigation_session(
        session,
        app.state.config,
        target_type="manual_ssh_target",
        name="pgsql",
        address="10.20.2.44",
        ssh_user="aicms",
        roots_text="/srv/postgres",
    )
    investigation.raw_facts_json = {
        "os_pretty_name": "Debian GNU/Linux 12",
        "cpu_cores": 4,
        "cpu_threads": 8,
        "memory_gib": 16.0,
        "management_ip": "10.20.2.44",
    }
    investigation.normalized_findings_json = {
        "identity": {
            "machine_kind": "service_node",
            "detected_role": "database VM",
            "detected_platform": "PostgreSQL stack",
            "stack_tags": ["PostgreSQL", "Debian"],
        }
    }
    investigation.draft_report_json = {
        "title": "PostgreSQL Database VM",
        "overview": "My PostgreSQL VM for internal services.",
        "report_text": "My PostgreSQL VM for internal services.\n\n- Stores application data",
        "publish_title": "PostgreSQL Database VM",
        "publish_summary": "My PostgreSQL VM for internal services.",
        "publish_long_description": "My PostgreSQL VM for internal services.\n\n- Stores application data",
    }
    investigation.draft_report_text = "My PostgreSQL VM for internal services.\n\n- Stores application data"
    session.commit()

    response = client.get(f"/web/investigations/{investigation.id}/preview")
    assert response.status_code == 200
    assert "PostgreSQL Database VM" in response.text
    assert "Stores application data" in response.text
    assert "Projects" in response.text


def test_investigation_status_api_returns_working_preview_and_filters_internal_messages(client, session, app):
    host = create_host(session, name="nextcloud", address="10.20.2.12")
    project = create_project(session, host, path="host://nextcloud", marker_name="README.md")
    session.add(_identity(project, host, signature_id="nextcloud", display_name="Nextcloud", confidence=0.86))
    session.add(_mapping(project, host, publish_title="nextcloud Service Workload"))
    investigation = investigation_service.create_investigation_session(
        session,
        app.state.config,
        target_type="existing_host",
        existing_host_id=host.id,
    )
    session.flush()
    investigation.draft_report_json = {
        "assistant_draft": {
            "title": "nextcloud Service Workload",
            "overview": "This is my self-hosted cloud storage setup.",
            "report_text": "This is my self-hosted cloud storage setup.",
            "publish_title": "nextcloud Service Workload",
            "publish_summary": "This is my self-hosted cloud storage setup.",
            "publish_long_description": "This is my self-hosted cloud storage setup.",
        },
        "public_preview_draft": {
            "title": "nextcloud Service Workload",
            "overview": "This is my self-hosted cloud storage setup.",
            "report_text": "This is my self-hosted cloud storage setup.",
            "publish_title": "nextcloud Service Workload",
            "publish_summary": "This is my self-hosted cloud storage setup.",
            "publish_long_description": "This is my self-hosted cloud storage setup.",
        },
        "assistant_draft_version": 2,
        "assistant_draft_updated_at": utcnow().isoformat(),
        "conversation_context_summary": "Earlier conversation context:\n- User: Keep this standalone.",
    }
    session.add(
        InvestigationMessage(
            session_id=investigation.id,
            sender_type="system",
            message_type="status",
            content="Internal only",
            hidden_internal=False,
            affects_generation=False,
        )
    )
    session.add(
        InvestigationMessage(
            session_id=investigation.id,
            sender_type="ai",
            message_type="summary",
            content="I’ve updated the draft to frame this as a standalone Nextcloud service workload.",
            affects_generation=False,
        )
    )
    session.commit()

    response = client.get(f"/api/investigations/{investigation.id}/status")
    assert response.status_code == 200
    payload = response.json()
    assert payload["working_preview"]["title"] == "nextcloud Service Workload"
    assert payload["working_preview"]["publish"]["long_description"] == "This is my self-hosted cloud storage setup."
    assert payload["session"]["draft_report_json"]["conversation_context_summary"].startswith("Earlier conversation context")
    assert all(message["sender_type"] in {"user", "ai"} for message in payload["messages"])


def test_visible_conversation_context_rolls_older_turns_into_summary(session, config):
    investigation = investigation_service.create_investigation_session(
        session,
        config,
        target_type="manual_ssh_target",
        name="pgsql",
        address="10.20.2.44",
        ssh_user="aicms",
        roots_text="/srv/postgres",
    )
    session.flush()
    for index in range(36):
        session.add(
            InvestigationMessage(
                session_id=investigation.id,
                sender_type="user" if index % 2 == 0 else "ai",
                message_type="note" if index % 2 == 0 else "summary",
                content=f"message {index}",
                affects_generation=True,
                hidden_internal=False,
            )
        )
    session.flush()

    recent = investigation_service._investigation_recent_messages(investigation)
    summary = investigation_service._conversation_context_summary(investigation)

    assert len(recent) == investigation_service.CONVERSATION_CONTEXT_WINDOW
    assert summary is not None
    assert "message 0" in summary
    assert "message 35" not in summary


def test_working_preview_uses_last_ai_draft_not_raw_user_reply(session, app):
    host = create_host(session, name="nextcloud", address="10.20.2.12")
    project = create_project(session, host, path="host://nextcloud", marker_name="README.md")
    session.add(_identity(project, host, signature_id="nextcloud", display_name="Nextcloud", confidence=0.86))
    session.add(_mapping(project, host, publish_title="nextcloud Service Workload"))
    investigation = investigation_service.create_investigation_session(
        session,
        app.state.config,
        target_type="existing_host",
        existing_host_id=host.id,
    )
    session.flush()
    investigation.draft_report_json = {
        "assistant_draft": {
            "title": "nextcloud Service Workload",
            "overview": "This is my self-hosted cloud storage setup.",
            "report_text": "This is my self-hosted cloud storage setup.",
            "publish_title": "nextcloud Service Workload",
            "publish_summary": "This is my self-hosted cloud storage setup.",
            "publish_long_description": "This is my self-hosted cloud storage setup.",
        },
        "public_preview_draft": {
            "title": "nextcloud Service Workload",
            "overview": "This is my self-hosted cloud storage setup.",
            "report_text": "This is my self-hosted cloud storage setup.",
            "publish_title": "nextcloud Service Workload",
            "publish_summary": "This is my self-hosted cloud storage setup.",
            "publish_long_description": "This is my self-hosted cloud storage setup.",
        },
    }
    session.add(
        InvestigationMessage(
            session_id=investigation.id,
            sender_type="user",
            message_type="note",
            content="no, go back",
            affects_generation=True,
            hidden_internal=False,
        )
    )
    session.commit()

    preview = build_investigation_working_preview(session, _load_investigation(session, investigation.id))
    assert preview.publish.long_description == "This is my self-hosted cloud storage setup."
    assert "no, go back" not in preview.publish.long_description.lower()


def test_regenerate_keeps_last_assistant_draft_when_llm_returns_no_draft_fields(session, config, monkeypatch):
    host = create_host(session, name="nextcloud", address="10.20.2.12")
    project = create_project(session, host, path="host://nextcloud", marker_name="README.md")
    investigation = investigation_service.create_investigation_session(
        session,
        config,
        target_type="existing_host",
        existing_host_id=host.id,
    )
    investigation.draft_report_json = {
        "assistant_draft": {
            "title": "Nextcloud Storage VM",
            "overview": "This is my self-hosted cloud storage VM.",
            "report_text": "This is my self-hosted cloud storage VM.",
            "publish_title": "Nextcloud Storage VM",
            "publish_summary": "This is my self-hosted cloud storage VM.",
            "publish_long_description": "This is my self-hosted cloud storage VM.",
        },
        "public_preview_draft": {
            "title": "Nextcloud Storage VM",
            "overview": "This is my self-hosted cloud storage VM.",
            "report_text": "This is my self-hosted cloud storage VM.",
            "publish_title": "Nextcloud Storage VM",
            "publish_summary": "This is my self-hosted cloud storage VM.",
            "publish_long_description": "This is my self-hosted cloud storage VM.",
        },
        "assistant_draft_version": 3,
        "assistant_draft_updated_at": utcnow().isoformat(),
    }
    session.commit()

    facts = {
        "os_pretty_name": "Debian GNU/Linux 12",
        "cpu_cores": 12,
        "memory_gib": 8.06,
        "management_ip": host.address,
        "selected_running_services": [{"service_name": "apache2.service"}],
        "guest_inventory": [],
    }
    monkeypatch.setattr(
        investigation_service,
        "refresh_host_facts",
        lambda session_obj, config_obj, host_id: SimpleNamespace(facts_json=facts),
    )
    monkeypatch.setattr(
        investigation_service,
        "refresh_machine_identity",
        lambda session_obj, config_obj, project_id: _identity(project, host, signature_id="nextcloud", display_name="Nextcloud", confidence=0.88),
    )
    monkeypatch.setattr(
        investigation_service,
        "refresh_project_research",
        lambda session_obj, config_obj, project_id, force=False: None,
    )
    monkeypatch.setattr(
        investigation_service,
        "refresh_project_mapping_preview",
        lambda session_obj, config_obj, project_id: _mapping(project, host, publish_title="Different Deterministic Title"),
    )
    monkeypatch.setattr(
        investigation_service,
        "build_public_copy",
        lambda **kwargs: {
            "publish_title": "Different Deterministic Title",
            "publish_summary": "Deterministic summary",
            "publish_long_description": "Deterministic long description.",
            "card_title": "Different Deterministic Title",
            "card_summary": "Deterministic card summary.",
        },
    )
    monkeypatch.setattr(
        investigation_service,
        "_generate_llm_investigation_turn",
        lambda *args, **kwargs: (
            SimpleNamespace(
                assistant_message="I’ve kept the prior public draft and only updated the conversation reply.",
                draft_title=None,
                draft_overview=None,
                draft_report_text=None,
                publish_title=None,
                publish_summary=None,
                publish_long_description=None,
                ready_for_confirmation=True,
                question=None,
            ),
            None,
        ),
    )

    loaded = _load_investigation(session, investigation.id)
    investigation_service.regenerate_investigation_session(session, config, loaded, recollect=True)
    session.commit()

    refreshed = _load_investigation(session, investigation.id)
    state = investigation_service.investigation_draft_state_payload(refreshed)
    assert state["assistant_draft"]["publish_title"] == "Nextcloud Storage VM"
    assert state["assistant_draft"]["publish_long_description"] == "This is my self-hosted cloud storage VM."
    assert refreshed.draft_report_text == "This is my self-hosted cloud storage VM."


def test_investigation_message_requires_recollect_only_for_refresh_style_prompts(session, config):
    investigation = investigation_service.create_investigation_session(
        session,
        config,
        target_type="manual_ssh_target",
        name="nextcloud",
        address="10.20.2.88",
        ssh_user="aicms",
        roots_text="/var/www/nextcloud",
    )
    investigation.raw_facts_json = {"os_pretty_name": "Debian GNU/Linux 12"}
    session.commit()

    loaded = _load_investigation(session, investigation.id)
    assert investigation_service.investigation_message_requires_recollect(loaded, "Please refresh facts and look again.") is True
    assert investigation_service.investigation_message_requires_recollect(loaded, "Frame this as my cloud storage VM.") is False


def test_regenerate_existing_host_builds_findings_and_proposals_without_fallback_question(session, config, monkeypatch):
    host = create_host(session, name="golem", address="10.20.2.25")
    project = create_project(session, host, path="host://golem", marker_name="README.md")
    investigation = investigation_service.create_investigation_session(
        session,
        config,
        target_type="existing_host",
        existing_host_id=host.id,
    )
    session.commit()

    facts = {
        "os_pretty_name": "Ubuntu 24.04 LTS",
        "cpu_cores": 4,
        "memory_gib": 8.0,
        "management_ip": host.address,
        "selected_running_services": [{"service_name": "cron.service"}],
        "guest_inventory": [],
    }
    monkeypatch.setattr(
        investigation_service,
        "refresh_host_facts",
        lambda session_obj, config_obj, host_id: SimpleNamespace(facts_json=facts),
    )
    monkeypatch.setattr(
        investigation_service,
        "refresh_machine_identity",
        lambda session_obj, config_obj, project_id: _identity(project, host, confidence=0.55),
    )
    monkeypatch.setattr(
        investigation_service,
        "refresh_project_research",
        lambda session_obj, config_obj, project_id, force=False: None,
    )
    monkeypatch.setattr(
        investigation_service,
        "refresh_project_mapping_preview",
        lambda session_obj, config_obj, project_id: _mapping(project, host, publish_title=None),
    )
    monkeypatch.setattr(
        investigation_service,
        "build_public_copy",
        lambda **kwargs: {
            "publish_title": None,
            "publish_summary": "This appears to be my golem workload VM.",
            "publish_long_description": "Longer golem workload description.",
            "card_title": None,
            "card_summary": "Short card summary.",
        },
    )

    loaded = _load_investigation(session, investigation.id)
    investigation_service.regenerate_investigation_session(session, config, loaded, recollect=True)
    session.commit()

    refreshed = _load_investigation(session, investigation.id)
    assert refreshed.status == "ready_for_confirmation"
    assert refreshed.findings
    assert refreshed.proposals
    assert not any(message.sender_type == "ai" and message.message_type == "question" for message in refreshed.messages)
    assert not any(message.sender_type == "ai" and message.message_type == "summary" for message in refreshed.messages if not message.hidden_internal)
    assert "This appears to be my golem workload VM." in refreshed.draft_report_text
    assert refreshed.last_error == "LM Studio is disabled or no investigation model is selected."


def test_post_investigation_message_persists_answer_and_chat_confirmation(session, config):
    host = create_host(session, name="mcp", address="10.20.2.40")
    create_project(session, host, path="host://mcp", marker_name="docker-compose.yml")
    investigation = investigation_service.create_investigation_session(
        session,
        config,
        target_type="existing_host",
        existing_host_id=host.id,
    )
    session.flush()
    question = InvestigationMessage(
        session_id=investigation.id,
        sender_type="ai",
        message_type="question",
        content="What exact title do you want me to use?",
        affects_generation=True,
        metadata_json={"question_type": "public_title", "ai_rationale": "Need a better title."},
    )
    session.add(question)
    session.commit()

    loaded = _load_investigation(session, investigation.id)
    message = investigation_service.post_investigation_message(
        session,
        loaded,
        "Looks good to me. Use MCP Control Layer VM.",
    )
    session.commit()

    refreshed = _load_investigation(session, investigation.id)
    assert message.message_type == "answer"
    assert refreshed.confirmation_state == "confirmed"
    assert refreshed.status == "ready_for_confirmation"
    assert any(
        item.sender_type == "system" and item.hidden_internal and "Confirmation noted from chat" in item.content
        for item in refreshed.messages
    )


def test_queue_investigation_collect_marks_pending_refresh_when_worker_is_active(session, app):
    host = create_host(session, name="alpha", address="10.20.2.20")
    create_project(session, host, path="host://alpha", marker_name="README.md")
    investigation = investigation_service.create_investigation_session(
        session,
        app.state.config,
        target_type="existing_host",
        existing_host_id=host.id,
    )
    session.commit()

    investigation_service._set_investigation_control(
        app,
        investigation.id,
        stop_event=Event(),
        future=object(),
        pending_refresh=False,
        pending_recollect=False,
    )
    investigation_service.queue_investigation_collect(app, investigation.id, recollect=True)
    control = investigation_service._get_investigation_control(app, investigation.id)
    session.expire_all()
    refreshed = _load_investigation(session, investigation.id)

    assert control["pending_refresh"] is True
    assert control["pending_recollect"] is True
    assert refreshed.status == "queued"
    investigation_service._pop_investigation_control(app, investigation.id)


def test_run_collect_job_drains_pending_follow_up_pass(session, app, monkeypatch):
    host = create_host(session, name="alpha", address="10.20.2.20")
    create_project(session, host, path="host://alpha", marker_name="README.md")
    investigation = investigation_service.create_investigation_session(
        session,
        app.state.config,
        target_type="existing_host",
        existing_host_id=host.id,
    )
    session.commit()

    calls: list[bool] = []

    def fake_regenerate(session_obj, config_obj, investigation_obj, *, recollect, stop_event=None):
        calls.append(recollect)
        if len(calls) == 1:
            investigation_service._set_investigation_control(
                app,
                investigation_obj.id,
                pending_refresh=True,
                pending_recollect=True,
            )

    monkeypatch.setattr(investigation_service, "regenerate_investigation_session", fake_regenerate)
    investigation_service._set_investigation_control(
        app,
        investigation.id,
        stop_event=Event(),
        future=None,
        pending_refresh=False,
        pending_recollect=False,
    )

    investigation_service._run_collect_job(app, investigation.id, recollect=False)

    assert calls == [False, True]
    assert investigation_service._get_investigation_control(app, investigation.id) == {}


def test_pop_investigation_control_if_current_keeps_newer_worker_control(session, app):
    host = create_host(session, name="alpha", address="10.20.2.20")
    create_project(session, host, path="host://alpha", marker_name="README.md")
    investigation = investigation_service.create_investigation_session(
        session,
        app.state.config,
        target_type="existing_host",
        existing_host_id=host.id,
    )
    session.commit()

    original_stop_event = Event()
    replacement_stop_event = Event()
    investigation_service._set_investigation_control(
        app,
        investigation.id,
        stop_event=original_stop_event,
        future=object(),
        pending_refresh=False,
        pending_recollect=False,
    )

    should_continue, requested_recollect = investigation_service._consume_pending_refresh(
        app,
        investigation.id,
        worker_stop_event=original_stop_event,
    )

    assert should_continue is False
    assert requested_recollect is False
    assert investigation_service._get_investigation_control(app, investigation.id) == {}

    investigation_service._set_investigation_control(
        app,
        investigation.id,
        stop_event=replacement_stop_event,
        future=object(),
        pending_refresh=False,
        pending_recollect=False,
    )
    investigation_service._pop_investigation_control_if_current(
        app,
        investigation.id,
        worker_stop_event=original_stop_event,
    )

    control = investigation_service._get_investigation_control(app, investigation.id)
    assert control["stop_event"] is replacement_stop_event
    investigation_service._pop_investigation_control(app, investigation.id)


def test_sanitize_investigation_draft_rebuilds_fields_after_filtered_content(session, config):
    host = create_host(session, name="nextcloud", address="10.20.2.12")
    project = create_project(session, host, path="host://nextcloud", marker_name="README.md")
    investigation = investigation_service.create_investigation_session(
        session,
        config,
        target_type="existing_host",
        existing_host_id=host.id,
    )
    session.flush()

    guidance = ProjectClarificationGuidance(
        project_id=project.id,
        host_id=host.id,
        preferred_summary_framing="Remove talk of FQDN.",
    )
    draft = {
        "overview": "My inventory also sees the FQDN nextcloud.",
        "report_text": "My inventory also sees the FQDN nextcloud.\n\nThis machine stores my personal cloud files.",
        "publish_summary": "My inventory also sees the FQDN nextcloud.",
        "publish_long_description": "My inventory also sees the FQDN nextcloud.\n\nThis machine stores my personal cloud files.",
        "card_summary": "My inventory also sees the FQDN nextcloud.",
    }

    sanitized = investigation_service._sanitize_investigation_draft(
        investigation,
        project,
        draft,
        guidance=guidance,
    )

    assert sanitized["overview"] == "This machine stores my personal cloud files."
    assert sanitized["publish_summary"] == "This machine stores my personal cloud files."
    assert sanitized["card_summary"] == "This machine stores my personal cloud files."
    assert "fqdn" not in sanitized["publish_long_description"].lower()


def test_regenerate_manual_target_creates_host_proposal(session, config, monkeypatch):
    investigation = investigation_service.create_investigation_session(
        session,
        config,
        target_type="manual_ssh_target",
        name="pgsql",
        address="10.20.2.44",
        ssh_user="aicms",
        roots_text="/srv/postgres",
    )
    session.commit()

    facts = {
        "os_pretty_name": "Debian GNU/Linux 12",
        "cpu_cores": 4,
        "memory_gib": 16.0,
        "management_ip": "10.20.2.44",
        "selected_running_services": [{"service_name": "postgresql.service"}],
        "guest_inventory": [],
    }
    resolved = SimpleNamespace(
        primary_signature_id="generic_service_node",
        primary_signature_name="Generic Service Node",
        machine_kind="service_node",
        detected_platform="Linux VM",
        detected_role="database workload",
        identity_confidence=0.62,
        stack_tags=["PostgreSQL"],
        identity_evidence=["Observed postgresql.service"],
        ambiguity_notes=["Public title still needs review."],
    )
    monkeypatch.setattr(
        investigation_service,
        "_collect_manual_target_facts",
        lambda config_obj, session_obj, stop_event=None: facts,
    )
    monkeypatch.setattr(investigation_service, "resolve_machine_identity", lambda **kwargs: resolved)

    loaded = _load_investigation(session, investigation.id)
    investigation_service.regenerate_investigation_session(session, config, loaded, recollect=True)
    session.commit()

    refreshed = _load_investigation(session, investigation.id)
    assert any(proposal.field_name == "host_record" for proposal in refreshed.proposals)
    assert refreshed.proposed_host_changes_json
    assert refreshed.status == "ready_for_confirmation"


def test_apply_requires_confirmation(session, app, config):
    investigation = investigation_service.create_investigation_session(
        session,
        config,
        target_type="manual_ssh_target",
        name="nextcloud",
        address="10.20.2.60",
        ssh_user="aicms",
        roots_text="/var/www/nextcloud",
    )
    session.commit()
    loaded = _load_investigation(session, investigation.id)

    with pytest.raises(ValueError, match="Confirm the investigation before applying"):
        investigation_service.apply_investigation(session, app, loaded)


def test_confirmed_manual_investigation_creates_host_and_queues_scan(session, app, config, monkeypatch):
    investigation = investigation_service.create_investigation_session(
        session,
        config,
        target_type="manual_ssh_target",
        name="nextcloud",
        address="10.20.2.60",
        ssh_user="aicms",
        roots_text="/var/www/nextcloud",
    )
    session.commit()

    queued: dict[str, object] = {}
    monkeypatch.setattr(
        investigation_service,
        "refresh_host_facts",
        lambda session_obj, config_obj, host_id: SimpleNamespace(facts_json={}),
    )
    monkeypatch.setattr(
        investigation_service,
        "queue_scan",
        lambda app_obj, scan_type, host_ids: queued.update({"scan_type": scan_type, "host_ids": host_ids}),
    )

    loaded = _load_investigation(session, investigation.id)
    investigation_service.confirm_investigation(session, loaded)
    investigation_service.apply_investigation(session, app, loaded)
    session.commit()

    refreshed = _load_investigation(session, investigation.id)
    assert refreshed.status == "applied"
    assert refreshed.linked_host_id is not None
    created_host = session.get(Host, refreshed.linked_host_id)
    assert created_host is not None
    assert created_host.name == "nextcloud"
    assert queued == {"scan_type": "full", "host_ids": [created_host.id]}


def test_confirmed_existing_host_investigation_persists_answer_into_guidance(session, app, config, monkeypatch):
    host = create_host(session, name="pgsql", address="10.20.2.44")
    project = create_project(session, host, path="host://pgsql", marker_name="postgresql.service")
    investigation = investigation_service.create_investigation_session(
        session,
        config,
        target_type="existing_host",
        existing_host_id=host.id,
    )
    session.flush()
    question = InvestigationMessage(
        session_id=investigation.id,
        sender_type="ai",
        message_type="question",
        content="What exact public-facing title should I use?",
        affects_generation=True,
        metadata_json={"question_type": "public_title", "ai_rationale": "Need a better public title."},
    )
    session.add(question)
    session.commit()

    loaded = _load_investigation(session, investigation.id)
    investigation_service.post_investigation_message(session, loaded, "PostgreSQL Database VM")
    investigation_service.confirm_investigation(session, loaded)

    monkeypatch.setattr(
        investigation_service,
        "refresh_host_facts",
        lambda session_obj, config_obj, host_id: SimpleNamespace(facts_json={}),
    )
    monkeypatch.setattr(
        investigation_service,
        "refresh_machine_identity",
        lambda session_obj, config_obj, project_id: SimpleNamespace(),
    )
    monkeypatch.setattr(
        investigation_service,
        "refresh_project_research",
        lambda session_obj, config_obj, project_id, force=False: None,
    )
    monkeypatch.setattr(
        investigation_service,
        "refresh_project_mapping_preview",
        lambda session_obj, config_obj, project_id: None,
    )

    investigation_service.apply_investigation(session, app, loaded)
    session.commit()

    guidance = session.execute(
        select(ProjectClarificationGuidance)
        .where(ProjectClarificationGuidance.project_id == project.id)
    ).scalar_one()
    refreshed = _load_investigation(session, investigation.id)
    assert guidance.preferred_public_title == "PostgreSQL Database VM"
    assert refreshed.linked_project_id == project.id
    assert refreshed.confirmation_state == "applied"


def test_investigation_thread_stays_out_of_public_preview(client, session, app):
    host = create_host(session, name="mcp", address="10.20.2.40")
    project = create_project(session, host, path="host://mcp", marker_name="docker-compose.yml")
    session.add(
        MachineIdentityResult(
            project_id=project.id,
            host_id=host.id,
            primary_signature_id="mcp_gateway",
            primary_signature_name="MCP Gateway",
            machine_kind="service_node",
            story_template_type="service_gateway",
            detected_platform="MCP stack",
            detected_role="AI control layer",
            stack_tags_json=["MCP"],
            identity_confidence=0.92,
            identity_evidence_json=["Observed MCP markers"],
            alternate_matches_json=[],
            ambiguity_notes_json=[],
            local_facts_evidence_json=["Observed Ubuntu 24.04."],
            topology_evidence_json=[],
            last_resolved_at=utcnow(),
        )
    )
    session.add(
        ProjectMappingPreview(
            project_id=project.id,
            host_id=host.id,
            primary_signature_id="mcp_gateway",
            project_title_candidate="MCP Control Layer VM",
            project_summary_candidate="My internal MCP stack.",
            tech_tags_candidate_json=["MCP"],
            card_title_candidate="MCP Control Layer VM",
            card_summary_candidate="Card summary.",
            card_tech_tags_candidate_json=["MCP"],
            publish_title_candidate="MCP Control Layer VM",
            publish_summary_candidate="Short publish summary.",
            publish_long_description_candidate="Longer publish-ready description for my MCP control layer.",
            publish_sections_json=["Overview"],
            publishable_kind="project",
            recommended_status="draft",
            ready_for_cms=True,
            needs_review=False,
            last_generated_at=utcnow(),
        )
    )
    investigation = investigation_service.create_investigation_session(
        session,
        app.state.config,
        target_type="existing_host",
        existing_host_id=host.id,
    )
    session.flush()
    session.add(
        InvestigationMessage(
            session_id=investigation.id,
            sender_type="user",
            message_type="note",
            content="Internal-only investigation note that must never leak.",
            affects_generation=True,
        )
    )
    session.commit()

    response = client.get(f"/web/projects/{project.id}/preview")
    assert response.status_code == 200
    assert "Internal-only investigation note that must never leak." not in response.text


def test_queue_investigation_collect_moves_session_out_of_queued_state(app, session, monkeypatch):
    host = create_host(session, name="alpha", address="10.20.2.20")
    investigation = investigation_service.create_investigation_session(
        session,
        app.state.config,
        target_type="existing_host",
        existing_host_id=host.id,
    )
    session.commit()

    def fake_run_collect_job(app_obj, investigation_id, *, recollect):
        with app_obj.state.session_factory() as worker_session:
            loaded = _load_investigation(worker_session, investigation_id)
            loaded.status = "ready_for_confirmation"
            loaded.current_phase = "ready_for_confirmation"
            worker_session.commit()

    monkeypatch.setattr(investigation_service, "_run_collect_job", fake_run_collect_job)

    investigation_service.queue_investigation_collect(app, investigation.id, recollect=True)
    session.expire_all()
    refreshed = _load_investigation(session, investigation.id)
    assert refreshed.status == "ready_for_confirmation"


def test_request_investigation_stop_cancels_queued_job(session, app, config):
    host = create_host(session, name="alpha", address="10.20.2.20")
    investigation = investigation_service.create_investigation_session(
        session,
        config,
        target_type="existing_host",
        existing_host_id=host.id,
    )
    session.commit()

    class CancelledFuture:
        def cancel(self) -> bool:
            return True

    investigation_service._set_investigation_control(
        app,
        investigation.id,
        future=CancelledFuture(),
        stop_event=Event(),
    )

    message = investigation_service.request_investigation_stop(app, investigation.id)
    session.expire_all()
    refreshed = _load_investigation(session, investigation.id)

    assert "Cancelled queued investigation" in message
    assert refreshed.status == "cancelled"


def test_reconcile_orphaned_investigation_marks_stuck_session_failed(session, app, config):
    host = create_host(session, name="alpha", address="10.20.2.20")
    investigation = investigation_service.create_investigation_session(
        session,
        config,
        target_type="existing_host",
        existing_host_id=host.id,
    )
    investigation.status = "queued"
    investigation.current_phase = "queued"
    session.commit()

    loaded = _load_investigation(session, investigation.id)
    changed = investigation_service.reconcile_orphaned_investigation(app, session, loaded)
    session.commit()

    refreshed = _load_investigation(session, investigation.id)
    assert changed is True
    assert refreshed.status == "failed"
    assert "no longer active" in (refreshed.last_error or "").lower()


def test_regenerate_investigation_uses_llm_turn_and_does_not_repeat_answered_question(session, config, monkeypatch):
    host = create_host(session, name="byteme2", address="10.20.2.12")
    project = create_project(session, host, path="host://byteme2", marker_name="README.md")
    investigation = investigation_service.create_investigation_session(
        session,
        config,
        target_type="existing_host",
        existing_host_id=host.id,
    )
    session.flush()
    question = InvestigationMessage(
        session_id=investigation.id,
        sender_type="ai",
        message_type="question",
        content="What exact public-facing title do you want me to use?",
        affects_generation=True,
        metadata_json={"question_type": "public_title", "ai_rationale": "Need a stronger title."},
    )
    session.add(question)
    session.flush()
    investigation_service.post_investigation_message(session, investigation, "Use Byteme2 Proxmox Hypervisor.")
    session.commit()

    facts = {
        "os_pretty_name": "Debian GNU/Linux 13",
        "cpu_cores": 24,
        "memory_gib": 64.0,
        "management_ip": host.address,
        "selected_running_services": [{"service_name": "pveproxy.service"}],
        "guest_inventory": [],
    }
    monkeypatch.setattr(
        investigation_service,
        "refresh_host_facts",
        lambda session_obj, config_obj, host_id: SimpleNamespace(facts_json=facts),
    )
    monkeypatch.setattr(
        investigation_service,
        "refresh_machine_identity",
        lambda session_obj, config_obj, project_id: _identity(
            project,
            host,
            signature_id="proxmox_hypervisor",
            display_name="Proxmox Hypervisor",
            confidence=0.95,
        ),
    )
    monkeypatch.setattr(
        investigation_service,
        "refresh_project_research",
        lambda session_obj, config_obj, project_id, force=False: None,
    )
    monkeypatch.setattr(
        investigation_service,
        "refresh_project_mapping_preview",
        lambda session_obj, config_obj, project_id: _mapping(project, host, publish_title="Byteme2 Proxmox Hypervisor"),
    )
    monkeypatch.setattr(
        investigation_service,
        "build_public_copy",
        lambda **kwargs: {
            "publish_title": "Byteme2 Proxmox Hypervisor",
            "publish_summary": "My main Proxmox hypervisor.",
            "publish_long_description": "Longer hypervisor description.",
            "card_title": "Byteme2 Proxmox Hypervisor",
            "card_summary": "Short card summary.",
        },
    )
    monkeypatch.setattr(
        investigation_service,
        "_generate_llm_investigation_turn",
        lambda *args, **kwargs: (
            SimpleNamespace(
                assistant_message="I updated the draft to use your preferred title and reframed the host as a Proxmox hypervisor.",
                draft_title="Byteme2 Proxmox Hypervisor",
                draft_overview="This investigation now treats the target as my Proxmox hypervisor.",
                draft_report_text="This investigation now treats the target as my Proxmox hypervisor.\n\nI applied your preferred title throughout the draft.",
                publish_title="Byteme2 Proxmox Hypervisor",
                publish_summary="My main Proxmox hypervisor.",
                ready_for_confirmation=True,
                question=SimpleNamespace(
                    question_text="What exact public-facing title do you want me to use?",
                    question_type="public_title",
                    ai_rationale="Need a stronger title.",
                ),
            ),
            None,
        ),
    )

    loaded = _load_investigation(session, investigation.id)
    investigation_service.regenerate_investigation_session(session, config, loaded, recollect=False)
    session.commit()

    refreshed = _load_investigation(session, investigation.id)
    ai_questions = [message for message in refreshed.messages if message.sender_type == "ai" and message.message_type == "question"]
    ai_summaries = [message for message in refreshed.messages if message.sender_type == "ai" and message.message_type == "summary"]

    assert len(ai_questions) == 1
    assert any("updated the draft" in message.content.lower() for message in ai_summaries)
    assert refreshed.status == "ready_for_confirmation"
    assert "preferred title" in refreshed.draft_report_text.lower()


def test_regenerate_investigation_preserves_last_assistant_draft_when_new_turn_has_no_draft_update(session, config, monkeypatch):
    host = create_host(session, name="nextcloud", address="10.20.2.12")
    project = create_project(session, host, path="host://nextcloud", marker_name="README.md")
    investigation = investigation_service.create_investigation_session(
        session,
        config,
        target_type="existing_host",
        existing_host_id=host.id,
    )
    investigation_service._persist_investigation_draft_state(
        investigation,
        base_draft={
            "title": "nextcloud Service Workload",
            "overview": "Old base draft.",
            "report_text": "Old base draft.",
            "publish_title": "nextcloud Service Workload",
            "publish_summary": "Old base draft.",
            "publish_long_description": "Old base draft.",
        },
        working_draft=None,
        assistant_draft={
            "title": "nextcloud Service Workload",
            "overview": "Assistant-owned overview.",
            "report_text": "Assistant-owned long draft.",
            "publish_title": "nextcloud Service Workload",
            "publish_summary": "Assistant-owned overview.",
            "publish_long_description": "Assistant-owned long draft.",
        },
        public_preview_draft={
            "title": "nextcloud Service Workload",
            "overview": "Assistant-owned overview.",
            "report_text": "Assistant-owned long draft.",
            "publish_title": "nextcloud Service Workload",
            "publish_summary": "Assistant-owned overview.",
            "publish_long_description": "Assistant-owned long draft.",
        },
        assistant_draft_version=2,
        assistant_draft_updated_at=utcnow().isoformat(),
    )
    session.commit()

    facts = {
        "os_pretty_name": "Debian GNU/Linux 12",
        "cpu_cores": 12,
        "memory_gib": 8.0,
        "management_ip": host.address,
        "selected_running_services": [{"service_name": "apache2.service"}],
        "guest_inventory": [],
    }
    monkeypatch.setattr(
        investigation_service,
        "refresh_host_facts",
        lambda session_obj, config_obj, host_id: SimpleNamespace(facts_json=facts),
    )
    monkeypatch.setattr(
        investigation_service,
        "refresh_machine_identity",
        lambda session_obj, config_obj, project_id: _identity(project, host, signature_id="nextcloud", display_name="Nextcloud", confidence=0.86),
    )
    monkeypatch.setattr(
        investigation_service,
        "refresh_project_research",
        lambda session_obj, config_obj, project_id, force=False: None,
    )
    monkeypatch.setattr(
        investigation_service,
        "refresh_project_mapping_preview",
        lambda session_obj, config_obj, project_id: _mapping(project, host, publish_title="nextcloud Service Workload"),
    )
    monkeypatch.setattr(
        investigation_service,
        "build_public_copy",
        lambda **kwargs: {
            "publish_title": "nextcloud Service Workload",
            "publish_summary": "Deterministic base summary.",
            "publish_long_description": "Deterministic base long description.",
            "card_title": "nextcloud Service Workload",
            "card_summary": "Deterministic base summary.",
        },
    )
    monkeypatch.setattr(
        investigation_service,
        "_generate_llm_investigation_turn",
        lambda *args, **kwargs: (
            SimpleNamespace(
                assistant_message="I reviewed the latest answer and kept the prior draft wording because no new public-copy change was needed.",
                draft_title=None,
                draft_overview=None,
                draft_report_text=None,
                publish_title=None,
                publish_summary=None,
                ready_for_confirmation=False,
                question=None,
            ),
            None,
        ),
    )

    loaded = _load_investigation(session, investigation.id)
    investigation_service.regenerate_investigation_session(session, config, loaded, recollect=False)
    session.commit()

    refreshed = _load_investigation(session, investigation.id)
    draft_state = investigation_service.investigation_draft_state_payload(refreshed)
    assert refreshed.draft_report_text == "Assistant-owned long draft."
    assert draft_state["assistant_draft"]["report_text"] == "Assistant-owned long draft."
    assert draft_state["base_draft"]["overview"] == "Deterministic base summary."
    assert "Debian GNU/Linux 12" in draft_state["base_draft"]["report_text"]
    assert draft_state["assistant_draft_version"] == 2


def test_investigation_preview_uses_last_assistant_draft_not_raw_user_reply(client, session, app):
    host = create_host(session, name="nextcloud", address="10.20.2.12")
    project = create_project(session, host, path="host://nextcloud", marker_name="README.md")
    session.add(_identity(project, host, signature_id="nextcloud", display_name="Nextcloud", confidence=0.86))
    session.add(_mapping(project, host, publish_title="nextcloud Service Workload"))
    investigation = investigation_service.create_investigation_session(
        session,
        app.state.config,
        target_type="existing_host",
        existing_host_id=host.id,
    )
    session.flush()
    investigation_service._persist_investigation_draft_state(
        investigation,
        base_draft={
            "title": "nextcloud Service Workload",
            "overview": "Base overview.",
            "report_text": "Base overview.",
            "publish_title": "nextcloud Service Workload",
            "publish_summary": "Base overview.",
            "publish_long_description": "Base overview.",
        },
        working_draft={
            "title": "nextcloud Service Workload",
            "overview": "no, go back",
            "report_text": "no, go back",
            "publish_title": "nextcloud Service Workload",
            "publish_summary": "no, go back",
            "publish_long_description": "no, go back",
        },
        assistant_draft={
            "title": "nextcloud Service Workload",
            "overview": "This is my self-hosted cloud storage workload.",
            "report_text": "This is my self-hosted cloud storage workload.\n\nIt stays separate from the parent host in the public project framing.",
            "publish_title": "nextcloud Service Workload",
            "publish_summary": "This is my self-hosted cloud storage workload.",
            "publish_long_description": "This is my self-hosted cloud storage workload.\n\nIt stays separate from the parent host in the public project framing.",
        },
        public_preview_draft={
            "title": "nextcloud Service Workload",
            "overview": "This is my self-hosted cloud storage workload.",
            "report_text": "This is my self-hosted cloud storage workload.\n\nIt stays separate from the parent host in the public project framing.",
            "publish_title": "nextcloud Service Workload",
            "publish_summary": "This is my self-hosted cloud storage workload.",
            "publish_long_description": "This is my self-hosted cloud storage workload.\n\nIt stays separate from the parent host in the public project framing.",
        },
        assistant_draft_version=3,
        assistant_draft_updated_at=utcnow().isoformat(),
    )
    session.add(
        InvestigationMessage(
            session_id=investigation.id,
            sender_type="user",
            message_type="note",
            content="no, go back",
            affects_generation=True,
        )
    )
    session.commit()

    response = client.get(f"/web/investigations/{investigation.id}/preview")
    assert response.status_code == 200
    assert "This is my self-hosted cloud storage workload." in response.text
    assert "It stays separate from the parent host in the public project framing." in response.text
    assert "no, go back" not in response.text


def test_regenerate_investigation_does_not_duplicate_model_question_when_reply_already_contains_it(session, config, monkeypatch):
    host = create_host(session, name="nextcloud", address="10.20.2.12")
    project = create_project(session, host, path="host://nextcloud", marker_name="README.md")
    investigation = investigation_service.create_investigation_session(
        session,
        config,
        target_type="existing_host",
        existing_host_id=host.id,
    )
    session.commit()

    facts = {
        "os_pretty_name": "Debian GNU/Linux 12",
        "cpu_cores": 12,
        "memory_gib": 8.0,
        "management_ip": host.address,
        "selected_running_services": [{"service_name": "apache2.service"}],
        "guest_inventory": [],
    }
    monkeypatch.setattr(
        investigation_service,
        "refresh_host_facts",
        lambda session_obj, config_obj, host_id: SimpleNamespace(facts_json=facts),
    )
    monkeypatch.setattr(
        investigation_service,
        "refresh_machine_identity",
        lambda session_obj, config_obj, project_id: _identity(project, host, signature_id="nextcloud", display_name="Nextcloud", confidence=0.86),
    )
    monkeypatch.setattr(
        investigation_service,
        "refresh_project_research",
        lambda session_obj, config_obj, project_id, force=False: None,
    )
    monkeypatch.setattr(
        investigation_service,
        "refresh_project_mapping_preview",
        lambda session_obj, config_obj, project_id: _mapping(project, host, publish_title="nextcloud Service Workload"),
    )
    monkeypatch.setattr(
        investigation_service,
        "build_public_copy",
        lambda **kwargs: {
            "publish_title": "nextcloud Service Workload",
            "publish_summary": "My self-hosted cloud storage workload.",
            "publish_long_description": "My self-hosted cloud storage workload.",
            "card_title": "nextcloud Service Workload",
            "card_summary": "My self-hosted cloud storage workload.",
        },
    )
    monkeypatch.setattr(
        investigation_service,
        "_generate_llm_investigation_turn",
        lambda *args, **kwargs: (
            SimpleNamespace(
                assistant_message="Got it. I’ll keep this framed as a standalone project. Should I also refer to it mainly as your self-hosted cloud storage platform?",
                draft_title="nextcloud Service Workload",
                draft_overview="This should read as my standalone self-hosted cloud storage project.",
                draft_report_text="This should read as my standalone self-hosted cloud storage project.",
                publish_title="nextcloud Service Workload",
                publish_summary="This should read as my standalone self-hosted cloud storage project.",
                ready_for_confirmation=False,
                question=SimpleNamespace(
                    question_text="Should I also refer to it mainly as your self-hosted cloud storage platform?",
                    question_type="role_framing",
                    ai_rationale="Need final framing detail.",
                ),
            ),
            None,
        ),
    )

    loaded = _load_investigation(session, investigation.id)
    investigation_service.regenerate_investigation_session(session, config, loaded, recollect=False)
    session.commit()

    refreshed = _load_investigation(session, investigation.id)
    visible_ai = [message for message in refreshed.messages if message.sender_type == "ai" and not message.hidden_internal]
    assert len(visible_ai) == 1
    assert "standalone project" in visible_ai[0].content.lower()
    assert visible_ai[0].message_type == "question"

    answer = investigation_service.post_investigation_message(
        session,
        refreshed,
        "Treat it as a standalone project hosted by Byteme2.",
    )
    session.commit()
    assert answer.message_type == "answer"


def test_delete_failed_investigation_route(client, session, config):
    host = create_host(session, name="alpha", address="10.20.2.20")
    investigation = investigation_service.create_investigation_session(
        session,
        config,
        target_type="existing_host",
        existing_host_id=host.id,
    )
    investigation.status = "failed"
    investigation.current_phase = "failed"
    session.commit()
    investigation_id = investigation.id

    response = client.post(f"/api/investigations/{investigation_id}/delete", follow_redirects=True)
    assert response.status_code == 200
    session.expire_all()
    assert session.get(InvestigationSession, investigation_id) is None
