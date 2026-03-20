import json
from pathlib import Path

import pytest

from operator_console.config import ConsoleSettings
from operator_console.launches import LaunchBusyError, LaunchManager, LaunchRequest
from operator_console.plan_catalog import build_launch_plan_options, default_launch_plan_name


def make_settings(tmp_path: Path) -> ConsoleSettings:
    workspace_dir = tmp_path / "agent_workspace"
    (workspace_dir / "run_outputs").mkdir(parents=True)
    (workspace_dir / "research").mkdir()
    (workspace_dir / ".seed_plans").mkdir()
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


def append_event(settings: ConsoleSettings, payload: dict) -> None:
    path = settings.trusted_log_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload) + "\n")


def write_run_summary(settings: ConsoleSettings, run_id: str) -> None:
    payload = {
        "run_id": run_id,
        "task": "demo task",
        "success": True,
        "finished_reason": "planner_finished",
        "finish_summary": "done",
        "steps_executed": 2,
        "steps": [
            {
                "step_index": 1,
                "kind": "write_binary_base64",
                "params": {"path": "research/current_real_site_screenshot.png"},
                "result": {"path": "research/current_real_site_screenshot.png", "bytes_written": 8},
            }
        ],
    }
    (settings.workspace_dir / "run_outputs" / f"{run_id}.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


def make_request(**overrides) -> LaunchRequest:
    payload = {
        "task": "demo task",
        "script": "stage8_real_site_approval_demo.json",
        "launch_mode": "default",
        "model": "",
        "input_url": "https://httpbin.org/html",
        "follow_target_url": "",
        "proposal_target_url": "https://httpbin.org/post",
        "max_steps": 8,
    }
    payload.update(overrides)
    return LaunchRequest(**payload)


@pytest.mark.fast
def test_list_seed_plans_returns_sorted_json_names(tmp_path: Path):
    settings = make_settings(tmp_path)
    (settings.workspace_dir / ".seed_plans" / "b.json").write_text("[]\n", encoding="utf-8")
    (settings.workspace_dir / ".seed_plans" / "a.json").write_text("[]\n", encoding="utf-8")
    (settings.workspace_dir / ".seed_plans" / "notes.txt").write_text("ignore\n", encoding="utf-8")

    manager = LaunchManager(settings)

    assert manager.list_seed_plans() == ["a.json", "b.json"]


@pytest.mark.fast
def test_build_launch_plan_options_prefers_answer_packet_and_flags_required_fields():
    options = build_launch_plan_options(
        [
            "stage3_local_task.json",
            "stage8_real_site_approval_demo.json",
            "stage6_answer_packet.json",
            "stage6_browser_demo.json",
        ]
    )

    assert [option.name for option in options] == [
        "stage6_answer_packet.json",
        "stage8_real_site_approval_demo.json",
        "stage3_local_task.json",
        "stage6_browser_demo.json",
    ]
    assert options[0].requires_input_url is True
    assert options[0].requires_proposal_target_url is False
    assert options[2].uses_fixed_urls is True
    assert default_launch_plan_name([option.name for option in options]) == "stage6_answer_packet.json"


@pytest.mark.fast
def test_build_launch_command_uses_default_and_provider_paths(tmp_path: Path):
    settings = make_settings(tmp_path)
    manager = LaunchManager(settings)

    default_argv = manager.build_launch_command(make_request())
    provider_argv = manager.build_launch_command(
        make_request(launch_mode="provider", model="openai/gpt-4.1-mini")
    )

    assert default_argv[:8] == [
        "docker",
        "compose",
        "exec",
        "-T",
        "agent",
        "python",
        "-m",
        "untrusted.agent.seed_runner",
    ]
    assert "--proposal-target-url" in default_argv
    assert provider_argv[:4] == ["bash", "./scripts/provider.sh", "seed-run", "--script"]
    assert "--proposal-target-url" in provider_argv
    assert "--model" in provider_argv


@pytest.mark.fast
def test_create_launch_persists_record_and_blocks_second_active_launch(tmp_path: Path):
    settings = make_settings(tmp_path)
    (settings.workspace_dir / ".seed_plans" / "stage8_real_site_approval_demo.json").write_text(
        "[]\n",
        encoding="utf-8",
    )
    calls: list[dict] = []

    def fake_runner(argv: list[str], *, log_path: Path, cwd: Path) -> int:
        calls.append({"argv": argv, "log_path": log_path, "cwd": cwd})
        log_path.write_text("launch started\n", encoding="utf-8")
        return 43210

    manager = LaunchManager(
        settings,
        runner=fake_runner,
        pid_checker=lambda pid: True,
        now_fn=lambda: "2026-03-20T00:10:00+00:00",
    )

    launch = manager.create_launch(make_request())

    assert launch.status == "starting"
    assert launch.pid == 43210
    assert calls[0]["cwd"] == settings.repo_root
    assert (settings.launches_dir / f"{launch.launch_id}.json").exists()

    with pytest.raises(LaunchBusyError):
        manager.create_launch(make_request(task="another task"))


@pytest.mark.fast
def test_refresh_launch_associates_run_events_and_latest_screenshot(tmp_path: Path):
    settings = make_settings(tmp_path)
    (settings.workspace_dir / ".seed_plans" / "stage8_real_site_approval_demo.json").write_text(
        "[]\n",
        encoding="utf-8",
    )

    manager = LaunchManager(
        settings,
        runner=lambda argv, *, log_path, cwd: 555,
        pid_checker=lambda pid: False,
        now_fn=lambda: "2026-03-20T00:20:00+00:00",
    )
    launch = manager.create_launch(make_request())
    (settings.launch_logs_dir / f"{launch.launch_id}.log").write_text(
        "stdout line\n__RSI_EXIT_CODE__=0\n",
        encoding="utf-8",
    )

    run_id = "run-123"
    append_event(
        settings,
        {
            "timestamp": "2026-03-20T00:20:01+00:00",
            "actor": "agent",
            "event_type": "agent_run",
            "outcome": "recorded",
            "request_id": "req-start",
            "trace_id": "trace-start",
            "source_service": "bridge",
            "summary": {
                "event_kind": "run_start",
                "run_id": run_id,
                "step_index": None,
                "tool_name": None,
                "reported_summary": {
                    "task": "demo task",
                    "input_url": "https://httpbin.org/html",
                    "follow_target_url": "",
                    "proposal_target_url": "https://httpbin.org/post",
                },
            },
        },
    )
    append_event(
        settings,
        {
            "timestamp": "2026-03-20T00:20:02+00:00",
            "actor": "agent",
            "event_type": "agent_run",
            "outcome": "recorded",
            "request_id": "req-step",
            "trace_id": "trace-step",
            "source_service": "bridge",
            "summary": {
                "event_kind": "step",
                "run_id": run_id,
                "step_index": 4,
                "tool_name": "bridge_create_proposal",
                "reported_summary": {
                    "step_kind": "bridge_create_proposal",
                    "result": {
                        "proposal_id": "proposal-1",
                        "status": "pending",
                        "action_type": "http_post",
                        "target_url": "https://httpbin.org/post",
                    },
                },
            },
        },
    )
    append_event(
        settings,
        {
            "timestamp": "2026-03-20T00:20:03+00:00",
            "actor": "agent",
            "event_type": "agent_run",
            "outcome": "recorded",
            "request_id": "req-end",
            "trace_id": "trace-end",
            "source_service": "bridge",
            "summary": {
                "event_kind": "run_end",
                "run_id": run_id,
                "step_index": 6,
                "tool_name": None,
                "reported_summary": {
                    "success": True,
                    "finished_reason": "planner_finished",
                    "finish_summary": "done",
                    "summary_path": f"run_outputs/{run_id}.json",
                },
            },
        },
    )
    write_run_summary(settings, run_id)
    (settings.workspace_dir / "research" / "current_real_site_screenshot.png").write_bytes(
        b"\x89PNG\r\n\x1a\n",
    )

    refreshed = manager.get_launch(launch.launch_id)
    snapshot = manager.get_snapshot(launch.launch_id)

    assert refreshed.run_id == run_id
    assert refreshed.summary_path == f"run_outputs/{run_id}.json"
    assert refreshed.status == "finished"
    assert refreshed.exit_code == 0
    assert snapshot["proposal_ids"] == ["proposal-1"]
    assert snapshot["latest_screenshot"]["relative_path"] == "research/current_real_site_screenshot.png"
    assert snapshot["current_screenshot"]["relative_path"] == "research/current_real_site_screenshot.png"
    assert snapshot["recent_screenshots"][0]["relative_path"] == "research/current_real_site_screenshot.png"
    assert snapshot["timeline"][-1]["event_kind"] == "run_end"


@pytest.mark.fast
def test_refresh_launch_marks_failed_when_process_exits_without_summary(tmp_path: Path):
    settings = make_settings(tmp_path)
    (settings.workspace_dir / ".seed_plans" / "stage8_real_site_approval_demo.json").write_text(
        "[]\n",
        encoding="utf-8",
    )
    manager = LaunchManager(
        settings,
        runner=lambda argv, *, log_path, cwd: 777,
        pid_checker=lambda pid: False,
        now_fn=lambda: "2026-03-20T00:30:00+00:00",
    )
    launch = manager.create_launch(make_request())
    (settings.launch_logs_dir / f"{launch.launch_id}.log").write_text(
        "error line\n__RSI_EXIT_CODE__=2\n",
        encoding="utf-8",
    )

    refreshed = manager.get_launch(launch.launch_id)

    assert refreshed.status == "failed"
    assert refreshed.exit_code == 2
    assert "exited" in refreshed.error


@pytest.mark.fast
def test_active_launch_snapshot_uses_live_image_scan_before_summary_exists(tmp_path: Path):
    settings = make_settings(tmp_path)
    (settings.workspace_dir / ".seed_plans" / "stage6_answer_packet.json").write_text(
        "[]\n",
        encoding="utf-8",
    )
    manager = LaunchManager(
        settings,
        runner=lambda argv, *, log_path, cwd: 888,
        pid_checker=lambda pid: True,
        now_fn=lambda: "2026-03-20T00:40:00+00:00",
    )
    launch = manager.create_launch(
        make_request(
            script="stage6_answer_packet.json",
            proposal_target_url="",
        )
    )
    image_path = settings.workspace_dir / "research" / "live_capture.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n")

    snapshot = manager.get_snapshot(launch.launch_id)

    assert snapshot["summary_url"] == ""
    assert snapshot["current_screenshot"]["relative_path"] == "research/live_capture.png"
    assert snapshot["recent_screenshots"][0]["relative_path"] == "research/live_capture.png"


@pytest.mark.fast
def test_failed_launch_snapshot_does_not_inherit_stale_images(tmp_path: Path):
    settings = make_settings(tmp_path)
    (settings.workspace_dir / ".seed_plans" / "stage6_answer_packet.json").write_text(
        "[]\n",
        encoding="utf-8",
    )
    manager = LaunchManager(
        settings,
        runner=lambda argv, *, log_path, cwd: 999,
        pid_checker=lambda pid: False,
        now_fn=lambda: "2026-03-20T00:50:00+00:00",
    )
    launch = manager.create_launch(
        make_request(
            script="stage6_answer_packet.json",
            proposal_target_url="",
        )
    )
    (settings.workspace_dir / "research" / "old_capture.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (settings.launch_logs_dir / f"{launch.launch_id}.log").write_text(
        "stderr line\n__RSI_EXIT_CODE__=1\n",
        encoding="utf-8",
    )

    snapshot = manager.get_snapshot(launch.launch_id)

    assert snapshot["launch"]["status"] == "failed"
    assert snapshot["current_screenshot"] is None
    assert snapshot["recent_screenshots"] == []
