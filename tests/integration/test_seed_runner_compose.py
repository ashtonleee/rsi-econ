import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PROJECT = "rsi_stage3_seed_test"
LOG_PATH = ROOT / "runtime" / "trusted_state" / "logs" / "bridge_events.jsonl"
STATE_PATH = ROOT / "runtime" / "trusted_state" / "state" / "operational_state.json"
WORKSPACE_ROOT = ROOT / "untrusted" / "agent_workspace"
RUN_OUTPUTS = WORKSPACE_ROOT / "run_outputs"
BUDGET_CAP = 120


def docker_env() -> dict[str, str]:
    env = os.environ.copy()
    env["COMPOSE_PROJECT_NAME"] = COMPOSE_PROJECT
    env["RSI_LLM_BUDGET_TOKEN_CAP"] = str(BUDGET_CAP)
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
        pytest.fail("Docker daemon is required for Stage 3 seed runner tests")

    compose_command(["down", "--remove-orphans", "--volumes"], env=env, check=False)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if LOG_PATH.exists():
        LOG_PATH.unlink()
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if STATE_PATH.exists():
        STATE_PATH.unlink()
    if RUN_OUTPUTS.exists():
        shutil.rmtree(RUN_OUTPUTS)

    compose_command(["up", "--build", "-d", "--wait"], env=env)
    yield env
    compose_command(["down", "--remove-orphans", "--volumes"], env=env, check=False)
    if RUN_OUTPUTS.exists():
        shutil.rmtree(RUN_OUTPUTS)


def test_seed_runner_uses_workspace_mount_and_bridge_surfaces(compose_stack):
    layout_probe = (
        "from pathlib import Path\n"
        "import json\n"
        "payload = {\n"
        "    'workspace_root': str(Path('/workspace/agent').resolve()),\n"
        "    'runtime_root': str(Path('/app/untrusted').resolve()),\n"
        "    'workspace_has_seedlib': Path('/workspace/agent/seedlib.py').exists(),\n"
        "    'runtime_has_seed_runner': Path('/app/untrusted/agent/seed_runner.py').exists(),\n"
        "}\n"
        "print(json.dumps(payload))\n"
    )
    probe = compose_exec("agent", ["python", "-c", layout_probe], env=compose_stack)
    probe_payload = json.loads(probe.stdout)
    assert probe_payload["workspace_root"] == "/workspace/agent"
    assert probe_payload["runtime_root"] == "/app/untrusted"
    assert probe_payload["workspace_has_seedlib"] is True
    assert probe_payload["runtime_has_seed_runner"] is True

    result = compose_exec(
        "agent",
        [
            "python",
            "-m",
            "untrusted.agent.seed_runner",
            "--task",
            "write a local-only run report",
            "--planner",
            "scripted",
            "--script",
            ".seed_plans/stage3_local_task.json",
            "--max-steps",
            "8",
        ],
        env=compose_stack,
    )
    payload = json.loads(result.stdout)
    assert payload["success"] is True
    assert payload["finished_reason"] == "planner_finished"
    assert payload["steps_executed"] >= 4

    latest_summary = json.loads(
        (RUN_OUTPUTS / "latest_seed_run.json").read_text(encoding="ascii")
    )
    assert latest_summary["task"] == "write a local-only run report"
    assert any(step["kind"] == "bridge_status" for step in latest_summary["steps"])
    assert any(step["kind"] == "bridge_chat" for step in latest_summary["steps"])
    assert (RUN_OUTPUTS / "stage3_report.txt").exists()

    events = load_events()
    assert any(
        event["event_type"] == "agent_run"
        and event["summary"]["event_kind"] == "run_start"
        and event["summary"]["reported_origin"] == "untrusted_agent"
        for event in events
    )
    assert any(
        event["event_type"] == "agent_run"
        and event["summary"]["event_kind"] == "run_end"
        and event["summary"]["reported_origin"] == "untrusted_agent"
        for event in events
    )
    assert any(
        event["event_type"] == "status_query" and event["actor"] == "agent"
        for event in events
    )
    assert any(
        event["event_type"] == "llm_call" and event["actor"] == "agent"
        for event in events
    )
