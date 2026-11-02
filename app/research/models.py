from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ResearchFindingPayload(BaseModel):
    source_type: str
    finding_kind: str
    result_summary: str
    confidence: float = 0.0
    relevance: float = 0.0
    evidence: list[str] = Field(default_factory=list)
    query_used: str | None = None
    title_hint: str | None = None
    summary_hint: str | None = None
    long_description_hint: str | None = None
    detected_platform_hint: str | None = None
    detected_role_hint: str | None = None
    tech_tags: list[str] = Field(default_factory=list)
    affected_identity: bool = False
    affected_title: bool = False
    affected_summary: bool = False
    affected_tags: bool = False
    used_in_generation: bool = False
    raw_payload: dict[str, Any] | None = None


class ResearchOutcome(BaseModel):
    triggered: bool = False
    reason: str | None = None
    findings: list[ResearchFindingPayload] = Field(default_factory=list)
    title_hint: str | None = None
    summary_hint: str | None = None
    long_description_hint: str | None = None
    detected_platform_hint: str | None = None
    detected_role_hint: str | None = None
    tech_tags: list[str] = Field(default_factory=list)
    affected_identity: bool = False
    affected_title: bool = False
    affected_summary: bool = False
    mcp_enrichment: dict[str, Any] | None = None
    web_search_enrichment: dict[str, Any] | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "triggered": self.triggered,
            "reason": self.reason,
            "title_hint": self.title_hint,
            "summary_hint": self.summary_hint,
            "long_description_hint": self.long_description_hint,
            "detected_platform_hint": self.detected_platform_hint,
            "detected_role_hint": self.detected_role_hint,
            "tech_tags": self.tech_tags,
            "affected_identity": self.affected_identity,
            "affected_title": self.affected_title,
            "affected_summary": self.affected_summary,
            "findings": [item.model_dump() for item in self.findings],
        }
