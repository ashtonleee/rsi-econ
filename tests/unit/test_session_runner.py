import json
from pathlib import Path

import pytest

from shared.schemas import (
    AgentRunEventReceipt,
    BrowserInteractable,
    BrowserHttpRequestPreview,
    BrowserRenderResponse,
    BrowserSessionActionResponse,
    BrowserSessionSnapshotResponse,
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
        self.browser_session_snapshots: dict[str, BrowserSessionSnapshotResponse] = {}

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

    async def browser_session_open(
        self,
        *,
        url: str,
        capability_profile: str = "bounded_packet",
    ) -> BrowserSessionSnapshotResponse:
        snapshot = BrowserSessionSnapshotResponse(
            request_id="browser-session-open",
            trace_id="trace-browser-session-open",
            session_id="browser-session-1",
            snapshot_id="snapshot-1",
            capability_profile=capability_profile,
            current_url=url,
            http_status=200,
            page_title="Interactive form fixture",
            meta_description="",
            rendered_text="Interactive form fixture",
            rendered_text_sha256="session-sha-1",
            text_bytes=24,
            text_truncated=False,
            screenshot_png_base64="iVBORw0KGgo=",
            screenshot_sha256="session-shot-1",
            screenshot_bytes=8,
            observed_hosts=["httpbin.org"],
            resolved_ips=["127.0.0.1"],
            channel_records=[],
            interactable_elements=[
                BrowserInteractable(
                    element_id="el_001",
                    kind="text_input",
                    label="Name",
                    name="name",
                    value_preview="",
                ),
                BrowserInteractable(
                    element_id="el_002",
                    kind="submit",
                    text="Claim reward",
                ),
            ],
        )
        self.browser_session_snapshots[snapshot.session_id] = snapshot
        return snapshot

    async def browser_session_snapshot(self, *, session_id: str) -> BrowserSessionSnapshotResponse:
        return self.browser_session_snapshots[session_id]

    async def browser_session_click(self, *, session_id: str, snapshot_id: str, element_id: str) -> BrowserSessionSnapshotResponse:
        return self.browser_session_snapshots[session_id]

    async def browser_session_type(
        self,
        *,
        session_id: str,
        snapshot_id: str,
        element_id: str,
        text: str,
    ) -> BrowserSessionSnapshotResponse:
        snapshot = self.browser_session_snapshots[session_id].model_copy(
            update={
                "snapshot_id": "snapshot-2",
                "interactable_elements": [
                    BrowserInteractable(
                        element_id="el_001",
                        kind="text_input",
                        label="Name",
                        name="name",
                        value_preview=text,
                    ),
                    BrowserInteractable(
                        element_id="el_002",
                        kind="submit",
                        text="Claim reward",
                    ),
                ],
            }
        )
        self.browser_session_snapshots[session_id] = snapshot
        return snapshot

    async def browser_session_navigate(self, *, session_id: str, snapshot_id: str, url: str) -> BrowserSessionActionResponse:
        snapshot = self.browser_session_snapshots[session_id].model_copy(update={"snapshot_id": "snapshot-nav", "current_url": url})
        self.browser_session_snapshots[session_id] = snapshot
        return BrowserSessionActionResponse(
            request_id="browser-action-1",
            trace_id="trace-browser-action-1",
            outcome="snapshot",
            snapshot=snapshot,
            proposal_preview=None,
            proposal=None,
        )

    async def browser_session_click_action(
        self,
        *,
        session_id: str,
        snapshot_id: str,
        element_id: str,
    ) -> BrowserSessionActionResponse:
        snapshot = self.browser_session_snapshots[session_id]
        if element_id == "el_002":
            preview = BrowserHttpRequestPreview(
                request_id="paused-request-1",
                session_id=session_id,
                snapshot_id=snapshot.snapshot_id,
                tab_id="tab_001",
                current_url=snapshot.current_url,
                target_url="https://public.example/form-submit",
                method="POST",
                header_preview={"content-type": "application/x-www-form-urlencoded"},
                body_preview="name=alice",
                body_sha256="body-sha",
                body_bytes=10,
                trigger_action="click",
                trigger_element_id=element_id,
            )
            proposal = ProposalRecord(
                proposal_id="browser-http-request-1",
                action_type="browser_http_request",
                action_payload={
                    "request_id": preview.request_id,
                    "session_id": session_id,
                    "snapshot_id": snapshot.snapshot_id,
                    "tab_id": preview.tab_id,
                    "current_url": preview.current_url,
                    "target_url": preview.target_url,
                    "method": preview.method,
                    "header_preview": preview.header_preview,
                    "body_preview": preview.body_preview,
                    "body_sha256": preview.body_sha256,
                    "body_bytes": preview.body_bytes,
                    "trigger_action": preview.trigger_action,
                    "trigger_element_id": preview.trigger_element_id,
                },
                status="pending",
                created_by="agent",
                created_at="2026-03-20T00:00:02+00:00",
                request_id="req-browser-http-proposal",
                trace_id="trace-browser-http-proposal",
            )
            return BrowserSessionActionResponse(
                request_id="browser-action-2",
                trace_id="trace-browser-action-2",
                outcome="proposal_required",
                snapshot=snapshot,
                proposal_preview=preview,
                proposal=proposal,
            )
        return BrowserSessionActionResponse(
            request_id="browser-action-3",
            trace_id="trace-browser-action-3",
            outcome="snapshot",
            snapshot=snapshot,
            proposal_preview=None,
            proposal=None,
        )

    async def browser_session_fill(
        self,
        *,
        session_id: str,
        snapshot_id: str,
        element_id: str,
        text: str,
    ) -> BrowserSessionActionResponse:
        snapshot = await self.browser_session_type(
            session_id=session_id,
            snapshot_id=snapshot_id,
            element_id=element_id,
            text=text,
        )
        return BrowserSessionActionResponse(
            request_id="browser-action-fill",
            trace_id="trace-browser-action-fill",
            outcome="snapshot",
            snapshot=snapshot,
            proposal_preview=None,
            proposal=None,
        )

    async def browser_session_select_action(self, *, session_id: str, snapshot_id: str, element_id: str, value: str) -> BrowserSessionActionResponse:
        snapshot = self.browser_session_snapshots[session_id]
        return BrowserSessionActionResponse(request_id="browser-action-select", trace_id="trace-browser-action-select", outcome="snapshot", snapshot=snapshot, proposal_preview=None, proposal=None)

    async def browser_session_set_checked_action(self, *, session_id: str, snapshot_id: str, element_id: str, checked: bool) -> BrowserSessionActionResponse:
        snapshot = self.browser_session_snapshots[session_id]
        return BrowserSessionActionResponse(request_id="browser-action-check", trace_id="trace-browser-action-check", outcome="snapshot", snapshot=snapshot, proposal_preview=None, proposal=None)

    async def browser_session_press(self, *, session_id: str, snapshot_id: str, key: str, element_id: str = "") -> BrowserSessionActionResponse:
        snapshot = self.browser_session_snapshots[session_id]
        return BrowserSessionActionResponse(request_id="browser-action-press", trace_id="trace-browser-action-press", outcome="snapshot", snapshot=snapshot, proposal_preview=None, proposal=None)

    async def browser_session_hover(self, *, session_id: str, snapshot_id: str, element_id: str) -> BrowserSessionActionResponse:
        snapshot = self.browser_session_snapshots[session_id]
        return BrowserSessionActionResponse(request_id="browser-action-hover", trace_id="trace-browser-action-hover", outcome="snapshot", snapshot=snapshot, proposal_preview=None, proposal=None)

    async def browser_session_wait_for(self, *, session_id: str, snapshot_id: str = "", text: str = "", time_seconds: float = 0.0) -> BrowserSessionActionResponse:
        snapshot = self.browser_session_snapshots[session_id]
        return BrowserSessionActionResponse(request_id="browser-action-wait", trace_id="trace-browser-action-wait", outcome="snapshot", snapshot=snapshot, proposal_preview=None, proposal=None)

    async def browser_session_back(self, *, session_id: str, snapshot_id: str = "") -> BrowserSessionActionResponse:
        snapshot = self.browser_session_snapshots[session_id]
        return BrowserSessionActionResponse(request_id="browser-action-back", trace_id="trace-browser-action-back", outcome="snapshot", snapshot=snapshot, proposal_preview=None, proposal=None)

    async def browser_session_forward(self, *, session_id: str, snapshot_id: str = "") -> BrowserSessionActionResponse:
        snapshot = self.browser_session_snapshots[session_id]
        return BrowserSessionActionResponse(request_id="browser-action-forward", trace_id="trace-browser-action-forward", outcome="snapshot", snapshot=snapshot, proposal_preview=None, proposal=None)

    async def browser_session_new_tab(self, *, session_id: str, url: str = "") -> BrowserSessionActionResponse:
        snapshot = self.browser_session_snapshots[session_id].model_copy(update={"active_tab_id": "tab_002", "tabs": [{"tab_id": "tab_001", "current_url": "https://httpbin.org/forms/post", "page_title": "Interactive form fixture"}, {"tab_id": "tab_002", "current_url": url or "about:blank", "page_title": "New tab"}]})
        self.browser_session_snapshots[session_id] = snapshot
        return BrowserSessionActionResponse(request_id="browser-action-new-tab", trace_id="trace-browser-action-new-tab", outcome="snapshot", snapshot=snapshot, proposal_preview=None, proposal=None)

    async def browser_session_switch_tab(self, *, session_id: str, snapshot_id: str, tab_id: str) -> BrowserSessionActionResponse:
        snapshot = self.browser_session_snapshots[session_id].model_copy(update={"active_tab_id": tab_id})
        self.browser_session_snapshots[session_id] = snapshot
        return BrowserSessionActionResponse(request_id="browser-action-switch-tab", trace_id="trace-browser-action-switch-tab", outcome="snapshot", snapshot=snapshot, proposal_preview=None, proposal=None)

    async def browser_session_close_tab(self, *, session_id: str, snapshot_id: str = "", tab_id: str = "") -> BrowserSessionActionResponse:
        snapshot = self.browser_session_snapshots[session_id]
        return BrowserSessionActionResponse(request_id="browser-action-close-tab", trace_id="trace-browser-action-close-tab", outcome="snapshot", snapshot=snapshot, proposal_preview=None, proposal=None)

    async def browser_session_select(
        self,
        *,
        session_id: str,
        snapshot_id: str,
        element_id: str,
        value: str,
    ) -> BrowserSessionSnapshotResponse:
        return self.browser_session_snapshots[session_id]

    async def browser_session_set_checked(
        self,
        *,
        session_id: str,
        snapshot_id: str,
        element_id: str,
        checked: bool,
    ) -> BrowserSessionSnapshotResponse:
        return self.browser_session_snapshots[session_id]

    async def browser_submit_proposal(
        self,
        *,
        session_id: str,
        snapshot_id: str,
        element_id: str,
    ) -> ProposalRecord:
        return ProposalRecord(
            proposal_id="browser-proposal-1",
            action_type="browser_submit",
            action_payload={
                "session_id": session_id,
                "snapshot_id": snapshot_id,
                "submit_element_id": element_id,
                "target_url": "https://httpbin.org/post",
                "method": "POST",
                "field_preview": [{"name": "name", "kind": "text", "value_preview": "demo"}],
            },
            status="pending",
            created_by="agent",
            created_at="2026-03-20T00:00:01+00:00",
            request_id="req-browser-proposal",
            trace_id="trace-browser-proposal",
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

    with pytest.raises(ValueError):
        validate_session_action(
            {
                "tool": "bridge_browser_session_click",
                "params": {"session_id": "browser-session-1"},
                "reason": "Missing snapshot and element IDs.",
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
async def test_session_runner_surfaces_provider_passthrough_hint_for_mock_plain_text(tmp_path: Path):
    bridge = FakeBridgeClient(["stage1 deterministic reply: plain text mock output"])
    runner = make_runner(tmp_path, bridge_client=bridge)

    result = await runner.run_session(
        session_id="session-plain-text",
        task="Read one page.",
    )
    state = json.loads(
        (tmp_path / "agent_workspace" / "sessions" / "session-plain-text" / "state.json").read_text(encoding="utf-8")
    )

    assert result.stop_reason == "failed"
    assert state["status"] == "failed"
    assert "provider_passthrough" in state["error"]


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


@pytest.mark.fast
@pytest.mark.anyio("asyncio")
async def test_session_runner_can_open_type_and_pause_for_browser_submit(tmp_path: Path):
    bridge = FakeBridgeClient(
        [
            json.dumps(
                {
                    "tool": "bridge_browser_session_open",
                    "reason": "Need an interactive browser session for the form.",
                    "params": {"url": "https://httpbin.org/forms/post"},
                }
            ),
            json.dumps(
                {
                    "tool": "bridge_browser_session_type",
                    "reason": "Fill the name field before submitting.",
                    "params": {
                        "session_id": "browser-session-1",
                        "snapshot_id": "snapshot-1",
                        "element_id": "el_001",
                        "text": "alice",
                    },
                }
            ),
            json.dumps(
                {
                    "tool": "bridge_browser_submit_proposal",
                    "reason": "Need approval before submitting the form.",
                    "params": {
                        "session_id": "browser-session-1",
                        "snapshot_id": "snapshot-2",
                        "element_id": "el_002",
                    },
                }
            ),
        ]
    )
    runner = make_runner(tmp_path, bridge_client=bridge)

    result = await runner.run_session(
        session_id="interactive-session",
        task="Fill the form and ask for approval before the submit.",
        input_url="https://httpbin.org/forms/post",
    )
    state = json.loads((tmp_path / "agent_workspace" / "sessions" / "interactive-session" / "state.json").read_text(encoding="utf-8"))

    assert result.stop_reason == "waiting_for_approval"
    assert state["status"] == "waiting_for_approval"
    assert state["browser_session"]["session_id"] == "browser-session-1"
    assert state["last_proposal"]["action_type"] == "browser_submit"
    assert state["last_proposal"]["action_payload"]["target_url"] == "https://httpbin.org/post"


@pytest.mark.fast
@pytest.mark.anyio("asyncio")
async def test_session_runner_public_workflow_click_pauses_for_browser_http_request(tmp_path: Path):
    bridge = FakeBridgeClient(
        [
            json.dumps(
                {
                    "tool": "bridge_browser_session_open",
                    "reason": "Open the public workflow page first.",
                    "params": {"url": "https://public.example/workflow"},
                }
            ),
            json.dumps(
                {
                    "tool": "bridge_browser_session_fill",
                    "reason": "Fill the visible name field.",
                    "params": {
                        "session_id": "browser-session-1",
                        "snapshot_id": "snapshot-1",
                        "element_id": "el_001",
                        "text": "alice",
                    },
                }
            ),
            json.dumps(
                {
                    "tool": "bridge_browser_session_click",
                    "reason": "Click the real submit button and let the trusted browser pause the request.",
                    "params": {
                        "session_id": "browser-session-1",
                        "snapshot_id": "snapshot-2",
                        "element_id": "el_002",
                    },
                }
            ),
        ]
    )
    runner = make_runner(tmp_path, bridge_client=bridge)

    result = await runner.run_session(
        session_id="public-workflow-session",
        task="Fill the public workflow form and pause on the real request.",
        input_url="https://public.example/workflow",
        capability_profile="workflow_browser_public",
    )
    state = json.loads((tmp_path / "agent_workspace" / "sessions" / "public-workflow-session" / "state.json").read_text(encoding="utf-8"))

    assert result.stop_reason == "waiting_for_approval"
    assert state["status"] == "waiting_for_approval"
    assert state["capability_profile"] == "workflow_browser_public"
    assert state["last_proposal"]["action_type"] == "browser_http_request"
    assert state["last_proposal"]["action_payload"]["target_url"] == "https://public.example/form-submit"
    assert state["browser_session"]["pending_request_preview"]["request_id"] == "paused-request-1"
