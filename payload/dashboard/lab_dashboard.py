#!/usr/bin/env python3
"""
Cloud Lab — laboratory dashboard.
Bind: 127.0.0.1:8700. Access via SSH tunnel only.
No authentication — the SSH tunnel is the gate.
"""

from __future__ import annotations

import html
import json
import os
import subprocess
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

HOST       = "127.0.0.1"
PORT       = int(os.getenv("LAB_DASHBOARD_PORT", "8700"))
FLEET_NAME = os.getenv("FLEET_NAME", "Cloud Lab")
VM_NAME    = os.getenv("FLEET_VM_NAME", "laboratory")


# ── env ───────────────────────────────────────────────────────────────────────

def _read_env() -> dict[str, str]:
    out: dict[str, str] = {}
    p = Path.home() / ".config" / "cloud-lab" / f"{VM_NAME}.env"
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


# ── system stats ──────────────────────────────────────────────────────────────

def _system_stats() -> dict:
    stats: dict = {}

    try:
        secs = int(float(Path("/proc/uptime").read_text().split()[0]))
        d, rem = divmod(secs, 86400); h, rem = divmod(rem, 3600); m = rem // 60
        stats["uptime"] = f"{d}d {h}h {m}m" if d else f"{h}h {m}m"
    except Exception:
        stats["uptime"] = "—"

    try:
        stats["load"] = " / ".join(Path("/proc/loadavg").read_text().split()[:3])
    except Exception:
        stats["load"] = "—"

    try:
        stats["cpus"] = sum(
            1 for ln in Path("/proc/cpuinfo").read_text().splitlines()
            if ln.startswith("processor")
        )
    except Exception:
        stats["cpus"] = "?"

    try:
        mem: dict[str, int] = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                mem[k.strip()] = int(v.strip().split()[0])
        total = mem.get("MemTotal", 0)
        avail = mem.get("MemAvailable", 0)
        used  = total - avail
        pct   = int(used / total * 100) if total else 0
        gb    = lambda kb: f"{kb / 1_048_576:.1f}"
        stats["memory"]  = f"{gb(used)} / {gb(total)} GB ({pct}%)"
        stats["mem_pct"] = pct
    except Exception:
        stats["memory"] = "—"; stats["mem_pct"] = 0

    try:
        r = subprocess.run(["df", "-h", "/"], capture_output=True, text=True, timeout=5)
        parts = r.stdout.strip().splitlines()[-1].split()
        stats["disk"]     = f"{parts[2]} / {parts[1]} ({parts[4]})"
        stats["disk_pct"] = int(parts[4].rstrip("%"))
    except Exception:
        stats["disk"] = "—"; stats["disk_pct"] = 0

    try:
        stats["hostname"] = Path("/etc/hostname").read_text().strip()
    except Exception:
        stats["hostname"] = VM_NAME

    return stats


def _running_services() -> list[tuple[str, str]]:
    try:
        r = subprocess.run(
            ["systemctl", "list-units", "--type=service", "--all",
             "--no-pager", "--no-legend", "cloud-lab-*"],
            capture_output=True, text=True, timeout=10,
        )
        out = []
        for line in r.stdout.splitlines():
            parts = line.split()
            if not parts or "cloud-lab-" not in parts[0]:
                continue
            name   = parts[0].replace(".service", "")
            active = parts[2] if len(parts) > 2 else "unknown"
            out.append((name, active))
        return out
    except Exception:
        return []


# ── CSS / JS ──────────────────────────────────────────────────────────────────

