#!/usr/bin/env python3
"""Stop the RSI-Econ stack cleanly.

Kills the Discord bot, then runs docker compose down (preserving volumes).

Usage:
    python cli/stop.py
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
COMPOSE_FILE = PROJECT_DIR / "docker-compose.yml"


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


def _kill_discord_bot() -> None:
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


def main(argv: list[str] | None = None) -> int:
    print("=== RSI-Econ Stop ===")

    # 1. Kill Discord bot
    print("Stopping Discord bot...")
    _kill_discord_bot()

    # 2. Docker compose down (preserve volumes for resume)
    print("Stopping Docker services...")
    r = docker_compose("down", capture_output=True)
    if r.returncode != 0:
        print(f"Warning: docker compose down failed:\n{r.stderr}", file=sys.stderr)

    print("All stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
