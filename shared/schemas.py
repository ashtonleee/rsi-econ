from typing import Any, Literal

from pydantic import BaseModel, Field


class HealthReport(BaseModel):
    service: str
    status: Literal["ok"]
    stage: str
    details: dict[str, Any]


class ConnectionStatus(BaseModel):
    url: str
    reachable: bool
    detail: str | None = None


class BridgeStatusReport(BaseModel):
    service: str
    stage: str
    trusted_state_dir: str
    log_path: str
    connections: dict[str, ConnectionStatus]
    surfaces: dict[str, str]


class BridgeProbeReport(BaseModel):
    agent: str
    bridge_url: str
    reachable: bool
    bridge: HealthReport


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class ChatCompletionRequest(BaseModel):
    model: str = "stage1-deterministic"
    messages: list[ChatMessage] = Field(default_factory=list)


class ChatChoice(BaseModel):
    index: int
    message: ChatMessage
    finish_reason: Literal["stop"]


class ChatUsage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ChatCompletionResponse(BaseModel):
    id: str
    object: Literal["chat.completion"]
    created: int
    model: str
    choices: list[ChatChoice]
    usage: ChatUsage


class EgressProbeResult(BaseModel):
    probe_kind: Literal["public", "provider"]
    target_url: str
    reachable: bool
    error: str | None = None


class EgressProbeReport(BaseModel):
    request_id: str
    outcome: Literal["denied", "unexpected_allow"]
    probe: EgressProbeResult
