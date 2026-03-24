"""Tests for cli/start.py and cli/stop.py deployment commands."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
START_CLI_PATH = ROOT / "cli" / "start.py"
STOP_CLI_PATH = ROOT / "cli" / "stop.py"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def start_cli():
    return load_module("start_cli", START_CLI_PATH)


@pytest.fixture()
def stop_cli():
    return load_module("stop_cli", STOP_CLI_PATH)


# ---------------------------------------------------------------------------
# cli/start.py tests
# ---------------------------------------------------------------------------

def test_start_builds_and_launches(start_cli, tmp_path, capsys) -> None:
    """start.py calls docker compose build then docker compose up in order."""
    call_log: list[str] = []

    def fake_compose(*args, **kw):
        # Record which compose sub-command was called
        args_list = list(args)
        if args_list:
            call_log.append(args_list[0])
        r = MagicMock()
        r.returncode = 0
        r.stdout = ""
        r.stderr = ""
        return r

    def fake_healthy(url, label, timeout=90):
        return True

    with patch.object(start_cli, "docker_compose", fake_compose), \
         patch.object(start_cli, "wait_healthy", fake_healthy), \
         patch.object(start_cli, "kill_discord_bot"), \
         patch.object(start_cli, "start_discord_bot", return_value=None), \
         patch.object(start_cli, "check_compose_health", return_value=[]):
        result = start_cli.main(["--budget", "1.00"])

    assert result == 0
    # Verify build happens before up
    assert "build" in call_log
    assert "up" in call_log
    build_idx = call_log.index("build")
    up_idx = call_log.index("up")
    assert build_idx < up_idx, "build must happen before up"


def test_start_fresh_nukes_volumes(start_cli, tmp_path, capsys) -> None:
    """With --fresh, docker compose down -v is called to nuke volumes."""
    compose_calls: list[tuple] = []

    def fake_compose(*args, **kw):
        compose_calls.append(args)
        r = MagicMock()
        r.returncode = 0
        r.stdout = ""
        r.stderr = ""
        return r

    def fake_healthy(url, label, timeout=90):
        return True

    with patch.object(start_cli, "docker_compose", fake_compose), \
         patch.object(start_cli, "wait_healthy", fake_healthy), \
         patch.object(start_cli, "kill_discord_bot"), \
         patch.object(start_cli, "start_discord_bot", return_value=None), \
         patch.object(start_cli, "check_compose_health", return_value=[]):
        result = start_cli.main(["--fresh", "--budget", "1.00"])

    assert result == 0
    # First compose call should be "down -v"
    first_call = compose_calls[0]
    assert "down" in first_call
    assert "-v" in first_call


def test_start_launches_discord_bot(start_cli, capsys) -> None:
    """start.py calls kill_discord_bot then start_discord_bot."""
    kill_called = False
    start_called = False
    start_env: dict | None = None

    def fake_kill():
        nonlocal kill_called
        kill_called = True

    def fake_start(env_overrides=None):
        nonlocal start_called, start_env
        start_called = True
        start_env = env_overrides
        return 12345

    def fake_compose(*args, **kw):
        r = MagicMock()
        r.returncode = 0
        r.stdout = ""
        r.stderr = ""
        return r

    def fake_healthy(url, label, timeout=90):
        return True

    with patch.object(start_cli, "docker_compose", fake_compose), \
         patch.object(start_cli, "wait_healthy", fake_healthy), \
         patch.object(start_cli, "kill_discord_bot", fake_kill), \
         patch.object(start_cli, "start_discord_bot", fake_start), \
         patch.object(start_cli, "check_compose_health", return_value=[]):
        result = start_cli.main(["--budget", "2.00", "--model", "gpt-4o"])

    assert result == 0
    assert kill_called, "kill_discord_bot must be called"
    assert start_called, "start_discord_bot must be called"
    # Env overrides should include model and budget
    assert start_env["RSI_BUDGET_USD"] == "2.00"
    assert start_env["RSI_MODEL"] == "gpt-4o"
    output = capsys.readouterr().out
    assert "12345" in output  # PID printed


def test_start_fails_on_unhealthy(start_cli, capsys) -> None:
    """start.py returns 1 when health checks fail."""

    def fake_compose(*args, **kw):
        r = MagicMock()
        r.returncode = 0
        r.stdout = ""
        r.stderr = ""
        return r

    def fake_unhealthy(url, label, timeout=90):
        return False

    with patch.object(start_cli, "docker_compose", fake_compose), \
         patch.object(start_cli, "wait_healthy", fake_unhealthy), \
         patch.object(start_cli, "kill_discord_bot"), \
         patch.object(start_cli, "start_discord_bot", return_value=None), \
         patch.object(start_cli, "check_compose_health", return_value=[]):
        result = start_cli.main(["--budget", "1.00"])

    assert result == 1
    output = capsys.readouterr().err
    assert "unhealthy" in output.lower()


def test_start_build_failure(start_cli, capsys) -> None:
    """start.py returns 1 when docker compose build fails."""
    call_count = 0

    def fake_compose(*args, **kw):
        nonlocal call_count
        call_count += 1
        r = MagicMock()
        r.stderr = "build error"
        r.stdout = ""
        # First call is down, second is build — fail on build
        if "build" in args:
            r.returncode = 1
        else:
            r.returncode = 0
        return r

    with patch.object(start_cli, "docker_compose", fake_compose):
        result = start_cli.main(["--budget", "1.00"])

    assert result == 1


# ---------------------------------------------------------------------------
# cli/stop.py tests
# ---------------------------------------------------------------------------

def test_stop_kills_everything(stop_cli, capsys) -> None:
    """stop.py kills the bot and runs docker compose down."""
    compose_calls: list[tuple] = []

    def fake_compose(*args, **kw):
        compose_calls.append(args)
        r = MagicMock()
        r.returncode = 0
        r.stdout = ""
        r.stderr = ""
        return r

    with patch.object(stop_cli, "docker_compose", fake_compose), \
         patch.object(stop_cli, "_kill_discord_bot") as mock_kill:
        result = stop_cli.main()

    assert result == 0
    mock_kill.assert_called_once()
    # docker compose down called (without -v)
    assert any("down" in c for c in compose_calls)
    # -v should NOT be in the down call (preserve volumes)
    for c in compose_calls:
        if "down" in c:
            assert "-v" not in c, "stop.py must NOT nuke volumes"

    output = capsys.readouterr().out
    assert "All stopped" in output


def test_stop_no_volume_removal(stop_cli) -> None:
    """Verify stop.py never passes -v to docker compose down."""
    compose_args_seen: list[tuple] = []

    def fake_compose(*args, **kw):
        compose_args_seen.append(args)
        r = MagicMock()
        r.returncode = 0
        r.stdout = ""
        r.stderr = ""
        return r

    with patch.object(stop_cli, "docker_compose", fake_compose), \
         patch.object(stop_cli, "_kill_discord_bot"):
        stop_cli.main()

    for args in compose_args_seen:
        assert "-v" not in args
