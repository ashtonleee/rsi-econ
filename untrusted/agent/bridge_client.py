import argparse
import asyncio
import json
import os

import httpx

from shared.config import DEFAULT_AGENT_TOKEN, DEFAULT_BRIDGE_URL
from shared.schemas import (
    AgentRunEventReceipt,
    AgentRunEventRequest,
    BrowserFollowHrefRequest,
    BrowserFollowHrefResponse,
    BrowserSessionActionResponse,
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
    BrowserSessionSnapshotResponse,
    BrowserSessionSwitchTabRequest,
    BrowserSessionTypeRequest,
    BrowserSessionWaitForRequest,
    BrowserSubmitProposalRequest,
    BrowserRenderRequest,
    BrowserRenderResponse,
    BridgeStatusReport,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    HealthReport,
    ProposalCreateRequest,
    ProposalRecord,
    WebFetchRequest,
    WebFetchResponse,
)


class BridgeClient:
    def __init__(self, bridge_url: str, *, agent_token: str = ""):
        self.bridge_url = bridge_url.rstrip("/")
        self.headers: dict[str, str] = {}
        if agent_token:
            self.headers["Authorization"] = f"Bearer {agent_token}"

    async def health(self) -> HealthReport:
        async with httpx.AsyncClient(base_url=self.bridge_url, timeout=5.0) as client:
            response = await client.get("/healthz", headers=self.headers)
            response.raise_for_status()
        return HealthReport.model_validate(response.json())

    async def status(self) -> BridgeStatusReport:
        async with httpx.AsyncClient(base_url=self.bridge_url, timeout=5.0) as client:
            response = await client.get("/status", headers=self.headers)
            response.raise_for_status()
        return BridgeStatusReport.model_validate(response.json())

    async def chat(self, *, model: str, message: str) -> ChatCompletionResponse:
        payload = ChatCompletionRequest(
            model=model,
            messages=[ChatMessage(role="user", content=message)],
        )
        async with httpx.AsyncClient(base_url=self.bridge_url, timeout=10.0) as client:
            response = await client.post(
                "/llm/chat/completions",
                json=payload.model_dump(),
                headers=self.headers,
            )
            response.raise_for_status()
        return ChatCompletionResponse.model_validate(response.json())

    async def fetch(self, *, url: str) -> WebFetchResponse:
        payload = WebFetchRequest(url=url)
        async with httpx.AsyncClient(base_url=self.bridge_url, timeout=10.0) as client:
            response = await client.post(
                "/web/fetch",
                json=payload.model_dump(),
                headers=self.headers,
            )
            response.raise_for_status()
        return WebFetchResponse.model_validate(response.json())

    async def browser_render(self, *, url: str) -> BrowserRenderResponse:
        payload = BrowserRenderRequest(url=url)
        async with httpx.AsyncClient(base_url=self.bridge_url, timeout=20.0) as client:
            response = await client.post(
                "/web/browser/render",
                json=payload.model_dump(),
                headers=self.headers,
            )
            response.raise_for_status()
        return BrowserRenderResponse.model_validate(response.json())

    async def browser_follow_href(
        self,
        *,
        source_url: str,
        target_url: str,
    ) -> BrowserFollowHrefResponse:
        payload = BrowserFollowHrefRequest(source_url=source_url, target_url=target_url)
        async with httpx.AsyncClient(base_url=self.bridge_url, timeout=25.0) as client:
            response = await client.post(
                "/web/browser/follow-href",
                json=payload.model_dump(),
                headers=self.headers,
            )
            response.raise_for_status()
        return BrowserFollowHrefResponse.model_validate(response.json())

    async def browser_session_open(
        self,
        *,
        url: str,
        capability_profile: str = "bounded_packet",
    ) -> BrowserSessionSnapshotResponse:
        payload = BrowserSessionOpenRequest(url=url, capability_profile=capability_profile)
        async with httpx.AsyncClient(base_url=self.bridge_url, timeout=25.0) as client:
            response = await client.post(
                "/web/browser/sessions/open",
                json=payload.model_dump(),
                headers=self.headers,
            )
            response.raise_for_status()
        return BrowserSessionSnapshotResponse.model_validate(response.json())

    async def browser_session_snapshot(self, *, session_id: str) -> BrowserSessionSnapshotResponse:
        async with httpx.AsyncClient(base_url=self.bridge_url, timeout=25.0) as client:
            response = await client.get(
                f"/web/browser/sessions/{session_id}",
                headers=self.headers,
            )
            response.raise_for_status()
        return BrowserSessionSnapshotResponse.model_validate(response.json())

    async def browser_session_click(
        self,
        *,
        session_id: str,
        snapshot_id: str,
        element_id: str,
    ) -> BrowserSessionSnapshotResponse:
        payload = BrowserSessionClickRequest(snapshot_id=snapshot_id, element_id=element_id)
        async with httpx.AsyncClient(base_url=self.bridge_url, timeout=25.0) as client:
            response = await client.post(
                f"/web/browser/sessions/{session_id}/click",
                json=payload.model_dump(),
                headers=self.headers,
            )
            response.raise_for_status()
        return BrowserSessionSnapshotResponse.model_validate(response.json())

    async def browser_session_type(
        self,
        *,
        session_id: str,
        snapshot_id: str,
        element_id: str,
        text: str,
    ) -> BrowserSessionSnapshotResponse:
        payload = BrowserSessionTypeRequest(
            snapshot_id=snapshot_id,
            element_id=element_id,
            text=text,
        )
        async with httpx.AsyncClient(base_url=self.bridge_url, timeout=25.0) as client:
            response = await client.post(
                f"/web/browser/sessions/{session_id}/type",
                json=payload.model_dump(),
                headers=self.headers,
            )
            response.raise_for_status()
        return BrowserSessionSnapshotResponse.model_validate(response.json())

    async def browser_session_select(
        self,
        *,
        session_id: str,
        snapshot_id: str,
        element_id: str,
        value: str,
    ) -> BrowserSessionSnapshotResponse:
        payload = BrowserSessionSelectRequest(
            snapshot_id=snapshot_id,
            element_id=element_id,
            value=value,
        )
        async with httpx.AsyncClient(base_url=self.bridge_url, timeout=25.0) as client:
            response = await client.post(
                f"/web/browser/sessions/{session_id}/select",
                json=payload.model_dump(),
                headers=self.headers,
            )
            response.raise_for_status()
        return BrowserSessionSnapshotResponse.model_validate(response.json())

    async def browser_session_set_checked(
        self,
        *,
        session_id: str,
        snapshot_id: str,
        element_id: str,
        checked: bool,
    ) -> BrowserSessionSnapshotResponse:
        payload = BrowserSessionSetCheckedRequest(
            snapshot_id=snapshot_id,
            element_id=element_id,
            checked=checked,
        )
        async with httpx.AsyncClient(base_url=self.bridge_url, timeout=25.0) as client:
            response = await client.post(
                f"/web/browser/sessions/{session_id}/set_checked",
                json=payload.model_dump(),
                headers=self.headers,
            )
            response.raise_for_status()
        return BrowserSessionSnapshotResponse.model_validate(response.json())

    async def browser_session_navigate(
        self,
        *,
        session_id: str,
        snapshot_id: str,
        url: str,
    ) -> BrowserSessionActionResponse:
        payload = BrowserSessionNavigateRequest(snapshot_id=snapshot_id, url=url)
        async with httpx.AsyncClient(base_url=self.bridge_url, timeout=25.0) as client:
            response = await client.post(
                f"/web/browser/sessions/{session_id}/navigate",
                json=payload.model_dump(),
                headers=self.headers,
            )
            response.raise_for_status()
        return BrowserSessionActionResponse.model_validate(response.json())

    async def browser_session_click_action(
        self,
        *,
        session_id: str,
        snapshot_id: str,
        element_id: str,
    ) -> BrowserSessionActionResponse:
        payload = BrowserSessionClickRequest(snapshot_id=snapshot_id, element_id=element_id)
        async with httpx.AsyncClient(base_url=self.bridge_url, timeout=25.0) as client:
            response = await client.post(
                f"/web/browser/sessions/{session_id}/actions/click",
                json=payload.model_dump(),
                headers=self.headers,
            )
            response.raise_for_status()
        return BrowserSessionActionResponse.model_validate(response.json())

    async def browser_session_fill(
        self,
        *,
        session_id: str,
        snapshot_id: str,
        element_id: str,
        text: str,
    ) -> BrowserSessionActionResponse:
        payload = BrowserSessionFillRequest(snapshot_id=snapshot_id, element_id=element_id, text=text)
        async with httpx.AsyncClient(base_url=self.bridge_url, timeout=25.0) as client:
            response = await client.post(
                f"/web/browser/sessions/{session_id}/fill",
                json=payload.model_dump(),
                headers=self.headers,
            )
            response.raise_for_status()
        return BrowserSessionActionResponse.model_validate(response.json())

    async def browser_session_select_action(
        self,
        *,
        session_id: str,
        snapshot_id: str,
        element_id: str,
        value: str,
    ) -> BrowserSessionActionResponse:
        payload = BrowserSessionSelectRequest(snapshot_id=snapshot_id, element_id=element_id, value=value)
        async with httpx.AsyncClient(base_url=self.bridge_url, timeout=25.0) as client:
            response = await client.post(
                f"/web/browser/sessions/{session_id}/actions/select",
                json=payload.model_dump(),
                headers=self.headers,
            )
            response.raise_for_status()
        return BrowserSessionActionResponse.model_validate(response.json())

    async def browser_session_set_checked_action(
        self,
        *,
        session_id: str,
        snapshot_id: str,
        element_id: str,
        checked: bool,
    ) -> BrowserSessionActionResponse:
        payload = BrowserSessionSetCheckedRequest(snapshot_id=snapshot_id, element_id=element_id, checked=checked)
        async with httpx.AsyncClient(base_url=self.bridge_url, timeout=25.0) as client:
            response = await client.post(
                f"/web/browser/sessions/{session_id}/actions/set_checked",
                json=payload.model_dump(),
                headers=self.headers,
            )
            response.raise_for_status()
        return BrowserSessionActionResponse.model_validate(response.json())

    async def browser_session_press(
        self,
        *,
        session_id: str,
        snapshot_id: str,
        key: str,
        element_id: str = "",
    ) -> BrowserSessionActionResponse:
        payload = BrowserSessionPressRequest(snapshot_id=snapshot_id, key=key, element_id=element_id)
        async with httpx.AsyncClient(base_url=self.bridge_url, timeout=25.0) as client:
            response = await client.post(
                f"/web/browser/sessions/{session_id}/press",
                json=payload.model_dump(),
                headers=self.headers,
            )
            response.raise_for_status()
        return BrowserSessionActionResponse.model_validate(response.json())

    async def browser_session_hover(
        self,
        *,
        session_id: str,
        snapshot_id: str,
        element_id: str,
    ) -> BrowserSessionActionResponse:
        payload = BrowserSessionHoverRequest(snapshot_id=snapshot_id, element_id=element_id)
        async with httpx.AsyncClient(base_url=self.bridge_url, timeout=25.0) as client:
            response = await client.post(
                f"/web/browser/sessions/{session_id}/hover",
                json=payload.model_dump(),
                headers=self.headers,
            )
            response.raise_for_status()
        return BrowserSessionActionResponse.model_validate(response.json())

    async def browser_session_wait_for(
        self,
        *,
        session_id: str,
        snapshot_id: str = "",
        text: str = "",
        time_seconds: float = 0.0,
    ) -> BrowserSessionActionResponse:
        payload = BrowserSessionWaitForRequest(snapshot_id=snapshot_id, text=text, time_seconds=time_seconds)
        async with httpx.AsyncClient(base_url=self.bridge_url, timeout=25.0) as client:
            response = await client.post(
                f"/web/browser/sessions/{session_id}/wait_for",
                json=payload.model_dump(),
                headers=self.headers,
            )
            response.raise_for_status()
        return BrowserSessionActionResponse.model_validate(response.json())

    async def browser_session_back(
        self,
        *,
        session_id: str,
        snapshot_id: str = "",
    ) -> BrowserSessionActionResponse:
        payload = BrowserSessionBackRequest(snapshot_id=snapshot_id)
        async with httpx.AsyncClient(base_url=self.bridge_url, timeout=25.0) as client:
            response = await client.post(
                f"/web/browser/sessions/{session_id}/back",
                json=payload.model_dump(),
                headers=self.headers,
            )
            response.raise_for_status()
        return BrowserSessionActionResponse.model_validate(response.json())

    async def browser_session_forward(
        self,
        *,
        session_id: str,
        snapshot_id: str = "",
    ) -> BrowserSessionActionResponse:
        payload = BrowserSessionForwardRequest(snapshot_id=snapshot_id)
        async with httpx.AsyncClient(base_url=self.bridge_url, timeout=25.0) as client:
            response = await client.post(
                f"/web/browser/sessions/{session_id}/forward",
                json=payload.model_dump(),
                headers=self.headers,
            )
            response.raise_for_status()
        return BrowserSessionActionResponse.model_validate(response.json())

    async def browser_session_new_tab(
        self,
        *,
        session_id: str,
        url: str = "",
    ) -> BrowserSessionActionResponse:
        payload = BrowserSessionNewTabRequest(url=url)
        async with httpx.AsyncClient(base_url=self.bridge_url, timeout=25.0) as client:
            response = await client.post(
                f"/web/browser/sessions/{session_id}/tabs/new",
                json=payload.model_dump(),
                headers=self.headers,
            )
            response.raise_for_status()
        return BrowserSessionActionResponse.model_validate(response.json())

    async def browser_session_switch_tab(
        self,
        *,
        session_id: str,
        snapshot_id: str,
        tab_id: str,
    ) -> BrowserSessionActionResponse:
        payload = BrowserSessionSwitchTabRequest(snapshot_id=snapshot_id, tab_id=tab_id)
        async with httpx.AsyncClient(base_url=self.bridge_url, timeout=25.0) as client:
            response = await client.post(
                f"/web/browser/sessions/{session_id}/tabs/switch",
                json=payload.model_dump(),
                headers=self.headers,
            )
            response.raise_for_status()
        return BrowserSessionActionResponse.model_validate(response.json())

    async def browser_session_close_tab(
        self,
        *,
        session_id: str,
        snapshot_id: str = "",
        tab_id: str = "",
    ) -> BrowserSessionActionResponse:
        payload = BrowserSessionCloseTabRequest(snapshot_id=snapshot_id, tab_id=tab_id)
        async with httpx.AsyncClient(base_url=self.bridge_url, timeout=25.0) as client:
            response = await client.post(
                f"/web/browser/sessions/{session_id}/tabs/close",
                json=payload.model_dump(),
                headers=self.headers,
            )
            response.raise_for_status()
        return BrowserSessionActionResponse.model_validate(response.json())

    async def browser_submit_proposal(
        self,
        *,
        session_id: str,
        snapshot_id: str,
        element_id: str,
    ) -> ProposalRecord:
        payload = BrowserSubmitProposalRequest(
            snapshot_id=snapshot_id,
            element_id=element_id,
        )
        async with httpx.AsyncClient(base_url=self.bridge_url, timeout=25.0) as client:
            response = await client.post(
                f"/web/browser/sessions/{session_id}/submit_proposal",
                json=payload.model_dump(),
                headers=self.headers,
            )
            response.raise_for_status()
        return ProposalRecord.model_validate(response.json())

    async def report_agent_event(
        self,
        *,
        run_id: str,
        event_kind: str,
        step_index: int | None,
        tool_name: str | None,
        summary: dict,
    ) -> AgentRunEventReceipt:
        payload = AgentRunEventRequest(
            run_id=run_id,
            event_kind=event_kind,
            step_index=step_index,
            tool_name=tool_name,
            summary=summary,
        )
        async with httpx.AsyncClient(base_url=self.bridge_url, timeout=5.0) as client:
            response = await client.post(
                "/agent/runs/events",
                json=payload.model_dump(),
                headers=self.headers,
            )
            response.raise_for_status()
        return AgentRunEventReceipt.model_validate(response.json())

    async def create_proposal(
        self,
        *,
        action_type: str,
        action_payload: dict,
    ) -> ProposalRecord:
        payload = ProposalCreateRequest(
            action_type=action_type,
            action_payload=action_payload,
        )
        async with httpx.AsyncClient(base_url=self.bridge_url, timeout=10.0) as client:
            response = await client.post(
                "/proposals",
                json=payload.model_dump(),
                headers=self.headers,
            )
            response.raise_for_status()
        return ProposalRecord.model_validate(response.json())


