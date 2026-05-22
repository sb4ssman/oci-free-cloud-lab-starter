#!/usr/bin/env python3
"""
Cloud Lab admin console — runs on the management VM.
Accessible at https://<ADMIN_DOMAIN> via Caddy reverse proxy.

Endpoints:
  GET  /                    Fleet status page (login required)
  GET  /login               Login form
  POST /login               Validate credentials, set session cookie, redirect to /
  GET  /logout              Clear session, redirect to /login
  POST /heartbeat           Liveness pings from worker/laboratory (no auth)
  GET  /export              Fleet connection details (login required)
  GET  /stats?vm=<name>     Live system stats (login required)
  GET  /logs?vm=<name>&service=<svc>  Journalctl logs (login required)
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import secrets
import shlex
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


# ── config ────────────────────────────────────────────────────────────────────

HOST       = os.getenv("ADMIN_CONSOLE_HOST", "127.0.0.1")
PORT       = int(os.getenv("ADMIN_CONSOLE_PORT", "8765"))
USERNAME   = os.getenv("ADMIN_USERNAME", "admin")
PW_HASH    = os.getenv("ADMIN_PASSWORD_HASH", "")
FLEET_NAME = os.getenv("FLEET_NAME", "Cloud Lab")
TOOLS_DIR  = Path(os.getenv("CLOUD_LAB_DIR",
             str(Path.home() / "cloud-lab"))).expanduser()
PROFILE_DIR     = TOOLS_DIR / "vm-profiles"
HEARTBEATS_FILE = TOOLS_DIR / "vm-profiles" / "_heartbeats.json"

COOKIE_NAME      = "fleet_session"
SESSION_DURATION = 7 * 24 * 3600   # 7 days

_sessions: dict[str, float] = {}
_sessions_lock = threading.Lock()

_heartbeats: dict[str, dict] = {}
_hb_lock = threading.Lock()

_login_fails: dict[str, tuple[int, float]] = {}
_fails_lock  = threading.Lock()
MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_SECONDS    = 900
_ACT = 'class="active"'   # used in f-string nav links (backslashes not allowed in f-expr)


# ── auth helpers ──────────────────────────────────────────────────────────────

def _verify_password(password: str) -> bool:
    if not PW_HASH:
        return False
    try:
        algo, iters, salt, expected = PW_HASH.split(":")
        actual = hashlib.pbkdf2_hmac(algo, password.encode(), salt.encode(), int(iters)).hex()
        return secrets.compare_digest(actual, expected)
    except Exception:
        return False


def _check_rate_limit(ip: str) -> bool:
    now = time.time()
    with _fails_lock:
        count, window_start = _login_fails.get(ip, (0, now))
        if now - window_start > LOCKOUT_SECONDS:
            _login_fails[ip] = (0, now)
            return True
        return count < MAX_LOGIN_ATTEMPTS


def _record_fail(ip: str) -> None:
    now = time.time()
    with _fails_lock:
        count, window_start = _login_fails.get(ip, (0, now))
        if now - window_start > LOCKOUT_SECONDS:
            _login_fails[ip] = (1, now)
        else:
            _login_fails[ip] = (count + 1, window_start)


def _clear_fails(ip: str) -> None:
    with _fails_lock:
        _login_fails.pop(ip, None)


def _create_session() -> str:
    sid = secrets.token_urlsafe(32)
    with _sessions_lock:
        _sessions[sid] = time.time() + SESSION_DURATION
        now = time.time()
        for k in [k for k, v in _sessions.items() if v < now]:
            del _sessions[k]
    return sid


def _is_authed(handler: BaseHTTPRequestHandler) -> bool:
    cookies = _parse_cookies(handler.headers.get("Cookie", ""))
    sid = cookies.get(COOKIE_NAME, "")
    if not sid:
        return False
    with _sessions_lock:
        expiry = _sessions.get(sid)
        if expiry is None or time.time() > expiry:
            _sessions.pop(sid, None)
            return False
        return True


def _parse_cookies(header: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in header.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


# ── data helpers ──────────────────────────────────────────────────────────────

def load_json(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def load_heartbeats() -> None:
    data = load_json(HEARTBEATS_FILE)
    if isinstance(data, dict):
        with _hb_lock:
            _heartbeats.update(data)


def save_heartbeats() -> None:
    HEARTBEATS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with _hb_lock:
        HEARTBEATS_FILE.write_text(json.dumps(_heartbeats, indent=2), encoding="utf-8")


def fmt_ago(iso: str) -> str:
    try:
        then = datetime.fromisoformat(iso)
        delta = int((datetime.now(timezone.utc) - then).total_seconds())
        if delta < 60:   return f"{delta}s ago"
        if delta < 3600: return f"{delta // 60}m ago"
        h = delta // 3600
        m = (delta % 3600) // 60
        return f"{h}h {m}m ago"
    except Exception:
        return iso


def oci_cmd(args: list[str], timeout: int = 60) -> dict:
    oci = shutil.which("oci") or "/home/ubuntu/bin/oci"
    child_env = os.environ.copy()
    child_env["OCI_CLI_AUTH"] = "instance_principal"
    child_env["OCI_CLI_SUPPRESS_FILE_PERMISSIONS_WARNING"] = "True"
    child_env["PYTHONWARNINGS"] = "ignore::FutureWarning"
    result = subprocess.run(
        [oci, *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
        env=child_env, timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stdout + " " + result.stderr).strip())
    if not result.stdout.strip():
        return {"data": []}
    return json.loads(result.stdout)


def get_vnic_ips(instance_id: str, compartment_id: str) -> tuple[str, str]:
    attachments = oci_cmd(
        ["compute", "vnic-attachment", "list",
         "--compartment-id", compartment_id,
         "--instance-id", instance_id, "--all"]
    ).get("data", [])
    active = [i for i in attachments
              if i.get("lifecycle-state") not in ("DETACHING", "DETACHED")]
    if not active:
        return "", ""
    vnic_id = active[0].get("vnic-id", "")
    if not vnic_id:
        return "", ""
    vnic = oci_cmd(["network", "vnic", "get", "--vnic-id", vnic_id]).get("data", {})
    return vnic.get("public-ip") or "", vnic.get("private-ip") or ""


def refresh_oci_snapshots() -> None:
    env = _mgmt_env()
    compartment_id = env.get("OCI_COMPARTMENT_ID", "")
    if not compartment_id:
        return
    fleet = load_json(TOOLS_DIR / "fleet.json") or {"vms": []}
    wanted = {vm.get("name") for vm in fleet.get("vms", []) if vm.get("name")}
    if not wanted:
        return
    try:
        instances = oci_cmd(
            ["compute", "instance", "list",
             "--compartment-id", compartment_id, "--all"],
            timeout=90,
        ).get("data", [])
    except Exception as exc:
        print(f"[admin_console] OCI refresh failed: {exc}", flush=True)
        return
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    for item in instances:
        name  = item.get("display-name", "")
        state = item.get("lifecycle-state", "")
        if name not in wanted or state in ("TERMINATING", "TERMINATED"):
            continue
        public_ip = private_ip = ""
        try:
            public_ip, private_ip = get_vnic_ips(item["id"], compartment_id)
        except Exception as exc:
            print(f"[admin_console] VNIC refresh for {name}: {exc}", flush=True)
        snapshot = {
            "synced_at": now, "public_ip": public_ip,
            "private_ip": private_ip, "instance": item, "probe": {},
        }
        (PROFILE_DIR / f"{name}.json").write_text(
            json.dumps(snapshot, indent=2, sort_keys=True), encoding="utf-8")


# ── live stats & logs ─────────────────────────────────────────────────────────

def _mgmt_env() -> dict[str, str]:
    out: dict[str, str] = {}
    p = Path.home() / ".config" / "cloud-lab" / "management.env"
    if not p.exists():
        return out
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip(); v = v.strip().strip('"').strip("'")
        if k:
            out[k] = v
    return out


_STATS_CMD = (
    "echo '=== UPTIME ===' && uptime && "
    "echo && echo '=== PROCESSES ===' && "
    "top -b -n 1 -c -w 200 2>/dev/null | head -40 && "
    "echo && echo '=== MEMORY ===' && free -h --si && "
    "echo && echo '=== DISK ===' && df -h"
)


def _ssh_run(vm_name: str, remote_cmd: str, timeout: int = 20) -> str:
    """SSH into a fleet VM and run a command; returns stdout+stderr."""
    env       = _mgmt_env()
    ssh_key   = env.get("OCI_SSH_PRIVATE_KEY_PATH", str(Path.home() / ".ssh" / "fleet.key"))
    ssh_user  = env.get("OCI_SSH_USER", "ubuntu")
    profile   = load_json(TOOLS_DIR / "vm-profiles" / f"{vm_name}.json") or {}
    public_ip = profile.get("public_ip", "")
    if not public_ip or public_ip == "—":
        return f"No public IP found for {vm_name} in vm-profiles."
    key_path = str(Path(ssh_key).expanduser())
    try:
        result = subprocess.run(
            ["ssh", "-i", key_path,
             "-o", "StrictHostKeyChecking=accept-new",
             "-o", "ConnectTimeout=8", "-o", "BatchMode=yes",
             f"{ssh_user}@{public_ip}", remote_cmd],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, timeout=timeout,
        )
        return result.stdout or "(no output)"
    except subprocess.TimeoutExpired:
        return f"SSH timed out connecting to {vm_name} ({public_ip})."
    except Exception as exc:
        return f"SSH error for {vm_name} ({public_ip}): {exc}"


def collect_local_stats() -> str:
    try:
        result = subprocess.run(
            ["bash", "-c", _STATS_CMD],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, timeout=10,
        )
        return result.stdout or "(no output)"
    except Exception as exc:
        return f"Error: {exc}"


def collect_remote_stats(vm_name: str) -> str:
    refresh_oci_snapshots()
    return _ssh_run(vm_name, _STATS_CMD, timeout=20)


def collect_local_logs(service: str, lines: int = 200) -> str:
    try:
        result = subprocess.run(
            ["journalctl", "-u", service, "-n", str(lines),
             "--no-pager", "--output=short-iso"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, timeout=10,
        )
        return result.stdout or "(no output)"
    except Exception as exc:
        return f"Error collecting logs: {exc}"


def collect_remote_logs(vm_name: str, service: str, lines: int = 200) -> str:
    refresh_oci_snapshots()
    cmd = f"sudo journalctl -u {shlex.quote(service)} -n {lines} --no-pager --output=short-iso 2>&1"
    return _ssh_run(vm_name, cmd, timeout=20)


def fleet_events_html() -> str:
    with _hb_lock:
        hbs = dict(_heartbeats)
    all_events: list[dict] = []
    for vm_name, hb in hbs.items():
        for ev in hb.get("events", []):
            all_events.append({
                "vm": vm_name,
                "received_at": ev.get("received_at", ""),
                "event": str(ev.get("event", "")),
            })
    all_events.sort(key=lambda e: e.get("received_at", ""), reverse=True)
    recent = all_events[:10]
    if not recent:
        return '<p class="muted">No events recorded yet.</p>'
    rows = []
    for ev in recent:
        rows.append(
            f'<div class="ev-row">'
            f'<span class="ev-vm">{html.escape(ev["vm"])}</span>'
            f'<span class="ev-type">{html.escape(ev["event"])}</span>'
            f'<span class="ev-time">{html.escape(fmt_ago(ev["received_at"]))}</span>'
            f'</div>'
        )
    return "\n".join(rows)


# ── CSS / JS constants ────────────────────────────────────────────────────────

# Default palette: Slate — neutral, works with any branding.
# Users can switch palettes at runtime via the settings panel.
PALETTE_CSS = """
:root {
  --c-primary:    #374151;
  --c-primary-lt: #4b5563;
  --c-primary-dk: #1f2937;
  --c-accent:     #f59e0b;
  --c-bg:         #f4f6f8;
  --c-card:       #ffffff;
  --c-text:       #111827;
  --c-muted:      #6b7280;
  --c-border:     #d1d5db;
  --c-ok:         #dcfce7;  --c-ok-text:   #166534;
  --c-warn:       #fef9c3;  --c-warn-text: #854d0e;
  --c-err:        #fee2e2;  --c-err-text:  #991b1b;
  --c-code-bg:    #0f172a;  --c-code-text: #e2e8f0;
}
[data-theme="dark"] {
  --c-bg:         #111827;
  --c-card:       #1f2937;
  --c-text:       #f9fafb;
  --c-muted:      #9ca3af;
  --c-border:     #374151;
  --c-ok:         #14532d;  --c-ok-text:   #bbf7d0;
  --c-warn:       #422006;  --c-warn-text: #fde68a;
  --c-err:        #450a0a;  --c-err-text:  #fca5a5;
  --c-code-bg:    #030712;  --c-code-text: #e2e8f0;
}
"""

COMMON_CSS = PALETTE_CSS + """
*, *::before, *::after { box-sizing: border-box; }
body { font-family: system-ui,-apple-system,sans-serif; margin: 0;
       background: var(--c-bg); color: var(--c-text);
       transition: background .15s, color .15s; }