_CSS = """
:root {
  --c-primary:    #374151;
  --c-primary-lt: #4b5563;
  --c-accent:     #f59e0b;
  --c-bg:         #f4f6f8;
  --c-card:       #ffffff;
  --c-text:       #111827;
  --c-muted:      #6b7280;
  --c-border:     #d1d5db;
  --c-ok:         #16a34a;
  --c-warn:       #d97706;
  --c-code-bg:    #0f172a;
  --c-code-text:  #e2e8f0;
}
[data-theme="dark"] {
  --c-bg:      #111827; --c-card:   #1f2937;
  --c-text:    #f9fafb; --c-muted:  #9ca3af;
  --c-border:  #374151;
}
*, *::before, *::after { box-sizing: border-box; }
body { font-family: system-ui,-apple-system,sans-serif; margin: 0;
       background: var(--c-bg); color: var(--c-text); transition: background .15s, color .15s; }

.topbar { background: var(--c-primary); color: #fff; padding: 0 20px;
          display: flex; align-items: center; justify-content: space-between; height: 52px; }
.topbar-left { display: flex; align-items: center; gap: 10px; }
.fleet-name  { font-size: 17px; font-weight: 700; }
.vm-label    { font-size: 13px; color: rgba(255,255,255,.6);
               background: rgba(255,255,255,.12); padding: 3px 10px; border-radius: 999px; }
.theme-btn   { background: transparent; border: none; cursor: pointer;
               color: rgba(255,255,255,.75); font-size: 16px; padding: 6px 10px;
               border-radius: 6px; transition: background .15s; }
.theme-btn:hover { background: rgba(255,255,255,.15); color: #fff; }

.content { max-width: 960px; margin: 28px auto; padding: 0 16px; }
.grid2   { display: grid; gap: 14px; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); }
.grid3   { display: grid; gap: 14px; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); }

.card       { background: var(--c-card); border: 1px solid var(--c-border); border-radius: 12px; padding: 18px 20px; }
.card-title { font-size: 11px; font-weight: 700; color: var(--c-muted);
              text-transform: uppercase; letter-spacing: .5px; margin: 0 0 12px; }

.stat-row          { display: flex; justify-content: space-between; align-items: baseline;
                     font-size: 14px; padding: 5px 0; border-bottom: 1px solid var(--c-border); }
.stat-row:last-child { border-bottom: none; }
.stat-label        { color: var(--c-muted); font-weight: 600; }
.stat-val          { font-family: monospace; font-size: 13px; }
.bar-wrap          { background: var(--c-border); border-radius: 4px; height: 5px; margin: 2px 0 6px; }
.bar               { height: 5px; border-radius: 4px; background: var(--c-ok); transition: width .3s; }
.bar.warn          { background: var(--c-warn); }

.svc-row           { display: flex; justify-content: space-between; align-items: center;
                     font-size: 13px; padding: 6px 0; border-bottom: 1px solid var(--c-border); }
.svc-row:last-child { border-bottom: none; }
.svc-name          { font-family: monospace; font-size: 12px; }
.svc-dot           { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
.svc-dot.active    { background: var(--c-ok); }
.svc-dot.inactive  { background: var(--c-muted); }
.svc-dot.failed    { background: #dc2626; }

.section-title { font-size: 15px; font-weight: 700; margin: 28px 0 10px; }

.idea-card  { background: var(--c-card); border: 1px solid var(--c-border); border-radius: 12px; padding: 16px 18px; }
.idea-title { font-size: 14px; font-weight: 700; margin: 0 0 4px; }
.idea-title a { color: inherit; text-decoration: none; }
.idea-title a:hover { text-decoration: underline; color: var(--c-accent); }
.idea-desc  { font-size: 13px; color: var(--c-muted); margin: 0 0 10px; line-height: 1.5; }
.tags       { display: flex; gap: 5px; flex-wrap: wrap; margin-bottom: 10px; }
.tag        { font-size: 11px; padding: 2px 8px; border-radius: 999px;
              background: var(--c-border); color: var(--c-muted); }
.tag.arm    { background: #dbeafe; color: #1d4ed8; }
.tag.llm    { background: #ede9fe; color: #6d28d9; }
.idea-install { background: var(--c-code-bg); color: var(--c-code-text); border-radius: 7px;
                padding: 9px 12px; font-family: monospace; font-size: 11.5px; line-height: 1.6;
                white-space: pre; overflow-x: auto; margin: 0; }
.idea-note  { font-size: 11px; color: var(--c-muted); margin: 16px 0 4px; font-style: italic; }

.tunnel-box { background: var(--c-code-bg); color: var(--c-code-text); border-radius: 10px;
              padding: 14px 18px; font-family: monospace; font-size: 13px; line-height: 1.7;
              overflow-x: auto; white-space: pre; }

footer { text-align: center; font-size: 12px; color: var(--c-muted); padding: 24px 16px; }

@media (max-width: 600px) {
  .fleet-name { font-size: 15px; }
  .topbar     { padding: 0 12px; }
}
"""

