#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="${AICMS_SERVICE_NAME:-aicms}"
SYSTEMD_UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
TARGET_BRANCH="${AICMS_GITHUB_BRANCH:-}"

normalize_github_https_remote() {
  local remote_url="$1"
  case "$remote_url" in
    git@github.com:*)
      printf 'https://github.com/%s\n' "${remote_url#git@github.com:}"
      ;;
    ssh://git@github.com/*)
      printf 'https://github.com/%s\n' "${remote_url#ssh://git@github.com/}"
      ;;
    https://github.com/*)
      printf '%s\n' "$remote_url"
      ;;
    http://github.com/*)
      printf 'https://github.com/%s\n' "${remote_url#http://github.com/}"
      ;;
    *)
      return 1
      ;;
  esac
}

ensure_service_killmode_process() {
  local unit_path="$1"
  [ -f "$unit_path" ] || return 0
  if grep -Eq '^[[:space:]]*KillMode=process[[:space:]]*$' "$unit_path"; then
    return 0
  fi

  echo "Ensuring ${SERVICE_NAME}.service uses KillMode=process so updater jobs survive restarts."
  local tmp_file
  tmp_file="$(mktemp)"
  python3 - "$unit_path" "$tmp_file" <<'PY'
from pathlib import Path
import sys

unit_path = Path(sys.argv[1])
tmp_path = Path(sys.argv[2])
lines = unit_path.read_text().splitlines()
output = []
inserted = False
in_service = False

for line in lines:
    stripped = line.strip()
    if stripped == "[Service]":
        in_service = True
        output.append(line)
        continue
    if in_service and stripped.startswith("[") and stripped.endswith("]"):
        if not inserted:
            output.append("KillMode=process")
            inserted = True
        in_service = False
    output.append(line)

if in_service and not inserted:
    output.append("KillMode=process")
    inserted = True

tmp_path.write_text("\n".join(output) + "\n")
PY

  if ! cmp -s "$tmp_file" "$unit_path"; then
    if [ -n "$SUDO" ]; then
      $SUDO cp "$tmp_file" "$unit_path"
    else
      cp "$tmp_file" "$unit_path"
    fi
    if [ -n "$SUDO" ]; then
      $SUDO systemctl daemon-reload
    else
      systemctl daemon-reload
    fi
  fi
  rm -f "$tmp_file"
}

if [ "$(id -u)" -ne 0 ]; then
  SUDO="sudo"
else
  SUDO=""
fi

cd "$APP_DIR"

if [ -d .git ]; then
  REMOTE_URL="$(git remote get-url origin 2>/dev/null || true)"
  CURRENT_BRANCH="$(git symbolic-ref --quiet --short HEAD 2>/dev/null || git rev-parse --abbrev-ref HEAD)"
  if [ -z "$CURRENT_BRANCH" ] || [ "$CURRENT_BRANCH" = "HEAD" ]; then
    echo "Could not determine the current git branch for update."
    exit 1
  fi
  if [ -z "$TARGET_BRANCH" ]; then
    TARGET_BRANCH="$CURRENT_BRANCH"
  fi
  echo "Updating using branch: ${TARGET_BRANCH}"
  if [ -n "${AICMS_GITHUB_PAT:-}" ] && printf '%s' "$REMOTE_URL" | grep -qi 'github\.com'; then
    echo "GitHub PAT detected. Using authenticated HTTPS git update for origin."
    AUTH_REMOTE_URL="$(normalize_github_https_remote "$REMOTE_URL")"
    if [ -z "$AUTH_REMOTE_URL" ]; then
      echo "Could not normalize GitHub remote URL for PAT-authenticated update."
      exit 1
    fi
    GITHUB_BASIC_AUTH="$(python3 - <<'PY'
import base64
import os

token = os.environ.get("AICMS_GITHUB_PAT", "")
print(base64.b64encode(f"x-access-token:{token}".encode()).decode())
PY
)"
    git \
      -c credential.helper= \
      -c core.askPass= \
      -c "http.https://github.com/.extraheader=AUTHORIZATION: basic ${GITHUB_BASIC_AUTH}" \
      fetch --prune "$AUTH_REMOTE_URL" "$TARGET_BRANCH"
  else
    if command -v gh >/dev/null 2>&1; then
      echo "gh detected. Preparing Git auth through gh when possible..."
      gh auth setup-git >/dev/null 2>&1 || true
    fi
    git fetch --prune origin "$TARGET_BRANCH"
  fi

  if [ "$CURRENT_BRANCH" != "$TARGET_BRANCH" ]; then
    echo "Switching working tree from ${CURRENT_BRANCH} to ${TARGET_BRANCH}."
    if git show-ref --verify --quiet "refs/heads/${TARGET_BRANCH}"; then
      git checkout "$TARGET_BRANCH"
    else
      git checkout -b "$TARGET_BRANCH" FETCH_HEAD
    fi
  fi

  git merge --ff-only FETCH_HEAD
else
  echo "No .git metadata found. Skipping git pull."
fi

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi

.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

ensure_service_killmode_process "$SYSTEMD_UNIT_PATH"

$SUDO systemctl restart "${SERVICE_NAME}.service"
$SUDO systemctl status "${SERVICE_NAME}.service" --no-pager

if [ -d .git ]; then
  echo "Current revision: $(git rev-parse HEAD)"
fi
