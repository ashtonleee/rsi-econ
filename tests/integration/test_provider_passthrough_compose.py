import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PROJECT = "rsi_provider_passthrough_test"
LOG_PATH = ROOT / "runtime" / "trusted_state" / "logs" / "bridge_events.jsonl"
WORKSPACE_ROOT = ROOT / "untrusted" / "agent_workspace"
ANSWER_REPORT_PATH = WORKSPACE_ROOT / "research" / "current_answer.md"
CAPTURE_REPORT_PATH = WORKSPACE_ROOT / "research" / "current_capture.md"
CAPTURE_TEXT_PATH = WORKSPACE_ROOT / "research" / "current_rendered_text.txt"
CAPTURE_SCREENSHOT_PATH = WORKSPACE_ROOT / "research" / "current_screenshot.png"
FOLLOW_ANSWER_REPORT_PATH = WORKSPACE_ROOT / "research" / "current_follow_answer.md"
FOLLOW_CAPTURE_REPORT_PATH = WORKSPACE_ROOT / "research" / "current_follow_capture.md"
FOLLOW_TEXT_PATH = WORKSPACE_ROOT / "research" / "current_follow_rendered_text.txt"
FOLLOW_CAPTURE_SCREENSHOT_PATH = WORKSPACE_ROOT / "research" / "current_follow_screenshot.png"


def docker_env() -> dict[str, str]:
    env = os.environ.copy()
    env["COMPOSE_PROJECT_NAME"] = COMPOSE_PROJECT
    env["RSI_LLM_BUDGET_TOKEN_CAP"] = "1000"
    env["RSI_WEB_ALLOWLIST_HOSTS"] = "allowed.test"
    env["RSI_FETCH_ALLOW_PRIVATE_TEST_HOSTS"] = "allowed.test"
    env["RSI_LITELLM_RESPONSE_MODE"] = "provider_passthrough"
    env["RSI_OPENAI_BASE_URL"] = "http://allowed.test/provider/v1"
    env["OPENAI_API_KEY"] = "sk-test-provider-key"
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
        "print(json.dumps({'status_code': response.status_code, 'headers': dict(response.headers), 'json': body}))\n"
    )
    result = compose_exec(service, ["python", "-c", code], env=env)
    return json.loads(result.stdout)


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


def load_events() -> list[dict]:
    if not LOG_PATH.exists():
        return []
    return [
        json.loads(line)
        for line in LOG_PATH.read_text(encoding="ascii").splitlines()
        if line.strip()
    ]


