"""Tests for the provider management CLI (cli/providers.py)."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
CLI_PATH = ROOT / "cli" / "providers.py"


def load_cli(tmp_path: Path):
    module_name = f"test_providers_cli_{tmp_path.name}"
    spec = importlib.util.spec_from_file_location(module_name, CLI_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_list_shows_proposals(tmp_path: Path) -> None:
    mod = load_cli(tmp_path)

    # Set up local proposals dir
    proposals_dir = tmp_path / "proposals"
    proposals_dir.mkdir()
    (proposals_dir / "abc.json").write_text(json.dumps({
        "proposal_id": "abc12345",
        "name": "groq-free",
        "model_id": "groq/llama-3.3-70b-instruct",
        "status": "pending_operator",
        "signup_url": "https://console.groq.com",
        "free_tier": "1000 req/day",
    }))

    mod.PROVIDER_PROPOSALS_DIR = proposals_dir

    # Mock bridge as unreachable so it reads local files
    with patch.object(mod, "bridge_get", return_value=None):
        out = io.StringIO()
        import argparse
        args = argparse.Namespace()
        mod.cmd_list(args, out=out)

    output = out.getvalue()
    assert "groq-free" in output
    assert "pending_operator" in output
    assert "console.groq.com" in output


def test_add_writes_env(tmp_path: Path) -> None:
    mod = load_cli(tmp_path)

    env_file = tmp_path / ".env"
    env_file.write_text("OPENROUTER_API_KEY=sk-existing\n")
    mod.ENV_FILE = env_file

    mod._set_env_var("GROQ_API_KEY", "gsk_test123")

    content = env_file.read_text()
    assert "GROQ_API_KEY=gsk_test123" in content
    assert "OPENROUTER_API_KEY=sk-existing" in content


def test_add_updates_env_existing_commented(tmp_path: Path) -> None:
    mod = load_cli(tmp_path)

    env_file = tmp_path / ".env"
    env_file.write_text("OPENROUTER_API_KEY=sk-existing\n# GROQ_API_KEY=\n")
    mod.ENV_FILE = env_file

    mod._set_env_var("GROQ_API_KEY", "gsk_test123")

    content = env_file.read_text()
    assert "GROQ_API_KEY=gsk_test123" in content
    assert "# GROQ_API_KEY=" not in content


def test_add_updates_litellm_config(tmp_path: Path) -> None:
    mod = load_cli(tmp_path)

    config_file = tmp_path / "litellm_config.yaml"
    config_file.write_text(
        'model_list:\n'
        '  - model_name: "default"\n'
        '    litellm_params:\n'
        '      model: "openrouter/minimax/minimax-m2.7"\n'
        '      api_key: "os.environ/OPENROUTER_API_KEY"\n'
        '  # --- DYNAMIC MODELS ---\n'
        '\n'
        'litellm_settings:\n'
        '  max_budget: 5.0\n'
    )
    mod.LITELLM_CONFIG = config_file

    result = mod._add_litellm_model("llama-3.3-70b-versatile", "groq/llama-3.3-70b-versatile", "GROQ_API_KEY")
    assert result is True

    content = config_file.read_text()
    assert 'model_name: "llama-3.3-70b-versatile"' in content
    assert 'model: "groq/llama-3.3-70b-versatile"' in content
    assert 'api_key: "os.environ/GROQ_API_KEY"' in content
    # Make sure it's after the marker
    marker_pos = content.index("DYNAMIC MODELS")
    model_pos = content.index("llama-3.3-70b-versatile")
    assert model_pos > marker_pos


def test_add_litellm_config_no_duplicate(tmp_path: Path) -> None:
    mod = load_cli(tmp_path)

    config_file = tmp_path / "litellm_config.yaml"
    config_file.write_text(
        'model_list:\n'
        '  - model_name: "llama-3.3-70b"\n'
        '    litellm_params:\n'
        '      model: "groq/llama-3.3-70b"\n'
        '      api_key: "os.environ/GROQ_API_KEY"\n'
        '  # --- DYNAMIC MODELS ---\n'
    )
    mod.LITELLM_CONFIG = config_file

    result = mod._add_litellm_model("llama-3.3-70b", "groq/llama-3.3-70b", "GROQ_API_KEY")
    assert result is True  # Returns True but skips

    content = config_file.read_text()
    assert content.count("llama-3.3-70b") == 2  # model_name + model lines, no new ones


def test_reload_restarts_litellm(tmp_path: Path) -> None:
    mod = load_cli(tmp_path)

    class FakeResult:
        returncode = 0
        stdout = '{"State": "running", "Health": "healthy"}'

    with patch.object(mod, "docker_compose", return_value=FakeResult()):
        out = io.StringIO()
        import argparse
        args = argparse.Namespace()
        rc = mod.cmd_reload(args, out=out)

    assert rc == 0
    assert "healthy" in out.getvalue().lower() or "restart" in out.getvalue().lower()


def test_find_proposal_by_provider(tmp_path: Path) -> None:
    mod = load_cli(tmp_path)

    proposals_dir = tmp_path / "proposals"
    proposals_dir.mkdir()
    (proposals_dir / "abc.json").write_text(json.dumps({
        "proposal_id": "abc12345",
        "name": "groq-free",
        "provider": "groq",
        "model_id": "groq/llama-3.3-70b-versatile",
    }))

    mod.PROVIDER_PROPOSALS_DIR = proposals_dir

    result = mod._find_proposal_by_provider("groq")
    assert result is not None
    assert result["model_id"] == "groq/llama-3.3-70b-versatile"

    result2 = mod._find_proposal_by_provider("nonexistent")
    assert result2 is None


def test_remove_litellm_model(tmp_path: Path) -> None:
    mod = load_cli(tmp_path)

    config_file = tmp_path / "litellm_config.yaml"
    config_file.write_text(
        'model_list:\n'
        '  - model_name: "default"\n'
        '    litellm_params:\n'
        '      model: "openrouter/minimax/minimax-m2.7"\n'
        '      api_key: "os.environ/OPENROUTER_API_KEY"\n'
        '  - model_name: "llama-3.3-70b-versatile"\n'
        '    litellm_params:\n'
        '      model: "groq/llama-3.3-70b-versatile"\n'
        '      api_key: "os.environ/GROQ_API_KEY"\n'
        'litellm_settings:\n'
        '  max_budget: 5.0\n'
    )
    mod.LITELLM_CONFIG = config_file

    result = mod._remove_litellm_model("llama-3.3-70b-versatile")
    assert result is True

    content = config_file.read_text()
    assert "llama-3.3-70b-versatile" not in content
    assert "default" in content  # Original models intact
    assert "litellm_settings" in content
