import asyncio
import json
from pathlib import Path

import pytest

from shared.config import agent_settings
from shared.schemas import (
    AgentRunEventReceipt,
    BridgeStatusReport,
    BudgetState,
    ChatChoice,
    ChatCompletionResponse,
    ChatMessage,
    ChatUsage,
    ConnectionStatus,
    RecentRequest,
)
from untrusted.agent.command_runner import BoundedCommandRunner
from untrusted.agent.seed_runner import PlanAction, ScriptedPlanner, SeedRunner
from untrusted.agent.workspace_tools import WorkspaceTools


class FakeBridgeClient:
    def __init__(self):
        self.status_calls = 0
        self.chat_calls = 0
        self.reported_events: list[dict] = []

    async def status(self) -> BridgeStatusReport:
        self.status_calls += 1
        return BridgeStatusReport(
            service="bridge",
            stage="stage3_local_seed_agent",
            trusted_state_dir="/var/lib/rsi/trusted_state",
            log_path="/var/lib/rsi/trusted_state/logs/bridge_events.jsonl",
            operational_state_path="/var/lib/rsi/trusted_state/state/operational_state.json",
            connections={
                "litellm": ConnectionStatus(
                    url="http://litellm:4000",
                    reachable=True,
                    detail=None,
                    checked_at="2026-03-12T00:00:00+00:00",
                )
            },
            budget=BudgetState(
                unit="mock_tokens",
                total=100,
                spent=9,
                remaining=91,
                exhausted=False,
                minimum_call_cost=5,
                approximation="deterministic_token_usage_from_stage2_mock_litellm",
                total_prompt_tokens=3,
                total_completion_tokens=6,
                total_tokens=9,
            ),
            counters={"status_queries": 1, "llm_calls_total": 1},
            recent_requests=[
                RecentRequest(
                    timestamp="2026-03-12T00:00:00+00:00",
                    event_type="status_query",
                    request_id="req-status",
                    trace_id="trace-status",
                    actor="agent",
                    source_service="bridge",
                    outcome="success",
                )
            ],
            surfaces={"seed_agent": "local_only_stage3_substrate"},
        )

    async def chat(self, *, model: str, message: str) -> ChatCompletionResponse:
        self.chat_calls += 1
        reply = ChatMessage(role="assistant", content=f"scripted reply: {message}")
        return ChatCompletionResponse(
            id="chatcmpl-scripted",
            object="chat.completion",
            created=1,
            model=model,
            choices=[ChatChoice(index=0, message=reply, finish_reason="stop")],
            usage=ChatUsage(prompt_tokens=4, completion_tokens=4, total_tokens=8),
        )

    async def report_agent_event(
        self,
        *,
        run_id: str,
        event_kind: str,
        step_index: int | None,
        tool_name: str | None,
        summary: dict,
    ) -> AgentRunEventReceipt:
        self.reported_events.append(
            {
                "run_id": run_id,
                "event_kind": event_kind,
                "step_index": step_index,
                "tool_name": tool_name,
                "summary": summary,
            }
        )
        return AgentRunEventReceipt(
            request_id=f"req-{len(self.reported_events)}",
            trace_id=f"trace-{len(self.reported_events)}",
            outcome="recorded",
        )


def make_local_task_workspace(workspace: Path):
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "calc.py").write_text(
        "def add(a, b):\n"
        "    return a - b\n",
        encoding="ascii",
    )
    (workspace / "tests").mkdir(exist_ok=True)
    (workspace / "tests" / "test_calc.py").write_text(
        "from calc import add\n\n"
        "def test_add():\n"
        "    assert add(2, 3) == 5\n",
        encoding="ascii",
    )


