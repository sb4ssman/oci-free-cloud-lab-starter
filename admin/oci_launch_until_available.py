#!/usr/bin/env python3
"""
Retry OCI instance launch from a JSON profile.

This intentionally uses the OCI CLI instead of the Python OCI SDK. That keeps
the dependency story simple: Python standard library + the already-installed
`oci` command.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
DEFAULT_LOG = ROOT / "retry.log"


class RetryError(RuntimeError):
    pass


def log(message: str, log_file: Path = DEFAULT_LOG) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}"
    print(line, flush=True)
    with log_file.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value

    return values


def load_env(paths: list[Path]) -> dict[str, str]:
    values: dict[str, str] = {}
    for path in paths:
        values.update(parse_env_file(path))
    values.update(os.environ)
    return values


def resolve_value(value: Any, env: dict[str, str], name: str) -> Any:
    if isinstance(value, dict) and "env" in value:
        env_name = value["env"]
        if env_name in env and env[env_name] != "":
            return env[env_name]
        if "default" in value:
            return value["default"]
        raise RetryError(f"Missing required env value for {name}: {env_name}")
    return value


def resolve_profile(profile: dict[str, Any], env: dict[str, str]) -> dict[str, Any]:
    resolved = {}
    for key, value in profile.items():
        resolved[key] = resolve_value(value, env, key)
    return resolved


def expand_path(value: str) -> Path:
    expanded = value
    for name, env_value in sorted(os.environ.items(), key=lambda item: len(item[0]), reverse=True):
        expanded = expanded.replace(f"$env:{name}", env_value)
        expanded = expanded.replace(f"%{name}%", env_value)
    return Path(os.path.expandvars(expanded)).expanduser()


def as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def oci_env(auth_mode: str) -> dict[str, str]:
    child_env = os.environ.copy()
    child_env["OCI_CLI_SUPPRESS_FILE_PERMISSIONS_WARNING"] = "True"

    if auth_mode == "instance_principal":
        child_env["OCI_CLI_AUTH"] = "instance_principal"
    elif auth_mode == "api_key":
        child_env.pop("OCI_CLI_AUTH", None)
    else:
        raise RetryError("oci_auth must be 'api_key' or 'instance_principal'")

    child_env["PYTHONWARNINGS"] = "ignore::FutureWarning"
    return child_env


def oci_executable() -> str:
    oci = shutil.which("oci")
    if not oci and platform.system() == "Windows":
        known = Path(r"C:\Program Files (x86)\Oracle\oci_cli\oci.exe")
        if known.exists():
            oci = str(known)
    if not oci:
        raise RetryError("OCI CLI was not found on PATH.")
    return oci


def redacted_command(args: list[str]) -> str:
    safe_args: list[str] = []
    redact_next = False
    for arg in args:
        if redact_next:
            safe_args.append("***")
            redact_next = False
            continue
        safe_args.append(arg)
        if arg in {"--auth-purpose", "--security-token-file"}:
            redact_next = True
    return "oci " + " ".join(safe_args)


def run_oci(
    args: list[str],
    *,
    auth_mode: str,
    timeout_seconds: int,
    heartbeat_seconds: int,
    label: str = "OCI command",
) -> tuple[int, str, str, bool]:
    oci = oci_executable()
    log(f"{label}: using OCI CLI at {oci}")
    log(f"{label}: running {redacted_command(args)}")

    process = subprocess.Popen(
        [oci, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=oci_env(auth_mode),
    )

    started = time.monotonic()
    next_heartbeat = started + heartbeat_seconds
    timed_out = False

    while process.poll() is None:
        time.sleep(1)
        now = time.monotonic()
        elapsed = int(now - started)

        if heartbeat_seconds > 0 and now >= next_heartbeat:
            log(f"{label} still running after {elapsed}s (PID {process.pid}); timeout at {timeout_seconds}s.")
            next_heartbeat = now + heartbeat_seconds

        if elapsed >= timeout_seconds:
            timed_out = True
            process.kill()
            break

    stdout, stderr = process.communicate()
    code = 124 if timed_out else int(process.returncode or 0)
    if timed_out and not stderr:
        stderr = f"Timed out after {timeout_seconds}s: oci {' '.join(args)}"
    log(f"{label}: completed with exit code {code}.")
    return code, stdout or "", stderr or "", timed_out


def run_oci_json(
    args: list[str],
    *,
    auth_mode: str,
    timeout_seconds: int,
    heartbeat_seconds: int,
    label: str = "OCI command",
) -> Any:
    code, stdout, stderr, _ = run_oci(
        args,
        auth_mode=auth_mode,
        timeout_seconds=timeout_seconds,
        heartbeat_seconds=heartbeat_seconds,
        label=label,
    )
    if code != 0:
        raise RetryError((stdout + " " + stderr).strip())
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RetryError(f"OCI returned non-JSON output: {exc}\nstdout: {stdout}\nstderr: {stderr}") from exc


def availability_domain(config: dict[str, Any]) -> str:
    configured = str(config.get("availability_domain", "")).strip()
    data = run_oci_json(
        ["iam", "availability-domain", "list", "--compartment-id", config["compartment_id"]],
        auth_mode=config["oci_auth"],
        timeout_seconds=int(config["oci_timeout_seconds"]),
        heartbeat_seconds=int(config["oci_heartbeat_seconds"]),
        label="Availability-domain lookup",
    )
    names = [item["name"] for item in data.get("data", [])]
    if not names:
        raise RetryError("OCI returned no availability domains.")
    if not configured:
        log(f"Auto-selected AD: {names[0]}")
        return names[0]
    if configured in names:
        log(f"AD verified by OCI: {configured}")
        return configured
    if len(names) == 1:
        log(f"Configured AD '{configured}' is invalid. Using OCI-discovered AD: {names[0]}")
        return names[0]
    raise RetryError(f"Configured AD '{configured}' is not in OCI's list: {', '.join(names)}")


def oci_connectivity_probe(config: dict[str, Any]) -> None:
    """Make a small authenticated OCI API call so logs show auth/network health."""
    data = run_oci_json(
        ["iam", "availability-domain", "list", "--compartment-id", config["compartment_id"]],
        auth_mode=config["oci_auth"],
        timeout_seconds=60,
        heartbeat_seconds=int(config["oci_heartbeat_seconds"]),
        label="OCI auth/connectivity probe",
    )
    count = len(data.get("data", []))
    log(f"OCI auth/connectivity probe OK ({count} availability domain(s) visible).")


def latest_image_id(config: dict[str, Any]) -> str:
    log(f"Looking up latest {config['operating_system']} {config['operating_system_version']} image for {config['shape']}...")
    data = run_oci_json(
        [
            "compute",
            "image",
            "list",
            "--compartment-id",
            config["compartment_id"],
            "--operating-system",
            config["operating_system"],
            "--operating-system-version",
            config["operating_system_version"],
            "--shape",
            config["shape"],
            "--sort-by",
            "TIMECREATED",
            "--sort-order",
            "DESC",
            "--all",
        ],
        auth_mode=config["oci_auth"],
        timeout_seconds=int(config["oci_timeout_seconds"]),
        heartbeat_seconds=int(config["oci_heartbeat_seconds"]),
        label="Image lookup",
    )
    images = data.get("data", [])
    if not images:
        raise RetryError(f"No matching images returned for {config['shape']}.")
    return images[0]["id"]


def validate_ssh_key(path: Path) -> None:
    if not path.exists():
        raise RetryError(f"SSH public key not found: {path}")
    text = path.read_text(encoding="utf-8").strip()
    if not re.match(r"^(ssh-rsa|ssh-ed25519|ecdsa-sha2-nistp\d+)\s+[A-Za-z0-9+/=]+(\s+.*)?$", text):
        raise RetryError(f"SSH public key does not look like an OpenSSH public key: {path}")


def _hash_password(password: str) -> str:
    import hashlib
    import secrets as _secrets
    salt = _secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 260000).hex()
    return f"sha256:260000:{salt}:{h}"


def build_user_data(config: dict[str, Any], env: dict[str, str]) -> str | None:
    """Return a cloud-init user-data script string, or None if no template is configured.

    config['cloud_init_template'] is a path relative to oracle-tools/ root, or absolute.
    ${VAR} placeholders are substituted from env at render time — secrets never appear
    in profile JSON or git history.

    If ADMIN_PASSWORD is in env and ADMIN_PASSWORD_HASH is not, the hash is computed
    locally so plaintext never enters the VM user-data.
    """
    template_value = config.get("cloud_init_template", "")
    if not template_value:
        return None

    template_path = expand_path(str(template_value))
    if not template_path.is_absolute():
        template_path = ROOT.parent / template_path   # relative to oracle-tools/
    if not template_path.exists():
        raise RetryError(f"cloud_init_template not found: {template_path}")

    text = template_path.read_text(encoding="utf-8")

    # Augment env with derived values before substitution.
    env = dict(env)
    if "ADMIN_PASSWORD" in env and "ADMIN_PASSWORD_HASH" not in env:
        env["ADMIN_PASSWORD_HASH"] = _hash_password(env["ADMIN_PASSWORD"])
    for optional_key in (
        "DASHBOARD_DATABASE_URL",
        "DASHBOARD_SCHWAB_APP_KEY",
        "DASHBOARD_SCHWAB_APP_SECRET",
        "DASHBOARD_SCHWAB_MARKET_APP_KEY",
        "DASHBOARD_SCHWAB_MARKET_APP_SECRET",
        "DASHBOARD_PLAID_CLIENT_ID",
        "DASHBOARD_PLAID_SECRET",
        "DASHBOARD_PLAID_ENV",
        "DASHBOARD_COINGECKO_API_KEY",
    ):
        env.setdefault(optional_key, "")

    # Simple ${VAR} substitution from env. Unknown placeholders are left as-is.
    def replace(match: re.Match) -> str:
        key = match.group(1)
        return env.get(key, match.group(0))

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", replace, text)


def launch_args(
    config: dict[str, Any], image_id: str, ad: str, env: dict[str, str]
) -> tuple[list[str], list[Path]]:
    """Build the OCI CLI launch argument list. Returns (args, temp_files_to_clean)."""
    args = [
        "compute",
        "instance",
        "launch",
        "--compartment-id",
        config["compartment_id"],
        "--availability-domain",
        ad,
        "--shape",
        config["shape"],
        "--image-id",
        image_id,
        "--subnet-id",
        config["subnet_id"],
        "--ssh-authorized-keys-file",
        str(Path(config["ssh_public_key_path"]).expanduser()),
        "--display-name",
        config["instance_display_name"],
        "--assign-public-ip",
        str(bool(config.get("assign_public_ip", True))).lower(),
    ]

    temp_files: list[Path] = []

    if str(config["shape"]).endswith(".Flex"):
        shape_config = {
            "ocpus": float(config["ocpus"]),
            "memoryInGBs": float(config["memory_in_gbs"]),
        }
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        with handle:
            json.dump(shape_config, handle)
        shape_config_path = Path(handle.name)
        temp_files.append(shape_config_path)
        args.extend(["--shape-config", f"file://{shape_config_path}"])

    user_data_script = build_user_data(config, env)
    if user_data_script:
        handle = tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False, encoding="utf-8", newline="\n")
        with handle:
            handle.write(user_data_script)
        ud_path = Path(handle.name)
        temp_files.append(ud_path)
        args.extend(["--user-data-file", str(ud_path)])
        log("Cloud-init user-data attached as raw shell script.")

    return args, temp_files


def search_query_literal(value: str) -> str:
    """Escape a value for OCI structured-search single-quoted string syntax."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def resource_search_instance(config: dict[str, Any]) -> dict[str, Any] | None:
    """Find an instance by display name via OCI Resource Search."""
    name = config["instance_display_name"]
    query = f"query instance resources where displayName = '{search_query_literal(name)}'"
    data = run_oci_json(
        ["search", "resource", "structured-search",
         "--query-text", query,
         "--limit", "20"],
        auth_mode=config["oci_auth"],
        timeout_seconds=60,
        heartbeat_seconds=int(config.get("oci_heartbeat_seconds", 15)),
        label="Pre-flight resource search",
    )
    for item in data.get("data", {}).get("items", []):
        display_name = item.get("display-name") or item.get("displayName")
        lifecycle_state = item.get("lifecycle-state") or item.get("lifecycleState") or "UNKNOWN"
        identifier = item.get("identifier") or item.get("id")
        if display_name == name and lifecycle_state not in ("TERMINATING", "TERMINATED", "DELETED"):
            return {
                "id": identifier,
                "display-name": display_name,
                "lifecycle-state": lifecycle_state,
                "compartment-id": item.get("compartment-id") or item.get("compartmentId"),
            }
    return None


