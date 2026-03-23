from __future__ import annotations

import importlib.util
import os
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock


ROOT = Path(__file__).resolve().parents[2]
SUPERVISOR_PATH = ROOT / "sandbox" / "supervisor.py"


def load_supervisor(tmp_path: Path):
    os.environ["RSI_AGENT_WORKSPACE"] = str(tmp_path)
    # Set backup/baseline dirs OUTSIDE workspace so git add -A doesn't pick them up
    parent = tmp_path.parent
    backup_dir = parent / f"_backups_{tmp_path.name}"
    backup_dir.mkdir(exist_ok=True)
    os.environ["RSI_BACKUP_DIR"] = str(backup_dir)
    os.environ["RSI_BASELINE_DIR"] = str(parent / f"_baseline_{tmp_path.name}")
    spec = importlib.util.spec_from_file_location(f"test_supervisor_{tmp_path.name}", SUPERVISOR_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def init_git_repo(tmp_path: Path) -> None:
    subprocess.run(["git", "init", str(tmp_path)], capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(tmp_path), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=str(tmp_path), capture_output=True, check=True)
    (tmp_path / "main.py").write_text("# seed\n")
    subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=str(tmp_path), capture_output=True, check=True)


def test_ensure_repo_creates_git_repo(tmp_path: Path) -> None:
    mod = load_supervisor(tmp_path)
    (tmp_path / "main.py").write_text("# agent\n")
    assert mod.ensure_repo() is True
    assert (tmp_path / ".git").exists()


def test_ensure_repo_skips_if_exists(tmp_path: Path) -> None:
    init_git_repo(tmp_path)
    mod = load_supervisor(tmp_path)
    assert mod.ensure_repo() is True


def test_commit_restart_with_changes(tmp_path: Path) -> None:
    init_git_repo(tmp_path)
    mod = load_supervisor(tmp_path)
    (tmp_path / ".restart_requested").touch()
    (tmp_path / "main.py").write_text("# edited\n")
    result = mod.commit_restart()
    assert result is True


def test_commit_restart_no_changes(tmp_path: Path) -> None:
    init_git_repo(tmp_path)
    mod = load_supervisor(tmp_path)
    (tmp_path / ".restart_requested").touch()
    result = mod.commit_restart()
    assert result is False


def test_revert_last_commit(tmp_path: Path) -> None:
    init_git_repo(tmp_path)
    mod = load_supervisor(tmp_path)
    (tmp_path / "main.py").write_text("# crash\n")
    subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "bad"], cwd=str(tmp_path), capture_output=True, check=True)
    assert mod.revert_last_commit() is True
    content = (tmp_path / "main.py").read_text()
    assert content == "# seed\n"


def test_try_git_push_no_remote(tmp_path: Path) -> None:
    init_git_repo(tmp_path)
    mod = load_supervisor(tmp_path)
    # Should not raise — no remote configured
    mod.try_git_push()


def test_launch_agent_clean_exit(tmp_path: Path) -> None:
    init_git_repo(tmp_path)
    mod = load_supervisor(tmp_path)

    fake_proc = MagicMock()
    fake_proc.poll.return_value = 0
    mod.POPEN = lambda *a, **kw: fake_proc

    result = mod.launch_agent()
    assert result == 0
