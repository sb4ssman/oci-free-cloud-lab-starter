#!/usr/bin/env python3
"""
Cloud Lab admin console — runs on the management VM.
Accessible at https://<ADMIN_DOMAIN> via Caddy reverse proxy.

Endpoints:
  GET  /              Fleet status page (login required)
  GET  /login         Login form
  POST /login         Validate credentials, set session cookie, redirect to /
  GET  /logout        Clear session, redirect to /login
  POST /heartbeat     Liveness pings from worker/laboratory (no auth required)
  GET  /export        Fleet connection details for downstream projects (login required)
  GET  /stats?vm=<name>  Live system stats for any fleet VM (login required)
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import secrets
import subprocess
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


HOST       = os.getenv("ADMIN_CONSOLE_HOST", "127.0.0.1")
PORT       = int(os.getenv("ADMIN_CONSOLE_PORT", "8765"))
USERNAME   = os.getenv("ADMIN_USERNAME", "admin")
PW_HASH    = os.getenv("ADMIN_PASSWORD_HASH", "")
FLEET_NAME = os.getenv("FLEET_NAME", "Cloud Lab")
TOOLS_DIR  = Path(os.getenv("CLOUD_LAB_DIR",
             str(Path.home() / "cloud-lab"))).expanduser()
HEARTBEATS_FILE = TOOLS_DIR / "vm-profiles" / "_heartbeats.json"

COOKIE_NAME      = "fleet_session"
SESSION_DURATION = 7 * 24 * 3600   # 7 days

_sessions: dict[str, float] = {}
_sessions_lock = threading.Lock()

_heartbeats: dict[str, dict] = {}
_hb_lock = threading.Lock()

# ip -> (fail_count, window_start)
_login_fails: dict[str, tuple[int, float]] = {}
_fails_lock  = threading.Lock()
MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_SECONDS    = 900   # 15 minutes


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
    """Return True if the IP is allowed to attempt login, False if locked out."""
    now = time.time()
    with _fails_lock:
        count, window_start = _login_fails.get(ip, (0, now))
        if now - window_start > LOCKOUT_SECONDS:
            # window expired — reset
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
        if delta < 60:
            return f"{delta}s ago"
        if delta < 3600:
            return f"{delta // 60}m ago"
        h = delta // 3600
        m = (delta % 3600) // 60
        return f"{h}h {m}m ago"
    except Exception:
        return iso


# ── live stats ────────────────────────────────────────────────────────────────

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


def collect_local_stats() -> str:
    try:
        result = subprocess.run(
            ["bash", "-c", _STATS_CMD],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, timeout=10,
        )
        return result.stdout or "(no output)"
    except Exception as exc:
        return f"Error collecting local stats: {exc}"


def collect_remote_stats(vm_name: str) -> str:
    env = _mgmt_env()
    ssh_key  = env.get("OCI_SSH_PRIVATE_KEY_PATH", str(Path.home() / ".ssh" / "fleet.key"))
    ssh_user = env.get("OCI_SSH_USER", "ubuntu")

    # look up public IP from vm-profiles snapshot
    profile = load_json(TOOLS_DIR / "vm-profiles" / f"{vm_name}.json") or {}
    public_ip = profile.get("public_ip", "")
    if not public_ip or public_ip == "—":
        return f"No public IP found for {vm_name} in vm-profiles. Run check-all-vms first."

    key_path = str(Path(ssh_key).expanduser())
    try:
        result = subprocess.run(
            ["ssh",
             "-i", key_path,
             "-o", "StrictHostKeyChecking=accept-new",
             "-o", "ConnectTimeout=8",
             "-o", "BatchMode=yes",
             f"{ssh_user}@{public_ip}",
             _STATS_CMD],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, timeout=20,
        )
        return result.stdout or "(no output)"
    except subprocess.TimeoutExpired:
        return f"SSH timed out connecting to {vm_name} ({public_ip})."
    except Exception as exc:
        return f"SSH error for {vm_name} ({public_ip}): {exc}"


def stats_page(vm_name: str, fleet_vms: list) -> bytes:
    title = html.escape(FLEET_NAME)
    safe_name = html.escape(vm_name)

    if vm_name == "management":
        raw = collect_local_stats()
    else:
        raw = collect_remote_stats(vm_name)

    output = html.escape(raw)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    vm_links = " &nbsp;·&nbsp; ".join(
        f'<a href="/stats?vm={html.escape(v)}" {"class=\"active\"" if v == vm_name else ""}>{html.escape(v)}</a>'
        for v in fleet_vms
    )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{title} — {safe_name} stats</title>
  <style>
    {COMMON_CSS}
    .topbar {{ background: #1e293b; color: white; padding: 14px 24px;
               display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; }}
    .topbar h1 {{ margin: 0; font-size: 18px; }}
    .topbar nav {{ display: flex; gap: 16px; align-items: center; }}
    .topbar a {{ color: #94a3b8; font-size: 13px; text-decoration: none; }}
    .topbar a:hover, .topbar a.active {{ color: white; }}
    .content {{ max-width: 1000px; margin: 24px auto; padding: 0 16px; }}
    .vmbar {{ display: flex; gap: 10px; margin-bottom: 16px; flex-wrap: wrap; align-items: center; }}
    .vmbar a {{ padding: 5px 14px; border-radius: 999px; font-size: 13px; font-weight: 500;
                background: #e2e8f0; color: #374151; text-decoration: none; }}
    .vmbar a:hover {{ background: #cbd5e1; }}
    .vmbar a.active {{ background: #1e293b; color: white; }}
    .meta {{ font-size: 12px; color: #64748b; margin-bottom: 8px; }}
    pre {{ background: #0f172a; color: #e2e8f0; border-radius: 10px; padding: 20px;
           font-size: 12.5px; line-height: 1.55; overflow-x: auto; white-space: pre; }}
    .actions {{ margin-top: 14px; display: flex; gap: 10px; align-items: center; }}
    .btn {{ padding: 7px 18px; border-radius: 7px; font-size: 13px; font-weight: 600;
            background: #2563eb; color: white; border: none; cursor: pointer; text-decoration: none; }}
    .btn:hover {{ background: #1d4ed8; }}
    label {{ font-size: 13px; color: #374151; display: flex; align-items: center; gap: 6px; cursor: pointer; }}
  </style>
  <script>
    let autoRefresh = false;
    let timer = null;
    function toggleAuto(cb) {{
      autoRefresh = cb.checked;
      if (autoRefresh) {{
        timer = setInterval(() => location.reload(), 10000);
      }} else {{
        clearInterval(timer);
      }}
    }}
  </script>
</head>
<body>
  <div class="topbar">
    <h1>{title} — Live Stats</h1>
    <nav>
      <a href="/">← Fleet</a>
      <a href="/export">Export</a>
      <a href="/logout">Sign out</a>
    </nav>
  </div>
  <div class="content">
    <div class="vmbar">{vm_links}</div>
    <p class="meta">Snapshot taken at {now}</p>
    <pre>{output}</pre>
    <div class="actions">
      <a class="btn" href="/stats?vm={html.escape(vm_name)}">Refresh</a>
      <label><input type="checkbox" onchange="toggleAuto(this)"> Auto-refresh every 10s</label>
    </div>
  </div>
</body>
</html>""".encode("utf-8")


