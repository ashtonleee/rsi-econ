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
    checked_at: str | None = None


class BudgetState(BaseModel):
    unit: str
    total: int
    spent: int
    remaining: int
    exhausted: bool
    minimum_call_cost: int
    approximation: str
    total_prompt_tokens: int
    total_completion_tokens: int
    total_tokens: int


class RecentRequest(BaseModel):
    timestamp: str
    event_type: str
    request_id: str
    trace_id: str
    actor: str
    source_service: str
    outcome: str


class BridgeStatusReport(BaseModel):
    service: str
    stage: str
    trusted_state_dir: str
    log_path: str
    operational_state_path: str
    connections: dict[str, ConnectionStatus]
    budget: BudgetState
    counters: dict[str, int]
    recent_requests: list[RecentRequest]
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
    trace_id: str
    outcome: Literal["denied", "unexpected_allow"]
    probe: EgressProbeResult


class AgentRunEventRequest(BaseModel):
    run_id: str
    event_kind: Literal["run_start", "step", "run_end"]
    step_index: int | None = None
    tool_name: str | None = None
    summary: dict[str, Any] = Field(default_factory=dict)


class AgentRunEventReceipt(BaseModel):
    request_id: str
    trace_id: str
    outcome: Literal["recorded"]
