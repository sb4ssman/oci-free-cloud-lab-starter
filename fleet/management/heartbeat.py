#!/usr/bin/env python3
"""
Management heartbeat — runs as a systemd timer on the management VM.
Sends a fleet status summary to the owner via ntfy every 12 hours.
"""

from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


TOOLS_DIR = Path(__file__).resolve().parent.parent.parent   # repo root
ENV_FILE  = Path.home() / ".config" / "cloud-lab" / "management.env"


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


def fleet_summary() -> str:
    fleet_file = TOOLS_DIR / "fleet.json"
    profile_dir = TOOLS_DIR / "vm-profiles"
    heartbeat_file = profile_dir / "_heartbeats.json"
    if not fleet_file.exists():
        return "fleet.json not found"

    fleet = json.loads(fleet_file.read_text(encoding="utf-8"))
    heartbeats = {}
    if heartbeat_file.exists():
        try:
            heartbeats = json.loads(heartbeat_file.read_text(encoding="utf-8"))
        except Exception:
            heartbeats = {}

    lines = []
    for vm in fleet.get("vms", []):
        name = vm["name"]
        profile_path = profile_dir / f"{name}.json"
        if profile_path.exists():
            p = json.loads(profile_path.read_text(encoding="utf-8"))
            state = p.get("instance", {}).get("lifecycle-state", "UNKNOWN")
            ip    = p.get("public_ip", "no-ip")
        else:
            state = "NO PROFILE"
            ip    = "?"
        hb = heartbeats.get(name, {})
        hb_at = hb.get("received_at", "")
        hb_text = f", heartbeat {fmt_ago(hb_at)}" if hb_at else ""
        events = hb.get("events", [])
        event_text = ""
        if events:
            last = events[-1]
            event_text = f", last event {last.get('event', '?')} {fmt_ago(last.get('received_at', ''))}"
        lines.append(f"{name}: {state} ({ip}){hb_text}{event_text}")
    return "\n".join(lines)


def fmt_ago(iso: str) -> str:
    try:
        then = datetime.fromisoformat(iso)
        delta = int((datetime.now(timezone.utc) - then).total_seconds())
        if delta < 60:
            return f"{delta}s ago"
        if delta < 3600:
            return f"{delta // 60}m ago"
        return f"{delta // 3600}h ago"
    except Exception:
        return iso


def main() -> None:
    env = parse_env_file(ENV_FILE)
    env.update(os.environ)
    topic       = env.get("NOTIFY_NTFY_TOPIC", "")
    ntfy_server = env.get("NOTIFY_NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    fleet_name  = env.get("FLEET_NAME", "Cloud Lab")

    if not topic:
        print("[heartbeat] NOTIFY_NTFY_TOPIC not set — skipping.", flush=True)
        return

    summary = fleet_summary()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    message = f"Fleet status at {now}:\n{summary}"

    print(f"[heartbeat] Sending to ntfy/{topic}...", flush=True)
    try:
        req = urllib.request.Request(
            f"{ntfy_server}/{topic}",
            data=message.encode("utf-8"),
            headers={"Title": f"{fleet_name} Heartbeat", "Tags": "heartbeat,green_circle"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10).read()
        print("[heartbeat] Sent.", flush=True)
    except Exception as exc:
        print(f"[heartbeat] Failed: {exc}", flush=True)


if __name__ == "__main__":
    main()