# ── HTML ──────────────────────────────────────────────────────────────────────

COMMON_CSS = """
  body { font-family: system-ui, sans-serif; margin: 0; background: #f0f2f5; color: #17202a; }
"""


def vm_cards() -> str:
    fleet = load_json(TOOLS_DIR / "fleet.json") or {"vms": []}
    profile_dir = TOOLS_DIR / "vm-profiles"
    with _hb_lock:
        hbs = dict(_heartbeats)

    cards = []
    for vm in fleet.get("vms", []):
        name       = vm.get("name", "")
        profile    = load_json(profile_dir / f"{name}.json") or {}
        instance   = profile.get("instance", {})
        state      = instance.get("lifecycle-state", "UNKNOWN")
        public_ip  = profile.get("public_ip", "—")
        private_ip = profile.get("private_ip", "—")
        synced_at  = fmt_ago(profile["synced_at"]) if profile.get("synced_at") else "never"

        hb = hbs.get(name, {})
        if hb:
            hb_html = (
                f'<p><b>Heartbeat:</b> {html.escape(fmt_ago(hb.get("received_at", "")))} '
                f'— uptime {html.escape(hb.get("uptime", "?"))}</p>'
            )
        elif vm.get("role") != "management":
            hb_html = '<p class="warn"><b>Heartbeat:</b> not received yet</p>'
        else:
            hb_html = ""

        sc = state.lower().replace("/", "-")
        stats_link = f'<a class="stats-link" href="/stats?vm={html.escape(name)}">Live stats ›</a>'
        cards.append(f"""<div class="card">
  <div class="card-header">
    <span class="name">{html.escape(name)}</span>
    <span class="badge {html.escape(sc)}">{html.escape(state)}</span>
  </div>
  <p><b>Role:</b> {html.escape(vm.get('role', ''))}</p>
  <p><b>Shape:</b> {html.escape(instance.get('shape') or vm.get('shape', ''))}</p>
  <p><b>Public IP:</b> {html.escape(public_ip)}</p>
  <p><b>Private IP:</b> {html.escape(private_ip)}</p>
  <p><b>OCI snapshot:</b> {html.escape(synced_at)}</p>
  {hb_html}
  <p class="notes">{html.escape(vm.get('notes', ''))}</p>
  {stats_link}
</div>""")
    return "\n".join(cards) if cards else "<p>No VMs defined in fleet.json.</p>"


