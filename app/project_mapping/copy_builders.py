from __future__ import annotations

from collections import Counter
import re
from typing import Any

from app.models import (
    Host,
    HostProfileOverride,
    MachineIdentityResult,
    MachineProfile,
    Project,
    ProjectClarificationGuidance,
)
from app.project_mapping.preferences import is_editorial_instruction_text, project_public_copy_preferences
from app.project_mapping.title_rules import research_matches_identity, resolved_public_title
from app.research.models import ResearchOutcome

PUBLIC_COPY_BLOCKLIST = (
    "deterministic identity resolved",
    "human verification recommended",
    "allowlisted root",
    "allowlisted roots",
    "observed file path",
    "observed config path",
    "identity evidence",
    "signature evidence",
    "topology evidence",
    "research evidence",
    "raw inventory",
)

GUIDANCE_INSTRUCTION_PREFIXES = (
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
    "avoid saying ",
    "remove talk like ",
    "remove references to ",
)


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    resolved: list[str] = []
    for item in items:
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        resolved.append(text)
    return resolved


def _looks_like_internal_path_dump(text: str) -> bool:
    if "http://" in text or "https://" in text:
        return False
    return bool(re.search(r"/[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)+", text))


def _is_public_safe_line(value: str) -> bool:
    text = str(value).strip()
    if not text:
        return False
    lowered = text.lower()
    if any(token in lowered for token in PUBLIC_COPY_BLOCKLIST):
        return False
    if lowered.startswith(("observed ", "local fact:", "topology fact:", "identity:", "signature:", "research:")):
        return False
    if "service inventory" in lowered or "component inventory" in lowered:
        return False
    if _looks_like_internal_path_dump(text):
        return False
    return True


def _public_safe_items(items: list[str], *, max_items: int) -> list[str]:
    return _dedupe([text for text in (str(item).strip() for item in items) if _is_public_safe_line(text)])[:max_items]


