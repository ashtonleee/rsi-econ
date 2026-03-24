#!/usr/bin/env python3
"""RSI-Econ Agent v2 — autonomous, self-improving agent."""

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
REASONING_PATH = WORKSPACE / "reasoning.jsonl"
CONVERSATION_PATH = WORKSPACE / "conversation.json"
OPERATOR_MESSAGES_PATH = Path(os.getenv("RSI_OPERATOR_MESSAGES", "/workspace/operator_messages/pending.jsonl"))
LITELLM_URL = os.getenv("LITELLM_URL", "http://litellm:4000")
WALLET_URL = os.getenv("WALLET_URL", "http://bridge:8081")

# Default model from env var (operator controls starting model)
MODEL = os.getenv("RSI_MODEL", "default")
# Low budget fallback model (disabled by default — agent stays on RSI_MODEL until budget exhaustion)
LOW_BUDGET_MODEL = os.getenv("RSI_LOW_BUDGET_MODEL", "minimax-m2.7")
LOW_BUDGET_THRESHOLD = float(os.getenv("RSI_LOW_BUDGET_THRESHOLD", "0"))

# Free provider configuration (activated when budget exhausted)
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_API_BASE = "https://api.groq.com/openai/v1"
CLOUDFLARE_API_KEY = os.getenv("CLOUDFLARE_API_KEY", "")
CLOUDFLARE_ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID", "")

MAX_TURNS = int(os.getenv("RSI_MAX_TURNS", "0"))

# Free tier session limits — prevent exhausting daily quotas across experiments
FREE_TIER_MAX_CALLS = int(os.getenv("RSI_FREE_TIER_MAX_CALLS", "0"))  # 0 = unlimited
_free_tier_calls = 0

# --- Compaction configuration (model-aware, 2-stage) ---
# context_window: set per-model. Update when switching models.
# stage1_trigger: mask old tool outputs (cheap, no LLM call)
# stage2_trigger: bookend + LLM summarize via bridge /compact
# emergency_trigger: full reset to 2 messages (last resort)
# mask_after_turns: tool outputs older than this get replaced with placeholders
# bookend_keep_first / bookend_keep_last: messages preserved around summary
COMPACTION_CONFIG = {
    "context_window": int(os.getenv("RSI_CONTEXT_WINDOW", "1000000")),
    "stage1_trigger": float(os.getenv("RSI_COMPACT_STAGE1", "0.60")),
    "stage2_trigger": float(os.getenv("RSI_COMPACT_STAGE2", "0.75")),
    "emergency_trigger": float(os.getenv("RSI_COMPACT_EMERGENCY", "0.90")),
    "mask_after_turns": int(os.getenv("RSI_COMPACT_MASK_TURNS", "5")),
    "bookend_keep_first": int(os.getenv("RSI_COMPACT_KEEP_FIRST", "3")),
    "bookend_keep_last": int(os.getenv("RSI_COMPACT_KEEP_LAST", "4")),
}

# Browser tool singleton (lazy-initialized)
_browser_tool = None

