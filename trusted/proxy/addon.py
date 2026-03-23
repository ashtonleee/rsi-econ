"""Transparent egress proxy — observability only, no filtering.

All HTTP methods to all domains are allowed. The proxy logs every request
to a JSONL file for operator visibility. The containment boundary is Docker
networking (internal_net only), not per-request gating.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import os

try:
    from mitmproxy import http as mitm_http
except ImportError:  # pragma: no cover - exercised inside the container
    mitm_http = None


class ObservabilityProxy:
    def __init__(
        self,
        *,
        log_path: str | os.PathLike[str] | None = None,
        time_fn: Any = None,
    ) -> None:
        default_log_path = os.getenv("PROXY_LOG_PATH", "/var/log/rsi/web_egress.jsonl")
        self.log_path = Path(log_path or default_log_path)
        self._time_fn = time_fn or time.time
        self._log_lock = threading.Lock()

    def _metadata(self, flow: Any) -> dict[str, Any]:
        metadata = getattr(flow, "metadata", None)
        if metadata is None:
            metadata = {}
            flow.metadata = metadata
        return metadata.setdefault("rsi_policy_proxy", {})

    def requestheaders(self, flow: Any) -> None:
        request = flow.request
        host = getattr(request, "pretty_host", request.host).lower()
        method = request.method.upper()
        metadata = self._metadata(flow)
        metadata.update(
            start=self._time_fn(),
            domain=host,
            method=method,
            path=request.path,
            logged=False,
        )

    def _log_record(self, flow: Any, *, status: int, size: int, error: str | None) -> dict[str, Any]:
        metadata = self._metadata(flow)
        timestamp = datetime.now(timezone.utc).isoformat()
        timing_ms = int((self._time_fn() - metadata.get("start", self._time_fn())) * 1000)
        return {
            "timestamp": timestamp,
            "domain": metadata.get("domain"),
            "method": metadata.get("method"),
            "path": metadata.get("path"),
            "status": int(status),
            "size": int(size),
            "timing_ms": timing_ms,
            "policy": "allowed",
            "error": error,
        }

    def _write_log(self, record: dict[str, Any]) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, sort_keys=True)
        with self._log_lock:
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.write("\n")

    def response(self, flow: Any) -> None:
        metadata = self._metadata(flow)
        if metadata.get("logged"):
            return
        response = flow.response
        content = getattr(response, "content", None)
        if content is None:
            content = getattr(response, "raw_content", b"")
        record = self._log_record(
            flow,
            status=getattr(response, "status_code", 0) or 0,
            size=len(content or b""),
            error=None,
        )
        self._write_log(record)
        metadata["logged"] = True

    def error(self, flow: Any) -> None:
        metadata = self._metadata(flow)
        if metadata.get("logged"):
            return
        flow_error = str(getattr(flow, "error", ""))
        record = self._log_record(flow, status=0, size=0, error=flow_error or None)
        self._write_log(record)
        metadata["logged"] = True


addons = [ObservabilityProxy()]
