"""Tests for context compaction logic."""

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
    module_name = f"test_compaction_{tmp_path.name}"
    spec = importlib.util.spec_from_file_location(module_name, SEED_AGENT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_estimate_tokens(tmp_path: Path) -> None:
    mod = load_seed_agent(tmp_path)
    messages = [{"role": "user", "content": "x" * 400}]
    tokens = mod.estimate_tokens(messages)
    # 400 chars content + json overhead ≈ ~110 tokens
    assert tokens > 50


def test_compact_context_reduces_to_two_messages(tmp_path: Path, monkeypatch) -> None:
    mod = load_seed_agent(tmp_path)

    # Mock chat() so compaction doesn't need a real LLM
    monkeypatch.setattr(mod, "chat", lambda messages, **kw: {
        "choices": [{"message": {"role": "assistant", "content": "Summary: found groq free tier"}}]
    })

    messages = [
        {"role": "system", "content": "You are an agent."},
        {"role": "user", "content": "Do something"},
        {"role": "assistant", "content": "OK"},
        {"role": "tool", "content": "Found https://api.groq.com free tier"},
        {"role": "assistant", "content": "Great"},
    ] * 10  # 50 messages
    messages[0] = {"role": "system", "content": "You are an agent."}
    result = mod.compact_context(messages)
    assert len(result) == 2
    assert result[0]["role"] == "system"
    assert "CONTEXT COMPACTED" in result[1]["content"]


def test_compact_context_writes_summary_file(tmp_path: Path, monkeypatch) -> None:
    mod = load_seed_agent(tmp_path)

    monkeypatch.setattr(mod, "chat", lambda messages, **kw: {
        "choices": [{"message": {"role": "assistant", "content": "My research summary"}}]
    })

    messages = [
        {"role": "system", "content": "You are an agent."},
        {"role": "user", "content": "research"},
    ]
    mod.compact_context(messages)

    summary_path = tmp_path / "last_compaction_summary.md"
    assert summary_path.exists()
    assert "My research summary" in summary_path.read_text()


def test_compact_context_fallback_on_error(tmp_path: Path, monkeypatch) -> None:
    mod = load_seed_agent(tmp_path)

    def failing_chat(messages, **kw):
        raise ConnectionError("LLM unavailable")

    monkeypatch.setattr(mod, "chat", failing_chat)

    messages = [
        {"role": "system", "content": "You are an agent."},
        {"role": "user", "content": "research"},
    ]
    result = mod.compact_context(messages)
    assert len(result) == 2
    assert "summary generation failed" in result[1]["content"]


def test_compaction_threshold_is_500k(tmp_path: Path) -> None:
    mod = load_seed_agent(tmp_path)
    assert mod.COMPACTION_TOKEN_THRESHOLD == 500000
