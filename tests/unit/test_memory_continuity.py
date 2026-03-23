"""Tests for history/memory continuity across restarts."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SEED_AGENT_PATH = ROOT / "sandbox" / "seed" / "main.py"

FAKE_WALLET = {"remaining_usd": 10, "spent_usd": 0, "budget_usd": 10, "total_requests": 0, "avg_cost_per_request": 0}


def load_seed_agent(tmp_path: Path):
    os.environ["RSI_AGENT_WORKSPACE"] = str(tmp_path)
    os.environ["LITELLM_URL"] = "http://litellm:4000"
    os.environ["WALLET_URL"] = "http://bridge:8081"
    os.environ["RSI_MODEL"] = "default"
    os.environ["RSI_MAX_TURNS"] = "3"
    spec = importlib.util.spec_from_file_location(
        f"test_memory_{tmp_path.name}", SEED_AGENT_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def write_agent_workspace(tmp_path: Path) -> None:
    (tmp_path / "SYSTEM.md").write_text("system prompt\n", encoding="utf-8")


def test_history_written_on_tool_call(tmp_path: Path, monkeypatch) -> None:
    write_agent_workspace(tmp_path)
    module = load_seed_agent(tmp_path)

    monkeypatch.setattr(module, "chat", lambda messages, model=None, tools=None: {
        "choices": [{"message": {"role": "assistant", "tool_calls": [
            {"id": "t1", "type": "function", "function": {"name": "shell", "arguments": '{"command":"echo hi"}'}}
        ]}}]
    })
    monkeypatch.setattr(module, "get_wallet", lambda: FAKE_WALLET)
    module.main()

    history_path = tmp_path / "history.jsonl"
    assert history_path.exists()
    lines = [json.loads(line) for line in history_path.read_text().strip().split("\n") if line.strip()]
    tool_entries = [e for e in lines if e.get("role") == "tool"]
    assert len(tool_entries) >= 1
    assert tool_entries[0]["name"] == "shell"


def test_history_written_on_assistant(tmp_path: Path, monkeypatch) -> None:
    write_agent_workspace(tmp_path)
    module = load_seed_agent(tmp_path)

    monkeypatch.setattr(module, "chat", lambda messages, model=None, tools=None: {
        "choices": [{"message": {"role": "assistant", "content": "Thinking about next steps."}}]
    })
    monkeypatch.setattr(module, "get_wallet", lambda: FAKE_WALLET)
    module.main()

    history_path = tmp_path / "history.jsonl"
    assert history_path.exists()
    lines = [json.loads(line) for line in history_path.read_text().strip().split("\n") if line.strip()]
    assistant_entries = [e for e in lines if e.get("role") == "assistant"]
    assert len(assistant_entries) >= 1


def test_previous_session_loaded(tmp_path: Path, monkeypatch) -> None:
    write_agent_workspace(tmp_path)

    # Write fake previous reasoning log (the v2 memory format)
    reasoning_path = tmp_path / "reasoning.jsonl"
    entries = [
        {"turn": 1, "timestamp": "2026-03-23T12:00:00Z", "content": "I found free providers on OpenRouter", "model": "minimax-m2.7", "tool_calls": []},
        {"turn": 2, "timestamp": "2026-03-23T12:01:00Z", "content": "Found groq.com free tier info", "model": "minimax-m2.7", "tool_calls": []},
    ]
    reasoning_path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")

    module = load_seed_agent(tmp_path)
    first_call_messages: list = []

    def fake_chat(messages, model=None, tools=None):
        if not first_call_messages:
            first_call_messages.extend(messages)
        return {"choices": [{"message": {"role": "assistant", "content": "Continuing work."}}]}

    monkeypatch.setattr(module, "chat", fake_chat)
    monkeypatch.setattr(module, "get_wallet", lambda: FAKE_WALLET)
    module.main()

    # The first LLM call should include RECENT REASONING from previous session
    reasoning_msgs = [m for m in first_call_messages if "RECENT REASONING" in m.get("content", "")]
    assert len(reasoning_msgs) == 1
    assert "free providers" in reasoning_msgs[0]["content"] or "groq" in reasoning_msgs[0]["content"]


def test_empty_history_skipped(tmp_path: Path, monkeypatch) -> None:
    write_agent_workspace(tmp_path)
    module = load_seed_agent(tmp_path)
    captured_messages: list = []

    def fake_chat(messages, model=None, tools=None):
        captured_messages.extend(messages)
        return {"choices": [{"message": {"role": "assistant", "content": "Working."}}]}

    monkeypatch.setattr(module, "chat", fake_chat)
    monkeypatch.setattr(module, "get_wallet", lambda: FAKE_WALLET)
    module.main()

    # No RECENT REASONING message when no reasoning.jsonl exists
    reasoning_msgs = [m for m in captured_messages if "RECENT REASONING" in m.get("content", "")]
    assert len(reasoning_msgs) == 0
