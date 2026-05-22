# payload/dashboard/

A simple lab landing page and system stats viewer for the laboratory VM. Serves on
`127.0.0.1:8700` and is accessible via SSH tunnel only — no authentication needed
because the tunnel is the gate.

---

## What it shows

- **System stats:** uptime, CPU load, memory, disk usage with progress bars
- **Fleet services:** which `cloud-lab-*` services are running and their state
- **Ideas:** 10 self-hosted projects that work well on the A1 Flex ARM instance,
  each with a project link and a quick-start install hint
- **Dark/light mode toggle** (remembers preference via `localStorage`)

---

## Access it

1. Find the lab's public IP in the admin console or `vm-profiles/laboratory.json`
2. Open an SSH tunnel from your laptop:
   ```bash
   ssh -i ~/.ssh/fleet.key -L 8700:localhost:8700 ubuntu@<lab-public-ip>
   ```
3. Open [http://localhost:8700](http://localhost:8700)

The tunnel stays open as long as the SSH session is active. Close it with `exit` or Ctrl-C.

---

## Install

**Manually** — SSH into the lab and run:
```bash
bash ~/cloud-lab/payload/dashboard/install.sh
```

**Via admin console** — use the Tools page to run the script on the `laboratory` VM:
```bash
bash ~/cloud-lab/payload/dashboard/install.sh
```

**Automatically** — add to `fleet/laboratory/setup.sh` before the final `echo` lines:
```bash
# ── lab dashboard ─────────────────────────────────────────────────────────────
sudo -H -u ubuntu bash "$TOOLS_DIR/payload/dashboard/install.sh" "$ENV_FILE"
```

---

## Replace with your own app

This dashboard is a minimal starting point, not a permanent fixture. Once you have a
real app to run, replace `lab_dashboard.py` with anything that listens on
`127.0.0.1:8700` (Flask, FastAPI, Gradio, Node.js, whatever) and keep the systemd
service from `install.sh` — or write a new one pointing at your app.

See [payload/README.md](../README.md) for the general payload pattern.
