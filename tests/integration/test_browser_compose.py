import base64
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PROJECT = "rsi_stage6_browser_test"
LOG_PATH = ROOT / "runtime" / "trusted_state" / "logs" / "bridge_events.jsonl"
STATE_PATH = ROOT / "runtime" / "trusted_state" / "state" / "operational_state.json"
WORKSPACE_ROOT = ROOT / "untrusted" / "agent_workspace"
REPORT_PATH = WORKSPACE_ROOT / "reports" / "stage6_browser_report.md"
SCREENSHOT_PATH = WORKSPACE_ROOT / "reports" / "stage6_browser_screenshot.png"
FOLLOW_REPORT_PATH = WORKSPACE_ROOT / "reports" / "stage6b_browser_follow_report.md"
FOLLOW_SCREENSHOT_PATH = WORKSPACE_ROOT / "reports" / "stage6b_browser_follow_screenshot.png"
ANSWER_REPORT_PATH = WORKSPACE_ROOT / "research" / "current_answer.md"
CAPTURE_REPORT_PATH = WORKSPACE_ROOT / "research" / "current_capture.md"
CAPTURE_TEXT_PATH = WORKSPACE_ROOT / "research" / "current_rendered_text.txt"
CAPTURE_SCREENSHOT_PATH = WORKSPACE_ROOT / "research" / "current_screenshot.png"
FOLLOW_ANSWER_REPORT_PATH = WORKSPACE_ROOT / "research" / "current_follow_answer.md"
FOLLOW_CAPTURE_REPORT_PATH = WORKSPACE_ROOT / "research" / "current_follow_capture.md"
FOLLOW_TEXT_PATH = WORKSPACE_ROOT / "research" / "current_follow_rendered_text.txt"
FOLLOW_CAPTURE_SCREENSHOT_PATH = WORKSPACE_ROOT / "research" / "current_follow_screenshot.png"

TEST_AGENT_TOKEN = "rsi-agent-token-dev-sentinel"
TEST_OPERATOR_TOKEN = "rsi-operator-token-dev-sentinel"


def agent_auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_AGENT_TOKEN}"}


def operator_auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_OPERATOR_TOKEN}"}


