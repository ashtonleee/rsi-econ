import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from operator_console.app import create_app
from operator_console.bridge_api import BridgeNotFoundError, BridgeUnavailableError
from operator_console.config import ConsoleSettings
from operator_console.data import RepoData
from operator_console.launches import LaunchBusyError, LaunchRecord
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
    def __init__(
        self,
        *,
        status: BridgeStatusReport | None = None,
        proposals: list[ProposalRecord] | None = None,
        error: str | None = None,
    ):
        self._status = status
        self._proposals = {proposal.proposal_id: proposal for proposal in (proposals or [])}
        self._error = error
        self.decisions: list[tuple[str, str, str]] = []
        self.executions: list[str] = []

    async def get_status(self) -> BridgeStatusReport:
        if self._error:
            raise BridgeUnavailableError(self._error)
        assert self._status is not None
        return self._status

    async def list_proposals(self, *, status: str | None = None) -> list[ProposalRecord]:
        if self._error:
            raise BridgeUnavailableError(self._error)
        proposals = list(self._proposals.values())
        if status:
            proposals = [proposal for proposal in proposals if proposal.status == status]
        return sorted(proposals, key=lambda proposal: proposal.created_at, reverse=True)

    async def get_proposal(self, proposal_id: str) -> ProposalRecord:
        if self._error:
            raise BridgeUnavailableError(self._error)
        proposal = self._proposals.get(proposal_id)
        if proposal is None:
            raise BridgeNotFoundError("proposal not found")
        return proposal

    async def decide_proposal(self, proposal_id: str, *, decision: str, reason: str) -> ProposalRecord:
        proposal = await self.get_proposal(proposal_id)
        self.decisions.append((proposal_id, decision, reason))
        status = "approved" if decision == "approve" else "rejected"
        updated = proposal.model_copy(
            update={
                "status": status,
                "decided_by": "operator",
                "decided_at": "2026-03-20T00:02:00+00:00",
                "decision_reason": reason,
            }
        )
        self._proposals[proposal_id] = updated
        return updated

    async def execute_proposal(self, proposal_id: str) -> ProposalRecord:
        proposal = await self.get_proposal(proposal_id)
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


class FakeLaunchManager:
    def __init__(self, settings: ConsoleSettings):
        self.settings = settings
        self.plans = ["stage8_real_site_approval_demo.json", "stage6_browser_demo.json"]
        self.launches: dict[str, LaunchRecord] = {}
        self.snapshots: dict[str, dict] = {}
        self.raise_busy = False
        self.created_requests: list[dict[str, object]] = []

    def list_seed_plans(self) -> list[str]:
        return self.plans

    def list_launches(self) -> list[LaunchRecord]:
        return sorted(self.launches.values(), key=lambda launch: launch.created_at, reverse=True)

    def get_active_launch(self) -> LaunchRecord | None:
        for launch in self.launches.values():
            if launch.status in {"starting", "running"}:
                return launch
        return None

    def create_launch(self, request) -> LaunchRecord:
        if self.raise_busy:
            raise LaunchBusyError("Another launch is still active.")
        launch = LaunchRecord(
            launch_id=f"launch-{len(self.launches) + 1}",
            created_at="2026-03-20T00:01:00+00:00",
            status="starting",
            task=request.task,
            script=request.script,
            launch_mode=request.launch_mode,
            model=request.model,
            input_url=request.input_url,
            follow_target_url=request.follow_target_url,
            proposal_target_url=request.proposal_target_url,
            max_steps=request.max_steps,
            pid=4242,
            run_id=None,
            summary_path="",
            exit_code=None,
            error="",
        )
        self.created_requests.append(request.to_dict())
        self.launches[launch.launch_id] = launch
        self.snapshots[launch.launch_id] = {
            "launch": launch.to_dict(),
            "timeline": [],
            "proposal_ids": [],
            "latest_screenshot": None,
            "summary_url": "",
            "related_artifacts": [],
            "log_tail": "",
            "version_token": "v1",
        }
        return launch

    def get_launch(self, launch_id: str) -> LaunchRecord:
        return self.launches[launch_id]

    def get_snapshot(self, launch_id: str) -> dict:
        return self.snapshots[launch_id]


def make_settings(tmp_path: Path) -> ConsoleSettings:
    workspace_dir = tmp_path / "agent_workspace"
    (workspace_dir / "run_outputs").mkdir(parents=True)
    (workspace_dir / "research").mkdir()
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


