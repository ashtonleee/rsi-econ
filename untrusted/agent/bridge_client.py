import argparse
import asyncio
import json

import httpx

from shared.config import DEFAULT_BRIDGE_URL
from shared.schemas import (
    AgentRunEventReceipt,
    AgentRunEventRequest,
    BridgeStatusReport,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    HealthReport,
)


class BridgeClient:
    def __init__(self, bridge_url: str):
        self.bridge_url = bridge_url.rstrip("/")
        self.headers = {"x-rsi-actor": "agent"}

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


async def probe_bridge(bridge_url: str) -> dict:
    bridge = await BridgeClient(bridge_url).health()
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

    args = parser.parse_args()
    if args.command in (None, "health"):
        result = asyncio.run(probe_bridge(args.bridge_url))
    elif args.command == "status":
        result = asyncio.run(BridgeClient(args.bridge_url).status()).model_dump()
    elif args.command == "chat":
        result = asyncio.run(
            BridgeClient(args.bridge_url).chat(
                model=args.model,
                message=args.message,
            )
        ).model_dump()
    else:
        raise ValueError(f"unsupported command: {args.command}")
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
