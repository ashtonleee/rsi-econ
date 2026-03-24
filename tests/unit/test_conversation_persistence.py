"""Tests for conversation persistence across restarts."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SEED_AGENT_PATH = ROOT / "sandbox" / "seed" / "main.py"


def load_seed_agent(tmp_path: Path):
    os.environ["RSI_AGENT_WORKSPACE"] = str(tmp_path)
    os.environ["LITELLM_URL"] = "http://litellm:4000"
    os.environ["WALLET_URL"] = "http://bridge:8081"
    os.environ["RSI_MODEL"] = "default"
    os.environ["RSI_MAX_TURNS"] = "5"
    os.environ["RSI_CONTEXT_WINDOW"] = "1000000"
    module_name = f"test_persist_{tmp_path.name}"
    spec = importlib.util.spec_from_file_location(module_name, SEED_AGENT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _sample_messages() -> list[dict]:
    """A realistic multi-turn conversation with tool calls."""
    return [
        {"role": "system", "content": "You are an AI agent."},
        {"role": "user", "content": "Start working."},
        {"role": "assistant", "content": "I'll check the budget.", "tool_calls": [
            {"id": "tc_1", "function": {"name": "shell", "arguments": '{"command": "echo hello"}'}}
        ]},
        {"role": "tool", "tool_call_id": "tc_1", "content": "[exit_code=0]\nhello"},
        {"role": "assistant", "content": "Budget looks good. Let me edit main.py.", "tool_calls": [
            {"id": "tc_2", "function": {"name": "edit_file", "arguments": '{"path": "/workspace/agent/main.py", "old_text": "foo", "new_text": "bar"}'}}
        ]},
        {"role": "tool", "tool_call_id": "tc_2", "content": "OK: edited /workspace/agent/main.py"},
        {"role": "assistant", "content": "Changes made. Requesting restart.", "tool_calls": [
            {"id": "tc_3", "function": {"name": "request_restart", "arguments": "{}"}}
        ]},
        {"role": "tool", "tool_call_id": "tc_3", "content": "OK: restart requested."},
    ]


# --- save_conversation tests ---


def test_conversation_saved_on_restart(tmp_path: Path) -> None:
    """After save_conversation, conversation.json exists with messages."""
    mod = load_seed_agent(tmp_path)
    messages = _sample_messages()

    mod.save_conversation(messages)

    conv_path = tmp_path / "conversation.json"
    assert conv_path.exists()
    loaded = json.loads(conv_path.read_text())
    assert len(loaded) == len(messages)
    assert loaded[0]["role"] == "system"
    # Tool call IDs should be preserved
    assert loaded[2]["tool_calls"][0]["id"] == "tc_1"
    assert loaded[3]["tool_call_id"] == "tc_1"


def test_save_is_atomic(tmp_path: Path) -> None:
    """Save uses tmp+rename, so no partial writes."""
    mod = load_seed_agent(tmp_path)
    messages = _sample_messages()

    mod.save_conversation(messages)

    # No temp file should remain
    assert not (tmp_path / "conversation.json.tmp").exists()
    # File should be valid JSON
    loaded = json.loads((tmp_path / "conversation.json").read_text())
    assert isinstance(loaded, list)


# --- load_conversation tests ---


def test_conversation_loaded_on_startup(tmp_path: Path) -> None:
    """With existing conversation.json, messages[] is loaded from it."""
    mod = load_seed_agent(tmp_path)
    messages = _sample_messages()

    # Write conversation to disk
    (tmp_path / "conversation.json").write_text(json.dumps(messages))

    loaded = mod.load_conversation()
    assert loaded is not None
    assert len(loaded) == len(messages)
    assert loaded[0]["role"] == "system"
    # Verify tool_call IDs survived serialization
    assert loaded[2]["tool_calls"][0]["id"] == "tc_1"


def test_corrupt_conversation_starts_fresh(tmp_path: Path) -> None:
    """Corrupt conversation.json → agent starts fresh (returns None)."""
    mod = load_seed_agent(tmp_path)

    # Write corrupt JSON
    (tmp_path / "conversation.json").write_text("not valid json {{{")

    loaded = mod.load_conversation()
    assert loaded is None


def test_empty_conversation_starts_fresh(tmp_path: Path) -> None:
    """Empty list in conversation.json → starts fresh."""
    mod = load_seed_agent(tmp_path)
    (tmp_path / "conversation.json").write_text("[]")

    loaded = mod.load_conversation()
    assert loaded is None


def test_missing_system_prompt_starts_fresh(tmp_path: Path) -> None:
    """Conversation without system prompt as first message → starts fresh."""
    mod = load_seed_agent(tmp_path)
    (tmp_path / "conversation.json").write_text(json.dumps([
        {"role": "user", "content": "hello"},
    ]))

    loaded = mod.load_conversation()
    assert loaded is None


def test_no_conversation_file_returns_none(tmp_path: Path) -> None:
    """No conversation.json → returns None (fresh start)."""
    mod = load_seed_agent(tmp_path)
    loaded = mod.load_conversation()
    assert loaded is None


# --- Restart marker tests ---


def test_conversation_marker_on_restart(tmp_path: Path) -> None:
    """Loaded conversation gets [RESTART] marker appended.

    We can't easily test main() directly, but we can verify the
    logic by simulating what main() does on startup.
    """
    mod = load_seed_agent(tmp_path)
    messages = _sample_messages()
    (tmp_path / "conversation.json").write_text(json.dumps(messages))
    # Create SYSTEM.md so build_system_prompt works
    (tmp_path / "SYSTEM.md").write_text("You are an agent.")

    loaded = mod.load_conversation()
    assert loaded is not None

    # Simulate what main() does: replace system prompt, add marker
    loaded[0] = {"role": "system", "content": "fresh system prompt"}
    loaded.append({
        "role": "user",
        "content": "[RESTART] Code changes applied. Your edits are now active. Check git log if needed.",
    })

    assert "[RESTART]" in loaded[-1]["content"]
    # Original conversation is preserved
    assert loaded[2]["tool_calls"][0]["id"] == "tc_1"


def test_crash_revert_marker(tmp_path: Path) -> None:
    """Crash+revert detection adds appropriate marker."""
    mod = load_seed_agent(tmp_path)

    # Create the crash marker file
    (tmp_path / ".crash_reverted").touch()

    assert mod.detect_crash_revert() is True
    # Marker should be consumed (deleted)
    assert not (tmp_path / ".crash_reverted").exists()


def test_crash_revert_false_when_no_marker(tmp_path: Path) -> None:
    """No crash marker → detect_crash_revert returns False."""
    mod = load_seed_agent(tmp_path)
    assert mod.detect_crash_revert() is False


# --- Compaction + persistence tests ---


def test_conversation_cleared_on_compaction(tmp_path: Path, monkeypatch) -> None:
    """After compaction, conversation.json has only compacted messages."""
    mod = load_seed_agent(tmp_path)

    # Write initial conversation
    messages = _sample_messages()
    mod.save_conversation(messages)
    assert len(json.loads((tmp_path / "conversation.json").read_text())) == 8

    # Mock bridge compact
    monkeypatch.setattr(mod, "_call_bridge_compact", lambda text: "Compacted summary")

    # Build a large enough conversation for emergency compaction
    big_messages = [{"role": "system", "content": "system"}]
    for i in range(50):
        big_messages.append({"role": "user", "content": f"msg {i}"})
        big_messages.append({"role": "assistant", "content": f"resp {i}"})

    result = mod.compact_context_emergency(big_messages)
    # Manually save like run_compaction does
    mod.save_conversation(result)

    # conversation.json should now have the compacted version
    loaded = json.loads((tmp_path / "conversation.json").read_text())
    assert len(loaded) == 2  # system + summary
    assert "EMERGENCY CONTEXT RESET" in loaded[1]["content"]


def test_run_compaction_persists(tmp_path: Path, monkeypatch) -> None:
    """run_compaction saves to conversation.json when compaction occurs."""
    mod = load_seed_agent(tmp_path)
    # Use small window to trigger compaction
    os.environ["RSI_CONTEXT_WINDOW"] = "100"
    # Reload module to pick up new window size
    module_name = f"test_persist_compact_{tmp_path.name}"
    spec = importlib.util.spec_from_file_location(module_name, SEED_AGENT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)

    monkeypatch.setattr(module, "_call_bridge_compact", lambda text: "Summary")
    module._last_known_tokens = 95  # 95% of 100 window → emergency

    messages = [{"role": "system", "content": "system"}]
    for i in range(10):
        messages.append({"role": "user", "content": f"msg {i}"})

    result = module.run_compaction(messages, current_turn=1)

    # conversation.json should exist and contain the compacted result
    conv_path = tmp_path / "conversation.json"
    assert conv_path.exists()
    loaded = json.loads(conv_path.read_text())
    assert len(loaded) == len(result)


# --- Model switch compaction tests ---


def test_model_switch_triggers_compaction(tmp_path: Path, monkeypatch) -> None:
    """Large conversation + smaller model window → compaction runs on load.

    We test the logic that main() uses: check utilization after loading,
    run compaction if needed.
    """
    mod = load_seed_agent(tmp_path)

    # Build a conversation that would be large
    messages = [{"role": "system", "content": "system"}]
    for i in range(20):
        messages.append({"role": "user", "content": f"msg {i}"})
        messages.append({"role": "assistant", "content": f"resp {i}"})

    # Simulate: conversation was built with a 1M window model.
    # Now switching to a 200 token window (very small for testing).
    mod.COMPACTION_CONFIG["context_window"] = 200
    # Set token count to simulate being over 75% of 200 = 150
    mod._last_known_tokens = 180

    monkeypatch.setattr(mod, "_call_bridge_compact", lambda text: "Model switch summary")

    result = mod.run_compaction(messages, current_turn=0)

    # Should have been compacted
    assert len(result) < len(messages)
    has_compacted = any("CONTEXT COMPACTED" in m.get("content", "") or
                        "EMERGENCY" in m.get("content", "")
                        for m in result)
    assert has_compacted


# --- Tool call ID preservation tests ---


def test_tool_call_ids_roundtrip(tmp_path: Path) -> None:
    """Tool call IDs survive save → load roundtrip."""
    mod = load_seed_agent(tmp_path)
    messages = _sample_messages()

    mod.save_conversation(messages)
    loaded = mod.load_conversation()

    assert loaded is not None
    # Check all tool_call_id references
    for i, msg in enumerate(loaded):
        if msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                tc_id = tc["id"]
                # Find the corresponding tool result
                found = False
                for j in range(i + 1, len(loaded)):
                    if loaded[j].get("tool_call_id") == tc_id:
                        found = True
                        break
                assert found, f"tool_call_id {tc_id} has no matching tool result"


def test_stale_tool_references_dont_crash(tmp_path: Path) -> None:
    """Conversation with tool calls for removed tools loads fine.

    Tool call messages referencing tools that no longer exist are just
    historical context — they shouldn't prevent loading.
    """
    mod = load_seed_agent(tmp_path)
    messages = [
        {"role": "system", "content": "system"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "tc_old", "function": {"name": "removed_tool", "arguments": '{"foo": "bar"}'}}
        ]},
        {"role": "tool", "tool_call_id": "tc_old", "content": "result from removed tool"},
        {"role": "user", "content": "continue"},
    ]

    mod.save_conversation(messages)
    loaded = mod.load_conversation()
    assert loaded is not None
    assert len(loaded) == 4
    assert loaded[1]["tool_calls"][0]["function"]["name"] == "removed_tool"