def test_agent_settings_make_workspace_target_explicit(monkeypatch, tmp_path):
    workspace_dir = tmp_path / "workspace"
    runtime_code_dir = tmp_path / "runtime_code"
    monkeypatch.setenv("RSI_AGENT_WORKSPACE_DIR", str(workspace_dir))
    monkeypatch.setenv("RSI_AGENT_RUNTIME_CODE_DIR", str(runtime_code_dir))

    settings = agent_settings()

    assert settings.workspace_dir == workspace_dir
    assert settings.runtime_code_dir == runtime_code_dir
    assert settings.workspace_dir != settings.runtime_code_dir


def test_workspace_tools_cannot_escape_mutable_workspace(tmp_path):
    workspace = WorkspaceTools(tmp_path)
    workspace.write_file("notes/summary.txt", "seed agent\n")

    assert workspace.read_file("notes/summary.txt") == "seed agent\n"
    assert workspace.list_files() == ["notes/summary.txt"]
    assert any(entry["path"] == "notes" for entry in workspace.list_tree())

    with pytest.raises(ValueError):
        workspace.read_file("../outside.txt")
    with pytest.raises(ValueError):
        workspace.write_file("/tmp/outside.txt", "nope\n")
    with pytest.raises(ValueError):
        workspace.list_files("../../")


def test_bounded_command_runner_enforces_cwd_timeout_and_output_limit(tmp_path):
    runner = BoundedCommandRunner(tmp_path, default_timeout_seconds=1.0, output_limit_bytes=64)

    cwd_result = runner.run(["python", "-c", "from pathlib import Path; print(Path.cwd().name)"])
    assert cwd_result.returncode == 0
    assert cwd_result.stdout.strip() == tmp_path.name
    assert cwd_result.cwd == str(tmp_path)

    with pytest.raises(ValueError):
        runner.run(["bash", "-lc", "pwd"])

    timeout_result = runner.run(
        ["python", "-c", "import time; time.sleep(2)"],
        timeout_seconds=0.1,
    )
    assert timeout_result.timed_out is True

    output_result = runner.run(
        ["python", "-c", "print('x' * 400)"],
        output_limit_bytes=40,
    )
    assert output_result.stdout_truncated is True
    assert len(output_result.stdout) <= 40


def test_scripted_planner_completes_local_task_end_to_end(tmp_path):
    workspace_dir = tmp_path / "workspace"
    make_local_task_workspace(workspace_dir)

    bridge = FakeBridgeClient()
    planner = ScriptedPlanner(
        [
            PlanAction(kind="bridge_status"),
            PlanAction(kind="bridge_chat", params={"message": "summarize {task}"}),
            PlanAction(kind="read_file", params={"path": "calc.py"}),
            PlanAction(
                kind="write_file",
                params={
                    "path": "calc.py",
                    "content": "def add(a, b):\n    return a + b\n",
                },
            ),
            PlanAction(
                kind="run_command",
                params={"argv": ["python", "-m", "pytest", "-q"]},
            ),
            PlanAction(kind="finish", params={"summary": "local task complete"}),
        ]
    )
    runner = SeedRunner(
        workspace_dir=workspace_dir,
        bridge_client=bridge,
        planner=planner,
        max_steps=8,
    )

    result = asyncio.run(runner.run("fix the local add helper"))

    assert result.success is True
    assert result.finished_reason == "planner_finished"
    assert bridge.status_calls == 1
    assert bridge.chat_calls == 1
    assert len(bridge.reported_events) >= 3
    assert bridge.reported_events[0]["event_kind"] == "run_start"
    assert bridge.reported_events[-1]["event_kind"] == "run_end"
    assert "return a + b" in workspace_dir.joinpath("calc.py").read_text(encoding="ascii")

    latest_summary = workspace_dir / "run_outputs" / "latest_seed_run.json"
    assert latest_summary.exists()
    payload = json.loads(latest_summary.read_text(encoding="ascii"))
    assert payload["task"] == "fix the local add helper"
    assert any(step["kind"] == "bridge_status" for step in payload["steps"])
    assert any(step["kind"] == "bridge_chat" for step in payload["steps"])
