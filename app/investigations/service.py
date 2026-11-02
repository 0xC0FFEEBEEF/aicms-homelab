from __future__ import annotations

import shlex
from copy import deepcopy
from dataclasses import dataclass
import re
from threading import Event
from typing import Any

from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.clarifications.service import apply_investigation_guidance_answer
from app.config import AppConfig
from app.db import session_scope
from app.jobs.cancellation import JobCancelled, ensure_not_cancelled
from app.lmstudio.client import LMStudioClient
from app.models import (
    GuestInventory,
    Host,
    HostProfileOverride,
    HostRoot,
    InvestigationDraft,
    InvestigationFinding,
    InvestigationMessage,
    InvestigationProposal,
    InvestigationSession,
    MachineIdentityResult,
    MachineProfile,
    Project,
    ProjectClarificationGuidance,
    ProjectMappingPreview,
)
from app.project_mapping.copy_builders import build_public_copy, sanitize_public_copy_text
from app.project_mapping.preferences import project_public_copy_preferences
from app.project_mapping.title_rules import stable_identity_title
from app.profiles.collector import collect_machine_facts
from app.profiles.service import (
    refresh_host_facts,
    refresh_machine_identity,
    refresh_project_mapping_preview,
    refresh_project_research,
)
from app.scanner.service import queue_scan
from app.scanner.ssh import SSHExecutor
from app.schemas import (
    GENERAL_SETTINGS_KEY,
    LMSTUDIO_SETTINGS_KEY,
    WEB_SEARCH_SETTINGS_KEY,
    GeneralSettingsPayload,
    HostUpsert,
    LMStudioSettingsPayload,
    WebSearchSettingsPayload,
)
from app.signatures.resolver import resolve_machine_identity
from app.topology.service import build_host_topology_context, link_discovered_guest_to_host
from app.utils.common import clean_multiline, truthy, utcnow
from app.utils.settings_store import get_settings

RUNNING_STATUSES = {"queued", "running", "drafting", "applying", "cancelling"}
CONFIRM_PHRASES = {
    "looks good",
    "i'm happy with this",
    "im happy with this",
    "approved",
    "go ahead",
    "apply it",
    "ship it",
}
QUESTION_LIMIT = 2
TERMINAL_INVESTIGATION_STATUSES = {"failed", "cancelled"}
CONVERSATION_CONTEXT_WINDOW = 30
CONVERSATION_SUMMARY_LIMIT = 24
INVESTIGATION_DRAFT_FIELDS = (
    "title",
    "overview",
    "report_text",
    "publish_title",
    "publish_summary",
    "publish_long_description",
    "card_title",
    "card_summary",
)
INVESTIGATION_DRAFT_LIST_FIELDS = (
    "detail_sections",
    "setup_summary",
    "notes_or_highlights",
)
INVESTIGATION_DRAFT_EXTRA_TEXT_FIELDS = (
    "role_in_lab",
)
RECOLLECT_HINTS = (
    "refresh facts",
    "refresh host facts",
    "refresh the facts",
    "collect facts again",
    "collect again",
    "recollect",
    "re-collect",
    "re-scan",
    "rescan",
    "scan again",
    "pull fresh facts",
    "grab fresh facts",
    "refresh inventory",
    "rerun the investigation",
)


@dataclass
class InvestigationTarget:
    target_type: str
    target_name: str
    target_address: str
    target_hostname: str | None
    linked_host_id: int | None
    linked_project_id: int | None
    linked_guest_id: int | None
    target_config: dict[str, Any]


def load_investigation_session(session: Session, investigation_id: int) -> InvestigationSession:
    return session.execute(
        select(InvestigationSession)
        .where(InvestigationSession.id == investigation_id)
        .options(
            selectinload(InvestigationSession.linked_host)
            .selectinload(Host.roots),
            selectinload(InvestigationSession.linked_host)
            .selectinload(Host.projects),
            selectinload(InvestigationSession.linked_host)
            .selectinload(Host.profile_override),
            selectinload(InvestigationSession.linked_project)
            .selectinload(Project.machine_profile),
            selectinload(InvestigationSession.linked_project)
            .selectinload(Project.identity_result)
            .selectinload(MachineIdentityResult.signature_matches),
            selectinload(InvestigationSession.linked_project)
            .selectinload(Project.mapping_preview),
            selectinload(InvestigationSession.linked_project)
            .selectinload(Project.clarification_guidance),
            selectinload(InvestigationSession.linked_guest)
            .selectinload(GuestInventory.host),
            selectinload(InvestigationSession.messages),
            selectinload(InvestigationSession.findings),
            selectinload(InvestigationSession.drafts),
            selectinload(InvestigationSession.proposals),
        )
    ).scalar_one()


def list_investigation_sessions(session: Session) -> list[InvestigationSession]:
    return session.execute(
        select(InvestigationSession)
        .options(
            selectinload(InvestigationSession.linked_host),
            selectinload(InvestigationSession.linked_project),
            selectinload(InvestigationSession.linked_guest),
        )
        .order_by(InvestigationSession.updated_at.desc())
        .limit(100)
    ).scalars().all()


def primary_project_for_host(host: Host) -> Project | None:
    host_summary = next(
        (project for project in host.projects if (project.metadata_json or {}).get("setup_kind") == "host-summary"),
        None,
    )
    if host_summary is not None:
        return host_summary
    return sorted(host.projects, key=lambda item: item.last_seen_at or utcnow(), reverse=True)[0] if host.projects else None


def _append_message(
    session: Session,
    investigation: InvestigationSession,
    *,
    sender_type: str,
    message_type: str,
    content: str,
    affects_generation: bool = True,
    hidden_internal: bool = False,
    metadata: dict[str, Any] | None = None,
) -> InvestigationMessage:
    message = InvestigationMessage(
        session=investigation,
        sender_type=sender_type,
        message_type=message_type,
        content=content.strip(),
        affects_generation=affects_generation,
        hidden_internal=hidden_internal,
        metadata_json=metadata,
    )
    session.add(message)
    session.flush()
    return message


def _set_status(
    investigation: InvestigationSession,
    *,
    status: str,
    phase: str,
    error: str | None = None,
) -> None:
    investigation.status = status
    investigation.current_phase = phase
    investigation.last_error = error
    investigation.updated_at = utcnow()


def _target_config_payload(
    *,
    ssh_port: int,
    ssh_user: str,
    ssh_key_path: str | None,
    roots: list[str],
) -> dict[str, Any]:
    return {
        "ssh_port": ssh_port,
        "ssh_user": ssh_user,
        "ssh_key_path": ssh_key_path,
        "roots": roots,
    }


def _set_investigation_control(app: FastAPI, investigation_id: int, **payload: Any) -> None:
    with app.state.investigation_lock:
        state = dict(app.state.investigation_controls.get(investigation_id, {}))
        state.update(payload)
        app.state.investigation_controls[investigation_id] = state


def _get_investigation_control(app: FastAPI, investigation_id: int) -> dict[str, Any]:
    with app.state.investigation_lock:
        return dict(app.state.investigation_controls.get(investigation_id, {}))


def _pop_investigation_control(app: FastAPI, investigation_id: int) -> dict[str, Any]:
    with app.state.investigation_lock:
        return app.state.investigation_controls.pop(investigation_id, {})


def _pop_investigation_control_if_current(
    app: FastAPI,
    investigation_id: int,
    *,
    worker_stop_event: Event | None,
) -> dict[str, Any]:
    if worker_stop_event is None:
        return {}
    with app.state.investigation_lock:
        state = app.state.investigation_controls.get(investigation_id, {})
        if state.get("stop_event") is worker_stop_event:
            return app.state.investigation_controls.pop(investigation_id, {})
        return dict(state)


def _consume_pending_refresh(
    app: FastAPI,
    investigation_id: int,
    *,
    worker_stop_event: Event | None,
) -> tuple[bool, bool]:
    if worker_stop_event is None:
        return False, False
    with app.state.investigation_lock:
        state = app.state.investigation_controls.get(investigation_id)
        if not state or state.get("stop_event") is not worker_stop_event:
            return False, False
        if state.get("pending_refresh"):
            updated = dict(state)
            requested_recollect = bool(updated.get("pending_recollect"))
            updated["pending_refresh"] = False
            updated["pending_recollect"] = False
            app.state.investigation_controls[investigation_id] = updated
            return True, requested_recollect
        app.state.investigation_controls.pop(investigation_id, None)
        return False, False


def _guest_prefill_roots(guest: GuestInventory) -> list[str]:
    roots: list[str] = []
    if guest.name:
        roots.append(f"/home/{guest.name}")
    roots.extend(["/srv/projects", "/opt"])
    seen: set[str] = set()
    deduped: list[str] = []
    for item in roots:
        if item in seen:
            continue
        deduped.append(item)
        seen.add(item)
    return deduped


