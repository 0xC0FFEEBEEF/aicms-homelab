from __future__ import annotations

import copy
import json
from typing import Any, Callable

import httpx
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, selectinload

from app.config import AppConfig
from app.jobs.cancellation import JobCancelled
from app.lmstudio.client import LMStudioClient
from app.models import LMStudioInteractionLog, LMStudioModel, Project, ProjectFile, ProjectInterpretation
from app.schemas import (
    LMSTUDIO_SETTINGS_KEY,
    ProjectBatchReviewResult,
    LMStudioSettingsPayload,
    ProjectFileReviewPlan,
    ProjectInterpretationResult,
)
from app.utils.common import utcnow
from app.utils.settings_store import get_settings, set_settings


PROMPT_VERSION = "aicms-v3"
DEFAULT_PROMPT_CHAR_BUDGET = 10000


def _record_log(session: Session, action: str, status: str, message: str, details: dict[str, Any] | None = None) -> None:
    session.add(
        LMStudioInteractionLog(
            action=action,
            status=status,
            message=message,
            details_json=details,
        )
    )
    session.commit()


def _client(config: AppConfig, settings: LMStudioSettingsPayload) -> LMStudioClient:
    return LMStudioClient(
        base_url=settings.base_url,
        timeout=settings.request_timeout_seconds if settings.use_custom_timeout else config.lmstudio_timeout_seconds,
        api_token=settings.api_token,
    )


def _store_settings(session: Session, settings: LMStudioSettingsPayload) -> None:
    set_settings(session, LMSTUDIO_SETTINGS_KEY, settings)


def _project_max_tokens_override(project: Project) -> int | None:
    metadata = project.metadata_json or {}
    preferences = metadata.get("interpretation_preferences") or {}
    override = preferences.get("max_output_tokens")
    if override in {None, ""}:
        return None
    try:
        return int(override)
    except (TypeError, ValueError):
        return None


def _resolve_final_summary_max_tokens(settings: LMStudioSettingsPayload, project: Project) -> int | None:
    project_override = _project_max_tokens_override(project)
    if project_override is not None:
        return project_override
    if settings.use_max_output_tokens:
        return None
    return settings.max_tokens


def test_connection(session: Session, config: AppConfig, *, prompt_test: bool = False) -> str:
    settings = get_settings(session, config, LMSTUDIO_SETTINGS_KEY, LMStudioSettingsPayload)
    client = _client(config, settings)
    action_name = "prompt_test" if prompt_test else "connection_test"

    try:
        models = client.list_models()
        message = f"LM Studio reachable. {len(models)} model(s) available."

        if prompt_test and not settings.selected_model:
            raise RuntimeError("Select an LM Studio model before running a prompt test.")

        if prompt_test and settings.selected_model:
            result = client.prompt_test(settings.selected_model)
            message = f"{message} Prompt test succeeded: {result['content']}"
            _record_log(session, "prompt_test", "success", "Prompt test succeeded.", {"response": result})
        else:
            _record_log(session, "connection_test", "success", message, {"models_count": len(models)})

        settings.last_successful_contact = utcnow()
        settings.last_error = None
        _store_settings(session, settings)
        return message
    except Exception as exc:
        settings.last_error = str(exc)
        _store_settings(session, settings)
        _record_log(session, action_name, "error", str(exc))
        raise


def refresh_models(session: Session, config: AppConfig) -> int:
    settings = get_settings(session, config, LMSTUDIO_SETTINGS_KEY, LMStudioSettingsPayload)
    client = _client(config, settings)
    models = client.list_models()

    session.execute(delete(LMStudioModel))
    now = utcnow()
    for model in models:
        session.add(
            LMStudioModel(
                model_id=model["id"],
                raw_json=model,
                last_seen_at=now,
            )
        )
    session.commit()

    settings.last_refresh_time = now
    settings.last_successful_contact = now
    settings.last_error = None
    _store_settings(session, settings)
    _record_log(session, "models_refresh", "success", f"Refreshed {len(models)} model(s).")
    return len(models)


