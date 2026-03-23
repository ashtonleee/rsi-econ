"""Tests for supervisor using bridge git API instead of local git."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from unittest.mock import MagicMock


ROOT = Path(__file__).resolve().parents[2]
SUPERVISOR_PATH = ROOT / "sandbox" / "supervisor.py"


def load_supervisor(tmp_path: Path):
    os.environ["RSI_AGENT_WORKSPACE"] = str(tmp_path)
    os.environ["WALLET_URL"] = "http://bridge:8081"
    backup_dir = tmp_path.parent / f"_backups_{tmp_path.name}"
    backup_dir.mkdir(exist_ok=True)
    os.environ["RSI_BACKUP_DIR"] = str(backup_dir)
    os.environ["RSI_BASELINE_DIR"] = str(tmp_path.parent / f"_baseline_{tmp_path.name}")
    os.environ["RSI_EVENTS_DIR"] = str(tmp_path.parent / f"_events_{tmp_path.name}")
    spec = importlib.util.spec_from_file_location(f"test_sv_bridge_{tmp_path.name}", SUPERVISOR_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_ensure_repo_calls_bridge_init(tmp_path: Path) -> None:
    mod = load_supervisor(tmp_path)
    calls = []

    def mock_bridge(method, path, payload=None):
        calls.append((method, path, payload))
        return {"status": "initialized", "hash": "abc123"}

    mod._bridge_request = mock_bridge
    result = mod.ensure_repo()

    assert result is True
    assert len(calls) == 1
    assert calls[0] == ("POST", "/git/init", None)


def test_ensure_repo_fails_on_bridge_error(tmp_path: Path) -> None:
    mod = load_supervisor(tmp_path)
    mod._bridge_request = lambda method, path, payload=None: None

    result = mod.ensure_repo()
    assert result is False
    assert (tmp_path / ".paused").exists()


def test_commit_restart_calls_bridge_commit(tmp_path: Path) -> None:
    mod = load_supervisor(tmp_path)
    (tmp_path / "main.py").write_text("print('hello')\n")
    (tmp_path / ".restart_requested").touch()

    calls = []

    def mock_bridge(method, path, payload=None):
        calls.append((method, path))
        if path == "/git/commit":
            return {"changed": True, "hash": "def456"}
        if path == "/git/fsck":
            return {"ok": True}
        return {"status": "ok"}

    mod._bridge_request = mock_bridge
    result = mod.commit_restart()

    assert result is True
    commit_calls = [c for c in calls if c[1] == "/git/commit"]
    assert len(commit_calls) == 1


def test_commit_restart_no_changes(tmp_path: Path) -> None:
    mod = load_supervisor(tmp_path)
    (tmp_path / "main.py").write_text("print('hello')\n")
    (tmp_path / ".restart_requested").touch()

    mod._bridge_request = lambda method, path, payload=None: {"changed": False, "hash": "abc"}
    result = mod.commit_restart()
    assert result is False


def test_commit_restart_reverts_syntax_error(tmp_path: Path) -> None:
    mod = load_supervisor(tmp_path)
    (tmp_path / "main.py").write_text("def broken(\n")  # syntax error
    (tmp_path / ".restart_requested").touch()

    calls = []

    def mock_bridge(method, path, payload=None):
        calls.append((method, path))
        if path == "/git/commit":
            return {"changed": True, "hash": "bad123"}
        if "/git/revert" in path:
            return {"hash": "reverted", "ref": "HEAD~1"}
        return {"ok": True}

    mod._bridge_request = mock_bridge
    result = mod.commit_restart()

    assert result is False  # prevented crash, not None
    revert_calls = [c for c in calls if "/git/revert" in c[1]]
    assert len(revert_calls) == 1


def test_revert_calls_bridge(tmp_path: Path) -> None:
    mod = load_supervisor(tmp_path)
    calls = []

    def mock_bridge(method, path, payload=None):
        calls.append((method, path))
        return {"hash": "reverted", "ref": "HEAD~1"}

    mod._bridge_request = mock_bridge
    result = mod.revert_last_commit()

    assert result is True
    assert any("/git/revert" in c[1] for c in calls)


def test_try_git_push_calls_bridge(tmp_path: Path) -> None:
    mod = load_supervisor(tmp_path)
    calls = []

    def mock_bridge(method, path, payload=None):
        calls.append((method, path))
        return {"status": "pushed"}

    mod._bridge_request = mock_bridge
    mod.try_git_push()

    assert any("/git/push" in c[1] for c in calls)