def parse_report_fields(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        fields[key] = value
    return fields


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
def compose_stack():
    env = docker_env()
    docker_ready = run_command(["docker", "info"], env=env, check=False)
    if docker_ready.returncode != 0:
        pytest.fail("Docker daemon is required for provider passthrough tests")

    compose_command(["down", "--remove-orphans", "--volumes"], env=env, check=False)
    if LOG_PATH.exists():
        LOG_PATH.unlink()
    if ANSWER_REPORT_PATH.parent.exists():
        shutil.rmtree(ANSWER_REPORT_PATH.parent)
    run_command(["./scripts/recovery.sh", "reset-workspace-to-seed-baseline"], env=env)

    compose_command(["up", "--build", "-d", "--wait"], env=env)
    yield env
    compose_command(["down", "--remove-orphans", "--volumes"], env=env, check=False)
    run_command(["./scripts/recovery.sh", "reset-workspace-to-seed-baseline"], env=env)


def test_provider_backed_answer_packet_uses_trusted_passthrough_and_records_returned_model(compose_stack):
    fixture_reset(env=compose_stack)
    expect_failure_via_agent("http://allowed.test/provider/v1/models", compose_stack)

    litellm_health = compose_http_response(
        "litellm",
        "GET",
        "http://127.0.0.1:4000/healthz",
        env=compose_stack,
    )
    assert litellm_health["status_code"] == 200
    assert litellm_health["json"]["details"]["response_mode"] == "provider_passthrough"
    assert litellm_health["json"]["details"]["provider_key_configured"] is True

    requested_model = "gpt-4.1-mini"
    result = compose_exec(
        "agent",
        [
            "python",
            "-m",
            "untrusted.agent.seed_runner",
            "--task",
            "What does this page say?",
            "--planner",
            "scripted",
            "--script",
            ".seed_plans/stage6_answer_packet_provider.json",
            "--input-url",
            "http://allowed.test/browser/rendered",
            "--max-steps",
            "8",
        ],
        env=compose_stack,
    )
    payload = json.loads(result.stdout)
    assert payload["success"] is True
    assert ANSWER_REPORT_PATH.exists()
    assert CAPTURE_REPORT_PATH.exists()
    assert CAPTURE_TEXT_PATH.exists()
    assert CAPTURE_SCREENSHOT_PATH.exists()

    summary = json.loads(
        (WORKSPACE_ROOT / "run_outputs" / "latest_seed_run.json").read_text(encoding="ascii")
    )
    bridge_chat_steps = [step for step in summary["steps"] if step["kind"] == "bridge_chat"]
    assert len(bridge_chat_steps) == 1
    returned_model = bridge_chat_steps[0]["result"]["model"]
    assert returned_model != requested_model

    answer_fields = parse_report_fields(ANSWER_REPORT_PATH.read_text(encoding="utf-8"))
    assert answer_fields["input_url"] == "http://allowed.test/browser/rendered"
    assert answer_fields["llm_model"] == returned_model

    counts = fixture_counts(env=compose_stack)
    assert counts.get("/provider/v1/chat/completions", 0) == 1
    assert counts.get("/browser/rendered", 0) >= 1

    events = load_events()
    matched = [
        event
        for event in events
        if event["event_type"] == "llm_call"
        and event["outcome"] == "success"
        and event["summary"]["model"] == returned_model
    ]
    assert matched


def test_provider_backed_follow_answer_packet_uses_trusted_passthrough_and_records_returned_model(
    compose_stack,
    tmp_path: Path,
):
    fixture_reset(env=compose_stack)
    expect_failure_via_agent("http://allowed.test/provider/v1/models", compose_stack)

    env_file = tmp_path / ".env.provider.local"
    env_file.write_text("OPENAI_API_KEY=sk-test-provider-key\n", encoding="ascii")
    provider_env = dict(compose_stack)
    provider_env["RSI_PROVIDER_ENV_FILE"] = str(env_file)

    requested_model = "openai/gpt-4.1-mini"
    source_url = "http://allowed.test/browser/follow-source"
    follow_target_url = "http://allowed.test/browser/follow-target"
    result = run_command(
        [
            "bash",
            str(ROOT / "scripts/provider.sh"),
            "follow-answer-packet",
            "--task",
            "What does the followed page say?",
            "--input-url",
            source_url,
            "--follow-target-url",
            follow_target_url,
            "--model",
            requested_model,
            "--max-steps",
            "10",
        ],
        env=provider_env,
    )
    payload = json.loads(result.stdout)
    assert payload["success"] is True
    assert payload["input_url"] == source_url
    assert payload["follow_target_url"] == follow_target_url
    assert FOLLOW_ANSWER_REPORT_PATH.exists()
    assert FOLLOW_CAPTURE_REPORT_PATH.exists()
    assert FOLLOW_TEXT_PATH.exists()
    assert FOLLOW_CAPTURE_SCREENSHOT_PATH.exists()

    summary = json.loads(
        (WORKSPACE_ROOT / "run_outputs" / "latest_seed_run.json").read_text(encoding="ascii")
    )
    bridge_chat_steps = [step for step in summary["steps"] if step["kind"] == "bridge_chat"]
    assert len(bridge_chat_steps) == 1
    returned_model = bridge_chat_steps[0]["result"]["model"]
    assert returned_model != requested_model

    answer_fields = parse_report_fields(FOLLOW_ANSWER_REPORT_PATH.read_text(encoding="utf-8"))
    capture_fields = parse_report_fields(FOLLOW_CAPTURE_REPORT_PATH.read_text(encoding="utf-8"))
    captured_text = FOLLOW_TEXT_PATH.read_text(encoding="utf-8")

    assert answer_fields["source_input_url"] == source_url
    assert answer_fields["requested_target_url"] == follow_target_url
    assert answer_fields["matched_link_text"] == "Follow same origin target"
    assert answer_fields["llm_model"] == returned_model
    assert capture_fields["request_id"] == answer_fields["request_id"]
    assert capture_fields["trace_id"] == answer_fields["trace_id"]
    assert "Stage 6B same origin target" in captured_text
    assert FOLLOW_CAPTURE_SCREENSHOT_PATH.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")

    counts = fixture_counts(env=compose_stack)
    assert counts.get("/provider/v1/chat/completions", 0) == 1
    assert counts.get("/browser/follow-source", 0) >= 1
    assert counts.get("/browser/follow-target", 0) >= 1

    events = load_events()
    matched_browser = [
        event
        for event in events
        if event["event_type"] == "browser_follow_href"
        and event["request_id"] == answer_fields["request_id"]
        and event["trace_id"] == answer_fields["trace_id"]
    ]
    assert matched_browser
    browser_event = matched_browser[0]
    assert browser_event["summary"]["requested_target_url"] == follow_target_url
    assert browser_event["summary"]["matched_link_text"] == "Follow same origin target"
    assert captured_text not in json.dumps(browser_event)
    assert any(
        event["event_type"] == "llm_call"
        and event["outcome"] == "success"
        and event["summary"]["model"] == returned_model
        for event in events
    )

    reset = run_command(
        ["./scripts/recovery.sh", "reset-workspace-to-seed-baseline"],
        env=compose_stack,
    )
    assert reset.returncode == 0
    assert not FOLLOW_ANSWER_REPORT_PATH.exists()
    assert not FOLLOW_CAPTURE_REPORT_PATH.exists()
    assert not FOLLOW_TEXT_PATH.exists()
    assert not FOLLOW_CAPTURE_SCREENSHOT_PATH.exists()
