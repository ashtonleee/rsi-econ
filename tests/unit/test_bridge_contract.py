from fastapi.testclient import TestClient

from trusted.bridge.app import app


def test_bridge_health_contract():
    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "bridge"
    assert body["status"] == "ok"
    assert body["stage"] == "stage1_hard_boundary"
    assert body["details"]["trusted_state_ready"] is True
    assert "litellm_reachable" in body["details"]
    assert body["details"]["log_path"].endswith("bridge_events.jsonl")


def test_bridge_status_exposes_litellm_connectivity_and_stubs_later_surfaces():
    with TestClient(app) as client:
        response = client.get("/status")

    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "bridge"
    assert body["surfaces"]["litellm"] == "mediated_via_trusted_service"
    assert body["surfaces"]["canonical_logging"] == "stubbed_for_stage_2"
    assert body["surfaces"]["approvals"] == "stubbed_for_stage_7"
    assert body["log_path"].endswith("bridge_events.jsonl")
    assert body["connections"]["litellm"]["url"].startswith("http://")
