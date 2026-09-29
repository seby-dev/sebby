"""The qa-swarm guides carry the rules the testers depend on."""

import json
import re
import subprocess
import sys
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
    "name", ["browser.md", "scenarios.md", "report-format.md", "collisions.md", "security.md"]
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
    for needle in ("`0`", "`75`", "`64`", "`1`", "abandoned", "not passed"):
        assert needle in body, needle
    for needle in (
        "Any code other than `0`",
        "`127` from a wrong path",
        "means you didn't act: record the scenario as abandoned",
        "you already ran the command",
    ):
        assert needle in body, needle
    assert "counts once" not in body


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
        'cd <run>/testers/<me> && { cmd1 & p1=$!; cmd2 & p2=$!; wait $p1; echo "a=$?";'
        ' wait $p2; echo "b=$?"; }',
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
        'the runbook\'s "Barrier timing" table',
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
    assert "`adv-1-02`" in body and "`qa-1-03`" in body
    assert "`adv-02`" not in body and "`qa-03`" not in body


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


def test_collisions_guide_describes_both_mismatch_directions() -> None:
    body = text("collisions.md")
    for needle in (
        "`released_count` greater than the number of `acted` entries means a released"
        " participant never recorded acting",
        "a failed write",
        "Fewer means a participant arrived after the release",
        "isn't a real collision for that participant",
        'Either way, the orchestrator reports the scenario as "not a real collision"',
    ):
        assert needle in body, needle


def test_collisions_guide_runs_the_charters_command_and_starts_spread_in_the_testers_folder() -> (
    None
):
    body = text("collisions.md")
    assert "Run the barrier command exactly as your charter writes it" in body
    assert "only the fallback when a charter gives none" in body
    assert 'cd <run>/testers/<me> && bash "${SEBBY_ROOT:-$HOME/Developer/sebby}' in body
    assert 'barrier.sh" spread <barrier.dir>' in body.split("## Measuring the spread", 1)[1]
    spread_block = body.split("## Measuring the spread", 1)[1].split("```bash", 1)[1]
    assert spread_block.lstrip().startswith("cd <run>/testers/<me> && bash")


def test_collisions_guide_copies_a_parallel_fetch_from_a_request_the_page_makes() -> None:
    body = text("collisions.md")
    for needle in (
        "a request the page itself makes",
        "`playwright-cli requests`",
        "`request <index>`",
        "method, URL, headers, and JSON body",
        '"$(cat body.json)"',
        "'Content-Type': 'application/json'",
    ):
        assert needle in body, needle
    assert "read it in the `eval`" not in body


def test_collisions_guide_says_spread_is_the_release_spread_and_names_clock_and_skipped() -> None:
    body = text("collisions.md")
    for needle in (
        "`spread_ms` is the release spread",
        "The action lands later",
        "action spread",
        "`clock`",
        "`skipped`",
    ):
        assert needle in body, needle


def test_collisions_guide_uses_active_voice_and_drops_the_old_title_rule() -> None:
    body = text("collisions.md")
    for phrase in ("was abandoned", "Nothing was created", "run_title` is `null`"):
        assert phrase not in body, phrase


def test_browser_guide_forbids_the_apps_sign_out() -> None:
    body = text("browser.md")
    for needle in (
        "Never use the app's Sign out",
        "Testers share one server session per identity",
        "ends every tester's session on that identity",
        "only with `cookie-delete s2s_session`, in your own session",
    ):
        assert needle in body, needle


def test_security_guide_covers_the_checklist_with_commands() -> None:
    body = text("security.md")
    for needle in (
        "## Access control between workspaces",
        "## Session handling",
        "## Malicious uploads",
        "## Injection into rendered fields",
        "## Cost-cap races",
        "## Rate-limit abuse",
        "## Headers and CSP",
        "`ROLE_RULES`",
        "--cacert <ca_file>",
        "--data @cap.json",
        "-F file=@xxe.musicxml",
        "-H 'Origin: https://evil.example'",
        "Sec-Fetch-Site: cross-site",
        "--path-as-is",
        "`held.md`",
        "wait",
    ):
        assert needle in body, needle


