#!/usr/bin/env bash
# Install keepalive cron jobs for this VM.
# Called during setup (cloud-init or bootstrap) with the VM's env file as $1.
#
# Core jobs (all VMs):
#   - Every 4h:   health_check.py  — system stats + ntfy heartbeat
#   - Daily 2:30: log_rotate.sh    — compress/prune logs (CPU burst)
#   - Daily 6:00: fleet_report.py  — full fleet status via ntfy
#
# Role-specific jobs (Oracle idle-reclamation protection):
#   worker:     Weekly Sunday 04:00 — apt update+upgrade (real I/O + CPU)
#   laboratory: Weekly Sunday 04:30 — compression benchmark (real CPU burst)
#
# Oracle reclaims Always Free VMs with <~10% average CPU over 7 days.
# These jobs generate genuine system activity — no fake load generators.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${1:-}"

echo "[keepalive] Installing cron jobs from $SCRIPT_DIR"
mkdir -p "$HOME/cloud-lab/logs"

# Helper: add a crontab entry if not already present (idempotent).
add_cron() {
    local entry="$1"
    ( crontab -l 2>/dev/null; echo "$entry" ) | sort -u | crontab -
}

add_cron "0 */4 * * * python3 $SCRIPT_DIR/health_check.py >> $HOME/cloud-lab/logs/keepalive.log 2>&1"
add_cron "30 2 * * * bash $SCRIPT_DIR/log_rotate.sh >> $HOME/cloud-lab/logs/keepalive.log 2>&1"
add_cron "0 6 * * * python3 $SCRIPT_DIR/fleet_report.py >> $HOME/cloud-lab/logs/keepalive.log 2>&1"

# Role-specific maintenance jobs — detected from the env file.
VM_NAME=""
if [[ -n "$ENV_FILE" && -f "$ENV_FILE" ]]; then
    VM_NAME=$(grep -E "^FLEET_VM_NAME=" "$ENV_FILE" | cut -d= -f2 | tr -d '"'"'"' ')
fi
VM_NAME="${VM_NAME:-${FLEET_VM_NAME:-}}"

if [[ "$VM_NAME" == "worker" ]]; then
    # Weekly apt update+upgrade: real package I/O and occasional compilation.
    add_cron "0 4 * * 0 sudo apt-get update -q && sudo apt-get upgrade -y -q >> $HOME/cloud-lab/logs/keepalive.log 2>&1"
    echo "[keepalive] Worker role: added weekly apt update+upgrade (Sun 04:00)"
fi

if [[ "$VM_NAME" == "laboratory" ]]; then
    # Weekly compression benchmark: 1000 rounds of zlib on 64 KiB blocks
    # (~1-2 CPU-seconds of genuine compute on each of the A1 Flex cores).
    add_cron "30 4 * * 0 python3 -c \"import zlib,os; [zlib.compress(os.urandom(65536), 6) for _ in range(1000)]; print('compression benchmark done')\" >> $HOME/cloud-lab/logs/keepalive.log 2>&1"
    echo "[keepalive] Laboratory role: added weekly compression benchmark (Sun 04:30)"
fi

echo "[keepalive] Cron jobs installed:"
crontab -l | grep "cloud-lab\|apt-get\|zlib"
