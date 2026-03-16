import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from trusted.bridge.app import app
from trusted.bridge.clients import TrustedBridgeClients
from shared.schemas import (
    BrowserFollowHrefInternalResponse,
    BrowserRenderInternalResponse,
    ChatChoice,
    ChatCompletionResponse,
    ChatMessage,
    ChatUsage,
    FetcherFetchResponse,
)


TINY_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO2Wb6wAAAAASUVORK5CYII="
)


def load_events(log_path: Path) -> list[dict]:
    if not log_path.exists():
        return []
    return [
        json.loads(line)
        for line in log_path.read_text(encoding="ascii").splitlines()
        if line.strip()
    ]


def test_bridge_health_contract(monkeypatch, tmp_path):
    monkeypatch.setenv("RSI_TRUSTED_STATE_DIR", str(tmp_path))
    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "bridge"
    assert body["status"] == "ok"
    assert body["stage"] == "stage6_read_only_browser"
    assert body["details"]["trusted_state_ready"] is True
    assert "litellm_reachable" in body["details"]
    assert "fetcher_reachable" in body["details"]
    assert "browser_reachable" in body["details"]
    assert body["details"]["log_path"].endswith("bridge_events.jsonl")


def test_bridge_status_exposes_budget_and_trusted_state_surfaces(monkeypatch, tmp_path):
    monkeypatch.setenv("RSI_TRUSTED_STATE_DIR", str(tmp_path))
    with TestClient(app) as client:
        response = client.get("/status")

    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "bridge"
    assert body["surfaces"]["litellm"] == "mediated_via_trusted_service"
    assert body["surfaces"]["canonical_logging"] == "active_canonical_event_log"
    assert body["surfaces"]["budgeting"] == "enforced_token_cap_stage2"
    assert body["surfaces"]["seed_agent"] == "local_only_stage3_substrate"
    assert body["surfaces"]["recovery"] == "trusted_host_checkpoint_controls_stage4"
    assert body["surfaces"]["read_only_web"] == "trusted_fetcher_stage5_read_only_get"
    assert body["surfaces"]["browser"] == "trusted_browser_stage6a_read_only_render"
    assert body["surfaces"]["browser_follow_href"] == "trusted_browser_stage6b_safe_follow_href"
    assert body["surfaces"]["approvals"] == "stubbed_for_stage_7"
    assert body["log_path"].endswith("bridge_events.jsonl")
    assert body["operational_state_path"].endswith("operational_state.json")
    assert body["connections"]["litellm"]["url"].startswith("http://")
    assert body["connections"]["fetcher"]["url"].startswith("http://")
    assert body["connections"]["browser"]["url"].startswith("http://")
    assert body["budget"]["unit"] == "mock_tokens"
    assert body["budget"]["remaining"] == body["budget"]["total"]
    assert body["recovery"]["baseline_id"]
    assert body["recovery"]["checkpoint_dir"].endswith("/checkpoints")
    assert body["recovery"]["current_workspace_status"] == "seed_baseline"
    assert body["web"]["allowlist_hosts"] == ["example.com"]
    assert body["web"]["fetcher"]["url"].startswith("http://")
    assert body["web"]["caps"]["max_redirects"] >= 1
    assert body["browser"]["service"]["url"].startswith("http://")
    assert body["browser"]["caps"]["viewport_width"] == 1280
    assert body["browser"]["counters"]["browser_render_total"] == 0
    assert body["browser"]["counters"]["browser_follow_href_total"] == 0
    assert body["browser"]["caps"]["max_follow_hops"] == 1
    assert body["browser"]["caps"]["max_followable_links"] == 20
    assert body["browser"]["recent_follows"] == []
    assert isinstance(body["recent_requests"], list)


def test_status_query_logs_server_assigned_unauthenticated_actor(monkeypatch, tmp_path):
    monkeypatch.setenv("RSI_TRUSTED_STATE_DIR", str(tmp_path))
    with TestClient(app) as client:
        response = client.get("/status", headers={"x-rsi-actor": "operator"})

    assert response.status_code == 200
    events = load_events(tmp_path / "logs" / "bridge_events.jsonl")
    status_events = [event for event in events if event["event_type"] == "status_query"]
    assert status_events
    assert status_events[-1]["actor"] == "unauthenticated_bridge_client"


def test_agent_run_events_ignore_spoofed_actor_header(monkeypatch, tmp_path):
    monkeypatch.setenv("RSI_TRUSTED_STATE_DIR", str(tmp_path))
    with TestClient(app) as client:
        response = client.post(
            "/agent/runs/events",
            headers={"x-rsi-actor": "operator"},
            json={
                "run_id": "run-1",
                "event_kind": "run_start",
                "step_index": None,
                "tool_name": None,
                "summary": {"task": "unit actor hardening"},
            },
        )

    assert response.status_code == 200
    events = load_events(tmp_path / "logs" / "bridge_events.jsonl")
    agent_events = [event for event in events if event["event_type"] == "agent_run"]
    assert agent_events
    assert agent_events[-1]["actor"] == "agent"
    assert agent_events[-1]["summary"]["reported_origin"] == "untrusted_agent"


