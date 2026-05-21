#!/usr/bin/env python3
"""List OCI VMs and optionally ping their public IPs."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
TOOLS_ROOT = ROOT.parent
PROFILE_DIR = TOOLS_ROOT / "vm-profiles"


class StatusError(RuntimeError):
    pass


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load_env(paths: list[Path]) -> dict[str, str]:
    values: dict[str, str] = {}
    for path in paths:
        values.update(parse_env_file(path))
    values.update(os.environ)
    return values


def load_json(path: Path) -> Any:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def expand_path(value: str) -> Path:
    expanded = value
    for name, env_value in sorted(os.environ.items(), key=lambda item: len(item[0]), reverse=True):
        expanded = expanded.replace(f"$env:{name}", env_value)
        expanded = expanded.replace(f"%{name}%", env_value)
    return Path(os.path.expandvars(expanded)).expanduser()


def oci_env(auth_mode: str) -> dict[str, str]:
    env = os.environ.copy()
    env["OCI_CLI_SUPPRESS_FILE_PERMISSIONS_WARNING"] = "True"
    env["PYTHONWARNINGS"] = "ignore::FutureWarning"
    if auth_mode == "instance_principal":
        env["OCI_CLI_AUTH"] = "instance_principal"
    elif auth_mode == "api_key":
        env.pop("OCI_CLI_AUTH", None)
    else:
        raise StatusError("OCI_AUTH_MODE must be api_key or instance_principal")
    return env


def run_oci(args: list[str], auth_mode: str) -> Any:
    oci = shutil.which("oci")
    if not oci and platform.system() == "Windows":
        known = Path(r"C:\Program Files (x86)\Oracle\oci_cli\oci.exe")
        if known.exists():
            oci = str(known)
    if not oci:
        raise StatusError("OCI CLI was not found on PATH.")

    result = subprocess.run(
        [oci, *args],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=oci_env(auth_mode),
    )
    if result.returncode != 0:
        raise StatusError((result.stdout + " " + result.stderr).strip())
    if not result.stdout.strip():
        return {"data": []}
    return json.loads(result.stdout)


def ping(host: str) -> str | None:
    if not host:
        return None
    if platform.system() == "Windows":
        cmd = ["ping", "-n", "1", "-w", "3000", host]
    else:
        cmd = ["ping", "-c", "1", "-W", "3", host]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    if result.returncode != 0:
        return "timeout"
    match = re.search(r"time[=<]([\d.]+)\s*ms", result.stdout, re.I)
    if match:
        return f"{float(match.group(1)):.0f}ms"
    return "ok"


def slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip("-")
    return slug or "unnamed"


def get_vnic_ips(instance_id: str, compartment_id: str, auth_mode: str) -> tuple[str, str, str]:
    try:
        attachments = run_oci(
            ["compute", "vnic-attachment", "list",
             "--compartment-id", compartment_id,
             "--instance-id", instance_id, "--all"],
            auth_mode,
        ).get("data", [])
        active = [
            item for item in attachments
            if item.get("lifecycle-state") not in ("DETACHING", "DETACHED")
        ]
        if not active:
            return "", "", ""
        vnic_id = active[0].get("vnic-id")
        if not vnic_id:
            return "", "", ""
        vnic = run_oci(["network", "vnic", "get", "--vnic-id", vnic_id], auth_mode).get("data", {})
        return vnic.get("public-ip") or "", vnic.get("private-ip") or "", ""
    except Exception as exc:
        return "", "", f"vnic lookup failed: {exc}"


def run_ssh(host: str, user: str, key_path: Path, command: str, timeout: int = 12) -> str:
    result = subprocess.run(
        ["ssh", "-i", str(key_path),
         "-o", "BatchMode=yes",
         "-o", "ConnectTimeout=5",
         "-o", "StrictHostKeyChecking=accept-new",
         f"{user}@{host}", command],
        text=True, encoding="utf-8", errors="replace",
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise StatusError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


def probe_host(public_ip: str, env: dict[str, str]) -> dict[str, Any]:
    key_value = env.get("OCI_SSH_PRIVATE_KEY_PATH", "")
    if not public_ip or not key_value:
        return {"ssh_probe": "skipped", "reason": "missing public IP or OCI_SSH_PRIVATE_KEY_PATH"}

    key_path = expand_path(key_value)
    user = env.get("OCI_SSH_USER", "ubuntu")
    if not key_path.exists():
        return {"ssh_probe": "skipped", "reason": f"private key not found: {key_path}"}

    probes = {
        "hostname": "hostname",
        "kernel": "uname -a",
        "uptime": "uptime -p",
        "disk": "df -h /",
        "memory": "free -h",
        "python": "python3 --version 2>&1 || true",
        "oci_cli": "oci --version 2>&1 || true",
        "systemd_timers": "systemctl list-timers --all --no-pager --no-legend | head -30 || true",
        "user_crontab": "crontab -l 2>/dev/null || true",
        "fleet_tree": "find ~/cloud-lab -maxdepth 3 -type f 2>/dev/null | sort | sed 's#^/home/[^/]*/##' | head -160 || true",
        "apt_manual_packages": "apt-mark showmanual 2>/dev/null | sort | head -160 || true",
    }

    result: dict[str, Any] = {"ssh_probe": "ok", "ssh_user": user}
    for name, command in probes.items():
        try:
            result[name] = run_ssh(public_ip, user, key_path, command)
        except Exception as exc:
            result[name] = f"probe failed: {exc}"
    return result


def render_markdown(snapshot: dict[str, Any]) -> str:
    instance = snapshot["instance"]
    probe = snapshot.get("probe", {})
    fleet = load_json(TOOLS_ROOT / "fleet.json") or {"vms": []}
    expected = next(
        (item for item in fleet.get("vms", []) if item.get("name") == instance.get("display-name")),
        {},
    )
    lines = [
        f"# {instance.get('display-name', 'OCI VM')}",
        "",
        f"- Synced: `{snapshot['synced_at']}`",
        f"- Role: `{expected.get('role', '')}`",
        f"- Expected state: `{expected.get('expected_state', '')}`",
        f"- State: `{instance.get('lifecycle-state', '')}`",
        f"- Shape: `{instance.get('shape', '')}`",
        f"- Public IP: `{snapshot.get('public_ip', '')}`",
        f"- Private IP: `{snapshot.get('private_ip', '')}`",
        f"- OCID: `{instance.get('id', '')}`",
        "",
        "## Notes",
        "",
        f"- {expected.get('notes', 'No notes in fleet.json for this VM.')}",
        "",
        "## Probe",
        "",
        f"- SSH probe: `{probe.get('ssh_probe', 'not-run')}`",
    ]

    for key in [
        "hostname", "kernel", "uptime", "python", "oci_cli",
        "disk", "memory", "systemd_timers", "user_crontab",
        "fleet_tree", "apt_manual_packages",
    ]:
        if key in probe:
            lines.extend(["", f"### {key}", "", "```text", str(probe[key]), "```"])

    return "\n".join(lines) + "\n"


def write_snapshot(
    instance: dict[str, Any],
    public_ip: str,
    private_ip: str,
    env: dict[str, str],
    no_ssh: bool,
    probe: dict[str, Any] | None = None,
) -> None:
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    if probe is None:
        probe = {} if no_ssh else probe_host(public_ip, env)
    snapshot = {
        "synced_at": datetime.now(timezone.utc).isoformat(),
        "public_ip": public_ip,
        "private_ip": private_ip,
        "instance": instance,
        "probe": probe,
    }
    slug = slugify(instance.get("display-name") or instance["id"])
    (PROFILE_DIR / f"{slug}.json").write_text(json.dumps(snapshot, indent=2, sort_keys=True), encoding="utf-8")
    (PROFILE_DIR / f"{slug}.md").write_text(render_markdown(snapshot), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Show OCI VM status and optional ping results.")
    parser.add_argument("--env", action="append", default=[])
    parser.add_argument("--name", help="Only show instances whose display name contains this text.")
    parser.add_argument("--id", help="Only show one instance OCID.")
    parser.add_argument("--ping", action="store_true", help="Ping public IPs when available.")
    parser.add_argument("--no-snapshot", action="store_true", help="Do not write vm-profiles snapshots.")
    parser.add_argument("--no-ssh", action="store_true", help="Skip SSH probes in vm-profiles snapshots.")
    args = parser.parse_args()

    env_paths = [Path(path).expanduser() for path in args.env]
    env_paths.append(TOOLS_ROOT / ".env")
    env = load_env(env_paths)

    compartment_id = env.get("OCI_COMPARTMENT_ID", "")
    auth_mode = env.get("OCI_AUTH_MODE", "api_key")
    if not compartment_id:
        raise StatusError("OCI_COMPARTMENT_ID is missing from .env")

    data = run_oci(["compute", "instance", "list", "--compartment-id", compartment_id, "--all"], auth_mode)
    instances = [
        item for item in data.get("data", [])
        if item.get("lifecycle-state") not in ("TERMINATING", "TERMINATED")
    ]

    if args.id:
        instances = [item for item in instances if item.get("id") == args.id]
    if args.name:
        needle = args.name.lower()
        instances = [item for item in instances if needle in (item.get("display-name") or "").lower()]

    if not instances:
        print("No matching instances found.")
        return 0

    print(f"{'NAME':24} {'STATE':12} {'SHAPE':22} {'PUBLIC IP':15} {'PRIVATE IP':15} {'PING':10} {'SSH'}")
    print("-" * 116)
    for item in sorted(instances, key=lambda row: row.get("display-name") or ""):
        instance_id = item["id"]
        public_ip, private_ip, ip_warning = get_vnic_ips(instance_id, compartment_id, auth_mode)
        probe = {} if args.no_snapshot or args.no_ssh else probe_host(public_ip, env)
        ping_text = "-"
        if args.ping and public_ip:
            ping_text = ping(public_ip) or "timeout"
        elif args.ping:
            ping_text = "no-ip"
        ssh_text = probe.get("ssh_probe", "skipped" if args.no_ssh else "-")

        print(
            f"{(item.get('display-name') or '')[:24]:24} "
            f"{(item.get('lifecycle-state') or '')[:12]:12} "
            f"{(item.get('shape') or '')[:22]:22} "
            f"{public_ip[:15]:15} "
            f"{private_ip[:15]:15} "
            f"{ping_text[:10]:10} "
            f"{ssh_text}"
        )
        if ip_warning:
            print(f"  warning: {ip_warning}")
        if not args.no_snapshot:
            write_snapshot(item, public_ip, private_ip, env, args.no_ssh, probe=probe)
    if not args.no_snapshot:
        print(f"\nSnapshots updated in {PROFILE_DIR}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except StatusError as exc:
        print(f"Fatal error: {exc}", file=sys.stderr)
        raise SystemExit(1)