def fleet_page() -> bytes:
    title = html.escape(FLEET_NAME)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta http-equiv="refresh" content="60">
  <title>{title}</title>
  <style>
    {COMMON_CSS}
    .topbar {{ background: #1e293b; color: white; padding: 14px 24px;
               display: flex; justify-content: space-between; align-items: center; }}
    .topbar h1 {{ margin: 0; font-size: 18px; letter-spacing: .5px; }}
    .topbar nav {{ display: flex; gap: 16px; }}
    .topbar a {{ color: #94a3b8; font-size: 13px; text-decoration: none; }}
    .topbar a:hover {{ color: white; }}
    .grid {{ max-width: 1060px; margin: 24px auto; padding: 0 16px;
             display: grid; gap: 14px;
             grid-template-columns: repeat(auto-fit, minmax(290px, 1fr)); }}
    .card {{ background: white; border: 1px solid #dde1e7; border-radius: 10px; padding: 18px; }}
    .card-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }}
    .name {{ font-size: 17px; font-weight: 600; }}
    p {{ margin: 5px 0; font-size: 14px; }}
    .badge {{ border-radius: 999px; padding: 3px 10px; font-size: 12px; font-weight: 500;
              background: #e2e8f0; color: #475569; }}
    .running {{ background: #dcfce7; color: #166534; }}
    .terminated, .terminating {{ background: #fee2e2; color: #991b1b; }}
    .warn {{ color: #92400e; }}
    .notes {{ color: #64748b; font-size: 13px; }}
    .stats-link {{ display: inline-block; margin-top: 8px; font-size: 12px; font-weight: 600;
                   color: #2563eb; text-decoration: none; }}
    .stats-link:hover {{ color: #1d4ed8; text-decoration: underline; }}
    footer {{ text-align: center; font-size: 12px; color: #94a3b8; padding: 16px; }}
  </style>
</head>
<body>
  <div class="topbar">
    <h1>{title}</h1>
    <nav>
      <a href="/stats">Stats</a>
      <a href="/export">Export config</a>
      <a href="/logout">Sign out</a>
    </nav>
  </div>
  <div class="grid">{vm_cards()}</div>
  <footer>Auto-refreshes every 60s &nbsp;·&nbsp; management VM</footer>
</body>
</html>""".encode("utf-8")


def export_page() -> bytes:
    """Fleet connection details — copy these into your downstream project's .env."""
    fleet = load_json(TOOLS_DIR / "fleet.json") or {"vms": []}
    profile_dir = TOOLS_DIR / "vm-profiles"
    fleet_name = html.escape(FLEET_NAME)

    lines = [f"# Fleet connection details — generated by {FLEET_NAME} management console",
             f"# Copy relevant values into your project's .env\n"]
    for vm in fleet.get("vms", []):
        name = vm.get("name", "")
        profile = load_json(profile_dir / f"{name}.json") or {}
        pub = profile.get("public_ip", "")
        priv = profile.get("private_ip", "")
        role = vm.get("role", "")
        lines.append(f"# {name} ({role})")
        slug = name.upper().replace("-", "_")
        lines.append(f"OCI_{slug}_HOST={pub}")
        lines.append(f"OCI_{slug}_PRIVATE_IP={priv}")
        lines.append("")

    env_file = TOOLS_DIR / ".config" / "cloud-lab" / "management.env"
    mgmt_env = {}
    if (Path.home() / ".config" / "cloud-lab" / "management.env").exists():
        for raw in (Path.home() / ".config" / "cloud-lab" / "management.env").read_text().splitlines():
            if "=" in raw and not raw.startswith("#"):
                k, v = raw.split("=", 1)
                mgmt_env[k.strip()] = v.strip()

    lines.append(f"FLEET_MANAGEMENT_PRIVATE_IP={mgmt_env.get('FLEET_MANAGEMENT_PRIVATE_IP', '')}")
    lines.append(f"OCI_SSH_USER=ubuntu")
    lines.append(f"# SSH key: ~/.ssh/fleet.key  (from the management VM)")

    config_text = html.escape("\n".join(lines))
    title = fleet_name

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{title} — Export</title>
  <style>
    {COMMON_CSS}
    .topbar {{ background: #1e293b; color: white; padding: 14px 24px;
               display: flex; justify-content: space-between; align-items: center; }}
    .topbar h1 {{ margin: 0; font-size: 18px; }}
    .topbar a {{ color: #94a3b8; font-size: 13px; text-decoration: none; }}
    .topbar a:hover {{ color: white; }}
    .content {{ max-width: 700px; margin: 32px auto; padding: 0 16px; }}
    h2 {{ font-size: 16px; color: #374151; }}
    pre {{ background: white; border: 1px solid #dde1e7; border-radius: 8px;
           padding: 20px; font-size: 13px; overflow-x: auto; white-space: pre-wrap; }}
    p.hint {{ color: #64748b; font-size: 13px; }}
  </style>
</head>
<body>
  <div class="topbar">
    <h1>{title}</h1>
    <a href="/">← Fleet</a>
  </div>
  <div class="content">
    <h2>Fleet connection details</h2>
    <p class="hint">Copy the values your downstream project needs into its own .env file.</p>
    <pre>{config_text}</pre>
  </div>
</body>
</html>""".encode("utf-8")


def login_page(error: bool = False, locked: bool = False) -> bytes:
    title = html.escape(FLEET_NAME)
    if locked:
        err = '<p class="error">Too many failed attempts. Try again in 15 minutes.</p>'
    elif error:
        err = '<p class="error">Incorrect username or password.</p>'
    else:
        err = ""
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{title} — Sign In</title>
  <style>
    {COMMON_CSS}
    body {{ display: flex; align-items: center; justify-content: center; min-height: 100vh; }}
    .box {{ background: white; border: 1px solid #dde1e7; border-radius: 12px;
            padding: 36px 32px; width: 100%; max-width: 360px; box-shadow: 0 2px 12px #0001; }}
    h1 {{ margin: 0 0 6px; font-size: 22px; }}
    p.sub {{ margin: 0 0 24px; color: #64748b; font-size: 14px; }}
    label {{ display: block; font-size: 13px; font-weight: 600; margin-bottom: 4px; color: #374151; }}
    input {{ width: 100%; box-sizing: border-box; padding: 10px 12px; font-size: 15px;
             border: 1px solid #d1d5db; border-radius: 7px; margin-bottom: 14px; outline: none; }}
    input:focus {{ border-color: #2563eb; box-shadow: 0 0 0 3px #2563eb22; }}
    button {{ width: 100%; padding: 11px; font-size: 15px; font-weight: 600;
              background: #2563eb; color: white; border: none; border-radius: 7px; cursor: pointer; }}
    button:hover {{ background: #1d4ed8; }}
    .error {{ color: #dc2626; font-size: 13px; margin: 0 0 14px; }}
  </style>
</head>
<body>
  <div class="box">
    <h1>{title}</h1>
    <p class="sub">Private admin console</p>
    {err}
    <form method="POST" action="/login">
      <label for="u">Username</label>
      <input id="u" type="text" name="username" autocomplete="username" autofocus>
      <label for="p">Password</label>
      <input id="p" type="password" name="password" autocomplete="current-password">
      <button type="submit">Sign in</button>
    </form>
  </div>
</body>
</html>""".encode("utf-8")


# ── HTTP handler ──────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip("/") or "/"
        qs     = parse_qs(parsed.query)

        if path == "/login":
            self._html(200, login_page())
        elif path == "/logout":
            cookies = _parse_cookies(self.headers.get("Cookie", ""))
            sid = cookies.get(COOKIE_NAME, "")
            if sid:
                with _sessions_lock:
                    _sessions.pop(sid, None)
            self.send_response(302)
            self.send_header("Location", "/login")
            self.send_header("Set-Cookie",
                f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0")
            self.end_headers()
        elif not _is_authed(self):
            self.send_response(302)
            self.send_header("Location", "/login")
            self.end_headers()
        elif path == "/export":
            self._html(200, export_page())
        elif path == "/stats":
            fleet = load_json(TOOLS_DIR / "fleet.json") or {"vms": []}
            fleet_names = [v.get("name", "") for v in fleet.get("vms", []) if v.get("name")]
            vm_name = (qs.get("vm") or ["management"])[0]
            if vm_name not in fleet_names:
                vm_name = "management"
            self._html(200, stats_page(vm_name, fleet_names))
        else:
            self._html(200, fleet_page())

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        path = urlparse(self.path).path.rstrip("/")

        if path == "/login":
            client_ip = self.client_address[0]
            if not _check_rate_limit(client_ip):
                self._html(429, login_page(error=True, locked=True))
                return
            params   = parse_qs(body.decode("utf-8", errors="replace"))
            username = (params.get("username") or [""])[0]
            password = (params.get("password") or [""])[0]
            if username == USERNAME and _verify_password(password):
                _clear_fails(client_ip)
                sid = _create_session()
                self.send_response(302)
                self.send_header("Location", "/")
                self.send_header("Set-Cookie",
                    f"{COOKIE_NAME}={sid}; Path=/; HttpOnly; SameSite=Strict; "
                    f"Max-Age={SESSION_DURATION}")
                self.end_headers()
            else:
                _record_fail(client_ip)
                self._html(401, login_page(error=True))

        elif path == "/heartbeat":
            try:
                data = json.loads(body) if body else {}
            except Exception:
                data = {}
            sender = str(data.get("vm_name", "unknown"))
            now = datetime.now(timezone.utc).isoformat()
            with _hb_lock:
                existing = _heartbeats.get(sender, {})
                events = list(existing.get("events", []))
                event = data.get("event")
                if event:
                    events.append({
                        "received_at": now,
                        "event": str(event),
                        "details": data.get("details", {}),
                    })
                    events = events[-20:]
                _heartbeats[sender] = {
                    "received_at": now,
                    "uptime": str(data.get("uptime", "?")),
                    "extra": data,
                    "events": events,
                }
            save_heartbeats()
            self._respond(200, b"ok")

        else:
            self._respond(404, b"Not found")

    def _html(self, code: int, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def _respond(self, code: int, body: bytes) -> None:
        self.send_response(code)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


if __name__ == "__main__":
    load_heartbeats()
    print(f"[admin_console] Listening on {HOST}:{PORT}", flush=True)
    print(f"[admin_console] Fleet: {FLEET_NAME}", flush=True)
    print(f"[admin_console] Tools dir: {TOOLS_DIR}", flush=True)
    if PW_HASH:
        print(f"[admin_console] Password auth enabled. Username: {USERNAME}", flush=True)
    else:
        print("[admin_console] WARNING: ADMIN_PASSWORD_HASH not set — all logins will fail.", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
