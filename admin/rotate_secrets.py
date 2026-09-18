#!/usr/bin/env python3
"""
Push rotated secrets from the local .env out to the live fleet.

Usage:
    python admin/rotate_secrets.py                  # push every secret present in .env
    python admin/rotate_secrets.py --token          # GITHUB_TOKEN only
    python admin/rotate_secrets.py --password       # admin console password only
    python admin/rotate_secrets.py --queue-key      # QUEUE_API_KEY only
    python admin/rotate_secrets.py --heartbeat      # FLEET_HEARTBEAT_TOKEN only
    python admin/rotate_secrets.py --dry-run        # report only, change nothing
    python admin/rotate_secrets.py --no-restart     # update files, skip service restarts

Rotating a secret means updating every copy of it, not just the one in .env.
For each reachable VM this updates:
  - ~/.config/cloud-lab/{role}.env      the role's environment file
  - ~/cloud-lab/.git/config             the token cloud-init baked into the clone URL
then restarts that role's cloud-lab services.

Secret values are never printed and never passed as command-line arguments
(argv is world-readable via `ps` on the remote host). They are embedded in a
script piped to the remote over stdin.

Reachability: only management is reachable from the laptop with your admin key.
Worker and laboratory trust management's fleet.key, so their updates hop
through management. A VM with no configured private IP is skipped.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_ROOT / ".env"

# Which secrets belong on which role. The console password and queue API key
# only mean anything on management, which is the only role serving the console.
ROLE_SECRETS = {
    "management": ["GITHUB_TOKEN", "ADMIN_PASSWORD_HASH", "QUEUE_API_KEY",
                   "FLEET_HEARTBEAT_TOKEN"],
    "worker":     ["GITHUB_TOKEN", "FLEET_HEARTBEAT_TOKEN"],
    "laboratory": ["GITHUB_TOKEN", "FLEET_HEARTBEAT_TOKEN"],
}

# Services restarted per role. The console is last on management so it is not
# killed mid-update.
ROLE_SERVICES = {
    "management": ["cloud-lab-orchestrator", "cloud-lab-heartbeat",
                   "cloud-lab-crosswatch", "cloud-lab-console"],
    "worker":     ["cloud-lab-a1-lottery", "cloud-lab-heartbeat",
                   "cloud-lab-crosswatch", "cloud-lab-keepalive"],
    "laboratory": ["cloud-lab-heartbeat", "cloud-lab-crosswatch",
                   "cloud-lab-keepalive"],
}

# Private-IP env key per non-management role.
ROLE_IP_KEY = {
    "worker": "FLEET_WORKER_PRIVATE_IP",
    "laboratory": "FLEET_LABORATORY_PRIVATE_IP",
}


def load_env(path: Path) -> dict[str, str]:
    """Parse a KEY=VALUE .env file. Values are never logged."""
    if not path.exists():
        sys.exit(f"ERROR: {path} not found. Copy .env.example to .env first.")
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


def hash_password(password: str) -> str:
    """Same scheme as admin/hash_password.py — must stay in sync."""
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 260000).hex()
    return f"sha256:260000:{salt}:{h}"


def normalize_repo(value: str) -> str:
    """
    Reduce any accepted FLEET_REPO spelling to `owner/repo`.

    cloud-init builds https://oauth2:<token>@github.com/${FLEET_REPO}.git, so the
    owner/repo form is what the clone URL needs — but users paste SSH and HTTPS
    URLs too.
    """
    value = value.strip()
    value = re.sub(r"^git@github\.com:", "", value)
    value = re.sub(r"^https://(?:[^@]+@)?github\.com/", "", value)
    value = re.sub(r"\.git$", "", value)
    return value.strip("/")


def build_remote_script(role: str, updates: dict[str, str], repo: str,
                        token: str | None, restart: bool) -> str:
    """
    Build the bash script executed on the remote host.

    Values are interpolated as Python reprs into a python3 heredoc, so quoting,
    special characters and shell metacharacters in a hash or token cannot break
    out of the string. Nothing here echoes a value.
    """
    env_path = f"$HOME/.config/cloud-lab/{role}.env"
    pairs = ", ".join(f"{k!r}: {v!r}" for k, v in updates.items())

    git_fix = ""
    if token and repo:
        new_url = f"https://oauth2:{token}@github.com/{repo}.git"
        git_fix = f"""
