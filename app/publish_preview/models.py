from __future__ import annotations

from pydantic import BaseModel, Field


class PreviewWarning(BaseModel):
    code: str
    label: str
    detail: str
    level: str = "warning"


class PreviewRelatedHost(BaseModel):
    id: int
    name: str
    address: str | None = None


class WebsiteProjectCardViewModel(BaseModel):
    title: str
    summary: str
    tech: list[str] = Field(default_factory=list)
    demo_url: str | None = None
    repo_url: str | None = None
    status: str = "draft"


class WebsitePublishCopyViewModel(BaseModel):
    title: str
    short_summary: str
    long_description: str
    content_blocks: list["PreviewContentBlock"] = Field(default_factory=list)
    tech: list[str] = Field(default_factory=list)
    sections: list[str] = Field(default_factory=list)
    demo_url: str | None = None
    repo_url: str | None = None
    status: str = "draft"
    publish_at: str | None = None


class CMSFieldPreviewRow(BaseModel):
    label: str
    value: str
    missing: bool = False
    multiline: bool = False


class PreviewContentBlock(BaseModel):
    kind: str
    heading: str | None = None
    text: str | None = None
    items: list[str] = Field(default_factory=list)


class PreviewGuestEntry(BaseModel):
    guest_name: str
    guest_type: str
    guest_id: str | None = None
    status: str | None = None
    cpu: str | None = None
    ram: str | None = None
    disk: str | None = None
    role: str | None = None
    linked_host_id: int | None = None
    linked_host_name: str | None = None


class PreviewCustomSection(BaseModel):
    heading: str
    items: list[str] = Field(default_factory=list)


class WebsiteProjectDetailViewModel(BaseModel):
    title: str
    short_summary: str
    long_description: str
    tech: list[str] = Field(default_factory=list)
    demo_url: str | None = None
    repo_url: str | None = None
    role_in_lab: str | None = None
    host_resources: list[str] = Field(default_factory=list)
    setup_summary: list[str] = Field(default_factory=list)
    detail_sections: list[str] = Field(default_factory=list)
    related_hosts: list[PreviewRelatedHost] = Field(default_factory=list)
    hosted_workload_summary: list[str] = Field(default_factory=list)
    hosted_lxcs: list[PreviewGuestEntry] = Field(default_factory=list)
    hosted_vms: list[PreviewGuestEntry] = Field(default_factory=list)
    notes_or_highlights: list[str] = Field(default_factory=list)
    custom_sections: list[PreviewCustomSection] = Field(default_factory=list)
    machine_kind: str | None = None
    detected_role: str | None = None
    detected_platform: str | None = None


class WebsitePreviewModel(BaseModel):
    title: str
    summary: str
    long_description: str
    overview_paragraphs: list[str] = Field(default_factory=list)
    setup_highlights: list[str] = Field(default_factory=list)
    why_it_matters: list[str] = Field(default_factory=list)
    tech_tags: list[str] = Field(default_factory=list)
    demo_url: str | None = None
    repo_url: str | None = None
    status: str = "draft"
    publish_at: str | None = None
    related_hosts: list[PreviewRelatedHost] = Field(default_factory=list)
    machine_kind: str | None = None
    detected_role: str | None = None
    detected_platform: str | None = None
    publishability_status: str = "needs_review"
    warnings: list[PreviewWarning] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    fallback_title_in_use: bool = False
    fallback_summary_in_use: bool = False
    card: WebsiteProjectCardViewModel
    publish: WebsitePublishCopyViewModel
    detail: WebsiteProjectDetailViewModel
    cms_fields: list[CMSFieldPreviewRow] = Field(default_factory=list)
