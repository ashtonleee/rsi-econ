"""Unit tests for egress POST method support (Stage 8).

Tests that POST is allowed only on the consequential_action channel,
and that existing GET-only channels reject POST requests.
"""
import base64

import pytest
from fastapi.testclient import TestClient

from trusted.egress.app import app, startup_checks, CHANNELS_ALLOWING_POST


@pytest.fixture(autouse=True)
def _egress_app(monkeypatch):
    monkeypatch.setenv("RSI_WEB_ALLOWLIST_HOSTS", "example.com")
    monkeypatch.setenv("RSI_FETCH_ALLOW_PRIVATE_TEST_HOSTS", "example.com")
    startup_checks(app)


@pytest.fixture()
def client():
    return TestClient(app)


# --- Channel-method gating ---


def test_post_on_consequential_action_channel_is_accepted(client):
    """POST on the consequential_action channel should not return 405."""
    body = base64.b64encode(b'{"key": "value"}').decode()
    resp = client.post("/internal/fetch", json={
        "url": "http://example.com/action",
        "channel": "consequential_action",
        "method": "POST",
        "request_body_base64": body,
        "request_content_type": "application/json",
        "max_body_bytes": 4096,
    })
    # May fail with 502 (no real server) but must NOT be 405
    assert resp.status_code != 405


def test_post_on_fetcher_channel_returns_405(client):
    """POST on the fetcher/top_level_navigation channel must return 405."""
    resp = client.post("/internal/fetch", json={
        "url": "http://example.com/data",
        "channel": "top_level_navigation",
        "method": "POST",
        "request_body_base64": base64.b64encode(b"body").decode(),
        "max_body_bytes": 4096,
    })
    assert resp.status_code == 405
    detail = resp.json()["detail"]
    assert "method_not_allowed" in detail["reason"]
    assert "top_level_navigation" in detail["detail"]


def test_post_on_redirect_channel_returns_405(client):
    """POST on the redirect channel must return 405."""
    resp = client.post("/internal/fetch", json={
        "url": "http://example.com/data",
        "channel": "redirect",
        "method": "POST",
        "max_body_bytes": 4096,
    })
    assert resp.status_code == 405


def test_post_on_subresource_channel_returns_405(client):
    """POST on the subresource channel must return 405."""
    resp = client.post("/internal/fetch", json={
        "url": "http://example.com/data",
        "channel": "subresource",
        "method": "POST",
        "max_body_bytes": 4096,
    })
    assert resp.status_code == 405


def test_unsupported_method_returns_405(client):
    """Methods other than GET and POST must return 405."""
    resp = client.post("/internal/fetch", json={
        "url": "http://example.com/data",
        "channel": "consequential_action",
        "method": "DELETE",
        "max_body_bytes": 4096,
    })
    assert resp.status_code == 405
    assert "unsupported method" in resp.json()["detail"]["detail"]


# --- GET backward compatibility ---


def test_get_on_consequential_action_channel_is_accepted(client):
    """GET on the consequential_action channel should not return 405."""
    resp = client.post("/internal/fetch", json={
        "url": "http://example.com/data",
        "channel": "consequential_action",
        "method": "GET",
        "max_body_bytes": 4096,
    })
    # May fail with 502 (no real server) but must NOT be 405
    assert resp.status_code != 405


def test_get_default_method_still_works(client):
    """Default method (GET) on a standard channel should work as before."""
    resp = client.post("/internal/fetch", json={
        "url": "http://example.com/data",
        "channel": "top_level_navigation",
        "max_body_bytes": 4096,
    })
    # May fail with 502 (no real server) but must NOT be 405
    assert resp.status_code != 405


# --- URL policy still enforced for POST ---


def test_post_on_blocked_host_returns_403(client):
    """POST to a non-allowlisted host must be denied by URL policy."""
    resp = client.post("/internal/fetch", json={
        "url": "http://not-allowed.test/action",
        "channel": "consequential_action",
        "method": "POST",
        "request_body_base64": base64.b64encode(b"{}").decode(),
        "request_content_type": "application/json",
        "max_body_bytes": 4096,
    })
    assert resp.status_code == 403


# --- Channel constant sanity check ---


def test_channels_allowing_post_only_contains_consequential_action():
    assert CHANNELS_ALLOWING_POST == {"consequential_action"}