def write_demo_files(settings: ConsoleSettings) -> None:
    (settings.workspace_dir / "run_outputs" / "latest_seed_run.json").write_text(
        json.dumps(
            {
                "task": "read site",
                "success": True,
                "finished_reason": "planner_finished",
                "steps_executed": 2,
                "steps": [
                    {"step_index": 0, "kind": "bridge_status", "params": {}, "result": {"stage": "stage8"}},
                    {"step_index": 1, "kind": "write_file", "params": {"path": "research/current_real_site_brief.md"}, "result": {"bytes_written": 10}},
                ],
            }
        ),
        encoding="utf-8",
    )
    (settings.workspace_dir / "research" / "current_real_site_brief.md").write_text(
        "# Brief\n\n- item one\n",
        encoding="utf-8",
    )
    (settings.workspace_dir / "research" / "current_real_site_screenshot.png").write_bytes(b"\x89PNG\r\n\x1a\n")


def make_status() -> BridgeStatusReport:
    connection = ConnectionStatus(
        url="http://service",
        reachable=True,
        detail=None,
        checked_at="2026-03-19T22:00:00+00:00",
    )
    return BridgeStatusReport(
        service="bridge",
        stage="stage8_consequential_actions",
        trusted_state_dir="/var/lib/rsi/trusted_state",
        log_path="/var/lib/rsi/trusted_state/logs/bridge_events.jsonl",
        operational_state_path="/var/lib/rsi/trusted_state/state/operational_state.json",
        connections={
            "litellm": connection,
            "fetcher": connection,
            "browser": connection,
        },
        budget=BudgetState(
            unit="mock_tokens",
            total=100,
            spent=20,
            remaining=80,
            exhausted=False,
            minimum_call_cost=5,
            approximation="deterministic",
            total_prompt_tokens=10,
            total_completion_tokens=10,
            total_tokens=20,
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
            counters={"web_fetch_total": 1},
            recent_fetches=[],
        ),
        browser=BrowserState(
            service=connection,
            caps={"timeout_seconds": 10},
            counters={"browser_render_total": 1},
            recent_renders=[],
            recent_follows=[],
        ),
        counters={"status_queries": 1},
        recent_requests=[
            RecentRequest(
                timestamp="2026-03-19T22:00:00+00:00",
                event_type="status_query",
                request_id="req-1",
                trace_id="trace-1",
                actor="operator",
                source_service="bridge",
                outcome="success",
            )
        ],
        surfaces={"browser": "trusted_browser"},
        proposals=ProposalState(total=2, pending=1, approved=1),
    )


def make_proposals() -> list[ProposalRecord]:
    return [
        ProposalRecord(
            proposal_id="pending-1",
            action_type="http_post",
            action_payload={"url": "https://httpbin.org/post"},
            status="pending",
            created_by="agent",
            created_at="2026-03-19T22:01:00+00:00",
            request_id="req-pending",
            trace_id="trace-pending",
        ),
        ProposalRecord(
            proposal_id="approved-1",
            action_type="http_post",
            action_payload={"url": "https://httpbin.org/post"},
            status="approved",
            created_by="agent",
            created_at="2026-03-19T21:59:00+00:00",
            decided_by="operator",
            decided_at="2026-03-19T22:02:00+00:00",
            decision_reason="ok",
            request_id="req-approved",
            trace_id="trace-approved",
        ),
    ]


def make_running_launch() -> LaunchRecord:
    return LaunchRecord(
        launch_id="launch-1",
        created_at="2026-03-20T00:01:00+00:00",
        status="running",
        task="demo task",
        script="stage8_real_site_approval_demo.json",
        launch_mode="provider",
        model="openai/gpt-4.1-mini",
        input_url="https://httpbin.org/html",
        follow_target_url="",
        proposal_target_url="https://httpbin.org/post",
        max_steps=8,
        pid=4242,
        run_id="run-123",
        summary_path="run_outputs/run-123.json",
        exit_code=None,
        error="",
    )


def test_home_renders_status_active_launch_and_links(tmp_path: Path):
    settings = make_settings(tmp_path)
    write_demo_files(settings)
    launch_manager = FakeLaunchManager(settings)
    active_launch = make_running_launch()
    launch_manager.launches[active_launch.launch_id] = active_launch
    app = create_app(
        settings=settings,
        bridge_api=FakeBridgeAPI(status=make_status(), proposals=make_proposals()),
        repo_data=RepoData(settings),
        launch_manager=launch_manager,
    )

    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "Start Agent" in response.text
    assert "Active Launch" in response.text
    assert "launch-1" in response.text
    assert "Latest Pending Proposal" in response.text