def build_project_packet(
    project: Project,
    *,
    files: list[ProjectFile] | None = None,
    requested_paths: list[str] | None = None,
    inventory_limit: int = 160,
    reviewed_limit: int = 8,
    content_sample_limit: int = 1200,
) -> dict[str, Any]:
    metadata = project.metadata_json or {}
    if metadata.get("setup_kind") == "host-summary":
        inventory_limit = min(inventory_limit, 96)
        reviewed_limit = min(reviewed_limit, 6)
    inventory_files = files if files is not None else list(project.files) if hasattr(project, "files") else []
    requested_path_set = set(requested_paths or [])
    reviewed_files = []
    file_inventory = []
    truncated_inventory = max(len(inventory_files) - inventory_limit, 0)
    for file_record in inventory_files[:inventory_limit]:
        item = {
            "path": file_record.path,
            "relative_path": file_record.relative_path,
            "entry_type": file_record.entry_type,
            "size_bytes": file_record.size_bytes,
            "last_modified_at": file_record.last_modified_at.isoformat() if file_record.last_modified_at else None,
            "fingerprint": file_record.fingerprint,
            "last_ai_reviewed_at": file_record.last_ai_reviewed_at.isoformat() if file_record.last_ai_reviewed_at else None,
            "changed_since_review": bool(
                file_record.fingerprint
                and file_record.last_ai_reviewed_fingerprint
                and file_record.fingerprint != file_record.last_ai_reviewed_fingerprint
            ),
        }
        file_inventory.append(item)
        if file_record.content_sample and (file_record.relative_path in requested_path_set or not requested_path_set):
            reviewed_files.append(
                {
                    **item,
                    "content_sample": file_record.content_sample[:content_sample_limit],
                }
            )
    if reviewed_limit >= 0:
        reviewed_files = reviewed_files[:reviewed_limit]

    inventory_summary = copy.deepcopy(metadata.get("inventory_summary") or {})
    if isinstance(inventory_summary.get("root_summaries"), list):
        inventory_summary["root_summaries"] = inventory_summary["root_summaries"][:8]

    child_projects = copy.deepcopy(metadata.get("child_projects") or [])
    if isinstance(child_projects, list):
        child_projects = child_projects[:12]

    operator_context = copy.deepcopy(metadata.get("operator_context") or {})

    packet_metadata = {
        key: value
        for key, value in metadata.items()
        if key
        not in {
            "host_profile",
            "grouped_roots",
            "inventory_summary",
            "proxmox_inventory",
            "child_projects",
            "operator_context",
        }
    }
    if child_projects:
        packet_metadata["child_project_count"] = len(metadata.get("child_projects") or [])
    if inventory_summary:
        packet_metadata["root_summary_count"] = len(inventory_summary.get("root_summaries") or [])

    return {
        "host_name": project.host.name if project.host else None,
        "host_context": {
            "host_name": project.host.name if project.host else None,
            "host_address": project.host.address if project.host else None,
            "ssh_user": project.host.ssh_user if project.host else None,
        },
        "host_profile": metadata.get("host_profile") or {},
        "path": project.path,
        "name_guess": project.name_guess,
        "project_type": project.project_type,
        "setup_kind": metadata.get("setup_kind"),
        "grouped_roots": metadata.get("grouped_roots") or [],
        "inventory_summary": inventory_summary,
        "proxmox_inventory": metadata.get("proxmox_inventory") or {},
        "child_projects": child_projects,
        "operator_context": operator_context,
        "project_counts": metadata.get("project_counts") or {},
        "marker_files": [marker.marker_name for marker in project.markers],
        "git_metadata": {
            "repo_remote": project.repo_remote,
            "repo_branch": project.repo_branch,
            "head_commit": project.head_commit,
            "is_git_repo": project.is_git_repo,
        },
        "shallow_tree": project.shallow_tree_json,
        "timestamps": {
            "created_at": project.created_at.isoformat() if project.created_at else None,
            "last_modified_at": project.last_modified_at.isoformat() if project.last_modified_at else None,
            "last_seen_at": project.last_seen_at.isoformat() if project.last_seen_at else None,
        },
        "file_inventory_total": len(inventory_files),
        "file_inventory_truncated": truncated_inventory,
        "file_inventory": file_inventory,
        "reviewed_files": reviewed_files,
        "requested_paths": requested_paths or [],
        "metadata_json": packet_metadata,
    }


