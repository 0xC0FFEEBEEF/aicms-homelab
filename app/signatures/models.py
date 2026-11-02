from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SignatureIndicator(BaseModel):
    source: str
    value: str
    label: str | None = None


class SignatureDefinition(BaseModel):
    signature_id: str
    display_name: str
    category: str
    machine_kind: str
    detected_platform: str
    detected_role: str
    story_template_type: str
    detected_stack_tags: list[str] = Field(default_factory=list)
    project_mapping_hint: str | None = None
    default_publishability: str = "needs_review"
    required_indicators: list[SignatureIndicator] = Field(default_factory=list)
    optional_indicators: list[SignatureIndicator] = Field(default_factory=list)
    forbidden_indicators: list[SignatureIndicator] = Field(default_factory=list)
    minimum_required_matches: int = 0
    minimum_optional_matches: int = 0
    base_confidence: float = Field(default=0.45, ge=0, le=1)
    required_weight: float = Field(default=0.16, ge=0, le=1)
    optional_weight: float = Field(default=0.07, ge=0, le=1)
    forbidden_penalty: float = Field(default=0.2, ge=0, le=1)


class SignatureMatchResult(BaseModel):
    signature_id: str
    display_name: str
    category: str
    machine_kind: str
    detected_platform: str
    detected_role: str
    story_template_type: str
    project_mapping_hint: str | None = None
    default_publishability: str = "needs_review"
    confidence: float = Field(ge=0, le=1)
    evidence: list[str] = Field(default_factory=list)
    required_matches: list[str] = Field(default_factory=list)
    optional_matches: list[str] = Field(default_factory=list)
    forbidden_matches: list[str] = Field(default_factory=list)
    stack_tags: list[str] = Field(default_factory=list)
    ambiguity_notes: list[str] = Field(default_factory=list)


class MachineIdentityResolution(BaseModel):
    primary_signature_id: str | None = None
    primary_signature_name: str | None = None
    machine_kind: str
    detected_platform: str
    detected_role: str
    story_template_type: str
    stack_tags: list[str] = Field(default_factory=list)
    identity_confidence: float = Field(ge=0, le=1)
    identity_evidence: list[str] = Field(default_factory=list)
    alternate_matches: list[dict[str, Any]] = Field(default_factory=list)
    ambiguity_notes: list[str] = Field(default_factory=list)
    local_facts_evidence: list[str] = Field(default_factory=list)
    topology_evidence: list[str] = Field(default_factory=list)
    signature_matches: list[SignatureMatchResult] = Field(default_factory=list)
