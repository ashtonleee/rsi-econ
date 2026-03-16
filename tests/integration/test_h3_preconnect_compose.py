import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_WORKSPACE_ROOT = ROOT / "untrusted" / "agent_workspace"


def docker_env(*, state_root: Path, workspace_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["COMPOSE_PROJECT_NAME"] = f"rsi_h3_{os.getpid()}"
    env["RSI_LLM_BUDGET_TOKEN_CAP"] = "120"
    env["RSI_WEB_ALLOWLIST_HOSTS"] = "allowed.test,allowed-two.test"
    env["RSI_FETCH_ALLOW_PRIVATE_TEST_HOSTS"] = "allowed.test,allowed-two.test"
    env["RSI_TRUSTED_STATE_HOST_DIR"] = str(state_root)
    env["RSI_AGENT_WORKSPACE_HOST_DIR"] = str(workspace_root)
    env["RSI_EGRESS_TEST_IP_OVERRIDES"] = "allowed-two.test=127.0.0.1"
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
        "with httpx.Client(timeout=20.0) as client:\n"
        "    response = client.request(method, url, json=json.loads(payload) if payload else None)\n"
        "body = None\n"
        "try:\n"
        "    body = response.json()\n"
        "except Exception:\n"
        "    body = {'raw': response.text}\n"
        "print(json.dumps({'status_code': response.status_code, 'json': body, 'headers': dict(response.headers)}))\n"
    )
    result = compose_exec(service, ["python", "-c", code], env=env)
    return json.loads(result.stdout)


def load_events(log_path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in log_path.read_text(encoding="ascii").splitlines()
        if line.strip()
    ]


def matched_event(events: list[dict], *, event_type_prefix: str, request_id: str, trace_id: str) -> dict:
    for event in events:
        if (
            event["event_type"].startswith(event_type_prefix)
            and event["request_id"] == request_id
            and event["trace_id"] == trace_id
        ):
            return event
    raise AssertionError(f"missing {event_type_prefix} event for {request_id}/{trace_id}")


def fixture_reset(*, env: dict[str, str]):
    compose_http_response(
        "egress",
        "POST",
        "http://allowed.test/debug/reset-counters",
        env=env,
    )


def fixture_counts(*, env: dict[str, str]) -> dict[str, int]:
    response = compose_http_response(
        "egress",
        "GET",
        "http://allowed.test/debug/counters",
        env=env,
    )
    assert response["status_code"] == 200
    return response["json"]["counts"]


@pytest.fixture(scope="module")
def compose_stack(tmp_path_factory):
    state_root = tmp_path_factory.mktemp("h3_trusted_state")
    workspace_root = tmp_path_factory.mktemp("h3_agent_workspace")
    shutil.copytree(DEFAULT_WORKSPACE_ROOT, workspace_root, dirs_exist_ok=True)
    env = docker_env(state_root=state_root, workspace_root=workspace_root)

    docker_ready = run_command(["docker", "info"], env=env, check=False)
    if docker_ready.returncode != 0:
        pytest.fail("Docker daemon is required for H3 mediation tests")

    compose_command(["down", "--remove-orphans", "--volumes"], env=env, check=False)
    compose_command(["up", "--build", "-d", "--wait"], env=env)
    yield {
        "env": env,
        "state_root": state_root,
        "workspace_root": workspace_root,
        "log_path": state_root / "logs" / "bridge_events.jsonl",
    }
    compose_command(["down", "--remove-orphans", "--volumes"], env=env, check=False)


