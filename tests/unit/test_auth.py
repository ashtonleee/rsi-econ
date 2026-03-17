import pytest
from fastapi import HTTPException

from trusted.bridge.auth import resolve_identity


AGENT_TOKEN = "test-agent-token"
OPERATOR_TOKEN = "test-operator-token"


def test_agent_token_resolves_to_agent():
    actor = resolve_identity(
        f"Bearer {AGENT_TOKEN}",
        agent_token=AGENT_TOKEN,
        operator_token=OPERATOR_TOKEN,
    )
    assert actor == "agent"


def test_operator_token_resolves_to_operator():
    actor = resolve_identity(
        f"Bearer {OPERATOR_TOKEN}",
        agent_token=AGENT_TOKEN,
        operator_token=OPERATOR_TOKEN,
    )
    assert actor == "operator"


def test_wrong_token_raises_401():
    with pytest.raises(HTTPException) as exc_info:
        resolve_identity(
            "Bearer wrong-token",
            agent_token=AGENT_TOKEN,
            operator_token=OPERATOR_TOKEN,
        )
    assert exc_info.value.status_code == 401
    assert "unrecognized" in exc_info.value.detail


def test_missing_header_raises_401():
    with pytest.raises(HTTPException) as exc_info:
        resolve_identity(
            None,
            agent_token=AGENT_TOKEN,
            operator_token=OPERATOR_TOKEN,
        )
    assert exc_info.value.status_code == 401
    assert "missing" in exc_info.value.detail


def test_malformed_header_no_bearer_prefix_raises_401():
    with pytest.raises(HTTPException) as exc_info:
        resolve_identity(
            f"Token {AGENT_TOKEN}",
            agent_token=AGENT_TOKEN,
            operator_token=OPERATOR_TOKEN,
        )
    assert exc_info.value.status_code == 401
    assert "malformed" in exc_info.value.detail


def test_empty_bearer_raises_401():
    with pytest.raises(HTTPException) as exc_info:
        resolve_identity(
            "Bearer ",
            agent_token=AGENT_TOKEN,
            operator_token=OPERATOR_TOKEN,
        )
    assert exc_info.value.status_code == 401
    assert "unrecognized" in exc_info.value.detail


def test_config_rejects_identical_tokens(monkeypatch):
    monkeypatch.setenv("RSI_AGENT_TOKEN", "same-token")
    monkeypatch.setenv("RSI_OPERATOR_TOKEN", "same-token")
    from shared.config import bridge_settings
    with pytest.raises(AssertionError, match="must differ"):
        bridge_settings()
