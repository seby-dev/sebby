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


def test_it_says_what_this_version_dispatches_and_runs() -> None:
    text = body()
    assert "only `qa-tester`" not in text
    assert "dispatches `qa-tester` (" in text and "`adversarial-tester` (" in text
    assert "`security-tester` (an Opus 5.5 agent" in text
    assert "It doesn't run security testers, load, or a Caddy layer yet." not in text
    assert "dispatch qa-tester, adversarial-tester, and security-tester agents" in text
    assert "runs `loadgen.py` beside a batch when the project enables load" in text
    assert "a restart phase that checks what a restart keeps and clears" in text


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
    assert (
        "Only the orchestrator's prepare step and `barrier.sh` write in the run's `shared/` folder"
        in text
    )
    assert "is written only by" not in text
    assert "6. Write only to the tester's own folder in the run." in text
    assert "doesn't catch a Bash write" in text and "step 7 lists each `shared/<id>`" in text


def test_the_step_numbers_the_last_line_cites_are_the_checking_steps() -> None:
    text = body()
    assert "so steps 7 and 11 check after the fact." in text
    assert "7. When a batch finishes, run `git status --porcelain`" in text
    assert "11. Merge and de-duplicate the findings" in text
    assert "12. Run `scripts/qa_env.sh stop --run-dir <run folder>`" in text


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


def test_the_charter_runs_the_collision_first_and_protects_other_scenarios_resources() -> None:
    text = body()
    for needle in (
        "run its scenario first, right after it reads the charter and the guides,"
        " before any free-form testing",
        "never to change a resource that another scenario's `target.json` names",
        "such as the seeded share or a seeded run",
        "every other scenario's `target.json` path",
    ):
        assert needle in text, needle


def test_scenarios_that_share_a_needs_resource_run_in_different_batches() -> None:
    text = body()
    assert "Scenarios that share a `needs` resource" in text
    assert "run in different batches, never in the same batch" in text


def test_a_mismatch_either_way_is_not_a_real_collision() -> None:
    text = body()
    for needle in (
        "greater than the number of `acted` entries means a released participant never recorded"
        " acting",
        "fewer means a participant arrived after the release",
    ):
        assert needle in text, needle


def test_a_load_section_no_longer_stops_the_run() -> None:
    text = body()
    assert "or `[load] enabled` is on, stop" not in text
    assert "`[load] enabled` doesn't stop the run" in text
    assert "The `[load]` and `[security]` sections are optional too." in text


def test_a_security_run_starts_with_caddy_and_says_what_a_missing_binary_does() -> None:
    text = body()
    for needle in (
        "add `--caddy`",
        "over `https://127.0.0.1:<port>`",
        "`CADDY_BIN`, or `caddy` on the `PATH`",
        "`start --caddy` exits 2 and starts nothing",
        "report the header and CSP checks as skipped",
        "`ca_file` and `browser_config` (with Caddy only)",
        "`skipped_checks`",
        "so don't dispatch a `qa-tester` on it",
    ):
        assert needle in text, needle


def test_a_planted_backend_defect_uses_backend_root_and_the_guard_protects_it() -> None:
    text = body()
    assert "add `--backend-root <dir>`" in text
    assert "the backend runs that checkout's `src/`" in text


def test_loadgen_runs_beside_a_batch_only_when_load_is_enabled_and_within_its_caps() -> None:
    text = body()
    for needle in (
        "When `[load] enabled` is true, run `loadgen.py` beside a batch",
        '"${SEBBY_ROOT:-$HOME/Developer/sebby}/claude/skills/qa-swarm/loadgen.py"',
        "--state <state file>",
        "clamps to 8 concurrent requests, 20 a second, and 300 seconds",
        "(exit 64)",
        "(exit 3)",
        "<run folder>/load/summary.json",
        "With three testers in a batch, keep load off unless the user asks.",
    ):
        assert needle in text, needle
    assert re.search(
        r'python3 "\$\{SEBBY_ROOT:-\$HOME/Developer/sebby\}/claude/skills/qa-swarm/loadgen.py"',
        text,
    )


def test_rate_limit_abuse_runs_last_and_alone() -> None:
    text = body()
    assert (
        "Run rate-limit abuse in a final batch, alone, after every other tester has finished"
        in text
    )
    assert "no `loadgen.py` beside it" in text
    assert "whether it's the final abuse batch and the address to exhaust" in text


