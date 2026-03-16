import json
import multiprocessing
from pathlib import Path

import pytest

from trusted.state.store import TrustedStateManager


def load_lines(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="ascii").splitlines()
        if line.strip()
    ]


def _append_events_worker(
    log_path: str,
    state_path: str,
    start_event,
    error_queue,
    worker_id: int,
    count: int,
):
    manager = TrustedStateManager(
        canonical_log_path=Path(log_path),
        operational_state_path=Path(state_path),
        budget_total=100,
        budget_unit="mock_tokens",
        stage="stage6_read_only_browser",
        surfaces={"canonical_logging": "active_canonical_event_log"},
    )
    start_event.wait()
    try:
        for index in range(count):
            event_id = f"worker-{worker_id}-event-{index}"
            manager.append_event(
                event_type="status_query",
                actor="agent",
                source_service="bridge",
                request_id=event_id,
                trace_id=event_id,
                outcome="success",
                summary={"query": "status", "worker_id": worker_id, "index": index},
            )
    except Exception as exc:
        error_queue.put(f"{type(exc).__name__}: {exc}")


@pytest.mark.fast
@pytest.mark.parametrize(
    "preexisting_state",
    [
        "{not valid json\n",
        json.dumps(
            {
                "marker": "stale_state",
                "budget": {"spent": 999, "remaining": 0, "exhausted": True},
                "recent_requests": [{"request_id": "stale-request"}],
            }
        )
        + "\n",
    ],
    ids=["invalid-json", "stale-json"],
)
def test_trusted_state_manager_rebuilds_from_canonical_log_when_snapshot_is_corrupt(
    tmp_path,
    preexisting_state,
):
    log_path = tmp_path / "logs" / "bridge_events.jsonl"
    state_path = tmp_path / "state" / "operational_state.json"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(preexisting_state, encoding="ascii")

    canonical_events = [
        {
            "timestamp": "2026-03-16T00:00:00+00:00",
            "event_type": "llm_call",
            "request_id": "req-llm",
            "trace_id": "trace-llm",
            "actor": "agent",
            "source_service": "bridge",
            "outcome": "success",
            "summary": {
                "model": "stage2-deterministic",
                "usage": {
                    "prompt_tokens": 4,
                    "completion_tokens": 3,
                    "total_tokens": 7,
                },
            },
        },
        {
            "timestamp": "2026-03-16T00:00:01+00:00",
            "event_type": "budget_update",
            "request_id": "req-llm",
            "trace_id": "trace-llm",
            "actor": "bridge",
            "source_service": "bridge",
            "outcome": "success",
            "summary": {
                "budget": {
                    "total": 30,
                    "spent": 7,
                    "remaining": 23,
                    "exhausted": False,
                },
                "usage": {
                    "total_prompt_tokens": 4,
                    "total_completion_tokens": 3,
                    "total_tokens": 7,
                },
            },
        },
    ]
    log_path.write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in canonical_events),
        encoding="ascii",
    )

    manager = TrustedStateManager(
        canonical_log_path=log_path,
        operational_state_path=state_path,
        budget_total=30,
        budget_unit="mock_tokens",
        stage="stage6_read_only_browser",
        surfaces={"canonical_logging": "active_canonical_event_log"},
    )

    snapshot = manager.snapshot()
    rewritten = json.loads(state_path.read_text(encoding="ascii"))

    assert snapshot["budget"]["spent"] == 7
    assert snapshot["budget"]["remaining"] == 23
    assert snapshot["budget"]["total_tokens"] == 7
    assert snapshot["counters"]["llm_calls_total"] == 1
    assert snapshot["counters"]["budget_updates"] == 1
    assert snapshot["recent_requests"][0]["event_type"] == "budget_update"
    assert snapshot == rewritten
    assert rewritten.get("marker") != "stale_state"
    assert rewritten["recent_requests"][0]["request_id"] == "req-llm"


