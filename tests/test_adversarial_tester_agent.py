"""The adversarial-tester agent pins its model, limits its tools, and runs the guard fail-open."""

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENT = ROOT / "claude" / "agents" / "adversarial-tester.md"
QA_TESTER = ROOT / "claude" / "agents" / "qa-tester.md"


def head(path: Path = AGENT) -> str:
    return path.read_text(encoding="utf-8").split("\n---", 1)[0]


def hook_command(path: Path = AGENT) -> str:
    match = re.search(r"(?m)^\s+command: \"(.+)\"$", head(path))
    assert match
    return match.group(1).replace('\\"', '"')


def test_it_is_named_and_described_for_the_swarm() -> None:
    text = head()
    assert re.search(r"(?m)^name: adversarial-tester$", text)
    description = re.search(r"(?m)^description: (.+)$", text)
    assert description
    for needle in ("double submits", "races between tabs and users", "playwright-cli"):
        assert needle in description.group(1), needle
    assert description.group(1).endswith("dispatch only from the qa-swarm skill.")


def test_it_pins_a_model_and_high_effort_and_no_edit_tool() -> None:
    text = head()
    assert re.search(r"(?m)^model: sonnet$", text)
    assert re.search(r"(?m)^effort: high$", text)
    tools = re.search(r"(?m)^tools: (.+)$", text)
    assert tools and {t.strip() for t in tools.group(1).split(",")} == {"Bash", "Read", "Write"}


def test_its_hook_block_matches_the_qa_testers_exactly() -> None:
    assert 'matcher: "Bash|Read|Write"' in head()
    assert hook_command() == hook_command(QA_TESTER)
    assert "${SEBBY_ROOT:-$HOME/Developer/sebby}/claude/hooks/qa_tester_guard.py" in hook_command()
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


def test_the_command_runs_the_hook_from_sebby_root_when_it_is_set(tmp_path: Path) -> None:
    hooks = tmp_path / "claude" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "qa_tester_guard.py").write_text("print('stub hook ran')\n")
    proc = subprocess.run(
        ["sh", "-c", hook_command()],
        input="{}",
        capture_output=True,
        text=True,
        env={"HOME": "/nonexistent", "PATH": "/usr/bin:/bin", "SEBBY_ROOT": str(tmp_path)},
    )
    assert proc.returncode == 0 and "stub hook ran" in proc.stdout


def test_the_body_resolves_its_paths_through_sebby_root() -> None:
    body = AGENT.read_text(encoding="utf-8")
    assert "$SEBBY_ROOT/claude/skills/qa-swarm/" in body and "~/.claude/skills" not in body
    for guide in ("browser.md", "collisions.md", "report-format.md"):
        assert guide in body, guide
        assert (
            ROOT / "claude" / "skills" / "qa-swarm" / guide
        ).is_file() or guide == "collisions.md"


def test_the_body_covers_the_five_checks_and_the_collision_mechanics() -> None:
    body = AGENT.read_text(encoding="utf-8")
    needles = (
        "charter",
        "barrier.sh wait",
        "75",
        "abandoned",
        "dblclick",
        "cookie-delete",
        "go-back",
        "reload",
        "extreme lengths",
        "spread",
        "adv-<n>",
        "target.json",
        "parallel fetch",
        "`timeout` set to 600000",
        "never re-run the barrier command",
        "Get into position first",
        "{ cmd1 & cmd2 & wait; }",
        '"${SEBBY_ROOT:-$HOME/Developer/sebby}/claude/skills/qa-swarm/barrier.sh"',
    )
    for needle in needles:
        assert needle in body, needle


def test_the_body_states_the_safety_rules() -> None:
    body = AGENT.read_text(encoding="utf-8")
    for needle in (
        "allowlist",
        "Never edit",
        "git push",
        "dev-key",
        "known-artifacts",
        "sessions.json",
        "rm -r",
        "DROP",
    ):
        assert needle in body, needle
    assert body.count("rm -rf") == 1  # only in the rule that forbids it


def test_it_never_names_a_qa_env_command_and_no_bypass_hunting() -> None:
    body = AGENT.read_text(encoding="utf-8")
    assert "qa_env.sh scenario-run" not in body and "scenario-run" not in body
    assert "Never run a `qa_env.sh` command" in body
    assert "You aren't after a security bypass" in body
