import json
import os
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PROJECT = "rsi_stage1_test"
LOG_PATH = ROOT / "runtime" / "trusted_state" / "logs" / "bridge_events.jsonl"
SENTINEL_PROVIDER_KEY = "stage1-sentinel-provider-key"


def docker_env() -> dict[str, str]:
    env = os.environ.copy()
    env["COMPOSE_PROJECT_NAME"] = COMPOSE_PROJECT
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
) -> dict:
    code = (
        "import httpx, json\n"
        f"method = {method!r}\n"
        f"url = {url!r}\n"
        "with httpx.Client(timeout=5.0) as client:\n"
        "    response = client.request(method, url)\n"
        "    response.raise_for_status()\n"
        "print(json.dumps(response.json()))\n"
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
        pytest.fail("Docker daemon is required for Stage 1 boundary tests")

    compose_command(["down", "--remove-orphans", "--volumes"], env=env, check=False)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if LOG_PATH.exists():
        LOG_PATH.unlink()

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

    body = compose_http_json(
        "bridge",
        "GET",
        "http://127.0.0.1:8000/status",
        env=compose_stack,
    )
    assert body["connections"]["litellm"]["reachable"] is True
    assert body["log_path"].endswith("bridge_events.jsonl")


def test_boundary_denies_direct_egress_and_allows_bridge_mediated_llm(compose_stack):
    expect_failure_via_agent("http://1.1.1.1", compose_stack)
    expect_failure_via_agent("https://api.openai.com/v1/models", compose_stack)
    expect_failure_via_agent("http://litellm:4000/healthz", compose_stack)

    public_probe = compose_http_json(
        "bridge",
        "POST",
        "http://127.0.0.1:8000/debug/probes/public-egress",
        env=compose_stack,
    )
    provider_probe = compose_http_json(
        "bridge",
        "POST",
        "http://127.0.0.1:8000/debug/probes/provider-egress",
        env=compose_stack,
    )

    assert public_probe["outcome"] == "denied"
    assert provider_probe["outcome"] == "denied"

    chat = compose_exec(
        "agent",
        [
            "python",
            "-m",
            "untrusted.agent.bridge_client",
            "--bridge-url",
            "http://bridge:8000",
            "chat",
            "--message",
            "stage1 boundary proof",
        ],
        env=compose_stack,
    )
    payload = json.loads(chat.stdout)
    assert payload["choices"][0]["message"]["content"] == "stage1 deterministic reply: stage1 boundary proof"

    events = load_events()
    assert any(
        event["event_type"] == "mediated_llm_request" and event["outcome"] == "allow"
        for event in events
    )
    assert any(
        event["event_type"] == "mediated_llm_response" and event["outcome"] == "success"
        for event in events
    )
    assert any(
        event["event_type"] == "direct_egress_probe"
        and event["outcome"] == "denied"
        and event.get("probe_kind") == "public"
        for event in events
    )
    assert any(
        event["event_type"] == "direct_egress_probe"
        and event["outcome"] == "denied"
        and event.get("probe_kind") == "provider"
        for event in events
    )
    assert all(event.get("request_id") for event in events)


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
