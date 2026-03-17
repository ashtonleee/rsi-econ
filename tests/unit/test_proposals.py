import json

import pytest

from trusted.state.proposals import ProposalStore


def make_store(tmp_path):
    return ProposalStore(tmp_path / "proposals")


def test_create_proposal_returns_pending_record(tmp_path):
    store = make_store(tmp_path)
    record = store.create_proposal(
        action_type="echo",
        action_payload={"message": "hello"},
        actor="agent",
        request_id="req-1",
        trace_id="trace-1",
    )
    assert record.status == "pending"
    assert record.action_type == "echo"
    assert record.action_payload == {"message": "hello"}
    assert record.created_by == "agent"
    assert record.request_id == "req-1"
    assert record.proposal_id


def test_get_proposal_by_id(tmp_path):
    store = make_store(tmp_path)
    created = store.create_proposal(
        action_type="echo",
        action_payload={},
        actor="agent",
        request_id="req-1",
        trace_id="trace-1",
    )
    fetched = store.get_proposal(created.proposal_id)
    assert fetched is not None
    assert fetched.proposal_id == created.proposal_id


def test_get_nonexistent_proposal_returns_none(tmp_path):
    store = make_store(tmp_path)
    assert store.get_proposal("nonexistent-id") is None


def test_list_proposals_with_status_filter(tmp_path):
    store = make_store(tmp_path)
    store.create_proposal(
        action_type="echo", action_payload={}, actor="agent",
        request_id="r1", trace_id="t1",
    )
    p2 = store.create_proposal(
        action_type="echo", action_payload={}, actor="agent",
        request_id="r2", trace_id="t2",
    )
    store.decide_proposal(p2.proposal_id, decision="approve", decided_by="operator")

    pending = store.list_proposals(status_filter="pending")
    approved = store.list_proposals(status_filter="approved")
    all_proposals = store.list_proposals()

    assert len(pending) == 1
    assert len(approved) == 1
    assert len(all_proposals) == 2


def test_approve_then_execute_lifecycle(tmp_path):
    store = make_store(tmp_path)
    record = store.create_proposal(
        action_type="echo",
        action_payload={"msg": "test"},
        actor="agent",
        request_id="req-1",
        trace_id="trace-1",
    )
    pid = record.proposal_id

    decided = store.decide_proposal(pid, decision="approve", decided_by="operator", reason="lgtm")
    assert decided.status == "approved"
    assert decided.decided_by == "operator"
    assert decided.decision_reason == "lgtm"
    assert decided.decided_at

    claimed = store.claim_for_execution(pid, claimed_by="operator")
    assert claimed.status == "executing"

    executed = store.mark_executed(pid, executed_by="operator", result={"echoed": {"msg": "test"}})
    assert executed.status == "executed"
    assert executed.executed_by == "operator"
    assert executed.executed_at
    assert executed.execution_result == {"echoed": {"msg": "test"}}


def test_reject_lifecycle(tmp_path):
    store = make_store(tmp_path)
    record = store.create_proposal(
        action_type="echo", action_payload={}, actor="agent",
        request_id="r1", trace_id="t1",
    )
    rejected = store.decide_proposal(
        record.proposal_id, decision="reject", decided_by="operator", reason="nope"
    )
    assert rejected.status == "rejected"
    assert rejected.decided_by == "operator"
    assert rejected.decision_reason == "nope"


def test_cannot_decide_already_decided(tmp_path):
    store = make_store(tmp_path)
    record = store.create_proposal(
        action_type="echo", action_payload={}, actor="agent",
        request_id="r1", trace_id="t1",
    )
    store.decide_proposal(record.proposal_id, decision="approve", decided_by="operator")

    with pytest.raises(ValueError, match="cannot decide"):
        store.decide_proposal(record.proposal_id, decision="reject", decided_by="operator")


def test_cannot_execute_pending_proposal(tmp_path):
    store = make_store(tmp_path)
    record = store.create_proposal(
        action_type="echo", action_payload={}, actor="agent",
        request_id="r1", trace_id="t1",
    )
    with pytest.raises(ValueError, match="cannot execute"):
        store.mark_executed(record.proposal_id, executed_by="operator", result={})


