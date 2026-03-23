from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


WORKSPACE = Path(os.getenv("RSI_AGENT_WORKSPACE", "/workspace/agent"))
MAIN_PATH = WORKSPACE / "main.py"
RESTART_PATH = WORKSPACE / ".restart_requested"
PAUSED_PATH = WORKSPACE / ".paused"
RESUME_PATH = WORKSPACE / ".resume"
PUSH_REQUESTED_PATH = WORKSPACE / ".push_requested"
CRASH_WINDOW_SECONDS = int(os.getenv("RSI_CRASH_WINDOW_SECONDS", "30"))
RESUME_POLL_SECONDS = int(os.getenv("RSI_RESUME_POLL_SECONDS", "5"))
RESTART_STOP_TIMEOUT_SECONDS = int(os.getenv("RSI_RESTART_STOP_TIMEOUT_SECONDS", "10"))

CURRENT_PROCESS: subprocess.Popen[str] | None = None
SHUTDOWN_REQUESTED = False
POPEN = subprocess.Popen


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(event: str, message: str) -> None:
    print(f"[supervisor] {iso_now()} {event}: {message}", flush=True)


def run_git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(WORKSPACE),
        capture_output=True,
        text=True,
        check=False,
    )


def run_git_checked(event: str, message: str, *args: str) -> bool:
    result = run_git(*args)
    if result.returncode == 0:
        return True
    details = result.stderr.strip() or result.stdout.strip() or "unknown git error"
    log(event, f"{message}: {details}")
    PAUSED_PATH.write_text("git failure\n", encoding="utf-8")
    return False


def ensure_repo() -> bool:
    if (WORKSPACE / ".git").exists():
        return True
    log("INIT", "initializing git repo for seed workspace")
    if not run_git_checked("PAUSED", "git init failed", "init"):
        return False
    if not run_git_checked("PAUSED", "git add failed during seed init", "add", "-A"):
        return False
    if not run_git_checked("PAUSED", "git commit failed during seed init", "commit", "-m", "seed"):
        return False
    log("INIT", 'created initial "seed" commit')
    return True


def commit_restart() -> bool | None:
    if RESTART_PATH.exists():
        RESTART_PATH.unlink()
    if not run_git_checked("PAUSED", "git add failed during self-edit commit", "add", "-A"):
        return None
    status = run_git("status", "--porcelain")
    if status.returncode != 0:
        details = status.stderr.strip() or status.stdout.strip() or "unknown git error"
        log("PAUSED", f"git status failed during self-edit commit: {details}")
        PAUSED_PATH.write_text("git failure\n", encoding="utf-8")
        return None
    if not status.stdout.strip():
        log("RESTART", "restart requested with no tracked changes; restarting current code")
        return False
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not run_git_checked(
        "PAUSED",
        "git commit failed during self-edit commit",
        "commit",
        "-m",
        f"self-edit {timestamp}",
    ):
        return None
    # Signal host to push (host watches for this flag on bind-mounted workspace)
    try:
        PUSH_REQUESTED_PATH.write_text(timestamp + "\n", encoding="utf-8")
    except OSError:
        pass
    return True


def revert_last_commit() -> bool:
    return run_git_checked("PAUSED", "git revert failed after rapid crash", "revert", "HEAD", "--no-edit")


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


def try_git_push() -> None:
    """Push current branch to origin if a remote is configured."""
    result = run_git("remote", "get-url", "origin")
    if result.returncode != 0:
        return  # no remote configured
    push = run_git("push", "origin", "HEAD")
    if push.returncode == 0:
        log("GIT_PUSH", "pushed to origin")
    else:
        detail = push.stderr.strip() or push.stdout.strip() or "unknown"
        log("GIT_PUSH", f"push failed (non-blocking): {detail}")


def launch_agent(after_edit: bool = False) -> int:
    global CURRENT_PROCESS
    crash_counter = 0
    current_after_edit = after_edit

    while not SHUTDOWN_REQUESTED:
        start_time = time.time()
        log("START", f"launching {MAIN_PATH}")
        CURRENT_PROCESS = POPEN([sys.executable, str(MAIN_PATH)], cwd=str(WORKSPACE), text=True)
        exit_code = wait_for_process(CURRENT_PROCESS)
        CURRENT_PROCESS = None
        elapsed = time.time() - start_time

        if SHUTDOWN_REQUESTED:
            log("EXIT", "supervisor shutting down")
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
            log("RESTART", "agent self-edited, restarting with new code")
            continue

        if exit_code == 0:
            log("EXIT", "agent exited cleanly")
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
