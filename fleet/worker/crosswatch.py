#!/usr/bin/env python3
"""
Worker cross-watch — runs as a systemd timer on the worker VM.
Checks OCI state of peer VMs every 6 hours.
Reports anomalies to the management VM; management is the only ntfy speaker.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.request
from pathlib import Path


TOOLS_DIR = Path(__file__).resolve().parent.parent.parent   # repo root
ENV_FILE  = Path.home() / ".config" / "cloud-lab" / "worker.env"


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


def report_to_management(mgmt_ip: str, vm_name: str, event: str, details: dict) -> None:
    if not mgmt_ip:
        print("[crosswatch] FLEET_MANAGEMENT_PRIVATE_IP not set — cannot report event.", flush=True)
        return
    payload = json.dumps({
        "vm_name": vm_name,
        "uptime": "crosswatch",
        "event": event,
        "details": details,
    }).encode("utf-8")
    url = f"http://{mgmt_ip}:8765/heartbeat"
    try:
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as exc:
        print(f"[crosswatch] management report failed: {exc}", flush=True)


def main() -> None:
    env = parse_env_file(ENV_FILE)
    env.update(os.environ)

    compartment_id = env.get("OCI_COMPARTMENT_ID", "")
    mgmt_ip = env.get("FLEET_MANAGEMENT_PRIVATE_IP", "")
    this_vm = env.get("FLEET_VM_NAME", "worker")

    if not compartment_id:
        print("[crosswatch] OCI_COMPARTMENT_ID not set — skipping.", flush=True)
        return

    fleet_file = TOOLS_DIR / "fleet.json"
    fleet = json.loads(fleet_file.read_text(encoding="utf-8")).get("vms", [])

    print("[crosswatch] Querying OCI instance states...", flush=True)
    try:
        states = oci_instance_states(compartment_id)
    except Exception as exc:
        print(f"[crosswatch] OCI query failed: {exc}", flush=True)
        report_to_management(mgmt_ip, this_vm, "crosswatch_error", {"error": str(exc)})
        return

    for vm in fleet:
        name = vm["name"]
        if name == this_vm:
            continue
        if vm.get("expected_state") != "RUNNING":
            continue

        state = states.get(name, "NOT FOUND")
        print(f"[crosswatch] {name}: {state}", flush=True)

        if state not in ("RUNNING", "STARTING", "PROVISIONING"):
            report_to_management(
                mgmt_ip,
                this_vm,
                "peer_unhealthy",
                {"peer": name, "state": state, "expected": "RUNNING"},
            )

    print("[crosswatch] Done.", flush=True)


if __name__ == "__main__":
    main()
