import os
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


def write_fake_docker(tmp_path: Path) -> Path:
    docker_path = tmp_path / "docker"
    docker_path.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "log=${RSI_FAKE_DOCKER_LOG:?}\n"
        "if [[ \"$1\" == \"info\" ]]; then\n"
        "  exit 0\n"
        "fi\n"
        "if [[ \"$1\" == \"compose\" ]]; then\n"
        "  printf 'argv=%s\\n' \"$*\" >> \"$log\"\n"
        "  printf 'mode=%s base=%s allowlist=%s budget=%s key_present=%s smoke_model=%s\\n' \\\n"
        "    \"${RSI_LITELLM_RESPONSE_MODE-}\" \\\n"
        "    \"${RSI_OPENAI_BASE_URL-}\" \\\n"
        "    \"${RSI_WEB_ALLOWLIST_HOSTS-}\" \\\n"
        "    \"${RSI_LLM_BUDGET_TOKEN_CAP-}\" \\\n"
        "    \"${OPENAI_API_KEY:+1}\" \\\n"
        "    \"${RSI_PROVIDER_SMOKE_MODEL-}\" >> \"$log\"\n"
        "  args=(\"$@\")\n"
        "  for ((i=0; i<${#args[@]}; i++)); do\n"
        "    if [[ \"${args[$i]}\" == \"--script\" ]]; then\n"
        "      script_path=\"${args[$((i+1))]}\"\n"
        "      if [[ \"$script_path\" != /* ]]; then\n"
        "        script_path=\"${RSI_PROVIDER_WORKSPACE_DIR:?}/$script_path\"\n"
        "      fi\n"
        "      printf 'script=%s\\n' \"$script_path\" >> \"$log\"\n"
        "      cat \"$script_path\" >> \"$log\"\n"
        "    fi\n"
        "  done\n"
        "  exit 0\n"
        "fi\n"
        "echo \"unexpected docker invocation: $*\" >&2\n"
        "exit 1\n",
        encoding="ascii",
    )
    docker_path.chmod(0o755)
    return docker_path