@pytest.mark.fast
def test_home_renders_bridge_degraded_state(tmp_path: Path):
    settings = make_settings(tmp_path)
    write_demo_files(settings)
    app = create_app(
        settings=settings,
        bridge_api=FakeBridgeAPI(error="bridge unavailable"),
        repo_data=RepoData(settings),
        launch_manager=FakeLaunchManager(settings),
    )

    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "Bridge data unavailable." in response.text
    assert "bridge unavailable" in response.text


@pytest.mark.fast
def test_runs_page_renders_empty_and_non_empty_states(tmp_path: Path):
    settings = make_settings(tmp_path)
    app = create_app(
        settings=settings,
        bridge_api=FakeBridgeAPI(error="bridge unavailable"),
        repo_data=RepoData(settings),
        launch_manager=FakeLaunchManager(settings),
    )

    with TestClient(app) as client:
        empty_response = client.get("/runs")

    assert "No run output JSON files exist yet" in empty_response.text

    write_demo_files(settings)
    app = create_app(
        settings=settings,
        bridge_api=FakeBridgeAPI(error="bridge unavailable"),
        repo_data=RepoData(settings),
        launch_manager=FakeLaunchManager(settings),
    )
    with TestClient(app) as client:
        filled_response = client.get("/runs")

    assert "latest_seed_run.json" in filled_response.text
    assert "read site" in filled_response.text


@pytest.mark.fast
def test_launches_page_renders_form_and_recent_launches(tmp_path: Path):
    settings = make_settings(tmp_path)
    launch_manager = FakeLaunchManager(settings)
    launch_manager.launches["launch-1"] = make_running_launch()
    app = create_app(
        settings=settings,
        bridge_api=FakeBridgeAPI(status=make_status(), proposals=make_proposals()),
        repo_data=RepoData(settings),
        launch_manager=launch_manager,
    )

    with TestClient(app) as client:
        response = client.get("/launches")

    assert response.status_code == 200
    assert "Start Agent" in response.text
    assert "stage8_real_site_approval_demo.json" in response.text
    assert "name=\"launch_mode\"" in response.text
    assert "name=\"model\"" in response.text
    assert "launch-1" in response.text


