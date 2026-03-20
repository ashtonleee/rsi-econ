import json
from pathlib import Path

import pytest

from operator_console.config import ConsoleSettings
from operator_console.sessions import SessionBusyError, SessionCreateRequest, SessionManager
from shared.schemas import ProposalRecord


def make_settings(tmp_path: Path) -> ConsoleSettings:
    workspace_dir = tmp_path / "agent_workspace"
    (workspace_dir / "run_outputs").mkdir(parents=True)
    (workspace_dir / "research").mkdir()
    (workspace_dir / "sessions").mkdir()
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


def write_workspace_state(settings: ConsoleSettings, session_id: str, payload: dict) -> None:
    session_dir = settings.workspace_dir / "sessions" / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "state.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (session_dir / "transcript.jsonl").write_text("", encoding="utf-8")


def make_request(**overrides) -> SessionCreateRequest:
    payload = {
        "task": "Read this page and answer in one sentence.",
        "launch_mode": "default",
        "model": "",
        "input_url": "https://httpbin.org/html",
        "proposal_target_url": "",
        "max_turns_per_resume": 4,
    }
    payload.update(overrides)
    return SessionCreateRequest(**payload)


@pytest.mark.fast
def test_build_session_command_uses_default_and_provider_paths(tmp_path: Path):
    settings = make_settings(tmp_path)
    manager = SessionManager(settings)

    default_argv = manager.build_session_command(make_request(), session_id="session-1")
    provider_argv = manager.build_session_command(
        make_request(launch_mode="provider", model="openai/gpt-4.1-mini"),
        session_id="session-2",
    )

    assert default_argv[:8] == [
        "docker",
        "compose",
        "exec",
        "-T",
        "agent",
        "python",
        "-m",
        "untrusted.agent.session_runner",
    ]
    assert provider_argv[:3] == ["bash", "./scripts/provider.sh", "session-run"]
    assert "--model" in provider_argv


@pytest.mark.fast
def test_create_session_persists_record_and_blocks_second_active_session(tmp_path: Path):
    settings = make_settings(tmp_path)
    calls: list[dict] = []

    def fake_runner(argv: list[str], *, log_path: Path, cwd: Path) -> int:
        calls.append({"argv": argv, "log_path": log_path, "cwd": cwd})
        log_path.write_text("session started\n", encoding="utf-8")
        return 43210

    manager = SessionManager(
        settings,
        runner=fake_runner,
        pid_checker=lambda pid: True,
        now_fn=lambda: "2026-03-20T00:10:00+00:00",
    )

    session = manager.create_session(make_request())

    assert session.status == "starting"
    assert session.pid == 43210
    assert calls[0]["cwd"] == settings.repo_root
    assert (settings.operator_runtime_dir / "sessions" / f"{session.session_id}.json").exists()

    with pytest.raises(SessionBusyError):
        manager.create_session(make_request(task="another task"))


@pytest.mark.fast
def test_session_snapshot_derives_waiting_state_and_transcript(tmp_path: Path):
    settings = make_settings(tmp_path)
    manager = SessionManager(
        settings,
        runner=lambda argv, *, log_path, cwd: 555,
        pid_checker=lambda pid: False,
        now_fn=lambda: "2026-03-20T00:20:00+00:00",
    )
    session = manager.create_session(make_request(proposal_target_url="https://httpbin.org/post"))
    write_workspace_state(
        settings,
        session.session_id,
        {
            "session_id": session.session_id,
            "status": "waiting_for_approval",
            "task": session.task,
            "input_url": session.input_url,
            "proposal_target_url": session.proposal_target_url,
            "launch_mode": session.launch_mode,
            "model": session.model,
            "resume_count": 0,
            "current_run_id": "run-1",
            "last_run_id": "run-1",
            "summary_path": "run_outputs/run-1.json",
            "last_proposal": {
                "proposal_id": "proposal-1",
                "status": "pending",
                "action_type": "http_post",
                "action_payload": {"url": "https://httpbin.org/post"},
            },
            "current_screenshot_path": "sessions/%s/artifacts/turn_001_browser.png" % session.session_id,
        },
    )
    transcript_path = settings.workspace_dir / "sessions" / session.session_id / "transcript.jsonl"
    transcript_path.write_text(
        json.dumps(
            {
                "kind": "tool_result",
                "run_id": "run-1",
                "tool": "bridge_create_proposal",
                "reason": "Need approval.",
                "result": {"proposal_id": "proposal-1", "status": "pending"},
                "timestamp": "2026-03-20T00:20:01+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    artifact_path = settings.workspace_dir / "sessions" / session.session_id / "artifacts" / "turn_001_browser.png"
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_bytes(b"\x89PNG\r\n\x1a\n")
    (settings.operator_runtime_dir / "session_logs" / f"{session.session_id}.log").write_text(
        "session log line\n__RSI_EXIT_CODE__=0\n",
        encoding="utf-8",
    )

    snapshot = manager.get_snapshot(session.session_id)

    assert snapshot["session"]["status"] == "waiting_for_approval"
    assert snapshot["phase_label"] == "waiting_for_approval"
    assert snapshot["current_screenshot"]["relative_path"].endswith("turn_001_browser.png")
    assert snapshot["transcript"][0]["tool"] == "bridge_create_proposal"


@pytest.mark.fast
def test_apply_proposal_update_syncs_state_and_auto_resumes(tmp_path: Path):
    settings = make_settings(tmp_path)
    calls: list[list[str]] = []

    def fake_runner(argv: list[str], *, log_path: Path, cwd: Path) -> int:
        calls.append(argv)
        log_path.write_text("resuming\n", encoding="utf-8")
        return 555

    manager = SessionManager(
        settings,
        runner=fake_runner,
        pid_checker=lambda pid: False,
        now_fn=lambda: "2026-03-20T00:30:00+00:00",
    )
    session = manager.create_session(make_request(proposal_target_url="https://httpbin.org/post"))
    write_workspace_state(
        settings,
        session.session_id,
        {
            "session_id": session.session_id,
            "status": "waiting_for_approval",
            "task": session.task,
            "input_url": session.input_url,
            "proposal_target_url": session.proposal_target_url,
            "launch_mode": session.launch_mode,
            "model": session.model,
            "resume_count": 0,
            "current_run_id": "run-1",
            "last_run_id": "run-1",
            "last_proposal": {
                "proposal_id": "proposal-1",
                "status": "pending",
                "action_type": "http_post",
                "action_payload": {"url": "https://httpbin.org/post"},
            },
        },
    )
    approved = ProposalRecord(
        proposal_id="proposal-1",
        action_type="http_post",
        action_payload={"url": "https://httpbin.org/post"},
        status="approved",
        created_by="agent",
        created_at="2026-03-20T00:00:00+00:00",
        decided_by="operator",
        decided_at="2026-03-20T00:31:00+00:00",
        decision_reason="ok",
        request_id="req-1",
        trace_id="trace-1",
    )

    updated = manager.apply_proposal_update(approved)
    state = json.loads((settings.workspace_dir / "sessions" / session.session_id / "state.json").read_text(encoding="utf-8"))

    assert updated is not None
    assert updated.status == "resuming"
    assert state["last_proposal"]["status"] == "approved"
    assert calls[0][-2:] == ["--session-id", session.session_id]
