#!/usr/bin/env python3
"""
Re-apply full setup to a running management VM from your local machine.

Used for: first-time setup if cloud-init didn't complete, updating config
after code changes, or recovering a broken management VM.

Reads .env, builds the setup script with values substituted, SSHes into the
management VM, and pipes it through bash.

Usage:
  admin/bootstrap-mgmt-vm.sh            Run full setup from a POSIX shell
  admin\\bootstrap-mgmt-vm.bat          Run full setup from Command Prompt or PowerShell
  ... --dry-run                         Print the script that would run; don't connect
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import shlex
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent   # repo root


def parse_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip(); v = v.strip().strip('"').strip("'")
        if k:
            out[k] = v
    return out


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 260000).hex()
    return f"sha256:260000:{salt}:{h}"


def expand(value: str, env: dict[str, str]) -> str:
    for name, val in sorted(env.items(), key=lambda i: len(i[0]), reverse=True):
        value = value.replace(f"$env:{name}", val).replace(f"%{name}%", val)
    return os.path.expandvars(value)


def q(value: str) -> str:
    return shlex.quote(value)


def clone_url_for_script(repo: str, token: str) -> str:
    repo = repo.strip()
    if not repo:
        return ""
    if repo.startswith("git@") or repo.startswith("ssh://"):
        return repo
    if repo.startswith("https://"):
        if token and repo.startswith("https://github.com/"):
            return repo.replace("https://github.com/", f"https://oauth2:{token}@github.com/", 1)
        return repo
    repo_path = repo
    if repo_path.endswith(".git"):
        repo_path = repo_path[:-4]
    if token:
        return f"https://oauth2:{token}@github.com/{repo_path}.git"
    return f"https://github.com/{repo_path}.git"


def build_script(env: dict[str, str]) -> str:
    token        = env.get("GITHUB_TOKEN", "")
    repo         = env.get("FLEET_REPO", "")
    compartment  = env.get("OCI_COMPARTMENT_ID", "")
    subnet       = env.get("OCI_SUBNET_ID", "")
    ntfy         = env.get("NOTIFY_NTFY_TOPIC", "")
    admin_domain = env.get("ADMIN_DOMAIN", "")
    admin_user   = env.get("ADMIN_USERNAME", "admin")
    admin_pass   = env.get("ADMIN_PASSWORD", "")
    admin_hash   = env.get("ADMIN_PASSWORD_HASH", "")
    fleet_name   = env.get("FLEET_NAME", "cloud-lab")
    queue_key    = env.get("QUEUE_API_KEY", "")
    heartbeat    = env.get("FLEET_HEARTBEAT_TOKEN", "")

    missing = [k for k, v in [
        ("FLEET_REPO",         repo),
        ("OCI_COMPARTMENT_ID", compartment),
        ("ADMIN_DOMAIN",       admin_domain),
    ] if not v]
    if not admin_hash and not admin_pass:
        missing.append("ADMIN_PASSWORD_HASH")
    if missing:
        raise SystemExit(f"Missing required values in .env: {', '.join(missing)}")

    # Prefer a precomputed hash. If plaintext is supplied, hash locally before sending.
    pw_hash = admin_hash or hash_password(admin_pass)
    clone_url = clone_url_for_script(repo, token)

    return f"""set -euo pipefail

echo "[bootstrap] Installing packages..."
sudo apt-get update -qq
sudo apt-get install -y -qq ca-certificates curl git jq python3 python3-venv tmux unzip debian-keyring debian-archive-keyring apt-transport-https

echo "[bootstrap] Installing OCI CLI..."
if ! command -v oci > /dev/null 2>&1 && [ ! -f "$HOME/bin/oci" ]; then
    bash -c "$(curl -L https://raw.githubusercontent.com/oracle/oci-cli/master/scripts/install/install.sh)" -- --accept-all-defaults
fi
if [ -f "$HOME/bin/oci" ] && [ ! -f /usr/local/bin/oci ]; then
    sudo ln -sf "$HOME/bin/oci" /usr/local/bin/oci
fi
oci --version

echo "[bootstrap] Installing Caddy..."
if ! command -v caddy > /dev/null 2>&1; then
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
    sudo apt-get update -qq
    sudo apt-get install -y -qq caddy
fi

echo "[bootstrap] Cloning or updating fleet repo..."
if [ -d "$HOME/cloud-lab/.git" ]; then
    git -C "$HOME/cloud-lab" pull --ff-only
else
    git clone {q(clone_url)} "$HOME/cloud-lab"
fi

echo "[bootstrap] Generating fleet SSH keypair..."
if [ ! -f "$HOME/.ssh/fleet.key" ]; then
    ssh-keygen -t ed25519 -f "$HOME/.ssh/fleet.key" -N "" -C {q(fleet_name + "-fleet")}
fi

echo "[bootstrap] Patching management config..."
mkdir -p "$HOME/.config/cloud-lab"
touch "$HOME/.config/cloud-lab/management.env"
chmod 600 "$HOME/.config/cloud-lab/management.env"
MGMT_PRIVATE_IP="$(hostname -I | awk '{{print $1}}')"

# patch_key: add only if missing (preserves manual edits).
patch_key() {{
    local key="$1" val="$2"
    grep -q "^${{key}}=" "$HOME/.config/cloud-lab/management.env" \
        || echo "${{key}}=${{val}}" >> "$HOME/.config/cloud-lab/management.env"
}}

