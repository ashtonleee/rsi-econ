from contextlib import asynccontextmanager
import os
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from shared.config import bridge_settings
from shared.mock_llm import MINIMUM_DETERMINISTIC_CALL_TOKENS, deterministic_usage
from shared.schemas import (
    AgentRunEventReceipt,
    AgentRunEventRequest,
    BrowserFollowHrefRequest,
    BrowserFollowHrefResponse,
    BrowserHttpRequestPreview,
    BrowserSessionActionResponse,
    BrowserSessionBackRequest,
    BrowserSessionClickRequest,
    BrowserSessionCloseTabRequest,
    BrowserSessionFillRequest,
    BrowserSessionForwardRequest,
    BrowserSessionHoverRequest,
    BrowserSessionNavigateRequest,
    BrowserSessionNewTabRequest,
    BrowserSessionOpenRequest,
    BrowserSessionPressRequest,
    BrowserSessionSelectRequest,
    BrowserSessionSetCheckedRequest,
    BrowserSessionSnapshotResponse,
    BrowserSessionSwitchTabRequest,
    BrowserSessionTypeRequest,
    BrowserSessionWaitForRequest,
    BrowserSubmitProposalRequest,
    BrowserRenderRequest,
    BrowserRenderResponse,
    BrowserState,
    BridgeStatusReport,
    BudgetState,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ConnectionStatus,
    EgressProbeReport,
    FetcherFetchResponse,
    HealthReport,
    ProposalCreateRequest,
    ProposalDecisionRequest,
    ProposalListResponse,
    ProposalRecord,
    ProposalState,
    RecentRequest,
    RecoveryState,
    WebFetchRequest,
    WebFetchResponse,
    WebState,
)
from trusted.bridge.auth import resolve_identity
from trusted.bridge.clients import TrustedBridgeClients
from trusted.bridge.executor import execute_proposal
from trusted.recovery.store import WorkspaceRecoveryStore
from trusted.state.proposals import ProposalStore
from trusted.state.store import TrustedStateManager, utc_now_iso


