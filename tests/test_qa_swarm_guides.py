"""The qa-swarm guides carry the rules the testers depend on."""

import re
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parent.parent / "claude" / "skills" / "qa-swarm"


def text(name: str) -> str:
    return (SKILL / name).read_text(encoding="utf-8")


def test_browser_guide_pins_the_cli_and_names_its_sessions_and_output() -> None:
    body = text("browser.md")
    needles = (
        "@playwright/cli",
        "0.1.22",
        "-s=<tester>-<n>",
        "state-load",
        "goto",
        "close every session",
        "sessions.json",
        "idle",
        "absolute path",
        "<cli folder>",
        "install-browser",
        ".playwright-cli",
        "cd <run folder>/testers/<tester> && npm --prefix <cli folder> exec --",
        "Never run `close-all` or `kill-all`",
        "an hour idle",
        "`groups`, `daemons`, and `bh_dirs`",
        "sibling tester's folder",
    )
    for needle in needles:
        assert needle in body, needle


def test_browser_guide_says_a_second_open_restarts_the_session() -> None:
    body = text("browser.md")
    assert "second `open` restarts" in body
    assert "state-load` needs an open session" in body


def test_scenarios_guide_defines_the_scenario_fields_and_names_no_executor() -> None:
    body = text("scenarios.md")
    needles = (
        "Scenario(",
        "goal",
        "verify",
        "PageState",
        "actors",
        "Budget",
        "run_scenario",
        "assert_passed",
        "start_url",
    )
    for needle in needles:
        assert needle in body, needle
    assert "jev" not in body.lower().replace("no executor", "")
    assert "field_text" not in body


def test_scenarios_guide_says_verify_never_trusts_the_agent_and_upload_needs_setup() -> None:
    body = text("scenarios.md")
    assert "never trusts" in body.lower() and "upload" in body.lower()


def test_scenarios_guide_reads_the_site_and_state_from_the_environment() -> None:
    body = text("scenarios.md")
    assert 'SITE_URL = os.environ["QA_SITE_URL"]' in body
    assert 'Path(os.environ["QA_RUN_DIR"]) / "state"' in body
    assert "/results" not in body and "illustrative" in body


def test_report_format_has_every_finding_field_and_the_severity_scale() -> None:
    body = text("report-format.md")
    fields = (
        "ID",
        "Tester and role",
        "Severity",
        "Title",
        "Steps to reproduce",
        "Expected and actual",
        "Evidence",
        "Reproduced",
    )
    for field in fields:
        assert field in body, field
    for level in ("blocker", "high", "medium", "low"):
        assert level in body
    assert "reachability" in body


@pytest.mark.parametrize(
    "name", ["browser.md", "scenarios.md", "report-format.md", "collisions.md"]
)
def test_guides_use_sentence_case_headings_and_no_directional_words(name: str) -> None:
    allowed = {"Playwright", "CLI"}
    for line in text(name).splitlines():
        if line.startswith("#"):
            words = line.lstrip("# ").split()
            for i, w in enumerate(words):
                if w[0].isalpha() and i > 0:
                    assert not w[0].isupper() or w.isupper() or w in allowed, line
    lowered = text(name).lower()
    for word in (" above ", " below "):
        assert word not in lowered, word


def test_collisions_guide_defines_the_target_file_and_the_barrier_command() -> None:
    body = text("collisions.md")
    needles = (
        "target.json",
        '"version": 1',
        '"barrier"',
        '"participants"',
        "`null` unless `method` is `barrier-click`",
        'bash "${SEBBY_ROOT:-$HOME/Developer/sebby}/claude/skills/qa-swarm/barrier.sh"'
        " wait <barrier.dir>",
        "`timeout` set to 600000",
        "never re-run the barrier command",
        "<barrier.count> <barrier.timeout_seconds>",
        "cd <run>/testers/<me> && bash",
        "-s=<session> click <ref>",
        "Snapshot first, then one command",
    )
    for needle in needles:
        assert needle in body, needle


def test_collisions_guide_says_what_each_exit_code_means() -> None:
    body = text("collisions.md")
    for needle in ("`0`", "`75`", "`64`", "abandoned", "not passed"):
        assert needle in body, needle


def test_collisions_guide_names_the_four_methods() -> None:
    body = text("collisions.md")
    for needle in (
        "**Barrier click.**",
        "**Repeat click.**",
        "`dblclick <ref>`",
        "`click <ref> && click <ref>`",
        "**Parallel fetch.**",
        "Promise.all([fetch(",
        "**Parallel sessions.**",
        "cd <run>/testers/<me> && { cmd1 & cmd2 & wait; }",
        "cd <run>/testers/<me> && npm --prefix <cli folder> exec --"
        " playwright-cli -s=<session> eval",
    ):
        assert needle in body, needle


def test_collisions_guide_keeps_testers_out_of_shared_and_names_the_spread_command() -> None:
    body = text("collisions.md")
    for needle in (
        "You never write in the run's `shared/` folder",
        'barrier.sh" spread <barrier.dir>',
        "`spread_ms`",
        "see the project's measurement",
        "lets the barrier abandon",
    ):
        assert needle in body, needle
    assert not re.search(r"\d+\s*ms\b", body), "the guide must not promise a timing number"


def test_browser_guide_lists_the_adversarial_testers_commands() -> None:
    body = text("browser.md")
    for command in (
        "dblclick",
        "go-back",
        "go-forward",
        "reload",
        "tab-new",
        "tab-select",
        "cookie-delete",
        "requests",
    ):
        assert f"`{command}" in body, command
    assert "The adversarial tester uses" in body
    assert "cookie-delete s2s_session" in body


def test_report_format_has_the_collision_row_and_the_adv_prefix() -> None:
    body = text("report-format.md")
    assert "| Collision |" in body and "`barrier.sh spread` output, or `abandoned`" in body
    assert "`adv-02`" in body


def test_every_barrier_path_in_the_guides_is_run_with_bash() -> None:
    for name in ("browser.md", "collisions.md", "report-format.md", "scenarios.md"):
        body = text(name)
        for match in re.finditer(r'[^\s"`]*/barrier\.sh', body):
            assert re.search(r'bash "?$', body[: match.start()]), (name, match.group(0))


def test_collisions_guide_says_a_signal_abandons_and_the_outcome_file_is_authoritative() -> None:
    body = text("collisions.md")
    for needle in (
        "A stopped command abandons the barrier for everyone",
        "never writes its `acted` entry",
        "The outcome file and `spread` are authoritative, not one participant's exit code",
        "the late participant has returned `75` while the others returned `0`",
    ):
        assert needle in body, needle
    assert "withdraws its own arrival" not in body


def test_collisions_guide_checks_released_count_against_the_acted_entries() -> None:
    body = text("collisions.md")
    for needle in (
        "`released_count` (how many had arrived when the barrier released, or `null`)",
        "`released_count` must equal the number of `acted` entries",
        "a SIGKILL can't be trapped",
        '"not a real collision", not as passed',
    ):
        assert needle in body, needle


def test_collisions_guide_names_the_unshared_run_keys() -> None:
    body = text("collisions.md")
    assert '"run_unshared_id": "...",' in body and '"run_unshared_title":' in body
    assert (
        "`run_id`, `run_title`, `run_unshared_id`, `run_unshared_title`, `share_id`, and"
        " `share_url` appear only when the scenario uses them"
    ) in body
    assert "a scenario that needs `run_unshared`, such as `double-publish`" in body
