#!/bin/bash
# Cloud-init bootstrap for the worker VM.
# Runs as root on first boot via OCI user-data.
# ${VAR} placeholders are substituted by oci_launch_until_available.py at launch time.
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

echo "[cloud-init] Setting up fleet SSH keypair..."
# Option 1: Retrieve private key from OCI Vault via instance principal (most secure).
if [ -n "${FLEET_OCI_VAULT_SECRET_OCID}" ]; then
    echo "[cloud-init] Retrieving fleet key from OCI Vault (${FLEET_OCI_VAULT_SECRET_OCID})..."
    if oci secrets secret-bundle get \
            --secret-id "${FLEET_OCI_VAULT_SECRET_OCID}" \
            --auth instance_principal \
            --query 'data."secret-bundle-content".content' \
            --raw-output 2>/dev/null \
        | base64 -d > /home/ubuntu/.ssh/fleet.key.tmp \
        && [ -s /home/ubuntu/.ssh/fleet.key.tmp ]; then
        mv /home/ubuntu/.ssh/fleet.key.tmp /home/ubuntu/.ssh/fleet.key
        chmod 600 /home/ubuntu/.ssh/fleet.key
        chown ubuntu:ubuntu /home/ubuntu/.ssh/fleet.key
        sudo -u ubuntu ssh-keygen -y -f /home/ubuntu/.ssh/fleet.key \
             > /home/ubuntu/.ssh/fleet.key.pub
        echo "[cloud-init] Fleet key loaded from Vault."
    else
        rm -f /home/ubuntu/.ssh/fleet.key.tmp
        echo "[cloud-init] WARNING: Vault retrieval failed; falling through to next option."
    fi
fi
# Option 2: Decode from base64 env var (fallback).
if [ ! -f /home/ubuntu/.ssh/fleet.key ] && [ -n "${FLEET_PRIVATE_KEY_B64}" ]; then
    echo "[cloud-init] Decoding fleet key from FLEET_PRIVATE_KEY_B64..."
    echo "${FLEET_PRIVATE_KEY_B64}" | base64 -d > /home/ubuntu/.ssh/fleet.key
    chmod 600 /home/ubuntu/.ssh/fleet.key
    chown ubuntu:ubuntu /home/ubuntu/.ssh/fleet.key
    sudo -u ubuntu ssh-keygen -y -f /home/ubuntu/.ssh/fleet.key \
         > /home/ubuntu/.ssh/fleet.key.pub
    echo "[cloud-init] Fleet key decoded from env."
fi
# Fallback: generate fresh (lab loses access when worker relaunches without Vault or B64).
if [ ! -f /home/ubuntu/.ssh/fleet.key ]; then
    echo "[cloud-init] Generating fresh fleet SSH keypair..."
    sudo -u ubuntu ssh-keygen -t ed25519 \
        -f /home/ubuntu/.ssh/fleet.key -N "" -C "${FLEET_NAME}-worker-fleet"
fi
# Option 3: Add admin public key for human SSH recovery (always applied when set).
if [ -n "${ADMIN_SSH_PUBLIC_KEY}" ]; then
    echo "[cloud-init] Adding admin SSH recovery key to authorized_keys..."
    echo "${ADMIN_SSH_PUBLIC_KEY}" >> /home/ubuntu/.ssh/authorized_keys
    chmod 600 /home/ubuntu/.ssh/authorized_keys
    chown ubuntu:ubuntu /home/ubuntu/.ssh/authorized_keys
fi

install -d -m 755 -o ubuntu -g ubuntu /home/ubuntu/.config/cloud-lab
cat > /home/ubuntu/.config/cloud-lab/worker.env << 'ENVEOF'
OCI_AUTH_MODE=instance_principal
OCI_COMPARTMENT_ID=${OCI_COMPARTMENT_ID}
OCI_SUBNET_ID=${OCI_SUBNET_ID}
FLEET_MANAGEMENT_PRIVATE_IP=${FLEET_MANAGEMENT_PRIVATE_IP}
NOTIFY_NTFY_TOPIC=${NOTIFY_NTFY_TOPIC}
GITHUB_TOKEN=${GITHUB_TOKEN}
FLEET_REPO=${FLEET_REPO}
FLEET_NAME=${FLEET_NAME}
FLEET_VM_NAME=worker
FLEET_HEARTBEAT_TOKEN=${FLEET_HEARTBEAT_TOKEN}
OCI_SSH_PUBLIC_KEY_PATH=/home/ubuntu/.ssh/fleet.key.pub
OCI_SSH_PRIVATE_KEY_PATH=/home/ubuntu/.ssh/fleet.key
OCI_SSH_USER=ubuntu
FLEET_OCI_VAULT_SECRET_OCID=${FLEET_OCI_VAULT_SECRET_OCID}
ADMIN_SSH_PUBLIC_KEY=${ADMIN_SSH_PUBLIC_KEY}
ENVEOF
chmod 600 /home/ubuntu/.config/cloud-lab/worker.env
chown ubuntu:ubuntu /home/ubuntu/.config/cloud-lab/worker.env

sudo -u ubuntu \
    env TOOLS_DIR=/home/ubuntu/cloud-lab \
    bash /home/ubuntu/cloud-lab/fleet/worker/setup.sh

loginctl enable-linger ubuntu

sudo -H -u ubuntu bash /home/ubuntu/cloud-lab/payload/keepalive/install.sh \
    /home/ubuntu/.config/cloud-lab/worker.env
