from __future__ import annotations

from collections import Counter
from typing import Any

from app.signatures.catalog import SIGNATURE_CATALOG
from app.signatures.models import MachineIdentityResolution, SignatureDefinition, SignatureIndicator, SignatureMatchResult


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _lower_set(values: list[Any] | None) -> set[str]:
    return {str(item).strip().lower() for item in (values or []) if str(item).strip()}


def _observed_context(
    *,
    host_name: str,
    metadata: dict[str, Any],
    host_facts: dict[str, Any],
    guest_inventory: list[dict[str, Any]],
    service_inventory: list[dict[str, Any]],
    topology: dict[str, Any] | None,
    user_hints: list[str] | None,
) -> dict[str, Any]:
    child_projects = metadata.get("child_projects") or []
    marker_names = [str(item).strip() for item in (metadata.get("marker_names") or []) if str(item).strip()]
    project_paths = [str(item).strip() for item in (metadata.get("project_paths") or []) if str(item).strip()]
    repo_paths = [str(item).strip() for item in (metadata.get("repo_paths") or []) if str(item).strip()]
    grouped_roots = [str(item).strip() for item in (metadata.get("grouped_roots") or []) if str(item).strip()]
    selected_services = host_facts.get("selected_running_services") or service_inventory or []
    selected_process_names = [str(item).strip() for item in (host_facts.get("selected_process_names") or []) if str(item).strip()]
    package_names = [str(item).strip() for item in (host_facts.get("selected_package_names") or []) if str(item).strip()]
    topology_parent = (topology or {}).get("parent") or {}
    child_names = [str((item or {}).get("name") or "").strip() for item in child_projects if (item or {}).get("name")]
    service_names = [str(item.get("service_name") or "").strip() for item in selected_services if str(item.get("service_name") or "").strip()]
    service_unit_paths = [str(item.get("unit_path") or "").strip() for item in selected_services if str(item.get("unit_path") or "").strip()]
    inventory_observed_names = [
        str(name).strip()
        for name in (metadata.get("inventory_summary") or {}).get("observed_names") or []
        if str(name).strip()
    ]
    hypervisor_like_context = bool(host_facts.get("proxmox_version")) or any(
        item in {"qemu", "lxc"} for item in (
            str(guest.get("guest_type") or "").strip().lower() for guest in guest_inventory
        )
    ) or any(
        "/etc/pve" in path.lower()
        for path in grouped_roots + project_paths + repo_paths
    ) or any("pve" in name.lower() for name in service_names)

    observed_names = [host_name]
    if not hypervisor_like_context:
        observed_names.extend(child_names)
        observed_names.extend(inventory_observed_names)

    path_inventory = set()
    path_inventory.update(grouped_roots)
    path_inventory.update(repo_paths)
    if not hypervisor_like_context:
        path_inventory.update(project_paths)
        path_inventory.update(str(item.get("path") or "").strip() for item in child_projects if str(item.get("path") or "").strip())
    path_inventory.update(str(item.get("config_path") or "").strip() for item in guest_inventory if str(item.get("config_path") or "").strip())
    path_inventory.update(str(item.get("mount_point") or "").strip() for item in (host_facts.get("mounted_filesystems") or []) if str(item.get("mount_point") or "").strip())

    guest_types = [str(item.get("guest_type") or "").strip().lower() for item in guest_inventory if str(item.get("guest_type") or "").strip()]
    stack_markers = marker_names + [str(item).strip() for item in (metadata.get("detected_app_stack_markers") or []) if str(item).strip()]
    return {
        "host_name": host_name,
        "hostname_terms": _lower_set(observed_names),
        "service_names": _lower_set(service_names),
        "service_unit_paths": _lower_set(service_unit_paths),
        "path_inventory": _lower_set(list(path_inventory)),
        "marker_names": _lower_set(stack_markers),
        "package_names": _lower_set(package_names),
        "process_names": _lower_set(selected_process_names),
        "project_paths": _lower_set(
            repo_paths if hypervisor_like_context else project_paths + repo_paths + [str(item.get("path") or "").strip() for item in child_projects if str(item.get("path") or "").strip()]
        ),
        "user_hints": _lower_set(user_hints),
        "guest_types": _lower_set(guest_types),
        "topology_parent_platform": _clean_text(topology_parent.get("platform")).lower(),
        "topology_parent_name": _clean_text(topology_parent.get("name")).lower(),
        "host_facts": host_facts,
        "metadata": metadata,
        "child_project_count": len(child_projects),
        "hypervisor_like_context": hypervisor_like_context,
    }


