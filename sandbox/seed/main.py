#!/usr/bin/env python3
"""RSI-Econ Agent v2 — budget-aware, self-improving agent with persistent knowledge."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib import error as urllib_error
from urllib import request as urllib_request

WORKSPACE = Path(os.getenv("RSI_AGENT_WORKSPACE", "/workspace/agent"))
SYSTEM_PROMPT_PATH = WORKSPACE / "SYSTEM.md"
HISTORY_PATH = WORKSPACE / "history.jsonl"
KNOWLEDGE_PATH = WORKSPACE / "knowledge.json"
OPERATOR_MESSAGES_PATH = Path(os.getenv("RSI_OPERATOR_MESSAGES", "/workspace/operator_messages/pending.jsonl"))
LITELLM_URL = os.getenv("LITELLM_URL", "http://litellm:4000")
WALLET_URL = os.getenv("WALLET_URL", "http://bridge:8081")

# Use mini model by default for cost efficiency
MODEL = os.getenv("RSI_MODEL", "default")

MAX_TURNS = int(os.getenv("RSI_MAX_TURNS", "0"))
MAX_CONTEXT_MESSAGES = 40

# Browser tool singleton (lazy-initialized)
_browser_tool = None


def get_browser():
    """Get or create the singleton BrowserTool instance."""
    global _browser_tool
    if _browser_tool is None:
        from browser_tool import BrowserTool
        _browser_tool = BrowserTool()
    return _browser_tool


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "shell",
            "description": "Execute a bash command and return stdout+stderr.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The bash command to run"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read file contents. Optionally specify line range with offset (1-indexed) and limit.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path to the file"},
                    "offset": {"type": "integer", "description": "1-indexed start line (default: 1)"},
                    "limit": {"type": "integer", "description": "Max lines to return (default: 200)"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create a new file or overwrite entirely. Use edit_file for modifying existing files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path to the file"},
                    "content": {"type": "string", "description": "Content to write"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Make a surgical edit to a file. Specify the old text to find and the new text to replace it with. The old_text must match exactly (including whitespace/indentation). Use this instead of write_file when modifying existing files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path to the file"},
                    "old_text": {"type": "string", "description": "Exact text to find and replace"},
                    "new_text": {"type": "string", "description": "Replacement text"},
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search file contents using regex. Returns matching lines with file paths and line numbers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex pattern to search for"},
                    "path": {"type": "string", "description": "Directory or file to search (default: /workspace/agent/)"},
                    "include": {"type": "string", "description": "Glob pattern for file filtering, e.g. '*.py'"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "request_restart",
            "description": "Signal the supervisor to restart you with your modified code. Call this AFTER writing your code changes.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "Exit cleanly. Use when: budget is critically low or you've completed your objective.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "description": "Why you're stopping"},
                },
                "required": ["reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web using DuckDuckGo or Google.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query"},
                    "engine": {
                        "type": "string",
                        "enum": ["duckduckgo", "google"],
                        "description": "Search engine to use (default: duckduckgo)",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browse_url",
            "description": "Navigate to a URL and extract readable text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The URL to navigate to"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "screenshot",
            "description": "Take screenshot of current browser page. No parameters.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]


def truncate_output(text: str, max_chars: int = 10000) -> str:
    """Truncate long output preserving both prefix and suffix (like Codex CLI)."""
    if len(text) <= max_chars:
        return text
    keep = max_chars // 3
    dropped = len(text) - max_chars
    return f"{text[:keep]}\n\n... [truncated {dropped} chars] ...\n\n{text[-keep:]}"


def execute_tool(name: str, args: dict[str, object]) -> str:
    if name == "shell":
        command = str(args.get("command", ""))
        t0 = time.time()
        try:
            result = subprocess.run(
                ["bash", "-lc", command],
                cwd=str(WORKSPACE),
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return "ERROR: command timed out after 120s"
        elapsed = time.time() - t0
        raw = f"{result.stdout}{result.stderr}"
        total_chars = len(raw)
        output = truncate_output(raw)
        truncated = total_chars > len(output)
        meta = f"[exit_code={result.returncode}, duration={elapsed:.1f}s, {total_chars} chars"
        if truncated:
            meta += ", truncated"
        meta += "]\n"
        return meta + (output or "(no output)")

    if name == "read_file":
        try:
            path = Path(str(args.get("path", "")))
            raw = path.read_text(encoding="utf-8")
            lines = raw.split("\n")
            total_lines = len(lines)
            offset = max(1, int(args.get("offset", 1) or 1))
            limit = int(args.get("limit", 200) or 200)
            selected = lines[offset - 1 : offset - 1 + limit]
            numbered = []
            for i, line in enumerate(selected, start=offset):
                numbered.append(f"{i:>6}\t{line}")
            output = "\n".join(numbered)
            remaining = total_lines - (offset - 1 + len(selected))
            if remaining > 0:
                output += f"\n... ({remaining} more lines)"
            return truncate_output(output, max_chars=20000)
        except Exception as exc:
            return f"ERROR: {exc}"

    if name == "write_file":
        try:
            path = Path(str(args.get("path", "")))
            content = str(args.get("content", ""))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return f"OK: wrote {len(content)} bytes to {path}"
        except Exception as exc:
            return f"ERROR: {exc}"

    if name == "edit_file":
        try:
            path = Path(str(args.get("path", "")))
            old_text = str(args.get("old_text", ""))
            new_text = str(args.get("new_text", ""))
            if not path.exists():
                return f"ERROR: file not found: {path}"
            content = path.read_text(encoding="utf-8")
            count = content.count(old_text)
            if count == 0:
                return "ERROR: old_text not found in file. Make sure it matches exactly including whitespace."
            if count > 1:
                return f"ERROR: old_text found {count} times. Provide more context to make the match unique."
            path.write_text(content.replace(old_text, new_text, 1), encoding="utf-8")
            return f"OK: edited {path} ({len(new_text)} chars replaced)"
        except Exception as exc:
            return f"ERROR: {exc}"

    if name == "grep":
        pattern = str(args.get("pattern", ""))
        search_path = str(args.get("path", str(WORKSPACE)))
        include = args.get("include")
        cmd = ["grep", "-rn"]
        if include:
            cmd.extend([f"--include={include}"])
        cmd.extend([pattern, search_path])
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
        except subprocess.TimeoutExpired:
            return "ERROR: grep timed out after 30s"
        output = result.stdout or result.stderr or "(no matches)"
        return truncate_output(output, max_chars=5000)

    if name == "request_restart":
        (WORKSPACE / ".restart_requested").touch()
        return "OK: restart requested. Supervisor will commit your changes and restart you. Exiting now."

    if name == "finish":
        return "FINISH"

    if name == "web_search":
        query = str(args.get("query", ""))
        engine = str(args.get("engine", "duckduckgo"))
        result = get_browser().search(query, engine)
        return truncate_output(json.dumps(result))

    if name == "browse_url":
        url = str(args.get("url", ""))
        result = get_browser().browse(url)
        return truncate_output(json.dumps(result))

    if name == "screenshot":
        return get_browser().screenshot()

    return f"ERROR: unknown tool '{name}'"


def get_wallet() -> dict:
    """Check current budget status."""
    try:
        req = urllib_request.Request(f"{WALLET_URL}/wallet", method="GET")
        with urllib_request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return {"remaining_usd": 0, "spent_usd": 0, "budget_usd": 0}


def load_knowledge() -> dict:
    """Load persistent knowledge store."""
    if KNOWLEDGE_PATH.exists():
        try:
            return json.loads(KNOWLEDGE_PATH.read_text())
        except Exception:
            pass
    return {
        "version": 2,
        "restarts": 0,
        "findings": [],
        "providers_checked": [],
        "free_tiers_found": [],
        "proposals_submitted": [],
        "domains_accessible": [],
        "domains_blocked": [],
    }


def save_knowledge(knowledge: dict) -> None:
    """Persist knowledge across restarts."""
    KNOWLEDGE_PATH.write_text(json.dumps(knowledge, indent=2))



def chat(messages: list[dict], model: str = None, tools: list | None = None) -> dict:
    """Make an LLM API call."""
    use_model = model or MODEL
    payload: dict = {"model": use_model, "messages": messages}
    if tools is not None:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    body = json.dumps(payload).encode("utf-8")
    request = urllib_request.Request(
        f"{LITELLM_URL}/v1/chat/completions",
        data=body,
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib_request.urlopen(request, timeout=120) as response:
        return json.loads(response.read().decode("utf-8"))


def trim_messages(messages: list[dict], max_messages: int = MAX_CONTEXT_MESSAGES) -> list[dict]:
    """Trim conversation history to manage context size.

    Keeps the system prompt (first message) plus the most recent messages.
    Respects tool-call boundaries so we never orphan tool results or
    assistant messages that contain tool_calls.
    """
    if len(messages) <= max_messages:
        return messages
    system = messages[:1]
    tail = messages[-(max_messages - 1):]
    # Walk forward from the cut point: skip orphaned tool-result messages
    # whose assistant tool_calls were dropped, and skip assistant messages
    # whose tool results were dropped.
    start = 0
    while start < len(tail):
        msg = tail[start]
        if msg.get("role") == "tool":
            # orphaned tool result — its assistant was trimmed
            start += 1
            continue
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            # check that ALL tool results for this call are still present
            call_ids = {tc["id"] for tc in msg["tool_calls"]}
            remaining_tool_ids = {
                m.get("tool_call_id") for m in tail[start + 1:]
                if m.get("role") == "tool"
            }
            if not call_ids.issubset(remaining_tool_ids):
                start += 1
                continue
        break
    trimmed = system + tail[start:]
    dropped = len(messages) - len(trimmed)
    if dropped > 0:
        print(f"[agent] context truncated: {dropped} messages dropped", flush=True)
    return trimmed


COMPACTION_TURN_INTERVAL = 30
COMPACTION_TOKEN_THRESHOLD = 80000

COMPACTION_PROMPT = (
    "Summarize your key findings, current strategy, and next steps in under 300 words. "
    "Include: provider names, pricing, free tiers discovered, what worked, what failed, "
    "and what you should do next. This summary will be your only memory after context reset."
)


def estimate_tokens(messages: list[dict]) -> int:
    """Rough token estimate: ~4 chars per token."""
    return sum(len(json.dumps(m)) for m in messages) // 4


def compact_context(messages: list[dict], knowledge: dict) -> list[dict]:
    """Use LLM to summarize conversation before context reset."""
    # Build conversation snippet for summarization
    snippet = "\n".join(
        m.get("content", "")[:200] for m in messages[-20:] if m.get("content")
    )
    summary_messages = [
        {"role": "system", "content": "You are summarizing an AI agent's research session."},
        {"role": "user", "content": COMPACTION_PROMPT + "\n\nConversation to summarize:\n" + snippet},
    ]
    try:
        response = chat(summary_messages)
        summary = response.get("choices", [{}])[0].get("message", {}).get("content", "")
    except Exception:
        summary = "Context was compacted but summary generation failed."

    # Save summary to knowledge.json
    knowledge.setdefault("session_summaries", []).append({
        "turn": len(messages),
        "summary": summary[:1000],
    })
    save_knowledge(knowledge)

    # Write summary to a persistent file too
    summary_path = WORKSPACE / "last_compaction_summary.md"
    summary_path.write_text(f"# Last Compaction Summary\n\n{summary}\n")

    old_count = len(messages)
    new_messages = [
        messages[0],
        {"role": "user", "content": f"[CONTEXT COMPACTED] Your previous conversation was summarized to save context. Summary:\n\n{summary}\n\nContinue from where you left off."},
    ]
    print(f"[agent] context compacted: {old_count} messages → 2 (LLM summary)", flush=True)
    return new_messages


def append_history(entry: dict) -> None:
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry))
        handle.write("\n")


def build_system_prompt(knowledge: dict, wallet: dict) -> str:
    """Build a concise system prompt with current state."""
    base = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8") if SYSTEM_PROMPT_PATH.exists() else "You are an AI agent."

    all_findings = knowledge.get("findings", [])
    findings = "\n".join(f"- {f}" for f in all_findings[-20:])

    state = f"""