def test_cannot_execute_rejected_proposal(tmp_path):
    store = make_store(tmp_path)
    record = store.create_proposal(
        action_type="echo", action_payload={}, actor="agent",
        request_id="r1", trace_id="t1",
    )
    store.decide_proposal(record.proposal_id, decision="reject", decided_by="operator")

    with pytest.raises(ValueError, match="cannot execute"):
        store.mark_executed(record.proposal_id, executed_by="operator", result={})


def test_cannot_decide_nonexistent_proposal(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(ValueError, match="not found"):
        store.decide_proposal("ghost-id", decision="approve", decided_by="operator")


def test_cannot_execute_nonexistent_proposal(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(ValueError, match="not found"):
        store.mark_executed("ghost-id", executed_by="operator", result={})


def test_claim_prevents_double_execution(tmp_path):
    """Second claim_for_execution on the same proposal must fail."""
    store = make_store(tmp_path)
    record = store.create_proposal(
        action_type="echo", action_payload={}, actor="agent",
        request_id="r1", trace_id="t1",
    )
    store.decide_proposal(record.proposal_id, decision="approve", decided_by="operator")
    store.claim_for_execution(record.proposal_id, claimed_by="operator")

    with pytest.raises(ValueError, match="not approved"):
        store.claim_for_execution(record.proposal_id, claimed_by="operator")


def test_cannot_mark_executed_without_claim(tmp_path):
    """mark_executed requires the proposal to be in 'executing' status (via claim)."""
    store = make_store(tmp_path)
    record = store.create_proposal(
        action_type="echo", action_payload={}, actor="agent",
        request_id="r1", trace_id="t1",
    )
    store.decide_proposal(record.proposal_id, decision="approve", decided_by="operator")

    with pytest.raises(ValueError, match="cannot execute"):
        store.mark_executed(record.proposal_id, executed_by="operator", result={})


def test_summary_counts(tmp_path):
    store = make_store(tmp_path)
    p1 = store.create_proposal(
        action_type="echo", action_payload={}, actor="agent",
        request_id="r1", trace_id="t1",
    )
    p2 = store.create_proposal(
        action_type="echo", action_payload={}, actor="agent",
        request_id="r2", trace_id="t2",
    )
    p3 = store.create_proposal(
        action_type="echo", action_payload={}, actor="agent",
        request_id="r3", trace_id="t3",
    )

    store.decide_proposal(p1.proposal_id, decision="approve", decided_by="operator")
    store.claim_for_execution(p1.proposal_id, claimed_by="operator")
    store.mark_executed(p1.proposal_id, executed_by="operator", result={})
    store.decide_proposal(p2.proposal_id, decision="reject", decided_by="operator")
    # p3 stays pending

    s = store.summary()
    assert s["total"] == 3
    assert s["pending"] == 1
    assert s["approved"] == 0  # p1 moved to executed
    assert s["rejected"] == 1
    assert s["executed"] == 1


def test_store_survives_restart(tmp_path):
    """State is rebuilt from JSONL on init — simulates process restart."""
    proposals_dir = tmp_path / "proposals"
    store1 = ProposalStore(proposals_dir)
    record = store1.create_proposal(
        action_type="echo", action_payload={"k": "v"}, actor="agent",
        request_id="r1", trace_id="t1",
    )
    store1.decide_proposal(record.proposal_id, decision="approve", decided_by="operator")

    # New store instance reads from same dir
    store2 = ProposalStore(proposals_dir)
    fetched = store2.get_proposal(record.proposal_id)
    assert fetched is not None
    assert fetched.status == "approved"
    assert fetched.decided_by == "operator"


def test_jsonl_file_contains_all_mutations(tmp_path):
    store = make_store(tmp_path)
    record = store.create_proposal(
        action_type="echo", action_payload={}, actor="agent",
        request_id="r1", trace_id="t1",
    )
    store.decide_proposal(record.proposal_id, decision="approve", decided_by="operator")
    store.claim_for_execution(record.proposal_id, claimed_by="operator")
    store.mark_executed(record.proposal_id, executed_by="operator", result={"ok": True})

    log_path = tmp_path / "proposals" / "proposals.jsonl"
    lines = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
    assert len(lines) == 4
    assert lines[0]["mutation"] == "created"
    assert lines[1]["mutation"] == "decided"
    assert lines[2]["mutation"] == "claimed"
    assert lines[3]["mutation"] == "executed"
