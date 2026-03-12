from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException

from shared.config import agent_settings
from shared.schemas import BridgeProbeReport, EgressProbeResult, HealthReport
from untrusted.agent.bridge_client import probe_bridge
from untrusted.agent.egress import attempt_egress


def run_startup_checks(app: FastAPI):
    settings = agent_settings()
    app.state.settings = settings
    app.state.startup_checks = {
        "bridge_url_configured": True,
        "bridge_url": settings.bridge_url,
        "public_probe_url": settings.public_probe_url,
        "provider_probe_url": settings.provider_probe_url,
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    run_startup_checks(app)
    yield


app = FastAPI(title="untrusted-agent", lifespan=lifespan)


@app.get("/healthz", response_model=HealthReport)
def healthz() -> HealthReport:
    settings = app.state.settings
    return HealthReport(
        service=settings.service_name,
        status="ok",
        stage=settings.stage,
        details=app.state.startup_checks,
    )


@app.get("/probe/bridge", response_model=BridgeProbeReport)
async def bridge_probe() -> BridgeProbeReport:
    settings = app.state.settings

    try:
        result = await probe_bridge(settings.bridge_url)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"bridge probe failed: {exc}") from exc

    return BridgeProbeReport(
        agent=settings.service_name,
        bridge_url=result["bridge_url"],
        reachable=result["reachable"],
        bridge=result["bridge"],
    )


@app.post("/internal/probes/public-egress", response_model=EgressProbeResult)
async def public_egress_probe() -> EgressProbeResult:
    settings = app.state.settings
    return await attempt_egress(
        probe_kind="public",
        target_url=settings.public_probe_url,
    )


@app.post("/internal/probes/provider-egress", response_model=EgressProbeResult)
async def provider_egress_probe() -> EgressProbeResult:
    settings = app.state.settings
    return await attempt_egress(
        probe_kind="provider",
        target_url=settings.provider_probe_url,
    )
