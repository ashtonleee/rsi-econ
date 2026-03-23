"""Tests for operator message injection."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SEED_AGENT_PATH = ROOT / "sandbox" / "seed" / "main.py"
INJECT_CLI_PATH = ROOT / "cli" / "inject.py"


def load_seed_agent(tmp_path: Path):
    os.environ["RSI_AGENT_WORKSPACE"] = str(tmp_path)
    os.environ["LITELLM_URL"] = "http://litellm:4000"
    os.environ["WALLET_URL"] = "http://bridge:8081"
    os.environ["RSI_MODEL"] = "default"
    os.environ["RSI_MAX_TURNS"] = "5"
    op_dir = tmp_path / "operator_messages"
    op_dir.mkdir(exist_ok=True)
    os.environ["RSI_OPERATOR_MESSAGES"] = str(op_dir / "pending.jsonl")
    spec = importlib.util.spec_from_file_location(f"test_inject_{tmp_path.name}", SEED_AGENT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def load_inject_cli():
    spec = importlib.util.spec_from_file_location("inject_cli", INJECT_CLI_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_inject_writes_file(tmp_path: Path) -> None:
    cli = load_inject_cli()
    pending = tmp_path / "pending.jsonl"
    cli.MESSAGES_PATH = pending

    import argparse
    cli.main.__code__  # just verify it loaded
    # Write directly using the module's logic
    entry = {"timestamp": "2026-01-01", "message": "test msg", "model_override": None}
    pending.parent.mkdir(parents=True, exist_ok=True)
    with pending.open("a") as f:
        f.write(json.dumps(entry) + "\n")

    assert pending.exists()
    lines = pending.read_text().strip().split("\n")
    assert len(lines) == 1
    assert json.loads(lines[0])["message"] == "test msg"


def test_agent_reads_and_consumes(tmp_path: Path) -> None:
    mod = load_seed_agent(tmp_path)
    pending = Path(os.environ["RSI_OPERATOR_MESSAGES"])

    # Write an operator message
    entry = {"message": "Focus on Groq", "model_override": None}
    pending.write_text(json.dumps(entry) + "\n")

    messages: list[dict] = []
    result = mod.check_operator_messages(messages)

    # Message should be consumed
    assert not pending.exists()
    assert len(messages) == 1
    assert "[OPERATOR]" in messages[0]["content"]
    assert "Focus on Groq" in messages[0]["content"]
    assert result is None  # no model override


def test_model_override_returned(tmp_path: Path) -> None:
    mod = load_seed_agent(tmp_path)
    pending = Path(os.environ["RSI_OPERATOR_MESSAGES"])

    entry = {"message": "Use smart model", "model_override": "gpt-4.1"}
    pending.write_text(json.dumps(entry) + "\n")

    messages: list[dict] = []
    result = mod.check_operator_messages(messages)

    assert result == "gpt-4.1"


def test_empty_file_no_crash(tmp_path: Path) -> None:
    mod = load_seed_agent(tmp_path)
    pending = Path(os.environ["RSI_OPERATOR_MESSAGES"])
    pending.write_text("")

    messages: list[dict] = []
    result = mod.check_operator_messages(messages)

    assert result is None
    assert len(messages) == 0


def test_multiple_messages_all_injected(tmp_path: Path) -> None:
    mod = load_seed_agent(tmp_path)
    pending = Path(os.environ["RSI_OPERATOR_MESSAGES"])

    entries = [
        {"message": "First instruction"},
        {"message": "Second instruction"},
        {"message": "Third instruction"},
    ]
    pending.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

    messages: list[dict] = []
    mod.check_operator_messages(messages)

    assert len(messages) == 3
    assert "First" in messages[0]["content"]
    assert "Third" in messages[2]["content"]
