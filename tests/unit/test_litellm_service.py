import json

import pytest
from fastapi.testclient import TestClient

from trusted.litellm.app import app


@pytest.mark.fast
def test_litellm_defaults_to_deterministic_mock(monkeypatch):
    monkeypatch.delenv("RSI_LITELLM_RESPONSE_MODE", raising=False)
    monkeypatch.delenv("RSI_OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with TestClient(app) as client:
        health = client.get("/healthz")
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "stage1-deterministic",
                "messages": [{"role": "user", "content": "summarize this"}],
            },
        )

    assert health.status_code == 200
    assert health.json()["details"]["response_mode"] == "deterministic_mock"
    assert health.json()["details"]["provider_key_configured"] is False
    assert response.status_code == 200
    body = response.json()
    assert body["model"] == "stage1-deterministic"
    assert body["choices"][0]["message"]["content"] == "stage1 deterministic reply: summarize this"


@pytest.mark.fast
def test_litellm_mock_returns_session_action_json_for_session_prompt(monkeypatch):
    monkeypatch.delenv("RSI_LITELLM_RESPONSE_MODE", raising=False)
    monkeypatch.delenv("RSI_OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    prompt = {
        "session_id": "session-1",
        "allowed_tools": ["bridge_status", "finish"],
        "instructions": ["Return JSON."],
    }

    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "stage1-deterministic",
                "messages": [{"role": "user", "content": json.dumps(prompt)}],
            },
        )

    assert response.status_code == 200
    content = response.json()["choices"][0]["message"]["content"]
    assert json.loads(content) == {
        "tool": "bridge_status",
        "reason": "deterministic mock session action",
        "params": {},
    }


@pytest.mark.fast
def test_provider_passthrough_mode_fails_fast_without_real_key(monkeypatch):
    monkeypatch.setenv("RSI_LITELLM_RESPONSE_MODE", "provider_passthrough")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(
        AssertionError,
        match="OPENAI_API_KEY must be set to a real provider key",
    ):
        with TestClient(app):
            pass


@pytest.mark.fast
def test_provider_passthrough_maps_request_and_response(monkeypatch):
    captured: dict[str, object] = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "id": "chatcmpl-provider",
                "object": "chat.completion",
                "created": 123,
                "model": "gpt-4.1-mini",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "Provider answer",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 7,
                    "completion_tokens": 3,
                    "total_tokens": 10,
                },
            }

    class FakeAsyncClient:
        def __init__(self, *, base_url, timeout, headers):
            captured["base_url"] = base_url
            captured["timeout"] = timeout
            captured["headers"] = headers

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, path, json):
            captured["path"] = path
            captured["json"] = json
            return FakeResponse()

    monkeypatch.setenv("RSI_LITELLM_RESPONSE_MODE", "provider_passthrough")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-provider-key")
    monkeypatch.setenv("RSI_OPENAI_BASE_URL", "https://provider.example/v1")
    monkeypatch.setattr("trusted.litellm.app.httpx.AsyncClient", FakeAsyncClient)

    with TestClient(app) as client:
        health = client.get("/healthz")
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4.1-mini",
                "messages": [{"role": "user", "content": "answer from provider"}],
            },
        )

    assert health.status_code == 200
    assert health.json()["details"]["response_mode"] == "provider_passthrough"
    assert health.json()["details"]["provider_key_configured"] is True
    assert response.status_code == 200
    body = response.json()
    assert body["model"] == "gpt-4.1-mini"
    assert body["choices"][0]["message"]["content"] == "Provider answer"
    assert captured["base_url"] == "https://provider.example/v1"
    assert captured["path"] == "/chat/completions"
    assert captured["headers"] == {"Authorization": "Bearer sk-test-provider-key"}
    assert captured["json"] == {
        "model": "gpt-4.1-mini",
        "messages": [{"role": "user", "content": "answer from provider"}],
    }
