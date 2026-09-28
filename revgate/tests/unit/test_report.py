"""The implementer report model and its path resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from revgate.model import Response
from revgate.report import (
    AmbiguousReport,
    Claim,
    load_report,
    parse_report,
    resolve_report_path,
)

FENCE = "```"

REPORT = f"""\
# Task 3 report

## Summary

Wired `render_page` into the sheet. `ignored_here` isn't disclosed.
234/234 passed, and ruff is clean.

## Deviations

- Kept `move_trailing_items` in src/x.py instead of moving it.
- Called `pkg.rows.build_rows()` directly; see `web/src/grid.ts:12`.

### Detail under the deviation

Also touched `helper_fn`.

## Concerns

None beyond {FENCE}inline{FENCE}.

{FENCE}revgate-responses
3f9a01c2: intentional — the brief was wrong
0123abcd: fixed
deadbeef: dispute -- the rule misread the call
not an id: fixed
{FENCE}

## Commands run

- `uv run pytest` (`not_disclosed_fn`)
"""


def test_disclosures_come_from_deviation_and_concern_sections() -> None:
    model = parse_report(REPORT, "r.md")
    assert "move_trailing_items" in model.disclosed
    assert "src/x.py" in model.disclosed
    assert "pkg.rows.build_rows" in model.disclosed
    assert "build_rows" in model.disclosed
    assert "web/src/grid.ts" in model.disclosed
    # A deeper heading stays inside the deviation section.
    assert "helper_fn" in model.disclosed
    assert "ignored_here" not in model.disclosed
    assert "not_disclosed_fn" not in model.disclosed
    assert model.path == "r.md" and model.text == REPORT


def test_responses_block() -> None:
    model = parse_report(REPORT, "r.md")
    assert model.responses["3f9a01c2"] == Response("3f9a01c2", "intentional", "the brief was wrong")
    assert model.responses["0123abcd"] == Response("0123abcd", "fixed", "")
    assert model.responses["deadbeef"] == Response(
        "deadbeef", "dispute", "the rule misread the call"
    )
    assert len(model.responses) == 3


def test_claims() -> None:
    model = parse_report(REPORT, "r.md")
    assert Claim("tests_passed", "234/234 passed, and ruff is clean.", "234/234") in model.claims
    assert any(c.kind == "gate_clean" and c.value == "ruff" for c in model.claims)


def test_heading_names_changed_counts_as_disclosure() -> None:
    model = parse_report("## What changed\n\nRenamed `old_name`.\n## Next\n`other`\n", "r.md")
    assert model.disclosed == frozenset({"old_name"})


def test_changes_made_heading_counts_as_disclosure() -> None:
    model = parse_report("## Changes made\n\nMoved `mover`.\n## Exchange rates\n`rate`\n", "r.md")
    assert model.disclosed == frozenset({"mover"})


PATTERN = ".superpowers/sdd/{plan}/task-{id}-report.md"


def _write(root: Path, rel: str, text: str = "## Concerns\n`x_fn`\n") -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)


def test_resolve_report_path(tmp_path: Path) -> None:
    assert resolve_report_path(tmp_path, PATTERN, "plan-a", "3") is None
    _write(tmp_path, ".superpowers/sdd/plan-a/task-3-report.md")
    _write(tmp_path, ".superpowers/sdd/plan-b/task-3-report.md")
    found = resolve_report_path(tmp_path, PATTERN, "plan-a", "3")
    assert found == tmp_path / ".superpowers/sdd/plan-a/task-3-report.md"


def test_resolve_report_path_ambiguous(tmp_path: Path) -> None:
    _write(tmp_path, "reports/plan-a/one/task-3-report.md")
    _write(tmp_path, "reports/plan-a/two/task-3-report.md")
    with pytest.raises(AmbiguousReport):
        resolve_report_path(tmp_path, "reports/{plan}/*/task-{id}-report.md", "plan-a", "3")


def test_resolve_report_path_requires_plan_placeholder(tmp_path: Path) -> None:
    _write(tmp_path, ".superpowers/sdd/plan-a/task-3-report.md")
    with pytest.raises(AmbiguousReport):
        resolve_report_path(tmp_path, ".superpowers/sdd/*/task-{id}-report.md", "plan-a", "3")


def test_resolve_report_path_refuses_escaping_patterns(tmp_path: Path) -> None:
    with pytest.raises(AmbiguousReport):
        resolve_report_path(tmp_path, "../{plan}/task-{id}.md", "plan-a", "3")
    with pytest.raises(AmbiguousReport):
        resolve_report_path(tmp_path, "/abs/{plan}/task-{id}.md", "plan-a", "3")
    with pytest.raises(AmbiguousReport):
        resolve_report_path(tmp_path, "r/{plan}/task-{id}.md", "plan-a", "../../x")


def test_load_report(tmp_path: Path) -> None:
    assert load_report(tmp_path, PATTERN, "plan-a", "3") is None
    _write(tmp_path, ".superpowers/sdd/plan-a/task-3-report.md")
    model = load_report(tmp_path, PATTERN, "plan-a", "3")
    assert model is not None
    assert model.path == ".superpowers/sdd/plan-a/task-3-report.md"
    assert model.disclosed == frozenset({"x_fn"})
