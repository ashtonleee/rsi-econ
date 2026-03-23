"""Tests for the terminal TUI — rendering and parsing helpers."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
TUI_PATH = ROOT / "cli" / "watch.py"


def load_tui_module(tmp_path: Path):
    os.environ["RSI_BRIDGE_URL"] = "http://bridge-test:8081"
    module_name = f"test_tui_{tmp_path.name}"
    spec = importlib.util.spec_from_file_location(module_name, TUI_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_budget_bar_full() -> None:
    mod = load_tui_module(Path("/tmp"))
    bar = mod.budget_bar(92, width=18)
    filled_count = bar.count("\u2588")
    empty_count = bar.count("\u2591")
    assert filled_count == 16  # int(92/100 * 18)
    assert empty_count == 2
    assert "FULL" in bar


def test_budget_bar_moderate() -> None:
    mod = load_tui_module(Path("/tmp"))
    bar = mod.budget_bar(35, width=20)
    assert "MODERATE" in bar


def test_budget_bar_conserve() -> None:
    mod = load_tui_module(Path("/tmp"))
    bar = mod.budget_bar(10, width=20)
    assert "CONSERVE" in bar


def test_budget_bar_wrapup() -> None:
    mod = load_tui_module(Path("/tmp"))
    bar = mod.budget_bar(3, width=20)
    assert "WRAPUP" in bar


def test_parse_agent_log_tool_lines() -> None:
    """parse_agent_logs extracts tool names from log lines."""
    mod = load_tui_module(Path("/tmp"))

    fake_output = (
        "sandbox-1  | [agent:minimax-m2.7] tool: web_search\n"
        "sandbox-1  | [agent:minimax-m2.7] tool: browse_url\n"
        "sandbox-1  | [agent:minimax-m2.7] thinking (turn 42)\n"
        "sandbox-1  | [agent:minimax-m2.7] tool: shell\n"
    )

    class FakeResult:
        returncode = 0
        stdout = fake_output

    with patch.object(mod, "docker_compose", return_value=FakeResult()):
        result = mod.parse_agent_logs(15)

    assert result["tools"] == ["web_search", "browse_url", "shell"]


def test_parse_agent_log_context_line() -> None:
    mod = load_tui_module(Path("/tmp"))

    fake_output = (
        "sandbox-1  | [agent:minimax-m2.7] context: 27/40 msgs, ~65000 tokens\n"
    )

    class FakeResult:
        returncode = 0
        stdout = fake_output

    with patch.object(mod, "docker_compose", return_value=FakeResult()):
        result = mod.parse_agent_logs(5)

    assert result["context"] == "27/40 msgs, ~65k tokens"


def test_get_domain_stats(tmp_path: Path) -> None:
    mod = load_tui_module(tmp_path)
    log_path = tmp_path / "web_egress.jsonl"
    entries = [
        {"host": "groq.com"},
        {"host": "groq.com"},
        {"host": "groq.com"},
        {"host": "openrouter.ai"},
        {"host": "openrouter.ai"},
        {"host": "github.com"},
    ]
    log_path.write_text("\n".join(json.dumps(e) for e in entries))

    mod.PROXY_LOG = log_path
    result = mod.get_domain_stats()

    assert "groq.com(3)" in result
    assert "openrouter.ai(2)" in result
    assert "github.com(1)" in result


def test_collect_data_bridge_unreachable(tmp_path: Path) -> None:
    """collect_data handles bridge failure gracefully."""
    mod = load_tui_module(tmp_path)
    mod.PROXY_LOG = tmp_path / "nonexistent.jsonl"

    class FakeResult:
        returncode = 1
        stdout = ""

    with patch.object(mod, "bridge_get", return_value=None), \
         patch.object(mod, "docker_compose", return_value=FakeResult()):
        data = mod.collect_data()

    assert data["wallet"] == {}
    assert data["status"] == "STOPPED"
    assert data["domains"] == ""
