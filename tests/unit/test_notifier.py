"""Tests for the bridge notification module."""

from __future__ import annotations

import importlib.util
import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[2]
NOTIFIER_PATH = ROOT / "trusted" / "bridge" / "notifier.py"


def load_notifier(tmp_path: Path):
    config_path = tmp_path / "notification_config.json"
    events_dir = tmp_path / "events"
    events_dir.mkdir(exist_ok=True)
    os.environ["NOTIFICATION_CONFIG_PATH"] = str(config_path)
    os.environ["EVENTS_DIR"] = str(events_dir)
    spec = importlib.util.spec_from_file_location(f"test_notifier_{tmp_path.name}", NOTIFIER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    # Reset module-level caches
    module._config_cache = None
    module._config_mtime = 0.0
    module._rate_limits = {}
    return module


def write_config(tmp_path: Path, webhook_url: str = "https://discord.test/webhook", **event_overrides) -> None:
    events = {
        "session_start": {"enabled": True},
        "proposal_submitted": {"enabled": True},
        "budget_warning": {"enabled": True, "threshold_pct": 25},
        "disabled_event": {"enabled": False},
    }
    events.update(event_overrides)
    config = {"webhook_url": webhook_url, "events": events}
    (tmp_path / "notification_config.json").write_text(json.dumps(config))


def test_notify_sends_webhook(tmp_path: Path) -> None:
    write_config(tmp_path)
    mod = load_notifier(tmp_path)

    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = MagicMock()
        result = mod.notify("session_start", "Agent launched")

    assert result is True
    mock_urlopen.assert_called_once()
    call_args = mock_urlopen.call_args
    req = call_args[0][0]
    body = json.loads(req.data.decode("utf-8"))
    assert "[rsi-econ]" in body["content"]
    assert "Agent launched" in body["content"]


def test_notify_skips_disabled_event(tmp_path: Path) -> None:
    write_config(tmp_path)
    mod = load_notifier(tmp_path)

    with patch("urllib.request.urlopen") as mock_urlopen:
        result = mod.notify("disabled_event", "Should not send")

    assert result is False
    mock_urlopen.assert_not_called()


def test_notify_skips_empty_url(tmp_path: Path) -> None:
    write_config(tmp_path, webhook_url="")
    mod = load_notifier(tmp_path)

    with patch("urllib.request.urlopen") as mock_urlopen:
        result = mod.notify("session_start", "Should not send")

    assert result is False
    mock_urlopen.assert_not_called()


def test_config_hot_reload(tmp_path: Path) -> None:
    write_config(tmp_path)
    mod = load_notifier(tmp_path)

    # Load config once
    config1 = mod.load_config()
    assert config1["webhook_url"] == "https://discord.test/webhook"

    # Modify config file (need different mtime)
    time.sleep(0.1)
    write_config(tmp_path, webhook_url="https://discord.test/new-webhook")

    config2 = mod.load_config()
    assert config2["webhook_url"] == "https://discord.test/new-webhook"


def test_rate_limit_prevents_spam(tmp_path: Path) -> None:
    write_config(tmp_path)
    mod = load_notifier(tmp_path)

    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = MagicMock()
        result1 = mod.notify("session_start", "First")
        result2 = mod.notify("session_start", "Second (should be rate-limited)")

    assert result1 is True
    assert result2 is False
    assert mock_urlopen.call_count == 1


def test_process_event_files(tmp_path: Path) -> None:
    write_config(tmp_path)
    mod = load_notifier(tmp_path)

    events_dir = tmp_path / "events"
    event = {"event": "session_start", "message": "Test event", "timestamp": "2026-01-01T00:00:00Z"}
    (events_dir / "20260101-000000-000000_session_start.json").write_text(json.dumps(event))

    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = MagicMock()
        mod.process_event_files()

    # File should be deleted after processing
    assert len(list(events_dir.glob("*.json"))) == 0
    mock_urlopen.assert_called_once()