if [ -d "$HOME/cloud-lab/.git" ]; then
    git -C "$HOME/cloud-lab" remote set-url origin {new_url!r} && echo "OK   git remote url updated"
else
    echo "SKIP no git clone at ~/cloud-lab"
fi
"""

    restart_block = ""
    if restart:
        services = " ".join(ROLE_SERVICES.get(role, []))
        restart_block = f"""
for svc in {services}; do
    if systemctl cat "$svc.service" >/dev/null 2>&1; then
        sudo systemctl restart "$svc" && echo "OK   restarted $svc" || echo "FAIL restart $svc"
    else
        echo "SKIP $svc not installed"
    fi
done
"""

    # The backup is best-effort: on some fleets ~/.config/cloud-lab is root-owned
    # even though the env file inside is writable, so a failed copy must not abort
    # the update.
    return f"""set -u
ENV_FILE="{env_path}"
if [ ! -f "$ENV_FILE" ]; then
    echo "FAIL $ENV_FILE missing"
    exit 1
fi
cp "$ENV_FILE" "$ENV_FILE.bak.$(date +%s)" 2>/dev/null \\
    || echo "WARN could not write backup (directory not writable); continuing"

python3 - "$ENV_FILE" <<'PYEOF'
import sys

path = sys.argv[1]
updates = {{{pairs}}}

with open(path, encoding="utf-8") as fh:
    lines = fh.read().splitlines()

seen = set()
out = []
for line in lines:
    key = line.split("=", 1)[0].strip() if "=" in line else None
    if key in updates:
        out.append(f"{{key}}={{updates[key]}}")
        seen.add(key)
    else:
        out.append(line)

for key, value in updates.items():
    if key not in seen:
        out.append(f"{{key}}={{value}}")

with open(path, "w", encoding="utf-8") as fh:
    fh.write("\\n".join(out) + "\\n")

for key in updates:
    print(f"OK   {{key}} set in {{path.split('/')[-1]}}")
PYEOF

