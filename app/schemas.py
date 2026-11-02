from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.config import DEFAULT_LMSTUDIO_BASE_URL
from app.utils.ssh_keys import normalize_private_key_path

GENERAL_SETTINGS_KEY = "general"
SCAN_SETTINGS_KEY = "scan"
LMSTUDIO_SETTINGS_KEY = "lmstudio"
GITHUB_SETTINGS_KEY = "github"
PROFILE_SETTINGS_KEY = "profiles"
MCP_SETTINGS_KEY = "mcp"
WEB_SEARCH_SETTINGS_KEY = "web_search"


def _strip_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


class HostUpsert(BaseModel):
    name: str
    address: str
    ssh_port: int = Field(default=22, ge=1, le=65535)
    ssh_user: str
    ssh_key_path: str | None = None
    enabled: bool = True
    roots: list[str]

    @field_validator("name", "address", "ssh_user", mode="before")
    @classmethod
    def _required_text(cls, value: Any) -> str:
        text = _strip_or_none(value)
        if not text:
            raise ValueError("This field is required.")
        return text

    @field_validator("ssh_key_path", mode="before")
    @classmethod
    def _optional_text(cls, value: Any) -> str | None:
        return normalize_private_key_path(_strip_or_none(value))

    @field_validator("roots", mode="before")
    @classmethod
    def _normalize_roots(cls, value: Any) -> list[str]:
        if isinstance(value, str):
            items = [line.strip() for line in value.splitlines()]
        elif isinstance(value, list):
            items = [str(item).strip() for item in value]
        else:
            items = []

        roots: list[str] = []
        seen: set[str] = set()
        for item in items:
            if not item or item in seen:
                continue
            if "<" in item and ">" in item:
                raise ValueError("Replace placeholder roots like <your-node> with the real path for this host.")
            if not item.startswith("/"):
                raise ValueError("Allowlisted roots must be absolute paths.")
            seen.add(item)
            roots.append(item)
        if not roots:
            raise ValueError("At least one allowlisted root is required.")
        return roots


class RootCheckResult(BaseModel):
    root_path: str
    exists: bool
    message: str


class ConnectionTestResult(BaseModel):
    success: bool
    message: str
    root_results: list[RootCheckResult] = Field(default_factory=list)


class GeneralSettingsPayload(BaseModel):
    app_name: str = "AICMS"
    timezone: str = "US/Central"
    default_max_tree_depth: int = Field(default=2, ge=1, le=8)
    default_scanner_ssh_key_path: str = "~/.ssh/aicms_scanner_ed25519"

    @field_validator("app_name", "timezone", mode="before")
    @classmethod
    def _required_text(cls, value: Any) -> str:
        text = _strip_or_none(value)
        if not text:
            raise ValueError("This field is required.")
        return text

    @field_validator("default_scanner_ssh_key_path", mode="before")
    @classmethod
    def _default_key_path(cls, value: Any) -> str:
        normalized = normalize_private_key_path(_strip_or_none(value))
        if not normalized:
            return "~/.ssh/aicms_scanner_ed25519"
        return normalized


class ScanSettingsPayload(BaseModel):
    ignore_patterns: list[str]
    allowed_marker_files: list[str]
    max_read_bytes: int = Field(default=4096, ge=256, le=1048576)
    collect_shallow_file_trees: bool = True
    collect_recent_git_metadata: bool = True

    @field_validator("ignore_patterns", "allowed_marker_files", mode="before")
    @classmethod
    def _normalize_list(cls, value: Any) -> list[str]:
        if isinstance(value, str):
            items = value.splitlines()
        elif isinstance(value, list):
            items = value
        else:
            items = []

        values = [str(item).strip() for item in items if str(item).strip()]
        if not values:
            raise ValueError("At least one value is required.")
        return values


class LMStudioSettingsPayload(BaseModel):
    enabled: bool = False
    base_url: str = DEFAULT_LMSTUDIO_BASE_URL
    api_token: str | None = None
    selected_model: str | None = None
    temperature: float = Field(default=0.2, ge=0, le=2)
    use_max_output_tokens: bool = True
    max_tokens: int = Field(default=500, ge=64, le=8192)
    use_custom_timeout: bool = False
    request_timeout_seconds: int = Field(default=60, ge=5, le=3600)
    last_successful_contact: datetime | None = None
    last_error: str | None = None
    last_refresh_time: datetime | None = None

    @field_validator("base_url", mode="before")
    @classmethod
    def _validate_base_url(cls, value: Any) -> str:
        text = _strip_or_none(value)
        if not text:
            raise ValueError("Base URL is required.")
        return text.rstrip("/")

    @field_validator("request_timeout_seconds", mode="before")
    @classmethod
    def _timeout_value(cls, value: Any) -> int:
        if value in {None, ""}:
            return 60
        return int(value)

    @field_validator("api_token", "selected_model", "last_error", mode="before")
    @classmethod
    def _optional_text(cls, value: Any) -> str | None:
        return _strip_or_none(value)


