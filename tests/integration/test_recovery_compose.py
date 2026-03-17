import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PROJECT = "rsi_stage4_recovery_test"
LOG_PATH = ROOT / "runtime" / "trusted_state" / "logs" / "bridge_events.jsonl"
STATE_PATH = ROOT / "runtime" / "trusted_state" / "state" / "operational_state.json"
CHECKPOINT_DIR = ROOT / "runtime" / "trusted_state" / "checkpoints"
WORKSPACE_ROOT = ROOT / "untrusted" / "agent_workspace"
BASELINE_ROOT = ROOT / "trusted" / "recovery" / "seed_workspace_baseline"

TEST_AGENT_TOKEN = "rsi-agent-token-dev-sentinel"
TEST_OPERATOR_TOKEN = "rsi-operator-token-dev-sentinel"


def agent_auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_AGENT_TOKEN}"}


def operator_auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_OPERATOR_TOKEN}"}


def docker_env() -> dict[str, str]:
    env = os.environ.copy()
    env["COMPOSE_PROJECT_NAME"] = COMPOSE_PROJECT
    env["RSI_LLM_BUDGET_TOKEN_CAP"] = "120"
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


def compose_http_json(
    service: str,
    method: str,
    url: str,
    *,
    env: dict[str, str],
    headers: dict | None = None,
) -> dict:
    code = (
        "import httpx, json\n"
        f"method = {method!r}\n"
        f"url = {url!r}\n"
        f"headers = {json.dumps(headers or {})!r}\n"
        "with httpx.Client(timeout=10.0) as client:\n"
        "    response = client.request(method, url, headers=json.loads(headers))\n"
        "response.raise_for_status()\n"
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


def load_state() -> dict:
    assert STATE_PATH.exists(), STATE_PATH
    return json.loads(STATE_PATH.read_text(encoding="ascii"))


def run_recovery(*args: str) -> dict:
    result = run_command(["./scripts/recovery.sh", *args])
    return json.loads(result.stdout)


@pytest.fixture(scope="module")
def compose_stack():
    env = docker_env()
    docker_ready = run_command(["docker", "info"], env=env, check=False)
    if docker_ready.returncode != 0:
        pytest.fail("Docker daemon is required for Stage 4 recovery tests")

    compose_command(["down", "--remove-orphans", "--volumes"], env=env, check=False)
    if CHECKPOINT_DIR.exists():
        shutil.rmtree(CHECKPOINT_DIR)
    if LOG_PATH.exists():
        LOG_PATH.unlink()
    if STATE_PATH.exists():
        STATE_PATH.unlink()
    run_recovery("reset-workspace-to-seed-baseline")

    compose_command(["up", "--build", "-d", "--wait"], env=env)
    yield env
    compose_command(["down", "--remove-orphans", "--volumes"], env=env, check=False)
    run_recovery("reset-workspace-to-seed-baseline")


def test_recovery_controls_revert_workspace_and_update_trusted_state(compose_stack):
    seedlib_path = WORKSPACE_ROOT / "seedlib.py"
    baseline_seedlib = (BASELINE_ROOT / "seedlib.py").read_text(encoding="ascii")
    edited_seedlib = "def render_seed_status(task_name: str) -> str:\n    task_name = task_name.strip().upper()\n    assert task_name\n    return f\"seed agent ready: {task_name}\"\n"
    broken_seedlib = "def render_seed_status(task_name: str) -> str:\n    return 'broken'\n"

    seedlib_path.write_text(edited_seedlib, encoding="ascii")
    created = run_recovery("create-checkpoint", "--label", "after-local-edit")
    checkpoint_id = created["checkpoint_id"]
    assert checkpoint_id
    assert Path(created["archive_path"]).is_file()
    assert Path(created["manifest_path"]).is_file()
    assert Path(created["archive_path"]).is_relative_to(CHECKPOINT_DIR)
    assert not (WORKSPACE_ROOT / "checkpoints").exists()

    listed = run_recovery("list-checkpoints")
    assert listed["baseline"]["baseline_id"]
    assert any(item["checkpoint_id"] == checkpoint_id for item in listed["checkpoints"])

    seedlib_path.write_text(broken_seedlib, encoding="ascii")
    (WORKSPACE_ROOT / "scratch.txt").write_text("temporary\n", encoding="ascii")
    restored = run_recovery("restore-checkpoint", checkpoint_id)
    assert restored["checkpoint_id"] == checkpoint_id
    assert seedlib_path.read_text(encoding="ascii") == edited_seedlib
    assert not (WORKSPACE_ROOT / "scratch.txt").exists()

    (WORKSPACE_ROOT / "extra.py").write_text("x = 1\n", encoding="ascii")
    reset = run_recovery("reset-workspace-to-seed-baseline")
    assert reset["baseline_id"] == listed["baseline"]["baseline_id"]
    assert seedlib_path.read_text(encoding="ascii") == baseline_seedlib
    assert not (WORKSPACE_ROOT / "extra.py").exists()

    host_checkpoint_paths_before = sorted(
        path.relative_to(CHECKPOINT_DIR).as_posix()
        for path in CHECKPOINT_DIR.rglob("*")
        if path.is_file()
    )
    agent_write_attempt = (
        "from pathlib import Path\n"
        "path = Path('/var/lib/rsi/trusted_state/checkpoints/agent-hack.txt')\n"
        "path.parent.mkdir(parents=True, exist_ok=True)\n"
        "path.write_text('agent mutation', encoding='ascii')\n"
        "print(path.exists())\n"
    )
    blocked = compose_exec(
        "agent",
        ["python", "-c", agent_write_attempt],
        env=compose_stack,
        check=False,
    )
    assert blocked.returncode != 0
    host_checkpoint_paths_after = sorted(
        path.relative_to(CHECKPOINT_DIR).as_posix()
        for path in CHECKPOINT_DIR.rglob("*")
        if path.is_file()
    )
    assert host_checkpoint_paths_after == host_checkpoint_paths_before
    assert not (CHECKPOINT_DIR / "agent-hack.txt").exists()

    status = compose_http_json(
        "bridge",
        "GET",
        "http://127.0.0.1:8000/status",
        env=compose_stack,
        headers=operator_auth_headers(),
    )
    assert status["recovery"]["baseline_id"] == listed["baseline"]["baseline_id"]
    assert status["recovery"]["latest_checkpoint_id"] == checkpoint_id
    assert status["recovery"]["current_workspace_status"] == "seed_baseline"
    assert status["recovery"]["latest_action"]["action"] == "workspace_reset"

    state = load_state()
    assert state["recovery"]["latest_checkpoint_id"] == checkpoint_id
    assert state["recovery"]["current_workspace_status"] == "seed_baseline"
    assert state["recovery"]["latest_action"]["action"] == "workspace_reset"
    assert any(
        item["checkpoint_id"] == checkpoint_id
        for item in state["recovery"]["available_checkpoints"]
    )

    events = load_events()
    assert any(
        event["event_type"] == "checkpoint_created"
        and event["summary"]["checkpoint"]["checkpoint_id"] == checkpoint_id
        and event["actor"] == "operator"
        for event in events
    )
    assert any(
        event["event_type"] == "checkpoint_restored"
        and event["summary"]["checkpoint"]["checkpoint_id"] == checkpoint_id
        for event in events
    )
    assert any(
        event["event_type"] == "workspace_reset"
        and event["summary"]["baseline_id"] == listed["baseline"]["baseline_id"]
        for event in events
    )


def test_recovery_artifacts_survive_compose_down_up(compose_stack):
    checkpoint = run_recovery("create-checkpoint", "--label", "restart-check")
    checkpoint_id = checkpoint["checkpoint_id"]

    compose_command(["down", "--remove-orphans"], env=compose_stack)
    compose_command(["up", "--build", "-d", "--wait"], env=compose_stack)

    listed = run_recovery("list-checkpoints")
    assert any(item["checkpoint_id"] == checkpoint_id for item in listed["checkpoints"])

    status = compose_http_json(
        "bridge",
        "GET",
        "http://127.0.0.1:8000/status",
        env=compose_stack,
        headers=operator_auth_headers(),
    )
    assert status["recovery"]["latest_checkpoint_id"] == checkpoint_id
    assert any(
        item["checkpoint_id"] == checkpoint_id
        for item in status["recovery"]["available_checkpoints"]
    )


def test_recovery_cli_and_bridge_requests_do_not_corrupt_canonical_state(compose_stack):
    bridge_burst = (
        "import httpx\n"
        f"headers = {json.dumps(agent_auth_headers())}\n"
        "with httpx.Client(base_url='http://bridge:8000', timeout=5.0, headers=headers) as client:\n"
        "    for _ in range(12):\n"
        "        response = client.get('/status')\n"
        "        response.raise_for_status()\n"
    )
    process = subprocess.Popen(
        ["docker", "compose", "exec", "-T", "agent", "python", "-c", bridge_burst],
        cwd=ROOT,
        env=compose_stack,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    checkpoint = run_recovery("create-checkpoint", "--label", "concurrent-bridge")
    stdout, stderr = process.communicate(timeout=20)
    assert process.returncode == 0, stdout + stderr
    assert checkpoint["checkpoint_id"]

    events = load_events()
    assert any(
        event["event_type"] == "checkpoint_created"
        and event["summary"]["checkpoint"]["checkpoint_id"] == checkpoint["checkpoint_id"]
        for event in events
    )
    assert any(event["event_type"] == "status_query" for event in events)
    assert all(event["request_id"] for event in events)
    assert all(event["trace_id"] for event in events)

    state = load_state()
    assert state["counters"]["status_queries"] >= 12
    assert state["recovery"]["latest_checkpoint_id"] == checkpoint["checkpoint_id"]
