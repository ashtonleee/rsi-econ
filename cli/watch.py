#!/usr/bin/env python3
"""RSI-Econ terminal TUI — live-updating operator dashboard.

Usage:
    python cli/watch.py

No external dependencies (uses only stdlib).
Keyboard: [q]uit  [i]nject  [p]ause/resume  [s]ummary
"""

from __future__ import annotations

import json
import os
import re
import select
import subprocess
import sys
import termios
import time
import tty
from pathlib import Path
from urllib import request as urllib_request

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "docker-compose.yml"
PROXY_LOG = ROOT / "state" / "logs" / "web_egress.jsonl"
SEED_DIR = ROOT / "sandbox" / "seed"
BRIDGE_URL = os.getenv("RSI_BRIDGE_URL", "http://localhost:8081")

REFRESH_INTERVAL = 5  # seconds
BOX_WIDTH = 58


# ── Bridge / Docker helpers ──────────────────────────────────────────


def bridge_get(path: str):
    try:
        req = urllib_request.Request(f"{BRIDGE_URL}{path}", method="GET")
        with urllib_request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def bridge_post(path: str, payload: dict | None = None):
    body = json.dumps(payload or {}).encode("utf-8")
    try:
        req = urllib_request.Request(
            f"{BRIDGE_URL}{path}", data=body,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib_request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def docker_compose(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), *args],
        capture_output=True, text=True, check=False, timeout=15,
    )


# ── Data collection ──────────────────────────────────────────────────


def budget_bar(pct: float, width: int = 20) -> str:
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


def get_container_state() -> str:
    ps = docker_compose("ps", "--format", "json", "sandbox")
    if ps.returncode != 0 or not ps.stdout.strip():
        return "STOPPED"
    try:
        for line in ps.stdout.strip().split("\n"):
            info = json.loads(line)
            if info.get("State") == "running":
                return "RUNNING"
    except (json.JSONDecodeError, KeyError):
        pass
    return "STOPPED"


def parse_agent_logs(tail: int = 15) -> dict:
    """Parse recent agent logs for tool calls, thinking, and context info."""
    result = docker_compose("logs", "--tail", str(tail), "--no-color", "sandbox")
    if result.returncode != 0:
        return {"tools": [], "context": "", "raw_lines": []}

    tools = []
    context_line = ""
    raw_lines = []

    for line in result.stdout.strip().split("\n"):
        raw_lines.append(line)
        # Parse tool calls: [agent:model] tool: name
        m = re.search(r"\[agent:\S+\]\s+tool:\s+(\S+)", line)
        if m:
            tools.append(m.group(1))
        # Parse context lines: [agent:model] context: N/M msgs, ~Nk tokens
        m = re.search(r"context:\s+(\d+)/(\d+)\s+msgs,\s+~(\d+)\s+tokens", line)
        if m:
            context_line = f"{m.group(1)}/{m.group(2)} msgs, ~{int(m.group(3)) // 1000}k tokens"
        # Parse turn info
        m = re.search(r"thinking\s+\(turn\s+(\d+)\)", line)
        if m:
            pass  # captured in raw_lines

    return {"tools": tools[-10:], "context": context_line, "raw_lines": raw_lines[-10:]}


def get_domain_stats() -> str:
    if not PROXY_LOG.exists():
        return ""
    try:
        lines = PROXY_LOG.read_text("utf-8").strip().split("\n")[-50:]
        domains: dict[str, int] = {}
        for line in lines:
            try:
                entry = json.loads(line)
                host = entry.get("host", entry.get("domain", ""))
                if host:
                    domains[host] = domains.get(host, 0) + 1
            except json.JSONDecodeError:
                continue
        top = sorted(domains.items(), key=lambda x: -x[1])[:5]
        return "  ".join(f"{d}({c})" for d, c in top)
    except Exception:
        return ""


def collect_data() -> dict:
    wallet = bridge_get("/wallet") or {}
    git_log = bridge_get("/git/log") or []
    proposals = bridge_get("/proposals") or []
    agent_status = bridge_get("/agent/status") or {}
    container = get_container_state()
    logs = parse_agent_logs()
    domains = get_domain_stats()

    paused = agent_status.get("paused", False)
    if paused:
        status = "PAUSED"
    elif container == "RUNNING":
        status = "RUNNING"
    else:
        status = "STOPPED"

    return {
        "wallet": wallet,
        "git_log": git_log,
        "proposals": proposals,
        "agent_status": agent_status,
        "container": container,
        "status": status,
        "logs": logs,
        "domains": domains,
    }