def run_provider_script(tmp_path: Path, *args: str, env_text: str) -> tuple[subprocess.CompletedProcess[str], str]:
    env_file = tmp_path / ".env.provider.local"
    env_file.write_text(env_text, encoding="ascii")
    log_path = tmp_path / "docker.log"
    write_fake_docker(tmp_path)
    workspace_dir = tmp_path / "agent_workspace"
    workspace_dir.mkdir()

    env = os.environ.copy()
    env["PATH"] = f"{tmp_path}:{env['PATH']}"
    env["RSI_PROVIDER_ENV_FILE"] = str(env_file)
    env["RSI_FAKE_DOCKER_LOG"] = str(log_path)
    env["RSI_PROVIDER_WORKSPACE_DIR"] = str(workspace_dir)

    result = subprocess.run(
        ["bash", str(ROOT / "scripts/provider.sh"), *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, log_path.read_text(encoding="ascii")


def run_provider_command(
    tmp_path: Path,
    command: str,
    *args: str,
    env_text: str,
    plan_files: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], str]:
    workspace_dir = tmp_path / "agent_workspace"
    plan_dir = workspace_dir / ".seed_plans"
    plan_dir.mkdir(parents=True)
    for filename, contents in (plan_files or {}).items():
        (plan_dir / filename).write_text(contents, encoding="utf-8")

    env_file = tmp_path / ".env.provider.local"
    env_file.write_text(env_text, encoding="ascii")
    log_path = tmp_path / "docker.log"
    write_fake_docker(tmp_path)

    env = os.environ.copy()
    env["PATH"] = f"{tmp_path}:{env['PATH']}"
    env["RSI_PROVIDER_ENV_FILE"] = str(env_file)
    env["RSI_FAKE_DOCKER_LOG"] = str(log_path)
    env["RSI_PROVIDER_WORKSPACE_DIR"] = str(workspace_dir)

    result = subprocess.run(
        ["bash", str(ROOT / "scripts/provider.sh"), command, *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, log_path.read_text(encoding="utf-8")


def run_provider_answer_packet(
    tmp_path: Path,
    *args: str,
    env_text: str,
    plan_model: str = "gpt-4.1-mini",
) -> tuple[subprocess.CompletedProcess[str], str]:
    return run_provider_command(
        tmp_path,
        "answer-packet",
        *args,
        env_text=env_text,
        plan_files={
            "stage6_answer_packet_provider.json": (
                "[\n"
                "  {\n"
                "    \"kind\": \"bridge_chat\",\n"
                f"    \"model\": \"{plan_model}\",\n"
                "    \"message\": \"reply\"\n"
                "  },\n"
                "  {\n"
                "    \"kind\": \"write_file\",\n"
                "    \"path\": \"research/current_answer.md\",\n"
                "    \"content_template\": \"llm_model={last_bridge_chat_model}\\n\"\n"
                "  }\n"
                "]\n"
            )
        },
    )


@pytest.mark.fast
def test_provider_init_creates_local_env_file_with_private_permissions(tmp_path: Path):
    template_file = tmp_path / ".env.provider.local.example"
    template_file.write_text("OPENAI_API_KEY=replace-me\n", encoding="ascii")
    env_file = tmp_path / ".env.provider.local"

    env = os.environ.copy()
    env["RSI_PROVIDER_ENV_FILE"] = str(env_file)
    env["RSI_PROVIDER_TEMPLATE_FILE"] = str(template_file)

    result = subprocess.run(
        ["bash", str(ROOT / "scripts/provider.sh"), "init"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert env_file.read_text(encoding="ascii") == "OPENAI_API_KEY=replace-me\n"
    assert (env_file.stat().st_mode & 0o777) == 0o600


@pytest.mark.fast
def test_provider_up_sources_local_env_and_enables_passthrough(tmp_path: Path):
    result, log = run_provider_script(
        tmp_path,
        "up",
        env_text=(
            "OPENAI_API_KEY=sk-test-provider-key\n"
            "RSI_OPENAI_BASE_URL=https://openrouter.ai/api/v1\n"
            "RSI_WEB_ALLOWLIST_HOSTS=docs.python.org\n"
            "RSI_LLM_BUDGET_TOKEN_CAP=4000\n"
        ),
    )

    assert result.returncode == 0, result.stderr
    assert "argv=compose up --build -d --wait" in log
    assert "mode=provider_passthrough" in log
    assert "base=https://openrouter.ai/api/v1" in log
    assert "allowlist=docs.python.org" in log
    assert "budget=4000" in log
    assert "key_present=1" in log


@pytest.mark.fast
def test_provider_smoke_accepts_explicit_model_override(tmp_path: Path):
    result, log = run_provider_script(
        tmp_path,
        "smoke",
        "--model",
        "openai/gpt-4.1-mini",
        "--message",
        "say ok",
        env_text=(
            "OPENAI_API_KEY=sk-test-provider-key\n"
            "RSI_PROVIDER_SMOKE_MODEL=anthropic/claude-3.7-sonnet\n"
        ),
    )

    assert result.returncode == 0, result.stderr
    assert "argv=compose exec -T agent python -m untrusted.agent.bridge_client chat --model openai/gpt-4.1-mini --message say ok" in log
    assert "mode=provider_passthrough" in log
    assert "key_present=1" in log
    assert "smoke_model=anthropic/claude-3.7-sonnet" in log


@pytest.mark.fast
def test_provider_answer_packet_defaults_to_openai_direct_model(tmp_path: Path):
    result, log = run_provider_answer_packet(
        tmp_path,
        "--task",
        "summarize pathlib",
        "--input-url",
        "https://docs.python.org/3/library/pathlib.html",
        env_text="OPENAI_API_KEY=sk-test-provider-key\n",
    )

    assert result.returncode == 0, result.stderr
    assert "argv=compose exec -T agent python -m untrusted.agent.seed_runner --task summarize pathlib --planner scripted" in log
    assert "mode=provider_passthrough" in log
    assert '"model": "gpt-4.1-mini"' in log
    assert 'llm_model={last_bridge_chat_model}' in log


@pytest.mark.fast
def test_provider_answer_packet_accepts_openrouter_model_override(tmp_path: Path):
    result, log = run_provider_answer_packet(
        tmp_path,
        "--task",
        "summarize pathlib",
        "--input-url",
        "https://docs.python.org/3/library/pathlib.html",
        "--model",
        "openai/gpt-4.1-mini",
        env_text=(
            "OPENAI_API_KEY=sk-test-provider-key\n"
            "RSI_PROVIDER_ANSWER_MODEL=anthropic/claude-3.7-sonnet\n"
        ),
    )

    assert result.returncode == 0, result.stderr
    assert "mode=provider_passthrough" in log
    assert '"model": "openai/gpt-4.1-mini"' in log
    assert 'llm_model={last_bridge_chat_model}' in log


@pytest.mark.fast
def test_provider_seed_run_rewrites_all_bridge_chat_models_and_passes_optional_inputs(tmp_path: Path):
    result, log = run_provider_command(
        tmp_path,
        "seed-run",
        "--script",
        ".seed_plans/stage6_follow_answer_packet.json",
        "--task",
        "answer from one followed page",
        "--input-url",
        "https://example.com/follow-source",
        "--follow-target-url",
        "https://example.com/follow-target",
        "--proposal-target-url",
        "https://example.com/post-target",
        "--max-steps",
        "12",
        "--model",
        "openai/gpt-4.1-mini",
        env_text="OPENAI_API_KEY=sk-test-provider-key\n",
        plan_files={
            "stage6_follow_answer_packet.json": (
                "[\n"
                "  {\n"
                "    \"kind\": \"bridge_chat\",\n"
                "    \"model\": \"stage1-deterministic\",\n"
                "    \"message\": \"first\"\n"
                "  },\n"
                "  {\n"
                "    \"kind\": \"bridge_chat\",\n"
                "    \"model\": \"gpt-4.1-mini\",\n"
                "    \"message\": \"second\"\n"
                "  },\n"
                "  {\n"
                "    \"kind\": \"write_file\",\n"
                "    \"path\": \"research/current_follow_answer.md\",\n"
                "    \"content_template\": \"llm_model={last_bridge_chat_model}\\n\"\n"
                "  }\n"
                "]\n"
            )
        },
    )

    assert result.returncode == 0, result.stderr
    assert (
        "argv=compose exec -T agent python -m untrusted.agent.seed_runner --task answer from one followed page "
        "--planner scripted --script .seed_plans/"
        in log
    )
    assert "--input-url https://example.com/follow-source" in log
    assert "--follow-target-url https://example.com/follow-target" in log
    assert "--proposal-target-url https://example.com/post-target" in log
    assert "--max-steps 12" in log
    assert log.count('"model": "openai/gpt-4.1-mini"') == 2
    assert '"model": "stage1-deterministic"' not in log
    assert 'llm_model={last_bridge_chat_model}' in log


@pytest.mark.fast
def test_provider_seed_run_allows_zero_bridge_chat_actions(tmp_path: Path):
    result, log = run_provider_command(
        tmp_path,
        "seed-run",
        "--script",
        ".seed_plans/no_chat.json",
        "--task",
        "status only",
        env_text="OPENAI_API_KEY=sk-test-provider-key\n",
        plan_files={
            "no_chat.json": (
                "[\n"
                "  {\n"
                "    \"kind\": \"bridge_status\"\n"
                "  },\n"
                "  {\n"
                "    \"kind\": \"finish\",\n"
                "    \"summary\": \"no chat\"\n"
                "  }\n"
                "]\n"
            )
        },
    )

    assert result.returncode == 0, result.stderr
    assert "argv=compose exec -T agent python -m untrusted.agent.seed_runner --task status only --planner scripted" in log
    assert '"kind": "bridge_status"' in log


@pytest.mark.fast
def test_provider_follow_answer_packet_wrapper_selects_follow_plan(tmp_path: Path):
    result, log = run_provider_command(
        tmp_path,
        "follow-answer-packet",
        "--task",
        "answer from one followed page",
        "--input-url",
        "https://example.com/follow-source",
        "--follow-target-url",
        "https://example.com/follow-target",
        "--model",
        "openai/gpt-4.1-mini",
        env_text="OPENAI_API_KEY=sk-test-provider-key\n",
        plan_files={
            "stage6_follow_answer_packet.json": (
                "[\n"
                "  {\n"
                "    \"kind\": \"bridge_chat\",\n"
                "    \"model\": \"stage1-deterministic\",\n"
                "    \"message\": \"reply\"\n"
                "  },\n"
                "  {\n"
                "    \"kind\": \"write_file\",\n"
                "    \"path\": \"research/current_follow_answer.md\",\n"
                "    \"content_template\": \"llm_model={last_bridge_chat_model}\\n\"\n"
                "  }\n"
                "]\n"
            )
        },
    )

    assert result.returncode == 0, result.stderr
    assert "--follow-target-url https://example.com/follow-target" in log
    assert '"path": "research/current_follow_answer.md"' in log
    assert '"model": "openai/gpt-4.1-mini"' in log
