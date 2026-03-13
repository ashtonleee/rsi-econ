import json
from pathlib import Path

from trusted.state.store import TrustedStateManager


def load_lines(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="ascii").splitlines()
        if line.strip()
    ]


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
