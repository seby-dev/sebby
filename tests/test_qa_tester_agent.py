"""The qa-tester agent pins its model, limits its tools, and runs the guard fail-open."""

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENT = ROOT / "claude" / "agents" / "qa-tester.md"


def head() -> str:
    return AGENT.read_text(encoding="utf-8").split("\n---", 1)[0]


def test_it_pins_a_model_and_high_effort_and_no_edit_tool() -> None:
    text = head()
    assert re.search(r"(?m)^model: sonnet$", text)
    assert re.search(r"(?m)^effort: high$", text)
    tools = re.search(r"(?m)^tools: (.+)$", text)
    assert tools and {t.strip() for t in tools.group(1).split(",")} == {"Bash", "Read", "Write"}


def test_it_registers_the_guard_on_bash_read_and_write_in_the_fail_open_form() -> None:
    text = head()
    assert 'matcher: "Bash|Read|Write"' in text
    command = re.search(r"(?m)^\s+command: \"(.+)\"$", text)
    assert command
    shell = command.group(1).replace('\\"', '"')
    assert "${SEBBY_ROOT:-$HOME/Developer/sebby}/claude/hooks/qa_tester_guard.py" in shell
    assert '[ -f "$f" ] || exit 0' in shell and "|| exit 0" in shell.split(";")[-1]


def test_the_frontmatter_command_exits_zero_when_the_hook_file_is_missing(tmp_path: Path) -> None:
    command = re.search(r"(?m)^\s+command: \"(.+)\"$", head()).group(1).replace('\\"', '"')  # type: ignore[union-attr]
    proc = subprocess.run(
        ["sh", "-c", command],
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
    command = re.search(r"(?m)^\s+command: \"(.+)\"$", head()).group(1).replace('\\"', '"')  # type: ignore[union-attr]
    proc = subprocess.run(
        ["sh", "-c", command],
        input="{}",
        capture_output=True,
        text=True,
        env={"HOME": "/nonexistent", "PATH": "/usr/bin:/bin", "SEBBY_ROOT": str(tmp_path)},
    )
    assert proc.returncode == 0 and "stub hook ran" in proc.stdout


def test_the_body_resolves_the_skill_path_through_sebby_root() -> None:
    body = AGENT.read_text(encoding="utf-8")
    assert "$SEBBY_ROOT/claude/skills/qa-swarm/" in body and "~/.claude/skills" not in body


def test_the_hook_file_the_agent_names_exists_in_this_checkout() -> None:
    assert (ROOT / "claude" / "hooks" / "qa_tester_guard.py").is_file()


def test_the_body_states_the_safety_rules_and_the_report() -> None:
    body = AGENT.read_text(encoding="utf-8")
    for needle in (
        "charter",
        "scenario-run",
        "never trusts",
        "allowlist",
        "Never edit",
        "git push",
        "dev-key",
        "known-artifacts",
        "Reproduce",
        "findings",
    ):
        assert needle.lower() in body.lower(), needle


def test_the_model_guard_sees_it_as_a_pinned_agent(tmp_path: Path) -> None:
    text = AGENT.read_text(encoding="utf-8")
    assert re.search(r"(?m)^model:\s*\S", text.split("\n---", 1)[0])