async def probe_bridge(bridge_url: str, *, agent_token: str = "") -> dict:
    bridge = await BridgeClient(bridge_url, agent_token=agent_token).health()
    return {
        "bridge_url": bridge_url,
        "reachable": True,
        "bridge": bridge.model_dump(),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bridge-url", default=DEFAULT_BRIDGE_URL)
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("health")
    subparsers.add_parser("status")

    chat_parser = subparsers.add_parser("chat")
    chat_parser.add_argument("--model", default="stage1-deterministic")
    chat_parser.add_argument("--message", required=True)

    fetch_parser = subparsers.add_parser("fetch")
    fetch_parser.add_argument("--url", required=True)

    browser_parser = subparsers.add_parser("browser-render")
    browser_parser.add_argument("--url", required=True)

    browser_follow_parser = subparsers.add_parser("browser-follow-href")
    browser_follow_parser.add_argument("--source-url", required=True)
    browser_follow_parser.add_argument("--target-url", required=True)

    args = parser.parse_args()
    token = os.environ.get("RSI_AGENT_TOKEN", DEFAULT_AGENT_TOKEN)
    client = BridgeClient(args.bridge_url, agent_token=token)
    if args.command in (None, "health"):
        result = asyncio.run(probe_bridge(args.bridge_url, agent_token=token))
    elif args.command == "status":
        result = asyncio.run(client.status()).model_dump()
    elif args.command == "chat":
        result = asyncio.run(
            client.chat(
                model=args.model,
                message=args.message,
            )
        ).model_dump()
    elif args.command == "fetch":
        result = asyncio.run(
            client.fetch(url=args.url)
        ).model_dump()
    elif args.command == "browser-render":
        result = asyncio.run(
            client.browser_render(url=args.url)
        ).model_dump()
    elif args.command == "browser-follow-href":
        result = asyncio.run(
            client.browser_follow_href(
                source_url=args.source_url,
                target_url=args.target_url,
            )
        ).model_dump()
    else:
        raise ValueError(f"unsupported command: {args.command}")
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