_THEME_JS = """
(function(){
  var s = localStorage.getItem('lab-theme');
  var p = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  document.documentElement.setAttribute('data-theme', s || p);
  var i = document.getElementById('theme-icon');
  if (i) i.textContent = (s||p) === 'dark' ? '☀' : '🌙';
})();
function toggleTheme() {
  var t = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', t);
  localStorage.setItem('lab-theme', t);
  var i = document.getElementById('theme-icon');
  if (i) i.textContent = t === 'dark' ? '☀' : '🌙';
}
"""


# ── Ideas: self-hosted projects that run well on the A1 Flex ──────────────────
# Each entry: (title, description, tags, homepage_url, quick_install_hint)

_IDEAS = [
    (
        "Vaultwarden",
        "Self-hosted Bitwarden-compatible password manager. Rust binary, ~10 MB, ARM-native.",
        ["arm-native", "single binary"],
        "https://github.com/dani-garcia/vaultwarden",
        "docker run -d --name vaultwarden -v ~/vw-data:/data -p 80:80 vaultwarden/server:latest",
    ),
    (
        "Miniflux",
        "Fast, minimalist RSS reader. Single Go binary, Postgres or SQLite backend.",
        ["arm-native", "single binary"],
        "https://miniflux.app",
        "# grab latest arm64 binary from github.com/miniflux/v2/releases\nchmod +x miniflux && ./miniflux -migrate -create-admin",
    ),
    (
        "Syncthing",
        "Peer-to-peer file sync — a self-hosted Dropbox. Works great on ARM.",
        ["arm-native", "single binary"],
        "https://syncthing.net",
        "sudo apt-get install -y syncthing\nsystemctl --user enable --now syncthing",
    ),
    (
        "Gitea",
        "Lightweight self-hosted Git service. Full GitHub-like UI in one Go binary.",
        ["arm-native", "single binary"],
        "https://gitea.com",
        "# grab latest arm64 binary from dl.gitea.com/gitea/latest/\nchmod +x gitea && ./gitea web",
    ),
    (
        "Jupyter Lab",
        "Notebook server for Python, data science, and ML. The 4 OCPU handles parallel kernels.",
        ["python", "arm-native"],
        "https://jupyter.org",
        "pip install jupyterlab\njupyter lab --ip=127.0.0.1 --port=8888 --no-browser",
    ),
    (
        "llama.cpp",
        "Run quantized LLMs locally. A 7B Q4 model fits easily in 24 GB. ARM NEON-optimized.",
        ["arm-native", "llm"],
        "https://github.com/ggerganov/llama.cpp",
        "git clone https://github.com/ggerganov/llama.cpp\ncd llama.cpp && make -j$(nproc)",
    ),
    (
        "Netdata",
        "Real-time system and fleet monitoring dashboard. One-script install, ARM-native.",
        ["arm-native", "monitoring"],
        "https://www.netdata.cloud",
        "wget -O /tmp/netdata.sh https://get.netdata.cloud/kickstart.sh\nbash /tmp/netdata.sh",
    ),
    (
        "Calibre-Web",
        "Browse and serve your ebook library from a web UI. Python + SQLite.",
        ["python"],
        "https://github.com/janeczku/calibre-web",
        "pip install calibreweb\ncps",
    ),
]


# ── Page renderer ─────────────────────────────────────────────────────────────

