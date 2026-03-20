import asyncio
import json
from pathlib import Path
import subprocess

import httpx
import pytest

from operator_console.bridge_api import BridgeAPI, BridgeAPIError, BridgeUnavailableError
from operator_console.config import ConsoleSettings, ROOT, console_settings
from operator_console.data import RepoData, artifact_kind


def make_workspace(tmp_path: Path) -> tuple[Path, Path]:
    workspace_dir = tmp_path / "agent_workspace"
    (workspace_dir / "run_outputs").mkdir(parents=True)
    (workspace_dir / "research").mkdir()
    trusted_state_dir = tmp_path / "trusted_state"
    (trusted_state_dir / "logs").mkdir(parents=True)
    return workspace_dir, trusted_state_dir


def write_run(path: Path, *, task: str, steps_executed: int, success: bool) -> None:
    path.write_text(
        json.dumps(
            {
                "task": task,
                "success": success,
                "finished_reason": "planner_finished",
                "steps_executed": steps_executed,
                "steps": [],
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.fast
def test_console_settings_defaults(monkeypatch):
    monkeypatch.delenv("RSI_OPERATOR_CONSOLE_BRIDGE_URL", raising=False)
    monkeypatch.delenv("RSI_OPERATOR_TOKEN", raising=False)
    monkeypatch.delenv("RSI_OPERATOR_CONSOLE_WORKSPACE_DIR", raising=False)
    monkeypatch.delenv("RSI_OPERATOR_CONSOLE_TRUSTED_STATE_DIR", raising=False)

    settings = console_settings()

    assert settings.bridge_url == "http://127.0.0.1:8000"
    assert settings.operator_token is None
    assert settings.workspace_dir == ROOT / "untrusted" / "agent_workspace"
    assert settings.trusted_state_dir == ROOT / "runtime" / "trusted_state"
    assert settings.operator_runtime_dir == ROOT / "runtime" / "operator_console"


@pytest.mark.fast
def test_console_settings_honor_relative_and_absolute_overrides(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("RSI_OPERATOR_CONSOLE_BRIDGE_URL", "http://127.0.0.1:8123")
    monkeypatch.setenv("RSI_OPERATOR_TOKEN", "token-1")
    monkeypatch.setenv("RSI_OPERATOR_CONSOLE_WORKSPACE_DIR", "tmp/workspace")
    monkeypatch.setenv("RSI_OPERATOR_CONSOLE_TRUSTED_STATE_DIR", str(tmp_path / "trusted"))

    settings = console_settings()

    assert settings.bridge_url == "http://127.0.0.1:8123"
    assert settings.operator_token == "token-1"
    assert settings.workspace_dir == ROOT / "tmp" / "workspace"
    assert settings.trusted_state_dir == tmp_path / "trusted"
    assert settings.operator_runtime_dir == ROOT / "runtime" / "operator_console"


@pytest.mark.fast
def test_resolve_artifact_path_rejects_escape(tmp_path: Path):
    workspace_dir, trusted_state_dir = make_workspace(tmp_path)
    repo = RepoData(
        ConsoleSettings(
            bridge_url="http://127.0.0.1:8000",
            operator_token="token",
            workspace_dir=workspace_dir,
            trusted_state_dir=trusted_state_dir,
        )
    )

    with pytest.raises(ValueError):
        repo.resolve_artifact_path("../secrets.txt")


@pytest.mark.fast
def test_list_run_summaries_sorts_newest_first(tmp_path: Path):
    workspace_dir, trusted_state_dir = make_workspace(tmp_path)
    older = workspace_dir / "run_outputs" / "older.json"
    newer = workspace_dir / "run_outputs" / "newer.json"
    write_run(older, task="older task", steps_executed=1, success=True)
    write_run(newer, task="newer task", steps_executed=2, success=False)
    older.touch()
    newer.touch()

    repo = RepoData(
        ConsoleSettings(
            bridge_url="http://127.0.0.1:8000",
            operator_token="token",
            workspace_dir=workspace_dir,
            trusted_state_dir=trusted_state_dir,
        )
    )
    runs = repo.list_run_summaries()

    assert [run.name for run in runs] == ["newer.json", "older.json"]
    assert runs[0].task == "newer task"
    assert runs[1].steps_executed == 1


@pytest.mark.fast
def test_artifact_kind_detects_markdown_text_and_image(tmp_path: Path):
    markdown_path = tmp_path / "brief.md"
    text_path = tmp_path / "payload.json"
    image_path = tmp_path / "shot.png"
    markdown_path.write_text("# hi\n", encoding="utf-8")
    text_path.write_text('{"ok": true}\n', encoding="utf-8")
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n")

    assert artifact_kind(markdown_path) == "markdown"
    assert artifact_kind(text_path) == "text"
    assert artifact_kind(image_path) == "image"


@pytest.mark.fast
def test_bridge_api_missing_token_fails_closed():
    client = BridgeAPI(base_url="http://127.0.0.1:8000", operator_token=None)

    with pytest.raises(BridgeUnavailableError):
        asyncio.run(client.get_status())


@pytest.mark.fast
def test_bridge_api_request_error_is_reported_cleanly():
    transport = httpx.MockTransport(lambda request: (_ for _ in ()).throw(httpx.ConnectError("boom", request=request)))
    client = BridgeAPI(
        base_url="http://127.0.0.1:8000",
        operator_token="token",
        transport=transport,
    )

    with pytest.raises(BridgeUnavailableError) as exc:
        asyncio.run(client.get_status())

    assert "bridge unavailable" in str(exc.value)


@pytest.mark.fast
def test_bridge_api_http_error_raises_detail():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": "forbidden"})

    client = BridgeAPI(
        base_url="http://127.0.0.1:8000",
        operator_token="token",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(BridgeAPIError) as exc:
        asyncio.run(client.list_proposals())

    assert "forbidden" in str(exc.value)


@pytest.mark.fast
def test_bridge_api_can_decide_and_execute_proposals():
    responses = [
        httpx.Response(
            200,
            json={
                "proposal_id": "proposal-1",
                "action_type": "http_post",
                "action_payload": {"url": "https://httpbin.org/post"},
                "status": "approved",
                "created_by": "agent",
                "created_at": "2026-03-20T00:00:00+00:00",
                "decided_by": "operator",
                "decided_at": "2026-03-20T00:01:00+00:00",
                "decision_reason": "ok",
                "request_id": "req-1",
                "trace_id": "trace-1",
            },
        ),
        httpx.Response(
            200,
            json={
                "proposal_id": "proposal-1",
                "action_type": "http_post",
                "action_payload": {"url": "https://httpbin.org/post"},
                "status": "executed",
                "created_by": "agent",
                "created_at": "2026-03-20T00:00:00+00:00",
                "decided_by": "operator",
                "decided_at": "2026-03-20T00:01:00+00:00",
                "decision_reason": "ok",
                "executed_by": "operator",
                "executed_at": "2026-03-20T00:02:00+00:00",
                "execution_result": {"http_status": 200},
                "request_id": "req-1",
                "trace_id": "trace-1",
            },
        ),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    client = BridgeAPI(
        base_url="http://127.0.0.1:8000",
        operator_token="token",
        transport=httpx.MockTransport(handler),
    )

    approved = asyncio.run(client.decide_proposal("proposal-1", decision="approve", reason="ok"))
    executed = asyncio.run(client.execute_proposal("proposal-1"))

    assert approved.status == "approved"
    assert executed.status == "executed"


@pytest.mark.fast
def test_bridge_api_falls_back_to_docker_compose_exec():
    transport = httpx.MockTransport(lambda request: (_ for _ in ()).throw(httpx.ConnectError("boom", request=request)))

    def fake_runner(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=json.dumps(
                {
                    "status_code": 200,
                    "body": {
                        "service": "bridge",
                        "stage": "stage8",
                        "trusted_state_dir": "/tmp/trusted",
                        "log_path": "/tmp/log.jsonl",
                        "operational_state_path": "/tmp/state.json",
                        "connections": {},
                        "budget": {
                            "unit": "mock_tokens",
                            "total": 100,
                            "spent": 0,
                            "remaining": 100,
                            "exhausted": False,
                            "minimum_call_cost": 1,
                            "approximation": "deterministic",
                            "total_prompt_tokens": 0,
                            "total_completion_tokens": 0,
                            "total_tokens": 0,
                        },
                        "recovery": {
                            "checkpoint_dir": "/tmp/checkpoints",
                            "baseline_id": "baseline-1",
                            "baseline_source_dir": "/tmp/source",
                            "baseline_archive_path": "/tmp/archive",
                            "available_checkpoints": [],
                            "current_workspace_status": "seed_baseline",
                        },
                        "web": {
                            "fetcher": {
                                "url": "http://fetcher",
                                "reachable": True,
                                "detail": None,
                                "checked_at": None,
                            },
                            "allowlist_hosts": [],
                            "private_test_hosts": [],
                            "allowed_content_types": [],
                            "caps": {},
                            "counters": {},
                            "recent_fetches": [],
                        },
                        "browser": {
                            "service": {
                                "url": "http://browser",
                                "reachable": True,
                                "detail": None,
                                "checked_at": None,
                            },
                            "caps": {},
                            "counters": {},
                            "recent_renders": [],
                            "recent_follows": [],
                        },
                        "counters": {},
                        "recent_requests": [],
                        "surfaces": {},
                        "proposals": {
                            "total": 0,
                            "pending": 0,
                            "approved": 0,
                            "rejected": 0,
                            "executing": 0,
                            "executed": 0,
                            "failed": 0,
                            "recent_proposals": [],
                        },
                    },
                }
            ),
            stderr="",
        )

    client = BridgeAPI(
        base_url="http://127.0.0.1:8000",
        operator_token="token",
        transport=transport,
        command_runner=fake_runner,
    )

    # transport is present here, so fallback should stay disabled
    with pytest.raises(BridgeUnavailableError):
        asyncio.run(client.get_status())

    client = BridgeAPI(
        base_url="http://127.0.0.1:8000",
        operator_token="token",
        command_runner=fake_runner,
    )
    status = asyncio.run(client.get_status())

    assert status.stage == "stage8"
