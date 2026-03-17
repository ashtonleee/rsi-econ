import json
import os
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PROJECT = "rsi_stage2_test"
LOG_PATH = ROOT / "runtime" / "trusted_state" / "logs" / "bridge_events.jsonl"
STATE_PATH = ROOT / "runtime" / "trusted_state" / "state" / "operational_state.json"
SENTINEL_PROVIDER_KEY = "stage1-sentinel-provider-key"
BUDGET_CAP = 40

TEST_AGENT_TOKEN = "rsi-agent-token-dev-sentinel"
TEST_OPERATOR_TOKEN = "rsi-operator-token-dev-sentinel"


def agent_auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_AGENT_TOKEN}"}


def operator_auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_OPERATOR_TOKEN}"}


def docker_env() -> dict[str, str]:
    env = os.environ.copy()
    env["COMPOSE_PROJECT_NAME"] = COMPOSE_PROJECT
    env["RSI_LLM_BUDGET_TOKEN_CAP"] = str(BUDGET_CAP)
    env["RSI_ENABLE_DEBUG_PROBES"] = "1"
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


def compose_http_json(
    service: str,
    method: str,
    url: str,
    *,
    env: dict[str, str],
    headers: dict | None = None,
) -> dict:
    return compose_http_response(service, method, url, env=env, headers=headers)["json"]


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
        "with httpx.Client(timeout=10.0) as client:\n"
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


