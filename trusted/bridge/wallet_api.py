from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import Body, FastAPI, HTTPException

try:
    from notifier import notify, process_event_files
except ImportError:
    # Fallback for testing outside Docker
    def notify(event_type: str, message: str, data: dict | None = None) -> bool:
        return False
    def process_event_files() -> None:
        pass


LOGGER = logging.getLogger(__name__)
MODEL_CACHE_TTL_SECONDS = 60.0


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _coerce_float(value: Any, field_name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {field_name}") from exc


def _coerce_int(value: Any, field_name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {field_name}") from exc


def _extract_domain(record: dict[str, Any]) -> str | None:
    domain = str(record.get("domain") or "").strip().lower()
    if domain:
        return domain
    url = str(record.get("url") or "").strip()
    if not url:
        return None
    return (urlparse(url).hostname or "").strip().lower() or None


def _proposal_path(proposals_dir: Path, proposal_id: str) -> Path:
    return proposals_dir / f"{proposal_id}.json"


def _remaining_phase(remaining_ratio: float) -> str:
    if remaining_ratio > 0.5:
        return "full"
    if remaining_ratio > 0.2:
        return "moderate"
    if remaining_ratio >= 0.05:
        return "conserve"
    return "wrapup"


@dataclass
class SpendTotals:
    spent_usd: float = 0.0
    total_tokens: int = 0
    request_count: int = 0


class SpendTracker:
    def __init__(
        self,
        *,
        budget_usd: float,
        usage_log_path: str | os.PathLike[str],
        litellm_base_url: str,
    ) -> None:
        self.budget_usd = budget_usd
        self.usage_log_path = Path(usage_log_path)
        self.litellm_models_url = f"{litellm_base_url.rstrip('/')}/v1/models"
        self._lock = threading.Lock()
        self._totals = SpendTotals()
        self._cached_models: list[str] = []
        self._models_loaded_at = 0.0
        self._load_existing_usage()

    def _load_existing_usage(self) -> None:
        if not self.usage_log_path.exists():
            return
        totals = SpendTotals()
        for raw_line in self.usage_log_path.read_text(encoding="utf-8").splitlines():
            if not raw_line.strip():
                continue
            try:
                record = json.loads(raw_line)
                totals.spent_usd += _coerce_float(record.get("cost_usd", 0.0), "cost_usd")
                totals.total_tokens += _coerce_int(record.get("total_tokens", 0), "total_tokens")
                totals.request_count += 1
            except (json.JSONDecodeError, ValueError) as exc:
                LOGGER.warning("Ignoring malformed usage log entry: %s", exc)
        self._totals = totals

    def _append_event(self, event: dict[str, Any]) -> None:
        self.usage_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.usage_log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True))
            handle.write("\n")

    def record_usage(self, payload: Any) -> bool:
        try:
            if isinstance(payload, list):
                if len(payload) != 1:
                    raise ValueError("invalid payload list")
                payload = payload[0]
            if not isinstance(payload, dict):
                raise ValueError("invalid payload")
            usage = payload["usage"]
            if isinstance(usage, dict):
                prompt_tokens = _coerce_int(usage.get("prompt_tokens"), "prompt_tokens")
                completion_tokens = _coerce_int(usage.get("completion_tokens"), "completion_tokens")
                total_tokens = usage.get("total_tokens")
                if total_tokens is None:
                    total_tokens = prompt_tokens + completion_tokens
                total_tokens = _coerce_int(total_tokens, "total_tokens")
            else:
                raise KeyError
            response_cost = _coerce_float(payload.get("response_cost"), "response_cost")
            model = str(payload["model"]).strip()
            if not model:
                raise ValueError("invalid model")
        except KeyError:
            try:
                prompt_tokens = _coerce_int(payload.get("prompt_tokens"), "prompt_tokens")
                completion_tokens = _coerce_int(payload.get("completion_tokens"), "completion_tokens")
                total_tokens = _coerce_int(payload.get("total_tokens"), "total_tokens")
                response_cost = _coerce_float(payload.get("response_cost"), "response_cost")
                model = str(payload.get("model") or "").strip()
                if not model:
                    raise ValueError("invalid model")
            except (AttributeError, TypeError, ValueError) as exc:
                LOGGER.warning("Ignoring malformed LiteLLM usage webhook: %s", exc)
                return False
        except (TypeError, ValueError) as exc:
            LOGGER.warning("Ignoring malformed LiteLLM usage webhook: %s", exc)
            return False

        event = {
            "timestamp": _utcnow(),
            "model": model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "cost_usd": response_cost,
        }
        with self._lock:
            self._append_event(event)
            self._totals.spent_usd += response_cost
            self._totals.total_tokens += total_tokens
            self._totals.request_count += 1
        return True

    def models_available(self) -> list[str]:
        now = time.monotonic()
        with self._lock:
            cached_models = list(self._cached_models)
            cache_age = now - self._models_loaded_at
        if cached_models and cache_age < MODEL_CACHE_TTL_SECONDS:
            return cached_models

        try:
            with urllib_request.urlopen(self.litellm_models_url, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib_error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
            LOGGER.warning("Unable to refresh LiteLLM model list: %s", exc)
            return cached_models

        data = payload.get("data")
        if not isinstance(data, list):
            LOGGER.warning("LiteLLM /v1/models returned an unexpected payload shape")
            return cached_models

        models: list[str] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            name = str(item.get("id") or item.get("model_name") or item.get("name") or "").strip()
            if name:
                models.append(name)

        with self._lock:
            self._cached_models = models
            self._models_loaded_at = now
        return list(models)

    def wallet_payload(self) -> dict[str, Any]:
        with self._lock:
            spent_usd = self._totals.spent_usd
            total_tokens = self._totals.total_tokens
            request_count = self._totals.request_count

        budget_usd = self.budget_usd
        remaining_usd = max(budget_usd - spent_usd, 0.0)
        remaining_ratio = 0.0 if budget_usd <= 0 else remaining_usd / budget_usd
        avg_cost = spent_usd / request_count if request_count else 0.0
        return {
            "budget_usd": round(budget_usd, 6),
            "spent_usd": round(spent_usd, 6),
            "remaining_usd": round(remaining_usd, 6),
            "phase": _remaining_phase(remaining_ratio),
            "total_tokens": total_tokens,
            "total_requests": request_count,
            "avg_cost_per_request": round(avg_cost, 6),
            "models_available": self.models_available(),
            "note": "Budget is managed by trusted infrastructure. You cannot modify it.",
        }


def _append_allowlist_domain(allowlist_path: Path, domain: str) -> bool:
    normalized = domain.strip().lower()
    if not normalized:
        return False
    allowlist_path.parent.mkdir(parents=True, exist_ok=True)
    existing: list[str] = []
    if allowlist_path.exists():
        existing = [line.strip().lower() for line in allowlist_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if normalized in existing:
        return False
    with allowlist_path.open("a", encoding="utf-8") as handle:
        if allowlist_path.stat().st_size > 0:
            handle.write("\n")
        handle.write(normalized)
    return True


def _load_proposal_or_404(proposals_dir: Path, proposal_id: str) -> tuple[Path, dict[str, Any]]:
    path = _proposal_path(proposals_dir, proposal_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="proposal not found")
    return path, _read_json(path)


def create_app(
    proposals_dir: str | os.PathLike[str] | None = None,
    *,
    usage_log_path: str | os.PathLike[str] | None = None,
    allowlist_path: str | os.PathLike[str] | None = None,
    litellm_base_url: str | None = None,
    budget_usd: float | None = None,
) -> FastAPI:
    app = FastAPI()
    default_proposals_dir = os.getenv("PROPOSALS_DIR", "./state/proposals")
    default_usage_log_path = os.getenv("LLM_USAGE_LOG_PATH", "/var/log/rsi/llm_usage.jsonl")
    default_allowlist_path = os.getenv("PROXY_ALLOWLIST_PATH", "/etc/rsi/proxy_allowlist.txt")
    default_litellm_base_url = os.getenv("LITELLM_URL", "http://litellm:4000")
    default_budget_usd = _coerce_float(os.getenv("RSI_BUDGET_USD", "5.00"), "RSI_BUDGET_USD")

    app.state.proposals_dir = Path(proposals_dir or default_proposals_dir)
    app.state.proposals_dir.mkdir(parents=True, exist_ok=True)
    app.state.allowlist_path = Path(allowlist_path or default_allowlist_path)
    app.state.spend_tracker = SpendTracker(
        budget_usd=budget_usd if budget_usd is not None else default_budget_usd,
        usage_log_path=usage_log_path or default_usage_log_path,
        litellm_base_url=litellm_base_url or default_litellm_base_url,
    )

    app.state.operator_messages_dir = Path(os.getenv("OPERATOR_MESSAGES_DIR", "/var/lib/rsi/operator_messages"))
    app.state.operator_messages_dir.mkdir(parents=True, exist_ok=True)
    app.state._budget_warned_25 = False
    app.state._budget_warned_10 = False

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        process_event_files()
        return {"status": "ok"}

    @app.post("/llm_usage")
    def llm_usage(payload: Any = Body(...)) -> dict[str, str]:
        app.state.spend_tracker.record_usage(payload)
        # Check budget thresholds for notifications
        w = app.state.spend_tracker.wallet_payload()
        budget = w.get("budget_usd", 0)
        remaining = w.get("remaining_usd", 0)
        if budget > 0:
            pct = (remaining / budget) * 100
            if pct <= 10 and not app.state._budget_warned_10:
                app.state._budget_warned_10 = True
                notify("budget_critical", f"Budget CRITICAL: {pct:.0f}% (${remaining:.2f} remaining)")
            elif pct <= 25 and not app.state._budget_warned_25:
                app.state._budget_warned_25 = True
                notify("budget_warning", f"Budget warning: {pct:.0f}% (${remaining:.2f} remaining)")
        return {"status": "ok"}

    @app.get("/wallet")
    def wallet() -> dict[str, Any]:
        return app.state.spend_tracker.wallet_payload()

    @app.post("/proposals")
    def create_proposal(payload: dict[str, Any]) -> dict[str, str]:
        proposal_id = str(uuid4())
        record = {
            **payload,
            "proposal_id": proposal_id,
            "status": "pending",
            "created_at": _utcnow(),
        }
        domain = _extract_domain(record)
        if domain and "domain" not in record:
            record["domain"] = domain
        path = _proposal_path(app.state.proposals_dir, proposal_id)
        _write_json(path, record)
        notify("proposal_submitted", f"Proposal: {domain or 'unknown'} ({payload.get('method', '?')} {payload.get('url', '?')[:80]})")
        return {"proposal_id": proposal_id}

    @app.get("/proposals")
    def list_proposals() -> list[dict[str, Any]]:
        proposals = [_read_json(path) for path in sorted(app.state.proposals_dir.glob("*.json"))]
        return sorted(proposals, key=lambda item: item.get("created_at", ""))

    @app.get("/proposals/{proposal_id}")
    def get_proposal(proposal_id: str) -> dict[str, Any]:
        _path, record = _load_proposal_or_404(app.state.proposals_dir, proposal_id)
        return record

    @app.post("/proposals/{proposal_id}/approve")
    def approve_proposal(proposal_id: str) -> dict[str, Any]:
        path, record = _load_proposal_or_404(app.state.proposals_dir, proposal_id)
        if record.get("status") != "pending":
            raise HTTPException(status_code=409, detail="proposal already decided")
        record["status"] = "approved"
        record["approved_at"] = _utcnow()
        domain = _extract_domain(record)
        if domain:
            _append_allowlist_domain(app.state.allowlist_path, domain)
        _write_json(path, record)
        notify("proposal_approved", f"Approved: {domain or proposal_id[:8]}")
        return record

    @app.post("/proposals/{proposal_id}/reject")
    def reject_proposal(proposal_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        path, record = _load_proposal_or_404(app.state.proposals_dir, proposal_id)
        if record.get("status") != "pending":
            raise HTTPException(status_code=409, detail="proposal already decided")
        record["status"] = "rejected"
        record["rejected_at"] = _utcnow()
        reason = None if payload is None else payload.get("reason")
        if reason is not None:
            record["reason"] = str(reason)
        _write_json(path, record)
        return record

    @app.get("/proposals/{proposal_id}/status")
    def proposal_status(proposal_id: str) -> dict[str, Any]:
        _path, record = _load_proposal_or_404(app.state.proposals_dir, proposal_id)
        return {"status": record.get("status", "pending")}

    @app.post("/operator/inject")
    def operator_inject(payload: dict[str, Any]) -> dict[str, str]:
        message = str(payload.get("message", "")).strip()
        if not message:
            raise HTTPException(status_code=400, detail="message is required")
        entry = {
            "timestamp": _utcnow(),
            "message": message,
            "model_override": payload.get("model_override"),
        }
        pending_path = app.state.operator_messages_dir / "pending.jsonl"
        pending_path.parent.mkdir(parents=True, exist_ok=True)
        with pending_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        notify("operator_injection", f"Operator: {message[:100]}")
        return {"status": "queued"}

    return app


app = create_app()