def test_the_restart_phase_runs_after_every_tester_and_reports_kept_and_cleared() -> None:
    text = body()
    assert "8. Restart phase, for a security run, after every tester has finished" in text
    for needle in (
        "If `[security] restart` names a command",
        "--probe-email <the address the abuse tester exhausted>",
        "stops only the backend",
        "same port, with the same key and data folders",
        "`kept` (`spend_ledger`, `saved_runs`, and `shares`",
        "`cleared` (`magic_link_limit` with `limited_before` and `mailed_after`",
        "Exit 0 means the backend came back; exit 1 means it didn't",
        "A `same` of `false` is a finding",
        "`limited_before: false` means the address wasn't over its limit",
        "`mailed_after: false` is a finding",
        "Testers never run it.",
    ):
        assert needle in text, needle
    assert text.index("8. Restart phase") > text.index("7. When a batch finishes")
    assert text.index("8. Restart phase") < text.index("9. Copy each passing scenario draft")


def test_the_report_has_the_held_and_restart_sections_and_names_skipped_checks() -> None:
    text = body()
    for needle in (
        '"Attacks that held" section',
        "every line of each security tester's `held.md`",
        '"Restart phase" section',
        "when `instance.json` says `caddy` is false",
        "the `loadgen.py` summary if it ran",
    ):
        assert needle in text, needle


def test_the_security_charter_carries_the_tls_files_the_backend_port_and_the_budget() -> None:
    text = body()
    for needle in (
        "`ca_file` and `browser_config` when `caddy` is true",
        '"no Caddy: skip headers and CSP" when it\'s false',
        "the direct backend port (`http://127.0.0.1:<backend_port>`",
        "its magic-link budget, its spare identity",
        "the path of the repository whose source it may read",
    ):
        assert needle in text, needle


def test_the_spare_identity_is_given_to_no_one_else() -> None:
    text = body()
    assert "Write a state file for the spare identity" in text
    assert "give it to no one else" in text


def test_the_raw_backend_url_is_the_one_exception_to_the_allowlist() -> None:
    text = body()
    assert (
        "The one exception is the raw backend URL, which only a `security-tester` may use" in text
    )
    assert '"direct backend"' in text


def test_the_cost_cap_probe_is_the_one_sanctioned_photo_read_and_runs_alone() -> None:
    text = body()
    for needle in (
        "The one sanctioned photo read is a `security-tester`'s cost-cap probe",
        "`STAFF2SOLFA_FORBID_BILLED=1` and holds no provider key",
        "leaves a `vision_forbidden` line in the backend log for each read",
        "only in a batch where that tester runs alone",
        "it changes the seeded workspace's spend cap",
        "whether it may run the cost-cap probe (only when it runs alone)",
        "only in a batch where it runs alone, and say so in the charter",
    ):
        assert needle in text, needle


def test_vision_forbidden_lines_from_the_probe_are_reported_as_expected() -> None:
    text = body()
    assert "the `vision_forbidden` lines that a `security-tester`'s cost-cap probe caused" in text
    assert "are expected: report them as its probe's, with their count" in text
    assert "not as a route that tried a billed call" in text


def test_loadgen_refuses_a_state_file_with_no_cookie_and_stops_on_gateway_errors() -> None:
    text = body()
    for needle in (
        "a `--state` file with no cookie for the site",
        "five failures in a row: connection failures, or `502`, `503`, or `504` from the proxy",
        "`stop_reason` names them",
        "`error_types` counts each connection failure by its type",
    ):
        assert needle in text, needle


def test_start_lists_the_outbox_among_its_fields() -> None:
    text = body()
    assert "`start` prints one JSON object with, among others, `run_dir`" in text
    assert "`outbox` (the folder the run's mail lands in)" in text


def test_the_security_charter_names_the_outbox_and_the_identities_file() -> None:
    text = body()
    for needle in (
        "the run's outbox (`outbox` in `start`'s output)",
        "the path of `<run folder>/env/identities.json`",
    ):
        assert needle in text, needle


def test_the_restart_phase_reads_exit_2_null_checks_and_the_previous_outcome() -> None:
    text = body()
    for needle in (
        "exit 2: not a started run folder or a usage error; report stderr (it also prints a JSON"
        " line with `restarted: false`)",
        "`same: null` or `mailed_after: null` with an `error` means that check couldn't run;"
        " report it as not checked, not as a finding",
        "`previous` is how the old backend ended: `stopped`, or `gone`",
        "`gone` means the backend had died before the restart, which is a finding",
        "`limited_before: true` after a heavy batch can come from the per-client limit",
    ):
        assert needle in text, needle


def test_the_skill_checks_a_second_cost_cap_job_in_the_backend_log() -> None:
    text = SKILL.read_text(encoding="utf-8")
    assert "needs orchestrator check" in text
    assert "`spend_reserved`" in text and "`spend_settled`" in text
