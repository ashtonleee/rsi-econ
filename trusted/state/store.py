import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shared.mock_llm import MINIMUM_DETERMINISTIC_CALL_TOKENS


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json_atomic(path: Path, payload: dict[str, Any]):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="ascii") as handle:
        json.dump(payload, handle, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    tmp_path.replace(path)


class TrustedStateManager:
    def __init__(
        self,
        *,
        canonical_log_path: Path,
        operational_state_path: Path,
        budget_total: int,
        budget_unit: str,
        stage: str,
        surfaces: dict[str, str],
        recent_limit: int = 12,
    ):
        self.canonical_log_path = canonical_log_path
        self.operational_state_path = operational_state_path
        self.budget_total = budget_total
        self.budget_unit = budget_unit
        self.stage = stage
        self.surfaces = dict(surfaces)
        self.recent_limit = recent_limit
        self._snapshot = self._initial_snapshot()
        self._rebuild_from_log()

    def _initial_snapshot(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "canonical_log_path": str(self.canonical_log_path),
            "operational_state_path": str(self.operational_state_path),
            "surfaces": dict(self.surfaces),
            "budget": {
                "unit": self.budget_unit,
                "total": self.budget_total,
                "spent": 0,
                "remaining": self.budget_total,
                "exhausted": False,
                "minimum_call_cost": MINIMUM_DETERMINISTIC_CALL_TOKENS,
                "approximation": "deterministic_token_usage_from_stage2_mock_litellm",
                "total_prompt_tokens": 0,
                "total_completion_tokens": 0,
                "total_tokens": 0,
            },
            "counters": {
                "llm_calls_total": 0,
                "llm_calls_success": 0,
                "llm_calls_denied": 0,
                "budget_updates": 0,
                "status_queries": 0,
                "system_events": 0,
                "agent_run_events": 0,
            },
            "connections": {
                "bridge": {
                    "url": "internal://bridge",
                    "reachable": True,
                    "detail": None,
                    "checked_at": None,
                },
                "litellm": {
                    "url": "http://litellm:4000",
                    "reachable": False,
                    "detail": "not_checked_yet",
                    "checked_at": None,
                },
            },
            "recent_requests": [],
            "last_event_timestamp": None,
        }

    def _rebuild_from_log(self):
        self.canonical_log_path.parent.mkdir(parents=True, exist_ok=True)
        if self.canonical_log_path.exists():
            for raw_line in self.canonical_log_path.read_text(encoding="ascii").splitlines():
                if not raw_line.strip():
                    continue
                event = json.loads(raw_line)
                self._apply_event(event)
        self._write_snapshot()

    def _merge_connections(self, updates: dict[str, dict[str, Any]]):
        for name, payload in updates.items():
            current = self._snapshot["connections"].get(
                name,
                {"url": "", "reachable": False, "detail": None, "checked_at": None},
            )
            current.update(payload)
            self._snapshot["connections"][name] = current

    def _apply_budget_update(self, summary: dict[str, Any]):
        budget = self._snapshot["budget"]
        budget_payload = summary.get("budget", {})
        usage_payload = summary.get("usage", {})

        if usage_payload:
            budget["total_prompt_tokens"] = usage_payload.get(
                "total_prompt_tokens",
                budget["total_prompt_tokens"],
            )
            budget["total_completion_tokens"] = usage_payload.get(
                "total_completion_tokens",
                budget["total_completion_tokens"],
            )
            budget["total_tokens"] = usage_payload.get(
                "total_tokens",
                budget["total_tokens"],
            )

        if budget_payload:
            budget["spent"] = budget_payload.get("spent", budget["spent"])
            budget["remaining"] = budget_payload.get("remaining", budget["remaining"])
            budget["exhausted"] = budget_payload.get("exhausted", budget["exhausted"])

    def _push_recent_request(self, event: dict[str, Any]):
        recent = self._snapshot["recent_requests"]
        recent.insert(
            0,
            {
                "timestamp": event["timestamp"],
                "event_type": event["event_type"],
                "request_id": event["request_id"],
                "trace_id": event["trace_id"],
                "actor": event["actor"],
                "source_service": event["source_service"],
                "outcome": event["outcome"],
            },
        )
        del recent[self.recent_limit :]

    def _apply_event(self, event: dict[str, Any]):
        self._snapshot["last_event_timestamp"] = event["timestamp"]
        self._push_recent_request(event)

        event_type = event["event_type"]
        summary = event["summary"]

        if event_type == "system":
            self._snapshot["counters"]["system_events"] += 1
        elif event_type == "llm_call":
            self._snapshot["counters"]["llm_calls_total"] += 1
            if event["outcome"] == "success":
                self._snapshot["counters"]["llm_calls_success"] += 1
            elif event["outcome"] == "denied":
                self._snapshot["counters"]["llm_calls_denied"] += 1
        elif event_type == "budget_update":
            self._snapshot["counters"]["budget_updates"] += 1
            self._apply_budget_update(summary)
        elif event_type == "status_query":
            self._snapshot["counters"]["status_queries"] += 1
        elif event_type == "agent_run":
            self._snapshot["counters"]["agent_run_events"] += 1

        connections = summary.get("connections")
        if connections:
            self._merge_connections(connections)

        surfaces = summary.get("surfaces")
        if surfaces:
            self._snapshot["surfaces"].update(surfaces)

    def _write_snapshot(self):
        _write_json_atomic(self.operational_state_path, self._snapshot)

    def append_event(
        self,
        *,
        event_type: str,
        actor: str,
        source_service: str,
        request_id: str,
        trace_id: str,
        outcome: str,
        summary: dict[str, Any],
    ):
        event = {
            "timestamp": utc_now_iso(),
            "event_type": event_type,
            "request_id": request_id,
            "trace_id": trace_id,
            "actor": actor,
            "source_service": source_service,
            "outcome": outcome,
            "summary": summary,
        }
        self.canonical_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.canonical_log_path.open("a", encoding="ascii") as handle:
            handle.write(json.dumps(event, sort_keys=True))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._apply_event(event)
        self._write_snapshot()

    def snapshot(self) -> dict[str, Any]:
        return json.loads(json.dumps(self._snapshot))
