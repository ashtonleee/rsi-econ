from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request


WORKSPACE = Path(os.getenv("RSI_AGENT_WORKSPACE", "/workspace/agent"))
MAIN_PATH = WORKSPACE / "main.py"
RESTART_PATH = WORKSPACE / ".restart_requested"
PAUSED_PATH = WORKSPACE / ".paused"
RESUME_PATH = WORKSPACE / ".resume"
PUSH_REQUESTED_PATH = WORKSPACE / ".push_requested"
BRIDGE_URL = os.getenv("WALLET_URL", "http://bridge:8081")
CRASH_WINDOW_SECONDS = int(os.getenv("RSI_CRASH_WINDOW_SECONDS", "30"))
RESUME_POLL_SECONDS = int(os.getenv("RSI_RESUME_POLL_SECONDS", "5"))
RESTART_STOP_TIMEOUT_SECONDS = int(os.getenv("RSI_RESTART_STOP_TIMEOUT_SECONDS", "10"))
BASELINE_DIR = Path(os.getenv("RSI_BASELINE_DIR", "/opt/baseline"))
BACKUP_DIR = Path(os.getenv("RSI_BACKUP_DIR", "/var/lib/rsi/backups"))
EVENTS_DIR = Path(os.getenv("RSI_EVENTS_DIR", "/var/lib/rsi/events"))
MAX_BACKUPS = 10

CURRENT_PROCESS: subprocess.Popen[str] | None = None
SHUTDOWN_REQUESTED = False
POPEN = subprocess.Popen


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(event: str, message: str) -> None:
    print(f"[supervisor] {iso_now()} {event}: {message}", flush=True)


def write_event(event_type: str, message: str, data: dict | None = None) -> None:
    """Write a JSON event file for the bridge notifier to pick up."""
    try:
        EVENTS_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        path = EVENTS_DIR / f"{ts}_{event_type}.json"
        payload: dict = {"event": event_type, "message": message, "timestamp": iso_now()}
        if data:
            payload["data"] = data
        path.write_text(json.dumps(payload), encoding="utf-8")
    except Exception:
        pass  # never crash supervisor for notifications


# --- Bridge HTTP communication (replaces direct git calls) ---

def _bridge_request(method: str, path: str, payload: dict | None = None) -> dict[str, Any] | None:
    """Make an HTTP request to the bridge API. Returns parsed JSON or None on error."""
    url = f"{BRIDGE_URL}{path}"
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib_request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method=method,
    )
    try:
        with urllib_request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib_error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        log("BRIDGE_ERROR", f"{method} {path} failed: {exc}")
        return None


# --- Syntax validation (runs locally, no git needed) ---

