#!/usr/bin/env bash
# Management VM role setup.
# Installs: fleet orchestrator, admin console, heartbeat timer, crosswatch timer, self-update timer.
# Safe to re-run — services are restarted, .env is never overwritten.
set -euo pipefail

TOOLS_DIR="${TOOLS_DIR:-$HOME/cloud-lab}"
ENV_FILE="${ENV_FILE:-$HOME/.config/cloud-lab/management.env}"
PYTHON="${PYTHON:-python3}"

SRC="$TOOLS_DIR/fleet/management"

# ── fleet orchestrator service ────────────────────────────────────────────────
cat > /tmp/cloud-lab-orchestrator.service <<SERVICE
[Unit]
Description=Cloud Lab fleet orchestrator — builds and watches the OCI VM fleet
After=network-online.target
Wants=network-online.target

[Service]
User=ubuntu
Type=simple
EnvironmentFile=${ENV_FILE}
Environment=OCI_AUTH_MODE=instance_principal
WorkingDirectory=${TOOLS_DIR}
ExecStart=${PYTHON} ${SRC}/fleet_orchestrator.py
Restart=on-failure
RestartSec=60
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
SERVICE
sudo mv /tmp/cloud-lab-orchestrator.service /etc/systemd/system/cloud-lab-orchestrator.service

# ── admin console service ─────────────────────────────────────────────────────
cat > /tmp/cloud-lab-console.service <<SERVICE
[Unit]
Description=Cloud Lab admin console — fleet status portal
After=network-online.target
Wants=network-online.target

[Service]
User=ubuntu
Type=simple
EnvironmentFile=${ENV_FILE}
WorkingDirectory=${SRC}
ExecStart=${PYTHON} ${SRC}/admin_console.py
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
SERVICE
sudo mv /tmp/cloud-lab-console.service /etc/systemd/system/cloud-lab-console.service

# ── heartbeat timer (→ owner via ntfy, every 12 h) ───────────────────────────
cat > /tmp/cloud-lab-heartbeat.service <<SERVICE
[Unit]
Description=Cloud Lab heartbeat — sends fleet status to owner via ntfy

[Service]
User=ubuntu
Type=oneshot
EnvironmentFile=${ENV_FILE}
Environment=OCI_AUTH_MODE=instance_principal
WorkingDirectory=${TOOLS_DIR}
ExecStart=${PYTHON} ${SRC}/heartbeat.py
Environment=PYTHONUNBUFFERED=1
SERVICE

cat > /tmp/cloud-lab-heartbeat.timer <<TIMER
[Unit]
Description=Cloud Lab heartbeat — every 12 hours

[Timer]
OnBootSec=5min
OnUnitActiveSec=12h
Persistent=true

[Install]
WantedBy=timers.target
TIMER
sudo mv /tmp/cloud-lab-heartbeat.service /etc/systemd/system/cloud-lab-heartbeat.service
sudo mv /tmp/cloud-lab-heartbeat.timer   /etc/systemd/system/cloud-lab-heartbeat.timer

# ── cross-watch timer (checks other VMs every 6 h, ntfy if TERMINATED) ───────
cat > /tmp/cloud-lab-crosswatch.service <<SERVICE
[Unit]
Description=Cloud Lab cross-watch — checks peer VMs via OCI, alerts if down

[Service]
User=ubuntu
Type=oneshot
EnvironmentFile=${ENV_FILE}
Environment=OCI_AUTH_MODE=instance_principal
WorkingDirectory=${TOOLS_DIR}
ExecStart=${PYTHON} ${SRC}/crosswatch.py
Environment=PYTHONUNBUFFERED=1
SERVICE

cat > /tmp/cloud-lab-crosswatch.timer <<TIMER
[Unit]
Description=Cloud Lab cross-watch — every 6 hours

[Timer]
OnBootSec=10min
OnUnitActiveSec=6h
Persistent=true

[Install]
WantedBy=timers.target
TIMER
sudo mv /tmp/cloud-lab-crosswatch.service /etc/systemd/system/cloud-lab-crosswatch.service
sudo mv /tmp/cloud-lab-crosswatch.timer   /etc/systemd/system/cloud-lab-crosswatch.timer

# ── nightly self-update timer ─────────────────────────────────────────────────
cat > /tmp/cloud-lab-update.service <<SERVICE
[Unit]
Description=Cloud Lab self-update — git pull fleet repo

[Service]
User=ubuntu
Type=oneshot
WorkingDirectory=$HOME/cloud-lab
ExecStart=/usr/bin/git pull --ff-only
SERVICE

cat > /tmp/cloud-lab-update.timer <<TIMER
[Unit]
Description=Cloud Lab self-update — nightly at 03:00

[Timer]
OnCalendar=03:00
Persistent=true

[Install]
WantedBy=timers.target
TIMER
sudo mv /tmp/cloud-lab-update.service /etc/systemd/system/cloud-lab-update.service
sudo mv /tmp/cloud-lab-update.timer   /etc/systemd/system/cloud-lab-update.timer

# ── enable and start everything ───────────────────────────────────────────────
sudo systemctl daemon-reload
sudo systemctl enable cloud-lab-orchestrator cloud-lab-console
sudo systemctl enable cloud-lab-heartbeat.timer cloud-lab-crosswatch.timer cloud-lab-update.timer
sudo systemctl restart cloud-lab-orchestrator cloud-lab-console
sudo systemctl start   cloud-lab-heartbeat.timer cloud-lab-crosswatch.timer cloud-lab-update.timer

echo ""
echo "Management role installed."
echo ""
echo "Services: cloud-lab-orchestrator, cloud-lab-console"
echo "Timers:   cloud-lab-heartbeat (12h), cloud-lab-crosswatch (6h), cloud-lab-update (nightly)"
echo ""
echo "Admin console: https://$(grep ^ADMIN_DOMAIN "$ENV_FILE" | cut -d= -f2)"
