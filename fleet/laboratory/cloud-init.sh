#!/bin/bash
# Cloud-init bootstrap for laboratory (the A1 Flex instance).
# Runs as root on first boot via OCI user-data.
# ${VAR} placeholders are substituted by fleet_orchestrator.py at launch time.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

apt-get update -qq
apt-get install -y -qq ca-certificates curl git jq python3 python3-venv tmux unzip

echo "[cloud-init] Installing OCI CLI..."
if [ ! -f /home/ubuntu/bin/oci ]; then
    sudo -u ubuntu bash -c "$(curl -L https://raw.githubusercontent.com/oracle/oci-cli/master/scripts/install/install.sh)" -- --accept-all-defaults
fi
ln -sf /home/ubuntu/bin/oci /usr/local/bin/oci

clean_repo_url() {
    case "$FLEET_REPO" in
        git@*|ssh://*|https://*) printf '%s\n' "$FLEET_REPO" ;;
        *.git) printf 'https://github.com/%s\n' "$FLEET_REPO" ;;
        *) printf 'https://github.com/%s.git\n' "$FLEET_REPO" ;;
    esac
}
auth_repo_url() {
    local clean
    clean="$(clean_repo_url)"
    if [[ -n "${GITHUB_TOKEN:-}" && "$clean" == https://github.com/* ]]; then
        printf '%s\n' "${clean/https:\/\/github.com\//https:\/\/oauth2:${GITHUB_TOKEN}@github.com\/}"
    else
        printf '%s\n' "$clean"
    fi
}
CLONE_URL="$(auth_repo_url)"
CLEAN_URL="$(clean_repo_url)"
sudo -u ubuntu git clone "$CLONE_URL" /home/ubuntu/cloud-lab \
  || sudo -u ubuntu git -C /home/ubuntu/cloud-lab pull --ff-only
sudo -u ubuntu git -C /home/ubuntu/cloud-lab remote set-url origin "$CLEAN_URL" || true

install -d -m 755 -o ubuntu -g ubuntu /home/ubuntu/.config/cloud-lab
cat > /home/ubuntu/.config/cloud-lab/laboratory.env << 'ENVEOF'
OCI_AUTH_MODE=instance_principal
OCI_COMPARTMENT_ID=${OCI_COMPARTMENT_ID}
OCI_SUBNET_ID=${OCI_SUBNET_ID}
FLEET_MANAGEMENT_PRIVATE_IP=${FLEET_MANAGEMENT_PRIVATE_IP}
NOTIFY_NTFY_TOPIC=${NOTIFY_NTFY_TOPIC}
GITHUB_TOKEN=${GITHUB_TOKEN}
FLEET_REPO=${FLEET_REPO}
FLEET_NAME=${FLEET_NAME}
FLEET_VM_NAME=laboratory
FLEET_HEARTBEAT_TOKEN=${FLEET_HEARTBEAT_TOKEN}
ENVEOF
chmod 600 /home/ubuntu/.config/cloud-lab/laboratory.env
chown ubuntu:ubuntu /home/ubuntu/.config/cloud-lab/laboratory.env

sudo -u ubuntu \
    env TOOLS_DIR=/home/ubuntu/cloud-lab \
    bash /home/ubuntu/cloud-lab/fleet/laboratory/setup.sh
sudo -H -u ubuntu bash /home/ubuntu/cloud-lab/payload/keepalive/install.sh \
    /home/ubuntu/.config/cloud-lab/laboratory.env
