# vm-profiles/

Auto-generated OCI state snapshots. Written by `admin/check_oci_vm_status.py`.

Each file (`management.json`, `worker.json`, `laboratory.json`) is a JSON snapshot of
the most recently observed OCI state for that VM: OCID, lifecycle state, public IP,
private IP, shape, availability domain, and last-seen timestamp.

**These files are gitignored** — they contain IP addresses and change every check.
They are read by several admin scripts to look up VM OCIDs and IPs without an extra
OCI API call.

If you delete them, run `admin/check-all-vms` to regenerate.
