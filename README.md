# Oracle Free Cloud Lab Starter

Fork this repo, fill in a `.env`, run one script — and watch three Oracle Cloud VMs
build themselves. The management VM comes up first and takes over: it provisions the
worker VM, then runs a continuous lottery loop until Oracle hands over the big A1 Flex
machine. You can watch the whole cascade from the admin portal that comes up
automatically on the management VM.

Once the lab is running, it keeps itself alive and self-heals indefinitely. You layer
your own workloads on top.

> **LLM-friendly:** point your favorite AI at this repo (or a clone of it) and ask it
> to walk you through setup — the structure is designed for that.

---

## How it builds itself

```
You run launch-management  →  Management VM boots (cloud-init, ~5 min)
                                  ↓
                           Admin console comes up at https://your-domain.duckdns.org
                                  ↓
                           Fleet orchestrator starts, reads fleet.json
                                  ↓
                           Launches worker VM  (micro, ~3 min)
                                  ↓
                           Starts A1 Flex lottery loop for laboratory  (hours–days)
                                  ↓
                           laboratory wins capacity, boots, checks in via heartbeat
```

You are watching a self-assembling cloud lab. The only manual step after the first
script is updating your DuckDNS record and the IP values in `.env` as VMs come up.

---

## Pre-flight checklist

Complete all of these before running anything.

