#!/usr/bin/env bash
# Install the lab dashboard as a systemd service on the laboratory VM.
# Safe to re-run. Restarts the service if already installed.
#
# Usage:
#   bash payload/dashboard/install.sh [env-file]
#   bash payload/dashboard/install.sh ~/.config/cloud-lab/laboratory.env
set -euo pipefail

ENV_FILE="${1:-$HOME/.config/cloud-lab/laboratory.env}"
TOOLS_DIR="${TOOLS_DIR:-$HOME/cloud-lab}"
PYTHON="${PYTHON:-python3}"

SRC="$TOOLS_DIR/payload/dashboard"

cat > /tmp/cloud-lab-dashboard.service <<SERVICE
[Unit]
Description=Cloud Lab laboratory dashboard — local stats viewer
After=network.target

[Service]
User=ubuntu
EnvironmentFile=${ENV_FILE}
WorkingDirectory=${TOOLS_DIR}
ExecStart=${PYTHON} ${SRC}/lab_dashboard.py
Restart=on-failure
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
SERVICE

sudo mv /tmp/cloud-lab-dashboard.service /etc/systemd/system/cloud-lab-dashboard.service
sudo systemctl daemon-reload
sudo systemctl enable cloud-lab-dashboard
sudo systemctl restart cloud-lab-dashboard

echo "[lab-dashboard] installed and started on 127.0.0.1:8700"
echo "  Access: ssh -i ~/.ssh/fleet.key -L 8700:localhost:8700 ubuntu@<lab-public-ip>"
echo "  Then open: http://localhost:8700"
