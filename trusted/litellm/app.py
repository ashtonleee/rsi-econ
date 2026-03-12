import time
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI

from shared.mock_llm import deterministic_reply, deterministic_usage
from shared.schemas import ChatChoice, ChatCompletionRequest, ChatCompletionResponse, ChatMessage, ChatUsage, HealthReport
from trusted.litellm.config import litellm_settings


def run_startup_checks(app: FastAPI):
    settings = litellm_settings()

    app.state.settings = settings
    app.state.startup_checks = {
        "provider_key_configured": True,
        "response_mode": "deterministic_stage3_mock",
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    run_startup_checks(app)
    yield


app = FastAPI(title="trusted-litellm", lifespan=lifespan)


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
def chat_completions(payload: ChatCompletionRequest) -> ChatCompletionResponse:
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
