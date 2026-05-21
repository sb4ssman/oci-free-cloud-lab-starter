#!/usr/bin/env bash
# Worker VM role setup.
# Installs: heartbeat timer (→ management), crosswatch timer, self-update timer.
# Safe to re-run — services are restarted, .env is never overwritten.
set -euo pipefail

TOOLS_DIR="${TOOLS_DIR:-$HOME/cloud-lab}"
ENV_FILE="${ENV_FILE:-$HOME/.config/cloud-lab/worker.env}"
PYTHON="${PYTHON:-python3}"

SRC="$TOOLS_DIR/fleet/worker"

# ── heartbeat timer (→ management console, every 4 h) ────────────────────────
cat > /tmp/cloud-lab-heartbeat.service <<SERVICE
[Unit]
Description=Cloud Lab worker heartbeat — POSTs liveness to management console

[Service]
User=ubuntu
Type=oneshot
EnvironmentFile=${ENV_FILE}
WorkingDirectory=${TOOLS_DIR}
ExecStart=${PYTHON} ${SRC}/heartbeat.py
Environment=PYTHONUNBUFFERED=1
SERVICE

cat > /tmp/cloud-lab-heartbeat.timer <<TIMER
[Unit]
Description=Cloud Lab worker heartbeat — every 4 hours

[Timer]
OnBootSec=3min
OnUnitActiveSec=4h
Persistent=true

[Install]
WantedBy=timers.target
TIMER
sudo mv /tmp/cloud-lab-heartbeat.service /etc/systemd/system/cloud-lab-heartbeat.service
sudo mv /tmp/cloud-lab-heartbeat.timer   /etc/systemd/system/cloud-lab-heartbeat.timer

# ── cross-watch timer (every 6 h, direct ntfy if peer TERMINATED) ────────────
cat > /tmp/cloud-lab-crosswatch.service <<SERVICE
[Unit]
Description=Cloud Lab cross-watch — checks peer VMs, ntfy direct alert if down

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
OnBootSec=8min
OnUnitActiveSec=6h
Persistent=true

[Install]
WantedBy=timers.target
TIMER
sudo mv /tmp/cloud-lab-crosswatch.service /etc/systemd/system/cloud-lab-crosswatch.service
sudo mv /tmp/cloud-lab-crosswatch.timer   /etc/systemd/system/cloud-lab-crosswatch.timer

# ── nightly self-update ───────────────────────────────────────────────────────
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
Description=Cloud Lab self-update — nightly at 03:30

[Timer]
OnCalendar=03:30
Persistent=true

[Install]
WantedBy=timers.target
TIMER
sudo mv /tmp/cloud-lab-update.service /etc/systemd/system/cloud-lab-update.service
sudo mv /tmp/cloud-lab-update.timer   /etc/systemd/system/cloud-lab-update.timer

# ── enable and start ──────────────────────────────────────────────────────────
sudo systemctl daemon-reload
sudo systemctl enable cloud-lab-heartbeat.timer cloud-lab-crosswatch.timer cloud-lab-update.timer
sudo systemctl start  cloud-lab-heartbeat.timer cloud-lab-crosswatch.timer cloud-lab-update.timer

echo ""
echo "Worker role installed."
echo "Timers: cloud-lab-heartbeat (4h -> management), cloud-lab-crosswatch (6h -> management), cloud-lab-update (nightly)"
