from __future__ import annotations

from typing import Any

from app.schemas import MachineClassificationResult
from app.signatures.resolver import resolve_machine_identity


def classify_machine(
    *,
    host_name: str,
    metadata: dict[str, Any],
    host_facts: dict[str, Any],
    guest_inventory: list[dict[str, Any]],
    service_inventory: list[dict[str, Any]],
    topology: dict[str, Any] | None = None,
    user_hints: list[str] | None = None,
) -> MachineClassificationResult:
    identity = resolve_machine_identity(
        host_name=host_name,
        metadata=metadata,
        host_facts=host_facts,
        guest_inventory=guest_inventory,
        service_inventory=service_inventory,
        topology=topology,
        user_hints=user_hints,
    )
    evidence = list(identity.identity_evidence)
    evidence.extend(item for item in identity.local_facts_evidence if item not in evidence)
    evidence.extend(item for item in identity.topology_evidence if item not in evidence)
    return MachineClassificationResult(
        machine_kind=identity.machine_kind,
        detected_platform=identity.detected_platform,
        detected_role=identity.detected_role,
        confidence=identity.identity_confidence,
        evidence=evidence,
        story_template_type=identity.story_template_type,
    )