def test_trusted_state_manager_materializes_operational_state_from_canonical_log(tmp_path):
    log_path = tmp_path / "logs" / "bridge_events.jsonl"
    state_path = tmp_path / "state" / "operational_state.json"
    manager = TrustedStateManager(
        canonical_log_path=log_path,
        operational_state_path=state_path,
        budget_total=30,
        budget_unit="mock_tokens",
        stage="stage4_workspace_recovery",
        surfaces={
            "canonical_logging": "active_canonical_event_log",
            "budgeting": "enforced_token_cap_stage2",
            "recovery": "trusted_host_checkpoint_controls_stage4",
        },
    )

    manager.append_event(
        event_type="system",
        actor="bridge",
        source_service="bridge",
        request_id="req-system",
        trace_id="trace-system",
        outcome="success",
        summary={"action": "startup"},
    )
    manager.append_event(
        event_type="llm_call",
        actor="bridge",
        source_service="bridge",
        request_id="req-1",
        trace_id="trace-1",
        outcome="success",
        summary={
            "model": "stage2-deterministic",
            "usage": {
                "prompt_tokens": 4,
                "completion_tokens": 7,
                "total_tokens": 11,
            },
        },
    )
    manager.append_event(
        event_type="budget_update",
        actor="bridge",
        source_service="bridge",
        request_id="req-1",
        trace_id="trace-1",
        outcome="success",
        summary={
            "delta_spent": 11,
            "budget": {
                "total": 30,
                "spent": 11,
                "remaining": 19,
                "exhausted": False,
            },
        },
    )
    manager.append_event(
        event_type="status_query",
        actor="operator",
        source_service="bridge",
        request_id="req-status",
        trace_id="trace-status",
        outcome="success",
        summary={"query": "status"},
    )

    events = load_lines(log_path)
    assert [event["event_type"] for event in events] == [
        "system",
        "llm_call",
        "budget_update",
        "status_query",
    ]
    assert all(event["request_id"] and event["trace_id"] for event in events)

    snapshot = manager.snapshot()
    assert snapshot["budget"]["spent"] == 11
    assert snapshot["budget"]["remaining"] == 19
    assert snapshot["counters"]["llm_calls_total"] == 1
    assert snapshot["counters"]["status_queries"] == 1
    assert snapshot["recent_requests"][0]["request_id"] == "req-status"

    reloaded = TrustedStateManager(
        canonical_log_path=log_path,
        operational_state_path=state_path,
        budget_total=30,
        budget_unit="mock_tokens",
        stage="stage4_workspace_recovery",
        surfaces={
            "canonical_logging": "active_canonical_event_log",
            "budgeting": "enforced_token_cap_stage2",
            "recovery": "trusted_host_checkpoint_controls_stage4",
        },
    )
    assert reloaded.snapshot()["budget"]["spent"] == 11
    assert reloaded.snapshot()["recent_requests"][1]["trace_id"] == "trace-1"


def test_trusted_state_manager_materializes_web_fetch_state(tmp_path):
    log_path = tmp_path / "logs" / "bridge_events.jsonl"
    state_path = tmp_path / "state" / "operational_state.json"
    manager = TrustedStateManager(
        canonical_log_path=log_path,
        operational_state_path=state_path,
        budget_total=30,
        budget_unit="mock_tokens",
        stage="stage5_read_only_web",
        surfaces={
            "canonical_logging": "active_canonical_event_log",
            "budgeting": "enforced_token_cap_stage2",
            "recovery": "trusted_host_checkpoint_controls_stage4",
            "read_only_web": "trusted_fetcher_stage5_read_only_get",
        },
        web_defaults={
            "allowlist_hosts": ["example.com"],
            "private_test_hosts": [],
            "allowed_content_types": ["text/plain", "text/html"],
            "caps": {
                "max_redirects": 3,
                "max_response_bytes": 32768,
                "max_preview_chars": 1024,
                "timeout_seconds": 5.0,
            },
            "fetcher": {
                "url": "http://fetcher:8082",
                "reachable": False,
                "detail": "not_checked_yet",
                "checked_at": None,
            },
        },
    )

    manager.append_event(
        event_type="web_fetch",
        actor="agent",
        source_service="bridge",
        request_id="req-fetch",
        trace_id="trace-fetch",
        outcome="success",
        summary={
            "normalized_url": "https://example.com/",
            "scheme": "https",
            "host": "example.com",
            "port": 443,
            "allowlist_decision": "allowed",
            "resolved_ips": ["93.184.216.34"],
            "used_ip": "93.184.216.34",
            "redirect_chain": [],
            "http_status": 200,
            "content_type": "text/html",
            "byte_count": 256,
            "truncated": False,
            "content_sha256": "deadbeef",
        },
    )

    snapshot = manager.snapshot()
    assert snapshot["web"]["counters"]["web_fetch_total"] == 1
    assert snapshot["web"]["counters"]["web_fetch_success"] == 1
    assert snapshot["web"]["recent_fetches"][0]["host"] == "example.com"
    assert snapshot["web"]["recent_fetches"][0]["request_id"] == "req-fetch"


