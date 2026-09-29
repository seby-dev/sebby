"""Every path the QA agents and the qa-swarm skill name exists in this checkout."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "claude" / "skills" / "qa-swarm"
AGENTS = ROOT / "claude" / "agents"
NAMING_FILES = [
    AGENTS / "qa-tester.md",
    AGENTS / "adversarial-tester.md",
    SKILL / "SKILL.md",
    SKILL / "browser.md",
    SKILL / "collisions.md",
    SKILL / "report-format.md",
    SKILL / "scenarios.md",
]
# `$SEBBY_ROOT/claude/...` and the frontmatter's `${SEBBY_ROOT:-$HOME/Developer/sebby}/claude/...`.
SEBBY_PATH = re.compile(r"(?:\$SEBBY_ROOT|\$\{SEBBY_ROOT:-[^}]*\})/(claude/[\w./-]+)")
BARRIER_PATH = re.compile(r'[^\s"`]*qa-swarm/barrier\.sh')
GUIDES = ("browser.md", "scenarios.md", "collisions.md", "report-format.md")


def test_every_sebby_root_path_the_agents_and_skill_name_exists() -> None:
    found = 0
    for file in NAMING_FILES:
        for match in SEBBY_PATH.finditer(file.read_text(encoding="utf-8")):
            found += 1
            relative = match.group(1).rstrip(".,")
            assert (ROOT / relative).exists(), f"{file.name} names {relative}"
    assert found >= 6


def test_every_guide_and_agent_the_skill_dispatches_exists() -> None:
    for guide in GUIDES:
        assert (SKILL / guide).is_file(), guide
    skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    for agent in ("qa-tester", "adversarial-tester"):
        assert f"`{agent}`" in skill and (AGENTS / f"{agent}.md").is_file(), agent


def test_each_agent_names_the_guides_it_reads() -> None:
    tester = (AGENTS / "qa-tester.md").read_text(encoding="utf-8")
    adversarial = (AGENTS / "adversarial-tester.md").read_text(encoding="utf-8")
    for guide in ("scenarios.md", "report-format.md", "browser.md"):
        assert guide in tester, guide
    for guide in ("browser.md", "collisions.md", "report-format.md"):
        assert guide in adversarial, guide


def test_the_barrier_is_a_bash_script_and_every_path_to_it_is_run_with_bash() -> None:
    barrier = SKILL / "barrier.sh"
    assert barrier.read_text(encoding="utf-8").startswith("#!/usr/bin/env bash\n")
    mentions = 0
    for file in NAMING_FILES:
        text = file.read_text(encoding="utf-8")
        for match in BARRIER_PATH.finditer(text):
            mentions += 1
            assert re.search(r'bash "?$', text[: match.start()]), f"{file.name}: {match.group(0)}"
    assert mentions >= 4
