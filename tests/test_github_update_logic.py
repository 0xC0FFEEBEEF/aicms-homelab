from __future__ import annotations

from pathlib import Path

from app.github.service import check_for_updates
from app.schemas import GITHUB_SETTINGS_KEY, GitHubSettingsPayload
from app.utils.settings_store import set_settings


def test_update_check_does_not_report_update_when_local_is_ahead(session, config, monkeypatch):
    set_settings(
        session,
        GITHUB_SETTINGS_KEY,
        GitHubSettingsPayload(
            repo_owner="octo",
            repo_name="aicms",
            branch="main",
            pat="secret-token",
        ),
    )

    monkeypatch.setattr("app.github.service.get_local_git_revision", lambda repo_path: "local123")
    monkeypatch.setattr("app.github.client.GitHubClient.get_branch_head", lambda self, **kwargs: "remote456")
    monkeypatch.setattr("app.github.service.compare_git_revisions", lambda repo_path, base_revision, head_revision: "behind")

    result = check_for_updates(session, config, Path.cwd())

    assert result.update_available is False
    assert "up to date" in result.message.lower()


def test_update_check_reports_update_when_remote_is_ahead(session, config, monkeypatch):
    set_settings(
        session,
        GITHUB_SETTINGS_KEY,
        GitHubSettingsPayload(
            repo_owner="octo",
            repo_name="aicms",
            branch="main",
            pat="secret-token",
        ),
    )

    monkeypatch.setattr("app.github.service.get_local_git_revision", lambda repo_path: "local123")
    monkeypatch.setattr("app.github.client.GitHubClient.get_branch_head", lambda self, **kwargs: "remote456")
    monkeypatch.setattr("app.github.service.compare_git_revisions", lambda repo_path, base_revision, head_revision: "ahead")

    result = check_for_updates(session, config, Path.cwd())

    assert result.update_available is True
    assert "available" in result.message.lower()