def _normalize_public_guidance(text: str | None, *, role: bool = False) -> str | None:
    cleaned = str(text or "").strip()
    if not cleaned:
        return None
    lowered = cleaned.lower()
    if any(lowered.startswith(prefix) for prefix in GUIDANCE_INSTRUCTION_PREFIXES):
        return None

    replacements = (
        ("frame it as ", ""),
        ("frame this as ", ""),
        ("describe it as ", ""),
        ("describe this as ", ""),
        ("present it as ", ""),
        ("present this as ", ""),
        ("treat it as ", ""),
        ("treat this as ", ""),
        ("call it ", ""),
        ("call this ", ""),
    )
    for prefix, replacement in replacements:
        if lowered.startswith(prefix):
            cleaned = replacement + cleaned[len(prefix):]
            break

    cleaned = re.sub(r"\bself hosted\b", "self-hosted", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bcloudflare zerotrust\b", "Cloudflare Zero Trust", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bzerotrust\b", "Zero Trust", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.strip(" -")
    if not cleaned:
        return None

    if not role:
        lower_cleaned = cleaned.lower()
        if lower_cleaned.startswith("my "):
            cleaned = f"This is {cleaned}"
        elif lower_cleaned.startswith(("a ", "an ", "the ")):
            cleaned = f"This is {cleaned}"

    cleaned = cleaned[:1].upper() + cleaned[1:]
    if cleaned[-1] not in ".!?":
        cleaned += "."
    return cleaned


def _append_unique_block(target: list[str], value: str | None) -> None:
    text = str(value or "").strip()
    if not text:
        return
    normalized = re.sub(r"\s+", " ", text).strip().lower()
    normalized = re.sub(r"^(this is|it is|it appears to be)\s+", "", normalized)
    for existing in target:
        existing_normalized = re.sub(r"\s+", " ", str(existing).strip()).lower()
        existing_normalized = re.sub(r"^(this is|it is|it appears to be)\s+", "", existing_normalized)
        if existing_normalized == normalized:
            return
    target.append(text)


def _default_tags(
    identity: MachineIdentityResult | None,
    override: HostProfileOverride | None,
    research: ResearchOutcome | None,
) -> list[str]:
    if override and override.public_tech_tags_override_json:
        return _dedupe(list(override.public_tech_tags_override_json))
    tags: list[str] = []
    if research and research.tech_tags and research_matches_identity(identity=identity, research=research):
        tags.extend(research.tech_tags)
    if identity and identity.stack_tags_json:
        tags.extend(identity.stack_tags_json)
    if identity and identity.detected_platform:
        tags.append(identity.detected_platform)
    return _dedupe(tags)


def _sort_guest_key(item: dict[str, Any]) -> tuple[str, int | str]:
    guest_id = str(item.get("guest_id") or "")
    return item.get("guest_type") or "unknown", int(guest_id) if guest_id.isdigit() else guest_id


def _normalized_guest_inventory(host: Host, profile: MachineProfile | None) -> list[dict[str, Any]]:
    workloads = (profile.contained_workloads_json or {}) if profile else {}
    preferred_keys = {
        (str(item.get("guest_type") or "unknown"), str(item.get("guest_id") or "unknown"))
        for item in list(workloads.get("lxcs") or []) + list(workloads.get("vms") or [])
        if item.get("guest_id")
    }
    guests: dict[tuple[str, str], dict[str, Any]] = {}
    for guest in host.guest_inventory or []:
        key = (str(guest.guest_type or "unknown"), str(guest.guest_id or "unknown"))
        if preferred_keys and key not in preferred_keys:
            continue
        guests[key] = {
            "guest_id": guest.guest_id,
            "guest_type": guest.guest_type,
            "name": guest.name,
            "status": guest.status,
            "cores": guest.cores,
            "ram_mb": guest.ram_mb,
            "disk_summary": guest.disk_summary,
            "role_guess": guest.role_guess,
        }

    for item in list(workloads.get("lxcs") or []) + list(workloads.get("vms") or []):
        key = (str(item.get("guest_type") or "unknown"), str(item.get("guest_id") or "unknown"))
        record = guests.setdefault(key, {})
        for field in ("guest_id", "guest_type", "name", "status", "cores", "ram_mb", "disk_summary", "role_guess"):
            value = item.get(field)
            if value not in (None, "", []):
                record[field] = value

    return sorted(guests.values(), key=_sort_guest_key)


def _guest_label(item: dict[str, Any]) -> str:
    return str(item.get("name") or f"{item.get('guest_type', 'guest')} {item.get('guest_id') or ''}").strip()


def _guest_bullet(item: dict[str, Any]) -> str:
    label = _guest_label(item)
    role = str(item.get("role_guess") or "role still needs review").strip()
    cpu = item.get("cores")
    ram_mb = item.get("ram_mb")
    disk = item.get("disk_summary")
    parts = [f"{label} — {role}"]
    resources: list[str] = []
    if cpu:
        resources.append(f"{cpu} core(s)")
    if ram_mb:
        resources.append(f"{round(float(ram_mb) / 1024, 1)} GiB RAM")
    if disk:
        resources.append(str(disk))
    if resources:
        parts.append(f"({', '.join(resources)})")
    return " ".join(parts)


def _host_resources_lines(profile: MachineProfile | None) -> list[str]:
    resources = (profile.host_resources_json or {}) if profile else {}
    lines: list[str] = []
    cpu = resources.get("cpu")
    ram = resources.get("memory")
    if cpu:
        lines.append(f"CPU: {cpu}")
    if ram:
        lines.append(f"RAM: {ram}")
    for item in resources.get("storage") or []:
        if isinstance(item, dict):
            mount = str(item.get("mount_point") or item.get("storage_name") or "storage").strip()
            storage_type = str(item.get("storage_type") or "").strip()
            pieces: list[str] = []
            if item.get("total_gib") is not None:
                pieces.append(f"{item.get('total_gib')} GiB total")
            if item.get("used_gib") is not None:
                pieces.append(f"{item.get('used_gib')} GiB used")
            if item.get("available_gib") is not None:
                pieces.append(f"{item.get('available_gib')} GiB available")
            line = mount
            if storage_type:
                line += f" ({storage_type})"
            if pieces:
                line += " — " + ", ".join(pieces)
            lines.append(line)
        else:
            lines.append(str(item))
    os_value = resources.get("operating_system")
    if os_value:
        lines.append(f"OS: {os_value}")
    return lines


def _top_component_names(profile: MachineProfile | None, *, max_items: int = 4) -> list[str]:
    components = profile.detected_components_json if profile else []
    labels: list[str] = []
    for item in components or []:
        label = str(item.get("name") or item.get("service") or item.get("label") or "").strip()
        if label:
            labels.append(label)
        if len(labels) >= max_items:
            break
    return _dedupe(labels)


def _stack_summary(identity: MachineIdentityResult | None, component_names: list[str], *, max_items: int = 6) -> list[str]:
    values: list[str] = []
    if identity and identity.stack_tags_json:
        values.extend(identity.stack_tags_json)
    values.extend(component_names[:max_items])
    return _dedupe(values)[:max_items]


def _role_sentence(
    *,
    identity: MachineIdentityResult | None,
    signature_id: str | None,
    component_names: list[str],
    guidance: ProjectClarificationGuidance | None,
    research: ResearchOutcome | None,
) -> str | None:
    if guidance and guidance.preferred_role_wording:
        normalized = _normalize_public_guidance(guidance.preferred_role_wording, role=True)
        if normalized:
            return normalized
    observed = ", ".join(component_names[:4]) if component_names else None
    if signature_id == "wazuh_stack":
        if observed:
            return f"It appears to host the main security monitoring pieces in my environment, including {observed}."
        return "It appears to host the core security monitoring and SIEM-related pieces in my environment."
    if signature_id == "mcp_gateway":
        return "It acts as the control layer between my local AI stack and the tools or integrations I want exposed in a more structured way."
    if signature_id == "golem_provider":
        if observed:
            return f"It appears to contain the main provider-side runtime pieces for my golem setup, including {observed}."
        return "It appears to be the main provider-side workload VM for my golem setup."
    if signature_id == "grafana":
        return "It appears to provide dashboards and observability views for parts of my lab."
    if research and research.long_description_hint and research_matches_identity(identity=identity, research=research):
        return research.long_description_hint
    return None


def _append_group(target: list[str], heading: str, items: list[str]) -> None:
    cleaned = [str(item).strip() for item in items if str(item).strip()]
    if not cleaned:
        return
    target.append(f"{heading}:")
    target.extend(cleaned)


def _service_unit_heavy(text: str) -> bool:
    return text.count(".service") >= 1


def _matches_banned_phrase(text: str, banned_phrases: set[str]) -> bool:
    lowered = text.lower()
    return any(phrase and phrase in lowered for phrase in banned_phrases)


def _is_filtered_public_block(text: str, preferences: dict[str, Any]) -> bool:
    cleaned = str(text or "").strip()
    if not cleaned:
        return True
    if is_editorial_instruction_text(cleaned):
        return True
    lowered = cleaned.lower()
    banned_phrases = set(str(item).strip().lower() for item in preferences.get("banned_phrases", set()) if str(item).strip())
    banned_role_labels = set(str(item).strip().lower() for item in preferences.get("banned_role_labels", set()) if str(item).strip())
    if _matches_banned_phrase(lowered, banned_phrases):
        return True
    if any(label in lowered for label in banned_role_labels):
        return True
    if preferences.get("strip_fqdn_mentions") and "fqdn" in lowered:
        return True
    if preferences.get("strip_service_unit_lines") and _service_unit_heavy(cleaned):
        return True
    return False


def sanitize_public_copy_text(text: str | None, preferences: dict[str, Any]) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    blocks = [item.strip() for item in re.split(r"\n\s*\n", raw) if item.strip()]
    cleaned_blocks: list[str] = []
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        if lines[0].endswith(":"):
            heading = lines[0]
            bullet_lines = [line for line in lines[1:] if not _is_filtered_public_block(line, preferences)]
            if bullet_lines:
                cleaned_blocks.append("\n".join([heading, *bullet_lines]))
            continue
        candidate = " ".join(lines)
        if _is_filtered_public_block(candidate, preferences):
            continue
        cleaned_blocks.append(candidate)
    return _compose_long_description(cleaned_blocks, [])


def _compose_long_description(paragraphs: list[str], sections: list[str]) -> str:
    blocks = [str(item).strip() for item in paragraphs if str(item).strip()]
    index = 0
    while index < len(sections):
        item = str(sections[index]).strip()
        if not item:
            index += 1
            continue
        if item.endswith(":"):
            heading = item
            index += 1
            bullets: list[str] = []
            while index < len(sections):
                candidate = str(sections[index]).strip()
                if not candidate:
                    index += 1
                    continue
                if candidate.endswith(":"):
                    break
                bullets.append(f"- {candidate}")
                index += 1
            if bullets:
                blocks.append(f"{heading}\n" + "\n".join(bullets))
            else:
                blocks.append(heading)
            continue
        blocks.append(f"- {item}")
        index += 1
    return "\n\n".join(blocks).strip()


def _clean_public_blocks(
    paragraphs: list[str],
    sections: list[str],
    preferences: dict[str, Any],
) -> tuple[list[str], list[str]]:
    cleaned_paragraphs: list[str] = []
    for paragraph in paragraphs:
        cleaned = sanitize_public_copy_text(paragraph, preferences)
        if cleaned:
            _append_unique_block(cleaned_paragraphs, cleaned)

    cleaned_sections: list[str] = []
    index = 0
    while index < len(sections):
        item = str(sections[index]).strip()
        if not item:
            index += 1
            continue
        if item.endswith(":"):
            heading = item
            index += 1
            bucket: list[str] = []
            while index < len(sections):
                candidate = str(sections[index]).strip()
                if not candidate:
                    index += 1
                    continue
                if candidate.endswith(":"):
                    break
                if not _is_filtered_public_block(candidate, preferences):
                    bucket.append(candidate)
                index += 1
            if bucket:
                cleaned_sections.append(heading)
                cleaned_sections.extend(_dedupe(bucket))
            continue
        if not _is_filtered_public_block(item, preferences):
            cleaned_sections.append(item)
        index += 1
    return cleaned_paragraphs, cleaned_sections


def build_public_copy(
    *,
    host: Host,
    project: Project,
    identity: MachineIdentityResult | None,
    profile: MachineProfile | None,
    override: HostProfileOverride | None,
    guidance: ProjectClarificationGuidance | None,
    research: ResearchOutcome | None,
) -> dict[str, Any]:
    title = resolved_public_title(
        host=host,
        project=project,
        identity=identity,
        override=override,
        guidance=guidance,
        research=research,
    )
    signature_id = identity.primary_signature_id if identity else None
    public_preferences = project_public_copy_preferences(project, guidance=guidance)
    tags = [
        tag
        for tag in _default_tags(identity, override, research)
        if not _is_filtered_public_block(tag, public_preferences)
    ]
    excluded_guest_names = public_preferences["excluded_guest_names"]
    include_all_guests = bool(public_preferences["include_all_guests"])
    guest_inventory = _normalized_guest_inventory(host, profile)
    presented_guest_inventory = [item for item in guest_inventory if _guest_label(item).lower() not in excluded_guest_names]
    vm_count = sum(1 for item in guest_inventory if item.get("guest_type") == "qemu")
    lxc_count = sum(1 for item in guest_inventory if item.get("guest_type") == "lxc")
    presented_vm_inventory = [item for item in presented_guest_inventory if item.get("guest_type") == "qemu"]
    presented_lxc_inventory = [item for item in presented_guest_inventory if item.get("guest_type") == "lxc"]
    component_names = _top_component_names(profile)
    stack_summary = _stack_summary(identity, component_names)
    host_resources = _host_resources_lines(profile)
    project_role = (profile.project_role_summary or "").strip() if profile else ""
    guidance_summary = _normalize_public_guidance(
        guidance.preferred_summary_framing if guidance else None,
        role=False,
    )
    guidance_role = _normalize_public_guidance(
        guidance.preferred_role_wording if guidance else None,
        role=True,
    )

    if override and override.public_summary_hint:
        card_summary = override.public_summary_hint
        publish_summary = override.public_summary_hint
    elif guidance_summary:
        card_summary = guidance_summary
        publish_summary = guidance_summary
    elif research and research.summary_hint and research.affected_summary and research_matches_identity(identity=identity, research=research):
        card_summary = research.summary_hint
        publish_summary = research.summary_hint
    elif signature_id == "proxmox_hypervisor":
        card_summary = (
            f"My main Proxmox hypervisor for lab infrastructure and project workloads. "
            f"It currently hosts {vm_count} VM(s) and {lxc_count} LXC container(s)."
        )
        publish_summary = "This is my main Proxmox hypervisor for lab infrastructure and project workloads."
    elif signature_id == "wazuh_stack":
        card_summary = "My Wazuh monitoring and security VM for SIEM, visibility, and security operations in my environment."
        publish_summary = "This VM is for my Wazuh monitoring and security stack."
    elif signature_id == "mcp_gateway":
        card_summary = "My control-layer VM for local AI tooling and structured tool access."
        publish_summary = "This VM is the control layer for my AI environment."
    elif signature_id == "golem_provider":
        card_summary = "My golem workload VM for provider-side compute services and supporting runtime components."
        publish_summary = "This VM is for my golem project."
    elif signature_id == "grafana":
        card_summary = "My Grafana observability VM for dashboards and monitoring views in the lab."
        publish_summary = "This VM appears to host Grafana-oriented observability services."
    elif signature_id == "generic_project_vm":
        card_summary = (
            f"This VM appears to host a project workload in my lab, but the exact project identity still needs review."
        )
        publish_summary = card_summary
    elif signature_id == "generic_service_node":
        card_summary = "This machine appears to run a service workload in my lab, but the exact role still needs review."
        publish_summary = card_summary
    else:
        role = (research.detected_role_hint if research and research.detected_role_hint else (identity.detected_role if identity else None)) or "unresolved workload"
        card_summary = f"This machine appears to be part of my lab as a {role}, but it still needs review before I describe it more specifically."
        publish_summary = card_summary

    setup_items = _public_safe_items(list((profile.setup_details_json or []) if profile else []), max_items=6)
    why_items = _public_safe_items(list((profile.why_it_matters_json or []) if profile else []), max_items=4)
    notes_items = _public_safe_items(list((profile.notes_json or []) if profile else []), max_items=4)

    long_blocks: list[str] = []
    _append_unique_block(long_blocks, publish_summary)
    publish_sections: list[str] = []
    role_sentence = _role_sentence(
        identity=identity,
        signature_id=signature_id,
        component_names=component_names,
        guidance=guidance,
        research=research,
    )

    if signature_id == "proxmox_hypervisor":
        guest_roles = Counter(
            str(item.get("role_guess") or "").strip()
            for item in presented_guest_inventory
            if str(item.get("role_guess") or "").strip()
        )
        role_categories = ", ".join(role for role, _count in guest_roles.most_common(5))
        long_blocks.append(
            f"It currently acts as a shared virtualization node for my environment, with {vm_count} VM(s) and {lxc_count} LXC container(s) visible in the inventory."
        )
        if guidance_role:
            _append_unique_block(long_blocks, guidance_role)
        elif role_categories:
            _append_unique_block(
                long_blocks,
                f"The hosted workload mix currently leans toward {role_categories}, which makes this machine one of the main infrastructure anchors in my lab."
            )
        elif project_role:
            _append_unique_block(long_blocks, project_role)
        if host_resources:
            _append_group(publish_sections, "Resources and highlights", host_resources[:5])
        workload_summary_items = [
            f"{vm_count} VM(s) and {lxc_count} LXC container(s) are currently tracked on this hypervisor.",
        ]
        if role_categories:
            workload_summary_items.append(f"The current workload mix includes {role_categories}.")
        _append_group(publish_sections, "Hosted workload summary", workload_summary_items)
        if include_all_guests:
            if presented_lxc_inventory:
                _append_group(
                    publish_sections,
                    "Hosted LXCs",
                    [_guest_bullet(item) for item in presented_lxc_inventory],
                )
            if presented_vm_inventory:
                _append_group(
                    publish_sections,
                    "Hosted VMs",
                    [_guest_bullet(item) for item in presented_vm_inventory],
                )
        elif presented_guest_inventory:
            _append_group(
                publish_sections,
                "Featured guests",
                [_guest_bullet(item) for item in presented_guest_inventory[:8]],
            )
        if setup_items:
            _append_group(publish_sections, "How it is set up", setup_items[:6])
    else:
        if role_sentence:
            _append_unique_block(long_blocks, role_sentence)
        if project_role:
            _append_unique_block(long_blocks, project_role)
        if stack_summary:
            _append_unique_block(long_blocks, "The main stack indicators currently point to " + ", ".join(stack_summary[:6]) + ".")
        if guidance_summary:
            _append_unique_block(long_blocks, guidance_summary)
        if host_resources:
            _append_group(publish_sections, "Resources and highlights", host_resources[:4])
        if setup_items:
            _append_group(publish_sections, "How it is set up", setup_items[:6])
        if component_names:
            _append_group(
                publish_sections,
                "Key components",
                [", ".join(component_names[:6])],
            )
        if stack_summary:
            _append_group(
                publish_sections,
                "Tech and stack",
                [", ".join(stack_summary[:6])],
            )
        if (
            research
            and research.long_description_hint
            and research_matches_identity(identity=identity, research=research)
        ):
            _append_unique_block(long_blocks, research.long_description_hint)

    if why_items:
        _append_group(publish_sections, "Why it matters in my lab", why_items[:5])
    if notes_items:
        _append_group(publish_sections, "Notes / unknowns", notes_items[:4])

    long_blocks, publish_sections = _clean_public_blocks(long_blocks, publish_sections, public_preferences)
    long_description = _compose_long_description(long_blocks, publish_sections)
    summary_fallback = next((str(block).strip() for block in long_blocks if str(block).strip()), "")
    if not summary_fallback and long_description:
        summary_fallback = long_description.split("\n\n", 1)[0].strip()
    publish_summary = sanitize_public_copy_text(publish_summary, public_preferences)
    if not publish_summary:
        publish_summary = summary_fallback
    card_summary = sanitize_public_copy_text(card_summary, public_preferences)
    if not card_summary:
        card_summary = publish_summary

    return {
        "title": title,
        "card_title": title,
        "card_summary": card_summary,
        "card_tags": tags,
        "publish_title": title,
        "publish_summary": publish_summary,
        "publish_long_description": long_description.strip(),
        "publish_sections": publish_sections,
        "tech_tags": tags,
    }
