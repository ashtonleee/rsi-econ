from dataclasses import asdict, dataclass, replace
from datetime import datetime
import json
from pathlib import Path
from typing import Literal
from uuid import uuid4

from operator_console.config import ConsoleSettings
from operator_console.data import RepoData, artifact_kind
from operator_console.launches import EXIT_MARKER_PREFIX, default_background_runner, default_pid_checker
from operator_console.session_live_state import build_session_snapshot
from shared.schemas import ProposalRecord


ACTIVE_SESSION_STATUSES = {"starting", "running", "resuming"}


class SessionBusyError(RuntimeError):
    """Raised when the console already has an active session."""


@dataclass(frozen=True)
class SessionCreateRequest:
    task: str
    launch_mode: Literal["default", "provider"]
    model: str = ""
    input_url: str = ""
    proposal_target_url: str = ""
    max_turns_per_resume: int = 4

    def __post_init__(self):
        assert self.task.strip(), "task is required"
        assert self.launch_mode in {"default", "provider"}
        assert self.max_turns_per_resume > 0, "max_turns_per_resume must be positive"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    created_at: str
    updated_at: str
    status: Literal["starting", "running", "waiting_for_approval", "resuming", "finished", "failed"]
    task: str
    launch_mode: Literal["default", "provider"]
    model: str = ""
    input_url: str = ""
    proposal_target_url: str = ""
    max_turns_per_resume: int = 4
    resume_count: int = 0
    current_run_id: str = ""
    last_run_id: str = ""
    last_proposal_id: str = ""
    auto_resume_enabled: bool = True
    pid: int | None = None
    error: str = ""
    summary_path: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict) -> "SessionRecord":
        return cls(**payload)


