from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
    )


class Host(Base, TimestampMixin):
    __tablename__ = "hosts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    address: Mapped[str] = mapped_column(String(255), nullable=False)
    ssh_port: Mapped[int] = mapped_column(Integer, default=22, nullable=False)
    ssh_user: Mapped[str] = mapped_column(String(255), nullable=False)
    ssh_key_path: Mapped[str | None] = mapped_column(String(512))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    connection_status: Mapped[str] = mapped_column(String(64), default="unknown", nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    parent_host_id: Mapped[int | None] = mapped_column(ForeignKey("hosts.id", ondelete="SET NULL"))
    parent_platform: Mapped[str | None] = mapped_column(String(255))
    parent_name: Mapped[str | None] = mapped_column(String(255))
    guest_type: Mapped[str | None] = mapped_column(String(32))
    guest_id: Mapped[str | None] = mapped_column(String(64))
    relationship_source: Mapped[str | None] = mapped_column(String(64))
    relationship_confidence: Mapped[float | None] = mapped_column(Float)
    last_relationship_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    roots: Mapped[list["HostRoot"]] = relationship(
        back_populates="host",
        cascade="all, delete-orphan",
        order_by="HostRoot.root_path",
    )
    projects: Mapped[list["Project"]] = relationship(back_populates="host", cascade="all, delete-orphan")
    scan_events: Mapped[list["ScanEvent"]] = relationship(back_populates="host")
    fact_snapshot: Mapped["HostFact | None"] = relationship(
        back_populates="host",
        cascade="all, delete-orphan",
        uselist=False,
    )
    storage_facts: Mapped[list["HostStorageFact"]] = relationship(
        back_populates="host",
        cascade="all, delete-orphan",
        order_by="HostStorageFact.storage_name",
    )
    guest_inventory: Mapped[list["GuestInventory"]] = relationship(
        back_populates="host",
        cascade="all, delete-orphan",
        order_by="GuestInventory.guest_type, GuestInventory.guest_id",
        foreign_keys="GuestInventory.host_id",
    )
    service_inventory: Mapped[list["ServiceInventory"]] = relationship(
        back_populates="host",
        cascade="all, delete-orphan",
        order_by="ServiceInventory.service_name",
    )
    machine_profiles: Mapped[list["MachineProfile"]] = relationship(back_populates="host")
    parent_host: Mapped["Host | None"] = relationship(
        back_populates="hosted_children",
        remote_side="Host.id",
        foreign_keys=[parent_host_id],
    )
    hosted_children: Mapped[list["Host"]] = relationship(
        back_populates="parent_host",
        foreign_keys=[parent_host_id],
    )
    linked_guest_inventory: Mapped[list["GuestInventory"]] = relationship(
        back_populates="linked_host",
        foreign_keys="GuestInventory.linked_host_id",
    )
    enrichment_runs: Mapped[list["EnrichmentRun"]] = relationship(
        back_populates="host",
        cascade="all, delete-orphan",
        order_by="EnrichmentRun.started_at.desc()",
    )
    enrichment_evidence: Mapped[list["EnrichmentEvidence"]] = relationship(
        back_populates="host",
        cascade="all, delete-orphan",
        order_by="EnrichmentEvidence.created_at.desc()",
    )
    machine_identity_results: Mapped[list["MachineIdentityResult"]] = relationship(
        back_populates="host",
        cascade="all, delete-orphan",
        order_by="MachineIdentityResult.last_resolved_at.desc()",
    )
    project_mapping_previews: Mapped[list["ProjectMappingPreview"]] = relationship(
        back_populates="host",
        cascade="all, delete-orphan",
        order_by="ProjectMappingPreview.last_generated_at.desc()",
    )
    clarification_threads: Mapped[list["ProjectQuestionThread"]] = relationship(
        back_populates="host",
        cascade="all, delete-orphan",
        order_by="ProjectQuestionThread.updated_at.desc()",
    )
    clarification_guidance: Mapped[list["ProjectClarificationGuidance"]] = relationship(
        back_populates="host",
        cascade="all, delete-orphan",
    )
    research_runs: Mapped[list["ResearchRun"]] = relationship(
        back_populates="host",
        cascade="all, delete-orphan",
        order_by="ResearchRun.started_at.desc()",
    )
    research_findings: Mapped[list["ResearchFinding"]] = relationship(
        back_populates="host",
        cascade="all, delete-orphan",
        order_by="ResearchFinding.created_at.desc()",
    )
    profile_override: Mapped["HostProfileOverride | None"] = relationship(
        back_populates="host",
        cascade="all, delete-orphan",
        uselist=False,
    )
    investigation_sessions: Mapped[list["InvestigationSession"]] = relationship(
        back_populates="linked_host",
        cascade="all, delete-orphan",
        order_by="InvestigationSession.updated_at.desc()",
    )


class HostRoot(Base, TimestampMixin):
    __tablename__ = "host_roots"
    __table_args__ = (UniqueConstraint("host_id", "root_path", name="uq_host_root_path"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    host_id: Mapped[int] = mapped_column(ForeignKey("hosts.id", ondelete="CASCADE"), nullable=False)
    root_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    host: Mapped["Host"] = relationship(back_populates="roots")


class Project(Base, TimestampMixin):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("host_id", "path", name="uq_project_host_path"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    host_id: Mapped[int] = mapped_column(ForeignKey("hosts.id", ondelete="CASCADE"), nullable=False)
    path: Mapped[str] = mapped_column(String(2048), nullable=False)
    name_guess: Mapped[str | None] = mapped_column(String(255))
    project_type: Mapped[str | None] = mapped_column(String(128))
    repo_remote: Mapped[str | None] = mapped_column(String(1024))
    repo_branch: Mapped[str | None] = mapped_column(String(255))
    head_commit: Mapped[str | None] = mapped_column(String(255))
    is_git_repo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fingerprint: Mapped[str | None] = mapped_column(String(128))
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    shallow_tree_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    host: Mapped["Host"] = relationship(back_populates="projects")
    markers: Mapped[list["ProjectMarker"]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="ProjectMarker.marker_name",
    )
    interpretations: Mapped[list["ProjectInterpretation"]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="ProjectInterpretation.created_at.desc()",
    )
    files: Mapped[list["ProjectFile"]] = relationship(
        back_populates="project",
        order_by="ProjectFile.relative_path",
    )
    machine_profile: Mapped["MachineProfile | None"] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        uselist=False,
    )
    enrichment_runs: Mapped[list["EnrichmentRun"]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="EnrichmentRun.started_at.desc()",
    )
    enrichment_evidence: Mapped[list["EnrichmentEvidence"]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="EnrichmentEvidence.created_at.desc()",
    )
    identity_result: Mapped["MachineIdentityResult | None"] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        uselist=False,
    )
    mapping_preview: Mapped["ProjectMappingPreview | None"] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        uselist=False,
    )
    clarification_threads: Mapped[list["ProjectQuestionThread"]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="ProjectQuestionThread.updated_at.desc()",
    )
    clarification_guidance: Mapped["ProjectClarificationGuidance | None"] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        uselist=False,
    )
    research_runs: Mapped[list["ResearchRun"]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="ResearchRun.started_at.desc()",
    )
    research_findings: Mapped[list["ResearchFinding"]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="ResearchFinding.created_at.desc()",
    )
    investigation_sessions: Mapped[list["InvestigationSession"]] = relationship(
        back_populates="linked_project",
        cascade="all, delete-orphan",
        order_by="InvestigationSession.updated_at.desc()",
    )