def _render_page() -> str:
    stats    = _system_stats()
    svcs     = _running_services()
    env      = _read_env()
    mgmt_ip  = env.get("FLEET_MANAGEMENT_PRIVATE_IP", "—")
    now      = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    hostname = html.escape(stats.get("hostname", VM_NAME))

    # stats rows
    mem_pct  = stats.get("mem_pct",  0)
    disk_pct = stats.get("disk_pct", 0)
    _rows = [
        ("Hostname",   html.escape(stats.get("hostname", "—")), None),
        ("Uptime",     html.escape(stats.get("uptime",   "—")), None),
        ("CPU load",
         html.escape(stats.get("load", "—")) + f" &middot; {stats.get('cpus','?')} cores",
         None),
        ("Memory",     html.escape(stats.get("memory",   "—")), mem_pct),
        ("Disk (/)",   html.escape(stats.get("disk",     "—")), disk_pct),
        ("Management", html.escape(mgmt_ip),                    None),
    ]
    stats_html = ""
    for label, val, pct in _rows:
        bar = ""
        if pct is not None:
            warn = " warn" if pct > 80 else ""
            bar = (f'<div class="bar-wrap">'
                   f'<div class="bar{warn}" style="width:{min(pct,100)}%"></div>'
                   f'</div>')
        stats_html += (
            f'<div class="stat-row">'
            f'<span class="stat-label">{label}</span>'
            f'<span class="stat-val">{val}</span>'
            f'</div>{bar}'
        )

    # services
    if svcs:
        svc_html = ""
        for name, active in svcs:
            dot = ("active" if active == "active"
                   else ("failed" if active == "failed" else "inactive"))
            svc_html += (
                f'<div class="svc-row">'
                f'<span class="svc-name">{html.escape(name)}</span>'
                f'<span class="svc-dot {dot}" title="{html.escape(active)}"></span>'
                f'</div>'
            )
    else:
        svc_html = '<p style="color:var(--c-muted);font-size:13px;margin:0">No cloud-lab services found.</p>'

    # ideas
    ideas_html = ""
    for title, desc, tags, url, install in _IDEAS:
        tag_html = "".join(
            f'<span class="tag{" arm" if t == "arm-native" else (" llm" if t == "llm" else "")}">'
            f'{html.escape(t)}</span>'
            for t in tags
        )
        ideas_html += (
            f'<div class="idea-card">'
            f'<p class="idea-title"><a href="{html.escape(url)}" target="_blank" rel="noopener">'
            f'{html.escape(title)}</a></p>'
            f'<p class="idea-desc">{html.escape(desc)}</p>'
            f'<div class="tags">{tag_html}</div>'
            f'<pre class="idea-install">{html.escape(install)}</pre>'
            f'</div>'
        )
    ideas_html += '<p class="idea-note">Community suggestions — review each project\'s docs before installing.</p>'

    return (
        f'<!doctype html><html lang="en"><head>'
        f'<meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{html.escape(FLEET_NAME)} — Lab Dashboard</title>'
        f'<style>{_CSS}</style>'
        f'<script>{_THEME_JS}</script>'
        f'</head><body>'
        f'<div class="topbar">'
        f'<div class="topbar-left">'
        f'<span class="fleet-name">{html.escape(FLEET_NAME)}</span>'
        f'<span class="vm-label">{hostname}</span>'
        f'</div>'
        f'<button class="theme-btn" title="Toggle dark/light" onclick="toggleTheme()">'
        f'<span id="theme-icon">&#127769;</span></button>'
        f'</div>'
        f'<div class="content">'
        f'<div class="grid2">'
        f'<div class="card"><p class="card-title">System</p>{stats_html}</div>'
        f'<div class="card"><p class="card-title">Fleet Services</p>{svc_html}</div>'
        f'</div>'
        f'<p class="section-title">What to run on your A1 Flex</p>'
        f'<div class="grid3">{ideas_html}</div>'
        f'<p class="section-title">Access this dashboard</p>'
        f'<div class="tunnel-box"># From your laptop:\n'
        f'ssh -i ~/.ssh/fleet.key -L 8700:localhost:8700 ubuntu@&lt;lab-public-ip&gt;\n'
        f'# Then open: http://localhost:8700</div>'
        f'<footer>{html.escape(FLEET_NAME)} &middot; lab &middot; {now}</footer>'
        f'</div></body></html>'
    )


# ── HTTP handler ──────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", ""):
            body = _render_page().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.send_header("Content-Length", "9")
            self.end_headers()
            self.wfile.write(b"Not found")

    def log_message(self, fmt, *args):
        pass


# ── startup ───────────────────────────────────────────────────────────────────

def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"[lab-dashboard] {FLEET_NAME} — listening on {HOST}:{PORT}", flush=True)
    print(f"  Access: ssh -i ~/.ssh/fleet.key -L 8700:localhost:8700 ubuntu@<lab-ip>", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
