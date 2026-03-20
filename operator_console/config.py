from dataclasses import dataclass
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BRIDGE_URL = "http://127.0.0.1:8000"
DEFAULT_WORKSPACE_DIR = ROOT / "untrusted" / "agent_workspace"
DEFAULT_TRUSTED_STATE_DIR = ROOT / "runtime" / "trusted_state"
DEFAULT_OPERATOR_RUNTIME_DIR = ROOT / "runtime" / "operator_console"


def _resolve_path(raw: str | None, default: Path) -> Path:
    if raw is None or raw.strip() == "":
        return default
    path = Path(raw.strip())
    if path.is_absolute():
        return path
    return ROOT / path


@dataclass(frozen=True)
class ConsoleSettings:
    bridge_url: str
    operator_token: str | None
    workspace_dir: Path
    trusted_state_dir: Path
    operator_runtime_dir: Path = DEFAULT_OPERATOR_RUNTIME_DIR

    @property
    def repo_root(self) -> Path:
        return ROOT

    @property
    def run_outputs_dir(self) -> Path:
        return self.workspace_dir / "run_outputs"

    @property
    def research_dir(self) -> Path:
        return self.workspace_dir / "research"

    @property
    def trusted_log_path(self) -> Path:
        return self.trusted_state_dir / "logs" / "bridge_events.jsonl"

    @property
    def launches_dir(self) -> Path:
        return self.operator_runtime_dir / "launches"

    @property
    def launch_logs_dir(self) -> Path:
        return self.operator_runtime_dir / "launch_logs"

    @property
    def sessions_dir(self) -> Path:
        return self.operator_runtime_dir / "sessions"

    @property
    def session_logs_dir(self) -> Path:
        return self.operator_runtime_dir / "session_logs"


def console_settings() -> ConsoleSettings:
    raw_token = os.environ.get("RSI_OPERATOR_TOKEN", "").strip()
    return ConsoleSettings(
        bridge_url=os.environ.get(
            "RSI_OPERATOR_CONSOLE_BRIDGE_URL",
            DEFAULT_BRIDGE_URL,
        ).strip(),
        operator_token=raw_token or None,
        workspace_dir=_resolve_path(
            os.environ.get("RSI_OPERATOR_CONSOLE_WORKSPACE_DIR"),
            DEFAULT_WORKSPACE_DIR,
        ),
        trusted_state_dir=_resolve_path(
            os.environ.get("RSI_OPERATOR_CONSOLE_TRUSTED_STATE_DIR"),
            DEFAULT_TRUSTED_STATE_DIR,
        ),
        operator_runtime_dir=_resolve_path(
            os.environ.get("RSI_OPERATOR_CONSOLE_RUNTIME_DIR"),
            DEFAULT_OPERATOR_RUNTIME_DIR,
        ),
    )
