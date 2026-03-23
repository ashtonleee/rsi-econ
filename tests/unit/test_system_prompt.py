"""Tests for system prompt construction."""

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
    module_name = f"test_system_prompt_{tmp_path.name}"
    spec = importlib.util.spec_from_file_location(module_name, SEED_AGENT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_no_hardcoded_tasks(tmp_path: Path) -> None:
    (tmp_path / "SYSTEM.md").write_text("You are an agent.\n", encoding="utf-8")
    module = load_seed_agent(tmp_path)
    wallet = {"remaining_usd": 5, "budget_usd": 5, "spent_usd": 0, "total_requests": 0, "avg_cost_per_request": 0}
    prompt = module.build_system_prompt(wallet)
    assert "research_ai_providers" not in prompt
    assert "check_proxy_domains" not in prompt
    assert "setup_caching" not in prompt


def test_no_priority_actions_section(tmp_path: Path) -> None:
    (tmp_path / "SYSTEM.md").write_text("You are an agent.\n", encoding="utf-8")
    module = load_seed_agent(tmp_path)
    wallet = {"remaining_usd": 5, "budget_usd": 5, "spent_usd": 0, "total_requests": 0, "avg_cost_per_request": 0}
    prompt = module.build_system_prompt(wallet)
    assert "## Priority Actions" not in prompt
    assert "## Pending Tasks" not in prompt


def test_budget_injected(tmp_path: Path) -> None:
    (tmp_path / "SYSTEM.md").write_text("You are an agent.\n", encoding="utf-8")
    module = load_seed_agent(tmp_path)
    wallet = {"remaining_usd": 3.50, "budget_usd": 5, "spent_usd": 1.50, "total_requests": 10, "avg_cost_per_request": 0.15}
    prompt = module.build_system_prompt(wallet)
    assert "$3.50" in prompt
    assert "$5.00" in prompt
    assert "$1.50" in prompt


def test_time_injected(tmp_path: Path) -> None:
    (tmp_path / "SYSTEM.md").write_text("You are an agent.\n", encoding="utf-8")
    module = load_seed_agent(tmp_path)
    wallet = {"remaining_usd": 5, "budget_usd": 5, "spent_usd": 0, "total_requests": 0, "avg_cost_per_request": 0}
    prompt = module.build_system_prompt(wallet)
    assert "Time:" in prompt
