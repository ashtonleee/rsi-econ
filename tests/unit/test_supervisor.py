"""Tests for supervisor process management (git via bridge API)."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from unittest.mock import MagicMock


ROOT = Path(__file__).resolve().parents[2]
SUPERVISOR_PATH = ROOT / "sandbox" / "supervisor.py"


def load_supervisor(tmp_path: Path):
    os.environ["RSI_AGENT_WORKSPACE"] = str(tmp_path)
    os.environ["WALLET_URL"] = "http://bridge:8081"
    parent = tmp_path.parent
    backup_dir = parent / f"_backups_{tmp_path.name}"
    backup_dir.mkdir(exist_ok=True)
    os.environ["RSI_BACKUP_DIR"] = str(backup_dir)
    os.environ["RSI_BASELINE_DIR"] = str(parent / f"_baseline_{tmp_path.name}")
    os.environ["RSI_EVENTS_DIR"] = str(parent / f"_events_{tmp_path.name}")
    spec = importlib.util.spec_from_file_location(f"test_supervisor_{tmp_path.name}", SUPERVISOR_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _mock_bridge_ok(method, path, payload=None):
    """Default mock that succeeds for all bridge requests."""
    if path == "/git/init":
        return {"status": "initialized", "hash": "abc123"}
    if path == "/git/commit":
        return {"changed": True, "hash": "def456"}
    if path == "/git/fsck":
        return {"ok": True}
    if "/git/revert" in path:
        return {"hash": "reverted", "ref": "HEAD~1"}
    if path == "/git/push":
        return {"status": "no_remote"}
    if path == "/git/diff":
        return {"diff": ""}
    return {"status": "ok"}


def test_ensure_repo_calls_bridge(tmp_path: Path) -> None:
    mod = load_supervisor(tmp_path)
    mod._bridge_request = _mock_bridge_ok
    assert mod.ensure_repo() is True


def test_ensure_repo_fails_on_bridge_error(tmp_path: Path) -> None:
    mod = load_supervisor(tmp_path)
    mod._bridge_request = lambda m, p, pl=None: None
    assert mod.ensure_repo() is False


def test_commit_restart_with_changes(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("print('hello')\n")
    (tmp_path / ".restart_requested").touch()
    mod = load_supervisor(tmp_path)
    mod._bridge_request = _mock_bridge_ok
    result = mod.commit_restart()
    assert result is True


def test_commit_restart_no_changes(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("print('hello')\n")
    (tmp_path / ".restart_requested").touch()
    mod = load_supervisor(tmp_path)
    mod._bridge_request = lambda m, p, pl=None: {"changed": False, "hash": "abc"}
    result = mod.commit_restart()
    assert result is False


def test_revert_last_commit(tmp_path: Path) -> None:
    mod = load_supervisor(tmp_path)
    mod._bridge_request = _mock_bridge_ok
    assert mod.revert_last_commit() is True


def test_try_git_push_no_remote(tmp_path: Path) -> None:
    mod = load_supervisor(tmp_path)
    mod._bridge_request = _mock_bridge_ok
    mod.try_git_push()  # should not raise


def test_launch_agent_clean_exit(tmp_path: Path) -> None:
    mod = load_supervisor(tmp_path)
    mod._bridge_request = _mock_bridge_ok

    fake_proc = MagicMock()
    fake_proc.poll.return_value = 0
    mod.POPEN = lambda *a, **kw: fake_proc

    result = mod.launch_agent()
    assert result == 0
