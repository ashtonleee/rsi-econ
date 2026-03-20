import httpx

from shared.schemas import (
    BrowserFollowHrefInternalResponse,
    BrowserFollowHrefRequest,
    BrowserHttpRequestExecuteInternalResponse,
    BrowserHttpRequestExecuteRequest,
    BrowserSessionActionInternalResponse,
    BrowserSessionBackRequest,
    BrowserSessionClickRequest,
    BrowserSessionCloseTabRequest,
    BrowserSessionFillRequest,
    BrowserSessionForwardRequest,
    BrowserSessionHoverRequest,
    BrowserSessionNavigateRequest,
    BrowserSessionNewTabRequest,
    BrowserSessionOpenRequest,
    BrowserSessionPressRequest,
    BrowserSessionSelectRequest,
    BrowserSessionSetCheckedRequest,
    BrowserSessionSnapshotInternalResponse,
    BrowserSessionSwitchTabRequest,
    BrowserSessionTypeRequest,
    BrowserSessionWaitForRequest,
    BrowserSubmitExecuteInternalResponse,
    BrowserSubmitExecuteRequest,
    BrowserSubmitPreviewInternalResponse,
    BrowserSubmitProposalRequest,
    BrowserRenderInternalResponse,
    BrowserRenderRequest,
    ChatCompletionRequest,
    ChatCompletionResponse,
    EgressFetchRequest,
    EgressFetchResponse,
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

    async def browser_session_open(
        self,
        payload: BrowserSessionOpenRequest,
    ) -> BrowserSessionSnapshotInternalResponse:
        async with httpx.AsyncClient(base_url=self.browser_url, timeout=25.0) as client:
            response = await client.post("/internal/sessions/open", json=payload.model_dump())
            response.raise_for_status()
        return BrowserSessionSnapshotInternalResponse.model_validate(response.json())

    async def browser_session_snapshot(self, session_id: str) -> BrowserSessionSnapshotInternalResponse:
        async with httpx.AsyncClient(base_url=self.browser_url, timeout=25.0) as client:
            response = await client.get(f"/internal/sessions/{session_id}")
            response.raise_for_status()
        return BrowserSessionSnapshotInternalResponse.model_validate(response.json())

    async def browser_session_click(
        self,
        session_id: str,
        payload: BrowserSessionClickRequest,
    ) -> BrowserSessionSnapshotInternalResponse:
        async with httpx.AsyncClient(base_url=self.browser_url, timeout=25.0) as client:
            response = await client.post(
                f"/internal/sessions/{session_id}/click",
                json=payload.model_dump(),
            )
            response.raise_for_status()
        return BrowserSessionSnapshotInternalResponse.model_validate(response.json())

    async def browser_session_type(
        self,
        session_id: str,
        payload: BrowserSessionTypeRequest,
    ) -> BrowserSessionSnapshotInternalResponse:
        async with httpx.AsyncClient(base_url=self.browser_url, timeout=25.0) as client:
            response = await client.post(
                f"/internal/sessions/{session_id}/type",
                json=payload.model_dump(),
            )
            response.raise_for_status()
        return BrowserSessionSnapshotInternalResponse.model_validate(response.json())

    async def browser_session_select(
        self,
        session_id: str,
        payload: BrowserSessionSelectRequest,
    ) -> BrowserSessionSnapshotInternalResponse:
        async with httpx.AsyncClient(base_url=self.browser_url, timeout=25.0) as client:
            response = await client.post(
                f"/internal/sessions/{session_id}/select",
                json=payload.model_dump(),
            )
            response.raise_for_status()
        return BrowserSessionSnapshotInternalResponse.model_validate(response.json())

    async def browser_session_set_checked(
        self,
        session_id: str,
        payload: BrowserSessionSetCheckedRequest,
    ) -> BrowserSessionSnapshotInternalResponse:
        async with httpx.AsyncClient(base_url=self.browser_url, timeout=25.0) as client:
            response = await client.post(
                f"/internal/sessions/{session_id}/set-checked",
                json=payload.model_dump(),
            )
            response.raise_for_status()
        return BrowserSessionSnapshotInternalResponse.model_validate(response.json())

    async def browser_session_navigate(
        self,
        session_id: str,
        payload: BrowserSessionNavigateRequest,
    ) -> BrowserSessionActionInternalResponse:
        async with httpx.AsyncClient(base_url=self.browser_url, timeout=25.0) as client:
            response = await client.post(
                f"/internal/sessions/{session_id}/navigate",
                json=payload.model_dump(),
            )
            response.raise_for_status()
        return BrowserSessionActionInternalResponse.model_validate(response.json())

    async def browser_session_click_action(
        self,
        session_id: str,
        payload: BrowserSessionClickRequest,
    ) -> BrowserSessionActionInternalResponse:
        async with httpx.AsyncClient(base_url=self.browser_url, timeout=25.0) as client:
            response = await client.post(
                f"/internal/sessions/{session_id}/actions/click",
                json=payload.model_dump(),
            )
            response.raise_for_status()
        return BrowserSessionActionInternalResponse.model_validate(response.json())

    async def browser_session_fill(
        self,
        session_id: str,
        payload: BrowserSessionFillRequest,
    ) -> BrowserSessionActionInternalResponse:
        async with httpx.AsyncClient(base_url=self.browser_url, timeout=25.0) as client:
            response = await client.post(
                f"/internal/sessions/{session_id}/fill",
                json=payload.model_dump(),
            )
            response.raise_for_status()
        return BrowserSessionActionInternalResponse.model_validate(response.json())

    async def browser_session_select_action(
        self,
        session_id: str,
        payload: BrowserSessionSelectRequest,
    ) -> BrowserSessionActionInternalResponse:
        async with httpx.AsyncClient(base_url=self.browser_url, timeout=25.0) as client:
            response = await client.post(
                f"/internal/sessions/{session_id}/actions/select",
                json=payload.model_dump(),
            )
            response.raise_for_status()
        return BrowserSessionActionInternalResponse.model_validate(response.json())

    async def browser_session_set_checked_action(
        self,
        session_id: str,
        payload: BrowserSessionSetCheckedRequest,
    ) -> BrowserSessionActionInternalResponse:
        async with httpx.AsyncClient(base_url=self.browser_url, timeout=25.0) as client:
            response = await client.post(
                f"/internal/sessions/{session_id}/actions/set-checked",
                json=payload.model_dump(),
            )
            response.raise_for_status()
        return BrowserSessionActionInternalResponse.model_validate(response.json())

    async def browser_session_press(
        self,
        session_id: str,
        payload: BrowserSessionPressRequest,
    ) -> BrowserSessionActionInternalResponse:
        async with httpx.AsyncClient(base_url=self.browser_url, timeout=25.0) as client:
            response = await client.post(
                f"/internal/sessions/{session_id}/press",
                json=payload.model_dump(),
            )
            response.raise_for_status()
        return BrowserSessionActionInternalResponse.model_validate(response.json())

    async def browser_session_hover(
        self,
        session_id: str,
        payload: BrowserSessionHoverRequest,
    ) -> BrowserSessionActionInternalResponse:
        async with httpx.AsyncClient(base_url=self.browser_url, timeout=25.0) as client:
            response = await client.post(
                f"/internal/sessions/{session_id}/hover",
                json=payload.model_dump(),
            )
            response.raise_for_status()
        return BrowserSessionActionInternalResponse.model_validate(response.json())

    async def browser_session_wait_for(
        self,
        session_id: str,
        payload: BrowserSessionWaitForRequest,
    ) -> BrowserSessionActionInternalResponse:
        async with httpx.AsyncClient(base_url=self.browser_url, timeout=25.0) as client:
            response = await client.post(
                f"/internal/sessions/{session_id}/wait-for",
                json=payload.model_dump(),
            )
            response.raise_for_status()
        return BrowserSessionActionInternalResponse.model_validate(response.json())

    async def browser_session_back(
        self,
        session_id: str,
        payload: BrowserSessionBackRequest,
    ) -> BrowserSessionActionInternalResponse:
        async with httpx.AsyncClient(base_url=self.browser_url, timeout=25.0) as client:
            response = await client.post(
                f"/internal/sessions/{session_id}/back",
                json=payload.model_dump(),
            )
            response.raise_for_status()
        return BrowserSessionActionInternalResponse.model_validate(response.json())

    async def browser_session_forward(
        self,
        session_id: str,
        payload: BrowserSessionForwardRequest,
    ) -> BrowserSessionActionInternalResponse:
        async with httpx.AsyncClient(base_url=self.browser_url, timeout=25.0) as client:
            response = await client.post(
                f"/internal/sessions/{session_id}/forward",
                json=payload.model_dump(),
            )
            response.raise_for_status()
        return BrowserSessionActionInternalResponse.model_validate(response.json())

    async def browser_session_new_tab(
        self,
        session_id: str,
        payload: BrowserSessionNewTabRequest,
    ) -> BrowserSessionActionInternalResponse:
        async with httpx.AsyncClient(base_url=self.browser_url, timeout=25.0) as client:
            response = await client.post(
                f"/internal/sessions/{session_id}/tabs/new",
                json=payload.model_dump(),
            )
            response.raise_for_status()
        return BrowserSessionActionInternalResponse.model_validate(response.json())

    async def browser_session_switch_tab(
        self,
        session_id: str,
        payload: BrowserSessionSwitchTabRequest,
    ) -> BrowserSessionActionInternalResponse:
        async with httpx.AsyncClient(base_url=self.browser_url, timeout=25.0) as client:
            response = await client.post(
                f"/internal/sessions/{session_id}/tabs/switch",
                json=payload.model_dump(),
            )
            response.raise_for_status()
        return BrowserSessionActionInternalResponse.model_validate(response.json())

    async def browser_session_close_tab(
        self,
        session_id: str,
        payload: BrowserSessionCloseTabRequest,
    ) -> BrowserSessionActionInternalResponse:
        async with httpx.AsyncClient(base_url=self.browser_url, timeout=25.0) as client:
            response = await client.post(
                f"/internal/sessions/{session_id}/tabs/close",
                json=payload.model_dump(),
            )
            response.raise_for_status()
        return BrowserSessionActionInternalResponse.model_validate(response.json())

    async def browser_prepare_submit(
        self,
        session_id: str,
        payload: BrowserSubmitProposalRequest,
    ) -> BrowserSubmitPreviewInternalResponse:
        async with httpx.AsyncClient(base_url=self.browser_url, timeout=25.0) as client:
            response = await client.post(
                f"/internal/sessions/{session_id}/prepare-submit",
                json=payload.model_dump(),
            )
            response.raise_for_status()
        return BrowserSubmitPreviewInternalResponse.model_validate(response.json())

    async def browser_execute_submit(
        self,
        session_id: str,
        payload: BrowserSubmitExecuteRequest,
    ) -> BrowserSubmitExecuteInternalResponse:
        async with httpx.AsyncClient(base_url=self.browser_url, timeout=25.0) as client:
            response = await client.post(
                f"/internal/sessions/{session_id}/execute-submit",
                json=payload.model_dump(),
            )
            response.raise_for_status()
        return BrowserSubmitExecuteInternalResponse.model_validate(response.json())

    async def browser_execute_http_request(
        self,
        session_id: str,
        payload: BrowserHttpRequestExecuteRequest,
    ) -> BrowserHttpRequestExecuteInternalResponse:
        async with httpx.AsyncClient(base_url=self.browser_url, timeout=25.0) as client:
            response = await client.post(
                f"/internal/sessions/{session_id}/execute-http-request",
                json=payload.model_dump(),
            )
            response.raise_for_status()
        return BrowserHttpRequestExecuteInternalResponse.model_validate(response.json())

    async def egress_fetch(self, payload: EgressFetchRequest) -> EgressFetchResponse:
        """Call egress /internal/fetch directly (used for consequential actions)."""
        async with httpx.AsyncClient(base_url=self.egress_url, timeout=15.0) as client:
            response = await client.post("/internal/fetch", json=payload.model_dump())
            response.raise_for_status()
        return EgressFetchResponse.model_validate(response.json())

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
