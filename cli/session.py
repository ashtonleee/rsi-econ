#!/usr/bin/env python3
"""RSI-Econ session management CLI.

Usage:
    python cli/session.py status           # Current session state
    python cli/session.py pause            # Pause agent
    python cli/session.py resume           # Resume agent
    python cli/session.py new [--name X]   # Reset to seed, new git branch
    python cli/session.py list             # List all session branches
    python cli/session.py push             # Push current branch to GitHub
    python cli/session.py fork <branch>    # Fork from another session's state
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib import request as urllib_request


SEED_DIR = Path(__file__).resolve().parents[1] / "sandbox" / "seed"
BASELINE_DIR = Path(__file__).resolve().parents[1] / "sandbox" / "baseline"
COMPOSE_FILE = Path(__file__).resolve().parents[1] / "docker-compose.yml"
WALLET_URL = "http://localhost:8081"


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=False, **kwargs)


def git(*args: str) -> subprocess.CompletedProcess[str]:
    return run(["git", *args], cwd=str(SEED_DIR))


def docker_compose(*args: str) -> subprocess.CompletedProcess[str]:
    return run(["docker", "compose", "-f", str(COMPOSE_FILE), *args])


def budget_bar(pct: float, width: int = 18) -> str:
    """Render a text progress bar for budget remaining."""
    filled = int(pct / 100 * width)
    bar = "\u2588" * filled + "\u2591" * (width - filled)
    if pct > 50:
        phase = "FULL"
    elif pct > 20:
        phase = "MODERATE"
    elif pct > 5:
        phase = "CONSERVE"
    else:
        phase = "WRAPUP"
    return f"{bar} {phase}"


def cmd_status(_args: argparse.Namespace) -> int:
    # Sandbox running?
    ps = docker_compose("ps", "--format", "json", "sandbox")
    running = False
    if ps.returncode == 0 and ps.stdout.strip():
        try:
            for line in ps.stdout.strip().split("\n"):
                info = json.loads(line)
                state = info.get("State", "")
                running = state == "running"
        except (json.JSONDecodeError, KeyError):
            pass

    paused = (SEED_DIR / ".paused").exists()
    state = "PAUSED" if paused else ("RUNNING" if running else "STOPPED")

    # Git info
    branch_name = git("branch", "--show-current").stdout.strip() or "(detached)"
    commit_count = git("rev-list", "--count", "HEAD").stdout.strip() or "?"
    last_commit = git("log", "-1", "--format=%h %s (%cr)").stdout.strip() or "none"

    # Wallet
    budget_str = "? / ?"
    bar_str = ""
    model_str = "?"
    try:
        req = urllib_request.Request(f"{WALLET_URL}/wallet", method="GET")
        with urllib_request.urlopen(req, timeout=5) as resp:
            wallet = json.loads(resp.read().decode("utf-8"))
            rem = wallet.get("remaining_usd", 0)
            total = wallet.get("budget_usd", 0)
            pct = (rem / total * 100) if total > 0 else 0
            budget_str = f"${rem:.2f} / ${total:.2f} ({pct:.0f}%)"
            bar_str = budget_bar(pct)
            models = wallet.get("models_available", [])
            model_str = models[0] if models else "unknown"
    except Exception:
        pass

    # Proposals
    proposal_str = "?"
    try:
        req = urllib_request.Request(f"{WALLET_URL}/proposals", method="GET")
        with urllib_request.urlopen(req, timeout=5) as resp:
            proposals = json.loads(resp.read().decode("utf-8"))
            counts = {"pending": 0, "approved": 0, "rejected": 0}
            for p in proposals:
                s = p.get("status", "pending")
                if s in counts:
                    counts[s] += 1
            proposal_str = f"{counts['pending']} pending, {counts['approved']} approved, {counts['rejected']} rejected"
    except Exception:
        pass

    # Knowledge findings
    findings_count = 0
    k_path = SEED_DIR / "knowledge.json"
    if k_path.exists():
        try:
            k = json.loads(k_path.read_text(encoding="utf-8"))
            findings_count = len(k.get("findings", []))
        except Exception:
            pass

    # Webhook
    webhook_str = "not configured"
    config_path = Path(__file__).resolve().parents[1] / "state" / "notification_config.json"
    if config_path.exists():
        try:
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
            if cfg.get("webhook_url", "").strip():
                webhook_str = "active (Discord)"
            else:
                webhook_str = "configured (no URL)"
        except Exception:
            pass

    print(f"\u2550\u2550\u2550 RSI-ECON STATUS \u2550\u2550\u2550")
    print(f"Session:    {branch_name}")
    print(f"Status:     {state}")
    print(f"Budget:     {budget_str}")
    if bar_str:
        print(f"            {bar_str}")
    print(f"Model:      {model_str}")
    print()
    print(f"Git:        {commit_count} commits")
    print(f"Last:       {last_commit}")
    print()
    print(f"Proposals:  {proposal_str}")
    print(f"Findings:   {findings_count} entries in knowledge.json")
    print()
    print(f"Webhook:    {webhook_str}")
    print("\u2550" * 23)
    return 0


def cmd_pause(_args: argparse.Namespace) -> int:
    result = docker_compose("exec", "sandbox", "touch", "/workspace/agent/.paused")
    if result.returncode != 0:
        print(f"Error: {result.stderr.strip()}", file=sys.stderr)
        return 1
    print("Agent paused.")
    return 0


def cmd_resume(_args: argparse.Namespace) -> int:
    r1 = docker_compose("exec", "sandbox", "touch", "/workspace/agent/.resume")
    r2 = docker_compose("exec", "sandbox", "rm", "-f", "/workspace/agent/.paused")
    if r1.returncode != 0:
        print(f"Error: {r1.stderr.strip()}", file=sys.stderr)
        return 1
    if r2.returncode != 0:
        print(f"Warning: {r2.stderr.strip()}", file=sys.stderr)
    print("Agent resumed.")
    return 0


def cmd_new(args: argparse.Namespace) -> int:
    name = args.name or f"{int(time.time())}"
    branch = f"session/{name}"

    # Stop sandbox
    print("Stopping sandbox...")
    docker_compose("stop", "sandbox")

    # Switch to main and create session branch
    current = git("branch", "--show-current").stdout.strip()
    print(f"Current branch: {current}")

    git("checkout", "main")
    result = git("checkout", "-b", branch)
    if result.returncode != 0:
        print(f"Error creating branch: {result.stderr.strip()}", file=sys.stderr)
        return 1

    # Start sandbox
    print("Starting sandbox...")
    docker_compose("start", "sandbox")
    print(f"New session: {branch}")
    return 0


def cmd_list(_args: argparse.Namespace) -> int:
    result = git("branch", "-a", "--format=%(refname:short)")
    if result.returncode != 0 or not result.stdout.strip():
        print("No branches found.")
        return 0

    branches = result.stdout.strip().split("\n")
    session_branches = [b for b in branches if "session/" in b]
    if not session_branches:
        print("No session branches found.")
        return 0

    current = git("branch", "--show-current").stdout.strip()
    for branch in session_branches:
        marker = "* " if branch == current else "  "
        info = git("log", "-1", "--format=%h %s (%cr)", branch)
        count = git("rev-list", "--count", branch)
        commits = count.stdout.strip() or "?"
        detail = info.stdout.strip() or ""
        print(f"{marker}{branch}  [{commits} commits]  {detail}")
    return 0


def cmd_push(_args: argparse.Namespace) -> int:
    # Push from host side (sandbox can't reach GitHub)
    branch = git("branch", "--show-current").stdout.strip()
    if not branch:
        print("Error: not on a branch.", file=sys.stderr)
        return 1

    # Check remote exists
    remote = git("remote", "get-url", "origin")
    if remote.returncode != 0:
        print("Error: no 'origin' remote configured.", file=sys.stderr)
        print("Set up with: git -C sandbox/seed remote add origin <url>", file=sys.stderr)
        return 1

    print(f"Pushing {branch} to origin...")
    result = git("push", "-u", "origin", branch)
    if result.returncode != 0:
        print(f"Error: {result.stderr.strip()}", file=sys.stderr)
        return 1

    # Clear push_requested flag if present
    flag = SEED_DIR / ".push_requested"
    if flag.exists():
        flag.unlink()

    print(f"Pushed {branch} to {remote.stdout.strip()}")
    return 0


def cmd_fork(args: argparse.Namespace) -> int:
    source = args.branch
    name = args.name or f"fork-{int(time.time())}"
    branch = f"session/{name}"

    # Verify source branch exists
    check = git("rev-parse", "--verify", source)
    if check.returncode != 0:
        print(f"Error: branch '{source}' not found.", file=sys.stderr)
        return 1

    # Stop sandbox
    print("Stopping sandbox...")
    docker_compose("stop", "sandbox")

    result = git("checkout", "-b", branch, source)
    if result.returncode != 0:
        print(f"Error: {result.stderr.strip()}", file=sys.stderr)
        return 1

    # Start sandbox
    print("Starting sandbox...")
    docker_compose("start", "sandbox")
    print(f"Forked {source} → {branch}")
    return 0


def cmd_restore_baseline(_args: argparse.Namespace) -> int:
    if not BASELINE_DIR.exists():
        print(f"Error: baseline directory not found at {BASELINE_DIR}", file=sys.stderr)
        return 1

    print("Stopping sandbox...")
    docker_compose("stop", "sandbox")

    for item in BASELINE_DIR.iterdir():
        if item.is_file():
            dest = SEED_DIR / item.name
            shutil.copy2(str(item), str(dest))
            print(f"  restored: {item.name}")

    git("add", "-A")
    git("commit", "-m", "restored from baseline")

    print("Starting sandbox...")
    docker_compose("start", "sandbox")
    print("Baseline restored successfully.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="RSI-Econ session management")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="Show current session state")
    sub.add_parser("pause", help="Pause the agent")
    sub.add_parser("resume", help="Resume the agent")

    new_p = sub.add_parser("new", help="Start a new session from seed")
    new_p.add_argument("--name", help="Session name (default: timestamp)")

    sub.add_parser("list", help="List all session branches")
    sub.add_parser("push", help="Push current branch to GitHub")

    fork_p = sub.add_parser("fork", help="Fork from another session")
    fork_p.add_argument("branch", help="Source branch to fork from")
    fork_p.add_argument("--name", help="New session name")

    sub.add_parser("restore-baseline", help="Restore seed to baseline (nuclear reset)")

    args = parser.parse_args()
    commands = {
        "status": cmd_status,
        "pause": cmd_pause,
        "resume": cmd_resume,
        "new": cmd_new,
        "list": cmd_list,
        "push": cmd_push,
        "fork": cmd_fork,
        "restore-baseline": cmd_restore_baseline,
    }

    if args.command not in commands:
        parser.print_help()
        return 1
    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