chmod 600 "$ENV_FILE"
{git_fix}{restart_block}
echo "DONE {role}"
"""


def run_remote(ssh_target: str, key_path: str, script: str,
               hop: tuple[str, str] | None = None) -> int:
    """
    Pipe `script` to bash on the remote host over stdin.

    hop = (inner_user_host, inner_key) routes through ssh_target to a second
    host; stdin passes straight through both legs.
    """
    if hop:
        inner_host, inner_key = hop
        remote_cmd = (
            f"ssh -i {inner_key} -o BatchMode=yes -o ConnectTimeout=10 "
            f"-o StrictHostKeyChecking=accept-new {inner_host} 'bash -s'"
        )
    else:
        remote_cmd = "bash -s"

    proc = subprocess.run(
        ["ssh", "-i", key_path, "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
         ssh_target, remote_cmd],
        input=script.encode("utf-8"),
        capture_output=True,
    )
    for stream in (proc.stdout, proc.stderr):
        text = stream.decode("utf-8", "replace").strip()
        if text:
            for line in text.splitlines():
                print(f"    {line}")
    return proc.returncode


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Push rotated secrets from .env to the live fleet.")
    parser.add_argument("--token", action="store_true", help="GITHUB_TOKEN only")
    parser.add_argument("--password", action="store_true", help="admin password only")
    parser.add_argument("--queue-key", action="store_true", help="QUEUE_API_KEY only")
    parser.add_argument("--heartbeat", action="store_true", help="FLEET_HEARTBEAT_TOKEN only")
    parser.add_argument("--dry-run", action="store_true", help="report only, change nothing")
    parser.add_argument("--no-restart", action="store_true", help="skip service restarts")
    args = parser.parse_args()

    selective = any([args.token, args.password, args.queue_key, args.heartbeat])
    want = {
        "GITHUB_TOKEN": args.token or not selective,
        "ADMIN_PASSWORD_HASH": args.password or not selective,
        "QUEUE_API_KEY": args.queue_key or not selective,
        "FLEET_HEARTBEAT_TOKEN": args.heartbeat or not selective,
    }

    env = load_env(ENV_FILE)

    key_path = os.path.expandvars(
        env.get("OCI_SSH_PRIVATE_KEY_PATH", "~/.ssh/fleet.key")).replace("~", str(Path.home()), 1)
    ssh_user = env.get("OCI_SSH_USER", "ubuntu")
    mgmt_host = env.get("OCI_MANAGEMENT_HOST", "").strip()
    repo = normalize_repo(env.get("FLEET_REPO", ""))

    if not mgmt_host:
        sys.exit("ERROR: OCI_MANAGEMENT_HOST not set in .env — launch management first.")

    # Resolve the actual values once. Anything absent from .env is skipped rather
    # than pushed as an empty string, which would silently disable a feature.
    values: dict[str, str] = {}

    if want["GITHUB_TOKEN"]:
        token = env.get("GITHUB_TOKEN", "").strip()
        if not token:
            if args.token:
                sys.exit("ERROR: GITHUB_TOKEN is empty in .env")
        elif not re.match(r"^(github_pat_|ghp_|gho_)", token):
            sys.exit("ERROR: GITHUB_TOKEN does not look like a GitHub token")
        else:
            values["GITHUB_TOKEN"] = token

    if want["ADMIN_PASSWORD_HASH"]:
        existing = env.get("ADMIN_PASSWORD_HASH", "").strip()
        password = env.get("ADMIN_PASSWORD", "").strip()
        if password:
            # Plaintext present: it is the source of truth, rehash it.
            values["ADMIN_PASSWORD_HASH"] = hash_password(password)
        elif existing:
            values["ADMIN_PASSWORD_HASH"] = existing
        elif args.password:
            sys.exit("ERROR: neither ADMIN_PASSWORD nor ADMIN_PASSWORD_HASH set in .env")

    for key in ("QUEUE_API_KEY", "FLEET_HEARTBEAT_TOKEN"):
        if want[key]:
            value = env.get(key, "").strip()
            if value:
                values[key] = value
            elif selective:
                sys.exit(f"ERROR: {key} is empty in .env")

    if not values:
        sys.exit("ERROR: nothing to push — no matching secrets are set in .env.")

    token_value = values.get("GITHUB_TOKEN")

    # Roles to visit: management directly, others only if a private IP is known.
    targets: list[tuple[str, tuple[str, str] | None]] = [("management", None)]
    for role, ip_key in ROLE_IP_KEY.items():
        ip = env.get(ip_key, "").strip()
        if ip:
            targets.append((role, (f"{ssh_user}@{ip}", "~/.ssh/fleet.key")))
        else:
            print(f"NOTE: {role} skipped — {ip_key} not set in .env")

    print(f"\nFleet secret rotation — repo {repo or '(FLEET_REPO unset)'}")
    print(f"  pushing: {', '.join(sorted(values))}")
    print(f"  targets: {', '.join(role for role, _ in targets)}")

    if args.dry_run:
        print("\nDRY RUN — nothing will be changed.")
        for role, hop in targets:
            applicable = [k for k in ROLE_SECRETS[role] if k in values]
            via = " (via management)" if hop else ""
            print(f"  {role}{via}: would set {', '.join(applicable) or '(nothing applicable)'}")
        return

    restart = not args.no_restart
    failures = 0

    for role, hop in targets:
        updates = {k: v for k, v in values.items() if k in ROLE_SECRETS[role]}
        if not updates:
            print(f"\n[{role}] nothing applicable — skipped")
            continue

        via = " (via management)" if hop else ""
        print(f"\n[{role}] {hop[0].split('@')[1] if hop else mgmt_host}{via}")
        script = build_remote_script(role, updates, repo, token_value, restart)
        rc = run_remote(f"{ssh_user}@{mgmt_host}", key_path, script, hop=hop)
        if rc != 0:
            failures += 1
            print(f"    ERROR: {role} update failed")

    print()
    if failures:
        sys.exit(f"{failures} host(s) failed — see output above.")
    print("All reachable hosts updated. No secret values were printed.")
    print("Verify with:  ssh <management> 'cd ~/cloud-lab && git ls-remote origin HEAD'")


if __name__ == "__main__":
    main()