class ProjectMarker(Base):
    __tablename__ = "project_markers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    marker_name: Mapped[str] = mapped_column(String(255), nullable=False)
    marker_path: Mapped[str] = mapped_column(String(2048), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    project: Mapped["Project"] = relationship(back_populates="markers")


class ProjectFile(Base, TimestampMixin):
    __tablename__ = "project_files"
    __table_args__ = (
        UniqueConstraint("host_id", "path", name="uq_project_file_host_path"),
        Index("ix_project_files_project_id", "project_id"),
        Index("ix_project_files_host_id", "host_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"))
    host_id: Mapped[int] = mapped_column(ForeignKey("hosts.id", ondelete="CASCADE"), nullable=False)
    root_path: Mapped[str] = mapped_column(String(2048), nullable=False)
    path: Mapped[str] = mapped_column(String(4096), nullable=False)
    relative_path: Mapped[str] = mapped_column(String(4096), nullable=False)
    entry_type: Mapped[str] = mapped_column(String(16), nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    last_modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fingerprint: Mapped[str | None] = mapped_column(String(128))
    content_sample: Mapped[str | None] = mapped_column(Text)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_ai_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_ai_reviewed_fingerprint: Mapped[str | None] = mapped_column(String(128))
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    project: Mapped["Project"] = relationship(back_populates="files")


class HostFact(Base, TimestampMixin):
    __tablename__ = "host_facts"
    __table_args__ = (UniqueConstraint("host_id", name="uq_host_facts_host_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    host_id: Mapped[int] = mapped_column(ForeignKey("hosts.id", ondelete="CASCADE"), nullable=False)
    machine_kind: Mapped[str | None] = mapped_column(String(64))
    detected_platform: Mapped[str | None] = mapped_column(String(255))
    detected_role: Mapped[str | None] = mapped_column(String(255))
    story_template_type: Mapped[str | None] = mapped_column(String(64))
    confidence: Mapped[float | None] = mapped_column(Float)
    evidence_json: Mapped[list[str] | None] = mapped_column(JSON)
    facts_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    host: Mapped["Host"] = relationship(back_populates="fact_snapshot")


class HostStorageFact(Base, TimestampMixin):
    __tablename__ = "host_storage_facts"
    __table_args__ = (
        UniqueConstraint("host_id", "storage_name", "mount_point", name="uq_host_storage_fact"),
        Index("ix_host_storage_facts_host_id", "host_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    host_id: Mapped[int] = mapped_column(ForeignKey("hosts.id", ondelete="CASCADE"), nullable=False)
    storage_name: Mapped[str] = mapped_column(String(255), nullable=False)
    mount_point: Mapped[str | None] = mapped_column(String(512))
    storage_type: Mapped[str | None] = mapped_column(String(128))
    total_bytes: Mapped[int | None] = mapped_column(Integer)
    used_bytes: Mapped[int | None] = mapped_column(Integer)
    available_bytes: Mapped[int | None] = mapped_column(Integer)
    notes: Mapped[str | None] = mapped_column(Text)
    raw_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    host: Mapped["Host"] = relationship(back_populates="storage_facts")


class GuestInventory(Base, TimestampMixin):
    __tablename__ = "guest_inventory"
    __table_args__ = (
        UniqueConstraint("host_id", "guest_type", "guest_id", name="uq_guest_inventory_host_guest"),
        Index("ix_guest_inventory_host_id", "host_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    host_id: Mapped[int] = mapped_column(ForeignKey("hosts.id", ondelete="CASCADE"), nullable=False)
    guest_id: Mapped[str] = mapped_column(String(64), nullable=False)
    guest_type: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str | None] = mapped_column(String(64))
    cores: Mapped[int | None] = mapped_column(Integer)
    ram_mb: Mapped[int | None] = mapped_column(Integer)
    disk_summary: Mapped[str | None] = mapped_column(String(512))
    role_guess: Mapped[str | None] = mapped_column(String(255))
    ip: Mapped[str | None] = mapped_column(String(255))
    config_path: Mapped[str | None] = mapped_column(String(1024))
    linked_host_id: Mapped[int | None] = mapped_column(ForeignKey("hosts.id", ondelete="SET NULL"))
    management_state: Mapped[str] = mapped_column(String(32), default="discovered", nullable=False)
    relationship_source: Mapped[str | None] = mapped_column(String(64))
    relationship_confidence: Mapped[float | None] = mapped_column(Float)
    last_relationship_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    host: Mapped["Host"] = relationship(back_populates="guest_inventory", foreign_keys=[host_id])
    linked_host: Mapped["Host | None"] = relationship(
        back_populates="linked_guest_inventory",
        foreign_keys=[linked_host_id],
    )
    investigation_sessions: Mapped[list["InvestigationSession"]] = relationship(
        back_populates="linked_guest",
        cascade="all, delete-orphan",
        foreign_keys="InvestigationSession.linked_guest_id",
    )


class ServiceInventory(Base, TimestampMixin):
    __tablename__ = "service_inventory"
    __table_args__ = (
        UniqueConstraint("host_id", "service_name", name="uq_service_inventory_host_service"),
        Index("ix_service_inventory_host_id", "host_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    host_id: Mapped[int] = mapped_column(ForeignKey("hosts.id", ondelete="CASCADE"), nullable=False)
    service_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str | None] = mapped_column(String(64))
    description: Mapped[str | None] = mapped_column(Text)
    role_guess: Mapped[str | None] = mapped_column(String(255))
    unit_path: Mapped[str | None] = mapped_column(String(1024))
    raw_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    host: Mapped["Host"] = relationship(back_populates="service_inventory")


class Scan(Base):
    __tablename__ = "scans"
    __table_args__ = (Index("ix_scans_started_at", "started_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scan_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    hosts_scanned: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    projects_found: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    errors_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)

    events: Mapped[list["ScanEvent"]] = relationship(
        back_populates="scan",
        cascade="all, delete-orphan",
        order_by="ScanEvent.created_at.desc()",
    )


class ScanEvent(Base):
    __tablename__ = "scan_events"
    __table_args__ = (
        Index("ix_scan_events_scan_id", "scan_id"),
        Index("ix_scan_events_host_id", "host_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scan_id: Mapped[int] = mapped_column(ForeignKey("scans.id", ondelete="CASCADE"), nullable=False)
    host_id: Mapped[int | None] = mapped_column(ForeignKey("hosts.id", ondelete="SET NULL"))
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"))
    level: Mapped[str] = mapped_column(String(32), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    details_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    scan: Mapped["Scan"] = relationship(back_populates="events")
    host: Mapped["Host"] = relationship(back_populates="scan_events")


class AppSetting(Base):
    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    value_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
    )


class LMStudioModel(Base):
    __tablename__ = "lmstudio_models"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    model_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class LMStudioInteractionLog(Base):
    __tablename__ = "lmstudio_interaction_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    details_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ProjectInterpretation(Base):
    __tablename__ = "project_interpretations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    model_id: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    response_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    summary: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    project: Mapped["Project"] = relationship(back_populates="interpretations")


class MachineProfile(Base, TimestampMixin):
    __tablename__ = "machine_profiles"
    __table_args__ = (
        UniqueConstraint("project_id", name="uq_machine_profiles_project_id"),
        Index("ix_machine_profiles_host_id", "host_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    host_id: Mapped[int] = mapped_column(ForeignKey("hosts.id", ondelete="CASCADE"), nullable=False)
    machine_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    story_template_type: Mapped[str] = mapped_column(String(64), nullable=False)
    detected_platform: Mapped[str | None] = mapped_column(String(255))
    detected_role: Mapped[str | None] = mapped_column(String(255))
    confidence: Mapped[float | None] = mapped_column(Float)
    lead_paragraph: Mapped[str | None] = mapped_column(Text)
    project_role_summary: Mapped[str | None] = mapped_column(Text)
    host_resources_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    project_role_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    setup_details_json: Mapped[list[str] | None] = mapped_column(JSON)
    contained_workloads_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    detected_components_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    why_it_matters_json: Mapped[list[str] | None] = mapped_column(JSON)
    notes_json: Mapped[list[str] | None] = mapped_column(JSON)
    unknowns_json: Mapped[list[str] | None] = mapped_column(JSON)
    evidence_json: Mapped[list[str] | None] = mapped_column(JSON)
    last_generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    llm_model_used: Mapped[str | None] = mapped_column(String(255))

    project: Mapped["Project"] = relationship(back_populates="machine_profile")
    host: Mapped["Host"] = relationship(back_populates="machine_profiles")


class MachineIdentityResult(Base, TimestampMixin):
    __tablename__ = "machine_identity_results"
    __table_args__ = (
        UniqueConstraint("project_id", name="uq_machine_identity_results_project_id"),
        Index("ix_machine_identity_results_host_id", "host_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    host_id: Mapped[int] = mapped_column(ForeignKey("hosts.id", ondelete="CASCADE"), nullable=False)
    primary_signature_id: Mapped[str | None] = mapped_column(String(128))
    primary_signature_name: Mapped[str | None] = mapped_column(String(255))
    machine_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    story_template_type: Mapped[str] = mapped_column(String(64), nullable=False)
    detected_platform: Mapped[str | None] = mapped_column(String(255))
    detected_role: Mapped[str | None] = mapped_column(String(255))
    stack_tags_json: Mapped[list[str] | None] = mapped_column(JSON)
    identity_confidence: Mapped[float | None] = mapped_column(Float)
    identity_evidence_json: Mapped[list[str] | None] = mapped_column(JSON)
    alternate_matches_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    ambiguity_notes_json: Mapped[list[str] | None] = mapped_column(JSON)
    local_facts_evidence_json: Mapped[list[str] | None] = mapped_column(JSON)
    topology_evidence_json: Mapped[list[str] | None] = mapped_column(JSON)
    last_resolved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    project: Mapped["Project"] = relationship(back_populates="identity_result")
    host: Mapped["Host"] = relationship(back_populates="machine_identity_results")
    signature_matches: Mapped[list["SignatureMatch"]] = relationship(
        back_populates="identity_result",
        cascade="all, delete-orphan",
        order_by="SignatureMatch.rank.asc()",
    )


class SignatureMatch(Base, TimestampMixin):
    __tablename__ = "signature_matches"
    __table_args__ = (
        Index("ix_signature_matches_identity_result_id", "identity_result_id"),
        Index("ix_signature_matches_project_id", "project_id"),
        Index("ix_signature_matches_host_id", "host_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    identity_result_id: Mapped[int] = mapped_column(
        ForeignKey("machine_identity_results.id", ondelete="CASCADE"),
        nullable=False,
    )
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    host_id: Mapped[int] = mapped_column(ForeignKey("hosts.id", ondelete="CASCADE"), nullable=False)
    signature_id: Mapped[str] = mapped_column(String(128), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(128), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float)
    machine_kind: Mapped[str | None] = mapped_column(String(64))
    detected_platform: Mapped[str | None] = mapped_column(String(255))
    detected_role: Mapped[str | None] = mapped_column(String(255))
    project_mapping_hint: Mapped[str | None] = mapped_column(String(255))
    default_publishability: Mapped[str | None] = mapped_column(String(64))
    evidence_json: Mapped[list[str] | None] = mapped_column(JSON)
    required_matches_json: Mapped[list[str] | None] = mapped_column(JSON)
    optional_matches_json: Mapped[list[str] | None] = mapped_column(JSON)
    forbidden_matches_json: Mapped[list[str] | None] = mapped_column(JSON)
    ambiguity_notes_json: Mapped[list[str] | None] = mapped_column(JSON)

    identity_result: Mapped["MachineIdentityResult"] = relationship(back_populates="signature_matches")


class ProjectMappingPreview(Base, TimestampMixin):
    __tablename__ = "project_mapping_previews"
    __table_args__ = (
        UniqueConstraint("project_id", name="uq_project_mapping_previews_project_id"),
        Index("ix_project_mapping_previews_host_id", "host_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    host_id: Mapped[int] = mapped_column(ForeignKey("hosts.id", ondelete="CASCADE"), nullable=False)
    primary_signature_id: Mapped[str | None] = mapped_column(String(128))
    project_title_candidate: Mapped[str | None] = mapped_column(String(255))
    project_summary_candidate: Mapped[str | None] = mapped_column(Text)
    tech_tags_candidate_json: Mapped[list[str] | None] = mapped_column(JSON)
    card_title_candidate: Mapped[str | None] = mapped_column(String(255))
    card_summary_candidate: Mapped[str | None] = mapped_column(Text)
    card_tech_tags_candidate_json: Mapped[list[str] | None] = mapped_column(JSON)
    publish_title_candidate: Mapped[str | None] = mapped_column(String(255))
    publish_summary_candidate: Mapped[str | None] = mapped_column(Text)
    publish_long_description_candidate: Mapped[str | None] = mapped_column(Text)
    publish_sections_json: Mapped[list[str] | None] = mapped_column(JSON)
    publishable_kind: Mapped[str | None] = mapped_column(String(64))
    recommended_status: Mapped[str | None] = mapped_column(String(64))
    demo_url_candidate: Mapped[str | None] = mapped_column(String(1024))
    repo_url_candidate: Mapped[str | None] = mapped_column(String(1024))
    publishability_reason: Mapped[str | None] = mapped_column(Text)
    internal_only_reason: Mapped[str | None] = mapped_column(Text)
    related_hosts_json: Mapped[list[int] | None] = mapped_column(JSON)
    ready_for_cms: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    project: Mapped["Project"] = relationship(back_populates="mapping_preview")
    host: Mapped["Host"] = relationship(back_populates="project_mapping_previews")


class HostProfileOverride(Base, TimestampMixin):
    __tablename__ = "host_profile_overrides"
    __table_args__ = (UniqueConstraint("host_id", name="uq_host_profile_overrides_host_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    host_id: Mapped[int] = mapped_column(ForeignKey("hosts.id", ondelete="CASCADE"), nullable=False)
    project_label_override: Mapped[str | None] = mapped_column(String(255))
    public_title_override: Mapped[str | None] = mapped_column(String(255))
    public_summary_hint: Mapped[str | None] = mapped_column(Text)
    publishable_project: Mapped[bool | None] = mapped_column(Boolean)
    internal_only: Mapped[bool | None] = mapped_column(Boolean)
    related_host_ids_json: Mapped[list[int] | None] = mapped_column(JSON)
    public_tech_tags_override_json: Mapped[list[str] | None] = mapped_column(JSON)
    project_group: Mapped[str | None] = mapped_column(String(255))
    notes_for_cms_export: Mapped[str | None] = mapped_column(Text)

    host: Mapped["Host"] = relationship(back_populates="profile_override")


class ProjectQuestionThread(Base, TimestampMixin):
    __tablename__ = "project_question_threads"
    __table_args__ = (
        Index("ix_project_question_threads_project_id", "project_id"),
        Index("ix_project_question_threads_host_id", "host_id"),
        Index("ix_project_question_threads_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    host_id: Mapped[int | None] = mapped_column(ForeignKey("hosts.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(32), default="open", nullable=False)
    question_type: Mapped[str] = mapped_column(String(64), nullable=False)
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    ai_rationale: Mapped[str | None] = mapped_column(Text)
    my_answer: Mapped[str | None] = mapped_column(Text)
    answer_affects_generation: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    public_safe: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    linked_profile_version: Mapped[str | None] = mapped_column(String(128))
    created_by: Mapped[str] = mapped_column(String(32), default="ai", nullable=False)
    last_answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_in_generation_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    project: Mapped["Project"] = relationship(back_populates="clarification_threads")
    host: Mapped["Host | None"] = relationship(back_populates="clarification_threads")
    messages: Mapped[list["ProjectQuestionMessage"]] = relationship(
        back_populates="thread",
        cascade="all, delete-orphan",
        order_by="ProjectQuestionMessage.created_at.asc()",
    )


class ProjectQuestionMessage(Base, TimestampMixin):
    __tablename__ = "project_question_messages"
    __table_args__ = (
        Index("ix_project_question_messages_thread_id", "thread_id"),
        Index("ix_project_question_messages_project_id", "project_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    thread_id: Mapped[int] = mapped_column(ForeignKey("project_question_threads.id", ondelete="CASCADE"), nullable=False)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    author_role: Mapped[str] = mapped_column(String(32), nullable=False)
    message_text: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(String(32), default="ai", nullable=False)

    thread: Mapped["ProjectQuestionThread"] = relationship(back_populates="messages")


class ProjectClarificationGuidance(Base, TimestampMixin):
    __tablename__ = "project_clarification_guidance"
    __table_args__ = (
        UniqueConstraint("project_id", name="uq_project_clarification_guidance_project_id"),
        Index("ix_project_clarification_guidance_host_id", "host_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    host_id: Mapped[int | None] = mapped_column(ForeignKey("hosts.id", ondelete="CASCADE"))
    preferred_public_title: Mapped[str | None] = mapped_column(String(255))
    preferred_role_wording: Mapped[str | None] = mapped_column(Text)
    standalone_preference: Mapped[str | None] = mapped_column(String(64))
    preferred_project_group: Mapped[str | None] = mapped_column(String(255))
    internal_only: Mapped[bool | None] = mapped_column(Boolean)
    publishable_project: Mapped[bool | None] = mapped_column(Boolean)
    preferred_summary_framing: Mapped[str | None] = mapped_column(Text)
    guidance_notes_json: Mapped[list[str] | None] = mapped_column(JSON)
    source_question_ids_json: Mapped[list[int] | None] = mapped_column(JSON)
    last_applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    project: Mapped["Project"] = relationship(back_populates="clarification_guidance")
    host: Mapped["Host | None"] = relationship(back_populates="clarification_guidance")


class ResearchRun(Base):
    __tablename__ = "research_runs"
    __table_args__ = (
        Index("ix_research_runs_host_id", "host_id"),
        Index("ix_research_runs_project_id", "project_id"),
        Index("ix_research_runs_started_at", "started_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    host_id: Mapped[int | None] = mapped_column(ForeignKey("hosts.id", ondelete="CASCADE"))
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    run_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    trigger_reason: Mapped[str | None] = mapped_column(Text)
    query_summary: Mapped[str | None] = mapped_column(Text)
    result_summary: Mapped[str | None] = mapped_column(Text)
    affected_identity: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    affected_title: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    affected_summary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    host: Mapped["Host | None"] = relationship(back_populates="research_runs")
    project: Mapped["Project | None"] = relationship(back_populates="research_runs")
    sources: Mapped[list["ResearchSource"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="ResearchSource.created_at.asc()",
    )
    findings: Mapped[list["ResearchFinding"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="ResearchFinding.created_at.asc()",
    )


class ResearchSource(Base):
    __tablename__ = "research_sources"
    __table_args__ = (Index("ix_research_sources_run_id", "run_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("research_runs.id", ondelete="CASCADE"), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    tool_name: Mapped[str | None] = mapped_column(String(255))
    query_summary: Mapped[str | None] = mapped_column(Text)
    response_summary: Mapped[str | None] = mapped_column(Text)
    relevance: Mapped[float | None] = mapped_column(Float)
    used_in_generation: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    raw_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    run: Mapped["ResearchRun"] = relationship(back_populates="sources")
    findings: Mapped[list["ResearchFinding"]] = relationship(
        back_populates="source",
        cascade="all, delete-orphan",
        order_by="ResearchFinding.created_at.asc()",
    )


class ResearchFinding(Base):
    __tablename__ = "research_findings"
    __table_args__ = (
        Index("ix_research_findings_run_id", "run_id"),
        Index("ix_research_findings_project_id", "project_id"),
        Index("ix_research_findings_host_id", "host_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("research_runs.id", ondelete="CASCADE"), nullable=False)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("research_sources.id", ondelete="SET NULL"))
    host_id: Mapped[int | None] = mapped_column(ForeignKey("hosts.id", ondelete="CASCADE"))
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    query_used: Mapped[str | None] = mapped_column(Text)
    finding_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    result_summary: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float)
    relevance: Mapped[float | None] = mapped_column(Float)
    affected_identity: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    affected_title: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    affected_summary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    affected_tags: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    used_in_generation: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    evidence_json: Mapped[list[str] | None] = mapped_column(JSON)
    raw_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    run: Mapped["ResearchRun"] = relationship(back_populates="findings")
    source: Mapped["ResearchSource | None"] = relationship(back_populates="findings")
    host: Mapped["Host | None"] = relationship(back_populates="research_findings")
    project: Mapped["Project | None"] = relationship(back_populates="research_findings")


class EnrichmentRun(Base):
    __tablename__ = "enrichment_runs"
    __table_args__ = (
        Index("ix_enrichment_runs_host_id", "host_id"),
        Index("ix_enrichment_runs_project_id", "project_id"),
        Index("ix_enrichment_runs_started_at", "started_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    host_id: Mapped[int | None] = mapped_column(ForeignKey("hosts.id", ondelete="CASCADE"))
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    run_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    query_summary: Mapped[str | None] = mapped_column(Text)
    response_summary: Mapped[str | None] = mapped_column(Text)
    used_in_profile: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    host: Mapped["Host | None"] = relationship(back_populates="enrichment_runs")
    project: Mapped["Project | None"] = relationship(back_populates="enrichment_runs")
    sources: Mapped[list["EnrichmentSource"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="EnrichmentSource.created_at.asc()",
    )
    evidence_items: Mapped[list["EnrichmentEvidence"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="EnrichmentEvidence.created_at.asc()",
    )


class EnrichmentSource(Base):
    __tablename__ = "enrichment_sources"
    __table_args__ = (Index("ix_enrichment_sources_run_id", "run_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("enrichment_runs.id", ondelete="CASCADE"), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    tool_name: Mapped[str | None] = mapped_column(String(255))
    query_summary: Mapped[str | None] = mapped_column(Text)
    response_summary: Mapped[str | None] = mapped_column(Text)
    relevance: Mapped[float | None] = mapped_column(Float)
    used_in_profile: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    raw_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    run: Mapped["EnrichmentRun"] = relationship(back_populates="sources")
    evidence_items: Mapped[list["EnrichmentEvidence"]] = relationship(
        back_populates="source",
        cascade="all, delete-orphan",
        order_by="EnrichmentEvidence.created_at.asc()",
    )


class EnrichmentEvidence(Base):
    __tablename__ = "enrichment_evidence"
    __table_args__ = (
        Index("ix_enrichment_evidence_run_id", "run_id"),
        Index("ix_enrichment_evidence_host_id", "host_id"),
        Index("ix_enrichment_evidence_project_id", "project_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("enrichment_runs.id", ondelete="CASCADE"), nullable=False)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("enrichment_sources.id", ondelete="SET NULL"))
    host_id: Mapped[int | None] = mapped_column(ForeignKey("hosts.id", ondelete="CASCADE"))
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str | None] = mapped_column(String(255))
    evidence_text: Mapped[str] = mapped_column(Text, nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(2048))
    relevance: Mapped[float | None] = mapped_column(Float)
    used_in_profile: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    raw_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    run: Mapped["EnrichmentRun"] = relationship(back_populates="evidence_items")
    source: Mapped["EnrichmentSource | None"] = relationship(back_populates="evidence_items")
    host: Mapped["Host | None"] = relationship(back_populates="enrichment_evidence")
    project: Mapped["Project | None"] = relationship(back_populates="enrichment_evidence")


class InvestigationSession(Base, TimestampMixin):
    __tablename__ = "investigation_sessions"
    __table_args__ = (
        Index("ix_investigation_sessions_status", "status"),
        Index("ix_investigation_sessions_linked_host_id", "linked_host_id"),
        Index("ix_investigation_sessions_linked_project_id", "linked_project_id"),
        Index("ix_investigation_sessions_started_at", "started_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_name: Mapped[str | None] = mapped_column(String(255))
    target_address: Mapped[str | None] = mapped_column(String(255))
    target_hostname: Mapped[str | None] = mapped_column(String(255))
    linked_host_id: Mapped[int | None] = mapped_column(ForeignKey("hosts.id", ondelete="SET NULL"))
    linked_project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"))
    linked_guest_id: Mapped[int | None] = mapped_column(ForeignKey("guest_inventory.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
    current_phase: Mapped[str] = mapped_column(String(64), default="queued", nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    started_by: Mapped[str] = mapped_column(String(64), default="user", nullable=False)
    raw_facts_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    normalized_findings_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    draft_report_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    draft_report_text: Mapped[str | None] = mapped_column(Text)
    proposed_host_changes_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    proposed_project_changes_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    confirmation_state: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    target_config_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    last_error: Mapped[str | None] = mapped_column(Text)

    linked_host: Mapped["Host | None"] = relationship(back_populates="investigation_sessions")
    linked_project: Mapped["Project | None"] = relationship(back_populates="investigation_sessions")
    linked_guest: Mapped["GuestInventory | None"] = relationship(back_populates="investigation_sessions")
    messages: Mapped[list["InvestigationMessage"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="InvestigationMessage.created_at.asc()",
    )
    findings: Mapped[list["InvestigationFinding"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="InvestigationFinding.created_at.asc()",
    )
    drafts: Mapped[list["InvestigationDraft"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="InvestigationDraft.version.asc()",
    )
    proposals: Mapped[list["InvestigationProposal"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="InvestigationProposal.created_at.asc()",
    )


class InvestigationMessage(Base):
    __tablename__ = "investigation_messages"
    __table_args__ = (
        Index("ix_investigation_messages_session_id", "session_id"),
        Index("ix_investigation_messages_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("investigation_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    sender_type: Mapped[str] = mapped_column(String(16), nullable=False)
    message_type: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    affects_generation: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    hidden_internal: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    session: Mapped["InvestigationSession"] = relationship(back_populates="messages")


class InvestigationFinding(Base):
    __tablename__ = "investigation_findings"
    __table_args__ = (
        Index("ix_investigation_findings_session_id", "session_id"),
        Index("ix_investigation_findings_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("investigation_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    finding_type: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    details_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    confidence: Mapped[float | None] = mapped_column(Float)
    source_type: Mapped[str] = mapped_column(String(32), default="local", nullable=False)

    session: Mapped["InvestigationSession"] = relationship(back_populates="findings")


class InvestigationDraft(Base):
    __tablename__ = "investigation_drafts"
    __table_args__ = (
        Index("ix_investigation_drafts_session_id", "session_id"),
        UniqueConstraint("session_id", "version", name="uq_investigation_draft_session_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("investigation_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    draft_type: Mapped[str] = mapped_column(String(32), default="report", nullable=False)
    content_text: Mapped[str | None] = mapped_column(Text)
    content_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    session: Mapped["InvestigationSession"] = relationship(back_populates="drafts")


class InvestigationProposal(Base):
    __tablename__ = "investigation_proposals"
    __table_args__ = (
        Index("ix_investigation_proposals_session_id", "session_id"),
        Index("ix_investigation_proposals_target_kind", "target_kind"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("investigation_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    proposal_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[int | None] = mapped_column(Integer)
    field_name: Mapped[str] = mapped_column(String(128), nullable=False)
    old_value_json: Mapped[Any | None] = mapped_column(JSON)
    new_value_json: Mapped[Any | None] = mapped_column(JSON)
    source: Mapped[str | None] = mapped_column(String(64))
    rationale: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(32), default="proposed", nullable=False)

    session: Mapped["InvestigationSession"] = relationship(back_populates="proposals")
