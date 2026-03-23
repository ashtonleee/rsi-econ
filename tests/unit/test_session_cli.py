"""Tests for cli/session.py session management."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


ROOT = Path(__file__).resolve().parents[2]
SESSION_CLI_PATH = ROOT / "cli" / "session.py"


def load_session_cli():
    spec = importlib.util.spec_from_file_location("session_cli", SESSION_CLI_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def cli():
    return load_session_cli()


@pytest.fixture()
def git_repo(tmp_path: Path):
    """Create a minimal git repo to simulate sandbox/seed with a main branch."""
    subprocess.run(["git", "init", str(tmp_path)], capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(tmp_path), capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "test"],
        cwd=str(tmp_path), capture_output=True, check=True,
    )
    (tmp_path / "main.py").write_text("# seed\n")
    subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "seed"],
        cwd=str(tmp_path), capture_output=True, check=True,
    )
    # Rename default branch to main
    subprocess.run(
        ["git", "branch", "-M", "main"],
        cwd=str(tmp_path), capture_output=True, check=True,
    )
    return tmp_path


def test_status_formats_output(cli, git_repo, capsys) -> None:
    """cmd_status prints session info without crashing."""
    import argparse

    cli.SEED_DIR = git_repo

    def fake_compose(*args):
        r = MagicMock()
        r.returncode = 1
        r.stdout = ""
        r.stderr = ""
        return r

    with patch.object(cli, "docker_compose", fake_compose):
        result = cli.cmd_status(argparse.Namespace())

    assert result == 0
    output = capsys.readouterr().out
    assert "Session:" in output
    assert "Status:" in output


def test_new_creates_session_branch(cli, git_repo) -> None:
    import argparse

    cli.SEED_DIR = git_repo

    def fake_compose(*args):
        r = MagicMock()
        r.returncode = 0
        r.stdout = ""
        r.stderr = ""
        return r

    with patch.object(cli, "docker_compose", fake_compose):
        result = cli.cmd_new(argparse.Namespace(name="test1"))

    assert result == 0
    # Check branch exists with session/ prefix
    branches = subprocess.run(
        ["git", "branch", "--list"],
        cwd=str(git_repo), capture_output=True, text=True, check=False,
    )
    assert "session/test1" in branches.stdout


def test_list_shows_session_branches(cli, git_repo, capsys) -> None:
    import argparse

    cli.SEED_DIR = git_repo

    # Create a session branch
    subprocess.run(
        ["git", "checkout", "-b", "session/demo"],
        cwd=str(git_repo), capture_output=True, check=True,
    )
    (git_repo / "main.py").write_text("# edited\n")
    subprocess.run(["git", "add", "-A"], cwd=str(git_repo), capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "edit"],
        cwd=str(git_repo), capture_output=True, check=True,
    )

    result = cli.cmd_list(argparse.Namespace())
    assert result == 0
    output = capsys.readouterr().out
    assert "session/demo" in output


def test_fork_creates_branch_from_source(cli, git_repo) -> None:
    import argparse

    cli.SEED_DIR = git_repo

    # Create source branch
    subprocess.run(
        ["git", "checkout", "-b", "session/source"],
        cwd=str(git_repo), capture_output=True, check=True,
    )
    (git_repo / "main.py").write_text("# source code\n")
    subprocess.run(["git", "add", "-A"], cwd=str(git_repo), capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "source commit"],
        cwd=str(git_repo), capture_output=True, check=True,
    )

    def fake_compose(*args):
        r = MagicMock()
        r.returncode = 0
        r.stdout = ""
        r.stderr = ""
        return r

    with patch.object(cli, "docker_compose", fake_compose):
        result = cli.cmd_fork(argparse.Namespace(branch="session/source", name="forked1"))

    assert result == 0
    # Verify fork branch exists and has the source content
    show = subprocess.run(
        ["git", "show", "session/forked1:main.py"],
        cwd=str(git_repo), capture_output=True, text=True, check=False,
    )
    assert "source code" in show.stdout


def test_push_fails_without_remote(cli, git_repo, capsys) -> None:
    import argparse

    cli.SEED_DIR = git_repo

    result = cli.cmd_push(argparse.Namespace())
    assert result == 1
    output = capsys.readouterr().err
    assert "no 'origin' remote" in output
