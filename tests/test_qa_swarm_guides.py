"""The qa-swarm guides carry the rules the testers depend on."""

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


@pytest.mark.parametrize("name", ["browser.md", "scenarios.md", "report-format.md"])
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
