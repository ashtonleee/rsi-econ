"""Docker-backed integration tests for Stage 8 consequential actions.

Tests the full http_post proposal lifecycle through bridge → egress → webfixture.
"""
import json
import os
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PROJECT = "rsi_stage8_action_test"
LOG_PATH = ROOT / "runtime" / "trusted_state" / "logs" / "bridge_events.jsonl"
STATE_PATH = ROOT / "runtime" / "trusted_state" / "state" / "operational_state.json"

TEST_AGENT_TOKEN = "rsi-agent-token-dev-sentinel"
TEST_OPERATOR_TOKEN = "rsi-operator-token-dev-sentinel"


def agent_auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_AGENT_TOKEN}"}


def operator_auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_OPERATOR_TOKEN}"}


def docker_env() -> dict[str, str]:
    env = os.environ.copy()
    env["COMPOSE_PROJECT_NAME"] = COMPOSE_PROJECT
    env["RSI_WEB_ALLOWLIST_HOSTS"] = "allowed.test,allowed-two.test"
    env["RSI_FETCH_ALLOW_PRIVATE_TEST_HOSTS"] = "allowed.test,allowed-two.test"
    env["RSI_ACTION_ALLOWLIST_HOSTS"] = "allowed.test"
    return env


def run_command(
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=check,
    )


