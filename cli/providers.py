#!/usr/bin/env python3
"""RSI-Econ provider management CLI.

Usage:
    python cli/providers.py list                     # Show active + proposed providers
    python cli/providers.py add groq --key sk-xxx    # Add a new provider with API key
    python cli/providers.py reload                   # Hot-reload LiteLLM config
    python cli/providers.py remove groq              # Remove a provider
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, TextIO
from urllib import error as urllib_error
from urllib import request as urllib_request


ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "docker-compose.yml"
ENV_FILE = ROOT / ".env"
LITELLM_CONFIG = ROOT / "trusted" / "litellm" / "litellm_config.yaml"
PROVIDER_PROPOSALS_DIR = ROOT / "state" / "provider_proposals"
BRIDGE_URL = os.getenv("RSI_BRIDGE_URL", "http://localhost:8081")
DYNAMIC_MARKER = "# --- DYNAMIC MODELS ---"

# Known provider templates: maps provider short name → litellm model prefix and env var
PROVIDER_TEMPLATES: dict[str, dict[str, str]] = {
    "groq": {
        "prefix": "groq/",
        "env_key": "GROQ_API_KEY",
        "default_model": "groq/llama-3.3-70b-versatile",
    },
    "cerebras": {
        "prefix": "cerebras/",
        "env_key": "CEREBRAS_API_KEY",
        "default_model": "cerebras/llama-3.3-70b",
    },
    "mistral": {
        "prefix": "mistral/",
        "env_key": "MISTRAL_API_KEY",
        "default_model": "mistral/mistral-small-latest",
    },
    "together": {
        "prefix": "together_ai/",
        "env_key": "TOGETHERAI_API_KEY",
        "default_model": "together_ai/meta-llama/Llama-3.3-70B-Instruct-Turbo",
    },
    "deepseek": {
        "prefix": "deepseek/",
        "env_key": "DEEPSEEK_API_KEY",
        "default_model": "deepseek/deepseek-chat",
    },
}


def bridge_get(path: str) -> Any:
    try:
        req = urllib_request.Request(f"{BRIDGE_URL}{path}", method="GET")
        with urllib_request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def bridge_post(path: str, payload: dict | None = None) -> Any:
    body = json.dumps(payload or {}).encode("utf-8")
    try:
        req = urllib_request.Request(
            f"{BRIDGE_URL}{path}", data=body,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib_request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def docker_compose(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), *args],
        capture_output=True, text=True, check=False, timeout=60,
    )


# ── Commands ─────────────────────────────────────────────────────────


def cmd_list(args: argparse.Namespace, out: TextIO = sys.stdout) -> int:
    data = bridge_get("/providers")
    if not data:
        # Fallback: read proposal files directly
        print("Bridge unreachable. Reading local state...", file=out)
        data = {"active": [], "proposed": []}
        if PROVIDER_PROPOSALS_DIR.exists():
            for path in sorted(PROVIDER_PROPOSALS_DIR.glob("*.json")):
                try:
                    record = json.loads(path.read_text("utf-8"))
                    data["proposed"].append(record)
                except Exception:
                    continue

    print("\n=== Active Providers ===", file=out)
    for p in data.get("active", []):
        models = ", ".join(p.get("models", [])[:5])
        print(f"  {p['name']}: {models}", file=out)

    print("\n=== Proposed Providers ===", file=out)
    proposed = data.get("proposed", [])
    if not proposed:
        print("  (none)", file=out)
    for p in proposed:
        status = p.get("status", "?")
        model = p.get("model_id", "?")
        name = p.get("name", "?")
        pid = p.get("proposal_id", "?")[:8]
        print(f"  [{pid}] {name} ({model}) — {status}", file=out)
        signup = p.get("signup_url", "")
        if signup:
            print(f"         Signup: {signup}", file=out)
        free_tier = p.get("free_tier", "")
        if free_tier:
            print(f"         Free tier: {free_tier}", file=out)

    print(file=out)
    return 0


def _set_env_var(key: str, value: str) -> None:
    """Append or update a variable in .env."""
    if ENV_FILE.exists():
        content = ENV_FILE.read_text("utf-8")
        # Check if key already exists (commented or not)
        pattern = re.compile(rf"^#?\s*{re.escape(key)}=.*$", re.MULTILINE)
        if pattern.search(content):
            content = pattern.sub(f"{key}={value}", content)
            ENV_FILE.write_text(content, encoding="utf-8")
            return
        # Append
        if not content.endswith("\n"):
            content += "\n"
        content += f"{key}={value}\n"
        ENV_FILE.write_text(content, encoding="utf-8")
    else:
        ENV_FILE.write_text(f"{key}={value}\n", encoding="utf-8")


def _add_litellm_model(model_name: str, litellm_model: str, env_key: str) -> bool:
    """Add a model entry to litellm_config.yaml after the DYNAMIC MODELS marker."""
    if not LITELLM_CONFIG.exists():
        print(f"Error: LiteLLM config not found at {LITELLM_CONFIG}", file=sys.stderr)
        return False

    content = LITELLM_CONFIG.read_text("utf-8")

    # Check if model already exists
    if f'model_name: "{model_name}"' in content:
        print(f"  Model '{model_name}' already in config, skipping.")
        return True

    entry = (
        f'  - model_name: "{model_name}"\n'
        f"    litellm_params:\n"
        f'      model: "{litellm_model}"\n'
        f'      api_key: "os.environ/{env_key}"\n'
    )

    if DYNAMIC_MARKER in content:
        content = content.replace(DYNAMIC_MARKER, DYNAMIC_MARKER + "\n" + entry, 1)
    else:
        # Append after last model entry
        content = content.rstrip() + "\n" + entry

    LITELLM_CONFIG.write_text(content, encoding="utf-8")
    return True


def _remove_litellm_model(model_name: str) -> bool:
    """Remove a model entry from litellm_config.yaml."""
    if not LITELLM_CONFIG.exists():
        return False

    content = LITELLM_CONFIG.read_text("utf-8")
    # Match a full model block: from "  - model_name: ..." to the next "  - " or section
    pattern = re.compile(
        rf'^  - model_name: "{re.escape(model_name)}".*?(?=^  - model_name:|^[a-z]|\Z)',
        re.MULTILINE | re.DOTALL,
    )
    new_content = pattern.sub("", content)
    if new_content == content:
        return False
    LITELLM_CONFIG.write_text(new_content, encoding="utf-8")
    return True


def _find_proposal_by_provider(provider: str) -> dict | None:
    """Find a provider proposal matching the given name."""
    if not PROVIDER_PROPOSALS_DIR.exists():
        return None
    for path in PROVIDER_PROPOSALS_DIR.glob("*.json"):
        try:
            record = json.loads(path.read_text("utf-8"))
            if record.get("name", "").lower() == provider.lower() or \
               record.get("provider", "").lower() == provider.lower():
                return record
        except Exception:
            continue
    return None


def cmd_add(args: argparse.Namespace, out: TextIO = sys.stdout) -> int:
    provider = args.provider.lower()
    key = args.key

    # Look up template
    template = PROVIDER_TEMPLATES.get(provider)
    if not template:
        print(f"Warning: no template for '{provider}', using defaults.", file=out)
        template = {
            "prefix": f"{provider}/",
            "env_key": f"{provider.upper()}_API_KEY",
            "default_model": f"{provider}/default",
        }

    env_key = template["env_key"]

    # Check for a proposal to get model details
    proposal = _find_proposal_by_provider(provider)
    model_id = template["default_model"]
    if proposal and proposal.get("model_id"):
        model_id = proposal["model_id"]

    # Derive a short model_name for litellm from the model_id
    # e.g. "groq/llama-3.3-70b-versatile" → "llama-3.3-70b-versatile"
    model_name = model_id.split("/", 1)[-1] if "/" in model_id else model_id

    print(f"Adding provider: {provider}", file=out)
    print(f"  Env var: {env_key}", file=out)
    print(f"  Model: {model_id} (as '{model_name}')", file=out)

    # 1. Write API key to .env
    _set_env_var(env_key, key)
    print(f"  Written {env_key} to .env", file=out)

    # 2. Also pass it to litellm container via docker-compose env
    _ensure_litellm_env_var(env_key)

    # 3. Add model to litellm config
    if _add_litellm_model(model_name, model_id, env_key):
        print(f"  Added '{model_name}' to litellm_config.yaml", file=out)
    else:
        return 1

    # 4. Reload
    print("  Reloading LiteLLM...", file=out)
    rc = _reload_litellm(out)

    # 5. Mark proposal as active if one exists
    if proposal and proposal.get("proposal_id"):
        bridge_post(f"/providers/proposals/{proposal['proposal_id']}/activate")
        print(f"  Proposal {proposal['proposal_id'][:8]} marked active", file=out)

    print("Done.", file=out)
    return rc


def _ensure_litellm_env_var(env_key: str) -> None:
    """Make sure the litellm service in docker-compose.yml has the env var."""
    if not COMPOSE_FILE.exists():
        return
    content = COMPOSE_FILE.read_text("utf-8")
    marker = f"{env_key}: ${{{env_key}:-}}"
    if env_key in content:
        return
    # Insert after OPENROUTER_API_KEY line in litellm environment
    target = "OPENROUTER_API_KEY: ${OPENROUTER_API_KEY:-}"
    if target in content:
        content = content.replace(target, f"{target}\n      {marker}")
        COMPOSE_FILE.write_text(content, encoding="utf-8")


def _reload_litellm(out: TextIO = sys.stdout) -> int:
    """Restart litellm container and wait for healthy."""
    result = docker_compose("restart", "litellm")
    if result.returncode != 0:
        print(f"  Error restarting litellm: {result.stderr.strip()}", file=out)
        return 1

    # Wait for healthy (up to 60s)
    for i in range(12):
        time.sleep(5)
        ps = docker_compose("ps", "--format", "json", "litellm")
        if ps.returncode == 0 and ps.stdout.strip():
            try:
                for line in ps.stdout.strip().split("\n"):
                    info = json.loads(line)
                    if info.get("Health", "") == "healthy" or info.get("State") == "running":
                        print("  LiteLLM restarted and healthy.", file=out)
                        return 0
            except (json.JSONDecodeError, KeyError):
                pass
        print(f"  Waiting for LiteLLM... ({(i + 1) * 5}s)", file=out)

    print("  Warning: LiteLLM may not be fully healthy yet.", file=out)
    return 0


def cmd_reload(args: argparse.Namespace, out: TextIO = sys.stdout) -> int:
    print("Reloading LiteLLM...", file=out)
    return _reload_litellm(out)


def cmd_remove(args: argparse.Namespace, out: TextIO = sys.stdout) -> int:
    provider = args.provider.lower()
    template = PROVIDER_TEMPLATES.get(provider)

    if template:
        model_name = template["default_model"].split("/", 1)[-1]
        if _remove_litellm_model(model_name):
            print(f"Removed '{model_name}' from litellm_config.yaml", file=out)
        else:
            print(f"Model for '{provider}' not found in config.", file=out)
    else:
        print(f"No template for '{provider}'. Remove manually from litellm_config.yaml.", file=out)
        return 1

    print("Reload LiteLLM with: python cli/providers.py reload", file=out)
    return 0


# ── Entrypoint ───────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description="RSI-Econ provider management")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("list", help="Show active and proposed providers")

    add_p = sub.add_parser("add", help="Add a new provider with API key")
    add_p.add_argument("provider", help="Provider name (groq, cerebras, mistral, etc.)")
    add_p.add_argument("--key", required=True, help="API key for the provider")

    sub.add_parser("reload", help="Hot-reload LiteLLM config (restart container)")

    rm_p = sub.add_parser("remove", help="Remove a provider")
    rm_p.add_argument("provider", help="Provider name to remove")

    args = parser.parse_args()
    commands = {
        "list": cmd_list,
        "add": cmd_add,
        "reload": cmd_reload,
        "remove": cmd_remove,
    }

    if args.command not in commands:
        parser.print_help()
        return 1
    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
