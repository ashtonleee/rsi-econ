import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.fast


def run(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def copy_script(repo_root: Path, relative_path: str) -> Path:
    source = ROOT / relative_path
    target = repo_root / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    target.chmod(0o755)
    return target


def init_repo(tmp_path: Path) -> Path:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "runtime").mkdir()
    (repo_root / "README.md").write_text("# demo\n", encoding="ascii")
    (repo_root / "STAGE_STATUS.md").write_text(
        "# STAGE_STATUS.md\n\n"
        "## Session Log\n\n"
        "| Date | Agent | Summary |\n"
        "|------|-------|---------|\n",
        encoding="ascii",
    )
    copy_script(repo_root, "finish")
    copy_script(repo_root, "scripts/finish_task.py")
    copy_script(repo_root, "scripts/post_task_update.py")

    assert run(["git", "init"], cwd=repo_root).returncode == 0
    assert run(["git", "config", "user.name", "Codex Tests"], cwd=repo_root).returncode == 0
    assert run(["git", "config", "user.email", "codex-tests@example.com"], cwd=repo_root).returncode == 0
    assert run(["git", "add", "."], cwd=repo_root).returncode == 0
    commit = run(["git", "commit", "-m", "baseline"], cwd=repo_root)
    assert commit.returncode == 0, commit.stderr
    return repo_root


def test_root_finish_command_runs_shared_closeout_path(tmp_path: Path):
    repo_root = init_repo(tmp_path)

    result = run(
        [
            "./finish",
            "--summary",
            "Closed the bounded task with the shared command.",
            "--changed-file",
            "finish",
            "--test",
            "passed::python -m pytest tests/unit/test_finish_command.py -q --tb=short",
        ],
        cwd=repo_root,
    )

    assert result.returncode == 0, result.stderr
    status_text = (repo_root / "STAGE_STATUS.md").read_text(encoding="utf-8")
    assert "Closed the bounded task with the shared command." in status_text
    packet_text = (repo_root / "runtime" / "post_task_updates" / "latest_linear_update.md").read_text(
        encoding="utf-8"
    )
    assert "- finish" in packet_text
