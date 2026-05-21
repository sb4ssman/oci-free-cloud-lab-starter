#!/usr/bin/env python3
"""
Fleet Orchestrator — runs on the management VM.

Single continuous loop:
  1. For each VM in fleet.json where expected_state = RUNNING:
       - Check OCI state via instance-principal
       - If missing or TERMINATED: launch with cloud-init, verify SSH, ntfy
  2. Sleep and repeat (long interval once fleet is healthy).

The laboratory (A1 Flex) uses the hitrov-pattern retry: checks existing capacity
before launching, catches "Out of host capacity" specifically, and retries until
the lottery is won. Micro VMs launch immediately.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


TOOLS_DIR  = Path(__file__).resolve().parent.parent.parent   # repo root
FLEET_FILE = TOOLS_DIR / "fleet.json"
ENV_FILE   = Path.home() / ".config" / "cloud-lab" / "management.env"
STATE_FILE = TOOLS_DIR / "vm-profiles" / "_orchestrator_state.json"

HEALTHY_POLL_SECONDS = 6 * 3600   # 6 hours between full checks when fleet is healthy
SSH_WAIT_TIMEOUT     = 600        # 10 minutes to wait for SSH on new VM
SSH_WAIT_INTERVAL    = 20


def ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def log(msg: str) -> None:
    print(f"[{ts()}] [orchestrator] {msg}", flush=True)


# ── env loading ───────────────────────────────────────────────────────────────

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


def load_env() -> dict[str, str]:
    env = parse_env_file(ENV_FILE)
    env.update(os.environ)
    return env


# ── OCI helpers ───────────────────────────────────────────────────────────────

def oci_cmd(args: list[str]) -> dict[str, Any]:
    oci = shutil.which("oci") or "/home/ubuntu/bin/oci"
    child_env = os.environ.copy()
    child_env["OCI_CLI_AUTH"] = "instance_principal"
    child_env["OCI_CLI_SUPPRESS_FILE_PERMISSIONS_WARNING"] = "True"
    result = subprocess.run(
        [oci, *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
        env=child_env, timeout=90,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stdout + " " + result.stderr).strip())
    return json.loads(result.stdout)


def get_instance_state(name: str, compartment_id: str) -> str | None:
    data = oci_cmd(["compute", "instance", "list", "--compartment-id", compartment_id, "--all"])
    for item in data.get("data", []):
        if item.get("display-name") == name:
            state = item.get("lifecycle-state", "")
            if state not in ("TERMINATING", "TERMINATED"):
                return state
    return None


def get_public_ip(name: str, compartment_id: str) -> str:
    data = oci_cmd(["compute", "instance", "list", "--compartment-id", compartment_id, "--all"])
    for item in data.get("data", []):
        if item.get("display-name") == name and item.get("lifecycle-state") == "RUNNING":
            instance_id = item["id"]
            attachments = oci_cmd(
                ["compute", "vnic-attachment", "list",
                 "--compartment-id", compartment_id,
                 "--instance-id", instance_id, "--all"]
            ).get("data", [])
            if attachments:
                vnic_id = attachments[0].get("vnic-id", "")
                vnic = oci_cmd(["network", "vnic", "get", "--vnic-id", vnic_id])
                return vnic.get("data", {}).get("public-ip") or ""
    return ""


# ── SSH helpers ───────────────────────────────────────────────────────────────

def wait_for_ssh(public_ip: str, key_path: Path, user: str = "ubuntu") -> bool:
    log(f"Waiting for SSH on {public_ip} (up to {SSH_WAIT_TIMEOUT}s)...")
    deadline = time.monotonic() + SSH_WAIT_TIMEOUT
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["ssh", "-i", str(key_path),
             "-o", "BatchMode=yes",
             "-o", "ConnectTimeout=8",
             "-o", "StrictHostKeyChecking=accept-new",
             f"{user}@{public_ip}", "echo ok"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            log(f"SSH ready on {public_ip}.")
            return True
        time.sleep(SSH_WAIT_INTERVAL)
    log(f"SSH wait timed out for {public_ip}.")
    return False


def run_first_contact(public_ip: str, vm_name: str, role: str, env: dict[str, str]) -> bool:
    """Bootstrap a freshly launched VM: git clone + write .env + run role setup."""
    key_path = Path(env.get("OCI_SSH_PRIVATE_KEY_PATH", "~/.ssh/fleet.key")).expanduser()
    user = env.get("OCI_SSH_USER", "ubuntu")

    if not wait_for_ssh(public_ip, key_path, user):
        return False

    github_token  = env.get("GITHUB_TOKEN", "")
    repo          = env.get("FLEET_REPO", "")
    compartment   = env.get("OCI_COMPARTMENT_ID", "")
    mgmt_priv_ip  = env.get("FLEET_MANAGEMENT_PRIVATE_IP", "")
    ntfy_topic    = env.get("NOTIFY_NTFY_TOPIC", "")
    fleet_name    = env.get("FLEET_NAME", "cloud-lab")

    if not github_token or not repo:
        log("ERROR: GITHUB_TOKEN or FLEET_REPO not set — cannot run first-contact.")
        return False

    clone_url = f"https://oauth2:{github_token}@github.com/{repo}.git"

    vm_env_lines = [
        "OCI_AUTH_MODE=instance_principal",
        f"OCI_COMPARTMENT_ID={compartment}",
        f"FLEET_MANAGEMENT_PRIVATE_IP={mgmt_priv_ip}",
        f"NOTIFY_NTFY_TOPIC={ntfy_topic}",
        f"GITHUB_TOKEN={github_token}",
        f"FLEET_REPO={repo}",
        f"FLEET_NAME={fleet_name}",
        f"FLEET_VM_NAME={vm_name}",
    ]
    vm_env = "\n".join(vm_env_lines)

    bootstrap = f"""set -euo pipefail
