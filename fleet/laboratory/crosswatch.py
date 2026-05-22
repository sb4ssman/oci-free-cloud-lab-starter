#!/usr/bin/env python3
"""laboratory cross-watch — runs as a systemd timer on the laboratory VM.
Checks OCI state of peer VMs every 6 hours.

Normal peers: reports anomalies to the management heartbeat endpoint.
Management down: sends ntfy directly, then defers any relaunch to the worker
  VM (which has the relaunch logic). If worker is also down, sends an urgent
  ntfy and waits for human intervention.

IP re-discovery (Option B): management's current private IP is always resolved
from OCI at crosswatch time, not from the potentially-stale env file. The env
file is updated and the heartbeat service is triggered if the IP has changed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.request
from pathlib import Path


TOOLS_DIR = Path(__file__).resolve().parent.parent.parent   # repo root
ENV_FILE  = Path.home() / ".config" / "cloud-lab" / "laboratory.env"


# ── env parsing ───────────────────────────────────────────────────────────────

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


def update_env_value(path: Path, key: str, value: str) -> None:
    """Update KEY=VALUE in an env file in-place, or append if absent."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    updated = False
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(f"{key}=") or stripped.startswith(f"{key} ="):
            new_lines.append(f'{key}="{value}"')
            updated = True
        else:
            new_lines.append(line)
    if not updated:
        new_lines.append(f'{key}="{value}"')
    path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


# ── OCI helpers ───────────────────────────────────────────────────────────────

def _oci_env() -> dict[str, str]:
    e = os.environ.copy()
    e["OCI_CLI_AUTH"] = "instance_principal"
    e["OCI_CLI_SUPPRESS_FILE_PERMISSIONS_WARNING"] = "True"
    return e


