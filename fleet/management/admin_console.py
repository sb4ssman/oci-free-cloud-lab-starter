#!/usr/bin/env python3
"""
Cloud Lab admin console — runs on the management VM.
Accessible at https://<ADMIN_DOMAIN> via Caddy reverse proxy.

Endpoints:
  GET  /                    Fleet status page (login required)
  GET  /login               Login form
  POST /login               Validate credentials, set session cookie, redirect to /
  GET  /logout              Clear session, redirect to /login
  POST /heartbeat           Liveness pings from worker/laboratory (Bearer token if configured)
  GET  /health              Liveness probe for UptimeRobot (no auth, JSON response)
  GET  /export              Fleet connection details (login required)
  GET  /stats?vm=<name>     Live system stats (login required)
  GET  /logs?vm=<name>&service=<svc>  Journalctl logs (login required)
  GET  /settings            LCARS mode settings — layout, scale, audio (login required)
  GET  /static/...          LCARS framework assets (css, fonts, beeps)
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import random
import re
import secrets
import shlex
import shutil
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


# ── config ────────────────────────────────────────────────────────────────────

HOST       = os.getenv("ADMIN_CONSOLE_HOST", "127.0.0.1")
PORT       = int(os.getenv("ADMIN_CONSOLE_PORT", "8765"))
USERNAME   = os.getenv("ADMIN_USERNAME", "admin")
PW_HASH    = os.getenv("ADMIN_PASSWORD_HASH", "")
FLEET_NAME = os.getenv("FLEET_NAME", "Cloud Lab")
DEV_MODE   = os.getenv("DEV_MODE", "") == "1" or "--dev" in __import__("sys").argv
TOOLS_DIR  = Path(os.getenv("CLOUD_LAB_DIR",
             str(Path.home() / "cloud-lab"))).expanduser()
STATIC_DIR       = Path(__file__).resolve().parents[2] / "static"
STATIC_ASSET_VERSION = "thelcars-v26-1"
PROFILE_DIR     = TOOLS_DIR / "vm-profiles"
HEARTBEATS_FILE = TOOLS_DIR / "vm-profiles" / "_heartbeats.json"
AUDIT_LOG        = TOOLS_DIR / "logs" / "audit.jsonl"
QUEUE_API_KEY    = os.getenv("QUEUE_API_KEY", "")
HEARTBEAT_TOKEN  = os.getenv("FLEET_HEARTBEAT_TOKEN", "")

COOKIE_NAME      = "fleet_session"
SESSION_DURATION = 7 * 24 * 3600   # 7 days

# Default interface: "standard" (classic dashboard) or "lcars".
# Users switch at runtime via settings; choice persists in a cookie.
DEFAULT_UI_MODE = os.getenv("CONSOLE_DEFAULT_UI", "standard")

# Per-request LCARS presentation state (layout + scale), set from cookies by
# the handler before rendering. Thread-local because the server is threaded.
_ui_ctx = threading.local()

_sessions: dict[str, float] = {}
_sessions_lock = threading.Lock()

_heartbeats: dict[str, dict] = {}
_hb_lock = threading.Lock()

_last_oci_snap: float = 0.0
_SNAP_TTL: float = 300.0   # seconds between OCI API refreshes

_quota_cache: dict = {}
_quota_cache_ts: float = 0.0
_QUOTA_TTL: float = 3600.0  # quota refreshes every hour

_login_fails: dict[str, tuple[int, float]] = {}
_fails_lock  = threading.Lock()
MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_SECONDS    = 900

MAX_LOGIN_BODY      = 8_192
MAX_HEARTBEAT_BODY  = 16_384
MAX_API_BODY        = 64_000


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
    if DEV_MODE:
        return True
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
    global _last_oci_snap
    now = time.monotonic()
    if now - _last_oci_snap < _SNAP_TTL:
        return   # cached snapshot is fresh enough
    _last_oci_snap = now

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
    _update_quota_cache()


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
    cmd = f"sudo journalctl -u {shlex.quote(service)} -n {lines} --no-pager --output=short-iso 2>&1"
    return _ssh_run(vm_name, cmd, timeout=20)


def fleet_events_html() -> str:
    with _hb_lock:
        hbs = dict(_heartbeats)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    all_events: list[dict] = []
    for vm_name, hb in hbs.items():
        for ev in hb.get("events", []):
            if ev.get("received_at", "") < cutoff:
                continue
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


# ── quota, TLS, DuckDNS, audit, service-control, queue helpers ────────────────

def _is_api_authed(handler: "BaseHTTPRequestHandler") -> bool:
    """Accept session cookie OR Bearer token for API endpoints."""
    if _is_authed(handler):
        return True
    if QUEUE_API_KEY:
        auth = handler.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and secrets.compare_digest(auth[7:].strip(), QUEUE_API_KEY):
            return True
    return False


def _is_heartbeat_authed(handler: "BaseHTTPRequestHandler") -> bool:
    """Require a shared heartbeat token when configured.

    Empty token keeps old deployments compatible, but new starters should set it.
    """
    if not HEARTBEAT_TOKEN:
        return True
    auth = handler.headers.get("Authorization", "")
    return auth.startswith("Bearer ") and secrets.compare_digest(auth[7:].strip(), HEARTBEAT_TOKEN)


def _write_audit(action: str, vm: str, details: str,
                 handler: "BaseHTTPRequestHandler | None" = None) -> None:
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts":     datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "action": action,
        "vm":     vm,
        "details": details,
        "ip":     handler.client_address[0] if handler else "local",
    }
    with AUDIT_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def _lcars_mgmt_tls_html() -> str:
    """Return HTML rows for TLS cert expiry and DuckDNS status (management card only)."""
    import subprocess as _sp, datetime as _dt
    rows = []
    cert_file: Path | None = None
    for cert_root in [
        Path.home() / ".local" / "share" / "caddy" / "certificates",
        Path("/var/lib/caddy/.local/share/caddy/certificates"),
        Path("/etc/caddy/certificates"),
    ]:
        try:
            root_exists = cert_root.exists()
        except PermissionError:
            root_exists = False
        if root_exists:
            try:
                for crt in sorted(cert_root.rglob("*.crt"), key=lambda p: p.stat().st_mtime, reverse=True):
                    cert_file = crt
                    break
            except PermissionError:
                pass
        if cert_file:
            break
    if cert_file:
        try:
            out  = _sp.check_output(["openssl", "x509", "-enddate", "-noout", "-in", str(cert_file)],
                                    text=True, stderr=_sp.DEVNULL, timeout=5).strip()
            date_str = out.split("=", 1)[1].strip()
            exp  = _dt.datetime.strptime(date_str, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=_dt.timezone.utc)
            days = (exp - _dt.datetime.now(_dt.timezone.utc)).days
            cls  = ' class="warn-text"' if days < 14 else ""
            rows.append(f'<div class="vm-field"><b>TLS cert</b>'
                        f'<span{cls}>expires in {days}d ({exp.strftime("%Y-%m-%d")})</span></div>')
        except Exception:
            rows.append('<div class="vm-field"><b>TLS cert</b><span class="muted">unable to read</span></div>')
    else:
        rows.append('<div class="vm-field"><b>TLS cert</b><span class="muted">not configured</span></div>')
    for log_path in [TOOLS_DIR / "logs" / "duckdns.log",
                     Path.home() / ".config" / "cloud-lab" / "duckdns.log"]:
        if log_path.exists():
            try:
                last = log_path.read_text(encoding="utf-8").strip().splitlines()[-1]
                rows.append(f'<div class="vm-field"><b>DuckDNS</b>'
                            f'<span class="muted">{html.escape(last[:80])}</span></div>')
            except Exception:
                pass
            break
    return "".join(rows)


def _update_quota_cache() -> None:
    global _quota_cache, _quota_cache_ts
    import time as _t
    now = _t.monotonic()
    if now - _quota_cache_ts < _QUOTA_TTL:
        return
    env = _mgmt_env()
    compartment_id = env.get("OCI_COMPARTMENT_ID", "")
    if not compartment_id:
        return
    # Known Always Free maximums — used as fallback when OCI API is unreachable.
    # A1 values updated June 2026 after Oracle halved the free-tier allocation.
    _AF_MAX = {
        "E2.Micro VMs": 2,
        "A1 OCPUs":     2,
        "A1 RAM (GB)":  12,
    }
    limits = {}
    for limit_name, label in [
        ("standard-e2-micro-count",   "E2.Micro VMs"),
        ("standard-a1-ocpus",         "A1 OCPUs"),
        ("standard-a1-memory-in-gbs", "A1 RAM (GB)"),
    ]:
        try:
            data = oci_cmd(["limits", "resource-availability", "get",
                            "--compartment-id", compartment_id,
                            "--service-name", "compute",
                            "--limit-name", limit_name], timeout=30).get("data", {})
            quota = data.get("effective-quota-value") or _AF_MAX.get(label, "?")
            limits[label] = {"used": data.get("used", "?"), "quota": quota}
        except Exception as exc:
            print(f"[quota] {label} ({limit_name}): {exc!s:.300}", flush=True)
            limits[label] = {"used": "?", "quota": _AF_MAX.get(label, "?")}
    _quota_cache    = limits
    _quota_cache_ts = now


def _quota_bars() -> str:
    """Oracle free-tier quota telemetry bars for the fleet status band."""
    fallback = {
        "E2.Micro VMs": {"used": "?", "quota": 2},
        "A1 OCPUs": {"used": "?", "quota": 2},
        "A1 RAM (GB)": {"used": "?", "quota": 12},
    }
    data = _quota_cache or fallback
    rows = []
    for label in ("E2.Micro VMs", "A1 OCPUs", "A1 RAM (GB)"):
        item = data.get(label, fallback[label])
        used = item.get("used", "?")
        quota = item.get("quota", "?")
        try:
            pct = max(0, min(100, int((float(used) / float(quota)) * 100)))
        except Exception:
            pct = 6
        rows.append(
            '<div class="quota-row">'
            f'<span class="quota-label">{html.escape(label)}</span>'
            '<span class="quota-track">'
            f'<span class="quota-fill" style="width:{pct}%"></span>'
            '</span>'
            f'<span class="quota-num">{html.escape(str(used))}<span>/{html.escape(str(quota))}</span></span>'
            '</div>'
        )
    return '<div class="quota-bars">' + "".join(rows) + '</div>'


def _read_vm_queue(vm_name: str) -> list[dict]:
    if vm_name == "management":
        p = TOOLS_DIR / "queue.json"
        try: return json.loads(p.read_text(encoding="utf-8"))
        except Exception: return []
    raw = _ssh_run(vm_name, "cat ~/cloud-lab/queue.json 2>/dev/null || echo '[]'", timeout=10)
    try: return json.loads(raw)
    except Exception: return []


def _read_vm_crontab(vm_name: str) -> str:
    if vm_name == "management":
        try:
            result = subprocess.run(["crontab", "-l"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=5)
            return result.stdout or "(no crontab)"
        except Exception as exc: return f"Error: {exc}"
    return _ssh_run(vm_name, "crontab -l 2>&1 || echo '(no crontab)'", timeout=10)


def _lcars_svc_row(name: str, svc: str, label: str, note: str = "") -> str:
    vm_h  = html.escape(name)
    svc_h = html.escape(svc)
    lbl_h = html.escape(label)
    note_h = f'<div class="work-note">{html.escape(note)}</div>' if note else ""
    if not svc:
        return (
            f'<div class="work-row"><div>'
            f'<div class="work-label">{lbl_h}</div>{note_h}'
            f'</div></div>'
        )
    return (
        f'<div class="work-row"><div>'
        f'<a class="work-label" href="/logs?vm={vm_h}&service={svc_h}">{lbl_h}</a>{note_h}'
        f'</div><div class="svc-actions">'
        f'<button class="background-bluey" title="restart" data-vm="{vm_h}" data-svc="{svc_h}"'
        f' onclick="svcCtl(this.dataset.vm,this.dataset.svc,&apos;restart&apos;)">&#x21BA;</button>'
        f'<button class="background-tomato" title="stop" data-vm="{vm_h}" data-svc="{svc_h}"'
        f' onclick="svcCtl(this.dataset.vm,this.dataset.svc,&apos;stop&apos;)">&#x25A0;</button>'
        f'<button class="background-lima-bean" title="start" data-vm="{vm_h}" data-svc="{svc_h}"'
        f' onclick="svcCtl(this.dataset.vm,this.dataset.svc,&apos;start&apos;)">&#x25BA;</button>'
        f'</div></div>'
    )


def _lcars_plain_work_row(label: str, note: str = "", status: str = "") -> str:
    status_h = (
        f'<span class="badge badge-{html.escape(status.lower())}">{html.escape(status)}</span>'
        if status else ""
    )
    note_h = f'<div class="work-note">{html.escape(note)}</div>' if note else ""
    return (
        f'<div class="work-row"><div>'
        f'<div class="work-label">{html.escape(label)}</div>{note_h}'
        f'</div>{status_h}</div>'
    )


def _lcars_work_section(title: str, rows: str) -> str:
    if not rows:
        rows = '<div class="work-empty">&mdash; none &mdash;</div>'
    return (
        f'<div class="vm-sec">'
        f'<div class="vm-sec-label">{html.escape(title)}</div>'
        f'{rows}'
        f'</div>'
    )


def _queue_sections(name: str, row) -> tuple[str, str, str]:
    """Shared queue summary; `row(label, note, status)` builds one row."""
    jobs = _read_vm_queue(name)
    running = [j for j in jobs if j.get("status") == "running"]
    pending = [j for j in jobs if j.get("status") == "pending"]
    completed = [j for j in jobs if j.get("status") in ("done", "failed")]
    pending.sort(key=lambda j: (j.get("priority", 5), j.get("queued_at", "")))
    completed.sort(key=lambda j: j.get("completed_at") or "", reverse=True)

    active_rows = "".join(
        row(j.get("label", "Queued job"), j.get("started_at", ""), "running")
        for j in running[:2]
    )
    queue_rows = "".join(
        row(
            j.get("label", "Queued job"),
            f'priority {j.get("priority", 5)} · queued {(j.get("queued_at") or "")[:16]}',
            "pending",
        )
        for j in pending[:3]
    )
    done_rows = "".join(
        row(
            j.get("label", "Queued job"),
            f'exit {j.get("exit_code", "")} · {(j.get("completed_at") or "")[:16]}',
            j.get("status", ""),
        )
        for j in completed[:2]
    )
    return active_rows, queue_rows, done_rows


_CONSOLE_SVC = "cloud-lab-console"
_SELF_RESTART_RE = re.compile(
    r'(sudo\s+)?systemctl\s+(restart|start)\s+cloud-lab-console'
)

def _defer_console_restart() -> None:
    """Restart the console service after a short delay so the HTTP response can be flushed first."""
    time.sleep(1.5)
    subprocess.run(["sudo", "systemctl", "restart", _CONSOLE_SVC],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _run_service_control(vm_name: str, service: str, action: str) -> tuple[int, str]:
    safe_svc = service if re.match(r"^cloud-lab-[a-z0-9_-]+$", service) else ""
    if not safe_svc: return 1, f"Disallowed service name: {service!r}"
    safe_action = action if action in ("start", "stop", "restart", "status") else ""
    if not safe_action: return 1, f"Disallowed action: {action!r}"
    cmd = f"sudo systemctl {safe_action} {safe_svc} 2>&1; sudo systemctl status {safe_svc} --no-pager 2>&1 | head -20"
    if vm_name == "management":
        if safe_svc == _CONSOLE_SVC and safe_action in ("restart", "start"):
            threading.Thread(target=_defer_console_restart, daemon=True).start()
            return 0, f"[{_CONSOLE_SVC}] restarting — page will reload in a few seconds"
        try:
            result = subprocess.run(["bash", "-c", cmd],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=15)
            return result.returncode, result.stdout or "(no output)"
        except Exception as exc: return 1, f"Error: {exc}"
    return 0, _ssh_run(vm_name, cmd, timeout=20)



# ═══ STANDARD UI ══════════════════════════════════════════════════════════════
# The classic dashboard renderer — default interface. Palettes, dark mode and
# the switch into LCARS mode live in its gear settings panel.

# Minimal server-rack SVG used as placeholder when no custom logo is configured.
_DEFAULT_LOGO = (
    "data:image/svg+xml;charset=utf-8,"
    "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 36 36' fill='none'"
    " stroke='rgba(255,255,255,.85)' stroke-width='2'"
    " stroke-linecap='round' stroke-linejoin='round'%3E"
    "%3Crect x='3' y='5' width='30' height='9' rx='2'/%3E"
    "%3Crect x='3' y='18' width='30' height='9' rx='2'/%3E"
    "%3Ccircle cx='29' cy='9.5' r='1.5' fill='%234ade80' stroke='none'/%3E"
    "%3Ccircle cx='29' cy='22.5' r='1.5' fill='%23f59e0b' stroke='none'/%3E"
    "%3C/svg%3E"
)


_ACT = 'class="active"'   # used in f-string nav links (backslashes not allowed in f-expr)


def _std_mgmt_tls_html() -> str:
    """Return HTML rows for TLS cert expiry and DuckDNS status (management card only)."""
    import subprocess as _sp, datetime as _dt
    rows = []
    cert_file: Path | None = None
    for cert_root in [
        Path.home() / ".local" / "share" / "caddy" / "certificates",
        Path("/var/lib/caddy/.local/share/caddy/certificates"),
        Path("/etc/caddy/certificates"),
    ]:
        try:
            root_exists = cert_root.exists()
        except PermissionError:
            root_exists = False
        if root_exists:
            try:
                for crt in sorted(cert_root.rglob("*.crt"), key=lambda p: p.stat().st_mtime, reverse=True):
                    cert_file = crt
                    break
            except PermissionError:
                pass
        if cert_file:
            break
    if cert_file:
        try:
            out  = _sp.check_output(["openssl", "x509", "-enddate", "-noout", "-in", str(cert_file)],
                                    text=True, stderr=_sp.DEVNULL, timeout=5).strip()
            date_str = out.split("=", 1)[1].strip()
            exp  = _dt.datetime.strptime(date_str, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=_dt.timezone.utc)
            days = (exp - _dt.datetime.now(_dt.timezone.utc)).days
            cls  = "warn-text" if days < 14 else ""
            rows.append(f'<p class="{cls}"><b>TLS cert:</b> expires in {days}d ({exp.strftime("%Y-%m-%d")})</p>')
        except Exception:
            rows.append('<p class="muted"><b>TLS cert:</b> unable to read</p>')
    else:
        rows.append('<p class="muted"><b>TLS cert:</b> not configured</p>')
    for log_path in [TOOLS_DIR / "logs" / "duckdns.log",
                     Path.home() / ".config" / "cloud-lab" / "duckdns.log"]:
        if log_path.exists():
            try:
                last = log_path.read_text(encoding="utf-8").strip().splitlines()[-1]
                rows.append(f'<p class="muted"><b>DuckDNS:</b> {html.escape(last[:80])}</p>')
            except Exception:
                pass
            break
    return "".join(rows)



def _quota_html() -> str:
    """One row per fleet VM: count · shape @ resources [state] name."""
    fleet   = load_json(TOOLS_DIR / "fleet.json") or {"vms": []}
    vm_defs = fleet.get("vms", [])
    if not vm_defs:
        return '<span class="muted" style="font-size:13px">fleet.json missing</span>'

    rows: list[str] = []
    for vm_def in vm_defs:
        name           = vm_def.get("name", "?")
        expected_shape = vm_def.get("shape", "")

        snap      = load_json(PROFILE_DIR / f"{name}.json") or {}
        inst      = snap.get("instance", {})
        state     = inst.get("lifecycle-state", "") if inst else ""
        shape     = inst.get("shape", expected_shape) if inst else expected_shape
        shape_cfg = inst.get("shape-config", {}) if inst else {}

        is_active = state in ("RUNNING", "PROVISIONING", "STARTING")
        is_a1     = "A1.Flex" in shape

        if "E2.1.Micro" in shape:
            ocpus, ram = 1, 1
        elif is_a1:
            if is_active:
                ocpus = int(shape_cfg.get("ocpus") or 2)
                ram   = int(shape_cfg.get("memory-in-gbs") or 12)
            else:
                prof  = load_json(TOOLS_DIR / "admin" / "profiles" / f"{name}.json") or {}
                ocpus = int(prof.get("ocpus", 2))
                ram   = int(prof.get("memory_in_gbs", 12))
        else:
            ocpus, ram = "?", "?"

        if not state:
            badge_cls, badge_txt = "badge-unknown", "NOT FOUND"
        elif is_active:
            badge_cls, badge_txt = "badge-running", state
        else:
            badge_cls, badge_txt = "badge-unknown", state

        rows.append(
            f'<div class="quota-vm-row">'
            f'<span class="quota-count">1</span>'
            f'<span class="quota-shape">{html.escape(shape or expected_shape)}</span>'
            f'<span class="quota-at">@</span>'
            f'<span class="quota-res">{ocpus} OCPU &middot; {ram} GB RAM</span>'
            f'<span class="badge {badge_cls}" style="font-size:11px;padding:2px 7px">'
            f'{html.escape(badge_txt)}</span>'
            f'<span class="quota-vm-name">{html.escape(name)}</span>'
            f'</div>'
        )
    return "\n".join(rows)



def _std_svc_row(name: str, svc: str, label: str, note: str = "") -> str:
    vm_h  = html.escape(name)
    svc_h = html.escape(svc)
    lbl_h = html.escape(label)
    note_h = f'<div class="svc-note">{html.escape(note)}</div>' if note else ""
    if not svc:
        return f'<tr><td>{lbl_h}{note_h}</td><td></td></tr>'
    return (
        f'<tr>'
        f'<td><a class="svc-chip" href="/logs?vm={vm_h}&service={svc_h}">{lbl_h}</a>{note_h}</td>'
        f'<td>'
        f'<button class="svc-ctl" title="restart" data-vm="{vm_h}" data-svc="{svc_h}"'
        f' onclick="svcCtl(this.dataset.vm,this.dataset.svc,&apos;restart&apos;)">&#x21BA;</button>'
        f'<button class="svc-ctl stop" title="stop" data-vm="{vm_h}" data-svc="{svc_h}"'
        f' onclick="svcCtl(this.dataset.vm,this.dataset.svc,&apos;stop&apos;)">&#x25A0;</button>'
        f'<button class="svc-ctl" title="start" data-vm="{vm_h}" data-svc="{svc_h}"'
        f' onclick="svcCtl(this.dataset.vm,this.dataset.svc,&apos;start&apos;)">&#x25BA;</button>'
        f'</td>'
        f'</tr>'
    )


def _std_plain_work_row(label: str, note: str = "", status: str = "") -> str:
    status_h = f'<span class="mini-badge {html.escape(status.lower())}">{html.escape(status)}</span>' if status else ""
    note_h = f'<div class="svc-note">{html.escape(note)}</div>' if note else ""
    return f'<tr><td>{html.escape(label)}{note_h}</td><td>{status_h}</td></tr>'


def _std_work_section(title: str, rows: str) -> str:
    if not rows:
        rows = '<tr><td colspan="2" class="empty-row">None right now.</td></tr>'
    return (
        f'<div class="svc-section">'
        f'<div class="svc-label">{html.escape(title)}</div>'
        f'<table class="svc-table"><tbody>{rows}</tbody></table>'
        f'</div>'
    )



STD_PALETTE_CSS = """
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

