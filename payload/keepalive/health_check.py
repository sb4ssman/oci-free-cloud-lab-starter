#!/usr/bin/env python3
"""
Fleet health check — runs every 4 hours via user crontab.

Collects system stats. Management sends ntfy; worker/laboratory report their
stats to management so the owner only gets one notification stream.
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


CONFIG_DIR = Path.home() / ".config" / "cloud-lab"


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


def run(cmd: list[str]) -> str:
    try:
        return subprocess.check_output(cmd, stderr=subprocess.DEVNULL, text=True).strip()
    except Exception:
        return "unavailable"


def collect_stats() -> dict:
    return {
        "hostname": run(["hostname"]),
        "uptime": run(["uptime", "-p"]),
        "load": run(["cat", "/proc/loadavg"]),
        "disk_root": run(["df", "-h", "--output=pcent,avail", "/"]),
        "mem": run(["free", "-h", "--si"]),
        "cpu_count": run(["nproc"]),
    }


def ntfy_heartbeat(topic: str, vm_name: str, fleet_name: str, stats: dict, server: str = "https://ntfy.sh") -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    body = (
        f"VM: {vm_name}\n"
        f"Time: {now}\n"
        f"Uptime: {stats['uptime']}\n"
        f"Load: {stats['load']}\n"
        f"Disk: {stats['disk_root']}\n"
        f"Memory:\n{stats['mem']}"
    )
    try:
        req = urllib.request.Request(
            f"{server.rstrip('/')}/{topic}",
            data=body.encode("utf-8"),
            headers={"Title": f"{fleet_name}: {vm_name} alive", "Tags": "heartbeat,green_circle"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10).read()
        print(f"[health_check] ntfy sent for {vm_name}.", flush=True)
    except Exception as exc:
        print(f"[health_check] ntfy failed: {exc}", flush=True)


def report_to_management(mgmt_ip: str, vm_name: str, stats: dict) -> None:
    if not mgmt_ip:
        print("[health_check] FLEET_MANAGEMENT_PRIVATE_IP not set — cannot report to management.", flush=True)
        return
    payload = json.dumps({
        "vm_name": vm_name,
        "uptime": stats.get("uptime", "?"),
        "event": "keepalive_health",
        "details": stats,
    }).encode("utf-8")
    try:
        req = urllib.request.Request(
            f"http://{mgmt_ip}:8765/heartbeat",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10).read()
        print("[health_check] reported to management.", flush=True)
    except Exception as exc:
        print(f"[health_check] management report failed: {exc}", flush=True)


def main() -> None:
    env_file = find_env_file()
    if not env_file:
        print("[health_check] No cloud-lab env file found — skipping ntfy.", flush=True)
        env: dict[str, str] = {}
    else:
        env = parse_env_file(env_file)
    env.update(os.environ)

    stats = collect_stats()
    print(f"[health_check] {stats}", flush=True)

    topic       = env.get("NOTIFY_NTFY_TOPIC", "")
    ntfy_server = env.get("NOTIFY_NTFY_SERVER", "https://ntfy.sh")
    vm_name     = env.get("FLEET_VM_NAME", "unknown")
    fleet_name  = env.get("FLEET_NAME", "Cloud Lab")
    mgmt_ip     = env.get("FLEET_MANAGEMENT_PRIVATE_IP", "")

    if vm_name == "management" and topic:
        ntfy_heartbeat(topic, vm_name, fleet_name, stats, ntfy_server)
    elif vm_name != "management":
        report_to_management(mgmt_ip, vm_name, stats)
    else:
        print("[health_check] NOTIFY_NTFY_TOPIC not set — skipping ntfy.", flush=True)


if __name__ == "__main__":
    main()
