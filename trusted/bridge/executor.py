import base64
import hashlib
import json
from typing import Any
from urllib.parse import urlsplit

import httpx

from shared.schemas import (
    BrowserHttpRequestExecuteRequest,
    BrowserSubmitExecuteRequest,
    EgressFetchRequest,
    ProposalRecord,
)
from trusted.bridge.clients import TrustedBridgeClients


async def execute_proposal(
    proposal: ProposalRecord,
    *,
    clients: TrustedBridgeClients,
    action_allowlist_hosts: set[str],
    action_max_body_bytes: int = 65536,
    action_max_response_bytes: int = 65536,
) -> dict[str, Any]:
    """Dispatch approved proposal to its action handler.

    Stage 7 echo is preserved. Stage 8 adds http_post.
    """
    if proposal.action_type == "echo":
        return {"echoed": proposal.action_payload}
    if proposal.action_type == "http_post":
        return await _execute_http_post(
            proposal,
            clients=clients,
            action_allowlist_hosts=action_allowlist_hosts,
            action_max_body_bytes=action_max_body_bytes,
            action_max_response_bytes=action_max_response_bytes,
        )
    if proposal.action_type == "browser_submit":
        return await _execute_browser_submit(
            proposal,
            clients=clients,
            action_allowlist_hosts=action_allowlist_hosts,
        )
    if proposal.action_type == "browser_http_request":
        return await _execute_browser_http_request(
            proposal,
            clients=clients,
        )
    return {"error": f"unsupported action_type: {proposal.action_type}"}