def test_trusted_state_manager_materializes_browser_render_state(tmp_path):
    log_path = tmp_path / "logs" / "bridge_events.jsonl"
    state_path = tmp_path / "state" / "operational_state.json"
    manager = TrustedStateManager(
        canonical_log_path=log_path,
        operational_state_path=state_path,
        budget_total=30,
        budget_unit="mock_tokens",
        stage="stage6_read_only_browser",
        surfaces={
            "canonical_logging": "active_canonical_event_log",
            "budgeting": "enforced_token_cap_stage2",
            "recovery": "trusted_host_checkpoint_controls_stage4",
            "read_only_web": "trusted_fetcher_stage5_read_only_get",
            "browser": "trusted_browser_stage6a_read_only_render",
        },
        web_defaults={
            "allowlist_hosts": ["example.com"],
            "private_test_hosts": [],
            "allowed_content_types": ["text/plain", "text/html"],
            "caps": {
                "max_redirects": 3,
                "max_response_bytes": 32768,
                "max_preview_chars": 1024,
                "timeout_seconds": 5.0,
            },
            "fetcher": {
                "url": "http://fetcher:8082",
                "reachable": True,
                "detail": None,
                "checked_at": "2026-03-12T00:00:00+00:00",
            },
        },
        browser_defaults={
            "service": {
                "url": "http://browser:8083",
                "reachable": False,
                "detail": "not_checked_yet",
                "checked_at": None,
            },
            "caps": {
                "viewport_width": 1280,
                "viewport_height": 720,
                "timeout_seconds": 10.0,
                "settle_time_ms": 500,
                "max_rendered_text_bytes": 16384,
                "max_screenshot_bytes": 1048576,
            },
        },
    )

    manager.append_event(
        event_type="browser_render",
        actor="agent",
        source_service="bridge",
        request_id="req-browser",
        trace_id="trace-browser",
        outcome="success",
        summary={
            "normalized_url": "https://example.com/",
            "final_url": "https://example.com/",
            "host": "example.com",
            "allowlist_decision": "allowed",
            "redirect_chain": [],
            "observed_hosts": ["example.com"],
            "resolved_ips": ["93.184.216.34"],
            "http_status": 200,
            "page_title": "Example Domain",
            "text_bytes": 120,
            "text_truncated": False,
            "screenshot_bytes": 512,
            "screenshot_sha256": "image-hash",
        },
    )

    snapshot = manager.snapshot()
    assert snapshot["browser"]["counters"]["browser_render_total"] == 1
    assert snapshot["browser"]["counters"]["browser_render_success"] == 1
    assert snapshot["browser"]["recent_renders"][0]["request_id"] == "req-browser"
    assert snapshot["browser"]["recent_renders"][0]["page_title"] == "Example Domain"


