from __future__ import annotations

import re
from typing import Any

from app.models import Project, ProjectClarificationGuidance

ALL_GUEST_HINTS = (
    "list all",
    "all my vms",
    "all my vm",
    "all my lxcs",
    "all my lxc",
    "each of the vms",
    "each of the vm",
    "each of the lxc",
    "each of the lxcs",
    "all the vms",
    "all the lxcs",
    "all the lxc",
    "all the vm",
)

GUIDANCE_FILTER_PREFIXES = (
    "focus on ",
    "focus only on ",
    "emphasize ",
    "mention ",
    "include ",
    "exclude ",
    "do a web search",
    "search for ",
    "list all ",
    "list each ",
    "avoid ",
    "don't ",
    "do not ",
    "remove ",
    "strip out ",
)

BANNED_ROLE_LABELS = (
    "service workload node",
    "generic service node",
    "generic project vm",
    "generic linux node",
)

PHRASE_CAPTURE_PATTERNS = (
    r"""remove talk like\s+["“']?([^"”'\n]+)["”']?""",
    r"""remove references to\s+["“']?([^"”'\n]+)["”']?""",
    r"""avoid saying\s+["“']?([^"”'\n]+)["”']?""",
    r"""don't mention\s+["“']?([^"”'\n]+)["”']?""",
    r"""do not mention\s+["“']?([^"”'\n]+)["”']?""",
    r"""strip out\s+["“']?([^"”'\n]+)["”']?""",
)


def _operator_context_text(project: Project | None) -> str:
    if project is None:
        return ""
    metadata = project.metadata_json or {}
    operator_context = metadata.get("operator_context") or {}
    return "\n".join(
        str(operator_context.get(field) or "").strip()
        for field in ("added_info", "extra_instructions")
        if str(operator_context.get(field) or "").strip()
    )


def _guidance_texts(
    project: Project | None,
    *,
    guidance: ProjectClarificationGuidance | None = None,
    extra_texts: list[str] | None = None,
) -> list[str]:
    texts = [_operator_context_text(project)]
    if guidance is not None:
        texts.extend(
            [
                str(guidance.preferred_public_title or "").strip(),
                str(guidance.preferred_role_wording or "").strip(),
                str(guidance.preferred_summary_framing or "").strip(),
            ]
        )
        texts.extend(str(item or "").strip() for item in (guidance.guidance_notes_json or []))
    if extra_texts:
        texts.extend(str(item or "").strip() for item in extra_texts)
    return [text for text in texts if text]


def is_editorial_instruction_text(text: str) -> bool:
    lowered = text.strip().lower()
    if not lowered:
        return False
    if any(lowered.startswith(prefix) for prefix in GUIDANCE_FILTER_PREFIXES):
        return True
    if any(re.search(pattern, lowered, flags=re.IGNORECASE) for pattern in PHRASE_CAPTURE_PATTERNS):
        return True
    return bool(
        "remove fqdn" in lowered
        or "remove the fqdn" in lowered
        or "talk of fqdn" in lowered
        or "remove .service" in lowered
        or "remove the .service" in lowered
        or "service crap" in lowered
        or "service noise" in lowered
    )


def _normalize_framing_candidate(text: str) -> str | None:
    cleaned = str(text or "").strip()
    if not cleaned or is_editorial_instruction_text(cleaned):
        return None
    return cleaned


def project_public_copy_preferences(
    project: Project | None,
    *,
    guidance: ProjectClarificationGuidance | None = None,
    extra_texts: list[str] | None = None,
) -> dict[str, Any]:
    texts = _guidance_texts(project, guidance=guidance, extra_texts=extra_texts)
    combined = "\n".join(texts)
    lowered = combined.lower()
    include_all_guests = any(phrase in lowered for phrase in ALL_GUEST_HINTS)
    excluded_names: set[str] = set()
    for match in re.finditer(r"\bexclude(?:\s+the)?\s+([A-Za-z0-9._-]+)", combined, flags=re.IGNORECASE):
        token = match.group(1).strip().lower()
        if token:
            excluded_names.add(token)

    banned_phrases: set[str] = set()
    for pattern in PHRASE_CAPTURE_PATTERNS:
        for match in re.finditer(pattern, combined, flags=re.IGNORECASE):
            phrase = str(match.group(1) or "").strip().strip(" .")
            if phrase:
                banned_phrases.add(phrase.lower())

    banned_role_labels: set[str] = set()
    for label in BANNED_ROLE_LABELS:
        if label in lowered and any(token in lowered for token in ("remove", "avoid", "don't", "do not", "strip out")):
            banned_role_labels.add(label)

    strip_fqdn_mentions = any(
        token in lowered
        for token in (
            "remove fqdn",
            "remove the fqdn",
            "talk of fqdn",
            "don't mention fqdn",
            "do not mention fqdn",
            "remove references to fqdn",
        )
    )
    strip_service_unit_lines = any(
        token in lowered
        for token in (
            "remove .service",
            "remove the .service",
            "remove service noise",
            "service noise",
            "service crap",
            "don't mention .service",
            "do not mention .service",
        )
    )

    focus_framing_text: list[str] = []
    for text in texts:
        normalized = _normalize_framing_candidate(text)
        if normalized:
            focus_framing_text.append(normalized)

    return {
        "include_all_guests": include_all_guests,
        "excluded_guest_names": excluded_names,
        "banned_phrases": banned_phrases,
        "banned_role_labels": banned_role_labels,
        "strip_fqdn_mentions": strip_fqdn_mentions,
        "strip_service_unit_lines": strip_service_unit_lines,
        "focus_framing_text": focus_framing_text,
    }


def hypervisor_instruction_preferences(project: Project | None) -> dict[str, Any]:
    preferences = project_public_copy_preferences(project)
    return {
        "include_all_guests": preferences["include_all_guests"],
        "excluded_guest_names": preferences["excluded_guest_names"],
    }
