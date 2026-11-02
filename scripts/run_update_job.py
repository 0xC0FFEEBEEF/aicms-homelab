#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_state(path: Path, **fields) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    current = {}
    if path.exists():
        try:
            current = json.loads(path.read_text())
        except Exception:
            current = {}
    current.update(fields)
    path.write_text(json.dumps(current, indent=2, sort_keys=True, default=str))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-dir", required=True)
    parser.add_argument("--state-path", required=True)
    parser.add_argument("--log-path", required=True)
    parser.add_argument("--service-name", default="aicms")
    args = parser.parse_args()

    app_dir = Path(args.app_dir).resolve()
    state_path = Path(args.state_path).resolve()
    log_path = Path(args.log_path).resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    script_path = app_dir / "scripts" / "update_aicms.sh"
    if not script_path.exists():
        write_state(
            state_path,
            status="failed",
            message=f"Updater script not found at {script_path}.",
            started_at=utcnow(),
            finished_at=utcnow(),
            exit_code=127,
            pid=os.getpid(),
            log_path=str(log_path),
        )
        return 127

    env = os.environ.copy()
    env["AICMS_SERVICE_NAME"] = args.service_name
    password = env.get("AICMS_UPDATE_SUDO_PASSWORD")

    write_state(
        state_path,
        status="running",
        message="Updater is running.",
        started_at=utcnow(),
        finished_at=None,
        exit_code=None,
        pid=os.getpid(),
        log_path=str(log_path),
    )

    if os.geteuid() == 0:
        command = ["bash", str(script_path)]
        stdin_data = None
    elif password:
        command = ["sudo", "-S", "-p", "", "bash", str(script_path)]
        stdin_data = f"{password}\n"
    else:
        command = ["bash", str(script_path)]
        stdin_data = None

    with log_path.open("w", encoding="utf-8") as log_file:
        log_file.write(f"[{utcnow()}] Starting updater command: {' '.join(command)}\n")
        log_file.flush()
        try:
            completed = subprocess.run(
                command,
                cwd=str(app_dir),
                env=env,
                input=stdin_data,
                text=True,
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
            exit_code = int(completed.returncode)
            status = "completed" if exit_code == 0 else "failed"
            message = "Updater completed successfully." if exit_code == 0 else f"Updater failed with exit code {exit_code}."
        except Exception as exc:
            exit_code = 1
            status = "failed"
            message = f"Updater failed before completion: {exc}"
            log_file.write(f"\n[{utcnow()}] ERROR: {exc}\n")

    write_state(
        state_path,
        status=status,
        message=message,
        finished_at=utcnow(),
        exit_code=exit_code,
        pid=os.getpid(),
        log_path=str(log_path),
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
