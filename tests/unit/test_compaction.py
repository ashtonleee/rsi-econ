"""Tests for 2-stage context compaction logic."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SEED_AGENT_PATH = ROOT / "sandbox" / "seed" / "main.py"


def load_seed_agent(tmp_path: Path):
    os.environ["RSI_AGENT_WORKSPACE"] = str(tmp_path)
    os.environ["LITELLM_URL"] = "http://litellm:4000"
    os.environ["WALLET_URL"] = "http://bridge:8081"
    os.environ["RSI_MODEL"] = "default"
    os.environ["RSI_MAX_TURNS"] = "5"
    # Use small context window for testing
    os.environ["RSI_CONTEXT_WINDOW"] = "1000"
    module_name = f"test_compaction_{tmp_path.name}"
    spec = importlib.util.spec_from_file_location(module_name, SEED_AGENT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# --- Compaction config tests ---


def test_compaction_config_is_model_aware(tmp_path: Path) -> None:
    """Config uses context_window with percentage-based triggers."""
    mod = load_seed_agent(tmp_path)
    cfg = mod.COMPACTION_CONFIG
    assert "context_window" in cfg
    assert "stage1_trigger" in cfg
    assert "stage2_trigger" in cfg
    assert "emergency_trigger" in cfg
    # Thresholds should be percentages < 1.0
    assert 0 < cfg["stage1_trigger"] < cfg["stage2_trigger"] < cfg["emergency_trigger"] <= 1.0


def test_compaction_config_env_override(tmp_path: Path) -> None:
    """Config values can be overridden via env vars (must be set before import)."""
    # Set env vars before loading the module (config is evaluated at import time)
    os.environ["RSI_CONTEXT_WINDOW"] = "200000"
    os.environ["RSI_COMPACT_STAGE1"] = "0.50"
    try:
        # Force fresh module load by using a unique name
        module_name = f"test_compaction_env_{tmp_path.name}"
        spec = importlib.util.spec_from_file_location(module_name, SEED_AGENT_PATH)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        assert module.COMPACTION_CONFIG["context_window"] == 200000
        assert module.COMPACTION_CONFIG["stage1_trigger"] == 0.50
    finally:
        os.environ["RSI_CONTEXT_WINDOW"] = "1000"
        os.environ.pop("RSI_COMPACT_STAGE1", None)


# --- Token estimation tests ---


def test_estimate_tokens_uses_api_data(tmp_path: Path) -> None:
    """After API response, estimate_tokens returns real count."""
    mod = load_seed_agent(tmp_path)
    # Simulate API response with usage data
    mod._update_token_count({"usage": {"prompt_tokens": 5000, "completion_tokens": 200}})
    assert mod.estimate_tokens([]) == 5200


def test_estimate_tokens_fallback(tmp_path: Path) -> None:
    """Without API data, falls back to char-based estimate."""
    mod = load_seed_agent(tmp_path)
    mod._last_known_tokens = 0
    mod._token_source = "estimate"
    messages = [{"role": "user", "content": "x" * 400}]
    tokens = mod.estimate_tokens(messages)
    assert tokens > 50


def test_update_token_count_from_total(tmp_path: Path) -> None:
    mod = load_seed_agent(tmp_path)
    mod._update_token_count({"usage": {"total_tokens": 9999}})
    assert mod._last_known_tokens == 9999
    assert mod._token_source == "api"


def test_update_token_count_missing_usage(tmp_path: Path) -> None:
    mod = load_seed_agent(tmp_path)
    mod._last_known_tokens = 0
    mod._token_source = "estimate"
    mod._update_token_count({"usage": {}})
    assert mod._last_known_tokens == 0  # unchanged


# --- Stage 1: Tool output masking tests ---


def test_mask_tool_outputs_preserves_recent(tmp_path: Path) -> None:
    """Tool outputs within mask_after_turns are preserved."""
    mod = load_seed_agent(tmp_path)
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "do something"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "tc1", "function": {"name": "shell", "arguments": '{"command": "ls"}'}}
        ]},
        {"role": "tool", "tool_call_id": "tc1", "content": "x" * 500},  # substantial
    ]
    # current_turn=1, message is from turn 1, so age=0 < mask_after(5)
    masked, count = mod.mask_tool_outputs(messages, current_turn=1)
    assert count == 0
    assert masked[3]["content"] == "x" * 500


def test_mask_tool_outputs_replaces_old(tmp_path: Path) -> None:
    """Tool outputs older than mask_after_turns get descriptive placeholders."""
    mod = load_seed_agent(tmp_path)
    messages = [
        {"role": "system", "content": "system"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "tc1", "function": {"name": "read_file", "arguments": '{"path": "/workspace/agent/main.py"}'}}
        ]},
        {"role": "tool", "tool_call_id": "tc1", "content": "line1\nline2\nline3\n" * 100},
    ]
    # current_turn=20, assistant is at turn 1, so age=19 > mask_after(5)
    masked, count = mod.mask_tool_outputs(messages, current_turn=20)
    assert count == 1
    assert "read_file" in masked[2]["content"]
    assert "/workspace/agent/main.py" in masked[2]["content"]
    assert "lines" in masked[2]["content"]
    # Original content should be gone
    assert len(masked[2]["content"]) < 200


def test_mask_skips_short_content(tmp_path: Path) -> None:
    """Short tool outputs (< 200 chars) are never masked."""
    mod = load_seed_agent(tmp_path)
    messages = [
        {"role": "system", "content": "system"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "tc1", "function": {"name": "shell", "arguments": '{"command": "echo hi"}'}}
        ]},
        {"role": "tool", "tool_call_id": "tc1", "content": "hi"},
    ]
    masked, count = mod.mask_tool_outputs(messages, current_turn=100)
    assert count == 0
    assert masked[2]["content"] == "hi"


# --- Descriptive placeholder tests ---


def test_placeholder_shell(tmp_path: Path) -> None:
    mod = load_seed_agent(tmp_path)
    placeholder = mod._build_descriptive_placeholder(
        "shell", {"command": "git log --oneline -10"}, "[exit_code=0, duration=0.1s, 847 chars]\ncommit abc..."
    )
    assert "shell" in placeholder
    assert "git log" in placeholder


def test_placeholder_web_search(tmp_path: Path) -> None:
    mod = load_seed_agent(tmp_path)
    content = '{"results": [{"title": "R1"}, {"title": "R2"}, {"title": "R3"}]}'
    placeholder = mod._build_descriptive_placeholder(
        "web_search", {"query": "free LLM providers"}, content
    )
    assert "web_search" in placeholder
    assert "free LLM providers" in placeholder
    assert "3 results" in placeholder


def test_placeholder_read_file(tmp_path: Path) -> None:
    mod = load_seed_agent(tmp_path)
    content = "line1\nline2\nline3\nline4\nline5\n"
    placeholder = mod._build_descriptive_placeholder(
        "read_file", {"path": "/workspace/main.py"}, content
    )
    assert "/workspace/main.py" in placeholder
    assert "5 lines" in placeholder


# --- Stage 2: Bookend + summarize tests ---


def test_bookend_compaction_structure(tmp_path: Path, monkeypatch) -> None:
    """Stage 2 preserves first N and last M messages with summary in between."""
    mod = load_seed_agent(tmp_path)

    # Mock bridge /compact endpoint
    def mock_compact(text):
        return "Summary of middle conversation"
    monkeypatch.setattr(mod, "_call_bridge_compact", mock_compact)

    # Build a conversation with enough messages
    messages = [{"role": "system", "content": "system prompt"}]  # kept as first
    messages.append({"role": "user", "content": "first user msg"})
    messages.append({"role": "assistant", "content": "first response"})
    # Middle (will be summarized)
    for i in range(10):
        messages.append({"role": "user", "content": f"middle msg {i}"})
        messages.append({"role": "assistant", "content": f"middle response {i}"})
    # Tail (last 4 will be kept)
    messages.append({"role": "user", "content": "recent 1"})
    messages.append({"role": "assistant", "content": "recent response 1"})
    messages.append({"role": "user", "content": "recent 2"})
    messages.append({"role": "assistant", "content": "recent response 2"})

    result = mod.compact_context_bookend(messages)

    # First 3 messages preserved
    assert result[0]["content"] == "system prompt"
    assert result[1]["content"] == "first user msg"
    assert result[2]["content"] == "first response"

    # Summary message in the middle
    assert "CONTEXT COMPACTED" in result[3]["content"]
    assert "Summary of middle conversation" in result[3]["content"]

    # Last 4 messages preserved
    assert result[-1]["content"] == "recent response 2"
    assert result[-2]["content"] == "recent 2"
    assert result[-3]["content"] == "recent response 1"
    assert result[-4]["content"] == "recent 1"


def test_bookend_writes_summary_file(tmp_path: Path, monkeypatch) -> None:
    mod = load_seed_agent(tmp_path)
    monkeypatch.setattr(mod, "_call_bridge_compact", lambda text: "Persistent summary")

    messages = [{"role": "system", "content": "s"}]
    for i in range(20):
        messages.append({"role": "user", "content": f"msg {i}"})
        messages.append({"role": "assistant", "content": f"resp {i}"})

    mod.compact_context_bookend(messages)
    summary_path = tmp_path / "last_compaction_summary.md"
    assert summary_path.exists()
    assert "Persistent summary" in summary_path.read_text()


# --- Stage 3: Emergency compaction tests ---


def test_emergency_compaction_resets_to_two(tmp_path: Path, monkeypatch) -> None:
    mod = load_seed_agent(tmp_path)
    monkeypatch.setattr(mod, "_call_bridge_compact", lambda text: "Emergency summary")

    messages = [
        {"role": "system", "content": "system prompt"},
        *[{"role": "user", "content": f"msg {i}"} for i in range(50)],
    ]
    result = mod.compact_context_emergency(messages)
    assert len(result) == 2
    assert result[0]["role"] == "system"
    assert "EMERGENCY CONTEXT RESET" in result[1]["content"]
    assert "Emergency summary" in result[1]["content"]


# --- Bridge compact fallback tests ---


def test_compact_fallback_local(tmp_path: Path, monkeypatch) -> None:
    """When bridge /compact fails, falls back to local LLM."""
    mod = load_seed_agent(tmp_path)

    monkeypatch.setattr(mod, "chat", lambda messages, **kw: {
        "choices": [{"message": {"role": "assistant", "content": "Local fallback summary"}}]
    })

    result = mod._compact_fallback_local("some conversation text")
    assert "Local fallback summary" in result


# --- run_compaction integration tests ---


def test_run_compaction_no_action_below_threshold(tmp_path: Path) -> None:
    """No compaction when utilization is below stage1 trigger."""
    mod = load_seed_agent(tmp_path)
    mod._last_known_tokens = 100  # 10% of 1000 window
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hello"},
    ]
    result = mod.run_compaction(messages, current_turn=1)
    assert result == messages  # unchanged


def test_run_compaction_stage1_only(tmp_path: Path) -> None:
    """Stage 1 masks tool outputs at 60% utilization."""
    mod = load_seed_agent(tmp_path)
    # Set token count to 60% of 1000 = 600
    mod._last_known_tokens = 650

    messages = [
        {"role": "system", "content": "system"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "tc1", "function": {"name": "read_file", "arguments": '{"path": "/test.py"}'}}
        ]},
        {"role": "tool", "tool_call_id": "tc1", "content": "x" * 500},
    ]
    result = mod.run_compaction(messages, current_turn=20)
    # Tool output should be masked (age=20 > 5)
    assert "read_file" in result[2]["content"]
    assert len(result[2]["content"]) < 200


def test_run_compaction_triggers_stage2(tmp_path: Path, monkeypatch) -> None:
    """Stage 2 fires when utilization exceeds 75% even after masking."""
    mod = load_seed_agent(tmp_path)
    # 80% of 1000 = 800
    mod._last_known_tokens = 800

    monkeypatch.setattr(mod, "_call_bridge_compact", lambda text: "Compacted summary")

    messages = [{"role": "system", "content": "system"}]
    messages.append({"role": "user", "content": "first"})
    messages.append({"role": "assistant", "content": "first resp"})
    for i in range(10):
        messages.append({"role": "user", "content": f"msg {i}"})
        messages.append({"role": "assistant", "content": f"resp {i}"})
    messages.append({"role": "user", "content": "recent"})
    messages.append({"role": "assistant", "content": "recent resp"})
    messages.append({"role": "user", "content": "latest"})
    messages.append({"role": "assistant", "content": "latest resp"})

    result = mod.run_compaction(messages, current_turn=50)
    # Should have been compacted — look for the summary message
    has_compaction_msg = any("CONTEXT COMPACTED" in m.get("content", "") for m in result)
    assert has_compaction_msg


# --- build_system_prompt tests ---


def test_build_system_prompt_includes_all_fields(tmp_path: Path) -> None:
    """SYSTEM.md promises budget, spend rate, model, and time."""
    mod = load_seed_agent(tmp_path)
    # Create minimal SYSTEM.md
    (tmp_path / "SYSTEM.md").write_text("You are an agent.")
    wallet = {
        "remaining_usd": 3.50,
        "budget_usd": 5.00,
        "spent_usd": 1.50,
        "total_requests": 30,
        "avg_cost_per_request": 0.05,
    }
    prompt = mod.build_system_prompt(wallet)
    assert "$3.50" in prompt  # budget remaining
    assert "$5.00" in prompt  # budget total
    assert "$1.50" in prompt  # spent
    assert "30" in prompt     # request count
    assert "0.0500" in prompt  # avg cost
    assert "context window" in prompt.lower() or "1,000" in prompt  # context window
