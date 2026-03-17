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


class BrowserRenderRecord(BaseModel):
    timestamp: str
    request_id: str
    trace_id: str
    outcome: str
    normalized_url: str
    final_url: str
    http_status: int | None = None
    page_title: str = ""
    text_bytes: int = 0
    text_truncated: bool = False
    screenshot_bytes: int = 0


class BrowserFollowRecord(BaseModel):
    timestamp: str
    request_id: str
    trace_id: str
    outcome: str
    source_url: str
    requested_target_url: str
    final_url: str
    http_status: int | None = None
    page_title: str = ""
    text_bytes: int = 0
    text_truncated: bool = False
    screenshot_bytes: int = 0


class BrowserState(BaseModel):
    service: ConnectionStatus
    caps: dict[str, int | float]
    counters: dict[str, int]
    recent_renders: list[BrowserRenderRecord]
    recent_follows: list[BrowserFollowRecord] = Field(default_factory=list)


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
    browser: BrowserState
    counters: dict[str, int]
    recent_requests: list[RecentRequest]
    surfaces: dict[str, str]
    proposals: "ProposalState | None" = None


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


class MediationHopRecord(BaseModel):
    channel: str
    requested_url: str
    normalized_url: str
    host: str
    approved_ips: list[str] = Field(default_factory=list)
    actual_peer_ip: str | None = None
    dialed_ip: str | None = None
    disposition: Literal["allowed", "denied", "classified"]
    reason: str
    http_status: int | None = None
    enforcement_stage: Literal["unknown", "pre_connect", "post_connect"] = "unknown"
    request_forwarded: bool = False


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
    approved_ips: list[str] = Field(default_factory=list)
    actual_peer_ip: str | None = None
    used_ip: str | None = None
    content_sha256: str
    text: str
    mediation_hops: list[MediationHopRecord] = Field(default_factory=list)


class WebFetchResponse(FetcherFetchResponse):
    request_id: str
    trace_id: str


class BrowserRenderRequest(BaseModel):
    url: str


class BrowserFollowLink(BaseModel):
    text: str
    target_url: str
    same_origin: bool


class BrowserChannelRecord(BaseModel):
    channel: str
    requested_url: str
    normalized_url: str
    host: str
    approved_ips: list[str] = Field(default_factory=list)
    actual_peer_ip: str | None = None
    dialed_ip: str | None = None
    disposition: Literal["allowed", "denied", "classified"]
    reason: str
    top_level: bool = False
    navigation: bool = False
    enforcement_stage: Literal["unknown", "pre_connect", "post_connect"] = "unknown"
    request_forwarded: bool = False


class BrowserRenderInternalResponse(BaseModel):
    normalized_url: str
    final_url: str
    http_status: int | None = None
    page_title: str
    meta_description: str
    rendered_text: str
    rendered_text_sha256: str
    text_bytes: int
    text_truncated: bool
    screenshot_png_base64: str
    screenshot_sha256: str
    screenshot_bytes: int
    redirect_chain: list[str]
    observed_hosts: list[str]
    resolved_ips: list[str]
    channel_records: list[BrowserChannelRecord] = Field(default_factory=list)
    followable_links: list[BrowserFollowLink] = Field(default_factory=list)


class BrowserRenderResponse(BrowserRenderInternalResponse):
    request_id: str
    trace_id: str


class BrowserFollowHrefRequest(BaseModel):
    source_url: str
    target_url: str


class BrowserFollowHrefInternalResponse(BaseModel):
    source_url: str
    source_final_url: str
    requested_target_url: str
    matched_link_text: str
    follow_hop_count: int
    navigation_history: list[str]
    normalized_url: str
    final_url: str
    http_status: int | None = None
    page_title: str
    meta_description: str
    rendered_text: str
    rendered_text_sha256: str
    text_bytes: int
    text_truncated: bool
    screenshot_png_base64: str
    screenshot_sha256: str
    screenshot_bytes: int
    redirect_chain: list[str]
    observed_hosts: list[str]
    resolved_ips: list[str]
    channel_records: list[BrowserChannelRecord] = Field(default_factory=list)


class BrowserFollowHrefResponse(BrowserFollowHrefInternalResponse):
    request_id: str
    trace_id: str


class EgressFetchRequest(BaseModel):
    url: str
    channel: str
    headers: dict[str, str] = Field(default_factory=dict)
    max_body_bytes: int = 0
    method: str = "GET"
    request_body_base64: str = ""
    request_content_type: str = ""


class EgressFetchResponse(BaseModel):
    normalized_url: str
    scheme: Literal["http", "https"]
    host: str
    port: int
    channel: str
    approved_ips: list[str] = Field(default_factory=list)
    actual_peer_ip: str | None = None
    dialed_ip: str | None = None
    request_forwarded: bool = False
    enforcement_stage: Literal["pre_connect"]
    http_status: int
    headers: dict[str, str] = Field(default_factory=dict)
    body_base64: str


class EgressDecisionRecord(BaseModel):
    token: str
    channel: str
    requested_url: str
    normalized_url: str
    host: str
    approved_ips: list[str] = Field(default_factory=list)
    actual_peer_ip: str | None = None
    dialed_ip: str | None = None
    disposition: Literal["allowed", "denied", "classified"]
    reason: str
    http_status: int | None = None
    enforcement_stage: Literal["pre_connect"]
    request_forwarded: bool = False


class EgressDecisionDrainRequest(BaseModel):
    tokens: list[str] = Field(default_factory=list)


class EgressDecisionDrainResponse(BaseModel):
    records: list[EgressDecisionRecord] = Field(default_factory=list)


# --- Stage 7: Proposal / Approval Flow ---

ProposalStatus = Literal["pending", "approved", "rejected", "executing", "executed", "failed"]


class ProposalCreateRequest(BaseModel):
    action_type: str
    action_payload: dict[str, Any] = Field(default_factory=dict)


class ProposalDecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]
    reason: str = ""


class ProposalRecord(BaseModel):
    proposal_id: str
    action_type: str
    action_payload: dict[str, Any] = Field(default_factory=dict)
    status: ProposalStatus = "pending"
    created_by: str = ""
    created_at: str = ""
    decided_by: str | None = None
    decided_at: str | None = None
    decision_reason: str | None = None
    executed_by: str | None = None
    executed_at: str | None = None
    execution_result: dict[str, Any] | None = None
    request_id: str = ""
    trace_id: str = ""


class ProposalListResponse(BaseModel):
    proposals: list[ProposalRecord] = Field(default_factory=list)


class ProposalState(BaseModel):
    total: int = 0
    pending: int = 0
    approved: int = 0
    rejected: int = 0
    executing: int = 0
    executed: int = 0
    failed: int = 0
    recent_proposals: list[ProposalRecord] = Field(default_factory=list)


# Resolve forward reference in BridgeStatusReport
BridgeStatusReport.model_rebuild()
