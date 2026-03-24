#!/usr/bin/env python3
"""Inject an operator message into the agent's next turn.

Usage:
    python cli/inject.py "Focus on Groq free tier"
    python cli/inject.py --model gpt-4.1 "Use smart model for complex reasoning"
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

MESSAGES_PATH = Path(__file__).resolve().parents[1] / "state" / "operator_messages" / "pending.jsonl"


def main() -> int:
    parser = argparse.ArgumentParser(description="Inject operator message to agent")
    parser.add_argument("message", help="Message to inject")
    parser.add_argument("--model", help="Override LLM model for next turn")
    args = parser.parse_args()

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "message": args.message,
        "model_override": args.model,
    }

    MESSAGES_PATH.parent.mkdir(parents=True, exist_ok=True)
    with MESSAGES_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")

    print(f"Queued: {args.message}")
    if args.model:
        print(f"Model override: {args.model}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