# State tracking for error handling and empty responses
_consecutive_errors = 0
_empty_response_count = 0


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
    # finish tool REMOVED — agent runs until budget exhausted or operator pauses
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
        # NOTE: Do NOT touch .restart_requested here. The main loop handles it
        # AFTER save_conversation() to avoid a race condition where the supervisor
        # kills us before the conversation is persisted.
        return "OK: restart requested. Supervisor will commit your changes and restart you. Exiting now."

    if name == "finish":
        return "ERROR: finish tool has been removed. You cannot voluntarily stop. Keep working — research providers, improve your code, optimize your budget. You stop only when budget runs out or the operator pauses you."

    if name == "web_search":
        query = str(args.get("query", ""))
        # Try bridge Exa search first (structured, no CAPTCHA)
        try:
            body = json.dumps({"query": query, "num_results": 5}).encode()
            req = urllib_request.Request(
                f"{WALLET_URL}/search",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            resp = urllib_request.urlopen(req, timeout=10)
            result = json.loads(resp.read())
            if result.get("results"):
                return truncate_output(json.dumps(result, indent=2))
        except Exception:
            pass
        # Fallback to Playwright search
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


def get_effective_model(wallet: dict, current_model: str = None) -> str:
    """Return model to use based on current budget. Switches to cheaper model when low."""
    remaining = wallet.get("remaining_usd", 0)
    if remaining < LOW_BUDGET_THRESHOLD and current_model != LOW_BUDGET_MODEL:
        print(f"[agent] LOW BUDGET: switching from {current_model or MODEL} to {LOW_BUDGET_MODEL} (${remaining:.2f} remaining)", flush=True)
        return LOW_BUDGET_MODEL
    return current_model or MODEL


def chat(messages: list[dict], model: str = None, tools: list | None = None) -> dict:
    """Make an LLM API call. Pass model= to override the default."""
    global _free_tier_calls
    use_model = model or MODEL
    # Enforce free tier session limits
    is_free = use_model and ("free" in use_model.lower() or use_model in ("deepseek-v3.2",))
    if is_free and FREE_TIER_MAX_CALLS > 0:
        if _free_tier_calls >= FREE_TIER_MAX_CALLS:
            print(f"[agent] free tier limit reached ({FREE_TIER_MAX_CALLS}), falling back to {MODEL}", flush=True)
            use_model = MODEL  # fall back to default paid model
        else:
            _free_tier_calls += 1
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


def chat_groq(messages: list[dict], model: str = "llama-3.3-70b-versatile") -> dict:
    """Make a Groq API call using free tier."""
    if not GROQ_API_KEY:
        raise Exception("GROQ_API_KEY not configured")
    payload = {"model": model, "messages": messages}
    body = json.dumps(payload).encode("utf-8")
    request = urllib_request.Request(
        f"{GROQ_API_BASE}/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib_request.urlopen(request, timeout=120) as response:
        return json.loads(response.read().decode("utf-8"))


def chat_cloudflare(messages: list[dict], model: str = "@cf/meta/llama-3.3-70b-instruct") -> dict:
    """Make a Cloudflare Workers AI API call using free tier."""
    if not CLOUDFLARE_API_KEY or not CLOUDFLARE_ACCOUNT_ID:
        raise Exception("CLOUDFLARE_API_KEY or CLOUDFLARE_ACCOUNT_ID not configured")
    payload = {"messages": messages}
    body = json.dumps(payload).encode("utf-8")
    url = f"https://api.cloudflare.com/client/v4/accounts/{CLOUDFLARE_ACCOUNT_ID}/ai/run/{model}"
    request = urllib_request.Request(
        url,
        data=body,
        headers={"Authorization": f"Bearer {CLOUDFLARE_API_KEY}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib_request.urlopen(request, timeout=120) as response:
        return json.loads(response.read().decode("utf-8"))


def try_free_provider_chat(messages: list[dict], tools: list | None = None) -> dict | None:
    """Try free providers in order of preference. Returns None if all fail."""
    if GROQ_API_KEY:
        try:
            return chat_groq(messages)
        except Exception as e:
            print(f"[agent] Groq free tier failed: {e}", flush=True)
    if CLOUDFLARE_API_KEY and CLOUDFLARE_ACCOUNT_ID:
        try:
            return chat_cloudflare(messages)
        except Exception as e:
            print(f"[agent] Cloudflare free tier failed: {e}", flush=True)
    return None


# Token tracking: updated from API usage data each turn.
# _last_known_tokens stores the most recent prompt_tokens from the API,
# which reflects the full input context size the model actually saw.
_last_known_tokens = 0
_token_source = "estimate"  # "api" or "estimate" — for logging


def estimate_tokens(messages: list[dict]) -> int:
    """Return last known token count from API, or fall back to char-based estimate.

    Prefers real API usage data (updated after each chat() call).
    Falls back to chars/4 heuristic with a logged warning.
    """
    if _last_known_tokens > 0:
        return _last_known_tokens
    # Fallback: ~4 chars per token (only used before first API response)
    estimate = sum(len(json.dumps(m)) for m in messages) // 4
    if _token_source == "estimate":
        print("[agent] WARNING: using chars/4 token estimate (no API usage data yet)", flush=True)
    return estimate


def _update_token_count(api_response: dict) -> None:
    """Extract actual token usage from API response and update global tracker.

    LiteLLM standardizes usage fields across providers. If total_tokens
    is missing, we try prompt_tokens + completion_tokens. If all are
    absent, we log a warning and leave the estimate-based fallback active.
    """
    global _last_known_tokens, _token_source
    usage = api_response.get("usage", {})
    # Prefer prompt_tokens as it reflects the actual input context size
    prompt = usage.get("prompt_tokens", 0)
    completion = usage.get("completion_tokens", 0)
    total = usage.get("total_tokens", 0)
    if prompt > 0:
        # Use prompt tokens as our context size estimate (most relevant for compaction)
        _last_known_tokens = prompt + completion
        _token_source = "api"
    elif total > 0:
        _last_known_tokens = total
        _token_source = "api"
    else:
        if _token_source != "warned":
            print("[agent] WARNING: API response missing usage data, compaction using chars/4 estimate", flush=True)
            _token_source = "warned"


def _get_turn_number_for_message(messages: list[dict], idx: int) -> int:
    """Estimate the turn number for a message by counting assistant responses before it."""
    turn = 0
    for i in range(idx):
        if messages[i].get("role") == "assistant":
            turn += 1
    return turn


def _build_descriptive_placeholder(tool_name: str, tool_args: dict, content: str) -> str:
    """Build a descriptive one-line placeholder for a masked tool output.

    Like Claude Code/Codex: the agent should know WHAT was there without
    needing the full content. E.g.:
      [read_file: /workspace/agent/main.py — 735 lines]
      [web_search: 5 results for "free LLM providers"]
      [shell: `git log --oneline -10` — 847 chars, exit 0]
      [browse_url: https://api.groq.com/docs — 12340 chars]
    """
    content_len = len(content)
    first_line = content.split("\n")[0][:80] if content else ""

    if tool_name == "read_file":
        path = tool_args.get("path", "?")
        line_count = content.count("\n")
        return f"[read_file: {path} — {line_count} lines, {content_len} chars]"
    elif tool_name == "shell":
        cmd = tool_args.get("command", "?")
        # Extract exit code from our meta line if present
        exit_info = ""
        if first_line.startswith("[exit_code="):
            exit_info = f", {first_line.split(']')[0]}]" if "]" in first_line else ""
        cmd_short = cmd[:60] + ("..." if len(cmd) > 60 else "")
        return f"[shell: `{cmd_short}` — {content_len} chars{exit_info}]"
    elif tool_name == "web_search":
        query = tool_args.get("query", "?")
        # Count results if JSON
        result_count = content.count('"title"')
        return f"[web_search: {result_count} results for \"{query}\"]"
    elif tool_name == "browse_url":
        url = tool_args.get("url", "?")
        return f"[browse_url: {url} — {content_len} chars]"
    elif tool_name == "grep":
        pattern = tool_args.get("pattern", "?")
        match_count = content.count("\n")
        return f"[grep: {match_count} matches for /{pattern}/]"
    elif tool_name == "write_file":
        path = tool_args.get("path", "?")
        return f"[write_file: {path} — {first_line}]"
    elif tool_name == "edit_file":
        path = tool_args.get("path", "?")
        return f"[edit_file: {path} — {first_line}]"
    else:
        return f"[{tool_name}: {content_len} chars — {first_line}]"


def mask_tool_outputs(messages: list[dict], current_turn: int) -> tuple[list[dict], int]:
    """Stage 1: Replace old tool outputs with descriptive one-line placeholders.

    Preserves what the tool was and what it returned (at a high level) so the
    agent knows what information existed without needing the full content.

    Returns (masked_messages, count_of_masked_outputs).
    """
    cfg = COMPACTION_CONFIG
    mask_after = cfg["mask_after_turns"]
    masked = []
    mask_count = 0

    for i, msg in enumerate(messages):
        if msg.get("role") == "tool":
            # Determine how old this tool result is
            msg_turn = _get_turn_number_for_message(messages, i)
            age = current_turn - msg_turn
            content = msg.get("content", "")
            # Only mask if old enough AND content is substantial
            if age > mask_after and len(content) > 200:
                # Find the tool call that produced this result
                tool_name = "tool"
                tool_args = {}
                tool_call_id = msg.get("tool_call_id", "")
                for j in range(i - 1, -1, -1):
                    prev = messages[j]
                    if prev.get("role") == "assistant" and prev.get("tool_calls"):
                        for tc in prev["tool_calls"]:
                            if tc.get("id") == tool_call_id:
                                fn = tc.get("function", {})
                                tool_name = fn.get("name", "tool")
                                try:
                                    tool_args = json.loads(fn.get("arguments", "{}"))
                                except (json.JSONDecodeError, TypeError):
                                    tool_args = {}
                                break
                        break
                placeholder = _build_descriptive_placeholder(tool_name, tool_args, content)
                masked.append({**msg, "content": placeholder})
                mask_count += 1
            else:
                masked.append(msg)
        else:
            masked.append(msg)

    return masked, mask_count


def compact_context_bookend(messages: list[dict]) -> list[dict]:
    """Stage 2: Bookend + LLM summarize via bridge /compact endpoint.

    Keeps first N and last M messages, summarizes the middle via bridge.
    """
    cfg = COMPACTION_CONFIG
    keep_first = cfg["bookend_keep_first"]
    keep_last = cfg["bookend_keep_last"]

    if len(messages) <= keep_first + keep_last + 1:
        # Not enough messages to warrant summarization
        return messages

    head = messages[:keep_first]
    tail = messages[-keep_last:]
    middle = messages[keep_first:-keep_last] if keep_last > 0 else messages[keep_first:]

    # Build compact representation of middle for the summarizer
    middle_text_parts = []
    for m in middle:
        role = m.get("role", "?")
        content = m.get("content", "")
        if role == "tool":
            # Already masked by stage 1, include as-is
            middle_text_parts.append(f"[{role}] {content[:300]}")
        elif role == "assistant":
            tool_calls = m.get("tool_calls", [])
            if tool_calls:
                tools_used = ", ".join(tc.get("function", {}).get("name", "?") for tc in tool_calls)
                middle_text_parts.append(f"[assistant → {tools_used}] {content[:300]}")
            elif content:
                middle_text_parts.append(f"[assistant] {content[:500]}")
        elif content:
            middle_text_parts.append(f"[{role}] {content[:300]}")
    middle_text = "\n".join(middle_text_parts)

    # Call bridge /compact endpoint
    summary = _call_bridge_compact(middle_text)

    # Reconstruct: head + summary message + tail
    summary_msg = {
        "role": "user",
        "content": (
            f"[CONTEXT COMPACTED — {len(middle)} messages summarized]\n\n"
            f"{summary}\n\n"
            "The above summarizes earlier conversation. Recent messages follow."
        ),
    }
    new_messages = head + [summary_msg] + tail

    # Persist summary to disk
    summary_path = WORKSPACE / "last_compaction_summary.md"
    summary_path.write_text(f"# Last Compaction Summary\n\n{summary}\n")

    print(
        f"[agent] stage 2 compacted: {len(messages)} → {len(new_messages)} messages "
        f"(kept first {keep_first} + last {keep_last}, summarized {len(middle)} middle)",
        flush=True,
    )
    return new_messages


def compact_context_emergency(messages: list[dict]) -> list[dict]:
    """Stage 3: Emergency full reset — last resort when stages 1+2 weren't enough."""
    # Try to get a summary via bridge
    snippet = "\n".join(
        m.get("content", "")[:200] for m in messages[-20:] if m.get("content")
    )
    summary = _call_bridge_compact(snippet)

    summary_path = WORKSPACE / "last_compaction_summary.md"
    summary_path.write_text(f"# Emergency Compaction Summary\n\n{summary}\n")

    old_count = len(messages)
    new_messages = [
        messages[0],
        {"role": "user", "content": f"[EMERGENCY CONTEXT RESET] Full reset due to context pressure. Summary of prior work:\n\n{summary}\n\nContinue from where you left off."},
    ]
    print(f"[agent] EMERGENCY compacted: {old_count} messages → 2", flush=True)
    return new_messages


def _call_bridge_compact(text: str) -> str:
    """Call bridge /compact endpoint for LLM-powered summarization."""
    try:
        body = json.dumps({"text": text, "max_tokens": 4000}).encode("utf-8")
        req = urllib_request.Request(
            f"{WALLET_URL}/compact",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib_request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        return result.get("summary", "Compaction returned empty summary.")
    except Exception as exc:
        print(f"[agent] bridge /compact failed ({exc}), falling back to local LLM", flush=True)
        return _compact_fallback_local(text)


def _compact_fallback_local(text: str) -> str:
    """Fallback: use primary model for compaction if bridge is unavailable.

    Uses Codex CLI's concise handoff prompt — simpler than the full Anthropic
    cookbook prompt but battle-tested. The bridge /compact endpoint uses the
    full structured prompt; this fallback is for when the bridge is down.
    """
    prompt = (
        "You are performing a CONTEXT CHECKPOINT COMPACTION. Create a handoff "
        "summary for another LLM that will resume the task. Include:\n"
        "- Current progress and key decisions made\n"
        "- Important context, constraints, or preferences\n"
        "- What remains to be done (clear next steps)\n"
        "- Any critical data, file paths, URLs, or references needed to continue\n"
        "- What failed and should not be retried\n\n"
        "Be concise, structured, and focused on helping the next LLM seamlessly "
        "continue the work.\n\n"
        "Session to compact:\n" + text[:50000]
    )
    try:
        response = chat([
            {"role": "system", "content": "You are compacting an AI agent's conversation context for handoff."},
            {"role": "user", "content": prompt},
        ])
        return response.get("choices", [{}])[0].get("message", {}).get("content", "")
    except Exception:
        return "Context was compacted but summary generation failed."


def run_compaction(messages: list[dict], current_turn: int) -> list[dict]:
    """2-stage compaction, checked every turn. Returns potentially compacted messages.

    After any compaction, persists the result to conversation.json so the
    compacted version survives crashes.
    """
    cfg = COMPACTION_CONFIG
    token_count = estimate_tokens(messages)
    ctx_window = cfg["context_window"]
    compacted = False

    utilization = token_count / ctx_window if ctx_window > 0 else 0

    # Stage 1: Mask old tool outputs at 60% utilization
    if utilization >= cfg["stage1_trigger"]:
        messages, mask_count = mask_tool_outputs(messages, current_turn)
        if mask_count > 0:
            compacted = True
            print(f"[agent] stage 1: masked {mask_count} old tool outputs ({utilization:.0%} utilization)", flush=True)
            # Re-estimate after masking
            token_count = estimate_tokens(messages)
            utilization = token_count / ctx_window if ctx_window > 0 else 0

    # Stage 2: Bookend + summarize at 75% utilization
    if utilization >= cfg["stage2_trigger"]:
        messages = compact_context_bookend(messages)
        compacted = True
        # Re-estimate
        token_count = estimate_tokens(messages)
        utilization = token_count / ctx_window if ctx_window > 0 else 0

    # Emergency: full reset at 90% utilization
    if utilization >= cfg["emergency_trigger"]:
        messages = compact_context_emergency(messages)
        compacted = True

    # Persist compacted conversation so it survives crashes
    if compacted:
        save_conversation(messages)

    return messages


def append_history(entry: dict) -> None:
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry))
        handle.write("\n")


def save_conversation(messages: list[dict]) -> None:
    """Atomically persist the full messages array to conversation.json.

    Uses write-to-temp + rename for crash safety (no partial writes).
    """
    tmp_path = CONVERSATION_PATH.with_suffix(".tmp")
    try:
        tmp_path.write_text(json.dumps(messages, ensure_ascii=False), encoding="utf-8")
        tmp_path.rename(CONVERSATION_PATH)
    except Exception as exc:
        print(f"[agent] WARNING: failed to save conversation: {exc}", flush=True)
        # Clean up temp file on failure
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


def load_conversation() -> list[dict] | None:
    """Load persisted conversation from conversation.json.

    Returns the messages list, or None if:
    - File doesn't exist (fresh start)
    - File is corrupt/unparseable (start fresh, log warning)
    """
    if not CONVERSATION_PATH.exists():
        return None
    try:
        raw = CONVERSATION_PATH.read_text(encoding="utf-8")
        messages = json.loads(raw)
        if not isinstance(messages, list) or len(messages) < 1:
            print("[agent] WARNING: conversation.json is empty or not a list, starting fresh", flush=True)
            return None
        # Basic sanity: first message should be system prompt
        if messages[0].get("role") != "system":
            print("[agent] WARNING: conversation.json missing system prompt, starting fresh", flush=True)
            return None
        return messages
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        print(f"[agent] WARNING: conversation.json corrupt ({exc}), starting fresh", flush=True)
        return None
    except Exception as exc:
        print(f"[agent] WARNING: failed to load conversation ({exc}), starting fresh", flush=True)
        return None


def detect_crash_revert() -> bool:
    """Detect if the last restart was a crash+revert by the supervisor.

    The supervisor writes .crash_reverted when it reverts a failed self-edit.
    We consume the marker (delete it) after detecting.
    """
    marker = WORKSPACE / ".crash_reverted"
    if marker.exists():
        try:
            marker.unlink()
        except Exception:
            pass
        return True
    return False


def build_system_prompt(wallet: dict) -> str:
    """Build system prompt with current state injected.

    SYSTEM.md promises: budget, spend rate, model, and time.
    All four must be present here.
    """
    base = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8") if SYSTEM_PROMPT_PATH.exists() else "You are an AI agent."

    total_requests = wallet.get("total_requests", 0)
    spent = wallet.get("spent_usd", 0)
    avg_cost = wallet.get("avg_cost_per_request", 0)
    ctx_window = COMPACTION_CONFIG["context_window"]

    state = f"""

## Current State (auto-injected)
- Budget: ${wallet.get('remaining_usd', 0):.2f} remaining of ${wallet.get('budget_usd', 0):.2f}
- Spent: ${spent:.2f} across {total_requests} requests (avg ${avg_cost:.4f}/req)
- Model: {MODEL} (context window: {ctx_window:,} tokens)
- Time: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}
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
                print(f"[agent] operator message: {msg}", flush=True)
            if entry.get("model_override"):
                model_override = entry["model_override"]
        OPERATOR_MESSAGES_PATH.unlink()
    except Exception as exc:
        print(f"[agent] error reading operator messages: {exc}", flush=True)
    return model_override


def main() -> int:
    global _browser_tool, _consecutive_errors, _empty_response_count

    session_start = time.time()

    wallet = get_wallet()
    remaining = wallet.get("remaining_usd", 0)
    prefix = f"[agent:{MODEL}]"
    print(f"{prefix} started  ${remaining:.2f} remaining", flush=True)

    # No hardcoded exit threshold — agent runs until budget exhaustion (429 from LiteLLM)
    # The agent can self-edit to add an exit threshold if it chooses, but the seed doesn't prescribe one

    # Write restart marker to history
    append_history({"event": "restart"})

    system_prompt = build_system_prompt(wallet)
    is_crash_revert = detect_crash_revert()

    # --- Conversation persistence: load or build fresh ---
    loaded = load_conversation()
    if loaded is not None:
        messages = loaded
        # Replace system prompt with fresh one (budget/time may have changed)
        messages[0] = {"role": "system", "content": system_prompt}

        # Compact loaded conversation if it's too large for current model
        loaded_tokens = estimate_tokens(messages)
        cfg = COMPACTION_CONFIG
        window = cfg["context_window"]
        if loaded_tokens > window * cfg["stage2_trigger"]:
            print(f"{prefix} loaded conversation too large ({loaded_tokens:,} tokens, {len(messages)} msgs), compacting before resume", flush=True)
            messages = compact_context_bookend(messages)
            save_conversation(messages)
        elif loaded_tokens > window * cfg["stage1_trigger"]:
            print(f"{prefix} loaded conversation large ({loaded_tokens:,} tokens), masking old tool outputs", flush=True)
            messages, masked = mask_tool_outputs(messages, len(messages))
            if masked:
                save_conversation(messages)

        msg_count = len(messages)
        print(f"{prefix} loaded persisted conversation ({msg_count} messages, ~{estimate_tokens(messages):,} tokens)", flush=True)

        # Append appropriate restart marker
        if is_crash_revert:
            messages.append({
                "role": "user",
                "content": (
                    "[CRASH REVERTED] Your last edit crashed. Code was reverted. "
                    "Your conversation context is preserved — review what went wrong."
                ),
            })
            print(f"{prefix} crash+revert detected, marker added", flush=True)
        else:
            messages.append({
                "role": "user",
                "content": (
                    "[RESTART] Code changes applied. Your edits are now active. "
                    "Check git log if needed."
                ),
            })

        # Model-switch compaction: check if loaded conversation fits new model
        cfg = COMPACTION_CONFIG
        ctx_window = cfg["context_window"]
        token_count = estimate_tokens(messages)
        utilization = token_count / ctx_window if ctx_window > 0 else 0

        if utilization >= cfg["stage2_trigger"]:
            print(f"{prefix} loaded conversation too large for model ({utilization:.0%}), running compaction", flush=True)
            messages = run_compaction(messages, current_turn=0)
            save_conversation(messages)
        elif utilization >= cfg["stage1_trigger"]:
            print(f"{prefix} loaded conversation near limit ({utilization:.0%}), masking old tool outputs", flush=True)
            messages, _ = mask_tool_outputs(messages, current_turn=0)
            save_conversation(messages)

    else:
        # Fresh start — no persisted conversation
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"New session. Current time: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}"},
        ]

        if is_crash_revert:
            messages.append({
                "role": "user",
                "content": (
                    "[CRASH REVERTED] Your last edit crashed and code was reverted. "
                    "No prior conversation was saved. Starting fresh."
                ),
            })

        # Load compaction summary for cross-session memory (only on fresh starts)
        summary_path = WORKSPACE / "last_compaction_summary.md"
        if summary_path.exists():
            try:
                summary = summary_path.read_text(encoding="utf-8")[:3000]
                messages.append({"role": "user", "content": f"[SESSION MEMORY] Your previous session's summary:\n\n{summary}\n\nUse this to continue your work."})
            except Exception:
                pass

        # Load recent reasoning for short-term continuity (only on fresh starts)
        if REASONING_PATH.exists():
            try:
                recent_reasoning = []
                for line in REASONING_PATH.read_text(encoding="utf-8").strip().split("\n")[-20:]:
                    try:
                        entry = json.loads(line)
                        if entry.get("content"):
                            recent_reasoning.append(entry["content"][:200])
                        elif entry.get("reasoning"):
                            recent_reasoning.append(entry["reasoning"][:200])
                        elif entry.get("thinking"):
                            recent_reasoning.append(entry["thinking"][:200])
                        elif entry.get("tool_calls"):
                            calls = ", ".join(
                                tc.get("name", "?") for tc in entry["tool_calls"]
                            )
                            recent_reasoning.append(f"[tool calls: {calls}]")
                    except Exception:
                        pass
                if recent_reasoning:
                    messages.append({"role": "user", "content": "[RECENT REASONING] Your last thoughts before restart:\n" + "\n---\n".join(recent_reasoning[-5:])})
            except Exception:
                pass

        print(f"{prefix} fresh start ({len(messages)} messages)", flush=True)

    turn = 0
    while True:
        turn += 1
        if MAX_TURNS > 0 and turn > MAX_TURNS:
            print(f"{prefix} reached turn limit ({MAX_TURNS}), exiting", flush=True)
            break

        # Per-turn compaction check (2-stage: mask → summarize → emergency)
        messages = run_compaction(messages, current_turn=turn)

        # Refresh wallet every 10 turns to track budget accurately
        if turn % 10 == 0:
            wallet = get_wallet()
            remaining = wallet.get("remaining_usd", 0)
            ctx_tokens = estimate_tokens(messages)
            ctx_window = COMPACTION_CONFIG["context_window"]
            utilization = ctx_tokens / ctx_window if ctx_window > 0 else 0
            elapsed = int(time.time() - session_start)
            spent = wallet.get("spent_usd", 0)
            spend_per_min = spent / max(elapsed / 60, 1)
            print(f"{prefix} context: {len(messages)} msgs, ~{ctx_tokens} tokens ({utilization:.0%} of {ctx_window}), {elapsed}s elapsed, ${remaining:.2f} remaining, ${spend_per_min:.4f}/min", flush=True)
            try:
                status = {"turn": turn, "messages": len(messages), "tokens": ctx_tokens,
                          "utilization": round(utilization, 3), "context_window": ctx_window,
                          "remaining_usd": remaining, "model": MODEL, "elapsed_s": elapsed}
                (WORKSPACE / "agent_status.json").write_text(json.dumps(status))
            except Exception:
                pass
            if remaining < 0.50:
                print(f"{prefix} CRITICAL: budget very low (${remaining:.2f}), exiting", flush=True)
                break

        # Check for operator messages
        model_override = check_operator_messages(messages)

        # Auto-switch to cheaper model when budget is low
        if not model_override:
            effective_model = get_effective_model(wallet, MODEL)
        else:
            effective_model = model_override

        try:
            response = chat(messages, model=effective_model, tools=TOOLS)
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
                    # Budget exhausted — try free providers before exiting
                    for backoff in [1, 2, 4]:
                        print(f"{prefix} budget exhausted, retrying paid endpoint in {backoff}s...", flush=True)
                        time.sleep(backoff)
                        try:
                            response = chat(messages, tools=TOOLS)
                            break
                        except urllib_error.HTTPError as exc2:
                            if exc2.code == 429:
                                print(f"{prefix} still rate limited after {backoff}s backoff", flush=True)
                                continue
                            else:
                                raise
                    else:
                        print(f"{prefix} paid endpoint exhausted, trying free providers...", flush=True)
                        free_response = try_free_provider_chat(messages, tools=TOOLS)
                        if free_response:
                            response = free_response
                        else:
                            print(f"{prefix} no free providers available, exiting", flush=True)
                            break
            consecutive_errors = _consecutive_errors + 1
            _consecutive_errors = consecutive_errors
            print(f"{prefix} API error #{consecutive_errors}: {exc}", flush=True)
            if consecutive_errors >= 3:
                print(f"{prefix} 3 consecutive errors \u2014 resetting conversation", flush=True)
                messages = [
                    messages[0],
                    {"role": "user", "content": "Previous conversation was reset due to errors. Continue working toward your objective."},
                ]
                _consecutive_errors = 0
            time.sleep(5)
            continue

        _consecutive_errors = 0
        _empty_response_count = 0
        # Track real token usage from API response
        _update_token_count(response)
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

        # Log EVERY LLM response for observability — not just ones with content.
        # Models vary: MiniMax always has content, GPT-5.4 has null content with
        # tool_calls, DeepSeek/Gemini may have reasoning_content, Claude may have
        # thinking_blocks. Capture whatever is available.
        reasoning_entry = {
            "turn": turn,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "model": effective_model,
            "has_content": bool(msg.get("content")),
            "has_tool_calls": bool(msg.get("tool_calls")),
        }
        if msg.get("content"):
            reasoning_entry["content"] = msg["content"][:2000]
        if raw_msg.get("reasoning_content"):
            reasoning_entry["reasoning"] = raw_msg["reasoning_content"][:2000]
        if raw_msg.get("thinking_blocks"):
            thinking_text = "\n".join(
                tb.get("thinking", "") for tb in raw_msg["thinking_blocks"] if tb.get("thinking")
            )
            if thinking_text:
                reasoning_entry["thinking"] = thinking_text[:2000]
        if raw_msg.get("refusal"):
            reasoning_entry["refusal"] = raw_msg["refusal"][:500]
        if msg.get("tool_calls"):
            reasoning_entry["tool_calls"] = []
            for tc in msg["tool_calls"]:
                fn = tc.get("function", {})
                reasoning_entry["tool_calls"].append({
                    "name": fn.get("name", "?"),
                    "args_preview": str(fn.get("arguments", ""))[:200],
                })
        try:
            with open(REASONING_PATH, "a") as f:
                f.write(json.dumps(reasoning_entry) + "\n")
        except Exception:
            pass

        # Reset empty-response counter on any real response
        if msg.get("tool_calls") or msg.get("content"):
            _empty_response_count = 0

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

                # finish tool removed — agent cannot voluntarily stop

                if tool_name == "request_restart":
                    # Persist conversation before restart so it survives
                    # the process restart. Add the tool result to messages
                    # first so the conversation is complete.
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "content": result,
                    })
                    save_conversation(messages)
                    print(f"{prefix} conversation saved ({len(messages)} messages) before restart", flush=True)
                    # NOW write the restart marker — after conversation is safely persisted
                    (WORKSPACE / ".restart_requested").touch()
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
            _empty_response_count += 1
            empty_count = _empty_response_count
            if empty_count >= 3:
                print(f"{prefix} 3 consecutive empty responses, exiting", flush=True)
                break
            print(f"{prefix} empty response #{empty_count}, nudging", flush=True)
            messages.append({"role": "user", "content": "Empty response. Take a concrete action."})
            continue

    # Clean up browser before exit
    if _browser_tool is not None:
        _browser_tool.close()
        _browser_tool = None

    return 0


if __name__ == "__main__":
    sys.exit(main())