def _patched_route_result(case: str):
    if case == "llm_call":
        async def fake_chat_completion(self, payload):
            return ChatCompletionResponse(
                id="chatcmpl-spoof-test",
                object="chat.completion",
                created=1,
                model=payload.model,
                choices=[
                    ChatChoice(
                        index=0,
                        message=ChatMessage(role="assistant", content="spoof-resistant reply"),
                        finish_reason="stop",
                    )
                ],
                usage=ChatUsage(prompt_tokens=2, completion_tokens=3, total_tokens=5),
            )

        return "chat_completion", fake_chat_completion

    if case == "web_fetch":
        async def fake_fetch_url(self, payload):
            return FetcherFetchResponse(
                normalized_url=payload.url,
                final_url=payload.url,
                scheme="http",
                host="allowed.test",
                port=80,
                http_status=200,
                content_type="text/plain",
                byte_count=12,
                truncated=False,
                redirect_chain=[],
                resolved_ips=["93.184.216.34"],
                approved_ips=["93.184.216.34"],
                actual_peer_ip="93.184.216.34",
                used_ip="93.184.216.34",
                content_sha256="fetch-sha256",
                text="fixture body",
                mediation_hops=[],
            )

        return "fetch_url", fake_fetch_url

    if case == "browser_render":
        async def fake_browser_render(self, payload):
            return BrowserRenderInternalResponse(
                normalized_url=payload.url,
                final_url=payload.url,
                http_status=200,
                page_title="Fixture Browser Title",
                meta_description="Fixture browser description",
                rendered_text="Rendered browser text",
                rendered_text_sha256="rendered-text-sha256",
                text_bytes=21,
                text_truncated=False,
                screenshot_png_base64=TINY_PNG_BASE64,
                screenshot_sha256="render-screenshot-sha256",
                screenshot_bytes=67,
                redirect_chain=[],
                observed_hosts=["allowed.test"],
                resolved_ips=["93.184.216.34"],
                channel_records=[],
                followable_links=[],
            )

        return "browser_render", fake_browser_render

    if case == "browser_follow_href":
        async def fake_browser_follow_href(self, payload):
            return BrowserFollowHrefInternalResponse(
                source_url=payload.source_url,
                source_final_url=payload.source_url,
                requested_target_url=payload.target_url,
                matched_link_text="safe link",
                follow_hop_count=1,
                navigation_history=[payload.source_url, payload.target_url],
                normalized_url=payload.target_url,
                final_url=payload.target_url,
                http_status=200,
                page_title="Followed Target",
                meta_description="Followed target description",
                rendered_text="Followed browser text",
                rendered_text_sha256="follow-text-sha256",
                text_bytes=20,
                text_truncated=False,
                screenshot_png_base64=TINY_PNG_BASE64,
                screenshot_sha256="follow-screenshot-sha256",
                screenshot_bytes=67,
                redirect_chain=[],
                observed_hosts=["allowed.test"],
                resolved_ips=["93.184.216.34"],
                channel_records=[],
            )

        return "browser_follow_href", fake_browser_follow_href

    raise ValueError(f"unsupported spoof test case: {case}")


@pytest.mark.fast
@pytest.mark.parametrize(
    ("case", "path", "payload", "event_type"),
    [
        (
            "llm_call",
            "/llm/chat/completions",
            {
                "model": "stage2-deterministic",
                "messages": [{"role": "user", "content": "spoof this actor"}],
            },
            "llm_call",
        ),
        (
            "web_fetch",
            "/web/fetch",
            {"url": "http://allowed.test/allowed"},
            "web_fetch",
        ),
        (
            "browser_render",
            "/web/browser/render",
            {"url": "http://allowed.test/browser/rendered"},
            "browser_render",
        ),
        (
            "browser_follow_href",
            "/web/browser/follow-href",
            {
                "source_url": "http://allowed.test/browser/follow-source",
                "target_url": "http://allowed.test/browser/follow-target",
            },
            "browser_follow_href",
        ),
    ],
    ids=["llm", "fetch", "browser-render", "browser-follow"],
)
def test_agent_facing_routes_ignore_spoofed_actor_headers(
    monkeypatch,
    tmp_path,
    case,
    path,
    payload,
    event_type,
):
    monkeypatch.setenv("RSI_TRUSTED_STATE_DIR", str(tmp_path))
    method_name, fake_method = _patched_route_result(case)
    monkeypatch.setattr(TrustedBridgeClients, method_name, fake_method)

    with TestClient(app) as client:
        response = client.post(
            path,
            headers={"x-rsi-actor": "operator"},
            json=payload,
        )

    assert response.status_code == 200
    request_id = response.headers["x-request-id"]
    trace_id = response.headers["x-trace-id"]
    events = load_events(tmp_path / "logs" / "bridge_events.jsonl")
    matched = [
        event
        for event in events
        if event["event_type"] == event_type
        and event["request_id"] == request_id
        and event["trace_id"] == trace_id
    ]
    assert matched
    assert matched[-1]["actor"] == "agent"


def test_debug_probe_routes_are_disabled_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv("RSI_TRUSTED_STATE_DIR", str(tmp_path))
    with TestClient(app) as client:
        response = client.post("/debug/probes/public-egress")

    assert response.status_code == 404
