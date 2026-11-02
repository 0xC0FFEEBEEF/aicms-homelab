from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator


def _strip_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


class HostProfileOverridePayload(BaseModel):
    project_label_override: str | None = None
    public_title_override: str | None = None
    public_summary_hint: str | None = None
    publishable_project: bool | None = None
    internal_only: bool | None = None
    related_host_ids: list[int] = Field(default_factory=list)
    public_tech_tags_override: list[str] = Field(default_factory=list)
    project_group: str | None = None
    notes_for_cms_export: str | None = None

    @field_validator(
        "project_label_override",
        "public_title_override",
        "public_summary_hint",
        "project_group",
        "notes_for_cms_export",
        mode="before",
    )
    @classmethod
    def _optional_text(cls, value: Any) -> str | None:
        return _strip_or_none(value)

    @field_validator("related_host_ids", mode="before")
    @classmethod
    def _related_host_ids(cls, value: Any) -> list[int]:
        if value in {None, ""}:
            return []
        if isinstance(value, list):
            items = value
        else:
            items = str(value).replace(",", "\n").splitlines()
        resolved: list[int] = []
        seen: set[int] = set()
        for item in items:
            try:
                host_id = int(str(item).strip())
            except (TypeError, ValueError):
                continue
            if host_id in seen:
                continue
            seen.add(host_id)
            resolved.append(host_id)
        return resolved

    @field_validator("public_tech_tags_override", mode="before")
    @classmethod
    def _tech_tags(cls, value: Any) -> list[str]:
        if value in {None, ""}:
            return []
        if isinstance(value, list):
            items = value
        else:
            items = str(value).replace(",", "\n").splitlines()
        resolved: list[str] = []
        seen: set[str] = set()
        for item in items:
            tag = str(item).strip()
            if not tag or tag in seen:
                continue
            seen.add(tag)
            resolved.append(tag)
        return resolved

    @field_validator("internal_only")
    @classmethod
    def _internal_only_conflict(cls, value: bool | None, info) -> bool | None:
        if value and info.data.get("publishable_project") is True:
            raise ValueError("A machine cannot be marked both publishable and internal-only.")
        return value

    @field_validator("publishable_project")
    @classmethod
    def _publishable_conflict(cls, value: bool | None, info) -> bool | None:
        if value and info.data.get("internal_only") is True:
            raise ValueError("A machine cannot be marked both publishable and internal-only.")
        return value


class ProjectMappingPreviewPayload(BaseModel):
    project_title_candidate: str
    project_summary_candidate: str
    tech_tags_candidate: list[str] = Field(default_factory=list)
    card_title_candidate: str
    card_summary_candidate: str
    card_tech_tags_candidate: list[str] = Field(default_factory=list)
    publish_title_candidate: str
    publish_summary_candidate: str
    publish_long_description_candidate: str
    publish_sections: list[str] = Field(default_factory=list)
    publishable_kind: str
    recommended_status: str
    demo_url_candidate: str | None = None
    repo_url_candidate: str | None = None
    publishability_reason: str
    internal_only_reason: str | None = None
    ready_for_cms: bool = False
    needs_review: bool = True
