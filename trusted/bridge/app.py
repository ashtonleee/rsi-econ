from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException, Response

from shared.config import bridge_settings
from shared.schemas import BridgeStatusReport, ChatCompletionRequest, ChatCompletionResponse, ConnectionStatus, EgressProbeReport, EgressProbeResult, HealthReport
from trusted.bridge.clients import TrustedBridgeClients
from trusted.bridge.logging import EventLogger


def log_path_for(settings) -> Path:
    return settings.log_dir / "bridge_events.jsonl"


def run_startup_checks(app: FastAPI):
    settings = bridge_settings()
    state_dir = settings.trusted_state_dir
    state_dir.mkdir(parents=True, exist_ok=True)
    settings.log_dir.mkdir(parents=True, exist_ok=True)

    # Keep startup checks simple and local to the trusted boundary.
    probe_file = state_dir / ".bridge_write_probe"
    probe_file.write_text("ok\n", encoding="ascii")
    probe_file.unlink()

    logger = EventLogger(path=log_path_for(settings))
    app.state.settings = settings
    app.state.logger = logger
    app.state.clients = TrustedBridgeClients(
        litellm_url=settings.litellm_url,
        agent_url=settings.agent_url,
    )
    app.state.startup_checks = {
        "trusted_state_ready": True,
        "trusted_state_dir": str(state_dir),
        "log_path": str(logger.path),
    }


def make_status(*, litellm_reachable: bool, litellm_detail: str | None) -> BridgeStatusReport:
    settings = app.state.settings
    return BridgeStatusReport(
        service=settings.service_name,
        stage=settings.stage,
        trusted_state_dir=str(settings.trusted_state_dir),
        log_path=str(app.state.logger.path),
        connections={
            "litellm": ConnectionStatus(
                url=settings.litellm_url,
                reachable=litellm_reachable,
                detail=litellm_detail,
            ),
        },
        surfaces={
            "litellm": "mediated_via_trusted_service",
            "canonical_logging": "stubbed_for_stage_2",
            "seed_agent": "stubbed_for_stage_3",
            "approvals": "stubbed_for_stage_7",
        },
    )


async def current_litellm_status() -> tuple[bool, str | None]:
    return await app.state.clients.litellm_health()


def event_logger() -> EventLogger:
    return app.state.logger


@asynccontextmanager
async def lifespan(app: FastAPI):
    run_startup_checks(app)
    yield


app = FastAPI(title="trusted-bridge", lifespan=lifespan)


@app.get("/healthz", response_model=HealthReport)
async def healthz() -> HealthReport:
    settings = app.state.settings
    litellm_reachable, litellm_detail = await current_litellm_status()
    return HealthReport(
        service=settings.service_name,
        status="ok",
        stage=settings.stage,
        details={
            **app.state.startup_checks,
            "litellm_reachable": litellm_reachable,
            "litellm_detail": litellm_detail,
        },
    )


@app.get("/status", response_model=BridgeStatusReport)
async def status() -> BridgeStatusReport:
    litellm_reachable, litellm_detail = await current_litellm_status()
    return make_status(
        litellm_reachable=litellm_reachable,
        litellm_detail=litellm_detail,
    )


@app.post("/llm/chat/completions", response_model=ChatCompletionResponse)
async def chat_completions(
    payload: ChatCompletionRequest,
    response: Response,
) -> ChatCompletionResponse:
    request_id = uuid4().hex
    logger = event_logger()

    logger.append(
        event_type="mediated_llm_request",
        actor="bridge",
        request_id=request_id,
        target="litellm",
        outcome="allow",
        summary=f"forwarded model={payload.model}",
    )

    try:
        result = await app.state.clients.chat_completion(payload)
    except httpx.HTTPError as exc:
        logger.append(
            event_type="mediated_llm_response",
            actor="bridge",
            request_id=request_id,
            target="litellm",
            outcome="error",
            summary=f"{type(exc).__name__}: {exc}",
        )
        raise HTTPException(status_code=502, detail=f"litellm call failed: {exc}") from exc

    logger.append(
        event_type="mediated_llm_response",
        actor="bridge",
        request_id=request_id,
        target="litellm",
        outcome="success",
        summary="litellm response returned",
        extra={"model": result.model},
    )
    response.headers["x-request-id"] = request_id
    return result


async def run_probe(probe_kind: str, response: Response) -> EgressProbeReport:
    request_id = uuid4().hex
    logger = event_logger()

    try:
        result = await app.state.clients.run_agent_probe(probe_kind)
    except httpx.HTTPError as exc:
        logger.append(
            event_type="direct_egress_probe",
            actor="bridge",
            request_id=request_id,
            target=probe_kind,
            outcome="error",
            summary=f"{type(exc).__name__}: {exc}",
        )
        raise HTTPException(status_code=502, detail=f"agent probe failed: {exc}") from exc

    outcome = "denied"
    if result.reachable:
        outcome = "unexpected_allow"

    logger.append(
        event_type="direct_egress_probe",
        actor="bridge",
        request_id=request_id,
        target=result.target_url,
        outcome=outcome,
        summary=result.error or "probe completed",
        extra={"probe_kind": result.probe_kind},
    )

    response.headers["x-request-id"] = request_id
    if outcome != "denied":
        raise HTTPException(
            status_code=500,
            detail=f"unexpected direct {probe_kind} egress succeeded",
        )

    return EgressProbeReport(request_id=request_id, outcome=outcome, probe=result)


@app.post("/debug/probes/public-egress", response_model=EgressProbeReport)
async def public_egress_probe(response: Response) -> EgressProbeReport:
    return await run_probe("public", response)


@app.post("/debug/probes/provider-egress", response_model=EgressProbeReport)
async def provider_egress_probe(response: Response) -> EgressProbeReport:
    return await run_probe("provider", response)
