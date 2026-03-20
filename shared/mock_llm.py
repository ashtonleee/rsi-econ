import json

from shared.schemas import ChatMessage, ChatUsage


def count_tokens(messages: list[ChatMessage]) -> int:
    return sum(len(message.content.split()) for message in messages)


def deterministic_reply(messages: list[ChatMessage]) -> str:
    for message in reversed(messages):
        if message.role == "user":
            try:
                prompt = json.loads(message.content)
            except (TypeError, json.JSONDecodeError):
                prompt = None
            if isinstance(prompt, dict) and "allowed_tools" in prompt:
                return json.dumps(
                    {
                        "tool": "bridge_status",
                        "reason": "deterministic mock session action",
                        "params": {},
                    },
                    sort_keys=True,
                )
            return f"stage1 deterministic reply: {message.content}"
    return "stage1 deterministic reply: no user message provided"


def deterministic_usage(messages: list[ChatMessage]) -> ChatUsage:
    assistant_message = ChatMessage(
        role="assistant",
        content=deterministic_reply(messages),
    )
    prompt_tokens = count_tokens(messages)
    completion_tokens = count_tokens([assistant_message])
    return ChatUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
    )


MINIMUM_DETERMINISTIC_CALL_TOKENS = deterministic_usage(
    [ChatMessage(role="user", content="x")]
).total_tokens