class GitHubSettingsPayload(BaseModel):
    repo_owner: str | None = None
    repo_name: str | None = None
    branch: str = "main"
    pat: str | None = None
    last_checked_at: datetime | None = None
    local_revision: str | None = None
    remote_revision: str | None = None
    update_available: bool = False
    last_error: str | None = None

    @field_validator("repo_owner", "repo_name", "pat", "last_error", "local_revision", "remote_revision", mode="before")
    @classmethod
    def _optional_text(cls, value: Any) -> str | None:
        return _strip_or_none(value)

    @field_validator("branch", mode="before")
    @classmethod
    def _branch_text(cls, value: Any) -> str:
        text = _strip_or_none(value)
        return text or "main"


class ProfileSettingsPayload(BaseModel):
    enabled: bool = True
    enable_lmstudio_formatting: bool = True
    fallback_to_deterministic_profile: bool = True
    max_guests_shown: int = Field(default=24, ge=1, le=200)
    max_services_shown: int = Field(default=20, ge=1, le=200)
    max_setup_bullets_shown: int = Field(default=10, ge=1, le=40)


class MCPSettingsPayload(BaseModel):
    enabled: bool = False
    server_url: str | None = None
    api_token: str | None = None
    allowed_tools: list[str] = Field(default_factory=list)
    read_only_search_only: bool = True
    auto_enrich_after_scan: bool = False
    max_searches_per_profile_refresh: int = Field(default=3, ge=1, le=20)
    timeout_seconds: int = Field(default=15, ge=1, le=300)
    max_results_items: int = Field(default=10, ge=1, le=100)
    last_successful_contact: datetime | None = None
    last_error: str | None = None

    @field_validator("server_url", "api_token", "last_error", mode="before")
    @classmethod
    def _optional_text(cls, value: Any) -> str | None:
        text = _strip_or_none(value)
        return text.rstrip("/") if text and text.startswith(("http://", "https://")) else text

    @field_validator("allowed_tools", mode="before")
    @classmethod
    def _allowed_tools(cls, value: Any) -> list[str]:
        if isinstance(value, str):
            items = value.replace(",", "\n").splitlines()
        elif isinstance(value, list):
            items = value
        else:
            items = []
        cleaned: list[str] = []
        seen: set[str] = set()
        for item in items:
            text = str(item).strip()
            if not text or text in seen:
                continue
            cleaned.append(text)
            seen.add(text)
        return cleaned

    @field_validator("server_url")
    @classmethod
    def _server_required_when_enabled(cls, value: str | None, info) -> str | None:
        if info.data.get("enabled") and not value:
            raise ValueError("MCP server URL is required when MCP integration is enabled.")
        return value


class WebSearchSettingsPayload(BaseModel):
    enabled: bool = False
    engine: str = "searxng"
    query_url_template: str = "http://10.20.2.8:8080/search?q=<query>"
    language: str = "all"
    result_count: int = Field(default=10, ge=1, le=50)
    concurrent_requests: int = Field(default=10, ge=1, le=50)
    domain_filters: list[str] = Field(default_factory=list)
    bypass_embedding_and_retrieval: bool = False
    bypass_web_loader: bool = False
    trust_proxy_environment: bool = False
    loader_engine: str = "default"
    timeout: int = Field(default=15, ge=1, le=120)
    verify_ssl_certificate: bool = True
    loader_concurrent_requests: int = Field(default=10, ge=1, le=50)
    youtube_language: str = "en"
    youtube_proxy_url: str | None = None
    last_successful_connection: datetime | None = None
    last_error: str | None = None

    @field_validator("engine", mode="before")
    @classmethod
    def _engine(cls, value: Any) -> str:
        text = (_strip_or_none(value) or "searxng").lower()
        if text not in {"searxng", "disabled"}:
            raise ValueError("Web Search Engine must be 'searxng' or 'disabled'.")
        return text

    @field_validator("query_url_template", "language", "loader_engine", "youtube_language", "youtube_proxy_url", "last_error", mode="before")
    @classmethod
    def _optional_text_fields(cls, value: Any) -> str | None:
        return _strip_or_none(value)

    @field_validator("domain_filters", mode="before")
    @classmethod
    def _domain_filters(cls, value: Any) -> list[str]:
        if isinstance(value, str):
            items = value.replace(",", "\n").splitlines()
        elif isinstance(value, list):
            items = value
        else:
            items = []
        cleaned: list[str] = []
        seen: set[str] = set()
        for item in items:
            text = str(item).strip().lower()
            if not text:
                continue
            text = text.removeprefix("http://").removeprefix("https://").strip("/")
            if not text or text in seen:
                continue
            cleaned.append(text)
            seen.add(text)
        return cleaned

    @field_validator("query_url_template")
    @classmethod
    def _query_template(cls, value: str | None, info) -> str | None:
        engine = info.data.get("engine", "searxng")
        enabled = info.data.get("enabled", False)
        text = value or "http://10.20.2.8:8080/search?q=<query>"
        if enabled and engine != "disabled" and "<query>" not in text:
            raise ValueError("Search query URL must contain <query> when Web Search is enabled.")
        return text