def _oci(args: list[str], timeout: int = 60) -> dict:
    oci = shutil.which("oci") or "/home/ubuntu/bin/oci"
    result = subprocess.run(
        [oci, *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", timeout=timeout,
        env=_oci_env(),
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    return json.loads(result.stdout)


def oci_instance_states(compartment_id: str) -> dict[str, dict]:
    """Returns {display_name: {"state": ..., "id": ...}} for all instances."""
    data = _oci(["compute", "instance", "list",
                 "--compartment-id", compartment_id, "--all"])
    out: dict[str, dict] = {}
    for item in data.get("data", []):
        name = item.get("display-name")
        if name:
            out[name] = {
                "state": item.get("lifecycle-state", "UNKNOWN"),
                "id": item.get("id", ""),
            }
    return out


def oci_private_ip(compartment_id: str, instance_id: str) -> str:
    """Return the primary private IP of an instance, or empty string."""
    if not instance_id:
        return ""
    try:
        attachments = _oci(
            ["compute", "vnic-attachment", "list",
             "--compartment-id", compartment_id,
             "--instance-id", instance_id, "--all"],
        ).get("data", [])
        for att in attachments:
            vnic_id = att.get("vnic-id", "")
            if not vnic_id:
                continue
            vnic = _oci(["network", "vnic", "get", "--vnic-id", vnic_id]).get("data", {})
            ip = vnic.get("private-ip", "")
            if ip:
                return ip
    except Exception as exc:
        print(f"[crosswatch] private-IP lookup failed: {exc}", flush=True)
    return ""


# ── notifications ─────────────────────────────────────────────────────────────

def notify_ntfy(topic: str, title: str, message: str, priority: str = "high") -> None:
    """Send a notification directly to ntfy, bypassing the management VM."""
    if not topic:
        return
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{topic}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": priority, "Tags": "rotating_light,cloud"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10).read()
        print(f"[crosswatch] ntfy sent: {title}", flush=True)
    except Exception as exc:
        print(f"[crosswatch] ntfy failed: {exc}", flush=True)


def report_to_management(mgmt_ip: str, vm_name: str, event: str, details: dict, heartbeat_token: str = "") -> None:
    if not mgmt_ip:
        print("[crosswatch] no management IP — cannot report event.", flush=True)
        return
    payload = json.dumps({
        "vm_name": vm_name,
        "uptime": "crosswatch",
        "event": event,
        "details": details,
    }).encode("utf-8")
    try:
        headers = {"Content-Type": "application/json"}
        if heartbeat_token:
            headers["Authorization"] = f"Bearer {heartbeat_token}"
        req = urllib.request.Request(
            f"http://{mgmt_ip}:8765/heartbeat",
            data=payload,
            headers=headers,
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as exc:
        print(f"[crosswatch] management report failed: {exc}", flush=True)


def trigger_heartbeat() -> None:
    """Start heartbeat service immediately so management gets a fresh liveness report."""
    try:
        subprocess.run(
            ["sudo", "systemctl", "start", "cloud-lab-heartbeat.service"],
            timeout=30, check=False,
        )
        print("[crosswatch] Heartbeat triggered.", flush=True)
    except Exception as exc:
        print(f"[crosswatch] Failed to trigger heartbeat: {exc}", flush=True)


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    env = parse_env_file(ENV_FILE)
    env.update(os.environ)

    compartment_id = env.get("OCI_COMPARTMENT_ID", "")
    ntfy_topic     = env.get("NOTIFY_NTFY_TOPIC", "")
    this_vm        = env.get("FLEET_VM_NAME", "laboratory")
    heartbeat_token = env.get("FLEET_HEARTBEAT_TOKEN", "")

    if not compartment_id:
        print("[crosswatch] OCI_COMPARTMENT_ID not set — skipping.", flush=True)
        return

    fleet_file = TOOLS_DIR / "fleet.json"
    fleet = json.loads(fleet_file.read_text(encoding="utf-8")).get("vms", [])

    print("[crosswatch] Querying OCI instance states...", flush=True)
    try:
        instances = oci_instance_states(compartment_id)
    except Exception as exc:
        print(f"[crosswatch] OCI query failed: {exc}", flush=True)
        return

    # Option B: resolve management's current private IP from OCI, not from env.
    mgmt_info  = instances.get("management", {})
    mgmt_state = mgmt_info.get("state", "NOT FOUND")
    mgmt_id    = mgmt_info.get("id", "")

    if mgmt_state == "RUNNING":
        mgmt_ip = oci_private_ip(compartment_id, mgmt_id)
        if mgmt_ip:
            saved_ip = env.get("FLEET_MANAGEMENT_PRIVATE_IP", "")
            if mgmt_ip != saved_ip:
                print(f"[crosswatch] Management IP updated: {saved_ip!r} → {mgmt_ip!r}", flush=True)
                update_env_value(ENV_FILE, "FLEET_MANAGEMENT_PRIVATE_IP", mgmt_ip)
                env["FLEET_MANAGEMENT_PRIVATE_IP"] = mgmt_ip
                trigger_heartbeat()
    else:
        mgmt_ip = ""

    for vm in fleet:
        name = vm["name"]
        if name == this_vm:
            continue
        if vm.get("expected_state") != "RUNNING":
            continue

        info  = instances.get(name, {})
        state = info.get("state", "NOT FOUND")
        print(f"[crosswatch] {name}: {state}", flush=True)

        if state in ("RUNNING", "STARTING", "PROVISIONING"):
            continue

        if name == "management":
            # Management is down — notify directly and let the worker handle relaunch.
            worker_info  = instances.get("worker", {})
            worker_state = worker_info.get("state", "NOT FOUND")

            if worker_state in ("RUNNING", "STARTING", "PROVISIONING"):
                notify_ntfy(
                    ntfy_topic,
                    f"[cloud-lab] Management VM down ({state})",
                    f"Laboratory detected management is {state}. "
                    f"Worker is {worker_state} and will attempt relaunch.",
                )
            else:
                # Both management and worker are down — only lab is left.
                notify_ntfy(
                    ntfy_topic,
                    "[cloud-lab] Management AND worker down — human needed",
                    f"Laboratory detected management is {state} and worker is {worker_state}. "
                    "No automatic recovery possible. Manual intervention required.",
                    priority="urgent",
                )
        else:
            # Non-management peer is down — report to management if reachable.
            report_to_management(
                mgmt_ip,
                this_vm,
                "peer_unhealthy",
                {"peer": name, "state": state, "expected": "RUNNING"},
                heartbeat_token,
            )

    print("[crosswatch] Done.", flush=True)


if __name__ == "__main__":
    main()
