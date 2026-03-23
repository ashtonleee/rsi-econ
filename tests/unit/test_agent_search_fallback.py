"""Tests for web_search tool: bridge-first with Playwright fallback."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
SEED_AGENT_PATH = ROOT / "sandbox" / "seed" / "main.py"


def load_seed_agent(tmp_path: Path):
    os.environ["RSI_AGENT_WORKSPACE"] = str(tmp_path)
    os.environ["LITELLM_URL"] = "http://litellm:4000"
    os.environ["WALLET_URL"] = "http://bridge:8081"
    os.environ["RSI_MODEL"] = "default"
    os.environ["RSI_MAX_TURNS"] = "5"
    module_name = f"test_seed_agent_search_{tmp_path.name}"
    spec = importlib.util.spec_from_file_location(module_name, SEED_AGENT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class FakeBridgeResponse:
    """Mock a successful bridge /search response."""

    def __init__(self, results=None):
        self.payload = {
            "results": [
                {"title": "Exa Result", "url": "https://exa.example.com", "text": "Found via Exa"},
            ] if results is None else results
        }

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeBrowser:
    """Mock browser tool."""

    def __init__(self):
        self.search_called = False
        self.search_args = None

    def search(self, query, engine="duckduckgo"):
        self.search_called = True
        self.search_args = (query, engine)
        return {"results": [{"title": "Playwright Result", "url": "https://ddg.example.com", "snippet": "Found via Playwright"}]}


def test_web_search_tries_bridge_first(tmp_path: Path) -> None:
    """When bridge /search succeeds, Playwright should NOT be called."""
    mod = load_seed_agent(tmp_path)
    fake_browser = FakeBrowser()

    with patch.object(mod.urllib_request, "urlopen", return_value=FakeBridgeResponse()):
        with patch.object(mod, "get_browser", return_value=fake_browser):
            result = mod.execute_tool("web_search", {"query": "test query"})

    # Should return Exa results
    parsed = json.loads(result)
    assert parsed["results"][0]["title"] == "Exa Result"
    # Playwright should NOT have been called
    assert not fake_browser.search_called


def test_web_search_falls_back_to_playwright(tmp_path: Path) -> None:
    """When bridge /search fails, should fall back to Playwright."""
    mod = load_seed_agent(tmp_path)
    fake_browser = FakeBrowser()

    with patch.object(mod.urllib_request, "urlopen", side_effect=Exception("bridge down")):
        with patch.object(mod, "get_browser", return_value=fake_browser):
            result = mod.execute_tool("web_search", {"query": "fallback query"})

    # Should return Playwright results
    parsed = json.loads(result)
    assert parsed["results"][0]["title"] == "Playwright Result"
    # Playwright SHOULD have been called
    assert fake_browser.search_called
    assert fake_browser.search_args[0] == "fallback query"


def test_web_search_falls_back_on_empty_results(tmp_path: Path) -> None:
    """When bridge returns empty results, should fall back to Playwright."""
    mod = load_seed_agent(tmp_path)
    fake_browser = FakeBrowser()

    empty_response = FakeBridgeResponse(results=[])

    with patch.object(mod.urllib_request, "urlopen", return_value=empty_response):
        with patch.object(mod, "get_browser", return_value=fake_browser):
            result = mod.execute_tool("web_search", {"query": "empty query"})

    # Should fall back to Playwright because bridge returned empty results
    assert fake_browser.search_called
    parsed = json.loads(result)
    assert parsed["results"][0]["title"] == "Playwright Result"