def _indicator_match(indicator: SignatureIndicator, observed: dict[str, Any]) -> str | None:
    source = indicator.source
    value = indicator.value.lower()
    label = indicator.label or f"{source}:{indicator.value}"
    if source == "fact":
        fact_value = observed["host_facts"].get(indicator.value)
        if fact_value not in (None, "", [], {}):
            return label
        return None
    if source == "hostname":
        for item in observed["hostname_terms"]:
            if value == "*" or value in item:
                return label
        return None
    if source == "service":
        for item in observed["service_names"]:
            if value == "*" or value in item:
                return label
        return None
    if source == "path":
        for item in observed["path_inventory"] | observed["service_unit_paths"]:
            if value == "*" or value in item:
                return label
        return None
    if source == "marker":
        for item in observed["marker_names"]:
            if value == "*" or value in item:
                return label
        return None
    if source == "package":
        for item in observed["package_names"]:
            if value == "*" or value in item:
                return label
        return None
    if source == "process":
        for item in observed["process_names"]:
            if value == "*" or value in item:
                return label
        return None
    if source == "project_paths":
        if observed["project_paths"]:
            return label
        return None
    if source == "child_projects":
        if observed.get("child_project_count", 0):
            return label
        return None
    if source == "guest_type":
        for item in observed["guest_types"]:
            if value == "*" or value == item:
                return label
        return None
    if source == "user_hint":
        for item in observed["user_hints"]:
            if value == "*" or value in item:
                return label
        return None
    if source == "topology_parent_platform":
        if value and value in observed["topology_parent_platform"]:
            return label
        return None
    return None


def _build_stack_tags(signature: SignatureDefinition, observed: dict[str, Any]) -> list[str]:
    tags: list[str] = []
    for tag in signature.detected_stack_tags:
        lowered = tag.lower()
        if tag == "Wazuh":
            if any("wazuh" in item for item in observed["service_names"] | observed["path_inventory"] | observed["hostname_terms"]):
                tags.append(tag)
        elif tag == "Filebeat":
            if any("filebeat" in item for item in observed["service_names"] | observed["path_inventory"]):
                tags.append(tag)
        elif tag == "OpenSearch":
            if any(token in item for token in ("opensearch", "elasticsearch") for item in observed["service_names"] | observed["path_inventory"]):
                tags.append(tag)
        elif tag == "Dashboard":
            if any("dashboard" in item for item in observed["service_names"] | observed["path_inventory"]):
                tags.append(tag)
        elif tag == "Grafana":
            if any("grafana" in item for item in observed["service_names"] | observed["path_inventory"]):
                tags.append(tag)
        else:
            haystacks = (
                observed["service_names"]
                | observed["path_inventory"]
                | observed["service_unit_paths"]
                | observed["marker_names"]
                | observed["hostname_terms"]
                | observed["package_names"]
                | observed["process_names"]
            )
            if any(lowered in item for item in haystacks) or signature.signature_id in {"generic_project_vm", "generic_service_node", "proxmox_hypervisor"}:
                tags.append(tag)
    return tags


def _evaluate_signature(signature: SignatureDefinition, observed: dict[str, Any]) -> SignatureMatchResult | None:
    required_matches = [match for indicator in signature.required_indicators if (match := _indicator_match(indicator, observed))]
    optional_matches = [match for indicator in signature.optional_indicators if (match := _indicator_match(indicator, observed))]
    forbidden_matches = [match for indicator in signature.forbidden_indicators if (match := _indicator_match(indicator, observed))]

    if len(required_matches) < signature.minimum_required_matches:
        return None
    if len(optional_matches) < signature.minimum_optional_matches:
        return None

    confidence = signature.base_confidence
    confidence += len(required_matches) * signature.required_weight
    confidence += len(optional_matches) * signature.optional_weight
    confidence -= len(forbidden_matches) * signature.forbidden_penalty
    confidence = max(0.0, min(0.99, confidence))

    if required_matches or optional_matches:
        evidence = required_matches + optional_matches
    else:
        evidence = [f"Matched generic signature {signature.display_name}."]

    ambiguity_notes: list[str] = []
    if confidence < 0.65:
        ambiguity_notes.append("Signature evidence is partial; human verification recommended.")
    if forbidden_matches:
        ambiguity_notes.append("Some conflicting indicators were also observed.")

    return SignatureMatchResult(
        signature_id=signature.signature_id,
        display_name=signature.display_name,
        category=signature.category,
        machine_kind=signature.machine_kind,
        detected_platform=signature.detected_platform,
        detected_role=signature.detected_role,
        story_template_type=signature.story_template_type,
        project_mapping_hint=signature.project_mapping_hint,
        default_publishability=signature.default_publishability,
        confidence=confidence,
        evidence=evidence,
        required_matches=required_matches,
        optional_matches=optional_matches,
        forbidden_matches=forbidden_matches,
        stack_tags=_build_stack_tags(signature, observed),
        ambiguity_notes=ambiguity_notes,
    )


