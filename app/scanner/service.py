from __future__ import annotations

import shlex
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import PurePosixPath
from threading import Event
from typing import Any, Callable, Iterable

from fastapi import FastAPI
from sqlalchemy import Select, select
from sqlalchemy.orm import Session, selectinload

from app.config import AppConfig
from app.db import session_scope
from app.diff.service import summarize_changes
from app.jobs.cancellation import JobCancelled, ensure_not_cancelled
from app.models import (
    Host,
    MachineProfile,
    Project,
    ProjectFile,
    ProjectMarker,
    Scan,
    ScanEvent,
)
from app.profiles.classifier import classify_machine
from app.profiles.collector import collect_machine_facts
from app.profiles.inventory import sync_host_fact_inventory
from app.profiles.service import generate_machine_profile
from app.redaction.rules import SENSITIVE_PATH_PARTS, filter_tree_entries, is_safe_text_path, should_ignore_path
from app.scanner.detection import (
    build_project_fingerprint,
    build_project_metadata,
    guess_project_name,
    guess_project_type,
)
from app.scanner.ssh import SSHCommandError, SSHExecutor
from app.schemas import (
    GENERAL_SETTINGS_KEY,
    MCP_SETTINGS_KEY,
    MCPSettingsPayload,
    PROFILE_SETTINGS_KEY,
    SCAN_SETTINGS_KEY,
    GeneralSettingsPayload,
    ProfileSettingsPayload,
    ProjectSnapshot,
    ScanSettingsPayload,
)
from app.topology.service import refresh_host_topology
from app.utils.common import fingerprint_payload, utcnow
from app.utils.settings_store import get_settings

EventRecorder = Callable[..., None]

LOW_SIGNAL_ROOTS = {
    "/bin",
    "/sbin",
    "/usr/bin",
    "/usr/sbin",
    "/usr/local/bin",
    "/usr/local/sbin",
}

COMMON_SIGNAL_TOKENS = {
    "app",
    "apps",
    "bin",
    "cache",
    "conf",
    "config",
    "data",
    "default",
    "etc",
    "file",
    "files",
    "home",
    "json",
    "lib",
    "local",
    "log",
    "logs",
    "node",
    "opt",
    "path",
    "private",
    "project",
    "projects",
    "root",
    "run",
    "script",
    "scripts",
    "service",
    "setup",
    "share",
    "srv",
    "state",
    "system",
    "tmp",
    "toml",
    "txt",
    "user",
    "var",
    "yaml",
    "yml",
}

REVIEW_BATCH_SIZE = 4