# ── ANSI rendering ───────────────────────────────────────────────────

RESET = "\033[0m"
BOLD = "\033[1m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
CYAN = "\033[36m"
DIM = "\033[2m"


def pad(text: str, width: int) -> str:
    """Pad text to width, accounting for ANSI escape codes."""
    visible_len = len(re.sub(r"\033\[[0-9;]*m", "", text))
    return text + " " * max(0, width - visible_len)


def render(data: dict) -> None:
    w = data["wallet"]
    remaining = w.get("remaining_usd", 0)
    total = w.get("budget_usd", 0)
    pct = (remaining / total * 100) if total > 0 else 0
    bar = budget_bar(pct)
    spent = w.get("spent_usd", 0)
    reqs = w.get("total_requests", 0)
    avg = w.get("avg_cost_per_request", 0)

    # Status color
    status = data["status"]
    if status == "RUNNING":
        status_str = f"{GREEN}{BOLD}\U0001f7e2 RUNNING{RESET}"
    elif status == "PAUSED":
        status_str = f"{YELLOW}{BOLD}\u23f8\ufe0f  PAUSED{RESET}"
    else:
        status_str = f"{RED}{BOLD}\U0001f534 STOPPED{RESET}"

    # Git
    git_log = data["git_log"]
    commit_count = len(git_log)
    last_msg = git_log[0]["message"][:30] if git_log else "none"

    # Model
    models = w.get("models_available", [])
    model = models[0] if models else "unknown"

    # Context
    ctx = data["agent_status"].get("agent_status", {})
    ctx_str = data["logs"].get("context", "")
    if not ctx_str and ctx:
        msgs = ctx.get("messages", "?")
        tokens = ctx.get("tokens", 0)
        ctx_str = f"{msgs}/40 msgs, ~{tokens // 1000}k tokens"
    turn = ctx.get("turn", "?") if ctx else "?"

    # Proposals
    proposals = data["proposals"]
    pending = sum(1 for p in proposals if p.get("status") == "pending")
    approved = sum(1 for p in proposals if p.get("status") == "approved")
    rejected = sum(1 for p in proposals if p.get("status") == "rejected")

    # Findings
    knowledge = data["agent_status"].get("knowledge", {})
    findings = len(knowledge.get("findings", []))

    # Tools
    tools = data["logs"].get("tools", [])
    tool_str = " \u2192 ".join(tools[-5:]) if tools else "(none)"

    # Domains
    domains = data["domains"]

    # Build output
    W = BOX_WIDTH
    lines = []
    lines.append(f"{BOLD}\u2554{'═' * W}\u2557{RESET}")
    lines.append(f"\u2551  {BOLD}RSI-ECON AGENT MONITOR{RESET}" + " " * (W - 24) + "\u2551")
    lines.append(f"\u2560{'═' * W}\u2563")

    lines.append(f"\u2551  Status: {pad(status_str, W - 12 + 14)}\u2551")  # +14 for ANSI codes
    lines.append(f"\u2551  Budget: ${remaining:.2f}/${total:.2f} ({pct:.0f}%)" + " " * max(0, W - 32) + "\u2551")
    lines.append(f"\u2551  {DIM}{bar}{RESET}" + " " * max(0, W - len(bar) - 2) + "\u2551")
    lines.append(f"\u2551  Model: {model:<20} Turn: ~{turn}" + " " * max(0, W - 35 - len(str(model)) - len(str(turn))) + "\u2551")
    lines.append(f"\u2551  Git: {commit_count} commits ({last_msg})" + " " * max(0, W - 18 - len(last_msg) - len(str(commit_count))) + "\u2551")
    lines.append(f"\u2551  Context: {ctx_str or '(unavailable)'}" + " " * max(0, W - 12 - len(ctx_str or '(unavailable)')) + "\u2551")

    lines.append(f"\u2560{'═' * W}\u2563")

    lines.append(f"\u2551  {CYAN}Last tools:{RESET} {tool_str[:W - 14]}" + " " * max(0, W - 14 - len(tool_str[:W - 14])) + "\u2551")
    lines.append(f"\u2551  Domains: {(domains or '(none)')[:W - 12]}" + " " * max(0, W - 12 - len((domains or '(none)')[:W - 12])) + "\u2551")
    lines.append(f"\u2551  Findings: {findings:<6} Proposals: {pending}p {approved}a {rejected}r" + " " * max(0, W - 40 - len(str(findings)) - len(str(pending)) - len(str(approved)) - len(str(rejected))) + "\u2551")

    lines.append(f"\u2560{'═' * W}\u2563")
    lines.append(f"\u2551  {DIM}[q]uit  [i]nject  [p]ause/resume  [s]ummary{RESET}" + " " * max(0, W - 45) + "\u2551")
    lines.append(f"\u255a{'═' * W}\u255d")

    # Clear screen and print
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.write("\n".join(lines))
    sys.stdout.write("\n")
    sys.stdout.flush()


