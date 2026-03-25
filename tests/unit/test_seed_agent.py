from __future__ import annotations

import importlib.util
import json
import os
import sys
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
    module_name = f"test_seed_agent_{tmp_path.name}"
    spec = importlib.util.spec_from_file_location(module_name, SEED_AGENT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[module_name] = module
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
                                "id": "restart-1",
                                "type": "function",
                                "function": {"name": "request_restart", "arguments": "{}"},
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
                                    "function": {"name": "request_restart", "arguments": "{}"},
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


def test_finish_tool_returns_error(tmp_path: Path) -> None:
    """finish tool is removed — it should return an error string, not stop the agent."""
    write_agent_workspace(tmp_path)
    module = load_seed_agent(tmp_path)
    result = module.execute_tool("finish", {"reason": "done"})
    assert "ERROR" in result
    assert "removed" in result


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


def test_tool_definitions_include_expected_tools(tmp_path: Path) -> None:
    module = load_seed_agent(tmp_path)
    tool_names = {t["function"]["name"] for t in module.TOOLS}
    # finish tool removed; 10 tools remain
    expected = {"shell", "read_file", "write_file", "edit_file", "grep",
                "request_restart", "web_search", "browse_url", "fetch_url", "screenshot"}
    assert tool_names == expected


# ── New tests for this changeset ────────────────────────────────────


def test_reasoning_log_written(tmp_path: Path, monkeypatch) -> None:
    """After assistant message with content, reasoning.jsonl gets an entry."""
    write_agent_workspace(tmp_path)
    module = load_seed_agent(tmp_path)
    call_count = 0

    def fake_chat(messages, model=None, tools=None):  # noqa: ANN001
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return {
                "choices": [{"message": {"role": "assistant", "content": "I should research providers."}}]
            }
        return {
            "choices": [{"message": {"role": "assistant", "tool_calls": [
                {"id": "r1", "type": "function", "function": {"name": "request_restart", "arguments": "{}"}}
            ]}}]
        }

    monkeypatch.setattr(module, "chat", fake_chat)
    monkeypatch.setattr(module, "get_wallet", lambda: FAKE_WALLET)

    assert module.main() == 0

    reasoning_path = tmp_path / "reasoning.jsonl"
    assert reasoning_path.exists()
    entries = [json.loads(line) for line in reasoning_path.read_text().strip().split("\n") if line.strip()]
    assert len(entries) >= 1
    assert "research providers" in entries[0]["content"]
    assert "timestamp" in entries[0]
    assert "turn" in entries[0]


def test_reasoning_logged_with_content(tmp_path: Path, monkeypatch) -> None:
    """Content-only response produces entry with content and correct flags."""
    write_agent_workspace(tmp_path)
    module = load_seed_agent(tmp_path)
    call_count = 0

    def fake_chat(messages, model=None, tools=None):  # noqa: ANN001
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return {"choices": [{"message": {"role": "assistant", "content": "Analyzing free tier options."}}]}
        return {"choices": [{"message": {"role": "assistant", "tool_calls": [
            {"id": "r1", "type": "function", "function": {"name": "request_restart", "arguments": "{}"}}
        ]}}]}

    monkeypatch.setattr(module, "chat", fake_chat)
    monkeypatch.setattr(module, "get_wallet", lambda: FAKE_WALLET)
    module.main()

    entries = [json.loads(line) for line in (tmp_path / "reasoning.jsonl").read_text().strip().split("\n") if line.strip()]
    content_entry = entries[0]
    assert content_entry["has_content"] is True
    assert content_entry["has_tool_calls"] is False
    assert "Analyzing free tier" in content_entry["content"]
    assert "tool_calls" not in content_entry


def test_reasoning_logged_without_content(tmp_path: Path, monkeypatch) -> None:
    """Tool-calls-only response (GPT-5.4 style) produces entry with tool_calls."""
    write_agent_workspace(tmp_path)
    module = load_seed_agent(tmp_path)
    call_count = 0

    def fake_chat(messages, model=None, tools=None):  # noqa: ANN001
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # GPT-5.4 style: tool_calls present, content is null
            return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
                {"id": "tc1", "type": "function", "function": {"name": "browser_navigate", "arguments": '{"url": "https://example.com"}'}}
            ]}}]}
        return {"choices": [{"message": {"role": "assistant", "tool_calls": [
            {"id": "r1", "type": "function", "function": {"name": "request_restart", "arguments": "{}"}}
        ]}}]}

    monkeypatch.setattr(module, "chat", fake_chat)
    monkeypatch.setattr(module, "get_wallet", lambda: FAKE_WALLET)
    # Patch execute_tool to handle browser_navigate without real browser
    original_execute = module.execute_tool

    def patched_execute(name, args_str):  # noqa: ANN001
        if name == "browser_navigate":
            return "Navigated to https://example.com"
        return original_execute(name, args_str)

    monkeypatch.setattr(module, "execute_tool", patched_execute)
    module.main()

    entries = [json.loads(line) for line in (tmp_path / "reasoning.jsonl").read_text().strip().split("\n") if line.strip()]
    # First entry is the tool-calls-only response
    tc_entry = entries[0]
    assert tc_entry["has_content"] is False
    assert tc_entry["has_tool_calls"] is True
    assert "content" not in tc_entry
    assert len(tc_entry["tool_calls"]) == 1
    assert tc_entry["tool_calls"][0]["name"] == "browser_navigate"
    assert "example.com" in tc_entry["tool_calls"][0]["args_preview"]


