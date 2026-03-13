from fastapi.testclient import TestClient

from trusted.bridge.app import app


def test_bridge_health_contract(monkeypatch, tmp_path):
    monkeypatch.setenv("RSI_TRUSTED_STATE_DIR", str(tmp_path))
    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "bridge"
    assert body["status"] == "ok"
    assert body["stage"] == "stage5_read_only_web"
    assert body["details"]["trusted_state_ready"] is True
    assert "litellm_reachable" in body["details"]
    assert "fetcher_reachable" in body["details"]
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
    assert body["surfaces"]["approvals"] == "stubbed_for_stage_7"
    assert body["log_path"].endswith("bridge_events.jsonl")
    assert body["operational_state_path"].endswith("operational_state.json")
    assert body["connections"]["litellm"]["url"].startswith("http://")
    assert body["connections"]["fetcher"]["url"].startswith("http://")
    assert body["budget"]["unit"] == "mock_tokens"
    assert body["budget"]["remaining"] == body["budget"]["total"]
    assert body["recovery"]["baseline_id"]
    assert body["recovery"]["checkpoint_dir"].endswith("/checkpoints")
    assert body["recovery"]["current_workspace_status"] == "seed_baseline"
    assert body["web"]["allowlist_hosts"] == ["example.com"]
    assert body["web"]["fetcher"]["url"].startswith("http://")
    assert body["web"]["caps"]["max_redirects"] >= 1
    assert isinstance(body["recent_requests"], list)


def test_debug_probe_routes_are_disabled_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv("RSI_TRUSTED_STATE_DIR", str(tmp_path))
    with TestClient(app) as client:
        response = client.post("/debug/probes/public-egress")

    assert response.status_code == 404
