from contextlib import contextmanager
import fcntl
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shared.schemas import ProposalRecord


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProposalStore:
    """JSONL-backed proposal lifecycle store.

    Each line in the JSONL file is a mutation event (created, decided, executed).
    Current state is rebuilt from the log on init and after each write.
    """

    def __init__(self, proposals_dir: Path):
        self.proposals_dir = proposals_dir
        self.proposals_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.proposals_dir / "proposals.jsonl"
        self.lock_path = self.proposals_dir / "proposals.lock"
        self._proposals: dict[str, ProposalRecord] = {}
        with self._file_lock():
            self._rebuild_from_log()

    @contextmanager
    def _file_lock(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="ascii") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _rebuild_from_log(self):
        self._proposals = {}
        if not self.log_path.exists():
            return
        for raw_line in self.log_path.read_text(encoding="ascii").splitlines():
            if not raw_line.strip():
                continue
            event = json.loads(raw_line)
            self._apply_mutation(event)

    def _apply_mutation(self, event: dict[str, Any]):
        mutation = event["mutation"]
        pid = event["proposal_id"]

        if mutation == "created":
            self._proposals[pid] = ProposalRecord(
                proposal_id=pid,
                action_type=event["action_type"],
                action_payload=event.get("action_payload", {}),
                status="pending",
                created_by=event["actor"],
                created_at=event["timestamp"],
                request_id=event.get("request_id", ""),
                trace_id=event.get("trace_id", ""),
            )
        elif mutation == "decided":
            record = self._proposals.get(pid)
            if record is None:
                return
            decision = event["decision"]
            record.status = "approved" if decision == "approve" else "rejected"
            record.decided_by = event["actor"]
            record.decided_at = event["timestamp"]
            record.decision_reason = event.get("reason", "")
        elif mutation == "claimed":
            record = self._proposals.get(pid)
            if record is None:
                return
            record.status = "executing"
        elif mutation == "failed":
            record = self._proposals.get(pid)
            if record is None:
                return
            record.status = "failed"
            record.execution_result = {"error": event.get("error", "unknown")}
        elif mutation == "executed":
            record = self._proposals.get(pid)
            if record is None:
                return
            record.status = "executed"
            record.executed_by = event["actor"]
            record.executed_at = event["timestamp"]
            record.execution_result = event.get("result")

    def _append_event(self, event: dict[str, Any]):
        with self.log_path.open("a", encoding="ascii") as handle:
            handle.write(json.dumps(event, sort_keys=True))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

    def create_proposal(
        self,
        *,
        action_type: str,
        action_payload: dict[str, Any],
        actor: str,
        request_id: str,
        trace_id: str,
    ) -> ProposalRecord:
        with self._file_lock():
            self._rebuild_from_log()
            proposal_id = str(uuid.uuid4())
            event = {
                "timestamp": _utc_now_iso(),
                "mutation": "created",
                "proposal_id": proposal_id,
                "action_type": action_type,
                "action_payload": action_payload,
                "actor": actor,
                "request_id": request_id,
                "trace_id": trace_id,
            }
            self._append_event(event)
            self._apply_mutation(event)
            return self._proposals[proposal_id]

    def get_proposal(self, proposal_id: str) -> ProposalRecord | None:
        with self._file_lock():
            self._rebuild_from_log()
        return self._proposals.get(proposal_id)

    def list_proposals(self, *, status_filter: str | None = None) -> list[ProposalRecord]:
        with self._file_lock():
            self._rebuild_from_log()
        records = list(self._proposals.values())
        if status_filter:
            records = [r for r in records if r.status == status_filter]
        return records

    def decide_proposal(
        self,
        proposal_id: str,
        *,
        decision: str,
        decided_by: str,
        reason: str = "",
    ) -> ProposalRecord:
        assert decision in ("approve", "reject"), f"invalid decision: {decision}"
        with self._file_lock():
            self._rebuild_from_log()
            record = self._proposals.get(proposal_id)
            if record is None:
                raise ValueError(f"proposal not found: {proposal_id}")
            if record.status != "pending":
                raise ValueError(
                    f"proposal {proposal_id} is {record.status}, cannot decide"
                )
            event = {
                "timestamp": _utc_now_iso(),
                "mutation": "decided",
                "proposal_id": proposal_id,
                "decision": decision,
                "actor": decided_by,
                "reason": reason,
            }
            self._append_event(event)
            self._apply_mutation(event)
            return self._proposals[proposal_id]

    def claim_for_execution(
        self,
        proposal_id: str,
        *,
        claimed_by: str,
    ) -> ProposalRecord:
        """Atomically transition approved → executing under the file lock.

        Must be called before dispatching any side effects. If two
        concurrent requests race, only one wins; the other sees
        'executing' and gets a ValueError.
        """
        with self._file_lock():
            self._rebuild_from_log()
            record = self._proposals.get(proposal_id)
            if record is None:
                raise ValueError(f"proposal not found: {proposal_id}")
            if record.status != "approved":
                raise ValueError(
                    f"proposal {proposal_id} is {record.status}, not approved"
                )
            event = {
                "timestamp": _utc_now_iso(),
                "mutation": "claimed",
                "proposal_id": proposal_id,
                "actor": claimed_by,
            }
            self._append_event(event)
            self._apply_mutation(event)
            return self._proposals[proposal_id]

    def mark_executed(
        self,
        proposal_id: str,
        *,
        executed_by: str,
        result: dict[str, Any],
    ) -> ProposalRecord:
        with self._file_lock():
            self._rebuild_from_log()
            record = self._proposals.get(proposal_id)
            if record is None:
                raise ValueError(f"proposal not found: {proposal_id}")
            if record.status != "executing":
                raise ValueError(
                    f"proposal {proposal_id} is {record.status}, cannot execute"
                )
            event = {
                "timestamp": _utc_now_iso(),
                "mutation": "executed",
                "proposal_id": proposal_id,
                "actor": executed_by,
                "result": result,
            }
            self._append_event(event)
            self._apply_mutation(event)
            return self._proposals[proposal_id]

    def mark_failed(
        self,
        proposal_id: str,
        *,
        failed_by: str,
        error: str,
    ) -> ProposalRecord:
        """Transition executing → failed when the executor crashes or errors."""
        with self._file_lock():
            self._rebuild_from_log()
            record = self._proposals.get(proposal_id)
            if record is None:
                raise ValueError(f"proposal not found: {proposal_id}")
            if record.status != "executing":
                raise ValueError(
                    f"proposal {proposal_id} is {record.status}, cannot mark failed"
                )
            event = {
                "timestamp": _utc_now_iso(),
                "mutation": "failed",
                "proposal_id": proposal_id,
                "actor": failed_by,
                "error": error,
            }
            self._append_event(event)
            self._apply_mutation(event)
            return self._proposals[proposal_id]

    def summary(self) -> dict[str, Any]:
        with self._file_lock():
            self._rebuild_from_log()
        counts = {"total": 0, "pending": 0, "approved": 0, "rejected": 0, "executing": 0, "executed": 0, "failed": 0}
        for record in self._proposals.values():
            counts["total"] += 1
            counts[record.status] += 1
        return counts
