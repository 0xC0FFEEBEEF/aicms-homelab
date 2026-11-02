from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    MachineIdentityResult,
    Project,
    ProjectClarificationGuidance,
    ProjectMappingPreview,
    ProjectQuestionMessage,
    ProjectQuestionThread,
)
from app.topology.service import build_host_topology_context
from app.utils.common import utcnow

ACTIVE_QUESTION_STATUSES = {"open", "answered"}
TERMINAL_QUESTION_STATUSES = {"ignored", "resolved"}
GENERIC_SIGNATURES = {None, "generic_project_vm", "generic_service_node"}
WEAK_TITLE_PREFIXES = ("Generic ", "Unresolved ", "Fallback ", "Unknown ")
GENERATION_SCOPES = {"identity", "title", "summary", "mapping", "public_copy", "all"}


def load_project_clarification_context(session: Session, project_id: int) -> Project:
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


def get_or_create_project_guidance(session: Session, project: Project) -> ProjectClarificationGuidance:
    guidance = project.clarification_guidance
    if guidance is None:
        guidance = session.execute(
            select(ProjectClarificationGuidance).where(ProjectClarificationGuidance.project_id == project.id)
        ).scalar_one_or_none()
    if guidance is None:
        guidance = ProjectClarificationGuidance(project_id=project.id, host_id=project.host_id)
        session.add(guidance)
        session.flush()
        project.clarification_guidance = guidance
    return guidance


def apply_investigation_guidance_answer(
    session: Session,
    project: Project,
    *,
    question_type: str,
    answer: str,
    source_label: str | None = None,
) -> ProjectClarificationGuidance:
    guidance = get_or_create_project_guidance(session, project)
    thread = ProjectQuestionThread(
        project_id=project.id,
        host_id=project.host_id,
        status="answered",
        question_type=question_type,
        question_text=source_label or "Investigation answer imported into project guidance.",
        ai_rationale=source_label,
        my_answer=answer.strip(),
        answer_affects_generation=True,
        public_safe=False,
        created_by="user",
        last_answered_at=utcnow(),
    )
    session.add(thread)
    session.flush()
    _append_message(
        session,
        thread,
        author_role="user",
        message_text=answer.strip(),
        created_by="user",
    )
    _apply_answer_to_guidance(guidance, thread, answer)
    session.flush()
    return guidance


def project_question_counts(project: Project) -> dict[str, int]:
    counts = {"open": 0, "answered": 0, "resolved": 0, "ignored": 0}
    for thread in project.clarification_threads:
        counts[thread.status] = counts.get(thread.status, 0) + 1
    return counts


def _is_weak_title(title: str | None) -> bool:
    text = (title or "").strip()
    if not text:
        return True
    return any(text.startswith(prefix) for prefix in WEAK_TITLE_PREFIXES)


def _is_weak_summary(summary: str | None) -> bool:
    text = (summary or "").strip().lower()
    if not text:
        return True
    weak_tokens = (
        "needs review",
        "still needs review",
        "appears to be part of my lab",
        "unresolved workload",
        "unresolved service",
        "generic ",
    )
    return any(token in text for token in weak_tokens)


def _active_thread_for_type(project: Project, question_type: str) -> ProjectQuestionThread | None:
    for thread in project.clarification_threads:
        if thread.question_type == question_type and thread.status in ACTIVE_QUESTION_STATUSES:
            return thread
    return None


def _all_threads_for_type(project: Project, question_type: str) -> list[ProjectQuestionThread]:
    return [thread for thread in project.clarification_threads if thread.question_type == question_type]


def _append_message(
    session: Session,
    thread: ProjectQuestionThread,
    *,
    author_role: str,
    message_text: str,
    created_by: str,
) -> ProjectQuestionMessage:
    message = ProjectQuestionMessage(
        thread_id=thread.id,
        project_id=thread.project_id,
        author_role=author_role,
        message_text=message_text.strip(),
        created_by=created_by,
    )
    session.add(message)
    session.flush()
    return message


def _add_note(guidance: ProjectClarificationGuidance, note: str) -> None:
    existing = list(guidance.guidance_notes_json or [])
    cleaned = note.strip()
    if not cleaned or cleaned in existing:
        guidance.guidance_notes_json = existing
        return
    existing.append(cleaned)
    guidance.guidance_notes_json = existing


def _add_source_question(guidance: ProjectClarificationGuidance, thread_id: int) -> None:
    existing = list(guidance.source_question_ids_json or [])
    if thread_id not in existing:
        existing.append(thread_id)
    guidance.source_question_ids_json = existing


