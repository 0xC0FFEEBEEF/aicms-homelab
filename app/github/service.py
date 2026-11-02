from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from app.config import AppConfig
from app.github.client import GitHubClient
from app.schemas import GITHUB_SETTINGS_KEY, GitHubSettingsPayload, GitHubUpdateResult
from app.utils.common import utcnow
from app.utils.git import compare_git_revisions, get_local_git_revision
from app.utils.settings_store import get_settings, set_settings


def check_for_updates(session: Session, config: AppConfig, repo_path: Path) -> GitHubUpdateResult:
    settings = get_settings(session, config, GITHUB_SETTINGS_KEY, GitHubSettingsPayload)
    client = GitHubClient(timeout=config.github_timeout_seconds)

    if not settings.repo_owner or not settings.repo_name:
        raise RuntimeError("GitHub repo owner and repo name are required before checking for updates.")

    if not settings.pat and not client.has_gh_cli():
        raise RuntimeError("Configure a GitHub PAT or install/authenticate the gh CLI before checking for updates.")

    local_revision = get_local_git_revision(repo_path)
    remote_revision = client.get_branch_head(
        owner=settings.repo_owner,
        repo=settings.repo_name,
        branch=settings.branch,
        token=settings.pat,
    )

    settings.last_checked_at = utcnow()
    settings.local_revision = local_revision
    settings.remote_revision = remote_revision
    update_available = False
    if local_revision and remote_revision:
        if local_revision == remote_revision:
            update_available = False
        else:
            compare_status = compare_git_revisions(repo_path, local_revision, remote_revision)
            if compare_status is None:
                compare_status = client.compare_revisions(
                    owner=settings.repo_owner,
                    repo=settings.repo_name,
                    token=settings.pat,
                    base=local_revision,
                    head=remote_revision,
                )
            update_available = compare_status in {"ahead", "diverged"}
    settings.update_available = update_available
    settings.last_error = None
    set_settings(session, GITHUB_SETTINGS_KEY, settings)

    if local_revision is None:
        message = "Remote revision fetched. Local git revision is unavailable in this checkout."
    elif settings.update_available:
        message = "A newer revision is available in GitHub."
    else:
        message = "This checkout is up to date."

    return GitHubUpdateResult(
        update_available=settings.update_available,
        local_revision=local_revision,
        remote_revision=remote_revision,
        message=message,
    )
