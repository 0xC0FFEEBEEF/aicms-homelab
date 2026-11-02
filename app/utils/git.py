from __future__ import annotations

import subprocess
from pathlib import Path


def get_local_git_revision(repo_path: Path) -> str | None:
    git_dir = repo_path / ".git"
    if not git_dir.exists():
        return None

    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_path,
            capture_output=True,
            check=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None

    revision = completed.stdout.strip()
    return revision or None


def compare_git_revisions(repo_path: Path, base_revision: str, head_revision: str) -> str | None:
    git_dir = repo_path / ".git"
    if not git_dir.exists():
        return None

    try:
        completed = subprocess.run(
            ["git", "rev-list", "--left-right", "--count", f"{base_revision}...{head_revision}"],
            cwd=repo_path,
            capture_output=True,
            check=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None

    parts = completed.stdout.strip().split()
    if len(parts) != 2:
        return None
    try:
        left_count = int(parts[0])
        right_count = int(parts[1])
    except ValueError:
        return None

    if left_count == 0 and right_count == 0:
        return "identical"
    if left_count == 0 and right_count > 0:
        return "ahead"
    if left_count > 0 and right_count == 0:
        return "behind"
    return "diverged"
