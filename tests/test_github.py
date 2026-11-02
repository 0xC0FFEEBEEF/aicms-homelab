from __future__ import annotations

from app.schemas import GITHUB_SETTINGS_KEY, GitHubSettingsPayload
from app.utils.settings_store import set_settings


def test_github_update_check(client, session, app, monkeypatch):
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

    monkeypatch.setattr("app.github.service.get_local_git_revision", lambda repo_path: "abc123")
    monkeypatch.setattr("app.github.client.GitHubClient.get_branch_head", lambda self, **kwargs: "def456")
    monkeypatch.setattr("app.github.service.compare_git_revisions", lambda repo_path, base_revision, head_revision: "ahead")

    response = client.post("/api/github/check-updates", follow_redirects=True)
    assert response.status_code == 200
    assert "available" in response.text.lower()


def test_github_update_check_uses_gh_when_available(client, session, app, monkeypatch):
    set_settings(
        session,
        GITHUB_SETTINGS_KEY,
        GitHubSettingsPayload(
            repo_owner="octo",
            repo_name="aicms",
            branch="main",
            pat=None,
        ),
    )

    monkeypatch.setattr("app.github.service.get_local_git_revision", lambda repo_path: "abc123")
    monkeypatch.setattr("app.github.client.GitHubClient.has_gh_cli", lambda self: True)
    monkeypatch.setattr("app.github.client.GitHubClient._get_branch_head_with_gh", lambda self, **kwargs: "def456")
    monkeypatch.setattr("app.github.service.compare_git_revisions", lambda repo_path, base_revision, head_revision: "ahead")

    response = client.post("/api/github/check-updates", follow_redirects=True)
    assert response.status_code == 200
    assert "available" in response.text.lower()