- [ ] **Oracle Cloud account** — free tier is enough; verify your tenancy is active
- [ ] **OCI CLI installed and authenticated** — run `oci setup config` and confirm
      `oci iam user get --user-id <your-ocid>` works
      ([install guide](https://docs.oracle.com/en-us/iaas/Content/API/SDKDocs/cliinstall.htm))
- [ ] **Python 3.10+** — the admin scripts run locally on your machine
- [ ] **SSH key pair** — generate one at `~/.ssh/fleet.key` if you don't have one:
      `ssh-keygen -t ed25519 -f ~/.ssh/fleet.key`
- [ ] **DuckDNS account + subdomain** — free at [duckdns.org](https://www.duckdns.org/).
      You need this for the admin console's HTTPS certificate.
- [ ] **ntfy topic name** — free at [ntfy.sh](https://ntfy.sh/). Pick any unique string;
      this is how the fleet notifies you. (Optional but strongly recommended.)
- [ ] **GitHub account** — you need to fork this repo so VMs can clone your copy.
      Create a read-only [personal access token](https://github.com/settings/tokens)
      with `repo` scope for the `GITHUB_TOKEN` in `.env`.

---

## Quickstart

### 1. Fork this repo

Click **Fork** on GitHub. The VMs will clone your fork — they need access to a repo
you control so you can customize `fleet.json` and the payload layer.

### 2. Clone your fork and configure

```bash
git clone https://github.com/YOUR_USERNAME/oci-free-cloud-lab-starter.git
cd oci-free-cloud-lab-starter

cp .env.example .env
# Edit .env — see the comments; every field is explained
```

Generate your admin console password hash:
```bash
python admin/hash_password.py
# Paste the output into ADMIN_PASSWORD_HASH in .env
```

### 3. Create OCI network resources (once)

**Windows:**
```
admin\setup-oci-network.bat
```
**Mac/Linux:**
```bash
bash admin/setup-oci-network.sh
```

This creates a VCN, subnet, internet gateway, route table, and security rules. Copy
the output `OCI_VCN_ID` and `OCI_SUBNET_ID` values into `.env`.

### 4. Launch the management VM

**Windows:**
```
admin\launch-management.bat
```
**Mac/Linux:**
```bash
bash admin/launch-management.sh
```

Wait 5–10 minutes for cloud-init to finish. Then:
- Update `OCI_MANAGEMENT_HOST` in `.env` with the public IP
- Update your DuckDNS record to point at that IP
- Open `https://your-domain.duckdns.org` — your admin console is up

### 5. Watch it build

The fleet orchestrator on the management VM handles everything from here. Check in
from your machine at any time:

**Windows:** `admin\check-all-vms.bat`
**Mac/Linux:** `bash admin/check-all-vms.sh`

Or just watch the admin console — it auto-refreshes every 60 seconds and shows live
VM state, heartbeat times, uptime, and links to log streams for each background service.
The console has a dark/light mode toggle, color palette presets, and a **Tools** page
where you can pick preset scripts or write your own and run them on any fleet VM
directly from the browser. It also includes:

- **Queue** (`/queue`) — view and manage the job queue across all VMs
- **Audit log** (`/audit`) — full trail of tool runs, service control, and API-enqueued jobs
- **Service control** — start/stop/restart individual services from the fleet page
- **Agent API** — `POST /enqueue` with a Bearer token so an LLM agent can queue work without a browser session

---

## Admin scripts reference

All scripts are in `admin/`. Run them from the repo root. `.bat` for Windows,
`.sh` for Mac/Linux — both call the same Python underneath.

| Script | What it does |
|---|---|
| `setup-oci-network` | Create VCN/subnet/security rules — run once |
| `launch-management` | Launch the management VM |
| `check-all-vms` | Query OCI state + SSH probe all VMs; updates `vm-profiles/` |
| `ssh-vm <name>` | SSH into any fleet VM (`management`, `worker`, `laboratory`) |
| `bootstrap-mgmt-vm` | Re-apply full config to a running management VM |
| `terminate-vm <name>` | Terminate a VM by name |
| `hash_password.py` | Generate `ADMIN_PASSWORD_HASH` for `.env` |

---

## Fleet layout

Three VMs, two shapes — all within Oracle's Always Free tier:

```
management   VM.Standard.E2.1.Micro   Orchestrator · admin console · heartbeat · crosswatch
worker       VM.Standard.E2.1.Micro   A1 lottery runner · general compute while waiting
laboratory   VM.Standard.A1.Flex      4 OCPU / 24 GB RAM — your main resource
```

VM configuration is in `fleet.json` (committed, safe to edit). Role-specific
cloud-init and setup scripts live in `fleet/<role>/`. The management VM uses Caddy
as a TLS-terminating reverse proxy in front of the admin console.

---

## What's automated vs. what you do manually

| Done for you automatically | You do this manually |
|---|---|
| All VM provisioning after management is up | Run `setup-oci-network` (once) |
| Repo clone + updates on each VM | Run `launch-management` (once) |
| All systemd service installation | Edit `.env` with your credentials |
| Fleet SSH key generation on management VM | Update DuckDNS record after management VM launches |
| TLS cert provisioning via Caddy + Let's Encrypt | Fill in `OCI_*_HOST` in `.env` as VMs come online |
| Peer health monitoring across all VMs | Wait for A1 Flex capacity (hours to days — out of your hands) |
| A1 Flex retry loop until capacity granted | — |
| Self-healing: relaunch terminated VMs | — |
| Self-healing: worker relaunches management if it goes down | — |
| Keepalive: periodic CPU activity to prevent idle reclamation | — |
| Resource alerts: ntfy if disk >80%, RAM <10%, or load spikes | — |

---

## Oracle Always Free — what you need to know

**The idle-reclamation problem.**  Oracle can terminate Always Free VMs that appear
idle (CPU below roughly 10% over 7 days). This repo solves it with real operational
work — health checks, log compression, OCI API calls via cron — rather than fake load.
The keepalive payload (`payload/keepalive/`) handles this automatically on every VM.

**The A1 Flex lottery.**  `VM.Standard.A1.Flex` is perpetually oversubscribed. Oracle
will almost certainly return "Out of host capacity" on your first launch attempt. The
fleet orchestrator retries silently and indefinitely. Most users win within 24–72 hours;
some wait longer. There is no way to accelerate this. Leave the orchestrator running.

**The 2-micro limit.**  Always Free includes exactly 2 `VM.Standard.E2.1.Micro`
instances total. `management` and `worker` each use one. If you terminate both and
try to relaunch simultaneously, Oracle may reject the second. Launch sequentially or
let the orchestrator handle it.

**Instance-principal auth.**  VMs authenticate to OCI using instance-principal — no
API key is stored on any VM. The `setup-oci-network` script creates the required
IAM dynamic group and policy for this.

---

## Layering your own workload

`payload/keepalive/` runs on every VM by default (user crontab, no sudo needed).

`payload/queue/` installs a 60-second systemd timer that runs the next queued job on
a given VM. The admin console's `/queue` page shows status across all VMs; `/enqueue`
lets you submit jobs from the browser or from an LLM agent via Bearer token. See
`payload/queue/install.sh`.

`payload/dashboard/` is an optional lab landing page for the laboratory VM — a simple
system stats viewer, running services list, and a curated board of self-hosted projects
that work well on A1 Flex ARM. Access it via SSH tunnel on port 8700. See
[payload/dashboard/README.md](payload/dashboard/README.md).

To add your own project:

1. Create `payload/<your-project>/`
2. Add an idempotent `install.sh`
3. Call it from `fleet/<role>/setup.sh`, or deploy it via `ssh-vm` after the fleet is up

See [payload/README.md](payload/README.md) for details.

**Connecting a separate project:** once the fleet is running, hit `/export` in the
admin console to get a block of env vars (public IPs, private IPs, SSH user). Paste
those into your project's `.env`. The fleet keeps managing itself independently.

---

## Security model

- `.env` is gitignored — never committed to your fork
- Password hashing is PBKDF2-SHA256 (260,000 rounds), computed locally;
  plaintext never leaves your machine
- VMs authenticate to OCI via instance-principal — no API key on any VM
- `vm-profiles/` state snapshots are gitignored (contain IP addresses)
- Admin console binds to `localhost:8765`; Caddy handles TLS and public exposure
- Session cookie is `HttpOnly; SameSite=Strict` with 7-day expiry
- Login is rate-limited: 5 attempts per IP per 15 minutes
- `GITHUB_TOKEN` needs only read (`repo`) scope — VMs never push anything

---

## Troubleshooting

**Admin console shows a TLS error on first load** — Caddy is still getting the
Let's Encrypt cert. Wait 60 seconds and reload.

**Admin console not loading at all** — check Caddy on the management VM:
```bash
bash admin/ssh-vm.sh management  # or: admin\ssh-vm.bat management (Windows)
sudo systemctl status caddy
sudo journalctl -u caddy -n 50
```

**OCI CLI errors on the management VM** — run `bootstrap-mgmt-vm` to re-apply setup.

**Worker or laboratory stuck as NOT FOUND** — the orchestrator is working on it. Check:
```bash
bash admin/ssh-vm.sh management  # or: admin\ssh-vm.bat management (Windows)
journalctl -u cloud-lab-orchestrator -f
```

**A1 Flex launch keeps failing** — completely normal. `Out of host capacity` is
Oracle's queue, not an error you can fix. The orchestrator is retrying. Check progress
in the orchestrator log above.

**VM terminated unexpectedly** — Oracle reclaimed it (idle or capacity pressure). The
orchestrator will relaunch worker and laboratory automatically. If the **management VM**
was reclaimed, the worker VM will detect this within 6 hours and attempt an automatic
relaunch. If both management and worker are down simultaneously, run `launch-management`
manually and then `bootstrap-mgmt-vm`.

---

## License

MIT. See [LICENSE](LICENSE).