a { color: var(--c-primary); }  a:hover { color: var(--c-primary-lt); }

/* topbar */
.topbar { background: var(--c-primary); color: #fff; padding: 0 20px;
          display: flex; align-items: center; justify-content: space-between;
          height: 52px; gap: 12px; }
.topbar-left  { display: flex; align-items: center; gap: 10px; }
.topbar-logo  { height: 44px; width: auto; display: none;
                filter: drop-shadow(0 1px 3px rgba(0,0,0,.4)) brightness(1.2); }
.topbar-logo.visible { display: block; }
.fleet-name   { font-size: 17px; font-weight: 700; color: #fff; }
.topbar-nav   { display: flex; align-items: center; gap: 4px; }
.topbar-nav a, .topbar-nav button {
  color: rgba(255,255,255,.75); font-size: 13px; font-weight: 500;
  text-decoration: none; padding: 6px 10px; border-radius: 6px;
  background: transparent; border: none; cursor: pointer;
  transition: background .15s, color .15s; }
.topbar-nav a:hover, .topbar-nav button:hover
                     { background: rgba(255,255,255,.15); color: #fff; }
.topbar-nav a.active { background: rgba(255,255,255,.2);  color: #fff; }
.theme-btn { font-size: 16px; padding: 5px 9px !important; }
.sign-out  { opacity: .65; }

/* layout */
.content { max-width: 1060px; margin: 28px auto; padding: 0 16px; }

/* cards */
.grid { display: grid; gap: 14px;
        grid-template-columns: repeat(auto-fit, minmax(290px,1fr)); }
.card { background: var(--c-card); border: 1px solid var(--c-border);
        border-radius: 12px; padding: 18px 20px;
        transition: background .15s, border-color .15s; }
.card-header { display: flex; justify-content: space-between;
               align-items: center; margin-bottom: 12px; }
.vm-name { font-size: 17px; font-weight: 700; }
.badge { border-radius: 999px; padding: 3px 11px; font-size: 12px;
         font-weight: 600; background: #6b7280; color: #fff; }
.badge.running      { background: #16a34a; color: #fff; }
.badge.provisioning { background: #d97706; color: #fff; }
.badge.terminated,
.badge.terminating  { background: #dc2626; color: #fff; }
.card p   { margin: 4px 0; font-size: 14px; }
.card p b { color: var(--c-muted); font-weight: 600; }
.notes    { color: var(--c-muted); font-size: 13px; margin-top: 8px; }
.warn-text { color: var(--c-warn-text); }

/* card actions */
.card-actions { display: flex; gap: 8px; margin-top: 12px; flex-wrap: wrap; }
.act-btn {
  font-size: 12px; font-weight: 600; padding: 4px 12px;
  border-radius: 6px; text-decoration: none; cursor: pointer;
  background: var(--c-bg); border: 1px solid var(--c-border);
  color: var(--c-primary); transition: background .12s; }
.act-btn:hover { background: var(--c-border); color: var(--c-primary-dk); }
.act-btn.accent { background: var(--c-accent); border-color: var(--c-accent); color: #fff; }
.act-btn.accent:hover { filter: brightness(1.1); }

/* fleet events */
.section-title { font-size: 15px; font-weight: 700; margin: 28px 0 10px; }
.ev-row { display: flex; gap: 12px; align-items: baseline; padding: 7px 0;
          border-bottom: 1px solid var(--c-border); font-size: 13px; }
.ev-vm   { font-weight: 600; color: var(--c-primary); min-width: 90px; }
.ev-type { flex: 1; }
.ev-time { color: var(--c-muted); white-space: nowrap; }

/* vmbar / svcbar */
.vmbar, .svcbar { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 12px; }
.vmbar a { padding: 5px 14px; border-radius: 999px; font-size: 13px; font-weight: 500;
           background: var(--c-border); color: var(--c-text); text-decoration: none; }
.svcbar a { padding: 4px 12px; border-radius: 8px; font-size: 12px; font-weight: 500;
            background: var(--c-bg); border: 1px solid var(--c-border);
            color: var(--c-text); text-decoration: none; }
.vmbar a:hover, .svcbar a:hover { background: var(--c-primary-lt); color: #fff; }
.vmbar a.active, .svcbar a.active { background: var(--c-accent); color: #1a1a1a; font-weight: 700; }

/* pre/code */
pre { background: var(--c-code-bg); color: var(--c-code-text);
      border-radius: 10px; padding: 18px 20px; font-size: 12.5px;
      line-height: 1.6; overflow-x: auto; white-space: pre; }
.meta { font-size: 12px; color: var(--c-muted); margin-bottom: 8px; }

/* buttons */
.btn { display: inline-block; padding: 8px 20px; border-radius: 8px;
       font-size: 13px; font-weight: 700; background: var(--c-primary);
       color: #fff; border: none; cursor: pointer; text-decoration: none; }
.btn:hover { background: var(--c-primary-lt); color: #fff; }
label { font-size: 13px; color: var(--c-text);
        display: flex; align-items: center; gap: 6px; cursor: pointer; }

/* settings panel */
.settings-panel {
  position: fixed; top: 52px; right: 0; width: 240px;
  background: var(--c-card); border-left: 1px solid var(--c-border);
  border-bottom: 1px solid var(--c-border); border-radius: 0 0 0 12px;
  padding: 16px; box-shadow: -4px 4px 16px #0002; z-index: 100; }
.settings-panel h3 { margin: 0 0 10px; font-size: 11px; color: var(--c-muted);
                     text-transform: uppercase; letter-spacing: .5px; }
.palette-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; }
.palette-btn  { border: 3px solid transparent; border-radius: 8px; padding: 8px 4px;
                color: #fff; font-size: 12px; font-weight: 700; cursor: pointer;
                text-shadow: 0 1px 3px #0006; transition: border-color .15s, opacity .15s; }
.palette-btn:hover, .palette-btn.active { border-color: var(--btn-accent, var(--c-accent)); opacity: 1; }
.palette-btn:not(:hover):not(.active) { opacity: .85; }
.custom-panel { margin-top: 12px; border-top: 1px solid var(--c-border); padding-top: 12px; }
.custom-panel label { display: flex; justify-content: space-between; align-items: center;
                      font-size: 12px; margin-bottom: 6px; color: var(--c-text); }
.custom-panel input[type=color] { width: 36px; height: 26px; padding: 0; border: 1px solid var(--c-border);
                                   border-radius: 4px; cursor: pointer; background: none; }
.custom-panel input[type=url],
.custom-panel input[type=text] { width: 100%; padding: 5px 8px; font-size: 11px; margin-top: 4px;
                                  border: 1px solid var(--c-border); border-radius: 5px;
                                  background: var(--c-bg); color: var(--c-text); }
.custom-hint  { font-size: 10px; color: var(--c-muted); margin: 2px 0 8px; line-height: 1.4; }

/* login */
.login-wrap { display: flex; align-items: center; justify-content: center; min-height: 100vh; }
.login-box  { background: var(--c-card); border: 1px solid var(--c-border);
              border-radius: 14px; padding: 38px 34px; width: 100%; max-width: 360px;
              box-shadow: 0 4px 24px #0002; }
.login-logo { height: 88px; display: none; margin: 0 auto 18px; }
.login-logo.visible { display: block; }
.login-box h1   { margin: 0 0 4px; font-size: 22px; text-align: center; }
.login-box .sub { margin: 0 0 22px; color: var(--c-muted); font-size: 14px; text-align: center; }
.login-box label { display: block; font-size: 13px; font-weight: 600;
                   margin-bottom: 4px; color: var(--c-muted); }
.login-box input { width: 100%; padding: 10px 12px; font-size: 15px;
                   border: 1px solid var(--c-border); border-radius: 8px;
                   margin-bottom: 14px; background: var(--c-bg);
                   color: var(--c-text); outline: none; }
.login-box input:focus { border-color: var(--c-primary); box-shadow: 0 0 0 3px rgba(55,65,81,.15); }
.login-box button { width: 100%; padding: 11px; font-size: 15px; font-weight: 700;
                    background: var(--c-primary); color: #fff; border: none;
                    border-radius: 8px; cursor: pointer; }
.login-box button:hover { background: var(--c-primary-lt); }
.error-msg { color: var(--c-err-text); font-size: 13px; margin: 0 0 14px; }
.export-pre { background: var(--c-card); border: 1px solid var(--c-border);
              border-radius: 10px; padding: 20px; font-size: 13px;
              overflow-x: auto; white-space: pre-wrap; color: var(--c-text); }
footer { text-align: center; font-size: 12px; color: var(--c-muted); padding: 20px 16px; }

/* mobile */
@media (max-width: 640px) {
  .fleet-name { display: none; }
  .topbar { padding: 0 10px; height: 46px; }
  .topbar-logo { height: 36px; }
  .topbar-nav a[href="/export"] { display: none; }
  .topbar-nav a, .topbar-nav button { padding: 5px 7px; font-size: 13px; }
}

/* service chips */
.svc-section { margin-top: 12px; border-top: 1px solid var(--c-border); padding-top: 10px; }
.svc-label   { font-size: 11px; font-weight: 600; color: var(--c-muted); text-transform: uppercase;
               letter-spacing: .5px; margin-bottom: 6px; }
.svc-chips   { display: flex; gap: 6px; flex-wrap: wrap; }
.svc-chip    { font-size: 11px; padding: 3px 9px; border-radius: 999px; text-decoration: none;
               background: var(--c-bg); border: 1px solid var(--c-border); color: var(--c-text);
               transition: background .12s; }
.svc-chip:hover { background: var(--c-accent); color: #1a1a1a; border-color: var(--c-accent); }

/* tools page */
.tools-grid   { display: grid; gap: 14px; grid-template-columns: repeat(auto-fit,minmax(280px,1fr)); margin-top: 16px; }
.payload-card { background: var(--c-card); border: 1px solid var(--c-border); border-radius: 12px;
                padding: 18px 20px; cursor: pointer; transition: border-color .15s; }
.payload-card:hover { border-color: var(--c-primary); }
.payload-title { font-size: 14px; font-weight: 700; margin: 0 0 4px; }
.payload-desc  { font-size: 13px; color: var(--c-muted); margin: 0; }
.script-editor { width: 100%; min-height: 140px; font-family: monospace; font-size: 13px;
                 padding: 12px; border: 1px solid var(--c-border); border-radius: 8px;
                 background: var(--c-code-bg); color: var(--c-code-text); resize: vertical; margin-top: 16px; }
.run-bar       { display: flex; gap: 10px; align-items: center; margin-top: 10px; flex-wrap: wrap; }
.vm-select     { padding: 7px 12px; border-radius: 8px; border: 1px solid var(--c-border);
                 background: var(--c-bg); color: var(--c-text); font-size: 13px; }
"""

# localStorage keys use 'fleet-' prefix (not 'mda-') for the generic starter.
THEME_JS = """
(function() {
  var saved = localStorage.getItem('fleet-theme');
  var pref  = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  var t = saved || pref;
  document.documentElement.setAttribute('data-theme', t);
  var icon = document.getElementById('theme-icon');
  if (icon) icon.textContent = t === 'dark' ? '☀' : '🌙';

  var pal = localStorage.getItem('fleet-palette');
  if (pal) { try {
    var p = JSON.parse(pal);
    var r = document.documentElement;
    r.style.setProperty('--c-primary',    p.primary);
    if (p.primaryLt) r.style.setProperty('--c-primary-lt', p.primaryLt);
    if (p.primaryDk) r.style.setProperty('--c-primary-dk', p.primaryDk);
    if (p.accent)    r.style.setProperty('--c-accent',     p.accent);
  } catch(e) {} }

  var logo = localStorage.getItem('fleet-logo');
  if (logo) {
    document.querySelectorAll('.topbar-logo,.login-logo').forEach(function(img) {
      img.src = logo; img.classList.add('visible');
    });
  }
})();

function toggleTheme() {
  var t = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', t);
  localStorage.setItem('fleet-theme', t);
  var icon = document.getElementById('theme-icon');
  if (icon) icon.textContent = t === 'dark' ? '☀' : '🌙';
}

var _spOpen = false;
function toggleSettings() {
  _spOpen = !_spOpen;
  var sp = document.getElementById('settings-panel');
  if (sp) sp.hidden = !_spOpen;
}

var _cpOpen = false;
function toggleCustomPanel() {
  _cpOpen = !_cpOpen;
  var cp = document.getElementById('custom-panel');
  if (cp) cp.hidden = !_cpOpen;
}

function applyPalette(btn) {
  var p  = btn.dataset.primary;
  var pl = btn.dataset.primaryLt;
  var pd = btn.dataset.primaryDk;
  var a  = btn.dataset.accent;
  var r  = document.documentElement;
  r.style.setProperty('--c-primary',    p);
  r.style.setProperty('--c-primary-lt', pl);
  r.style.setProperty('--c-primary-dk', pd);
  r.style.setProperty('--c-accent',     a);
  localStorage.setItem('fleet-palette', JSON.stringify({primary:p,primaryLt:pl,primaryDk:pd,accent:a}));
  document.querySelectorAll('.palette-btn').forEach(function(b) {
    b.classList.toggle('active', b === btn);
  });
}

function applyCustomPalette() {
  var p  = document.getElementById('cp-primary').value;
  var pl = document.getElementById('cp-primary-lt').value;
  var pd = document.getElementById('cp-primary-dk').value;
  var a  = document.getElementById('cp-accent').value;
  var logo = (document.getElementById('cp-logo') || {value:''}).value.trim();
  var r = document.documentElement;
  r.style.setProperty('--c-primary',    p);
  r.style.setProperty('--c-primary-lt', pl);
  r.style.setProperty('--c-primary-dk', pd);
  r.style.setProperty('--c-accent',     a);
  localStorage.setItem('fleet-palette', JSON.stringify({primary:p,primaryLt:pl,primaryDk:pd,accent:a}));
  if (logo) {
    document.querySelectorAll('.topbar-logo,.login-logo').forEach(function(img) {
      img.src = logo; img.classList.add('visible');
    });
    localStorage.setItem('fleet-logo', logo);
  }
  document.querySelectorAll('.palette-btn').forEach(function(b) { b.classList.remove('active'); });
}

function copyText(text, btn) {
  navigator.clipboard.writeText(text).then(function() {
    var orig = btn.textContent;
    btn.textContent = 'Copied!';
    setTimeout(function() { btn.textContent = orig; }, 1500);
  }).catch(function() { prompt('Copy:', text); });
}
"""

# Five named presets + the Custom slot rendered separately in _topbar().
# Tuple: (primary, primary_lt, primary_dk, accent, label)
PALETTE_PRESETS = [
    ("#374151", "#4b5563", "#1f2937", "#f59e0b", "Slate"),       # default neutral
    ("#1a3d6e", "#2d6cb5", "#0f2547", "#38bdf8", "Ocean"),       # navy + sky blue
    ("#285e39", "#3a7a50", "#1b3f28", "#c5a028", "MDA Green"),   # forest green + gold
    ("#0d1117", "#1c2128", "#010409", "#39ff14", "Neons"),       # near-black + neon green
    ("#6b4226", "#8a5530", "#4a2d1b", "#d4882a", "Earthy"),     # terracotta + warm amber
]

_LOG_SERVICES = [
    ("cloud-lab-a1-lottery",   "Lottery"),
    ("cloud-lab-orchestrator", "Orchestrator"),
    ("cloud-lab-console",      "Console"),
    ("cloud-lab-heartbeat",    "Heartbeat"),
    ("cloud-lab-crosswatch",   "Crosswatch"),
    ("cloud-lab-update",       "Update"),
]

_ROLE_SERVICES: dict[str, list[tuple[str, str]]] = {
    "management": [
        ("cloud-lab-orchestrator", "Orchestrator"),
        ("cloud-lab-console",      "Console"),
        ("cloud-lab-heartbeat",    "Heartbeat · 12h"),
        ("cloud-lab-crosswatch",   "Crosswatch · 6h"),
        ("cloud-lab-update",       "Auto-update · nightly"),
    ],
    "worker": [
        ("cloud-lab-a1-lottery",   "A1 Lottery"),
        ("cloud-lab-heartbeat",    "Heartbeat"),
        ("cloud-lab-crosswatch",   "Crosswatch"),
    ],
    "laboratory": [
        ("cloud-lab-heartbeat",    "Heartbeat"),
    ],
}

_PAYLOAD_PRESETS: list[tuple[str, str, str, str]] = [
    (
        "system-info",
        "System info",
        "CPU, memory, disk, uptime at a glance",
        "echo '=== Uptime ===' && uptime\necho\necho '=== Memory ===' && free -h --si\necho\necho '=== Disk ===' && df -h",
    ),
    (
        "list-services",
        "List services",
        "All cloud-lab systemd units and their status",
        "systemctl list-units 'cloud-lab-*' --all --no-pager",
    ),
    (
        "update-repo",
        "Update repo",
        "Pull latest code from origin and restart all services",
        "cd ~/cloud-lab && git pull --ff-only && sudo systemctl restart 'cloud-lab-*'",
    ),
    (
        "apt-upgrade",
        "APT upgrade",
        "Update package lists and upgrade installed packages",
        "sudo apt-get update -qq && sudo DEBIAN_FRONTEND=noninteractive apt-get upgrade -y",
    ),
    (
        "check-logs",
        "Check logs",
        "Recent journal entries for all cloud-lab services",
        "sudo journalctl -u 'cloud-lab-*' -n 50 --no-pager --output=short-iso",
    ),
]


# ── shared HTML helpers ───────────────────────────────────────────────────────

def _head(title: str, auto_refresh: int = 0) -> str:
    refresh = f'<meta http-equiv="refresh" content="{auto_refresh}">' if auto_refresh else ""
    return (
        f'<!doctype html><html lang="en"><head>'
        f'<meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'{refresh}'
        f'<title>{html.escape(title)}</title>'
        f'<style>{COMMON_CSS}</style>'
        f'<script>{THEME_JS}</script>'
        f'</head><body>'
    )


def _topbar(active: str = "") -> str:
    nav_items = [
        ("Fleet",  "/",       "fleet"),
        ("Stats",  "/stats",  "stats"),
        ("Logs",   "/logs",   "logs"),
        ("Tools",  "/tools",  "tools"),
        ("Export", "/export", "export"),
    ]
    links = " ".join(
        f'<a href="{href}" {_ACT if active == key else ""}>{label}</a>'
        for label, href, key in nav_items
    )
    preset_btns = " ".join(
        f'<button class="palette-btn"'
        f' style="background:{p};--btn-accent:{a}"'
        f' data-primary="{p}" data-primary-lt="{pl}"'
        f' data-primary-dk="{pd}" data-accent="{a}"'
        f' onclick="applyPalette(this)">{name}</button>'
        for p, pl, pd, a, name in PALETTE_PRESETS
    )
    # Custom button: diagonal split of two grays to suggest "make your own"
    custom_btn = (
        '<button class="palette-btn"'
        ' style="background:linear-gradient(135deg,#4b5563 55%,#9ca3af 45%)"'
        ' onclick="toggleCustomPanel()">Custom</button>'
    )
    custom_panel = (
        '<div id="custom-panel" class="custom-panel" hidden>'
        '<h3>Pick colors</h3>'
        '<label>Primary <input type="color" id="cp-primary" value="#374151"></label>'
        '<label>Primary (light) <input type="color" id="cp-primary-lt" value="#4b5563"></label>'
        '<label>Primary (dark) <input type="color" id="cp-primary-dk" value="#1f2937"></label>'
        '<label>Accent <input type="color" id="cp-accent" value="#f59e0b"></label>'
        '<h3 style="margin-top:10px">Logo</h3>'
        '<input type="url" id="cp-logo" placeholder="https://... or data:image/...">'
        '<p class="custom-hint">'
        'PNG or SVG &middot; transparent background &middot; ~44 px tall<br>'
        'To embed locally: <code>base64 -w0 logo.png</code> then prefix with<br>'
        '<code>data:image/png;base64,</code>'
        '</p>'
        '<button class="btn" onclick="applyCustomPalette()" style="width:100%;padding:7px;margin-top:4px">Apply</button>'
        '</div>'
    )
    return (
        f'<div class="topbar">'
        f'<div class="topbar-left">'
        f'<img id="topbar-logo" class="topbar-logo" alt="Fleet Logo">'
        f'<span class="fleet-name">{html.escape(FLEET_NAME)}</span>'
        f'</div>'
        f'<nav class="topbar-nav">{links}'
        f'<button class="theme-btn" title="Toggle dark/light" onclick="toggleTheme()">'
        f'<span id="theme-icon">&#127769;</span></button>'
        f'<button class="theme-btn" title="Appearance" onclick="toggleSettings()">&#9881;</button>'
        f'<a href="/logout" class="sign-out">Sign out</a>'
        f'</nav></div>'
        f'<div id="settings-panel" class="settings-panel" hidden>'
        f'<h3>Color palette</h3>'
        f'<div class="palette-grid">{preset_btns}{custom_btn}</div>'
        f'{custom_panel}'
        f'</div>'
    )


# ── VM cards ──────────────────────────────────────────────────────────────────

def vm_cards() -> str:
    refresh_oci_snapshots()
    fleet = load_json(TOOLS_DIR / "fleet.json") or {"vms": []}
    with _hb_lock:
        hbs = dict(_heartbeats)
    env      = _mgmt_env()
    ssh_user = env.get("OCI_SSH_USER", "ubuntu")

    cards = []
    for vm in fleet.get("vms", []):
        name       = vm.get("name", "")
        profile    = load_json(PROFILE_DIR / f"{name}.json") or {}
        instance   = profile.get("instance", {})
        state      = instance.get("lifecycle-state", "UNKNOWN")
        shape      = html.escape(instance.get("shape") or vm.get("shape", ""))
        public_ip  = html.escape(profile.get("public_ip") or "—")
        private_ip = html.escape(profile.get("private_ip") or "—")
        role       = vm.get("role", name)
        role_label = html.escape(role)
        notes      = html.escape(vm.get("notes", ""))
        synced_at  = profile.get("synced_at", "")

        hb = hbs.get(name, {})
        hb_time  = hb.get("received_at", "")
        hb_ago   = fmt_ago(hb_time) if hb_time else '<span class="warn-text">not received yet</span>'
        uptime   = html.escape(hb.get("uptime", "") or "—")
        snap_ago = fmt_ago(synced_at) if synced_at else "never"

        state_cls = state.lower().replace(" ", "-")
        badge = f'<span class="badge {state_cls}">{html.escape(state)}</span>'

        ssh_cmd = f"ssh -i ~/.ssh/fleet.key {ssh_user}@{public_ip}"

        actions = [
            f'<a class="act-btn" href="/stats?vm={html.escape(name)}">Live stats</a>',
            f'<a class="act-btn" href="/logs?vm={html.escape(name)}">Logs</a>',
        ]
        if public_ip != "—":
            actions.append(
                f'<button class="act-btn" onclick="copyText({json.dumps(ssh_cmd)},this)">Copy SSH</button>'
            )
        actions.append(f'<a class="act-btn" href="/tools?vm={html.escape(name)}">Tools</a>')
        if name == "worker":
            actions.append(
                '<a class="act-btn accent" href="/logs?vm=worker&service=cloud-lab-a1-lottery">Lottery logs</a>'
            )

        svcs = _ROLE_SERVICES.get(role, [])
        if svcs:
            chip_links = "".join(
                f'<a class="svc-chip" href="/logs?vm={html.escape(name)}&service={html.escape(svc_id)}">'
                f'{html.escape(svc_lbl)}</a>'
                for svc_id, svc_lbl in svcs
            )
            svc_html = (
                f'<div class="svc-section">'
                f'<div class="svc-label">Background Services</div>'
                f'<div class="svc-chips">{chip_links}</div>'
                f'</div>'
            )
        else:
            svc_html = ""

        cards.append(
            f'<div class="card">'
            f'<div class="card-header">'
            f'<span class="vm-name">{html.escape(name)}</span>{badge}'
            f'</div>'
            f'<p><b>Role:</b> {role_label}</p>'
            f'<p><b>Shape:</b> {shape}</p>'
            f'<p><b>Uptime:</b> {uptime}</p>'
            f'<p><b>Public IP:</b> {public_ip}</p>'
            f'<p><b>Private IP:</b> {private_ip}</p>'
            f'<p><b>OCI snapshot:</b> {html.escape(snap_ago)}</p>'
            f'<p><b>Heartbeat:</b> {hb_ago}</p>'
            + (f'<p class="notes">{notes}</p>' if notes else "")
            + f'<div class="card-actions">{"".join(actions)}</div>'
            + svc_html
            + '</div>'
        )
    return "\n".join(cards)


# ── stats page ────────────────────────────────────────────────────────────────

def stats_page(vm_name: str, fleet_vms: list) -> bytes:
    title  = f"{FLEET_NAME} — {vm_name} stats"
    raw    = collect_local_stats() if vm_name == "management" else collect_remote_stats(vm_name)
    output = html.escape(raw)
    now    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    vm_links = " ".join(
        f'<a href="/stats?vm={html.escape(v)}" {_ACT if v == vm_name else ""}>{html.escape(v)}</a>'
        for v in fleet_vms
    )
    page = (
        _head(title)
        + _topbar("stats")
        + f'<div class="content">'
        + f'<div class="vmbar">{vm_links}</div>'
        + f'<p class="meta">Snapshot taken {now}</p>'
        + f'<pre>{output}</pre>'
        + '<div style="display:flex;gap:12px;align-items:center;margin-top:12px">'
        + f'<a class="btn" href="/stats?vm={html.escape(vm_name)}">Refresh</a>'
        + '<label><input type="checkbox" onchange="(function(cb){'
        + 'if(cb.checked){window._ar=setInterval(()=>location.reload(),10000)}'
        + 'else{clearInterval(window._ar)}})(this)"> Auto-refresh 10s</label>'
        + '</div></div></body></html>'
    )
    return page.encode("utf-8")


# ── logs page ─────────────────────────────────────────────────────────────────

def logs_page(vm_name: str, service_name: str, fleet_vms: list) -> bytes:
    title  = f"{FLEET_NAME} — {vm_name} logs"
    raw    = (collect_local_logs(service_name) if vm_name == "management"
              else collect_remote_logs(vm_name, service_name))
    output = html.escape(raw)
    now    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    vm_links = " ".join(
        f'<a href="/logs?vm={html.escape(v)}&service={html.escape(service_name)}" '
        f'{_ACT if v == vm_name else ""}>{html.escape(v)}</a>'
        for v in fleet_vms
    )
    svc_links = " ".join(
        f'<a href="/logs?vm={html.escape(vm_name)}&service={html.escape(svc)}" '
        f'{_ACT if svc == service_name else ""}>{html.escape(label)}</a>'
        for svc, label in _LOG_SERVICES
    )
    page = (
        _head(title)
        + _topbar("logs")
        + f'<div class="content">'
        + f'<div class="vmbar">{vm_links}</div>'
        + f'<div class="svcbar">{svc_links}</div>'
        + f'<p class="meta">Fetched {now}</p>'
        + f'<pre>{output}</pre>'
        + '<div style="display:flex;gap:12px;align-items:center;margin-top:12px">'
        + f'<a class="btn" href="/logs?vm={html.escape(vm_name)}&service={html.escape(service_name)}">Refresh</a>'
        + '<label><input type="checkbox" onchange="(function(cb){'
        + 'if(cb.checked){window._ar=setInterval(()=>location.reload(),15000)}'
        + 'else{clearInterval(window._ar)}})(this)"> Auto-refresh 15s</label>'
        + '</div></div></body></html>'
    )
    return page.encode("utf-8")


# ── login page ────────────────────────────────────────────────────────────────

def login_page(error: bool = False, locked: bool = False) -> bytes:
    if locked:
        err = '<p class="error-msg">Too many failed attempts. Try again in 15 minutes.</p>'
    elif error:
        err = '<p class="error-msg">Incorrect username or password.</p>'
    else:
        err = ""
    page = (
        _head(FLEET_NAME)
        + f'<div class="login-wrap"><div class="login-box">'
        + f'<img id="login-logo" class="login-logo" alt="Fleet Logo">'
        + f'<h1>{html.escape(FLEET_NAME)}</h1>'
        + '<p class="sub">Private admin console</p>'
        + f'{err}'
        + '<form method="POST" action="/login">'
        + '<label for="u">Username</label>'
        + '<input id="u" type="text" name="username" autocomplete="username" autofocus>'
        + '<label for="p">Password</label>'
        + '<input id="p" type="password" name="password" autocomplete="current-password">'
        + '<button type="submit">Sign in</button>'
        + '</form></div></div></body></html>'
    )
    return page.encode("utf-8")


# ── export page ───────────────────────────────────────────────────────────────

def export_page() -> bytes:
    fleet = load_json(TOOLS_DIR / "fleet.json") or {"vms": []}
    env   = _mgmt_env()
    lines = [
        f"# {FLEET_NAME} — Fleet Connection Details",
        f"# Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
    ]
    for vm in fleet.get("vms", []):
        name    = vm.get("name", "")
        profile = load_json(PROFILE_DIR / f"{name}.json") or {}
        pub     = profile.get("public_ip", "")
        priv    = profile.get("private_ip", "")
        lines += [
            f"# {name.upper()}",
            f"OCI_{name.upper()}_HOST={pub}",
            f"OCI_{name.upper()}_PRIVATE_IP={priv}",
            "",
        ]
    ssh_key = env.get("OCI_SSH_PRIVATE_KEY_PATH", "~/.ssh/fleet.key")
    ssh_user = env.get("OCI_SSH_USER", "ubuntu")
    lines += [
        "# SSH",
        f"OCI_SSH_USER={ssh_user}",
        f"OCI_SSH_PRIVATE_KEY_PATH={ssh_key}",
        f"# SSH example: {ssh_key} {ssh_user}@<public-ip>",
    ]
    content = html.escape("\n".join(lines))
    page = (
        _head(f"{FLEET_NAME} — Export")
        + _topbar("export")
        + f'<div class="content">'
        + f'<h2 style="margin:0 0 16px">Fleet connection details</h2>'
        + f'<pre class="export-pre">{content}</pre>'
        + '</div></body></html>'
    )
    return page.encode("utf-8")


# ── tools page ───────────────────────────────────────────────────────────────

def run_payload_on_vm(vm_name: str, script: str) -> tuple[bool, str]:
    if vm_name == "management":
        try:
            result = subprocess.run(
                ["bash", "-s"],
                input=script,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, timeout=30,
            )
            return True, result.stdout or "(no output)"
        except Exception as exc:
            return False, str(exc)
    else:
        refresh_oci_snapshots()
        env      = _mgmt_env()
        ssh_key  = env.get("OCI_SSH_PRIVATE_KEY_PATH", str(Path.home() / ".ssh" / "fleet.key"))
        ssh_user = env.get("OCI_SSH_USER", "ubuntu")
        profile  = load_json(TOOLS_DIR / "vm-profiles" / f"{vm_name}.json") or {}
        public_ip = profile.get("public_ip", "")
        if not public_ip or public_ip == "—":
            return False, f"No public IP found for {vm_name}."
        key_path = str(Path(ssh_key).expanduser())
        try:
            result = subprocess.run(
                ["ssh", "-i", key_path,
                 "-o", "StrictHostKeyChecking=accept-new",
                 "-o", "ConnectTimeout=8", "-o", "BatchMode=yes",
                 f"{ssh_user}@{public_ip}", "bash -s"],
                input=script,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, timeout=60,
            )
            return True, result.stdout or "(no output)"
        except subprocess.TimeoutExpired:
            return False, f"SSH timed out connecting to {vm_name}."
        except Exception as exc:
            return False, str(exc)


def tools_page(preselect_vm: str = "") -> bytes:
    title     = f"{FLEET_NAME} — Tools"
    fleet     = load_json(TOOLS_DIR / "fleet.json") or {"vms": []}
    vms       = [v.get("name") for v in fleet.get("vms", []) if v.get("name")]
    if not vms:
        vms = ["management"]
    preselect = preselect_vm if preselect_vm in vms else vms[0]

    vm_options = "".join(
        f'<option value="{html.escape(v)}"{" selected" if v == preselect else ""}>{html.escape(v)}</option>'
        for v in vms
    )

    preset_cards = "".join(
        f'<div class="payload-card" onclick="loadPreset({json.dumps(script)})">'
        f'<p class="payload-title">{html.escape(label)}</p>'
        f'<p class="payload-desc">{html.escape(desc)}</p>'
        f'</div>'
        for _slug, label, desc, script in _PAYLOAD_PRESETS
    )

    page = (
        _head(title)
        + _topbar("tools")
        + '<div class="content">'
        + '<h2 style="margin:0 0 4px">Admin Tools</h2>'
        + '<p style="color:var(--c-muted);font-size:14px;margin:0 0 16px">Run scripts on fleet VMs via SSH.</p>'
        + '<h3 style="font-size:13px;font-weight:700;margin:0 0 8px;color:var(--c-muted);text-transform:uppercase;letter-spacing:.5px">Presets</h3>'
        + '<div class="tools-grid">' + preset_cards + '</div>'
        + '<h3 style="font-size:13px;font-weight:700;margin:20px 0 8px;color:var(--c-muted);text-transform:uppercase;letter-spacing:.5px">Script</h3>'
        + '<textarea id="payload-editor" class="script-editor" placeholder="Enter bash script here, or click a preset above..."></textarea>'
        + '<div class="run-bar">'
        + '<select id="payload-vm" class="vm-select">' + vm_options + '</select>'
        + '<button class="btn" onclick="runPayload()">Run on VM</button>'
        + '<span id="payload-status" style="font-size:13px;color:var(--c-muted)"></span>'
        + '</div>'
        + '<pre id="payload-output" style="margin-top:16px;display:none"></pre>'
        + "<script>"
        + "function loadPreset(script){"
        + "  document.getElementById('payload-editor').value=script;"
        + "  document.getElementById('payload-editor').scrollIntoView({behavior:'smooth'});}"
        + "function runPayload(){"
        + "  var script=document.getElementById('payload-editor').value.trim();"
        + "  var vm=document.getElementById('payload-vm').value;"
        + "  if(!script){alert('No script entered.');return;}"
        + "  var status=document.getElementById('payload-status');"
        + "  var out=document.getElementById('payload-output');"
        + "  status.textContent='Running…';out.style.display='none';"
        + "  fetch('/run-payload',{method:'POST',"
        + "    headers:{'Content-Type':'application/json'},"
        + "    body:JSON.stringify({vm:vm,script:script})})"
        + "  .then(function(r){return r.json();})"
        + "  .then(function(d){"
        + "    status.textContent=d.ok?'Done.':'Error.';"
        + "    out.textContent=d.output||'(no output)';out.style.display='block';})"
        + "  .catch(function(e){"
        + "    status.textContent='Request failed.';"
        + "    out.textContent=String(e);out.style.display='block';});}"
        + "</script>"
        + '</div></body></html>'
    )
    return page.encode("utf-8")


# ── HTTP handler ──────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip("/") or "/"
        qs     = parse_qs(parsed.query)

        if path == "/login":
            self._html(200, login_page()); return
        if path == "/logout":
            self._redirect("/login",
                           f'{COOKIE_NAME}=; Max-Age=0; Path=/; HttpOnly; SameSite=Strict')
            return

        if not _is_authed(self):
            self._redirect("/login"); return

        if path == "/":
            fleet  = load_json(TOOLS_DIR / "fleet.json") or {"vms": []}
            names  = [v.get("name") for v in fleet.get("vms", []) if v.get("name")]
            cards  = vm_cards()
            events = fleet_events_html()
            page = (
                _head(FLEET_NAME, auto_refresh=60)
                + _topbar("fleet")
                + f'<div class="content">'
                + f'<div class="grid">{cards}</div>'
                + f'<p class="section-title">Recent Fleet Events</p>'
                + f'<div>{events}</div>'
                + f'<footer>Auto-refreshes every 60s &middot; management VM</footer>'
                + '</div></body></html>'
            )
            self._html(200, page.encode()); return

        if path == "/stats":
            fleet     = load_json(TOOLS_DIR / "fleet.json") or {"vms": []}
            fleet_vms = [v.get("name") for v in fleet.get("vms", []) if v.get("name")]
            vm_name   = qs.get("vm", ["management"])[0]
            if vm_name not in fleet_vms:
                vm_name = "management"
            self._html(200, stats_page(vm_name, fleet_vms)); return

        if path == "/logs":
            fleet       = load_json(TOOLS_DIR / "fleet.json") or {"vms": []}
            fleet_vms   = [v.get("name") for v in fleet.get("vms", []) if v.get("name")]
            vm_name     = qs.get("vm", ["worker"])[0]
            service_raw = qs.get("service", ["cloud-lab-a1-lottery"])[0]
            valid_svcs  = {s for s, _ in _LOG_SERVICES}
            service     = service_raw if service_raw in valid_svcs else "cloud-lab-a1-lottery"
            if vm_name not in fleet_vms:
                vm_name = fleet_vms[0] if fleet_vms else "management"
            self._html(200, logs_page(vm_name, service, fleet_vms)); return

        if path == "/export":
            self._html(200, export_page()); return

        if path == "/tools":
            preselect = qs.get("vm", [""])[0]
            self._html(200, tools_page(preselect)); return

        self._html(404, b"Not found")

    def do_POST(self):
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip("/") or "/"

        if path == "/run-payload":
            if not _is_authed(self):
                self._json(403, {"ok": False, "output": "Not authenticated."}); return
            length = int(self.headers.get("Content-Length", 0))
            try:
                data   = json.loads(self.rfile.read(length))
                vm     = str(data.get("vm", "management"))[:64]
                script = str(data.get("script", ""))[:8192]
            except Exception:
                self._json(400, {"ok": False, "output": "Bad request."}); return
            fleet     = load_json(TOOLS_DIR / "fleet.json") or {"vms": []}
            fleet_vms = {v.get("name") for v in fleet.get("vms", []) if v.get("name")}
            fleet_vms.add("management")
            if vm not in fleet_vms:
                self._json(400, {"ok": False, "output": f"Unknown VM: {vm}"}); return
            ok, output = run_payload_on_vm(vm, script)
            self._json(200, {"ok": ok, "output": output}); return

        if path == "/heartbeat":
            length = int(self.headers.get("Content-Length", 0))
            body   = self.rfile.read(length)
            try:
                data = json.loads(body)
                vm   = str(data.get("vm", ""))[:64]
                if vm:
                    now = datetime.now(timezone.utc).isoformat()
                    with _hb_lock:
                        existing = _heartbeats.get(vm, {"events": []})
                        existing["received_at"] = now
                        existing["uptime"]      = str(data.get("uptime", ""))[:80]
                        ev = data.get("event")
                        if ev:
                            existing.setdefault("events", []).append(
                                {"received_at": now, "event": str(ev)[:200]}
                            )
                            existing["events"] = existing["events"][-50:]
                        _heartbeats[vm] = existing
                    save_heartbeats()
            except Exception:
                pass
            self._json(200, {"ok": True}); return

        if path == "/login":
            length  = int(self.headers.get("Content-Length", 0))
            body    = self.rfile.read(length).decode("utf-8", errors="replace")
            fields  = parse_qs(body)
            uname   = fields.get("username", [""])[0].strip()
            pw      = fields.get("password", [""])[0]
            client  = self.client_address[0]

            if not _check_rate_limit(client):
                self._html(429, login_page(error=True, locked=True)); return
            if uname == USERNAME and _verify_password(pw):
                _clear_fails(client)
                sid = _create_session()
                self._redirect("/",
                    f'{COOKIE_NAME}={sid}; Max-Age={SESSION_DURATION}; '
                    f'Path=/; HttpOnly; SameSite=Strict')
            else:
                _record_fail(client)
                self._html(401, login_page(error=True))
            return

        self._html(404, b"Not found")

    # ── response helpers ───────────────────────────────────────────────────────

    def _html(self, code: int, body: bytes | str) -> None:
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, data: dict) -> None:
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str, set_cookie: str = "") -> None:
        self.send_response(303)
        self.send_header("Location", location)
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.end_headers()

    def log_message(self, fmt, *args):
        pass   # suppress per-request access log noise


# ── startup ───────────────────────────────────────────────────────────────────

def main() -> None:
    load_heartbeats()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"[admin_console] {FLEET_NAME} — listening on {HOST}:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