STD_CSS = STD_PALETTE_CSS + """
*, *::before, *::after { box-sizing: border-box; }
body { font-family: system-ui,-apple-system,sans-serif; margin: 0;
       background: var(--c-bg); color: var(--c-text);
       transition: background .15s, color .15s; }
a { color: var(--c-primary); }  a:hover { color: var(--c-primary-lt); }

/* topbar */
.topbar { background: var(--c-primary); color: #fff; padding: 0 20px;
          display: flex; align-items: center; justify-content: space-between;
          height: 52px; gap: 12px; }
.topbar-left  { display: flex; align-items: center; gap: 10px; padding-left: 10px; }
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
                     { background: rgba(0,0,0,.18); color: #fff; }
.topbar-nav a.active, .topbar-nav a.active:hover { background: rgba(0,0,0,.35); color: #fff; }
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
.settings-row { display: flex; align-items: center; padding: 4px 0 12px; font-size: 13px; }
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
  .topbar-left { padding-left: 0; }
  .topbar-logo { height: 36px; }
  .topbar-nav a[href="/export"] { display: none; }
  .topbar-nav a, .topbar-nav button { padding: 5px 7px; font-size: 13px; }
}

/* workload sections on VM cards */
.svc-section { margin-top: 12px; border-top: 1px solid var(--c-border); padding-top: 10px; }
.svc-label   { font-size: 11px; font-weight: 600; color: var(--c-muted); text-transform: uppercase;
               letter-spacing: .5px; margin-bottom: 6px; }
.svc-table   { width: 100%; border-collapse: collapse; font-size: 12px; }
.svc-table td { padding: 4px 6px; border-top: 1px solid var(--c-border); vertical-align: middle; }
.svc-table tr:first-child td { border-top: none; }
.svc-table td:last-child { text-align: right; white-space: nowrap; }
.svc-chip    { font-size: 11px; padding: 3px 9px; border-radius: 999px; text-decoration: none;
               background: var(--c-bg); border: 1px solid var(--c-border); color: var(--c-text);
               transition: background .12s; }
.svc-chip:hover { background: var(--c-accent); color: #1a1a1a; border-color: var(--c-accent); }
.svc-note { margin-top: 2px; color: var(--c-muted); font-size: 11px; line-height: 1.35; }
.svc-ctl { font-size: 10px; padding: 1px 5px; border-radius: 4px; border: 1px solid var(--c-border);
           background: var(--c-bg); color: var(--c-muted); cursor: pointer; line-height: 1.5;
           transition: background .12s, color .12s; }
.svc-ctl:hover { background: var(--c-primary); color: #fff; border-color: var(--c-primary); }
.svc-ctl.stop:hover { background: #dc2626; border-color: #dc2626; }
.mini-badge { display: inline-block; border-radius: 999px; padding: 1px 7px; font-size: 10px;
              font-weight: 700; text-transform: uppercase; letter-spacing: .25px;
              background: var(--c-border); color: var(--c-text); }
.mini-badge.running, .mini-badge.active { background: #d97706; color: #fff; }
.mini-badge.pending { background: #6b7280; color: #fff; }
.mini-badge.done { background: #16a34a; color: #fff; }
.mini-badge.failed { background: #dc2626; color: #fff; }
.empty-row { color: var(--c-muted); font-size: 12px; padding: 2px 6px; }

/* queue page */
.q-table { width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 8px; }
.q-table th { text-align: left; padding: 6px 10px; border-bottom: 2px solid var(--c-border);
              font-size: 11px; color: var(--c-muted); text-transform: uppercase; }
.q-table td { padding: 6px 10px; border-bottom: 1px solid var(--c-border); vertical-align: top; }
.q-table tr:hover td { background: var(--c-bg); }
.q-output   { font-family: monospace; font-size: 11px; max-height: 80px; overflow-y: auto;
              white-space: pre-wrap; color: var(--c-muted); }
.badge-pending  { background: #6b7280; color:#fff; }
.badge-running  { background: #d97706; color:#fff; }
.badge-done     { background: #16a34a; color:#fff; }
.badge-failed   { background: #dc2626; color:#fff; }

/* audit log page */
.audit-row { display: flex; gap: 10px; align-items: baseline; padding: 6px 0;
             border-bottom: 1px solid var(--c-border); font-size: 12px; }
.audit-ts   { color: var(--c-muted); white-space: nowrap; min-width: 120px; }
.audit-who  { font-weight: 600; min-width: 60px; }
.audit-act  { color: var(--c-primary); min-width: 120px; }
.audit-vm   { color: var(--c-muted); min-width: 80px; }
.audit-det  { flex: 1; color: var(--c-muted); font-family: monospace; }

/* quota display */
.quota-section  { background: var(--c-card); border: 1px solid var(--c-border);
                  border-radius: 10px; padding: 10px 16px; margin-top: 6px; }
.quota-vm-row   { display: flex; align-items: center; gap: 8px; padding: 6px 0;
                  border-bottom: 1px solid var(--c-border); font-size: 13px; }
.quota-vm-row:last-child { border-bottom: none; }
.quota-count    { color: var(--c-muted); min-width: 10px; }
.quota-shape    { font-family: monospace; font-size: 12px; min-width: 196px; }
.quota-at       { color: var(--c-muted); }
.quota-res      { min-width: 148px; }
.quota-vm-name  { color: var(--c-muted); font-size: 12px; margin-left: 4px; }

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

# localStorage keys use a generic 'fleet-' prefix.
STD_THEME_JS = """
(function() {
  var r = document.documentElement;
  var pal = localStorage.getItem('fleet-palette');
  if (pal) {
    try {
      var p = JSON.parse(pal);
      if (p.vars) {
        Object.keys(p.vars).forEach(function(k) { r.style.setProperty(k, p.vars[k]); });
        if (p.theme) r.setAttribute('data-theme', p.theme);
      }
    } catch(e) { localStorage.removeItem('fleet-palette'); }
  } else {
    var saved = localStorage.getItem('fleet-theme');
    var pref  = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    r.setAttribute('data-theme', saved || pref);
  }
  var t = r.getAttribute('data-theme');
  var icon = document.getElementById('theme-icon');
  if (icon) icon.textContent = t === 'dark' ? '☀' : '🌙';

  var logo = localStorage.getItem('fleet-logo');
  if (logo) {
    document.querySelectorAll('.topbar-logo,.login-logo').forEach(function(img) {
      img.src = logo; img.classList.add('visible');
    });
  }
})();