def compose_command(
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return run_command(["docker", "compose", *args], env=env, check=check)


def compose_exec(
    service: str,
    command: list[str],
    *,
    env: dict[str, str],
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return compose_command(["exec", "-T", service, *command], env=env, check=check)


def compose_http_response(
    service: str,
    method: str,
    url: str,
    *,
    env: dict[str, str],
    payload: dict | None = None,
    headers: dict | None = None,
) -> dict:
    code = (
        "import httpx, json\n"
        f"method = {method!r}\n"
        f"url = {url!r}\n"
        f"payload = {json.dumps(payload)!r}\n"
        f"headers = {json.dumps(headers or {})!r}\n"
        "with httpx.Client(timeout=15.0) as client:\n"
        "    response = client.request(method, url, json=json.loads(payload) if payload else None, headers=json.loads(headers))\n"
        "body = None\n"
        "try:\n"
        "    body = response.json()\n"
        "except Exception:\n"
        "    body = {'raw': response.text}\n"
        "print(json.dumps({'status_code': response.status_code, 'headers': dict(response.headers), 'json': body}))\n"
    )
    result = compose_exec(service, ["python", "-c", code], env=env)
    return json.loads(result.stdout)


def load_events() -> list[dict]:
    if not LOG_PATH.exists():
        return []
    return [
        json.loads(line)
        for line in LOG_PATH.read_text(encoding="ascii").splitlines()
        if line.strip()
    ]


@pytest.fixture(scope="module")
def compose_stack():
    env = docker_env()
    docker_ready = run_command(["docker", "info"], env=env, check=False)
    if docker_ready.returncode != 0:
        pytest.fail("Docker daemon is required for Stage 8 action tests")

    compose_command(["down", "--remove-orphans", "--volumes"], env=env, check=False)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if LOG_PATH.exists():
        LOG_PATH.unlink()
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if STATE_PATH.exists():
        STATE_PATH.unlink()

    compose_command(["up", "--build", "-d", "--wait"], env=env)
    yield env
    compose_command(["down", "--remove-orphans", "--volumes"], env=env, check=False)


def test_full_http_post_lifecycle(compose_stack):
    """Agent proposes POST → operator approves → operator executes → verify response + canonical log."""
    # Create proposal
    create = compose_http_response(
        "agent",
        "POST",
        "http://bridge:8000/proposals",
        env=compose_stack,
        payload={
            "action_type": "http_post",
            "action_payload": {
                "url": "http://allowed.test/action/echo-post",
                "body": {"stage": 8, "test": "integration"},
                "content_type": "application/json",
            },
        },
        headers=agent_auth_headers(),
    )
    assert create["status_code"] == 200
    pid = create["json"]["proposal_id"]
    assert create["json"]["status"] == "pending"
    assert create["json"]["action_type"] == "http_post"

    # Approve
    decide = compose_http_response(
        "bridge",
        "POST",
        f"http://127.0.0.1:8000/proposals/{pid}/decide",
        env=compose_stack,
        payload={"decision": "approve", "reason": "stage 8 integration test"},
        headers=operator_auth_headers(),
    )
    assert decide["status_code"] == 200
    assert decide["json"]["status"] == "approved"

    # Execute
    execute = compose_http_response(
        "bridge",
        "POST",
        f"http://127.0.0.1:8000/proposals/{pid}/execute",
        env=compose_stack,
        headers=operator_auth_headers(),
    )
    assert execute["status_code"] == 200
    body = execute["json"]
    assert body["status"] == "executed"

    result = body["execution_result"]
    assert result["http_status"] == 200
    assert result["url"] == "http://allowed.test/action/echo-post"
    assert "response_body_sha256" in result
    assert result["response_byte_count"] > 0
    # The webfixture echoes the body back
    assert "stage" in result["response_body_preview"]

    # Verify canonical log
    events = load_events()
    proposal_events = [e for e in events if e["event_type"].startswith("proposal_")]
    executed_events = [e for e in proposal_events if e["event_type"] == "proposal_executed"]
    assert len(executed_events) >= 1
    last_executed = executed_events[-1]
    assert last_executed["summary"]["action_type"] == "http_post"
    assert last_executed["summary"]["result"]["http_status"] == 200
    assert last_executed["summary"]["result"]["url"] == "http://allowed.test/action/echo-post"
    # Canonical log must NOT contain response_body_preview
    assert "response_body_preview" not in last_executed["summary"]["result"]
    assert "response_body_sha256" in last_executed["summary"]["result"]


def test_rejected_http_post_cannot_execute(compose_stack):
    """Rejected http_post proposals cannot be executed."""
    create = compose_http_response(
        "agent",
        "POST",
        "http://bridge:8000/proposals",
        env=compose_stack,
        payload={
            "action_type": "http_post",
            "action_payload": {
                "url": "http://allowed.test/action/echo-post",
                "body": {"rejected": True},
            },
        },
        headers=agent_auth_headers(),
    )
    pid = create["json"]["proposal_id"]

    compose_http_response(
        "bridge",
        "POST",
        f"http://127.0.0.1:8000/proposals/{pid}/decide",
        env=compose_stack,
        payload={"decision": "reject", "reason": "denied"},
        headers=operator_auth_headers(),
    )

    execute = compose_http_response(
        "bridge",
        "POST",
        f"http://127.0.0.1:8000/proposals/{pid}/execute",
        env=compose_stack,
        headers=operator_auth_headers(),
    )
    assert execute["status_code"] == 409


def test_action_allowlist_blocks_unlisted_host(compose_stack):
    """POST to a host not in the action allowlist returns an error result, not an HTTP error."""
    # allowed-two.test is in the web allowlist but NOT in the action allowlist
    create = compose_http_response(
        "agent",
        "POST",
        "http://bridge:8000/proposals",
        env=compose_stack,
        payload={
            "action_type": "http_post",
            "action_payload": {
                "url": "http://allowed-two.test/action/echo-post",
                "body": {},
            },
        },
        headers=agent_auth_headers(),
    )
    pid = create["json"]["proposal_id"]

    compose_http_response(
        "bridge",
        "POST",
        f"http://127.0.0.1:8000/proposals/{pid}/decide",
        env=compose_stack,
        payload={"decision": "approve", "reason": "test allowlist"},
        headers=operator_auth_headers(),
    )

    execute = compose_http_response(
        "bridge",
        "POST",
        f"http://127.0.0.1:8000/proposals/{pid}/execute",
        env=compose_stack,
        headers=operator_auth_headers(),
    )
    assert execute["status_code"] == 200
    result = execute["json"]["execution_result"]
    assert result["error"] == "host_not_in_action_allowlist"
    assert result["host"] == "allowed-two.test"


def test_echo_still_works_after_stage8(compose_stack):
    """Echo action is preserved and works after Stage 8 executor changes."""
    create = compose_http_response(
        "agent",
        "POST",
        "http://bridge:8000/proposals",
        env=compose_stack,
        payload={"action_type": "echo", "action_payload": {"backward": "compat"}},
        headers=agent_auth_headers(),
    )
    pid = create["json"]["proposal_id"]

    compose_http_response(
        "bridge",
        "POST",
        f"http://127.0.0.1:8000/proposals/{pid}/decide",
        env=compose_stack,
        payload={"decision": "approve", "reason": "echo test"},
        headers=operator_auth_headers(),
    )

    execute = compose_http_response(
        "bridge",
        "POST",
        f"http://127.0.0.1:8000/proposals/{pid}/execute",
        env=compose_stack,
        headers=operator_auth_headers(),
    )
    assert execute["status_code"] == 200
    assert execute["json"]["execution_result"] == {"echoed": {"backward": "compat"}}


def test_http_post_visible_in_status(compose_stack):
    """After http_post execution, /status shows proposal as executed."""
    status = compose_http_response(
        "bridge",
        "GET",
        "http://127.0.0.1:8000/status",
        env=compose_stack,
        headers=operator_auth_headers(),
    )
    assert status["status_code"] == 200
    body = status["json"]
    assert body["proposals"]["executed"] >= 1
    assert body["surfaces"]["consequential_actions"] == "active_consequential_actions_stage8"


def test_consequential_action_counters_in_status(compose_stack):
    """Proposal counters reflect the executed actions."""
    status = compose_http_response(
        "bridge",
        "GET",
        "http://127.0.0.1:8000/status",
        env=compose_stack,
        headers=operator_auth_headers(),
    )
    body = status["json"]
    assert body["counters"]["proposals_created"] >= 1
    assert body["counters"]["proposals_executed"] >= 1
