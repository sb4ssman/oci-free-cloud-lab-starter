#!/usr/bin/env bash
# Local dev launcher — shows the admin console UI with no login required.
# Runs on http://localhost:8765 — Ctrl+C to stop.

cd "$(dirname "$0")"

export FLEET_NAME="Test Fleet"
export CLOUD_LAB_DIR="$(pwd)"

echo "Starting dev console at http://localhost:8765 ..."
echo "Press Ctrl+C to stop."

(sleep 2 && (xdg-open http://localhost:8765/ 2>/dev/null || open http://localhost:8765/ 2>/dev/null)) &

python3 fleet/management/admin_console.py --dev