class SessionManager:
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
        self.settings.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.settings.session_logs_dir.mkdir(parents=True, exist_ok=True)

    def list_sessions(self) -> list[SessionRecord]:
        sessions = [
            self.get_session(path.stem)
            for path in self.settings.sessions_dir.glob("*.json")
            if path.is_file()
        ]
        return sorted(sessions, key=lambda item: item.updated_at, reverse=True)

    def get_active_session(self) -> SessionRecord | None:
        for session in self.list_sessions():
            if session.status in ACTIVE_SESSION_STATUSES:
                return session
        return None

    def build_session_command(self, request: SessionCreateRequest, *, session_id: str, resume: bool = False) -> list[str]:
        if request.launch_mode == "default":
            argv = [
                "docker",
                "compose",
                "exec",
                "-T",
                "agent",
                "python",
                "-m",
                "untrusted.agent.session_runner",
            ]
        else:
            argv = ["bash", "./scripts/provider.sh", "session-run"]
            if request.model:
                argv.extend(["--model", request.model])
        if resume:
            argv.extend(["--resume", "--session-id", session_id])
            return argv

        argv.extend(
            [
                "--task",
                request.task,
                "--launch-mode",
                request.launch_mode,
                "--max-turns-per-resume",
                str(request.max_turns_per_resume),
            ]
        )
        if request.input_url:
            argv.extend(["--input-url", request.input_url])
        if request.proposal_target_url:
            argv.extend(["--proposal-target-url", request.proposal_target_url])
        if request.launch_mode == "default" and request.model:
            argv.extend(["--model", request.model])
        argv.extend(["--session-id", session_id])
        return argv

    def create_session(self, request: SessionCreateRequest) -> SessionRecord:
        active = self.get_active_session()
        if active is not None:
            raise SessionBusyError("Another session is still active.")

        session_id = uuid4().hex
        record = SessionRecord(
            session_id=session_id,
            created_at=self.now_fn(),
            updated_at=self.now_fn(),
            status="starting",
            task=request.task,
            launch_mode=request.launch_mode,
            model=request.model,
            input_url=request.input_url,
            proposal_target_url=request.proposal_target_url,
            max_turns_per_resume=request.max_turns_per_resume,
        )
        try:
            pid = self.runner(
                self.build_session_command(request, session_id=session_id),
                log_path=self._log_path(session_id),
                cwd=self.settings.repo_root,
            )
        except Exception as exc:
            failed = replace(record, status="failed", error=str(exc) or "failed to start session process")
            self._write_session(failed)
            return failed

        workspace_dir = self._workspace_session_dir(session_id)
        workspace_dir.mkdir(parents=True, exist_ok=True)
        self._write_workspace_state(
            session_id,
            {
                "session_id": session_id,
                "created_at": record.created_at,
                "updated_at": record.updated_at,
                "status": "starting",
                "task": request.task,
                "input_url": request.input_url,
                "proposal_target_url": request.proposal_target_url,
                "launch_mode": request.launch_mode,
                "model": request.model,
                "resume_count": 0,
                "current_run_id": "",
                "last_run_id": "",
                "summary_path": "",
                "last_proposal": {},
                "current_screenshot_path": "",
                "error": "",
            },
        )
        record = replace(record, pid=pid)
        self._write_session(record)
        return record

    def get_session(self, session_id: str) -> SessionRecord:
        record = self._read_session(session_id)
        refreshed = self._refresh_session(record)
        if refreshed != record:
            self._write_session(refreshed)
        return refreshed

    def get_snapshot(self, session_id: str) -> dict:
        session = self.get_session(session_id)
        workspace_state = self._read_workspace_state(session_id)
        transcript = self._read_transcript(session_id)
        current_screenshot = self._current_screenshot(session_id, workspace_state, session)
        recent_screenshots = self._recent_screenshots(session_id, current_screenshot)
        summary_url = ""
        related_artifacts = self._session_artifacts(session_id)
        if session.summary_path:
            summary_url = f"/artifacts/{session.summary_path}"
        raw_snapshot = {
            "session": session.to_dict(),
            "workspace_state": workspace_state,
            "transcript": transcript,
            "current_screenshot": current_screenshot,
            "recent_screenshots": recent_screenshots,
            "related_artifacts": related_artifacts,
            "summary_url": summary_url,
            "log_tail": self._tail_log(session_id),
        }
        return build_session_snapshot(raw_snapshot)

    def apply_proposal_update(self, proposal: ProposalRecord):
        for session in self.list_sessions():
            if session.last_proposal_id != proposal.proposal_id:
                continue
            state = self._read_workspace_state(session.session_id)
            if not state:
                return None
            state["last_proposal"] = proposal.model_dump()
            state["updated_at"] = self.now_fn()
            if proposal.status in {"pending", "approved"}:
                state["status"] = "waiting_for_approval"
            self._write_workspace_state(session.session_id, state)
            self._append_transcript(
                session.session_id,
                {
                    "kind": "operator_state",
                    "timestamp": self.now_fn(),
                    "summary": f"Operator updated proposal {proposal.proposal_id} to {proposal.status}.",
                    "proposal": proposal.model_dump(),
                },
            )
            updated = replace(
                session,
                updated_at=self.now_fn(),
                status="resuming" if proposal.status in {"approved", "executed"} else "waiting_for_approval",
                last_proposal_id=proposal.proposal_id,
            )
            if proposal.status in {"approved", "executed"} and updated.auto_resume_enabled:
                pid = self.runner(
                    self.build_session_command(
                        SessionCreateRequest(
                            task=updated.task,
                            launch_mode=updated.launch_mode,
                            model=updated.model,
                            input_url=updated.input_url,
                            proposal_target_url=updated.proposal_target_url,
                            max_turns_per_resume=updated.max_turns_per_resume,
                        ),
                        session_id=updated.session_id,
                        resume=True,
                    ),
                    log_path=self._log_path(updated.session_id),
                    cwd=self.settings.repo_root,
                )
                updated = replace(updated, pid=pid)
            self._write_session(updated)
            return updated
        return None

    def _refresh_session(self, record: SessionRecord) -> SessionRecord:
        workspace_state = self._read_workspace_state(record.session_id)
        updated = record
        if workspace_state:
            last_proposal = workspace_state.get("last_proposal", {})
            updated = replace(
                updated,
                updated_at=str(workspace_state.get("updated_at", record.updated_at)),
                current_run_id=str(workspace_state.get("current_run_id", record.current_run_id)),
                last_run_id=str(workspace_state.get("last_run_id", record.last_run_id)),
                summary_path=str(workspace_state.get("summary_path", record.summary_path)),
                last_proposal_id=str((last_proposal or {}).get("proposal_id", record.last_proposal_id)),
                resume_count=int(workspace_state.get("resume_count", record.resume_count) or 0),
                error=str(workspace_state.get("error", record.error)),
            )
        running = self.pid_checker(updated.pid)
        exit_code = self._read_exit_code(updated.session_id)
        state_status = str((workspace_state or {}).get("status", updated.status))
        if state_status in {"waiting_for_approval", "finished", "failed"}:
            return replace(updated, status=state_status)
        if running:
            return replace(updated, status="running" if updated.current_run_id else updated.status)
        if exit_code is not None:
            if state_status == "finished":
                return replace(updated, status="finished")
            if state_status == "waiting_for_approval":
                return replace(updated, status="waiting_for_approval")
            return replace(
                updated,
                status="failed" if exit_code != 0 else ("finished" if updated.summary_path else updated.status),
                error=updated.error or (f"session exited with code {exit_code}" if exit_code != 0 else ""),
            )
        return updated

    def _session_path(self, session_id: str) -> Path:
        return self.settings.sessions_dir / f"{session_id}.json"

    def _log_path(self, session_id: str) -> Path:
        return self.settings.session_logs_dir / f"{session_id}.log"

    def _workspace_session_dir(self, session_id: str) -> Path:
        return self.settings.workspace_dir / "sessions" / session_id

    def _workspace_state_path(self, session_id: str) -> Path:
        return self._workspace_session_dir(session_id) / "state.json"

    def _workspace_transcript_path(self, session_id: str) -> Path:
        return self._workspace_session_dir(session_id) / "transcript.jsonl"

    def _read_session(self, session_id: str) -> SessionRecord:
        return SessionRecord.from_dict(json.loads(self._session_path(session_id).read_text(encoding="utf-8")))

    def _write_session(self, record: SessionRecord) -> None:
        path = self._session_path(record.session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def _read_workspace_state(self, session_id: str) -> dict:
        path = self._workspace_state_path(session_id)
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_workspace_state(self, session_id: str, payload: dict) -> None:
        path = self._workspace_state_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def _append_transcript(self, session_id: str, payload: dict) -> None:
        path = self._workspace_transcript_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")

    def _read_transcript(self, session_id: str) -> list[dict]:
        path = self._workspace_transcript_path(session_id)
        if not path.exists():
            return []
        items: list[dict] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            payload = json.loads(line)
            if isinstance(payload, dict):
                items.append(payload)
        return items

    def _current_screenshot(self, session_id: str, workspace_state: dict, session: SessionRecord) -> dict | None:
        explicit = str(workspace_state.get("current_screenshot_path", "")).strip()
        if explicit:
            path = self.settings.workspace_dir / explicit
            if path.exists() and path.is_file():
                return _artifact_dict(path, self.settings.workspace_dir)
        if session.status in ACTIVE_SESSION_STATUSES:
            images = self._scan_session_images(session_id)
            if images:
                return images[0]
        return None

    def _recent_screenshots(self, session_id: str, current_screenshot: dict | None) -> list[dict]:
        images = self._scan_session_images(session_id)
        if current_screenshot and not any(item["relative_path"] == current_screenshot["relative_path"] for item in images):
            images.insert(0, current_screenshot)
        return images

    def _scan_session_images(self, session_id: str) -> list[dict]:
        root = self._workspace_session_dir(session_id) / "artifacts"
        if not root.exists():
            return []
        images = [path for path in root.rglob("*") if path.is_file() and artifact_kind(path) == "image"]
        images.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        return [_artifact_dict(path, self.settings.workspace_dir) for path in images]

    def _session_artifacts(self, session_id: str) -> list[dict]:
        root = self._workspace_session_dir(session_id) / "artifacts"
        if not root.exists():
            return []
        paths = [path for path in root.rglob("*") if path.is_file()]
        paths.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        return [_artifact_dict(path, self.settings.workspace_dir) for path in paths]

    def _tail_log(self, session_id: str, *, max_lines: int = 40) -> str:
        path = self._log_path(session_id)
        if not path.exists():
            return ""
        lines = path.read_text(encoding="utf-8").splitlines()
        visible = [line for line in lines if not line.startswith(EXIT_MARKER_PREFIX)]
        return "\n".join(visible[-max_lines:])

    def _read_exit_code(self, session_id: str) -> int | None:
        path = self._log_path(session_id)
        if not path.exists():
            return None
        for line in reversed(path.read_text(encoding="utf-8").splitlines()):
            if line.startswith(EXIT_MARKER_PREFIX):
                raw = line.partition("=")[2].strip()
                if raw:
                    return int(raw)
        return None


def _artifact_dict(path: Path, workspace_root: Path) -> dict:
    relative_path = path.resolve().relative_to(workspace_root.resolve()).as_posix()
    return {
        "name": path.name,
        "relative_path": relative_path,
        "kind": artifact_kind(path),
        "modified_at": datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds"),
        "size_bytes": path.stat().st_size,
        "url": f"/artifacts/{relative_path}",
    }


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
