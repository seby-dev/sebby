"""The security-tester agent pins Opus, limits its tools, runs the guard fail-open, and states
its checklist and its rules."""

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENT = ROOT / "claude" / "agents" / "security-tester.md"
ADVERSARIAL = ROOT / "claude" / "agents" / "adversarial-tester.md"
QA_TESTER = ROOT / "claude" / "agents" / "qa-tester.md"


def head(path: Path = AGENT) -> str:
    return path.read_text(encoding="utf-8").split("\n---", 1)[0]


def hook_command(path: Path = AGENT) -> str:
    match = re.search(r"(?m)^\s+command: \"(.+)\"$", head(path))
    assert match
    return match.group(1).replace('\\"', '"')


def body() -> str:
    return AGENT.read_text(encoding="utf-8")


def test_it_is_named_and_described_for_the_swarm() -> None:
    text = head()
    assert re.search(r"(?m)^name: security-tester$", text)
    description = re.search(r"(?m)^description: (.+)$", text)
    assert description
    for needle in (
        "locally started instance only",
        "access control between workspaces",
        "cost-cap races",
        "playwright-cli",
    ):
        assert needle in description.group(1), needle
    assert description.group(1).endswith("dispatch only from the qa-swarm skill.")


def test_it_pins_opus_at_high_effort_and_has_no_edit_tool() -> None:
    text = head()
    assert re.search(r"(?m)^model: opus$", text)
    assert re.search(r"(?m)^effort: high$", text)
    tools = re.search(r"(?m)^tools: (.+)$", text)
    assert tools and {t.strip() for t in tools.group(1).split(",")} == {"Bash", "Read", "Write"}


def test_its_hook_block_is_byte_identical_to_the_other_testers() -> None:
    def hooks_block(path: Path) -> str:
        return head(path).split("hooks:", 1)[1]

    assert hooks_block(AGENT) == hooks_block(ADVERSARIAL) == hooks_block(QA_TESTER)
    assert hook_command() == hook_command(ADVERSARIAL)
    assert 'matcher: "Bash|Read|Write"' in head()
    shell = hook_command()
    assert '[ -f "$f" ] || exit 0' in shell and "|| exit 0" in shell.split(";")[-1]


def test_the_frontmatter_command_exits_zero_when_the_hook_file_is_missing(tmp_path: Path) -> None:
    proc = subprocess.run(
        ["sh", "-c", hook_command()],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}}),
        capture_output=True,
        text=True,
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0


def test_the_body_resolves_its_paths_through_sebby_root_and_names_its_guides() -> None:
    text = body()
    assert "$SEBBY_ROOT/claude/skills/qa-swarm/" in text and "~/.claude/skills" not in text
    for guide in ("security.md", "browser.md", "report-format.md"):
        assert guide in text, guide
        assert (ROOT / "claude" / "skills" / "qa-swarm" / guide).is_file() or guide == "security.md"


def test_the_body_covers_the_five_checklist_areas() -> None:
    text = body()
    for needle in (
        "Access control between workspaces",
        "IDOR",
        "`ROLE_RULES`",
        "Session handling",
        "cross-site request forgery (CSRF)",
        "Malicious uploads",
        "XML external entities",
        "zip bombs",
        "Injection into rendered fields",
        "LilyPond",
        "Cost-cap races",
        "restart phase",
    ):
        assert needle in text, needle


def test_it_labels_direct_backend_probes_and_limits_them_to_the_charters_port() -> None:
    text = body()
    for needle in (
        '"direct backend"',
        'lists a "direct backend port"',
        "The guard denies the port to every other agent",
        "the two sound hosts",
    ):
        assert needle in text, needle


def test_it_checks_headers_only_under_caddy_and_says_skipped_otherwise() -> None:
    text = body()
    assert "When the charter says the run has Caddy" in text
    assert "skipped: no Caddy" in text
    assert "--cacert <ca_file>" in text and "--config=<browser config>" in text


def test_it_limits_magic_links_and_rate_limit_abuse_to_what_the_charter_allows() -> None:
    text = body()
    for needle in (
        "5 per address and 20 per client each hour",
        "a `202` and no email",
        "only as many real link requests as your charter budgets",
        "only when your charter says it's the final batch and you're alone",
    ):
        assert needle in text, needle


def test_it_keeps_payloads_in_files_and_never_names_destructive_sql() -> None:
    text = body()
    assert "`--data @file`" in text and "`-F name=@file`" in text
    assert "`DROP` or `DELETE FROM`" in text
    assert "`rm -r`, never `rm -rf`" in text
    assert text.count("rm -rf") == 1  # only in the rule that forbids it


def test_it_states_the_safety_rules() -> None:
    text = body()
    for needle in (
        "allowlist",
        "Never edit source",
        "git push",
        "dev-key",
        "known-artifacts",
        "sessions.json",
        "You never see the run's API key",
        "Never write a cookie value, a token, or a key",
        "Never run a `qa_env.sh` command",
        "don't start, stop, or restart it",
        "Sign out and revoke only what your charter gives you alone",
        "publish a share of your own",
        "If the instance stops answering, stop",
    ):
        assert needle in text, needle


def test_it_rates_by_reachability_and_reports_what_held() -> None:
    text = body()
    for needle in (
        "by reachability",
        "Security detail",
        "`sec-1-02`",
        "`testers/<your name>/held.md`",
        "`<area>: <attack> -> <what the app did>`",
        "only the attacks you list count as tried",
    ):
        assert needle in text, needle


def test_it_never_names_a_qa_env_command_it_may_run() -> None:
    text = body()
    assert "scenario-run" not in text
    assert "qa_env.sh restart" not in text  # the orchestrator's command, never the tester's


def test_it_is_authorized_testing_of_the_users_own_app_on_the_local_instance() -> None:
    text = body()
    assert "authorized testing of the user's own app" in text
    assert "on the instance the orchestrator started for this run and nowhere else" in text
    assert "don't use an external service or callback" in text


def test_the_cost_cap_probe_is_named_as_the_one_photo_read_and_runs_alone() -> None:
    text = body()
    for needle in (
        "`POST /v1/extract` with an image, the one sanctioned photo read",
        "`STAFF2SOLFA_FORBID_BILLED=1` and holds no provider key",
        "`vision_forbidden` log line",
        "Run it only when your charter says your batch runs alone",
        "put the cap back when you finish",
    ):
        assert needle in text, needle