def _answer_implies_true(answer: str, tokens: tuple[str, ...]) -> bool:
    lowered = answer.lower()
    return any(token in lowered for token in tokens)


def _apply_answer_to_guidance(
    guidance: ProjectClarificationGuidance,
    thread: ProjectQuestionThread,
    answer: str,
) -> None:
    cleaned = answer.strip()
    if not cleaned:
        return

    if thread.question_type == "public_title":
        guidance.preferred_public_title = cleaned
    elif thread.question_type == "role_framing":
        guidance.preferred_role_wording = cleaned
        guidance.preferred_summary_framing = cleaned
    elif thread.question_type == "project_positioning":
        lowered = cleaned.lower()
        if any(token in lowered for token in ("part of", "part-of", "under", "within", "inside")):
            guidance.standalone_preference = "part_of_parent"
        elif any(token in lowered for token in ("standalone", "separate", "own project", "public project")):
            guidance.standalone_preference = "standalone"
        else:
            guidance.standalone_preference = "needs_review"
    elif thread.question_type == "publishability":
        lowered = cleaned.lower()
        if any(token in lowered for token in ("internal", "private", "do not publish", "don't publish")):
            guidance.internal_only = True
            guidance.publishable_project = False
        elif any(token in lowered for token in ("publish", "public", "standalone project", "yes")):
            guidance.publishable_project = True
            guidance.internal_only = False
    elif thread.question_type == "project_grouping":
        guidance.preferred_project_group = cleaned
    elif thread.question_type == "summary_framing":
        guidance.preferred_summary_framing = cleaned

    _add_note(guidance, cleaned)
    _add_source_question(guidance, thread.id)
    guidance.last_applied_at = utcnow()


def answer_project_question(
    session: Session,
    thread: ProjectQuestionThread,
    answer: str,
    *,
    created_by: str = "user",
) -> ProjectClarificationGuidance:
    cleaned = answer.strip()
    if not cleaned:
        raise ValueError("Answer text is required.")
    _append_message(
        session,
        thread,
        author_role="user",
        message_text=cleaned,
        created_by=created_by,
    )
    thread.my_answer = cleaned
    thread.status = "answered"
    thread.last_answered_at = utcnow()
    thread.updated_at = utcnow()
    guidance = get_or_create_project_guidance(session, thread.project)
    _apply_answer_to_guidance(guidance, thread, cleaned)
    session.flush()
    return guidance


def set_project_question_status(
    session: Session,
    thread: ProjectQuestionThread,
    status: str,
) -> ProjectQuestionThread:
    normalized = status.strip().lower()
    if normalized not in {"open", "answered", "ignored", "resolved"}:
        raise ValueError("Unsupported question status.")
    thread.status = normalized
    thread.updated_at = utcnow()
    session.flush()
    return thread


def mark_project_clarifications_used(session: Session, project: Project) -> None:
    now = utcnow()
    for thread in project.clarification_threads:
        if thread.my_answer and thread.answer_affects_generation and thread.status in {"answered", "resolved"}:
            thread.last_used_in_generation_at = now
    guidance = project.clarification_guidance
    if guidance is not None:
        guidance.last_applied_at = now
    session.flush()


