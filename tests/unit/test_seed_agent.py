from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from urllib import error as urllib_error


ROOT = Path(__file__).resolve().parents[2]
SEED_AGENT_PATH = ROOT / "sandbox" / "seed" / "main.py"


def load_seed_agent(tmp_path: Path):
    os.environ["RSI_AGENT_WORKSPACE"] = str(tmp_path)
    os.environ["LITELLM_URL"] = "http://litellm:4000"
    os.environ["WALLET_URL"] = "http://bridge:8081"
    os.environ["RSI_MODEL"] = "default"
    os.environ["RSI_MAX_TURNS"] = "5"
    spec = importlib.util.spec_from_file_location(f"test_seed_agent_{tmp_path.name}", SEED_AGENT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeHTTPResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):  # noqa: ANN001
        return False


FAKE_WALLET = {"remaining_usd": 10, "spent_usd": 0, "budget_usd": 10, "total_requests": 0, "avg_cost_per_request": 0}


def write_agent_workspace(tmp_path: Path) -> None:
    (tmp_path / "SYSTEM.md").write_text("system prompt\n", encoding="utf-8")


def test_agent_reads_system_prompt_as_system_message(tmp_path: Path, monkeypatch) -> None:
    write_agent_workspace(tmp_path)
    module = load_seed_agent(tmp_path)
    calls: list[list[dict[str, object]]] = []

    def fake_chat(messages, model=None, tools=None):  # noqa: ANN001
        calls.append(messages)
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "finish-1",
                                "type": "function",
                                "function": {"name": "finish", "arguments": '{"reason":"done"}'},
                            }
                        ],
                    }
                }
            ]
        }

    monkeypatch.setattr(module, "chat", fake_chat)
    monkeypatch.setattr(module, "get_wallet", lambda: FAKE_WALLET)

    assert module.main() == 0
    assert calls[0][0]["role"] == "system"
    assert calls[0][0]["content"].startswith("system prompt\n")


def test_chat_sends_correct_tool_definitions(tmp_path: Path, monkeypatch) -> None:
    write_agent_workspace(tmp_path)
    module = load_seed_agent(tmp_path)
    captured: dict[str, object] = {}

    def fake_urlopen(request, timeout=0):  # noqa: ANN001
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeHTTPResponse({"choices": [{"message": {"role": "assistant", "content": "ok"}}]})

    monkeypatch.setattr(module.urllib_request, "urlopen", fake_urlopen)

    module.chat([{"role": "system", "content": "hi"}], tools=module.TOOLS)

    assert captured["url"] == "http://litellm:4000/v1/chat/completions"
    assert captured["body"]["tools"] == module.TOOLS
    assert captured["body"]["tool_choice"] == "auto"


def test_agent_parses_tool_calls_and_executes_shell(tmp_path: Path, monkeypatch) -> None:
    write_agent_workspace(tmp_path)
    module = load_seed_agent(tmp_path)
    responses = iter(
        [
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "running shell",
                            "tool_calls": [
                                {
                                    "id": "tool-1",
                                    "type": "function",
                                    "function": {"name": "shell", "arguments": '{"command":"printf hello"}'},
                                }
                            ],
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": "tool-2",
                                    "type": "function",
                                    "function": {"name": "finish", "arguments": '{"reason":"done"}'},
                                }
                            ],
                        }
                    }
                ]
            },
        ]
    )
    recorded_messages: list[list[dict[str, object]]] = []

    def fake_chat(messages, model=None, tools=None):  # noqa: ANN001
        recorded_messages.append(messages.copy())
        return next(responses)

    monkeypatch.setattr(module, "chat", fake_chat)
    monkeypatch.setattr(module, "get_wallet", lambda: FAKE_WALLET)

    assert module.main() == 0
    # Shell output now includes metadata prefix; check that "hello" appears in the tool result
    assert any(message.get("role") == "tool" and "hello" in message.get("content", "") for message in recorded_messages[-1])


def test_execute_tool_handles_read_file_and_write_file(tmp_path: Path) -> None:
    write_agent_workspace(tmp_path)
    module = load_seed_agent(tmp_path)
    target = tmp_path / "notes.txt"

    result = module.execute_tool("write_file", {"path": str(target), "content": "hello"})
    assert "OK: wrote" in result
    assert target.read_text(encoding="utf-8") == "hello"
    # read_file now returns line-numbered output
    read_result = module.execute_tool("read_file", {"path": str(target)})
    assert "hello" in read_result


def test_request_restart_creates_marker_and_exits_cleanly(tmp_path: Path, monkeypatch) -> None:
    write_agent_workspace(tmp_path)
    module = load_seed_agent(tmp_path)

    monkeypatch.setattr(
        module,
        "chat",
        lambda messages, model=None, tools=None: {  # noqa: ARG005
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "restart-1",
                                "type": "function",
                                "function": {"name": "request_restart", "arguments": "{}"},
                            }
                        ],
                    }
                }
            ]
        },
    )
    monkeypatch.setattr(module, "get_wallet", lambda: FAKE_WALLET)

    assert module.main() == 0
    assert (tmp_path / ".restart_requested").exists()


def test_finish_tool_exits_cleanly(tmp_path: Path, monkeypatch) -> None:
    write_agent_workspace(tmp_path)
    module = load_seed_agent(tmp_path)

    monkeypatch.setattr(
        module,
        "chat",
        lambda messages, model=None, tools=None: {  # noqa: ARG005
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "finish-1",
                                "type": "function",
                                "function": {"name": "finish", "arguments": '{"reason":"done"}'},
                            }
                        ],
                    }
                }
            ]
        },
    )
    monkeypatch.setattr(module, "get_wallet", lambda: FAKE_WALLET)

    assert module.main() == 0


def test_http_429_causes_clean_exit(tmp_path: Path, monkeypatch) -> None:
    write_agent_workspace(tmp_path)
    module = load_seed_agent(tmp_path)

    def raise_429(messages, model=None, tools=None):  # noqa: ANN001, ARG001
        raise urllib_error.HTTPError(module.LITELLM_URL, 429, "too many requests", {}, None)

    monkeypatch.setattr(module, "chat", raise_429)
    monkeypatch.setattr(module, "get_wallet", lambda: FAKE_WALLET)

    assert module.main() == 0


def test_low_budget_exits_immediately(tmp_path: Path, monkeypatch) -> None:
    write_agent_workspace(tmp_path)
    module = load_seed_agent(tmp_path)
    monkeypatch.setattr(module, "get_wallet", lambda: {"remaining_usd": 0.10, "spent_usd": 4.90, "budget_usd": 5.00})
    assert module.main() == 0


def test_tool_definitions_include_all_10_tools(tmp_path: Path) -> None:
    module = load_seed_agent(tmp_path)
    tool_names = {t["function"]["name"] for t in module.TOOLS}
    expected = {"shell", "read_file", "write_file", "edit_file", "grep",
                "request_restart", "finish", "web_search", "browse_url", "screenshot"}
    assert tool_names == expected
