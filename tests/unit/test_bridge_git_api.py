"""Tests for bridge GitManager (trusted git repo management)."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WALLET_API_PATH = ROOT / "trusted" / "bridge" / "wallet_api.py"


def load_git_manager_class(tmp_path: Path):
    """Load just the GitManager class from wallet_api.py without creating the FastAPI app."""
    mod_name = f"wallet_api_git_{tmp_path.name}"
    spec = importlib.util.spec_from_file_location(mod_name, WALLET_API_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    # Set env vars to prevent FastAPI app creation from failing
    os.environ["PROPOSALS_DIR"] = str(tmp_path / "proposals")
    os.environ["LLM_USAGE_LOG_PATH"] = str(tmp_path / "usage.jsonl")
    os.environ["NOTIFICATION_CONFIG_PATH"] = str(tmp_path / "notif.json")
    os.environ["EVENTS_DIR"] = str(tmp_path / "events")
    os.environ["OPERATOR_MESSAGES_DIR"] = str(tmp_path / "op_msgs")
    (tmp_path / "notif.json").write_text('{"webhook_url":"","events":{}}')
    # We need to exec the module to get GitManager, but this also creates the app
    # which will fail on Python 3.9 due to type annotations in FastAPI routes.
    # Instead, extract GitManager from source directly.
    import types
    # Read just the GitManager class and its dependencies
    source = WALLET_API_PATH.read_text(encoding="utf-8")
    # Create a minimal module with just GitManager
    exec_globals: dict = {
        "__builtins__": __builtins__,
        "os": os,
        "Path": Path,
        "subprocess": subprocess,
        "shutil": __import__("shutil"),
        "Any": __import__("typing").Any,
    }
    # Extract the GitManager class source
    lines = source.split("\n")
    in_class = False
    class_lines = []
    for line in lines:
        if line.startswith("class GitManager:"):
            in_class = True
        if in_class:
            if line and not line[0].isspace() and not line.startswith("class GitManager"):
                break
            class_lines.append(line)
    class_source = "\n".join(class_lines)
    exec(compile(class_source, str(WALLET_API_PATH), "exec"), exec_globals)
    return exec_globals["GitManager"]


def make_git_manager(tmp_path: Path):
    GitManager = load_git_manager_class(tmp_path)
    repo_dir = tmp_path / "git-repo"
    workspace_dir = tmp_path / "workspace"
    seed_dir = tmp_path / "seed"
    repo_dir.mkdir()
    workspace_dir.mkdir()
    seed_dir.mkdir()
    (seed_dir / "main.py").write_text("print('hello seed')\n")
    (seed_dir / "SYSTEM.md").write_text("# System prompt\n")

    subprocess.run(["git", "config", "--global", "user.email", "test@test.com"],
                   capture_output=True, check=False)
    subprocess.run(["git", "config", "--global", "user.name", "test"],
                   capture_output=True, check=False)

    return GitManager(repo_dir=repo_dir, workspace_dir=workspace_dir, seed_dir=seed_dir), workspace_dir


def test_git_init_creates_repo(tmp_path: Path) -> None:
    gm, workspace = make_git_manager(tmp_path)
    result = gm.init_repo()
    assert result["status"] == "initialized"
    assert len(result["hash"]) > 6
    assert (workspace / "main.py").exists()
    assert "hello seed" in (workspace / "main.py").read_text()


def test_git_init_idempotent(tmp_path: Path) -> None:
    gm, workspace = make_git_manager(tmp_path)
    r1 = gm.init_repo()
    r2 = gm.init_repo()
    assert r2["status"] == "already_initialized"
    assert r1["hash"] == r2["hash"]


def test_git_commit(tmp_path: Path) -> None:
    gm, workspace = make_git_manager(tmp_path)
    gm.init_repo()
    (workspace / "main.py").write_text("print('edited')\n")
    result = gm.commit("test edit")
    assert result["changed"] is True
    assert len(result["hash"]) > 6


def test_git_commit_no_changes(tmp_path: Path) -> None:
    gm, workspace = make_git_manager(tmp_path)
    gm.init_repo()
    result = gm.commit("no changes")
    assert result["changed"] is False


def test_git_log(tmp_path: Path) -> None:
    gm, workspace = make_git_manager(tmp_path)
    gm.init_repo()
    (workspace / "main.py").write_text("print('v2')\n")
    gm.commit("second commit")
    entries = gm.log()
    assert len(entries) == 2
    assert entries[0]["message"] == "second commit"
    assert entries[1]["message"] == "seed"


def test_git_show(tmp_path: Path) -> None:
    gm, workspace = make_git_manager(tmp_path)
    gm.init_repo()
    content = gm.show("HEAD", "main.py")
    assert content is not None
    assert "hello seed" in content


def test_git_diff(tmp_path: Path) -> None:
    gm, workspace = make_git_manager(tmp_path)
    gm.init_repo()
    (workspace / "main.py").write_text("print('changed')\n")
    gm.commit("change")
    diff = gm.diff("HEAD~1", "HEAD")
    assert "changed" in diff


def test_git_revert(tmp_path: Path) -> None:
    gm, workspace = make_git_manager(tmp_path)
    gm.init_repo()
    original = (workspace / "main.py").read_text()
    (workspace / "main.py").write_text("print('bad edit')\n")
    gm.commit("bad edit")
    result = gm.revert("HEAD~1")
    assert "error" not in result
    assert (workspace / "main.py").read_text() == original


def test_git_fsck(tmp_path: Path) -> None:
    gm, workspace = make_git_manager(tmp_path)
    gm.init_repo()
    assert gm.fsck() is True