def _compact_project_packet(packet: dict[str, Any], *, level: int) -> dict[str, Any]:
    compacted = copy.deepcopy(packet)
    if level == 0:
        return compacted

    operator_context = compacted.get("operator_context") or {}
    if isinstance(operator_context, dict):
        if level >= 1:
            for key in ("added_info", "extra_instructions"):
                if isinstance(operator_context.get(key), str):
                    operator_context[key] = operator_context[key][:2000]
        if level >= 2:
            for key in ("added_info", "extra_instructions"):
                if isinstance(operator_context.get(key), str):
                    operator_context[key] = operator_context[key][:1200]
        if level >= 3:
            for key in ("added_info", "extra_instructions"):
                if isinstance(operator_context.get(key), str):
                    operator_context[key] = operator_context[key][:600]
        if level >= 4:
            for key in ("added_info", "extra_instructions"):
                if isinstance(operator_context.get(key), str):
                    operator_context[key] = operator_context[key][:300]
        compacted["operator_context"] = {key: value for key, value in operator_context.items() if value}

    batch_reviews = compacted.get("batch_reviews") or []
    if isinstance(batch_reviews, list):
        if level >= 1:
            batch_reviews = [
                {
                    "batch_index": item.get("batch_index"),
                    "paths_reviewed": (item.get("paths_reviewed") or [])[:4],
                    "summary": (item.get("summary") or "")[:500],
                    "notable_points": (item.get("notable_points") or [])[:4],
                }
                for item in batch_reviews[:12]
                if isinstance(item, dict)
            ]
        if level >= 2:
            batch_reviews = [
                {
                    "batch_index": item.get("batch_index"),
                    "summary": (item.get("summary") or "")[:320],
                    "notable_points": (item.get("notable_points") or [])[:3],
                }
                for item in batch_reviews[:8]
                if isinstance(item, dict)
            ]
        if level >= 3:
            batch_reviews = [
                {
                    "batch_index": item.get("batch_index"),
                    "summary": (item.get("summary") or "")[:220],
                }
                for item in batch_reviews[:6]
                if isinstance(item, dict)
            ]
        if level >= 4:
            batch_reviews = [
                {
                    "batch_index": item.get("batch_index"),
                    "summary": (item.get("summary") or "")[:140],
                }
                for item in batch_reviews[:4]
                if isinstance(item, dict)
            ]
        compacted["batch_reviews"] = batch_reviews

    for reviewed_file in compacted.get("reviewed_files", []):
        if isinstance(reviewed_file, dict) and isinstance(reviewed_file.get("content_sample"), str):
            reviewed_file["content_sample"] = reviewed_file["content_sample"][:1200]

    if level >= 1:
        compacted["file_inventory"] = [
            {
                "relative_path": item.get("relative_path"),
                "entry_type": item.get("entry_type"),
                "changed_since_review": item.get("changed_since_review"),
            }
            for item in compacted.get("file_inventory", [])[:16]
            if isinstance(item, dict)
        ]
        compacted["reviewed_files"] = compacted.get("reviewed_files", [])[:4]
        compacted["child_projects"] = [
            {
                "name": item.get("name"),
                "path": item.get("path"),
                "project_type": item.get("project_type"),
                "setup_kind": item.get("setup_kind"),
                "marker_names": item.get("marker_names", [])[:4],
            }
            for item in compacted.get("child_projects", [])[:6]
            if isinstance(item, dict)
        ]
        inventory_summary = compacted.get("inventory_summary") or {}
        if isinstance(inventory_summary.get("root_summaries"), list):
            inventory_summary["root_summaries"] = [
                {
                    "root_path": item.get("root_path"),
                    "entry_count": item.get("entry_count"),
                    "observed_names": item.get("observed_names", [])[:6],
                    "top_level_entries": item.get("top_level_entries", [])[:4],
                }
                for item in inventory_summary["root_summaries"][:4]
                if isinstance(item, dict)
            ]
        compacted["inventory_summary"] = inventory_summary
        compacted["shallow_tree"] = compacted.get("shallow_tree", [])[:24]
        compacted["marker_files"] = compacted.get("marker_files", [])[:24]
    if level >= 2:
        compacted["file_inventory"] = [
            item.get("relative_path")
            for item in compacted.get("file_inventory", [])[:8]
            if isinstance(item, dict) and item.get("relative_path")
        ]
        compacted["reviewed_files"] = compacted.get("reviewed_files", [])[:3]
        for reviewed_file in compacted["reviewed_files"]:
            if isinstance(reviewed_file, dict) and isinstance(reviewed_file.get("content_sample"), str):
                reviewed_file["content_sample"] = reviewed_file["content_sample"][:700]
        compacted["child_projects"] = compacted.get("child_projects", [])[:4]
        inventory_summary = compacted.get("inventory_summary") or {}
        compacted["inventory_summary"] = {
            "root_count": inventory_summary.get("root_count"),
            "entry_count": inventory_summary.get("entry_count"),
            "safe_text_candidates": inventory_summary.get("safe_text_candidates"),
            "observed_names": (inventory_summary.get("observed_names") or [])[:8],
        }
        compacted["metadata_json"] = {
            key: value
            for key, value in (compacted.get("metadata_json") or {}).items()
            if key in {"discovered_via", "setup_kind", "child_project_count", "root_summary_count"}
        }
        compacted["shallow_tree"] = compacted.get("shallow_tree", [])[:12]
        compacted["timestamps"] = {
            "last_modified_at": (compacted.get("timestamps") or {}).get("last_modified_at"),
            "last_seen_at": (compacted.get("timestamps") or {}).get("last_seen_at"),
        }
    if level >= 3:
        compacted["file_inventory"] = []
        compacted["reviewed_files"] = compacted.get("reviewed_files", [])[:2]
        for reviewed_file in compacted["reviewed_files"]:
            if isinstance(reviewed_file, dict):
                reviewed_file_keys = {"relative_path", "entry_type", "content_sample"}
                for key in list(reviewed_file.keys()):
                    if key not in reviewed_file_keys:
                        reviewed_file.pop(key, None)
                if isinstance(reviewed_file.get("content_sample"), str):
                    reviewed_file["content_sample"] = reviewed_file["content_sample"][:400]
        compacted["child_projects"] = [
            {
                "name": item.get("name"),
                "project_type": item.get("project_type"),
                "path": item.get("path"),
            }
            for item in compacted.get("child_projects", [])[:3]
            if isinstance(item, dict)
        ]
        compacted["grouped_roots"] = compacted.get("grouped_roots", [])[:4]
        compacted["inventory_summary"] = {
            "root_count": (compacted.get("inventory_summary") or {}).get("root_count"),
            "entry_count": (compacted.get("inventory_summary") or {}).get("entry_count"),
            "observed_names": ((compacted.get("inventory_summary") or {}).get("observed_names") or [])[:6],
        }
        compacted["project_counts"] = compacted.get("project_counts", {})
        compacted["marker_files"] = compacted.get("marker_files", [])[:12]
        compacted["shallow_tree"] = []
        compacted["timestamps"] = {
            "last_seen_at": (compacted.get("timestamps") or {}).get("last_seen_at"),
        }
    if level >= 4:
        compacted["child_projects"] = compacted.get("child_projects", [])[:2]
        compacted["reviewed_files"] = compacted.get("reviewed_files", [])[:1]
        compacted["inventory_summary"] = {
            "root_count": (compacted.get("inventory_summary") or {}).get("root_count"),
            "entry_count": (compacted.get("inventory_summary") or {}).get("entry_count"),
        }
        compacted["marker_files"] = []
        compacted["metadata_json"] = {
            key: value
            for key, value in (compacted.get("metadata_json") or {}).items()
            if key in {"discovered_via", "setup_kind"}
        }
    return compacted