function toggleTheme() {
  var r = document.documentElement;
  r.style.cssText = '';
  localStorage.removeItem('fleet-palette');
  document.querySelectorAll('.palette-btn').forEach(function(b) { b.classList.remove('active'); });
  var t = r.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
  r.setAttribute('data-theme', t);
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
  var vars  = JSON.parse(btn.dataset.vars);
  var theme = btn.dataset.theme || 'light';
  var r = document.documentElement;
  r.style.cssText = '';
  Object.keys(vars).forEach(function(k) { r.style.setProperty(k, vars[k]); });
  r.setAttribute('data-theme', theme);
  localStorage.setItem('fleet-palette', JSON.stringify({vars: vars, theme: theme}));
  localStorage.setItem('fleet-theme', theme);
  document.querySelectorAll('.palette-btn').forEach(function(b) {
    b.classList.toggle('active', b === btn);
  });
  var icon = document.getElementById('theme-icon');
  if (icon) icon.textContent = theme === 'dark' ? '☀' : '🌙';
}

function applyCustomPalette() {
  var p  = document.getElementById('cp-primary').value;
  var pl = document.getElementById('cp-primary-lt').value;
  var pd = document.getElementById('cp-primary-dk').value;
  var a  = document.getElementById('cp-accent').value;
  var logo = (document.getElementById('cp-logo') || {value:''}).value.trim();
  var vars = {'--c-primary':p,'--c-primary-lt':pl,'--c-primary-dk':pd,'--c-accent':a};
  var r = document.documentElement;
  r.style.cssText = '';
  Object.keys(vars).forEach(function(k) { r.style.setProperty(k, vars[k]); });
  localStorage.setItem('fleet-palette', JSON.stringify({vars: vars, theme: null}));
  if (logo) {
    document.querySelectorAll('.topbar-logo,.login-logo').forEach(function(img) {
      img.src = logo; img.classList.add('visible');
    });
    localStorage.setItem('fleet-logo', logo);
  }
  document.querySelectorAll('.palette-btn').forEach(function(b) { b.classList.remove('active'); });
}

function setUiMode(mode) {
  document.cookie = 'fleet_ui_mode=' + mode + '; Path=/; SameSite=Strict; Max-Age=31536000';
  window.location.href = '/';
}

async function svcCtl(vm, svc, action) {
  if (!confirm(action + " " + svc + " on " + vm + "?")) return;
  var r;
  try {
    r = await fetch("/service-control", {method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify({vm: vm, service: svc, action: action})});
  } catch(e) { alert("Request failed: " + e); return; }
  var d = await r.json();
  alert((d.output || d.error || "Done").trim());
}

