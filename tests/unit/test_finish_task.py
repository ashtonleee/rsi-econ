import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "finish_task.py"

pytestmark = pytest.mark.fast

LINEAR_SITECUSTOMIZE = textwrap.dedent(
    """
    import io
    import json
    import os
    import urllib.request


    class _Response(io.StringIO):
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            self.close()
            return False


    def _urlopen(request, *args, **kwargs):
        payload = json.loads(request.data.decode("utf-8"))
        record = {
            "query": payload.get("query"),
            "variables": payload.get("variables"),
            "headers": {
                str(key).lower(): str(value)
                for key, value in request.header_items()
            },
        }
        log_path = os.environ["LINEAR_STUB_LOG"]
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\\n")

        query = str(payload.get("query", ""))
        if "query IssueById" in query:
            response = {
                "data": {
                    "issue": {
                        "id": os.environ.get("LINEAR_STUB_ISSUE_ID", "issue-1"),
                        "identifier": os.environ.get("LINEAR_STUB_ISSUE_IDENTIFIER", "ENG-123"),
                        "title": "Demo issue",
                    }
                }
            }
        elif "mutation CommentCreate" in query:
            response = {
                "data": {
                    "commentCreate": {
                        "success": True,
                        "comment": {"id": "comment-1"},
                    }
                }
            }
        else:
            response = {"errors": [{"message": "unexpected query"}]}

        return _Response(json.dumps(response))


    urllib.request.urlopen = _urlopen
    """
)


def run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def packet_path(repo_root: Path) -> Path:
    return repo_root / "runtime" / "post_task_updates" / "latest_linear_update.md"


def linear_requests(log_path: Path) -> list[dict[str, object]]:
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]


def init_repo(tmp_path: Path, *, stage_status_text: str | None = None) -> Path:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
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

    assert run(["git", "init"], cwd=repo_root).returncode == 0
    assert run(["git", "config", "user.name", "Codex Tests"], cwd=repo_root).returncode == 0
    assert run(["git", "config", "user.email", "codex-tests@example.com"], cwd=repo_root).returncode == 0
    assert run(["git", "add", "."], cwd=repo_root).returncode == 0
    commit = run(["git", "commit", "-m", "baseline"], cwd=repo_root)
    assert commit.returncode == 0, commit.stderr
    return repo_root


def linear_stub_env(tmp_path: Path, *, api_key: str | None = "linear-test-key", home_dir: Path | None = None) -> tuple[dict[str, str], Path]:
    stub_dir = tmp_path / "linear_stub"
    stub_dir.mkdir(exist_ok=True)
    (stub_dir / "sitecustomize.py").write_text(LINEAR_SITECUSTOMIZE, encoding="utf-8")
    log_path = stub_dir / "requests.jsonl"
    env = os.environ.copy()
    pythonpath = str(stub_dir)
    if env.get("PYTHONPATH"):
        pythonpath = f"{pythonpath}{os.pathsep}{env['PYTHONPATH']}"
    env["PYTHONPATH"] = pythonpath
    env["LINEAR_API_URL"] = "https://linear.invalid/graphql"
    env["LINEAR_STUB_LOG"] = str(log_path)
    if api_key is None:
        env.pop("LINEAR_API_KEY", None)
    else:
        env["LINEAR_API_KEY"] = api_key
    env.pop("LINEAR_TOKEN", None)
    if home_dir is not None:
        env["HOME"] = str(home_dir)
    return env, log_path


def base_args() -> list[str]:
    return [
        sys.executable,
        str(SCRIPT),
        "--summary",
        "Wrapped the post-task update flow.",
        "--changed-file",
        "scripts/finish_task.py",
        "--changed-file",
        "tests/unit/test_finish_task.py",
        "--test",
        "passed::python -m pytest tests/unit/test_finish_task.py -q --tb=short",
    ]


def test_finish_task_does_not_post_to_linear_if_canonical_update_fails(tmp_path: Path):
    repo_root = init_repo(
        tmp_path,
        stage_status_text="# STAGE_STATUS.md\n\n## Session Log\n\nbroken\n",
    )
    env, log_path = linear_stub_env(tmp_path)

    result = run(
        [*base_args(), "--task-ref", "ENG-123"],
        cwd=repo_root,
        env=env,
    )

    assert result.returncode != 0
    assert linear_requests(log_path) == []
    assert not packet_path(repo_root).exists()


def test_finish_task_posts_rendered_packet_comment_when_task_ref_is_present(tmp_path: Path):
    repo_root = init_repo(tmp_path)
    env, log_path = linear_stub_env(tmp_path)

    result = run(
        [*base_args(), "--task-ref", "ENG-123"],
        cwd=repo_root,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    requests = linear_requests(log_path)
    assert len(requests) == 2
    assert "query IssueById" in str(requests[0]["query"])
    assert "mutation CommentCreate" in str(requests[1]["query"])
    assert requests[1]["variables"]["issueId"] == "issue-1"
    assert requests[1]["variables"]["body"] == packet_path(repo_root).read_text(encoding="utf-8")
    assert requests[0]["headers"]["authorization"] == "linear-test-key"


def test_finish_task_uses_codex_linear_config_when_env_token_is_absent(tmp_path: Path):
    repo_root = init_repo(tmp_path)
    home_dir = tmp_path / "home"
    codex_dir = home_dir / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "config.toml").write_text(
        "[mcp_servers.linear]\n"
        'command = "npx"\n'
        'args = ["-y", "@anthropic/linear-mcp@latest", "-e", "LINEAR_API_KEY=lin_config_fallback"]\n',
        encoding="ascii",
    )
    env, log_path = linear_stub_env(tmp_path, api_key=None, home_dir=home_dir)

    result = run(
        [*base_args(), "--task-ref", "ENG-123"],
        cwd=repo_root,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    requests = linear_requests(log_path)
    assert len(requests) == 2
    assert requests[1]["variables"]["issueId"] == "issue-1"
    assert requests[0]["headers"]["authorization"] == "lin_config_fallback"


def test_finish_task_is_linear_no_op_when_task_ref_is_absent(tmp_path: Path):
    repo_root = init_repo(tmp_path)
    env, log_path = linear_stub_env(tmp_path)

    result = run(
        base_args(),
        cwd=repo_root,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert linear_requests(log_path) == []
    assert packet_path(repo_root).exists()