def _packet_char_size(packet: dict[str, Any]) -> int:
    return len(json.dumps(packet, sort_keys=True))


def _build_packet_variants(base_project_packet: dict[str, Any]) -> list[dict[str, Any]]:
    if base_project_packet.get("setup_kind") != "host-summary" and not base_project_packet.get("batch_reviews"):
        return [base_project_packet]

    variants = [_compact_project_packet(base_project_packet, level=level) for level in range(5)]
    fitting = [packet for packet in variants if _packet_char_size(packet) <= DEFAULT_PROMPT_CHAR_BUDGET]
    if fitting:
        preferred = fitting[0]
        preferred_index = variants.index(preferred)
        ordered = variants[preferred_index:]
        deduped: list[dict[str, Any]] = []
        seen_payloads: set[str] = set()
        for packet in ordered:
            serialized = json.dumps(packet, sort_keys=True)
            if serialized in seen_payloads:
                continue
            seen_payloads.add(serialized)
            deduped.append(packet)
        return deduped

    smallest = min(variants, key=_packet_char_size)
    return [smallest]


def choose_files_for_review(
    session: Session,
    config: AppConfig,
    *,
    project: Project,
    candidate_files: list[ProjectFile],
) -> ProjectFileReviewPlan:
    settings = get_settings(session, config, LMSTUDIO_SETTINGS_KEY, LMStudioSettingsPayload)
    if not settings.enabled:
        raise RuntimeError("LM Studio interpretation is disabled.")
    if not settings.selected_model:
        raise RuntimeError("No LM Studio model is selected.")

    client = _client(config, settings)
    packet = build_project_packet(project, files=candidate_files)
    plan = client.plan_file_review(model_id=settings.selected_model, project_packet=packet)
    return ProjectFileReviewPlan(
        requested_paths=[
            path for path in plan.requested_paths if any(file_record.relative_path == path for file_record in candidate_files)
        ],
        notes=plan.notes,
    )