function copyText(text, btn) {
  navigator.clipboard.writeText(text).then(function() {
    var orig = btn.textContent;
    btn.textContent = 'Copied!';
    setTimeout(function() { btn.textContent = orig; }, 1500);
  }).catch(function() { prompt('Copy:', text); });
}
"""

# Six named presets + the Custom slot rendered separately in _std_topbar().
# Tuple: (name, btn_bg, theme, vars)
# vars sets ALL CSS custom properties — palette is a complete visual identity.
# theme is 'light' or 'dark'; applyPalette forces data-theme to match.
PALETTE_PRESETS = [
    ("Slate", "#1f2937", "light", {
        "--c-primary": "#374151", "--c-primary-lt": "#4b5563", "--c-primary-dk": "#1f2937",
        "--c-accent": "#f59e0b",
        "--c-bg": "#f4f6f8", "--c-card": "#ffffff", "--c-border": "#d1d5db",
        "--c-text": "#111827", "--c-muted": "#6b7280",
    }),
    ("Ocean", "#0f2547", "light", {
        "--c-primary": "#1a3d6e", "--c-primary-lt": "#2d6cb5", "--c-primary-dk": "#0f2547",
        "--c-accent": "#38bdf8",
        "--c-bg": "#f0f4f9", "--c-card": "#ffffff", "--c-border": "#c8d8ec",
        "--c-text": "#0f2030", "--c-muted": "#64748b",
    }),
    ("Forest", "#1b3f28", "light", {
        "--c-primary": "#285e39", "--c-primary-lt": "#3a7a50", "--c-primary-dk": "#1b3f28",
        "--c-accent": "#c5a028",
        "--c-bg": "#eef6f0", "--c-card": "#ffffff", "--c-border": "#c4deca",
        "--c-text": "#17202a", "--c-muted": "#64748b",
    }),
    ("Neon", "#010409", "dark", {
        "--c-primary": "#39ff14", "--c-primary-lt": "#7fff00", "--c-primary-dk": "#2bcc10",
        "--c-accent": "#ff00ff",
        "--c-bg": "#0d1117", "--c-card": "#1c2128", "--c-border": "#30363d",
        "--c-text": "#e6edf3", "--c-muted": "#8b949e",
    }),
    ("Earthy", "#4a2d1b", "light", {
        "--c-primary": "#6b4226", "--c-primary-lt": "#8a5530", "--c-primary-dk": "#4a2d1b",
        "--c-accent": "#d4882a",
        "--c-bg": "#fdf6ef", "--c-card": "#ffffff", "--c-border": "#e8d5c4",
        "--c-text": "#2c1810", "--c-muted": "#8b6e5a",
    }),
    ("Midnight", "#071d3e", "dark", {
        "--c-primary": "#1a4f8c", "--c-primary-lt": "#2563b0", "--c-primary-dk": "#0f3460",
        "--c-accent": "#e94560",
        "--c-bg": "#0a0f1c", "--c-card": "#0f1729", "--c-border": "#1e2d45",
        "--c-text": "#e2e8f0", "--c-muted": "#94a3b8",
    }),
]


def _std_head(title: str, auto_refresh: int = 0) -> str:
    refresh = f'<meta http-equiv="refresh" content="{auto_refresh}">' if auto_refresh else ""
    return (
        f'<!doctype html><html lang="en"><head>'
        f'<meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'{refresh}'
        f'<title>{html.escape(title)}</title>'
        f'<style>{STD_CSS}</style>'
        f'<script>{STD_THEME_JS}</script>'
        f'</head><body>'
    )


_PAGE_SUBTITLES: dict[str, str] = {
    "fleet":  "Fleet Management",
    "stats":  "VM Stats",
    "logs":   "Log Stream",
    "export": "Export",
}


def _std_topbar(active: str = "") -> str:
    nav_items = [
        ("Fleet",  "/",       "fleet"),
        ("Stats",  "/stats",  "stats"),
        ("Logs",   "/logs",   "logs"),
        ("Export", "/export", "export"),
    ]
    links = " ".join(
        f'<a href="{href}" {_ACT if active == key else ""}>{label}</a>'
        for label, href, key in nav_items
    )
    preset_btns = " ".join(
        f'<button class="palette-btn"'
        f' style="background:{btn_bg}"'
        f' data-vars=\'{json.dumps(pvars)}\''
        f' data-theme="{theme}"'
        f' onclick="applyPalette(this)">{name}</button>'
        for name, btn_bg, theme, pvars in PALETTE_PRESETS
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
        f'<a href="/" style="display:flex;align-items:center;gap:10px;text-decoration:none">'
        f'<img id="topbar-logo" class="topbar-logo visible" src="{_DEFAULT_LOGO}" alt="Fleet Logo">'
        f'<span class="fleet-name">{html.escape(_PAGE_SUBTITLES.get(active, FLEET_NAME))}</span>'
        f'</a>'
        f'</div>'
        f'<nav class="topbar-nav">{links}'
        f'<button class="theme-btn" title="Appearance" onclick="toggleSettings()">&#9881;</button>'
        f'<a href="/logout" class="sign-out">Sign out</a>'
        f'</nav></div>'
        f'<div id="settings-panel" class="settings-panel" hidden>'
        f'<div class="settings-row">'
        f'<span>Dark mode</span>'
        f'<button class="theme-btn" onclick="toggleTheme()" style="margin-left:auto">'
        f'<span id="theme-icon">&#127769;</span></button>'
        f'</div>'
        f'<h3>Interface</h3>'
        f'<div class="settings-row" style="gap:6px">'
        f'<button class="btn" style="flex:1;padding:7px" disabled>Standard</button>'
        f'<button class="btn" style="flex:1;padding:7px" onclick="setUiMode(&apos;lcars&apos;)">LCARS</button>'
        f'</div>'
        f'<h3>Color palette</h3>'
        f'<div class="palette-grid">{preset_btns}{custom_btn}</div>'
        f'{custom_panel}'
        f'</div>'
    )



def _std_vm_cards() -> str:
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
        snap_ago = fmt_ago(synced_at) if synced_at else "never"

        if name == "management":
            # Management is the heartbeat server — it never heartbeats itself.
            # Read uptime directly from /proc/uptime instead.
            try:
                secs = int(float(Path("/proc/uptime").read_text().split()[0]))
                d, rem = divmod(secs, 86400)
                h, rem = divmod(rem, 3600)
                m = rem // 60
                uptime = f"{d}d {h}h {m}m" if d else f"{h}h {m}m"
            except Exception:
                uptime = "—"
            hb_ago = ""   # management doesn't heartbeat to itself
        else:
            uptime = html.escape(hb.get("uptime", "") or "—")
            hb_ago = fmt_ago(hb_time) if hb_time else '<span class="warn-text">not received yet</span>'

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

        workloads = _ROLE_WORKLOADS.get(role, {})
        queued_active, queue_rows, completed_rows = _queue_sections(name, _std_plain_work_row)
        active_rows = queued_active + "".join(
            _std_svc_row(name, svc_id, svc_lbl, svc_note)
            for svc_id, svc_lbl, svc_note in workloads.get("active", [])
        )
        background_rows = "".join(
            _std_svc_row(name, svc_id, svc_lbl, svc_note)
            for svc_id, svc_lbl, svc_note in workloads.get("background", [])
        )
        scheduled_rows = "".join(
            _std_svc_row(name, svc_id, svc_lbl, svc_note)
            for svc_id, svc_lbl, svc_note in workloads.get("scheduled", [])
        )
        svc_html = (
            _std_work_section("Active work", active_rows)
            + _std_work_section("Queue", queue_rows)
            + _std_work_section("Scheduled tasks", scheduled_rows)
            + _std_work_section("Background services", background_rows)
            + _std_work_section("Completed", completed_rows)
        )

        cards.append(
            f'<div class="card">'
            f'<div class="card-header">'
            f'<span class="vm-name">{html.escape(name)}</span>{badge}'
            f'</div>'
            + (f'<p class="notes">{notes}</p>' if notes else "")
            + f'<p><b>Role:</b> {role_label}</p>'
            f'<p><b>Shape:</b> {shape}</p>'
            f'<p><b>Uptime:</b> {uptime}</p>'
            f'<p><b>Public IP:</b> {public_ip}</p>'
            f'<p><b>Private IP:</b> {private_ip}</p>'
            f'<p><b>OCI snapshot:</b> {html.escape(snap_ago)}</p>'
            + (_std_mgmt_tls_html() if name == 'management' else '')
            + (f'<p><b>Heartbeat:</b> {hb_ago}</p>' if hb_ago else "")
            + f'<div class="card-actions">{"".join(actions)}</div>'
            + svc_html
            + '</div>'
        )
    return "\n".join(cards)




# ── fleet page ────────────────────────────────────────────────────────────────

def std_fleet_page() -> bytes:
    cards  = _std_vm_cards()
    events = fleet_events_html()
    page = (
        _std_head(FLEET_NAME, auto_refresh=60)
        + _std_topbar("fleet")
        + '<div class="content">'
        + f'<div class="grid">{cards}</div>'
        + '<p class="section-title">Oracle Free Tier Quota</p>'
        + f'<div class="quota-section">{_quota_html()}</div>'
        + '<p class="section-title">Recent Fleet Events</p>'
        + f'<div>{events}</div>'
        + '<p style="margin:24px 0 0;font-size:13px;color:var(--c-muted);text-align:center">'
        + '<a href="/tools">Tools</a> &nbsp;&middot;&nbsp; <a href="/queue">Queue</a> &nbsp;&middot;&nbsp; '
        + '<a href="/audit">Audit log</a>'
        + '</p>'
        + '<footer>Auto-refreshes every 60s &middot; management VM</footer>'
        + '</div></body></html>'
    )
    return page.encode("utf-8")


def std_stats_page(vm_name: str, fleet_vms: list) -> bytes:
    title  = f"{FLEET_NAME} — {vm_name} stats"
    raw    = collect_local_stats() if vm_name == "management" else collect_remote_stats(vm_name)
    output = html.escape(raw)
    now    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    vm_links = " ".join(
        f'<a href="/stats?vm={html.escape(v)}" {_ACT if v == vm_name else ""}>{html.escape(v)}</a>'
        for v in fleet_vms
    )
    page = (
        _std_head(title)
        + _std_topbar("stats")
        + f'<div class="content">'
        + f'<div class="vmbar">{vm_links}</div>'
        + f'<p class="meta">Snapshot taken {now}</p>'
        + f'<pre>{output}</pre>'
        + '<div style="display:flex;gap:12px;align-items:center;margin-top:12px">'
        + f'<a class="btn" href="/stats?vm={html.escape(vm_name)}">Refresh</a>'
        + '<label><input type="checkbox" onchange="(function(cb){'
        + 'if(cb.checked){window._ar=setInterval(()=>location.reload(),10000)}'
        + 'else{clearInterval(window._ar)}})(this)"> Auto-refresh 10s</label>'
        + '</div>'
        + f'<p class="section-title">Scheduled Tasks (crontab)</p>'
        + f'<pre>{html.escape(_read_vm_crontab(vm_name))}</pre>'
        + '</div></body></html>'
    )
    return page.encode("utf-8")


# ── logs page ─────────────────────────────────────────────────────────────────

def std_logs_page(vm_name: str, service_name: str, fleet_vms: list) -> bytes:
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
        _std_head(title)
        + _std_topbar("logs")
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

def std_login_page(error: bool = False, locked: bool = False) -> bytes:
    if locked:
        err = '<p class="error-msg">Too many failed attempts. Try again in 15 minutes.</p>'
    elif error:
        err = '<p class="error-msg">Incorrect username or password.</p>'
    else:
        err = ""
    page = (
        _std_head(FLEET_NAME)
        + f'<div class="login-wrap"><div class="login-box">'
        + f'<img id="login-logo" class="login-logo" alt="Fleet Logo">'
        + f'<h1>{html.escape(FLEET_NAME)}</h1>'
        + '<p class="sub">Admin Dashboard</p>'
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

def std_export_page() -> bytes:
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
        _std_head(f"{FLEET_NAME} — Export")
        + _std_topbar("export")
        + f'<div class="content">'
        + f'<h2 style="margin:0 0 16px">Fleet connection details</h2>'
        + f'<pre class="export-pre">{content}</pre>'
        + '</div></body></html>'
    )
    return page.encode("utf-8")


# ── tools page ───────────────────────────────────────────────────────────────


def std_tools_page(preselect_vm: str = "") -> bytes:
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

    # Build preset cards — onclick references a JS object keyed by slug.
    # Scripts are stored in JS (not in HTML attributes) to avoid > < " escaping issues.
    preset_cards = "".join(
        f'<div class="payload-card" id="preset-{html.escape(slug)}"'
        f' data-slug="{html.escape(slug)}" onclick="selectPreset(this.dataset.slug)">'
        f'<p class="payload-title">{html.escape(label)}</p>'
        f'<p class="payload-desc">{html.escape(desc)}</p>'
        f'</div>'
        for slug, label, desc, _script in _PAYLOAD_PRESETS
    )

    # Serialize scripts as a JS object — json.dumps handles all escaping.
    scripts_js = "{" + ",".join(
        f'{json.dumps(slug)}:{json.dumps(script)}'
        for slug, _label, _desc, script in _PAYLOAD_PRESETS
    ) + "}"

    page = (
        _std_head(title)
        + _std_topbar("tools")
        + '<div class="content">'
        + '<h2 style="margin:0 0 4px">Admin Tools</h2>'
        + '<p style="color:var(--c-muted);font-size:14px;margin:0 0 4px">'
        + 'Click a preset to load its script. Select a target VM, then click Run. '
        + 'The script runs via SSH as bash on remote VMs, or locally on management.</p>'
        + '<h3 style="font-size:13px;font-weight:700;margin:16px 0 8px;color:var(--c-muted);text-transform:uppercase;letter-spacing:.5px">Presets</h3>'
        + '<div class="tools-grid">' + preset_cards
        + '<div class="payload-card" id="preset-custom" data-slug="custom" onclick="selectPreset(this.dataset.slug)">'
        + '<p class="payload-title">Custom script</p>'
        + '<p class="payload-desc">Write or paste your own bash script below.</p>'
        + '</div></div>'
        + '<h3 style="font-size:13px;font-weight:700;margin:20px 0 8px;color:var(--c-muted);text-transform:uppercase;letter-spacing:.5px">Script</h3>'
        + '<textarea id="payload-editor" class="script-editor"'
        + ' placeholder="#!/bin/bash&#10;# Click a preset above, or write your own script here.&#10;# Runs via SSH on the VM selected below."></textarea>'
        + '<div class="run-bar">'
        + '<select id="payload-vm" class="vm-select">' + vm_options + '</select>'
        + '<button class="btn" id="run-btn" onclick="runPayload()">&#9654; Run on VM</button>'
        + '<span id="payload-status" style="font-size:13px;color:var(--c-muted)"></span>'
        + '</div>'
        + '<div id="payload-header" style="display:none;justify-content:space-between;align-items:center;margin-top:16px;margin-bottom:4px">'
        + '<span style="font-size:11px;font-weight:700;color:var(--c-muted);text-transform:uppercase;letter-spacing:.5px">Output</span>'
        + '<button id="copy-btn" onclick="copyOutput()" style="font-size:12px;padding:3px 10px;'
        + 'border:1px solid var(--c-border);border-radius:4px;background:var(--c-bg);'
        + 'color:var(--c-text);cursor:pointer">&#x2398; Copy</button>'
        + '</div>'
        + '<pre id="payload-output" style="margin-top:0;display:none"></pre>'
        + "<script>"
        + f"var SCRIPTS={scripts_js};"
        + "function selectPreset(slug){"
        + "  document.querySelectorAll('.payload-card').forEach(function(c){c.classList.remove('selected');});"
        + "  var card=document.getElementById('preset-'+slug);"
        + "  if(card)card.classList.add('selected');"
        + "  var ed=document.getElementById('payload-editor');"
        + "  if(slug==='custom'){ed.value='';ed.focus();}"
        + "  else if(SCRIPTS[slug]){ed.value=SCRIPTS[slug];}"
        + "}"
        + "function runPayload(){"
        + "  var script=document.getElementById('payload-editor').value.trim();"
        + "  var vm=document.getElementById('payload-vm').value;"
        + "  var status=document.getElementById('payload-status');"
        + "  var btn=document.getElementById('run-btn');"
        + "  if(!script){status.textContent='Select a preset or write a script first.';return;}"
        + "  btn.disabled=true;btn.textContent='Running…';"
        + "  status.textContent='Running on '+vm+'…';"
        + "  var out=document.getElementById('payload-output');"
        + "  out.style.display='none';"
        + "  fetch('/run-payload',{method:'POST',"
        + "    headers:{'Content-Type':'application/json'},"
        + "    body:JSON.stringify({vm:vm,script:script})})"
        + "  .then(function(r){return r.json();})"
        + "  .then(function(d){"
        + "    btn.disabled=false;btn.textContent='▶ Run on VM';"
        + "    status.textContent=d.exit_code===0?'✓ Done on '+vm:'✗ Exit '+d.exit_code+' on '+vm;"
        + "    out.textContent=d.output||'(no output)';out.style.display='block';"
        + "    document.getElementById('payload-header').style.display='flex';})"
        + "  .catch(function(e){"
        + "    btn.disabled=false;btn.textContent='▶ Run on VM';"
        + "    status.textContent='Request failed: '+e.message;"
        + "    out.textContent=String(e);out.style.display='block';"
        + "    document.getElementById('payload-header').style.display='flex';});}"
        + "function copyOutput(){"
        + "  navigator.clipboard.writeText(document.getElementById('payload-output').textContent)"
        + "  .then(function(){"
        + "    var b=document.getElementById('copy-btn');b.textContent='✓ Copied';"
        + "    setTimeout(function(){b.textContent='⎘ Copy';},1500);});}"
        + "</script>"
        + '</div></body></html>'
    )
    return page.encode("utf-8")


# ── queue page ────────────────────────────────────────────────────────────────

def std_queue_page(fleet_vms: list) -> bytes:
    title = f"{FLEET_NAME} — Job Queue"
    sections = []
    for vm in fleet_vms:
        jobs = _read_vm_queue(vm)
        if not jobs:
            rows_html = '<p class="muted">No jobs.</p>'
        else:
            rows = []
            for j in jobs:
                st  = j.get("status", "?")
                out = html.escape((j.get("output") or "")[:300])
                rows.append(
                    f'<tr>'
                    f'<td><span class="badge badge-{html.escape(st)}">{html.escape(st)}</span></td>'
                    f'<td>{html.escape(j.get("label","?"))}</td>'
                    f'<td class="muted">{html.escape(str(j.get("priority","?")))}</td>'
                    f'<td class="muted">{html.escape(j.get("queued_at","")[:16])}</td>'
                    f'<td class="muted">{html.escape(j.get("completed_at","")[:16])}</td>'
                    f'<td><div class="q-output">{out}</div></td>'
                    f'</tr>'
                )
            rows_html = (
                '<table class="q-table"><thead><tr>'
                '<th>Status</th><th>Label</th><th>Pri</th>'
                '<th>Queued</th><th>Done</th><th>Output</th>'
                '</tr></thead><tbody>' + "".join(rows) + '</tbody></table>'
            )
        sections.append(f'<p class="section-title">{html.escape(vm)}</p>' + rows_html)
    page = (
        _std_head(title)
        + _std_topbar("queue")
        + '<div class="content">'
        + '<p style="font-size:13px;color:var(--c-muted);margin:0 0 16px">'
        + 'On-demand job queue &#8212; tasks submitted via this console or the <code>/enqueue</code> API. '
        + '&#8220;No jobs&#8221; is normal on a fresh deployment until a job is submitted. '
        + 'Background systemd services are not shown here &#8212; '
        + 'those appear as service chips on the <a href="/">Fleet</a> page.'
        + '</p>'
        + "".join(sections)
        + '</div></body></html>'
    )
    return page.encode("utf-8")


# ── audit log page ─────────────────────────────────────────────────────────────

def std_audit_page() -> bytes:
    title   = f"{FLEET_NAME} — Audit Log"
    entries: list[dict] = []
    if AUDIT_LOG.exists():
        try:
            lines = AUDIT_LOG.read_text(encoding="utf-8").strip().splitlines()
            for line in lines[-200:]:
                try: entries.append(json.loads(line))
                except Exception: pass
        except Exception:
            pass
    entries.reverse()
    if not entries:
        body = '<p class="muted">No audit entries yet.</p>'
    else:
        rows = []
        for e in entries:
            rows.append(
                f'<div class="audit-row">'
                f'<span class="audit-ts">{html.escape(e.get("ts","")[:16])}</span>'
                f'<span class="audit-who">{html.escape(e.get("ip","?"))}</span>'
                f'<span class="audit-act">{html.escape(e.get("action","?"))}</span>'
                f'<span class="audit-vm">{html.escape(e.get("vm","—"))}</span>'
                f'<span class="audit-det">{html.escape(e.get("details",""))}</span>'
                f'</div>'
            )
        body = "".join(rows)
    page = (
        _std_head(title)
        + _std_topbar("audit")
        + '<div class="content">'
        + '<p style="font-size:13px;color:var(--c-muted);margin:0 0 16px">'
        + 'Audit log &#8212; admin actions recorded by this console: script runs, service control, job submissions. '
        + 'Written to <code>~/cloud-lab/logs/audit.jsonl</code> on the management VM. '
        + 'Last 200 entries shown, newest first.'
        + '</p>' + body + '</div></body></html>'
    )
    return page.encode("utf-8")



# ── console JS ────────────────────────────────────────────────────────────────
# All styling comes from the vendored LCARS framework (TheLCARS.com Classic
# Theme) plus console.css supplements, both served from /static/lcars/.

CONSOLE_JS = """
function updateStardate() {
  var n = new Date();
  var y = n.getFullYear() - 1946;
  var start = new Date(n.getFullYear(), 0, 1);
  var day = Math.ceil((n - start) / 864e5);
  var el = document.getElementById('Stardate');
  if (el) el.textContent = y + String(Math.floor(day * 2.732)).padStart(3, '0') + '.' + n.getHours();
}
updateStardate();
setInterval(updateStardate, 60000);

