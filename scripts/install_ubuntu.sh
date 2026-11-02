#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="${AICMS_SERVICE_NAME:-aicms}"
RUN_USER="${SUDO_USER:-$USER}"
HOST_BIND="${AICMS_HOST:-0.0.0.0}"
PORT_BIND="${AICMS_PORT:-8000}"

if [ "$(id -u)" -ne 0 ]; then
  SUDO="sudo"
else
  SUDO=""
fi

echo "Installing Ubuntu packages..."
$SUDO apt-get update
$SUDO apt-get install -y python3 python3-venv python3-pip git openssh-client

cd "$APP_DIR"

echo "Creating virtual environment..."
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example"
fi

mkdir -p data logs

echo "Installing systemd service ${SERVICE_NAME}..."
$SUDO tee "/etc/systemd/system/${SERVICE_NAME}.service" >/dev/null <<EOF
[Unit]
Description=AICMS local inventory service
After=network.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/.env
ExecStart=${APP_DIR}/.venv/bin/uvicorn app.main:app --host ${HOST_BIND} --port ${PORT_BIND}
KillMode=process
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

$SUDO systemctl daemon-reload
$SUDO systemctl enable --now "${SERVICE_NAME}.service"
$SUDO systemctl status "${SERVICE_NAME}.service" --no-pager

echo
echo "AICMS installed."
echo "Open http://$(hostname -f 2>/dev/null || hostname):${PORT_BIND}/web"
