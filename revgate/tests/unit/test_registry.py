"""The shipped tier table against the algorithm spec's Appendix A.

The spec lives beside the plan that ships `revgate`, which isn't always in this repository.
The test reads it from `docs/superpowers/specs/` at the repository root, or from the path in
`REVGATE_ALGORITHM_SPEC`, and skips when neither exists.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import pytest

from revgate.model import Finding, Grade, Source, Tier
from revgate.rules.registry import RuleSpec, StaticRule, load_tier_table, spec_for

SPEC_NAME = "2026-09-27-deterministic-review-algorithm-design.md"
TEMPLATED = ("lesson.<id>", "distilled.<id>", "family.<id>")


def spec_path() -> Path | None:
    candidates = [Path(__file__).resolve().parents[3] / "docs/superpowers/specs" / SPEC_NAME]
    env = os.environ.get("REVGATE_ALGORITHM_SPEC")
    if env:
        candidates.insert(0, Path(env).expanduser())
    return next((p for p in candidates if p.is_file()), None)


@dataclass(frozen=True)
class Row:
    tier: str
    grade: int
    review: bool
    context: bool


def parse_tier(cell: str) -> tuple[str, bool]:
    text = cell.strip()
    if text.startswith("task: deferred"):
        return "advisory", False
    if text.startswith("context") or text.startswith("branch map only"):
        return "shadow", True
    for tier in ("blocking", "advisory", "shadow"):
        if text.startswith(tier):
            return tier, False
    raise AssertionError(f"no tier in {cell!r}")


def appendix_a_rows(text: str) -> dict[str, Row]:
    start = text.index("## Appendix A: Rule catalog")
    end = text.index("## Appendix B", start)
    rows: dict[str, Row] = {}
    for line in text[start:end].splitlines():
        if not line.startswith("| `"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        ids = re.findall(r"`([^`]+)`", cells[0])
        grades = [int(g) for g in re.findall(r"E(\d)", cells[3])]
        tier, context = parse_tier(cells[5])
        review = cells[6].startswith("yes")
        for i, rule_id in enumerate(ids):
            grade = grades[i] if len(grades) == len(ids) else grades[0]
            row = Row(tier, grade, review, context)
            if rule_id in rows:  # a rule listed in two sections must agree with itself
                assert rows[rule_id] == row, rule_id
                continue
            rows[rule_id] = row
    return rows


def is_project_lesson(rule_id: str) -> bool:
    """A concrete lesson is learned per project and lives in that project's configuration."""
    return rule_id.startswith("lesson.") and rule_id != "lesson.<id>"


def test_tier_table_matches_appendix_a() -> None:
    path = spec_path()
    if path is None:
        pytest.skip(f"{SPEC_NAME} isn't in this checkout; set REVGATE_ALGORITHM_SPEC")
    rows = appendix_a_rows(path.read_text(encoding="utf-8"))
    table = load_tier_table()
    expected = {k: v for k, v in rows.items() if not is_project_lesson(k)}
    assert len(expected) > 100
    assert set(table) == set(expected)
    for rule_id, row in expected.items():
        spec = table[rule_id]
        got = Row(spec.tier.value, int(spec.grade), spec.review, spec.context)
        assert got == row, rule_id


def test_tier_table_shape() -> None:
    table = load_tier_table()
    assert table["gate.failed"] == RuleSpec(
        id="gate.failed",
        stage="parent",
        langs=("any",),
        grade=Grade.E0_EXECUTED,
        source=Source.POLICY,
        tier=Tier.BLOCKING,
        review=True,
        proven="exact",
    )
    assert table["wave.integrity"].stage == "parent"
    assert table["policy.expected_value_chased"].proven is None
    assert table["errors.logged_not_recorded"].source is Source.DECLARED
    assert table["contract.default_changed"].context is True
    assert table["obligation.docs_branch"].context is True
    assert table["wiring.unwired_new_symbol"].tier is Tier.ADVISORY
    assert table["wave.stale_callee"].grade is Grade.E3_HEURISTIC
    assert table["wave.stale_callee_typed"].grade is Grade.E2_STRUCTURAL
    assert table["mirror.type_drift"].langs == ("py", "ts")
    for templated in TEMPLATED:
        assert templated in table
    assert list(table)[0] == "gate.failed"


def test_spec_for_falls_back_to_the_templated_id() -> None:
    table = load_tier_table()
    assert spec_for("lesson.anything", table) is table["lesson.<id>"]
    assert spec_for("distilled.fullmatch", table) is table["distilled.<id>"]
    assert spec_for("family.x.y", table) is table["family.<id>"]
    assert spec_for("gate.failed", table) is table["gate.failed"]
    with pytest.raises(KeyError):
        spec_for("nosuch.rule", table)
    with pytest.raises(KeyError):
        spec_for("plain", table)


class _Rule:
    ids = frozenset({"a.b"})
    languages = frozenset({"py"})

    def check(self, ctx: object) -> Iterable[Finding]:
        return ()


def test_static_rule_protocol_is_structural() -> None:
    rule: StaticRule = _Rule()
    assert list(rule.check(None)) == []
    assert isinstance(_Rule(), StaticRule)
