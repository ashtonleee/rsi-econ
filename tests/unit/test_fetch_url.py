"""Tests for the fetch_url tool in sandbox/seed/main.py."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
from http.client import HTTPResponse
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
    module_name = f"test_fetch_url_{tmp_path.name}"
    spec = importlib.util.spec_from_file_location(module_name, SEED_AGENT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _mock_urlopen(body: bytes, content_type: str = "text/html"):
    """Create a mock for urllib.request.urlopen returning given body."""
    resp = MagicMock()
    resp.read.return_value = body
    resp.headers = MagicMock()
    resp.headers.get.return_value = content_type
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


class TestFetchUrl:
    def test_fetch_html_extracts_text(self, tmp_path):
        agent = load_seed_agent(tmp_path)
        html = b"""<html><head><title>Test</title></head><body>
        <nav>Navigation here</nav>
        <script>var x = 1;</script>
        <main><p>Important content</p><p>More content</p></main>
        <footer>Footer stuff</footer>
        </body></html>"""
        mock_resp = _mock_urlopen(html, "text/html")
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = agent.execute_tool("fetch_url", {"url": "http://example.com"})
        data = json.loads(result)
        assert "Important content" in data["text"]
        assert "More content" in data["text"]
        # Nav, script, footer should be stripped
        assert "Navigation here" not in data["text"]
        assert "var x = 1" not in data["text"]
        assert "Footer stuff" not in data["text"]

    def test_fetch_json_returns_raw(self, tmp_path):
        agent = load_seed_agent(tmp_path)
        json_body = b'{"key": "value", "count": 42}'
        mock_resp = _mock_urlopen(json_body, "application/json")
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = agent.execute_tool("fetch_url", {"url": "http://api.example.com/data"})
        # JSON content-type should return raw body
        assert '"key": "value"' in result
        assert '"count": 42' in result

    def test_fetch_raw_flag(self, tmp_path):
        agent = load_seed_agent(tmp_path)
        html = b"<html><body><p>Hello</p></body></html>"
        mock_resp = _mock_urlopen(html, "text/html")
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = agent.execute_tool("fetch_url", {"url": "http://example.com", "raw": True})
        # raw=True should return full HTML, not extracted text
        assert "<html>" in result
        assert "<p>Hello</p>" in result

    def test_fetch_error_returns_json(self, tmp_path):
        agent = load_seed_agent(tmp_path)
        with patch("urllib.request.urlopen", side_effect=Exception("Connection refused")):
            result = agent.execute_tool("fetch_url", {"url": "http://bad.example.com"})
        data = json.loads(result)
        assert data["url"] == "http://bad.example.com"
        assert "Connection refused" in data["error"]

    def test_fetch_strips_script_style(self, tmp_path):
        agent = load_seed_agent(tmp_path)
        html = b"""<html><body>
        <style>body { color: red; }</style>
        <script>alert('xss');</script>
        <p>Visible text</p>
        <style>.hidden { display: none; }</style>
        </body></html>"""
        mock_resp = _mock_urlopen(html, "text/html")
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = agent.execute_tool("fetch_url", {"url": "http://example.com"})
        data = json.loads(result)
        assert "Visible text" in data["text"]
        assert "color: red" not in data["text"]
        assert "alert" not in data["text"]
        assert "display: none" not in data["text"]

    def test_fetch_truncates_long_content(self, tmp_path):
        agent = load_seed_agent(tmp_path)
        # Generate HTML with >15K chars of text content
        long_text = "A" * 50000
        html = f"<html><body><p>{long_text}</p></body></html>".encode()
        mock_resp = _mock_urlopen(html, "text/html")
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = agent.execute_tool("fetch_url", {"url": "http://example.com"})
        # text[:15000] truncates the extracted text, then truncate_output
        # further caps the serialized JSON to 10K, so the final result
        # is well under the original 50K and contains the truncation marker
        assert len(result) < 50000
        assert "truncated" in result


class TestFetchUrlInToolList:
    def test_fetch_url_in_tools(self, tmp_path):
        agent = load_seed_agent(tmp_path)
        tool_names = [t["function"]["name"] for t in agent.TOOLS]
        assert "fetch_url" in tool_names
        # browse_url should still exist
        assert "browse_url" in tool_names
