import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from operator_console.app import create_app
from operator_console.config import ConsoleSettings
from operator_console.data import RepoData
from operator_console.sessions import SessionRecord
from shared.schemas import (
    BridgeStatusReport,
    BrowserState,
    BudgetState,
    ConnectionStatus,
    ProposalRecord,
    ProposalState,
    RecentRequest,
    RecoveryState,
    WebState,
)


class FakeBridgeAPI:
    def __init__(self, *, proposals: list[ProposalRecord] | None = None):
        self._proposals = {proposal.proposal_id: proposal for proposal in (proposals or [])}
        self.decisions: list[tuple[str, str, str]] = []
        self.executions: list[str] = []

    async def get_status(self) -> BridgeStatusReport:
        connection = ConnectionStatus(url="http://service", reachable=True, detail=None, checked_at="2026-03-20T00:00:00+00:00")
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
                    actor="operator",
                    source_service="bridge",
                    outcome="success",
                )
            ],
            surfaces={"browser": "trusted_browser"},
            proposals=ProposalState(total=len(self._proposals), pending=1 if "pending-1" in self._proposals else 0),
        )

    async def list_proposals(self, *, status: str | None = None) -> list[ProposalRecord]:
        proposals = list(self._proposals.values())
        if status:
            proposals = [proposal for proposal in proposals if proposal.status == status]
        return proposals

    async def get_proposal(self, proposal_id: str) -> ProposalRecord:
        return self._proposals[proposal_id]

    async def decide_proposal(self, proposal_id: str, *, decision: str, reason: str) -> ProposalRecord:
        proposal = self._proposals[proposal_id]
        self.decisions.append((proposal_id, decision, reason))
        updated = proposal.model_copy(
            update={
                "status": "approved" if decision == "approve" else "rejected",
                "decided_by": "operator",
                "decided_at": "2026-03-20T00:02:00+00:00",
                "decision_reason": reason,
            }
        )
        self._proposals[proposal_id] = updated
        return updated

    async def execute_proposal(self, proposal_id: str) -> ProposalRecord:
        proposal = self._proposals[proposal_id]
        self.executions.append(proposal_id)
        updated = proposal.model_copy(
            update={
                "status": "executed",
                "executed_by": "operator",
                "executed_at": "2026-03-20T00:03:00+00:00",
                "execution_result": {"http_status": 200},
            }
        )
        self._proposals[proposal_id] = updated
        return updated


class FakeSessionManager:
    def __init__(self, settings: ConsoleSettings):
        self.settings = settings
        self.created_requests: list[dict] = []
        self.proposal_updates: list[str] = []
        self.sessions = {
            "session-1": SessionRecord(
                session_id="session-1",
                created_at="2026-03-20T00:01:00+00:00",
                updated_at="2026-03-20T00:01:30+00:00",
                status="waiting_for_approval",
                task="Read a page and ask for approval.",
                launch_mode="provider",
                model="openai/gpt-4.1-mini",
                input_url="https://httpbin.org/html",
                proposal_target_url="https://httpbin.org/post",
                max_turns_per_resume=4,
                resume_count=0,
                current_run_id="run-1",
                last_run_id="run-1",
                last_proposal_id="pending-1",
                auto_resume_enabled=True,
                pid=None,
                error="",
                summary_path="",
            )
        }
        self.snapshots = {
            "session-1": {
                "session": self.sessions["session-1"].to_dict(),
                "phase_label": "waiting_for_approval",
                "phase_tone": "warn",
                "headline": "The session is paused for approval.",
                "next_action": "Approve or reject the pending proposal below.",
                "diagnostics": [{"severity": "warn", "title": "Waiting for approval", "body": "Needs operator action."}],
                "transcript": [
                    {
                        "kind": "tool_result",
                        "timestamp": "2026-03-20T00:01:30+00:00",
                        "tool": "bridge_create_proposal",
                        "reason": "Need approval.",
                        "result": {"proposal_id": "pending-1", "status": "pending"},
                    }
                ],
                "current_screenshot": {
                    "name": "turn_001_browser.png",
                    "relative_path": "sessions/session-1/artifacts/turn_001_browser.png",
                    "url": "/artifacts/sessions/session-1/artifacts/turn_001_browser.png",
                },
                "recent_screenshots": [
                    {
                        "name": "turn_001_browser.png",
                        "relative_path": "sessions/session-1/artifacts/turn_001_browser.png",
                        "url": "/artifacts/sessions/session-1/artifacts/turn_001_browser.png",
                    }
                ],
                "related_artifacts": [],
                "summary_url": "",
                "log_tail": "",
                "proposal_ids": ["pending-1"],
                "version_token": "v1",
            }
        }

    def list_sessions(self) -> list[SessionRecord]:
        return list(self.sessions.values())

    def get_active_session(self) -> SessionRecord | None:
        return None

    def create_session(self, request):
        record = SessionRecord(
            session_id="session-2",
            created_at="2026-03-20T00:05:00+00:00",
            updated_at="2026-03-20T00:05:00+00:00",
            status="starting",
            task=request.task,
            launch_mode=request.launch_mode,
            model=request.model,
            input_url=request.input_url,
            proposal_target_url=request.proposal_target_url,
            max_turns_per_resume=request.max_turns_per_resume,
            resume_count=0,
            current_run_id="",
            last_run_id="",
            last_proposal_id="",
            auto_resume_enabled=True,
            pid=4242,
            error="",
            summary_path="",
        )
        self.created_requests.append(request.to_dict())
        self.sessions[record.session_id] = record
        self.snapshots[record.session_id] = {
            "session": record.to_dict(),
            "phase_label": "starting",
            "phase_tone": "warn",
            "headline": "Starting session.",
            "next_action": "Wait for the first turn.",
            "diagnostics": [],
            "transcript": [],
            "current_screenshot": None,
            "recent_screenshots": [],
            "related_artifacts": [],
            "summary_url": "",
            "log_tail": "",
            "proposal_ids": [],
            "version_token": "v1",
        }
        return record

    def get_session(self, session_id: str) -> SessionRecord:
        return self.sessions[session_id]

    def get_snapshot(self, session_id: str) -> dict:
        return self.snapshots[session_id]

    def apply_proposal_update(self, proposal: ProposalRecord):
        self.proposal_updates.append(proposal.proposal_id)
        return self.sessions["session-1"]


