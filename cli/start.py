#!/usr/bin/env python3
"""One-command start for the RSI-Econ stack.

Builds images (cache-busted), starts all Docker services, waits for health,
kills stale discord_bot processes, and relaunches the bot.

Usage:
    python cli/start.py [--fresh] [--model MODEL] [--budget BUDGET]

Flags:
    --fresh   Nuke agent-workspace and git-repo volumes before starting
    --model   LLM model name (sets RSI_MODEL env var)
    --budget  Budget in USD (sets RSI_BUDGET_USD env var)
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib import request as urllib_request

PROJECT_DIR = Path(__file__).resolve().parents[1]
COMPOSE_FILE = PROJECT_DIR / "docker-compose.yml"
WALLET_URL = "http://localhost:8081"


# --- Docker detection (shared with session.py) ---

def _find_docker() -> str:
    for p in ["/usr/local/bin/docker", "/opt/homebrew/bin/docker"]:
        if Path(p).exists():
            return p
    r = subprocess.run(["which", "docker"], capture_output=True, text=True, check=False)
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    print("Error: docker not found.", file=sys.stderr)
    sys.exit(1)


DOCKER = _find_docker()


def docker_compose(*args: str, **kw) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [DOCKER, "compose", "-f", str(COMPOSE_FILE), *args],
        text=True, check=False, **kw,
    )


# --- Discord bot management ---

def kill_discord_bot() -> None:
    """Kill any running discord_bot.py processes."""
    r = subprocess.run(
        ["pgrep", "-f", "discord_bot.py"],
        capture_output=True, text=True, check=False,
    )
    if r.returncode == 0 and r.stdout.strip():
        for pid_str in r.stdout.strip().split("\n"):
            pid_str = pid_str.strip()
            if pid_str:
                try:
                    os.kill(int(pid_str), signal.SIGTERM)
                    print(f"  Killed discord_bot.py (PID {pid_str})")
                except (ProcessLookupError, ValueError):
                    pass


def _load_env() -> dict[str, str]:
    """Load .env file into a dict, layered on top of os.environ."""
    env = os.environ.copy()
    env_file = PROJECT_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip())
    return env


def start_discord_bot(env_overrides: dict[str, str] | None = None) -> int | None:
    """Start discord_bot.py in background. Returns PID or None if skipped."""
    env = _load_env()
    if env_overrides:
        env.update(env_overrides)

    if not env.get("DISCORD_BOT_TOKEN"):
        print("  Discord bot: skipped (no DISCORD_BOT_TOKEN)")
        return None

    bot_script = PROJECT_DIR / "cli" / "discord_bot.py"
    proc = subprocess.Popen(
        [sys.executable, str(bot_script)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    print(f"  Discord bot: started (PID {proc.pid})")
    return proc.pid


# --- Health checks ---

def wait_healthy(url: str, label: str, timeout: int = 90) -> bool:
    """Poll a URL until it returns HTTP 200."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            req = urllib_request.Request(url, method="GET")
            with urllib_request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(2)
    return False


def check_compose_health() -> list[dict]:
    """Return a list of service dicts from docker compose ps."""
    r = docker_compose("ps", "--format", "json", capture_output=True)
    if r.returncode != 0:
        return []
    raw = r.stdout.strip()
    if not raw:
        return []
    # Some docker compose versions return a JSON array, others JSON lines.
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return parsed
        return [parsed]
    except json.JSONDecodeError:
        pass
    # Fall back to JSON-lines parsing
    services = []
    for line in raw.split("\n"):
        line = line.strip()
        if line:
            try:
                services.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return services


# --- Main ---

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Start the RSI-Econ stack")
    parser.add_argument("--fresh", action="store_true",
                        help="Nuke agent-workspace and git-repo volumes first")
    parser.add_argument("--model", default=None, help="LLM model name")
    parser.add_argument("--budget", default=None, help="Budget in USD")
    args = parser.parse_args(argv)

    print("=== RSI-Econ Start ===")

    # Step 1: Tear down
    if args.fresh:
        print("\n[1/6] Nuking volumes (--fresh)...")
        docker_compose("down", "-v")
    else:
        print("\n[1/6] Stopping existing services...")
        docker_compose("down")

    # Step 2: Clear logs
    print("\n[2/6] Clearing logs...")
    logs_dir = PROJECT_DIR / "state" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    for f in logs_dir.glob("*.jsonl"):
        f.write_text("", encoding="utf-8")
        print(f"  Cleared {f.name}")

    # Step 3: Build with cache bust
    print("\n[3/6] Building images (cache-busted)...")
    cache_bust = str(int(time.time()))
    r = docker_compose(
        "build",
        "--build-arg", f"CACHE_BUST={cache_bust}",
        "sandbox", "bridge",
        capture_output=True,
    )
    if r.returncode != 0:
        print(f"Build failed:\n{r.stderr}", file=sys.stderr)
        return 1
    print(f"  Images built (CACHE_BUST={cache_bust})")

    # Step 4: Set env vars and start
    print("\n[4/6] Starting services...")
    env_overrides: dict[str, str] = {}
    if args.budget:
        env_overrides["RSI_BUDGET_USD"] = args.budget
    if args.model:
        env_overrides["RSI_MODEL"] = args.model

    r = docker_compose("up", "-d", capture_output=True)
    if r.returncode != 0:
        print(f"Start failed:\n{r.stderr}", file=sys.stderr)
        return 1
    print("  Services started")

    # Step 5: Health checks
    print("\n[5/6] Waiting for health checks...")
    all_healthy = True
    health_checks = [
        (f"{WALLET_URL}/healthz", "bridge"),
        (f"{WALLET_URL}/wallet", "wallet API"),
    ]
    for url, label in health_checks:
        if wait_healthy(url, label, timeout=90):
            print(f"  {label}: healthy")
        else:
            print(f"  {label}: UNHEALTHY")
            all_healthy = False

    # Also show compose service states
    for svc in check_compose_health():
        name = svc.get("Service", svc.get("Name", "?"))
        state = svc.get("State", svc.get("Health", "?"))
        print(f"  {name}: {state}")

    # Step 6: Discord bot
    print("\n[6/6] Discord bot...")
    kill_discord_bot()
    bot_pid = start_discord_bot(env_overrides)

    # Summary
    budget = args.budget or "5.00"
    model = args.model or "default"
    print("\n=== Status ===")
    print(f"Budget:  ${budget}")
    print(f"Model:   {model}")
    print(f"Bridge:  {WALLET_URL}")
    if bot_pid:
        print(f"Discord: running (PID {bot_pid})")
    else:
        print("Discord: not configured")

    if all_healthy:
        print("\nAll services healthy.")
    else:
        print("\nWARNING: Some services unhealthy!", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
