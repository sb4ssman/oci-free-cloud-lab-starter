#!/usr/bin/env python3
"""
laboratory heartbeat — runs as a systemd timer.
POSTs a liveness ping to the management admin console every 4 hours.
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.request
from pathlib import Path


ENV_FILE = Path.home() / ".config" / "cloud-lab" / "laboratory.env"


def parse_env_file(path: Path) -> dict[str, str]:
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


def uptime() -> str:
    try:
        result = subprocess.run(["uptime", "-p"], stdout=subprocess.PIPE, text=True)
        return result.stdout.strip()
    except Exception:
        return "unknown"


def main() -> None:
    env = parse_env_file(ENV_FILE)
    env.update(os.environ)

    mgmt_ip = env.get("FLEET_MANAGEMENT_PRIVATE_IP", "")
    vm_name = env.get("FLEET_VM_NAME", "laboratory")
    heartbeat_token = env.get("FLEET_HEARTBEAT_TOKEN", "")

    if not mgmt_ip:
        print("[heartbeat] FLEET_MANAGEMENT_PRIVATE_IP not set — skipping.", flush=True)
        return

    payload = json.dumps({"vm_name": vm_name, "uptime": uptime()}).encode("utf-8")
    url = f"http://{mgmt_ip}:8765/heartbeat"
    print(f"[heartbeat] POSTing to {url} as {vm_name}...", flush=True)
    try:
        headers = {"Content-Type": "application/json"}
        if heartbeat_token:
            headers["Authorization"] = f"Bearer {heartbeat_token}"
        req = urllib.request.Request(
            url, data=payload,
            headers=headers,
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10).read()
        print("[heartbeat] Sent.", flush=True)
    except Exception as exc:
        print(f"[heartbeat] Failed: {exc}", flush=True)


if __name__ == "__main__":
    main()
