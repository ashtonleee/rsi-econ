"""Tests for the Discord bot — helper functions tested in isolation (no Discord connection)."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

discord = pytest.importorskip("discord", reason="discord.py not installed")

ROOT = Path(__file__).resolve().parents[2]
BOT_PATH = ROOT / "cli" / "discord_bot.py"


def load_bot_module(tmp_path: Path):
    """Load discord_bot module with test paths."""
    os.environ["RSI_BRIDGE_URL"] = "http://bridge-test:8081"
    os.environ["DISCORD_GUILD_ID"] = "123456789"
    module_name = f"test_discord_bot_{tmp_path.name}"
    spec = importlib.util.spec_from_file_location(module_name, BOT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_budget_bar_full() -> None:
    """Budget bar renders correctly at various levels."""
    # Use a fresh import since budget_bar is a pure function
    mod = load_bot_module(Path("/tmp"))
    bar = mod.budget_bar(9.0, 10.0, width=10)
    assert "\u2588" * 9 in bar  # 90% filled


def test_budget_bar_empty() -> None:
    mod = load_bot_module(Path("/tmp"))
    bar = mod.budget_bar(0.0, 10.0, width=10)
    assert "\u2591" * 10 in bar  # all empty


def test_build_status_embed_running() -> None:
    mod = load_bot_module(Path("/tmp"))
    wallet = {
        "remaining_usd": 3.50, "budget_usd": 5.00, "spent_usd": 1.50,
        "total_requests": 42, "avg_cost_per_request": 0.035,
        "models_available": ["minimax-m2.7"],
    }
    git_log = [{"hash": "abc1234", "message": "seed", "date": "2026-01-01"}]
    agent_status = {
        "paused": False,
        "knowledge": {"findings": ["Found free tier"]},
        "agent_status": {"turn": 50, "messages": 25, "tokens": 60000},
    }

    with patch.object(mod, "is_sandbox_running", return_value=True):
        embed = mod.build_status_embed(wallet, git_log, agent_status)

    assert embed.title == "RSI-Econ Agent Status"
    assert embed.color.value == mod.COLOR_GREEN
    # Check fields exist
    field_names = [f.name for f in embed.fields]
    assert "Status" in field_names
    assert "Budget" in field_names
    assert "Git" in field_names
    assert "Model" in field_names


def test_build_status_embed_stopped() -> None:
    mod = load_bot_module(Path("/tmp"))
    wallet = {"remaining_usd": 0, "budget_usd": 5.00, "models_available": []}
    with patch.object(mod, "is_sandbox_running", return_value=False):
        embed = mod.build_status_embed(wallet, [], {"paused": False})
    assert embed.color.value == mod.COLOR_GRAY


def test_build_proposal_embed_has_footer() -> None:
    mod = load_bot_module(Path("/tmp"))
    data = {
        "method": "POST",
        "domain": "api.groq.com",
        "url": "https://api.groq.com/v1/keys",
        "proposal_id": "test-uuid-1234",
    }
    embed = mod.build_proposal_embed(data)
    assert embed.title == "\U0001f527 Proposal: POST api.groq.com"
    assert embed.color.value == mod.COLOR_YELLOW
    assert "\u2705" in embed.footer.text
    assert "\u274c" in embed.footer.text


def test_build_summary_embed() -> None:
    mod = load_bot_module(Path("/tmp"))
    wallet = {"remaining_usd": 2.0, "budget_usd": 5.0, "total_requests": 10, "models_available": ["minimax-m2.7"]}
    agent_status = {"agent_status": {"messages": 20, "tokens": 50000, "context_window": 1000000}}
    embed = mod.build_summary_embed("Agent is browsing groq.com for free tier info.", wallet, agent_status)
    assert "Activity Summary" in embed.title
    assert embed.color.value == mod.COLOR_BLUE
    assert embed.description == "Agent is browsing groq.com for free tier info."
    # Status bar fields should be present
    field_names = [f.name for f in embed.fields]
    assert "Model" in field_names
    assert "Budget" in field_names
    assert "Requests" in field_names
    assert "Context" in field_names


def test_build_summary_embed_with_compaction() -> None:
    mod = load_bot_module(Path("/tmp"))
    wallet = {"remaining_usd": 1.0, "budget_usd": 5.0, "total_requests": 50, "models_available": ["m2.7"]}
    agent_status = {"agent_status": {"messages": 30, "tokens": 80000, "context_window": 1000000}}
    compaction = {"stage1": 2, "stage2": 1, "total": 3}
    embed = mod.build_summary_embed("Summary text.", wallet, agent_status, compaction=compaction)
    field_names = [f.name for f in embed.fields]
    assert "Compactions" in field_names
    compaction_field = next(f for f in embed.fields if f.name == "Compactions")
    assert "s1:2" in compaction_field.value
    assert "s2:1" in compaction_field.value


def test_parse_compaction_counts() -> None:
    mod = load_bot_module(Path("/tmp"))
    logs = (
        "sandbox-1  | [agent] stage 1 mask: 3 tool outputs masked\n"
        "sandbox-1  | [agent] stage 1 mask: 2 tool outputs masked\n"
        "sandbox-1  | [agent] context compacted: 50 messages → 8\n"
    )
    result = mod.parse_compaction_counts(logs)
    assert result["stage1"] == 2
    assert result["stage2"] == 1
    assert result["total"] == 3


def test_build_evolution_embed() -> None:
    mod = load_bot_module(Path("/tmp"))
    data = {
        "commit_hash": "abc1234567890",
        "files_changed": "main.py, browser_tool.py",
        "diff_summary": "+5 -3 lines",
    }
    embed = mod.build_evolution_embed(data, "Agent edited its own code")
    assert "\U0001f9ec" in embed.title
    assert embed.color.value == mod.COLOR_GREEN
    field_names = [f.name for f in embed.fields]
    assert "Commit" in field_names
    assert "Files" in field_names


def test_bot_state_persistence(tmp_path: Path) -> None:
    mod = load_bot_module(tmp_path)
    # Override STATE_FILE for testing
    state_file = tmp_path / "discord_state.json"
    mod.STATE_FILE = state_file

    state = mod.BotState()
    state.channels = {"alerts": 111, "activity": 222}
    state.active_session = "test-session"
    state.approval_messages = {"proposal-1": 333}
    state.save()

    # Reload
    state2 = mod.BotState()
    assert state2.channels["alerts"] == 111
    assert state2.active_session == "test-session"
    assert state2.approval_messages["proposal-1"] == 333


def test_bridge_get_helper(tmp_path: Path) -> None:
    mod = load_bot_module(tmp_path)

    class FakeResp:
        def read(self):
            return b'{"remaining_usd": 3.5}'
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    with patch.object(mod.urllib_request, "urlopen", return_value=FakeResp()):
        result = mod.bridge_get("/wallet")

    assert result["remaining_usd"] == 3.5


def test_bridge_get_returns_none_on_failure(tmp_path: Path) -> None:
    mod = load_bot_module(tmp_path)

    with patch.object(mod.urllib_request, "urlopen", side_effect=Exception("timeout")):
        result = mod.bridge_get("/wallet")

    assert result is None


# ── Fix 1: Diff parsing ─────────────────────────────────────────────


def test_parse_diff_extracts_files_and_counts() -> None:
    mod = load_bot_module(Path("/tmp"))
    diff = (
        "diff --git a/main.py b/main.py\n"
        "--- a/main.py\n"
        "+++ b/main.py\n"
        "@@ -10,3 +10,5 @@\n"
        " unchanged line\n"
        "-old line\n"
        "-another old\n"
        "+new line\n"
        "+another new\n"
        "+third new\n"
        "diff --git a/browser_tool.py b/browser_tool.py\n"
        "--- a/browser_tool.py\n"
        "+++ b/browser_tool.py\n"
        "+added line\n"
    )
    result = mod._parse_diff(diff)
    assert result["files"] == ["main.py", "browser_tool.py"]
    assert result["added"] == 4  # 3 from main.py + 1 from browser_tool.py
    assert result["removed"] == 2


def test_evolution_embed_with_diff_and_summary() -> None:
    mod = load_bot_module(Path("/tmp"))
    data = {"commit_hash": "abc123"}
    diff = "diff --git a/main.py b/main.py\n+new code\n-old code\n"
    embed = mod.build_evolution_embed(
        data, "edit",
        llm_summary="Added error handling to the chat function.",
        diff_text=diff,
    )
    assert "main.py" in embed.title
    assert embed.description == "Added error handling to the chat function."
    field_names = [f.name for f in embed.fields]
    assert "Lines" in field_names
    assert "Raw Diff" in field_names
    # Check the lines field
    lines_field = next(f for f in embed.fields if f.name == "Lines")
    assert "+1" in lines_field.value
    assert "-1" in lines_field.value


def test_evolution_embed_truncates_long_diff() -> None:
    mod = load_bot_module(Path("/tmp"))
    long_diff = "diff --git a/main.py b/main.py\n" + "+x\n" * 1000
    embed = mod.build_evolution_embed({}, "edit", diff_text=long_diff)
    raw_field = next(f for f in embed.fields if f.name == "Raw Diff")
    assert "truncated" in raw_field.value
    assert len(raw_field.value) < 600  # 500 chars + code block markers
