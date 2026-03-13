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


class RecoveryCheckpoint(BaseModel):
    checkpoint_id: str
    created_at: str
    archive_path: str
    manifest_path: str
    label: str | None = None
    workspace_digest: str | None = None
    file_count: int | None = None
    size_bytes: int | None = None


class RecoveryAction(BaseModel):
    action: str
    timestamp: str
    outcome: str
    checkpoint_id: str | None = None
    baseline_id: str | None = None
    detail: str | None = None
    request_id: str | None = None
    trace_id: str | None = None


class RecoveryState(BaseModel):
    checkpoint_dir: str
    baseline_id: str
    baseline_source_dir: str
    baseline_archive_path: str
    available_checkpoints: list[RecoveryCheckpoint]
    latest_checkpoint_id: str | None = None
    latest_action: RecoveryAction | None = None
    current_workspace_status: str


class WebFetchRecord(BaseModel):
    timestamp: str
    request_id: str
    trace_id: str
    outcome: str
    normalized_url: str
    host: str
    http_status: int | None = None
    content_type: str | None = None
    byte_count: int = 0
    truncated: bool = False


class WebState(BaseModel):
    fetcher: ConnectionStatus
    allowlist_hosts: list[str]
    private_test_hosts: list[str]
    allowed_content_types: list[str]
    caps: dict[str, int | float]
    counters: dict[str, int]
    recent_fetches: list[WebFetchRecord]


class BridgeStatusReport(BaseModel):
    service: str
    stage: str
    trusted_state_dir: str
    log_path: str
    operational_state_path: str
    connections: dict[str, ConnectionStatus]
    budget: BudgetState
    recovery: RecoveryState
    web: WebState
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


class WebFetchRequest(BaseModel):
    url: str


class FetcherFetchResponse(BaseModel):
    normalized_url: str
    final_url: str
    scheme: Literal["http", "https"]
    host: str
    port: int
    http_status: int
    content_type: str
    byte_count: int
    truncated: bool
    redirect_chain: list[str]
    resolved_ips: list[str]
    used_ip: str | None = None
    content_sha256: str
    text: str


class WebFetchResponse(FetcherFetchResponse):
    request_id: str
    trace_id: str
