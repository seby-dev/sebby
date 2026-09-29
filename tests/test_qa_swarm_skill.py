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


def test_it_says_what_this_version_dispatches_and_doesnt_run() -> None:
    text = body()
    assert "only `qa-tester`" not in text
    assert "dispatches `qa-tester` (" in text and "`adversarial-tester` (" in text
    assert "It doesn't run security testers, load, or a Caddy layer yet." in text


def test_it_uses_the_default_state_folder_and_lists_the_launcher_path_and_start_failures() -> None:
    text = body()
    assert "default `--out`" in text and "--out <state file>" not in text
    assert "absolute path of `scripts/qa_env.sh`" in text
    assert "stderr" in text and "sweeps" in text and "an hour idle" in text


def test_start_passes_a_web_root_and_reuses_a_running_instance() -> None:
    text = body()
    assert "--web-root <dir>" in text
    assert "already running" in text


def test_it_prepares_each_collision_scenario_and_writes_a_target_file() -> None:
    text = body()
    for needle in (
        "`[collisions]` section is optional",
        "`prepare`",
        "scripts/qa_env.sh collide",
        "--scenario <id> --assign <slot>=<tester>",
        "shared/<id>/target.json",
        "schema in `collisions.md`",
        "same batch",
        "bash <skill folder>/barrier.sh wait <barrier.dir> <tester>",
        "absolute path of this skill's folder",
        "get into position",
        "`timeout` set to 600000",
        "never re-runs it",
        "Dispatch a scenario's participants at once, in the same batch",
        "a second scenario that consumes a resource an earlier one in this run already consumes",
    ):
        assert needle in text, needle


def test_a_scenario_that_consumes_a_resource_runs_last_and_only_one_per_run() -> None:
    text = body()
    assert "`consumes` a resource" in text and "runs last" in text
    assert "only one scenario that consumes a given resource runs in a run" in text
    assert "re-seeding is out of scope" in text


def test_only_the_adversarial_tester_joins_collisions_and_no_agent_takes_a_model() -> None:
    text = body()
    assert "Only `adversarial-tester` joins a collision scenario" in text
    assert "each by its typed agent with no `model` override" in text


def test_it_reports_the_spread_and_never_reports_abandoned_as_passed() -> None:
    text = body()
    for needle in (
        'bash "${SEBBY_ROOT:-$HOME/Developer/sebby}/claude/skills/qa-swarm/barrier.sh"'
        " spread <barrier.dir>",
        "run `ls <run folder>/shared/<id>`",
        '"Collision scenarios" section',
        "`abandoned`",
        "never as passed",
    ):
        assert needle in text, needle


def test_shared_is_written_only_by_the_prepare_step_and_the_barrier() -> None:
    text = body()
    assert "only by the orchestrator's prepare step and `barrier.sh`" in text
    assert "6. Write only to the tester's own folder in the run." in text
    assert "doesn't catch a Bash write" in text and "step 7 lists each `shared/<id>`" in text


def test_the_step_numbers_the_last_line_cites_are_the_checking_steps() -> None:
    text = body()
    assert "so steps 7 and 10 check after the fact." in text
    assert "7. When a batch finishes, run `git status --porcelain`" in text
    assert "10. Merge and de-duplicate the findings" in text


def test_after_the_batch_it_checks_released_count_against_the_acted_entries() -> None:
    text = body()
    for needle in (
        "Its `outcome` is authoritative, not a participant's exit code.",
        "Its `released_count` must equal the number of `acted` entries",
        "a SIGKILL, which can't be trapped",
        'report that scenario as "not a real collision", not as passed',
        "`abandoned`, or `not a real collision`",
    ):
        assert needle in text, needle


def test_the_seed_makes_two_runs_one_with_a_share() -> None:
    text = body()
    assert "two seeded runs, one with a share and one without" in text
    assert "one seeded run and share" not in text