class ProjectContextPayload(BaseModel):
    added_info: str | None = None
    extra_instructions: str | None = None
    use_project_token_limit: bool = False
    project_max_tokens: int = Field(default=2048, ge=64, le=8192)

    @field_validator("added_info", "extra_instructions", mode="before")
    @classmethod
    def _optional_text(cls, value: Any) -> str | None:
        text = _strip_or_none(value)
        if text and len(text) > 12000:
            raise ValueError("Keep project notes under 12,000 characters.")
        return text

    @field_validator("project_max_tokens", mode="before")
    @classmethod
    def _project_max_tokens(cls, value: Any) -> int:
        if value in {None, ""}:
            return 2048
        return int(value)


class ProjectInterpretationResult(BaseModel):
    asset_kind: str
    canonical_name: str
    one_line_summary: str
    operator_summary: str
    evidence: list[str]
    likely_role: str
    primary_stack: str
    tags: list[str]
    confidence: float = Field(ge=0, le=1)
    unknowns: list[str]
    risks_or_notes: list[str]


class MachineClassificationResult(BaseModel):
    machine_kind: str
    detected_platform: str
    detected_role: str
    confidence: float = Field(ge=0, le=1)
    evidence: list[str] = Field(default_factory=list)
    story_template_type: str


class MachineProfileNarrativeResult(BaseModel):
    lead_paragraph: str
    project_role_summary: str
    setup_summary_items: list[str] = Field(default_factory=list)
    why_it_matters_items: list[str] = Field(default_factory=list)
    notes_items: list[str] = Field(default_factory=list)
    unknowns_items: list[str] = Field(default_factory=list)


class InvestigationAssistantQuestion(BaseModel):
    question_text: str
    question_type: str | None = None
    ai_rationale: str | None = None


class InvestigationCustomSection(BaseModel):
    heading: str
    items: list[str] = Field(default_factory=list)


class InvestigationAssistantTurn(BaseModel):
    assistant_message: str | None = None
    draft_title: str | None = None
    draft_overview: str | None = None
    draft_report_text: str | None = None
    publish_title: str | None = None
    publish_summary: str | None = None
    publish_long_description: str | None = None
    detail_sections: list[str] | None = None
    role_in_lab: str | None = None
    setup_summary: list[str] | None = None
    notes_or_highlights: list[str] | None = None
    custom_sections: list[InvestigationCustomSection] | None = None
    ready_for_confirmation: bool = False
    question: InvestigationAssistantQuestion | None = None


class ProjectFileReviewPlan(BaseModel):
    requested_paths: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ProjectBatchReviewResult(BaseModel):
    batch_summary: str
    notable_points: list[str] = Field(default_factory=list)


class ProjectSnapshot(BaseModel):
    host_id: int
    path: str
    name_guess: str
    project_type: str
    repo_remote: str | None = None
    repo_branch: str | None = None
    head_commit: str | None = None
    is_git_repo: bool = False
    last_modified_at: datetime | None = None
    fingerprint: str
    marker_files: list[dict[str, str]]
    shallow_tree_json: list[dict[str, Any]] | None = None
    metadata_json: dict[str, Any] = Field(default_factory=dict)


class GitHubUpdateResult(BaseModel):
    update_available: bool
    local_revision: str | None
    remote_revision: str | None
    message: str
