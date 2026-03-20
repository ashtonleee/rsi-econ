from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / ".agents" / "skills" / "finish-task"

pytestmark = pytest.mark.fast


def test_finish_task_skill_routes_closeout_through_finish() -> None:
    skill_text = (SKILL / "SKILL.md").read_text(encoding="utf-8")

    assert skill_text.startswith("---\nname: finish-task\n")
    assert "description:" in skill_text
    assert "./finish" in skill_text
    assert "--changed-file" in skill_text
    assert "--test" in skill_text
    assert "The default behavior is to close out from the current session" in skill_text
    assert "Gather the summary, changed files, and tests from the current session context before asking the user anything." in skill_text
    assert "If there is no suitable explicit test record yet, run `./scripts/preflight.sh` or a narrower relevant verification command" in skill_text
    assert "Do not infer changed files from ambient git status." in skill_text
    assert "Do not upgrade a test result to `passed` unless a passing command ran in this session." in skill_text
    assert "Do not ask the user to restate summary, changed files, or tests that are already available from the current session." in skill_text


def test_finish_task_skill_is_explicit_only() -> None:
    metadata_text = (SKILL / "agents" / "openai.yaml").read_text(encoding="utf-8")

    assert 'display_name: "Finish Task"' in metadata_text
    assert 'short_description: "Close out bounded repo work"' in metadata_text
    assert 'default_prompt: "Use $finish-task to wrap up this bounded task from the current session context via ./finish."' in metadata_text
    assert "allow_implicit_invocation: false" in metadata_text
