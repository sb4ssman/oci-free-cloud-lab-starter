#!/usr/bin/env python3
"""
SSH into a fleet VM by name.

Usage:
  admin/ssh-vm.sh management              open interactive shell from a POSIX shell
  admin\\ssh-vm.bat management            open interactive shell from Command Prompt or PowerShell
  admin/ssh-vm.sh laboratory -- <cmd>     run one command and exit

IP resolution order:
  1. OCI_<NAME_SLUG>_HOST in .env  (e.g. OCI_MANAGEMENT_HOST, OCI_LABORATORY_HOST)
  2. public_ip in vm-profiles/<name>.json  (written by check-all-vms)
  3. Bail with instructions.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT     = Path(__file__).resolve().parent.parent   # repo root
PROFILES = ROOT / "vm-profiles"


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


def expand(value: str, env: dict[str, str]) -> str:
    for name, val in sorted(env.items(), key=lambda i: len(i[0]), reverse=True):
        value = value.replace(f"$env:{name}", val).replace(f"%{name}%", val)
    return os.path.expandvars(value)


def resolve_ip(name: str, env: dict[str, str]) -> str:
    # management → OCI_MANAGEMENT_HOST, laboratory → OCI_LABORATORY_HOST
    env_key = f"OCI_{name.upper().replace('-', '_')}_HOST"
    ip = env.get(env_key, "").strip()
    if ip:
        return ip

    profile_path = PROFILES / f"{name}.json"
    if profile_path.exists():
        try:
            data = json.loads(profile_path.read_text(encoding="utf-8"))
            ip = data.get("public_ip", "")
            if ip:
                return ip
        except Exception:
            pass

    return ""


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        return 0

    name = sys.argv[1]

    extra: list[str] = []
    if "--" in sys.argv[2:]:
        idx = sys.argv.index("--", 2)
        extra = sys.argv[idx + 1:]

    env = parse_env(ROOT / ".env")
    env.update(os.environ)

    user = env.get("OCI_SSH_USER", "ubuntu")
    key_raw = env.get("OCI_SSH_PRIVATE_KEY_PATH", "")
    if not key_raw:
        print("Error: OCI_SSH_PRIVATE_KEY_PATH not set in .env")
        return 1
    key = Path(expand(key_raw, env)).expanduser()
    if not key.exists():
        print(f"Error: SSH key not found: {key}")
        return 1

    ip = resolve_ip(name, env)
    if not ip:
        env_key = f"OCI_{name.upper().replace('-', '_')}_HOST"
        print(f"No IP found for '{name}'.")
        print(f"  Set {env_key} in .env")
        print(f"  — or run check-all-vms to refresh vm-profiles/.")
        return 1

    cmd = [
        "ssh", "-i", str(key),
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=10",
        f"{user}@{ip}",
        *extra,
    ]

    print(f"[ssh] {name} ({ip}) as {user}")
    return subprocess.run(cmd).returncode


if __name__ == "__main__":
    raise SystemExit(main())
