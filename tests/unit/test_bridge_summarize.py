"""Tests for the bridge /summarize, /agent/screenshot, and /agent/status endpoints."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
WALLET_API_PATH = ROOT / "trusted" / "bridge" / "wallet_api.py"


def load_wallet_api(tmp_path: Path):
    """Load wallet_api module with test-appropriate paths."""
    os.environ["PROPOSALS_DIR"] = str(tmp_path / "proposals")
    os.environ["LLM_USAGE_LOG_PATH"] = str(tmp_path / "llm_usage.jsonl")
    os.environ["PROXY_ALLOWLIST_PATH"] = str(tmp_path / "proxy_allowlist.txt")
    os.environ["LITELLM_URL"] = "http://litellm-test:4000"
    os.environ["RSI_BUDGET_USD"] = "5.00"
    os.environ["GIT_REPO_DIR"] = str(tmp_path / "git-repo")
    os.environ["GIT_WORKSPACE_DIR"] = str(tmp_path / "workspace")
    os.environ["SEED_DIR"] = str(tmp_path / "seed")
    os.environ["OPERATOR_MESSAGES_DIR"] = str(tmp_path / "operator_messages")
    os.environ["NOTIFICATION_CONFIG_PATH"] = str(tmp_path / "notification_config.json")
    os.environ["EVENTS_DIR"] = str(tmp_path / "events")
    os.environ["EVENT_POLL_INTERVAL"] = "9999"

    # Write minimal notification config
    (tmp_path / "notification_config.json").write_text(json.dumps({
        "webhook_url": "", "events": {}
    }))
    (tmp_path / "events").mkdir(exist_ok=True)

    module_name = f"test_wallet_api_{tmp_path.name}"
    spec = importlib.util.spec_from_file_location(module_name, WALLET_API_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # Register in sys.modules so dataclass introspection works on Python 3.9
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class FakeLLMResponse:
    def __init__(self, summary: str = "The agent is researching providers."):
        self.payload = {
            "choices": [{"message": {"content": summary}}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 30, "total_tokens": 80},
        }

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_summarize_returns_string(tmp_path: Path) -> None:
    mod = load_wallet_api(tmp_path)
    app = mod.create_app(
        proposals_dir=tmp_path / "proposals",
        usage_log_path=tmp_path / "llm_usage.jsonl",
        allowlist_path=tmp_path / "proxy_allowlist.txt",
        litellm_base_url="http://litellm-test:4000",
        budget_usd=5.0,
    )
    from fastapi.testclient import TestClient

    with patch.object(mod.urllib_request, "urlopen", return_value=FakeLLMResponse()):
        with TestClient(app) as client:
            resp = client.post("/summarize", json={"text": "Agent browsed groq.com and found free tier"})

    assert resp.status_code == 200
    body = resp.json()
    assert "summary" in body
    assert body["summary"] == "The agent is researching providers."


def test_summarize_truncates_input(tmp_path: Path) -> None:
    mod = load_wallet_api(tmp_path)
    app = mod.create_app(
        proposals_dir=tmp_path / "proposals",
        usage_log_path=tmp_path / "llm_usage.jsonl",
        allowlist_path=tmp_path / "proxy_allowlist.txt",
        litellm_base_url="http://litellm-test:4000",
        budget_usd=5.0,
    )
    from fastapi.testclient import TestClient

    captured_body = {}

    def fake_urlopen(req, timeout=0):
        captured_body["data"] = json.loads(req.data.decode("utf-8"))
        return FakeLLMResponse()

    with patch.object(mod.urllib_request, "urlopen", side_effect=fake_urlopen):
        with TestClient(app) as client:
            long_text = "x" * 10000
            resp = client.post("/summarize", json={"text": long_text})

    assert resp.status_code == 200
    # The user message should be truncated to 4000 chars
    user_msg = captured_body["data"]["messages"][1]["content"]
    assert len(user_msg) <= 4000


def test_summarize_separate_budget(tmp_path: Path) -> None:
    """Summary calls should NOT be tracked in the agent's SpendTracker."""
    mod = load_wallet_api(tmp_path)
    app = mod.create_app(
        proposals_dir=tmp_path / "proposals",
        usage_log_path=tmp_path / "llm_usage.jsonl",
        allowlist_path=tmp_path / "proxy_allowlist.txt",
        litellm_base_url="http://litellm-test:4000",
        budget_usd=5.0,
    )
    from fastapi.testclient import TestClient

    with patch.object(mod.urllib_request, "urlopen", return_value=FakeLLMResponse()):
        with TestClient(app) as client:
            # Check wallet before
            wallet_before = client.get("/wallet").json()
            client.post("/summarize", json={"text": "some logs"})
            wallet_after = client.get("/wallet").json()

    # SpendTracker should be unaffected
    assert wallet_before["spent_usd"] == wallet_after["spent_usd"]
    assert wallet_before["total_requests"] == wallet_after["total_requests"]


def test_summarize_empty_text(tmp_path: Path) -> None:
    mod = load_wallet_api(tmp_path)
    app = mod.create_app(
        proposals_dir=tmp_path / "proposals",
        usage_log_path=tmp_path / "llm_usage.jsonl",
        allowlist_path=tmp_path / "proxy_allowlist.txt",
        litellm_base_url="http://litellm-test:4000",
        budget_usd=5.0,
    )
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        resp = client.post("/summarize", json={"text": ""})

    assert resp.status_code == 200
    assert resp.json()["summary"] == "(no content to summarize)"


def test_agent_screenshot_returns_image(tmp_path: Path) -> None:
    mod = load_wallet_api(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    # Write a fake PNG
    fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
    (workspace / "latest_screenshot.png").write_bytes(fake_png)

    app = mod.create_app(
        proposals_dir=tmp_path / "proposals",
        usage_log_path=tmp_path / "llm_usage.jsonl",
        allowlist_path=tmp_path / "proxy_allowlist.txt",
        litellm_base_url="http://litellm-test:4000",
        budget_usd=5.0,
    )
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        resp = client.get("/agent/screenshot")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content == fake_png


def test_agent_screenshot_404_when_missing(tmp_path: Path) -> None:
    mod = load_wallet_api(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)

    app = mod.create_app(
        proposals_dir=tmp_path / "proposals",
        usage_log_path=tmp_path / "llm_usage.jsonl",
        allowlist_path=tmp_path / "proxy_allowlist.txt",
        litellm_base_url="http://litellm-test:4000",
        budget_usd=5.0,
    )
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        resp = client.get("/agent/screenshot")

    assert resp.status_code == 404


def test_agent_status_returns_knowledge_and_status(tmp_path: Path) -> None:
    mod = load_wallet_api(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)

    # Write knowledge.json
    knowledge = {"version": 2, "findings": ["Found free tier at Groq"]}
    (workspace / "knowledge.json").write_text(json.dumps(knowledge))

    # Write agent_status.json
    status = {"turn": 42, "messages": 20, "tokens": 50000}
    (workspace / "agent_status.json").write_text(json.dumps(status))

    app = mod.create_app(
        proposals_dir=tmp_path / "proposals",
        usage_log_path=tmp_path / "llm_usage.jsonl",
        allowlist_path=tmp_path / "proxy_allowlist.txt",
        litellm_base_url="http://litellm-test:4000",
        budget_usd=5.0,
    )
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        resp = client.get("/agent/status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["knowledge"]["findings"] == ["Found free tier at Groq"]
    assert body["agent_status"]["turn"] == 42
    assert body["paused"] is False