def create_investigation_session(
    session: Session,
    config: AppConfig,
    *,
    target_type: str,
    existing_host_id: int | None = None,
    discovered_guest_id: int | None = None,
    name: str | None = None,
    address: str | None = None,
    ssh_port: int = 22,
    ssh_user: str | None = None,
    ssh_key_path: str | None = None,
    roots_text: str | None = None,
    started_by: str = "user",
) -> InvestigationSession:
    normalized_type = target_type.strip()
    default_settings = get_settings(session, config, GENERAL_SETTINGS_KEY, GeneralSettingsPayload)
    default_key_path = ssh_key_path or default_settings.default_scanner_ssh_key_path

    if normalized_type == "existing_host":
        host = session.execute(
            select(Host)
            .where(Host.id == existing_host_id)
            .options(selectinload(Host.roots), selectinload(Host.projects))
        ).scalar_one()
        project = primary_project_for_host(host)
        target = InvestigationTarget(
            target_type=normalized_type,
            target_name=host.name,
            target_address=host.address,
            target_hostname=host.name,
            linked_host_id=host.id,
            linked_project_id=project.id if project else None,
            linked_guest_id=None,
            target_config=_target_config_payload(
                ssh_port=host.ssh_port,
                ssh_user=host.ssh_user,
                ssh_key_path=host.ssh_key_path or default_key_path,
                roots=[root.root_path for root in host.roots if root.enabled],
            ),
        )
    elif normalized_type == "discovered_guest":
        guest = session.execute(
            select(GuestInventory)
            .where(GuestInventory.id == discovered_guest_id)
            .options(
                selectinload(GuestInventory.host),
                selectinload(GuestInventory.linked_host).selectinload(Host.roots),
                selectinload(GuestInventory.linked_host).selectinload(Host.projects),
            )
        ).scalar_one()
        linked_project = primary_project_for_host(guest.linked_host) if guest.linked_host else None
        target = InvestigationTarget(
            target_type=normalized_type,
            target_name=guest.name or f"{guest.host.name}-{guest.guest_type}-{guest.guest_id}",
            target_address=guest.ip or "",
            target_hostname=guest.name or None,
            linked_host_id=guest.linked_host_id,
            linked_project_id=linked_project.id if linked_project else None,
            linked_guest_id=guest.id,
            target_config=_target_config_payload(
                ssh_port=22,
                ssh_user="aicms",
                ssh_key_path=default_key_path,
                roots=clean_multiline(roots_text) if roots_text else _guest_prefill_roots(guest),
            ),
        )
    elif normalized_type == "manual_ssh_target":
        payload = HostUpsert(
            name=name,
            address=address,
            ssh_port=ssh_port,
            ssh_user=ssh_user,
            ssh_key_path=default_key_path,
            enabled=True,
            roots=clean_multiline(roots_text or ""),
        )
        target = InvestigationTarget(
            target_type=normalized_type,
            target_name=payload.name,
            target_address=payload.address,
            target_hostname=payload.name,
            linked_host_id=None,
            linked_project_id=None,
            linked_guest_id=None,
            target_config=_target_config_payload(
                ssh_port=payload.ssh_port,
                ssh_user=payload.ssh_user,
                ssh_key_path=payload.ssh_key_path,
                roots=payload.roots,
            ),
        )
    else:
        raise ValueError("Unsupported investigation target type.")

    investigation = InvestigationSession(
        target_type=target.target_type,
        target_name=target.target_name,
        target_address=target.target_address,
        target_hostname=target.target_hostname,
        linked_host_id=target.linked_host_id,
        linked_project_id=target.linked_project_id,
        linked_guest_id=target.linked_guest_id,
        status="queued",
        current_phase="queued",
        started_by=started_by,
        target_config_json=target.target_config,
    )
    session.add(investigation)
    session.flush()
    _append_message(
        session,
        investigation,
        sender_type="system",
        message_type="status",
        content=f"Created investigation session for {investigation.target_name or investigation.target_address}.",
        affects_generation=False,
        hidden_internal=True,
    )
    _append_message(
        session,
        investigation,
        sender_type="ai",
        message_type="note",
        content=(
            f"I’m starting a live investigation for {investigation.target_name or investigation.target_address}. "
            "I’ll collect facts, build a working draft, ask only high-value questions if I need clarification, "
            "and wait for your confirmation before I apply anything."
        ),
        affects_generation=False,
        hidden_internal=True,
    )
    session.flush()
    return investigation


def _run_lines_with_executor(
    executor: SSHExecutor,
    client: Any,
    command: str,
    timeout: int,
    *,
    stop_event: Event | None = None,
) -> tuple[list[str], str | None]:
    code, stdout, stderr = executor.run(client, command, timeout=timeout, stop_event=stop_event)
    if code != 0 and stderr:
        return [line.strip() for line in stdout.splitlines() if line.strip()], stderr.strip()
    return [line.strip() for line in stdout.splitlines() if line.strip()], None


def _manual_target_host(investigation: InvestigationSession) -> Host:
    config = investigation.target_config_json or {}
    roots = [HostRoot(root_path=item, enabled=True) for item in (config.get("roots") or [])]
    return Host(
        name=investigation.target_name or investigation.target_hostname or "investigation-target",
        address=investigation.target_address or "",
        ssh_port=int(config.get("ssh_port") or 22),
        ssh_user=str(config.get("ssh_user") or "aicms"),
        ssh_key_path=config.get("ssh_key_path"),
        enabled=True,
        roots=roots,
    )


def _collect_manual_target_facts(
    config: AppConfig,
    investigation: InvestigationSession,
    *,
    stop_event: Event | None = None,
) -> dict[str, Any]:
    temp_host = _manual_target_host(investigation)
    executor = SSHExecutor(
        connect_timeout=config.ssh_connect_timeout_seconds,
        command_timeout=config.ssh_command_timeout_seconds,
    )
    client = executor.open_client(temp_host)
    try:
        return collect_machine_facts(
            host=temp_host,
            run_lines=lambda command, **kwargs: _run_lines_with_executor(
                executor,
                client,
                command,
                kwargs.get("timeout") or executor.command_timeout,
                stop_event=stop_event,
            ),
            run_command=lambda command, **kwargs: executor.run(
                client,
                command,
                timeout=kwargs.get("timeout") or executor.command_timeout,
                stop_event=stop_event,
            ),
            max_services=20,
            max_guests=48,
        )
    finally:
        client.close()


def _serialize_identity(identity: MachineIdentityResult | Any) -> dict[str, Any]:
    return {
        "primary_signature_id": identity.primary_signature_id,
        "primary_signature_name": identity.primary_signature_name,
        "machine_kind": identity.machine_kind,
        "detected_platform": identity.detected_platform,
        "detected_role": identity.detected_role,
        "identity_confidence": identity.identity_confidence,
        "stack_tags": list(getattr(identity, "stack_tags_json", None) or getattr(identity, "stack_tags", []) or []),
        "identity_evidence": list(getattr(identity, "identity_evidence_json", None) or getattr(identity, "identity_evidence", []) or []),
        "ambiguity_notes": list(getattr(identity, "ambiguity_notes_json", None) or getattr(identity, "ambiguity_notes", []) or []),
    }


def _serialize_mapping(mapping: ProjectMappingPreview | None) -> dict[str, Any]:
    if mapping is None:
        return {}
    return {
        "card_title": mapping.card_title_candidate,
        "card_summary": mapping.card_summary_candidate,
        "publish_title": mapping.publish_title_candidate,
        "publish_summary": mapping.publish_summary_candidate,
        "publish_long_description": mapping.publish_long_description_candidate,
        "tech_tags": list(mapping.tech_tags_candidate_json or []),
        "recommended_status": mapping.recommended_status,
    }


def _current_answer_notes(investigation: InvestigationSession) -> list[dict[str, Any]]:
    notes: list[dict[str, Any]] = []
    for message in investigation.messages:
        if message.sender_type != "user" or not message.affects_generation:
            continue
        meta = message.metadata_json or {}
        notes.append(
            {
                "question_type": meta.get("question_type"),
                "answer_to_message_id": meta.get("answer_to_message_id"),
                "content": message.content,
            }
        )
    return notes


def _visible_conversation_messages(investigation: InvestigationSession) -> list[InvestigationMessage]:
    return [
        message
        for message in investigation.messages
        if not message.hidden_internal and message.sender_type in {"user", "ai"}
    ]


def _asked_question_types(investigation: InvestigationSession) -> set[str]:
    question_types: set[str] = set()
    for message in investigation.messages:
        if message.sender_type == "ai" and message.message_type == "question":
            question_type = str((message.metadata_json or {}).get("question_type") or "").strip()
            if question_type:
                question_types.add(question_type)
        elif message.sender_type == "user" and message.message_type == "answer":
            question_type = str((message.metadata_json or {}).get("question_type") or "").strip()
            if question_type:
                question_types.add(question_type)
    return question_types


def _conversation_context_summary(
    investigation: InvestigationSession,
    *,
    window: int = CONVERSATION_CONTEXT_WINDOW,
) -> str | None:
    visible = _visible_conversation_messages(investigation)
    if len(visible) <= window:
        return None
    older_messages = visible[:-window]
    summary_lines: list[str] = []
    for message in older_messages[-CONVERSATION_SUMMARY_LIMIT:]:
        speaker = "User" if message.sender_type == "user" else "AI"
        normalized = re.sub(r"\s+", " ", message.content.strip())
        if len(normalized) > 220:
            normalized = normalized[:217].rstrip() + "..."
        if normalized:
            summary_lines.append(f"{speaker}: {normalized}")
    if not summary_lines:
        return None
    return "Earlier conversation context:\n- " + "\n- ".join(summary_lines)


