#!/usr/bin/env python3
"""
Management cross-watch — runs as a systemd timer on the management VM.
Checks OCI state of peer VMs every 6 hours.
Sends a direct ntfy alert if any expected-RUNNING VM is TERMINATED or missing.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.request
from pathlib import Path


TOOLS_DIR = Path(__file__).resolve().parent.parent.parent   # repo root
ENV_FILE  = Path.home() / ".config" / "cloud-lab" / "management.env"
THIS_ROLE = "management"


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


def _vm_was_previously_running(name: str) -> bool:
    """Return True if this VM has a saved profile with a public IP — i.e., it ran before."""
    profile_path = TOOLS_DIR / "vm-profiles" / f"{name}.json"
    if not profile_path.exists():
        return False
    try:
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        return bool(profile.get("public_ip", ""))
    except Exception:
        return False


def _lottery_is_running() -> bool:
    """Check if the A1 lottery service is active on the worker — means lab is still awaiting capacity."""
    try:
        result = subprocess.run(
            ["sudo", "journalctl", "-u", "cloud-lab-orchestrator", "-n", "20",
             "--no-pager", "--output=cat"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=10,
        )
        return "lottery" in result.stdout.lower() or "a1" in result.stdout.lower()
    except Exception:
        return False


_STATE_DESCRIPTIONS = {
    "NOT FOUND":    "OCI has no record of this VM — it was never created or was fully deleted.",
    "TERMINATED":   "OCI terminated this VM — capacity reclaim or explicit delete.",
    "TERMINATING":  "OCI is in the process of terminating this VM.",
    "STOPPED":      "VM exists in OCI but is stopped (not billed for compute, but still occupies quota).",
    "STOPPING":     "VM is in the process of stopping.",
    "UNKNOWN":      "OCI returned an unrecognized lifecycle state.",
}


def ntfy_alert(topic: str, title: str, message: str, server: str = "https://ntfy.sh",
               priority: str = "high", tags: str = "warning,red_circle") -> None:
    if not topic:
        return
    try:
        req = urllib.request.Request(
            f"{server.rstrip('/')}/{topic}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Tags": tags, "Priority": priority},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as exc:
        print(f"[crosswatch] ntfy failed: {exc}", flush=True)


def main() -> None:
    env = parse_env_file(ENV_FILE)
    env.update(os.environ)

    compartment_id = env.get("OCI_COMPARTMENT_ID", "")
    topic       = env.get("NOTIFY_NTFY_TOPIC", "")
    ntfy_server = env.get("NOTIFY_NTFY_SERVER", "https://ntfy.sh")
    fleet_name  = env.get("FLEET_NAME", "Cloud Lab")
    this_vm     = env.get("FLEET_VM_NAME", THIS_ROLE)

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
        ntfy_alert(topic, f"{fleet_name} Cross-Watch Error",
                   f"{this_vm} could not query OCI: {exc}", ntfy_server)
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
            was_running = _vm_was_previously_running(name)
            state_desc  = _STATE_DESCRIPTIONS.get(state, f"OCI state: {state}")

            if not was_running and state in ("NOT FOUND", "UNKNOWN"):
                # VM has never had a public IP — it was never fully provisioned.
                # For laboratory this is normal during the A1 Flex lottery wait.
                if name == "laboratory":
                    context = (
                        f"laboratory has not been provisioned yet (state: {state}).\n"
                        f"This is expected — worker is running the A1 Flex capacity lottery.\n"
                        f"No action needed. You will be notified when the lottery succeeds."
                    )
                    priority = "default"
                    tags     = "hourglass_flowing_sand"
                else:
                    context = (
                        f"{name} is {state} and has never been provisioned.\n"
                        f"{state_desc}\n"
                        f"Fleet orchestrator will attempt to launch it."
                    )
                    priority = "high"
                    tags     = "warning,red_circle"
            elif was_running:
                # VM was running before — this is an unexpected loss.
                context = (
                    f"{name} was previously running but is now {state}.\n"
                    f"{state_desc}\n"
                    f"Possible cause: Oracle idle-reclaim or capacity pressure.\n"
                    f"Fleet orchestrator will attempt automatic relaunch."
                )
                priority = "urgent"
                tags     = "rotating_light,red_circle"
            else:
                context = (
                    f"{name} is {state}.\n"
                    f"{state_desc}\n"
                    f"Fleet orchestrator will attempt recovery."
                )
                priority = "high"
                tags     = "warning"

            ntfy_alert(topic, f"{fleet_name}: {name} is {state}", context,
                       ntfy_server, priority=priority, tags=tags)

    print("[crosswatch] Done.", flush=True)


if __name__ == "__main__":
    main()
