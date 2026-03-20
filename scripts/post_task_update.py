#!/usr/bin/env python3
"""Append a concise session update and write a Linear-ready post-task packet."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path


DEFAULT_OUTPUT = Path("runtime/post_task_updates/latest_linear_update.md")
TIER2_PATHS = {
    "TASK_GRAPH.md",
    "ACCEPTANCE_TEST_MATRIX.md",
    "REPO_LAYOUT.md",
    "assurance/REGISTRY.yaml",
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--result-file",
        required=True,
        help="Path to a JSON task-result payload, or '-' to read from stdin.",
    )
    parser.add_argument(
        "--stage-status",
        default="STAGE_STATUS.md",
        help="Path to the repo stage-status file to update.",
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT),
        help="Path for the derived Linear-ready markdown packet.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Render the update without modifying files.",
    )
    return parser.parse_args(argv)


def strip_repo_prefix(path: Path, repo_root: Path) -> Path:
    parts = [part for part in path.parts if part not in ("", ".")]
    if repo_root.name in parts:
        index = parts.index(repo_root.name)
        if index < len(parts) - 1:
            parts = parts[index + 1 :]
    if not parts:
        return Path(".")
    return Path(*parts)


def normalize_repo_path(path: str | Path, repo_root: Path) -> str:
    path = Path(str(path).strip())
    if path.is_absolute():
        try:
            return path.resolve().relative_to(repo_root).as_posix()
        except ValueError:
            return path.as_posix()
    return strip_repo_prefix(path, repo_root).as_posix()


def resolve_cli_path(raw_path: str, repo_root: Path) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path
    return repo_root / strip_repo_prefix(path, repo_root)


def require_payload_list(payload: dict[str, object], field_name: str) -> list[object]:
    raw = payload.get(field_name)
    if not isinstance(raw, list):
        raise SystemExit(f"Task result payload requires a '{field_name}' list.")
    return raw


def load_payload(result_file: Path | None) -> dict[str, object]:
    if result_file is None:
        raw = sys.stdin.read()
    else:
        raw = result_file.read_text(encoding="utf-8")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Could not parse task result JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit("Task result payload must be a JSON object.")
    summary = str(payload.get("summary", "")).strip()
    if not summary:
        raise SystemExit("Task result payload requires a non-empty 'summary'.")
    require_payload_list(payload, "changed_files")
    require_payload_list(payload, "tests_run")
    return payload


def normalize_changed_files(payload: dict[str, object], repo_root: Path, *, excluded: set[str]) -> list[str]:
    changed: list[str] = []
    for item in require_payload_list(payload, "changed_files"):
        value = str(item).strip()
        if not value:
            raise SystemExit("Each changed_files entry requires a non-empty path.")
        path = normalize_repo_path(value, repo_root)
        if path in excluded:
            continue
        if path.startswith("runtime/post_task_updates/"):
            continue
        changed.append(path)
    return sorted(dict.fromkeys(changed))


def normalize_tests(payload: dict[str, object]) -> list[dict[str, str]]:
    tests: list[dict[str, str]] = []
    for item in require_payload_list(payload, "tests_run"):
        if not isinstance(item, dict):
            raise SystemExit("Each tests_run entry requires an object with explicit non-empty 'command' and 'status'.")
        command = str(item.get("command", "")).strip()
        status = str(item.get("status", "")).strip().lower()
        if not command:
            raise SystemExit("Each tests_run entry requires an explicit non-empty 'command'.")
        if not status:
            raise SystemExit("Each tests_run entry requires an explicit non-empty 'status'.")
        tests.append({"command": command, "status": status})
    return tests


def normalize_follow_ups(payload: dict[str, object]) -> list[dict[str, str]]:
    follow_ups: list[dict[str, str]] = []
    raw = payload.get("follow_ups")
    if not isinstance(raw, list):
        return follow_ups
    for item in raw:
        if isinstance(item, str):
            title = item.strip()
            body = ""
        elif isinstance(item, dict):
            title = str(item.get("title", "")).strip()
            body = str(item.get("body", "")).strip()
        else:
            continue
        if not title:
            continue
        follow_ups.append({"title": title, "body": body})
    return follow_ups


def ensure_sentence(text: str) -> str:
    text = " ".join(text.strip().split())
    if not text:
        return text
    if text.endswith((".", "!", "?")):
        return text
    return f"{text}."


def session_summary(summary: str, tests: list[dict[str, str]]) -> str:
    passed = [item["command"] for item in tests if item["status"] == "passed"]
    summary = ensure_sentence(summary)
    if not passed:
        return summary
    if len(passed) == 1:
        return f"{summary} Verified `{passed[0]}`."
    if len(passed) == 2:
        return f"{summary} Verified `{passed[0]}` and `{passed[1]}`."
    return f"{summary} Verified {len(passed)} checks including `{passed[0]}` and `{passed[1]}`."


def stage_boundary_note(payload: dict[str, object], changed_files: list[str]) -> str:
    if payload.get("stage_boundary") is True:
        return "Stage-boundary follow-up likely: task payload marked `stage_boundary=true`."
    tier2_hits = [path for path in changed_files if path in TIER2_PATHS]
    if tier2_hits:
        joined = ", ".join(f"`{path}`" for path in tier2_hits)
        return f"Stage-boundary follow-up likely: changed files include Tier 2 paths {joined}. Tier 2 docs were not auto-updated."
    return "No stage-boundary trigger detected."


def render_packet(
    payload: dict[str, object],
    changed_files: list[str],
    tests: list[dict[str, str]],
    follow_ups: list[dict[str, str]],
    boundary_note: str,
) -> str:
    task_ref = str(payload.get("task_ref", "")).strip()
    agent = str(payload.get("agent", "codex-gpt-5")).strip() or "codex-gpt-5"
    summary = ensure_sentence(str(payload["summary"]))

    lines = ["# Linear Update Packet", ""]
    if task_ref:
        lines.append(f"- Task: {task_ref}")
    lines.append(f"- Agent: {agent}")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(summary)
    lines.append("")
    lines.append("## Changed Files")
    lines.append("")
    if changed_files:
        lines.extend(f"- {path}" for path in changed_files)
    else:
        lines.append("- None recorded.")
    lines.append("")
    lines.append("## Tests Run")
    lines.append("")
    if tests:
        for item in tests:
            lines.append(f"- {item['status'].upper()} `{item['command']}`")
    else:
        lines.append("- None recorded.")
    lines.append("")
    lines.append("## Tier 2 / Stage Boundary")
    lines.append("")
    lines.append(boundary_note)
    lines.append("")
    lines.append("## Proposed Follow-Ups (Backlog Only)")
    lines.append("")
    if follow_ups:
        for item in follow_ups:
            if item["body"]:
                lines.append(f"- {item['title']}: {item['body']}")
            else:
                lines.append(f"- {item['title']}")
    else:
        lines.append("- None.")
    lines.append("")
    return "\n".join(lines)


def sanitize_table_cell(text: str) -> str:
    return " ".join(text.replace("|", "\\|").split())


def append_stage_status(stage_status_path: Path, *, agent: str, summary: str, today: date) -> None:
    if not stage_status_path.exists():
        raise SystemExit(f"{stage_status_path} does not exist.")
    text = stage_status_path.read_text(encoding="utf-8")
    divider = "|------|-------|---------|"
    if divider not in text:
        raise SystemExit(f"{stage_status_path} is missing the session-log table header.")
    if not text.endswith("\n"):
        text += "\n"
    row = f"| {today.isoformat()} | {sanitize_table_cell(agent)} | {sanitize_table_cell(summary)} |\n"
    stage_status_path.write_text(text + row, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo_root = Path.cwd().resolve()
    result_file = None if args.result_file == "-" else resolve_cli_path(args.result_file, repo_root)
    stage_status_path = resolve_cli_path(args.stage_status, repo_root)
    output_path = resolve_cli_path(args.output, repo_root)

    payload = load_payload(result_file)
    excluded = {normalize_repo_path(output_path, repo_root)}
    if result_file is not None:
        excluded.add(normalize_repo_path(result_file, repo_root))

    changed_files = normalize_changed_files(payload, repo_root, excluded=excluded)
    tests = normalize_tests(payload)
    follow_ups = normalize_follow_ups(payload)
    agent = str(payload.get("agent", "codex-gpt-5")).strip() or "codex-gpt-5"
    summary = session_summary(str(payload["summary"]), tests)
    boundary_note = stage_boundary_note(payload, changed_files)
    packet = render_packet(payload, changed_files, tests, follow_ups, boundary_note)

    if args.dry_run:
        print(packet)
        return 0

    append_stage_status(stage_status_path, agent=agent, summary=summary, today=date.today())
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(packet, encoding="utf-8")
    print(packet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