def record_scan_event(
    session: Session,
    *,
    scan_id: int,
    level: str,
    event_type: str,
    message: str,
    host_id: int | None = None,
    project_id: int | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    session.add(
        ScanEvent(
            scan_id=scan_id,
            host_id=host_id,
            project_id=project_id,
            level=level,
            event_type=event_type,
            message=message,
            details_json=details,
        )
    )


def _emit_event(
    record_event: EventRecorder | None,
    *,
    level: str,
    event_type: str,
    message: str,
    host_id: int | None = None,
    project_id: int | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    if record_event is None:
        return
    record_event(
        level=level,
        event_type=event_type,
        message=message,
        host_id=host_id,
        project_id=project_id,
        details=details,
    )


def _discovery_depth(general_settings: GeneralSettingsPayload) -> int:
    return max(general_settings.default_max_tree_depth + 2, 4)


def _enabled_roots(host: Host) -> list[str]:
    return [root.root_path for root in host.roots if root.enabled]


def _root_prefix(path: str) -> str:
    return path.rstrip("/") + "/"


def _matches_path_prefix(path: str, prefix: str) -> bool:
    trimmed_prefix = prefix.rstrip("/")
    return path == trimmed_prefix or path.startswith(_root_prefix(trimmed_prefix))


def _prune_names(ignore_patterns: list[str]) -> list[str]:
    names = {pattern for pattern in ignore_patterns if "/" not in pattern and pattern != ".git"}
    names.update(SENSITIVE_PATH_PARTS)
    return sorted(names)


def _safe_int(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value.strip())
    except (TypeError, ValueError):
        return None


def _bytes_to_gib(value: int | None) -> float | None:
    if value is None:
        return None
    return round(value / (1024**3), 2)


def _path_overlaps_root(path: str, root_path: str) -> bool:
    return _matches_path_prefix(path, root_path) or _matches_path_prefix(root_path, path)


def _classify_root(root_path: str) -> dict[str, str]:
    path = PurePosixPath(root_path)
    parts = path.parts
    if len(parts) >= 6 and parts[1:4] == ("etc", "pve", "nodes"):
        node_name = parts[4]
        node_role = parts[5]
        if node_role in {"qemu-server", "lxc"}:
            return {
                "kind": "proxmox-node-config",
                "node_name": node_name,
                "node_role": node_role,
                "group_key": f"proxmox:{node_name}",
            }
    if root_path == "/var/lib/vz/snippets":
        return {"kind": "proxmox-snippets"}
    if root_path in LOW_SIGNAL_ROOTS:
        return {"kind": "low-signal-root"}
    if len(parts) >= 3 and parts[1] == "home":
        return {"kind": "home-root", "home_user": parts[2]}
    return {"kind": "generic-root"}


def _top_level_entries(root_path: str, inventory_entries: list[dict[str, Any]], *, limit: int = 24) -> list[dict[str, str]]:
    root = PurePosixPath(root_path)
    items: dict[str, dict[str, str]] = {}
    for entry in inventory_entries:
        try:
            relative = PurePosixPath(entry["path"]).relative_to(root)
        except ValueError:
            continue
        if not relative.parts:
            continue
        first_segment = relative.parts[0]
        current = items.get(first_segment)
        candidate = {"name": first_segment, "type": entry["entry_type"]}
        if current is None or (current["type"] == "file" and entry["entry_type"] == "dir"):
            items[first_segment] = candidate
    return sorted(items.values(), key=lambda item: (item["name"], item["type"]))[:limit]


def _signal_names(inventory_entries: list[dict[str, Any]], *, limit: int = 10) -> list[str]:
    counts: Counter[str] = Counter()
    for entry in inventory_entries:
        relative_path = entry.get("relative_path") or ""
        path_weight = 2 if "/" not in relative_path else 1
        for token in re.split(r"[^a-z0-9]+", relative_path.lower()):
            if len(token) < 3 or token.isdigit() or token in COMMON_SIGNAL_TOKENS:
                continue
            counts[token] += path_weight
    return [token for token, _count in counts.most_common(limit)]


def _summarize_inventory(root_path: str, inventory_entries: list[dict[str, Any]]) -> dict[str, Any]:
    file_count = sum(1 for entry in inventory_entries if entry["entry_type"] == "file")
    dir_count = sum(1 for entry in inventory_entries if entry["entry_type"] == "dir")
    top_level = _top_level_entries(root_path, inventory_entries)
    summary = {
        "root_path": root_path,
        "entry_count": len(inventory_entries),
        "file_count": file_count,
        "dir_count": dir_count,
        "top_level_entries": top_level,
        "observed_names": _signal_names(inventory_entries),
        "safe_text_candidates": sum(
            1 for entry in inventory_entries if entry["entry_type"] == "file" and is_safe_text_path(entry["path"])
        ),
    }
    safe_file_examples = [
        entry["relative_path"]
        for entry in inventory_entries
        if entry["entry_type"] == "file" and is_safe_text_path(entry["path"])
    ][:12]
    if safe_file_examples:
        summary["safe_file_examples"] = safe_file_examples
    return summary


def _friendly_workspace_name(root_path: str, inventory_entries: list[dict[str, Any]], host_name: str | None = None) -> str:
    root_info = _classify_root(root_path)
    inventory_summary = _summarize_inventory(root_path, inventory_entries)
    observed_names = inventory_summary.get("observed_names") or []

    if root_info.get("kind") == "home-root":
        home_user = root_info.get("home_user") or "home"
        if observed_names:
            lead = observed_names[0]
            if lead != home_user.lower():
                return f"{home_user} {lead} setup"
        return f"{home_user} host setup"

    if observed_names:
        lead = observed_names[0]
        return f"{lead} setup"

    root_name = PurePosixPath(root_path).name or root_path.strip("/") or "workspace"
    if host_name and root_name in {"opt", "srv", "projects"}:
        return f"{host_name} {root_name}"
    return root_name


def _collect_host_facts(
    executor: SSHExecutor,
    client: Any,
    *,
    host: Host,
    record_event: EventRecorder | None = None,
) -> dict[str, Any]:
    profile_settings = ProfileSettingsPayload()
    facts = collect_machine_facts(
        host=host,
        run_lines=lambda command, **kwargs: _run_lines(
            executor,
            client,
            command,
            record_event=record_event,
            host_id=host.id,
            **kwargs,
        ),
        run_command=lambda command, **kwargs: _run_command(
            executor,
            client,
            command,
            record_event=record_event,
            host_id=host.id,
            **kwargs,
        ),
        max_services=profile_settings.max_services_shown,
        max_guests=profile_settings.max_guests_shown,
    )
    _emit_event(
        record_event,
        level="info",
        event_type="host_facts_collected",
        message=f"Collected host facts for {host.name}.",
        host_id=host.id,
        details={
            "os": facts.get("os_pretty_name"),
            "cpu_cores": facts.get("cpu_cores"),
            "cpu_threads": facts.get("cpu_threads"),
            "memory_gib": facts.get("memory_gib"),
            "root_size_gib": facts.get("root_size_gib"),
            "virtualization": facts.get("virtualization"),
            "proxmox_version": facts.get("proxmox_version"),
        },
    )
    return facts


def _record_command_start(
    record_event: EventRecorder | None,
    *,
    command: str,
    host_id: int | None = None,
    project_id: int | None = None,
    phase: str = "remote",
) -> None:
    _emit_event(
        record_event,
        level="info",
        event_type="command_started",
        message=f"Running {phase} command.",
        host_id=host_id,
        project_id=project_id,
        details={"command": command},
    )


def _record_command_finish(
    record_event: EventRecorder | None,
    *,
    command: str,
    host_id: int | None = None,
    project_id: int | None = None,
    stdout_preview: str | None = None,
    stderr_preview: str | None = None,
    exit_code: int | None = None,
    partial: bool = False,
) -> None:
    level = "warning" if partial else "info"
    event_type = "command_partial" if partial else "command_completed"
    message = "Command completed with warnings." if partial else "Command completed."
    details = {"command": command, "exit_code": exit_code}
    if stdout_preview:
        details["stdout_preview"] = stdout_preview
    if stderr_preview:
        details["stderr_preview"] = stderr_preview
    _emit_event(
        record_event,
        level=level,
        event_type=event_type,
        message=message,
        host_id=host_id,
        project_id=project_id,
        details=details,
    )


def _run_command(
    executor: SSHExecutor,
    client: Any,
    command: str,
    *,
    timeout: int | None = None,
    record_event: EventRecorder | None = None,
    host_id: int | None = None,
    project_id: int | None = None,
    phase: str = "remote",
    allow_partial: bool = False,
) -> tuple[int, str, str]:
    _record_command_start(record_event, command=command, host_id=host_id, project_id=project_id, phase=phase)
    code, stdout, stderr = executor.run(client, command, timeout=timeout)
    stdout_preview = "\n".join(stdout.splitlines()[:8]).strip()
    stderr_preview = "\n".join(stderr.splitlines()[:8]).strip()
    if code != 0 and not allow_partial:
        _emit_event(
            record_event,
            level="error",
            event_type="command_failed",
            message=stderr_preview or f"Command failed with exit code {code}.",
            host_id=host_id,
            project_id=project_id,
            details={"command": command, "exit_code": code, "stdout_preview": stdout_preview, "stderr_preview": stderr_preview},
        )
        raise SSHCommandError(stderr.strip() or f"Command failed: {command}")

    _record_command_finish(
        record_event,
        command=command,
        host_id=host_id,
        project_id=project_id,
        stdout_preview=stdout_preview,
        stderr_preview=stderr_preview,
        exit_code=code,
        partial=allow_partial and code != 0,
    )
    return code, stdout, stderr


def _run_lines(
    executor: SSHExecutor,
    client: Any,
    command: str,
    *,
    timeout: int | None = None,
    record_event: EventRecorder | None = None,
    host_id: int | None = None,
    project_id: int | None = None,
    phase: str = "remote",
    allow_partial: bool = False,
) -> tuple[list[str], str | None]:
    code, stdout, stderr = _run_command(
        executor,
        client,
        command,
        timeout=timeout,
        record_event=record_event,
        host_id=host_id,
        project_id=project_id,
        phase=phase,
        allow_partial=allow_partial,
    )
    warning = stderr.strip() if allow_partial and code != 0 else None
    return [line.strip() for line in stdout.splitlines() if line.strip()], warning


def _build_find_command(
    root_path: str,
    marker_names: list[str],
    ignore_patterns: list[str],
    max_depth: int,
    modified_since: datetime | None = None,
) -> str:
    prune_targets = [pattern for pattern in ignore_patterns if "/" not in pattern and pattern != ".git"]
    prune_expr = ""
    if prune_targets:
        prune_expr = r"\( -type d \( " + " -o ".join(f"-name {shlex.quote(name)}" for name in prune_targets) + r" \) -prune \) -o "

    file_markers = [marker for marker in marker_names if marker != ".git"]
    file_patterns = []
    for marker in file_markers:
        if marker.startswith(".") and marker not in {".git", ".env"}:
            file_patterns.append(f"-name {shlex.quote(f'*{marker}')}")
        else:
            file_patterns.append(f"-name {shlex.quote(marker)}")
    file_expr = " -o ".join(file_patterns)
    modified_expr = f"-newermt {shlex.quote(modified_since.strftime('%Y-%m-%d %H:%M:%S'))} " if modified_since else ""

    return (
        f"find {shlex.quote(root_path)} -mindepth 1 -maxdepth {max_depth} "
        f"{prune_expr}"
        f"\\( -type f \\( {file_expr} \\) {modified_expr}-print \\) -o "
        f"\\( -type d -name '.git' -print -prune \\)"
    )


def _build_inventory_command(
    root_path: str,
    ignore_patterns: list[str],
    modified_since: datetime | None = None,
) -> str:
    prune_targets = _prune_names(ignore_patterns)
    prune_expr = ""
    if prune_targets:
        prune_expr = r"\( -type d \( " + " -o ".join(f"-name {shlex.quote(name)}" for name in prune_targets) + r" \) -prune \) -o "

    if modified_since:
        changed_clause = f"-newermt {shlex.quote(modified_since.strftime('%Y-%m-%d %H:%M:%S'))} "
        payload_expr = (
            r"\( -type d "
            f"{changed_clause}-printf '%y\t%s\t%T@\t%p\n' "
            r"\) -o "
            r"\( -type f "
            f"{changed_clause}-printf '%y\t%s\t%T@\t%p\n' "
            r"\)"
        )
    else:
        payload_expr = r"\( -printf '%y\t%s\t%T@\t%p\n' \)"

    return f"find {shlex.quote(root_path)} -mindepth 1 {prune_expr}{payload_expr}"


def _parse_marker_results(lines: Iterable[str], ignore_patterns: list[str]) -> dict[str, list[dict[str, str]]]:
    projects: dict[str, list[dict[str, str]]] = {}
    for line in lines:
        path = PurePosixPath(line)
        if path.name == ".git":
            project_path = str(path.parent)
            marker_name = ".git"
        else:
            if should_ignore_path(line, ignore_patterns):
                continue
            project_path = str(path.parent)
            marker_name = path.name

        projects.setdefault(project_path, []).append(
            {
                "marker_name": marker_name,
                "marker_path": line,
            }
        )
    return projects


def _path_matches_marker(file_name: str, marker_name: str) -> bool:
    if file_name == marker_name:
        return True
    return marker_name.startswith(".") and file_name.endswith(marker_name)


def _parse_inventory_results(
    lines: Iterable[str],
    *,
    root_path: str,
    ignore_patterns: list[str],
) -> list[dict[str, Any]]:
    root = PurePosixPath(root_path)
    entries: list[dict[str, Any]] = []
    for line in lines:
        entry_type, size_text, mtime_text, path = line.split("\t", 3)
        if should_ignore_path(path, ignore_patterns):
            continue

        try:
            relative_path = str(PurePosixPath(path).relative_to(root))
        except ValueError:
            relative_path = path

        last_modified_at = None
        try:
            last_modified_at = datetime.fromtimestamp(float(mtime_text), tz=timezone.utc)
        except (TypeError, ValueError):
            last_modified_at = None

        entries.append(
            {
                "root_path": root_path,
                "path": path,
                "relative_path": relative_path,
                "entry_type": "dir" if entry_type == "d" else "file",
                "size_bytes": int(size_text) if size_text.isdigit() else None,
                "last_modified_at": last_modified_at,
                "fingerprint": fingerprint_payload(
                    {
                        "path": path,
                        "entry_type": entry_type,
                        "size_bytes": size_text,
                        "last_modified_at": last_modified_at.isoformat() if last_modified_at else None,
                    }
                ),
                "metadata_json": {
                    "name": PurePosixPath(path).name,
                    "root_path": root_path,
                },
            }
        )
    return entries


def _markers_from_inventory(
    entries: Iterable[dict[str, Any]],
    allowed_markers: list[str],
    ignore_patterns: list[str],
) -> dict[str, list[dict[str, str]]]:
    projects: dict[str, list[dict[str, str]]] = {}
    for entry in entries:
        marker_path = entry["path"]
        if should_ignore_path(marker_path, ignore_patterns):
            continue
        path = PurePosixPath(marker_path)
        if entry["entry_type"] == "dir" and path.name == ".git":
            project_path = str(path.parent)
            marker_name = ".git"
        elif entry["entry_type"] == "file":
            marker_name = next((marker for marker in allowed_markers if marker != ".git" and _path_matches_marker(path.name, marker)), None)
            if not marker_name:
                continue
            project_path = str(path.parent)
        else:
            continue

        projects.setdefault(project_path, []).append({"marker_name": marker_name, "marker_path": marker_path})
    return projects


def _filter_inventory_for_project(
    project_path: str,
    inventory_entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [entry for entry in inventory_entries if _matches_path_prefix(entry["path"], project_path)]


def _derive_shallow_tree(
    project_path: str,
    inventory_entries: list[dict[str, Any]],
    tree_depth: int,
    ignore_patterns: list[str],
) -> list[dict[str, str]]:
    base = PurePosixPath(project_path)
    entries: list[dict[str, str]] = []
    for entry in inventory_entries:
        if entry["entry_type"] not in {"file", "dir"}:
            continue
        try:
            rel_path = str(PurePosixPath(entry["path"]).relative_to(base))
        except ValueError:
            continue
        if not rel_path or rel_path == ".":
            continue
        if rel_path.count("/") + 1 > tree_depth:
            continue
        entries.append({"type": entry["entry_type"], "path": rel_path})
    entries.sort(key=lambda item: (item["path"], item["type"]))
    return filter_tree_entries(entries[:200], ignore_patterns)


def _stat_mtime(
    executor: SSHExecutor,
    client: Any,
    project_path: str,
    *,
    record_event: EventRecorder | None = None,
    host_id: int | None = None,
    project_id: int | None = None,
) -> datetime | None:
    code, stdout, _ = _run_command(
        executor,
        client,
        f"stat -c %Y {shlex.quote(project_path)}",
        record_event=record_event,
        host_id=host_id,
        project_id=project_id,
        phase="stat",
        allow_partial=True,
    )
    if code != 0:
        return None
    try:
        return datetime.fromtimestamp(int(stdout.strip()), tz=timezone.utc)
    except (TypeError, ValueError):
        return None


def _collect_git_metadata(
    executor: SSHExecutor,
    client: Any,
    project_path: str,
    *,
    record_event: EventRecorder | None = None,
    host_id: int | None = None,
    project_id: int | None = None,
) -> dict[str, Any]:
    git_probe = _run_command(
        executor,
        client,
        f"git -C {shlex.quote(project_path)} rev-parse --is-inside-work-tree",
        record_event=record_event,
        host_id=host_id,
        project_id=project_id,
        phase="git",
        allow_partial=True,
    )
    if git_probe[0] != 0 or "true" not in git_probe[1]:
        return {"is_git_repo": False}

    remote = _run_command(
        executor,
        client,
        f"git -C {shlex.quote(project_path)} config --get remote.origin.url",
        record_event=record_event,
        host_id=host_id,
        project_id=project_id,
        phase="git",
        allow_partial=True,
    )[1].strip() or None
    branch = _run_command(
        executor,
        client,
        f"git -C {shlex.quote(project_path)} branch --show-current",
        record_event=record_event,
        host_id=host_id,
        project_id=project_id,
        phase="git",
        allow_partial=True,
    )[1].strip() or None
    commit = _run_command(
        executor,
        client,
        f"git -C {shlex.quote(project_path)} rev-parse HEAD",
        record_event=record_event,
        host_id=host_id,
        project_id=project_id,
        phase="git",
        allow_partial=True,
    )[1].strip() or None

    return {
        "is_git_repo": True,
        "repo_remote": remote,
        "repo_branch": branch,
        "head_commit": commit,
    }


def _collect_shallow_tree(
    executor: SSHExecutor,
    client: Any,
    project_path: str,
    tree_depth: int,
    ignore_patterns: list[str],
    *,
    record_event: EventRecorder | None = None,
    host_id: int | None = None,
    project_id: int | None = None,
) -> list[dict[str, str]]:
    command = (
        f"find {shlex.quote(project_path)} -mindepth 1 -maxdepth {tree_depth} "
        "-printf '%y\t%P\n' | sort | head -n 200"
    )
    lines, _ = _run_lines(
        executor,
        client,
        command,
        record_event=record_event,
        host_id=host_id,
        project_id=project_id,
        phase="tree",
        allow_partial=True,
    )
    entries = []
    for line in lines:
        kind, _, rel_path = line.partition("\t")
        if not rel_path:
            continue
        entries.append({"type": "dir" if kind == "d" else "file", "path": rel_path})
    return filter_tree_entries(entries, ignore_patterns)


def _collect_project_markers(
    executor: SSHExecutor,
    client: Any,
    project_path: str,
    allowed_markers: list[str],
    ignore_patterns: list[str],
    *,
    record_event: EventRecorder | None = None,
    host_id: int | None = None,
    project_id: int | None = None,
) -> list[dict[str, str]]:
    scan_depth = 2
    command = _build_find_command(project_path, allowed_markers, ignore_patterns, scan_depth)
    lines, _ = _run_lines(
        executor,
        client,
        command,
        record_event=record_event,
        host_id=host_id,
        project_id=project_id,
        phase="marker-discovery",
        allow_partial=True,
    )
    return _parse_marker_results(lines, ignore_patterns).get(project_path, [])


def _collect_project_snapshot(
    executor: SSHExecutor,
    client: Any,
    host_id: int,
    project_path: str,
    marker_files: list[dict[str, str]],
    scan_settings: ScanSettingsPayload,
    general_settings: GeneralSettingsPayload,
    *,
    inventory_entries: list[dict[str, Any]] | None = None,
    host_name: str | None = None,
    host_facts: dict[str, Any] | None = None,
    record_event: EventRecorder | None = None,
    project_id: int | None = None,
) -> ProjectSnapshot:
    last_modified_at = _stat_mtime(
        executor,
        client,
        project_path,
        record_event=record_event,
        host_id=host_id,
        project_id=project_id,
    )
    git_metadata = (
        _collect_git_metadata(
            executor,
            client,
            project_path,
            record_event=record_event,
            host_id=host_id,
            project_id=project_id,
        )
        if scan_settings.collect_recent_git_metadata
        else {"is_git_repo": False}
    )
    project_entries = _filter_inventory_for_project(project_path, inventory_entries or [])
    shallow_tree = None
    if scan_settings.collect_shallow_file_trees:
        shallow_tree = (
            _derive_shallow_tree(
                project_path,
                project_entries,
                general_settings.default_max_tree_depth,
                scan_settings.ignore_patterns,
            )
            if project_entries
            else _collect_shallow_tree(
                executor,
                client,
                project_path,
                general_settings.default_max_tree_depth,
                scan_settings.ignore_patterns,
                record_event=record_event,
                host_id=host_id,
                project_id=project_id,
            )
        )

    project_type = guess_project_type([marker["marker_name"] for marker in marker_files])
    name_guess = guess_project_name(project_path, marker_files)
    metadata_json = build_project_metadata(marker_files, shallow_tree)
    if project_entries:
        metadata_json["inventory_summary"] = _summarize_inventory(project_path, project_entries)
    metadata_json["discovered_via"] = "marker_scan"
    metadata_json["file_inventory_count"] = len(project_entries)
    metadata_json["safe_text_candidates"] = sum(1 for entry in project_entries if entry["entry_type"] == "file" and is_safe_text_path(entry["path"]))
    metadata_json["host_profile"] = host_facts or {}
    metadata_json["setup_kind"] = "marker-project"
    if host_name:
        metadata_json["host_name"] = host_name

    fingerprint = build_project_fingerprint(
        host_id=host_id,
        path=project_path,
        marker_files=marker_files,
        project_type=project_type,
        last_modified_at=last_modified_at.isoformat() if last_modified_at else None,
        repo_branch=git_metadata.get("repo_branch"),
        head_commit=git_metadata.get("head_commit"),
    )

    return ProjectSnapshot(
        host_id=host_id,
        path=project_path,
        name_guess=name_guess,
        project_type=project_type,
        repo_remote=git_metadata.get("repo_remote"),
        repo_branch=git_metadata.get("repo_branch"),
        head_commit=git_metadata.get("head_commit"),
        is_git_repo=bool(git_metadata.get("is_git_repo")),
        last_modified_at=last_modified_at,
        fingerprint=fingerprint,
        marker_files=marker_files,
        shallow_tree_json=shallow_tree,
        metadata_json=metadata_json,
    )


def _build_workspace_snapshot(
    host_id: int,
    *,
    host_name: str,
    root_path: str,
    inventory_entries: list[dict[str, Any]],
    general_settings: GeneralSettingsPayload,
    scan_settings: ScanSettingsPayload,
    host_facts: dict[str, Any] | None = None,
) -> ProjectSnapshot:
    name_guess = _friendly_workspace_name(root_path, inventory_entries, host_name)
    shallow_tree = (
        _derive_shallow_tree(root_path, inventory_entries, general_settings.default_max_tree_depth, scan_settings.ignore_patterns)
        if scan_settings.collect_shallow_file_trees
        else None
    )
    marker_files = _markers_from_inventory(inventory_entries, scan_settings.allowed_marker_files, scan_settings.ignore_patterns).get(root_path, [])
    latest_mtime = max((entry["last_modified_at"] for entry in inventory_entries if entry.get("last_modified_at")), default=None)
    metadata_json = build_project_metadata(marker_files, shallow_tree)
    root_info = _classify_root(root_path)
    project_type = "host-setup" if root_info.get("kind") == "home-root" else "workspace"
    metadata_json.update(
        {
            "discovered_via": "root_inventory",
            "file_inventory_count": len(inventory_entries),
            "safe_text_candidates": sum(1 for entry in inventory_entries if entry["entry_type"] == "file" and is_safe_text_path(entry["path"])),
            "root_path": root_path,
            "root_kind": root_info.get("kind"),
            "inventory_summary": _summarize_inventory(root_path, inventory_entries),
            "host_profile": host_facts or {},
            "host_name": host_name,
            "setup_kind": "home-root" if root_info.get("kind") == "home-root" else "workspace-root",
        }
    )
    return ProjectSnapshot(
        host_id=host_id,
        path=root_path,
        name_guess=name_guess,
        project_type=project_type,
        is_git_repo=False,
        last_modified_at=latest_mtime,
        fingerprint=fingerprint_payload(
            {
                "host_id": host_id,
                "path": root_path,
                "entry_count": len(inventory_entries),
                "latest_mtime": latest_mtime.isoformat() if latest_mtime else None,
                "root_kind": root_info.get("kind"),
                "observed_names": metadata_json["inventory_summary"].get("observed_names"),
            }
        ),
        marker_files=marker_files,
        shallow_tree_json=shallow_tree,
        metadata_json=metadata_json,
    )


def _proxmox_vm_inventory(entries: list[dict[str, Any]]) -> dict[str, Any]:
    qemu_vms = sorted(
        {
            PurePosixPath(entry["path"]).stem
            for entry in entries
            if "/qemu-server/" in entry["path"] and entry["entry_type"] == "file" and entry["path"].endswith(".conf")
        }
    )
    lxc_containers = sorted(
        {
            PurePosixPath(entry["path"]).stem
            for entry in entries
            if "/lxc/" in entry["path"] and entry["entry_type"] == "file" and entry["path"].endswith(".conf")
        }
    )
    return {
        "qemu_vm_ids": qemu_vms,
        "qemu_vm_count": len(qemu_vms),
        "lxc_container_ids": lxc_containers,
        "lxc_container_count": len(lxc_containers),
    }


def _build_proxmox_group_snapshot(
    host: Host,
    *,
    node_name: str,
    root_inventories: list[dict[str, Any]],
    general_settings: GeneralSettingsPayload,
    scan_settings: ScanSettingsPayload,
    host_facts: dict[str, Any] | None = None,
) -> ProjectSnapshot:
    grouped_roots = [item["root_path"] for item in root_inventories]
    entries: list[dict[str, Any]] = []
    for root_inventory in root_inventories:
        entries.extend(root_inventory["entries"])
    entries.sort(key=lambda item: item["path"])
    project_path = f"/etc/pve/nodes/{node_name}"
    shallow_tree = (
        _derive_shallow_tree(project_path, entries, general_settings.default_max_tree_depth + 1, scan_settings.ignore_patterns)
        if scan_settings.collect_shallow_file_trees
        else None
    )
    metadata_json = build_project_metadata([], shallow_tree)
    metadata_json.update(
        {
            "discovered_via": "host_grouping",
            "setup_kind": "proxmox-hypervisor",
            "grouped_roots": grouped_roots,
            "host_name": host.name,
            "host_profile": host_facts or {},
            "inventory_summary": _summarize_inventory(project_path, entries),
            "proxmox_inventory": _proxmox_vm_inventory(entries),
            "file_inventory_count": len(entries),
            "safe_text_candidates": sum(1 for entry in entries if entry["entry_type"] == "file" and is_safe_text_path(entry["path"])),
        }
    )
    latest_mtime = max((entry["last_modified_at"] for entry in entries if entry.get("last_modified_at")), default=None)
    vm_inventory = metadata_json["proxmox_inventory"]
    title = f"{node_name} virtualization host"
    if vm_inventory["qemu_vm_count"] or vm_inventory["lxc_container_count"]:
        title = f"{node_name} VM host"
    return ProjectSnapshot(
        host_id=host.id,
        path=project_path,
        name_guess=title,
        project_type="virtualization",
        is_git_repo=False,
        last_modified_at=latest_mtime,
        fingerprint=fingerprint_payload(
            {
                "host_id": host.id,
                "path": project_path,
                "grouped_roots": grouped_roots,
                "latest_mtime": latest_mtime.isoformat() if latest_mtime else None,
                "proxmox_inventory": vm_inventory,
            }
        ),
        marker_files=[],
        shallow_tree_json=shallow_tree,
        metadata_json=metadata_json,
    )


def _build_grouped_setup_snapshots(
    host: Host,
    *,
    root_inventories: list[dict[str, Any]],
    general_settings: GeneralSettingsPayload,
    scan_settings: ScanSettingsPayload,
    host_facts: dict[str, Any] | None = None,
) -> tuple[list[ProjectSnapshot], set[str], set[str]]:
    proxmox_roots: dict[str, list[dict[str, Any]]] = {}
    proxmox_snippets: list[dict[str, Any]] = []
    suppressed_roots: set[str] = set()

    for root_inventory in root_inventories:
        root_path = root_inventory["root_path"]
        root_info = _classify_root(root_path)
        if root_info.get("kind") == "proxmox-node-config":
            proxmox_roots.setdefault(root_info["node_name"], []).append(root_inventory)
        elif root_info.get("kind") == "proxmox-snippets":
            proxmox_snippets.append(root_inventory)

    snapshots: list[ProjectSnapshot] = []
    consumed_roots: set[str] = set()

    if proxmox_roots:
        primary_node = next(iter(sorted(proxmox_roots)))
        for snippet_root in proxmox_snippets:
            proxmox_roots.setdefault(primary_node, []).append(snippet_root)

        for node_name, grouped_inventories in sorted(proxmox_roots.items()):
            snapshots.append(
                _build_proxmox_group_snapshot(
                    host,
                    node_name=node_name,
                    root_inventories=grouped_inventories,
                    general_settings=general_settings,
                    scan_settings=scan_settings,
                    host_facts=host_facts,
                )
            )
            consumed_roots.update(item["root_path"] for item in grouped_inventories)

        if consumed_roots:
            for root_inventory in root_inventories:
                if _classify_root(root_inventory["root_path"]).get("kind") == "low-signal-root":
                    suppressed_roots.add(root_inventory["root_path"])

    return snapshots, consumed_roots, suppressed_roots


def _child_snapshot_brief(snapshot: ProjectSnapshot) -> dict[str, Any]:
    metadata = snapshot.metadata_json or {}
    inventory_summary = metadata.get("inventory_summary") or {}
    return {
        "name": snapshot.name_guess,
        "path": snapshot.path,
        "project_type": snapshot.project_type,
        "setup_kind": metadata.get("setup_kind"),
        "discovered_via": metadata.get("discovered_via"),
        "is_git_repo": snapshot.is_git_repo,
        "marker_names": sorted({marker["marker_name"] for marker in snapshot.marker_files}),
        "file_inventory_count": metadata.get("file_inventory_count", 0),
        "safe_text_candidates": metadata.get("safe_text_candidates", 0),
        "observed_names": inventory_summary.get("observed_names", []),
        "grouped_roots": metadata.get("grouped_roots", []),
        "last_modified_at": snapshot.last_modified_at.isoformat() if snapshot.last_modified_at else None,
    }


def _build_host_summary_inventory(root_inventories: list[dict[str, Any]]) -> dict[str, Any]:
    root_summaries = [
        _summarize_inventory(root_inventory["root_path"], root_inventory["entries"])
        for root_inventory in root_inventories
        if root_inventory["entries"]
    ]
    observed_names: list[str] = []
    seen_names: set[str] = set()
    for summary in root_summaries:
        for name in summary.get("observed_names", []):
            if name in seen_names:
                continue
            seen_names.add(name)
            observed_names.append(name)

    return {
        "root_count": len(root_inventories),
        "entry_count": sum(summary.get("entry_count", 0) for summary in root_summaries),
        "file_count": sum(summary.get("file_count", 0) for summary in root_summaries),
        "dir_count": sum(summary.get("dir_count", 0) for summary in root_summaries),
        "safe_text_candidates": sum(summary.get("safe_text_candidates", 0) for summary in root_summaries),
        "observed_names": observed_names[:20],
        "root_summaries": root_summaries,
    }


def _build_host_summary_tree(
    root_inventories: list[dict[str, Any]],
    *,
    per_root_limit: int = 6,
    total_limit: int = 120,
) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for root_inventory in root_inventories:
        root_path = root_inventory["root_path"]
        entries.append({"type": "dir", "path": root_path})
        for item in _top_level_entries(root_path, root_inventory["entries"], limit=per_root_limit):
            joined_path = f"{root_path.rstrip('/')}/{item['name']}" if root_path != "/" else f"/{item['name']}"
            entries.append({"type": item["type"], "path": joined_path})
            if len(entries) >= total_limit:
                return entries[:total_limit]
    return entries[:total_limit]


def _build_host_summary_snapshot(
    host: Host,
    *,
    child_snapshots: list[ProjectSnapshot],
    root_inventories: list[dict[str, Any]],
    host_facts: dict[str, Any],
) -> ProjectSnapshot:
    inventory_summary = _build_host_summary_inventory(root_inventories)
    child_projects = [_child_snapshot_brief(snapshot) for snapshot in child_snapshots]
    project_type_counts = Counter(snapshot.project_type for snapshot in child_snapshots if snapshot.project_type)
    marker_names = sorted(
        {
            marker["marker_name"]
            for snapshot in child_snapshots
            for marker in snapshot.marker_files
        }
    )
    marker_files = []
    for snapshot in child_snapshots:
        marker_files.extend(snapshot.marker_files)

    proxmox_inventory = next(
        (
            snapshot.metadata_json.get("proxmox_inventory")
            for snapshot in child_snapshots
            if snapshot.metadata_json and snapshot.metadata_json.get("proxmox_inventory")
        ),
        {},
    )
    grouped_roots = [root_inventory["root_path"] for root_inventory in root_inventories]
    last_modified_at = max(
        (snapshot.last_modified_at for snapshot in child_snapshots if snapshot.last_modified_at),
        default=None,
    )
    repo_paths = [snapshot.path for snapshot in child_snapshots if snapshot.is_git_repo]
    project_paths = [snapshot.path for snapshot in child_snapshots]
    classification = classify_machine(
        host_name=host.name,
        metadata={
            "setup_kind": "host-summary",
            "grouped_roots": grouped_roots,
            "inventory_summary": inventory_summary,
            "child_projects": child_projects,
            "project_counts": {
                "total": len(child_snapshots),
                "git_repos": sum(1 for snapshot in child_snapshots if snapshot.is_git_repo),
                "by_type": dict(project_type_counts),
            },
            "marker_names": marker_names,
        },
        host_facts=host_facts,
        guest_inventory=(host_facts or {}).get("guest_inventory") or [],
        service_inventory=(host_facts or {}).get("selected_running_services") or [],
    )
    metadata_json = {
        "discovered_via": "host_summary",
        "setup_kind": "host-summary",
        "host_name": host.name,
        "host_address": host.address,
        "host_profile": host_facts,
        "grouped_roots": grouped_roots,
        "inventory_summary": inventory_summary,
        "child_projects": child_projects,
        "project_counts": {
            "total": len(child_snapshots),
            "git_repos": sum(1 for snapshot in child_snapshots if snapshot.is_git_repo),
            "by_type": dict(project_type_counts),
        },
        "marker_names": marker_names,
        "proxmox_inventory": proxmox_inventory or {},
        "machine_classification": classification.model_dump(mode="json"),
        "project_paths": project_paths,
        "repo_paths": repo_paths,
        "detected_app_stack_markers": marker_names,
        "selected_services": (host_facts or {}).get("selected_running_services") or [],
        "storage_summary": (host_facts or {}).get("storage_summary") or [],
        "file_inventory_count": inventory_summary["entry_count"],
        "safe_text_candidates": inventory_summary["safe_text_candidates"],
    }
    return ProjectSnapshot(
        host_id=host.id,
        path=f"host://{host.id}",
        name_guess=host.name,
        project_type="host",
        repo_remote=None,
        repo_branch=None,
        head_commit=None,
        is_git_repo=False,
        last_modified_at=last_modified_at,
        fingerprint=fingerprint_payload(
            {
                "host_id": host.id,
                "host_name": host.name,
                "host_address": host.address,
                "grouped_roots": grouped_roots,
                "child_projects": child_projects,
                "host_profile": {
                    "os_pretty_name": host_facts.get("os_pretty_name"),
                    "cpu_count": host_facts.get("cpu_count"),
                    "memory_gib": host_facts.get("memory_gib"),
                    "root_size_gib": host_facts.get("root_size_gib"),
                    "virtualization": host_facts.get("virtualization"),
                },
            }
        ),
        marker_files=marker_files,
        shallow_tree_json=_build_host_summary_tree(root_inventories),
        metadata_json=metadata_json,
    )


def _project_grouped_roots(project: Project) -> list[str]:
    metadata = project.metadata_json or {}
    roots = metadata.get("grouped_roots")
    if isinstance(roots, list):
        return [root for root in roots if isinstance(root, str)]
    return []


def _project_matches_path(project: Project, path: str) -> bool:
    metadata = project.metadata_json or {}
    if metadata.get("setup_kind") == "host-summary":
        return any(_matches_path_prefix(path, root_path) for root_path in _project_grouped_roots(project))
    return _matches_path_prefix(path, project.path)


def _project_match_priority(project: Project) -> int:
    grouped_roots = _project_grouped_roots(project)
    if grouped_roots:
        return max(len(root_path) for root_path in grouped_roots)
    return len(project.path or "")


def _collect_root_inventory(
    executor: SSHExecutor,
    client: Any,
    *,
    host_id: int,
    root_path: str,
    scan_settings: ScanSettingsPayload,
    modified_since: datetime | None = None,
    record_event: EventRecorder | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    command = _build_inventory_command(root_path, scan_settings.ignore_patterns, modified_since=modified_since)
    lines, warning = _run_lines(
        executor,
        client,
        command,
        record_event=record_event,
        host_id=host_id,
        phase="inventory",
        allow_partial=True,
    )
    entries = _parse_inventory_results(lines, root_path=root_path, ignore_patterns=scan_settings.ignore_patterns)
    _emit_event(
        record_event,
        level="info",
        event_type="root_inventory_collected",
        message=f"Captured {len(entries)} inventory entries under {root_path}.",
        host_id=host_id,
        details={"root_path": root_path, "entries": len(entries)},
    )
    return entries, warning


def _match_project_for_path(path: str, projects: list[Project]) -> Project | None:
    for project in sorted(projects, key=_project_match_priority, reverse=True):
        if _project_matches_path(project, path):
            return project
    return None


def scan_host_full(
    executor: SSHExecutor,
    client: Any,
    host: Host,
    general_settings: GeneralSettingsPayload,
    scan_settings: ScanSettingsPayload,
    *,
    record_event: EventRecorder | None = None,
) -> dict[str, Any]:
    candidates: dict[str, list[dict[str, str]]] = {}
    warnings: list[str] = []
    root_inventories: list[dict[str, Any]] = []
    host_facts: dict[str, Any] = {}
    try:
        host_facts = _collect_host_facts(executor, client, host=host, record_event=record_event)
    except Exception as exc:
        warnings.append(f"Host facts unavailable for {host.name}: {exc}")

    for root_path in _enabled_roots(host):
        try:
            inventory_entries, warning = _collect_root_inventory(
                executor,
                client,
                host_id=host.id,
                root_path=root_path,
                scan_settings=scan_settings,
                record_event=record_event,
            )
            root_inventories.append({"root_path": root_path, "entries": inventory_entries})
            for path, markers in _markers_from_inventory(
                inventory_entries,
                scan_settings.allowed_marker_files,
                scan_settings.ignore_patterns,
            ).items():
                candidates.setdefault(path, [])
                candidates[path].extend(markers)
            if warning:
                warnings.append(f"Inventory warning for {root_path}: {warning}")
        except SSHCommandError as exc:
            warnings.append(f"Skipped root {root_path}: {exc}")

    grouped_snapshots, consumed_roots, suppressed_roots = _build_grouped_setup_snapshots(
        host,
        root_inventories=root_inventories,
        general_settings=general_settings,
        scan_settings=scan_settings,
        host_facts=host_facts,
    )

    child_snapshots = list(grouped_snapshots)
    for root_inventory in root_inventories:
        root_path = root_inventory["root_path"]
        inventory_entries = root_inventory["entries"]
        if not inventory_entries:
            continue
        if root_path in consumed_roots or root_path in suppressed_roots:
            continue
        should_add_workspace = not any(
            _matches_path_prefix(project_path, root_path) and project_path != root_path for project_path in candidates
        )
        if should_add_workspace and root_path not in candidates:
            candidates[root_path] = []

    child_snapshots.extend(
        _collect_project_snapshot(
            executor,
            client,
            host.id,
            project_path,
            markers,
            scan_settings,
            general_settings,
            inventory_entries=next(
                (
                    root_inventory["entries"]
                    for root_inventory in root_inventories
                    if _matches_path_prefix(project_path, root_inventory["root_path"])
                ),
                [],
            ),
            host_name=host.name,
            host_facts=host_facts,
            record_event=record_event,
        )
        for project_path, markers in sorted(candidates.items())
        if markers and project_path not in consumed_roots and project_path not in suppressed_roots
    )
    for root_inventory in root_inventories:
        root_path = root_inventory["root_path"]
        if root_path in consumed_roots or root_path in suppressed_roots:
            continue
        if root_path in candidates and not candidates[root_path]:
            child_snapshots.append(
                _build_workspace_snapshot(
                    host.id,
                    host_name=host.name,
                    root_path=root_path,
                    inventory_entries=root_inventory["entries"],
                    general_settings=general_settings,
                    scan_settings=scan_settings,
                    host_facts=host_facts,
                )
            )
    host_snapshot = _build_host_summary_snapshot(
        host,
        child_snapshots=child_snapshots,
        root_inventories=root_inventories,
        host_facts=host_facts,
    )
    return {
        "snapshots": [host_snapshot],
        "unchanged_project_ids": [],
        "warnings": warnings,
        "root_inventories": root_inventories,
        "host_facts": host_facts,
    }


def scan_host_incremental(
    executor: SSHExecutor,
    client: Any,
    host: Host,
    general_settings: GeneralSettingsPayload,
    scan_settings: ScanSettingsPayload,
    last_scan_at: datetime | None,
    *,
    record_event: EventRecorder | None = None,
) -> dict[str, Any]:
    if last_scan_at is None:
        return scan_host_full(
            executor,
            client,
            host,
            general_settings,
            scan_settings,
            record_event=record_event,
        )

    warnings: list[str] = []
    enabled_roots = _enabled_roots(host)
    root_inventories: list[dict[str, Any]] = []
    host_facts: dict[str, Any] = {}
    try:
        host_facts = _collect_host_facts(executor, client, host=host, record_event=record_event)
    except Exception as exc:
        warnings.append(f"Host facts unavailable for {host.name}: {exc}")
    host_summary_project = next(
        (
            project
            for project in host.projects
            if (project.metadata_json or {}).get("setup_kind") == "host-summary"
        ),
        None,
    )
    for root_path in enabled_roots:
        try:
            inventory_entries, warning = _collect_root_inventory(
                executor,
                client,
                host_id=host.id,
                root_path=root_path,
                scan_settings=scan_settings,
                modified_since=last_scan_at,
                record_event=record_event,
            )
            root_inventories.append({"root_path": root_path, "entries": inventory_entries})
            if warning:
                warnings.append(f"Inventory warning for {root_path}: {warning}")
        except SSHCommandError as exc:
            warnings.append(f"Skipped root {root_path}: {exc}")

    has_changes = any(root_inventory["entries"] for root_inventory in root_inventories)
    if has_changes or not host_summary_project:
        full_result = scan_host_full(
            executor,
            client,
            host,
            general_settings,
            scan_settings,
            record_event=record_event,
        )
        full_result["warnings"] = [*warnings, *full_result.get("warnings", [])]
        return full_result

    return {
        "snapshots": [],
        "unchanged_project_ids": [host_summary_project.id],
        "warnings": warnings,
        "root_inventories": [],
        "host_facts": host_facts,
    }


def _host_query(host_ids: list[int] | None = None) -> Select[tuple[Host]]:
    query = (
        select(Host)
        .options(
            selectinload(Host.roots),
            selectinload(Host.projects).selectinload(Project.markers),
        )
        .order_by(Host.name)
    )
    if host_ids:
        query = query.where(Host.id.in_(host_ids))
    return query


def _upsert_project(session: Session, snapshot: ProjectSnapshot, seen_at: datetime) -> tuple[Project, list[str]]:
    project = session.execute(
        select(Project).where(Project.host_id == snapshot.host_id, Project.path == snapshot.path).options(selectinload(Project.markers))
    ).scalar_one_or_none()

    old_payload = None
    if project is None:
        project = Project(
            host_id=snapshot.host_id,
            path=snapshot.path,
            first_seen_at=seen_at,
        )
        session.add(project)
        session.flush()
        changed_fields = ["new"]
    else:
        old_payload = {
            "name_guess": project.name_guess,
            "project_type": project.project_type,
            "repo_remote": project.repo_remote,
            "repo_branch": project.repo_branch,
            "head_commit": project.head_commit,
            "is_git_repo": project.is_git_repo,
            "fingerprint": project.fingerprint,
        }
        changed_fields = summarize_changes(
            old_payload,
            {
                "name_guess": snapshot.name_guess,
                "project_type": snapshot.project_type,
                "repo_remote": snapshot.repo_remote,
                "repo_branch": snapshot.repo_branch,
                "head_commit": snapshot.head_commit,
                "is_git_repo": snapshot.is_git_repo,
                "fingerprint": snapshot.fingerprint,
            },
        )

    project.name_guess = snapshot.name_guess
    project.project_type = snapshot.project_type
    project.repo_remote = snapshot.repo_remote
    project.repo_branch = snapshot.repo_branch
    project.head_commit = snapshot.head_commit
    project.is_git_repo = snapshot.is_git_repo
    project.last_modified_at = snapshot.last_modified_at
    project.fingerprint = snapshot.fingerprint
    project.last_seen_at = seen_at
    project.shallow_tree_json = snapshot.shallow_tree_json
    project.metadata_json = snapshot.metadata_json

    project.markers.clear()
    project.markers.extend(
        ProjectMarker(marker_name=marker["marker_name"], marker_path=marker["marker_path"])
        for marker in snapshot.marker_files
    )
    session.flush()
    return project, changed_fields


def _sync_project_files(
    session: Session,
    *,
    host: Host,
    projects: list[Project],
    root_inventories: list[dict[str, Any]],
    seen_at: datetime,
    full_scan: bool,
) -> tuple[int, int]:
    existing_by_path = {
        record.path: record
        for record in session.execute(select(ProjectFile).where(ProjectFile.host_id == host.id)).scalars().all()
    }
    scanned_roots = [item["root_path"] for item in root_inventories]
    seen_paths: set[str] = set()
    created = 0
    updated = 0

    for root_inventory in root_inventories:
        root_path = root_inventory["root_path"]
        for entry in root_inventory["entries"]:
            seen_paths.add(entry["path"])
            project = _match_project_for_path(entry["path"], projects)
            record = existing_by_path.get(entry["path"])
            if record is None:
                record = ProjectFile(host_id=host.id, path=entry["path"])
                session.add(record)
                existing_by_path[entry["path"]] = record
                created += 1
            else:
                updated += 1

            previous_fingerprint = record.fingerprint
            record.project_id = project.id if project else None
            record.root_path = root_path
            record.relative_path = entry["relative_path"]
            record.entry_type = entry["entry_type"]
            record.size_bytes = entry["size_bytes"]
            record.last_modified_at = entry["last_modified_at"]
            record.fingerprint = entry["fingerprint"]
            record.last_seen_at = seen_at
            record.metadata_json = entry["metadata_json"]
            if previous_fingerprint and previous_fingerprint != entry["fingerprint"]:
                record.content_sample = None

    if full_scan:
        for path, record in list(existing_by_path.items()):
            if any(_matches_path_prefix(path, root) for root in scanned_roots) and path not in seen_paths:
                session.delete(record)

    session.flush()
    return created, updated


def _delete_stale_projects(
    session: Session,
    *,
    host: Host,
    active_paths: set[str],
    scanned_roots: list[str],
) -> int:
    if not scanned_roots:
        return 0

    stale_projects = [
        project
        for project in session.execute(select(Project).where(Project.host_id == host.id)).scalars().all()
        if project.path not in active_paths
        and (
            (project.metadata_json or {}).get("setup_kind") == "host-summary"
            or any(_path_overlaps_root(project.path, root_path) for root_path in scanned_roots)
        )
    ]
    for project in stale_projects:
        session.delete(project)
    session.flush()
    return len(stale_projects)


def _reviewable_files_for_project(project: Project, ignore_patterns: list[str]) -> list[ProjectFile]:
    def file_priority(file_record: ProjectFile) -> tuple[int, int, str]:
        relative_path = (file_record.relative_path or "").lower()
        score = 0
        high_signal_fragments = [
            ".conf",
            ".service",
            ".yaml",
            ".yml",
            ".toml",
            ".json",
            "compose",
            "dockerfile",
            "readme",
            "cloudflared",
            "yagna",
            "golem",
            "provider",
            "qemu-server",
            "/lxc/",
        ]
        for fragment in high_signal_fragments:
            if fragment in relative_path:
                score += 1
        if relative_path.endswith(".sh"):
            score += 1
        stale_penalty = 0 if (file_record.last_ai_reviewed_at is None or file_record.last_ai_reviewed_fingerprint != file_record.fingerprint) else 1
        return (-score, stale_penalty, file_record.relative_path)

    candidates = [
        file_record
        for file_record in project.files
        if file_record.entry_type == "file"
        and is_safe_text_path(file_record.path)
        and not should_ignore_path(file_record.path, ignore_patterns)
    ]
    return sorted(candidates, key=file_priority)


def _chunk_reviewable_files(files: list[ProjectFile], *, batch_size: int = REVIEW_BATCH_SIZE) -> list[list[ProjectFile]]:
    if batch_size <= 0:
        batch_size = REVIEW_BATCH_SIZE
    return [files[index : index + batch_size] for index in range(0, len(files), batch_size)]


def _fallback_batch_review(batch_files: list[ProjectFile], *, batch_index: int, batch_count: int) -> dict[str, Any]:
    relative_paths = [file_record.relative_path for file_record in batch_files]
    summary = (
        f"Batch {batch_index} of {batch_count} reviewed {len(batch_files)} file(s): "
        + ", ".join(relative_paths[:4])
    )
    if len(relative_paths) > 4:
        summary = f"{summary}, and {len(relative_paths) - 4} more."
    return {
        "batch_index": batch_index,
        "paths_reviewed": relative_paths,
        "summary": summary,
        "notable_points": [f"Reviewed {path}" for path in relative_paths[:6]],
    }


def _read_file_sample(
    executor: SSHExecutor,
    client: Any,
    *,
    file_record: ProjectFile,
    max_read_bytes: int,
    host_id: int,
    project_id: int | None,
    record_event: EventRecorder | None = None,
) -> str:
    command = f"head -c {max_read_bytes} {shlex.quote(file_record.path)}"
    _, stdout, _ = _run_command(
        executor,
        client,
        command,
        record_event=record_event,
        host_id=host_id,
        project_id=project_id,
        phase="file-read",
        allow_partial=True,
    )
    _emit_event(
        record_event,
        level="info",
        event_type="file_sampled",
        message=f"Sampled {file_record.relative_path}.",
        host_id=host_id,
        project_id=project_id,
        details={"path": file_record.path, "bytes_requested": max_read_bytes},
    )
    return stdout


def _interpret_project_with_review(
    session: Session,
    config: AppConfig,
    executor: SSHExecutor,
    client: Any,
    *,
    host: Host,
    project: Project,
    scan_settings: ScanSettingsPayload,
    record_event: EventRecorder | None = None,
    stream_callback: Callable[[str], None] | None = None,
    stop_event: Event | None = None,
) -> ProjectInterpretation:
    from app.lmstudio.service import build_project_packet, interpret_project, review_project_batch

    ensure_not_cancelled(stop_event)
    reviewable_files = _reviewable_files_for_project(project, scan_settings.ignore_patterns)
    if not reviewable_files:
        return interpret_project(session, config, project.id, stream_callback=stream_callback)

    review_batches = _chunk_reviewable_files(reviewable_files)
    _emit_event(
        record_event,
        level="info",
        event_type="ai_review_planned",
        message=f"Planned batched review of {len(reviewable_files)} file(s) across {len(review_batches)} batch(es) for {project.path}.",
        host_id=host.id,
        project_id=project.id,
        details={"reviewable_files": len(reviewable_files), "batch_count": len(review_batches)},
    )
    if stream_callback:
        stream_callback(
            f"\n[AICMS] Reviewing {len(reviewable_files)} safe file(s) across {len(review_batches)} batch(es) before final synthesis.\n"
        )

    review_time = utcnow()
    batch_reviews: list[dict[str, Any]] = []
    reused_count = 0
    resampled_count = 0

    for batch_index, batch_files in enumerate(review_batches, start=1):
        ensure_not_cancelled(stop_event)
        relative_paths = [file_record.relative_path for file_record in batch_files]
        _emit_event(
            record_event,
            level="info",
            event_type="ai_review_batch_started",
            message=f"Reviewing batch {batch_index}/{len(review_batches)} for {project.path}.",
            host_id=host.id,
            project_id=project.id,
            details={"paths_reviewed": relative_paths},
        )
        if stream_callback:
            stream_callback(
                f"\n[AICMS] Reviewing batch {batch_index}/{len(review_batches)}: {', '.join(relative_paths[:4])}\n"
            )

        for file_record in batch_files:
            ensure_not_cancelled(stop_event)
            needs_refresh = (
                not file_record.content_sample
                or file_record.last_ai_reviewed_fingerprint != file_record.fingerprint
            )
            if needs_refresh:
                file_record.content_sample = _read_file_sample(
                    executor,
                    client,
                    file_record=file_record,
                    max_read_bytes=scan_settings.max_read_bytes,
                    host_id=host.id,
                    project_id=project.id,
                    record_event=record_event,
                )
                resampled_count += 1
            else:
                reused_count += 1
                _emit_event(
                    record_event,
                    level="info",
                    event_type="ai_file_reused",
                    message=f"Reused unchanged review sample for {file_record.relative_path}.",
                    host_id=host.id,
                    project_id=project.id,
                    details={"path": file_record.path},
                )
            file_record.last_ai_reviewed_at = review_time
            file_record.last_ai_reviewed_fingerprint = file_record.fingerprint

        session.flush()
        ensure_not_cancelled(stop_event)

        try:
            batch_review = review_project_batch(
                session,
                config,
                project=project,
                batch_files=batch_files,
                batch_index=batch_index,
                batch_count=len(review_batches),
            )
            batch_entry = {
                "batch_index": batch_index,
                "paths_reviewed": relative_paths,
                "summary": batch_review.batch_summary,
                "notable_points": batch_review.notable_points,
            }
            _emit_event(
                record_event,
                level="info",
                event_type="ai_review_batch_completed",
                message=f"Completed batch {batch_index}/{len(review_batches)} review for {project.path}.",
                host_id=host.id,
                project_id=project.id,
                details=batch_entry,
            )
            if stream_callback:
                stream_callback(f"[AICMS] Batch {batch_index}/{len(review_batches)} summary: {batch_review.batch_summary}\n")
        except Exception as exc:
            ensure_not_cancelled(stop_event)
            batch_entry = _fallback_batch_review(
                batch_files,
                batch_index=batch_index,
                batch_count=len(review_batches),
            )
            _emit_event(
                record_event,
                level="warning",
                event_type="ai_review_batch_fallback",
                message=f"Batch {batch_index}/{len(review_batches)} review failed, using fallback summary: {exc}",
                host_id=host.id,
                project_id=project.id,
                details=batch_entry,
            )
            if stream_callback:
                stream_callback(
                    f"[AICMS] Batch {batch_index}/{len(review_batches)} fell back to filename-based summary.\n"
                )
        batch_reviews.append(batch_entry)

    session.flush()
    ensure_not_cancelled(stop_event)
    final_packet = build_project_packet(project, files=project.files)
    final_packet["reviewed_files"] = []
    final_packet["batch_reviews"] = batch_reviews
    final_packet["review_coverage"] = {
        "reviewed_file_count": len(reviewable_files),
        "batch_count": len(review_batches),
        "resampled_file_count": resampled_count,
        "reused_file_count": reused_count,
    }
    if stream_callback:
        stream_callback("\n[AICMS] Merging batch reviews into the final project summary...\n")

    interpretation = interpret_project(
        session,
        config,
        project.id,
        project_packet_override=final_packet,
        stream_callback=stream_callback,
    )
    _emit_event(
        record_event,
        level="info",
        event_type="ai_interpretation_completed",
        message=f"Stored LM Studio interpretation for {project.path}.",
        host_id=host.id,
        project_id=project.id,
        details={
            "reviewed_file_count": len(reviewable_files),
            "batch_count": len(review_batches),
        },
    )
    return interpretation


def _maybe_interpret_project_during_scan(
    session: Session,
    config: AppConfig,
    executor: SSHExecutor,
    client: Any,
    *,
    host: Host,
    project: Project,
    scan_settings: ScanSettingsPayload,
    record_event: EventRecorder | None = None,
    stop_event: Event | None = None,
) -> None:
    try:
        ensure_not_cancelled(stop_event)
        mcp_settings = get_settings(session, config, MCP_SETTINGS_KEY, MCPSettingsPayload)
        profile = generate_machine_profile(
            session,
            config,
            project.id,
            stream_callback=None,
            include_enrichment=mcp_settings.auto_enrich_after_scan,
        )
        _emit_event(
            record_event,
            level="info",
            event_type="machine_profile_refreshed",
            message=f"Refreshed machine profile for {project.path}.",
            host_id=host.id,
            project_id=project.id,
            details={
                "machine_kind": profile.machine_kind,
                "story_template_type": profile.story_template_type,
            },
        )
    except JobCancelled:
        raise
    except Exception as exc:
        _emit_event(
            record_event,
            level="warning",
            event_type="machine_profile_skipped",
            message=str(exc),
            host_id=host.id,
            project_id=project.id,
        )


def interpret_project_with_live_review(session: Session, config: AppConfig, project_id: int) -> MachineProfile:
    return interpret_project_with_live_review_streaming(session, config, project_id, stream_callback=None)


def interpret_project_with_live_review_streaming(
    session: Session,
    config: AppConfig,
    project_id: int,
    *,
    stream_callback: Callable[[str], None] | None = None,
    stop_event: Event | None = None,
) -> MachineProfile:
    ensure_not_cancelled(stop_event)
    if stream_callback:
        stream_callback("[AICMS] Building deterministic machine facts and profile structure first.\n")
    return generate_machine_profile(session, config, project_id, stream_callback=stream_callback, include_enrichment=True)


def _set_scan_control(app: FastAPI, scan_id: int, **payload: Any) -> None:
    with app.state.scan_lock:
        state = dict(app.state.scan_controls.get(scan_id, {}))
        state.update(payload)
        app.state.scan_controls[scan_id] = state


def _pop_scan_control(app: FastAPI, scan_id: int) -> dict[str, Any]:
    with app.state.scan_lock:
        return app.state.scan_controls.pop(scan_id, {})


def _get_scan_control(app: FastAPI, scan_id: int) -> dict[str, Any]:
    with app.state.scan_lock:
        return dict(app.state.scan_controls.get(scan_id, {}))


def _set_interpretation_run_state(app: FastAPI, project_id: int, **payload: Any) -> None:
    with app.state.interpretation_lock:
        state = dict(app.state.interpretation_runs.get(project_id, {}))
        state.update(payload)
        state["updated_at"] = utcnow().isoformat()
        app.state.interpretation_runs[project_id] = state


def _set_interpretation_control(app: FastAPI, project_id: int, **payload: Any) -> None:
    with app.state.interpretation_lock:
        state = dict(app.state.interpretation_controls.get(project_id, {}))
        state.update(payload)
        app.state.interpretation_controls[project_id] = state


def _pop_interpretation_control(app: FastAPI, project_id: int) -> dict[str, Any]:
    with app.state.interpretation_lock:
        return app.state.interpretation_controls.pop(project_id, {})


def _get_interpretation_control(app: FastAPI, project_id: int) -> dict[str, Any]:
    with app.state.interpretation_lock:
        return dict(app.state.interpretation_controls.get(project_id, {}))


def _append_interpretation_output(app: FastAPI, project_id: int, chunk: str) -> None:
    with app.state.interpretation_lock:
        state = dict(app.state.interpretation_runs.get(project_id, {}))
        state["status"] = "running"
        state["output"] = f"{state.get('output', '')}{chunk}"[-20000:]
        state["updated_at"] = utcnow().isoformat()
        app.state.interpretation_runs[project_id] = state


def queue_project_interpretation(app: FastAPI, project_id: int) -> None:
    stop_event = Event()
    _set_interpretation_run_state(app, project_id, status="queued", output="", error=None)
    _set_interpretation_control(app, project_id, stop_event=stop_event, future=None)
    future = app.state.job_runner.submit(run_project_interpretation_job, app, project_id)
    _set_interpretation_control(app, project_id, future=future)


def request_project_interpretation_stop(app: FastAPI, project_id: int) -> str:
    control = _get_interpretation_control(app, project_id)
    if not control:
        return "No active profile refresh job was found for this project."

    future = control.get("future")
    stop_event = control.get("stop_event")
    if future is not None and future.cancel():
        if isinstance(stop_event, Event):
            stop_event.set()
        _set_interpretation_run_state(app, project_id, status="cancelled", error="Profile refresh stopped by user.")
        _pop_interpretation_control(app, project_id)
        return f"Cancelled queued profile refresh for project #{project_id}."

    if isinstance(stop_event, Event):
        stop_event.set()
    _set_interpretation_run_state(app, project_id, status="cancelling", error=None)
    return f"Stop requested for profile refresh on project #{project_id}."


def run_project_interpretation_job(app: FastAPI, project_id: int) -> None:
    config: AppConfig = app.state.config
    control = _get_interpretation_control(app, project_id)
    stop_event = control.get("stop_event") if control else None
    try:
        ensure_not_cancelled(stop_event, "Profile refresh stopped by user.")
        _set_interpretation_run_state(app, project_id, status="running", output="", error=None)
        with session_scope(app.state.session_factory) as session:
            interpretation = interpret_project_with_live_review_streaming(
                session,
                config,
                project_id,
                stream_callback=lambda chunk: (
                    ensure_not_cancelled(stop_event, "Profile refresh stopped by user."),
                    _append_interpretation_output(app, project_id, chunk),
                )[-1],
                stop_event=stop_event,
            )
            _set_interpretation_run_state(
                app,
                project_id,
                status="completed",
                interpretation_id=interpretation.id,
            )
    except JobCancelled as exc:
        _set_interpretation_run_state(app, project_id, status="cancelled", error=str(exc))
    except Exception as exc:
        _set_interpretation_run_state(app, project_id, status="failed", error=str(exc))
    finally:
        _pop_interpretation_control(app, project_id)


def queue_scan(app: FastAPI, scan_type: str, host_ids: list[int] | None = None) -> int:
    with session_scope(app.state.session_factory) as session:
        scan = Scan(scan_type=scan_type, status="queued", started_at=utcnow())
        session.add(scan)
        session.flush()
        record_scan_event(
            session,
            scan_id=scan.id,
            level="info",
            event_type="scan_queued",
            message=f"Queued {scan_type} scan.",
            details={"host_ids": host_ids or []},
        )
        scan_id = scan.id

    stop_event = Event()
    _set_scan_control(app, scan_id, stop_event=stop_event, future=None)
    future = app.state.job_runner.submit(run_scan_job, app, scan_id, host_ids)
    _set_scan_control(app, scan_id, future=future)
    return scan_id


def request_scan_stop(app: FastAPI, scan_id: int) -> str:
    control = _get_scan_control(app, scan_id)
    if not control:
        return "No active scan job was found."

    future = control.get("future")
    stop_event = control.get("stop_event")
    if future is not None and future.cancel():
        if isinstance(stop_event, Event):
            stop_event.set()
        with session_scope(app.state.session_factory) as session:
            scan = session.get(Scan, scan_id)
            if scan is not None:
                scan.status = "cancelled"
                scan.finished_at = utcnow()
                scan.summary = "Scan stopped by user before it started."
                record_scan_event(
                    session,
                    scan_id=scan.id,
                    level="warning",
                    event_type="scan_cancelled",
                    message=scan.summary,
                )
        _pop_scan_control(app, scan_id)
        return f"Cancelled queued scan #{scan_id}."

    if isinstance(stop_event, Event):
        stop_event.set()
    with session_scope(app.state.session_factory) as session:
        scan = session.get(Scan, scan_id)
        if scan is not None and scan.status in {"queued", "running"}:
            scan.status = "cancelling"
            record_scan_event(
                session,
                scan_id=scan.id,
                level="warning",
                event_type="scan_stop_requested",
                message=f"Stop requested for scan #{scan_id}.",
            )
    return f"Stop requested for scan #{scan_id}."


def reconcile_orphaned_scan(app: FastAPI, session: Session, scan: Scan) -> bool:
    if scan.status not in {"queued", "running", "cancelling"}:
        return False

    control = _get_scan_control(app, scan.id)
    if control:
        return False

    scan.finished_at = utcnow()
    if scan.status == "cancelling":
        scan.status = "cancelled"
        scan.summary = scan.summary or "Scan stop was requested, but the worker was no longer active. Marked cancelled."
        record_scan_event(
            session,
            scan_id=scan.id,
            level="warning",
            event_type="scan_cancelled",
            message=scan.summary,
        )
    else:
        scan.status = "failed"
        scan.errors_count += 1
        scan.summary = scan.summary or "Scan worker was no longer active. Marked failed."
        record_scan_event(
            session,
            scan_id=scan.id,
            level="error",
            event_type="scan_failed",
            message=scan.summary,
        )
    session.flush()
    return True


def run_scan_job(app: FastAPI, scan_id: int, host_ids: list[int] | None = None) -> None:
    config: AppConfig = app.state.config
    control = _get_scan_control(app, scan_id)
    stop_event = control.get("stop_event") if control else None
    executor = SSHExecutor(
        connect_timeout=config.ssh_connect_timeout_seconds,
        command_timeout=config.ssh_command_timeout_seconds,
    )
    executor.stop_event = stop_event

    with session_scope(app.state.session_factory) as session:
        general_settings = get_settings(session, config, GENERAL_SETTINGS_KEY, GeneralSettingsPayload)
        scan_settings = get_settings(session, config, SCAN_SETTINGS_KEY, ScanSettingsPayload)
        profile_settings = get_settings(session, config, PROFILE_SETTINGS_KEY, ProfileSettingsPayload)
        scan = session.get(Scan, scan_id)
        if scan is None:
            return
        ensure_not_cancelled(stop_event, "Scan stopped by user.")
        scan.status = "running"
        record_scan_event(
            session,
            scan_id=scan.id,
            level="info",
            event_type="scan_started",
            message=f"Started {scan.scan_type} scan.",
        )
        session.commit()

    try:
        with session_scope(app.state.session_factory) as session:
            scan = session.get(Scan, scan_id)
            if scan is None:
                return

            def live_event(
                *,
                level: str,
                event_type: str,
                message: str,
                host_id: int | None = None,
                project_id: int | None = None,
                details: dict[str, Any] | None = None,
            ) -> None:
                record_scan_event(
                    session,
                    scan_id=scan.id,
                    level=level,
                    event_type=event_type,
                    message=message,
                    host_id=host_id,
                    project_id=project_id,
                    details=details,
                )
                session.commit()

            hosts = session.execute(_host_query(host_ids).where(Host.enabled.is_(True))).scalars().all()
            scan.hosts_scanned = len(hosts)
            session.commit()

            for host in hosts:
                ensure_not_cancelled(stop_event, "Scan stopped by user.")
                roots = _enabled_roots(host)
                record_scan_event(
                    session,
                    scan_id=scan.id,
                    level="info",
                    event_type="host_scan_started",
                    message=f"Scanning host {host.name}.",
                    host_id=host.id,
                    details={"roots": roots},
                )
                session.commit()

                try:
                    client = executor.open_client(host)
                except Exception as exc:  # pragma: no cover - exercised by mocked tests
                    host.connection_status = "unreachable"
                    scan.errors_count += 1
                    record_scan_event(
                        session,
                        scan_id=scan.id,
                        level="error",
                        event_type="host_connect_failed",
                        message=str(exc),
                        host_id=host.id,
                    )
                    session.commit()
                    continue

                try:
                    if scan.scan_type == "incremental":
                        result = scan_host_incremental(
                            executor,
                            client,
                            host,
                            general_settings,
                            scan_settings,
                            host.last_scan_at,
                            record_event=live_event,
                        )
                    else:
                        result = scan_host_full(
                            executor,
                            client,
                            host,
                            general_settings,
                            scan_settings,
                            record_event=live_event,
                        )

                    seen_at = utcnow()
                    host_summary_snapshot = next(
                        (
                            snapshot
                            for snapshot in result["snapshots"]
                            if (snapshot.metadata_json or {}).get("setup_kind") == "host-summary"
                        ),
                        None,
                    )
                    sync_host_fact_inventory(
                        session,
                        host=host,
                        host_facts=result.get("host_facts") or {},
                        classification=(host_summary_snapshot.metadata_json or {}).get("machine_classification")
                        if host_summary_snapshot
                        else None,
                        seen_at=seen_at,
                    )
                    for project_id in result["unchanged_project_ids"]:
                        project = session.get(Project, project_id)
                        if project:
                            project.last_seen_at = seen_at

                    refreshed_projects: list[Project] = []
                    for snapshot in result["snapshots"]:
                        ensure_not_cancelled(stop_event, "Scan stopped by user.")
                        project, changed_fields = _upsert_project(session, snapshot, seen_at)
                        refreshed_projects.append(project)
                        scan.projects_found += 1
                        live_event(
                            host_id=host.id,
                            project_id=project.id,
                            level="info",
                            event_type="project_discovered",
                            message=f"Stored project {project.path}.",
                            details={"changed_fields": changed_fields},
                        )

                    if scan.scan_type == "full":
                        deleted_projects = _delete_stale_projects(
                            session,
                            host=host,
                            active_paths={snapshot.path for snapshot in result["snapshots"]},
                            scanned_roots=[item["root_path"] for item in result.get("root_inventories", [])],
                        )
                        if deleted_projects:
                            live_event(
                                host_id=host.id,
                                level="info",
                                event_type="stale_projects_deleted",
                                message=f"Removed {deleted_projects} stale project record(s) for {host.name}.",
                                details={"deleted_projects": deleted_projects},
                            )

                    created_files, updated_files = _sync_project_files(
                        session,
                        host=host,
                        projects=refreshed_projects,
                        root_inventories=result.get("root_inventories", []),
                        seen_at=seen_at,
                        full_scan=scan.scan_type == "full",
                    )
                    live_event(
                        host_id=host.id,
                        level="info",
                        event_type="file_inventory_synced",
                        message=f"Synced file inventory for {host.name}.",
                        details={"created": created_files, "updated": updated_files},
                    )
                    try:
                        topology_summary = refresh_host_topology(session, host.id)
                        live_event(
                            host_id=host.id,
                            level="info",
                            event_type="topology_refreshed",
                            message=(
                                f"Refreshed topology for {host.name}: "
                                f"{topology_summary['updated_guests']} guest entries checked, "
                                f"{topology_summary['new_guests']} new."
                            ),
                            details=topology_summary,
                        )
                    except Exception as exc:
                        live_event(
                            host_id=host.id,
                            level="warning",
                            event_type="topology_refresh_skipped",
                            message=str(exc),
                        )
                    session.flush()

                    project_ids = [project.id for project in refreshed_projects]
                    profile_project_ids = set(project_ids)
                    for project_id in result["unchanged_project_ids"]:
                        unchanged_project = session.get(Project, project_id)
                        if unchanged_project and (unchanged_project.metadata_json or {}).get("setup_kind") == "host-summary":
                            profile_project_ids.add(unchanged_project.id)
                    refreshed_projects = (
                        session.execute(
                            select(Project)
                            .where(Project.id.in_(profile_project_ids))
                            .options(selectinload(Project.files), selectinload(Project.host), selectinload(Project.markers))
                        ).scalars().all()
                        if profile_project_ids
                        else []
                    )
                    if profile_settings.enabled:
                        for project in refreshed_projects:
                            ensure_not_cancelled(stop_event, "Scan stopped by user.")
                            _maybe_interpret_project_during_scan(
                                session,
                                config,
                                executor,
                                client,
                                host=host,
                                project=project,
                                scan_settings=scan_settings,
                                record_event=live_event,
                                stop_event=stop_event,
                            )

                    for warning in result.get("warnings", []):
                        live_event(
                            host_id=host.id,
                            level="warning",
                            event_type="host_scan_warning",
                            message=warning,
                        )

                    host.connection_status = "reachable"
                    host.last_seen_at = seen_at
                    host.last_scan_at = seen_at
                    live_event(
                        host_id=host.id,
                        level="info",
                        event_type="host_scan_completed",
                        message=f"Completed scan for {host.name}.",
                        details={
                            "projects_refreshed": len(result["snapshots"]),
                            "projects_reused": len(result["unchanged_project_ids"]),
                        },
                    )
                    session.commit()
                except JobCancelled:
                    raise
                except Exception as exc:
                    scan.errors_count += 1
                    host.connection_status = "error"
                    record_scan_event(
                        session,
                        scan_id=scan.id,
                        host_id=host.id,
                        level="error",
                        event_type="host_scan_failed",
                        message=str(exc),
                    )
                    session.commit()
                finally:
                    client.close()

            scan.status = "completed"
            scan.finished_at = utcnow()
            scan.summary = f"Scanned {scan.hosts_scanned} host(s), stored {scan.projects_found} project snapshot(s), errors: {scan.errors_count}."
            record_scan_event(
                session,
                scan_id=scan.id,
                level="info",
                event_type="scan_completed",
                message=scan.summary,
            )
            session.commit()
    except JobCancelled as exc:
        with session_scope(app.state.session_factory) as session:
            scan = session.get(Scan, scan_id)
            if scan is None:
                return
            scan.status = "cancelled"
            scan.finished_at = utcnow()
            scan.summary = str(exc)
            record_scan_event(
                session,
                scan_id=scan.id,
                level="warning",
                event_type="scan_cancelled",
                message=str(exc),
            )
    except Exception as exc:  # pragma: no cover - defensive fallback
        with session_scope(app.state.session_factory) as session:
            scan = session.get(Scan, scan_id)
            if scan is None:
                return
            scan.status = "failed"
            scan.finished_at = utcnow()
            scan.errors_count += 1
            scan.summary = f"Scan failed: {exc}"
            record_scan_event(
                session,
                scan_id=scan.id,
                level="error",
                event_type="scan_failed",
                message=str(exc),
            )
    finally:
        _pop_scan_control(app, scan_id)