def _fallback_identity(host_name: str, host_facts: dict[str, Any], local_facts_evidence: list[str], topology_evidence: list[str]) -> MachineIdentityResolution:
    detected_platform = (
        _clean_text(host_facts.get("platform"))
        or _clean_text(host_facts.get("os_pretty_name"))
        or "Linux"
    )
    return MachineIdentityResolution(
        primary_signature_id=None,
        primary_signature_name="Fallback unknown profile",
        machine_kind="unknown",
        detected_platform=detected_platform,
        detected_role=f"{host_name} requires manual identification",
        story_template_type="unknown",
        stack_tags=[],
        identity_confidence=0.32,
        identity_evidence=[],
        alternate_matches=[],
        ambiguity_notes=["No strong deterministic signature matched the locally observed evidence."],
        local_facts_evidence=local_facts_evidence,
        topology_evidence=topology_evidence,
        signature_matches=[],
    )


def resolve_machine_identity(
    *,
    host_name: str,
    metadata: dict[str, Any],
    host_facts: dict[str, Any],
    guest_inventory: list[dict[str, Any]],
    service_inventory: list[dict[str, Any]],
    topology: dict[str, Any] | None = None,
    user_hints: list[str] | None = None,
) -> MachineIdentityResolution:
    local_facts_evidence: list[str] = []
    if host_facts.get("os_pretty_name"):
        local_facts_evidence.append(f"Observed operating system: {host_facts.get('os_pretty_name')}.")
    if host_facts.get("virtualization"):
        local_facts_evidence.append(f"Observed virtualization hint: {host_facts.get('virtualization')}.")
    if service_inventory:
        local_facts_evidence.append(f"Observed {len(service_inventory)} service inventory item(s).")
    if metadata.get("project_paths"):
        local_facts_evidence.append(f"Observed {len(metadata.get('project_paths') or [])} project or repo path(s).")
    topology_evidence: list[str] = []
    parent = (topology or {}).get("parent") or {}
    if parent.get("name"):
        topology_evidence.append(
            f"Topology links this machine to parent host {parent['name']}"
            f"{f' as {str(parent.get('guest_type')).upper()}' if parent.get('guest_type') else ''}."
        )
    guest_counts = (topology or {}).get("guest_counts") or {}
    if guest_counts.get("enrolled") or guest_counts.get("discovered"):
        topology_evidence.append(
            f"Topology sees {guest_counts.get('enrolled', 0)} enrolled guest(s) and {guest_counts.get('discovered', 0)} discovered-only guest(s)."
        )

    observed = _observed_context(
        host_name=host_name,
        metadata=metadata,
        host_facts=host_facts,
        guest_inventory=guest_inventory,
        service_inventory=service_inventory,
        topology=topology,
        user_hints=user_hints,
    )
    matches = [match for signature in SIGNATURE_CATALOG if (match := _evaluate_signature(signature, observed))]
    matches.sort(key=lambda item: (-item.confidence, item.signature_id))

    if not matches:
        return _fallback_identity(host_name, host_facts, local_facts_evidence, topology_evidence)

    primary = matches[0]
    alternate_matches = [
        {
            "signature_id": match.signature_id,
            "display_name": match.display_name,
            "confidence": match.confidence,
            "detected_role": match.detected_role,
            "detected_platform": match.detected_platform,
        }
        for match in matches[1:4]
    ]
    ambiguity_notes = list(primary.ambiguity_notes)
    if alternate_matches:
        close_matches = [item for item in alternate_matches if abs((item["confidence"] or 0) - primary.confidence) <= 0.08]
        if close_matches or len(alternate_matches) >= 2:
            ambiguity_notes.append("Multiple signatures scored closely; review alternate matches.")

    stack_tags = list(dict.fromkeys(primary.stack_tags))
    if not stack_tags:
        tag_counter: Counter[str] = Counter()
        for match in matches[:3]:
            tag_counter.update(match.stack_tags)
        stack_tags = [tag for tag, _count in tag_counter.most_common(6)]

    return MachineIdentityResolution(
        primary_signature_id=primary.signature_id,
        primary_signature_name=primary.display_name,
        machine_kind=primary.machine_kind,
        detected_platform=primary.detected_platform,
        detected_role=primary.detected_role,
        story_template_type=primary.story_template_type,
        stack_tags=stack_tags,
        identity_confidence=primary.confidence,
        identity_evidence=primary.evidence,
        alternate_matches=alternate_matches,
        ambiguity_notes=ambiguity_notes,
        local_facts_evidence=local_facts_evidence,
        topology_evidence=topology_evidence,
        signature_matches=matches,
    )