def review_project_batch(
    session: Session,
    config: AppConfig,
    *,
    project: Project,
    batch_files: list[ProjectFile],
    batch_index: int,
    batch_count: int,
) -> ProjectBatchReviewResult:
    settings = get_settings(session, config, LMSTUDIO_SETTINGS_KEY, LMStudioSettingsPayload)
    if not settings.enabled:
        raise RuntimeError("LM Studio interpretation is disabled.")
    if not settings.selected_model:
        raise RuntimeError("No LM Studio model is selected.")

    client = _client(config, settings)
    batch_paths = [file_record.relative_path for file_record in batch_files]
    batch_packet = build_project_packet(
        project,
        files=batch_files,
        requested_paths=batch_paths,
        inventory_limit=max(len(batch_files), 1),
        reviewed_limit=max(len(batch_files), 1),
        content_sample_limit=1200,
    )
    try:
        result = client.review_project_batch(
            model_id=settings.selected_model,
            project_packet=batch_packet,
            batch_index=batch_index,
            batch_count=batch_count,
        )
        _record_log(
            session,
            "review_project_batch",
            "success",
            f"Reviewed batch {batch_index}/{batch_count} for {project.path}.",
            {"project_id": project.id, "paths_reviewed": batch_paths},
        )
        return result
    except Exception as exc:
        _record_log(
            session,
            "review_project_batch",
            "error",
            str(exc),
            {"project_id": project.id, "paths_reviewed": batch_paths, "batch_index": batch_index},
        )
        raise


def interpret_project(
    session: Session,
    config: AppConfig,
    project_id: int,
    *,
    project_packet_override: dict[str, Any] | None = None,
    stream_callback: Callable[[str], None] | None = None,
) -> ProjectInterpretation:
    settings = get_settings(session, config, LMSTUDIO_SETTINGS_KEY, LMStudioSettingsPayload)
    project = session.execute(
        select(Project)
        .where(Project.id == project_id)
        .options(selectinload(Project.host), selectinload(Project.markers), selectinload(Project.files))
    ).scalar_one()

    record = ProjectInterpretation(
        project_id=project_id,
        model_id=settings.selected_model,
        status="failed",
        prompt_version=PROMPT_VERSION,
    )
    session.add(record)
    session.flush()

    if not settings.enabled:
        record.summary = "LM Studio interpretation is disabled."
        session.commit()
        raise RuntimeError(record.summary)

    if not settings.selected_model:
        record.summary = "No LM Studio model is selected."
        session.commit()
        raise RuntimeError(record.summary)

    client = _client(config, settings)
    base_project_packet = project_packet_override or build_project_packet(project)
    packet_variants = _build_packet_variants(base_project_packet)

    try:
        last_exc: Exception | None = None
        parsed = None
        for index, packet_variant in enumerate(packet_variants):
            try:
                parsed = client.interpret_project(
                    model_id=settings.selected_model,
                    project_packet=packet_variant,
                    temperature=settings.temperature,
                    max_tokens=_resolve_final_summary_max_tokens(settings, project),
                    stream_callback=stream_callback if index == 0 else None,
                )
                break
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                if exc.response is None or exc.response.status_code != 400 or index == len(packet_variants) - 1:
                    raise
            except Exception as exc:
                last_exc = exc
                if index == len(packet_variants) - 1:
                    raise

        if parsed is None:
            raise last_exc or RuntimeError("LM Studio did not return an interpretation.")
        record.status = "completed"
        record.response_json = parsed.model_dump(mode="json")
        record.summary = parsed.one_line_summary
        record.confidence = parsed.confidence
        session.commit()
        _record_log(
            session,
            "interpret_project",
            "success",
            f"Interpreted project {project.path}.",
            {"project_id": project.id, "model_id": settings.selected_model},
        )
        return record
    except JobCancelled as exc:
        record.status = "cancelled"
        record.summary = str(exc)
        session.commit()
        _record_log(
            session,
            "interpret_project",
            "cancelled",
            str(exc),
            {"project_id": project.id},
        )
        raise
    except Exception as exc:
        record.summary = str(exc)
        session.commit()
        _record_log(
            session,
            "interpret_project",
            "error",
            str(exc),
            {"project_id": project.id},
        )
        raise


def latest_logs(session: Session, limit: int = 20) -> list[LMStudioInteractionLog]:
    return session.execute(select(LMStudioInteractionLog).order_by(LMStudioInteractionLog.created_at.desc()).limit(limit)).scalars().all()
