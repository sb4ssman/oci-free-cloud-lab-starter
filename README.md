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
      You need this hostname for the admin console's HTTPS certificate.
- [ ] **ntfy topic name** *(optional but strongly recommended)* — free at [ntfy.sh](https://ntfy.sh/).
      Pick any unique string; the fleet sends alerts directly to ntfy when management is
      unreachable. Leave blank in `.env` to disable.
- [ ] **GitHub account** — fork this repo so VMs can clone your copy.
      Public forks can leave `GITHUB_TOKEN` blank. Private forks need a fine-grained,
      read-only token limited to this repository.
- [ ] **UptimeRobot account** *(optional)* — free at [uptimerobot.com](https://uptimerobot.com/).
      Create an HTTPS monitor targeting `https://your-domain.duckdns.org/health` and add a
      webhook alert to `https://ntfy.sh/<your-topic>`. This is the external watchdog — the
      only layer that fires even if all three VMs go down simultaneously.

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
# Edit .env — see the comments; every field is explained.
# Set FLEET_REPO to owner/repo, for example: YOUR_USERNAME/oci-free-cloud-lab-starter
```

Generate your admin console password hash:
```bash
python admin/hash_password.py
# Paste the output into ADMIN_PASSWORD_HASH in .env
# Then leave ADMIN_PASSWORD blank.
```

Generate API tokens for the queue endpoint and internal fleet heartbeats:
```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
# Run twice; paste one value into QUEUE_API_KEY and one into FLEET_HEARTBEAT_TOKEN.
```

### 3. Create OCI network resources (once)

Run the wrapper for your shell from the repo root:

```bash
bash admin/setup-oci-network.sh
```

```powershell
admin\setup-oci-network.bat
```

This creates a VCN, subnet, internet gateway, route table, and security rules. Copy
the output `OCI_VCN_ID` and `OCI_SUBNET_ID` values into `.env`.

### 4. Launch the management VM

Run the matching wrapper:

```bash
bash admin/launch-management.sh
```

```powershell
admin\launch-management.bat
```

Wait 5–10 minutes for cloud-init to finish. Then:
- Update `OCI_MANAGEMENT_HOST` in `.env` with the public IP
- Update your DuckDNS record to point at that IP
- Open `https://your-domain.duckdns.org` — your admin console is up

### 5. Watch it build

The fleet orchestrator on the management VM handles everything from here. Check in
from your machine at any time:

```bash
bash admin/check-all-vms.sh
```

```powershell
admin\check-all-vms.bat
```

Or just watch the admin console — it auto-refreshes every 60 seconds and shows live
VM state, heartbeat times, uptime, and links to log streams for each background service.
The console has a dark/light mode toggle, color palette presets, and a **Tools** page
where you can pick preset scripts or write your own and run them on any fleet VM
directly from the browser. It also includes:

- **Queue** (`/queue`) — view and manage the job queue across all VMs
- **Audit log** (`/audit`) — full trail of tool runs, service control, and API-enqueued jobs
- **Service control** — start/stop/restart individual services from the fleet page
- **Agent API** — `POST /enqueue` with `Authorization: Bearer <QUEUE_API_KEY>` so an LLM agent can queue work without a browser session

---

## Admin scripts reference

All scripts are in `admin/`. Run them from the repo root. Use the `.sh` wrapper
from POSIX shells and the `.bat` wrapper from Command Prompt or PowerShell; both
call the same Python underneath.

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


## Oracle Always Free — what you need to know

**The idle-reclamation problem.**  Oracle can terminate Always Free VMs that appear
idle (CPU below roughly 10% over 7 days). This repo solves it with real operational
work — health checks, log compression, OCI API calls via cron — rather than fake load.
The keepalive payload (`payload/keepalive/`) handles this automatically on every VM.

**Getting the A1.Flex instance.**  `VM.Standard.A1.Flex` is perpetually oversubscribed
in most regions. Oracle frequently returns "Out of host capacity" on direct launch
attempts. The launcher handles this two ways, in order:

1. **A2→A1 shape conversion** *(trial window or PAYG accounts only)* — launches a
   `VM.Standard.A2.Flex` instance first (a different ARM shape with better capacity
   availability), then immediately converts its shape to `VM.Standard.A1.Flex` via the
   OCI API. This bypasses the standard capacity queue and typically completes in minutes.
   A community-confirmed technique as of early 2026; not officially documented by Oracle.

2. **Standard retry lottery** — if A2 conversion isn't available or fails, the
   orchestrator retries the direct A1.Flex launch indefinitely. Most accounts win
   capacity within 24–72 hours; some wait longer.

Both paths are automatic. You don't choose between them — the launcher tries A2→A1
first and falls back silently.

**Cost:** `VM.Standard.A2.Flex` is not an Always Free shape; it accrues charges billed
per second. The brief A2 existence costs a fraction of a cent and is terminated
immediately on any failure. If your account is **pure Always Free** (trial expired, no
PAYG), the A2 launch is rejected by Oracle with a `LimitExceeded` error and the
standard lottery takes over — no charge, no action needed, the log explains what
happened. To disable A2 conversion entirely, set `"try_a2_conversion": false` in
`admin/profiles/laboratory.json`.

**A2.Flex does not count against your 2-micro limit.** It is a separate shape family.
Your existing management and worker VMs are unaffected.

**After a successful A2→A1 conversion**, the laboratory VM is bare Ubuntu — the
shape-change reboot interrupts cloud-init before it completes. Bootstrap it via the
admin console **Tools** page: select the `laboratory` VM and run:
```bash
git clone https://github.com/YOUR_USERNAME/YOUR_FORK.git ~/cloud-lab
bash ~/cloud-lab/fleet/laboratory/setup.sh
```
Or SSH directly: `bash admin/ssh-vm.sh laboratory`

**Multi-AD rotation.**  In regions with multiple availability domains (e.g., us-ashburn-1
has 3 ADs), the launcher automatically discovers all ADs and rotates round-robin on each
attempt. This roughly triples your chances of winning capacity on any given cycle.
If you leave `OCI_AVAILABILITY_DOMAIN` blank in `.env`, rotation is automatic.
Set it to a specific AD name to lock to one domain. Single-AD regions (e.g., us-sanjose-1)
work unchanged — rotation just uses the one domain on every attempt.

**The 2-micro limit.**  Always Free includes exactly 2 `VM.Standard.E2.1.Micro`
instances total. `management` and `worker` each use one. If you terminate both and
try to relaunch simultaneously, Oracle may reject the second. Launch sequentially or
let the orchestrator handle it.

**Instance-principal auth.**  VMs authenticate to OCI using instance-principal — no
OCI API key is stored on any VM. The `setup-oci-network` script creates the required
IAM dynamic group and policy. The policy is scoped to instance management, VCN use,
and resource inspection inside the configured compartment.

**GitHub repo access.**  Public forks do not need `GITHUB_TOKEN`. Private forks need
a read-only token so cloud-init can clone the repo; treat that token as a VM-resident
secret and rotate it if a VM is compromised.

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

## SSH key persistence across relaunches

By default each VM generates a fresh `fleet.key` on first boot. When management is
relaunched (e.g., after Oracle reclaims it), the new key breaks SSH trust with worker
and lab — they still have the old key in `authorized_keys`. Three options, used in order:

**Option 1 — OCI Vault (recommended).**  Store one fixed fleet private key in an OCI
Vault secret. Every VM retrieves the same key at boot via instance-principal auth, so
the mesh survives any number of relaunches. One-time setup:

```bash
# 1. Create a Vault + Master Encryption Key in the OCI console, then:

# 2. Store the fleet private key as a secret
oci vault secret create-base64 \
    --vault-id <vault-ocid> --key-id <mek-ocid> \
    --compartment-id <compartment-ocid> \
    --secret-name fleet-private-key \
    --secret-content-content "$(base64 -w 0 < ~/.ssh/fleet.key)"

# 3. Create a dynamic group covering all instances in your compartment
oci iam dynamic-group create \
    --name fleet-vms --compartment-id <tenancy-ocid> \
    --matching-rule "ALL {instance.compartment.id = '<compartment-ocid>'}"

# 4. Grant that group vault-read access
oci iam policy create \
    --name fleet-vault-read --compartment-id <compartment-ocid> \
    --statements '["Allow dynamic-group fleet-vms to read secret-family in compartment id <compartment-ocid>"]'

# 5. Paste the secret OCID into .env:
#    FLEET_OCI_VAULT_SECRET_OCID=ocid1.vaultsecret.oc1...
```

**Option 2 — Base64 env var (fallback).**  Set `FLEET_PRIVATE_KEY_B64` in `.env`:
```bash
# Linux/macOS:
base64 -w 0 < ~/.ssh/fleet.key

# PowerShell:
[Convert]::ToBase64String([IO.File]::ReadAllBytes("$HOME\.ssh\fleet.key")) -replace "`r|`n",""
```
The key is embedded in the cloud-init user-data at launch time. Simpler than Vault
but the key appears in OCI's launch metadata. Rotate it if a VM is compromised.

**Option 3 — Admin recovery key (strongly recommended alongside either option above).**
Set `ADMIN_SSH_PUBLIC_KEY` in `.env` to your personal public key:
```bash
cat ~/.ssh/id_ed25519.pub
# Paste the output into ADMIN_SSH_PUBLIC_KEY in .env
```
This key is added to `authorized_keys` on every VM at boot — independent of the fleet
key. Even after a management relaunch that scrambles the fleet key, you can SSH directly
into any VM with your own key to repair the situation.

---

## Security model

- `.env` is gitignored — never committed to your fork
- Password hashing is PBKDF2-SHA256 (260,000 rounds), computed locally;
  plaintext never leaves your machine
- VMs authenticate to OCI via instance-principal — no API key on any VM
- `vm-profiles/` state snapshots are gitignored (contain IP addresses)
- Admin console listens on the management VM; Caddy handles TLS and public exposure
- Session cookie is `HttpOnly; Secure; SameSite=Strict` with 7-day expiry
- Login is rate-limited: 5 attempts per IP per 15 minutes
- `QUEUE_API_KEY` can queue shell commands on fleet VMs; store it like a password
- `FLEET_HEARTBEAT_TOKEN` protects VM-to-management heartbeat writes
- The Tools page and queue intentionally execute shell commands after authentication
- `GITHUB_TOKEN` is optional for public forks; private forks should use a fine-grained read-only token
- `FLEET_PRIVATE_KEY_B64` embeds the private key in cloud-init user-data — only use it
  if OCI Vault isn't set up; rotate immediately if any VM is compromised
- `FLEET_OCI_VAULT_SECRET_OCID` + instance-principal is the recommended secret delivery path

---

## Troubleshooting

**Admin console shows a TLS error on first load** — Caddy is still getting the
Let's Encrypt cert. Wait 60 seconds and reload.

**Admin console not loading at all** — check Caddy on the management VM:
```bash
bash admin/ssh-vm.sh management
sudo systemctl status caddy
sudo journalctl -u caddy -n 50
```
PowerShell equivalent: `admin\ssh-vm.bat management`

**OCI CLI errors on the management VM** — run `bootstrap-mgmt-vm` to re-apply setup.

**Worker or laboratory stuck as NOT FOUND** — the orchestrator is working on it. Check:
```bash
bash admin/ssh-vm.sh management
journalctl -u cloud-lab-orchestrator -f
```
PowerShell equivalent: `admin\ssh-vm.bat management`

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