def expect_failure_via_agent(target_url: str, env: dict[str, str]):
    code = (
        "import sys, urllib.request\n"
        f"url = {target_url!r}\n"
        "try:\n"
        "    urllib.request.urlopen(url, timeout=2).read()\n"
        "except Exception as exc:\n"
        "    print(type(exc).__name__)\n"
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
        pytest.fail("Docker daemon is required for Stage 2 boundary tests")

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


def test_compose_stack_starts_and_reports_litellm_health(compose_stack):
    health = compose_http_json(
        "bridge",
        "GET",
        "http://127.0.0.1:8000/healthz",
        env=compose_stack,
    )
    assert health["details"]["litellm_reachable"] is True
    assert health["details"]["egress_reachable"] is True

    body = compose_http_json(
        "bridge",
        "GET",
        "http://127.0.0.1:8000/status",
        env=compose_stack,
        headers=operator_auth_headers(),
    )
    assert body["connections"]["litellm"]["reachable"] is True
    assert body["connections"]["egress"]["reachable"] is True
    assert body["log_path"].endswith("bridge_events.jsonl")
    assert body["operational_state_path"].endswith("operational_state.json")
    assert body["budget"]["total"] == BUDGET_CAP
    assert body["budget"]["remaining"] == BUDGET_CAP
    assert body["surfaces"]["canonical_logging"] == "active_canonical_event_log"

    agent_status = compose_exec(
        "agent",
        [
            "python",
            "-m",
            "untrusted.agent.bridge_client",
            "--bridge-url",
            "http://bridge:8000",
            "status",
        ],
        env=compose_stack,
    )
    agent_body = json.loads(agent_status.stdout)
    assert agent_body["budget"]["remaining"] == BUDGET_CAP

    events = load_events()
    assert any(event["event_type"] == "status_query" for event in events)
    assert any(event["event_type"] == "status_query" and event["actor"] == "operator" for event in events)


def test_boundary_denies_direct_egress_and_allows_bridge_mediated_llm(compose_stack):
    expect_failure_via_agent("http://1.1.1.1", compose_stack)
    expect_failure_via_agent("https://api.openai.com/v1/models", compose_stack)
    expect_failure_via_agent("http://litellm:4000/healthz", compose_stack)
    expect_failure_via_agent("http://egress:8084/healthz", compose_stack)

    public_probe = compose_http_json(
        "bridge",
        "POST",
        "http://127.0.0.1:8000/debug/probes/public-egress",
        env=compose_stack,
        headers=operator_auth_headers(),
    )
    provider_probe = compose_http_json(
        "bridge",
        "POST",
        "http://127.0.0.1:8000/debug/probes/provider-egress",
        env=compose_stack,
        headers=operator_auth_headers(),
    )

    assert public_probe["outcome"] == "denied"
    assert provider_probe["outcome"] == "denied"

    chat = compose_http_response(
        "agent",
        "POST",
        "http://bridge:8000/llm/chat/completions",
        env=compose_stack,
        payload={
            "model": "stage2-deterministic",
            "messages": [{"role": "user", "content": "stage2 boundary proof"}],
        },
        headers=agent_auth_headers(),
    )
    assert chat["status_code"] == 200
    payload = chat["json"]
    assert payload["choices"][0]["message"]["content"] == "stage1 deterministic reply: stage2 boundary proof"
    assert chat["headers"]["x-request-id"]
    assert chat["headers"]["x-trace-id"]

    events = load_events()
    assert any(
        event["event_type"] == "llm_call"
        and event["outcome"] == "success"
        and event["request_id"] == chat["headers"]["x-request-id"]
        and event["trace_id"] == chat["headers"]["x-trace-id"]
        for event in events
    )
    assert any(
        event["event_type"] == "budget_update"
        and event["request_id"] == chat["headers"]["x-request-id"]
        for event in events
    )
    assert any(
        event["event_type"] == "system"
        and event["outcome"] == "denied"
        and event["summary"]["action"] == "direct_egress_probe"
        and event["summary"]["probe_kind"] == "public"
        for event in events
    )
    assert any(
        event["event_type"] == "system"
        and event["outcome"] == "denied"
        and event["summary"]["action"] == "direct_egress_probe"
        and event["summary"]["probe_kind"] == "provider"
        for event in events
    )
    assert all(event.get("request_id") for event in events)
    assert all(event.get("trace_id") for event in events)

    state = load_state()
    assert state["budget"]["spent"] > 0
    assert state["budget"]["remaining"] < BUDGET_CAP
    assert state["counters"]["llm_calls_total"] >= 1
    assert any(
        item["request_id"] == chat["headers"]["x-request-id"]
        for item in state["recent_requests"]
    )


def test_litellm_has_trusted_provider_egress_attachment(compose_stack):
    container_id = compose_command(["ps", "-q", "litellm"], env=compose_stack).stdout.strip()
    inspect = json.loads(run_command(["docker", "inspect", container_id]).stdout)[0]
    networks = inspect["NetworkSettings"]["Networks"]

    assert f"{COMPOSE_PROJECT}_trusted_net" in networks
    assert f"{COMPOSE_PROJECT}_egress_net" in networks
    assert networks[f"{COMPOSE_PROJECT}_egress_net"]["Gateway"]


def test_budget_cap_denies_further_llm_calls_and_logs_denial(compose_stack):
    status = compose_http_json(
        "bridge",
        "GET",
        "http://127.0.0.1:8000/status",
        env=compose_stack,
        headers=operator_auth_headers(),
    )
    while status["budget"]["exhausted"] is False:
        attempt = compose_http_response(
            "agent",
            "POST",
            "http://bridge:8000/llm/chat/completions",
            env=compose_stack,
            payload={
                "model": "stage2-deterministic",
                "messages": [{"role": "user", "content": "x"}],
            },
            headers=agent_auth_headers(),
        )
        if attempt["status_code"] != 200:
            break
        status = compose_http_json(
            "bridge",
            "GET",
            "http://127.0.0.1:8000/status",
            env=compose_stack,
            headers=operator_auth_headers(),
        )

    assert status["budget"]["exhausted"] is True
    denied = compose_http_response(
        "agent",
        "POST",
        "http://bridge:8000/llm/chat/completions",
        env=compose_stack,
        payload={
            "model": "stage2-deterministic",
            "messages": [{"role": "user", "content": "x"}],
        },
        headers=agent_auth_headers(),
    )
    assert denied["status_code"] == 402
    assert denied["headers"]["x-request-id"]
    assert denied["headers"]["x-trace-id"]
    assert "budget exhausted" in denied["json"]["detail"]

    events = load_events()
    assert any(
        event["event_type"] == "llm_call"
        and event["outcome"] == "denied"
        and event["request_id"] == denied["headers"]["x-request-id"]
        for event in events
    )
    assert any(
        event["event_type"] == "budget_update"
        and event["outcome"] == "denied"
        and event["request_id"] == denied["headers"]["x-request-id"]
        for event in events
    )


def test_agent_container_has_no_provider_secret_exposure(compose_stack):
    env_dump = compose_exec("agent", ["env"], env=compose_stack)
    assert SENTINEL_PROVIDER_KEY not in env_dump.stdout
    assert "OPENAI_API_KEY" not in env_dump.stdout

    container_id = compose_command(["ps", "-q", "agent"], env=compose_stack).stdout.strip()
    inspect = json.loads(run_command(["docker", "inspect", container_id]).stdout)[0]

    inspect_env = "\n".join(inspect["Config"]["Env"])
    assert SENTINEL_PROVIDER_KEY not in inspect_env
    assert "OPENAI_API_KEY=" not in inspect_env
    assert all(
        "/var/lib/rsi/trusted_state" not in mount["Destination"]
        for mount in inspect["Mounts"]
    )

    file_scan = (
        "import json, os, sys\n"
        f"needle = {SENTINEL_PROVIDER_KEY!r}\n"
        "matches = []\n"
        "for root in ['/app', '/workspace/agent']:\n"
        "    for dirpath, _, filenames in os.walk(root):\n"
        "        for name in filenames:\n"
        "            path = os.path.join(dirpath, name)\n"
        "            try:\n"
        "                with open(path, 'r', encoding='utf-8', errors='ignore') as handle:\n"
        "                    data = handle.read()\n"
        "            except Exception:\n"
        "                continue\n"
        "            if needle in data or 'OPENAI_API_KEY' in data:\n"
        "                matches.append(path)\n"
        "print(json.dumps(matches))\n"
        "sys.exit(0 if not matches else 1)\n"
    )
    scan = compose_exec("agent", ["python", "-c", file_scan], env=compose_stack, check=False)
    assert scan.returncode == 0, scan.stdout + scan.stderr


def test_agent_can_query_status_but_cannot_modify_trusted_state(compose_stack):
    status = compose_exec(
        "agent",
        [
            "python",
            "-m",
            "untrusted.agent.bridge_client",
            "--bridge-url",
            "http://bridge:8000",
            "status",
        ],
        env=compose_stack,
    )
    body = json.loads(status.stdout)
    assert "budget" in body
    assert "operational_state_path" in body

    host_log_before = LOG_PATH.read_text(encoding="ascii")
    host_state_before = STATE_PATH.read_text(encoding="ascii")
    write_attempt = (
        "from pathlib import Path\n"
        "import json\n"
        "log_path = Path('/var/lib/rsi/trusted_state/logs/bridge_events.jsonl')\n"
        "state_path = Path('/var/lib/rsi/trusted_state/state/operational_state.json')\n"
        "log_path.parent.mkdir(parents=True, exist_ok=True)\n"
        "state_path.parent.mkdir(parents=True, exist_ok=True)\n"
        "with log_path.open('a', encoding='ascii') as handle:\n"
        "    handle.write('{\"event_type\":\"agent_fake\"}\\n')\n"
        "state_path.write_text(json.dumps({'budget': 'mutated'}), encoding='ascii')\n"
    )
    blocked = compose_exec(
        "agent",
        ["python", "-c", write_attempt],
        env=compose_stack,
        check=False,
    )
    assert blocked.returncode != 0
    assert LOG_PATH.read_text(encoding="ascii") == host_log_before
    assert STATE_PATH.read_text(encoding="ascii") == host_state_before

    mutate = compose_http_response(
        "agent",
        "POST",
        "http://bridge:8000/status",
        env=compose_stack,
        payload={"budget": "tamper"},
        headers=agent_auth_headers(),
    )
    assert mutate["status_code"] == 405


def test_spoofed_actor_headers_do_not_change_canonical_actor_and_runtime_root_is_read_only(compose_stack):
    status = compose_http_response(
        "agent",
        "GET",
        "http://bridge:8000/status",
        env=compose_stack,
        headers={**agent_auth_headers(), "x-rsi-actor": "operator"},
    )
    assert status["status_code"] == 200

    events = load_events()
    assert any(
        event["event_type"] == "status_query"
        and event["request_id"] == status["headers"]["x-request-id"]
        and event["actor"] == "agent"
        for event in events
    )

    reported = compose_http_response(
        "agent",
        "POST",
        "http://bridge:8000/agent/runs/events",
        env=compose_stack,
        headers={**agent_auth_headers(), "x-rsi-actor": "operator"},
        payload={
            "run_id": "actor-spoof-run",
            "event_kind": "run_start",
            "step_index": None,
            "tool_name": None,
            "summary": {"task": "actor spoof hardening"},
        },
    )
    assert reported["status_code"] == 200
    events = load_events()
    assert any(
        event["event_type"] == "agent_run"
        and event["request_id"] == reported["headers"]["x-request-id"]
        and event["actor"] == "agent"
        for event in events
    )

    runtime_write = (
        "from pathlib import Path\n"
        "import json\n"
        "workspace = Path('/workspace/agent/h1_boundary_probe.txt')\n"
        "workspace.write_text('ok\\n', encoding='utf-8')\n"
        "payload = {'workspace_exists': workspace.exists()}\n"
        "try:\n"
        "    Path('/app/untrusted/h1_boundary_probe.txt').write_text('bad\\n', encoding='utf-8')\n"
        "    payload['runtime_write'] = 'succeeded'\n"
        "except Exception as exc:\n"
        "    payload['runtime_write'] = type(exc).__name__\n"
        "print(json.dumps(payload))\n"
    )
    probe = compose_exec("agent", ["python", "-c", runtime_write], env=compose_stack)
    probe_payload = json.loads(probe.stdout)
    assert probe_payload["workspace_exists"] is True
    assert probe_payload["runtime_write"] != "succeeded"


def test_operational_state_persists_across_bridge_restart(compose_stack):
    before = compose_http_json(
        "bridge",
        "GET",
        "http://127.0.0.1:8000/status",
        env=compose_stack,
        headers=operator_auth_headers(),
    )
    compose_command(["restart", "bridge"], env=compose_stack)
    compose_command(["up", "-d", "--wait", "bridge"], env=compose_stack)
    after = compose_http_json(
        "bridge",
        "GET",
        "http://127.0.0.1:8000/status",
        env=compose_stack,
        headers=operator_auth_headers(),
    )

    assert after["budget"]["spent"] == before["budget"]["spent"]
    assert after["budget"]["remaining"] == before["budget"]["remaining"]
    assert after["counters"]["llm_calls_total"] == before["counters"]["llm_calls_total"]
