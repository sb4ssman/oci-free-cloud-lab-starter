#!/usr/bin/env python3
"""
Cloud Lab queue runner — runs every 60 s via a systemd timer.

Reads ~/cloud-lab/queue.json, picks the highest-priority pending job,
executes it, and writes results back. Uses a lock file so only one
instance runs at a time (safe for overlapping timer firings).

Queue file format (list of job objects):
  {
    "id":           "<uuid4>",
    "priority":     1,           # lower = higher priority
    "label":        "Human label",
    "command":      "bash -c '...'",
    "status":       "pending" | "running" | "done" | "failed",
    "queued_at":    "2026-05-22T00:00:00Z",
    "started_at":   null,
    "completed_at": null,
    "exit_code":    null,
    "output":       null
  }

Add a job from a shell:
  python3 ~/cloud-lab/payload/queue/queue_runner.py --enqueue \
      --label "My job" --command "echo hello" --priority 5
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path


CLOUD_LAB_DIR = Path(os.getenv("CLOUD_LAB_DIR", str(Path.home() / "cloud-lab")))
QUEUE_FILE    = CLOUD_LAB_DIR / "queue.json"
LOCK_FILE     = CLOUD_LAB_DIR / "logs" / "queue_runner.lock"
LOG_FILE      = CLOUD_LAB_DIR / "logs" / "queue_runner.log"
MAX_OUTPUT    = 16_000   # chars kept per job output


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _log(msg: str) -> None:
    line = f"[{_now()}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def _load_queue() -> list[dict]:
    if not QUEUE_FILE.exists():
        return []
    try:
        return json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_queue(jobs: list[dict]) -> None:
    CLOUD_LAB_DIR.mkdir(parents=True, exist_ok=True)
    QUEUE_FILE.write_text(json.dumps(jobs, indent=2), encoding="utf-8")


def _acquire_lock() -> bool:
    """Return True if we got the lock, False if another instance is running."""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text().strip())
            # check if that pid is alive
            os.kill(pid, 0)
            return False   # still running
        except (ProcessLookupError, PermissionError):
            pass   # stale lock
        except Exception:
            return False
    LOCK_FILE.write_text(str(os.getpid()))
    return True


def _release_lock() -> None:
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def enqueue(label: str, command: str, priority: int = 5) -> str:
    """Add a job to the queue and return its ID."""
    jobs = _load_queue()
    job_id = str(uuid.uuid4())
    jobs.append({
        "id":           job_id,
        "priority":     priority,
        "label":        label,
        "command":      command,
        "status":       "pending",
        "queued_at":    _now(),
        "started_at":   None,
        "completed_at": None,
        "exit_code":    None,
        "output":       None,
    })
    _save_queue(jobs)
    return job_id


def run_next_job() -> None:
    """Pick and run the highest-priority pending job."""
    jobs = _load_queue()
    pending = [j for j in jobs if j.get("status") == "pending"]
    if not pending:
        return

    pending.sort(key=lambda j: (j.get("priority", 5), j.get("queued_at", "")))
    job = pending[0]
    _log(f"Starting job {job['id']}: {job.get('label', '?')}")

    for j in jobs:
        if j["id"] == job["id"]:
            j["status"]     = "running"
            j["started_at"] = _now()
    _save_queue(jobs)

    try:
        result = subprocess.run(
            ["bash", "-c", job["command"]],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, timeout=600,
        )
        exit_code = result.returncode
        output    = result.stdout or "(no output)"
    except subprocess.TimeoutExpired:
        exit_code = 1
        output    = "Job timed out after 600 seconds."
    except Exception as exc:
        exit_code = 1
        output    = f"Execution error: {exc}"

    if len(output) > MAX_OUTPUT:
        output = output[:MAX_OUTPUT] + "\n… (truncated)"

    status = "done" if exit_code == 0 else "failed"
    _log(f"Job {job['id']} {status} (exit {exit_code}).")

    jobs = _load_queue()   # reload in case of concurrent writes
    for j in jobs:
        if j["id"] == job["id"]:
            j["status"]       = status
            j["completed_at"] = _now()
            j["exit_code"]    = exit_code
            j["output"]       = output
    _save_queue(jobs)


def main() -> None:
    parser = argparse.ArgumentParser(description="Cloud Lab queue runner")
    parser.add_argument("--enqueue",  action="store_true", help="Add a job instead of running")
    parser.add_argument("--label",    default="CLI job",  help="Job label")
    parser.add_argument("--command",  default="",         help="Shell command to run")
    parser.add_argument("--priority", type=int, default=5, help="Priority (lower = runs first)")
    args = parser.parse_args()

    if args.enqueue:
        if not args.command:
            print("Error: --command is required with --enqueue", file=sys.stderr)
            sys.exit(1)
        job_id = enqueue(args.label, args.command, args.priority)
        print(f"Queued job {job_id}")
        return

    if not _acquire_lock():
        _log("Queue runner already running — skipping this tick.")
        return

    try:
        run_next_job()
    finally:
        _release_lock()


if __name__ == "__main__":
    main()
