import json
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "post_task_update.py"

pytestmark = pytest.mark.fast


def run(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def packet_path(repo_root: Path) -> Path:
    return repo_root / "runtime" / "post_task_updates" / "latest_linear_update.md"


def init_repo(tmp_path: Path, *, nested: bool = False, stage_status_text: str | None = None) -> Path:
    if nested:
        repo_root = tmp_path / "outer" / "rsi-econ"
        repo_root.parent.mkdir(parents=True)
    else:
        repo_root = tmp_path / "repo"
    repo_root.mkdir(exist_ok=True)
    (repo_root / "runtime").mkdir()
    (repo_root / "README.md").write_text("# demo\n", encoding="ascii")
    (repo_root / "STAGE_STATUS.md").write_text(
        stage_status_text
        or (
            "# STAGE_STATUS.md\n\n"
            "## Session Log\n\n"
            "| Date | Agent | Summary |\n"
            "|------|-------|---------|\n"
        ),
        encoding="ascii",
    )

    result = run(["git", "init"], cwd=repo_root)
    assert result.returncode == 0, result.stderr
    assert run(["git", "config", "user.name", "Codex Tests"], cwd=repo_root).returncode == 0
    assert run(["git", "config", "user.email", "codex-tests@example.com"], cwd=repo_root).returncode == 0
    assert run(["git", "add", "."], cwd=repo_root).returncode == 0
    commit = run(["git", "commit", "-m", "baseline"], cwd=repo_root)
    assert commit.returncode == 0, commit.stderr
    return repo_root


def write_payload(path: Path, payload: dict[str, object]) -> Path:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def base_payload() -> dict[str, object]:
    return {
        "task_ref": "plan 0013",
        "agent": "codex-gpt-5",
        "summary": "Repaired the post-task hook.",
        "changed_files": [
            "scripts/post_task_update.py",
            "tests/unit/test_post_task_update.py",
        ],
        "tests_run": [
            {
                "command": "python -m pytest tests/unit/test_post_task_update.py -q --tb=short",
                "status": "passed",
            }
        ],
    }


def run_hook(repo_root: Path, payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
    payload_path = write_payload(repo_root / "task_result.json", payload)
    return run(
        [
            sys.executable,
            str(SCRIPT),
            "--result-file",
            str(payload_path),
        ],
        cwd=repo_root,
    )


def test_post_task_update_writes_packet_only_from_explicit_payload(tmp_path: Path):
    repo_root = init_repo(tmp_path)
    payload = base_payload()
    payload["follow_ups"] = [
        {
            "title": "Optionally add an explicit discovery mode later",
            "body": "Only if a caller can scope it tightly.",
        }
    ]
    (repo_root / "ambient_notes.txt").write_text("dirty\n", encoding="ascii")

    result = run_hook(repo_root, payload)

    assert result.returncode == 0, result.stderr
    status_text = (repo_root / "STAGE_STATUS.md").read_text(encoding="utf-8")
    assert "Repaired the post-task hook." in status_text
    assert "Verified `python -m pytest tests/unit/test_post_task_update.py -q --tb=short`." in status_text

    packet_text = packet_path(repo_root).read_text(encoding="utf-8")
    assert "- scripts/post_task_update.py" in packet_text
    assert "- tests/unit/test_post_task_update.py" in packet_text
    assert "ambient_notes.txt" not in packet_text
    assert "Optionally add an explicit discovery mode later" in packet_text
    assert "No stage-boundary trigger detected." in packet_text


def test_post_task_update_writes_no_packet_when_stage_status_update_fails(tmp_path: Path):
    repo_root = init_repo(
        tmp_path,
        stage_status_text="# STAGE_STATUS.md\n\n## Session Log\n\nbroken\n",
    )
    before = (repo_root / "STAGE_STATUS.md").read_text(encoding="utf-8")

    result = run_hook(repo_root, base_payload())

    assert result.returncode != 0
    assert "session-log table header" in result.stderr
    assert (repo_root / "STAGE_STATUS.md").read_text(encoding="utf-8") == before
    assert not packet_path(repo_root).exists()


def test_post_task_update_requires_explicit_changed_files_instead_of_ambient_git_state(tmp_path: Path):
    repo_root = init_repo(tmp_path)
    (repo_root / "README.md").write_text("# demo\n\nambient dirty change\n", encoding="ascii")
    (repo_root / "ambient_notes.txt").write_text("ambient\n", encoding="ascii")
    payload = base_payload()
    payload.pop("changed_files")
    before = (repo_root / "STAGE_STATUS.md").read_text(encoding="utf-8")

    result = run_hook(repo_root, payload)

    assert result.returncode != 0
    assert "requires a 'changed_files' list" in result.stderr
    assert (repo_root / "STAGE_STATUS.md").read_text(encoding="utf-8") == before
    assert not packet_path(repo_root).exists()


def test_post_task_update_normalizes_prefixed_paths_for_stage_boundary_detection(tmp_path: Path):
    repo_root = init_repo(tmp_path, nested=True)
    payload = base_payload()
    payload["changed_files"] = [
        str(repo_root / "ACCEPTANCE_TEST_MATRIX.md"),
        f"{repo_root.name}/assurance/REGISTRY.yaml",
        f"{repo_root.name}/scripts/post_task_update.py",
    ]

    result = run_hook(repo_root, payload)

    assert result.returncode == 0, result.stderr
    packet_text = packet_path(repo_root).read_text(encoding="utf-8")
    assert "- ACCEPTANCE_TEST_MATRIX.md" in packet_text
    assert "- assurance/REGISTRY.yaml" in packet_text
    assert "- scripts/post_task_update.py" in packet_text
    assert "Stage-boundary follow-up likely: changed files include Tier 2 paths" in packet_text


def test_post_task_update_requires_explicit_test_status(tmp_path: Path):
    repo_root = init_repo(tmp_path)
    payload = base_payload()
    payload["tests_run"] = [{"command": "python -m pytest tests/unit/test_post_task_update.py -q --tb=short"}]
    before = (repo_root / "STAGE_STATUS.md").read_text(encoding="utf-8")

    result = run_hook(repo_root, payload)

    assert result.returncode != 0
    assert "explicit non-empty 'status'" in result.stderr
    assert (repo_root / "STAGE_STATUS.md").read_text(encoding="utf-8") == before
    assert not packet_path(repo_root).exists()
