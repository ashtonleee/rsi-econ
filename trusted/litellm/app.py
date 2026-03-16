import time
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException

from shared.mock_llm import deterministic_reply, deterministic_usage
from shared.schemas import ChatChoice, ChatCompletionRequest, ChatCompletionResponse, ChatMessage, ChatUsage, HealthReport
from trusted.litellm.config import SENTINEL_PROVIDER_KEY, litellm_settings


def run_startup_checks(app: FastAPI):
    settings = litellm_settings()

    app.state.settings = settings
    app.state.startup_checks = {
        "provider_key_configured": (
            settings.provider_api_key not in {"", SENTINEL_PROVIDER_KEY}
        ),
        "response_mode": settings.response_mode,
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    run_startup_checks(app)
    yield


app = FastAPI(title="trusted-litellm", lifespan=lifespan)


async def provider_chat_completion(
    settings,
    payload: ChatCompletionRequest,
) -> ChatCompletionResponse:
    headers = {"Authorization": f"Bearer {settings.provider_api_key}"}
    async with httpx.AsyncClient(
        base_url=settings.provider_base_url,
        timeout=30.0,
        headers=headers,
    ) as client:
        response = await client.post("/chat/completions", json=payload.model_dump())
        response.raise_for_status()
    return ChatCompletionResponse.model_validate(response.json())


@app.get("/healthz", response_model=HealthReport)
def healthz() -> HealthReport:
    settings = app.state.settings
    return HealthReport(
        service=settings.service_name,
        status="ok",
        stage=settings.stage,
        details=app.state.startup_checks,
    )


@app.post("/v1/chat/completions", response_model=ChatCompletionResponse)
async def chat_completions(payload: ChatCompletionRequest) -> ChatCompletionResponse:
    settings = app.state.settings
    if settings.response_mode == "provider_passthrough":
        try:
            return await provider_chat_completion(settings, payload)
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"provider passthrough failed: {type(exc).__name__}: {exc}",
            ) from exc

    assistant_message = ChatMessage(
        role="assistant",
        content=deterministic_reply(payload.messages),
    )
    usage = deterministic_usage(payload.messages)

    return ChatCompletionResponse(
        id=f"chatcmpl-{uuid4().hex[:12]}",
        object="chat.completion",
        created=int(time.time()),
        model=payload.model,
        choices=[
            ChatChoice(
                index=0,
                message=assistant_message,
                finish_reason="stop",
            )
        ],
        usage=ChatUsage.model_validate(usage.model_dump()),
    )
