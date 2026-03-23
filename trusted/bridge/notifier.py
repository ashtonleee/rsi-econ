"""Discord webhook notification module for RSI-Econ bridge."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

LOGGER = logging.getLogger(__name__)

CONFIG_PATH = Path(os.getenv("NOTIFICATION_CONFIG_PATH", "/var/lib/rsi/notification_config.json"))
EVENTS_DIR = Path(os.getenv("EVENTS_DIR", "/var/lib/rsi/events"))
RATE_LIMIT_SECONDS = 60

_config_cache: dict[str, Any] | None = None
_config_mtime: float = 0.0
_rate_limits: dict[str, float] = {}


def load_config() -> dict[str, Any]:
    """Load notification config, hot-reloading on file change."""
    global _config_cache, _config_mtime
    if _config_cache is not None:
        try:
            current_mtime = CONFIG_PATH.stat().st_mtime
            if current_mtime == _config_mtime:
                return _config_cache
        except OSError:
            return _config_cache
    try:
        raw = CONFIG_PATH.read_text(encoding="utf-8")
        _config_cache = json.loads(raw)
        _config_mtime = CONFIG_PATH.stat().st_mtime
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.warning("Failed to load notification config: %s", exc)
        if _config_cache is None:
            _config_cache = {"webhook_url": "", "events": {}}
    return _config_cache


def notify(event_type: str, message: str, data: dict | None = None) -> bool:
    """Send a Discord webhook notification if the event is enabled.

    Returns True if notification was sent, False otherwise.
    """
    config = load_config()
    webhook_url = config.get("webhook_url", "").strip()
    if not webhook_url:
        return False

    events = config.get("events", {})
    event_config = events.get(event_type, {})
    if not event_config.get("enabled", False):
        return False

    # Rate limit: max 1 per event type per RATE_LIMIT_SECONDS
    now = time.monotonic()
    last_sent = _rate_limits.get(event_type)
    if last_sent is not None and now - last_sent < RATE_LIMIT_SECONDS:
        return False

    content = f"\U0001f916 [rsi-econ] {message}"
    payload = json.dumps({"content": content[:2000]}).encode("utf-8")
    try:
        req = urllib_request.Request(
            webhook_url,
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "rsi-econ/1.0"},
            method="POST",
        )
        urllib_request.urlopen(req, timeout=10)
        _rate_limits[event_type] = now
        print(f"[bridge] notification sent: {event_type} — {message[:80]}", flush=True)
        return True
    except (urllib_error.URLError, TimeoutError, OSError) as exc:
        print(f"[bridge] notification FAILED for {event_type}: {exc}", flush=True)
        return False


def process_event_files() -> None:
    """Read and process supervisor event files, send notifications, delete processed."""
    if not EVENTS_DIR.exists():
        return
    for event_file in sorted(EVENTS_DIR.glob("*.json")):
        try:
            raw = event_file.read_text(encoding="utf-8")
        except OSError:
            continue  # file sync lag — retry next poll, don't delete
        try:
            event = json.loads(raw)
            event_type = event.get("event", "unknown")
            message = event.get("message", "")
            data = event.get("data")
            sent = notify(event_type, message, data)
            if sent:
                LOGGER.info("Sent notification: %s — %s", event_type, message[:100])
            event_file.unlink()
        except (OSError, json.JSONDecodeError) as exc:
            LOGGER.warning("Failed to process event file %s: %s", event_file.name, exc)
            try:
                event_file.unlink()
            except OSError:
                pass
