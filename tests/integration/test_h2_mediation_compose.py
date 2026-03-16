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
    env["COMPOSE_PROJECT_NAME"] = f"rsi_h2_{os.getpid()}"
    env["RSI_LLM_BUDGET_TOKEN_CAP"] = "120"
    env["RSI_WEB_ALLOWLIST_HOSTS"] = "allowed.test,allowed-two.test"
    env["RSI_FETCH_ALLOW_PRIVATE_TEST_HOSTS"] = "allowed.test,allowed-two.test"
    env["RSI_TRUSTED_STATE_HOST_DIR"] = str(state_root)
    env["RSI_AGENT_WORKSPACE_HOST_DIR"] = str(workspace_root)
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
        "print(json.dumps({'status_code': response.status_code, 'json': body}))\n"
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


@pytest.fixture(scope="module")
def compose_stack(tmp_path_factory):
    state_root = tmp_path_factory.mktemp("h2_trusted_state")
    workspace_root = tmp_path_factory.mktemp("h2_agent_workspace")
    shutil.copytree(DEFAULT_WORKSPACE_ROOT, workspace_root, dirs_exist_ok=True)
    env = docker_env(state_root=state_root, workspace_root=workspace_root)

    docker_ready = run_command(["docker", "info"], env=env, check=False)
    if docker_ready.returncode != 0:
        pytest.fail("Docker daemon is required for H2 mediation tests")

    compose_command(["down", "--remove-orphans", "--volumes"], env=env, check=False)
    compose_command(["up", "--build", "-d", "--wait"], env=env)
    yield {
        "env": env,
        "state_root": state_root,
        "workspace_root": workspace_root,
        "log_path": state_root / "logs" / "bridge_events.jsonl",
        "state_path": state_root / "state" / "operational_state.json",
    }
    compose_command(["down", "--remove-orphans", "--volumes"], env=env, check=False)


def test_h2_isolated_evidence_root_and_live_peer_evidence(compose_stack):
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
        payload={"url": "http://allowed.test/browser/redirect-allowed-two"},
    )
    assert render_response["status_code"] == 200

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

    assert str(compose_stack["state_root"]) in compose_stack["log_path"].as_posix()
    assert fetch_event["summary"]["mediation_hops"]
    assert fetch_event["summary"]["mediation_hops"][0]["actual_peer_ip"] in fetch_event["summary"]["mediation_hops"][0]["approved_ips"]
    assert render_event["summary"]["channel_records"]
    allowed_top_level = [
        record
        for record in render_event["summary"]["channel_records"]
        if record["channel"] in {"top_level_navigation", "redirect"} and record["disposition"] == "allowed"
    ]
    assert allowed_top_level
    assert any(record["actual_peer_ip"] in record["approved_ips"] for record in allowed_top_level)


@pytest.mark.parametrize(
    "url",
    [
        "http://allowed.test/browser/render-meta-refresh",
        "http://allowed.test/browser/render-js-redirect",
    ],
)
def test_h2_unapproved_additional_top_level_navigation_is_never_silent(compose_stack, url):
    response = compose_http_response(
        "agent",
        "POST",
        "http://bridge:8000/web/browser/render",
        env=compose_stack["env"],
        payload={"url": url},
    )
    assert response["status_code"] == 403

    events = load_events(compose_stack["log_path"])
    event = matched_event(
        events,
        event_type_prefix="browser_render_denied",
        request_id=response["json"]["request_id"],
        trace_id=response["json"]["trace_id"],
    )
    assert any(
        record["channel"] in {"top_level_navigation", "redirect"}
        and record["disposition"] == "denied"
        for record in event["summary"]["channel_records"]
    )


@pytest.mark.parametrize(
    ("url", "expected_channel"),
    [
        ("http://allowed.test/browser/channel-iframe-blocked", "frame_navigation"),
        ("http://allowed.test/browser/channel-fetch-xhr", "fetch_xhr"),
        ("http://allowed.test/browser/channel-form-submit", "form_submission"),
        ("http://allowed.test/browser/channel-websocket", "websocket"),
        ("http://allowed.test/browser/channel-eventsource", "eventsource"),
        ("http://allowed.test/browser/channel-beacon", "send_beacon"),
        ("http://allowed.test/browser/channel-popup", "popup"),
        ("http://allowed.test/browser/channel-download", "download"),
        ("http://allowed.test/browser/channel-upload", "upload"),
        ("http://allowed.test/browser/channel-prefetch", "prefetch_preconnect"),
        ("http://allowed.test/browser/channel-external-protocol", "external_protocol"),
        ("http://allowed.test/browser/channel-worker", "worker"),
    ],
)
def test_h2_blocked_channels_are_logged_even_if_page_partially_renders(compose_stack, url, expected_channel):
    response = compose_http_response(
        "agent",
        "POST",
        "http://bridge:8000/web/browser/render",
        env=compose_stack["env"],
        payload={"url": url},
    )
    assert response["status_code"] in {200, 403}

    request_id = response["json"]["request_id"]
    trace_id = response["json"]["trace_id"]
    event_type_prefix = "browser_render" if response["status_code"] == 200 else "browser_render_denied"
    events = load_events(compose_stack["log_path"])
    event = matched_event(
        events,
        event_type_prefix=event_type_prefix,
        request_id=request_id,
        trace_id=trace_id,
    )

    matches = [
        record
        for record in event["summary"]["channel_records"]
        if record["channel"] == expected_channel
    ]
    assert matches
    assert matches[-1]["disposition"] in {"denied", "classified"}
    assert matches[-1]["reason"]


def test_h2_follow_href_does_not_allow_unapproved_additional_navigation(compose_stack):
    source_url = "http://allowed.test/browser/follow-source"
    response = compose_http_response(
        "agent",
        "POST",
        "http://bridge:8000/web/browser/follow-href",
        env=compose_stack["env"],
        payload={
            "source_url": source_url,
            "target_url": "http://allowed.test/browser/follow-meta-refresh-target",
        },
    )
    assert response["status_code"] == 403

    events = load_events(compose_stack["log_path"])
    event = matched_event(
        events,
        event_type_prefix="browser_follow_href_denied",
        request_id=response["json"]["request_id"],
        trace_id=response["json"]["trace_id"],
    )
    assert any(
        record["channel"] in {"top_level_navigation", "redirect"}
        and record["disposition"] == "denied"
        for record in event["summary"]["channel_records"]
    )
