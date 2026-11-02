from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from app.config import AppConfig
from app.utils.common import utcnow


def _updater_dir(config: AppConfig) -> Path:
    path = config.resolved_data_dir / "updater"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _state_path(config: AppConfig) -> Path:
    return _updater_dir(config) / "latest.json"


def _log_path(config: AppConfig) -> Path:
    return _updater_dir(config) / "latest.log"


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def read_update_status(config: AppConfig) -> dict[str, Any]:
    state_path = _state_path(config)
    log_path = _log_path(config)
    state: dict[str, Any] = {
        "status": "idle",
        "message": "No updater run has been started yet.",
        "started_at": None,
        "finished_at": None,
        "exit_code": None,
        "pid": None,
        "log_path": str(log_path),
        "log_tail": "",
    }
    if state_path.exists():
        try:
            loaded = json.loads(state_path.read_text())
            if isinstance(loaded, dict):
                state.update(loaded)
        except Exception:
            state["status"] = "failed"
            state["message"] = "Updater state could not be read."
    pid = state.get("pid")
    if state.get("status") == "running" and not _pid_alive(pid):
        state["status"] = "failed"
        state["message"] = "Updater job exited unexpectedly."
    if log_path.exists():
        lines = log_path.read_text(errors="replace").splitlines()
        state["log_tail"] = "\n".join(lines[-60:])
    return state


def start_update_job(
    *,
    config: AppConfig,
    repo_path: Path,
    sudo_password: str | None = None,
    github_pat: str | None = None,
    github_branch: str | None = None,
    service_name: str = "aicms",
) -> dict[str, Any]:
    status = read_update_status(config)
    if status.get("status") == "running" and _pid_alive(status.get("pid")):
        raise RuntimeError("An updater job is already running.")

    runner_path = repo_path / "scripts" / "run_update_job.py"
    script_path = repo_path / "scripts" / "update_aicms.sh"
    if not runner_path.exists():
        raise RuntimeError("Updater runner script is missing.")
    if not script_path.exists():
        raise RuntimeError("Updater shell script is missing.")

    env = os.environ.copy()
    if sudo_password:
        env["AICMS_UPDATE_SUDO_PASSWORD"] = sudo_password
    if github_pat:
        env["AICMS_GITHUB_PAT"] = github_pat
    if github_branch:
        env["AICMS_GITHUB_BRANCH"] = github_branch.strip()

    process = subprocess.Popen(
        [
            sys.executable,
            str(runner_path),
            "--app-dir",
            str(repo_path),
            "--state-path",
            str(_state_path(config)),
            "--log-path",
            str(_log_path(config)),
            "--service-name",
            service_name,
        ],
        cwd=str(repo_path),
        env=env,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return {
        "status": "started",
        "pid": process.pid,
        "state_path": str(_state_path(config)),
        "log_path": str(_log_path(config)),
    }
