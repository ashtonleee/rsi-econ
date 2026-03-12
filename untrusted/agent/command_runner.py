from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def _read_capped(handle, *, limit_bytes: int) -> tuple[str, bool]:
    handle.seek(0)
    data = handle.read(limit_bytes + 1)
    truncated = len(data) > limit_bytes
    return data[:limit_bytes].decode("utf-8", errors="replace"), truncated


@dataclass
class CommandResult:
    argv: list[str]
    cwd: str
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    stdout_truncated: bool
    stderr_truncated: bool


class BoundedCommandRunner:
    def __init__(
        self,
        workspace_dir: Path,
        *,
        default_timeout_seconds: float = 20.0,
        output_limit_bytes: int = 4096,
    ):
        self.workspace_dir = workspace_dir.resolve()
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.default_timeout_seconds = default_timeout_seconds
        self.output_limit_bytes = output_limit_bytes

    def _resolve_argv(self, argv: list[str]) -> list[str]:
        if not argv:
            raise ValueError("argv must not be empty")
        if argv[0] != "python":
            raise ValueError(f"command not allowed in stage3 runner: {argv[0]}")
        return [sys.executable, *argv[1:]]

    def _env(self) -> dict[str, str]:
        return {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONUNBUFFERED": "1",
        }

    def run(
        self,
        argv: list[str],
        *,
        timeout_seconds: float | None = None,
        output_limit_bytes: int | None = None,
    ) -> CommandResult:
        resolved_argv = self._resolve_argv(argv)
        timeout = timeout_seconds or self.default_timeout_seconds
        output_limit = output_limit_bytes or self.output_limit_bytes

        with tempfile.TemporaryFile() as stdout_handle, tempfile.TemporaryFile() as stderr_handle:
            process = subprocess.Popen(
                resolved_argv,
                cwd=self.workspace_dir,
                env=self._env(),
                stdin=subprocess.DEVNULL,
                stdout=stdout_handle,
                stderr=stderr_handle,
            )
            timed_out = False
            try:
                returncode = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                process.kill()
                returncode = process.wait()

            stdout, stdout_truncated = _read_capped(stdout_handle, limit_bytes=output_limit)
            stderr, stderr_truncated = _read_capped(stderr_handle, limit_bytes=output_limit)

        return CommandResult(
            argv=list(argv),
            cwd=str(self.workspace_dir),
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
        )
