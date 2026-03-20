from dataclasses import asdict, dataclass, replace
from datetime import datetime
import json
import os
from pathlib import Path
import shlex
import subprocess
from typing import Literal
from uuid import uuid4

from operator_console.config import ConsoleSettings
from operator_console.data import RepoData, artifact_kind


ACTIVE_LAUNCH_STATUSES = {"starting", "running"}
EXIT_MARKER_PREFIX = "__RSI_EXIT_CODE__="


class LaunchBusyError(RuntimeError):
    """Raised when the console already has an active launch."""


@dataclass(frozen=True)
class LaunchRequest:
    task: str
    script: str
    launch_mode: Literal["default", "provider"]
    model: str = ""
    input_url: str = ""
    follow_target_url: str = ""
    proposal_target_url: str = ""
    max_steps: int = 8

    def __post_init__(self):
        assert self.task.strip(), "task is required"
        assert self.script.strip(), "script is required"
        assert self.launch_mode in {"default", "provider"}
        assert self.max_steps > 0, "max_steps must be positive"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class LaunchRecord:
    launch_id: str
    created_at: str
    status: Literal["starting", "running", "finished", "failed"]
    task: str
    script: str
    launch_mode: Literal["default", "provider"]
    model: str = ""
    input_url: str = ""
    follow_target_url: str = ""
    proposal_target_url: str = ""
    max_steps: int = 8
    pid: int | None = None
    run_id: str | None = None
    summary_path: str = ""
    exit_code: int | None = None
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict) -> "LaunchRecord":
        return cls(**payload)


def default_background_runner(argv: list[str], *, log_path: Path, cwd: Path) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command_text = " ".join(shlex.quote(part) for part in argv)
    wrapped = (
        f"{command_text}; "
        "rc=$?; "
        f"printf '\\n{EXIT_MARKER_PREFIX}%s\\n' \"$rc\""
    )
    with log_path.open("a", encoding="utf-8") as log_handle:
        process = subprocess.Popen(
            ["bash", "-lc", wrapped],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            text=True,
        )
    return process.pid