# force_key: always update (must stay in sync with local .env).
force_key() {{
    local key="$1" val="$2"
    if grep -q "^${{key}}=" "$HOME/.config/cloud-lab/management.env"; then
        sed -i "s|^${{key}}=.*|${{key}}=${{val}}|" "$HOME/.config/cloud-lab/management.env"
    else
        echo "${{key}}=${{val}}" >> "$HOME/.config/cloud-lab/management.env"
    fi
}}

patch_key OCI_AUTH_MODE                instance_principal
patch_key OCI_COMPARTMENT_ID           {q(compartment)}
patch_key OCI_SUBNET_ID                {q(subnet)}
patch_key NOTIFY_NTFY_TOPIC            {q(ntfy)}
patch_key GITHUB_TOKEN                 {q(token)}
patch_key FLEET_REPO                   {q(repo)}
patch_key FLEET_NAME                   {q(fleet_name)}
patch_key FLEET_VM_NAME                management
patch_key ADMIN_DOMAIN                 {q(admin_domain)}
patch_key ADMIN_CONSOLE_HOST           0.0.0.0
patch_key QUEUE_API_KEY                {q(queue_key)}
patch_key FLEET_HEARTBEAT_TOKEN        {q(heartbeat)}
patch_key OCI_SSH_PUBLIC_KEY_PATH      "$HOME/.ssh/fleet.key.pub"
patch_key OCI_SSH_PRIVATE_KEY_PATH     "$HOME/.ssh/fleet.key"
patch_key OCI_SSH_USER                 ubuntu

force_key FLEET_MANAGEMENT_PRIVATE_IP  "$MGMT_PRIVATE_IP"
force_key ADMIN_USERNAME               {q(admin_user)}
force_key ADMIN_PASSWORD_HASH          {q(pw_hash)}

echo "[bootstrap] Running role setup..."
TOOLS_DIR="$HOME/cloud-lab" bash "$HOME/cloud-lab/fleet/management/setup.sh"

echo "[bootstrap] Opening ports 80/443 and internal 8765 in iptables..."
sudo apt-get install -y -qq iptables-persistent
if ! sudo iptables -C INPUT -p tcp --dport 80 -j ACCEPT 2>/dev/null; then
    sudo iptables -I INPUT 5 -p tcp --dport 80 -j ACCEPT
fi
if ! sudo iptables -C INPUT -p tcp --dport 443 -j ACCEPT 2>/dev/null; then
    sudo iptables -I INPUT 5 -p tcp --dport 443 -j ACCEPT
fi
if ! sudo iptables -C INPUT -p tcp -s 10.0.0.0/16 --dport 8765 -j ACCEPT 2>/dev/null; then
    sudo iptables -I INPUT 5 -p tcp -s 10.0.0.0/16 --dport 8765 -j ACCEPT
fi
sudo netfilter-persistent save

echo "[bootstrap] Configuring Caddy..."
sudo tee /etc/caddy/Caddyfile > /dev/null << 'CADDYEOF'
{admin_domain} {{
    reverse_proxy localhost:8765
}}
CADDYEOF
sudo systemctl enable caddy
sudo systemctl restart caddy

echo "[bootstrap] Installing keepalive payload..."
bash "$HOME/cloud-lab/payload/keepalive/install.sh" "$HOME/.config/cloud-lab/management.env"

echo "[bootstrap] Fixing hostname..."
sudo hostnamectl set-hostname management

echo ""
echo "Bootstrap complete."
echo "Admin console: https://{admin_domain}"
echo "(TLS cert issues within ~60s on first run)"
"""


def main() -> int:
    dry_run = "--dry-run" in sys.argv

    env = parse_env(ROOT / ".env")
    env.update(os.environ)

    ip = env.get("OCI_MANAGEMENT_HOST", "").strip()
    if not ip:
        print("Error: OCI_MANAGEMENT_HOST not set in .env")
        return 1

    user = env.get("OCI_SSH_USER", "ubuntu")
    key_raw = env.get("OCI_SSH_PRIVATE_KEY_PATH", "")
    if not key_raw:
        print("Error: OCI_SSH_PRIVATE_KEY_PATH not set in .env")
        return 1
    key = Path(expand(key_raw, env)).expanduser()
    if not key.exists():
        print(f"Error: SSH key not found: {key}")
        return 1

    script = build_script(env)

    if dry_run:
        print("-- DRY RUN -- script that would run on the VM --")
        redacted = script
        for key in ("GITHUB_TOKEN", "ADMIN_PASSWORD", "NOTIFY_NTFY_TOPIC", "QUEUE_API_KEY", "FLEET_HEARTBEAT_TOKEN"):
            val = env.get(key, "")
            if val:
                redacted = redacted.replace(val, f"***{key}***")
        # redact any PBKDF2 hash line (computed value, different salt each run)
        redacted = re.sub(
            r"sha256:\d+:[0-9a-f]{32}:[0-9a-f]{64}",
            "***ADMIN_PASSWORD_HASH***",
            redacted,
        )
        print(redacted)
        return 0

    print(f"[bootstrap] Connecting to management ({ip})...")
    result = subprocess.run(
        ["ssh", "-i", str(key),
         "-o", "BatchMode=yes",
         "-o", "ConnectTimeout=15",
         "-o", "StrictHostKeyChecking=accept-new",
         f"{user}@{ip}", "bash -s"],
        input=script.encode("utf-8"),
    )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
