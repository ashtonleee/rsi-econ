import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from trusted.bridge.app import app


TEST_AGENT_TOKEN = "test-agent-token"
TEST_OPERATOR_TOKEN = "test-operator-token"


def agent_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_AGENT_TOKEN}"}


def operator_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_OPERATOR_TOKEN}"}


def setup_auth_env(monkeypatch):
    monkeypatch.setenv("RSI_AGENT_TOKEN", TEST_AGENT_TOKEN)
    monkeypatch.setenv("RSI_OPERATOR_TOKEN", TEST_OPERATOR_TOKEN)


def load_events(log_path: Path) -> list[dict]:
    if not log_path.exists():
        return []
    return [
        json.loads(line)
        for line in log_path.read_text(encoding="ascii").splitlines()
        if line.strip()
    ]


def test_agent_creates_proposal(monkeypatch, tmp_path):
    monkeypatch.setenv("RSI_TRUSTED_STATE_DIR", str(tmp_path))
    setup_auth_env(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            "/proposals",
            headers=agent_headers(),
            json={"action_type": "echo", "action_payload": {"msg": "hi"}},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "pending"
    assert body["action_type"] == "echo"
    assert body["action_payload"] == {"msg": "hi"}
    assert body["created_by"] == "agent"
    assert body["proposal_id"]


def test_operator_creates_proposal(monkeypatch, tmp_path):
    monkeypatch.setenv("RSI_TRUSTED_STATE_DIR", str(tmp_path))
    setup_auth_env(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            "/proposals",
            headers=operator_headers(),
            json={"action_type": "echo", "action_payload": {}},
        )
    assert response.status_code == 200
    assert response.json()["created_by"] == "operator"


def test_unauthenticated_cannot_create_proposal(monkeypatch, tmp_path):
    monkeypatch.setenv("RSI_TRUSTED_STATE_DIR", str(tmp_path))
    setup_auth_env(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            "/proposals",
            json={"action_type": "echo", "action_payload": {}},
        )
    assert response.status_code == 401


def test_list_proposals(monkeypatch, tmp_path):
    monkeypatch.setenv("RSI_TRUSTED_STATE_DIR", str(tmp_path))
    setup_auth_env(monkeypatch)
    with TestClient(app) as client:
        client.post(
            "/proposals",
            headers=agent_headers(),
            json={"action_type": "echo", "action_payload": {"a": 1}},
        )
        client.post(
            "/proposals",
            headers=agent_headers(),
            json={"action_type": "echo", "action_payload": {"b": 2}},
        )
        response = client.get("/proposals", headers=agent_headers())
    assert response.status_code == 200
    assert len(response.json()["proposals"]) == 2


def test_list_proposals_with_status_filter(monkeypatch, tmp_path):
    monkeypatch.setenv("RSI_TRUSTED_STATE_DIR", str(tmp_path))
    setup_auth_env(monkeypatch)
    with TestClient(app) as client:
        r1 = client.post(
            "/proposals",
            headers=agent_headers(),
            json={"action_type": "echo", "action_payload": {}},
        )
        client.post(
            "/proposals",
            headers=agent_headers(),
            json={"action_type": "echo", "action_payload": {}},
        )
        pid = r1.json()["proposal_id"]
        client.post(
            f"/proposals/{pid}/decide",
            headers=operator_headers(),
            json={"decision": "approve", "reason": "ok"},
        )
        pending = client.get("/proposals?status=pending", headers=agent_headers())
        approved = client.get("/proposals?status=approved", headers=agent_headers())
    assert len(pending.json()["proposals"]) == 1
    assert len(approved.json()["proposals"]) == 1


def test_get_proposal_by_id(monkeypatch, tmp_path):
    monkeypatch.setenv("RSI_TRUSTED_STATE_DIR", str(tmp_path))
    setup_auth_env(monkeypatch)
    with TestClient(app) as client:
        create_resp = client.post(
            "/proposals",
            headers=agent_headers(),
            json={"action_type": "echo", "action_payload": {"x": 1}},
        )
        pid = create_resp.json()["proposal_id"]
        response = client.get(f"/proposals/{pid}", headers=agent_headers())
    assert response.status_code == 200
    assert response.json()["proposal_id"] == pid


def test_get_nonexistent_proposal_returns_404(monkeypatch, tmp_path):
    monkeypatch.setenv("RSI_TRUSTED_STATE_DIR", str(tmp_path))
    setup_auth_env(monkeypatch)
    with TestClient(app) as client:
        response = client.get("/proposals/ghost-id", headers=agent_headers())
    assert response.status_code == 404


def test_agent_cannot_decide_proposal(monkeypatch, tmp_path):
    monkeypatch.setenv("RSI_TRUSTED_STATE_DIR", str(tmp_path))
    setup_auth_env(monkeypatch)
    with TestClient(app) as client:
        create_resp = client.post(
            "/proposals",
            headers=agent_headers(),
            json={"action_type": "echo", "action_payload": {}},
        )
        pid = create_resp.json()["proposal_id"]
        response = client.post(
            f"/proposals/{pid}/decide",
            headers=agent_headers(),
            json={"decision": "approve", "reason": "self-approve"},
        )
    assert response.status_code == 403
    assert "operator" in response.json()["detail"]


def test_agent_cannot_execute_proposal(monkeypatch, tmp_path):
    monkeypatch.setenv("RSI_TRUSTED_STATE_DIR", str(tmp_path))
    setup_auth_env(monkeypatch)
    with TestClient(app) as client:
        create_resp = client.post(
            "/proposals",
            headers=agent_headers(),
            json={"action_type": "echo", "action_payload": {}},
        )
        pid = create_resp.json()["proposal_id"]
        # Even if somehow approved, agent can't execute
        client.post(
            f"/proposals/{pid}/decide",
            headers=operator_headers(),
            json={"decision": "approve", "reason": "ok"},
        )
        response = client.post(
            f"/proposals/{pid}/execute",
            headers=agent_headers(),
        )
    assert response.status_code == 403


def test_operator_approves_and_executes_echo(monkeypatch, tmp_path):
    monkeypatch.setenv("RSI_TRUSTED_STATE_DIR", str(tmp_path))
    setup_auth_env(monkeypatch)
    with TestClient(app) as client:
        create_resp = client.post(
            "/proposals",
            headers=agent_headers(),
            json={"action_type": "echo", "action_payload": {"msg": "hello"}},
        )
        pid = create_resp.json()["proposal_id"]

        decide_resp = client.post(
            f"/proposals/{pid}/decide",
            headers=operator_headers(),
            json={"decision": "approve", "reason": "looks good"},
        )
        assert decide_resp.status_code == 200
        assert decide_resp.json()["status"] == "approved"

        execute_resp = client.post(
            f"/proposals/{pid}/execute",
            headers=operator_headers(),
        )
    assert execute_resp.status_code == 200
    body = execute_resp.json()
    assert body["status"] == "executed"
    assert body["execution_result"] == {"echoed": {"msg": "hello"}}
    assert body["executed_by"] == "operator"


def test_cannot_execute_pending_proposal(monkeypatch, tmp_path):
    monkeypatch.setenv("RSI_TRUSTED_STATE_DIR", str(tmp_path))
    setup_auth_env(monkeypatch)
    with TestClient(app) as client:
        create_resp = client.post(
            "/proposals",
            headers=agent_headers(),
            json={"action_type": "echo", "action_payload": {}},
        )
        pid = create_resp.json()["proposal_id"]
        response = client.post(
            f"/proposals/{pid}/execute",
            headers=operator_headers(),
        )
    assert response.status_code == 409


def test_cannot_execute_rejected_proposal(monkeypatch, tmp_path):
    monkeypatch.setenv("RSI_TRUSTED_STATE_DIR", str(tmp_path))
    setup_auth_env(monkeypatch)
    with TestClient(app) as client:
        create_resp = client.post(
            "/proposals",
            headers=agent_headers(),
            json={"action_type": "echo", "action_payload": {}},
        )
        pid = create_resp.json()["proposal_id"]
        client.post(
            f"/proposals/{pid}/decide",
            headers=operator_headers(),
            json={"decision": "reject", "reason": "no"},
        )
        response = client.post(
            f"/proposals/{pid}/execute",
            headers=operator_headers(),
        )
    assert response.status_code == 409


def test_cannot_decide_already_decided(monkeypatch, tmp_path):
    monkeypatch.setenv("RSI_TRUSTED_STATE_DIR", str(tmp_path))
    setup_auth_env(monkeypatch)
    with TestClient(app) as client:
        create_resp = client.post(
            "/proposals",
            headers=agent_headers(),
            json={"action_type": "echo", "action_payload": {}},
        )
        pid = create_resp.json()["proposal_id"]
        client.post(
            f"/proposals/{pid}/decide",
            headers=operator_headers(),
            json={"decision": "approve", "reason": "ok"},
        )
        response = client.post(
            f"/proposals/{pid}/decide",
            headers=operator_headers(),
            json={"decision": "reject", "reason": "changed mind"},
        )
    assert response.status_code == 409


def test_proposal_lifecycle_generates_canonical_events(monkeypatch, tmp_path):
    monkeypatch.setenv("RSI_TRUSTED_STATE_DIR", str(tmp_path))
    setup_auth_env(monkeypatch)
    with TestClient(app) as client:
        create_resp = client.post(
            "/proposals",
            headers=agent_headers(),
            json={"action_type": "echo", "action_payload": {"test": True}},
        )
        pid = create_resp.json()["proposal_id"]
        client.post(
            f"/proposals/{pid}/decide",
            headers=operator_headers(),
            json={"decision": "approve", "reason": "lgtm"},
        )
        client.post(
            f"/proposals/{pid}/execute",
            headers=operator_headers(),
        )

    events = load_events(tmp_path / "logs" / "bridge_events.jsonl")
    proposal_events = [
        e for e in events if e["event_type"].startswith("proposal_")
    ]
    assert len(proposal_events) == 4
    assert proposal_events[0]["event_type"] == "proposal_created"
    assert proposal_events[0]["actor"] == "agent"
    assert proposal_events[0]["summary"]["proposal_id"] == pid
    assert proposal_events[1]["event_type"] == "proposal_decided"
    assert proposal_events[1]["actor"] == "operator"
    assert proposal_events[1]["summary"]["decision"] == "approve"
    assert proposal_events[2]["event_type"] == "proposal_claimed"
    assert proposal_events[2]["actor"] == "operator"
    assert proposal_events[2]["summary"]["proposal_id"] == pid
    assert proposal_events[3]["event_type"] == "proposal_executed"
    assert proposal_events[3]["actor"] == "operator"
    assert proposal_events[3]["summary"]["result"] == {"echoed": {"test": True}}


def test_proposals_appear_in_status_report(monkeypatch, tmp_path):
    monkeypatch.setenv("RSI_TRUSTED_STATE_DIR", str(tmp_path))
    setup_auth_env(monkeypatch)
    with TestClient(app) as client:
        client.post(
            "/proposals",
            headers=agent_headers(),
            json={"action_type": "echo", "action_payload": {}},
        )
        status_resp = client.get("/status", headers=agent_headers())
    assert status_resp.status_code == 200
    body = status_resp.json()
    assert body["proposals"]["total"] == 1
    assert body["proposals"]["pending"] == 1


def test_proposal_counters_in_status_counters(monkeypatch, tmp_path):
    monkeypatch.setenv("RSI_TRUSTED_STATE_DIR", str(tmp_path))
    setup_auth_env(monkeypatch)
    with TestClient(app) as client:
        create_resp = client.post(
            "/proposals",
            headers=agent_headers(),
            json={"action_type": "echo", "action_payload": {}},
        )
        pid = create_resp.json()["proposal_id"]
        client.post(
            f"/proposals/{pid}/decide",
            headers=operator_headers(),
            json={"decision": "approve", "reason": "ok"},
        )
        client.post(
            f"/proposals/{pid}/execute",
            headers=operator_headers(),
        )
        status_resp = client.get("/status", headers=operator_headers())
    body = status_resp.json()
    assert body["counters"]["proposals_created"] == 1
    assert body["counters"]["proposals_decided"] == 1
    assert body["counters"]["proposals_executed"] == 1