# ── Keyboard input ───────────────────────────────────────────────────


def get_keypress(timeout: float = 0.1) -> str | None:
    """Non-blocking keypress read."""
    rlist, _, _ = select.select([sys.stdin], [], [], timeout)
    if rlist:
        return sys.stdin.read(1)
    return None


def pause_agent() -> None:
    result = docker_compose("exec", "sandbox", "touch", "/workspace/agent/.paused")
    if result.returncode == 0:
        print("\n\u23f8\ufe0f  Agent paused.")
    else:
        print(f"\n\u274c Pause failed: {result.stderr.strip()}")
    time.sleep(1)


def resume_agent() -> None:
    result = docker_compose(
        "exec", "sandbox", "bash", "-c",
        "touch /workspace/agent/.resume && rm -f /workspace/agent/.paused",
    )
    if result.returncode == 0:
        print("\n\u25b6\ufe0f  Agent resumed.")
    else:
        print(f"\n\u274c Resume failed: {result.stderr.strip()}")
    time.sleep(1)


def inject_message() -> None:
    # Temporarily restore terminal for input
    sys.stdout.write("\n\033[K")
    sys.stdout.write("Message to inject: ")
    sys.stdout.flush()

    # Need to restore normal terminal mode for input
    old_settings = termios.tcgetattr(sys.stdin)
    try:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
        tty.setcbreak(sys.stdin.fileno())
        # Read character by character until Enter
        chars = []
        while True:
            ch = sys.stdin.read(1)
            if ch in ("\n", "\r"):
                break
            if ch == "\x7f":  # backspace
                if chars:
                    chars.pop()
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
                continue
            chars.append(ch)
            sys.stdout.write(ch)
            sys.stdout.flush()
        message = "".join(chars).strip()
    finally:
        pass

    if message:
        result = bridge_post("/operator/inject", {"message": message})
        if result and result.get("status") == "queued":
            print(f"\n\u2705 Injected: '{message[:60]}'")
        else:
            print(f"\n\u274c Failed to inject")
    else:
        print("\n(cancelled)")
    time.sleep(1)


def show_summary() -> None:
    print("\n\U0001f504 Generating summary...")
    logs_result = docker_compose("logs", "--tail", "30", "--no-color", "sandbox")
    logs = logs_result.stdout if logs_result.returncode == 0 else ""
    result = bridge_post("/summarize", {"text": logs, "max_tokens": 150})
    summary = result.get("summary", "(no summary)") if result else "(bridge unreachable)"
    print(f"\n\U0001f4ca {summary}")
    print(f"\n{DIM}Press any key to continue...{RESET}")
    # Wait for keypress
    for _ in range(100):
        if get_keypress(0.1):
            break


# ── Main loop ────────────────────────────────────────────────────────


def main() -> int:
    old_settings = termios.tcgetattr(sys.stdin)
    try:
        tty.setcbreak(sys.stdin.fileno())
        sys.stdout.write("\033[?25l")  # hide cursor

        while True:
            try:
                data = collect_data()
            except Exception:
                data = {
                    "wallet": {}, "git_log": [], "proposals": [],
                    "agent_status": {}, "container": "UNKNOWN",
                    "status": "UNKNOWN", "logs": {"tools": [], "context": "", "raw_lines": []},
                    "domains": "",
                }
            render(data)

            # Wait REFRESH_INTERVAL, checking for keypresses
            for _ in range(REFRESH_INTERVAL * 10):
                key = get_keypress(0.1)
                if key == "q":
                    return 0
                elif key == "i":
                    inject_message()
                    break
                elif key == "p":
                    agent_status = bridge_get("/agent/status") or {}
                    if agent_status.get("paused"):
                        resume_agent()
                    else:
                        pause_agent()
                    break
                elif key == "s":
                    show_summary()
                    break
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write("\033[?25h")  # show cursor
        sys.stdout.write("\033[2J\033[H")  # clear screen
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
        print("Goodbye.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