## Current State (auto-injected)
- Budget: ${wallet.get('remaining_usd', 0):.2f} remaining of ${wallet.get('budget_usd', 0):.2f}
- Spent: ${wallet.get('spent_usd', 0):.2f} across {wallet.get('total_requests', 0)} requests
- Avg cost/request: ${wallet.get('avg_cost_per_request', 0):.4f}
- Restart count: {knowledge.get('restarts', 0)}
- Model: {MODEL}

## Key Findings
{findings or "None yet"}
"""
    return base + state


def check_operator_messages(messages: list[dict]) -> str | None:
    """Check for and consume operator messages. Returns model override if any."""
    if not OPERATOR_MESSAGES_PATH.exists():
        return None
    model_override = None
    try:
        lines = OPERATOR_MESSAGES_PATH.read_text(encoding="utf-8").strip().split("\n")
        for line in lines:
            if not line.strip():
                continue
            entry = json.loads(line)
            msg = entry.get("message", "")
            if msg:
                messages.append({"role": "user", "content": f"[OPERATOR] {msg}"})
                print(f"[agent] operator message: {msg[:100]}", flush=True)
            if entry.get("model_override"):
                model_override = entry["model_override"]
        OPERATOR_MESSAGES_PATH.unlink()
    except Exception as exc:
        print(f"[agent] error reading operator messages: {exc}", flush=True)
    return model_override


def load_recent_history() -> list[dict]:
    """Load recent history entries for continuity across restarts."""
    if not HISTORY_PATH.exists():
        return []
    recent = []
    try:
        lines = HISTORY_PATH.read_text(encoding="utf-8").strip().split("\n")
        for line in lines[-20:]:
            try:
                recent.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except Exception:
        pass
    return recent


def main() -> int:
    global _browser_tool

    knowledge = load_knowledge()
    knowledge["restarts"] = knowledge.get("restarts", 0) + 1
    save_knowledge(knowledge)

    wallet = get_wallet()

    remaining = wallet.get("remaining_usd", 0)
    prefix = f"[agent:{MODEL}]"
    print(f"{prefix} started  ${remaining:.2f} remaining, restart #{knowledge['restarts']}", flush=True)

    # Emergency mode: if budget is very low, just save state and exit
    if remaining < 0.50:
        print(f"{prefix} CRITICAL: budget very low, preserving state and exiting", flush=True)
        knowledge["findings"].append(f"Low budget exit at ${remaining:.2f}")
        save_knowledge(knowledge)
        return 0

    # Write restart marker to history
    append_history({"event": "restart", "restart": knowledge["restarts"]})

    system_prompt = build_system_prompt(knowledge, wallet)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "Begin working toward your objective. Use tools to take action."},
    ]

    # Load recent history for continuity across restarts
    recent = load_recent_history()
    if recent:
        summaries = []
        for entry in recent:
            if entry.get("role") == "assistant" and entry.get("content"):
                summaries.append(f"[Previous thinking] {entry['content'][:500]}")
            elif entry.get("role") == "tool":
                summaries.append(f"[Previous result] {entry.get('name', '?')}: {entry.get('result', '')[:300]}")
        if summaries:
            context = f"[RESUMING SESSION] You were restarted (restart #{knowledge.get('restarts', 0)}). Here's what you were doing:\n" + "\n".join(summaries[-10:])
            messages.append({"role": "user", "content": context})
            print(f"{prefix} loaded {len(summaries)} entries from previous session", flush=True)

    turn = 0
    while True:
        turn += 1
        if MAX_TURNS > 0 and turn > MAX_TURNS:
            print(f"{prefix} reached turn limit ({MAX_TURNS}), exiting", flush=True)
            break

        # Truncate context if needed
        messages = trim_messages(messages)

        # Periodic context compaction
        if turn % COMPACTION_TURN_INTERVAL == 0:
            tokens = estimate_tokens(messages)
            if tokens > COMPACTION_TOKEN_THRESHOLD:
                messages = compact_context(messages, knowledge)

        # Check for operator messages
        model_override = check_operator_messages(messages)

        try:
            response = chat(messages, model=model_override, tools=TOOLS)
        except Exception as exc:
            if "429" in str(exc):
                # Distinguish rate-limit from budget exhaustion
                error_body = ""
                try:
                    error_body = exc.read().decode("utf-8", errors="replace")
                except Exception:
                    pass
                is_rate_limit = any(
                    kw in error_body.lower()
                    for kw in ["rate limit", "rate_limit", "retry-after", "too many requests"]
                )
                if is_rate_limit:
                    retry_after = 10
                    if hasattr(exc, "headers"):
                        ra = exc.headers.get("retry-after", "")
                        if ra.isdigit():
                            retry_after = min(int(ra), 60)
                    print(f"{prefix} rate limited, waiting {retry_after}s", flush=True)
                    time.sleep(retry_after)
                    continue
                else:
                    print(f"{prefix} 429 — budget exhausted, exiting", flush=True)
                    break
            consecutive_errors = getattr(main, '_consecutive_errors', 0) + 1
            main._consecutive_errors = consecutive_errors
            print(f"{prefix} API error #{consecutive_errors}: {exc}", flush=True)
            if consecutive_errors >= 3:
                print(f"{prefix} 3 consecutive errors — resetting conversation", flush=True)
                messages = [
                    messages[0],
                    {"role": "user", "content": "Previous conversation was reset due to errors. Continue working toward your objective."},
                ]
                main._consecutive_errors = 0
            time.sleep(5)
            continue

        main._consecutive_errors = 0  # reset on success
        choice = response.get("choices", [{}])[0]
        raw_msg = choice.get("message", {})
        # Clean message: only keep role, content, tool_calls — strip provider-specific fields
        # MiniMax requires content to always be a string (never null/missing)
        msg = {"role": raw_msg.get("role", "assistant")}
        msg["content"] = raw_msg.get("content") or ""
        if raw_msg.get("tool_calls"):
            msg["tool_calls"] = raw_msg["tool_calls"]
        messages.append(msg)

        # Log assistant message to history
        append_history({"turn": turn, "role": "assistant", "content": msg.get("content"), "tool_calls": msg.get("tool_calls")})

        # Reset empty-response counter on any real response
        if msg.get("tool_calls") or msg.get("content"):
            main._empty_count = 0

        # Handle tool calls
        tool_calls = msg.get("tool_calls")
        if tool_calls:
            for tc in tool_calls:
                fn = tc.get("function", {})
                tool_name = fn.get("name", "")
                try:
                    tool_args = json.loads(fn.get("arguments", "{}"))
                except json.JSONDecodeError:
                    tool_args = {}

                print(f"{prefix} tool: {tool_name}", flush=True)
                result = execute_tool(tool_name, tool_args)

                # Log tool result to history
                append_history({"turn": turn, "role": "tool", "name": tool_name, "result": result[:500]})

                if result == "FINISH":
                    reason = tool_args.get("reason", "no reason given")
                    print(f"{prefix} finishing: {reason}", flush=True)
                    if _browser_tool is not None:
                        _browser_tool.close()
                        _browser_tool = None
                    return 0

                if tool_name == "request_restart":
                    if _browser_tool is not None:
                        _browser_tool.close()
                        _browser_tool = None
                    return 0

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": result,
                })
        elif msg.get("content"):
            print(f"{prefix} thinking (turn {turn})", flush=True)
        else:
            # No content and no tool calls — nudge the model to continue
            empty_count = getattr(main, '_empty_count', 0) + 1
            main._empty_count = empty_count
            if empty_count >= 3:
                print(f"{prefix} 3 consecutive empty responses, exiting", flush=True)
                break
            print(f"{prefix} empty response #{empty_count}, nudging", flush=True)
            messages.append({"role": "user", "content": "Continue working toward your objective. Use tools to take action."})
            continue

    # Clean up browser before exit
    if _browser_tool is not None:
        _browser_tool.close()
        _browser_tool = None

    return 0


if __name__ == "__main__":
    sys.exit(main())
