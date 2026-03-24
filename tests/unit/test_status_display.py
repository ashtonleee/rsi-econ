"""Tests for enhanced status display."""

from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SESSION_CLI_PATH = ROOT / "cli" / "session.py"


def load_session_cli():
    spec = importlib.util.spec_from_file_location("session_cli_status", SESSION_CLI_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_status_formats_budget_bar() -> None:
    cli = load_session_cli()
    bar_full = cli._budget_bar(95)
    assert "FULL" in bar_full
    assert "\u2588" in bar_full  # filled block

    bar_low = cli._budget_bar(8)
    assert "CONSERVE" in bar_low

    bar_critical = cli._budget_bar(3)
    assert "WRAPUP" in bar_critical


def test_budget_bar_moderate() -> None:
    cli = load_session_cli()
    bar = cli._budget_bar(35)
    assert "MODERATE" in bar
