#!/usr/bin/env python3
"""
Daily fleet report — runs once per day via user crontab on the management VM.

Queries OCI for current instance states, compares to fleet.json expectations,
and sends a summary via ntfy. More comprehensive than the 4-hour heartbeat.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


CONFIG_DIR = Path.home() / ".config" / "cloud-lab"
TOOLS_DIR  = Path.home() / "cloud-lab"


def find_env_file() -> Path | None:
    for candidate in ["management.env", "worker.env", "laboratory.env"]:
        p = CONFIG_DIR / candidate
        if p.exists():
            return p
    return None


def parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip(); v = v.strip().strip('"').strip("'")
        if k:
            out[k] = v
    return out


def oci_instance_states(compartment_id: str) -> dict[str, str]:
    oci = shutil.which("oci") or "/home/ubuntu/bin/oci"
    child_env = os.environ.copy()
    child_env["OCI_CLI_AUTH"] = "instance_principal"
    child_env["OCI_CLI_SUPPRESS_FILE_PERMISSIONS_WARNING"] = "True"
    result = subprocess.run(
        [oci, "compute", "instance", "list",
         "--compartment-id", compartment_id, "--all"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", timeout=60,
        env=child_env,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    data = json.loads(result.stdout)
    return {
        item["display-name"]: item.get("lifecycle-state", "UNKNOWN")
        for item in data.get("data", [])
        if item.get("display-name")
    }


def main() -> None:
    env_file = find_env_file()
    if not env_file:
        print("[fleet_report] No cloud-lab env file — skipping.", flush=True)
        return

    env = parse_env_file(env_file)
    env.update(os.environ)

    vm_name = env.get("FLEET_VM_NAME", "unknown")
    if vm_name != "management":
        print(f"[fleet_report] {vm_name} is not management — skipping owner ntfy report.", flush=True)
        return

    topic       = env.get("NOTIFY_NTFY_TOPIC", "")
    ntfy_server = env.get("NOTIFY_NTFY_SERVER", "https://ntfy.sh")
    fleet_name  = env.get("FLEET_NAME", "Cloud Lab")
    compartment_id = env.get("OCI_COMPARTMENT_ID", "")

    fleet_file = TOOLS_DIR / "fleet.json"
    if not fleet_file.exists():
        print("[fleet_report] fleet.json not found.", flush=True)
        return

    fleet = json.loads(fleet_file.read_text()).get("vms", [])
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [f"Daily fleet report — {now}\n"]
    all_ok = True

    if compartment_id:
        try:
            states = oci_instance_states(compartment_id)
        except Exception as exc:
            states = {}
            lines.append(f"OCI query failed: {exc}")
            all_ok = False
    else:
        states = {}
        lines.append("OCI_COMPARTMENT_ID not set — no live data")
        all_ok = False

    for vm in fleet:
        name = vm["name"]
        expected = vm.get("expected_state", "")
        actual = states.get(name, "NOT FOUND")
        ok = actual in ("RUNNING", "STARTING", "PROVISIONING") if expected == "RUNNING" else True
        if not ok:
            all_ok = False
        status_icon = "✓" if ok else "✗"
        lines.append(f"{status_icon} {name}: {actual} (expected {expected})")

    body = "\n".join(lines)
    print(f"[fleet_report]\n{body}", flush=True)

    if not topic:
        return

    tags = "white_check_mark,green_circle" if all_ok else "warning,red_circle"
    title = f"{fleet_name}: daily report {'OK' if all_ok else 'ISSUES FOUND'}"
    try:
        req = urllib.request.Request(
            f"{ntfy_server.rstrip('/')}/{topic}",
            data=body.encode("utf-8"),
            headers={"Title": title, "Tags": tags},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10).read()
        print("[fleet_report] ntfy sent.", flush=True)
    except Exception as exc:
        print(f"[fleet_report] ntfy failed: {exc}", flush=True)


if __name__ == "__main__":
    main()