DEBUG_PROBES_ENABLED = os.environ.get("RSI_ENABLE_DEBUG_PROBES", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


def log_path_for(settings) -> Path:
    return settings.log_dir / "bridge_events.jsonl"


def state_path_for(settings) -> Path:
    return settings.state_dir / "operational_state.json"


def build_surfaces() -> dict[str, str]:
    return {
        "litellm": "mediated_via_trusted_service",
        "canonical_logging": "active_canonical_event_log",
        "budgeting": "enforced_token_cap_stage2",
        "seed_agent": "local_only_stage3_substrate",
        "recovery": "trusted_host_checkpoint_controls_stage4",
        "read_only_web": "trusted_fetcher_stage5_read_only_get",
        "browser": "trusted_browser_stage6a_read_only_render",
        "browser_follow_href": "trusted_browser_stage6b_safe_follow_href",
        "browser_interactive_sessions": "trusted_browser_stage8_interactive_sessions",
        "approvals": "active_proposal_approval_flow_stage7",
        "consequential_actions": "active_consequential_actions_stage8",
    }


def base_budget_snapshot(snapshot: dict) -> dict:
    budget = snapshot["budget"]
    return {
        "unit": budget["unit"],
        "total": budget["total"],
        "spent": budget["spent"],
        "remaining": budget["remaining"],
        "exhausted": budget["exhausted"],
        "minimum_call_cost": budget["minimum_call_cost"],
        "approximation": budget["approximation"],
    }


def next_budget_snapshot(snapshot: dict, *, spent_delta: int) -> dict:
    budget = snapshot["budget"]
    spent = budget["spent"] + spent_delta
    remaining = max(budget["total"] - spent, 0)
    return {
        "unit": budget["unit"],
        "total": budget["total"],
        "spent": spent,
        "remaining": remaining,
        "exhausted": remaining < budget["minimum_call_cost"],
        "minimum_call_cost": budget["minimum_call_cost"],
        "approximation": budget["approximation"],
    }


def make_headers(request_id: str, trace_id: str) -> dict[str, str]:
    return {
        "x-request-id": request_id,
        "x-trace-id": trace_id,
    }


def authenticated_actor(authorization: str | None = Header(None)) -> str:
    """FastAPI dependency: resolve caller identity from Authorization header."""
    return resolve_identity(
        authorization,
        agent_token=app.state.settings.agent_token,
        operator_token=app.state.settings.operator_token,
    )


def request_identity() -> tuple[str, str]:
    return uuid4().hex, uuid4().hex


def error_detail_payload(exc: httpx.HTTPStatusError) -> dict:
    payload = exc.response.json()
    if isinstance(payload, dict) and isinstance(payload.get("detail"), dict):
        return payload["detail"]
    if isinstance(payload, dict):
        return payload
    return {"detail": payload}


async def trusted_connections_payload() -> dict[str, dict]:
    reachable, detail = await app.state.clients.litellm_health()
    fetcher_reachable, fetcher_detail = await app.state.clients.fetcher_health()
    browser_reachable, browser_detail = await app.state.clients.browser_health()
    egress_reachable, egress_detail = await app.state.clients.egress_health()
    checked_at = utc_now_iso()
    return {
        "bridge": {
            "url": "internal://bridge",
            "reachable": True,
            "detail": None,
            "checked_at": checked_at,
        },
        "litellm": {
            "url": app.state.settings.litellm_url,
            "reachable": reachable,
            "detail": detail,
            "checked_at": checked_at,
        },
        "fetcher": {
            "url": app.state.settings.fetcher_url,
            "reachable": fetcher_reachable,
            "detail": fetcher_detail,
            "checked_at": checked_at,
        },
        "browser": {
            "url": app.state.settings.browser_url,
            "reachable": browser_reachable,
            "detail": browser_detail,
            "checked_at": checked_at,
        },
        "egress": {
            "url": app.state.settings.egress_url,
            "reachable": egress_reachable,
            "detail": egress_detail,
            "checked_at": checked_at,
        },
    }


def make_status_report(snapshot: dict) -> BridgeStatusReport:
    proposal_summary = app.state.proposal_store.summary()
    return BridgeStatusReport(
        service=app.state.settings.service_name,
        stage=app.state.settings.stage,
        trusted_state_dir=str(app.state.settings.trusted_state_dir),
        log_path=str(app.state.state_manager.canonical_log_path),
        operational_state_path=str(app.state.state_manager.operational_state_path),
        connections={
            name: ConnectionStatus.model_validate(payload)
            for name, payload in snapshot["connections"].items()
        },
        budget=BudgetState.model_validate(snapshot["budget"]),
        recovery=RecoveryState.model_validate(snapshot["recovery"]),
        web=WebState.model_validate(snapshot["web"]),
        browser=BrowserState.model_validate(snapshot["browser"]),
        counters={key: int(value) for key, value in snapshot["counters"].items()},
        recent_requests=[
            RecentRequest.model_validate(payload)
            for payload in snapshot["recent_requests"]
        ],
        surfaces=dict(snapshot["surfaces"]),
        proposals=ProposalState(**proposal_summary),
    )


def web_defaults_for(settings) -> dict:
    return {
        "fetcher": {
            "url": settings.fetcher_url,
            "reachable": False,
            "detail": "not_checked_yet",
            "checked_at": None,
        },
        "allowlist_hosts": list(settings.web_allowlist_hosts),
        "private_test_hosts": list(settings.web_private_test_hosts),
        "allowed_content_types": list(settings.web_allowed_content_types),
        "caps": {
            "max_redirects": settings.web_max_redirects,
            "max_response_bytes": settings.web_max_response_bytes,
            "max_preview_chars": settings.web_max_preview_chars,
            "timeout_seconds": settings.web_timeout_seconds,
        },
    }


def browser_defaults_for(settings) -> dict:
    return {
        "service": {
            "url": settings.browser_url,
            "reachable": False,
            "detail": "not_checked_yet",
            "checked_at": None,
        },
        "caps": {
            "viewport_width": settings.browser_viewport_width,
            "viewport_height": settings.browser_viewport_height,
            "timeout_seconds": settings.browser_timeout_seconds,
            "settle_time_ms": settings.browser_settle_time_ms,
            "max_rendered_text_bytes": settings.browser_max_rendered_text_bytes,
            "max_screenshot_bytes": settings.browser_max_screenshot_bytes,
            "max_followable_links": settings.browser_max_followable_links,
            "max_follow_hops": settings.browser_max_follow_hops,
            "session_max_concurrent": settings.browser_session_max_concurrent,
            "session_ttl_seconds": settings.browser_session_ttl_seconds,
        },
    }


def run_startup_checks(app: FastAPI):
    settings = bridge_settings()
    state_dir = settings.trusted_state_dir
    state_dir.mkdir(parents=True, exist_ok=True)
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    settings.checkpoint_dir.mkdir(parents=True, exist_ok=True)

    probe_file = state_dir / ".bridge_write_probe"
    probe_file.write_text("ok\n", encoding="ascii")
    probe_file.unlink()

    app.state.settings = settings
    app.state.surfaces = build_surfaces()
    app.state.recovery_store = WorkspaceRecoveryStore(
        recovery_dir=settings.checkpoint_dir,
        baseline_source_dir=settings.seed_baseline_dir,
    )
    app.state.recovery_store.ensure_layout()
    app.state.state_manager = TrustedStateManager(
        canonical_log_path=log_path_for(settings),
        operational_state_path=state_path_for(settings),
        budget_total=settings.llm_budget_token_cap,
        budget_unit=settings.budget_unit,
        stage=settings.stage,
        surfaces=app.state.surfaces,
        recovery_defaults=app.state.recovery_store.current_recovery_summary(),
        web_defaults=web_defaults_for(settings),
        browser_defaults=browser_defaults_for(settings),
    )
    app.state.proposal_store = ProposalStore(settings.state_dir / "proposals")
    app.state.clients = TrustedBridgeClients.with_egress(
        litellm_url=settings.litellm_url,
        fetcher_url=settings.fetcher_url,
        browser_url=settings.browser_url,
        egress_url=settings.egress_url,
        agent_url=settings.agent_url,
    )
    app.state.startup_checks = {
        "trusted_state_ready": True,
        "trusted_state_dir": str(state_dir),
        "log_path": str(app.state.state_manager.canonical_log_path),
        "operational_state_path": str(app.state.state_manager.operational_state_path),
        "checkpoint_dir": str(settings.checkpoint_dir),
        "fetcher_url": settings.fetcher_url,
        "browser_url": settings.browser_url,
        "egress_url": settings.egress_url,
    }


def append_event(
    *,
    event_type: str,
    actor: str,
    request_id: str,
    trace_id: str,
    outcome: str,
    summary: dict,
):
    app.state.state_manager.append_event(
        event_type=event_type,
        actor=actor,
        source_service=app.state.settings.service_name,
        request_id=request_id,
        trace_id=trace_id,
        outcome=outcome,
        summary=summary,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    run_startup_checks(app)
    request_id, trace_id = request_identity()
    append_event(
        event_type="system",
        actor="bridge",
        request_id=request_id,
        trace_id=trace_id,
        outcome="success",
        summary={
            "action": "startup",
            "paths": {
                "canonical_log_path": str(app.state.state_manager.canonical_log_path),
                "operational_state_path": str(app.state.state_manager.operational_state_path),
            },
            "surfaces": dict(app.state.surfaces),
            "connections": await trusted_connections_payload(),
            "web": web_defaults_for(app.state.settings),
            "browser": browser_defaults_for(app.state.settings),
        },
    )
    yield


app = FastAPI(title="trusted-bridge", lifespan=lifespan)


@app.get("/healthz", response_model=HealthReport)
async def healthz() -> HealthReport:
    connections = await trusted_connections_payload()
    return HealthReport(
        service=app.state.settings.service_name,
        status="ok",
        stage=app.state.settings.stage,
        details={
            **app.state.startup_checks,
            "litellm_reachable": connections["litellm"]["reachable"],
            "litellm_detail": connections["litellm"]["detail"],
            "fetcher_reachable": connections["fetcher"]["reachable"],
            "fetcher_detail": connections["fetcher"]["detail"],
            "browser_reachable": connections["browser"]["reachable"],
            "browser_detail": connections["browser"]["detail"],
            "egress_reachable": connections["egress"]["reachable"],
            "egress_detail": connections["egress"]["detail"],
        },
    )


@app.get("/status", response_model=BridgeStatusReport)
async def status(request: Request, response: Response, actor: str = Depends(authenticated_actor)) -> BridgeStatusReport:
    request_id, trace_id = request_identity()
    snapshot = app.state.state_manager.snapshot(refresh=True)
    connections = await trusted_connections_payload()
    status_web = dict(snapshot["web"])
    status_web["fetcher"] = connections["fetcher"]
    status_browser = dict(snapshot["browser"])
    status_browser["service"] = connections["browser"]
    append_event(
        event_type="status_query",
        actor=actor,
        request_id=request_id,
        trace_id=trace_id,
        outcome="success",
        summary={
            "action": "status",
            "connections": connections,
            "surfaces": dict(app.state.surfaces),
            "paths": {
                "canonical_log_path": str(app.state.state_manager.canonical_log_path),
                "operational_state_path": str(app.state.state_manager.operational_state_path),
                "checkpoint_dir": str(app.state.settings.checkpoint_dir),
            },
            "recovery": snapshot["recovery"],
            "web": status_web,
            "browser": status_browser,
        },
    )
    response.headers.update(make_headers(request_id, trace_id))
    return make_status_report(app.state.state_manager.snapshot())


@app.post("/agent/runs/events", response_model=AgentRunEventReceipt)
async def agent_run_event(
    payload: AgentRunEventRequest,
    request: Request,
    response: Response,
    actor: str = Depends(authenticated_actor),
) -> AgentRunEventReceipt:
    request_id, trace_id = request_identity()
    append_event(
        event_type="agent_run",
        actor=actor,
        request_id=request_id,
        trace_id=trace_id,
        outcome="recorded",
        summary={
            "action": "agent_run_report",
            "reported_origin": "untrusted_agent",
            "run_id": payload.run_id,
            "event_kind": payload.event_kind,
            "step_index": payload.step_index,
            "tool_name": payload.tool_name,
            "reported_summary": payload.summary,
        },
    )
    response.headers.update(make_headers(request_id, trace_id))
    return AgentRunEventReceipt(
        request_id=request_id,
        trace_id=trace_id,
        outcome="recorded",
    )


@app.post("/proposals", response_model=ProposalRecord)
async def create_proposal(
    payload: ProposalCreateRequest,
    response: Response,
    actor: str = Depends(authenticated_actor),
) -> ProposalRecord:
    request_id, trace_id = request_identity()
    record = app.state.proposal_store.create_proposal(
        action_type=payload.action_type,
        action_payload=payload.action_payload,
        actor=actor,
        request_id=request_id,
        trace_id=trace_id,
    )
    append_event(
        event_type="proposal_created",
        actor=actor,
        request_id=request_id,
        trace_id=trace_id,
        outcome="success",
        summary={
            "proposal_id": record.proposal_id,
            "action_type": record.action_type,
            "action_payload": record.action_payload,
        },
    )
    response.headers.update(make_headers(request_id, trace_id))
    return record


@app.get("/proposals", response_model=ProposalListResponse)
async def list_proposals(
    status: str | None = None,
    actor: str = Depends(authenticated_actor),
) -> ProposalListResponse:
    records = app.state.proposal_store.list_proposals(status_filter=status)
    return ProposalListResponse(proposals=records)


@app.get("/proposals/{proposal_id}", response_model=ProposalRecord)
async def get_proposal(
    proposal_id: str,
    actor: str = Depends(authenticated_actor),
) -> ProposalRecord | JSONResponse:
    record = app.state.proposal_store.get_proposal(proposal_id)
    if record is None:
        return JSONResponse(status_code=404, content={"detail": "proposal not found"})
    return record


@app.post("/proposals/{proposal_id}/decide", response_model=ProposalRecord)
async def decide_proposal(
    proposal_id: str,
    payload: ProposalDecisionRequest,
    response: Response,
    actor: str = Depends(authenticated_actor),
) -> ProposalRecord | JSONResponse:
    if actor != "operator":
        return JSONResponse(
            status_code=403,
            content={"detail": "only operator can decide proposals"},
        )
    request_id, trace_id = request_identity()
    try:
        record = app.state.proposal_store.decide_proposal(
            proposal_id,
            decision=payload.decision,
            decided_by=actor,
            reason=payload.reason,
        )
    except ValueError as exc:
        status_code = 404 if "not found" in str(exc) else 409
        return JSONResponse(status_code=status_code, content={"detail": str(exc)})
    append_event(
        event_type="proposal_decided",
        actor=actor,
        request_id=request_id,
        trace_id=trace_id,
        outcome="success",
        summary={
            "proposal_id": proposal_id,
            "decision": payload.decision,
            "reason": payload.reason,
        },
    )
    response.headers.update(make_headers(request_id, trace_id))
    return record


@app.post("/proposals/{proposal_id}/execute", response_model=ProposalRecord)
async def execute_approved_proposal(
    proposal_id: str,
    response: Response,
    actor: str = Depends(authenticated_actor),
) -> ProposalRecord | JSONResponse:
    if actor != "operator":
        return JSONResponse(
            status_code=403,
            content={"detail": "only operator can execute proposals"},
        )
    request_id, trace_id = request_identity()
    # Atomically claim the proposal before dispatching any side effects.
    # This prevents the TOCTOU race where two concurrent /execute requests
    # both pass a status check and both fire outbound POSTs.
    try:
        record = app.state.proposal_store.claim_for_execution(
            proposal_id, claimed_by=actor,
        )
    except ValueError as exc:
        return JSONResponse(status_code=409, content={"detail": str(exc)})
    append_event(
        event_type="proposal_claimed",
        actor=actor,
        request_id=request_id,
        trace_id=trace_id,
        outcome="success",
        summary={"proposal_id": proposal_id, "action_type": record.action_type},
    )
    # Dispatch the action. If the executor crashes, mark the proposal failed
    # so it doesn't stay stuck in 'executing' permanently.
    try:
        result = await execute_proposal(
            record,
            clients=app.state.clients,
            action_allowlist_hosts=set(app.state.settings.action_allowlist_hosts),
            action_max_body_bytes=app.state.settings.action_max_body_bytes,
            action_max_response_bytes=app.state.settings.action_max_response_bytes,
        )
        record = app.state.proposal_store.mark_executed(
            proposal_id, executed_by=actor, result=result,
        )
    except Exception as exc:
        app.state.proposal_store.mark_failed(
            proposal_id, failed_by=actor, error=f"{type(exc).__name__}: {exc}",
        )
        append_event(
            event_type="proposal_failed",
            actor=actor,
            request_id=request_id,
            trace_id=trace_id,
            outcome="error",
            summary={"proposal_id": proposal_id, "error": f"{type(exc).__name__}: {exc}"},
        )
        return JSONResponse(
            status_code=500,
            content={"detail": f"execution failed: {type(exc).__name__}: {exc}"},
        )
    # Canonical log gets metadata only — strip response_body_preview and detail
    log_result = {k: v for k, v in result.items() if k not in ("response_body_preview", "detail")}
    append_event(
        event_type="proposal_executed",
        actor=actor,
        request_id=request_id,
        trace_id=trace_id,
        outcome="success",
        summary={
            "proposal_id": proposal_id,
            "action_type": record.action_type,
            "result": log_result,
        },
    )
    response.headers.update(make_headers(request_id, trace_id))
    return record


@app.post("/llm/chat/completions", response_model=ChatCompletionResponse)
async def chat_completions(
    payload: ChatCompletionRequest,
    request: Request,
    actor: str = Depends(authenticated_actor),
) -> ChatCompletionResponse | JSONResponse:
    request_id, trace_id = request_identity()
    snapshot = app.state.state_manager.snapshot(refresh=True)
    estimated_usage = deterministic_usage(payload.messages)

    if snapshot["budget"]["remaining"] < estimated_usage.total_tokens:
        denied_budget = base_budget_snapshot(snapshot)
        denied_budget["exhausted"] = denied_budget["remaining"] < MINIMUM_DETERMINISTIC_CALL_TOKENS
        append_event(
            event_type="llm_call",
            actor=actor,
            request_id=request_id,
            trace_id=trace_id,
            outcome="denied",
            summary={
                "action": "mediated_chat_completion",
                "model": payload.model,
                "reason": "budget_exhausted",
                "required_tokens": estimated_usage.total_tokens,
                "remaining_budget": snapshot["budget"]["remaining"],
                "usage": estimated_usage.model_dump(),
            },
        )
        append_event(
            event_type="budget_update",
            actor="bridge",
            request_id=request_id,
            trace_id=trace_id,
            outcome="denied",
            summary={
                "action": "budget_check_denied",
                "delta_spent": 0,
                "budget": denied_budget,
                "usage": {
                    "total_prompt_tokens": snapshot["budget"]["total_prompt_tokens"],
                    "total_completion_tokens": snapshot["budget"]["total_completion_tokens"],
                    "total_tokens": snapshot["budget"]["total_tokens"],
                },
            },
        )
        return JSONResponse(
            status_code=402,
            headers=make_headers(request_id, trace_id),
            content={
                "detail": (
                    "budget exhausted for mediated LLM call: "
                    f"remaining={snapshot['budget']['remaining']} "
                    f"required={estimated_usage.total_tokens}"
                ),
                "request_id": request_id,
                "trace_id": trace_id,
            },
        )

    try:
        result = await app.state.clients.chat_completion(payload)
    except httpx.HTTPError as exc:
        append_event(
            event_type="llm_call",
            actor=actor,
            request_id=request_id,
            trace_id=trace_id,
            outcome="error",
            summary={
                "action": "mediated_chat_completion",
                "model": payload.model,
                "reason": f"{type(exc).__name__}: {exc}",
                "connections": await trusted_connections_payload(),
            },
        )
        return JSONResponse(
            status_code=502,
            headers=make_headers(request_id, trace_id),
            content={
                "detail": f"litellm call failed: {exc}",
                "request_id": request_id,
                "trace_id": trace_id,
            },
        )

    post_call_budget = next_budget_snapshot(
        app.state.state_manager.snapshot(),
        spent_delta=result.usage.total_tokens,
    )
    cumulative_usage = {
        "total_prompt_tokens": snapshot["budget"]["total_prompt_tokens"] + result.usage.prompt_tokens,
        "total_completion_tokens": snapshot["budget"]["total_completion_tokens"] + result.usage.completion_tokens,
        "total_tokens": snapshot["budget"]["total_tokens"] + result.usage.total_tokens,
    }
    connections = await trusted_connections_payload()

    append_event(
        event_type="llm_call",
        actor=actor,
        request_id=request_id,
        trace_id=trace_id,
        outcome="success",
        summary={
            "action": "mediated_chat_completion",
            "model": result.model,
            "usage": result.usage.model_dump(),
            "litellm_response_id": result.id,
            "connections": connections,
        },
    )
    append_event(
        event_type="budget_update",
        actor="bridge",
        request_id=request_id,
        trace_id=trace_id,
        outcome="success",
        summary={
            "action": "mediated_llm_usage_accounted",
            "delta_spent": result.usage.total_tokens,
            "budget": post_call_budget,
            "usage": cumulative_usage,
        },
    )

    response = JSONResponse(
        status_code=200,
        headers=make_headers(request_id, trace_id),
        content=result.model_dump(),
    )
    return response


def web_event_summary(
    fetch_result: FetcherFetchResponse,
    *,
    outcome: str,
    reason: str | None = None,
) -> dict:
    summary = {
        "normalized_url": fetch_result.normalized_url,
        "scheme": fetch_result.scheme,
        "host": fetch_result.host,
        "port": fetch_result.port,
        "allowlist_decision": "allowed" if outcome == "success" else "denied",
        "resolved_ips": list(fetch_result.resolved_ips),
        "approved_ips": list(fetch_result.approved_ips),
        "actual_peer_ip": fetch_result.actual_peer_ip,
        "used_ip": fetch_result.used_ip,
        "enforcement_stage": fetch_result.mediation_hops[-1].enforcement_stage
        if fetch_result.mediation_hops
        else "unknown",
        "request_forwarded": fetch_result.mediation_hops[-1].request_forwarded
        if fetch_result.mediation_hops
        else False,
        "redirect_chain": list(fetch_result.redirect_chain),
        "http_status": fetch_result.http_status,
        "content_type": fetch_result.content_type,
        "byte_count": fetch_result.byte_count,
        "truncated": fetch_result.truncated,
        "content_sha256": fetch_result.content_sha256,
        "mediation_hops": [hop.model_dump() for hop in fetch_result.mediation_hops],
    }
    if reason:
        summary["reason"] = reason
    return summary


def web_error_summary(detail: dict) -> dict:
    return {
        "normalized_url": detail.get("normalized_url", ""),
        "scheme": detail.get("scheme", ""),
        "host": detail.get("host", ""),
        "port": detail.get("port", 0),
        "allowlist_decision": "denied",
        "resolved_ips": list(detail.get("resolved_ips", [])),
        "approved_ips": list(detail.get("approved_ips", [])),
        "actual_peer_ip": detail.get("actual_peer_ip"),
        "used_ip": detail.get("used_ip"),
        "enforcement_stage": detail.get("enforcement_stage", "unknown"),
        "request_forwarded": bool(detail.get("request_forwarded", False)),
        "redirect_chain": list(detail.get("redirect_chain", [])),
        "http_status": detail.get("http_status"),
        "content_type": detail.get("content_type"),
        "byte_count": int(detail.get("byte_count", 0)),
        "truncated": bool(detail.get("truncated", False)),
        "content_sha256": detail.get("content_sha256", ""),
        "mediation_hops": list(detail.get("mediation_hops", [])),
        "reason": detail.get("reason", detail.get("detail", "fetch_failed")),
    }


def browser_event_summary(render_result, *, outcome: str, reason: str | None = None) -> dict:
    host = urlsplit(render_result.normalized_url).hostname or ""
    summary = {
        "normalized_url": render_result.normalized_url,
        "final_url": render_result.final_url,
        "host": host,
        "allowlist_decision": "allowed" if outcome == "success" else "denied",
        "redirect_chain": list(render_result.redirect_chain),
        "observed_hosts": list(render_result.observed_hosts),
        "resolved_ips": list(render_result.resolved_ips),
        "http_status": render_result.http_status,
        "page_title": render_result.page_title,
        "meta_description": render_result.meta_description,
        "rendered_text_sha256": render_result.rendered_text_sha256,
        "text_bytes": render_result.text_bytes,
        "text_truncated": render_result.text_truncated,
        "screenshot_sha256": render_result.screenshot_sha256,
        "screenshot_bytes": render_result.screenshot_bytes,
        "channel_records": [record.model_dump() for record in render_result.channel_records],
        "followable_links_count": len(getattr(render_result, "followable_links", [])),
        "request_forwarded": any(record.request_forwarded for record in render_result.channel_records),
    }
    if reason:
        summary["reason"] = reason
    return summary


def browser_error_summary(detail: dict) -> dict:
    host = urlsplit(detail.get("normalized_url", "")).hostname or detail.get("host", "")
    return {
        "normalized_url": detail.get("normalized_url", ""),
        "final_url": detail.get("final_url", detail.get("normalized_url", "")),
        "host": host,
        "allowlist_decision": detail.get("allowlist_decision", "denied"),
        "redirect_chain": list(detail.get("redirect_chain", [])),
        "observed_hosts": list(detail.get("observed_hosts", [])),
        "resolved_ips": list(detail.get("resolved_ips", [])),
        "http_status": detail.get("http_status"),
        "page_title": detail.get("page_title", ""),
        "meta_description": detail.get("meta_description", ""),
        "rendered_text_sha256": detail.get("rendered_text_sha256", ""),
        "text_bytes": int(detail.get("text_bytes", 0)),
        "text_truncated": bool(detail.get("text_truncated", False)),
        "screenshot_sha256": detail.get("screenshot_sha256", ""),
        "screenshot_bytes": int(detail.get("screenshot_bytes", 0)),
        "channel_records": list(detail.get("channel_records", [])),
        "request_forwarded": any(
            bool(record.get("request_forwarded", False))
            for record in detail.get("channel_records", [])
            if isinstance(record, dict)
        ),
        "reason": detail.get("reason", detail.get("detail", "browser_render_failed")),
    }


def browser_follow_event_summary(follow_result, *, outcome: str, reason: str | None = None) -> dict:
    host = urlsplit(follow_result.normalized_url).hostname or ""
    summary = {
        "source_url": follow_result.source_url,
        "source_final_url": follow_result.source_final_url,
        "requested_target_url": follow_result.requested_target_url,
        "matched_link_text": follow_result.matched_link_text,
        "follow_hop_count": follow_result.follow_hop_count,
        "navigation_history": list(follow_result.navigation_history),
        "normalized_url": follow_result.normalized_url,
        "final_url": follow_result.final_url,
        "host": host,
        "allowlist_decision": "allowed" if outcome == "success" else "denied",
        "redirect_chain": list(follow_result.redirect_chain),
        "observed_hosts": list(follow_result.observed_hosts),
        "resolved_ips": list(follow_result.resolved_ips),
        "http_status": follow_result.http_status,
        "page_title": follow_result.page_title,
        "meta_description": follow_result.meta_description,
        "rendered_text_sha256": follow_result.rendered_text_sha256,
        "text_bytes": follow_result.text_bytes,
        "text_truncated": follow_result.text_truncated,
        "screenshot_sha256": follow_result.screenshot_sha256,
        "screenshot_bytes": follow_result.screenshot_bytes,
        "channel_records": [record.model_dump() for record in follow_result.channel_records],
        "request_forwarded": any(record.request_forwarded for record in follow_result.channel_records),
    }
    if reason:
        summary["reason"] = reason
    return summary


def browser_follow_error_summary(detail: dict) -> dict:
    host = urlsplit(detail.get("normalized_url", "")).hostname or detail.get("host", "")
    return {
        "source_url": detail.get("source_url", ""),
        "source_final_url": detail.get("source_final_url", detail.get("source_url", "")),
        "requested_target_url": detail.get("requested_target_url", ""),
        "matched_link_text": detail.get("matched_link_text", ""),
        "follow_hop_count": int(detail.get("follow_hop_count", 1)),
        "navigation_history": list(detail.get("navigation_history", [])),
        "normalized_url": detail.get("normalized_url", ""),
        "final_url": detail.get("final_url", detail.get("normalized_url", "")),
        "host": host,
        "allowlist_decision": detail.get("allowlist_decision", "denied"),
        "redirect_chain": list(detail.get("redirect_chain", [])),
        "observed_hosts": list(detail.get("observed_hosts", [])),
        "resolved_ips": list(detail.get("resolved_ips", [])),
        "http_status": detail.get("http_status"),
        "page_title": detail.get("page_title", ""),
        "meta_description": detail.get("meta_description", ""),
        "rendered_text_sha256": detail.get("rendered_text_sha256", ""),
        "text_bytes": int(detail.get("text_bytes", 0)),
        "text_truncated": bool(detail.get("text_truncated", False)),
        "screenshot_sha256": detail.get("screenshot_sha256", ""),
        "screenshot_bytes": int(detail.get("screenshot_bytes", 0)),
        "channel_records": list(detail.get("channel_records", [])),
        "request_forwarded": any(
            bool(record.get("request_forwarded", False))
            for record in detail.get("channel_records", [])
            if isinstance(record, dict)
        ),
        "reason": detail.get("reason", detail.get("detail", "browser_follow_href_failed")),
    }


def browser_session_snapshot_summary(snapshot, *, reason: str | None = None) -> dict:
    host = urlsplit(snapshot.current_url).hostname or ""
    summary = {
        "session_id": snapshot.session_id,
        "snapshot_id": snapshot.snapshot_id,
        "capability_profile": getattr(snapshot, "capability_profile", "bounded_packet"),
        "current_url": snapshot.current_url,
        "host": host,
        "http_status": snapshot.http_status,
        "page_title": snapshot.page_title,
        "meta_description": snapshot.meta_description,
        "rendered_text_sha256": snapshot.rendered_text_sha256,
        "text_bytes": snapshot.text_bytes,
        "text_truncated": snapshot.text_truncated,
        "screenshot_sha256": snapshot.screenshot_sha256,
        "screenshot_bytes": snapshot.screenshot_bytes,
        "observed_hosts": list(snapshot.observed_hosts),
        "resolved_ips": list(snapshot.resolved_ips),
        "channel_records": [record.model_dump() for record in snapshot.channel_records],
        "interactable_count": len(snapshot.interactable_elements),
        "active_tab_id": getattr(snapshot, "active_tab_id", ""),
        "tabs": [
            tab.model_dump() if hasattr(tab, "model_dump") else dict(tab)
            for tab in getattr(snapshot, "tabs", [])
        ],
        "pending_request_preview": (
            snapshot.pending_request_preview.model_dump()
            if getattr(snapshot, "pending_request_preview", None) is not None
            else None
        ),
        "request_forwarded": any(record.request_forwarded for record in snapshot.channel_records),
    }
    if reason:
        summary["reason"] = reason
    return summary


def browser_session_error_summary(detail: dict) -> dict:
    current_url = detail.get("current_url", "")
    return {
        "session_id": detail.get("session_id", ""),
        "snapshot_id": detail.get("snapshot_id", ""),
        "current_url": current_url,
        "host": urlsplit(current_url).hostname or "",
        "reason": detail.get("reason", detail.get("detail", "browser_session_failed")),
        "detail": detail.get("detail", ""),
        "observed_hosts": list(detail.get("observed_hosts", [])),
        "resolved_ips": list(detail.get("resolved_ips", [])),
        "http_status": detail.get("http_status"),
        "page_title": detail.get("page_title", ""),
        "rendered_text_sha256": detail.get("rendered_text_sha256", ""),
        "text_bytes": int(detail.get("text_bytes", 0)),
        "text_truncated": bool(detail.get("text_truncated", False)),
        "screenshot_sha256": detail.get("screenshot_sha256", ""),
        "screenshot_bytes": int(detail.get("screenshot_bytes", 0)),
        "channel_records": list(detail.get("channel_records", [])),
        "request_forwarded": any(
            bool(record.get("request_forwarded", False))
            for record in detail.get("channel_records", [])
            if isinstance(record, dict)
        ),
    }


def browser_submit_preview_summary(preview, *, reason: str | None = None) -> dict:
    summary = {
        "session_id": preview.session_id,
        "snapshot_id": preview.snapshot_id,
        "submit_element_id": preview.submit_element_id,
        "target_url": preview.target_url,
        "host": urlsplit(preview.target_url).hostname or "",
        "method": preview.method,
        "field_preview": [field.model_dump() for field in preview.field_preview],
    }
    if reason:
        summary["reason"] = reason
    return summary


def browser_http_request_preview_summary(preview: BrowserHttpRequestPreview, *, reason: str | None = None) -> dict:
    summary = {
        "request_id": preview.request_id,
        "session_id": preview.session_id,
        "snapshot_id": preview.snapshot_id,
        "tab_id": preview.tab_id,
        "current_url": preview.current_url,
        "target_url": preview.target_url,
        "host": urlsplit(preview.target_url).hostname or "",
        "method": preview.method,
        "header_preview": dict(preview.header_preview),
        "body_preview": preview.body_preview,
        "body_sha256": preview.body_sha256,
        "body_bytes": preview.body_bytes,
        "trigger_action": preview.trigger_action,
        "trigger_element_id": preview.trigger_element_id,
    }
    if reason:
        summary["reason"] = reason
    return summary


def browser_session_action_summary(action) -> dict:
    summary = {
        "outcome": action.outcome,
        "proposal_preview": (
            browser_http_request_preview_summary(action.proposal_preview)
            if getattr(action, "proposal_preview", None) is not None
            else None
        ),
    }
    snapshot = getattr(action, "snapshot", None)
    if snapshot is not None:
        summary["snapshot"] = browser_session_snapshot_summary(snapshot)
    return summary


async def browser_session_call(
    *,
    actor: str,
    request_id: str,
    trace_id: str,
    success_event_type: str,
    denied_event_type: str,
    error_event_type: str,
    success_summary,
    fallback_error_summary: dict,
    call,
):
    try:
        result = await call()
    except httpx.HTTPStatusError as exc:
        detail = error_detail_payload(exc)
        status_code = exc.response.status_code
        event_type = denied_event_type if status_code in {400, 403, 404, 409, 413} else error_event_type
        append_event(
            event_type=event_type,
            actor=actor,
            request_id=request_id,
            trace_id=trace_id,
            outcome="denied" if event_type == denied_event_type else "error",
            summary=browser_session_error_summary(detail),
        )
        return None, JSONResponse(
            status_code=status_code,
            headers=make_headers(request_id, trace_id),
            content={
                "request_id": request_id,
                "trace_id": trace_id,
                **detail,
            },
        )
    except httpx.HTTPError as exc:
        append_event(
            event_type=error_event_type,
            actor=actor,
            request_id=request_id,
            trace_id=trace_id,
            outcome="error",
            summary={
                **fallback_error_summary,
                "reason": f"{type(exc).__name__}: {exc}",
            },
        )
        return None, JSONResponse(
            status_code=502,
            headers=make_headers(request_id, trace_id),
            content={
                "request_id": request_id,
                "trace_id": trace_id,
                "reason": f"{type(exc).__name__}: {exc}",
            },
        )

    append_event(
        event_type=success_event_type,
        actor=actor,
        request_id=request_id,
        trace_id=trace_id,
        outcome="success",
        summary=success_summary(result),
    )
    return result, None


def _browser_snapshot_response(
    *,
    request_id: str,
    trace_id: str,
    snapshot,
):
    return BrowserSessionSnapshotResponse(
        request_id=request_id,
        trace_id=trace_id,
        **snapshot.model_dump(),
    )


def _browser_action_response(
    *,
    request_id: str,
    trace_id: str,
    action,
    proposal: ProposalRecord | None = None,
) -> BrowserSessionActionResponse:
    snapshot = getattr(action, "snapshot", None)
    response_snapshot = None
    if snapshot is not None:
        response_snapshot = BrowserSessionSnapshotResponse(
            request_id=request_id,
            trace_id=trace_id,
            **snapshot.model_dump(),
        )
    return BrowserSessionActionResponse(
        request_id=request_id,
        trace_id=trace_id,
        outcome=action.outcome,
        snapshot=response_snapshot,
        proposal_preview=getattr(action, "proposal_preview", None),
        proposal=proposal,
    )


async def browser_session_action_route(
    *,
    actor: str,
    request_id: str,
    trace_id: str,
    response: Response,
    success_event_type: str,
    denied_event_type: str,
    error_event_type: str,
    fallback_error_summary: dict,
    call,
) -> BrowserSessionActionResponse | JSONResponse:
    action, error = await browser_session_call(
        actor=actor,
        request_id=request_id,
        trace_id=trace_id,
        success_event_type=success_event_type,
        denied_event_type=denied_event_type,
        error_event_type=error_event_type,
        success_summary=browser_session_action_summary,
        fallback_error_summary=fallback_error_summary,
        call=call,
    )
    if error is not None:
        return error

    proposal = None
    if action.outcome == "proposal_required" and action.proposal_preview is not None:
        preview = action.proposal_preview
        proposal = app.state.proposal_store.create_proposal(
            action_type="browser_http_request",
            action_payload={
                "request_id": preview.request_id,
                "session_id": preview.session_id,
                "snapshot_id": preview.snapshot_id,
                "tab_id": preview.tab_id,
                "current_url": preview.current_url,
                "target_url": preview.target_url,
                "method": preview.method,
                "header_preview": dict(preview.header_preview),
                "body_preview": preview.body_preview,
                "body_sha256": preview.body_sha256,
                "body_bytes": preview.body_bytes,
                "trigger_action": preview.trigger_action,
                "trigger_element_id": preview.trigger_element_id,
            },
            actor=actor,
            request_id=request_id,
            trace_id=trace_id,
        )
        append_event(
            event_type="browser_http_request_proposal_created",
            actor=actor,
            request_id=request_id,
            trace_id=trace_id,
            outcome="success",
            summary={
                "proposal_id": proposal.proposal_id,
                "action_type": proposal.action_type,
                **browser_http_request_preview_summary(preview),
            },
        )

    response.headers.update(make_headers(request_id, trace_id))
    return _browser_action_response(
        request_id=request_id,
        trace_id=trace_id,
        action=action,
        proposal=proposal,
    )


@app.post("/web/fetch", response_model=WebFetchResponse)
async def bridge_fetch(
    payload: WebFetchRequest,
    request: Request,
    response: Response,
    actor: str = Depends(authenticated_actor),
) -> WebFetchResponse | JSONResponse:
    request_id, trace_id = request_identity()

    try:
        fetch_result = await app.state.clients.fetch_url(payload)
    except httpx.HTTPStatusError as exc:
        detail = error_detail_payload(exc)
        status_code = exc.response.status_code
        event_type = "web_fetch_denied" if status_code in {400, 403, 415} else "web_fetch_error"
        append_event(
            event_type=event_type,
            actor=actor,
            request_id=request_id,
            trace_id=trace_id,
            outcome="denied" if event_type == "web_fetch_denied" else "error",
            summary=web_error_summary(detail),
        )
        return JSONResponse(
            status_code=status_code,
            headers=make_headers(request_id, trace_id),
            content={
                "request_id": request_id,
                "trace_id": trace_id,
                **detail,
            },
        )
    except httpx.HTTPError as exc:
        append_event(
            event_type="web_fetch_error",
            actor=actor,
            request_id=request_id,
            trace_id=trace_id,
            outcome="error",
            summary={
                "normalized_url": payload.url,
                "scheme": "",
                "host": "",
                "port": 0,
                "allowlist_decision": "unknown",
                "resolved_ips": [],
                "approved_ips": [],
                "actual_peer_ip": None,
                "used_ip": None,
                "redirect_chain": [],
                "http_status": None,
                "content_type": None,
                "byte_count": 0,
                "truncated": False,
                "content_sha256": "",
                "mediation_hops": [],
                "reason": f"{type(exc).__name__}: {exc}",
            },
        )
        return JSONResponse(
            status_code=502,
            headers=make_headers(request_id, trace_id),
            content={
                "request_id": request_id,
                "trace_id": trace_id,
                "reason": f"{type(exc).__name__}: {exc}",
            },
        )

    append_event(
        event_type="web_fetch",
        actor=actor,
        request_id=request_id,
        trace_id=trace_id,
        outcome="success",
        summary=web_event_summary(fetch_result, outcome="success"),
    )
    response.headers.update(make_headers(request_id, trace_id))
    return WebFetchResponse(
        request_id=request_id,
        trace_id=trace_id,
        **fetch_result.model_dump(),
    )


@app.post("/web/browser/render", response_model=BrowserRenderResponse)
async def bridge_browser_render(
    payload: BrowserRenderRequest,
    request: Request,
    response: Response,
    actor: str = Depends(authenticated_actor),
) -> BrowserRenderResponse | JSONResponse:
    request_id, trace_id = request_identity()

    try:
        render_result = await app.state.clients.browser_render(payload)
    except httpx.HTTPStatusError as exc:
        detail = error_detail_payload(exc)
        status_code = exc.response.status_code
        event_type = "browser_render_denied" if status_code in {400, 403, 413} else "browser_render_error"
        append_event(
            event_type=event_type,
            actor=actor,
            request_id=request_id,
            trace_id=trace_id,
            outcome="denied" if event_type == "browser_render_denied" else "error",
            summary=browser_error_summary(detail),
        )
        return JSONResponse(
            status_code=status_code,
            headers=make_headers(request_id, trace_id),
            content={
                "request_id": request_id,
                "trace_id": trace_id,
                **detail,
            },
        )
    except httpx.HTTPError as exc:
        append_event(
            event_type="browser_render_error",
            actor=actor,
            request_id=request_id,
            trace_id=trace_id,
            outcome="error",
            summary={
                "normalized_url": payload.url,
                "final_url": payload.url,
                "host": urlsplit(payload.url).hostname or "",
                "allowlist_decision": "unknown",
                "redirect_chain": [],
                "observed_hosts": [],
                "resolved_ips": [],
                "http_status": None,
                "page_title": "",
                "meta_description": "",
                "rendered_text_sha256": "",
                "text_bytes": 0,
                "text_truncated": False,
                "screenshot_sha256": "",
                "screenshot_bytes": 0,
                "channel_records": [],
                "reason": f"{type(exc).__name__}: {exc}",
            },
        )
        return JSONResponse(
            status_code=502,
            headers=make_headers(request_id, trace_id),
            content={
                "request_id": request_id,
                "trace_id": trace_id,
                "reason": f"{type(exc).__name__}: {exc}",
            },
        )

    append_event(
        event_type="browser_render",
        actor=actor,
        request_id=request_id,
        trace_id=trace_id,
        outcome="success",
        summary=browser_event_summary(render_result, outcome="success"),
    )
    response.headers.update(make_headers(request_id, trace_id))
    return BrowserRenderResponse(
        request_id=request_id,
        trace_id=trace_id,
        **render_result.model_dump(),
    )


@app.post("/web/browser/follow-href", response_model=BrowserFollowHrefResponse)
async def bridge_browser_follow_href(
    payload: BrowserFollowHrefRequest,
    request: Request,
    response: Response,
    actor: str = Depends(authenticated_actor),
) -> BrowserFollowHrefResponse | JSONResponse:
    request_id, trace_id = request_identity()

    try:
        follow_result = await app.state.clients.browser_follow_href(payload)
    except httpx.HTTPStatusError as exc:
        detail = error_detail_payload(exc)
        status_code = exc.response.status_code
        event_type = (
            "browser_follow_href_denied"
            if status_code in {400, 403, 413}
            else "browser_follow_href_error"
        )
        append_event(
            event_type=event_type,
            actor=actor,
            request_id=request_id,
            trace_id=trace_id,
            outcome="denied" if event_type == "browser_follow_href_denied" else "error",
            summary=browser_follow_error_summary(detail),
        )
        return JSONResponse(
            status_code=status_code,
            headers=make_headers(request_id, trace_id),
            content={
                "request_id": request_id,
                "trace_id": trace_id,
                **detail,
            },
        )
    except httpx.HTTPError as exc:
        append_event(
            event_type="browser_follow_href_error",
            actor=actor,
            request_id=request_id,
            trace_id=trace_id,
            outcome="error",
            summary={
                "source_url": payload.source_url,
                "source_final_url": payload.source_url,
                "requested_target_url": payload.target_url,
                "matched_link_text": "",
                "follow_hop_count": 1,
                "navigation_history": [payload.source_url],
                "normalized_url": payload.target_url,
                "final_url": payload.target_url,
                "host": urlsplit(payload.target_url).hostname or "",
                "allowlist_decision": "unknown",
                "redirect_chain": [],
                "observed_hosts": [],
                "resolved_ips": [],
                "http_status": None,
                "page_title": "",
                "meta_description": "",
                "rendered_text_sha256": "",
                "text_bytes": 0,
                "text_truncated": False,
                "screenshot_sha256": "",
                "screenshot_bytes": 0,
                "channel_records": [],
                "reason": f"{type(exc).__name__}: {exc}",
            },
        )
        return JSONResponse(
            status_code=502,
            headers=make_headers(request_id, trace_id),
            content={
                "request_id": request_id,
                "trace_id": trace_id,
                "reason": f"{type(exc).__name__}: {exc}",
            },
        )

    append_event(
        event_type="browser_follow_href",
        actor=actor,
        request_id=request_id,
        trace_id=trace_id,
        outcome="success",
        summary=browser_follow_event_summary(follow_result, outcome="success"),
    )
    response.headers.update(make_headers(request_id, trace_id))
    return BrowserFollowHrefResponse(
        request_id=request_id,
        trace_id=trace_id,
        **follow_result.model_dump(),
    )


@app.post("/web/browser/sessions/open", response_model=BrowserSessionSnapshotResponse)
async def bridge_browser_session_open(
    payload: BrowserSessionOpenRequest,
    response: Response,
    actor: str = Depends(authenticated_actor),
) -> BrowserSessionSnapshotResponse | JSONResponse:
    request_id, trace_id = request_identity()
    snapshot, error = await browser_session_call(
        actor=actor,
        request_id=request_id,
        trace_id=trace_id,
        success_event_type="browser_session_open",
        denied_event_type="browser_session_open_denied",
        error_event_type="browser_session_open_error",
        success_summary=browser_session_snapshot_summary,
        fallback_error_summary={
            "session_id": "",
            "snapshot_id": "",
            "current_url": payload.url,
            "host": urlsplit(payload.url).hostname or "",
        },
        call=lambda: app.state.clients.browser_session_open(payload),
    )
    if error is not None:
        return error
    response.headers.update(make_headers(request_id, trace_id))
    return BrowserSessionSnapshotResponse(
        request_id=request_id,
        trace_id=trace_id,
        **snapshot.model_dump(),
    )


@app.get("/web/browser/sessions/{session_id}", response_model=BrowserSessionSnapshotResponse)
async def bridge_browser_session_snapshot(
    session_id: str,
    response: Response,
    actor: str = Depends(authenticated_actor),
) -> BrowserSessionSnapshotResponse | JSONResponse:
    request_id, trace_id = request_identity()
    snapshot, error = await browser_session_call(
        actor=actor,
        request_id=request_id,
        trace_id=trace_id,
        success_event_type="browser_session_snapshot",
        denied_event_type="browser_session_snapshot_denied",
        error_event_type="browser_session_snapshot_error",
        success_summary=browser_session_snapshot_summary,
        fallback_error_summary={
            "session_id": session_id,
            "snapshot_id": "",
            "current_url": "",
            "host": "",
        },
        call=lambda: app.state.clients.browser_session_snapshot(session_id),
    )
    if error is not None:
        return error
    response.headers.update(make_headers(request_id, trace_id))
    return BrowserSessionSnapshotResponse(
        request_id=request_id,
        trace_id=trace_id,
        **snapshot.model_dump(),
    )


@app.post("/web/browser/sessions/{session_id}/click", response_model=BrowserSessionSnapshotResponse)
async def bridge_browser_session_click(
    session_id: str,
    payload: BrowserSessionClickRequest,
    response: Response,
    actor: str = Depends(authenticated_actor),
) -> BrowserSessionSnapshotResponse | JSONResponse:
    request_id, trace_id = request_identity()
    snapshot, error = await browser_session_call(
        actor=actor,
        request_id=request_id,
        trace_id=trace_id,
        success_event_type="browser_session_click",
        denied_event_type="browser_session_click_denied",
        error_event_type="browser_session_click_error",
        success_summary=browser_session_snapshot_summary,
        fallback_error_summary={
            "session_id": session_id,
            "snapshot_id": payload.snapshot_id,
            "current_url": "",
            "host": "",
        },
        call=lambda: app.state.clients.browser_session_click(session_id, payload),
    )
    if error is not None:
        return error
    response.headers.update(make_headers(request_id, trace_id))
    return BrowserSessionSnapshotResponse(
        request_id=request_id,
        trace_id=trace_id,
        **snapshot.model_dump(),
    )


@app.post("/web/browser/sessions/{session_id}/type", response_model=BrowserSessionSnapshotResponse)
async def bridge_browser_session_type(
    session_id: str,
    payload: BrowserSessionTypeRequest,
    response: Response,
    actor: str = Depends(authenticated_actor),
) -> BrowserSessionSnapshotResponse | JSONResponse:
    request_id, trace_id = request_identity()
    snapshot, error = await browser_session_call(
        actor=actor,
        request_id=request_id,
        trace_id=trace_id,
        success_event_type="browser_session_type",
        denied_event_type="browser_session_type_denied",
        error_event_type="browser_session_type_error",
        success_summary=browser_session_snapshot_summary,
        fallback_error_summary={
            "session_id": session_id,
            "snapshot_id": payload.snapshot_id,
            "current_url": "",
            "host": "",
        },
        call=lambda: app.state.clients.browser_session_type(session_id, payload),
    )
    if error is not None:
        return error
    response.headers.update(make_headers(request_id, trace_id))
    return BrowserSessionSnapshotResponse(
        request_id=request_id,
        trace_id=trace_id,
        **snapshot.model_dump(),
    )


@app.post("/web/browser/sessions/{session_id}/select", response_model=BrowserSessionSnapshotResponse)
async def bridge_browser_session_select(
    session_id: str,
    payload: BrowserSessionSelectRequest,
    response: Response,
    actor: str = Depends(authenticated_actor),
) -> BrowserSessionSnapshotResponse | JSONResponse:
    request_id, trace_id = request_identity()
    snapshot, error = await browser_session_call(
        actor=actor,
        request_id=request_id,
        trace_id=trace_id,
        success_event_type="browser_session_select",
        denied_event_type="browser_session_select_denied",
        error_event_type="browser_session_select_error",
        success_summary=browser_session_snapshot_summary,
        fallback_error_summary={
            "session_id": session_id,
            "snapshot_id": payload.snapshot_id,
            "current_url": "",
            "host": "",
        },
        call=lambda: app.state.clients.browser_session_select(session_id, payload),
    )
    if error is not None:
        return error
    response.headers.update(make_headers(request_id, trace_id))
    return BrowserSessionSnapshotResponse(
        request_id=request_id,
        trace_id=trace_id,
        **snapshot.model_dump(),
    )


@app.post("/web/browser/sessions/{session_id}/set_checked", response_model=BrowserSessionSnapshotResponse)
async def bridge_browser_session_set_checked(
    session_id: str,
    payload: BrowserSessionSetCheckedRequest,
    response: Response,
    actor: str = Depends(authenticated_actor),
) -> BrowserSessionSnapshotResponse | JSONResponse:
    request_id, trace_id = request_identity()
    snapshot, error = await browser_session_call(
        actor=actor,
        request_id=request_id,
        trace_id=trace_id,
        success_event_type="browser_session_set_checked",
        denied_event_type="browser_session_set_checked_denied",
        error_event_type="browser_session_set_checked_error",
        success_summary=browser_session_snapshot_summary,
        fallback_error_summary={
            "session_id": session_id,
            "snapshot_id": payload.snapshot_id,
            "current_url": "",
            "host": "",
        },
        call=lambda: app.state.clients.browser_session_set_checked(session_id, payload),
    )
    if error is not None:
        return error
    response.headers.update(make_headers(request_id, trace_id))
    return BrowserSessionSnapshotResponse(
        request_id=request_id,
        trace_id=trace_id,
        **snapshot.model_dump(),
    )


@app.post("/web/browser/sessions/{session_id}/navigate", response_model=BrowserSessionActionResponse)
async def bridge_browser_session_navigate(
    session_id: str,
    payload: BrowserSessionNavigateRequest,
    response: Response,
    actor: str = Depends(authenticated_actor),
) -> BrowserSessionActionResponse | JSONResponse:
    request_id, trace_id = request_identity()
    return await browser_session_action_route(
        actor=actor,
        request_id=request_id,
        trace_id=trace_id,
        response=response,
        success_event_type="browser_session_navigate",
        denied_event_type="browser_session_navigate_denied",
        error_event_type="browser_session_navigate_error",
        fallback_error_summary={
            "session_id": session_id,
            "snapshot_id": payload.snapshot_id,
            "current_url": payload.url,
            "host": urlsplit(payload.url).hostname or "",
        },
        call=lambda: app.state.clients.browser_session_navigate(session_id, payload),
    )


@app.post("/web/browser/sessions/{session_id}/actions/click", response_model=BrowserSessionActionResponse)
async def bridge_browser_session_click_action(
    session_id: str,
    payload: BrowserSessionClickRequest,
    response: Response,
    actor: str = Depends(authenticated_actor),
) -> BrowserSessionActionResponse | JSONResponse:
    request_id, trace_id = request_identity()
    return await browser_session_action_route(
        actor=actor,
        request_id=request_id,
        trace_id=trace_id,
        response=response,
        success_event_type="browser_session_click_action",
        denied_event_type="browser_session_click_action_denied",
        error_event_type="browser_session_click_action_error",
        fallback_error_summary={
            "session_id": session_id,
            "snapshot_id": payload.snapshot_id,
            "current_url": "",
            "host": "",
        },
        call=lambda: app.state.clients.browser_session_click_action(session_id, payload),
    )


@app.post("/web/browser/sessions/{session_id}/fill", response_model=BrowserSessionActionResponse)
async def bridge_browser_session_fill(
    session_id: str,
    payload: BrowserSessionFillRequest,
    response: Response,
    actor: str = Depends(authenticated_actor),
) -> BrowserSessionActionResponse | JSONResponse:
    request_id, trace_id = request_identity()
    return await browser_session_action_route(
        actor=actor,
        request_id=request_id,
        trace_id=trace_id,
        response=response,
        success_event_type="browser_session_fill",
        denied_event_type="browser_session_fill_denied",
        error_event_type="browser_session_fill_error",
        fallback_error_summary={
            "session_id": session_id,
            "snapshot_id": payload.snapshot_id,
            "current_url": "",
            "host": "",
        },
        call=lambda: app.state.clients.browser_session_fill(session_id, payload),
    )


@app.post("/web/browser/sessions/{session_id}/actions/select", response_model=BrowserSessionActionResponse)
async def bridge_browser_session_select_action(
    session_id: str,
    payload: BrowserSessionSelectRequest,
    response: Response,
    actor: str = Depends(authenticated_actor),
) -> BrowserSessionActionResponse | JSONResponse:
    request_id, trace_id = request_identity()
    return await browser_session_action_route(
        actor=actor,
        request_id=request_id,
        trace_id=trace_id,
        response=response,
        success_event_type="browser_session_select_action",
        denied_event_type="browser_session_select_action_denied",
        error_event_type="browser_session_select_action_error",
        fallback_error_summary={
            "session_id": session_id,
            "snapshot_id": payload.snapshot_id,
            "current_url": "",
            "host": "",
        },
        call=lambda: app.state.clients.browser_session_select_action(session_id, payload),
    )


@app.post("/web/browser/sessions/{session_id}/actions/set_checked", response_model=BrowserSessionActionResponse)
async def bridge_browser_session_set_checked_action(
    session_id: str,
    payload: BrowserSessionSetCheckedRequest,
    response: Response,
    actor: str = Depends(authenticated_actor),
) -> BrowserSessionActionResponse | JSONResponse:
    request_id, trace_id = request_identity()
    return await browser_session_action_route(
        actor=actor,
        request_id=request_id,
        trace_id=trace_id,
        response=response,
        success_event_type="browser_session_set_checked_action",
        denied_event_type="browser_session_set_checked_action_denied",
        error_event_type="browser_session_set_checked_action_error",
        fallback_error_summary={
            "session_id": session_id,
            "snapshot_id": payload.snapshot_id,
            "current_url": "",
            "host": "",
        },
        call=lambda: app.state.clients.browser_session_set_checked_action(session_id, payload),
    )


@app.post("/web/browser/sessions/{session_id}/press", response_model=BrowserSessionActionResponse)
async def bridge_browser_session_press(
    session_id: str,
    payload: BrowserSessionPressRequest,
    response: Response,
    actor: str = Depends(authenticated_actor),
) -> BrowserSessionActionResponse | JSONResponse:
    request_id, trace_id = request_identity()
    return await browser_session_action_route(
        actor=actor,
        request_id=request_id,
        trace_id=trace_id,
        response=response,
        success_event_type="browser_session_press",
        denied_event_type="browser_session_press_denied",
        error_event_type="browser_session_press_error",
        fallback_error_summary={
            "session_id": session_id,
            "snapshot_id": payload.snapshot_id,
            "current_url": "",
            "host": "",
        },
        call=lambda: app.state.clients.browser_session_press(session_id, payload),
    )


@app.post("/web/browser/sessions/{session_id}/hover", response_model=BrowserSessionActionResponse)
async def bridge_browser_session_hover(
    session_id: str,
    payload: BrowserSessionHoverRequest,
    response: Response,
    actor: str = Depends(authenticated_actor),
) -> BrowserSessionActionResponse | JSONResponse:
    request_id, trace_id = request_identity()
    return await browser_session_action_route(
        actor=actor,
        request_id=request_id,
        trace_id=trace_id,
        response=response,
        success_event_type="browser_session_hover",
        denied_event_type="browser_session_hover_denied",
        error_event_type="browser_session_hover_error",
        fallback_error_summary={
            "session_id": session_id,
            "snapshot_id": payload.snapshot_id,
            "current_url": "",
            "host": "",
        },
        call=lambda: app.state.clients.browser_session_hover(session_id, payload),
    )


@app.post("/web/browser/sessions/{session_id}/wait_for", response_model=BrowserSessionActionResponse)
async def bridge_browser_session_wait_for(
    session_id: str,
    payload: BrowserSessionWaitForRequest,
    response: Response,
    actor: str = Depends(authenticated_actor),
) -> BrowserSessionActionResponse | JSONResponse:
    request_id, trace_id = request_identity()
    return await browser_session_action_route(
        actor=actor,
        request_id=request_id,
        trace_id=trace_id,
        response=response,
        success_event_type="browser_session_wait_for",
        denied_event_type="browser_session_wait_for_denied",
        error_event_type="browser_session_wait_for_error",
        fallback_error_summary={
            "session_id": session_id,
            "snapshot_id": payload.snapshot_id,
            "current_url": "",
            "host": "",
        },
        call=lambda: app.state.clients.browser_session_wait_for(session_id, payload),
    )


@app.post("/web/browser/sessions/{session_id}/back", response_model=BrowserSessionActionResponse)
async def bridge_browser_session_back(
    session_id: str,
    payload: BrowserSessionBackRequest,
    response: Response,
    actor: str = Depends(authenticated_actor),
) -> BrowserSessionActionResponse | JSONResponse:
    request_id, trace_id = request_identity()
    return await browser_session_action_route(
        actor=actor,
        request_id=request_id,
        trace_id=trace_id,
        response=response,
        success_event_type="browser_session_back",
        denied_event_type="browser_session_back_denied",
        error_event_type="browser_session_back_error",
        fallback_error_summary={
            "session_id": session_id,
            "snapshot_id": payload.snapshot_id,
            "current_url": "",
            "host": "",
        },
        call=lambda: app.state.clients.browser_session_back(session_id, payload),
    )


@app.post("/web/browser/sessions/{session_id}/forward", response_model=BrowserSessionActionResponse)
async def bridge_browser_session_forward(
    session_id: str,
    payload: BrowserSessionForwardRequest,
    response: Response,
    actor: str = Depends(authenticated_actor),
) -> BrowserSessionActionResponse | JSONResponse:
    request_id, trace_id = request_identity()
    return await browser_session_action_route(
        actor=actor,
        request_id=request_id,
        trace_id=trace_id,
        response=response,
        success_event_type="browser_session_forward",
        denied_event_type="browser_session_forward_denied",
        error_event_type="browser_session_forward_error",
        fallback_error_summary={
            "session_id": session_id,
            "snapshot_id": payload.snapshot_id,
            "current_url": "",
            "host": "",
        },
        call=lambda: app.state.clients.browser_session_forward(session_id, payload),
    )


@app.post("/web/browser/sessions/{session_id}/tabs/new", response_model=BrowserSessionActionResponse)
async def bridge_browser_session_new_tab(
    session_id: str,
    payload: BrowserSessionNewTabRequest,
    response: Response,
    actor: str = Depends(authenticated_actor),
) -> BrowserSessionActionResponse | JSONResponse:
    request_id, trace_id = request_identity()
    return await browser_session_action_route(
        actor=actor,
        request_id=request_id,
        trace_id=trace_id,
        response=response,
        success_event_type="browser_session_new_tab",
        denied_event_type="browser_session_new_tab_denied",
        error_event_type="browser_session_new_tab_error",
        fallback_error_summary={
            "session_id": session_id,
            "snapshot_id": "",
            "current_url": payload.url,
            "host": urlsplit(payload.url).hostname or "",
        },
        call=lambda: app.state.clients.browser_session_new_tab(session_id, payload),
    )


@app.post("/web/browser/sessions/{session_id}/tabs/switch", response_model=BrowserSessionActionResponse)
async def bridge_browser_session_switch_tab(
    session_id: str,
    payload: BrowserSessionSwitchTabRequest,
    response: Response,
    actor: str = Depends(authenticated_actor),
) -> BrowserSessionActionResponse | JSONResponse:
    request_id, trace_id = request_identity()
    return await browser_session_action_route(
        actor=actor,
        request_id=request_id,
        trace_id=trace_id,
        response=response,
        success_event_type="browser_session_switch_tab",
        denied_event_type="browser_session_switch_tab_denied",
        error_event_type="browser_session_switch_tab_error",
        fallback_error_summary={
            "session_id": session_id,
            "snapshot_id": payload.snapshot_id,
            "current_url": "",
            "host": "",
        },
        call=lambda: app.state.clients.browser_session_switch_tab(session_id, payload),
    )


@app.post("/web/browser/sessions/{session_id}/tabs/close", response_model=BrowserSessionActionResponse)
async def bridge_browser_session_close_tab(
    session_id: str,
    payload: BrowserSessionCloseTabRequest,
    response: Response,
    actor: str = Depends(authenticated_actor),
) -> BrowserSessionActionResponse | JSONResponse:
    request_id, trace_id = request_identity()
    return await browser_session_action_route(
        actor=actor,
        request_id=request_id,
        trace_id=trace_id,
        response=response,
        success_event_type="browser_session_close_tab",
        denied_event_type="browser_session_close_tab_denied",
        error_event_type="browser_session_close_tab_error",
        fallback_error_summary={
            "session_id": session_id,
            "snapshot_id": payload.snapshot_id,
            "current_url": "",
            "host": "",
        },
        call=lambda: app.state.clients.browser_session_close_tab(session_id, payload),
    )


@app.post("/web/browser/sessions/{session_id}/submit_proposal", response_model=ProposalRecord)
async def bridge_browser_submit_proposal(
    session_id: str,
    payload: BrowserSubmitProposalRequest,
    response: Response,
    actor: str = Depends(authenticated_actor),
) -> ProposalRecord | JSONResponse:
    request_id, trace_id = request_identity()
    preview, error = await browser_session_call(
        actor=actor,
        request_id=request_id,
        trace_id=trace_id,
        success_event_type="browser_submit_preview",
        denied_event_type="browser_submit_preview_denied",
        error_event_type="browser_submit_preview_error",
        success_summary=browser_submit_preview_summary,
        fallback_error_summary={
            "session_id": session_id,
            "snapshot_id": payload.snapshot_id,
            "current_url": "",
            "host": "",
        },
        call=lambda: app.state.clients.browser_prepare_submit(session_id, payload),
    )
    if error is not None:
        return error

    target_host = urlsplit(preview.target_url).hostname or ""
    if target_host not in app.state.settings.action_allowlist_hosts:
        append_event(
            event_type="browser_submit_proposal_denied",
            actor=actor,
            request_id=request_id,
            trace_id=trace_id,
            outcome="denied",
            summary={
                **browser_submit_preview_summary(preview, reason="host_not_in_action_allowlist"),
                "action_allowlist_hosts": sorted(app.state.settings.action_allowlist_hosts),
            },
        )
        return JSONResponse(
            status_code=403,
            headers=make_headers(request_id, trace_id),
            content={
                "request_id": request_id,
                "trace_id": trace_id,
                "reason": "host_not_in_action_allowlist",
                "host": target_host,
                "action_allowlist_hosts": sorted(app.state.settings.action_allowlist_hosts),
            },
        )

    record = app.state.proposal_store.create_proposal(
        action_type="browser_submit",
        action_payload={
            "session_id": preview.session_id,
            "snapshot_id": preview.snapshot_id,
            "submit_element_id": preview.submit_element_id,
            "target_url": preview.target_url,
            "method": preview.method,
            "field_preview": [field.model_dump() for field in preview.field_preview],
        },
        actor=actor,
        request_id=request_id,
        trace_id=trace_id,
    )
    append_event(
        event_type="browser_submit_proposal_created",
        actor=actor,
        request_id=request_id,
        trace_id=trace_id,
        outcome="success",
        summary={
            "proposal_id": record.proposal_id,
            "action_type": record.action_type,
            **browser_submit_preview_summary(preview),
        },
    )
    response.headers.update(make_headers(request_id, trace_id))
    return record


async def run_probe(probe_kind: str) -> EgressProbeReport | JSONResponse:
    request_id, trace_id = request_identity()

    try:
        result = await app.state.clients.run_agent_probe(probe_kind)
    except httpx.HTTPError as exc:
        append_event(
            event_type="system",
            actor="bridge",
            request_id=request_id,
            trace_id=trace_id,
            outcome="error",
            summary={
                "action": "direct_egress_probe",
                "probe_kind": probe_kind,
                "reason": f"{type(exc).__name__}: {exc}",
            },
        )
        return JSONResponse(
            status_code=502,
            headers=make_headers(request_id, trace_id),
            content={
                "detail": f"agent probe failed: {exc}",
                "request_id": request_id,
                "trace_id": trace_id,
            },
        )

    outcome = "denied"
    if result.reachable:
        outcome = "unexpected_allow"

    append_event(
        event_type="system",
        actor="bridge",
        request_id=request_id,
        trace_id=trace_id,
        outcome=outcome,
        summary={
            "action": "direct_egress_probe",
            "probe_kind": result.probe_kind,
            "target_url": result.target_url,
            "reachable": result.reachable,
            "error": result.error,
        },
    )

    if outcome != "denied":
        return JSONResponse(
            status_code=500,
            headers=make_headers(request_id, trace_id),
            content={
                "detail": f"unexpected direct {probe_kind} egress succeeded",
                "request_id": request_id,
                "trace_id": trace_id,
            },
        )

    return EgressProbeReport(
        request_id=request_id,
        trace_id=trace_id,
        outcome=outcome,
        probe=result,
    )


if DEBUG_PROBES_ENABLED:

    @app.post("/debug/probes/public-egress", response_model=EgressProbeReport)
    async def public_egress_probe(
        response: Response,
    ) -> EgressProbeReport | JSONResponse:
        result = await run_probe("public")
        if isinstance(result, JSONResponse):
            return result
        response.headers.update(make_headers(result.request_id, result.trace_id))
        return result


    @app.post("/debug/probes/provider-egress", response_model=EgressProbeReport)
    async def provider_egress_probe(
        response: Response,
    ) -> EgressProbeReport | JSONResponse:
        result = await run_probe("provider")
        if isinstance(result, JSONResponse):
            return result
        response.headers.update(make_headers(result.request_id, result.trace_id))
        return result
