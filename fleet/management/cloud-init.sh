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
sudo -u ubuntu git clone \
    "https://oauth2:${GITHUB_TOKEN}@github.com/${FLEET_REPO}.git" \
    /home/ubuntu/cloud-lab \
  || sudo -u ubuntu git -C /home/ubuntu/cloud-lab pull --ff-only

echo "[cloud-init] Generating fleet SSH keypair..."
if [ ! -f /home/ubuntu/.ssh/fleet.key ]; then
    sudo -u ubuntu ssh-keygen -t ed25519 \
        -f /home/ubuntu/.ssh/fleet.key -N "" -C "${FLEET_NAME}-fleet"
fi

echo "[cloud-init] Writing management.env..."
mkdir -p /home/ubuntu/.config/cloud-lab
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
ADMIN_DOMAIN=${ADMIN_DOMAIN}
ADMIN_CONSOLE_HOST=0.0.0.0
ADMIN_USERNAME=${ADMIN_USERNAME}
ADMIN_PASSWORD_HASH=${ADMIN_PASSWORD_HASH}
OCI_SSH_PUBLIC_KEY_PATH=/home/ubuntu/.ssh/fleet.key.pub
OCI_SSH_PRIVATE_KEY_PATH=/home/ubuntu/.ssh/fleet.key
OCI_SSH_USER=ubuntu
ENVEOF
sed -i "s/__MANAGEMENT_PRIVATE_IP__/${MANAGEMENT_PRIVATE_IP}/g" /home/ubuntu/.config/cloud-lab/management.env
chmod 600 /home/ubuntu/.config/cloud-lab/management.env
chown ubuntu:ubuntu /home/ubuntu/.config/cloud-lab/management.env

echo "[cloud-init] Running role setup (installs systemd services)..."
sudo -H -u ubuntu \
    env TOOLS_DIR=/home/ubuntu/cloud-lab \
    bash /home/ubuntu/cloud-lab/fleet/management/setup.sh

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