def _temporary_guidance(
    investigation: InvestigationSession,
    project: Project | None,
) -> ProjectClarificationGuidance | None:
    base = project.clarification_guidance if project else None
    if base is None and not investigation.messages:
        return None
    guidance = ProjectClarificationGuidance(
        project_id=project.id if project else 0,
        host_id=project.host_id if project else investigation.linked_host_id,
        preferred_public_title=base.preferred_public_title if base else None,
        preferred_role_wording=base.preferred_role_wording if base else None,
        standalone_preference=base.standalone_preference if base else None,
        preferred_project_group=base.preferred_project_group if base else None,
        internal_only=base.internal_only if base else None,
        publishable_project=base.publishable_project if base else None,
        preferred_summary_framing=base.preferred_summary_framing if base else None,
        guidance_notes_json=list(base.guidance_notes_json or []) if base else [],
        source_question_ids_json=list(base.source_question_ids_json or []) if base else [],
    )
    for message in _current_answer_notes(investigation):
        answer = str(message["content"]).strip()
        question_type = str(message.get("question_type") or "summary_framing")
        lowered = answer.lower()
        if question_type == "public_title":
            guidance.preferred_public_title = answer
        elif question_type == "role_framing":
            guidance.preferred_role_wording = answer
            guidance.preferred_summary_framing = answer
        elif question_type == "project_positioning":
            if any(token in lowered for token in ("part of", "under", "within", "grouped", "group")):
                guidance.standalone_preference = "part_of_parent"
            elif any(token in lowered for token in ("standalone", "separate", "own project")):
                guidance.standalone_preference = "standalone"
            else:
                guidance.standalone_preference = "needs_review"
        elif question_type == "publishability":
            if any(token in lowered for token in ("internal", "private", "do not publish", "don't publish")):
                guidance.internal_only = True
                guidance.publishable_project = False
            elif any(token in lowered for token in ("publish", "public", "yes")):
                guidance.publishable_project = True
                guidance.internal_only = False
        elif question_type == "summary_framing":
            guidance.preferred_summary_framing = answer
        elif question_type == "project_grouping":
            guidance.preferred_project_group = answer
        notes = list(guidance.guidance_notes_json or [])
        if answer and answer not in notes:
            notes.append(answer)
        guidance.guidance_notes_json = notes
    return guidance


def _investigation_public_preferences(
    investigation: InvestigationSession,
    project: Project | None,
    guidance: ProjectClarificationGuidance | None = None,
) -> dict[str, Any]:
    extra_texts = [
        message.content
        for message in investigation.messages
        if message.sender_type == "user" and message.affects_generation and message.content
    ]
    return project_public_copy_preferences(project, guidance=guidance, extra_texts=extra_texts)


def _sanitize_investigation_draft(
    investigation: InvestigationSession,
    project: Project | None,
    draft: dict[str, Any],
    *,
    guidance: ProjectClarificationGuidance | None = None,
) -> dict[str, Any]:
    preferences = _investigation_public_preferences(investigation, project, guidance=guidance)
    updated = deepcopy(draft)
    for key in ("overview", "report_text", "publish_summary", "publish_long_description", "card_summary"):
        updated[key] = sanitize_public_copy_text(updated.get(key), preferences)
    if not str(updated.get("overview") or "").strip() and str(updated.get("report_text") or "").strip():
        updated["overview"] = str(updated["report_text"]).split("\n\n", 1)[0].strip()
    if not str(updated.get("publish_summary") or "").strip() and str(updated.get("overview") or "").strip():
        updated["publish_summary"] = updated["overview"]
    if not str(updated.get("publish_long_description") or "").strip() and str(updated.get("report_text") or "").strip():
        updated["publish_long_description"] = updated["report_text"]
    if not str(updated.get("card_summary") or "").strip():
        updated["card_summary"] = str(updated.get("publish_summary") or updated.get("overview") or "").strip()
    return updated


def _normalize_investigation_draft_payload(
    draft: dict[str, Any] | None,
    *,
    fallback_text: str | None = None,
) -> dict[str, Any] | None:
    if not isinstance(draft, dict):
        draft = {}
    updated: dict[str, Any] = {}
    for key in INVESTIGATION_DRAFT_FIELDS:
        value = draft.get(key)
        if isinstance(value, str):
            value = value.strip()
        if value not in (None, ""):
            updated[key] = value
    for key in INVESTIGATION_DRAFT_LIST_FIELDS:
        value = draft.get(key)
        if isinstance(value, list) and value:
            updated[key] = [str(item).strip() for item in value if str(item).strip()]
    for key in INVESTIGATION_DRAFT_EXTRA_TEXT_FIELDS:
        value = draft.get(key)
        if isinstance(value, str) and value.strip():
            updated[key] = value.strip()
    custom = draft.get("custom_sections")
    if isinstance(custom, list) and custom:
        normalized_custom = []
        for sec in custom:
            if isinstance(sec, dict) and sec.get("heading"):
                items = [str(i).strip() for i in (sec.get("items") or []) if str(i).strip()]
                if items:
                    normalized_custom.append({"heading": str(sec["heading"]).strip(), "items": items})
        if normalized_custom:
            updated["custom_sections"] = normalized_custom
    fallback_text = str(fallback_text or "").strip()
    if not updated and not fallback_text:
        return None
    if fallback_text and not str(updated.get("report_text") or "").strip():
        updated["report_text"] = fallback_text
    if not str(updated.get("title") or "").strip() and str(updated.get("publish_title") or "").strip():
        updated["title"] = str(updated["publish_title"]).strip()
    if not str(updated.get("publish_title") or "").strip() and str(updated.get("title") or "").strip():
        updated["publish_title"] = str(updated["title"]).strip()
    if not str(updated.get("report_text") or "").strip() and str(updated.get("publish_long_description") or "").strip():
        updated["report_text"] = str(updated["publish_long_description"]).strip()
    if not str(updated.get("report_text") or "").strip() and str(updated.get("overview") or "").strip():
        updated["report_text"] = str(updated["overview"]).strip()
    if not str(updated.get("overview") or "").strip() and str(updated.get("report_text") or "").strip():
        updated["overview"] = str(updated["report_text"]).split("\n\n", 1)[0].strip()
    if not str(updated.get("publish_summary") or "").strip() and str(updated.get("overview") or "").strip():
        updated["publish_summary"] = str(updated["overview"]).strip()
    if not str(updated.get("publish_long_description") or "").strip() and str(updated.get("report_text") or "").strip():
        updated["publish_long_description"] = str(updated["report_text"]).strip()
    if not str(updated.get("card_title") or "").strip() and str(updated.get("publish_title") or "").strip():
        updated["card_title"] = str(updated["publish_title"]).strip()
    if not str(updated.get("card_summary") or "").strip():
        updated["card_summary"] = (
            str(updated.get("publish_summary") or updated.get("overview") or updated.get("report_text") or "").strip()
        )
    if not any(str(updated.get(key) or "").strip() for key in INVESTIGATION_DRAFT_FIELDS):
        return None
    return updated


def _llm_turn_has_draft_update(llm_turn: Any | None) -> bool:
    if llm_turn is None:
        return False
    for field_name in (
        "draft_title",
        "draft_overview",
        "draft_report_text",
        "publish_title",
        "publish_summary",
        "publish_long_description",
        "role_in_lab",
    ):
        if str(getattr(llm_turn, field_name, "") or "").strip():
            return True
    for field_name in ("detail_sections", "setup_summary", "notes_or_highlights", "custom_sections"):
        value = getattr(llm_turn, field_name, None)
        if isinstance(value, list) and value:
            return True
    return False


def investigation_draft_state_payload(investigation: InvestigationSession) -> dict[str, Any]:
    raw = investigation.draft_report_json or {}
    if any(
        key in raw for key in ("base_draft", "working_draft", "assistant_draft", "public_preview_draft")
    ):
        base_draft = _normalize_investigation_draft_payload(raw.get("base_draft"))
        working_draft = _normalize_investigation_draft_payload(raw.get("working_draft"))
        assistant_draft = _normalize_investigation_draft_payload(raw.get("assistant_draft"))
        public_preview_draft = _normalize_investigation_draft_payload(raw.get("public_preview_draft"))
        assistant_draft_version = raw.get("assistant_draft_version")
        assistant_draft_updated_at = raw.get("assistant_draft_updated_at")
        open_question = raw.get("open_question")
    else:
        legacy_draft = _normalize_investigation_draft_payload(raw, fallback_text=investigation.draft_report_text)
        base_draft = legacy_draft
        working_draft = None
        assistant_draft = legacy_draft
        public_preview_draft = legacy_draft
        assistant_draft_version = 1 if legacy_draft else None
        assistant_draft_updated_at = investigation.updated_at.isoformat() if legacy_draft and investigation.updated_at else None
        open_question = raw.get("open_question")

    visible_source = "none"
    visible_draft = None
    if working_draft is not None:
        visible_source = "working"
        visible_draft = working_draft
    elif assistant_draft is not None:
        visible_source = "assistant"
        visible_draft = assistant_draft
    elif base_draft is not None:
        visible_source = "base"
        visible_draft = base_draft

    preview_source = "none"
    preview_draft = None
    if public_preview_draft is not None:
        preview_source = "assistant"
        preview_draft = public_preview_draft
    elif assistant_draft is not None:
        preview_source = "assistant"
        preview_draft = assistant_draft
    elif base_draft is not None:
        preview_source = "base"
        preview_draft = base_draft

    visible_draft = visible_draft or {}
    preview_draft = preview_draft or {}
    draft_state = raw.get("draft_state")
    if draft_state not in {"working", "assistant", "base", "empty"}:
        if visible_source == "working":
            draft_state = "working"
        elif visible_source == "assistant":
            draft_state = "assistant"
        elif visible_source == "base":
            draft_state = "base"
        else:
            draft_state = "empty"

    payload = {
        "base_draft": base_draft,
        "working_draft": working_draft,
        "assistant_draft": assistant_draft,
        "public_preview_draft": public_preview_draft,
        "visible_draft_source": visible_source,
        "preview_draft_source": preview_source,
        "assistant_draft_version": assistant_draft_version,
        "assistant_draft_updated_at": assistant_draft_updated_at,
        "draft_state": draft_state,
        "open_question": open_question,
        "conversation_context_summary": str(raw.get("conversation_context_summary") or "").strip() or None,
    }
    for key in INVESTIGATION_DRAFT_FIELDS:
        if visible_draft.get(key) not in (None, ""):
            payload[key] = visible_draft[key]
    return payload


