#!/usr/bin/env python3
"""RSI-Econ session management CLI.

Manages agent sessions via Docker volumes. All git operations run inside
the bridge container, which has access to the git-repo and agent-workspace
volumes. Pushes to GitHub happen from the host (to use local credentials).

The canonical seed lives on the host at sandbox/seed/ (read-only mount into
bridge at /opt/seed). The agent workspace is a Docker volume that gets
reset to the seed state between sessions.

Usage:
    python cli/session.py status              # Current session state
    python cli/session.py new [--name X]      # New session from canonical seed
    python cli/session.py push                # Push current branch to GitHub
    python cli/session.py list                # List session branches
    python cli/session.py reset               # Nuclear: wipe volumes, recreate from seed
    python cli/session.py fork <branch> [--name X]  # Fork from another session
    python cli/session.py pause               # Pause agent
    python cli/session.py resume              # Resume agent
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib import request as urllib_request


# --- Configuration ---

PROJECT_DIR = Path(__file__).resolve().parents[1]
COMPOSE_FILE = PROJECT_DIR / "docker-compose.yml"
SEED_DIR = PROJECT_DIR / "sandbox" / "seed"
WALLET_URL = "http://localhost:8081"
GITHUB_REMOTE = "https://github.com/ashtonleee/rsi-econ-agent-workspace.git"

BRIDGE_CONTAINER = "rsi-econ-bridge-1"
SANDBOX_CONTAINER = "rsi-econ-sandbox-1"

# Paths inside the bridge container
_GIT_DIR = "/var/lib/rsi/git-repo/.git"
_GIT_REPO = "/var/lib/rsi/git-repo"
_WORKSPACE = "/var/lib/rsi/workspace"
_SEED_MOUNT = "/opt/seed"


# --- Docker detection ---

def _find_docker() -> str:
    """Find the docker binary (may not be in PATH in some shells)."""
    for p in ["/usr/local/bin/docker", "/opt/homebrew/bin/docker"]:
        if Path(p).exists():
            return p
    r = subprocess.run(["which", "docker"], capture_output=True, text=True, check=False)
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    print("Error: docker not found.", file=sys.stderr)
    sys.exit(1)


DOCKER = _find_docker()


# --- Low-level helpers ---

def run(cmd: list[str], **kw) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=False, **kw)


def docker_compose(*args: str) -> subprocess.CompletedProcess[str]:
    return run([DOCKER, "compose", "-f", str(COMPOSE_FILE), *args])


def bridge_git(*args: str) -> subprocess.CompletedProcess[str]:
    """Run git in the bridge container with proper GIT_DIR and GIT_WORK_TREE."""
    return run([
        DOCKER, "exec",
        "-e", f"GIT_DIR={_GIT_DIR}",
        "-e", f"GIT_WORK_TREE={_WORKSPACE}",
        BRIDGE_CONTAINER,
        "git", *args,
    ])


def bridge_sh(script: str) -> subprocess.CompletedProcess[str]:
    """Run a shell command in the bridge container."""
    return run([DOCKER, "exec", BRIDGE_CONTAINER, "sh", "-c", script])


def bridge_api(method: str, path: str, payload: dict | None = None) -> dict | None:
    """Call the bridge HTTP API. Returns parsed JSON or None on error."""
    url = f"{WALLET_URL}{path}"
    data = json.dumps(payload).encode() if payload else None
    req = urllib_request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method=method,
    )
    try:
        with urllib_request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


def _container_running(name: str) -> bool:
    r = run([DOCKER, "inspect", "--format={{.State.Running}}", name])
    return r.stdout.strip() == "true"


def _require_bridge() -> bool:
    if not _container_running(BRIDGE_CONTAINER):
        print("Error: bridge container not running.", file=sys.stderr)
        print(f"Start with: {DOCKER} compose -f {COMPOSE_FILE} up -d bridge", file=sys.stderr)
        return False
    return True


# --- Git lifecycle helpers ---

def _ensure_repo() -> bool:
    """Ensure the bridge's git repo has a main branch and GitHub remote.

    Handles first-time initialization and the case where bridge init_repo()
    created a 'master' branch (renames to 'main').
    """
    if not _require_bridge():
        return False

    # Check if repo exists
    r = bridge_git("rev-parse", "--git-dir")
    if r.returncode != 0:
        # No repo yet — initialize
        bridge_sh(f"git init {_GIT_REPO}")
        _copy_seed_to_workspace()
        bridge_git("add", "-A")
        bridge_git("commit", "-m", "canonical seed v2")

    # Ensure main branch exists (bridge init_repo may have created 'master')
    branches = bridge_git("branch", "--list", "main").stdout.strip()
    if not branches:
        current = bridge_git("branch", "--show-current").stdout.strip()
        if current and current != "main":
            bridge_git("branch", "-M", "main")

    # Ensure GitHub remote
    r = bridge_git("remote", "get-url", "origin")
    if r.returncode != 0:
        bridge_git("remote", "add", "origin", GITHUB_REMOTE)
    elif r.stdout.strip() != GITHUB_REMOTE:
        bridge_git("remote", "set-url", "origin", GITHUB_REMOTE)

    return True


def _copy_seed_to_workspace() -> None:
    """Copy canonical seed files from /opt/seed into workspace volume.

    Clears all existing workspace contents first (full reset to seed state).
    """
    bridge_sh(
        f"rm -rf {_WORKSPACE}/* {_WORKSPACE}/.[!.]* 2>/dev/null; "
        f"cp {_SEED_MOUNT}/.gitignore {_WORKSPACE}/ 2>/dev/null; "
        f"for f in {_SEED_MOUNT}/*.py {_SEED_MOUNT}/*.md {_SEED_MOUNT}/*.json; do "
        f"  [ -f \"$f\" ] && cp \"$f\" {_WORKSPACE}/; "
        f"done; true"
    )


def _stop_sandbox() -> None:
    if _container_running(SANDBOX_CONTAINER):
        print("Stopping sandbox...")
        docker_compose("stop", "sandbox")
    else:
        print("Sandbox already stopped.")


def _start_sandbox() -> None:
    print("Starting sandbox...")
    docker_compose("up", "-d", "sandbox")


def _budget_bar(pct: float, width: int = 18) -> str:
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


# --- Commands ---

def cmd_status(_args: argparse.Namespace) -> int:
    """Show current session state: branch, budget, agent state."""
    sandbox_up = _container_running(SANDBOX_CONTAINER)
    bridge_up = _container_running(BRIDGE_CONTAINER)

    if not bridge_up:
        print("\u2550\u2550\u2550 RSI-ECON STATUS \u2550\u2550\u2550")
        print(f"Sandbox:    {'RUNNING' if sandbox_up else 'STOPPED'}")
        print(f"Bridge:     NOT RUNNING")
        print("\u2550" * 23)
        return 0

    # Git info from bridge container
    branch = bridge_git("branch", "--show-current").stdout.strip() or "(none)"
    count = bridge_git("rev-list", "--count", "HEAD").stdout.strip() or "?"
    last = bridge_git("log", "-1", "--format=%h %s (%cr)").stdout.strip() or "none"

    # Agent state
    paused = bridge_sh(f"test -f {_WORKSPACE}/.paused && echo yes").stdout.strip() == "yes"
    if paused:
        state = "PAUSED"
    elif sandbox_up:
        state = "RUNNING"
    else:
        state = "STOPPED"

    # Wallet info
    budget_str = "? / ?"
    bar_str = ""
    model_str = "?"
    wallet = bridge_api("GET", "/wallet")
    if wallet:
        rem = wallet.get("remaining_usd", 0)
        total = wallet.get("budget_usd", 0)
        pct = (rem / total * 100) if total > 0 else 0
        budget_str = f"${rem:.2f} / ${total:.2f} ({pct:.0f}%)"
        bar_str = _budget_bar(pct)
        models = wallet.get("models_available", [])
        model_str = models[0] if models else "unknown"

    # Proposals
    proposal_str = "?"
    proposals = bridge_api("GET", "/proposals")
    if proposals is not None:
        counts = {"pending": 0, "approved": 0, "rejected": 0}
        for p in proposals:
            s = p.get("status", "pending")
            if s in counts:
                counts[s] += 1
        proposal_str = f"{counts['pending']} pending, {counts['approved']} approved, {counts['rejected']} rejected"

    # Webhook
    webhook_str = "not configured"
    config_path = PROJECT_DIR / "state" / "notification_config.json"
    if config_path.exists():
        try:
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
            if cfg.get("webhook_url", "").strip():
                webhook_str = "active (Discord)"
        except Exception:
            pass

    print("\u2550\u2550\u2550 RSI-ECON STATUS \u2550\u2550\u2550")
    print(f"Session:    {branch}")
    print(f"Status:     {state}")
    print(f"Budget:     {budget_str}")
    if bar_str:
        print(f"            {bar_str}")
    print(f"Model:      {model_str}")
    print()
    print(f"Git:        {count} commits")
    print(f"Last:       {last}")
    print()
    print(f"Proposals:  {proposal_str}")
    print(f"Webhook:    {webhook_str}")
    print("\u2550" * 23)
    return 0


def cmd_new(args: argparse.Namespace) -> int:
    """Start a new session from the canonical seed.

    Stops sandbox, resets workspace to seed, creates a session branch, starts sandbox.
    """
    name = args.name or f"s-{int(time.time())}"
    branch = f"session/{name}"

    if not _ensure_repo():
        return 1

    # Check branch doesn't already exist
    if bridge_git("rev-parse", "--verify", branch).returncode == 0:
        print(f"Error: branch '{branch}' already exists.", file=sys.stderr)
        return 1

    _stop_sandbox()

    # Switch to main (canonical seed state)
    r = bridge_git("checkout", "main")
    if r.returncode != 0:
        print(f"Error switching to main: {r.stderr.strip()}", file=sys.stderr)
        return 1

    # Reset workspace to pristine seed (from /opt/seed mount)
    _copy_seed_to_workspace()

    # Create session branch
    r = bridge_git("checkout", "-b", branch)
    if r.returncode != 0:
        print(f"Error creating branch: {r.stderr.strip()}", file=sys.stderr)
        return 1

    _start_sandbox()
    print(f"New session: {branch}")
    return 0


def cmd_push(_args: argparse.Namespace) -> int:
    """Push current session branch to GitHub.

    Copies the git repo from the bridge container to a temp dir on the host,
    then pushes using the host's git credentials (macOS keychain, etc).
    """
    if not _require_bridge():
        return 1

    branch = bridge_git("branch", "--show-current").stdout.strip()
    if not branch:
        print("Error: not on a branch.", file=sys.stderr)
        return 1

    # Ensure any pending changes are committed
    bridge_git("add", "-A")
    bridge_git("commit", "-m", f"pre-push snapshot {int(time.time())}")

    # Copy git repo to host temp dir and push (uses host credentials)
    tmpdir = tempfile.mkdtemp(prefix="rsi-push-")
    try:
        print(f"Pushing {branch}...")
        r = run([DOCKER, "cp", f"{BRIDGE_CONTAINER}:{_GIT_REPO}/.", tmpdir])
        if r.returncode != 0:
            print(f"Error copying git repo: {r.stderr.strip()}", file=sys.stderr)
            return 1

        # Ensure remote URL is correct in the copied repo
        run(["git", "-C", tmpdir, "remote", "set-url", "origin", GITHUB_REMOTE])

        # Push from host (has macOS keychain / credential helper)
        r = run(["git", "-C", tmpdir, "push", "-u", "origin", branch])
        if r.returncode != 0:
            # Show stderr but also stdout (git push sometimes uses stderr for info)
            err = r.stderr.strip() or r.stdout.strip()
            print(f"Push failed: {err}", file=sys.stderr)
            return 1

        # Clear push_requested flag in workspace
        bridge_sh(f"rm -f {_WORKSPACE}/.push_requested")
        print(f"Pushed {branch}")
        return 0
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def cmd_list(_args: argparse.Namespace) -> int:
    """List all session branches (local in Docker + remote on GitHub)."""
    if not _require_bridge():
        return 1

    # Local branches from bridge container
    r = bridge_git("branch", "--format=%(refname:short)")
    local_branches = [b for b in r.stdout.strip().split("\n") if b] if r.stdout.strip() else []
    current = bridge_git("branch", "--show-current").stdout.strip()

    # Remote branches from GitHub (queried from host)
    remote_branches: list[str] = []
    r = run(["git", "ls-remote", "--heads", GITHUB_REMOTE])
    if r.returncode == 0 and r.stdout.strip():
        for line in r.stdout.strip().split("\n"):
            parts = line.split("\t")
            if len(parts) == 2 and parts[1].startswith("refs/heads/"):
                remote_branches.append("origin/" + parts[1][len("refs/heads/"):])

    if not local_branches and not remote_branches:
        print("No branches found.")
        return 0

    if local_branches:
        print("Local branches:")
        for b in local_branches:
            marker = "* " if b == current else "  "
            info = bridge_git("log", "-1", "--format=%h %s (%cr)", b)
            count = bridge_git("rev-list", "--count", b)
            print(f"  {marker}{b}  [{count.stdout.strip()} commits]  {info.stdout.strip()}")

    if remote_branches:
        print("\nRemote branches (GitHub):")
        for b in remote_branches:
            print(f"    {b}")

    return 0


def cmd_reset(_args: argparse.Namespace) -> int:
    """Nuclear reset: wipe git repo and workspace, recreate from seed.

    This destroys ALL local session branches. Remote branches on GitHub are
    preserved. Use this when the workspace is corrupted or you want a fresh start.
    """
    _stop_sandbox()

    if not _require_bridge():
        return 1

    print("Resetting workspace and git repo to canonical seed...")

    # Nuke git repo
    bridge_sh(f"rm -rf {_GIT_REPO}/.git")

    # Reset workspace to seed
    _copy_seed_to_workspace()

    # Re-initialize
    bridge_sh(f"git init {_GIT_REPO}")
    bridge_git("add", "-A")
    bridge_git("commit", "-m", "canonical seed v2")
    bridge_git("branch", "-M", "main")
    bridge_git("remote", "add", "origin", GITHUB_REMOTE)

    print("Reset complete. Workspace restored to canonical seed.")
    print("Run: python cli/session.py new --name <name>")
    return 0


def cmd_fork(args: argparse.Namespace) -> int:
    """Fork a new session from another session's end state.

    Creates a new session branch starting from the tip of the source branch.
    The workspace is restored to match the source branch's state.
    """
    source = args.branch
    if not source.startswith("session/"):
        source = f"session/{source}"
    name = args.name or f"fork-{int(time.time())}"
    branch = f"session/{name}"

    if not _ensure_repo():
        return 1

    # Verify source exists
    if bridge_git("rev-parse", "--verify", source).returncode != 0:
        print(f"Error: branch '{source}' not found.", file=sys.stderr)
        r = bridge_git("branch", "--format=%(refname:short)")
        if r.stdout.strip():
            sessions = [b for b in r.stdout.strip().split("\n") if b.startswith("session/")]
            if sessions:
                print(f"Available: {', '.join(sessions)}", file=sys.stderr)
        return 1

    _stop_sandbox()

    # Checkout source branch (restores workspace to that branch's state)
    r = bridge_git("checkout", source)
    if r.returncode != 0:
        print(f"Error: {r.stderr.strip()}", file=sys.stderr)
        return 1

    # Create new branch from source
    r = bridge_git("checkout", "-b", branch)
    if r.returncode != 0:
        print(f"Error: {r.stderr.strip()}", file=sys.stderr)
        return 1

    # Ensure workspace matches the branch
    bridge_git("checkout", "--", ".")

    _start_sandbox()
    print(f"Forked {source} -> {branch}")
    return 0


def cmd_pause(_args: argparse.Namespace) -> int:
    """Pause the agent (creates .paused flag in workspace)."""
    if not _container_running(SANDBOX_CONTAINER):
        print("Sandbox not running.", file=sys.stderr)
        return 1
    r = docker_compose("exec", "sandbox", "touch", "/workspace/agent/.paused")
    if r.returncode != 0:
        print(f"Error: {r.stderr.strip()}", file=sys.stderr)
        return 1
    print("Agent paused.")
    return 0


def cmd_resume(_args: argparse.Namespace) -> int:
    """Resume the agent (creates .resume flag, removes .paused)."""
    if not _container_running(SANDBOX_CONTAINER):
        print("Sandbox not running.", file=sys.stderr)
        return 1
    docker_compose("exec", "sandbox", "touch", "/workspace/agent/.resume")
    docker_compose("exec", "sandbox", "rm", "-f", "/workspace/agent/.paused")
    print("Agent resumed.")
    return 0


# --- CLI entry point ---

def main() -> int:
    parser = argparse.ArgumentParser(
        description="RSI-Econ session management",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  python cli/session.py new --name experiment-003
  python cli/session.py push
  python cli/session.py fork experiment-002 --name experiment-003
  python cli/session.py reset""",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="Show current session state")

    new_p = sub.add_parser("new", help="New session from canonical seed")
    new_p.add_argument("--name", help="Session name (default: auto-generated)")

    sub.add_parser("push", help="Push current session branch to GitHub")
    sub.add_parser("list", help="List session branches (local + remote)")
    sub.add_parser("reset", help="Nuclear reset to canonical seed")

    fork_p = sub.add_parser("fork", help="Fork from another session")
    fork_p.add_argument("branch", help="Source session (e.g. experiment-002)")
    fork_p.add_argument("--name", help="New session name")

    sub.add_parser("pause", help="Pause agent")
    sub.add_parser("resume", help="Resume agent")

    args = parser.parse_args()
    commands = {
        "status": cmd_status,
        "new": cmd_new,
        "push": cmd_push,
        "list": cmd_list,
        "reset": cmd_reset,
        "fork": cmd_fork,
        "pause": cmd_pause,
        "resume": cmd_resume,
    }

    if args.command not in commands:
        parser.print_help()
        return 1
    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
