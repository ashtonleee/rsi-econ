"""Tests for 429 handling: rate-limit vs budget-exhausted."""
from __future__ import annotations

import io
import json
import time
from http.client import HTTPResponse
from pathlib import Path
from unittest.mock import patch
from urllib import error as urllib_error

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_seed_agent import FAKE_WALLET, load_seed_agent, write_agent_workspace


class FakeHTTPError(urllib_error.HTTPError):
    """HTTPError with a readable body and optional headers."""

    def __init__(self, body: str = "", headers: dict | None = None):
        self._body = body.encode("utf-8")
        self._headers_dict = headers or {}
        super().__init__("http://litellm:4000", 429, "Too Many Requests", self._headers_dict, io.BytesIO(self._body))

    def read(self) -> bytes:
        return self._body

    @property
    def headers(self):
        return self._headers_dict


def test_rate_limit_429_retries(tmp_path: Path, monkeypatch) -> None:
    """A 429 with 'rate limit' in the body should retry, not exit."""
    write_agent_workspace(tmp_path)
    module = load_seed_agent(tmp_path)

    call_count = 0

    def fake_chat(messages, model=None, tools=None):  # noqa: ANN001, ARG001
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise FakeHTTPError(body='{"error": {"message": "rate limit exceeded"}}')
        # Second call succeeds and finishes
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "finish-1",
                                "type": "function",
                                "function": {"name": "finish", "arguments": '{"reason":"done"}'},
                            }
                        ],
                    }
                }
            ]
        }

    monkeypatch.setattr(module, "chat", fake_chat)
    monkeypatch.setattr(module, "get_wallet", lambda: FAKE_WALLET)
    monkeypatch.setattr(time, "sleep", lambda _: None)  # skip actual sleep

    assert module.main() == 0
    assert call_count == 2, "Agent should have retried after rate limit"


def test_budget_exhausted_429_exits(tmp_path: Path, monkeypatch) -> None:
    """A 429 with 'budget exceeded' in the body should exit."""
    write_agent_workspace(tmp_path)
    module = load_seed_agent(tmp_path)

    def fake_chat(messages, model=None, tools=None):  # noqa: ANN001, ARG001
        raise FakeHTTPError(body='{"error": {"message": "budget exceeded for this key"}}')

    monkeypatch.setattr(module, "chat", fake_chat)
    monkeypatch.setattr(module, "get_wallet", lambda: FAKE_WALLET)

    assert module.main() == 0  # clean exit


def test_empty_body_429_exits(tmp_path: Path, monkeypatch) -> None:
    """A 429 with no body should default to budget-exhausted (safe default)."""
    write_agent_workspace(tmp_path)
    module = load_seed_agent(tmp_path)

    def fake_chat(messages, model=None, tools=None):  # noqa: ANN001, ARG001
        raise FakeHTTPError(body="")

    monkeypatch.setattr(module, "chat", fake_chat)
    monkeypatch.setattr(module, "get_wallet", lambda: FAKE_WALLET)

    assert module.main() == 0  # clean exit


def test_rate_limit_respects_retry_after_header(tmp_path: Path, monkeypatch) -> None:
    """Rate limit should use retry-after header when available."""
    write_agent_workspace(tmp_path)
    module = load_seed_agent(tmp_path)

    call_count = 0
    sleep_values: list[float] = []

    def fake_chat(messages, model=None, tools=None):  # noqa: ANN001, ARG001
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise FakeHTTPError(
                body='{"error": {"message": "too many requests"}}',
                headers={"retry-after": "5"},
            )
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "finish-1",
                                "type": "function",
                                "function": {"name": "finish", "arguments": '{"reason":"done"}'},
                            }
                        ],
                    }
                }
            ]
        }

    monkeypatch.setattr(module, "chat", fake_chat)
    monkeypatch.setattr(module, "get_wallet", lambda: FAKE_WALLET)

    original_sleep = time.sleep
    def track_sleep(secs):
        sleep_values.append(secs)
    monkeypatch.setattr(time, "sleep", track_sleep)

    module.main()
    assert 5 in sleep_values, f"Expected sleep(5) from retry-after header, got {sleep_values}"
