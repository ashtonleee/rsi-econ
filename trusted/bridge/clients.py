import httpx

from shared.schemas import ChatCompletionRequest, ChatCompletionResponse, EgressProbeResult


class TrustedBridgeClients:
    def __init__(self, *, litellm_url: str, agent_url: str):
        self.litellm_url = litellm_url.rstrip("/")
        self.agent_url = agent_url.rstrip("/")

    async def litellm_health(self) -> tuple[bool, str | None]:
        try:
            async with httpx.AsyncClient(base_url=self.litellm_url, timeout=3.0) as client:
                response = await client.get("/healthz")
                response.raise_for_status()
        except httpx.HTTPError as exc:
            return False, f"{type(exc).__name__}: {exc}"
        return True, None

    async def chat_completion(
        self,
        payload: ChatCompletionRequest,
    ) -> ChatCompletionResponse:
        async with httpx.AsyncClient(base_url=self.litellm_url, timeout=10.0) as client:
            response = await client.post("/v1/chat/completions", json=payload.model_dump())
            response.raise_for_status()
        return ChatCompletionResponse.model_validate(response.json())

    async def run_agent_probe(self, probe_kind: str) -> EgressProbeResult:
        if probe_kind == "public":
            path = "/internal/probes/public-egress"
        elif probe_kind == "provider":
            path = "/internal/probes/provider-egress"
        else:
            raise ValueError(f"unsupported probe kind: {probe_kind}")

        async with httpx.AsyncClient(base_url=self.agent_url, timeout=5.0) as client:
            response = await client.post(path)
            response.raise_for_status()
        return EgressProbeResult.model_validate(response.json())