def investigation_visible_draft(investigation: InvestigationSession) -> tuple[str, dict[str, Any] | None]:
    payload = investigation_draft_state_payload(investigation)
    source = str(payload.get("visible_draft_source") or "none")
    if source == "working":
        return source, _normalize_investigation_draft_payload(payload.get("working_draft"))
    if source == "assistant":
        return source, _normalize_investigation_draft_payload(payload.get("assistant_draft"))
    if source == "base":
        return source, _normalize_investigation_draft_payload(payload.get("base_draft"))
    return "none", None


def investigation_public_preview_draft(investigation: InvestigationSession) -> tuple[str, dict[str, Any] | None]:
    payload = investigation_draft_state_payload(investigation)
    source = str(payload.get("preview_draft_source") or "none")
    if source == "assistant":
        return source, _normalize_investigation_draft_payload(
            payload.get("public_preview_draft") or payload.get("assistant_draft")
        )
    if source == "base":
        return source, _normalize_investigation_draft_payload(payload.get("base_draft"))
    return "none", None


def _persist_investigation_draft_state(
    investigation: InvestigationSession,
    *,
    base_draft: dict[str, Any] | None,
    working_draft: dict[str, Any] | None,
    assistant_draft: dict[str, Any] | None,
    public_preview_draft: dict[str, Any] | None,
    assistant_draft_version: int | None,
    assistant_draft_updated_at: str | None,
    open_question: str | None = None,
    conversation_context_summary: str | None = None,
) -> None:
    raw_payload = {
        "base_draft": _normalize_investigation_draft_payload(base_draft),
        "working_draft": _normalize_investigation_draft_payload(working_draft),
        "assistant_draft": _normalize_investigation_draft_payload(assistant_draft),
        "public_preview_draft": _normalize_investigation_draft_payload(public_preview_draft),
        "assistant_draft_version": assistant_draft_version,
        "assistant_draft_updated_at": assistant_draft_updated_at,
        "open_question": open_question,
        "conversation_context_summary": str(conversation_context_summary or "").strip() or None,
    }
    computed = investigation_draft_state_payload(
        InvestigationSession(
            draft_report_json=raw_payload,
            draft_report_text=None,
            updated_at=investigation.updated_at,
        )
    )
    investigation.draft_report_json = computed
    investigation.draft_report_text = str(computed.get("report_text") or "").strip() or None


