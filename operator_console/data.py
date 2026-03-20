from dataclasses import dataclass
from datetime import datetime
from html import escape
import json
import mimetypes
from pathlib import Path

from operator_console.config import ConsoleSettings


ALLOWED_ARTIFACT_DIRS = ("research", "run_outputs")
TEXT_SUFFIXES = {".json", ".log", ".txt", ".yaml", ".yml"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


@dataclass(frozen=True)
class RunSummary:
    name: str
    relative_path: str
    modified_at: str
    task: str
    success: bool | None
    finished_reason: str
    steps_executed: int
    path: Path


@dataclass(frozen=True)
class ArtifactEntry:
    name: str
    relative_path: str
    kind: str
    modified_at: str
    size_bytes: int


@dataclass(frozen=True)
class RunDetail:
    summary: RunSummary
    payload: dict
    steps: list[dict]
    related_artifacts: list[ArtifactEntry]


@dataclass(frozen=True)
class ArtifactView:
    name: str
    relative_path: str
    kind: str
    size_bytes: int
    modified_at: str
    raw_text: str | None = None
    rendered_html: str | None = None
    path: Path | None = None


class RepoData:
    def __init__(self, settings: ConsoleSettings):
        self.settings = settings

    def list_run_summaries(self) -> list[RunSummary]:
        runs: list[RunSummary] = []
        if not self.settings.run_outputs_dir.exists():
            return runs
        for path in self.settings.run_outputs_dir.glob("*.json"):
            runs.append(self._summary_for_run(path))
        return sorted(runs, key=lambda run: run.path.stat().st_mtime, reverse=True)

    def load_run_detail(self, run_name: str) -> RunDetail:
        path = self._resolve_run_path(run_name)
        payload = _read_json(path)
        summary = self._summary_from_payload(path, payload)
        artifacts = self._related_artifacts_for_run(payload)
        steps = payload.get("steps", [])
        if not isinstance(steps, list):
            steps = []
        return RunDetail(
            summary=summary,
            payload=payload,
            steps=[step for step in steps if isinstance(step, dict)],
            related_artifacts=artifacts,
        )

    def load_artifact(self, relative_path: str) -> ArtifactView:
        path = self.resolve_artifact_path(relative_path)
        kind = artifact_kind(path)
        stat = path.stat()
        if kind == "image":
            return ArtifactView(
                name=path.name,
                relative_path=_relative_to_workspace(self.settings.workspace_dir, path),
                kind=kind,
                size_bytes=stat.st_size,
                modified_at=_format_timestamp(stat.st_mtime),
                path=path,
            )

        raw_text = path.read_text(encoding="utf-8")
        if path.suffix.lower() == ".json":
            try:
                raw_text = json.dumps(json.loads(raw_text), indent=2, sort_keys=True)
            except json.JSONDecodeError:
                pass

        rendered_html = render_markdown_html(raw_text) if kind == "markdown" else None
        return ArtifactView(
            name=path.name,
            relative_path=_relative_to_workspace(self.settings.workspace_dir, path),
            kind=kind,
            size_bytes=stat.st_size,
            modified_at=_format_timestamp(stat.st_mtime),
            raw_text=raw_text,
            rendered_html=rendered_html,
            path=path,
        )

    def resolve_artifact_path(self, relative_path: str) -> Path:
        candidate = (self.settings.workspace_dir / relative_path).resolve()
        allowed_roots = [
            (self.settings.workspace_dir / directory_name).resolve()
            for directory_name in ALLOWED_ARTIFACT_DIRS
        ]
        if not any(candidate.is_relative_to(root) for root in allowed_roots):
            raise ValueError("artifact path escapes allowed workspace directories")
        if not candidate.exists() or not candidate.is_file():
            raise FileNotFoundError(relative_path)
        return candidate

    def _summary_for_run(self, path: Path) -> RunSummary:
        payload = _read_json(path)
        return self._summary_from_payload(path, payload)

    def _summary_from_payload(self, path: Path, payload: dict) -> RunSummary:
        stat = path.stat()
        return RunSummary(
            name=path.name,
            relative_path=_relative_to_workspace(self.settings.workspace_dir, path),
            modified_at=_format_timestamp(stat.st_mtime),
            task=str(payload.get("task", "")),
            success=_coerce_bool(payload.get("success")),
            finished_reason=str(payload.get("finished_reason", "")),
            steps_executed=_coerce_int(payload.get("steps_executed")),
            path=path,
        )

    def _resolve_run_path(self, run_name: str) -> Path:
        candidate = (self.settings.run_outputs_dir / run_name).resolve()
        root = self.settings.run_outputs_dir.resolve()
        if not candidate.is_relative_to(root):
            raise ValueError("run path escapes run_outputs")
        if candidate.suffix.lower() != ".json":
            raise FileNotFoundError(run_name)
        if not candidate.exists() or not candidate.is_file():
            raise FileNotFoundError(run_name)
        return candidate

    def _related_artifacts_for_run(self, payload: dict) -> list[ArtifactEntry]:
        steps = payload.get("steps", [])
        if not isinstance(steps, list):
            return []

        explicit_paths: list[str] = []
        for step in steps:
            if not isinstance(step, dict):
                continue
            for source in (step.get("result"), step.get("params")):
                if not isinstance(source, dict):
                    continue
                raw_path = source.get("path")
                if not isinstance(raw_path, str) or raw_path in explicit_paths:
                    continue
                try:
                    resolved = self.resolve_artifact_path(raw_path)
                except (ValueError, FileNotFoundError):
                    continue
                explicit_paths.append(_relative_to_workspace(self.settings.workspace_dir, resolved))

        entries = [
            _artifact_entry(self.settings.workspace_dir, self.settings.workspace_dir / relative_path)
            for relative_path in explicit_paths
        ]
        entries.sort(key=lambda entry: _artifact_sort_key(self.settings.workspace_dir, entry), reverse=True)
        return entries


def artifact_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".md":
        return "markdown"
    if suffix in IMAGE_SUFFIXES:
        return "image"
    if suffix in TEXT_SUFFIXES:
        return "text"

    guessed_type, _ = mimetypes.guess_type(path.name)
    if guessed_type is None:
        return "binary"
    if guessed_type.startswith("image/"):
        return "image"
    if guessed_type.startswith("text/"):
        return "text"
    return "binary"


def render_markdown_html(markdown_text: str) -> str:
    lines = markdown_text.splitlines()
    parts: list[str] = []
    paragraph: list[str] = []
    list_items: list[str] = []
    code_lines: list[str] = []
    in_code_block = False

    def flush_paragraph() -> None:
        nonlocal paragraph
        if not paragraph:
            return
        content = "<br>\n".join(paragraph)
        parts.append(f"<p>{content}</p>")
        paragraph = []

    def flush_list() -> None:
        nonlocal list_items
        if not list_items:
            return
        items = "".join(f"<li>{item}</li>" for item in list_items)
        parts.append(f"<ul>{items}</ul>")
        list_items = []

    def flush_code() -> None:
        nonlocal code_lines
        if not code_lines:
            return
        parts.append(f"<pre><code>{escape(chr(10).join(code_lines))}</code></pre>")
        code_lines = []

    for raw_line in lines:
        line = raw_line.rstrip()

        if line.startswith("```"):
            flush_paragraph()
            flush_list()
            if in_code_block:
                flush_code()
                in_code_block = False
            else:
                in_code_block = True
            continue

        if in_code_block:
            code_lines.append(line)
            continue

        if not line.strip():
            flush_paragraph()
            flush_list()
            continue

        if line.startswith("#"):
            flush_paragraph()
            flush_list()
            level = min(6, len(line) - len(line.lstrip("#")))
            text = escape(line[level:].strip())
            parts.append(f"<h{level}>{text}</h{level}>")
            continue

        if line.startswith("- "):
            flush_paragraph()
            list_items.append(escape(line[2:].strip()))
            continue

        paragraph.append(escape(line))

    if in_code_block:
        flush_code()
    flush_paragraph()
    flush_list()
    return "\n".join(parts)


def _artifact_entry(workspace_dir: Path, path: Path) -> ArtifactEntry:
    stat = path.stat()
    return ArtifactEntry(
        name=path.name,
        relative_path=_relative_to_workspace(workspace_dir, path),
        kind=artifact_kind(path),
        modified_at=_format_timestamp(stat.st_mtime),
        size_bytes=stat.st_size,
    )


def _artifact_sort_key(workspace_dir: Path, entry: ArtifactEntry) -> float:
    return (workspace_dir / entry.relative_path).stat().st_mtime


def _relative_to_workspace(workspace_dir: Path, path: Path) -> str:
    return str(path.resolve().relative_to(workspace_dir.resolve()))


def _read_json(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict), f"expected JSON object in {path}"
    return payload


def _format_timestamp(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).astimezone().isoformat(timespec="seconds")


def _coerce_bool(value) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def _coerce_int(value) -> int:
    if isinstance(value, int):
        return value
    return 0