def default_pid_checker(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class LaunchManager:
    def __init__(
        self,
        settings: ConsoleSettings,
        *,
        repo_data: RepoData | None = None,
        runner=default_background_runner,
        pid_checker=default_pid_checker,
        now_fn=None,
    ):
        self.settings = settings
        self.repo_data = repo_data or RepoData(settings)
        self.runner = runner
        self.pid_checker = pid_checker
        self.now_fn = now_fn or _now_iso
        self.settings.launches_dir.mkdir(parents=True, exist_ok=True)
        self.settings.launch_logs_dir.mkdir(parents=True, exist_ok=True)

    def list_seed_plans(self) -> list[str]:
        plan_dir = self.settings.workspace_dir / ".seed_plans"
        if not plan_dir.exists():
            return []
        return sorted(path.name for path in plan_dir.glob("*.json") if path.is_file())

    def list_launches(self) -> list[LaunchRecord]:
        launches = [
            self.get_launch(path.stem)
            for path in self.settings.launches_dir.glob("*.json")
            if path.is_file()
        ]
        return sorted(launches, key=lambda launch: launch.created_at, reverse=True)

    def get_active_launch(self) -> LaunchRecord | None:
        for launch in self.list_launches():
            if launch.status in ACTIVE_LAUNCH_STATUSES:
                return launch
        return None

    def build_launch_command(self, request: LaunchRequest) -> list[str]:
        script_name = Path(request.script).name
        script_ref = f".seed_plans/{script_name}"
        if request.launch_mode == "default":
            argv = [
                "docker",
                "compose",
                "exec",
                "-T",
                "agent",
                "python",
                "-m",
                "untrusted.agent.seed_runner",
                "--task",
                request.task,
                "--planner",
                "scripted",
                "--script",
                script_ref,
            ]
        else:
            argv = [
                "bash",
                "./scripts/provider.sh",
                "seed-run",
                "--script",
                script_ref,
                "--task",
                request.task,
            ]
            if request.model:
                argv.extend(["--model", request.model])

        if request.input_url:
            argv.extend(["--input-url", request.input_url])
        if request.follow_target_url:
            argv.extend(["--follow-target-url", request.follow_target_url])
        if request.proposal_target_url:
            argv.extend(["--proposal-target-url", request.proposal_target_url])
        if request.max_steps:
            argv.extend(["--max-steps", str(request.max_steps)])
        return argv

    def create_launch(self, request: LaunchRequest) -> LaunchRecord:
        if Path(request.script).name not in self.list_seed_plans():
            raise FileNotFoundError(request.script)
        active = self.get_active_launch()
        if active is not None:
            raise LaunchBusyError("Another launch is still active.")

        launch_id = uuid4().hex
        log_path = self._log_path(launch_id)
        record = LaunchRecord(
            launch_id=launch_id,
            created_at=self.now_fn(),
            status="starting",
            task=request.task,
            script=Path(request.script).name,
            launch_mode=request.launch_mode,
            model=request.model,
            input_url=request.input_url,
            follow_target_url=request.follow_target_url,
            proposal_target_url=request.proposal_target_url,
            max_steps=request.max_steps,
        )
        try:
            pid = self.runner(
                self.build_launch_command(request),
                log_path=log_path,
                cwd=self.settings.repo_root,
            )
        except Exception as exc:
            failed = replace(
                record,
                status="failed",
                error=str(exc) or "failed to launch background process",
            )
            self._write_launch(failed)
            return failed

        record = replace(record, pid=pid)
        self._write_launch(record)
        return record

    def get_launch(self, launch_id: str) -> LaunchRecord:
        record = self._read_launch(launch_id)
        refreshed = self._refresh_launch(record)
        if refreshed != record:
            self._write_launch(refreshed)
        return refreshed

    def get_snapshot(self, launch_id: str) -> dict:
        launch = self.get_launch(launch_id)
        timeline = self._timeline_for_launch(launch)
        proposal_ids = []
        for event in timeline:
            summary = event.get("summary", {})
            if not isinstance(summary, dict):
                continue
            proposal_id = summary.get("proposal_id")
            if isinstance(proposal_id, str) and proposal_id and proposal_id not in proposal_ids:
                proposal_ids.append(proposal_id)

        related_artifacts: list[dict] = []
        summary_url = ""
        latest_screenshot = self._latest_screenshot_for_launch(launch)
        if launch.summary_path:
            summary_url = f"/runs/{Path(launch.summary_path).name}"
            try:
                detail = self.repo_data.load_run_detail(Path(launch.summary_path).name)
            except (FileNotFoundError, ValueError):
                detail = None
            if detail is not None:
                related_artifacts = [_artifact_dict(artifact) for artifact in detail.related_artifacts]
                if latest_screenshot is None:
                    for artifact in detail.related_artifacts:
                        artifact_path = self.settings.workspace_dir / artifact.relative_path
                        if artifact_kind(artifact_path) == "image":
                            latest_screenshot = {
                                "name": artifact.name,
                                "relative_path": artifact.relative_path,
                                "url": f"/artifacts/{artifact.relative_path}",
                            }
                            break

        return {
            "launch": launch.to_dict(),
            "timeline": timeline,
            "proposal_ids": proposal_ids,
            "latest_screenshot": latest_screenshot,
            "summary_url": summary_url,
            "related_artifacts": related_artifacts,
            "log_tail": self._tail_log(launch.launch_id),
            "version_token": self._version_token(
                launch,
                timeline=timeline,
                proposal_ids=proposal_ids,
                latest_screenshot=latest_screenshot,
            ),
        }

    def _refresh_launch(self, record: LaunchRecord) -> LaunchRecord:
        events = self._read_agent_run_events()
        updated = record
        if updated.run_id is None:
            matched_run_id = self._match_run_id(updated, events)
            if matched_run_id:
                updated = replace(updated, run_id=matched_run_id)

        run_events = [
            event
            for event in events
            if updated.run_id and event["run_id"] == updated.run_id
        ]

        if updated.summary_path == "":
            for event in reversed(run_events):
                if event["event_kind"] != "run_end":
                    continue
                summary_path = event.get("summary", {}).get("summary_path", "")
                if summary_path:
                    updated = replace(updated, summary_path=summary_path)
                    break

        exit_code = self._read_exit_code(updated.launch_id)
        if exit_code is not None and exit_code != updated.exit_code:
            updated = replace(updated, exit_code=exit_code)

        has_run_end = any(event["event_kind"] == "run_end" for event in run_events)
        has_summary = bool(updated.summary_path) and self._summary_exists(updated.summary_path)
        running = self.pid_checker(updated.pid)

        new_status = updated.status
        error = updated.error
        if has_run_end or has_summary:
            new_status = "finished"
        elif running:
            new_status = "running" if updated.run_id else "starting"
        elif updated.exit_code is not None:
            new_status = "failed"
            if not error:
                error = f"launch exited with code {updated.exit_code}"
        elif updated.status in ACTIVE_LAUNCH_STATUSES:
            new_status = "failed"
            if not error:
                error = "launch exited before a run summary appeared"

        return replace(updated, status=new_status, error=error)

    def _match_run_id(self, record: LaunchRecord, events: list[dict]) -> str | None:
        created_at = _parse_timestamp(record.created_at)
        for event in events:
            if event["event_kind"] != "run_start":
                continue
            if _parse_timestamp(event["timestamp"]) < created_at:
                continue
            summary = event.get("summary", {})
            if (
                summary.get("task", "") == record.task
                and summary.get("input_url", "") == record.input_url
                and summary.get("follow_target_url", "") == record.follow_target_url
                and summary.get("proposal_target_url", "") == record.proposal_target_url
            ):
                return event["run_id"]
        return None

    def _read_agent_run_events(self) -> list[dict]:
        if not self.settings.trusted_log_path.exists():
            return []
        events: list[dict] = []
        for line in self.settings.trusted_log_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            payload = json.loads(line)
            if payload.get("event_type") != "agent_run":
                continue
            summary = payload.get("summary", {})
            reported_summary = summary.get("reported_summary", {})
            events.append(
                {
                    "timestamp": payload.get("timestamp", ""),
                    "event_kind": summary.get("event_kind", ""),
                    "run_id": summary.get("run_id", ""),
                    "step_index": summary.get("step_index"),
                    "tool_name": summary.get("tool_name"),
                    "summary": reported_summary.get("result", reported_summary),
                }
            )
        return sorted(events, key=lambda event: event["timestamp"])

    def _timeline_for_launch(self, launch: LaunchRecord) -> list[dict]:
        if not launch.run_id:
            return []
        return [
            event
            for event in self._read_agent_run_events()
            if event["run_id"] == launch.run_id
        ]

    def _latest_screenshot_for_launch(self, launch: LaunchRecord) -> dict | None:
        research_dir = self.settings.research_dir
        if not research_dir.exists():
            return None
        created_at = _parse_timestamp(launch.created_at)
        candidates: list[Path] = []
        for path in research_dir.iterdir():
            if not path.is_file():
                continue
            if artifact_kind(path) != "image":
                continue
            if datetime.fromtimestamp(path.stat().st_mtime).astimezone() >= created_at:
                candidates.append(path)
        if not candidates:
            return None
        latest = max(candidates, key=lambda path: path.stat().st_mtime)
        relative_path = str(latest.resolve().relative_to(self.settings.workspace_dir.resolve()))
        return {
            "name": latest.name,
            "relative_path": relative_path,
            "url": f"/artifacts/{relative_path}",
        }

    def _summary_exists(self, summary_path: str) -> bool:
        path = (self.settings.workspace_dir / summary_path).resolve()
        return path.exists() and path.is_file()

    def _tail_log(self, launch_id: str, *, max_lines: int = 40) -> str:
        log_path = self._log_path(launch_id)
        if not log_path.exists():
            return ""
        lines = log_path.read_text(encoding="utf-8").splitlines()
        visible = [line for line in lines if not line.startswith(EXIT_MARKER_PREFIX)]
        return "\n".join(visible[-max_lines:])

    def _read_exit_code(self, launch_id: str) -> int | None:
        log_path = self._log_path(launch_id)
        if not log_path.exists():
            return None
        for line in reversed(log_path.read_text(encoding="utf-8").splitlines()):
            if line.startswith(EXIT_MARKER_PREFIX):
                raw = line.partition("=")[2].strip()
                if raw:
                    return int(raw)
        return None

    def _launch_path(self, launch_id: str) -> Path:
        return self.settings.launches_dir / f"{launch_id}.json"

    def _log_path(self, launch_id: str) -> Path:
        return self.settings.launch_logs_dir / f"{launch_id}.log"

    def _read_launch(self, launch_id: str) -> LaunchRecord:
        path = self._launch_path(launch_id)
        payload = json.loads(path.read_text(encoding="utf-8"))
        return LaunchRecord.from_dict(payload)

    def _write_launch(self, record: LaunchRecord) -> None:
        path = self._launch_path(record.launch_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(record.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _version_token(
        self,
        launch: LaunchRecord,
        *,
        timeline: list[dict],
        proposal_ids: list[str],
        latest_screenshot: dict | None,
    ) -> str:
        last_timestamp = timeline[-1]["timestamp"] if timeline else ""
        screenshot_path = ""
        if latest_screenshot:
            screenshot_path = latest_screenshot.get("relative_path", "")
        return "|".join(
            [
                launch.status,
                launch.run_id or "",
                launch.summary_path,
                str(launch.exit_code if launch.exit_code is not None else ""),
                last_timestamp,
                ",".join(proposal_ids),
                screenshot_path,
            ]
        )


def _artifact_dict(artifact) -> dict:
    return {
        "name": artifact.name,
        "relative_path": artifact.relative_path,
        "kind": artifact.kind,
        "modified_at": artifact.modified_at,
        "size_bytes": artifact.size_bytes,
        "url": f"/artifacts/{artifact.relative_path}",
    }


def _parse_timestamp(raw: str) -> datetime:
    return datetime.fromisoformat(raw)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
