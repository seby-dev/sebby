"""The qa-swarm skill covers the orchestration steps and the safety rules."""

import re
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent / "claude" / "skills" / "qa-swarm" / "SKILL.md"


def body() -> str:
    return SKILL.read_text(encoding="utf-8")


def test_frontmatter_names_the_skill_and_says_when_to_use_it() -> None:
    head = body().split("\n---", 1)[0]
    assert re.search(r"(?m)^name: qa-swarm$", head)
    assert re.search(r"(?m)^description: Use when", head)


def test_it_reads_qa_toml_checks_schema_and_secrets_first() -> None:
    text = body()
    for needle in (
        ".claude/qa.toml",
        "schema_version",
        "scenario-run --check",
        "names the variable",
        "absolute path",
        "<cli folder>",
    ):
        assert needle in text, needle


def test_it_uses_every_qa_env_subcommand_the_slice_ships() -> None:
    text = body()
    for sub in ("qa_env.sh start", "qa_env.sh seed", "qa_env.sh session", "qa_env.sh stop"):
        assert sub in text, sub


def test_it_caps_concurrency_and_stops_the_environment_on_every_exit_path() -> None:
    text = body()
    assert "three" in text and "every exit path" in text
    assert "no `model` override" in text or "no model override" in text


def test_it_checks_the_repository_and_the_logs_after_the_testers_finish() -> None:
    text = body()
    for needle in (
        "git status --porcelain",
        "testers/<name>/commands.log",
        "testers/_unattributed/commands.log",
        "vision_forbidden",
        "jev_forbidden",
    ):
        assert needle in text, needle


def test_it_commits_only_passing_scenarios_and_reproduces_blockers_and_highs() -> None:
    text = body()
    for needle in (
        "tests/qa_swarm/scenarios/",
        "orchestrator, not the tester",
        "blocker and high",
        "REPORT.md",
    ):
        assert needle in text, needle


def test_it_says_what_this_version_doesnt_run() -> None:
    text = body()
    assert "only `qa-tester`" in text


def test_it_uses_the_default_state_folder_and_lists_the_launcher_path_and_start_failures() -> None:
    text = body()
    assert "default `--out`" in text and "--out <state file>" not in text
    assert "absolute path of `scripts/qa_env.sh`" in text
    assert "stderr" in text and "sweeps" in text and "an hour idle" in text
