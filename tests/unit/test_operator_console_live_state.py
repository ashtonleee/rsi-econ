from operator_console.launches import LaunchRecord
from operator_console.live_state import build_live_snapshot
from shared.schemas import ProposalRecord

import pytest


def make_launch(**overrides) -> LaunchRecord:
    payload = {
        "launch_id": "launch-1",
        "created_at": "2026-03-20T00:01:00+00:00",
        "status": "running",
        "task": "read and summarize",
        "script": "stage6_answer_packet.json",
        "launch_mode": "default",
        "model": "",
        "input_url": "https://blocked.example/article",
        "follow_target_url": "",
        "proposal_target_url": "https://httpbin.org/post",
        "max_steps": 8,
        "pid": 4242,
        "run_id": "run-1",
        "summary_path": "",
        "exit_code": None,
        "error": "",
    }
    payload.update(overrides)
    return LaunchRecord(**payload)


def make_snapshot(**overrides) -> dict:
    payload = {
        "launch": make_launch().to_dict(),
        "timeline": [],
        "proposal_ids": [],
        "related_artifacts": [],
        "summary_url": "",
        "log_tail": "",
        "version_token": "v1",
        "recent_screenshots": [],
        "current_screenshot": None,
        "latest_screenshot": None,
    }
    payload.update(overrides)
    return payload


def make_proposal(status: str) -> ProposalRecord:
    return ProposalRecord(
        proposal_id=f"{status}-proposal",
        action_type="http_post",
        action_payload={"url": "https://httpbin.org/post"},
        status=status,
        created_by="agent",
        created_at="2026-03-20T00:02:00+00:00",
        request_id="req-1",
        trace_id="trace-1",
    )


@pytest.mark.fast
def test_live_snapshot_classifies_browsing_state():
    snapshot = make_snapshot(
        timeline=[
            {
                "timestamp": "2026-03-20T00:01:02+00:00",
                "event_kind": "step",
                "run_id": "run-1",
                "step_index": 1,
                "tool_name": "bridge_browser_render",
                "summary": {
                    "final_url": "https://blocked.example/article",
                    "http_status": 200,
                    "page_title": "Example page",
                },
            }
        ]
    )

    enriched = build_live_snapshot(snapshot, allowlist_hosts=["httpbin.org"])

    assert enriched["phase_label"] == "browsing"
    assert enriched["phase_tone"] == "ok"
    assert "reading a page" in enriched["headline"].lower()


@pytest.mark.fast
def test_live_snapshot_classifies_waiting_for_approval_state():
    snapshot = make_snapshot()

    enriched = build_live_snapshot(
        snapshot,
        related_proposals=[make_proposal("pending")],
        allowlist_hosts=["httpbin.org"],
    )

    assert enriched["phase_label"] == "waiting_for_approval"
    assert enriched["next_action"].lower().startswith("review the pending proposal")
    assert any(diag["title"] == "Waiting for approval" for diag in enriched["diagnostics"])


@pytest.mark.fast
def test_live_snapshot_classifies_ready_to_execute_and_executed_states():
    approved = build_live_snapshot(
        make_snapshot(),
        related_proposals=[make_proposal("approved")],
        allowlist_hosts=["httpbin.org"],
    )
    executed = build_live_snapshot(
        make_snapshot(launch=make_launch(status="finished", summary_path="run_outputs/run-1.json").to_dict()),
        related_proposals=[make_proposal("executed")],
        allowlist_hosts=["httpbin.org"],
    )

    assert approved["phase_label"] == "ready_to_execute"
    assert any(diag["title"] == "Ready to execute" for diag in approved["diagnostics"])
    assert executed["phase_label"] == "executed"
    assert executed["phase_tone"] == "ok"


