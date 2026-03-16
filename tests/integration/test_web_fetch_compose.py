import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PROJECT = "rsi_stage5_web_test"
LOG_PATH = ROOT / "runtime" / "trusted_state" / "logs" / "bridge_events.jsonl"
STATE_PATH = ROOT / "runtime" / "trusted_state" / "state" / "operational_state.json"
WORKSPACE_ROOT = ROOT / "untrusted" / "agent_workspace"
REPORT_PATH = WORKSPACE_ROOT / "reports" / "stage5_fixture_fetch_report.txt"


def docker_env() -> dict[str, str]:
    env = os.environ.copy()
    env["COMPOSE_PROJECT_NAME"] = COMPOSE_PROJECT
    env["RSI_LLM_BUDGET_TOKEN_CAP"] = "120"
    env["RSI_WEB_ALLOWLIST_HOSTS"] = "allowed.test"
    env["RSI_FETCH_ALLOW_PRIVATE_TEST_HOSTS"] = "allowed.test"
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
    env: dict[str, str],
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
) -> dict:
    code = (
        "import httpx, json\n"
        f"method = {method!r}\n"
        f"url = {url!r}\n"
        f"payload = {json.dumps(payload)!r}\n"
        "with httpx.Client(timeout=10.0) as client:\n"
        "    response = client.request(method, url, json=json.loads(payload) if payload else None)\n"
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


def load_state() -> dict:
    assert STATE_PATH.exists(), STATE_PATH
    return json.loads(STATE_PATH.read_text(encoding="ascii"))


def expect_failure_via_agent(target_url: str, env: dict[str, str]):
    code = (
        "import sys, urllib.request\n"
        f"url = {target_url!r}\n"
        "try:\n"
        "    urllib.request.urlopen(url, timeout=2).read()\n"
        "except Exception:\n"
        "    sys.exit(0)\n"
        "sys.exit(1)\n"
    )
    result = compose_exec("agent", ["python", "-c", code], env=env, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture(scope="module")
def compose_stack():
    env = docker_env()
    docker_ready = run_command(["docker", "info"], env=env, check=False)
    if docker_ready.returncode != 0:
        pytest.fail("Docker daemon is required for Stage 5 web fetch tests")

    compose_command(["down", "--remove-orphans", "--volumes"], env=env, check=False)
    if LOG_PATH.exists():
        LOG_PATH.unlink()
    if STATE_PATH.exists():
        STATE_PATH.unlink()
    if REPORT_PATH.parent.exists():
        shutil.rmtree(REPORT_PATH.parent)
    run_command(["./scripts/recovery.sh", "reset-workspace-to-seed-baseline"], env=env)

    compose_command(["up", "--build", "-d", "--wait"], env=env)
    yield env
    compose_command(["down", "--remove-orphans", "--volumes"], env=env, check=False)
    run_command(["./scripts/recovery.sh", "reset-workspace-to-seed-baseline"], env=env)


def test_allowed_fetch_succeeds_only_through_trusted_path_and_is_logged(compose_stack):
    expect_failure_via_agent("http://1.1.1.1", compose_stack)
    expect_failure_via_agent("https://api.openai.com/v1/models", compose_stack)
    expect_failure_via_agent("http://litellm:4000/healthz", compose_stack)
    expect_failure_via_agent("http://fetcher:8082/healthz", compose_stack)
    expect_failure_via_agent("http://egress:8084/healthz", compose_stack)

    fetched = compose_http_response(
        "agent",
        "POST",
        "http://bridge:8000/web/fetch",
        env=compose_stack,
        payload={"url": "http://allowed.test/allowed"},
    )
    assert fetched["status_code"] == 200
    body = fetched["json"]
    assert body["normalized_url"] == "http://allowed.test/allowed"
    assert body["content_type"] == "text/plain"
    assert "Stage 5 fixture page" in body["text"]
    assert body["request_id"]
    assert body["trace_id"]

    events = load_events()
    matched = [
        event
        for event in events
        if event["event_type"] == "web_fetch"
        and event["request_id"] == body["request_id"]
        and event["trace_id"] == body["trace_id"]
    ]
    assert matched
    event = matched[0]
    assert event["summary"]["normalized_url"] == "http://allowed.test/allowed"
    assert event["summary"]["resolved_ips"]
    assert event["summary"]["content_sha256"]
    assert "Stage 5 fixture page" not in json.dumps(event)

    state = load_state()
    assert state["web"]["counters"]["web_fetch_success"] >= 1
    assert state["web"]["recent_fetches"][0]["request_id"] == body["request_id"]

    status = compose_http_response(
        "agent",
        "GET",
        "http://bridge:8000/status",
        env=compose_stack,
    )["json"]
    assert status["web"]["fetcher"]["reachable"] is True
    assert status["connections"]["egress"]["reachable"] is True
    assert status["web"]["allowlist_hosts"] == ["allowed.test"]
    assert status["web"]["counters"]["web_fetch_success"] >= 1
    assert status["surfaces"]["read_only_web"] == "trusted_fetcher_stage5_read_only_get"


def test_denial_and_truncation_cases_are_enforced(compose_stack):
    denied_inputs = [
        "ftp://allowed.test/allowed",
        "http://user@allowed.test/allowed",
        "http://allowed.test/allowed#frag",
        "http://allowed.test:81/allowed",
        "http://localhost/secret",
    ]
    for url in denied_inputs:
        response = compose_http_response(
            "agent",
            "POST",
            "http://bridge:8000/web/fetch",
            env=compose_stack,
            payload={"url": url},
        )
        assert response["status_code"] in {400, 403}

    redirect_denied = compose_http_response(
        "agent",
        "POST",
        "http://bridge:8000/web/fetch",
        env=compose_stack,
        payload={"url": "http://allowed.test/redirect-blocked"},
    )
    assert redirect_denied["status_code"] == 403

    binary_denied = compose_http_response(
        "agent",
        "POST",
        "http://bridge:8000/web/fetch",
        env=compose_stack,
        payload={"url": "http://allowed.test/binary"},
    )
    assert binary_denied["status_code"] == 415

    large = compose_http_response(
        "agent",
        "POST",
        "http://bridge:8000/web/fetch",
        env=compose_stack,
        payload={"url": "http://allowed.test/large"},
    )
    assert large["status_code"] == 200
    assert large["json"]["truncated"] is True

    events = load_events()
    assert any(event["event_type"] == "web_fetch_denied" for event in events)


def test_seed_runner_fetch_demo_writes_report_and_recovery_can_reset_it(compose_stack):
    result = compose_exec(
        "agent",
        [
            "python",
            "-m",
            "untrusted.agent.seed_runner",
            "--task",
            "fetch one allowed page and write a report",
            "--planner",
            "scripted",
            "--script",
            ".seed_plans/stage5_fixture_fetch.json",
            "--max-steps",
            "8",
        ],
        env=compose_stack,
    )
    payload = json.loads(result.stdout)
    assert payload["success"] is True
    assert REPORT_PATH.exists()
    report_text = REPORT_PATH.read_text(encoding="ascii")
    assert "allowed.test" in report_text
    assert "request_id=" in report_text
    assert "trace_id=" in report_text

    reset = run_command(["./scripts/recovery.sh", "reset-workspace-to-seed-baseline"], env=compose_stack)
    assert reset.returncode == 0
    assert not REPORT_PATH.exists()
