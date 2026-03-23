"""Tests for the bridge provider hot-add endpoints."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
WALLET_API_PATH = ROOT / "trusted" / "bridge" / "wallet_api.py"


def load_wallet_api(tmp_path: Path):
    os.environ["PROPOSALS_DIR"] = str(tmp_path / "proposals")
    os.environ["LLM_USAGE_LOG_PATH"] = str(tmp_path / "llm_usage.jsonl")
    os.environ["PROXY_ALLOWLIST_PATH"] = str(tmp_path / "proxy_allowlist.txt")
    os.environ["LITELLM_URL"] = "http://litellm-test:4000"
    os.environ["RSI_BUDGET_USD"] = "5.00"
    os.environ["GIT_REPO_DIR"] = str(tmp_path / "git-repo")
    os.environ["GIT_WORKSPACE_DIR"] = str(tmp_path / "workspace")
    os.environ["SEED_DIR"] = str(tmp_path / "seed")
    os.environ["OPERATOR_MESSAGES_DIR"] = str(tmp_path / "operator_messages")
    os.environ["PROVIDER_PROPOSALS_DIR"] = str(tmp_path / "provider_proposals")
    os.environ["NOTIFICATION_CONFIG_PATH"] = str(tmp_path / "notification_config.json")
    os.environ["EVENTS_DIR"] = str(tmp_path / "events")
    os.environ["EVENT_POLL_INTERVAL"] = "9999"

    (tmp_path / "notification_config.json").write_text(json.dumps({
        "webhook_url": "", "events": {}
    }))
    (tmp_path / "events").mkdir(exist_ok=True)
    (tmp_path / "provider_proposals").mkdir(exist_ok=True)

    module_name = f"test_wallet_api_providers_{tmp_path.name}"
    spec = importlib.util.spec_from_file_location(module_name, WALLET_API_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _make_app(mod, tmp_path):
    return mod.create_app(
        proposals_dir=tmp_path / "proposals",
        usage_log_path=tmp_path / "llm_usage.jsonl",
        allowlist_path=tmp_path / "proxy_allowlist.txt",
        litellm_base_url="http://litellm-test:4000",
        budget_usd=5.0,
    )


def test_propose_provider(tmp_path: Path) -> None:
    mod = load_wallet_api(tmp_path)
    app = _make_app(mod, tmp_path)
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        resp = client.post("/providers/propose", json={
            "name": "groq-free",
            "provider": "groq",
            "model_id": "groq/llama-3.3-70b-instruct",
            "signup_url": "https://console.groq.com",
            "free_tier": "1000 req/day",
            "needs_api_key": True,
            "notes": "Phone verification required",
        })

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending_operator"
    assert "proposal_id" in body

    # Verify file was created
    proposals_dir = tmp_path / "provider_proposals"
    files = list(proposals_dir.glob("*.json"))
    assert len(files) == 1
    record = json.loads(files[0].read_text())
    assert record["name"] == "groq-free"
    assert record["model_id"] == "groq/llama-3.3-70b-instruct"


def test_propose_provider_requires_name(tmp_path: Path) -> None:
    mod = load_wallet_api(tmp_path)
    app = _make_app(mod, tmp_path)
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        resp = client.post("/providers/propose", json={"model_id": "groq/llama"})

    assert resp.status_code == 400


def test_list_providers(tmp_path: Path) -> None:
    mod = load_wallet_api(tmp_path)
    app = _make_app(mod, tmp_path)
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        # Propose one
        client.post("/providers/propose", json={
            "name": "groq-free",
            "provider": "groq",
            "model_id": "groq/llama-3.3-70b-instruct",
        })
        resp = client.get("/providers")

    assert resp.status_code == 200
    body = resp.json()
    assert "active" in body
    assert "proposed" in body
    assert len(body["proposed"]) == 1
    assert body["proposed"][0]["name"] == "groq-free"
    assert body["proposed"][0]["status"] == "pending_operator"


def test_propose_duplicate(tmp_path: Path) -> None:
    mod = load_wallet_api(tmp_path)
    app = _make_app(mod, tmp_path)
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        resp1 = client.post("/providers/propose", json={
            "name": "groq-free",
            "model_id": "groq/llama-3.3-70b-instruct",
        })
        resp2 = client.post("/providers/propose", json={
            "name": "groq-free",
            "model_id": "groq/llama-3.3-70b-instruct",
        })

    id1 = resp1.json()["proposal_id"]
    id2 = resp2.json()["proposal_id"]
    assert id1 == id2  # Same proposal returned
    assert "duplicate" in resp2.json().get("note", "")

    # Only one file
    files = list((tmp_path / "provider_proposals").glob("*.json"))
    assert len(files) == 1


def test_activate_provider(tmp_path: Path) -> None:
    mod = load_wallet_api(tmp_path)
    app = _make_app(mod, tmp_path)
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        resp = client.post("/providers/propose", json={
            "name": "groq-free",
            "model_id": "groq/llama-3.3-70b-instruct",
        })
        proposal_id = resp.json()["proposal_id"]

        resp2 = client.post(f"/providers/proposals/{proposal_id}/activate")

    assert resp2.status_code == 200
    assert resp2.json()["status"] == "active"
    assert "activated_at" in resp2.json()


def test_get_provider_proposal(tmp_path: Path) -> None:
    mod = load_wallet_api(tmp_path)
    app = _make_app(mod, tmp_path)
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        resp = client.post("/providers/propose", json={
            "name": "cerebras",
            "model_id": "cerebras/llama-3.3-70b",
            "free_tier": "Free inference",
        })
        pid = resp.json()["proposal_id"]

        resp2 = client.get(f"/providers/proposals/{pid}")

    assert resp2.status_code == 200
    assert resp2.json()["name"] == "cerebras"
    assert resp2.json()["free_tier"] == "Free inference"


def test_get_provider_proposal_404(tmp_path: Path) -> None:
    mod = load_wallet_api(tmp_path)
    app = _make_app(mod, tmp_path)
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        resp = client.get("/providers/proposals/nonexistent-id")

    assert resp.status_code == 404