def test_allowed_control_paths_still_succeed_under_h3(compose_stack):
    fetch_response = compose_http_response(
        "agent",
        "POST",
        "http://bridge:8000/web/fetch",
        env=compose_stack["env"],
        payload={"url": "http://allowed.test/allowed"},
    )
    assert fetch_response["status_code"] == 200

    render_response = compose_http_response(
        "agent",
        "POST",
        "http://bridge:8000/web/browser/render",
        env=compose_stack["env"],
        payload={"url": "http://allowed.test/browser/rendered"},
    )
    assert render_response["status_code"] == 200

    follow_response = compose_http_response(
        "agent",
        "POST",
        "http://bridge:8000/web/browser/follow-href",
        env=compose_stack["env"],
        payload={
            "source_url": "http://allowed.test/browser/follow-source",
            "target_url": "http://allowed.test/browser/follow-target",
        },
    )
    assert follow_response["status_code"] == 200

    events = load_events(compose_stack["log_path"])
    fetch_event = matched_event(
        events,
        event_type_prefix="web_fetch",
        request_id=fetch_response["json"]["request_id"],
        trace_id=fetch_response["json"]["trace_id"],
    )
    render_event = matched_event(
        events,
        event_type_prefix="browser_render",
        request_id=render_response["json"]["request_id"],
        trace_id=render_response["json"]["trace_id"],
    )
    assert fetch_event["summary"]["enforcement_stage"] == "pre_connect"
    assert fetch_event["summary"]["request_forwarded"] is True
    allowed_top_level = [
        record
        for record in render_event["summary"]["channel_records"]
        if record["channel"] in {"top_level_navigation", "redirect"} and record["disposition"] == "allowed"
    ]
    assert allowed_top_level
    assert all(record["enforcement_stage"] == "pre_connect" for record in allowed_top_level)
    assert all(record["request_forwarded"] is True for record in allowed_top_level)


def test_fetcher_prevents_mismatch_before_upstream_request(compose_stack):
    fixture_reset(env=compose_stack["env"])
    response = compose_http_response(
        "agent",
        "POST",
        "http://bridge:8000/web/fetch",
        env=compose_stack["env"],
        payload={"url": "http://allowed-two.test/allowed"},
    )
    assert response["status_code"] == 502

    counts = fixture_counts(env=compose_stack["env"])
    assert counts.get("/allowed", 0) == 0

    events = load_events(compose_stack["log_path"])
    event = matched_event(
        events,
        event_type_prefix="web_fetch_error",
        request_id=response["json"]["request_id"],
        trace_id=response["json"]["trace_id"],
    )
    assert event["summary"]["enforcement_stage"] == "pre_connect"
    assert event["summary"]["request_forwarded"] is False
    assert event["summary"]["reason"] == "connect_failed"


def test_browser_prevents_mismatch_before_navigation_reaches_fixture(compose_stack):
    fixture_reset(env=compose_stack["env"])
    response = compose_http_response(
        "agent",
        "POST",
        "http://bridge:8000/web/browser/render",
        env=compose_stack["env"],
        payload={"url": "http://allowed-two.test/browser/rendered"},
    )
    assert response["status_code"] == 403

    counts = fixture_counts(env=compose_stack["env"])
    assert counts.get("/browser/rendered", 0) == 0

    events = load_events(compose_stack["log_path"])
    event = matched_event(
        events,
        event_type_prefix="browser_render_denied",
        request_id=response["json"]["request_id"],
        trace_id=response["json"]["trace_id"],
    )
    denied_top_level = [
        record
        for record in event["summary"]["channel_records"]
        if record["channel"] == "top_level_navigation" and record["disposition"] == "denied"
    ]
    assert denied_top_level
    assert denied_top_level[-1]["enforcement_stage"] == "pre_connect"
    assert denied_top_level[-1]["request_forwarded"] is False
    assert denied_top_level[-1]["reason"] == "connect_failed"


def test_redirect_hop_revalidation_blocks_before_forwarding_next_hop(compose_stack):
    fixture_reset(env=compose_stack["env"])
    response = compose_http_response(
        "agent",
        "POST",
        "http://bridge:8000/web/fetch",
        env=compose_stack["env"],
        payload={"url": "http://allowed.test/redirect-allowed-two"},
    )
    assert response["status_code"] == 502

    counts = fixture_counts(env=compose_stack["env"])
    assert counts.get("/allowed", 0) == 0

    events = load_events(compose_stack["log_path"])
    event = matched_event(
        events,
        event_type_prefix="web_fetch_error",
        request_id=response["json"]["request_id"],
        trace_id=response["json"]["trace_id"],
    )
    assert len(event["summary"]["mediation_hops"]) >= 2
    assert event["summary"]["mediation_hops"][-1]["enforcement_stage"] == "pre_connect"
    assert event["summary"]["mediation_hops"][-1]["request_forwarded"] is False
