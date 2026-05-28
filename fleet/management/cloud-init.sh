#!/bin/bash
# Cloud-init for the management VM — full one-click setup.
# Runs as root on first boot via OCI user-data.
# ${VAR} placeholders are substituted by oci_launch_until_available.py at launch time.
# ADMIN_PASSWORD_HASH is pre-computed locally — plaintext never enters user-data.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

echo "[cloud-init] Installing base packages..."
apt-get update -qq
apt-get install -y -qq \
    ca-certificates curl git jq python3 python3-venv tmux unzip \
    debian-keyring debian-archive-keyring apt-transport-https \
    iptables-persistent dnsutils

echo "[cloud-init] Installing OCI CLI..."
if [ ! -f /home/ubuntu/bin/oci ]; then
    sudo -u ubuntu bash -c "$(curl -L https://raw.githubusercontent.com/oracle/oci-cli/master/scripts/install/install.sh)" -- --accept-all-defaults
fi
ln -sf /home/ubuntu/bin/oci /usr/local/bin/oci

echo "[cloud-init] Installing Caddy..."
if ! command -v caddy > /dev/null 2>&1; then
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
        | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
        | tee /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -qq
    apt-get install -y -qq caddy
fi

echo "[cloud-init] Opening ports 80/443 in iptables..."
iptables -C INPUT -p tcp --dport 80  -j ACCEPT 2>/dev/null || iptables -I INPUT 5 -p tcp --dport 80  -j ACCEPT
iptables -C INPUT -p tcp --dport 443 -j ACCEPT 2>/dev/null || iptables -I INPUT 5 -p tcp --dport 443 -j ACCEPT
iptables -C INPUT -p tcp -s 10.0.0.0/16 --dport 8765 -j ACCEPT 2>/dev/null || iptables -I INPUT 5 -p tcp -s 10.0.0.0/16 --dport 8765 -j ACCEPT
netfilter-persistent save

echo "[cloud-init] Cloning fleet repo..."
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
# Requires IAM dynamic group + vault-read policy; key never in env or git.
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
# Option 2: Decode from base64 env var (fallback; rotate the key if it is ever compromised).
# Generate: base64 -w 0 < ~/.ssh/your_fleet.key
if [ ! -f /home/ubuntu/.ssh/fleet.key ] && [ -n "${FLEET_PRIVATE_KEY_B64}" ]; then
    echo "[cloud-init] Decoding fleet key from FLEET_PRIVATE_KEY_B64..."
    echo "${FLEET_PRIVATE_KEY_B64}" | base64 -d > /home/ubuntu/.ssh/fleet.key
    chmod 600 /home/ubuntu/.ssh/fleet.key
    chown ubuntu:ubuntu /home/ubuntu/.ssh/fleet.key
    sudo -u ubuntu ssh-keygen -y -f /home/ubuntu/.ssh/fleet.key \
         > /home/ubuntu/.ssh/fleet.key.pub
    echo "[cloud-init] Fleet key decoded from env."
fi
# Fallback: generate fresh. Worker/lab lose SSH access when management relaunches without Vault or B64.
if [ ! -f /home/ubuntu/.ssh/fleet.key ]; then
    echo "[cloud-init] Generating fresh fleet SSH keypair (consider OCI Vault to avoid mesh disruption)..."
    sudo -u ubuntu ssh-keygen -t ed25519 \
        -f /home/ubuntu/.ssh/fleet.key -N "" -C "${FLEET_NAME}-fleet"
fi
# Option 3: Add admin public key for human SSH recovery (always applied when set).
if [ -n "${ADMIN_SSH_PUBLIC_KEY}" ]; then
    echo "[cloud-init] Adding admin SSH recovery key to authorized_keys..."
    echo "${ADMIN_SSH_PUBLIC_KEY}" >> /home/ubuntu/.ssh/authorized_keys
    chmod 600 /home/ubuntu/.ssh/authorized_keys
    chown ubuntu:ubuntu /home/ubuntu/.ssh/authorized_keys
fi

echo "[cloud-init] Writing management.env..."
install -d -m 755 -o ubuntu -g ubuntu /home/ubuntu/.config/cloud-lab
MANAGEMENT_PRIVATE_IP="$(hostname -I | awk '{print $1}')"
# Single-quoted heredoc: bash won't re-expand values that Python already substituted.
cat > /home/ubuntu/.config/cloud-lab/management.env << 'ENVEOF'
OCI_AUTH_MODE=instance_principal
OCI_COMPARTMENT_ID=${OCI_COMPARTMENT_ID}
OCI_SUBNET_ID=${OCI_SUBNET_ID}
FLEET_MANAGEMENT_PRIVATE_IP=__MANAGEMENT_PRIVATE_IP__
NOTIFY_NTFY_TOPIC=${NOTIFY_NTFY_TOPIC}
GITHUB_TOKEN=${GITHUB_TOKEN}
FLEET_REPO=${FLEET_REPO}
FLEET_NAME=${FLEET_NAME}
FLEET_VM_NAME=management
QUEUE_API_KEY=${QUEUE_API_KEY}
FLEET_HEARTBEAT_TOKEN=${FLEET_HEARTBEAT_TOKEN}
ADMIN_DOMAIN=${ADMIN_DOMAIN}
ADMIN_CONSOLE_HOST=0.0.0.0
ADMIN_USERNAME=${ADMIN_USERNAME}
ADMIN_PASSWORD_HASH=${ADMIN_PASSWORD_HASH}
OCI_SSH_PUBLIC_KEY_PATH=/home/ubuntu/.ssh/fleet.key.pub
OCI_SSH_PRIVATE_KEY_PATH=/home/ubuntu/.ssh/fleet.key
OCI_SSH_USER=ubuntu
FLEET_OCI_VAULT_SECRET_OCID=${FLEET_OCI_VAULT_SECRET_OCID}
ADMIN_SSH_PUBLIC_KEY=${ADMIN_SSH_PUBLIC_KEY}
ENVEOF
sed -i "s/__MANAGEMENT_PRIVATE_IP__/${MANAGEMENT_PRIVATE_IP}/g" /home/ubuntu/.config/cloud-lab/management.env
chmod 600 /home/ubuntu/.config/cloud-lab/management.env
chown ubuntu:ubuntu /home/ubuntu/.config/cloud-lab/management.env

echo "[cloud-init] Running role setup (installs systemd services)..."
sudo -H -u ubuntu \
    env TOOLS_DIR=/home/ubuntu/cloud-lab \
    bash /home/ubuntu/cloud-lab/fleet/management/setup.sh

echo "[cloud-init] Enabling user session linger (services survive reboot)..."
loginctl enable-linger ubuntu

echo "[cloud-init] Configuring Caddy..."
cat > /etc/caddy/Caddyfile << 'CADDYEOF'
${ADMIN_DOMAIN} {
    reverse_proxy localhost:8765
}
CADDYEOF
systemctl enable caddy
systemctl restart caddy

echo "[cloud-init] Setting hostname..."
hostnamectl set-hostname management

echo "[cloud-init] Installing keepalive payload..."
sudo -H -u ubuntu bash /home/ubuntu/cloud-lab/payload/keepalive/install.sh \
    /home/ubuntu/.config/cloud-lab/management.env

echo ""
echo "[cloud-init] Done."
echo "Admin console: https://${ADMIN_DOMAIN}"
echo "(TLS cert issues within ~60s of DNS propagation)"
