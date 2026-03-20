import json
from pathlib import Path

import pytest

from shared.schemas import (
    AgentRunEventReceipt,
    BrowserRenderResponse,
    BrowserState,
    BridgeStatusReport,
    BudgetState,
    ChatChoice,
    ChatCompletionResponse,
    ChatMessage,
    ChatUsage,
    ConnectionStatus,
    ProposalRecord,
    RecentRequest,
    RecoveryState,
    WebState,
)
from untrusted.agent.session_runner import SessionRunner, SessionToolAction, validate_session_action


class FakeBridgeClient:
    def __init__(self, chat_messages: list[str]):
        self.chat_messages = list(chat_messages)
        self.reported_events: list[dict] = []
        self.created_proposals: list[dict] = []

    async def status(self) -> BridgeStatusReport:
        connection = ConnectionStatus(
            url="http://service",
            reachable=True,
            detail=None,
            checked_at="2026-03-20T00:00:00+00:00",
        )
        return BridgeStatusReport(
            service="bridge",
            stage="stage8_consequential_actions",
            trusted_state_dir="/var/lib/rsi/trusted_state",
            log_path="/var/lib/rsi/trusted_state/logs/bridge_events.jsonl",
            operational_state_path="/var/lib/rsi/trusted_state/state/operational_state.json",
            connections={"browser": connection, "fetcher": connection, "litellm": connection},
            budget=BudgetState(
                unit="mock_tokens",
                total=100,
                spent=10,
                remaining=90,
                exhausted=False,
                minimum_call_cost=5,
                approximation="deterministic",
                total_prompt_tokens=5,
                total_completion_tokens=5,
                total_tokens=10,
            ),
            recovery=RecoveryState(
                checkpoint_dir="/tmp/checkpoints",
                baseline_id="baseline-1",
                baseline_source_dir="/tmp/source",
                baseline_archive_path="/tmp/archive",
                available_checkpoints=[],
                latest_checkpoint_id=None,
                latest_action=None,
                current_workspace_status="seed_baseline",
            ),
            web=WebState(
                fetcher=connection,
                allowlist_hosts=["httpbin.org"],
                private_test_hosts=[],
                allowed_content_types=["text/html"],
                caps={"max_redirects": 3},
                counters={"web_fetch_total": 0},
                recent_fetches=[],
            ),
            browser=BrowserState(
                service=connection,
                caps={"timeout_seconds": 10},
                counters={"browser_render_total": 0},
                recent_renders=[],
                recent_follows=[],
            ),
            counters={"status_queries": 1},
            recent_requests=[
                RecentRequest(
                    timestamp="2026-03-20T00:00:00+00:00",
                    event_type="status_query",
                    request_id="req-1",
                    trace_id="trace-1",
                    actor="agent",
                    source_service="bridge",
                    outcome="success",
                )
            ],
            surfaces={"browser": "trusted_browser"},
            proposals=None,
        )

    async def chat(self, *, model: str, message: str) -> ChatCompletionResponse:
        assert self.chat_messages, "no fake chat messages left"
        content = self.chat_messages.pop(0)
        return ChatCompletionResponse(
            id="chatcmpl-1",
            object="chat.completion",
            created=1,
            model=model,
            choices=[ChatChoice(index=0, message=ChatMessage(role="assistant", content=content), finish_reason="stop")],
            usage=ChatUsage(prompt_tokens=6, completion_tokens=6, total_tokens=12),
        )

    async def browser_render(self, *, url: str) -> BrowserRenderResponse:
        return BrowserRenderResponse(
            request_id="browser-1",
            trace_id="trace-browser-1",
            normalized_url=url,
            final_url=url,
            http_status=200,
            page_title="Demo page",
            meta_description="",
            rendered_text="A demo page for session testing.",
            rendered_text_sha256="abc123",
            text_bytes=31,
            text_truncated=False,
            screenshot_png_base64="iVBORw0KGgo=",
            screenshot_sha256="def456",
            screenshot_bytes=8,
            redirect_chain=[],
            observed_hosts=["httpbin.org"],
            resolved_ips=["127.0.0.1"],
            channel_records=[],
            followable_links=[],
        )

    async def create_proposal(self, *, action_type: str, action_payload: dict) -> ProposalRecord:
        self.created_proposals.append({"action_type": action_type, "action_payload": action_payload})
        return ProposalRecord(
            proposal_id="proposal-1",
            action_type=action_type,
            action_payload=action_payload,
            status="pending",
            created_by="agent",
            created_at="2026-03-20T00:00:01+00:00",
            request_id="req-proposal",
            trace_id="trace-proposal",
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
        return AgentRunEventReceipt(request_id="req-event", trace_id="trace-event", outcome="recorded")


def make_runner(tmp_path: Path, *, bridge_client: FakeBridgeClient, model: str = "openai/gpt-4.1-mini") -> SessionRunner:
    workspace_dir = tmp_path / "agent_workspace"
    workspace_dir.mkdir()
    return SessionRunner(
        workspace_dir=workspace_dir,
        runtime_code_dir=tmp_path / "runtime_code",
        bridge_client=bridge_client,
        model=model,
        max_turns_per_resume=4,
    )


@pytest.mark.fast
def test_validate_session_action_accepts_allowed_tools_and_rejects_run_command():
    allowed = validate_session_action(
        {
            "tool": "bridge_browser_render",
            "params": {"url": "https://httpbin.org/html"},
            "reason": "Need the rendered page packet first.",
        }
    )

    assert allowed == SessionToolAction(
        tool="bridge_browser_render",
        params={"url": "https://httpbin.org/html"},
        reason="Need the rendered page packet first.",
    )

    with pytest.raises(ValueError):
        validate_session_action(
            {
                "tool": "run_command",
                "params": {"argv": ["python", "-m", "pytest"]},
                "reason": "Should be blocked.",
            }
        )


@pytest.mark.fast
@pytest.mark.anyio("asyncio")
async def test_session_runner_creates_proposal_and_stops_waiting_for_approval(tmp_path: Path):
    bridge = FakeBridgeClient(
        [
            json.dumps(
                {
                    "tool": "bridge_create_proposal",
                    "reason": "Need operator approval before posting the summary.",
                    "params": {
                        "action_type": "http_post",
                        "action_payload": {"url": "https://httpbin.org/post", "body": {"summary": "demo"}},
                    },
                }
            )
        ]
    )
    runner = make_runner(tmp_path, bridge_client=bridge)

    result = await runner.run_session(
        session_id="session-1",
        task="Read a page and ask for approval to post a summary.",
        input_url="https://httpbin.org/html",
        proposal_target_url="https://httpbin.org/post",
    )

    state = json.loads((tmp_path / "agent_workspace" / "sessions" / "session-1" / "state.json").read_text(encoding="utf-8"))
    transcript_path = tmp_path / "agent_workspace" / "sessions" / "session-1" / "transcript.jsonl"

    assert result.stop_reason == "waiting_for_approval"
    assert state["status"] == "waiting_for_approval"
    assert state["last_proposal"]["proposal_id"] == "proposal-1"
    assert transcript_path.exists()
    assert any("proposal-1" in line for line in transcript_path.read_text(encoding="utf-8").splitlines())


@pytest.mark.fast
@pytest.mark.anyio("asyncio")
async def test_session_runner_resume_uses_persisted_state_and_finishes(tmp_path: Path):
    bridge = FakeBridgeClient(
        [
            json.dumps(
                {
                    "tool": "bridge_create_proposal",
                    "reason": "Need approval.",
                    "params": {
                        "action_type": "http_post",
                        "action_payload": {"url": "https://httpbin.org/post", "body": {"summary": "demo"}},
                    },
                }
            ),
            json.dumps(
                {
                    "tool": "finish",
                    "reason": "The proposal already executed and the session can conclude.",
                    "params": {"summary": "The page was read and the approved action already executed."},
                }
            ),
        ]
    )
    runner = make_runner(tmp_path, bridge_client=bridge)
    workspace_state_path = tmp_path / "agent_workspace" / "sessions" / "session-1" / "state.json"

    first = await runner.run_session(
        session_id="session-1",
        task="Read a page and ask for approval to post a summary.",
        input_url="https://httpbin.org/html",
        proposal_target_url="https://httpbin.org/post",
    )
    state = json.loads(workspace_state_path.read_text(encoding="utf-8"))
    state["last_proposal"]["status"] = "executed"
    state["last_proposal"]["executed_at"] = "2026-03-20T00:02:00+00:00"
    state["last_proposal"]["execution_result"] = {"http_status": 200}
    workspace_state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    second = await runner.run_session(session_id="session-1", resume=True)
    updated = json.loads(workspace_state_path.read_text(encoding="utf-8"))
    final_answer = tmp_path / "agent_workspace" / "sessions" / "session-1" / "artifacts" / "final_answer.md"

    assert first.stop_reason == "waiting_for_approval"
    assert second.stop_reason == "finished"
    assert updated["status"] == "finished"
    assert updated["resume_count"] == 1
    assert final_answer.exists()
    assert "already executed" in final_answer.read_text(encoding="utf-8")


@pytest.mark.fast
@pytest.mark.anyio("asyncio")
async def test_session_runner_fails_when_model_requests_disallowed_tool(tmp_path: Path):
    bridge = FakeBridgeClient(
        [
            json.dumps(
                {
                    "tool": "run_command",
                    "reason": "Should never be allowed.",
                    "params": {"argv": ["python", "-m", "pytest"]},
                }
            )
        ]
    )
    runner = make_runner(tmp_path, bridge_client=bridge)

    result = await runner.run_session(
        session_id="session-1",
        task="Do something unsafe.",
    )
    state = json.loads((tmp_path / "agent_workspace" / "sessions" / "session-1" / "state.json").read_text(encoding="utf-8"))

    assert result.stop_reason == "failed"
    assert state["status"] == "failed"
    assert "run_command" in state["error"]


@pytest.mark.fast
@pytest.mark.anyio("asyncio")
async def test_session_runner_writes_packet_screenshot_artifact(tmp_path: Path):
    bridge = FakeBridgeClient(
        [
            json.dumps(
                {
                    "tool": "bridge_browser_render",
                    "reason": "Need the browser packet first.",
                    "params": {"url": "https://httpbin.org/html"},
                }
            ),
            json.dumps(
                {
                    "tool": "finish",
                    "reason": "Enough information captured.",
                    "params": {"summary": "Captured the packet."},
                }
            ),
        ]
    )
    runner = make_runner(tmp_path, bridge_client=bridge)

    result = await runner.run_session(
        session_id="session-2",
        task="Capture one page.",
        input_url="https://httpbin.org/html",
    )
    state = json.loads((tmp_path / "agent_workspace" / "sessions" / "session-2" / "state.json").read_text(encoding="utf-8"))
    screenshot_path = tmp_path / "agent_workspace" / state["current_screenshot_path"]

    assert result.stop_reason == "finished"
    assert screenshot_path.exists()
    assert screenshot_path.read_bytes().startswith(b"\x89PNG")
