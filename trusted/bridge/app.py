from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from shared.config import bridge_settings
from shared.mock_llm import MINIMUM_DETERMINISTIC_CALL_TOKENS, deterministic_usage
from shared.schemas import (
    AgentRunEventReceipt,
    AgentRunEventRequest,
    BridgeStatusReport,
    BudgetState,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ConnectionStatus,
    EgressProbeReport,
    FetcherFetchResponse,
    HealthReport,
    RecentRequest,
    RecoveryState,
    WebFetchRequest,
    WebFetchResponse,
    WebState,
)
from trusted.bridge.clients import TrustedBridgeClients
from trusted.recovery.store import WorkspaceRecoveryStore
from trusted.state.store import TrustedStateManager, utc_now_iso


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
        "browser": "stubbed_for_stage_6",
        "approvals": "stubbed_for_stage_7",
        "consequential_actions": "stubbed_for_stage_8",
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


def caller_actor(request: Request, *, default: str) -> str:
    actor = request.headers.get("x-rsi-actor", "").strip()
    return actor or default


def request_identity() -> tuple[str, str]:
    return uuid4().hex, uuid4().hex


async def trusted_connections_payload() -> dict[str, dict]:
    reachable, detail = await app.state.clients.litellm_health()
    fetcher_reachable, fetcher_detail = await app.state.clients.fetcher_health()
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
    }


def make_status_report(snapshot: dict) -> BridgeStatusReport:
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
        counters={key: int(value) for key, value in snapshot["counters"].items()},
        recent_requests=[
            RecentRequest.model_validate(payload)
            for payload in snapshot["recent_requests"]
        ],
        surfaces=dict(snapshot["surfaces"]),
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
    )
    app.state.clients = TrustedBridgeClients(
        litellm_url=settings.litellm_url,
        fetcher_url=settings.fetcher_url,
        agent_url=settings.agent_url,
    )
    app.state.startup_checks = {
        "trusted_state_ready": True,
        "trusted_state_dir": str(state_dir),
        "log_path": str(app.state.state_manager.canonical_log_path),
        "operational_state_path": str(app.state.state_manager.operational_state_path),
        "checkpoint_dir": str(settings.checkpoint_dir),
        "fetcher_url": settings.fetcher_url,
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
        },
    )


@app.get("/status", response_model=BridgeStatusReport)
async def status(request: Request, response: Response) -> BridgeStatusReport:
    request_id, trace_id = request_identity()
    snapshot = app.state.state_manager.snapshot(refresh=True)
    connections = await trusted_connections_payload()
    status_web = dict(snapshot["web"])
    status_web["fetcher"] = connections["fetcher"]
    append_event(
        event_type="status_query",
        actor=caller_actor(request, default="operator"),
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
        },
    )
    response.headers.update(make_headers(request_id, trace_id))
    return make_status_report(app.state.state_manager.snapshot())


@app.post("/agent/runs/events", response_model=AgentRunEventReceipt)
async def agent_run_event(
    payload: AgentRunEventRequest,
    request: Request,
    response: Response,
) -> AgentRunEventReceipt:
    request_id, trace_id = request_identity()
    append_event(
        event_type="agent_run",
        actor=caller_actor(request, default="agent"),
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


@app.post("/llm/chat/completions", response_model=ChatCompletionResponse)
async def chat_completions(
    payload: ChatCompletionRequest,
    request: Request,
) -> ChatCompletionResponse | JSONResponse:
    request_id, trace_id = request_identity()
    actor = caller_actor(request, default="unknown")
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
        "used_ip": fetch_result.used_ip,
        "redirect_chain": list(fetch_result.redirect_chain),
        "http_status": fetch_result.http_status,
        "content_type": fetch_result.content_type,
        "byte_count": fetch_result.byte_count,
        "truncated": fetch_result.truncated,
        "content_sha256": fetch_result.content_sha256,
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
        "used_ip": detail.get("used_ip"),
        "redirect_chain": list(detail.get("redirect_chain", [])),
        "http_status": detail.get("http_status"),
        "content_type": detail.get("content_type"),
        "byte_count": int(detail.get("byte_count", 0)),
        "truncated": bool(detail.get("truncated", False)),
        "content_sha256": detail.get("content_sha256", ""),
        "reason": detail.get("reason", detail.get("detail", "fetch_failed")),
    }


@app.post("/web/fetch", response_model=WebFetchResponse)
async def bridge_fetch(
    payload: WebFetchRequest,
    request: Request,
    response: Response,
) -> WebFetchResponse | JSONResponse:
    request_id, trace_id = request_identity()
    actor = caller_actor(request, default="agent")

    try:
        fetch_result = await app.state.clients.fetch_url(payload)
    except httpx.HTTPStatusError as exc:
        detail = exc.response.json()
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
                "used_ip": None,
                "redirect_chain": [],
                "http_status": None,
                "content_type": None,
                "byte_count": 0,
                "truncated": False,
                "content_sha256": "",
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


@app.post("/debug/probes/public-egress", response_model=EgressProbeReport)
async def public_egress_probe(
    response: Response,
) -> EgressProbeReport | JSONResponse:
    if not app.state.settings.enable_debug_probes:
        raise HTTPException(status_code=404, detail="debug probes disabled")
    result = await run_probe("public")
    if isinstance(result, JSONResponse):
        return result
    response.headers.update(make_headers(result.request_id, result.trace_id))
    return result


@app.post("/debug/probes/provider-egress", response_model=EgressProbeReport)
async def provider_egress_probe(
    response: Response,
) -> EgressProbeReport | JSONResponse:
    if not app.state.settings.enable_debug_probes:
        raise HTTPException(status_code=404, detail="debug probes disabled")
    result = await run_probe("provider")
    if isinstance(result, JSONResponse):
        return result
    response.headers.update(make_headers(result.request_id, result.trace_id))
    return result
