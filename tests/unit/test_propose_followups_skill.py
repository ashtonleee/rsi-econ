from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / ".agents" / "skills" / "propose-followups"

pytestmark = pytest.mark.fast


def test_propose_followups_skill_is_evidence_bound_and_capped() -> None:
    skill_text = (SKILL / "SKILL.md").read_text(encoding="utf-8")

    assert skill_text.startswith("---\nname: propose-followups\n")
    assert "at most 1-3" in skill_text
    assert "Prefer zero issues over weak issues." in skill_text
    assert "STAGE_STATUS.md" in skill_text
    assert "plans/INDEX.md" in skill_text
    assert "runtime/post_task_updates/latest_linear_update.md" in skill_text
    assert "backlog/proposed items only" in skill_text
    assert "Do not create active work." in skill_text
    assert "Do not invent evidence." in skill_text
    assert "Use the existing Linear integration already available to Codex." in skill_text


def test_propose_followups_skill_is_explicit_only() -> None:
    metadata_text = (SKILL / "agents" / "openai.yaml").read_text(encoding="utf-8")

    assert 'display_name: "Propose Follow-Ups"' in metadata_text
    assert 'short_description: "Create bounded backlog follow-ups"' in metadata_text
    assert (
        'default_prompt: "Use $propose-followups to create 1-3 repo-grounded backlog follow-up issues from the just-finished task."'
        in metadata_text
    )
    assert "allow_implicit_invocation: false" in metadata_text