def docker_env() -> dict[str, str]:
    env = os.environ.copy()
    env["COMPOSE_PROJECT_NAME"] = COMPOSE_PROJECT
    env["RSI_LLM_BUDGET_TOKEN_CAP"] = "200"
    env["RSI_WEB_ALLOWLIST_HOSTS"] = "allowed.test,allowed-two.test"
    env["RSI_FETCH_ALLOW_PRIVATE_TEST_HOSTS"] = "allowed.test,allowed-two.test,blocked.test"
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
    headers: dict | None = None,
) -> dict:
    code = (
        "import httpx, json\n"
        f"method = {method!r}\n"
        f"url = {url!r}\n"
        f"payload = {json.dumps(payload)!r}\n"
        f"headers = {json.dumps(headers or {})!r}\n"
        "with httpx.Client(timeout=20.0) as client:\n"
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


def load_state() -> dict:
    assert STATE_PATH.exists(), STATE_PATH
    return json.loads(STATE_PATH.read_text(encoding="ascii"))


def parse_report_fields(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        fields[key] = value
    return fields


def find_interactable(
    snapshot: dict,
    *,
    kind: str | None = None,
    name: str | None = None,
    label: str | None = None,
    text: str | None = None,
) -> dict:
    for item in snapshot["interactable_elements"]:
        if kind and item.get("kind") != kind:
            continue
        if name and item.get("name") != name:
            continue
        if label and item.get("label") != label:
            continue
        if text and item.get("text") != text:
            continue
        return item
    raise AssertionError(
        f"missing interactable kind={kind!r} name={name!r} label={label!r} text={text!r}: "
        f"{snapshot['interactable_elements']!r}"
    )


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


def remove_project_containers(*, env: dict[str, str]):
    result = run_command(
        [
            "docker",
            "ps",
            "-aq",
            "--filter",
            f"name={COMPOSE_PROJECT}",
        ],
        env=env,
        check=False,
    )
    container_ids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if container_ids:
        run_command(["docker", "rm", "-f", *container_ids], env=env, check=False)


def compose_up_with_retry(*, env: dict[str, str]):
    try:
        compose_command(["up", "--build", "-d", "--wait"], env=env)
        return
    except subprocess.CalledProcessError as exc:
        if "No such container" not in exc.stderr:
            raise
        compose_command(["down", "--remove-orphans", "--volumes"], env=env, check=False)
        remove_project_containers(env=env)
        compose_command(["up", "--build", "-d", "--wait"], env=env)


@pytest.fixture(scope="module")
def compose_stack():
    env = docker_env()
    docker_ready = run_command(["docker", "info"], env=env, check=False)
    if docker_ready.returncode != 0:
        pytest.fail("Docker daemon is required for Stage 6 browser tests")

    compose_command(["down", "--remove-orphans", "--volumes"], env=env, check=False)
    remove_project_containers(env=env)
    if LOG_PATH.exists():
        LOG_PATH.unlink()
    if STATE_PATH.exists():
        STATE_PATH.unlink()
    if REPORT_PATH.parent.exists():
        shutil.rmtree(REPORT_PATH.parent)
    run_command(["./scripts/recovery.sh", "reset-workspace-to-seed-baseline"], env=env)

    compose_up_with_retry(env=env)
    yield env
    compose_command(["down", "--remove-orphans", "--volumes"], env=env, check=False)
    remove_project_containers(env=env)
    run_command(["./scripts/recovery.sh", "reset-workspace-to-seed-baseline"], env=env)


def test_browser_render_succeeds_only_through_trusted_path(compose_stack):
    expect_failure_via_agent("http://1.1.1.1", compose_stack)
    expect_failure_via_agent("https://api.openai.com/v1/models", compose_stack)
    expect_failure_via_agent("http://litellm:4000/healthz", compose_stack)
    expect_failure_via_agent("http://fetcher:8082/healthz", compose_stack)
    expect_failure_via_agent("http://browser:8083/healthz", compose_stack)
    expect_failure_via_agent("http://egress:8084/healthz", compose_stack)

    rendered = compose_http_response(
        "agent",
        "POST",
        "http://bridge:8000/web/browser/render",
        env=compose_stack,
        payload={"url": "http://allowed.test/browser/rendered"},
        headers=agent_auth_headers(),
    )
    assert rendered["status_code"] == 200
    body = rendered["json"]
    assert body["page_title"] == "Stage 6 Fixture Title"
    assert "Stage 6 fixture rendered body" in body["rendered_text"]
    assert body["request_id"]
    assert body["trace_id"]
    assert isinstance(body["followable_links"], list)
    screenshot = base64.b64decode(body["screenshot_png_base64"])
    assert screenshot.startswith(b"\x89PNG\r\n\x1a\n")

    events = load_events()
    matched = [
        event
        for event in events
        if event["event_type"] == "browser_render"
        and event["request_id"] == body["request_id"]
        and event["trace_id"] == body["trace_id"]
    ]
    assert matched
    event = matched[0]
    assert event["summary"]["final_url"] == "http://allowed.test/browser/rendered"
    assert event["summary"]["page_title"] == "Stage 6 Fixture Title"
    assert event["summary"]["screenshot_sha256"]
    assert "Stage 6 fixture rendered body" not in json.dumps(event)
    assert body["screenshot_png_base64"] not in json.dumps(event)


def test_browser_container_runs_non_root_without_unsafe_sandbox_flags(compose_stack):
    runtime_probe = (
        "import json, os\n"
        "self_pid = str(os.getpid())\n"
        "cmdlines = []\n"
        "for entry in os.listdir('/proc'):\n"
        "    if not entry.isdigit():\n"
        "        continue\n"
        "    if entry == self_pid:\n"
        "        continue\n"
        "    exe_path = f'/proc/{entry}/exe'\n"
        "    try:\n"
        "        exe = os.path.basename(os.readlink(exe_path))\n"
        "    except Exception:\n"
        "        continue\n"
        "    if exe not in {'chrome', 'chromium', 'headless_shell'}:\n"
        "        continue\n"
        "    path = f'/proc/{entry}/cmdline'\n"
        "    try:\n"
        "        raw = open(path, 'rb').read().replace(b'\\x00', b' ').decode('utf-8', errors='ignore')\n"
        "    except Exception:\n"
        "        continue\n"
        "    cmdlines.append(raw)\n"
        "joined = '\\n'.join(cmdlines)\n"
        "payload = {\n"
        "    'euid': os.geteuid(),\n"
        "    'unsafe_flag_seen': ('--no-sandbox' in joined) or ('--disable-setuid-sandbox' in joined),\n"
        "}\n"
        "print(json.dumps(payload))\n"
    )
    probe = compose_exec("browser", ["python", "-c", runtime_probe], env=compose_stack)
    payload = json.loads(probe.stdout)
    assert payload["euid"] != 0
    assert payload["unsafe_flag_seen"] is False

    health = compose_http_response(
        "browser",
        "GET",
        "http://127.0.0.1:8083/healthz",
        env=compose_stack,
    )
    assert health["status_code"] == 200
    body = health["json"]
    assert body["details"]["running_as_root"] is False
    assert body["details"]["chromium_sandbox"] is True
    assert "--no-sandbox" not in body["details"]["launch_args"]
    assert "--disable-setuid-sandbox" not in body["details"]["launch_args"]


def test_browser_fails_closed_and_status_exposes_browser_state(compose_stack):
    for url in [
        "http://allowed.test/browser/blocked-subresource",
        "http://allowed.test/browser/popup",
        "http://allowed.test/browser/download-page",
        "http://allowed.test/browser/redirect-blocked",
    ]:
        response = compose_http_response(
            "agent",
            "POST",
            "http://bridge:8000/web/browser/render",
            env=compose_stack,
            payload={"url": url},
            headers=agent_auth_headers(),
        )
        assert response["status_code"] == 403

    status = compose_http_response(
        "agent",
        "GET",
        "http://bridge:8000/status",
        env=compose_stack,
        headers=agent_auth_headers(),
    )["json"]
    assert status["browser"]["service"]["reachable"] is True
    assert status["browser"]["caps"]["viewport_width"] == 1280
    assert status["browser"]["counters"]["browser_render_total"] >= 1
    assert status["surfaces"]["browser"] == "trusted_browser_stage6a_read_only_render"

    events = load_events()
    assert any(event["event_type"] == "browser_render_denied" for event in events)

    probe_response = compose_http_response(
        "agent",
        "POST",
        "http://bridge:8000/debug/probes/public-egress",
        env=compose_stack,
        headers=agent_auth_headers(),
    )
    assert probe_response["status_code"] == 404


def test_browser_follow_href_succeeds_only_through_trusted_path(compose_stack):
    source_url = "http://allowed.test/browser/follow-source"
    source = compose_http_response(
        "agent",
        "POST",
        "http://bridge:8000/web/browser/render",
        env=compose_stack,
        payload={"url": source_url},
        headers=agent_auth_headers(),
    )
    assert source["status_code"] == 200
    links = source["json"]["followable_links"]
    same_origin = next(
        link for link in links if link["target_url"] == "http://allowed.test/browser/follow-target"
    )
    cross_origin = next(
        link
        for link in links
        if link["target_url"] == "http://allowed-two.test/browser/cross-origin-target"
    )
    assert same_origin["same_origin"] is True
    assert cross_origin["same_origin"] is False

    same_response = compose_http_response(
        "agent",
        "POST",
        "http://bridge:8000/web/browser/follow-href",
        env=compose_stack,
        payload={"source_url": source_url, "target_url": same_origin["target_url"]},
        headers=agent_auth_headers(),
    )
    assert same_response["status_code"] == 200
    same_body = same_response["json"]
    assert same_body["final_url"] == "http://allowed.test/browser/follow-target"
    assert same_body["source_final_url"] == source_url
    assert same_body["follow_hop_count"] == 1
    assert same_body["navigation_history"] == [
        source_url,
        "http://allowed.test/browser/follow-target",
    ]
    assert same_body["page_title"] == "Stage 6B Same Origin Target"
    assert base64.b64decode(same_body["screenshot_png_base64"]).startswith(b"\x89PNG\r\n\x1a\n")

    cross_response = compose_http_response(
        "agent",
        "POST",
        "http://bridge:8000/web/browser/follow-href",
        env=compose_stack,
        payload={"source_url": source_url, "target_url": cross_origin["target_url"]},
        headers=agent_auth_headers(),
    )
    assert cross_response["status_code"] == 200
    cross_body = cross_response["json"]
    assert cross_body["final_url"] == "http://allowed-two.test/browser/cross-origin-target"
    assert cross_body["navigation_history"] == [
        source_url,
        "http://allowed-two.test/browser/cross-origin-target",
    ]

    events = load_events()
    matched = [
        event
        for event in events
        if event["event_type"] == "browser_follow_href"
        and event["request_id"] == same_body["request_id"]
        and event["trace_id"] == same_body["trace_id"]
    ]
    assert matched
    event = matched[0]
    assert event["summary"]["requested_target_url"] == "http://allowed.test/browser/follow-target"
    assert event["summary"]["navigation_history"] == [
        source_url,
        "http://allowed.test/browser/follow-target",
    ]
    assert event["summary"]["screenshot_sha256"]
    assert same_body["screenshot_png_base64"] not in json.dumps(event)
    assert same_body["rendered_text"] not in json.dumps(event)


def test_browser_follow_href_fails_closed_and_status_exposes_follow_state(compose_stack):
    source_url = "http://allowed.test/browser/follow-source"
    denied_cases = [
        "http://allowed.test/browser/not-linked",
        "http://blocked.test/browser/rendered",
        "http://allowed.test/browser/follow-blocked-subresource",
        "http://allowed.test/browser/follow-popup-target",
        "http://allowed.test/browser/follow-download-target",
        "http://allowed.test/browser/follow-meta-refresh-target",
        "http://allowed.test/browser/follow-redirect-blocked-target",
    ]
    for target_url in denied_cases:
        response = compose_http_response(
            "agent",
            "POST",
            "http://bridge:8000/web/browser/follow-href",
            env=compose_stack,
            payload={"source_url": source_url, "target_url": target_url},
            headers=agent_auth_headers(),
        )
        assert response["status_code"] == 403

    status = compose_http_response(
        "agent",
        "GET",
        "http://bridge:8000/status",
        env=compose_stack,
        headers=agent_auth_headers(),
    )["json"]
    assert status["browser"]["service"]["reachable"] is True
    assert status["browser"]["counters"]["browser_follow_href_total"] >= 1
    assert status["browser"]["caps"]["max_follow_hops"] == 1
    assert status["browser"]["caps"]["max_followable_links"] == 20
    assert status["surfaces"]["browser_follow_href"] == "trusted_browser_stage6b_safe_follow_href"

    events = load_events()
    assert any(event["event_type"] == "browser_follow_href_denied" for event in events)

    probe_response = compose_http_response(
        "agent",
        "POST",
        "http://bridge:8000/debug/probes/public-egress",
        env=compose_stack,
        headers=agent_auth_headers(),
    )
    assert probe_response["status_code"] == 404


def test_interactive_browser_session_lifecycle_supports_click_fill_and_gated_submit(compose_stack):
    help_open = compose_http_response(
        "agent",
        "POST",
        "http://bridge:8000/web/browser/sessions/open",
        env=compose_stack,
        payload={"url": "http://allowed.test/browser/interactive-form"},
        headers=agent_auth_headers(),
    )
    assert help_open["status_code"] == 200
    help_snapshot = help_open["json"]
    assert help_snapshot["page_title"] == "Interactive form fixture"
    assert help_snapshot["current_url"] == "http://allowed.test/browser/interactive-form"

    help_link = find_interactable(help_snapshot, kind="link", text="Read more")
    help_click = compose_http_response(
        "agent",
        "POST",
        f"http://bridge:8000/web/browser/sessions/{help_snapshot['session_id']}/click",
        env=compose_stack,
        payload={
            "snapshot_id": help_snapshot["snapshot_id"],
            "element_id": help_link["element_id"],
        },
        headers=agent_auth_headers(),
    )
    assert help_click["status_code"] == 200
    assert help_click["json"]["current_url"] == "http://allowed.test/browser/interactive-help"
    assert help_click["json"]["page_title"] == "Interactive help fixture"

    opened = compose_http_response(
        "agent",
        "POST",
        "http://bridge:8000/web/browser/sessions/open",
        env=compose_stack,
        payload={"url": "http://allowed.test/browser/interactive-form"},
        headers=agent_auth_headers(),
    )
    assert opened["status_code"] == 200
    snapshot = opened["json"]
    session_id = snapshot["session_id"]
    first_snapshot_id = snapshot["snapshot_id"]

    submit = find_interactable(snapshot, kind="submit", text="Claim reward")
    direct_submit_click = compose_http_response(
        "agent",
        "POST",
        f"http://bridge:8000/web/browser/sessions/{session_id}/click",
        env=compose_stack,
        payload={
            "snapshot_id": first_snapshot_id,
            "element_id": submit["element_id"],
        },
        headers=agent_auth_headers(),
    )
    assert direct_submit_click["status_code"] == 409
    assert direct_submit_click["json"]["reason"] == "browser_submit_requires_proposal"

    name_field = find_interactable(snapshot, kind="text_input", name="name")
    typed = compose_http_response(
        "agent",
        "POST",
        f"http://bridge:8000/web/browser/sessions/{session_id}/type",
        env=compose_stack,
        payload={
            "snapshot_id": first_snapshot_id,
            "element_id": name_field["element_id"],
            "text": "alice",
        },
        headers=agent_auth_headers(),
    )
    assert typed["status_code"] == 200
    typed_snapshot = typed["json"]
    assert find_interactable(typed_snapshot, kind="text_input", name="name")["value_preview"] == "alice"

    stale_select = compose_http_response(
        "agent",
        "POST",
        f"http://bridge:8000/web/browser/sessions/{session_id}/select",
        env=compose_stack,
        payload={
            "snapshot_id": first_snapshot_id,
            "element_id": find_interactable(snapshot, kind="select", name="plan")["element_id"],
            "value": "pro",
        },
        headers=agent_auth_headers(),
    )
    assert stale_select["status_code"] == 409
    assert stale_select["json"]["reason"] == "browser_snapshot_stale"

    select = compose_http_response(
        "agent",
        "POST",
        f"http://bridge:8000/web/browser/sessions/{session_id}/select",
        env=compose_stack,
        payload={
            "snapshot_id": typed_snapshot["snapshot_id"],
            "element_id": find_interactable(typed_snapshot, kind="select", name="plan")["element_id"],
            "value": "pro",
        },
        headers=agent_auth_headers(),
    )
    assert select["status_code"] == 200
    selected_snapshot = select["json"]
    assert find_interactable(selected_snapshot, kind="select", name="plan")["value_preview"] == "Pro"

    checked = compose_http_response(
        "agent",
        "POST",
        f"http://bridge:8000/web/browser/sessions/{session_id}/set_checked",
        env=compose_stack,
        payload={
            "snapshot_id": selected_snapshot["snapshot_id"],
            "element_id": find_interactable(selected_snapshot, kind="checkbox", name="agree")["element_id"],
            "checked": True,
        },
        headers=agent_auth_headers(),
    )
    assert checked["status_code"] == 200
    checked_snapshot = checked["json"]
    assert find_interactable(checked_snapshot, kind="checkbox", name="agree")["checked"] is True

    proposal = compose_http_response(
        "agent",
        "POST",
        f"http://bridge:8000/web/browser/sessions/{session_id}/submit_proposal",
        env=compose_stack,
        payload={
            "snapshot_id": checked_snapshot["snapshot_id"],
            "element_id": find_interactable(checked_snapshot, kind="submit", text="Claim reward")["element_id"],
        },
        headers=agent_auth_headers(),
    )
    assert proposal["status_code"] == 200
    proposal_body = proposal["json"]
    assert proposal_body["action_type"] == "browser_submit"
    assert proposal_body["status"] == "pending"
    assert proposal_body["action_payload"]["target_url"] == "http://allowed.test/browser/interactive-result"
    assert proposal_body["action_payload"]["method"] == "POST"
    assert proposal_body["action_payload"]["field_preview"] == [
        {"name": "name", "kind": "text", "value_preview": "alice", "checked": False},
        {"name": "plan", "kind": "select", "value_preview": "Pro", "checked": False},
        {"name": "agree", "kind": "checkbox", "value_preview": "yes", "checked": True},
    ]
    proposal_id = proposal_body["proposal_id"]

    decide = compose_http_response(
        "bridge",
        "POST",
        f"http://127.0.0.1:8000/proposals/{proposal_id}/decide",
        env=compose_stack,
        payload={"decision": "approve", "reason": "interactive browser lifecycle test"},
        headers=operator_auth_headers(),
    )
    assert decide["status_code"] == 200
    assert decide["json"]["status"] == "approved"

    execute = compose_http_response(
        "bridge",
        "POST",
        f"http://127.0.0.1:8000/proposals/{proposal_id}/execute",
        env=compose_stack,
        headers=operator_auth_headers(),
    )
    assert execute["status_code"] == 200
    execute_body = execute["json"]
    assert execute_body["status"] == "executed"
    result = execute_body["execution_result"]
    assert result["target_url"] == "http://allowed.test/browser/interactive-result"
    assert result["method"] == "POST"
    assert result["http_status"] == 200
    assert result["page_title"] == "Interactive result fixture"
    assert result["current_url"] == "http://allowed.test/browser/interactive-result"
    assert result["field_preview"] == [
        {"name": "name", "kind": "text", "value_preview": "alice", "checked": False},
        {"name": "plan", "kind": "select", "value_preview": "Pro", "checked": False},
        {"name": "agree", "kind": "checkbox", "value_preview": "yes", "checked": True},
    ]
    assert result["session_id"] == session_id
    assert result["snapshot_id"]

    post_submit_snapshot = compose_http_response(
        "agent",
        "GET",
        f"http://bridge:8000/web/browser/sessions/{session_id}",
        env=compose_stack,
        headers=agent_auth_headers(),
    )
    assert post_submit_snapshot["status_code"] == 200
    assert post_submit_snapshot["json"]["current_url"] == "http://allowed.test/browser/interactive-result"
    assert "Submitted name=alice plan=pro agree=yes." in post_submit_snapshot["json"]["rendered_text"]

    events = load_events()
    event_types = [event["event_type"] for event in events]
    assert "browser_session_open" in event_types
    assert "browser_session_click" in event_types
    assert "browser_session_type" in event_types
    assert "browser_session_select" in event_types
    assert "browser_session_set_checked" in event_types
    assert "browser_submit_proposal_created" in event_types
    assert "proposal_executed" in event_types


def test_browser_submit_preview_blocks_targets_outside_action_allowlist(compose_stack):
    opened = compose_http_response(
        "agent",
        "POST",
        "http://bridge:8000/web/browser/sessions/open",
        env=compose_stack,
        payload={"url": "http://allowed.test/browser/interactive-form-disallowed"},
        headers=agent_auth_headers(),
    )
    assert opened["status_code"] == 200
    snapshot = opened["json"]

    proposal = compose_http_response(
        "agent",
        "POST",
        f"http://bridge:8000/web/browser/sessions/{snapshot['session_id']}/submit_proposal",
        env=compose_stack,
        payload={
            "snapshot_id": snapshot["snapshot_id"],
            "element_id": find_interactable(snapshot, kind="submit", text="Try blocked submit")["element_id"],
        },
        headers=agent_auth_headers(),
    )
    assert proposal["status_code"] == 403
    assert proposal["json"]["reason"] == "host_not_in_action_allowlist"
    assert proposal["json"]["host"] == "allowed-two.test"


def test_public_workflow_browser_session_supports_tabs_history_and_approval_gated_fetch(compose_stack):
    opened = compose_http_response(
        "agent",
        "POST",
        "http://bridge:8000/web/browser/sessions/open",
        env=compose_stack,
        payload={
            "url": "http://blocked.test/browser/public-workflow-start",
            "capability_profile": "workflow_browser_public",
        },
        headers=agent_auth_headers(),
    )
    assert opened["status_code"] == 200
    snapshot = opened["json"]
    assert snapshot["capability_profile"] == "workflow_browser_public"
    assert snapshot["current_url"] == "http://blocked.test/browser/public-workflow-start"
    assert snapshot["page_title"] == "Public workflow start fixture"
    assert snapshot["active_tab_id"]
    assert len(snapshot["tabs"]) == 1

    navigate = compose_http_response(
        "agent",
        "POST",
        f"http://bridge:8000/web/browser/sessions/{snapshot['session_id']}/navigate",
        env=compose_stack,
        payload={
            "snapshot_id": snapshot["snapshot_id"],
            "url": "http://blocked.test/browser/public-workflow-step",
        },
        headers=agent_auth_headers(),
    )
    assert navigate["status_code"] == 200
    navigate_body = navigate["json"]
    assert navigate_body["outcome"] == "snapshot"
    step_snapshot = navigate_body["snapshot"]
    assert step_snapshot["current_url"] == "http://blocked.test/browser/public-workflow-step"
    assert step_snapshot["page_title"] == "Public workflow step fixture"
    first_tab_id = step_snapshot["active_tab_id"]

    back = compose_http_response(
        "agent",
        "POST",
        f"http://bridge:8000/web/browser/sessions/{snapshot['session_id']}/back",
        env=compose_stack,
        payload={"snapshot_id": step_snapshot["snapshot_id"]},
        headers=agent_auth_headers(),
    )
    assert back["status_code"] == 200
    back_snapshot = back["json"]["snapshot"]
    assert back_snapshot["current_url"] == "http://blocked.test/browser/public-workflow-start"

    forward = compose_http_response(
        "agent",
        "POST",
        f"http://bridge:8000/web/browser/sessions/{snapshot['session_id']}/forward",
        env=compose_stack,
        payload={"snapshot_id": back_snapshot["snapshot_id"]},
        headers=agent_auth_headers(),
    )
    assert forward["status_code"] == 200
    step_snapshot = forward["json"]["snapshot"]
    assert step_snapshot["current_url"] == "http://blocked.test/browser/public-workflow-step"

    new_tab = compose_http_response(
        "agent",
        "POST",
        f"http://bridge:8000/web/browser/sessions/{snapshot['session_id']}/tabs/new",
        env=compose_stack,
        payload={"url": "http://allowed.test/browser/interactive-help"},
        headers=agent_auth_headers(),
    )
    assert new_tab["status_code"] == 200
    new_tab_snapshot = new_tab["json"]["snapshot"]
    assert new_tab_snapshot["current_url"] == "http://allowed.test/browser/interactive-help"
    assert new_tab_snapshot["page_title"] == "Interactive help fixture"
    assert len(new_tab_snapshot["tabs"]) == 2
    assert new_tab_snapshot["active_tab_id"] != first_tab_id

    switched = compose_http_response(
        "agent",
        "POST",
        f"http://bridge:8000/web/browser/sessions/{snapshot['session_id']}/tabs/switch",
        env=compose_stack,
        payload={
            "snapshot_id": new_tab_snapshot["snapshot_id"],
            "tab_id": first_tab_id,
        },
        headers=agent_auth_headers(),
    )
    assert switched["status_code"] == 200
    step_snapshot = switched["json"]["snapshot"]
    assert step_snapshot["current_url"] == "http://blocked.test/browser/public-workflow-step"
    assert step_snapshot["active_tab_id"] == first_tab_id

    email_field = find_interactable(step_snapshot, kind="text_input", name="email")
    filled = compose_http_response(
        "agent",
        "POST",
        f"http://bridge:8000/web/browser/sessions/{snapshot['session_id']}/fill",
        env=compose_stack,
        payload={
            "snapshot_id": step_snapshot["snapshot_id"],
            "element_id": email_field["element_id"],
            "text": "alice@example.com",
        },
        headers=agent_auth_headers(),
    )
    assert filled["status_code"] == 200
    filled_body = filled["json"]
    assert filled_body["outcome"] == "snapshot"
    filled_snapshot = filled_body["snapshot"]
    assert find_interactable(filled_snapshot, kind="text_input", name="email")["value_preview"] == "alice@example.com"

    send_button = find_interactable(
        filled_snapshot,
        kind="button",
        text="Send public workflow request",
    )
    request_pause = compose_http_response(
        "agent",
        "POST",
        f"http://bridge:8000/web/browser/sessions/{snapshot['session_id']}/actions/click",
        env=compose_stack,
        payload={
            "snapshot_id": filled_snapshot["snapshot_id"],
            "element_id": send_button["element_id"],
        },
        headers=agent_auth_headers(),
    )
    assert request_pause["status_code"] == 200
    request_pause_body = request_pause["json"]
    assert request_pause_body["outcome"] == "proposal_required"
    assert request_pause_body["proposal_preview"]["target_url"] == "http://blocked.test/browser/public-workflow-result"
    assert request_pause_body["proposal_preview"]["method"] == "POST"
    assert request_pause_body["proposal"]["action_type"] == "browser_http_request"
    assert request_pause_body["proposal"]["status"] == "pending"
    proposal_id = request_pause_body["proposal"]["proposal_id"]

    decide = compose_http_response(
        "bridge",
        "POST",
        f"http://127.0.0.1:8000/proposals/{proposal_id}/decide",
        env=compose_stack,
        payload={"decision": "approve", "reason": "public workflow browser compose test"},
        headers=operator_auth_headers(),
    )
    assert decide["status_code"] == 200
    assert decide["json"]["status"] == "approved"

    execute = compose_http_response(
        "bridge",
        "POST",
        f"http://127.0.0.1:8000/proposals/{proposal_id}/execute",
        env=compose_stack,
        headers=operator_auth_headers(),
    )
    assert execute["status_code"] == 200
    execute_body = execute["json"]
    assert execute_body["status"] == "executed"
    result = execute_body["execution_result"]
    assert result["method"] == "POST"
    assert result["target_url"] == "http://blocked.test/browser/public-workflow-result"
    assert result["session_id"] == snapshot["session_id"]
    assert result["snapshot_id"]

    post_execute_snapshot = compose_http_response(
        "agent",
        "GET",
        f"http://bridge:8000/web/browser/sessions/{snapshot['session_id']}",
        env=compose_stack,
        headers=agent_auth_headers(),
    )
    assert post_execute_snapshot["status_code"] == 200
    final_snapshot = post_execute_snapshot["json"]
    assert final_snapshot["current_url"] == "http://blocked.test/browser/public-workflow-step"
    assert "Workflow submitted for alice@example.com from /browser/public-workflow-step." in final_snapshot["rendered_text"]
    assert final_snapshot["pending_request_preview"] is None

    events = load_events()
    event_types = [event["event_type"] for event in events]
    assert "browser_session_navigate" in event_types
    assert "browser_session_back" in event_types
    assert "browser_session_forward" in event_types
    assert "browser_session_new_tab" in event_types
    assert "browser_session_switch_tab" in event_types
    assert "browser_http_request_proposal_created" in event_types
    assert "proposal_executed" in event_types


def test_seed_runner_browser_demo_writes_artifacts_and_recovery_resets_them(compose_stack):
    result = compose_exec(
        "agent",
        [
            "python",
            "-m",
            "untrusted.agent.seed_runner",
            "--task",
            "render one allowed page and write a browser report",
            "--planner",
            "scripted",
            "--script",
            ".seed_plans/stage6_browser_demo.json",
            "--max-steps",
            "8",
        ],
        env=compose_stack,
    )
    payload = json.loads(result.stdout)
    assert payload["success"] is True
    assert REPORT_PATH.exists()
    assert SCREENSHOT_PATH.exists()
    report = REPORT_PATH.read_text(encoding="utf-8")
    assert "Stage 6 Fixture Title" in report
    assert "request_id=" in report
    assert "trace_id=" in report
    assert SCREENSHOT_PATH.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")

    reset = run_command(
        ["./scripts/recovery.sh", "reset-workspace-to-seed-baseline"],
        env=compose_stack,
    )
    assert reset.returncode == 0
    assert not REPORT_PATH.exists()
    assert not SCREENSHOT_PATH.exists()

    state = load_state()
    assert state["browser"]["counters"]["browser_render_success"] >= 1


def test_seed_runner_single_url_capture_packet_writes_artifacts_and_recovery_resets_them(compose_stack):
    input_url = "http://allowed.test/browser/rendered"
    direct = compose_http_response(
        "agent",
        "POST",
        "http://bridge:8000/web/browser/render",
        env=compose_stack,
        payload={"url": input_url},
        headers=agent_auth_headers(),
    )
    assert direct["status_code"] == 200
    direct_body = direct["json"]

    result = compose_exec(
        "agent",
        [
            "python",
            "-m",
            "untrusted.agent.seed_runner",
            "--task",
            "capture one allowlisted page into a research packet",
            "--planner",
            "scripted",
            "--script",
            ".seed_plans/stage6_capture_packet.json",
            "--input-url",
            input_url,
            "--max-steps",
            "8",
        ],
        env=compose_stack,
    )
    payload = json.loads(result.stdout)
    assert payload["success"] is True
    assert payload["input_url"] == input_url
    assert CAPTURE_REPORT_PATH.exists()
    assert CAPTURE_TEXT_PATH.exists()
    assert CAPTURE_SCREENSHOT_PATH.exists()

    report = CAPTURE_REPORT_PATH.read_text(encoding="utf-8")
    fields = parse_report_fields(report)
    captured_text = CAPTURE_TEXT_PATH.read_text(encoding="utf-8")

    assert fields["input_url"] == input_url
    assert fields["final_url"] == input_url
    assert fields["title"] == "Stage 6 Fixture Title"
    assert fields["request_id"]
    assert fields["trace_id"]
    assert fields["text_bytes"] == str(direct_body["text_bytes"])
    assert fields["text_truncated"] == str(direct_body["text_truncated"])
    assert captured_text == direct_body["rendered_text"]
    assert CAPTURE_SCREENSHOT_PATH.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")

    summary = json.loads(
        (WORKSPACE_ROOT / "run_outputs" / "latest_seed_run.json").read_text(encoding="ascii")
    )
    assert summary["input_url"] == input_url
    assert any(step["kind"] == "bridge_browser_render" for step in summary["steps"])

    events = load_events()
    matched = [
        event
        for event in events
        if event["event_type"] == "browser_render"
        and event["request_id"] == fields["request_id"]
        and event["trace_id"] == fields["trace_id"]
    ]
    assert matched

    reset = run_command(
        ["./scripts/recovery.sh", "reset-workspace-to-seed-baseline"],
        env=compose_stack,
    )
    assert reset.returncode == 0
    assert not CAPTURE_REPORT_PATH.exists()
    assert not CAPTURE_TEXT_PATH.exists()
    assert not CAPTURE_SCREENSHOT_PATH.exists()


def test_seed_runner_single_source_answer_packet_writes_artifacts_logs_llm_and_resets(compose_stack):
    input_url = "http://allowed.test/browser/rendered"
    question = "What does this page say?"
    direct = compose_http_response(
        "agent",
        "POST",
        "http://bridge:8000/web/browser/render",
        env=compose_stack,
        payload={"url": input_url},
        headers=agent_auth_headers(),
    )
    assert direct["status_code"] == 200
    direct_body = direct["json"]

    result = compose_exec(
        "agent",
        [
            "python",
            "-m",
            "untrusted.agent.seed_runner",
            "--task",
            question,
            "--planner",
            "scripted",
            "--script",
            ".seed_plans/stage6_answer_packet.json",
            "--input-url",
            input_url,
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

    answer = ANSWER_REPORT_PATH.read_text(encoding="utf-8")
    capture = CAPTURE_REPORT_PATH.read_text(encoding="utf-8")
    captured_text = CAPTURE_TEXT_PATH.read_text(encoding="utf-8")
    answer_fields = parse_report_fields(answer)
    capture_fields = parse_report_fields(capture)

    assert answer_fields["question"] == question
    assert answer_fields["input_url"] == input_url
    assert answer_fields["final_url"] == input_url
    assert answer_fields["title"] == "Stage 6 Fixture Title"
    assert answer_fields["request_id"]
    assert answer_fields["trace_id"]
    assert answer_fields["text_bytes"] == str(direct_body["text_bytes"])
    assert answer_fields["text_truncated"] == str(direct_body["text_truncated"])
    assert answer_fields["llm_model"] == "stage1-deterministic"
    assert "stage1 deterministic reply:" in answer
    assert question in answer
    assert capture_fields["request_id"] == answer_fields["request_id"]
    assert capture_fields["trace_id"] == answer_fields["trace_id"]
    assert captured_text == direct_body["rendered_text"]
    assert CAPTURE_SCREENSHOT_PATH.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")

    summary = json.loads(
        (WORKSPACE_ROOT / "run_outputs" / "latest_seed_run.json").read_text(encoding="ascii")
    )
    assert summary["input_url"] == input_url
    assert any(step["kind"] == "bridge_browser_render" for step in summary["steps"])
    assert any(step["kind"] == "bridge_chat" for step in summary["steps"])

    events = load_events()
    assert any(
        event["event_type"] == "browser_render"
        and event["request_id"] == answer_fields["request_id"]
        and event["trace_id"] == answer_fields["trace_id"]
        for event in events
    )
    assert any(
        event["event_type"] == "llm_call"
        and event["actor"] == "agent"
        and event["outcome"] == "success"
        and event["summary"]["model"] == "stage1-deterministic"
        for event in events
    )
    assert any(
        event["event_type"] == "budget_update"
        and event["outcome"] == "success"
        and event["summary"]["action"] == "mediated_llm_usage_accounted"
        for event in events
    )
    assert any(
        event["event_type"] == "agent_run"
        and event["summary"]["event_kind"] == "run_end"
        and event["summary"]["reported_origin"] == "untrusted_agent"
        for event in events
    )

    reset = run_command(
        ["./scripts/recovery.sh", "reset-workspace-to-seed-baseline"],
        env=compose_stack,
    )
    assert reset.returncode == 0
    assert not ANSWER_REPORT_PATH.exists()
    assert not CAPTURE_REPORT_PATH.exists()
    assert not CAPTURE_TEXT_PATH.exists()
    assert not CAPTURE_SCREENSHOT_PATH.exists()


def test_seed_runner_follow_answer_packet_writes_artifacts_logs_llm_and_resets(compose_stack):
    source_url = "http://allowed.test/browser/follow-source"
    follow_target_url = "http://allowed.test/browser/follow-target"
    question = "What does the followed page say?"
    direct = compose_http_response(
        "agent",
        "POST",
        "http://bridge:8000/web/browser/follow-href",
        env=compose_stack,
        payload={"source_url": source_url, "target_url": follow_target_url},
        headers=agent_auth_headers(),
    )
    assert direct["status_code"] == 200
    direct_body = direct["json"]

    result = compose_exec(
        "agent",
        [
            "python",
            "-m",
            "untrusted.agent.seed_runner",
            "--task",
            question,
            "--planner",
            "scripted",
            "--script",
            ".seed_plans/stage6_follow_answer_packet.json",
            "--input-url",
            source_url,
            "--follow-target-url",
            follow_target_url,
            "--max-steps",
            "10",
        ],
        env=compose_stack,
    )
    payload = json.loads(result.stdout)
    assert payload["success"] is True
    assert payload["input_url"] == source_url
    assert payload["follow_target_url"] == follow_target_url
    assert FOLLOW_ANSWER_REPORT_PATH.exists()
    assert FOLLOW_CAPTURE_REPORT_PATH.exists()
    assert FOLLOW_TEXT_PATH.exists()
    assert FOLLOW_CAPTURE_SCREENSHOT_PATH.exists()

    answer = FOLLOW_ANSWER_REPORT_PATH.read_text(encoding="utf-8")
    capture = FOLLOW_CAPTURE_REPORT_PATH.read_text(encoding="utf-8")
    captured_text = FOLLOW_TEXT_PATH.read_text(encoding="utf-8")
    answer_fields = parse_report_fields(answer)
    capture_fields = parse_report_fields(capture)

    assert answer_fields["question"] == question
    assert answer_fields["source_input_url"] == source_url
    assert answer_fields["source_final_url"] == source_url
    assert answer_fields["requested_target_url"] == follow_target_url
    assert answer_fields["matched_link_text"] == "Follow same origin target"
    assert answer_fields["followed_final_url"] == follow_target_url
    assert answer_fields["title"] == direct_body["page_title"]
    assert answer_fields["request_id"]
    assert answer_fields["trace_id"]
    assert answer_fields["text_bytes"] == str(direct_body["text_bytes"])
    assert answer_fields["text_truncated"] == str(direct_body["text_truncated"])
    assert answer_fields["llm_model"] == "stage1-deterministic"
    assert "stage1 deterministic reply:" in answer
    assert question in answer
    assert capture_fields["request_id"] == answer_fields["request_id"]
    assert capture_fields["trace_id"] == answer_fields["trace_id"]
    assert capture_fields["matched_link_text"] == "Follow same origin target"
    assert captured_text == direct_body["rendered_text"]
    assert FOLLOW_CAPTURE_SCREENSHOT_PATH.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")

    summary = json.loads(
        (WORKSPACE_ROOT / "run_outputs" / "latest_seed_run.json").read_text(encoding="ascii")
    )
    assert summary["input_url"] == source_url
    assert summary["follow_target_url"] == follow_target_url
    assert any(step["kind"] == "bridge_browser_render" for step in summary["steps"])
    assert any(step["kind"] == "bridge_browser_follow_href" for step in summary["steps"])
    assert any(step["kind"] == "bridge_chat" for step in summary["steps"])

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
    assert browser_event["summary"]["screenshot_sha256"]
    assert direct_body["rendered_text"] not in json.dumps(browser_event)
    assert direct_body["screenshot_png_base64"] not in json.dumps(browser_event)
    assert any(
        event["event_type"] == "llm_call"
        and event["actor"] == "agent"
        and event["outcome"] == "success"
        and event["summary"]["model"] == "stage1-deterministic"
        for event in events
    )
    assert any(
        event["event_type"] == "budget_update"
        and event["outcome"] == "success"
        and event["summary"]["action"] == "mediated_llm_usage_accounted"
        for event in events
    )
    assert any(
        event["event_type"] == "agent_run"
        and event["summary"]["event_kind"] == "run_end"
        and event["summary"]["reported_origin"] == "untrusted_agent"
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


def test_seed_runner_browser_follow_demo_writes_artifacts_and_recovery_resets_them(compose_stack):
    result = compose_exec(
        "agent",
        [
            "python",
            "-m",
            "untrusted.agent.seed_runner",
            "--task",
            "follow one safe href and write a browser report",
            "--planner",
            "scripted",
            "--script",
            ".seed_plans/stage6b_browser_follow_demo.json",
            "--max-steps",
            "10",
        ],
        env=compose_stack,
    )
    payload = json.loads(result.stdout)
    assert payload["success"] is True
    assert FOLLOW_REPORT_PATH.exists()
    assert FOLLOW_SCREENSHOT_PATH.exists()
    report = FOLLOW_REPORT_PATH.read_text(encoding="utf-8")
    assert "http://allowed.test/browser/follow-source" in report
    assert "http://allowed.test/browser/follow-target" in report
    assert "Stage 6B Same Origin Target" in report
    assert "request_id=" in report
    assert "trace_id=" in report
    assert FOLLOW_SCREENSHOT_PATH.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")

    reset = run_command(
        ["./scripts/recovery.sh", "reset-workspace-to-seed-baseline"],
        env=compose_stack,
    )
    assert reset.returncode == 0
    assert not FOLLOW_REPORT_PATH.exists()
    assert not FOLLOW_SCREENSHOT_PATH.exists()

    state = load_state()
    assert state["browser"]["counters"]["browser_follow_href_success"] >= 1