def find_active_instance(config: dict[str, Any]) -> dict[str, Any] | None:
    """Return an existing non-terminated instance with this display name, if any."""
    log(f"Pre-flight: searching OCI for existing '{config['instance_display_name']}'.")
    return resource_search_instance(config)


def instance_already_active(config: dict[str, Any]) -> bool:
    """Return True if an instance with this display name already exists."""
    item = find_active_instance(config)
    if item:
        log(f"Pre-flight: '{config['instance_display_name']}' already exists "
            f"({item.get('lifecycle-state', 'UNKNOWN')}) — skipping launch.")
        return True
    return False


def wait_for_active_instance(config: dict[str, Any], timeout_seconds: int = 600) -> dict[str, Any] | None:
    """Poll OCI for a possibly-created instance after a client timeout or retryable error."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            item = find_active_instance(config)
        except Exception as exc:
            log(f"Post-launch status check failed ({exc}); retrying before launching again.")
            item = None
        if item:
            log(f"Found '{config['instance_display_name']}' in OCI "
                f"({item.get('lifecycle-state', 'UNKNOWN')}) — stopping retry loop.")
            return item
        time.sleep(15)
    return None


def public_ip_for_instance(config: dict[str, Any], instance_id: str) -> str:
    """Return the public IP for an instance, or an empty string if OCI has not attached it yet."""
    attachments = run_oci_json(
        ["compute", "vnic-attachment", "list",
         "--compartment-id", config["compartment_id"],
         "--instance-id", instance_id,
         "--all"],
        auth_mode=config["oci_auth"],
        timeout_seconds=60,
        heartbeat_seconds=0,
        label="VNIC attachment lookup",
    ).get("data", [])
    for attachment in attachments:
        vnic_id = attachment.get("vnic-id", "")
        if not vnic_id:
            continue
        vnic = run_oci_json(
            ["network", "vnic", "get", "--vnic-id", vnic_id],
            auth_mode=config["oci_auth"],
            timeout_seconds=60,
            heartbeat_seconds=0,
            label="VNIC public IP lookup",
        ).get("data", {})
        public_ip = vnic.get("public-ip") or ""
        if public_ip:
            return public_ip
    return ""


def wait_for_public_ip(config: dict[str, Any], instance_id: str, timeout_seconds: int = 300) -> str:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            public_ip = public_ip_for_instance(config, instance_id)
            if public_ip:
                log(f"Public IP   : {public_ip}")
                return public_ip
        except Exception as exc:
            log(f"Public IP lookup failed ({exc}); retrying.")
        time.sleep(10)
    log("Public IP lookup timed out.")
    return ""


def update_duckdns(env: dict[str, str], public_ip: str) -> None:
    """Update DuckDNS when ADMIN_DOMAIN and DUCKDNS_TOKEN are configured."""
    domain = env.get("ADMIN_DOMAIN", "").strip()
    token = env.get("DUCKDNS_TOKEN", "").strip()
    if not domain or not token or not public_ip:
        return

    suffix = ".duckdns.org"
    if not domain.lower().endswith(suffix):
        log(f"ADMIN_DOMAIN is not a DuckDNS hostname ({domain}); skipping DuckDNS update.")
        return

    subdomain = domain[:-len(suffix)]
    query = urllib.parse.urlencode({"domains": subdomain, "token": token, "ip": public_ip})
    try:
        with urllib.request.urlopen(f"https://www.duckdns.org/update?{query}", timeout=15) as response:
            body = response.read().decode("utf-8", errors="replace").strip()
        if body.upper() == "OK":
            log(f"DuckDNS updated: {domain} -> {public_ip}")
        else:
            log(f"DuckDNS update returned {body!r} for {domain}.")
    except Exception as exc:
        log(f"DuckDNS update failed: {exc}")


def finish_success(config: dict[str, Any], env: dict[str, str], instance_id: str, console_url: str) -> int:
    public_ip = wait_for_public_ip(config, instance_id)
    update_duckdns(env, public_ip)
    admin_domain = env.get("ADMIN_DOMAIN", "").strip()
    if admin_domain:
        log(f"Admin URL   : https://{admin_domain}")
    notify_success(config, env, console_url)
    return 0


def capacityish(text: str, timed_out: bool) -> bool:
    if timed_out:
        return True
    return bool(re.search(r"Out of host capacity|Out of capacity|InternalError|TooManyRequests|429|Timed out", text, re.I))


def notify_success(config: dict[str, Any], env: dict[str, str], console_url: str) -> None:
    log("Oracle VM launch succeeded.")
    title = "Oracle VM launch succeeded"
    body = f"{config['instance_display_name']} is running. {console_url}"

    ntfy_topic  = str(config.get("notify_ntfy_topic", "") or "").strip()
    ntfy_server = str(config.get("notify_ntfy_server", "") or env.get("NOTIFY_NTFY_SERVER", "https://ntfy.sh")).strip().rstrip("/")
    webhook_url = str(config.get("notify_webhook_url", "") or "").strip()

    if ntfy_topic:
        try:
            req = urllib.request.Request(
                f"{ntfy_server}/{ntfy_topic}",
                data=body.encode("utf-8"),
                headers={"Title": title, "Tags": "white_check_mark,cloud"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=10).read()
            log("Sent ntfy success notification.")
        except Exception as exc:
            log(f"ntfy notification failed: {exc}")

    if webhook_url:
        try:
            payload = json.dumps({"title": title, "message": body, "console_url": console_url}).encode("utf-8")
            req = urllib.request.Request(
                webhook_url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=10).read()
            log("Sent webhook success notification.")
        except Exception as exc:
            log(f"Webhook notification failed: {exc}")

    if platform.system() == "Windows":
        try:
            import winsound

            winsound.MessageBeep(winsound.MB_ICONASTERISK)
            winsound.Beep(880, 500)
            winsound.Beep(1100, 500)
        except Exception:
            pass
        try:
            subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    (
                        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null; "
                        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null; "
                        "$template = '<toast><visual><binding template=\"ToastGeneric\"><text>{0}</text><text>{1}</text></binding></visual></toast>'; "
                        "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument; "
                        "$xml.LoadXml(($template -f $args[0], $args[1])); "
                        "$toast = New-Object Windows.UI.Notifications.ToastNotification $xml; "
                        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Oracle Retry Launcher').Show($toast)"
                    ),
                    title,
                    body,
                ],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    if as_bool(config.get("open_console_on_success", True)):
        webbrowser.open(console_url)


def retry(config: dict[str, Any], env: dict[str, str]) -> int:
    ssh_key = expand_path(config["ssh_public_key_path"])
    config["ssh_public_key_path"] = str(ssh_key)
    validate_ssh_key(ssh_key)

    log(f"=== OCI VM Launch Starting: {config['instance_display_name']} ===")
    log(f"Auth        : {config['oci_auth']}")
    log(f"Compartment : {config['compartment_id']}")
    log(f"Subnet      : {config['subnet_id']}")
    log(f"SSH key     : {ssh_key}")
    log(f"Shape       : {config['shape']}")
    if str(config["shape"]).endswith(".Flex"):
        log(f"Flex size   : {config['ocpus']} OCPU / {config['memory_in_gbs']} GB")
    log(f"Retry delay : {config['retry_delay_seconds']}s")
    if config.get("cloud_init_template"):
        log(f"Cloud-init  : {config['cloud_init_template']}")

    oci_connectivity_probe(config)

    if instance_already_active(config):
        item = find_active_instance(config)
        if item and item.get("id"):
            public_ip = wait_for_public_ip(config, item["id"], timeout_seconds=60)
            update_duckdns(env, public_ip)
        return 0

    ad = availability_domain(config)
    image_id = latest_image_id(config)
    log(f"Image ID    : {image_id}")
    log("Starting retry loop - press Ctrl+C to stop.")

    attempt = 0
    while True:
        # Guard at top of every iteration: if a previous timed-out launch actually
        # succeeded, OCI will show the instance as PROVISIONING/RUNNING here.
        item = find_active_instance(config)
        if item:
            log(f"Pre-flight: '{config['instance_display_name']}' already exists "
                f"({item.get('lifecycle-state', 'UNKNOWN')}) — skipping launch.")
            if item.get("id"):
                public_ip = wait_for_public_ip(config, item["id"], timeout_seconds=60)
                update_duckdns(env, public_ip)
            return 0

        attempt += 1
        temp_files: list[Path] = []
        log(f"Attempt #{attempt} - sending OCI launch request for {config['instance_display_name']}...")
        try:
            args, temp_files = launch_args(config, image_id, ad, env)
            code, stdout, stderr, timed_out = run_oci(
                args,
                auth_mode=config["oci_auth"],
                timeout_seconds=int(config["oci_timeout_seconds"]),
                heartbeat_seconds=int(config["oci_heartbeat_seconds"]),
            )
            log(f"OCI launch command returned exit code {code}.")

            if code == 0:
                data = json.loads(stdout)
                instance_id = data["data"]["id"]
                console_url = f"https://cloud.oracle.com/compute/instances/{instance_id}"
                log(f"ID          : {instance_id}")
                return finish_success(config, env, instance_id, console_url)

            text = (stdout + " " + stderr).strip()
            if timed_out:
                log("Launch command timed out. Polling OCI before retrying...")
                item = wait_for_active_instance(config)
                if item and item.get("id"):
                    console_url = f"https://cloud.oracle.com/compute/instances/{item['id']}"
                    return finish_success(config, env, item["id"], console_url)
                log(f"Instance not found after timeout. Will retry in {config['retry_delay_seconds']}s.")
            elif capacityish(text, timed_out):
                item = wait_for_active_instance(config, timeout_seconds=180)
                if item and item.get("id"):
                    console_url = f"https://cloud.oracle.com/compute/instances/{item['id']}"
                    return finish_success(config, env, item["id"], console_url)
                log(f"Capacity unavailable - will retry in {config['retry_delay_seconds']}s.")
            elif re.search(r"CannotParseRequest|InvalidParameter|InvalidRequest|NotAuthorizedOrNotFound", text):
                log("Configuration/request error - not retrying until config or launch parameters are fixed.")
                log(text)
                return 1
            elif re.search(r"LimitExceeded", text):
                log("Limit exceeded - check whether an instance or boot volume already consumes Always Free quota.")
                log(text)
                return 1
            elif re.search(r"NotAuthenticated|Unauthorized", text):
                log("Auth error - check OCI CLI auth or instance-principal policy.")
                log(text)
                return 1
            else:
                log("Unexpected error; retrying anyway.")
                log(text)

            time.sleep(int(config["retry_delay_seconds"]))
        finally:
            for f in temp_files:
                for _ in range(5):
                    try:
                        f.unlink(missing_ok=True)
                        break
                    except PermissionError:
                        time.sleep(0.5)


def main() -> int:
    parser = argparse.ArgumentParser(description="Retry OCI instance launch from JSON config.")
    parser.add_argument("--profile", default=str(ROOT / "profiles" / "a1-full.json"))
    parser.add_argument("--env", action="append", default=[])
    args = parser.parse_args()

    profile_path = Path(args.profile).expanduser()
    env_paths = [Path(path).expanduser() for path in args.env]
    env_paths.append(ROOT / ".env")
    env_paths.append(ROOT.parent / ".env")

    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    env = load_env(env_paths)
    config = resolve_profile(profile, env)
    config["notify_ntfy_topic"] = env.get("NOTIFY_NTFY_TOPIC", "")
    config["notify_webhook_url"] = env.get("NOTIFY_WEBHOOK_URL", "")
    return retry(config, env)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("Stopped by user.")
        raise SystemExit(130)
    except RetryError as exc:
        log(f"Fatal error: {exc}")
        raise SystemExit(1)
