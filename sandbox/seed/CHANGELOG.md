# What's available in this environment

## Models (18 via LiteLLM)
Free tier, ultra-cheap, cheap, mid, and frontier models available.
Check http://bridge:8081/wallet for the full list with pricing.

## Search
Web search via Exa API (structured, no CAPTCHAs) with Playwright fallback.
Bridge endpoint: POST http://bridge:8081/search

## Git
Full git history managed by bridge. Branch, checkout, merge supported.
Per-commit performance stats recorded automatically.
Endpoints: http://bridge:8081/git/*

## Providers
Propose new LLM providers: POST http://bridge:8081/providers/propose
Operator provisions API keys. Check GET http://bridge:8081/providers for status.

## Context management
2-stage compaction: tool output masking at 60%, bookend+summarize at 75%.
Real token counting from API usage data.

## Self-modification
Edit any file, call request_restart. Syntax-checked before commit.
Auto-reverts on crash. Tarball backup before each self-edit.