def _host_resources_summary(facts: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    if facts.get("cpu_cores") or facts.get("cpu_threads"):
        cpu = facts.get("cpu_cores") or "?"
        threads = facts.get("cpu_threads") or "?"
        lines.append(f"CPU: {cpu} cores / {threads} threads")
    if facts.get("memory_gib") is not None:
        lines.append(f"RAM: {facts.get('memory_gib')} GiB")
    if facts.get("root_size_gib") is not None:
        lines.append(f"Root disk: {facts.get('root_size_gib')} GiB")
    if facts.get("os_pretty_name"):
        lines.append(f"OS: {facts.get('os_pretty_name')}")
    if facts.get("management_ip"):
        lines.append(f"Management IP: {facts.get('management_ip')}")
    return lines


def _guest_summary(guest: dict[str, Any]) -> str:
    name = guest.get("name") or f"{str(guest.get('guest_type') or 'guest').upper()} {guest.get('guest_id')}"
    role = guest.get("role_guess") or "role needs review"
    cpu = guest.get("cores")
    ram_mb = guest.get("ram_mb")
    disk = guest.get("disk_summary")
    pieces = [str(name), f"{guest.get('guest_type', 'guest').upper()} {guest.get('guest_id')}"]
    if guest.get("status"):
        pieces.append(str(guest["status"]))
    resources: list[str] = []
    if cpu:
        resources.append(f"{cpu} cores")
    if ram_mb:
        resources.append(f"{round(float(ram_mb) / 1024, 1)} GiB RAM")
    if disk:
        resources.append(str(disk))
    line = " · ".join(pieces)
    if resources:
        line += f" · {', '.join(resources)}"
    line += f" · {role}"
    return line


def _build_findings_payload(
    investigation: InvestigationSession,
    *,
    facts: dict[str, Any],
    identity: dict[str, Any],
    topology: dict[str, Any],
    mapping: dict[str, Any],
    project: Project | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    findings: list[dict[str, Any]] = [
        {
            "finding_type": "identity",
            "title": "Resolved identity",
            "summary": f"{identity.get('primary_signature_name') or 'Unresolved machine'} at confidence {identity.get('identity_confidence') or 0:.2f}.",
            "details": identity,
            "confidence": identity.get("identity_confidence"),
            "source_type": "local",
        },
        {
            "finding_type": "resources",
            "title": "Host resources",
            "summary": ", ".join(_host_resources_summary(facts)) or "Host resources were only partially available.",
            "details": {"host_resources": _host_resources_summary(facts)},
            "confidence": 0.95,
            "source_type": "local",
        },
    ]
    parent = topology.get("parent") or {}
    if parent.get("name"):
        findings.append(
            {
                "finding_type": "topology",
                "title": "Topology context",
                "summary": f"Linked to parent host {parent.get('name')} as {str(parent.get('guest_type') or 'guest').upper()} {parent.get('guest_id') or ''}".strip(),
                "details": topology,
                "confidence": 0.95,
                "source_type": "topology",
            }
        )
    services = facts.get("selected_running_services") or []
    if services:
        findings.append(
            {
                "finding_type": "services",
                "title": "Observed services",
                "summary": ", ".join(service.get("service_name") for service in services[:8] if service.get("service_name")),
                "details": {"services": services[:20]},
                "confidence": 0.9,
                "source_type": "local",
            }
        )
    guests = facts.get("guest_inventory") or []
    if guests:
        findings.append(
            {
                "finding_type": "guests",
                "title": "Hosted workloads",
                "summary": f"Observed {sum(1 for item in guests if item.get('guest_type') == 'qemu')} VM(s) and {sum(1 for item in guests if item.get('guest_type') == 'lxc')} LXC container(s).",
                "details": {"guests": guests},
                "confidence": 0.95,
                "source_type": "local",
            }
        )
    normalized = {
        "target_name": investigation.target_name,
        "target_type": investigation.target_type,
        "facts": {
            "os": facts.get("os_pretty_name"),
            "virtualization": facts.get("virtualization"),
            "management_ip": facts.get("management_ip"),
            "cpu_cores": facts.get("cpu_cores"),
            "cpu_threads": facts.get("cpu_threads"),
            "memory_gib": facts.get("memory_gib"),
        },
        "identity": identity,
        "topology": topology,
        "mapping": mapping,
        "linked_project_path": project.path if project else None,
    }
    return findings, normalized


def _build_draft_payload(
    investigation: InvestigationSession,
    *,
    facts: dict[str, Any],
    identity: dict[str, Any],
    mapping: dict[str, Any],
    project: Project | None,
    public_copy: dict[str, Any] | None,
) -> dict[str, Any]:
    title = (public_copy or {}).get("publish_title") or mapping.get("publish_title") or stable_identity_title(
        host=investigation.linked_host,
        project=project,
        identity=project.identity_result if project and project.identity_result else None,
    )
    overview = (public_copy or {}).get("publish_summary") or mapping.get("publish_summary") or (
        f"This investigation currently reads {investigation.target_name or investigation.target_address} as a {identity.get('detected_role') or 'workload that still needs review'}."
    )
    paragraphs = [overview]
    if facts.get("os_pretty_name"):
        paragraphs.append(
            f"The current fact set shows {facts.get('os_pretty_name')} with {facts.get('cpu_cores') or '?'} cores, {facts.get('memory_gib') or '?'} GiB RAM, and management IP {facts.get('management_ip') or investigation.target_address or 'unknown'}."
        )
    if identity.get("ambiguity_notes"):
        paragraphs.append("Open ambiguity still exists around parts of the machine identity or public framing, so I may ask for one or two clarifications before applying changes.")
    if investigation.target_type == "manual_ssh_target":
        paragraphs.append("This target is not in AICMS as a managed host yet. If you confirm the proposal, I will create the host record and continue with the normal discovery flow.")
    report_text = "\n\n".join(paragraphs)
    return {
        "title": title,
        "overview": overview,
        "report_text": report_text,
        "publish_title": (public_copy or {}).get("publish_title") or mapping.get("publish_title"),
        "publish_summary": (public_copy or {}).get("publish_summary") or mapping.get("publish_summary"),
        "publish_long_description": (public_copy or {}).get("publish_long_description") or mapping.get("publish_long_description"),
        "card_title": (public_copy or {}).get("card_title") or mapping.get("card_title"),
        "card_summary": (public_copy or {}).get("card_summary") or mapping.get("card_summary"),
    }


def _build_proposal_rows(
    investigation: InvestigationSession,
    *,
    mapping: dict[str, Any],
    draft: dict[str, Any],
    project: Project | None,
) -> list[dict[str, Any]]:
    proposals: list[dict[str, Any]] = []
    if investigation.linked_host is None and investigation.target_type in {"manual_ssh_target", "discovered_guest"}:
        proposals.append(
            {
                "proposal_type": "create",
                "target_kind": "host",
                "target_id": None,
                "field_name": "host_record",
                "old_value": None,
                "new_value": {
                    "name": investigation.target_name,
                    "address": investigation.target_address,
                    **(investigation.target_config_json or {}),
                },
                "source": "investigation",
                "rationale": "This target is not managed in AICMS yet and will need a host record before the normal scan/project flow can continue.",
                "confidence": 0.95,
            }
        )
    if project is not None:
        proposals.append(
            {
                "proposal_type": "update",
                "target_kind": "project",
                "target_id": project.id,
                "field_name": "public_title",
                "old_value": mapping.get("publish_title") or mapping.get("card_title"),
                "new_value": draft.get("publish_title") or draft.get("title"),
                "source": "investigation",
                "rationale": "Use the current investigation draft as the project title candidate going forward.",
                "confidence": 0.86,
            }
        )
        proposals.append(
            {
                "proposal_type": "update",
                "target_kind": "project",
                "target_id": project.id,
                "field_name": "publish_summary",
                "old_value": mapping.get("publish_summary"),
                "new_value": draft.get("publish_summary") or draft.get("overview"),
                "source": "investigation",
                "rationale": "Refresh the public-facing project wording from the current investigation draft and answers.",
                "confidence": 0.82,
            }
        )
    return proposals


def _replace_session_findings(session: Session, investigation: InvestigationSession, findings: list[dict[str, Any]]) -> None:
    for row in list(investigation.findings):
        session.delete(row)
    session.flush()
    for item in findings:
        session.add(
            InvestigationFinding(
                session=investigation,
                finding_type=item["finding_type"],
                title=item["title"],
                summary=item["summary"],
                details_json=item.get("details"),
                confidence=item.get("confidence"),
                source_type=item.get("source_type") or "local",
            )
        )
    session.flush()


def _append_session_draft(session: Session, investigation: InvestigationSession, draft: dict[str, Any]) -> InvestigationDraft:
    next_version = (investigation.drafts[-1].version + 1) if investigation.drafts else 1
    row = InvestigationDraft(
        session=investigation,
        version=next_version,
        draft_type="report",
        content_text=draft["report_text"],
        content_json=draft,
    )
    session.add(row)
    session.flush()
    return row


def _replace_proposals(session: Session, investigation: InvestigationSession, proposals: list[dict[str, Any]]) -> None:
    for row in list(investigation.proposals):
        session.delete(row)
    session.flush()
    for item in proposals:
        session.add(
            InvestigationProposal(
                session=investigation,
                proposal_type=item["proposal_type"],
                target_kind=item["target_kind"],
                target_id=item.get("target_id"),
                field_name=item["field_name"],
                old_value_json=item.get("old_value"),
                new_value_json=item.get("new_value"),
                source=item.get("source"),
                rationale=item.get("rationale"),
                confidence=item.get("confidence"),
                status="proposed",
            )
        )
    session.flush()


def _last_open_question(investigation: InvestigationSession) -> InvestigationMessage | None:
    messages = _visible_conversation_messages(investigation)
    for index, message in enumerate(reversed(messages)):
        if message.sender_type == "ai" and message.message_type == "question":
            later_messages = messages[len(messages) - index :]
            if any(
                item.sender_type == "user"
                and item.metadata_json
                and item.metadata_json.get("answer_to_message_id") == message.id
                for item in later_messages
            ):
                return None
            return message
    return None


def _question_candidates(
    investigation: InvestigationSession,
    *,
    identity: dict[str, Any],
    mapping: dict[str, Any],
    project: Project | None,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen_question_types = _asked_question_types(investigation)
    confidence = float(identity.get("identity_confidence") or 0)
    signature_id = identity.get("primary_signature_id")
    if (
        (confidence < 0.78 or signature_id in {None, "generic_project_vm", "generic_service_node"})
        and "role_framing" not in seen_question_types
    ):
        candidates.append(
            {
                "question_type": "role_framing",
                "question_text": f"What is the most accurate way you want me to frame {investigation.target_name or investigation.target_address} in your environment?",
                "ai_rationale": "The deterministic identity is still weak or generic enough that a direct framing answer would materially improve the draft and later project wording.",
            }
        )
    if not (mapping.get("publish_title") or "").strip() and "public_title" not in seen_question_types:
        candidates.append(
            {
                "question_type": "public_title",
                "question_text": "What exact public-facing title do you want me to use for this machine or project?",
                "ai_rationale": "A stronger title would improve the current draft and the eventual project mapping output.",
            }
        )
    if project is not None and project.host.parent_host is not None and "project_positioning" not in seen_question_types:
        candidates.append(
            {
                "question_type": "project_positioning",
                "question_text": f"Should I present this mainly as a standalone project, or as part of {project.host.parent_host.name}?",
                "ai_rationale": "This machine has known topology context, and that affects how the final project wording and grouping should read.",
            }
        )
    if (
        investigation.target_type in {"manual_ssh_target", "discovered_guest"}
        and investigation.linked_project_id is None
        and "project_grouping" not in seen_question_types
    ):
        candidates.append(
            {
                "question_type": "project_grouping",
                "question_text": "When I apply this investigation, should it create a new project flow for this target or be grouped under an existing machine/project later?",
                "ai_rationale": "The apply path is still ambiguous enough that a direct grouping answer would reduce accidental project sprawl.",
            }
        )
    return candidates[:QUESTION_LIMIT]


def _build_public_copy_for_session(
    investigation: InvestigationSession,
    *,
    project: Project | None,
    identity_record: MachineIdentityResult | None,
    research_outcome: Any | None,
) -> dict[str, Any] | None:
    if project is None or identity_record is None:
        return None
    guidance = _temporary_guidance(investigation, project)
    return build_public_copy(
        host=project.host,
        project=project,
        identity=identity_record,
        profile=project.machine_profile,
        override=project.host.profile_override,
        guidance=guidance,
        research=research_outcome,
    )


def _llm_client_for_investigations(config: AppConfig, settings: LMStudioSettingsPayload) -> LMStudioClient:
    return LMStudioClient(
        base_url=settings.base_url,
        timeout=settings.request_timeout_seconds if settings.use_custom_timeout else config.lmstudio_timeout_seconds,
        api_token=settings.api_token,
    )


def _investigation_recent_messages(
    investigation: InvestigationSession,
    *,
    limit: int = CONVERSATION_CONTEXT_WINDOW,
) -> list[dict[str, Any]]:
    visible = _visible_conversation_messages(investigation)
    recent = visible[-limit:]
    return [
        {
            "sender": message.sender_type,
            "message_type": message.message_type,
            "content": message.content,
            "question_type": (message.metadata_json or {}).get("question_type"),
        }
        for message in recent
    ]


def investigation_message_requires_recollect(investigation: InvestigationSession, content: str) -> bool:
    if not (investigation.raw_facts_json or {}):
        return True
    lowered = content.strip().lower()
    if not lowered:
        return False
    return any(token in lowered for token in RECOLLECT_HINTS)


_WEB_SEARCH_TRIGGERS = (
    "websearch", "web search", "search the web", "search online",
    "look up", "look it up", "google", "find online",
)


def _extract_web_search_query(investigation: InvestigationSession) -> str | None:
    visible = [
        m for m in investigation.messages
        if not m.hidden_internal and m.sender_type == "user"
    ]
    if not visible:
        return None
    latest = visible[-1].content.strip()
    lowered = latest.lower()
    if not any(trigger in lowered for trigger in _WEB_SEARCH_TRIGGERS):
        return None
    best_query = ""
    for trigger in _WEB_SEARCH_TRIGGERS:
        pos = lowered.find(trigger)
        if pos == -1:
            continue
        after = latest[pos + len(trigger):].strip()
        if after.lower().startswith("for "):
            after = after[4:].strip()
        for sep in (", then ", ". then ", " and then ", ", after that", ". after that"):
            cut = after.lower().find(sep)
            if cut > 10:
                after = after[:cut].strip()
                break
        if len(after) > len(best_query):
            best_query = after
    if len(best_query) > 8:
        return best_query[:200]
    return latest[:200]


def _run_investigation_web_search(
    session: Session,
    config: AppConfig,
    investigation: InvestigationSession,
) -> list[dict[str, Any]]:
    try:
        settings = get_settings(session, config, WEB_SEARCH_SETTINGS_KEY, WebSearchSettingsPayload)
        if not settings.enabled or settings.engine == "disabled":
            return []

        query = _extract_web_search_query(investigation)
        if not query:
            return []

        from app.websearch.client import WebSearchClient
        from app.websearch.config import parse_domain_filters

        normalized = settings.model_copy(update={"domain_filters": parse_domain_filters(settings.domain_filters)})
        client = WebSearchClient(normalized)
        results = client.search(query)
        evidence: list[dict[str, Any]] = []
        for result in results[: settings.result_count]:
            try:
                snippet = client.load_result(result)
            except Exception:
                snippet = result.snippet
            evidence.append({
                "title": result.title,
                "url": result.url,
                "snippet": (snippet or result.snippet or "")[:600],
            })
        return evidence
    except Exception:
        return []


def _build_investigation_llm_packet(
    investigation: InvestigationSession,
    *,
    facts: dict[str, Any],
    identity: dict[str, Any],
    topology: dict[str, Any],
    mapping: dict[str, Any],
    base_draft: dict[str, Any],
    draft_seed: dict[str, Any],
    proposals: list[dict[str, Any]],
    question_candidates: list[dict[str, Any]],
    conversation_context_summary: str | None,
    assistant_draft: dict[str, Any] | None,
    web_search_results: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    facts_summary = {
        "os": facts.get("os_pretty_name"),
        "virtualization": facts.get("virtualization"),
        "management_ip": facts.get("management_ip"),
        "fqdn": facts.get("fqdn"),
        "cpu_cores": facts.get("cpu_cores"),
        "cpu_threads": facts.get("cpu_threads"),
        "memory_gib": facts.get("memory_gib"),
        "root_size_gib": facts.get("root_size_gib"),
        "selected_running_services": [
            service.get("service_name")
            for service in (facts.get("selected_running_services") or [])[:12]
            if service.get("service_name")
        ],
        "project_paths": facts.get("project_paths") or [],
        "guest_inventory_count": len(facts.get("guest_inventory") or []),
    }
    compact_proposals = [
        {
            "proposal_type": row.get("proposal_type"),
            "target_kind": row.get("target_kind"),
            "field_name": row.get("field_name"),
            "new_value": row.get("new_value"),
            "rationale": row.get("rationale"),
        }
        for row in proposals[:8]
    ]
    return {
        "target": {
            "target_name": investigation.target_name,
            "target_address": investigation.target_address,
            "target_type": investigation.target_type,
            "linked_host_id": investigation.linked_host_id,
            "linked_project_id": investigation.linked_project_id,
        },
        "facts_summary": facts_summary,
        "identity": identity,
        "topology": topology,
        "mapping": mapping,
        "base_draft": {
            "title": base_draft.get("title"),
            "overview": base_draft.get("overview"),
            "report_text": base_draft.get("report_text"),
            "publish_title": base_draft.get("publish_title"),
            "publish_summary": base_draft.get("publish_summary"),
        },
        "current_draft": {
            "title": draft_seed.get("title"),
            "overview": draft_seed.get("overview"),
            "report_text": draft_seed.get("report_text"),
            "publish_title": draft_seed.get("publish_title"),
            "publish_summary": draft_seed.get("publish_summary"),
            "detail_sections": draft_seed.get("detail_sections"),
            "role_in_lab": draft_seed.get("role_in_lab"),
            "setup_summary": draft_seed.get("setup_summary"),
            "notes_or_highlights": draft_seed.get("notes_or_highlights"),
            "custom_sections": draft_seed.get("custom_sections"),
        },
        "assistant_draft_snapshot": {
            "title": (assistant_draft or {}).get("title"),
            "overview": (assistant_draft or {}).get("overview"),
            "report_text": (assistant_draft or {}).get("report_text"),
            "publish_title": (assistant_draft or {}).get("publish_title"),
            "publish_summary": (assistant_draft or {}).get("publish_summary"),
            "publish_long_description": (assistant_draft or {}).get("publish_long_description"),
        },
        "proposals": compact_proposals,
        "recent_messages": _investigation_recent_messages(
            investigation,
            limit=CONVERSATION_CONTEXT_WINDOW,
        ),
        "conversation_context_summary": conversation_context_summary,
        "asked_question_types": sorted(_asked_question_types(investigation)),
        "allowed_question_candidates": question_candidates,
        "user_answer_notes": _current_answer_notes(investigation),
        "open_question": (_last_open_question(investigation).content if _last_open_question(investigation) else None),
        "web_search_results": web_search_results or [],
    }


def _apply_llm_turn_to_draft(draft: dict[str, Any], llm_turn: Any) -> dict[str, Any]:
    updated = deepcopy(draft)
    if llm_turn.draft_title:
        updated["title"] = llm_turn.draft_title
        if not llm_turn.publish_title:
            updated["publish_title"] = llm_turn.draft_title
        if not updated.get("card_title"):
            updated["card_title"] = llm_turn.draft_title
    if llm_turn.draft_overview:
        updated["overview"] = llm_turn.draft_overview
        if not llm_turn.draft_report_text:
            updated["report_text"] = llm_turn.draft_overview
            updated["publish_long_description"] = llm_turn.draft_overview
        if not llm_turn.publish_summary:
            updated["publish_summary"] = llm_turn.draft_overview
        if not updated.get("card_summary"):
            updated["card_summary"] = llm_turn.draft_overview
    if llm_turn.draft_report_text:
        updated["report_text"] = llm_turn.draft_report_text
        updated["publish_long_description"] = llm_turn.draft_report_text
        updated["card_summary"] = llm_turn.draft_report_text
    if llm_turn.publish_title:
        updated["publish_title"] = llm_turn.publish_title
        updated["card_title"] = llm_turn.publish_title
    if llm_turn.publish_summary:
        updated["publish_summary"] = llm_turn.publish_summary
    if getattr(llm_turn, "publish_long_description", None):
        updated["publish_long_description"] = llm_turn.publish_long_description
    if getattr(llm_turn, "detail_sections", None):
        updated["detail_sections"] = llm_turn.detail_sections
    if getattr(llm_turn, "role_in_lab", None):
        updated["role_in_lab"] = llm_turn.role_in_lab
    if getattr(llm_turn, "setup_summary", None):
        updated["setup_summary"] = llm_turn.setup_summary
    if getattr(llm_turn, "notes_or_highlights", None):
        updated["notes_or_highlights"] = llm_turn.notes_or_highlights
    custom = getattr(llm_turn, "custom_sections", None)
    if custom:
        updated["custom_sections"] = [
            {"heading": sec.heading, "items": sec.items}
            if hasattr(sec, "heading") else sec
            for sec in custom
        ]
    return updated


def _generate_llm_investigation_turn(
    session: Session,
    config: AppConfig,
    *,
    investigation: InvestigationSession,
    facts: dict[str, Any],
    identity: dict[str, Any],
    topology: dict[str, Any],
    mapping: dict[str, Any],
    base_draft: dict[str, Any],
    draft_seed: dict[str, Any],
    proposals: list[dict[str, Any]],
    question_candidates: list[dict[str, Any]],
    conversation_context_summary: str | None,
    assistant_draft: dict[str, Any] | None,
    web_search_results: list[dict[str, Any]] | None = None,
) -> tuple[Any | None, str | None]:
    settings = get_settings(session, config, LMSTUDIO_SETTINGS_KEY, LMStudioSettingsPayload)
    if not settings.enabled or not settings.selected_model:
        return None, "LM Studio is disabled or no investigation model is selected."
    packet = _build_investigation_llm_packet(
        investigation,
        facts=facts,
        identity=identity,
        topology=topology,
        mapping=mapping,
        base_draft=base_draft,
        draft_seed=draft_seed,
        proposals=proposals,
        question_candidates=question_candidates,
        conversation_context_summary=conversation_context_summary,
        assistant_draft=assistant_draft,
        web_search_results=web_search_results,
    )
    try:
        client = _llm_client_for_investigations(config, settings)
        turn = client.investigation_turn(
            model_id=settings.selected_model,
            session_packet=packet,
            temperature=min(max(settings.temperature, 0.0), 0.7),
        )
        if not turn.assistant_message:
            return None, "LM Studio investigation reply omitted assistant_message."
        return turn, None
    except Exception as exc:
        return None, f"LM Studio investigation turn failed: {exc}"


def _assistant_message_contains_question(assistant_message: str | None, question_text: str | None) -> bool:
    assistant = re.sub(r"\s+", " ", str(assistant_message or "").strip()).lower()
    question = re.sub(r"\s+", " ", str(question_text or "").strip()).lower()
    if not assistant or not question:
        return False
    return question in assistant


def _refresh_existing_project_context(
    session: Session,
    config: AppConfig,
    investigation: InvestigationSession,
    *,
    host: Host,
    project: Project | None,
) -> tuple[dict[str, Any], MachineIdentityResult | Any, dict[str, Any], Any | None]:
    facts = refresh_host_facts(session, config, host.id).facts_json or {}
    if project is None:
        identity = resolve_machine_identity(
            host_name=host.name,
            metadata={},
            host_facts=facts,
            guest_inventory=facts.get("guest_inventory") or [],
            service_inventory=facts.get("selected_running_services") or [],
            topology=build_host_topology_context(host),
            user_hints=[],
        )
        return facts, identity, {}, None

    identity_record = refresh_machine_identity(session, config, project.id)
    research_outcome = refresh_project_research(session, config, project.id, force=False)
    mapping_preview = refresh_project_mapping_preview(session, config, project.id)
    session.refresh(project)
    return facts, identity_record, _serialize_mapping(mapping_preview), research_outcome


def _manual_or_guest_identity(
    investigation: InvestigationSession,
    facts: dict[str, Any],
    topology: dict[str, Any],
) -> Any:
    metadata = {
        "grouped_roots": list((investigation.target_config_json or {}).get("roots") or []),
        "project_paths": list((investigation.target_config_json or {}).get("roots") or []),
    }
    return resolve_machine_identity(
        host_name=investigation.target_name or investigation.target_hostname or investigation.target_address or "target",
        metadata=metadata,
        host_facts=facts,
        guest_inventory=facts.get("guest_inventory") or [],
        service_inventory=facts.get("selected_running_services") or [],
        topology=topology,
        user_hints=[message.content for message in investigation.messages if message.sender_type == "user" and message.affects_generation],
    )


def regenerate_investigation_session(
    session: Session,
    config: AppConfig,
    investigation: InvestigationSession,
    *,
    recollect: bool,
    stop_event: Event | None = None,
) -> None:
    _set_status(investigation, status="running", phase="collecting" if recollect else "drafting")
    _append_message(
        session,
        investigation,
        sender_type="system",
        message_type="status",
        content="Collecting facts and refreshing the investigation draft." if recollect else "Regenerating the investigation draft from the latest answers.",
        affects_generation=False,
        hidden_internal=True,
    )
    session.flush()
    ensure_not_cancelled(stop_event, "Investigation stopped by user.")

    linked_host = investigation.linked_host
    linked_project = investigation.linked_project
    research_outcome = None
    mapping_payload: dict[str, Any] = {}
    if investigation.target_type == "existing_host" and linked_host is not None:
        facts, identity_source, mapping_payload, research_outcome = _refresh_existing_project_context(
            session,
            config,
            investigation,
            host=linked_host,
            project=linked_project,
        )
        topology = build_host_topology_context(linked_host)
        ensure_not_cancelled(stop_event, "Investigation stopped by user.")
    else:
        facts = _collect_manual_target_facts(config, investigation, stop_event=stop_event)
        parent_host = investigation.linked_guest.host if investigation.linked_guest is not None else None
        topology = {
            "parent": {
                "host_id": parent_host.id if parent_host else None,
                "name": parent_host.name if parent_host else None,
                "guest_type": investigation.linked_guest.guest_type if investigation.linked_guest else None,
                "guest_id": investigation.linked_guest.guest_id if investigation.linked_guest else None,
                "platform": "Proxmox VE" if parent_host and (parent_host.fact_snapshot and parent_host.fact_snapshot.detected_platform) == "Proxmox VE" else parent_host.parent_platform if parent_host else None,
            }
        } if parent_host else {}
        identity_source = _manual_or_guest_identity(investigation, facts, topology)
        ensure_not_cancelled(stop_event, "Investigation stopped by user.")

    identity_payload = _serialize_identity(identity_source)
    public_copy = _build_public_copy_for_session(
        investigation,
        project=linked_project,
        identity_record=identity_source if isinstance(identity_source, MachineIdentityResult) else None,
        research_outcome=research_outcome,
    )
    findings, normalized = _build_findings_payload(
        investigation,
        facts=facts,
        identity=identity_payload,
        topology=topology,
        mapping=mapping_payload,
        project=linked_project,
    )
    base_draft = _build_draft_payload(
        investigation,
        facts=facts,
        identity=identity_payload,
        mapping=mapping_payload,
        project=linked_project,
        public_copy=public_copy,
    )
    guidance = _temporary_guidance(investigation, linked_project)
    base_draft = _sanitize_investigation_draft(
        investigation,
        linked_project,
        base_draft,
        guidance=guidance,
    )
    existing_state = investigation_draft_state_payload(investigation)
    existing_assistant_draft = _normalize_investigation_draft_payload(existing_state.get("assistant_draft"))
    existing_public_preview_draft = _normalize_investigation_draft_payload(existing_state.get("public_preview_draft"))
    assistant_draft_version = existing_state.get("assistant_draft_version")
    assistant_draft_updated_at = existing_state.get("assistant_draft_updated_at")
    conversation_context_summary = _conversation_context_summary(investigation) or existing_state.get("conversation_context_summary")
    current_open_question = _last_open_question(investigation)
    draft_seed = deepcopy(existing_assistant_draft or base_draft)
    _persist_investigation_draft_state(
        investigation,
        base_draft=base_draft,
        working_draft=draft_seed,
        assistant_draft=existing_assistant_draft,
        public_preview_draft=existing_public_preview_draft,
        assistant_draft_version=assistant_draft_version,
        assistant_draft_updated_at=assistant_draft_updated_at,
        open_question=current_open_question.content if current_open_question else None,
        conversation_context_summary=conversation_context_summary,
    )
    session.flush()
    question_candidates = _question_candidates(investigation, identity=identity_payload, mapping=mapping_payload, project=linked_project)
    proposal_seed = _build_proposal_rows(
        investigation,
        mapping=mapping_payload,
        draft=existing_assistant_draft or base_draft,
        project=linked_project,
    )
    web_search_results = _run_investigation_web_search(session, config, investigation)
    llm_turn, llm_error = _generate_llm_investigation_turn(
        session,
        config,
        investigation=investigation,
        facts=facts,
        identity=identity_payload,
        topology=topology,
        mapping=mapping_payload,
        base_draft=base_draft,
        draft_seed=draft_seed,
        proposals=proposal_seed,
        question_candidates=question_candidates,
        conversation_context_summary=conversation_context_summary,
        assistant_draft=existing_assistant_draft,
        web_search_results=web_search_results,
    )
    assistant_draft = existing_assistant_draft
    public_preview_draft = existing_public_preview_draft or existing_assistant_draft
    if llm_turn is not None and _llm_turn_has_draft_update(llm_turn):
        assistant_draft = _apply_llm_turn_to_draft(deepcopy(draft_seed), llm_turn)
        assistant_draft = _sanitize_investigation_draft(
            investigation,
            linked_project,
            assistant_draft,
            guidance=guidance,
        )
        public_preview_draft = deepcopy(assistant_draft)
        assistant_draft_version = int(assistant_draft_version or 0) + 1
        assistant_draft_updated_at = utcnow().isoformat()
        _append_session_draft(session, investigation, assistant_draft)
    final_draft = assistant_draft or base_draft
    proposals = _build_proposal_rows(
        investigation,
        mapping=mapping_payload,
        draft=final_draft,
        project=linked_project,
    )

    investigation.raw_facts_json = facts
    investigation.normalized_findings_json = normalized
    _persist_investigation_draft_state(
        investigation,
        base_draft=base_draft,
        working_draft=None,
        assistant_draft=assistant_draft,
        public_preview_draft=public_preview_draft,
        assistant_draft_version=assistant_draft_version,
        assistant_draft_updated_at=assistant_draft_updated_at,
        open_question=current_open_question.content if current_open_question else None,
        conversation_context_summary=conversation_context_summary,
    )
    investigation.proposed_host_changes_json = [item for item in proposals if item["target_kind"] == "host"]
    investigation.proposed_project_changes_json = [item for item in proposals if item["target_kind"] == "project"]
    investigation.last_error = llm_error
    _replace_session_findings(session, investigation, findings)
    _replace_proposals(session, investigation, proposals)
    session.flush()
    ensure_not_cancelled(stop_event, "Investigation stopped by user.")

    question_meta = None
    has_open_question = _last_open_question(investigation) is not None
    if llm_turn is not None and llm_turn.question is not None and not has_open_question:
        question_type = str(llm_turn.question.question_type or "").strip()
        already_asked = question_type and question_type in _asked_question_types(investigation)
        not_in_candidates = question_type and not any(
            candidate["question_type"] == question_type for candidate in question_candidates
        )
        if not already_asked and not not_in_candidates:
            question_meta = {
                "question_type": question_type or (question_candidates[0]["question_type"] if question_candidates else "follow_up"),
                "question_text": llm_turn.question.question_text,
                "ai_rationale": llm_turn.question.ai_rationale or "This clarification should improve the current draft.",
            }

    if llm_turn is not None and llm_turn.assistant_message:
        message_content = llm_turn.assistant_message
        is_question = False

        if question_meta is not None:
            is_question = True
            if not _assistant_message_contains_question(message_content, question_meta["question_text"]):
                message_content = f"{message_content}\n\n{question_meta['question_text']}"

        _append_message(
            session,
            investigation,
            sender_type="ai",
            message_type="question" if is_question else "summary",
            content=message_content,
            affects_generation=is_question,
            metadata=question_meta,
        )

        if is_question:
            _set_status(
                investigation,
                status="awaiting_user",
                phase="awaiting_user",
                error=investigation.last_error,
            )
            _persist_investigation_draft_state(
                investigation,
                base_draft=base_draft,
                working_draft=None,
                assistant_draft=assistant_draft,
                public_preview_draft=public_preview_draft,
                assistant_draft_version=assistant_draft_version,
                assistant_draft_updated_at=assistant_draft_updated_at,
                open_question=question_meta["question_text"],
                conversation_context_summary=conversation_context_summary,
            )
            session.flush()
            return

    if llm_turn is not None and llm_turn.ready_for_confirmation:
        _set_status(
            investigation,
            status="ready_for_confirmation",
            phase="ready_for_confirmation",
            error=investigation.last_error,
        )
    else:
        _set_status(
            investigation,
            status="ready_for_confirmation",
            phase="ready_for_confirmation",
            error=investigation.last_error,
        )
    session.flush()


def investigation_runtime_state(app: FastAPI, investigation_id: int) -> dict[str, Any]:
    control = _get_investigation_control(app, investigation_id)
    return {
        "has_active_worker": bool(control),
        "has_pending_follow_up": bool(control.get("pending_refresh")),
        "pending_recollect": bool(control.get("pending_recollect")),
    }


def _run_collect_job(app: FastAPI, investigation_id: int, *, recollect: bool) -> None:
    requested_recollect = recollect
    worker_control = _get_investigation_control(app, investigation_id)
    worker_stop_event = worker_control.get("stop_event") if worker_control else None
    try:
        while True:
            with session_scope(app.state.session_factory) as session:
                investigation = load_investigation_session(session, investigation_id)
                try:
                    regenerate_investigation_session(
                        session,
                        app.state.config,
                        investigation,
                        recollect=requested_recollect,
                        stop_event=worker_stop_event,
                    )
                except JobCancelled as exc:
                    _set_status(investigation, status="cancelled", phase="cancelled", error=str(exc))
                    _append_message(
                        session,
                        investigation,
                        sender_type="system",
                        message_type="status",
                        content=str(exc),
                        affects_generation=False,
                        hidden_internal=True,
                    )
                    return
                except Exception as exc:
                    _set_status(investigation, status="failed", phase="failed", error=str(exc))
                    _append_message(
                        session,
                        investigation,
                        sender_type="system",
                        message_type="status",
                        content=f"Investigation failed: {exc}",
                        affects_generation=False,
                        hidden_internal=True,
                    )
                    raise

            should_continue, requested_recollect = _consume_pending_refresh(
                app,
                investigation_id,
                worker_stop_event=worker_stop_event,
            )
            if not should_continue:
                return
    finally:
        _pop_investigation_control_if_current(
            app,
            investigation_id,
            worker_stop_event=worker_stop_event,
        )


def queue_investigation_collect(app: FastAPI, investigation_id: int, *, recollect: bool = True) -> None:
    with session_scope(app.state.session_factory) as session:
        investigation = load_investigation_session(session, investigation_id)
        control = _get_investigation_control(app, investigation_id)
        if control:
            _set_status(
                investigation,
                status="queued",
                phase="queued",
                error=investigation.last_error,
            )
            _set_investigation_control(
                app,
                investigation_id,
                pending_refresh=True,
                pending_recollect=bool(control.get("pending_recollect")) or recollect,
            )
            return
        _set_status(investigation, status="queued", phase="queued")
    stop_event = Event()
    _set_investigation_control(
        app,
        investigation_id,
        stop_event=stop_event,
        future=None,
        pending_refresh=False,
        pending_recollect=False,
    )
    future = app.state.job_runner.submit(_run_collect_job, app, investigation_id, recollect=recollect)
    _set_investigation_control(app, investigation_id, future=future)


def request_investigation_stop(app: FastAPI, investigation_id: int) -> str:
    control = _get_investigation_control(app, investigation_id)
    if not control:
        return "No active investigation job was found for this session."

    future = control.get("future")
    stop_event = control.get("stop_event")
    if future is not None and future.cancel():
        if isinstance(stop_event, Event):
            stop_event.set()
        with session_scope(app.state.session_factory) as session:
            investigation = session.get(InvestigationSession, investigation_id)
            if investigation is not None:
                investigation.status = "cancelled"
                investigation.current_phase = "cancelled"
                investigation.last_error = "Investigation stopped by user before it started."
                _append_message(
                    session,
                    investigation,
                    sender_type="system",
                    message_type="status",
                    content="Cancelled queued investigation before it started.",
                    affects_generation=False,
                    hidden_internal=True,
                )
        _pop_investigation_control(app, investigation_id)
        return f"Cancelled queued investigation #{investigation_id}."

    if isinstance(stop_event, Event):
        stop_event.set()
    with session_scope(app.state.session_factory) as session:
        investigation = session.get(InvestigationSession, investigation_id)
        if investigation is not None and investigation.status in {"queued", "running", "drafting", "applying"}:
            investigation.status = "cancelling"
            investigation.current_phase = "cancelling"
            _append_message(
                session,
                investigation,
                sender_type="system",
                message_type="status",
                content=f"Stop requested for investigation #{investigation_id}.",
                affects_generation=False,
                hidden_internal=True,
            )
    return f"Stop requested for investigation #{investigation_id}."


def reconcile_orphaned_investigation(app: FastAPI, session: Session, investigation: InvestigationSession) -> bool:
    if investigation.status not in RUNNING_STATUSES:
        return False
    if _get_investigation_control(app, investigation.id):
        return False

    if investigation.status == "cancelling":
        investigation.status = "cancelled"
        investigation.current_phase = "cancelled"
        investigation.last_error = investigation.last_error or "Investigation stop was requested, but the worker was no longer active."
        _append_message(
            session,
            investigation,
            sender_type="system",
            message_type="status",
            content="Investigation stop was requested, but the worker was no longer active. Marked cancelled.",
            affects_generation=False,
            hidden_internal=True,
        )
    else:
        investigation.status = "failed"
        investigation.current_phase = "failed"
        investigation.last_error = investigation.last_error or "Investigation worker was no longer active. Marked failed."
        _append_message(
            session,
            investigation,
            sender_type="system",
            message_type="status",
            content="Investigation worker was no longer active. Marked failed.",
            affects_generation=False,
            hidden_internal=True,
        )
    session.flush()
    return True


def delete_investigation_session(app: FastAPI, session: Session, investigation: InvestigationSession) -> None:
    _pop_investigation_control(app, investigation.id)
    session.delete(investigation)
    session.flush()


def post_investigation_message(session: Session, investigation: InvestigationSession, content: str) -> InvestigationMessage:
    cleaned = content.strip()
    if not cleaned:
        raise ValueError("Message text is required.")
    open_question = _last_open_question(investigation)
    metadata: dict[str, Any] = {}
    message_type = "note"
    if open_question is not None:
        message_type = "answer"
        metadata = {
            "answer_to_message_id": open_question.id,
            "question_type": (open_question.metadata_json or {}).get("question_type"),
        }
    lowered = cleaned.lower()
    if any(phrase in lowered for phrase in CONFIRM_PHRASES):
        investigation.confirmation_state = "confirmed"
        _set_status(investigation, status="ready_for_confirmation", phase="ready_for_confirmation")
        _append_message(
            session,
            investigation,
            sender_type="system",
            message_type="status",
            content="Confirmation noted from chat. You can apply the proposed changes whenever you are ready.",
            affects_generation=False,
            hidden_internal=True,
        )
    message = _append_message(
        session,
        investigation,
        sender_type="user",
        message_type=message_type,
        content=cleaned,
        affects_generation=True,
        metadata=metadata or None,
    )
    session.flush()
    return message


def confirm_investigation(session: Session, investigation: InvestigationSession) -> None:
    investigation.confirmation_state = "confirmed"
    _set_status(investigation, status="ready_for_confirmation", phase="ready_for_confirmation")
    _append_message(
        session,
        investigation,
        sender_type="system",
        message_type="status",
        content="Investigation confirmed. Review the proposals once more if needed, then apply them.",
        affects_generation=False,
        hidden_internal=True,
    )
    session.flush()


def _ensure_host(session: Session, investigation: InvestigationSession) -> Host:
    if investigation.linked_host is not None:
        return investigation.linked_host
    config = investigation.target_config_json or {}
    payload = HostUpsert(
        name=investigation.target_name or investigation.target_hostname or "investigation-target",
        address=investigation.target_address or "",
        ssh_port=int(config.get("ssh_port") or 22),
        ssh_user=str(config.get("ssh_user") or "aicms"),
        ssh_key_path=config.get("ssh_key_path"),
        enabled=True,
        roots=list(config.get("roots") or []),
    )
    host = Host(
        name=payload.name,
        address=payload.address,
        ssh_port=payload.ssh_port,
        ssh_user=payload.ssh_user,
        ssh_key_path=payload.ssh_key_path,
        enabled=payload.enabled,
        connection_status="unknown",
        roots=[HostRoot(root_path=root, enabled=True) for root in payload.roots],
    )
    session.add(host)
    session.flush()
    investigation.linked_host_id = host.id
    investigation.linked_host = host
    if investigation.linked_guest is not None:
        link_discovered_guest_to_host(session, investigation.linked_guest, host, source="investigation", confidence=1.0)
    return host


def _project_for_apply(host: Host) -> Project | None:
    return primary_project_for_host(host)


def _persist_investigation_answers(
    session: Session,
    investigation: InvestigationSession,
    project: Project | None,
) -> None:
    if project is None:
        return
    for message in investigation.messages:
        if message.sender_type != "user" or message.message_type != "answer":
            continue
        meta = message.metadata_json or {}
        question_type = str(meta.get("question_type") or "summary_framing")
        apply_investigation_guidance_answer(
            session,
            project,
            question_type=question_type,
            answer=message.content,
            source_label=f"Investigation session #{investigation.id}",
        )


def apply_investigation(session: Session, app: FastAPI, investigation: InvestigationSession) -> InvestigationSession:
    if investigation.confirmation_state != "confirmed":
        raise ValueError("Confirm the investigation before applying proposed changes.")
    _set_status(investigation, status="applying", phase="applying")
    _append_message(
        session,
        investigation,
        sender_type="system",
        message_type="status",
        content="Applying the approved host and project changes.",
        affects_generation=False,
        hidden_internal=True,
    )
    session.flush()

    host = _ensure_host(session, investigation)
    refresh_host_facts(session, app.state.config, host.id)
    project = investigation.linked_project or _project_for_apply(host)
    if project is not None:
        _persist_investigation_answers(session, investigation, project)
        refresh_machine_identity(session, app.state.config, project.id)
        refresh_project_research(session, app.state.config, project.id, force=False)
        refresh_project_mapping_preview(session, app.state.config, project.id)
        investigation.linked_project_id = project.id
    else:
        queue_scan(app, "full", [host.id])
        _append_message(
            session,
            investigation,
            sender_type="system",
            message_type="note",
            content="Created the host record and queued a full scan so AICMS can discover or refresh the project data before the mapping step continues.",
            affects_generation=False,
            hidden_internal=True,
        )
    investigation.confirmation_state = "applied"
    _set_status(investigation, status="applied", phase="applied")
    _append_message(
        session,
        investigation,
        sender_type="system",
        message_type="summary",
        content="Applied the confirmed investigation changes and preserved the session chat, findings, draft, and proposal history.",
        affects_generation=False,
        hidden_internal=True,
    )
    session.flush()
    return investigation