@pytest.mark.fast
def test_live_snapshot_classifies_failed_launch_and_adds_friendly_diagnostic():
    snapshot = make_snapshot(
        launch=make_launch(
            status="failed",
            exit_code=2,
            error="launch exited before a run summary appeared",
        ).to_dict(),
    )

    enriched = build_live_snapshot(snapshot, allowlist_hosts=["httpbin.org"])

    assert enriched["phase_label"] == "failed"
    assert enriched["phase_tone"] == "bad"
    assert any(diag["title"] == "Launch failed before summary" for diag in enriched["diagnostics"])


@pytest.mark.fast
def test_live_snapshot_adds_allowlist_and_missing_target_diagnostics():
    blocked = build_live_snapshot(
        make_snapshot(
            launch=make_launch(
                status="failed",
                error="HTTPStatusError: Client error '403 Forbidden' for url 'http://bridge:8000/web/browser/render'",
            ).to_dict(),
            timeline=[
                {
                    "timestamp": "2026-03-20T00:01:02+00:00",
                    "event_kind": "step",
                    "run_id": "run-1",
                    "step_index": 1,
                    "tool_name": "bridge_browser_render",
                    "summary": {"detail": "HTTPStatusError: Client error '403 Forbidden'"},
                }
            ],
        ),
        allowlist_hosts=["httpbin.org"],
    )
    missing_target = build_live_snapshot(
        make_snapshot(
            launch=make_launch(
                script="stage8_real_site_approval_demo.json",
                proposal_target_url="",
            ).to_dict(),
        ),
        allowlist_hosts=["httpbin.org"],
    )

    blocked_titles = [diag["title"] for diag in blocked["diagnostics"]]
    missing_titles = [diag["title"] for diag in missing_target["diagnostics"]]

    assert "Host not allowlisted" in blocked_titles
    assert any("blocked.example" in diag["body"] for diag in blocked["diagnostics"])
    assert "Proposal target missing" in missing_titles


@pytest.mark.fast
def test_live_snapshot_adds_bridge_unavailable_diagnostic():
    enriched = build_live_snapshot(
        make_snapshot(),
        bridge_error="bridge unavailable",
    )

    assert any(diag["title"] == "Bridge unavailable" for diag in enriched["diagnostics"])


@pytest.mark.fast
def test_live_snapshot_maps_timeline_items_to_display_text_and_tone():
    snapshot = make_snapshot(
        launch=make_launch(status="finished", summary_path="run_outputs/run-1.json").to_dict(),
        timeline=[
            {
                "timestamp": "2026-03-20T00:01:00+00:00",
                "event_kind": "step",
                "run_id": "run-1",
                "step_index": 1,
                "tool_name": "bridge_browser_render",
                "summary": {
                    "final_url": "https://example.com/article",
                    "http_status": 200,
                    "page_title": "Example page",
                },
            },
            {
                "timestamp": "2026-03-20T00:01:01+00:00",
                "event_kind": "step",
                "run_id": "run-1",
                "step_index": 2,
                "tool_name": "bridge_create_proposal",
                "summary": {
                    "proposal_id": "pending-1",
                    "status": "pending",
                    "target_url": "https://httpbin.org/post",
                },
            },
            {
                "timestamp": "2026-03-20T00:01:02+00:00",
                "event_kind": "step",
                "run_id": "run-1",
                "step_index": 3,
                "tool_name": "bridge_browser_render",
                "summary": {"detail": "HTTPStatusError: Client error '403 Forbidden'"},
            },
            {
                "timestamp": "2026-03-20T00:01:03+00:00",
                "event_kind": "run_end",
                "run_id": "run-1",
                "step_index": 4,
                "tool_name": None,
                "summary": {
                    "success": True,
                    "finished_reason": "planner_finished",
                    "finish_summary": "done",
                },
            },
        ],
    )

    enriched = build_live_snapshot(snapshot, allowlist_hosts=["httpbin.org"])
    items = {(item["title"], item["tone"]) for item in enriched["timeline"]}

    assert ("Opened page", "ok") in items
    assert ("Prepared approval request", "warn") in items
    assert ("Step failed", "bad") in items
    assert ("Run finished", "ok") in items
