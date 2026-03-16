import httpx

from shared.schemas import (
    BrowserFollowHrefInternalResponse,
    BrowserFollowHrefRequest,
    BrowserRenderInternalResponse,
    BrowserRenderRequest,
    ChatCompletionRequest,
    ChatCompletionResponse,
    EgressProbeResult,
    FetcherFetchResponse,
    WebFetchRequest,
)


class TrustedBridgeClients:
    def __init__(self, *, litellm_url: str, fetcher_url: str, browser_url: str, agent_url: str):
        self.litellm_url = litellm_url.rstrip("/")
        self.fetcher_url = fetcher_url.rstrip("/")
        self.browser_url = browser_url.rstrip("/")
        self.agent_url = agent_url.rstrip("/")
        self.egress_url = ""

    @classmethod
    def with_egress(
        cls,
        *,
        litellm_url: str,
        fetcher_url: str,
        browser_url: str,
        egress_url: str,
        agent_url: str,
    ):
        instance = cls(
            litellm_url=litellm_url,
            fetcher_url=fetcher_url,
            browser_url=browser_url,
            agent_url=agent_url,
        )
        instance.egress_url = egress_url.rstrip("/")
        return instance

    async def litellm_health(self) -> tuple[bool, str | None]:
        try:
            async with httpx.AsyncClient(base_url=self.litellm_url, timeout=3.0) as client:
                response = await client.get("/healthz")
                response.raise_for_status()
        except httpx.HTTPError as exc:
            return False, f"{type(exc).__name__}: {exc}"
        return True, None

    async def fetcher_health(self) -> tuple[bool, str | None]:
        try:
            async with httpx.AsyncClient(base_url=self.fetcher_url, timeout=3.0) as client:
                response = await client.get("/healthz")
                response.raise_for_status()
        except httpx.HTTPError as exc:
            return False, f"{type(exc).__name__}: {exc}"
        return True, None

    async def browser_health(self) -> tuple[bool, str | None]:
        try:
            async with httpx.AsyncClient(base_url=self.browser_url, timeout=3.0) as client:
                response = await client.get("/healthz")
                response.raise_for_status()
        except httpx.HTTPError as exc:
            return False, f"{type(exc).__name__}: {exc}"
        return True, None

    async def egress_health(self) -> tuple[bool, str | None]:
        if not self.egress_url:
            return False, "egress_url_not_configured"
        try:
            async with httpx.AsyncClient(base_url=self.egress_url, timeout=3.0) as client:
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

    async def fetch_url(self, payload: WebFetchRequest) -> FetcherFetchResponse:
        async with httpx.AsyncClient(base_url=self.fetcher_url, timeout=10.0) as client:
            response = await client.post("/internal/fetch", json=payload.model_dump())
            response.raise_for_status()
        return FetcherFetchResponse.model_validate(response.json())

    async def browser_render(
        self,
        payload: BrowserRenderRequest,
    ) -> BrowserRenderInternalResponse:
        async with httpx.AsyncClient(base_url=self.browser_url, timeout=20.0) as client:
            response = await client.post("/internal/render", json=payload.model_dump())
            response.raise_for_status()
        return BrowserRenderInternalResponse.model_validate(response.json())

    async def browser_follow_href(
        self,
        payload: BrowserFollowHrefRequest,
    ) -> BrowserFollowHrefInternalResponse:
        async with httpx.AsyncClient(base_url=self.browser_url, timeout=25.0) as client:
            response = await client.post("/internal/follow-href", json=payload.model_dump())
            response.raise_for_status()
        return BrowserFollowHrefInternalResponse.model_validate(response.json())

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