sudo apt-get update -qq
sudo apt-get install -y -qq ca-certificates curl git jq python3 python3-venv tmux unzip
git clone {clone_url} $HOME/cloud-lab || (cd $HOME/cloud-lab && git pull --ff-only)
mkdir -p $HOME/.config/cloud-lab
printf '%s\\n' {repr(vm_env)} > $HOME/.config/cloud-lab/{role}.env
chmod 600 $HOME/.config/cloud-lab/{role}.env
ENV_FILE=$HOME/.config/cloud-lab/{role}.env TOOLS_DIR=$HOME/cloud-lab bash $HOME/cloud-lab/fleet/{role}/setup.sh
bash $HOME/cloud-lab/payload/keepalive/install.sh $HOME/.config/cloud-lab/{role}.env
"""

    log(f"Running first-contact bootstrap for {vm_name} ({role}) at {public_ip}...")
    result = subprocess.run(
        ["ssh", "-i", str(key_path),
         "-o", "BatchMode=yes",
         "-o", "ConnectTimeout=10",
         "-o", "StrictHostKeyChecking=accept-new",
         f"{user}@{public_ip}", "bash -s"],
        input=bootstrap, text=True, encoding="utf-8",
    )
    if result.returncode != 0:
        log(f"First-contact bootstrap failed (exit {result.returncode}).")
        return False

    log(f"First-contact complete for {vm_name} at {public_ip}.")
    return True


# ── ntfy ──────────────────────────────────────────────────────────────────────

def ntfy(topic: str, title: str, message: str, tags: str = "white_check_mark", server: str = "https://ntfy.sh") -> None:
    if not topic:
        return
    try:
        req = urllib.request.Request(
            f"{server.rstrip('/')}/{topic}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Tags": tags},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as exc:
        log(f"ntfy failed: {exc}")


# ── state persistence ─────────────────────────────────────────────────────────

def load_state() -> dict[str, Any]:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


# ── launch wrapper ────────────────────────────────────────────────────────────

def launch_vm(vm: dict[str, Any], env: dict[str, str]) -> bool:
    name = vm["name"]
    role = vm["role"]
    profile_path = TOOLS_DIR / "admin" / "profiles" / f"{name}.json"

    if not profile_path.exists():
        log(f"No launch profile at {profile_path} — skipping {name}.")
        return False

    launcher = TOOLS_DIR / "admin" / "oci_launch_until_available.py"
    log(f"Launching {name} (role={role}) using profile {profile_path.name}...")

    result = subprocess.run(
        [sys.executable, str(launcher), "--profile", str(profile_path)],
        env={**os.environ, "OCI_CLI_AUTH": "instance_principal"},
    )
    return result.returncode == 0


# ── main loop ─────────────────────────────────────────────────────────────────

def check_and_repair_fleet(fleet: list[dict[str, Any]], env: dict[str, str]) -> bool:
    compartment_id = env.get("OCI_COMPARTMENT_ID", "")
    if not compartment_id:
        log("ERROR: OCI_COMPARTMENT_ID not set.")
        return False

    ntfy_topic  = env.get("NOTIFY_NTFY_TOPIC", "")
    ntfy_server = env.get("NOTIFY_NTFY_SERVER", "https://ntfy.sh")
    fleet_name  = env.get("FLEET_NAME", "cloud-lab")
    all_healthy = True

    for vm in fleet:
        if vm.get("expected_state") != "RUNNING":
            continue

        name = vm["name"]
        role = vm["role"]
        log(f"Checking {name} ({role})...")

        try:
            state = get_instance_state(name, compartment_id)
        except Exception as exc:
            log(f"OCI query failed for {name}: {exc}")
            all_healthy = False
            continue

        if state in ("RUNNING", "PROVISIONING", "STARTING"):
            log(f"{name} is {state} — ok.")
            continue

        log(f"{name} state={state or 'NOT FOUND'} — need to launch.")
        all_healthy = False

        launched = launch_vm(vm, env)
        if not launched:
            log(f"Launch failed for {name} — will retry next cycle.")
            continue

        # Wait for RUNNING state before querying IP (PROVISIONING takes 60-120s).
        log(f"Waiting for {name} to reach RUNNING state (up to 5 min)...")
        for _ in range(30):
            try:
                if get_instance_state(name, compartment_id) == "RUNNING":
                    break
            except Exception:
                pass
            time.sleep(10)

        try:
            public_ip = get_public_ip(name, compartment_id)
        except Exception as exc:
            log(f"Could not get public IP for {name}: {exc}")
            ntfy(ntfy_topic, f"{fleet_name}: {name} launched (IP unknown)", vm.get("notes", ""), tags="rocket", server=ntfy_server)
            continue

        if not public_ip:
            log(f"Could not get public IP for {name} — VM may still be provisioning. Will retry next cycle.")
            continue

        log(f"{name} public IP: {public_ip}")
        ok = run_first_contact(public_ip, name, role, env)

        if ok:
            ntfy(ntfy_topic, f"{fleet_name}: {name} is live",
                 f"{name} launched and configured. IP: {public_ip}", tags="rocket,white_check_mark", server=ntfy_server)
        else:
            ntfy(ntfy_topic, f"{fleet_name}: {name} launched but first-contact failed",
                 f"IP: {public_ip} — SSH in and check.", tags="warning", server=ntfy_server)

    return all_healthy


def main() -> None:
    log("Fleet orchestrator starting.")
    fleet_data = json.loads(FLEET_FILE.read_text(encoding="utf-8"))
    fleet = fleet_data.get("vms", [])
    consecutive_healthy = 0

    while True:
        env = load_env()
        log("--- Fleet check ---")
        try:
            healthy = check_and_repair_fleet(fleet, env)
        except Exception as exc:
            log(f"Unhandled error in fleet check: {exc}")
            healthy = False

        if healthy:
            consecutive_healthy += 1
            log(f"Fleet is healthy (streak: {consecutive_healthy}). Sleeping {HEALTHY_POLL_SECONDS}s.")
            time.sleep(HEALTHY_POLL_SECONDS)
        else:
            consecutive_healthy = 0
            log("Fleet not fully healthy. Sleeping 120s before recheck.")
            time.sleep(120)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Stopped by user.")
        sys.exit(0)