@pytest.mark.fast
def test_post_launch_creates_launch_and_redirects(tmp_path: Path):
    settings = make_settings(tmp_path)
    launch_manager = FakeLaunchManager(settings)
    app = create_app(
        settings=settings,
        bridge_api=FakeBridgeAPI(status=make_status(), proposals=make_proposals()),
        repo_data=RepoData(settings),
        launch_manager=launch_manager,
    )

    with TestClient(app) as client:
        response = client.post(
            "/launches",
            data={
                "task": "demo task",
                "script": "stage8_real_site_approval_demo.json",
                "launch_mode": "provider",
                "model": "openai/gpt-4.1-mini",
                "input_url": "https://httpbin.org/html",
                "follow_target_url": "",
                "proposal_target_url": "https://httpbin.org/post",
                "max_steps": "8",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/launches/launch-1")
    assert launch_manager.created_requests[0]["launch_mode"] == "provider"
    assert launch_manager.created_requests[0]["proposal_target_url"] == "https://httpbin.org/post"


@pytest.mark.fast
def test_post_launch_blocks_when_another_launch_is_active(tmp_path: Path):
    settings = make_settings(tmp_path)
    launch_manager = FakeLaunchManager(settings)
    launch_manager.raise_busy = True
    app = create_app(
        settings=settings,
        bridge_api=FakeBridgeAPI(status=make_status(), proposals=make_proposals()),
        repo_data=RepoData(settings),
        launch_manager=launch_manager,
    )

    with TestClient(app) as client:
        response = client.post(
            "/launches",
            data={
                "task": "demo task",
                "script": "stage8_real_site_approval_demo.json",
                "launch_mode": "default",
                "model": "",
                "input_url": "",
                "follow_target_url": "",
                "proposal_target_url": "",
                "max_steps": "8",
            },
            follow_redirects=False,
        )
        follow = client.get(response.headers["location"])

    assert response.status_code == 303
    assert "Another launch is still active." in follow.text


@pytest.mark.fast
def test_launch_detail_and_api_render_timeline_and_proposals(tmp_path: Path):
    settings = make_settings(tmp_path)
    write_demo_files(settings)
    launch_manager = FakeLaunchManager(settings)
    launch = make_running_launch()
    launch_manager.launches[launch.launch_id] = launch
    launch_manager.snapshots[launch.launch_id] = {
        "launch": launch.to_dict(),
        "timeline": [
            {
                "timestamp": "2026-03-20T00:01:02+00:00",
                "event_kind": "step",
                "step_index": 1,
                "tool_name": "bridge_browser_render",
                "summary": {"page_title": "Demo"},
            },
            {
                "timestamp": "2026-03-20T00:01:03+00:00",
                "event_kind": "step",
                "step_index": 4,
                "tool_name": "bridge_create_proposal",
                "summary": {"proposal_id": "pending-1"},
            },
        ],
        "proposal_ids": ["pending-1"],
        "latest_screenshot": {
            "relative_path": "research/current_real_site_screenshot.png",
            "url": "/artifacts/research/current_real_site_screenshot.png",
            "name": "current_real_site_screenshot.png",
        },
        "summary_url": "/runs/latest_seed_run.json",
        "related_artifacts": [
            {
                "name": "current_real_site_brief.md",
                "relative_path": "research/current_real_site_brief.md",
                "kind": "markdown",
            }
        ],
        "log_tail": "stdout line",
        "version_token": "v2",
    }
    app = create_app(
        settings=settings,
        bridge_api=FakeBridgeAPI(status=make_status(), proposals=make_proposals()),
        repo_data=RepoData(settings),
        launch_manager=launch_manager,
    )

    with TestClient(app) as client:
        html_response = client.get("/launches/launch-1")
        api_response = client.get("/api/launches/launch-1")

    assert html_response.status_code == 200
    assert "Live Launch" in html_response.text
    assert "bridge_browser_render" in html_response.text
    assert "pending-1" in html_response.text
    assert "current_real_site_screenshot.png" in html_response.text
    assert api_response.status_code == 200
    assert api_response.json()["launch"]["launch_id"] == "launch-1"
    assert api_response.json()["timeline"][0]["tool_name"] == "bridge_browser_render"


@pytest.mark.fast
def test_proposal_detail_renders_action_buttons_by_status(tmp_path: Path):
    settings = make_settings(tmp_path)
    app = create_app(
        settings=settings,
        bridge_api=FakeBridgeAPI(status=make_status(), proposals=make_proposals()),
        repo_data=RepoData(settings),
        launch_manager=FakeLaunchManager(settings),
    )

    with TestClient(app) as client:
        pending = client.get("/proposals/pending-1")
        approved = client.get("/proposals/approved-1")

    assert "Approve" in pending.text
    assert "Reject" in pending.text
    assert "Execute" not in pending.text
    assert "Execute" in approved.text


@pytest.mark.fast
def test_proposal_action_routes_call_bridge_and_redirect(tmp_path: Path):
    settings = make_settings(tmp_path)
    bridge = FakeBridgeAPI(status=make_status(), proposals=make_proposals())
    app = create_app(
        settings=settings,
        bridge_api=bridge,
        repo_data=RepoData(settings),
        launch_manager=FakeLaunchManager(settings),
    )

    with TestClient(app) as client:
        approve = client.post(
            "/proposals/pending-1/approve",
            data={"reason": "looks good", "redirect_to": "/proposals/pending-1"},
            follow_redirects=False,
        )
        reject = client.post(
            "/proposals/pending-1/reject",
            data={"reason": "not now", "redirect_to": "/proposals/pending-1"},
            follow_redirects=False,
        )
        execute = client.post(
            "/proposals/approved-1/execute",
            data={"redirect_to": "/proposals/approved-1"},
            follow_redirects=False,
        )

    assert approve.status_code == 303
    assert reject.status_code == 303
    assert execute.status_code == 303
    assert bridge.decisions[0] == ("pending-1", "approve", "looks good")
    assert bridge.decisions[1] == ("pending-1", "reject", "not now")
    assert bridge.executions == ["approved-1"]


@pytest.mark.fast
def test_artifact_view_rejects_traversal_and_serves_allowed_files(tmp_path: Path):
    settings = make_settings(tmp_path)
    write_demo_files(settings)
    app = create_app(
        settings=settings,
        bridge_api=FakeBridgeAPI(error="bridge unavailable"),
        repo_data=RepoData(settings),
        launch_manager=FakeLaunchManager(settings),
    )

    with TestClient(app) as client:
        reject_response = client.get("/artifacts/../secrets.txt")
        markdown_response = client.get("/artifacts/research/current_real_site_brief.md")
        image_response = client.get("/artifacts/research/current_real_site_screenshot.png")

    assert reject_response.status_code == 404
    assert markdown_response.status_code == 200
    assert "<article class=\"markdown\">" in markdown_response.text
    assert image_response.status_code == 200
    assert image_response.headers["content-type"] == "image/png"
