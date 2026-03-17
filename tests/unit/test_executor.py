"""Unit tests for the proposal executor (Stage 8).

Tests echo (preserved from Stage 7), http_post dispatch, and error paths.
"""
import base64
import hashlib
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from shared.schemas import EgressFetchResponse, ProposalRecord
from trusted.bridge.executor import execute_proposal


def _make_proposal(action_type: str, action_payload: dict | None = None) -> ProposalRecord:
    return ProposalRecord(
        proposal_id="test-proposal-id",
        action_type=action_type,
        action_payload=action_payload or {},
        status="approved",
        created_by="agent",
        created_at="2026-03-16T00:00:00Z",
        decided_by="operator",
        decided_at="2026-03-16T00:00:01Z",
        request_id="req-1",
        trace_id="trace-1",
    )


def _mock_clients(*, egress_response: EgressFetchResponse | None = None, egress_error: Exception | None = None):
    clients = AsyncMock()
    if egress_error:
        clients.egress_fetch.side_effect = egress_error
    elif egress_response:
        clients.egress_fetch.return_value = egress_response
    return clients


def _make_egress_response(body: bytes = b'{"ok": true}', http_status: int = 200) -> EgressFetchResponse:
    return EgressFetchResponse(
        normalized_url="http://example.com/action",
        scheme="http",
        host="example.com",
        port=80,
        channel="consequential_action",
        approved_ips=["1.2.3.4"],
        actual_peer_ip="1.2.3.4",
        dialed_ip="1.2.3.4",
        request_forwarded=True,
        enforcement_stage="pre_connect",
        http_status=http_status,
        headers={"content-type": "application/json"},
        body_base64=base64.b64encode(body).decode(),
    )


ALLOWLIST = {"example.com"}


# --- Echo action (preserved from Stage 7) ---


@pytest.mark.anyio
async def test_echo_action_returns_payload():
    proposal = _make_proposal("echo", {"message": "hello"})
    result = await execute_proposal(
        proposal, clients=_mock_clients(), action_allowlist_hosts=set(),
    )
    assert result == {"echoed": {"message": "hello"}}


# --- Unknown action type ---


@pytest.mark.anyio
async def test_unknown_action_type_returns_error():
    proposal = _make_proposal("unknown_action")
    result = await execute_proposal(
        proposal, clients=_mock_clients(), action_allowlist_hosts=set(),
    )
    assert "error" in result
    assert "unsupported" in result["error"]


# --- http_post: action allowlist ---


@pytest.mark.anyio
async def test_http_post_host_not_in_action_allowlist_returns_error():
    proposal = _make_proposal("http_post", {
        "url": "http://example.com/action",
        "body": {"key": "value"},
    })
    result = await execute_proposal(
        proposal, clients=_mock_clients(), action_allowlist_hosts=set(),
    )
    assert result["error"] == "host_not_in_action_allowlist"
    assert result["host"] == "example.com"


@pytest.mark.anyio
async def test_http_post_missing_url_returns_error():
    proposal = _make_proposal("http_post", {"body": {}})
    result = await execute_proposal(
        proposal, clients=_mock_clients(), action_allowlist_hosts=ALLOWLIST,
    )
    assert "url is required" in result["error"]


# --- http_post: body size limit ---


@pytest.mark.anyio
async def test_http_post_body_exceeds_max_size_returns_error():
    big_body = {"data": "x" * 100000}
    proposal = _make_proposal("http_post", {
        "url": "http://example.com/action",
        "body": big_body,
    })
    result = await execute_proposal(
        proposal,
        clients=_mock_clients(),
        action_allowlist_hosts=ALLOWLIST,
        action_max_body_bytes=1024,
    )
    assert result["error"] == "body_exceeds_max_size"


# --- http_post: success ---


@pytest.mark.anyio
async def test_http_post_success_returns_structured_result():
    response_body = b'{"echoed": {"key": "value"}}'
    egress_resp = _make_egress_response(body=response_body, http_status=200)
    clients = _mock_clients(egress_response=egress_resp)

    proposal = _make_proposal("http_post", {
        "url": "http://example.com/action",
        "body": {"key": "value"},
        "content_type": "application/json",
    })
    result = await execute_proposal(
        proposal, clients=clients, action_allowlist_hosts=ALLOWLIST,
    )

    assert result["http_status"] == 200
    assert result["url"] == "http://example.com/action"
    assert result["response_content_type"] == "application/json"
    assert result["response_byte_count"] == len(response_body)
    assert result["response_body_sha256"] == hashlib.sha256(response_body).hexdigest()
    assert "echoed" in result["response_body_preview"]

    # Verify the egress was called with correct parameters
    call_args = clients.egress_fetch.call_args
    egress_req = call_args[0][0]
    assert egress_req.method == "POST"
    assert egress_req.channel == "consequential_action"
    assert egress_req.request_content_type == "application/json"


@pytest.mark.anyio
async def test_http_post_with_empty_body_succeeds():
    egress_resp = _make_egress_response(body=b"ok", http_status=200)
    clients = _mock_clients(egress_response=egress_resp)

    proposal = _make_proposal("http_post", {
        "url": "http://example.com/action",
    })
    result = await execute_proposal(
        proposal, clients=clients, action_allowlist_hosts=ALLOWLIST,
    )
    assert result["http_status"] == 200

    # Empty body still serializes as {} (the default)
    call_args = clients.egress_fetch.call_args
    egress_req = call_args[0][0]
    decoded = base64.b64decode(egress_req.request_body_base64)
    assert json.loads(decoded) == {}


# --- http_post: egress errors ---


@pytest.mark.anyio
async def test_http_post_egress_denied_returns_error():
    error_response = httpx.Response(403, json={"detail": {"reason": "blocked"}})
    error = httpx.HTTPStatusError(
        "denied", request=httpx.Request("POST", "http://egress/internal/fetch"), response=error_response,
    )
    clients = _mock_clients(egress_error=error)

    proposal = _make_proposal("http_post", {
        "url": "http://example.com/action",
        "body": {"key": "value"},
    })
    result = await execute_proposal(
        proposal, clients=clients, action_allowlist_hosts=ALLOWLIST,
    )
    assert result["error"] == "egress_denied"
    assert result["egress_status"] == 403


@pytest.mark.anyio
async def test_http_post_egress_unreachable_returns_error():
    clients = _mock_clients(egress_error=httpx.ConnectError("connection refused"))

    proposal = _make_proposal("http_post", {
        "url": "http://example.com/action",
        "body": {},
    })
    result = await execute_proposal(
        proposal, clients=clients, action_allowlist_hosts=ALLOWLIST,
    )
    assert "egress_unreachable" in result["error"]