def make_settings(tmp_path: Path) -> ConsoleSettings:
    workspace_dir = tmp_path / "agent_workspace"
    (workspace_dir / "run_outputs").mkdir(parents=True)
    (workspace_dir / "research").mkdir()
    (workspace_dir / "sessions" / "session-1" / "artifacts").mkdir(parents=True)
    (workspace_dir / "sessions" / "session-1" / "artifacts" / "turn_001_browser.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    trusted_state_dir = tmp_path / "trusted_state"
    (trusted_state_dir / "logs").mkdir(parents=True)
    operator_runtime_dir = tmp_path / "operator_console_runtime"
    return ConsoleSettings(
        bridge_url="http://127.0.0.1:8000",
        operator_token="token",
        workspace_dir=workspace_dir,
        trusted_state_dir=trusted_state_dir,
        operator_runtime_dir=operator_runtime_dir,
    )


def make_proposals() -> list[ProposalRecord]:
    return [
        ProposalRecord(
            proposal_id="pending-1",
            action_type="http_post",
            action_payload={"url": "https://httpbin.org/post"},
            status="pending",
            created_by="agent",
            created_at="2026-03-20T00:01:00+00:00",
            request_id="req-pending",
            trace_id="trace-pending",
        )
    ]


@pytest.mark.fast
def test_sessions_pages_render_and_create_sessions(tmp_path: Path):
    settings = make_settings(tmp_path)
    session_manager = FakeSessionManager(settings)
    app = create_app(
        settings=settings,
        bridge_api=FakeBridgeAPI(proposals=make_proposals()),
        repo_data=RepoData(settings),
        session_manager=session_manager,
    )

    with TestClient(app) as client:
        list_response = client.get("/sessions")
        new_response = client.get("/sessions/new")
        create_response = client.post(
            "/sessions",
            data={
                "task": "Answer from one page.",
                "launch_mode": "provider",
                "model": "openai/gpt-4.1-mini",
                "input_url": "https://httpbin.org/html",
                "proposal_target_url": "",
                "max_turns_per_resume": "4",
            },
            follow_redirects=False,
        )

    assert list_response.status_code == 200
    assert "session-1" in list_response.text
    assert new_response.status_code == 200
    assert "Start Session" in new_response.text
    assert create_response.status_code == 303
    assert create_response.headers["location"].startswith("/sessions/session-2")
    assert session_manager.created_requests[0]["model"] == "openai/gpt-4.1-mini"


@pytest.mark.fast
def test_session_detail_and_stream_render_transcript_and_preview(tmp_path: Path):
    settings = make_settings(tmp_path)
    session_manager = FakeSessionManager(settings)
    app = create_app(
        settings=settings,
        bridge_api=FakeBridgeAPI(proposals=make_proposals()),
        repo_data=RepoData(settings),
        session_manager=session_manager,
    )

    with TestClient(app) as client:
        html_response = client.get("/sessions/session-1")
        api_response = client.get("/api/sessions/session-1")
        with client.stream("GET", "/api/sessions/session-1/stream?once=1") as stream_response:
            chunks = []
            for line in stream_response.iter_lines():
                chunks.append(line)
                if line == "":
                    break

    assert html_response.status_code == 200
    assert "Session Workspace" in html_response.text
    assert "bridge_create_proposal" in html_response.text
    assert "turn_001_browser.png" in html_response.text
    assert api_response.status_code == 200
    assert api_response.json()["session"]["session_id"] == "session-1"
    assert api_response.json()["phase_label"] == "waiting_for_approval"
    assert "event: snapshot" in "\n".join(chunks)


@pytest.mark.fast
def test_proposal_actions_trigger_session_update_hook(tmp_path: Path):
    settings = make_settings(tmp_path)
    session_manager = FakeSessionManager(settings)
    bridge = FakeBridgeAPI(proposals=make_proposals())
    app = create_app(
        settings=settings,
        bridge_api=bridge,
        repo_data=RepoData(settings),
        session_manager=session_manager,
    )

    with TestClient(app) as client:
        approve = client.post(
            "/proposals/pending-1/approve",
            data={"reason": "looks good", "redirect_to": "/sessions/session-1"},
            follow_redirects=False,
        )

    assert approve.status_code == 303
    assert bridge.decisions[0] == ("pending-1", "approve", "looks good")
    assert session_manager.proposal_updates == ["pending-1"]
