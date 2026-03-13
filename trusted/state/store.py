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
        recovery_defaults: dict[str, Any] | None = None,
        web_defaults: dict[str, Any] | None = None,
        recent_limit: int = 12,
    ):
        self.canonical_log_path = canonical_log_path
        self.operational_state_path = operational_state_path
        self.budget_total = budget_total
        self.budget_unit = budget_unit
        self.stage = stage
        self.surfaces = dict(surfaces)
        self.recovery_defaults = json.loads(json.dumps(recovery_defaults or {}))
        self.web_defaults = json.loads(json.dumps(web_defaults or {}))
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
                "checkpoint_events": 0,
                "recovery_errors": 0,
                "web_fetch_total": 0,
                "web_fetch_success": 0,
                "web_fetch_denied": 0,
                "web_fetch_errors": 0,
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
                "fetcher": {
                    "url": self.web_defaults.get("fetcher", {}).get("url", "http://fetcher:8082"),
                    "reachable": self.web_defaults.get("fetcher", {}).get("reachable", False),
                    "detail": self.web_defaults.get("fetcher", {}).get(
                        "detail",
                        "not_checked_yet",
                    ),
                    "checked_at": self.web_defaults.get("fetcher", {}).get("checked_at"),
                },
            },
            "recovery": {
                "checkpoint_dir": self.recovery_defaults.get("checkpoint_dir", ""),
                "baseline_id": self.recovery_defaults.get("baseline_id", ""),
                "baseline_source_dir": self.recovery_defaults.get("baseline_source_dir", ""),
                "baseline_archive_path": self.recovery_defaults.get(
                    "baseline_archive_path",
                    "",
                ),
                "available_checkpoints": list(
                    self.recovery_defaults.get("available_checkpoints", [])
                ),
                "latest_checkpoint_id": self.recovery_defaults.get("latest_checkpoint_id"),
                "latest_action": self.recovery_defaults.get("latest_action"),
                "current_workspace_status": self.recovery_defaults.get(
                    "current_workspace_status",
                    "seed_baseline",
                ),
            },
            "web": {
                "fetcher": dict(
                    self.web_defaults.get(
                        "fetcher",
                        {
                            "url": "http://fetcher:8082",
                            "reachable": False,
                            "detail": "not_checked_yet",
                            "checked_at": None,
                        },
                    )
                ),
                "allowlist_hosts": list(self.web_defaults.get("allowlist_hosts", [])),
                "private_test_hosts": list(self.web_defaults.get("private_test_hosts", [])),
                "allowed_content_types": list(
                    self.web_defaults.get("allowed_content_types", [])
                ),
                "caps": dict(self.web_defaults.get("caps", {})),
                "counters": {
                    "web_fetch_total": 0,
                    "web_fetch_success": 0,
                    "web_fetch_denied": 0,
                    "web_fetch_errors": 0,
                },
                "recent_fetches": [],
            },
            "recent_requests": [],
            "last_event_timestamp": None,
        }

    def _rebuild_from_log(self, *, write_snapshot: bool = True):
        self._snapshot = self._initial_snapshot()
        self.canonical_log_path.parent.mkdir(parents=True, exist_ok=True)
        if self.canonical_log_path.exists():
            for raw_line in self.canonical_log_path.read_text(encoding="ascii").splitlines():
                if not raw_line.strip():
                    continue
                event = json.loads(raw_line)
                self._apply_event(event)
        if write_snapshot:
            self._write_snapshot()

    def _merge_connections(self, updates: dict[str, dict[str, Any]]):
        for name, payload in updates.items():
            current = self._snapshot["connections"].get(
                name,
                {"url": "", "reachable": False, "detail": None, "checked_at": None},
            )
            current.update(payload)
            self._snapshot["connections"][name] = current
            if name == "fetcher":
                self._snapshot["web"]["fetcher"] = dict(current)

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

    def _apply_recovery_update(self, summary: dict[str, Any]):
        recovery_payload = summary.get("recovery", {})
        if not recovery_payload:
            return

        recovery = self._snapshot["recovery"]
        recovery["checkpoint_dir"] = recovery_payload.get(
            "checkpoint_dir",
            recovery["checkpoint_dir"],
        )
        recovery["baseline_id"] = recovery_payload.get("baseline_id", recovery["baseline_id"])
        recovery["baseline_source_dir"] = recovery_payload.get(
            "baseline_source_dir",
            recovery["baseline_source_dir"],
        )
        recovery["baseline_archive_path"] = recovery_payload.get(
            "baseline_archive_path",
            recovery["baseline_archive_path"],
        )
        recovery["available_checkpoints"] = list(
            recovery_payload.get("available_checkpoints", recovery["available_checkpoints"])
        )
        recovery["latest_checkpoint_id"] = recovery_payload.get(
            "latest_checkpoint_id",
            recovery["latest_checkpoint_id"],
        )
        recovery["latest_action"] = recovery_payload.get(
            "latest_action",
            recovery["latest_action"],
        )
        recovery["current_workspace_status"] = recovery_payload.get(
            "current_workspace_status",
            recovery["current_workspace_status"],
        )

    def _apply_web_defaults(self, summary: dict[str, Any]):
        web_payload = summary.get("web", {})
        if not web_payload:
            return

        web = self._snapshot["web"]
        if "fetcher" in web_payload:
            web["fetcher"] = dict(web_payload["fetcher"])
        if "allowlist_hosts" in web_payload:
            web["allowlist_hosts"] = list(web_payload["allowlist_hosts"])
        if "private_test_hosts" in web_payload:
            web["private_test_hosts"] = list(web_payload["private_test_hosts"])
        if "allowed_content_types" in web_payload:
            web["allowed_content_types"] = list(web_payload["allowed_content_types"])
        if "caps" in web_payload:
            web["caps"] = dict(web_payload["caps"])

    def _push_recent_fetch(self, event: dict[str, Any]):
        summary = event["summary"]
        fetches = self._snapshot["web"]["recent_fetches"]
        fetches.insert(
            0,
            {
                "timestamp": event["timestamp"],
                "request_id": event["request_id"],
                "trace_id": event["trace_id"],
                "outcome": event["outcome"],
                "normalized_url": summary.get("normalized_url", ""),
                "host": summary.get("host", ""),
                "http_status": summary.get("http_status"),
                "content_type": summary.get("content_type"),
                "byte_count": int(summary.get("byte_count", 0)),
                "truncated": bool(summary.get("truncated", False)),
            },
        )
        del fetches[self.recent_limit :]

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
        elif event_type in {"checkpoint_created", "checkpoint_restored", "workspace_reset"}:
            self._snapshot["counters"]["checkpoint_events"] += 1
            self._apply_recovery_update(summary)
        elif event_type == "recovery_error":
            self._snapshot["counters"]["recovery_errors"] += 1
            self._apply_recovery_update(summary)
        elif event_type == "web_fetch":
            self._snapshot["counters"]["web_fetch_total"] += 1
            self._snapshot["counters"]["web_fetch_success"] += 1
            self._snapshot["web"]["counters"]["web_fetch_total"] += 1
            self._snapshot["web"]["counters"]["web_fetch_success"] += 1
            self._push_recent_fetch(event)
        elif event_type == "web_fetch_denied":
            self._snapshot["counters"]["web_fetch_total"] += 1
            self._snapshot["counters"]["web_fetch_denied"] += 1
            self._snapshot["web"]["counters"]["web_fetch_total"] += 1
            self._snapshot["web"]["counters"]["web_fetch_denied"] += 1
            self._push_recent_fetch(event)
        elif event_type == "web_fetch_error":
            self._snapshot["counters"]["web_fetch_total"] += 1
            self._snapshot["counters"]["web_fetch_errors"] += 1
            self._snapshot["web"]["counters"]["web_fetch_total"] += 1
            self._snapshot["web"]["counters"]["web_fetch_errors"] += 1
            self._push_recent_fetch(event)
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
        self._apply_web_defaults(summary)

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
        self._rebuild_from_log(write_snapshot=False)
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

    def snapshot(self, *, refresh: bool = False) -> dict[str, Any]:
        if refresh:
            self._rebuild_from_log()
        return json.loads(json.dumps(self._snapshot))