def validate_agent_code() -> bool:
    """Syntax-check main.py before allowing restart with new code."""
    result = subprocess.run(
        [sys.executable, "-c", f"import py_compile; py_compile.compile('{MAIN_PATH}', doraise=True)"],
        cwd=str(WORKSPACE),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown compile error"
        log("VALIDATION_FAILED", f"syntax error in {MAIN_PATH}: {detail[:200]}")
        return False
    return True


# --- Backup (runs locally on workspace files, no git needed) ---

def backup_workspace() -> None:
    """Create a tarball snapshot of the workspace before self-edit commits."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    tarball_path = BACKUP_DIR / f"workspace-{timestamp}.tar.gz"
    try:
        with tarfile.open(str(tarball_path), "w:gz") as tar:
            tar.add(str(WORKSPACE), arcname="workspace")
        log("BACKUP", f"created {tarball_path.name}")
    except Exception as exc:
        log("BACKUP_FAILED", f"could not create backup: {exc}")
        return
    backups = sorted(BACKUP_DIR.glob("workspace-*.tar.gz"))
    while len(backups) > MAX_BACKUPS:
        oldest = backups.pop(0)
        try:
            oldest.unlink()
        except Exception:
            pass


# --- Restore (file operations local, git commit via bridge) ---

def restore_from_backup() -> bool:
    """Restore workspace from the latest tarball backup."""
    backups = sorted(BACKUP_DIR.glob("workspace-*.tar.gz"))
    if not backups:
        log("RESTORE", "no backups available")
        return False
    latest = backups[-1]
    try:
        for item in WORKSPACE.iterdir():
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
        with tarfile.open(str(latest), "r:gz") as tar:
            for member in tar.getmembers():
                if member.name.startswith("workspace/"):
                    member.name = member.name[len("workspace/"):]
                    if member.name:
                        tar.extract(member, str(WORKSPACE))
        log("RESTORE", f"restored from {latest.name}")
        _bridge_request("POST", "/git/commit", {"message": "restored from backup"})
        return True
    except Exception as exc:
        log("RESTORE_FAILED", f"could not restore from backup: {exc}")
        return False


def restore_from_baseline() -> bool:
    """Nuclear restore: copy read-only baseline files into workspace."""
    if not BASELINE_DIR.exists():
        log("RESTORE", "no baseline directory available")
        return False
    try:
        for item in BASELINE_DIR.iterdir():
            if item.is_file():
                shutil.copy2(str(item), str(WORKSPACE / item.name))
        _bridge_request("POST", "/git/commit", {"message": "restored from baseline (nuclear)"})
        log("RESTORE", "restored from baseline")
        return True
    except Exception as exc:
        log("RESTORE_FAILED", f"could not restore from baseline: {exc}")
        return False


# --- Git operations via bridge API ---

def ensure_repo() -> bool:
    """Initialize the git repo via bridge API."""
    resp = _bridge_request("POST", "/git/init")
    if resp is None:
        log("PAUSED", "bridge git init failed")
        PAUSED_PATH.write_text("bridge git init failed\n", encoding="utf-8")
        return False
    log("INIT", f"repo ready: {resp.get('status', '?')} hash={resp.get('hash', '?')[:8]}")
    return True


def commit_restart() -> bool | None:
    """Commit self-edit via bridge, validate, check integrity."""
    if RESTART_PATH.exists():
        RESTART_PATH.unlink()
    backup_workspace()

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    resp = _bridge_request("POST", "/git/commit", {"message": f"self-edit {timestamp}"})
    if resp is None:
        log("PAUSED", "bridge commit failed")
        PAUSED_PATH.write_text("bridge commit failed\n", encoding="utf-8")
        return None

    if not resp.get("changed", False):
        log("RESTART", "restart requested with no tracked changes; restarting current code")
        return False

    # Signal host to push
    try:
        PUSH_REQUESTED_PATH.write_text(timestamp + "\n", encoding="utf-8")
    except OSError:
        pass

    # Validate syntax before restarting with new code
    if not validate_agent_code():
        _bridge_request("POST", "/git/revert/HEAD~1")
        log("VALIDATION_REVERTED", "reverted self-edit due to syntax error")
        return False

    # Verify git integrity
    fsck = _bridge_request("GET", "/git/fsck")
    if fsck and not fsck.get("ok", True):
        log("GIT_CORRUPT", "git integrity check failed, attempting restore")
        if not restore_from_backup():
            restore_from_baseline()
        return False

    return True


def revert_last_commit() -> bool:
    """Revert to previous commit via bridge API."""
    resp = _bridge_request("POST", "/git/revert/HEAD~1")
    if resp is None or "error" in (resp or {}):
        log("PAUSED", "bridge revert failed")
        PAUSED_PATH.write_text("bridge revert failed\n", encoding="utf-8")
        return False
    return True


def try_git_push() -> None:
    """Push current branch via bridge API."""
    resp = _bridge_request("POST", "/git/push")
    if resp and resp.get("status") == "pushed":
        log("GIT_PUSH", "pushed to origin via bridge")
    elif resp and resp.get("status") == "no_remote":
        pass  # no remote configured, silent
    elif resp:
        log("GIT_PUSH", f"push result: {resp.get('status', '?')}")


# --- Process management (unchanged) ---

def stop_agent_for_restart(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    log("RESTART", "restart marker detected, stopping current agent process")
    process.terminate()
    deadline = time.time() + RESTART_STOP_TIMEOUT_SECONDS
    while process.poll() is None and time.time() < deadline:
        time.sleep(0.2)
    if process.poll() is None:
        log("RESTART", "agent did not stop after SIGTERM, killing process")
        process.kill()


def wait_for_process(process: subprocess.Popen[str]) -> int:
    while True:
        exit_code = process.poll()
        if exit_code is not None:
            return exit_code
        if RESTART_PATH.exists() and not SHUTDOWN_REQUESTED:
            stop_agent_for_restart(process)
        time.sleep(0.2)


def wait_for_resume() -> bool:
    while PAUSED_PATH.exists() and not RESUME_PATH.exists() and not SHUTDOWN_REQUESTED:
        time.sleep(RESUME_POLL_SECONDS)
    if SHUTDOWN_REQUESTED:
        return False
    if RESUME_PATH.exists():
        if PAUSED_PATH.exists():
            PAUSED_PATH.unlink()
        RESUME_PATH.unlink()
        log("RESUMED", "operator resume detected, restarting agent")
        return True
    return False


def handle_signal(signum: int, _frame: object) -> None:
    global SHUTDOWN_REQUESTED
    SHUTDOWN_REQUESTED = True
    log("SIGNAL", f"received signal {signum}, forwarding to agent")
    if CURRENT_PROCESS is not None and CURRENT_PROCESS.poll() is None:
        CURRENT_PROCESS.send_signal(signum)


# --- Main agent loop ---

def launch_agent(after_edit: bool = False) -> int:
    global CURRENT_PROCESS
    crash_counter = 0
    current_after_edit = after_edit

    while not SHUTDOWN_REQUESTED:
        start_time = time.time()
        log("START", f"launching {MAIN_PATH}")
        write_event("session_start", f"Agent launched (edit={'yes' if current_after_edit else 'no'})")
        CURRENT_PROCESS = POPEN([sys.executable, str(MAIN_PATH)], cwd=str(WORKSPACE), text=True)
        exit_code = wait_for_process(CURRENT_PROCESS)
        CURRENT_PROCESS = None
        elapsed = time.time() - start_time

        if SHUTDOWN_REQUESTED:
            log("EXIT", "supervisor shutting down")
            write_event("session_stop", "Supervisor shutting down (signal)")
            return 0

        if RESTART_PATH.exists():
            commit_result = commit_restart()
            if commit_result is None:
                if wait_for_resume():
                    crash_counter = 0
                    current_after_edit = False
                    continue
                return 0
            crash_counter = 0
            current_after_edit = bool(commit_result)
            if commit_result:
                # Get commit hash for notification (bot fetches diff separately)
                log_resp = _bridge_request("GET", "/git/log")
                commit_hash = (log_resp or [{}])[0].get("hash", "")[:8] if log_resp else ""
                write_event("self_edit", f"commit {commit_hash}", {"commit_hash": commit_hash})
            log("RESTART", "agent self-edited, restarting with new code")
            continue

        if exit_code == 0:
            log("EXIT", "agent exited cleanly")
            write_event("session_stop", "Agent exited cleanly")
            try:
                PUSH_REQUESTED_PATH.write_text("clean-exit\n", encoding="utf-8")
            except OSError:
                pass
            try_git_push()
            return 0

        if current_after_edit and elapsed < CRASH_WINDOW_SECONDS:
            crash_counter += 1
            log("CRASH", f"agent crashed {elapsed:.1f}s after start (crash #{crash_counter})")
            if crash_counter >= 3:
                log("PAUSED", "3 crashes in rapid succession, writing .paused")
                PAUSED_PATH.write_text("paused after rapid crashes\n", encoding="utf-8")
                try_git_push()
                if wait_for_resume():
                    crash_counter = 0
                    current_after_edit = False
                    continue
                return 0
            if not revert_last_commit():
                if wait_for_resume():
                    crash_counter = 0
                    current_after_edit = False
                    continue
                return 0
            log("REVERTED", "reverted to previous version, restarting")
            current_after_edit = True
            continue

        log("CRASH", f"agent crashed after {elapsed:.1f}s, restarting without revert")
        crash_counter = 0
        current_after_edit = False

    log("EXIT", "supervisor shutting down")
    return 0


def main() -> int:
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    if not ensure_repo():
        if wait_for_resume():
            return main()
        return 0
    return launch_agent()


if __name__ == "__main__":
    raise SystemExit(main())
