from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from app.utils.common import fingerprint_payload


PROJECT_TYPE_PRIORITY = [
    ("pyproject.toml", "python"),
    ("requirements.txt", "python"),
    ("package.json", "node"),
    ("Cargo.toml", "rust"),
    ("go.mod", "go"),
    (".csproj", "dotnet"),
    (".sln", "dotnet"),
    ("docker-compose.yml", "container"),
    ("compose.yaml", "container"),
    ("Dockerfile", "container"),
    ("README.md", "generic"),
    (".git", "generic-git"),
]


def guess_project_type(marker_names: list[str]) -> str:
    marker_set = set(marker_names)
    for marker_name, project_type in PROJECT_TYPE_PRIORITY:
        if marker_name in marker_set:
            return project_type
        if marker_name.startswith(".") and any(name.endswith(marker_name) for name in marker_set):
            return project_type
    return "generic"


def guess_project_name(project_path: str, marker_files: list[dict[str, str]]) -> str:
    path_name = PurePosixPath(project_path).name
    if path_name:
        return path_name

    for marker in marker_files:
        marker_path = marker.get("marker_path")
        if marker_path:
            return PurePosixPath(marker_path).parent.name
    return "unknown-project"


def build_project_fingerprint(
    host_id: int,
    path: str,
    marker_files: list[dict[str, str]],
    project_type: str,
    last_modified_at: str | None,
    repo_branch: str | None,
    head_commit: str | None,
) -> str:
    return fingerprint_payload(
        {
            "host_id": host_id,
            "path": path,
            "markers": marker_files,
            "project_type": project_type,
            "last_modified_at": last_modified_at,
            "repo_branch": repo_branch,
            "head_commit": head_commit,
        }
    )


def summarize_marker_names(marker_files: list[dict[str, str]]) -> list[str]:
    return sorted({marker["marker_name"] for marker in marker_files})


def build_project_metadata(marker_files: list[dict[str, str]], tree_entries: list[dict[str, Any]] | None) -> dict[str, Any]:
    return {
        "marker_names": summarize_marker_names(marker_files),
        "tree_entry_count": len(tree_entries or []),
    }
