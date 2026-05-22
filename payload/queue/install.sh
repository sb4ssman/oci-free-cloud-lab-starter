#!/usr/bin/env bash
# Install the cloud-lab queue runner as a systemd user timer.
# Runs every 60 seconds, picks and executes the next queued job.
set -euo pipefail

CLOUD_LAB_DIR="${CLOUD_LAB_DIR:-$HOME/cloud-lab}"
PAYLOAD_DIR="$CLOUD_LAB_DIR/payload/queue"
LOG_DIR="$CLOUD_LAB_DIR/logs"
UNIT_DIR="$HOME/.config/systemd/user"
QUEUE_FILE="$CLOUD_LAB_DIR/queue.json"

mkdir -p "$PAYLOAD_DIR" "$LOG_DIR" "$UNIT_DIR"

# Copy runner script
cp "$(dirname "$0")/queue_runner.py" "$PAYLOAD_DIR/queue_runner.py"
chmod +x "$PAYLOAD_DIR/queue_runner.py"

# Initialise empty queue if not present
if [[ ! -f "$QUEUE_FILE" ]]; then
    echo "[]" > "$QUEUE_FILE"
    echo "[queue/install] Created empty $QUEUE_FILE"
fi

# --- systemd service unit ---
cat > "$UNIT_DIR/cloud-lab-queue.service" <<EOF
[Unit]
Description=Cloud Lab queue runner (one-shot job executor)
After=network.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 $PAYLOAD_DIR/queue_runner.py
StandardOutput=append:$LOG_DIR/queue_runner.log
StandardError=append:$LOG_DIR/queue_runner.log
Environment=CLOUD_LAB_DIR=$CLOUD_LAB_DIR
EOF

# --- systemd timer unit (every 60 s) ---
cat > "$UNIT_DIR/cloud-lab-queue.timer" <<EOF
[Unit]
Description=Cloud Lab queue runner — tick every 60 s

[Timer]
OnBootSec=30
OnUnitActiveSec=60
Unit=cloud-lab-queue.service

[Install]
WantedBy=timers.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now cloud-lab-queue.timer

echo "[queue/install] cloud-lab-queue.timer installed and active."