def test_reasoning_logged_with_reasoning_content(tmp_path: Path, monkeypatch) -> None:
    """Response with reasoning_content field (DeepSeek style) produces reasoning entry."""
    write_agent_workspace(tmp_path)
    module = load_seed_agent(tmp_path)
    call_count = 0

    def fake_chat(messages, model=None, tools=None):  # noqa: ANN001
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return {"choices": [{"message": {
                "role": "assistant",
                "content": "Let me search for providers.",
                "reasoning_content": "I need to think step by step about which providers offer free tiers.",
            }}]}
        return {"choices": [{"message": {"role": "assistant", "tool_calls": [
            {"id": "r1", "type": "function", "function": {"name": "request_restart", "arguments": "{}"}}
        ]}}]}

    monkeypatch.setattr(module, "chat", fake_chat)
    monkeypatch.setattr(module, "get_wallet", lambda: FAKE_WALLET)
    module.main()

    entries = [json.loads(line) for line in (tmp_path / "reasoning.jsonl").read_text().strip().split("\n") if line.strip()]
    reasoning_entry = entries[0]
    assert "reasoning" in reasoning_entry
    assert "step by step" in reasoning_entry["reasoning"]
    assert "content" in reasoning_entry
    assert "search for providers" in reasoning_entry["content"]


def test_reasoning_always_logged(tmp_path: Path, monkeypatch) -> None:
    """Every LLM response produces exactly one reasoning entry."""
    write_agent_workspace(tmp_path)
    module = load_seed_agent(tmp_path)
    call_count = 0

    def fake_chat(messages, model=None, tools=None):  # noqa: ANN001
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Content-only response
            return {"choices": [{"message": {"role": "assistant", "content": "Planning next step."}}]}
        if call_count == 2:
            # Tool-calls-only response (GPT-5.4 style)
            return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
                {"id": "tc1", "type": "function", "function": {"name": "request_restart", "arguments": "{}"}}
            ]}}]}
        return {"choices": [{"message": {"role": "assistant", "tool_calls": [
            {"id": "r1", "type": "function", "function": {"name": "request_restart", "arguments": "{}"}}
        ]}}]}

    monkeypatch.setattr(module, "chat", fake_chat)
    monkeypatch.setattr(module, "get_wallet", lambda: FAKE_WALLET)
    module.main()

    entries = [json.loads(line) for line in (tmp_path / "reasoning.jsonl").read_text().strip().split("\n") if line.strip()]
    # Both LLM calls should have produced reasoning entries
    assert len(entries) == 2
    assert entries[0]["has_content"] is True
    assert entries[1]["has_tool_calls"] is True
    assert entries[1]["has_content"] is False


def test_chat_accepts_model_param(tmp_path: Path, monkeypatch) -> None:
    """chat() forwards the model parameter to LiteLLM."""
    write_agent_workspace(tmp_path)
    module = load_seed_agent(tmp_path)
    captured: dict[str, object] = {}

    def fake_urlopen(request, timeout=0):  # noqa: ANN001
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeHTTPResponse({"choices": [{"message": {"role": "assistant", "content": "ok"}}]})

    monkeypatch.setattr(module.urllib_request, "urlopen", fake_urlopen)

    module.chat([{"role": "system", "content": "hi"}], model="gpt-4.1")
    assert captured["body"]["model"] == "gpt-4.1"


def test_no_message_count_limit(tmp_path: Path) -> None:
    """There should be no MAX_CONTEXT_MESSAGES constant."""
    write_agent_workspace(tmp_path)
    module = load_seed_agent(tmp_path)
    assert not hasattr(module, "MAX_CONTEXT_MESSAGES")
    assert not hasattr(module, "trim_messages")


def test_compaction_config_exists(tmp_path: Path) -> None:
    """Compaction config should have model-aware thresholds."""
    write_agent_workspace(tmp_path)
    module = load_seed_agent(tmp_path)
    cfg = module.COMPACTION_CONFIG
    assert cfg["context_window"] > 0
    assert 0 < cfg["stage1_trigger"] < cfg["stage2_trigger"] < cfg["emergency_trigger"] <= 1.0


def test_time_in_context_log(tmp_path: Path, monkeypatch, capsys) -> None:
    """Session time/elapsed appears in agent log output at turn 10."""
    write_agent_workspace(tmp_path)
    module = load_seed_agent(tmp_path)
    os.environ["RSI_MAX_TURNS"] = "11"
    # Reload to pick up MAX_TURNS=11
    module.MAX_TURNS = 11

    call_count = 0

    def fake_chat(messages, model=None, tools=None):  # noqa: ANN001
        nonlocal call_count
        call_count += 1
        return {"choices": [{"message": {"role": "assistant", "content": f"thinking turn {call_count}"}}]}

    monkeypatch.setattr(module, "chat", fake_chat)
    monkeypatch.setattr(module, "get_wallet", lambda: FAKE_WALLET)

    module.main()
    captured = capsys.readouterr()
    # At turn 10, the periodic log should include elapsed time and remaining budget
    assert "elapsed" in captured.out
    assert "remaining" in captured.out


def test_no_knowledge_json_references(tmp_path: Path) -> None:
    """No KNOWLEDGE_PATH, load_knowledge, or save_knowledge in module."""
    write_agent_workspace(tmp_path)
    module = load_seed_agent(tmp_path)
    assert not hasattr(module, "KNOWLEDGE_PATH")
    assert not hasattr(module, "load_knowledge")
    assert not hasattr(module, "save_knowledge")


def test_build_system_prompt_no_knowledge_param(tmp_path: Path) -> None:
    """build_system_prompt takes wallet only, no knowledge parameter."""
    write_agent_workspace(tmp_path)
    module = load_seed_agent(tmp_path)
    import inspect
    sig = inspect.signature(module.build_system_prompt)
    params = list(sig.parameters.keys())
    assert params == ["wallet"]
