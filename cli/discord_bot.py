#!/usr/bin/env python3
"""RSI-Econ Discord bot — operator dashboard for monitoring and controlling the agent.

Usage:
    python cli/discord_bot.py

Requires:
    pip install discord.py>=2.0

Environment variables (set in .env):
    DISCORD_BOT_TOKEN   — Bot token (required)
    DISCORD_GUILD_ID    — Server ID (required)
    RSI_BRIDGE_URL      — Bridge URL (default: http://localhost:8081)
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

import discord
from discord import app_commands
from discord.ext import tasks

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("rsi-bot")

# --- Paths (host-side, relative to project root) ---
ROOT = Path(__file__).resolve().parents[1]
EVENTS_DIR = ROOT / "state" / "events"
STATE_FILE = ROOT / "state" / "discord_state.json"
COMPOSE_FILE = ROOT / "docker-compose.yml"
PROXY_LOG = ROOT / "state" / "logs" / "web_egress.jsonl"

BRIDGE_URL = os.getenv("RSI_BRIDGE_URL", "http://localhost:8081")
GUILD_ID = os.getenv("DISCORD_GUILD_ID", "")

# Channel names the bot will auto-create
CHANNEL_NAMES = {
    "alerts": "rsi-alerts",
    "approvals": "rsi-approvals",
    "evolution": "rsi-evolution",
    "activity": "rsi-activity",
}

# Embed colors
COLOR_GREEN = 0x2ECC71
COLOR_RED = 0xE74C3C
COLOR_YELLOW = 0xF1C40F
COLOR_ORANGE = 0xE67E22
COLOR_BLUE = 0x3498DB
COLOR_GRAY = 0x95A5A6


# ── Bridge HTTP helpers ──────────────────────────────────────────────


def bridge_get(path: str) -> Any:
    """GET request to bridge API. Returns parsed JSON or None on failure."""
    try:
        req = urllib_request.Request(f"{BRIDGE_URL}{path}", method="GET")
        with urllib_request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        log.warning("bridge GET %s failed: %s", path, exc)
        return None


def bridge_post(path: str, payload: dict | None = None) -> Any:
    """POST request to bridge API. Returns parsed JSON or None on failure."""
    body = json.dumps(payload or {}).encode("utf-8")
    try:
        req = urllib_request.Request(
            f"{BRIDGE_URL}{path}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib_request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        log.warning("bridge POST %s failed: %s", path, exc)
        return None


def bridge_get_bytes(path: str) -> bytes | None:
    """GET binary content from bridge API."""
    try:
        req = urllib_request.Request(f"{BRIDGE_URL}{path}", method="GET")
        with urllib_request.urlopen(req, timeout=10) as resp:
            return resp.read()
    except Exception:
        return None


# ── Docker helpers ───────────────────────────────────────────────────


DOCKER_BIN = "/usr/local/bin/docker"


def docker_compose(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [DOCKER_BIN, "compose", "-f", str(COMPOSE_FILE), *args],
        capture_output=True, text=True, check=False, timeout=30,
    )


def get_agent_logs(tail: int = 30) -> str:
    result = docker_compose("logs", "--tail", str(tail), "--no-color", "sandbox")
    return result.stdout if result.returncode == 0 else ""


def is_sandbox_running() -> bool:
    ps = docker_compose("ps", "--format", "json", "sandbox")
    if ps.returncode != 0 or not ps.stdout.strip():
        return False
    try:
        for line in ps.stdout.strip().split("\n"):
            info = json.loads(line)
            if info.get("State") == "running":
                return True
    except (json.JSONDecodeError, KeyError):
        pass
    return False


# ── Embed builders ───────────────────────────────────────────────────


def budget_bar(remaining: float, total: float, width: int = 18) -> str:
    pct = (remaining / total * 100) if total > 0 else 0
    filled = int(pct / 100 * width)
    return "\u2588" * filled + "\u2591" * (width - filled)


def build_status_embed(wallet: dict, git_log: list, agent_status: dict) -> discord.Embed:
    running = is_sandbox_running()
    paused = agent_status.get("paused", False)

    if paused:
        status_text = "\u23f8\ufe0f PAUSED"
        color = COLOR_YELLOW
    elif running:
        status_text = "\U0001f7e2 RUNNING"
        color = COLOR_GREEN
    else:
        status_text = "\U0001f534 STOPPED"
        color = COLOR_GRAY

    remaining = wallet.get("remaining_usd", 0)
    total = wallet.get("budget_usd", 0)
    pct = (remaining / total * 100) if total > 0 else 0
    bar = budget_bar(remaining, total)

    embed = discord.Embed(title="RSI-Econ Agent Status", color=color)
    embed.add_field(name="Status", value=status_text, inline=True)

    # Model
    models = wallet.get("models_available", [])
    model = models[0] if models else "unknown"
    embed.add_field(name="Model", value=f"`{model}`", inline=True)

    # Budget
    embed.add_field(
        name="Budget",
        value=f"${remaining:.2f} / ${total:.2f} ({pct:.0f}%)\n`{bar}`",
        inline=False,
    )

    # Git
    commit_count = len(git_log) if git_log else 0
    last_commit = git_log[0]["message"] if git_log else "none"
    embed.add_field(name="Git", value=f"{commit_count} commits\nLast: {last_commit}", inline=True)

    # Context
    ctx = agent_status.get("agent_status", {})
    if ctx:
        msgs = ctx.get("messages", "?")
        tokens = ctx.get("tokens", 0)
        turn = ctx.get("turn", "?")
        embed.add_field(name="Context", value=f"{msgs}/40 msgs, ~{tokens // 1000}k tokens\nTurn: ~{turn}", inline=True)

    # Findings
    knowledge = agent_status.get("knowledge", {})
    findings = len(knowledge.get("findings", []))
    if findings:
        embed.add_field(name="Findings", value=f"{findings} entries", inline=True)

    # Requests
    avg_cost = wallet.get("avg_cost_per_request", 0)
    total_reqs = wallet.get("total_requests", 0)
    embed.add_field(name="Requests", value=f"{total_reqs} total, ${avg_cost:.4f}/call avg", inline=True)

    embed.timestamp = datetime.now(timezone.utc)
    return embed


def build_summary_embed(summary: str, wallet: dict) -> discord.Embed:
    remaining = wallet.get("remaining_usd", 0)
    total = wallet.get("budget_usd", 0)
    embed = discord.Embed(title="\U0001f4ca Activity Summary", description=summary, color=COLOR_BLUE)
    embed.add_field(name="Budget", value=f"${remaining:.2f} / ${total:.2f}", inline=True)
    embed.add_field(name="Requests", value=str(wallet.get("total_requests", 0)), inline=True)
    embed.timestamp = datetime.now(timezone.utc)
    return embed


def build_alert_embed(title: str, message: str, color: int, mention: bool = False) -> discord.Embed:
    embed = discord.Embed(title=title, description=message, color=color)
    embed.timestamp = datetime.now(timezone.utc)
    return embed


def build_proposal_embed(data: dict) -> discord.Embed:
    method = data.get("method", "?")
    domain = data.get("domain", "unknown")
    url = data.get("url", "")
    proposal_id = data.get("proposal_id", "?")
    description = data.get("description", f"Agent wants to {method} {domain}")

    embed = discord.Embed(
        title=f"\U0001f527 Proposal: {method} {domain}",
        description=description,
        color=COLOR_YELLOW,
    )
    embed.add_field(name="Domain", value=domain, inline=True)
    embed.add_field(name="Method", value=method, inline=True)
    if url:
        embed.add_field(name="Path", value=url[:100], inline=False)
    embed.set_footer(text=f"ID: {proposal_id}\nReact \u2705 to approve, \u274c to reject")
    embed.timestamp = datetime.now(timezone.utc)
    return embed


def _parse_diff(diff_text: str) -> dict:
    """Parse a git diff to extract file names and line counts."""
    files = []
    added = 0
    removed = 0
    for line in diff_text.split("\n"):
        if line.startswith("diff --git"):
            # "diff --git a/foo.py b/foo.py" → "foo.py"
            parts = line.split(" b/", 1)
            if len(parts) == 2:
                files.append(parts[1])
        elif line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return {"files": files, "added": added, "removed": removed}


def build_evolution_embed(
    data: dict, message: str, *, llm_summary: str = "", diff_text: str = "",
) -> discord.Embed:
    parsed = _parse_diff(diff_text) if diff_text else {"files": [], "added": 0, "removed": 0}
    files = parsed["files"] or data.get("files_changed", "").split(", ") if data.get("files_changed") else parsed["files"]
    file_list = ", ".join(f"`{f}`" for f in files) if files else "(unknown)"

    title = f"\U0001f9ec Self-Edit: {', '.join(files)}" if files else "\U0001f9ec Self-Edit"
    # Discord embed titles max 256 chars
    if len(title) > 256:
        title = title[:253] + "..."

    embed = discord.Embed(title=title, color=COLOR_GREEN)

    if llm_summary:
        embed.description = llm_summary

    commit_hash = data.get("commit_hash", "")
    if commit_hash:
        embed.add_field(name="Commit", value=f"`{commit_hash[:8]}`", inline=True)

    embed.add_field(name="Files", value=file_list[:200], inline=True)
    embed.add_field(
        name="Lines",
        value=f"+{parsed['added']} / -{parsed['removed']}",
        inline=True,
    )

    # Collapsed raw diff at the bottom, truncated
    if diff_text:
        truncated = diff_text[:500]
        if len(diff_text) > 500:
            truncated += "\n... (truncated)"
        embed.add_field(name="Raw Diff", value=f"```diff\n{truncated}\n```", inline=False)

    embed.timestamp = datetime.now(timezone.utc)
    return embed


# ── Bot state persistence ────────────────────────────────────────────


class BotState:
    def __init__(self) -> None:
        self.channels: dict[str, int] = {}
        self.active_session: str = ""
        self.threads: dict[str, int] = {}
        self.approval_messages: dict[str, int] = {}
        self._load()

    def _load(self) -> None:
        if STATE_FILE.exists():
            try:
                data = json.loads(STATE_FILE.read_text("utf-8"))
                self.channels = data.get("channels", {})
                self.active_session = data.get("active_session", "")
                self.threads = data.get("threads", {})
                self.approval_messages = data.get("approval_messages", {})
            except Exception:
                pass

    def save(self) -> None:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps({
            "channels": self.channels,
            "active_session": self.active_session,
            "threads": self.threads,
            "approval_messages": self.approval_messages,
        }, indent=2))


# ── Main Bot class ───────────────────────────────────────────────────


class RSIBot(discord.Client):
    def __init__(self, guild_id: int) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.reactions = True
        intents.guilds = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.guild_id = guild_id
        self.guild_obj: discord.Guild | None = None
        self.state = BotState()
        self._setup_commands()

    async def on_ready(self) -> None:
        log.info("Bot connected as %s", self.user)
        self.guild_obj = self.get_guild(self.guild_id)
        if not self.guild_obj:
            log.error("Guild %s not found! Check DISCORD_GUILD_ID.", self.guild_id)
            return

        await self._ensure_channels()
        guild = discord.Object(id=self.guild_id)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        log.info("Slash commands synced to guild %s", self.guild_id)

        if not self.event_poll_loop.is_running():
            self.event_poll_loop.start()
        if not self.summary_loop.is_running():
            self.summary_loop.start()

    async def _ensure_channels(self) -> None:
        """Create rsi-* channels if they don't exist."""
        if not self.guild_obj:
            return
        existing = {ch.name: ch.id for ch in self.guild_obj.text_channels}
        for key, name in CHANNEL_NAMES.items():
            if key in self.state.channels:
                # Verify it still exists
                ch = self.guild_obj.get_channel(self.state.channels[key])
                if ch:
                    continue
            if name in existing:
                self.state.channels[key] = existing[name]
            else:
                ch = await self.guild_obj.create_text_channel(name)
                self.state.channels[key] = ch.id
                log.info("Created channel #%s (%s)", name, ch.id)
        self.state.save()

    def _get_channel(self, key: str) -> discord.TextChannel | None:
        cid = self.state.channels.get(key)
        if not cid or not self.guild_obj:
            return None
        ch = self.guild_obj.get_channel(cid)
        return ch if isinstance(ch, discord.TextChannel) else None

    async def _get_or_create_thread(self, channel_key: str, thread_key: str, name: str) -> discord.Thread | None:
        """Get an existing thread or create a new one."""
        ch = self._get_channel(channel_key)
        if not ch:
            return None

        # Check cached thread
        tid = self.state.threads.get(thread_key)
        if tid:
            try:
                thread = ch.get_thread(tid)
                if thread:
                    if thread.archived:
                        await thread.edit(archived=False)
                    return thread
            except Exception:
                pass

        # Create new thread
        return await self._create_new_thread(channel_key, thread_key, name)

    async def _create_new_thread(self, channel_key: str, thread_key: str, name: str) -> discord.Thread | None:
        """Always create a fresh thread, replacing any cached thread ID."""
        ch = self._get_channel(channel_key)
        if not ch:
            return None
        try:
            thread = await ch.create_thread(name=name, type=discord.ChannelType.public_thread)
            self.state.threads[thread_key] = thread.id
            self.state.save()
            return thread
        except Exception as exc:
            log.warning("Failed to create thread %s: %s", name, exc)
            return None

    # ── Event polling ────────────────────────────────────────────────

    @tasks.loop(seconds=10)
    async def event_poll_loop(self) -> None:
        """Poll state/events/*.json and route to Discord channels."""
        if not EVENTS_DIR.exists():
            return
        event_files = sorted(EVENTS_DIR.glob("*.json"))
        for event_file in event_files:
            try:
                raw = event_file.read_text("utf-8")
                event = json.loads(raw)
                await self._route_event(event)
                event_file.unlink()
            except Exception as exc:
                log.warning("Failed to process event %s: %s", event_file.name, exc)
                try:
                    event_file.unlink()
                except OSError:
                    pass
            # Rate limit: avoid Discord API throttling
            await asyncio.sleep(0.5)

    @event_poll_loop.before_loop
    async def before_event_poll(self) -> None:
        await self.wait_until_ready()

    async def _route_event(self, event: dict) -> None:
        event_type = event.get("event", "unknown")
        message = event.get("message", "")
        data = event.get("data") or {}

        if event_type == "session_start":
            await self._handle_session_start(message, data)
        elif event_type == "session_stop":
            await self._handle_session_stop(message, data)
        elif event_type == "self_edit":
            await self._handle_self_edit(message, data)
        elif event_type.startswith("budget_"):
            await self._handle_budget_alert(event_type, message)
        elif event_type == "proposal_submitted":
            await self._handle_proposal(message, data)
        elif event_type in ("proposal_approved", "proposal_rejected"):
            pass  # Handled by reaction callbacks
        elif event_type == "finding":
            await self._handle_finding(message, data)
        else:
            # Generic alert for unknown events
            ch = self._get_channel("alerts")
            if ch:
                embed = build_alert_embed(f"Event: {event_type}", message, COLOR_GRAY)
                await ch.send(embed=embed)

    async def _handle_session_start(self, message: str, data: dict) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        session_name = data.get("session_name", ts)
        self.state.active_session = session_name

        # Post to alerts
        ch = self._get_channel("alerts")
        if ch:
            embed = build_alert_embed("\U0001f7e2 Session Started", message, COLOR_GREEN)
            await ch.send(embed=embed)

        # Close old threads with a farewell message
        for thread_key, channel_key in [("activity", "activity"), ("evolution", "evolution")]:
            old_tid = self.state.threads.get(thread_key)
            if old_tid:
                old_ch = self._get_channel(channel_key)
                if old_ch:
                    try:
                        old_thread = old_ch.get_thread(old_tid)
                        if old_thread and not old_thread.archived:
                            await old_thread.send("\U0001f6d1 Session ended. New session starting.")
                            await old_thread.edit(archived=True)
                    except Exception:
                        pass

        # Always create NEW threads (never reuse)
        self.state.threads.pop("activity", None)
        self.state.threads.pop("evolution", None)

        await self._create_new_thread(
            "activity", "activity",
            f"\U0001f9ea Session {ts}",
        )
        await self._create_new_thread(
            "evolution", "evolution",
            f"\U0001f9ec Evolution {ts}",
        )
        self.state.save()

    async def _handle_session_stop(self, message: str, data: dict) -> None:
        ch = self._get_channel("alerts")
        if ch:
            embed = build_alert_embed("\U0001f534 Session Stopped", message, COLOR_RED)
            await ch.send(content="@here", embed=embed)
        self.state.active_session = ""
        self.state.save()

    async def _handle_self_edit(self, message: str, data: dict) -> None:
        thread = await self._get_or_create_thread(
            "evolution", "evolution",
            f"\U0001f9ec Evolution {self.state.active_session or 'unknown'}",
        )
        if not thread:
            return

        # Fetch the actual diff from the bridge
        diff_text = ""
        diff_data = bridge_get("/git/diff")
        if diff_data and diff_data.get("diff"):
            diff_text = diff_data["diff"]

        # Get LLM summary of the change
        llm_summary = ""
        if diff_text:
            prompt = "Summarize this code change in one sentence. What was changed and why?"
            result = bridge_post("/summarize", {
                "text": f"{prompt}\n\n{diff_text[:3000]}",
                "max_tokens": 80,
            })
            if result and result.get("summary"):
                llm_summary = result["summary"]

        embed = build_evolution_embed(data, message, llm_summary=llm_summary, diff_text=diff_text)
        await thread.send(embed=embed)

    async def _handle_budget_alert(self, event_type: str, message: str) -> None:
        ch = self._get_channel("alerts")
        if not ch:
            return
        if "critical" in event_type:
            embed = build_alert_embed("\U0001f534 CRITICAL Budget", message, COLOR_RED)
            await ch.send(content="@here", embed=embed)
        else:
            color = COLOR_ORANGE if "25" in event_type else COLOR_YELLOW
            embed = build_alert_embed("\u26a0\ufe0f Budget Warning", message, color)
            await ch.send(embed=embed)

    async def _handle_proposal(self, message: str, data: dict) -> None:
        ch = self._get_channel("approvals")
        if not ch:
            return
        embed = build_proposal_embed(data)
        msg = await ch.send(embed=embed)
        await msg.add_reaction("\u2705")
        await msg.add_reaction("\u274c")

        proposal_id = data.get("proposal_id", "")
        if proposal_id:
            self.state.approval_messages[proposal_id] = msg.id
            self.state.save()

    async def _handle_finding(self, message: str, data: dict) -> None:
        thread = await self._get_or_create_thread(
            "activity", "activity",
            f"\U0001f9ea Session {self.state.active_session or 'unknown'}",
        )
        if thread:
            embed = discord.Embed(title="\U0001f4a1 Finding", description=message, color=COLOR_BLUE)
            embed.timestamp = datetime.now(timezone.utc)
            await thread.send(embed=embed)

    # ── Reaction-based approvals ─────────────────────────────────────

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.user_id == self.user.id:
            return
        if str(payload.emoji) not in ("\u2705", "\u274c"):
            return

        # Find proposal_id from message_id
        proposal_id = None
        for pid, mid in self.state.approval_messages.items():
            if mid == payload.message_id:
                proposal_id = pid
                break
        if not proposal_id:
            return

        ch = self.get_channel(payload.channel_id)
        if not ch or not isinstance(ch, discord.TextChannel):
            return

        try:
            msg = await ch.fetch_message(payload.message_id)
        except Exception:
            return

        user = self.get_user(payload.user_id) or await self.fetch_user(payload.user_id)
        user_name = user.display_name if user else "unknown"

        if str(payload.emoji) == "\u2705":
            result = bridge_post(f"/proposals/{proposal_id}/approve")
            if result and result.get("status") == "approved":
                embed = msg.embeds[0] if msg.embeds else discord.Embed()
                embed.color = COLOR_GREEN
                embed.add_field(name="\u2705 Approved", value=f"by {user_name}", inline=False)
                await msg.edit(embed=embed)
                log.info("Proposal %s approved by %s", proposal_id, user_name)
            else:
                # Might already be decided
                await ch.send(f"Could not approve proposal `{proposal_id[:8]}`: {result}", delete_after=10)
        else:
            result = bridge_post(f"/proposals/{proposal_id}/reject", {"reason": f"Rejected by {user_name}"})
            if result:
                embed = msg.embeds[0] if msg.embeds else discord.Embed()
                embed.color = COLOR_RED
                embed.add_field(name="\u274c Rejected", value=f"by {user_name}", inline=False)
                await msg.edit(embed=embed)
                log.info("Proposal %s rejected by %s", proposal_id, user_name)

        # Remove from tracked messages
        self.state.approval_messages.pop(proposal_id, None)
        self.state.save()

    # ── Periodic activity summary (every 5 minutes) ──────────────────

    @tasks.loop(minutes=5)
    async def summary_loop(self) -> None:
        """Post periodic LLM summary to #activity thread."""
        if not self.state.active_session:
            return
        if not is_sandbox_running():
            return

        thread = await self._get_or_create_thread(
            "activity", "activity",
            f"\U0001f9ea Session {self.state.active_session}",
        )
        if not thread:
            return

        # Gather data
        logs = get_agent_logs(30)
        wallet = bridge_get("/wallet") or {}

        # Read proxy logs for domain stats
        domain_stats = ""
        if PROXY_LOG.exists():
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
                domain_stats = ", ".join(f"{d}({c})" for d, c in top)
            except Exception:
                pass

        # Fetch recent reasoning from bridge
        reasoning_text = ""
        reasoning_data = bridge_get("/agent/reasoning?lines=5")
        if reasoning_data and reasoning_data.get("entries"):
            entries = reasoning_data["entries"]
            # Take last 2 for the embed field
            recent = entries[-2:]
            reasoning_text = "\n---\n".join(
                e.get("content", "")[:150] for e in recent if e.get("content")
            )

        # Build summary text
        text = f"Agent logs:\n{logs}\n\nDomains: {domain_stats}"
        result = bridge_post("/summarize", {"text": text, "max_tokens": 150})
        summary = result.get("summary", "(no summary)") if result else "(bridge unreachable)"

        embed = build_summary_embed(summary, wallet)
        if domain_stats:
            embed.add_field(name="Domains", value=domain_stats, inline=False)
        if reasoning_text:
            embed.add_field(
                name="\U0001f4ad Latest Thinking",
                value=reasoning_text[:300],
                inline=False,
            )
        await thread.send(embed=embed)

    @summary_loop.before_loop
    async def before_summary(self) -> None:
        await self.wait_until_ready()

    # ── Slash commands ───────────────────────────────────────────────

    def _setup_commands(self) -> None:
        @self.tree.command(name="status", description="Show current agent status")
        async def cmd_status(interaction: discord.Interaction) -> None:
            await interaction.response.defer()
            wallet = bridge_get("/wallet") or {}
            git_log = bridge_get("/git/log") or []
            agent_status = bridge_get("/agent/status") or {}
            embed = build_status_embed(wallet, git_log, agent_status)
            await interaction.followup.send(embed=embed)

        @self.tree.command(name="summary", description="LLM-generated summary of current agent activity")
        async def cmd_summary(interaction: discord.Interaction) -> None:
            await interaction.response.defer()
            logs = await asyncio.to_thread(get_agent_logs, 30)
            wallet = bridge_get("/wallet") or {}
            text = f"Agent logs:\n{logs}"
            result = bridge_post("/summarize", {"text": text, "max_tokens": 150})
            summary = result.get("summary", "(no summary)") if result else "(bridge unreachable)"
            embed = build_summary_embed(summary, wallet)
            await interaction.followup.send(embed=embed)

        @self.tree.command(name="inject", description="Send operator message to agent")
        @app_commands.describe(message="Message to inject into agent context")
        async def cmd_inject(interaction: discord.Interaction, message: str) -> None:
            result = bridge_post("/operator/inject", {"message": message})
            if result and result.get("status") == "queued":
                await interaction.response.send_message(f"\u2705 Message queued: '{message[:100]}'")
            else:
                await interaction.response.send_message(f"\u274c Failed to inject: {result}")

        @self.tree.command(name="pause", description="Pause the agent")
        async def cmd_pause(interaction: discord.Interaction) -> None:
            result = await asyncio.to_thread(
                docker_compose, "exec", "sandbox", "touch", "/workspace/agent/.paused"
            )
            if result.returncode == 0:
                await interaction.response.send_message("\u23f8\ufe0f Agent paused by operator")
                ch = self._get_channel("alerts")
                if ch:
                    embed = build_alert_embed("\u23f8\ufe0f Agent Paused", "Paused by operator via Discord", COLOR_YELLOW)
                    await ch.send(embed=embed)
            else:
                await interaction.response.send_message(f"\u274c Failed to pause: {result.stderr.strip()}")

        @self.tree.command(name="resume", description="Resume the agent")
        async def cmd_resume(interaction: discord.Interaction) -> None:
            result = await asyncio.to_thread(
                docker_compose, "exec", "sandbox", "bash", "-c",
                "touch /workspace/agent/.resume && rm -f /workspace/agent/.paused",
            )
            if result.returncode == 0:
                await interaction.response.send_message("\u25b6\ufe0f Agent resumed by operator")
                ch = self._get_channel("alerts")
                if ch:
                    embed = build_alert_embed("\u25b6\ufe0f Agent Resumed", "Resumed by operator via Discord", COLOR_GREEN)
                    await ch.send(embed=embed)
            else:
                await interaction.response.send_message(f"\u274c Failed to resume: {result.stderr.strip()}")

        @self.tree.command(name="screenshot", description="Show latest browser screenshot")
        async def cmd_screenshot(interaction: discord.Interaction) -> None:
            await interaction.response.defer()
            img_bytes = bridge_get_bytes("/agent/screenshot")
            if img_bytes:
                file = discord.File(io.BytesIO(img_bytes), filename="screenshot.png")
                embed = discord.Embed(title="\U0001f4f7 Browser Screenshot", color=COLOR_BLUE)
                embed.set_image(url="attachment://screenshot.png")
                embed.timestamp = datetime.now(timezone.utc)
                await interaction.followup.send(embed=embed, file=file)
            else:
                await interaction.followup.send("No recent browser activity (screenshot unavailable or stale)")


# ── Entrypoint ───────────────────────────────────────────────────────


def main() -> None:
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        print("Error: DISCORD_BOT_TOKEN not set", file=sys.stderr)
        print("Set it in .env or export DISCORD_BOT_TOKEN=...", file=sys.stderr)
        sys.exit(1)

    guild_id_str = os.environ.get("DISCORD_GUILD_ID", GUILD_ID)
    if not guild_id_str:
        print("Error: DISCORD_GUILD_ID not set", file=sys.stderr)
        sys.exit(1)

    try:
        guild_id = int(guild_id_str)
    except ValueError:
        print(f"Error: DISCORD_GUILD_ID must be an integer, got '{guild_id_str}'", file=sys.stderr)
        sys.exit(1)

    bot = RSIBot(guild_id=guild_id)
    log.info("Starting RSI-Econ Discord bot...")
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