def test_security_guide_builds_a_cookie_jar_without_printing_the_cookie() -> None:
    body = text("security.md")
    assert "prints nothing" in body and "chmod 600 singer.jar" in body
    assert "`Secure` only for an `https://` origin" in body
    script = re.search(r"```python\n(.*?)```", body, re.DOTALL)
    assert script and "print(" not in script.group(1)
    assert 'json.load(handle)["cookies"]' in script.group(1)


def test_security_guide_runs_the_script_it_ships(tmp_path: Path) -> None:
    body = text("security.md")
    script = re.search(r"```python\n(.*?)```", body, re.DOTALL)
    assert script
    path = tmp_path / "jar.py"
    path.write_text(script.group(1), encoding="utf-8")
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps(
            {
                "cookies": [
                    {
                        "name": "s2s_session",
                        "value": "COOKIEVALUE",
                        "domain": "127.0.0.1",
                        "path": "/",
                        "httpOnly": True,
                    }
                ]
            }
        )
    )
    for site, flag in (("https://127.0.0.1:1", "TRUE"), ("http://127.0.0.1:1", "FALSE")):
        jar = tmp_path / f"{flag}.jar"
        done = subprocess.run(
            [sys.executable, str(path), str(state), str(jar), site],
            capture_output=True,
            text=True,
            check=True,
        )
        assert done.stdout == "" and done.stderr == ""
        rows = jar.read_text().splitlines()
        assert rows[0] == "# Netscape HTTP Cookie File"
        assert rows[1] == f"#HttpOnly_127.0.0.1\tFALSE\t/\t{flag}\t0\ts2s_session\tCOOKIEVALUE"


def test_security_guide_says_what_holds_and_what_to_write() -> None:
    body = text("security.md")
    for needle in (
        "expect a refusal or a 404, never data",
        "A write that matches no rule is refused too.",
        "undefined entity",
        "A missing header the Caddyfile promises is a finding.",
        "skipped: no Caddy",
        "an empty report proves nothing about it",
        "Only with your spare identity",
        "Only when your charter says you're the final batch and alone.",
    ):
        assert needle in body, needle


def test_browser_guide_has_the_https_section_with_the_certificate_option() -> None:
    body = text("browser.md")
    for needle in (
        "## HTTPS origins",
        "--config=<browser config>",
        "--ignore-certificate-errors-spki-list",
        "blocks service workers",
        "net::ERR_CERT_AUTHORITY_INVALID",
        "Never turn on a blanket switch such as `--ignore-certificate-errors`",
        "A run without Caddy has an `http://` origin and needs no config.",
    ):
        assert needle in body, needle
    assert body.index("## HTTPS origins") < body.index("## Commands you'll use")


def test_report_format_has_the_security_detail_row_the_sec_prefix_and_the_held_section() -> None:
    body = text("report-format.md")
    assert "| Security detail |" in body and '"direct backend"' in body
    assert "Never a cookie, a token, or a key." in body
    assert "`sec-1-02`" in body
    assert "## Attacks that held" in body
    assert "`testers/<tester>/held.md`" in body
    assert "`<area>: <attack> -> <what the app did>`" in body
    assert "a control with no line wasn't tested" in body


def test_security_guide_names_the_photo_read_and_leaves_loadgen_to_the_orchestrator() -> None:
    body = text("security.md")
    for needle in (
        "the one sanctioned photo read",
        "`STAFF2SOLFA_FORBID_BILLED=1` and holds no provider key",
        "`vision_forbidden` line in the backend log that the orchestrator expects",
        "Only when your charter says your batch runs alone",
        '{"cap_usd": null}',
        "Only the orchestrator runs `loadgen.py`",
    ):
        assert needle in body, needle
    assert "`loadgen.py` can send the burst" not in body
