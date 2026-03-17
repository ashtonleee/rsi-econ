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
    BrowserRenderRequest,
    BrowserRenderResponse,
    BridgeStatusReport,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    HealthReport,
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
