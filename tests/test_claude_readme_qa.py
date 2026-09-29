"""claude/README.md lists the QA swarm's assets and how to install and roll back the skill."""

import re
from pathlib import Path

README = Path(__file__).resolve().parent.parent / "claude" / "README.md"


def text() -> str:
    return README.read_text(encoding="utf-8")


def test_the_asset_list_names_the_agent_the_skill_and_the_hook() -> None:
    body = text()
    for needle in ("qa-tester", "skills/qa-swarm/", "qa_tester_guard.py"):
        assert needle in body, needle


def test_the_install_step_symlinks_the_skill() -> None:
    assert "ln -s ~/Developer/sebby/claude/skills/qa-swarm ~/.claude/skills/qa-swarm" in text()


def test_it_says_the_guard_isnt_in_settings_hooks_and_why() -> None:
    body = text()
    assert "settings-hooks.json" in body
    section = body.split("qa_tester_guard.py", 1)[1]
    assert re.search(r"tester agents only|agent's frontmatter|each agent's frontmatter", section)


def test_rollback_removes_the_skill_symlink() -> None:
    body = text()
    assert re.search(
        r"Remove the .*qa-swarm.* symlink|remove the `~/.claude/skills/qa-swarm` symlink",
        body,
        re.IGNORECASE,
    )


def test_the_existing_hook_docs_are_intact() -> None:
    body = text()
    for needle in ("agent_model_guard.py", "cd_only_reminder.py", "Each hook command fails open"):
        assert needle in body, needle


def test_the_asset_list_names_the_adversarial_tester_the_barrier_and_the_collisions_guide() -> None:
    body = text()
    for needle in ("`adversarial-tester`", "`barrier.sh`", "`collisions.md`"):
        assert needle in body, needle


def test_the_readme_says_the_barrier_needs_no_execute_bit() -> None:
    body = " ".join(text().split())
    assert "bash <skill folder>/barrier.sh" in body and "needs no execute bit" in body


def test_rollback_covers_both_agents_and_the_barrier() -> None:
    body = " ".join(text().split())
    assert (
        "`qa-tester` and `adversarial-tester` agents, the barrier script, and the guard hook"
        in body
    )
