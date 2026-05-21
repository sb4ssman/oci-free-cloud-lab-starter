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

    return child_env


def run_oci(
    args: list[str],
    *,
    auth_mode: str,
    timeout_seconds: int,
    heartbeat_seconds: int,
) -> tuple[int, str, str, bool]:
    oci = shutil.which("oci")
    if not oci and platform.system() == "Windows":
        known = Path(r"C:\Program Files (x86)\Oracle\oci_cli\oci.exe")
        if known.exists():
            oci = str(known)
    if not oci:
        raise RetryError("OCI CLI was not found on PATH.")

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
            log(f"OCI command still running after {elapsed}s (PID {process.pid}); timeout at {timeout_seconds}s.")
            next_heartbeat = now + heartbeat_seconds

        if elapsed >= timeout_seconds:
            timed_out = True
            process.kill()
            break

    stdout, stderr = process.communicate()
    code = 124 if timed_out else int(process.returncode or 0)
    return code, stdout or "", stderr or "", timed_out


def run_oci_json(
    args: list[str],
    *,
    auth_mode: str,
    timeout_seconds: int,
    heartbeat_seconds: int,
) -> Any:
    code, stdout, stderr, _ = run_oci(
        args,
        auth_mode=auth_mode,
        timeout_seconds=timeout_seconds,
        heartbeat_seconds=heartbeat_seconds,
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
        import base64
        encoded = base64.b64encode(user_data_script.encode("utf-8")).decode("ascii")
        handle = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8")
        with handle:
            handle.write(encoded)
        ud_path = Path(handle.name)
        temp_files.append(ud_path)
        args.extend(["--user-data-file", str(ud_path)])
        log("Cloud-init user-data attached.")

    return args, temp_files


def instance_already_active(config: dict[str, Any]) -> bool:
    """Return True if an instance with this display name is already RUNNING or PROVISIONING."""
    try:
        data = run_oci_json(
            ["compute", "instance", "list",
             "--compartment-id", config["compartment_id"], "--all"],
            auth_mode=config["oci_auth"],
            timeout_seconds=int(config["oci_timeout_seconds"]),
            heartbeat_seconds=int(config["oci_heartbeat_seconds"]),
        )
        for item in data.get("data", []):
            if (item.get("display-name") == config["instance_display_name"] and
                    item.get("lifecycle-state") not in ("TERMINATING", "TERMINATED")):
                log(f"Pre-flight: '{config['instance_display_name']}' already exists "
                    f"({item['lifecycle-state']}) — skipping launch.")
                return True
        return False
    except Exception as exc:
        log(f"Pre-flight check failed ({exc}); proceeding with launch attempt.")
        return False


def capacityish(text: str, timed_out: bool) -> bool:
    if timed_out:
        return True
    return bool(re.search(r"Out of host capacity|Out of capacity|InternalError|TooManyRequests|429|Timed out", text, re.I))


def notify_success(config: dict[str, Any], console_url: str) -> None:
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

    log("=== Oracle Retry Starting ===")
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

    if instance_already_active(config):
        return 0

    ad = availability_domain(config)
    image_id = latest_image_id(config)
    log(f"Image ID    : {image_id}")
    log("Starting retry loop - press Ctrl+C to stop.")

    attempt = 0
    while True:
        attempt += 1
        temp_files: list[Path] = []
        log(f"Attempt #{attempt} - instance retry for {config['instance_display_name']}...")
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
                notify_success(config, console_url)
                return 0

            text = (stdout + " " + stderr).strip()
            if capacityish(text, timed_out):
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
                f.unlink(missing_ok=True)


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
