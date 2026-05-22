#!/usr/bin/env python3
"""
Fleet health check — runs every 4 hours via user crontab.

Collects system stats. Management sends ntfy; worker/laboratory report their
stats to management so the owner only gets one notification stream.

Also fires ntfy alerts when resource thresholds are crossed:
  - Disk usage > 80%
  - Available RAM < 10% of total
  - Load average (1m) > 2× CPU count

A cooldown file (~/cloud-lab/logs/threshold_alerted.json) prevents
repeat alerts for the same condition within 12 hours.
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


CONFIG_DIR    = Path.home() / ".config" / "cloud-lab"
CLOUD_LAB_DIR = Path(os.getenv("CLOUD_LAB_DIR", str(Path.home() / "cloud-lab")))
COOLDOWN_FILE = CLOUD_LAB_DIR / "logs" / "threshold_alerted.json"
COOLDOWN_SECS = 12 * 3600


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
        "hostname":  run(["hostname"]),
        "uptime":    run(["uptime", "-p"]),
        "load":      run(["cat", "/proc/loadavg"]),
        "disk_root": run(["df", "-h", "--output=pcent,avail", "/"]),
        "mem":       run(["free", "-h", "--si"]),
        "cpu_count": run(["nproc"]),
    }


# ── threshold helpers ──────────────────────────────────────────────────────────

def _disk_pct() -> int | None:
    """Return root disk usage percentage, or None on error."""
    try:
        out = subprocess.check_output(
            ["df", "--output=pcent", "/"], text=True, stderr=subprocess.DEVNULL
        ).strip().splitlines()
        return int(out[-1].strip().rstrip("%"))
    except Exception:
        return None


def _mem_available_pct() -> int | None:
    """Return available RAM as percentage of total, or None on error."""
    try:
        info = Path("/proc/meminfo").read_text()
        total = avail = None
        for line in info.splitlines():
            if line.startswith("MemTotal:"):
                total = int(line.split()[1])
            elif line.startswith("MemAvailable:"):
                avail = int(line.split()[1])
        if total and avail:
            return int(avail * 100 / total)
        return None
    except Exception:
        return None


def _load_avg_1m() -> float | None:
    """Return 1-minute load average, or None on error."""
    try:
        return float(Path("/proc/loadavg").read_text().split()[0])
    except Exception:
        return None


def _cpu_count() -> int:
    try:
        return int(subprocess.check_output(["nproc"], text=True).strip())
    except Exception:
        return 1


def _load_cooldowns() -> dict[str, float]:
    try:
        return json.loads(COOLDOWN_FILE.read_text())
    except Exception:
        return {}


def _save_cooldowns(data: dict[str, float]) -> None:
    try:
        COOLDOWN_FILE.parent.mkdir(parents=True, exist_ok=True)
        COOLDOWN_FILE.write_text(json.dumps(data))
    except Exception:
        pass


def _should_alert(key: str, cooldowns: dict[str, float]) -> bool:
    import time
    last = cooldowns.get(key, 0.0)
    return (time.time() - last) > COOLDOWN_SECS


def _mark_alerted(key: str, cooldowns: dict[str, float]) -> dict[str, float]:
    import time
    updated = dict(cooldowns)
    updated[key] = time.time()
    return updated


def _ntfy(topic: str, server: str, title: str, body: str,
          priority: str = "high", tags: str = "warning") -> None:
    if not topic:
        return
    try:
        req = urllib.request.Request(
            f"{server.rstrip('/')}/{topic}",
            data=body.encode("utf-8"),
            headers={"Title": title, "Tags": tags, "Priority": priority},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as exc:
        print(f"[health_check] ntfy failed: {exc}", flush=True)


def check_thresholds(
    vm_name: str, fleet_name: str,
    topic: str, ntfy_server: str,
) -> None:
    """Fire ntfy alerts if any resource threshold is crossed (with cooldown)."""
    cooldowns = _load_cooldowns()
    now_ts    = None   # lazy import

    disk = _disk_pct()
    if disk is not None and disk > 80:
        key = f"disk_{vm_name}"
        if _should_alert(key, cooldowns):
            _ntfy(
                topic, ntfy_server,
                f"{fleet_name}: {vm_name} disk {disk}% full",
                f"{vm_name} root filesystem is {disk}% full.\n"
                f"Run 'df -h /' to investigate. Consider 'sudo apt-get autoremove && sudo apt-get clean'.",
                priority="high", tags="warning,floppy_disk",
            )
            cooldowns = _mark_alerted(key, cooldowns)
            print(f"[health_check] Disk alert sent for {vm_name}: {disk}%", flush=True)

    mem = _mem_available_pct()
    if mem is not None and mem < 10:
        key = f"mem_{vm_name}"
        if _should_alert(key, cooldowns):
            _ntfy(
                topic, ntfy_server,
                f"{fleet_name}: {vm_name} low memory ({mem}% free)",
                f"{vm_name} has only {mem}% RAM available.\n"
                f"Run 'free -h' and 'ps aux --sort=-%mem | head -10' to identify the culprit.",
                priority="high", tags="warning,rotating_light",
            )
            cooldowns = _mark_alerted(key, cooldowns)
            print(f"[health_check] Memory alert sent for {vm_name}: {mem}% free", flush=True)

    load = _load_avg_1m()
    cpus = _cpu_count()
    if load is not None and load > 2 * cpus:
        key = f"load_{vm_name}"
        if _should_alert(key, cooldowns):
            _ntfy(
                topic, ntfy_server,
                f"{fleet_name}: {vm_name} high load ({load:.1f}, {cpus} CPU)",
                f"{vm_name} load average is {load:.1f} on {cpus} CPU(s) — {load/cpus:.1f}× baseline.\n"
                f"Run 'top' or 'ps aux --sort=-%cpu | head -10' to investigate.",
                priority="default", tags="chart_with_upwards_trend",
            )
            cooldowns = _mark_alerted(key, cooldowns)
            print(f"[health_check] Load alert sent for {vm_name}: {load:.1f}", flush=True)

    _save_cooldowns(cooldowns)


# ── notifications ──────────────────────────────────────────────────────────────

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
        "uptime":  stats.get("uptime", "?"),
        "event":   "keepalive_health",
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

    # Check thresholds and fire alerts if needed (management sends directly, others via management)
    if topic:
        check_thresholds(vm_name, fleet_name, topic, ntfy_server)

    if vm_name == "management" and topic:
        ntfy_heartbeat(topic, vm_name, fleet_name, stats, ntfy_server)
    elif vm_name != "management":
        report_to_management(mgmt_ip, vm_name, stats)
    else:
        print("[health_check] NOTIFY_NTFY_TOPIC not set — skipping ntfy.", flush=True)


if __name__ == "__main__":
    main()
