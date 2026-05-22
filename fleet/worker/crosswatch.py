#!/usr/bin/env python3
"""Worker cross-watch — runs as a systemd timer on the worker VM.
Checks OCI state of peer VMs every 6 hours.

Normal peers: reports anomalies to the management heartbeat endpoint.
Management down: sends ntfy directly, attempts OCI relaunch, re-discovers
  management's private IP and updates the local env file.
Other peer down + management also down: worker attempts to relaunch that peer
  directly (symmetric triangle — all three VMs can relaunch any other).

IP re-discovery (Option B): management's current private IP is always resolved
from OCI at crosswatch time, not from the potentially-stale env file. The env
file is updated and the heartbeat service is triggered if the IP has changed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


TOOLS_DIR = Path(__file__).resolve().parent.parent.parent   # repo root
ENV_FILE  = Path.home() / ".config" / "cloud-lab" / "worker.env"

_PEER_MAX_ATTEMPTS  = 3
_PEER_COOLDOWN      = 15 * 60   # seconds between relaunch attempts


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


def report_to_management(mgmt_ip: str, vm_name: str, event: str, details: dict,
                         heartbeat_token: str = "") -> None:
    if not mgmt_ip:
        print("[crosswatch] no management IP — cannot report event.", flush=True)
        return
    payload = json.dumps({
        "vm_name": vm_name,
        "uptime": "crosswatch",
        "event": event,
        "details": details,
    }).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if heartbeat_token:
        headers["Authorization"] = f"Bearer {heartbeat_token}"
    try:
        req = urllib.request.Request(
            f"http://{mgmt_ip}:8765/heartbeat",
            data=payload,
            headers=headers,
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as exc:
        print(f"[crosswatch] management report failed: {exc}", flush=True)


# ── peer relaunch ─────────────────────────────────────────────────────────────

def _state_file_for(target: str) -> Path:
    return Path.home() / "cloud-lab" / "logs" / f"{target}-relaunch.json"


def _load_relaunch_state(target: str) -> dict:
    sf = _state_file_for(target)
    if sf.exists():
        try:
            return json.loads(sf.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"attempts": 0, "last_attempt_ts": 0.0}


def _save_relaunch_state(target: str, state: dict) -> None:
    sf = _state_file_for(target)
    sf.parent.mkdir(parents=True, exist_ok=True)
    sf.write_text(json.dumps(state), encoding="utf-8")


def attempt_peer_relaunch(target: str, ntfy_topic: str) -> bool:
    """Launch any peer VM via oci_launch_until_available.py + its profile JSON.
    target: VM name matching a profile in admin/profiles/<target>.json
    Tracks attempt count and cooldown per target in a state file.
    Returns True if a launch was attempted (not necessarily successful).
    """
    state = _load_relaunch_state(target)
    now = time.time()

    if state["attempts"] >= _PEER_MAX_ATTEMPTS:
        print(f"[crosswatch] {target} relaunch: max attempts reached.", flush=True)
        return False

    elapsed = now - state["last_attempt_ts"]
    if elapsed < _PEER_COOLDOWN:
        remaining = int(_PEER_COOLDOWN - elapsed)
        print(f"[crosswatch] {target} relaunch: cooldown active ({remaining}s left).", flush=True)
        return False

    attempt_n = state["attempts"] + 1
    print(f"[crosswatch] Attempting {target} relaunch ({attempt_n}/{_PEER_MAX_ATTEMPTS})...", flush=True)
    notify_ntfy(
        ntfy_topic,
        f"[cloud-lab] {target.capitalize()} relaunch attempt {attempt_n}/{_PEER_MAX_ATTEMPTS}",
        f"Worker is attempting to relaunch the {target} VM via OCI.",
        priority="default",
    )

    state["attempts"] = attempt_n
    state["last_attempt_ts"] = now
    _save_relaunch_state(target, state)

    launcher = TOOLS_DIR / "admin" / "oci_launch_until_available.py"
    profile  = TOOLS_DIR / "admin" / "profiles" / f"{target}.json"

    child_env = os.environ.copy()
    child_env["OCI_CLI_AUTH"] = "instance_principal"
    child_env["OCI_CLI_SUPPRESS_FILE_PERMISSIONS_WARNING"] = "True"
    child_env["OCI_AUTH_MODE"] = "instance_principal"
    child_env["OCI_OPEN_CONSOLE_ON_SUCCESS"] = "false"

    try:
        result = subprocess.run(
            [sys.executable, str(launcher),
             "--profile", str(profile),
             "--env", str(ENV_FILE)],
            timeout=300,   # E2.1.Micro typically launches in < 2 min
            env=child_env,
        )
        if result.returncode == 0:
            print(f"[crosswatch] {target} relaunch succeeded.", flush=True)
            _save_relaunch_state(target, {"attempts": 0, "last_attempt_ts": 0.0})
            return True
        print(f"[crosswatch] {target} relaunch exited {result.returncode}.", flush=True)
    except subprocess.TimeoutExpired:
        print(f"[crosswatch] {target} relaunch timed out (5 min) — will retry next cycle.", flush=True)
    except Exception as exc:
        print(f"[crosswatch] {target} relaunch error: {exc}", flush=True)

    return False


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

    compartment_id  = env.get("OCI_COMPARTMENT_ID", "")
    ntfy_topic      = env.get("NOTIFY_NTFY_TOPIC", "")
    this_vm         = env.get("FLEET_VM_NAME", "worker")
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
        # Management is healthy — reset relaunch counter and refresh IP if changed.
        _save_relaunch_state("management", {"attempts": 0, "last_attempt_ts": 0.0})
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

    mgmt_down = mgmt_state not in ("RUNNING", "STARTING", "PROVISIONING")

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
            # Management is down — act directly without going through management's endpoint.
            notify_ntfy(
                ntfy_topic,
                f"[cloud-lab] Management VM down ({state})",
                f"Worker ({this_vm}) detected management is {state}. Attempting relaunch.",
            )
            relaunched = attempt_peer_relaunch("management", ntfy_topic)
            if relaunched:
                # Give cloud-init a moment, then discover the new private IP.
                time.sleep(30)
                try:
                    new_instances = oci_instance_states(compartment_id)
                except Exception:
                    new_instances = {}
                new_info = new_instances.get("management", {})
                new_ip = oci_private_ip(compartment_id, new_info.get("id", ""))
                if new_ip:
                    update_env_value(ENV_FILE, "FLEET_MANAGEMENT_PRIVATE_IP", new_ip)
                    trigger_heartbeat()
                    notify_ntfy(
                        ntfy_topic,
                        "[cloud-lab] Management relaunched",
                        f"Management VM is back up at {new_ip}. Heartbeat sent.",
                        priority="default",
                    )
                else:
                    notify_ntfy(
                        ntfy_topic,
                        "[cloud-lab] Management relaunched — IP pending",
                        "Management launched but private IP not yet visible. Will update next cycle.",
                        priority="default",
                    )
            else:
                relaunch_state = _load_relaunch_state("management")
                if relaunch_state["attempts"] >= _PEER_MAX_ATTEMPTS:
                    notify_ntfy(
                        ntfy_topic,
                        "[cloud-lab] Management relaunch failed — human needed",
                        f"After {_PEER_MAX_ATTEMPTS} attempts, management is still down. Manual intervention required.",
                        priority="urgent",
                    )

        else:
            # Non-management peer (laboratory, etc.)
            if mgmt_down:
                # Management is also down — worker relaunches this peer directly.
                notify_ntfy(
                    ntfy_topic,
                    f"[cloud-lab] {name} VM down ({state}) — management also unavailable",
                    f"Worker is attempting to relaunch {name} since management is unavailable.",
                )
                relaunched = attempt_peer_relaunch(name, ntfy_topic)
                if not relaunched:
                    peer_rs = _load_relaunch_state(name)
                    if peer_rs["attempts"] >= _PEER_MAX_ATTEMPTS:
                        notify_ntfy(
                            ntfy_topic,
                            f"[cloud-lab] {name} relaunch failed — human needed",
                            f"After {_PEER_MAX_ATTEMPTS} attempts, {name} is still down. Manual intervention required.",
                            priority="urgent",
                        )
            else:
                # Management is up — report to management (it handles non-management peers).
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
