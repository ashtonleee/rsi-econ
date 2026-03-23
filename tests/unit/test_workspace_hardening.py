"""Tests for workspace corruption hardening in supervisor.py."""

from __future__ import annotations

import importlib.util
import os
import tarfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SUPERVISOR_PATH = ROOT / "sandbox" / "supervisor.py"


def load_supervisor(tmp_path: Path, baseline_dir: Path | None = None, backup_dir: Path | None = None):
    os.environ["RSI_AGENT_WORKSPACE"] = str(tmp_path)
    os.environ["WALLET_URL"] = "http://bridge:8081"
    os.environ["RSI_EVENTS_DIR"] = str(tmp_path.parent / f"_events_{tmp_path.name}")
    if baseline_dir is not None:
        os.environ["RSI_BASELINE_DIR"] = str(baseline_dir)
    else:
        os.environ.pop("RSI_BASELINE_DIR", None)
    if backup_dir is not None:
        os.environ["RSI_BACKUP_DIR"] = str(backup_dir)
    else:
        os.environ.pop("RSI_BACKUP_DIR", None)
    spec = importlib.util.spec_from_file_location(f"test_hardening_{tmp_path.name}", SUPERVISOR_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _mock_bridge_ok(method, path, payload=None):
    if path == "/git/commit":
        return {"changed": True, "hash": "abc123"}
    if path == "/git/fsck":
        return {"ok": True}
    if "/git/revert" in path:
        return {"hash": "reverted", "ref": "HEAD~1"}
    return {"status": "ok"}


def test_syntax_validation_passes_valid_python(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("x = 1\nprint(x)\n")
    mod = load_supervisor(tmp_path)
    assert mod.validate_agent_code() is True


def test_syntax_validation_catches_syntax_error(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("def foo(\n")
    mod = load_supervisor(tmp_path)
    assert mod.validate_agent_code() is False


def test_syntax_validation_catches_indent_error(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("def foo():\nreturn 1\n")
    mod = load_supervisor(tmp_path)
    assert mod.validate_agent_code() is False


def test_backup_creates_tarball(tmp_path: Path) -> None:
    backup_dir = tmp_path / "backups"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("print('hello')\n")

    mod = load_supervisor(workspace, backup_dir=backup_dir)
    mod.backup_workspace()

    tarballs = list(backup_dir.glob("workspace-*.tar.gz"))
    assert len(tarballs) == 1
    with tarfile.open(str(tarballs[0]), "r:gz") as tar:
        names = tar.getnames()
        assert any("main.py" in n for n in names)


def test_backup_rotation_keeps_10(tmp_path: Path) -> None:
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("print('hello')\n")

    for i in range(12):
        (backup_dir / f"workspace-20260101-{i:06d}.tar.gz").write_text("fake")

    mod = load_supervisor(workspace, backup_dir=backup_dir)
    mod.backup_workspace()

    tarballs = list(backup_dir.glob("workspace-*.tar.gz"))
    assert len(tarballs) == 10


def test_restore_from_baseline(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    baseline = tmp_path / "baseline"
    baseline.mkdir()

    (baseline / "main.py").write_text("# canonical seed\nprint('baseline')\n")
    (baseline / "SYSTEM.md").write_text("# baseline system prompt\n")
    (workspace / "main.py").write_text("CORRUPTED GARBAGE")

    mod = load_supervisor(workspace, baseline_dir=baseline)
    mod._bridge_request = _mock_bridge_ok
    assert mod.restore_from_baseline() is True
    assert "baseline" in (workspace / "main.py").read_text()
    assert (workspace / "SYSTEM.md").exists()


def test_restore_from_backup(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()

    (workspace / "main.py").write_text("print('good code')\n")

    mod = load_supervisor(workspace, backup_dir=backup_dir)
    mod._bridge_request = _mock_bridge_ok
    mod.backup_workspace()

    (workspace / "main.py").write_text("CORRUPTED")

    assert mod.restore_from_backup() is True
    assert "good code" in (workspace / "main.py").read_text()


def test_commit_restart_reverts_syntax_error(tmp_path: Path) -> None:
    backup_dir = tmp_path / "backups"
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    (workspace / "main.py").write_text("def broken(\n")
    (workspace / ".restart_requested").touch()

    revert_called = []

    def mock_bridge(method, path, payload=None):
        if path == "/git/commit":
            return {"changed": True, "hash": "bad123"}
        if "/git/revert" in path:
            # Simulate revert by restoring valid code
            (workspace / "main.py").write_text("print('reverted')\n")
            revert_called.append(True)
            return {"hash": "reverted", "ref": "HEAD~1"}
        return {"ok": True}

    mod = load_supervisor(workspace, backup_dir=backup_dir)
    mod._bridge_request = mock_bridge
    result = mod.commit_restart()

    assert result is False
    assert len(revert_called) == 1
