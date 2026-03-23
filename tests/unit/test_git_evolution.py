"""Tests for git evolutionary substrate: branching, tagging, per-commit stats, and branch metadata."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WALLET_API_PATH = ROOT / "trusted" / "bridge" / "wallet_api.py"


def _load_git_manager_class(tmp_path: Path):
    """Load GitManager from wallet_api.py by executing the class source."""
    import re
    import shutil
    from datetime import datetime, timezone
    from typing import Any

    source = WALLET_API_PATH.read_text(encoding="utf-8")
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
    import logging
    exec_globals: dict = {
        "__builtins__": __builtins__,
        "os": os,
        "Path": Path,
        "subprocess": subprocess,
        "shutil": shutil,
        "json": json,
        "re": re,
        "Any": Any,
        "datetime": datetime,
        "timezone": timezone,
        "LOGGER": logging.getLogger("test_git_evolution"),
    }
    # Prepend annotations import for Python 3.9 compat (Path | None syntax)
    class_source = "from __future__ import annotations\n" + class_source
    exec(compile(class_source, str(WALLET_API_PATH), "exec"), exec_globals)
    return exec_globals["GitManager"]


@pytest.fixture()
def git_env(tmp_path: Path):
    """Create a git repo with session branch, mimicking bridge setup."""
    GitManager = _load_git_manager_class(tmp_path)
    repo_dir = tmp_path / "git-repo"
    workspace_dir = tmp_path / "workspace"
    seed_dir = tmp_path / "seed"
    stats_dir = tmp_path / "git-stats"

    repo_dir.mkdir()
    workspace_dir.mkdir()
    seed_dir.mkdir()
    (seed_dir / "main.py").write_text("print('seed')\n")
    (seed_dir / "SYSTEM.md").write_text("# System\n")

    subprocess.run(["git", "config", "--global", "user.email", "test@test.com"],
                   capture_output=True, check=False)
    subprocess.run(["git", "config", "--global", "user.name", "test"],
                   capture_output=True, check=False)

    gm = GitManager(repo_dir=repo_dir, workspace_dir=workspace_dir,
                     seed_dir=seed_dir, stats_dir=stats_dir)
    gm.init_repo()

    # Create a session branch (mimics cli/session.py new)
    env = {"GIT_DIR": str(repo_dir / ".git"), "GIT_WORK_TREE": str(workspace_dir),
           **os.environ}
    subprocess.run(["git", "checkout", "-b", "session/test-run"], env=env,
                   capture_output=True, check=True)

    return gm, workspace_dir, stats_dir


# --- Branch helper tests ---


def test_current_branch(git_env):
    gm, _, _ = git_env
    assert gm.current_branch() == "session/test-run"


def test_session_root(git_env):
    gm, _, _ = git_env
    assert gm._session_root() == "session/test-run"


def test_session_root_from_exp_branch(git_env):
    gm, _, _ = git_env
    gm.create_branch("session/test-run--exp/foo")
    assert gm._session_root() == "session/test-run"


def test_validate_namespace_valid(git_env):
    gm, _, _ = git_env
    assert gm._validate_branch_namespace("session/test-run") is None
    assert gm._validate_branch_namespace("session/test-run--exp/foo") is None


def test_validate_namespace_invalid(git_env):
    gm, _, _ = git_env
    err = gm._validate_branch_namespace("session/other--exp/foo")
    assert err is not None
    assert "outside session namespace" in err


# --- Branch creation tests ---


def test_create_branch_valid(git_env):
    gm, _, _ = git_env
    result = gm.create_branch("session/test-run--exp/new-compaction")
    assert result["status"] == "created"
    assert result["branch"] == "session/test-run--exp/new-compaction"
    assert gm.current_branch() == "session/test-run--exp/new-compaction"


def test_create_branch_invalid_prefix(git_env):
    gm, _, _ = git_env
    result = gm.create_branch("session/other--exp/foo")
    assert "error" in result
    assert "must start with" in result["error"]


def test_create_branch_no_exp(git_env):
    gm, _, _ = git_env
    result = gm.create_branch("session/test-run/foo")
    assert "error" in result
    assert "must start with" in result["error"]


def test_create_branch_invalid_chars(git_env):
    gm, _, _ = git_env
    result = gm.create_branch("session/test-run--exp/bad name!")
    assert "error" in result
    assert "must be non-empty" in result["error"]


def test_create_branch_empty_suffix(git_env):
    gm, _, _ = git_env
    result = gm.create_branch("session/test-run--exp/")
    assert "error" in result


# --- Checkout tests ---


def test_checkout_within_namespace(git_env):
    gm, workspace, _ = git_env
    # Create experiment branch and modify file
    gm.create_branch("session/test-run--exp/experiment-1")
    (workspace / "main.py").write_text("print('experiment')\n")
    gm.commit("experiment change")

    # Checkout back to session root
    result = gm.checkout("session/test-run")
    assert result["status"] == "checked_out"
    assert result["restart"] is True
    assert gm.current_branch() == "session/test-run"
    # Workspace should reflect original code
    assert "seed" in (workspace / "main.py").read_text()


def test_checkout_writes_restart_marker(git_env):
    gm, workspace, _ = git_env
    gm.create_branch("session/test-run--exp/test")
    result = gm.checkout("session/test-run")
    assert (workspace / ".restart_requested").exists()
    assert (workspace / ".restart_requested").read_text() == "checkout"


def test_checkout_outside_namespace(git_env):
    gm, _, _ = git_env
    result = gm.checkout("main")
    assert "error" in result
    assert "outside session namespace" in result["error"]


def test_checkout_nonexistent(git_env):
    gm, _, _ = git_env
    result = gm.checkout("session/test-run--exp/nope")
    assert "error" in result
    assert "does not exist" in result["error"]


# --- Merge tests ---


def test_merge_success(git_env):
    gm, workspace, _ = git_env
    # Create experiment branch with a change
    gm.create_branch("session/test-run--exp/good-change")
    (workspace / "new_file.py").write_text("# new feature\n")
    gm.commit("add feature")

    # Switch back to session root and merge
    gm.checkout("session/test-run")
    # Clean up restart marker from checkout
    (workspace / ".restart_requested").unlink(missing_ok=True)

    result = gm.merge("session/test-run--exp/good-change")
    assert result["status"] == "merged"
    assert len(result["hash"]) > 6
    # File should be present after merge
    assert (workspace / "new_file.py").exists()


def test_merge_conflict_aborts(git_env):
    gm, workspace, _ = git_env
    # Create experiment branch FIRST (from current state)
    gm.create_branch("session/test-run--exp/conflict")
    # Make a change on the experiment branch
    (workspace / "main.py").write_text("print('experiment version')\n")
    gm.commit("experiment change")

    # Switch back to session root and make a DIFFERENT change to the same file
    gm.checkout("session/test-run")
    (workspace / ".restart_requested").unlink(missing_ok=True)
    (workspace / "main.py").write_text("print('session version')\n")
    gm.commit("session change")

    # Now merge — both branches changed main.py differently from the same base
    result = gm.merge("session/test-run--exp/conflict")
    assert result["status"] == "conflict"
    assert "CONFLICT" in result["message"]
    # Workspace should be clean (merge aborted)
    content = (workspace / "main.py").read_text()
    assert "<<<" not in content  # No conflict markers


def test_merge_outside_namespace(git_env):
    gm, _, _ = git_env
    result = gm.merge("main")
    assert "error" in result


# --- List branches ---


def test_list_branches(git_env):
    gm, _, _ = git_env
    gm.create_branch("session/test-run--exp/alpha")
    gm.checkout("session/test-run")
    gm.create_branch("session/test-run--exp/beta")

    result = gm.list_branches()
    assert "session/test-run" in result["branches"]
    assert "session/test-run--exp/alpha" in result["branches"]
    assert "session/test-run--exp/beta" in result["branches"]
    assert result["current"] == "session/test-run--exp/beta"


# --- Delete branch ---


def test_delete_branch(git_env):
    gm, _, _ = git_env
    gm.create_branch("session/test-run--exp/to-delete")
    gm.checkout("session/test-run")

    result = gm.delete_branch("session/test-run--exp/to-delete")
    assert result["status"] == "deleted"

    branches = gm.list_branches()
    assert "session/test-run--exp/to-delete" not in branches["branches"]


def test_delete_current_branch_fails(git_env):
    gm, _, _ = git_env
    result = gm.delete_branch("session/test-run")
    assert "error" in result
    assert "cannot delete" in result["error"]


# --- Tag tests ---


def test_tag_create(git_env):
    gm, _, _ = git_env
    result = gm.tag("stable-v1")
    assert result["tag"] == "stable-v1"
    assert len(result["ref"]) > 6


def test_tag_list(git_env):
    gm, _, _ = git_env
    gm.tag("stable-v1")
    gm.tag("stable-v2")
    tags = gm.list_tags()
    names = [t["name"] for t in tags]
    assert "stable-v1" in names
    assert "stable-v2" in names


def test_tag_specific_ref(git_env):
    gm, workspace, _ = git_env
    (workspace / "main.py").write_text("print('v2')\n")
    gm.commit("v2")
    first_hash = gm.log()[-1]["hash"]
    result = gm.tag("at-seed", first_hash)
    assert result["ref"] == first_hash


# --- Per-commit stats tests ---


def test_commit_writes_stats_to_workspace(git_env):
    gm, workspace, _ = git_env
    (workspace / "main.py").write_text("print('changed')\n")
    wallet = {"budget_usd": 5.0, "spent_usd": 1.0, "remaining_usd": 4.0,
              "total_requests": 10, "avg_cost_per_request": 0.1}
    result = gm.commit("test edit", wallet_state=wallet)
    assert result["changed"]

    stats_dir = workspace / ".git-stats"
    assert stats_dir.exists()
    stats_files = list(stats_dir.glob("*.json"))
    assert len(stats_files) >= 1

    stats = json.loads(stats_files[0].read_text())
    assert stats["hash"] == result["hash"]
    assert stats["source"] == "agent_edit"
    assert stats["wallet"]["budget_usd"] == 5.0
    assert stats["wallet"]["remaining_usd"] == 4.0


def test_commit_writes_stats_to_bridge_dir(git_env):
    gm, workspace, stats_dir = git_env
    (workspace / "main.py").write_text("print('v2')\n")
    result = gm.commit("edit", wallet_state={"budget_usd": 5, "spent_usd": 0,
                        "remaining_usd": 5, "total_requests": 0, "avg_cost_per_request": 0})

    bridge_file = stats_dir / f"{result['hash']}.json"
    assert bridge_file.exists()
    stats = json.loads(bridge_file.read_text())
    assert stats["hash"] == result["hash"]


def test_stats_content_structure(git_env):
    gm, workspace, _ = git_env
    (workspace / "main.py").write_text("print('v3')\n")
    wallet = {"budget_usd": 10, "spent_usd": 2.5, "remaining_usd": 7.5,
              "total_requests": 50, "avg_cost_per_request": 0.05}
    result = gm.commit("structured test", wallet_state=wallet, source="agent_edit")

    stats_file = workspace / ".git-stats" / f"{result['hash']}.json"
    stats = json.loads(stats_file.read_text())

    assert "hash" in stats
    assert "short_hash" in stats
    assert "timestamp" in stats
    assert "branch" in stats
    assert "message" in stats
    assert "source" in stats
    assert "wallet" in stats
    assert "diff_stat" in stats
    assert stats["branch"] == "session/test-run"
    assert stats["message"] == "structured test"
    assert stats["source"] == "agent_edit"
    assert stats["diff_stat"]["files_changed"] >= 1


def test_stats_source_field(git_env):
    gm, workspace, _ = git_env
    (workspace / "main.py").write_text("print('custom source')\n")
    result = gm.commit("custom", source="supervisor_revert")

    stats_file = workspace / ".git-stats" / f"{result['hash']}.json"
    stats = json.loads(stats_file.read_text())
    assert stats["source"] == "supervisor_revert"


def test_stats_without_wallet(git_env):
    gm, workspace, _ = git_env
    (workspace / "main.py").write_text("print('no wallet')\n")
    result = gm.commit("no wallet test")

    stats_file = workspace / ".git-stats" / f"{result['hash']}.json"
    stats = json.loads(stats_file.read_text())
    assert stats["wallet"] is None


def test_no_stats_on_noop_commit(git_env):
    gm, workspace, _ = git_env
    result = gm.commit("noop")
    assert result["changed"] is False
    stats_dir = workspace / ".git-stats"
    # Stats dir may exist from init, but should have no files for this commit
    if stats_dir.exists():
        stats_files = [f for f in stats_dir.glob("*.json") if f.name.startswith(result["hash"])]
        assert len(stats_files) == 0


def test_stats_failure_does_not_block_commit(git_env, tmp_path):
    """Stats write failure should not prevent the commit."""
    _load = _load_git_manager_class(tmp_path)
    repo_dir = tmp_path / "repo2"
    workspace_dir = tmp_path / "ws2"
    seed_dir = tmp_path / "seed2"
    bad_stats_dir = tmp_path / "bad-stats"

    repo_dir.mkdir()
    workspace_dir.mkdir()
    seed_dir.mkdir()
    (seed_dir / "main.py").write_text("x\n")

    gm = _load(repo_dir=repo_dir, workspace_dir=workspace_dir,
                seed_dir=seed_dir, stats_dir=bad_stats_dir)
    gm.init_repo()

    env = {"GIT_DIR": str(repo_dir / ".git"), "GIT_WORK_TREE": str(workspace_dir), **os.environ}
    subprocess.run(["git", "checkout", "-b", "session/s"], env=env, capture_output=True, check=True)

    # Make stats_dir a file instead of directory to cause write failure
    bad_stats_dir.rmdir()
    bad_stats_dir.write_text("not a directory")

    (workspace_dir / "main.py").write_text("y\n")
    result = gm.commit("should still work")
    assert result["changed"] is True
    assert len(result["hash"]) > 6


# --- Diff stat parsing ---


def test_parse_diff_stat(git_env):
    gm, _, _ = git_env
    assert gm._parse_diff_stat("") == {"files_changed": 0, "insertions": 0, "deletions": 0}
    assert gm._parse_diff_stat(
        " main.py | 2 +-\n 1 file changed, 1 insertion(+), 1 deletion(-)\n"
    ) == {"files_changed": 1, "insertions": 1, "deletions": 1}
    assert gm._parse_diff_stat(
        " a.py | 10 ++++++++++\n b.py | 5 -----\n 2 files changed, 10 insertions(+), 5 deletions(-)\n"
    ) == {"files_changed": 2, "insertions": 10, "deletions": 5}
    assert gm._parse_diff_stat(
        " new.py | 3 +++\n 1 file changed, 3 insertions(+)\n"
    ) == {"files_changed": 1, "insertions": 3, "deletions": 0}


# --- Branch metadata tests ---


def test_branch_metadata_created_on_branch(git_env):
    gm, workspace, stats_dir = git_env
    gm.create_branch("session/test-run--exp/meta-test")

    # Check workspace copy
    ws_meta = workspace / ".git-stats" / "branches" / "session_test-run--exp_meta-test.json"
    assert ws_meta.exists()
    meta = json.loads(ws_meta.read_text())
    assert meta["branch"] == "session/test-run--exp/meta-test"
    assert meta["parent_branch"] == "session/test-run"
    assert meta["status"] == "active"
    assert meta["commits"] == 0

    # Check bridge copy
    bridge_meta = stats_dir / "branches" / "session_test-run--exp_meta-test.json"
    assert bridge_meta.exists()


def test_branch_metadata_updated_on_merge(git_env):
    gm, workspace, stats_dir = git_env
    gm.create_branch("session/test-run--exp/to-merge")
    (workspace / "feature.py").write_text("# feature\n")
    gm.commit("add feature")
    gm.checkout("session/test-run")
    (workspace / ".restart_requested").unlink(missing_ok=True)

    gm.merge("session/test-run--exp/to-merge")

    meta_path = stats_dir / "branches" / "session_test-run--exp_to-merge.json"
    meta = json.loads(meta_path.read_text())
    assert meta["status"] == "merged"
    assert meta["merged_at"] is not None


def test_branch_metadata_updated_on_delete(git_env):
    gm, workspace, stats_dir = git_env
    gm.create_branch("session/test-run--exp/to-abandon")
    gm.checkout("session/test-run")
    (workspace / ".restart_requested").unlink(missing_ok=True)

    gm.delete_branch("session/test-run--exp/to-abandon")

    meta_path = stats_dir / "branches" / "session_test-run--exp_to-abandon.json"
    meta = json.loads(meta_path.read_text())
    assert meta["status"] == "abandoned"
    assert meta["abandoned_at"] is not None


def test_branch_metadata_commit_count(git_env):
    gm, workspace, stats_dir = git_env
    gm.create_branch("session/test-run--exp/counting")
    (workspace / "main.py").write_text("print('edit1')\n")
    gm.commit("edit 1")
    (workspace / "main.py").write_text("print('edit2')\n")
    gm.commit("edit 2")

    meta_path = stats_dir / "branches" / "session_test-run--exp_counting.json"
    meta = json.loads(meta_path.read_text())
    assert meta["commits"] == 2
