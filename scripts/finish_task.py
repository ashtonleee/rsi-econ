#!/usr/bin/env python3
"""Run the canonical post-task updater and optionally mirror its packet to Linear."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path


DEFAULT_LINEAR_URL = "https://api.linear.app/graphql"
POST_TASK_UPDATE = Path(__file__).resolve().with_name("post_task_update.py")

ISSUE_BY_ID_QUERY = """
query IssueById($id: String!) {
  issue(id: $id) {
    id
    identifier
    title
  }
}
""".strip()

COMMENT_CREATE_MUTATION = """
mutation CommentCreate($issueId: String!, $body: String!) {
  commentCreate(input: { issueId: $issueId, body: $body }) {
    success
    comment {
      id
    }
  }
}
""".strip()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", required=True, help="One-line bounded task summary.")
    parser.add_argument("--task-ref", help="Existing Linear issue identifier, for example ENG-123.")
    parser.add_argument("--agent", default="codex-gpt-5", help="Agent label for the session log and packet.")
    parser.add_argument(
        "--changed-file",
        action="append",
        default=[],
        help="Explicit changed file path. Repeat for multiple files.",
    )
    parser.add_argument(
        "--test",
        action="append",
        default=[],
        help="Explicit test result as <status>::<command>. Repeat for multiple tests.",
    )
    parser.add_argument(
        "--stage-boundary",
        action="store_true",
        help="Mark the task as a likely stage-boundary update.",
    )
    parser.add_argument(
        "--stage-status",
        default="STAGE_STATUS.md",
        help="Pass-through path for scripts/post_task_update.py.",
    )
    parser.add_argument(
        "--output",
        default="runtime/post_task_updates/latest_linear_update.md",
        help="Pass-through packet path for scripts/post_task_update.py.",
    )
    return parser.parse_args(argv)


def parse_test(raw: str) -> dict[str, str]:
    status, separator, command = raw.partition("::")
    status = status.strip().lower()
    command = command.strip()
    if separator != "::" or not status or not command:
        raise SystemExit("Each --test value must use the form <status>::<command>.")
    return {"status": status, "command": command}


def resolve_repo_path(raw_path: str, repo_root: Path) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path
    return repo_root / path


def build_payload(args: argparse.Namespace) -> dict[str, object]:
    payload: dict[str, object] = {
        "summary": args.summary,
        "agent": args.agent,
        "changed_files": list(args.changed_file),
        "tests_run": [parse_test(item) for item in args.test],
    }
    if args.task_ref:
        payload["task_ref"] = args.task_ref
    if args.stage_boundary:
        payload["stage_boundary"] = True
    return payload


def run_post_task_update(args: argparse.Namespace, payload: dict[str, object], repo_root: Path) -> tuple[int, str]:
    command = [
        sys.executable,
        str(POST_TASK_UPDATE),
        "--result-file",
        "-",
        "--stage-status",
        args.stage_status,
        "--output",
        args.output,
    ]
    result = subprocess.run(
        command,
        cwd=repo_root,
        input=json.dumps(payload) + "\n",
        capture_output=True,
        text=True,
        check=False,
    )
    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    return result.returncode, result.stdout


def linear_headers() -> dict[str, str]:
    api_key = os.environ.get("LINEAR_API_KEY", "").strip()
    oauth_token = os.environ.get("LINEAR_TOKEN", "").strip()
    if api_key:
        return {
            "Content-Type": "application/json",
            "Authorization": api_key,
        }
    if oauth_token:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {oauth_token}",
        }
    config_key = load_linear_api_key_from_codex_config()
    if config_key:
        return {
            "Content-Type": "application/json",
            "Authorization": config_key,
        }
    raise SystemExit("Set LINEAR_API_KEY or LINEAR_TOKEN to post Linear comments.")


def load_linear_api_key_from_codex_config() -> str | None:
    config_path = Path.home() / ".codex" / "config.toml"
    if not config_path.exists():
        return None
    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    server = config.get("mcp_servers", {}).get("linear")
    if not isinstance(server, dict):
        return None
    args = server.get("args")
    if not isinstance(args, list):
        return None
    for item in args:
        value = str(item)
        prefix = "LINEAR_API_KEY="
        if value.startswith(prefix):
            token = value[len(prefix) :].strip()
            if token:
                return token
    return None


def linear_graphql(query: str, variables: dict[str, object]) -> dict[str, object]:
    url = os.environ.get("LINEAR_API_URL", DEFAULT_LINEAR_URL).strip() or DEFAULT_LINEAR_URL
    request = urllib.request.Request(
        url,
        data=json.dumps({"query": query, "variables": variables}).encode("utf-8"),
        headers=linear_headers(),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"Linear request failed with HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"Linear request failed: {exc}") from exc

    errors = payload.get("errors") or []
    if errors:
        message = "; ".join(str(item.get("message", "unknown error")) for item in errors if isinstance(item, dict))
        raise SystemExit(f"Linear GraphQL error: {message or errors}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise SystemExit("Linear response did not include a data object.")
    return data


def post_linear_comment(task_ref: str, packet: str) -> None:
    issue_data = linear_graphql(ISSUE_BY_ID_QUERY, {"id": task_ref}).get("issue")
    if not isinstance(issue_data, dict):
        raise SystemExit(f"Linear issue '{task_ref}' was not found.")
    issue_id = str(issue_data.get("id", "")).strip()
    if not issue_id:
        raise SystemExit(f"Linear issue '{task_ref}' did not return an id.")

    comment_data = linear_graphql(
        COMMENT_CREATE_MUTATION,
        {"issueId": issue_id, "body": packet},
    ).get("commentCreate")
    if not isinstance(comment_data, dict) or comment_data.get("success") is not True:
        raise SystemExit(f"Linear commentCreate failed for issue '{task_ref}'.")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo_root = Path.cwd().resolve()
    payload = build_payload(args)
    output_path = resolve_repo_path(args.output, repo_root)
    returncode, packet = run_post_task_update(args, payload, repo_root)
    if returncode != 0:
        return returncode
    if output_path.exists():
        packet = output_path.read_text(encoding="utf-8")
    if args.task_ref:
        post_linear_comment(args.task_ref, packet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
