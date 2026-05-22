#!/usr/bin/env python3
"""
Terminate an OCI VM by name.

Reads instance OCIDs from vm-profiles/, calls OCI CLI to terminate,
and removes the profile file on success.

Usage:
  admin/terminate-vm.sh                 List running VMs and prompt which to terminate
  admin\\terminate-vm.bat               Same command from Command Prompt or PowerShell
  admin/terminate-vm.sh management      Terminate a specific VM (still prompts to confirm)
  admin/terminate-vm.sh worker --yes    Skip confirmation prompt
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT        = Path(__file__).resolve().parent.parent   # repo root
PROFILE_DIR = ROOT / "vm-profiles"
ENV_FILE    = ROOT / ".env"


def parse_env(path: Path) -> dict[str, str]:
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


def oci_env() -> dict[str, str]:
    e = os.environ.copy()
    e["OCI_CLI_SUPPRESS_FILE_PERMISSIONS_WARNING"] = "True"
    return e


def run_oci(args: list[str]) -> tuple[int, str, str]:
    oci = shutil.which("oci")
    if not oci:
        raise SystemExit("OCI CLI not found on PATH. Install it first.")
    result = subprocess.run(
        [oci, *args],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", env=oci_env(),
    )
    return result.returncode, result.stdout, result.stderr


def load_profiles() -> list[dict]:
    profiles = []
    for path in sorted(PROFILE_DIR.glob("*.json")):
        if path.name.startswith("_"):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        ocid = (data.get("instance") or {}).get("id", "")
        state = (data.get("instance") or {}).get("lifecycle-state", "UNKNOWN")
        name = path.stem
        if ocid:
            profiles.append({"name": name, "ocid": ocid, "state": state, "path": path})
    return profiles


def terminate(name: str, ocid: str, profile_path: Path, yes: bool) -> int:
    print(f"\nVM:    {name}")
    print(f"OCID:  {ocid}")
    print(f"State: {(json.loads(profile_path.read_text()).get('instance') or {}).get('lifecycle-state', 'UNKNOWN')}")
    print()

    if not yes:
        answer = input("Type the VM name to confirm termination (or Enter to cancel): ").strip()
        if answer != name:
            print("Cancelled.")
            return 0

    print(f"Terminating {name}...")
    code, stdout, stderr = run_oci([
        "compute", "instance", "terminate",
        "--instance-id", ocid,
        "--preserve-boot-volume", "false",
        "--force",
    ])

    if code != 0:
        print(f"OCI CLI error (exit {code}):")
        print(stderr or stdout)
        return code

    print(f"{name} termination initiated.")
    profile_path.unlink(missing_ok=True)
    print(f"Removed profile: {profile_path.name}")
    return 0


def main() -> int:
    args = sys.argv[1:]
    yes = "--yes" in args
    args = [a for a in args if a != "--yes"]

    profiles = load_profiles()
    if not profiles:
        print("No VM profiles found in vm-profiles/. Run check-all-vms first.")
        return 0

    if args:
        name = args[0]
        match = next((p for p in profiles if p["name"] == name), None)
        if not match:
            print(f"No profile found for '{name}'.")
            print("Known VMs:", ", ".join(p["name"] for p in profiles))
            return 1
        return terminate(match["name"], match["ocid"], match["path"], yes)

    print("Running VMs (from vm-profiles/):\n")
    for i, p in enumerate(profiles, 1):
        print(f"  {i}) {p['name']:<24} {p['state']}")
    print()

    choice = input("Enter number to terminate (or Enter to cancel): ").strip()
    if not choice:
        print("Cancelled.")
        return 0

    try:
        idx = int(choice) - 1
        if not 0 <= idx < len(profiles):
            raise ValueError
    except ValueError:
        print("Invalid selection.")
        return 1

    p = profiles[idx]
    return terminate(p["name"], p["ocid"], p["path"], yes)


if __name__ == "__main__":
    raise SystemExit(main())
