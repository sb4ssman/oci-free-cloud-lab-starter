#!/usr/bin/env python3
"""
One-time OCI network/IAM setup for the cloud-lab fleet.

Creates the VCN, internet gateway, public subnet, route table,
security list, and instance-principal IAM policy needed before
the management VM can orchestrate the rest of the fleet.

Safe to re-run — skips resources that already exist.
Prints the values you need to add to .env when done.

Usage:
  admin/setup-oci-network.sh
  admin\\setup-oci-network.bat
  ... --dry-run   Show what would be created; don't create anything
  ... --iam-only  Only create/update instance-principal IAM
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT     = Path(__file__).resolve().parent.parent   # repo root
ENV_FILE = ROOT / ".env"

VCN_CIDR    = "10.0.0.0/16"
SUBNET_CIDR = "10.0.0.0/24"
VCN_NAME    = "oracle-fleet-vcn"
SUBNET_NAME = "oracle-fleet-subnet-public"
IGW_NAME    = "oracle-fleet-igw"
SL_NAME     = "oracle-fleet-seclist"
RT_NAME     = "oracle-fleet-routetable"
DG_NAME     = "oracle-fleet-instances"
POLICY_NAME = "oracle-fleet-instance-principal-policy"


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


def run_oci(args: list[str], dry_run: bool = False) -> dict:
    if dry_run:
        print(f"  [dry-run] oci {' '.join(args)}")
        return {}
    oci = shutil.which("oci")
    if not oci:
        raise SystemExit("OCI CLI not found on PATH. Install it: https://docs.oracle.com/en-us/iaas/Content/API/SDKDocs/cliinstall.htm")
    result = subprocess.run(
        [oci, *args],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", env=oci_env(),
    )
    if result.returncode != 0:
        raise SystemExit(f"OCI CLI error:\n{result.stderr or result.stdout}")
    if not result.stdout.strip():
        return {}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"raw": result.stdout}


def find_existing(resource_list: list[dict], display_name: str) -> dict | None:
    for item in resource_list:
        if item.get("display-name") == display_name and item.get("lifecycle-state") != "TERMINATED":
            return item
    return None


def find_by_name(resource_list: list[dict], name: str) -> dict | None:
    for item in resource_list:
        if item.get("name") == name and item.get("lifecycle-state") not in ("DELETING", "DELETED"):
            return item
    return None


def tcp_rule(source: str, port: int, description: str) -> dict:
    return {
        "protocol": "6",
        "source": source,
        "isStateless": False,
        "tcpOptions": {"destinationPortRange": {"min": port, "max": port}},
        "description": description,
    }


def has_tcp_rule(rules: list[dict], source: str, port: int) -> bool:
    for rule in rules:
        opts = rule.get("tcp-options") or rule.get("tcpOptions") or {}
        port_range = opts.get("destination-port-range") or opts.get("destinationPortRange") or {}
        if (rule.get("protocol") == "6" and
                rule.get("source") == source and
                int(port_range.get("min", -1)) <= port <= int(port_range.get("max", -1))):
            return True
    return False


def normalize_security_rule(rule: dict) -> dict:
    normalized = {
        "protocol": rule.get("protocol"),
        "source": rule.get("source"),
        "isStateless": rule.get("isStateless", rule.get("is-stateless", False)),
        "description": rule.get("description", ""),
    }
    tcp_options = rule.get("tcpOptions") or rule.get("tcp-options")
    if tcp_options:
        port_range = tcp_options.get("destinationPortRange") or tcp_options.get("destination-port-range")
        if port_range:
            normalized["tcpOptions"] = {
                "destinationPortRange": {
                    "min": port_range.get("min"),
                    "max": port_range.get("max"),
                }
            }
    icmp_options = rule.get("icmpOptions") or rule.get("icmp-options")
    if icmp_options:
        normalized["icmpOptions"] = dict(icmp_options)
    return {k: v for k, v in normalized.items() if v not in (None, "")}


def ensure_security_list_rules(sl_id: str, ingress_rules: list[dict], egress_rules: list[dict], dry_run: bool) -> None:
    ingress_rules = [normalize_security_rule(rule) for rule in ingress_rules]
    egress_rules = [normalize_security_rule(rule) for rule in egress_rules]
    changed = False
    if not has_tcp_rule(ingress_rules, VCN_CIDR, 8765):
        print("  Adding management heartbeat/admin-console port 8765 from VCN CIDR...")
        ingress_rules.append(tcp_rule(VCN_CIDR, 8765, "Management heartbeat/admin console from fleet VCN"))
        changed = True

    if changed:
        run_oci([
            "network", "security-list", "update",
            "--security-list-id", sl_id,
            "--ingress-security-rules", json.dumps(ingress_rules),
            "--egress-security-rules", json.dumps(egress_rules),
            "--force",
        ], dry_run)
        print("  Security List updated.")


def tenancy_id_from_env(env: dict[str, str], compartment_id: str) -> str:
    tenancy_id = (
        env.get("OCI_TENANCY_ID", "")
        or env.get("OCI_TENANCY_OCID", "")
        or env.get("OCI_TENANCY", "")
    ).strip()
    if tenancy_id:
        return tenancy_id
    if compartment_id.startswith("ocid1.tenancy."):
        return compartment_id
    return ""


def ensure_instance_principal_iam(tenancy_id: str, compartment_id: str, dry_run: bool) -> None:
    if not tenancy_id:
        print("  Skipping IAM setup: set OCI_TENANCY_ID when OCI_COMPARTMENT_ID is a child compartment.")
        return

    matching_rule = f"ALL {{instance.compartment.id = '{compartment_id}'}}"
    statements = json.dumps([
        f"Allow dynamic-group {DG_NAME} to manage instance-family in compartment id {compartment_id}",
        f"Allow dynamic-group {DG_NAME} to use virtual-network-family in compartment id {compartment_id}",
        f"Allow dynamic-group {DG_NAME} to inspect all-resources in compartment id {compartment_id}",
    ])

    print("Checking instance-principal dynamic group...")
    dg_list = run_oci(["iam", "dynamic-group", "list",
                       "--compartment-id", tenancy_id, "--all"], dry_run)
    existing_dg = find_by_name(dg_list.get("data", []), DG_NAME)
    if existing_dg:
        dg_id = existing_dg["id"]
        print(f"  Found existing dynamic group: {dg_id}")
        if existing_dg.get("matching-rule") != matching_rule:
            print("  Updating dynamic group matching rule...")
            run_oci(["iam", "dynamic-group", "update",
                     "--dynamic-group-id", dg_id,
                     "--matching-rule", matching_rule,
                     "--force"], dry_run)
    else:
        print(f"  Creating dynamic group '{DG_NAME}'...")
        result = run_oci([
            "iam", "dynamic-group", "create",
            "--compartment-id", tenancy_id,
            "--name", DG_NAME,
            "--description", "Cloud Lab fleet VM instance principals",
            "--matching-rule", matching_rule,
        ], dry_run)
        dg_id = (result.get("data") or {}).get("id", "DRY_RUN_DYNAMIC_GROUP_OCID")
        print(f"  Created dynamic group: {dg_id}")

    print("Checking instance-principal policy...")
    policy_list = run_oci(["iam", "policy", "list",
                           "--compartment-id", tenancy_id, "--all"], dry_run)
    existing_policy = find_by_name(policy_list.get("data", []), POLICY_NAME)
    if existing_policy:
        policy_id = existing_policy["id"]
        print(f"  Found existing policy: {policy_id}")
        if existing_policy.get("statements") != json.loads(statements):
            print("  Updating policy statements...")
            run_oci(["iam", "policy", "update",
                     "--policy-id", policy_id,
                     "--statements", statements,
                     "--force"], dry_run)
    else:
        print(f"  Creating policy '{POLICY_NAME}'...")
        result = run_oci([
            "iam", "policy", "create",
            "--compartment-id", tenancy_id,
            "--name", POLICY_NAME,
            "--description", "Allow Cloud Lab fleet instances to manage lab resources",
            "--statements", statements,
        ], dry_run)
        policy_id = (result.get("data") or {}).get("id", "DRY_RUN_POLICY_OCID")
        print(f"  Created policy: {policy_id}")


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    iam_only = "--iam-only" in sys.argv

    env = parse_env(ENV_FILE)
    env.update(os.environ)

    compartment_id = env.get("OCI_COMPARTMENT_ID", "").strip()
    if not compartment_id:
        print("Error: OCI_COMPARTMENT_ID not set in .env")
        return 1

    if dry_run:
        print("-- DRY RUN -- nothing will be created --\n")

    print(f"Compartment: {compartment_id}\n")
    tenancy_id = tenancy_id_from_env(env, compartment_id)

    if iam_only:
        ensure_instance_principal_iam(tenancy_id, compartment_id, dry_run)
        print("\nIAM setup complete. It can take about 10-60 seconds for OCI policy changes to propagate.")
        return 0

    # ── 1. VCN ────────────────────────────────────────────────────────────────
    print("Checking VCN...")
    vcn_list = run_oci(["network", "vcn", "list", "--compartment-id", compartment_id, "--all"])
    existing_vcn = find_existing(vcn_list.get("data", []), VCN_NAME)

    if existing_vcn:
        vcn_id = existing_vcn["id"]
        print(f"  Found existing VCN: {vcn_id}")
    else:
        print(f"  Creating VCN '{VCN_NAME}' ({VCN_CIDR})...")
        result = run_oci([
            "network", "vcn", "create",
            "--compartment-id", compartment_id,
            "--cidr-block", VCN_CIDR,
            "--display-name", VCN_NAME,
            "--dns-label", "fleetvcn",
        ], dry_run)
        vcn_id = (result.get("data") or {}).get("id", "DRY_RUN_VCN_OCID")
        print(f"  Created VCN: {vcn_id}")

    # ── 2. Internet Gateway ───────────────────────────────────────────────────
    print("Checking Internet Gateway...")
    igw_list = run_oci(["network", "internet-gateway", "list",
                        "--compartment-id", compartment_id,
                        "--vcn-id", vcn_id, "--all"])
    existing_igw = find_existing(igw_list.get("data", []), IGW_NAME)

    if existing_igw:
        igw_id = existing_igw["id"]
        print(f"  Found existing IGW: {igw_id}")
    else:
        print(f"  Creating Internet Gateway '{IGW_NAME}'...")
        result = run_oci([
            "network", "internet-gateway", "create",
            "--compartment-id", compartment_id,
            "--vcn-id", vcn_id,
            "--is-enabled", "true",
            "--display-name", IGW_NAME,
        ], dry_run)
        igw_id = (result.get("data") or {}).get("id", "DRY_RUN_IGW_OCID")
        print(f"  Created IGW: {igw_id}")

    # ── 3. Route Table ────────────────────────────────────────────────────────
    print("Checking Route Table...")
    rt_list = run_oci(["network", "route-table", "list",
                       "--compartment-id", compartment_id,
                       "--vcn-id", vcn_id, "--all"])
    existing_rt = find_existing(rt_list.get("data", []), RT_NAME)

    if existing_rt:
        rt_id = existing_rt["id"]
        print(f"  Found existing Route Table: {rt_id}")
    else:
        print(f"  Creating Route Table '{RT_NAME}' with default route → IGW...")
        route_rules = json.dumps([{
            "cidrBlock": "0.0.0.0/0",
            "networkEntityId": igw_id,
        }])
        result = run_oci([
            "network", "route-table", "create",
            "--compartment-id", compartment_id,
            "--vcn-id", vcn_id,
            "--display-name", RT_NAME,
            "--route-rules", route_rules,
        ], dry_run)
        rt_id = (result.get("data") or {}).get("id", "DRY_RUN_RT_OCID")
        print(f"  Created Route Table: {rt_id}")

    # ── 4. Security List ──────────────────────────────────────────────────────
    print("Checking Security List...")
    sl_list = run_oci(["network", "security-list", "list",
                       "--compartment-id", compartment_id,
                       "--vcn-id", vcn_id, "--all"])
    existing_sl = find_existing(sl_list.get("data", []), SL_NAME)

    if existing_sl:
        sl_id = existing_sl["id"]
        print(f"  Found existing Security List: {sl_id}")
        ensure_security_list_rules(
            sl_id,
            existing_sl.get("ingress-security-rules", []),
            existing_sl.get("egress-security-rules", []),
            dry_run,
        )
    else:
        print(f"  Creating Security List '{SL_NAME}'...")
        ingress_rules = json.dumps([
            tcp_rule("0.0.0.0/0", 22, "SSH"),
            tcp_rule("0.0.0.0/0", 80, "HTTP"),
            tcp_rule("0.0.0.0/0", 443, "HTTPS"),
            tcp_rule(VCN_CIDR, 8765, "Management heartbeat/admin console from fleet VCN"),
            {   # ICMP ping
                "protocol": "1", "source": "0.0.0.0/0", "isStateless": False,
                "icmpOptions": {"type": 8},
                "description": "ICMP ping",
            },
        ])
        egress_rules = json.dumps([
            {   # All outbound
                "protocol": "all", "destination": "0.0.0.0/0", "isStateless": False,
                "description": "All outbound",
            },
        ])
        result = run_oci([
            "network", "security-list", "create",
            "--compartment-id", compartment_id,
            "--vcn-id", vcn_id,
            "--display-name", SL_NAME,
            "--ingress-security-rules", ingress_rules,
            "--egress-security-rules", egress_rules,
        ], dry_run)
        sl_id = (result.get("data") or {}).get("id", "DRY_RUN_SL_OCID")
        print(f"  Created Security List: {sl_id}")

    # ── 5. Subnet ─────────────────────────────────────────────────────────────
    print("Checking Subnet...")
    subnet_list = run_oci(["network", "subnet", "list",
                           "--compartment-id", compartment_id,
                           "--vcn-id", vcn_id, "--all"])
    existing_subnet = find_existing(subnet_list.get("data", []), SUBNET_NAME)

    if existing_subnet:
        subnet_id = existing_subnet["id"]
        print(f"  Found existing Subnet: {subnet_id}")
    else:
        print(f"  Creating public Subnet '{SUBNET_NAME}' ({SUBNET_CIDR})...")
        result = run_oci([
            "network", "subnet", "create",
            "--compartment-id", compartment_id,
            "--vcn-id", vcn_id,
            "--cidr-block", SUBNET_CIDR,
            "--display-name", SUBNET_NAME,
            "--dns-label", "fleetpub",
            "--route-table-id", rt_id,
            "--security-list-ids", json.dumps([sl_id]),
        ], dry_run)
        subnet_id = (result.get("data") or {}).get("id", "DRY_RUN_SUBNET_OCID")
        print(f"  Created Subnet: {subnet_id}")

    # ── 6. Availability Domain ────────────────────────────────────────────────
    print("\nFetching availability domains...")
    ad_list = run_oci(["iam", "availability-domain", "list",
                       "--compartment-id", compartment_id])
    ads = [item["name"] for item in (ad_list.get("data") or [])]
    ad = ads[0] if ads else "UNKNOWN"
    print(f"  Availability domain: {ad}")
    if len(ads) > 1:
        print(f"  (All ADs: {', '.join(ads)} — using first)")

    # ── 7. Instance-principal IAM ────────────────────────────────────────────
    print()
    ensure_instance_principal_iam(tenancy_id, compartment_id, dry_run)

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n" + "─" * 60)
    print("Network setup complete. Add these to .env:\n")
    print(f"OCI_SUBNET_ID={subnet_id}")
    print(f"OCI_AVAILABILITY_DOMAIN={ad}")
    print()
    if not dry_run:
        print("Also update your .env file now — then run launch-management.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