def test_trusted_state_manager_materializes_browser_follow_state(tmp_path):
    log_path = tmp_path / "logs" / "bridge_events.jsonl"
    state_path = tmp_path / "state" / "operational_state.json"
    manager = TrustedStateManager(
        canonical_log_path=log_path,
        operational_state_path=state_path,
        budget_total=30,
        budget_unit="mock_tokens",
        stage="stage6_read_only_browser",
        surfaces={
            "canonical_logging": "active_canonical_event_log",
            "budgeting": "enforced_token_cap_stage2",
            "recovery": "trusted_host_checkpoint_controls_stage4",
            "read_only_web": "trusted_fetcher_stage5_read_only_get",
            "browser": "trusted_browser_stage6a_read_only_render",
            "browser_follow_href": "trusted_browser_stage6b_safe_follow_href",
        },
        browser_defaults={
            "service": {
                "url": "http://browser:8083",
                "reachable": True,
                "detail": None,
                "checked_at": "2026-03-12T00:00:00+00:00",
            },
            "caps": {
                "viewport_width": 1280,
                "viewport_height": 720,
                "timeout_seconds": 10.0,
                "settle_time_ms": 500,
                "max_rendered_text_bytes": 16384,
                "max_screenshot_bytes": 1048576,
                "max_follow_hops": 1,
                "max_followable_links": 20,
            },
        },
    )

    manager.append_event(
        event_type="browser_follow_href",
        actor="agent",
        source_service="bridge",
        request_id="req-follow",
        trace_id="trace-follow",
        outcome="success",
        summary={
            "source_url": "http://allowed.test/browser/follow-source",
            "source_final_url": "http://allowed.test/browser/follow-source",
            "requested_target_url": "http://allowed.test/browser/follow-target",
            "matched_link_text": "Follow same origin target",
            "follow_hop_count": 1,
            "navigation_history": [
                "http://allowed.test/browser/follow-source",
                "http://allowed.test/browser/follow-target",
            ],
            "normalized_url": "http://allowed.test/browser/follow-target",
            "final_url": "http://allowed.test/browser/follow-target",
            "host": "allowed.test",
            "allowlist_decision": "allowed",
            "redirect_chain": [],
            "observed_hosts": ["allowed.test"],
            "resolved_ips": ["172.24.0.5"],
            "http_status": 200,
            "page_title": "Stage 6B Same Origin Target",
            "text_bytes": 120,
            "text_truncated": False,
            "screenshot_bytes": 512,
            "screenshot_sha256": "follow-image-hash",
            "rendered_text_sha256": "follow-text-hash",
        },
    )

    snapshot = manager.snapshot()
    assert snapshot["browser"]["counters"]["browser_follow_href_total"] == 1
    assert snapshot["browser"]["counters"]["browser_follow_href_success"] == 1
    assert snapshot["browser"]["recent_follows"][0]["request_id"] == "req-follow"
    assert snapshot["browser"]["recent_follows"][0]["source_url"] == "http://allowed.test/browser/follow-source"
    assert snapshot["browser"]["recent_follows"][0]["final_url"] == "http://allowed.test/browser/follow-target"


def test_trusted_state_manager_serializes_concurrent_appends(tmp_path):
    log_path = tmp_path / "logs" / "bridge_events.jsonl"
    state_path = tmp_path / "state" / "operational_state.json"
    ctx = multiprocessing.get_context("spawn")
    start_event = ctx.Event()
    error_queue = ctx.Queue()
    processes = [
        ctx.Process(
            target=_append_events_worker,
            args=(str(log_path), str(state_path), start_event, error_queue, worker_id, 12),
        )
        for worker_id in range(4)
    ]

    for process in processes:
        process.start()
    start_event.set()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0

    assert error_queue.empty()
    events = load_lines(log_path)
    assert len(events) == 48
    assert all(event["event_type"] == "status_query" for event in events)

    snapshot = json.loads(state_path.read_text(encoding="ascii"))
    assert snapshot["counters"]["status_queries"] == 48
    assert len(snapshot["recent_requests"]) <= 12