async function svcCtl(vm, svc, action) {
  if (!confirm(action + " " + svc + " on " + vm + "?")) return;
  var r;
  try {
    r = await fetch("/service-control", {method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({vm: vm, service: svc, action: action})});
  } catch(e) { alert("Request failed: " + e); return; }
  var d = await r.json();
  alert((d.output || d.error || "Done").trim());
}

function copyText(text, btn) {
  navigator.clipboard.writeText(text).then(function() {
    var orig = btn.textContent;
    btn.textContent = 'COPIED';
    setTimeout(function() { btn.textContent = orig; }, 1500);
  }).catch(function() { prompt('Copy:', text); });
}

function applyBeepVolume() {
  var off = localStorage.getItem('lcars-beeps') === '0';
  document.querySelectorAll('audio').forEach(function(a) { a.volume = off ? 0 : 1; });
}
document.addEventListener('DOMContentLoaded', applyBeepVolume);
"""


_LOG_SERVICES = [
    ("cloud-lab-a1-lottery",   "Lottery"),
    ("cloud-lab-orchestrator", "Orchestrator"),
    ("cloud-lab-console",      "Console"),
    ("cloud-lab-heartbeat",    "Heartbeat"),
    ("cloud-lab-crosswatch",   "Crosswatch"),
    ("cloud-lab-update",       "Update"),
]

_ROLE_WORKLOADS: dict[str, dict[str, list[tuple[str, str, str]]]] = {
    "management": {
        "active": [
            ("cloud-lab-orchestrator", "Orchestrator", "Reconciles the expected fleet state."),
            ("cloud-lab-console",      "Console",      "Serves this admin dashboard."),
        ],
        "background": [
            ("cloud-lab-heartbeat",  "Heartbeat",  "Owner-facing fleet summary every 12h."),
            ("cloud-lab-crosswatch", "Crosswatch", "Peer VM health check every 6h."),
        ],
        "scheduled": [
            ("cloud-lab-update", "Auto-update", "Nightly git pull maintenance."),
        ],
    },
    "worker": {
        "active": [
            ("cloud-lab-a1-lottery", "A1 Lottery", "Retries A1 Flex capacity until laboratory is won."),
        ],
        "background": [
            ("cloud-lab-heartbeat",  "Heartbeat",  "Reports worker health to management every 4h."),
            ("cloud-lab-crosswatch", "Crosswatch", "Watches peers and can relaunch when needed."),
        ],
        "scheduled": [
            ("cloud-lab-update", "Auto-update", "Nightly git pull maintenance."),
        ],
    },
    "laboratory": {
        "active": [
            ("", "Ready for workload", "Install a payload or enqueue work for this VM."),
        ],
        "background": [
            ("cloud-lab-heartbeat", "Heartbeat", "Reports laboratory health to management every 4h."),
        ],
        "scheduled": [
            ("cloud-lab-update", "Auto-update", "Nightly git pull maintenance."),
        ],
    },
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
        "set -euo pipefail\ngit -C \"$CLOUD_LAB_DIR\" pull --ff-only\nsudo systemctl daemon-reload 2>/dev/null || true\nsudo systemctl restart 'cloud-lab-*'\necho 'Update complete.'",
    ),
    (
        "apt-maintenance",
        "APT maintenance",
        "Update package metadata and clean cached packages",
        "sudo apt-get update -qq && sudo apt-get autoclean -qq",
    ),
    (
        "check-logs",
        "Check logs",
        "Recent journal entries for all cloud-lab services",
        "sudo journalctl -u 'cloud-lab-*' -n 50 --no-pager --output=short-iso",
    ),
]


# ── LCARS chassis ─────────────────────────────────────────────────────────────
# Markup follows TheLCARS.com Classic Theme (V26). The framework CSS, Antonio
# fonts and beep audio are vendored under static/lcars/thelcars/; console.css
# adds the console-specific components (cards, tables, forms, readouts).

_LC = "/static/lcars/thelcars"

_NAV_ITEMS = [
    ("01", "FLEET", "/",      "fleet"),
    ("02", "STATS", "/stats", "stats"),
    ("03", "LOGS",  "/logs",  "logs"),
    ("04", "TOOLS", "/tools", "tools"),
]

_SIDE_PANELS = [
    ("panel-3", "05", "QUEUE",  "/queue"),
    ("panel-4", "06", "AUDIT",  "/audit"),
    ("panel-5", "07", "EXPORT", "/export"),
    ("panel-6", "08", "EXIT",   "/logout"),
]


def _lcars_head(title: str, auto_refresh: int = 0) -> str:
    refresh = f'<meta http-equiv="refresh" content="{auto_refresh}">' if auto_refresh else ""
    scale = getattr(_ui_ctx, "scale", 1.0)
    style = f' style="--ui-scale:{scale:g}"' if scale != 1.0 else ""
    return (
        f'<!doctype html><html lang="en"{style}><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">'
        f'{refresh}'
        f'<title>{html.escape(title)}</title>'
        f'<link rel="stylesheet" href="{_LC}/lcars-classic.css?v={STATIC_ASSET_VERSION}">'
        f'<link rel="stylesheet" href="/static/lcars/console.css?v={STATIC_ASSET_VERSION}">'
        '</head><body>'
    )


def _lcars_data_cascade() -> str:
    """Animated numeric cascade for the header — pure LCARS set dressing."""
    cols = []
    for _ in range(16):
        rows = "".join(
            f'<div class="dc-row-{r}">{random.randint(0, 10 ** random.randint(2, 7))}</div>'
            for r in (1, 1, 2, 3, 3, 4, 5, 6, 7)
        )
        cols.append(f'<div class="data-column">{rows}</div>')
    return '<div class="data-cascade-wrapper" id="default">' + "".join(cols) + '</div>'


def _t_frame(active: str, banner: str, content: str, fleet_h: str, footer: str) -> str:
    """TheLCARS standard 'sideways T' — header elbow + rail + content elbow."""
    nav_parts = []
    for num, label, href, key in _NAV_ITEMS:
        cur = ' class="nav-current"' if key == active else ""
        nav_parts.append(
            f'<button{cur} onclick="playSoundAndRedirect(\'audio2\',\'{href}\')">{num}-{label}</button>'
        )
    nav = "".join(nav_parts)
    side = "".join(
        f'<button class="{cls}" onclick="playSoundAndRedirect(\'audio2\',\'{href}\')">'
        f'<span class="hop">{num}-</span>{label}</button>'
        for cls, num, label, href in _SIDE_PANELS
    )
    return (
        '<div class="wrap">'
        '<div class="left-frame-top">'
        f'<button onclick="playSoundAndRedirect(\'audio2\',\'/\')" class="panel-1-button">{fleet_h}</button>'
        '<button onclick="playSoundAndRedirect(\'audio2\',\'/\')" class="panel-2">02<span class="hop">-262000</span></button>'
        '</div>'
        '<div class="right-frame-top">'
        f'<div class="banner">{html.escape(banner)} &#149; <span id="Stardate"></span></div>'
        '<div class="data-cascade-button-group">'
        f'{_lcars_data_cascade()}'
        f'<nav>{nav}</nav>'
        '</div>'
        '<div class="bar-panel first-bar-panel">'
        '<div class="bar-1"></div><div class="bar-2"></div><div class="bar-3"></div>'
        '<div class="bar-4"></div><div class="bar-5"></div>'
        '</div>'
        '</div>'
        '</div>'
        '<div class="wrap" id="gap">'
        '<div class="left-frame">'
        '<button onclick="topFunction(); playSound()" id="topBtn"><span class="hop">screen</span> top</button>'
        f'<div>{side}<div class="panel-spacer"></div></div>'
        '<div><button class="panel-7" onclick="playSoundAndRedirect(\'audio2\',\'/settings\')">'
        '<span class="hop">09-</span>SETUP</button></div>'
        '</div>'
        '<div class="right-frame">'
        '<div class="bar-panel">'
        '<div class="bar-6"></div><div class="bar-7"></div><div class="bar-8"></div>'
        '<div class="bar-9"></div><div class="bar-10"></div>'
        '</div>'
        f'<main>{content}</main>'
        f'{footer}'
        '</div>'
        '</div>'
    )


_C_RAIL_ITEMS = [
    ("FLEET",  "/",       "fleet",  "background-gold"),
    ("STATS",  "/stats",  "stats",  "background-bluey"),
    ("LOGS",   "/logs",   "logs",   "background-lilac"),
    ("EXPORT", "/export", "export", "background-ice"),
]


def _c_frame(active: str, banner: str, content: str, fleet_h: str, footer: str,
             auto_refresh: int) -> str:
    """Fully enclosed 'C' frame — top bar, left rail, bottom bar, all connected."""
    rail_parts = [
        '<button onclick="topFunction(); playSound()" id="topBtn"><span class="hop">screen</span> top</button>'
    ]
    for label, href, key, color in _C_RAIL_ITEMS:
        cur = " nav-current" if key == active else ""
        rail_parts.append(
            f'<button class="{color}{cur}" '
            f'onclick="playSoundAndRedirect(\'audio2\',\'{href}\')">{label}</button>'
        )
    rail_parts.append('<div class="rail-fill"></div>')
    rail_parts.append(
        '<button class="background-red" '
        'onclick="playSoundAndRedirect(\'audio2\',\'/logout\')">Sign out</button>'
    )
    rail = "".join(rail_parts)
    setup_cur = ' class="background-almond nav-current"' if active == "settings" \
                else ' class="background-almond"'
    refresh_label = f"Auto-refresh {auto_refresh}s" if auto_refresh else "LCARS 47.3"
    return (
        '<div class="wrap">'
        '<div class="cframe-elbow cframe-elbow--top"></div>'
        '<div class="cframe-right cframe-right--top">'
        '<div class="cframe-bar">'
        f'<div class="bar-fill">{html.escape(banner)}&nbsp;&#149;&nbsp;<span id="Stardate"></span></div>'
        '<div class="cframe-chip"></div>'
        '<div class="cframe-chip cframe-chip--red"></div>'
        f'<div class="cframe-cap">{fleet_h}</div>'
        '</div>'
        '</div>'
        '</div>'
        '<div class="wrap">'
        f'<div class="cframe-rail">{rail}</div>'
        '<div class="cframe-main">'
        f'<main>{content}</main>'
        f'{footer}'
        '</div>'
        '</div>'
        '<div class="wrap">'
        '<div class="cframe-elbow cframe-elbow--bottom">L4-7</div>'
        '<div class="cframe-right cframe-right--bottom">'
        '<div class="cframe-bar">'
        f'<div class="bar-fill">LCARS 47.3 &middot; {html.escape(active)}</div>'
        '<a class="background-gold" href="/tools">Tools</a>'
        '<a class="background-bluey" href="/queue">Queue</a>'
        '<a class="background-lilac" href="/audit">Audit log</a>'
        f'<a{setup_cur} href="/settings">Setup</a>'
        f'<div class="cframe-cap">{html.escape(refresh_label)}</div>'
        '</div>'
        '</div>'
        '</div>'
    )


def _page(active: str, banner: str, content: str, *,
          auto_refresh: int = 0, page_js: str = "") -> bytes:
    """Wrap page content in the LCARS chassis (T or C layout) and return HTML."""
    layout = getattr(_ui_ctx, "layout", "t")
    fleet_h = html.escape(FLEET_NAME.upper())
    footer = (
        '<footer>'
        f'{fleet_h} admin console &middot; runs on the management VM<br>'
        'LCARS Inspired Website Template by <a href="https://www.thelcars.com">TheLCARS.com</a><br>'
        'STAR TREK &#174; and related marks are trademarks of CBS Studios Inc. '
        'This private console is not affiliated with CBS Studios Inc. '
        'LCARS was designed by Michael Okuda.'
        '</footer>'
    )
    if layout == "c":
        frame = _c_frame(active, banner, content, fleet_h, footer, auto_refresh)
    else:
        frame = _t_frame(active, banner, content, fleet_h, footer)
    page = (
        _lcars_head(f"{FLEET_NAME} — {banner}", auto_refresh=auto_refresh)
        + '<section class="wrap-standard" id="column-3">'
        + frame
        + f'<script src="{_LC}/lcars.js?v={STATIC_ASSET_VERSION}"></script>'
        f'<script>{CONSOLE_JS}{page_js}</script>'
        '<div class="headtrim"></div>'
        '<div class="baseboard"></div>'
        f'<audio id="audio1" src="{_LC}/beep1.mp3" preload="auto"></audio>'
        f'<audio id="audio2" src="{_LC}/beep2.mp3" preload="auto"></audio>'
        f'<audio id="audio3" src="{_LC}/beep3.mp3" preload="auto"></audio>'
        f'<audio id="audio4" src="{_LC}/beep4.mp3" preload="auto"></audio>'
        '</section></body></html>'
    )
    return page.encode("utf-8")


def _lcars_pill(href: str, label: str, current: bool = False) -> str:
    cur = ' class="current"' if current else ""
    return f'<a{cur} href="{href}">{html.escape(label)}</a>'


def _lcars_text_bar(heading: str, level: str = "h3", the_end: bool = False) -> str:
    end = " the-end" if the_end else ""
    return (
        f'<div class="lcars-text-bar{end}">'
        f'<{level}>{html.escape(heading)}</{level}>'
        f'</div>'
    )


# ── VM cards ──────────────────────────────────────────────────────────────────

_ROLE_CARD_CLASSES = {
    "management": "role-management",
    "worker":     "role-worker",
    "laboratory": "role-laboratory",
}


def _lcars_vm_cards() -> str:
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
        shape      = instance.get("shape") or vm.get("shape", "")
        public_ip  = profile.get("public_ip") or "—"
        private_ip = profile.get("private_ip") or "—"
        role       = vm.get("role", name)
        notes      = vm.get("notes", "")
        synced_at  = profile.get("synced_at", "")

        hb       = hbs.get(name, {})
        hb_time  = hb.get("received_at", "")
        snap_ago = fmt_ago(synced_at) if synced_at else "never"

        if name == "management":
            # Management is the heartbeat server — it never heartbeats itself.
            # Read uptime directly from /proc/uptime instead.
            try:
                secs = int(float(Path("/proc/uptime").read_text().split()[0]))
                d, rem = divmod(secs, 86400)
                h, rem = divmod(rem, 3600)
                m = rem // 60
                uptime = f"{d}d {h}h {m}m" if d else f"{h}h {m}m"
            except Exception:
                uptime = "—"
            hb_html = ""
        else:
            uptime = hb.get("uptime", "") or "—"
            hb_val = (html.escape(fmt_ago(hb_time)) if hb_time
                      else '<span class="warn-text">not received yet</span>')
            hb_html = f'<div class="vm-field"><b>Heartbeat</b><span>{hb_val}</span></div>'

        fields = "".join(
            f'<div class="vm-field"><b>{html.escape(k)}</b><span>{html.escape(str(v))}</span></div>'
            for k, v in [
                ("Role", role), ("Shape", shape), ("Uptime", uptime),
                ("Public IP", public_ip), ("Private IP", private_ip),
                ("OCI snapshot", snap_ago),
            ]
        )
        fields += _lcars_mgmt_tls_html() if name == "management" else ""
        fields += hb_html

        ssh_cmd = f"ssh -i ~/.ssh/fleet.key {ssh_user}@{public_ip}"
        actions = [
            f'<a class="button-bluey" href="/stats?vm={html.escape(name)}">Live stats</a>',
            f'<a class="button-lilac" href="/logs?vm={html.escape(name)}">Logs</a>',
            f'<a class="button-almond" href="/tools?vm={html.escape(name)}">Tools</a>',
        ]
        if public_ip != "—":
            actions.append(
                f'<button class="button-sky" onclick="copyText({json.dumps(ssh_cmd)},this)">Copy SSH</button>'
            )
        if name == "worker":
            actions.append(
                '<a class="button-golden-orange" href="/logs?vm=worker&service=cloud-lab-a1-lottery">Lottery logs</a>'
            )

        workloads = _ROLE_WORKLOADS.get(role, {})
        queued_active, queue_rows, completed_rows = _queue_sections(name, _lcars_plain_work_row)
        active_rows = queued_active + "".join(
            _lcars_svc_row(name, svc_id, svc_lbl, svc_note)
            for svc_id, svc_lbl, svc_note in workloads.get("active", [])
        )
        background_rows = "".join(
            _lcars_svc_row(name, svc_id, svc_lbl, svc_note)
            for svc_id, svc_lbl, svc_note in workloads.get("background", [])
        )
        scheduled_rows = "".join(
            _lcars_svc_row(name, svc_id, svc_lbl, svc_note)
            for svc_id, svc_lbl, svc_note in workloads.get("scheduled", [])
        )
        sections = (
            _lcars_work_section("Active work", active_rows)
            + _lcars_work_section("Queue", queue_rows)
            + _lcars_work_section("Scheduled tasks", scheduled_rows)
            + _lcars_work_section("Background services", background_rows)
            + _lcars_work_section("Completed", completed_rows)
        )

        role_cls = _ROLE_CARD_CLASSES.get(role, "role-default")
        cards.append(
            '<div class="vm-card">'
            f'<div class="vm-card-head {role_cls}">'
            f'<span class="vm-state">{html.escape(state)}</span>'
            f'<strong>{html.escape(name)}</strong>'
            '</div>'
            + (f'<p class="vm-notes">{html.escape(notes)}</p>' if notes else "")
            + f'<div class="vm-fields">{fields}</div>'
            f'<div class="btn-row">{"".join(actions)}</div>'
            f'{sections}'
            '</div>'
        )
    return "\n".join(cards) if cards else '<p class="muted">No VMs defined in fleet.json.</p>'


def _lcars_fleet_body() -> str:
    fleet    = load_json(TOOLS_DIR / "fleet.json") or {"vms": []}
    profiles = [load_json(PROFILE_DIR / f"{v.get('name', '')}.json") or {}
                for v in fleet.get("vms", [])]
    total    = len(fleet.get("vms", []))
    running  = sum(1 for p in profiles
                   if p.get("instance", {}).get("lifecycle-state") == "RUNNING")
    down     = sum(1 for p in profiles
                   if p.get("instance", {}).get("lifecycle-state") in ("TERMINATED", "TERMINATING"))
    awaiting = max(0, total - running - down)
    return (
        '<h1>Fleet Management</h1>'
        '<div class="stat-row">'
        f'<div class="stat-block background-golden-orange"><small>Instances</small><strong>{total}</strong></div>'
        f'<div class="stat-block background-lima-bean"><small>Online</small><strong>{running}</strong></div>'
        f'<div class="stat-block background-sunglow"><small>Awaiting</small><strong>{awaiting}</strong></div>'
        f'<div class="stat-block background-tomato"><small>Down</small><strong>{down}</strong></div>'
        f'{_quota_bars()}'
        '</div>'
        f'<div class="vm-grid">{_lcars_vm_cards()}</div>'
        + _lcars_text_bar("Fleet Events")
        + fleet_events_html()
        + '<p class="meta-line">Auto-refreshes every 60 seconds</p>'
    )



# ── stats page ────────────────────────────────────────────────────────────────

def lcars_stats_page(vm_name: str, fleet_vms: list) -> bytes:
    raw    = collect_local_stats() if vm_name == "management" else collect_remote_stats(vm_name)
    output = html.escape(raw)
    now    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    vm_links = "".join(
        _lcars_pill(f"/stats?vm={html.escape(v)}", v, v == vm_name)
        for v in fleet_vms
    )
    body = (
        f'<h1>{html.escape(vm_name)} telemetry</h1>'
        f'<div class="btn-row">{vm_links}</div>'
        f'<p class="meta-line">Snapshot taken {now}</p>'
        + _lcars_text_bar("System Snapshot")
        + f'<pre class="readout">{output}</pre>'
        '<div class="btn-row">'
        f'<a class="button-golden-orange" href="/stats?vm={html.escape(vm_name)}">Refresh</a>'
        '<button class="button-bluey" onclick="(function(b){'
        "if(window._ar){clearInterval(window._ar);window._ar=null;b.textContent='AUTO-REFRESH 10S';}"
        "else{window._ar=setInterval(function(){location.reload()},10000);b.textContent='AUTO ON';}"
        '})(this)">Auto-refresh 10s</button>'
        '</div>'
        + _lcars_text_bar("Scheduled Tasks")
        + f'<pre class="readout">{html.escape(_read_vm_crontab(vm_name))}</pre>'
    )
    return _page("stats", f"Stats / {vm_name}", body)


# ── logs page ─────────────────────────────────────────────────────────────────

def lcars_logs_page(vm_name: str, service_name: str, fleet_vms: list) -> bytes:
    raw    = (collect_local_logs(service_name) if vm_name == "management"
              else collect_remote_logs(vm_name, service_name))
    output = html.escape(raw)
    now    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    vm_links = "".join(
        _lcars_pill(f"/logs?vm={html.escape(v)}&service={html.escape(service_name)}", v, v == vm_name)
        for v in fleet_vms
    )
    svc_links = "".join(
        _lcars_pill(f"/logs?vm={html.escape(vm_name)}&service={html.escape(svc)}", label, svc == service_name)
        for svc, label in _LOG_SERVICES
    )
    body = (
        f'<h1>{html.escape(vm_name)} logs</h1>'
        f'<div class="btn-row">{vm_links}</div>'
        f'<div class="btn-row">{svc_links}</div>'
        f'<p class="meta-line">{html.escape(service_name)} &middot; fetched {now}</p>'
        + _lcars_text_bar("Log Stream")
        + f'<pre class="readout">{output}</pre>'
        '<div class="btn-row">'
        f'<a class="button-golden-orange" href="/logs?vm={html.escape(vm_name)}&service={html.escape(service_name)}">Refresh</a>'
        '<button class="button-bluey" onclick="(function(b){'
        "if(window._ar){clearInterval(window._ar);window._ar=null;b.textContent='AUTO-REFRESH 15S';}"
        "else{window._ar=setInterval(function(){location.reload()},15000);b.textContent='AUTO ON';}"
        '})(this)">Auto-refresh 15s</button>'
        '</div>'
    )
    return _page("logs", f"Logs / {vm_name}", body)


# ── login page ────────────────────────────────────────────────────────────────

def lcars_login_page(error: bool = False, locked: bool = False) -> bytes:
    if locked:
        err = '<p class="error-msg">Too many failed attempts. Try again in 15 minutes.</p>'
    elif error:
        err = '<p class="error-msg">Incorrect username or password.</p>'
    else:
        err = ""
    body = (
        '<div class="login-panel lcars-form">'
        f'<h2>{html.escape(FLEET_NAME)}</h2>'
        '<p class="meta-line">Admin Dashboard &middot; authorization required</p>'
        f'{err}'
        '<form method="POST" action="/login">'
        '<label for="u">Username</label>'
        '<input id="u" type="text" name="username" autocomplete="username" autofocus>'
        '<label for="p">Password</label>'
        '<input id="p" type="password" name="password" autocomplete="current-password">'
        '<div class="buttons">'
        '<button type="submit" class="button-golden-orange">Sign in</button>'
        '</div>'
        '</form>'
        '</div>'
    )
    return _page("login", "Access Control", body)


# ── export page ───────────────────────────────────────────────────────────────

def lcars_export_page() -> bytes:
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
    ssh_key  = env.get("OCI_SSH_PRIVATE_KEY_PATH", "~/.ssh/fleet.key")
    ssh_user = env.get("OCI_SSH_USER", "ubuntu")
    lines += [
        "# SSH",
        f"OCI_SSH_USER={ssh_user}",
        f"OCI_SSH_PRIVATE_KEY_PATH={ssh_key}",
        f"# SSH example: ssh -i {ssh_key} {ssh_user}@<public-ip>",
    ]
    content = html.escape("\n".join(lines))
    body = (
        '<h1>Fleet connection details</h1>'
        '<p class="meta-line">Copy/paste endpoint and SSH values</p>'
        f'<pre class="readout wrap-lines">{content}</pre>'
    )
    return _page("export", "Export", body)


# ── tools page ───────────────────────────────────────────────────────────────

def run_payload_on_vm(vm_name: str, script: str) -> tuple[bool, str]:
    # Inject CLOUD_LAB_DIR so scripts never need to hardcode the repo path.
    # Management: the console's actual TOOLS_DIR. Other VMs: cloud-init standard.
    if vm_name == "management":
        header = f"export CLOUD_LAB_DIR={shlex.quote(str(TOOLS_DIR))}\n"
    else:
        header = 'export CLOUD_LAB_DIR="$HOME/cloud-lab"\n'
    script = header + script

    if vm_name == "management":
        self_restart = bool(_SELF_RESTART_RE.search(script))
        run_script = (
            _SELF_RESTART_RE.sub(
                'echo "[cloud-lab-console will restart after this script completes]"',
                script,
            ) if self_restart else script
        )
        try:
            result = subprocess.run(
                ["bash", "-s"],
                input=run_script,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, timeout=30,
            )
            output = result.stdout or "(no output)"
            if self_restart:
                output += "\n\n[Restarting cloud-lab-console — page will reload in a few seconds]"
                threading.Thread(target=_defer_console_restart, daemon=True).start()
            return True, output
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


_TOOL_CARD_COLORS = [
    "background-bluey",
    "background-lilac",
    "background-orange",
    "background-golden-orange",
    "background-sky",
]

_TOOLS_JS_TEMPLATE = """
var SCRIPTS = %(scripts)s;
function selectPreset(slug) {
  document.querySelectorAll('.tool-card').forEach(function(c) { c.classList.remove('selected'); });
  var card = document.getElementById('preset-' + slug);
  if (card) card.classList.add('selected');
  var ed = document.getElementById('payload-editor');
  if (slug === 'custom') { ed.value = ''; ed.focus(); }
  else if (SCRIPTS[slug]) { ed.value = SCRIPTS[slug]; }
}
function runPayload() {
  var script = document.getElementById('payload-editor').value.trim();
  var vm     = document.getElementById('payload-vm').value;
  var status = document.getElementById('payload-status');
  var btn    = document.getElementById('run-btn');
  if (!script) { status.textContent = 'SELECT A PRESET OR WRITE A SCRIPT FIRST'; return; }
  btn.disabled = true; btn.textContent = 'RUNNING';
  status.textContent = 'RUNNING ON ' + vm.toUpperCase();
  var out = document.getElementById('payload-output');
  out.style.display = 'none';
  fetch('/run-payload', {method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({vm: vm, script: script})})
  .then(function(r) { return r.json(); })
  .then(function(d) {
    btn.disabled = false; btn.textContent = 'RUN ON VM';
    status.textContent = d.ok ? 'DONE ON ' + vm.toUpperCase() : 'FAILED ON ' + vm.toUpperCase();
    out.textContent = d.output || '(no output)'; out.style.display = 'block';
    document.getElementById('payload-header').style.display = 'flex';
  })
  .catch(function(e) {
    btn.disabled = false; btn.textContent = 'RUN ON VM';
    status.textContent = 'REQUEST FAILED: ' + e.message;
    out.textContent = String(e); out.style.display = 'block';
    document.getElementById('payload-header').style.display = 'flex';
  });
}
function copyOutput() {
  navigator.clipboard.writeText(document.getElementById('payload-output').textContent)
  .then(function() {
    var b = document.getElementById('copy-btn'); b.textContent = 'COPIED';
    setTimeout(function() { b.textContent = 'COPY'; }, 1500);
  });
}
"""


def lcars_tools_page(preselect_vm: str = "") -> bytes:
    fleet = load_json(TOOLS_DIR / "fleet.json") or {"vms": []}
    vms   = [v.get("name") for v in fleet.get("vms", []) if v.get("name")]
    if not vms:
        vms = ["management"]
    preselect = preselect_vm if preselect_vm in vms else vms[0]

    vm_options = "".join(
        f'<option value="{html.escape(v)}"{" selected" if v == preselect else ""}>{html.escape(v)}</option>'
        for v in vms
    )
    preset_cards = "".join(
        f'<div class="tool-card {color}" id="preset-{html.escape(slug)}"'
        f' data-slug="{html.escape(slug)}" onclick="selectPreset(this.dataset.slug)">'
        f'<span class="tool-title">{html.escape(label)}</span>'
        f'<span class="tool-desc">{html.escape(desc)}</span>'
        f'</div>'
        for (slug, label, desc, _script), color in zip(
            _PAYLOAD_PRESETS,
            _TOOL_CARD_COLORS * (len(_PAYLOAD_PRESETS) // len(_TOOL_CARD_COLORS) + 1),
        )
    )
    scripts_js = "{" + ",".join(
        f'{json.dumps(slug)}:{json.dumps(script)}'
        for slug, _label, _desc, script in _PAYLOAD_PRESETS
    ) + "}"

    body = (
        '<h1>Admin Tools</h1>'
        '<p>Click a preset to load its script. Select a target VM, then click Run. '
        'The script runs via SSH as bash on remote VMs, or locally on management.</p>'
        + _lcars_text_bar("Presets")
        + f'<div class="tool-grid">{preset_cards}'
        '<div class="tool-card background-tomato" id="preset-custom" data-slug="custom"'
        ' onclick="selectPreset(this.dataset.slug)">'
        '<span class="tool-title">Custom script</span>'
        '<span class="tool-desc">Write or paste your own bash script below.</span>'
        '</div></div>'
        + _lcars_text_bar("Script")
        + '<textarea id="payload-editor" class="script-editor"'
        ' placeholder="#!/bin/bash&#10;# Click a preset above, or write your own script here.&#10;'
        '# Runs via SSH on the VM selected below."></textarea>'
        '<div class="run-bar">'
        f'<select id="payload-vm" class="vm-select">{vm_options}</select>'
        '<div class="btn-row" style="margin:0">'
        '<button class="button-golden-orange" id="run-btn" onclick="runPayload()">Run on VM</button>'
        '</div>'
        '<span id="payload-status" class="run-status"></span>'
        '</div>'
        '<div id="payload-header" class="run-bar" style="display:none">'
        '<span class="meta-line" style="margin:0">Output</span>'
        '<div class="btn-row" style="margin:0">'
        '<button class="button-sky" id="copy-btn" onclick="copyOutput()">Copy</button>'
        '</div>'
        '</div>'
        '<pre id="payload-output" class="readout wrap-lines" style="display:none;margin-top:.5rem"></pre>'
    )
    return _page("tools", "Admin Tools", body,
                 page_js=_TOOLS_JS_TEMPLATE % {"scripts": scripts_js})


# ── queue page ────────────────────────────────────────────────────────────────

def lcars_queue_page(fleet_vms: list) -> bytes:
    sections = []
    for vm in fleet_vms:
        jobs = _read_vm_queue(vm)
        if not jobs:
            rows_html = '<p class="muted">No jobs.</p>'
        else:
            rows = []
            for j in jobs:
                st  = j.get("status", "?")
                out = html.escape((j.get("output") or "")[:300])
                rows.append(
                    f'<tr>'
                    f'<td><span class="badge badge-{html.escape(st)}">{html.escape(st)}</span></td>'
                    f'<td>{html.escape(j.get("label", "?"))}</td>'
                    f'<td class="muted">{html.escape(str(j.get("priority", "?")))}</td>'
                    f'<td class="muted">{html.escape(j.get("queued_at", "")[:16])}</td>'
                    f'<td class="muted">{html.escape(j.get("completed_at", "")[:16])}</td>'
                    f'<td><div class="job-output">{out}</div></td>'
                    f'</tr>'
                )
            rows_html = (
                '<div class="table-scroll"><table class="lcars-table"><thead><tr>'
                '<th>Status</th><th>Label</th><th>Pri</th>'
                '<th>Queued</th><th>Done</th><th>Output</th>'
                '</tr></thead><tbody>' + "".join(rows) + '</tbody></table></div>'
            )
        sections.append(_lcars_text_bar(vm, level="h4") + rows_html)
    body = (
        '<h1>Job Queue</h1>'
        '<p>On-demand job queue &#8212; tasks submitted via this console or the '
        '<code>/enqueue</code> API. &#8220;No jobs&#8221; is normal on a fresh deployment. '
        'Background systemd services are not shown here &#8212; those appear on the '
        '<a href="/">Fleet</a> page.</p>'
        + "".join(sections)
    )
    return _page("queue", "Job Queue", body)


# ── audit log page ─────────────────────────────────────────────────────────────

def lcars_audit_page() -> bytes:
    entries: list[dict] = []
    if AUDIT_LOG.exists():
        try:
            lines = AUDIT_LOG.read_text(encoding="utf-8").strip().splitlines()
            for line in lines[-200:]:
                try: entries.append(json.loads(line))
                except Exception: pass
        except Exception:
            pass
    entries.reverse()
    if not entries:
        rows_html = '<p class="muted">No audit entries yet.</p>'
    else:
        rows = []
        for e in entries:
            rows.append(
                f'<div class="audit-row">'
                f'<span class="audit-ts">{html.escape(e.get("ts", "")[:16])}</span>'
                f'<span class="audit-who">{html.escape(e.get("ip", "?"))}</span>'
                f'<span class="audit-act">{html.escape(e.get("action", "?"))}</span>'
                f'<span class="audit-vm">{html.escape(e.get("vm", "—"))}</span>'
                f'<span class="audit-det">{html.escape(e.get("details", ""))}</span>'
                f'</div>'
            )
        rows_html = "".join(rows)
    body = (
        '<h1>Audit Log</h1>'
        '<p>Admin actions recorded by this console: script runs, service control, '
        'job submissions. Written to <code>~/cloud-lab/logs/audit.jsonl</code> on the '
        'management VM. Last 200 entries shown, newest first.</p>'
        + _lcars_text_bar("Action Trail")
        + rows_html
    )
    return _page("audit", "Audit Log", body)


# ── LCARS settings page ───────────────────────────────────────────────────────

_SETTINGS_JS = """
function setCookie(k, v) { document.cookie = k + '=' + v + '; Path=/; SameSite=Strict; Max-Age=31536000'; }
function setUiMode(m) { setCookie('fleet_ui_mode', m); window.location.href = '/'; }
function setLcarsLayout(l) { setCookie('lcars_layout', l); location.reload(); }
function uiScale(v) {
  document.documentElement.style.setProperty('--ui-scale', v / 100);
  setCookie('ui_scale', v);
  var el = document.getElementById('scale-val'); if (el) el.textContent = v + '%';
}
function resetScale() {
  uiScale(100);
  var s = document.getElementById('scale-slider'); if (s) s.value = 100;
}
function setBeeps(on) {
  localStorage.setItem('lcars-beeps', on ? '1' : '0');
  markBeeps(); applyBeepVolume();
}
function markBeeps() {
  var off = localStorage.getItem('lcars-beeps') === '0';
  document.getElementById('beeps-on').classList.toggle('current', !off);
  document.getElementById('beeps-off').classList.toggle('current', off);
}
markBeeps();
"""


def lcars_settings_page(layout: str, scale: float) -> bytes:
    pct = int(round(scale * 100))
    t_cur = ' class="current"' if layout != "c" else ""
    c_cur = ' class="current"' if layout == "c" else ""
    body = (
        '<h1>Console Setup</h1>'
        + _lcars_text_bar("Interface Mode")
        + '<div class="btn-row">'
        '<button class="current">LCARS</button>'
        '<button onclick="setUiMode(\'standard\')">Standard console</button>'
        '</div>'
        '<p class="meta-line">Standard mode has its own dark mode and color palettes '
        'under its gear icon &#8212; including the switch back to LCARS.</p>'
        + _lcars_text_bar("Frame Layout")
        + '<div class="btn-row">'
        f'<button{t_cur} onclick="setLcarsLayout(\'t\')">Sideways T</button>'
        f'<button{c_cur} onclick="setLcarsLayout(\'c\')">Full frame C</button>'
        '</div>'
        '<p class="meta-line">The C layout closes the frame with a bottom bar.</p>'
        + _lcars_text_bar("Interface Scale")
        + '<div class="run-bar">'
        f'<input id="scale-slider" type="range" min="70" max="140" step="5" value="{pct}"'
        ' oninput="uiScale(this.value)">'
        f'<span id="scale-val" class="run-status">{pct}%</span>'
        '<div class="btn-row" style="margin:0">'
        '<button class="button-bluey" onclick="resetScale()">Reset</button>'
        '</div>'
        '</div>'
        '<p class="meta-line">Applies live &middot; saved for this browser.</p>'
        + _lcars_text_bar("Audio")
        + '<div class="btn-row">'
        '<button id="beeps-on" onclick="setBeeps(1)">Beeps on</button>'
        '<button id="beeps-off" onclick="setBeeps(0)">Beeps off</button>'
        '</div>'
    )
    return _page("settings", "Console Setup", body, page_js=_SETTINGS_JS)


# ── HTTP handler ──────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def _read_body(self, limit: int) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return None
        if length < 0 or length > limit:
            return None
        return self.rfile.read(length)

    def _read_json_body(self, limit: int) -> dict | None:
        body = self._read_body(limit)
        if body is None:
            return None
        try:
            data = json.loads(body)
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def _ui_mode(self) -> str:
        """Read UI cookies; prime the per-request LCARS presentation state."""
        cookies = _parse_cookies(self.headers.get("Cookie", ""))
        try:
            scale = float(cookies.get("ui_scale", "100")) / 100.0
        except ValueError:
            scale = 1.0
        _ui_ctx.scale  = min(1.4, max(0.7, scale))
        _ui_ctx.layout = "c" if cookies.get("lcars_layout") == "c" else "t"
        mode = cookies.get("fleet_ui_mode", DEFAULT_UI_MODE)
        return "lcars" if mode == "lcars" else "standard"

    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip("/") or "/"
        qs     = parse_qs(parsed.query)
        lcars  = self._ui_mode() == "lcars"

        if path.startswith("/static/"):
            self._static_file(path)
            return

        if path == "/login":
            if DEV_MODE:
                self._redirect("/"); return
            self._html(200, lcars_login_page() if lcars else std_login_page()); return
        if path == "/logout":
            if DEV_MODE:
                self._redirect("/"); return
            self._redirect("/login",
                           f'{COOKIE_NAME}=; Max-Age=0; Path=/; HttpOnly; Secure; SameSite=Strict')
            return
        if path == "/health":
            body_out = json.dumps({"status": "ok", "ts": int(time.time())}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body_out)))
            self.end_headers(); self.wfile.write(body_out); return

        if not _is_authed(self):
            self._redirect("/login"); return

        if path == "/":
            if lcars:
                self._html(200, _page("fleet", "Fleet Management", _lcars_fleet_body(),
                                      auto_refresh=60))
            else:
                self._html(200, std_fleet_page())
            return

        if path == "/settings":
            if not lcars:
                self._redirect("/"); return
            self._html(200, lcars_settings_page(getattr(_ui_ctx, "layout", "t"),
                                                getattr(_ui_ctx, "scale", 1.0)))
            return

        if path == "/stats":
            fleet     = load_json(TOOLS_DIR / "fleet.json") or {"vms": []}
            fleet_vms = [v.get("name") for v in fleet.get("vms", []) if v.get("name")]
            vm_name   = qs.get("vm", ["management"])[0]
            if vm_name not in fleet_vms:
                vm_name = "management"
            self._html(200, (lcars_stats_page if lcars else std_stats_page)(vm_name, fleet_vms)); return

        if path == "/logs":
            fleet       = load_json(TOOLS_DIR / "fleet.json") or {"vms": []}
            fleet_vms   = [v.get("name") for v in fleet.get("vms", []) if v.get("name")]
            vm_name     = qs.get("vm", ["worker"])[0]
            service_raw = qs.get("service", ["cloud-lab-a1-lottery"])[0]
            valid_svcs  = {s for s, _ in _LOG_SERVICES}
            service     = service_raw if service_raw in valid_svcs else "cloud-lab-a1-lottery"
            if vm_name not in fleet_vms:
                vm_name = fleet_vms[0] if fleet_vms else "management"
            self._html(200, (lcars_logs_page if lcars else std_logs_page)(vm_name, service, fleet_vms)); return

        if path == "/export":
            self._html(200, lcars_export_page() if lcars else std_export_page()); return

        if path == "/tools":
            preselect = qs.get("vm", [""])[0]
            self._html(200, (lcars_tools_page if lcars else std_tools_page)(preselect)); return

        if path == "/queue":
            fleet     = load_json(TOOLS_DIR / "fleet.json") or {"vms": []}
            names     = [v.get("name") for v in fleet.get("vms", []) if v.get("name")]
            self._html(200, (lcars_queue_page if lcars else std_queue_page)(names)); return

        if path == "/audit":
            self._html(200, lcars_audit_page() if lcars else std_audit_page()); return

        if path == "/health":
            # Lightweight liveness probe for UptimeRobot / external monitors. No auth.
            body_out = json.dumps({"status": "ok", "ts": int(time.time())}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body_out)))
            self.end_headers()
            self.wfile.write(body_out)
            return

        self._html(404, b"Not found")

    def do_POST(self):
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip("/") or "/"
        login_pg = lcars_login_page if self._ui_mode() == "lcars" else std_login_page

        if path == "/run-payload":
            if not _is_authed(self):
                self._json(403, {"ok": False, "output": "Not authenticated."}); return
            data = self._read_json_body(MAX_API_BODY)
            if data is None:
                self._json(400, {"ok": False, "output": "Bad request."}); return
            vm     = str(data.get("vm", "management"))[:64]
            script = str(data.get("script", ""))[:8192]
            fleet     = load_json(TOOLS_DIR / "fleet.json") or {"vms": []}
            fleet_vms = {v.get("name") for v in fleet.get("vms", []) if v.get("name")}
            fleet_vms.add("management")
            if vm not in fleet_vms:
                self._json(400, {"ok": False, "output": f"Unknown VM: {vm}"}); return
            _write_audit("run-payload", vm, script[:120], self)
            ok, output = run_payload_on_vm(vm, script)
            self._json(200, {"ok": ok, "output": output}); return

        if path == "/service-control":
            if not _is_authed(self):
                self._json(403, {"ok": False, "output": "Not authenticated."}); return
            data = self._read_json_body(MAX_API_BODY)
            if data is None:
                self._json(400, {"ok": False, "output": "Bad request."}); return
            vm      = str(data.get("vm", ""))[:64]
            service = str(data.get("service", ""))[:64]
            action  = str(data.get("action", ""))[:16]
            fleet     = load_json(TOOLS_DIR / "fleet.json") or {"vms": []}
            fleet_vms = {v.get("name") for v in fleet.get("vms", []) if v.get("name")}
            fleet_vms.add("management")
            if vm not in fleet_vms:
                self._json(400, {"error": "unknown vm"}); return
            _write_audit("service-control", vm, f"{action} {service}", self)
            ec, out = _run_service_control(vm, service, action)
            self._json(200, {"exit_code": ec, "output": out}); return

        if path == "/enqueue":
            if not _is_api_authed(self):
                self._json(403, {"ok": False, "output": "Not authenticated."}); return
            data = self._read_json_body(MAX_API_BODY)
            if data is None:
                self._json(400, {"ok": False, "output": "Bad request."}); return
            try:
                priority = int(data.get("priority", 5))
            except Exception:
                self._json(400, {"ok": False, "output": "Bad priority."}); return
            priority = max(1, min(priority, 100))
            vm       = str(data.get("vm", "management"))[:64]
            label    = str(data.get("label", "API job"))[:200]
            command  = str(data.get("command", ""))[:4096]
            if not command.strip():
                self._json(400, {"ok": False, "output": "command required"}); return
            fleet     = load_json(TOOLS_DIR / "fleet.json") or {"vms": []}
            fleet_vms = {v.get("name") for v in fleet.get("vms", []) if v.get("name")}
            fleet_vms.add("management")
            if vm not in fleet_vms:
                self._json(400, {"ok": False, "output": f"Unknown VM: {vm}"}); return
            enq_cmd = (
                f"python3 \"$CLOUD_LAB_DIR/payload/queue/queue_runner.py\" "
                f"--enqueue --label {shlex.quote(label)} "
                f"--command {shlex.quote(command)} --priority {priority}"
            )
            ok, out = run_payload_on_vm(vm, enq_cmd)
            _write_audit("enqueue", vm, f"label={label!r} priority={priority}", self)
            self._json(200, {"ok": ok, "output": out}); return

        if path == "/heartbeat":
            if not _is_heartbeat_authed(self):
                self._json(403, {"ok": False}); return
            data = self._read_json_body(MAX_HEARTBEAT_BODY)
            if data is None:
                self._json(400, {"ok": False}); return
            vm = str(data.get("vm") or data.get("vm_name") or "")[:64]
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
                    cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
                    existing["events"] = [
                        e for e in existing.get("events", []) if e.get("received_at", "") >= cutoff_iso
                    ][-50:]
                    _heartbeats[vm] = existing
                save_heartbeats()
            self._json(200, {"ok": True}); return

        if path == "/login":
            raw_body = self._read_body(MAX_LOGIN_BODY)
            if raw_body is None:
                self._html(400, login_pg(error=True)); return
            body    = raw_body.decode("utf-8", errors="replace")
            fields  = parse_qs(body)
            uname   = fields.get("username", [""])[0].strip()
            pw      = fields.get("password", [""])[0]
            client  = self.client_address[0]

            if not _check_rate_limit(client):
                self._html(429, login_pg(error=True, locked=True)); return
            if uname == USERNAME and _verify_password(pw):
                _clear_fails(client)
                sid = _create_session()
                self._redirect("/",
                    f'{COOKIE_NAME}={sid}; Max-Age={SESSION_DURATION}; '
                    f'Path=/; HttpOnly; Secure; SameSite=Strict')
            else:
                _record_fail(client)
                self._html(401, login_pg(error=True))
            return

        self._html(404, b"Not found")

    # ── response helpers ───────────────────────────────────────────────────────

    def _html(self, code: int, body: bytes | str) -> None:
        if isinstance(body, str):
            body = body.encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return

    def _json(self, code: int, data: dict) -> None:
        body = json.dumps(data).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return

    def _static_file(self, path: str) -> None:
        rel = path.removeprefix("/static/").replace("/", os.sep)
        try:
            static_root = STATIC_DIR.resolve()
            file_path = (STATIC_DIR / rel).resolve()
        except Exception:
            self._html(404, b"Not found")
            return
        if static_root != file_path and static_root not in file_path.parents:
            self._html(404, b"Not found")
            return
        if not file_path.is_file():
            self._html(404, b"Not found")
            return
        suffix = file_path.suffix.lower()
        content_type = {
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".woff2": "font/woff2",
            ".woff": "font/woff",
            ".m4a": "audio/mp4",
            ".ogg": "audio/ogg",
            ".webm": "audio/webm",
        }.get(suffix, "application/octet-stream")
        body = file_path.read_bytes()
        try:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            if DEV_MODE or suffix in {".css", ".js"}:
                self.send_header("Cache-Control", "no-cache, max-age=0")
            else:
                self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return

    def _redirect(self, location: str, set_cookie: str = "") -> None:
        self.send_response(303)
        self.send_header("Location", location)
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.end_headers()

    def log_message(self, fmt, *args):
        pass   # suppress per-request access log noise

    def handle_error(self, request, client_address):
        import sys
        if issubclass(sys.exc_info()[0], (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


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
