from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.clarifications.service import (
    answer_project_question,
    generate_project_clarification_questions,
    set_project_question_status,
)
from app.models import MachineIdentityResult, MachineProfile, ProjectQuestionThread
from app.project_mapping.service import build_project_mapping_preview
from app.publish_preview.service import build_project_website_preview
from app.utils.common import utcnow
from tests.conftest import create_host, create_project


def _loaded_project(session, project_id: int):
    from app.models import Project

    return session.execute(
        select(Project)
        .where(Project.id == project_id)
        .options(
            selectinload(Project.host),
            selectinload(Project.identity_result),
            selectinload(Project.mapping_preview),
            selectinload(Project.machine_profile),
            selectinload(Project.clarification_threads).selectinload(ProjectQuestionThread.messages),
            selectinload(Project.clarification_guidance),
        )
    ).scalar_one()


def test_generate_project_clarification_questions_for_ambiguous_project(session):
    host = create_host(session, name="golem", address="10.20.2.25", root_path="/srv/golem")
    project = create_project(session, host, path="host://golem", marker_name="README.md")
    session.add(
        MachineIdentityResult(
            project_id=project.id,
            host_id=host.id,
            primary_signature_id="generic_project_vm",
            primary_signature_name="Generic Project VM",
            machine_kind="project_node",
            story_template_type="project_vm",
            detected_platform="Linux VM",
            detected_role="project workload",
            stack_tags_json=["Linux"],
            identity_confidence=0.51,
            identity_evidence_json=["Observed README.md"],
            alternate_matches_json=[],
            ambiguity_notes_json=["Identity is still generic."],
            local_facts_evidence_json=["Observed Ubuntu 24.04."],
            topology_evidence_json=[],
            last_resolved_at=utcnow(),
        )
    )
    session.commit()

    loaded = _loaded_project(session, project.id)
    created = generate_project_clarification_questions(session, loaded)
    session.commit()

    assert created
    question_types = {thread.question_type for thread in created}
    assert "role_framing" in question_types
    assert "public_title" in question_types or "summary_framing" in question_types


def test_answered_clarification_updates_guidance_and_mapping_title(session):
    host = create_host(session, name="golem", address="10.20.2.25", root_path="/srv/golem")
    project = create_project(session, host, path="host://golem", marker_name="README.md")
    identity = MachineIdentityResult(
        project_id=project.id,
        host_id=host.id,
        primary_signature_id="generic_project_vm",
        primary_signature_name="Generic Project VM",
        machine_kind="project_node",
        story_template_type="project_vm",
        detected_platform="Linux VM",
        detected_role="project workload",
        stack_tags_json=["Linux"],
        identity_confidence=0.55,
        identity_evidence_json=["Observed README.md"],
        alternate_matches_json=[],
        ambiguity_notes_json=["Identity is still generic."],
        local_facts_evidence_json=["Observed Ubuntu 24.04."],
        topology_evidence_json=[],
        last_resolved_at=utcnow(),
    )
    profile = MachineProfile(
        project_id=project.id,
        host_id=host.id,
        machine_kind="project_node",
        story_template_type="project_vm",
        detected_platform="Linux VM",
        detected_role="project workload",
        confidence=0.55,
        lead_paragraph="This VM appears to host a project workload in my lab.",
        project_role_summary="The exact role still needs review.",
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
    session.add_all([identity, profile])
    session.commit()

    loaded = _loaded_project(session, project.id)
    thread = ProjectQuestionThread(
        project_id=project.id,
        host_id=host.id,
        question_type="public_title",
        question_text="What exact public title do you want me to use for this project?",
        status="open",
        created_by="ai",
    )
    session.add(thread)
    session.commit()
    session.refresh(thread)

    loaded_thread = session.execute(
        select(ProjectQuestionThread)
        .where(ProjectQuestionThread.id == thread.id)
        .options(selectinload(ProjectQuestionThread.project))
    ).scalar_one()
    guidance = answer_project_question(session, loaded_thread, "golem Provider Workload VM")
    session.commit()

    assert guidance.preferred_public_title == "golem Provider Workload VM"
    refreshed_project = _loaded_project(session, project.id)
    preview = build_project_mapping_preview(
        session,
        project=refreshed_project,
        identity=identity,
        profile=profile,
    )
    session.commit()

    assert preview.publish_title_candidate == "golem Provider Workload VM"
    assert preview.project_title_candidate == "golem Provider Workload VM"


def test_question_status_transitions_and_saved_answers(session):
    host = create_host(session)
    project = create_project(session, host)
    thread = ProjectQuestionThread(
        project_id=project.id,
        host_id=host.id,
        question_type="publishability",
        question_text="Should this remain internal-only?",
        status="open",
        created_by="ai",
    )
    session.add(thread)
    session.commit()

    set_project_question_status(session, thread, "resolved")
    session.commit()
    assert thread.status == "resolved"

    set_project_question_status(session, thread, "ignored")
    session.commit()
    assert thread.status == "ignored"


def test_clarification_threads_stay_out_of_public_preview(client, session):
    host = create_host(session, name="wazuh", address="10.20.2.13", root_path="/var/ossec")
    project = create_project(session, host, path="host://wazuh", marker_name="wazuh-manager.service")
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
            setup_details_json=["Runs Wazuh manager services."],
            why_it_matters_json=["It centralizes security visibility for my lab."],
            last_generated_at=utcnow(),
        )
    )
    session.commit()
    refreshed_project = _loaded_project(session, project.id)
    preview = build_project_mapping_preview(
        session,
        project=refreshed_project,
        identity=refreshed_project.identity_result,
        profile=refreshed_project.machine_profile,
    )
    thread = ProjectQuestionThread(
        project_id=project.id,
        host_id=host.id,
        question_type="role_framing",
        question_text="Should this VM be described as my main SIEM stack?",
        ai_rationale="I need clearer editorial framing before generation.",
        my_answer="Yes, this is my main SIEM stack.",
        status="answered",
        created_by="ai",
    )
    session.add(thread)
    session.commit()

    response = client.get(f"/web/projects/{project.id}/preview")
    assert response.status_code == 200
    assert preview.publish_title_candidate in response.text
    assert "Should this VM be described as my main SIEM stack?" not in response.text
    assert "I need clearer editorial framing before generation." not in response.text
    assert "AI Clarification Workspace" not in response.text


def test_project_detail_page_renders_ai_clarification_workspace(client, session):
    host = create_host(session, name="mcp", address="10.20.2.40", root_path="/srv/mcp")
    project = create_project(session, host, path="host://mcp", marker_name="docker-compose.yml")
    session.add(
        ProjectQuestionThread(
            project_id=project.id,
            host_id=host.id,
            question_type="project_positioning",
            question_text="Should this VM be shown as a standalone project or part of another machine?",
            status="open",
            created_by="ai",
        )
    )
    session.commit()

    response = client.get(f"/web/projects/{project.id}")
    assert response.status_code == 200
    assert "AI Clarification Workspace" in response.text
    assert "Should this VM be shown as a standalone project or part of another machine?" in response.text
