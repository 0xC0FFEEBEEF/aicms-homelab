from __future__ import annotations

import json
from pathlib import Path

from app.schemas import GITHUB_SETTINGS_KEY, GitHubSettingsPayload
from app.updater.service import read_update_status
from app.utils.settings_store import set_settings


def test_read_update_status_defaults_to_idle(config):
    status = read_update_status(config)
    assert status["status"] == "idle"
    assert "No updater run" in status["message"]


def test_github_run_update_route_starts_job_without_persisting_password(client, session, monkeypatch):
    captured: dict[str, str | None] = {}

    set_settings(
        session,
        GITHUB_SETTINGS_KEY,
        GitHubSettingsPayload(
            repo_owner="example",
            repo_name="aicms",
            branch="main",
            pat="ghp_test_token",
        ),
    )

    def fake_start_update_job(*, config, repo_path, sudo_password=None, github_pat=None, github_branch=None, service_name="aicms"):
        captured["password"] = sudo_password
        captured["github_pat"] = github_pat
        captured["github_branch"] = github_branch
        return {"status": "started", "pid": 12345, "state_path": "x", "log_path": "y"}

    monkeypatch.setattr("app.web.api.start_update_job", fake_start_update_job)

    response = client.post(
        "/api/github/run-update",
        data={"sudo_password": "super-secret"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Started updater job" in response.text
    assert captured["password"] == "super-secret"
    assert captured["github_pat"] == "ghp_test_token"
    assert captured["github_branch"] == "main"


def test_settings_page_renders_updater_status_from_state_file(client, config):
    updater_dir = config.resolved_data_dir / "updater"
    updater_dir.mkdir(parents=True, exist_ok=True)
    (updater_dir / "latest.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "message": "Updater completed successfully.",
                "started_at": "2026-03-22T12:00:00+00:00",
                "finished_at": "2026-03-22T12:05:00+00:00",
                "exit_code": 0,
            }
        )
    )
    (updater_dir / "latest.log").write_text("git pull --ff-only\nsystemctl restart aicms.service\n")

    response = client.get("/web/settings?tab=github")

    assert response.status_code == 200
    assert "Updater completed successfully." in response.text
    assert "Run Update Script" in response.text
    assert "git pull --ff-only" in response.text


def test_install_script_sets_killmode_process_for_aicms_service():
    script = Path("scripts/install_ubuntu.sh").read_text()
    assert "KillMode=process" in script


def test_update_script_honors_configured_branch():
    script = Path("scripts/update_aicms.sh").read_text()
    assert 'TARGET_BRANCH="${AICMS_GITHUB_BRANCH:-}"' in script
    assert 'fetch --prune "$AUTH_REMOTE_URL" "$TARGET_BRANCH"' in script
    assert 'git fetch --prune origin "$TARGET_BRANCH"' in script
    assert 'git checkout "$TARGET_BRANCH"' in script