async def _execute_http_post(
    proposal: ProposalRecord,
    *,
    clients: TrustedBridgeClients,
    action_allowlist_hosts: set[str],
    action_max_body_bytes: int,
    action_max_response_bytes: int,
) -> dict[str, Any]:
    payload = proposal.action_payload
    url = payload.get("url", "")
    body = payload.get("body", {})
    content_type = payload.get("content_type", "application/json")

    if not url:
        return {"error": "action_payload.url is required"}

    # Gate 1: action allowlist check
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if not host:
        return {"error": f"cannot parse host from url: {url}"}
    if host not in action_allowlist_hosts:
        return {
            "error": "host_not_in_action_allowlist",
            "host": host,
            "action_allowlist_hosts": sorted(action_allowlist_hosts),
        }

    # Serialize body to bytes
    try:
        body_bytes = json.dumps(body, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        return {"error": f"cannot serialize action_payload.body: {exc}"}

    if len(body_bytes) > action_max_body_bytes:
        return {
            "error": "body_exceeds_max_size",
            "body_bytes": len(body_bytes),
            "max_body_bytes": action_max_body_bytes,
        }

    body_b64 = base64.b64encode(body_bytes).decode("ascii")

    # Gate 2 + 3: egress URL policy + outbound request
    egress_request = EgressFetchRequest(
        url=url,
        channel="consequential_action",
        method="POST",
        request_body_base64=body_b64,
        request_content_type=content_type,
        max_body_bytes=action_max_response_bytes,
    )

    try:
        egress_response = await clients.egress_fetch(egress_request)
    except httpx.HTTPStatusError as exc:
        # Egress denied the request (policy violation, blocked host, etc.)
        detail = {}
        try:
            detail = exc.response.json()
        except Exception:
            pass
        return {
            "error": "egress_denied",
            "egress_status": exc.response.status_code,
            "detail": detail,
        }
    except httpx.HTTPError as exc:
        return {"error": f"egress_unreachable: {type(exc).__name__}: {exc}"}

    # Build structured result
    response_body_raw = base64.b64decode(egress_response.body_base64)
    response_body_sha256 = hashlib.sha256(response_body_raw).hexdigest()
    preview_limit = 4096
    try:
        response_body_preview = response_body_raw[:preview_limit].decode("utf-8", errors="replace")
    except Exception:
        response_body_preview = ""

    return {
        "url": egress_response.normalized_url,
        "http_status": egress_response.http_status,
        "response_content_type": egress_response.headers.get("content-type", ""),
        "response_byte_count": len(response_body_raw),
        "response_body_preview": response_body_preview,
        "response_body_sha256": response_body_sha256,
    }


async def _execute_browser_submit(
    proposal: ProposalRecord,
    *,
    clients: TrustedBridgeClients,
    action_allowlist_hosts: set[str],
) -> dict[str, Any]:
    payload = proposal.action_payload
    session_id = str(payload.get("session_id", "")).strip()
    snapshot_id = str(payload.get("snapshot_id", "")).strip()
    submit_element_id = str(payload.get("submit_element_id", "")).strip()
    target_url = str(payload.get("target_url", "")).strip()
    method = str(payload.get("method", "")).strip().upper()
    field_preview = payload.get("field_preview", [])

    if not session_id:
        return {"error": "action_payload.session_id is required"}
    if not snapshot_id:
        return {"error": "action_payload.snapshot_id is required"}
    if not submit_element_id:
        return {"error": "action_payload.submit_element_id is required"}
    if not target_url:
        return {"error": "action_payload.target_url is required"}

    host = (urlsplit(target_url).hostname or "").lower()
    if not host:
        return {"error": f"cannot parse host from target_url: {target_url}"}
    if host not in action_allowlist_hosts:
        return {
            "error": "host_not_in_action_allowlist",
            "host": host,
            "action_allowlist_hosts": sorted(action_allowlist_hosts),
        }

    try:
        execute_result = await clients.browser_execute_submit(
            session_id,
            BrowserSubmitExecuteRequest(
                snapshot_id=snapshot_id,
                element_id=submit_element_id,
            ),
        )
    except httpx.HTTPStatusError as exc:
        detail = {}
        try:
            detail = exc.response.json()
        except Exception:
            pass
        reason = ""
        if isinstance(detail, dict):
            if isinstance(detail.get("detail"), dict):
                reason = str(detail["detail"].get("reason", "")).strip()
            else:
                reason = str(detail.get("reason", "")).strip()
        if reason in {"browser_session_missing", "browser_snapshot_stale"}:
            return {
                "error": reason,
                "detail": detail,
            }
        return {
            "error": "browser_submit_denied",
            "browser_status": exc.response.status_code,
            "detail": detail,
        }
    except httpx.HTTPError as exc:
        return {"error": f"browser_unreachable: {type(exc).__name__}: {exc}"}

    snapshot = execute_result.snapshot
    return {
        "target_url": target_url,
        "method": method or execute_result.method,
        "field_preview": field_preview if isinstance(field_preview, list) else [],
        "current_url": snapshot.current_url,
        "http_status": snapshot.http_status,
        "page_title": snapshot.page_title,
        "meta_description": snapshot.meta_description,
        "rendered_text_sha256": snapshot.rendered_text_sha256,
        "text_bytes": snapshot.text_bytes,
        "text_truncated": snapshot.text_truncated,
        "screenshot_sha256": snapshot.screenshot_sha256,
        "screenshot_bytes": snapshot.screenshot_bytes,
        "session_id": execute_result.session_id,
        "snapshot_id": snapshot.snapshot_id,
    }


async def _execute_browser_http_request(
    proposal: ProposalRecord,
    *,
    clients: TrustedBridgeClients,
) -> dict[str, Any]:
    payload = proposal.action_payload
    session_id = str(payload.get("session_id", "")).strip()
    request_id = str(payload.get("request_id", "")).strip()
    target_url = str(payload.get("target_url", "")).strip()
    method = str(payload.get("method", "")).strip().upper() or "POST"

    if not session_id:
        return {"error": "action_payload.session_id is required"}
    if not request_id:
        return {"error": "action_payload.request_id is required"}
    if not target_url:
        return {"error": "action_payload.target_url is required"}

    try:
        execute_result = await clients.browser_execute_http_request(
            session_id,
            BrowserHttpRequestExecuteRequest(request_id=request_id),
        )
    except httpx.HTTPStatusError as exc:
        detail = {}
        try:
            detail = exc.response.json()
        except Exception:
            pass
        reason = ""
        if isinstance(detail, dict):
            nested = detail.get("detail") if isinstance(detail.get("detail"), dict) else detail
            if isinstance(nested, dict):
                reason = str(nested.get("reason", "")).strip()
        if reason in {"browser_session_missing", "browser_pending_request_missing", "browser_snapshot_stale"}:
            return {"error": reason, "detail": detail}
        return {
            "error": "browser_http_request_denied",
            "browser_status": exc.response.status_code,
            "detail": detail,
        }
    except httpx.HTTPError as exc:
        return {"error": f"browser_unreachable: {type(exc).__name__}: {exc}"}

    snapshot = execute_result.snapshot
    return {
        "request_id": execute_result.request_id,
        "target_url": execute_result.target_url,
        "method": execute_result.method or method,
        "current_url": snapshot.current_url,
        "http_status": snapshot.http_status,
        "page_title": snapshot.page_title,
        "meta_description": snapshot.meta_description,
        "rendered_text_sha256": snapshot.rendered_text_sha256,
        "text_bytes": snapshot.text_bytes,
        "text_truncated": snapshot.text_truncated,
        "screenshot_sha256": snapshot.screenshot_sha256,
        "screenshot_bytes": snapshot.screenshot_bytes,
        "session_id": execute_result.session_id,
        "snapshot_id": snapshot.snapshot_id,
    }