def _question_payloads(project: Project) -> list[dict[str, Any]]:
    identity: MachineIdentityResult | None = project.identity_result
    mapping: ProjectMappingPreview | None = project.mapping_preview
    host = project.host
    guidance = project.clarification_guidance
    topology = build_host_topology_context(host) if host else {}

    primary_signature = identity.primary_signature_id if identity else None
    identity_confidence = float(identity.identity_confidence or 0) if identity else 0.0
    title_candidate = None
    summary_candidate = None
    if mapping is not None:
        title_candidate = (
            mapping.publish_title_candidate
            or mapping.card_title_candidate
            or mapping.project_title_candidate
        )
        summary_candidate = (
            mapping.publish_long_description_candidate
            or mapping.publish_summary_candidate
            or mapping.project_summary_candidate
        )

    candidates: list[dict[str, Any]] = []
    parent = topology.get("parent") or {}
    parent_name = parent.get("name") or host.parent_name if host else None

    if identity is None or identity_confidence < 0.78 or primary_signature in GENERIC_SIGNATURES:
        candidates.append(
            {
                "question_type": "role_framing",
                "question_text": (
                    f"How do you want me to frame {project.name_guess or host.name} publicly? "
                    "What is the most accurate short description of its role in your environment?"
                ),
                "ai_rationale": "Identity is still ambiguous enough that a clearer role description would materially improve title, summary, and mapping quality.",
            }
        )

    if _is_weak_title(title_candidate):
        candidates.append(
            {
                "question_type": "public_title",
                "question_text": "What exact public title do you want me to use for this project?",
                "ai_rationale": "The current title candidate is weak or generic, so a preferred title would improve previews and future CMS export.",
            }
        )

    publishability_unset = guidance is None or (
        guidance.publishable_project is None and guidance.internal_only is None
    )
    if publishability_unset:
        if mapping is None or mapping.recommended_status == "needs_review":
            candidates.append(
                {
                    "question_type": "publishability",
                    "question_text": "Should I treat this as publishable later, internal-only, or still needs review for future CMS export?",
                    "ai_rationale": "Publishability is still ambiguous, and a direct editorial answer would improve later project mapping decisions.",
                }
            )

    if parent_name:
        candidates.append(
            {
                "question_type": "project_positioning",
                "question_text": (
                    f"Should this machine be presented publicly as a standalone project, or mainly as part of {parent_name}?"
                ),
                "ai_rationale": "This machine has known topology context, so its public positioning may be clearer with an explicit owner decision.",
            }
        )

    if (mapping is not None and _is_weak_summary(summary_candidate)) or primary_signature in GENERIC_SIGNATURES:
        candidates.append(
            {
                "question_type": "summary_framing",
                "question_text": "What summary framing do you want me to emphasize when I describe this project publicly?",
                "ai_rationale": "The current public wording is still too generic, so a short framing note would improve future card and publish copy.",
            }
        )

    if identity and identity.primary_signature_id == "proxmox_hypervisor":
        candidates.append(
            {
                "question_type": "project_grouping",
                "question_text": "Do you want this hypervisor framed mainly as a standalone infrastructure project, or as the host that ties a broader project family together?",
                "ai_rationale": "Hypervisors often sit between infrastructure and project-group framing, so an editorial decision improves later mapping quality.",
            }
        )

    return candidates


def generate_project_clarification_questions(
    session: Session,
    project: Project,
    *,
    force: bool = False,
    max_questions: int = 4,
) -> list[ProjectQuestionThread]:
    created: list[ProjectQuestionThread] = []
    active_count = sum(1 for thread in project.clarification_threads if thread.status in ACTIVE_QUESTION_STATUSES)
    if active_count >= max_questions and not force:
        return created

    for candidate in _question_payloads(project):
        existing_threads = _all_threads_for_type(project, candidate["question_type"])
        if existing_threads and not force:
            continue
        if existing_threads and force and any(thread.status in ACTIVE_QUESTION_STATUSES for thread in existing_threads):
            continue
        if len(created) + active_count >= max_questions:
            break

        thread = ProjectQuestionThread(
            project_id=project.id,
            host_id=project.host_id,
            status="open",
            question_type=candidate["question_type"],
            question_text=candidate["question_text"],
            ai_rationale=candidate["ai_rationale"],
            answer_affects_generation=True,
            public_safe=False,
            linked_profile_version=(project.machine_profile.last_generated_at.isoformat() if project.machine_profile else None),
            created_by="ai",
        )
        session.add(thread)
        session.flush()
        _append_message(
            session,
            thread,
            author_role="ai",
            message_text=thread.question_text,
            created_by="ai",
        )
        project.clarification_threads.append(thread)
        created.append(thread)

    session.flush()
    return created


def ask_project_question_follow_up(
    session: Session,
    thread: ProjectQuestionThread,
) -> ProjectQuestionThread:
    follow_up_map = {
        "role_framing": "What is the shortest accurate way you want me to describe this machine's role?",
        "public_title": "If you have a preferred public title, what exact wording should I use?",
        "publishability": "Should I treat this as publishable, internal-only, or needs-review for later export?",
        "project_positioning": "If this should be part of another project, what parent project or family should I attach it to conceptually?",
        "project_grouping": "What project family or group name would you want me to associate with this machine?",
        "summary_framing": "What angle should I lead with when describing this project publicly?",
    }
    follow_up = follow_up_map.get(
        thread.question_type,
        "What additional clarification would most improve how I describe this project?",
    )
    thread.question_text = follow_up
    thread.status = "open"
    thread.updated_at = utcnow()
    thread.created_by = "ai"
    _append_message(
        session,
        thread,
        author_role="ai",
        message_text=follow_up,
        created_by="ai",
    )
    session.flush()
    return thread
