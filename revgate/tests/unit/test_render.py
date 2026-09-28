"""The implementer's summary: shape, budget, and exit code."""

from __future__ import annotations

import dataclasses

from revgate.model import Finding, Grade, Source, Tier, Verdict, new_finding
from revgate.verdict.render import exit_code_for, render_summary

IMPL = frozenset({"implementer", "wave", "log"})


def _f(rule: str, n: int, tier: Tier, grade: Grade, audience: frozenset[str]) -> Finding:
    f = new_finding(
        rule,
        file="src/a.py",
        line=n,
        message=f"message {n}",
        evidence=f"evidence {n}",
        grade=grade,
        source=Source.GENERIC,
    )
    return dataclasses.replace(f, tier=tier, audience=audience)


def _verdict(findings: list[Finding], focus: tuple[str, ...] = ()) -> Verdict:
    return Verdict(tuple(findings), bool(focus), focus, (), ())


def _render(v: Verdict, *, provisional: bool = False) -> list[str]:
    text = render_summary(
        v,
        task="T8",
        head="ef45ab6c0ffee",
        round_=1,
        run_file_path="/state/runs/T8/ef45ab6.implementer.json",
        checks=41,
        provisional=provisional,
    )
    return text.splitlines()


def test_summary_shape() -> None:
    findings = (
        [_f("policy.protected_path", i, Tier.BLOCKING, Grade.E1_EXACT, IMPL) for i in range(3)]
        + [_f("plan.symbol_missing", 10 + i, Tier.ADVISORY, Grade.E1_EXACT, IMPL) for i in range(8)]
        + [
            _f(
                "plan.call_edge_missing",
                30 + i,
                Tier.ADVISORY,
                Grade.E2_STRUCTURAL,
                IMPL - {"implementer"},
            )
            for i in range(2)
        ]
        + [
            _f(
                "contract.optional_unguarded",
                50 + i,
                Tier.SHADOW,
                Grade.E2_STRUCTURAL,
                frozenset({"log"}),
            )
            for i in range(5)
        ]
    )
    lines = _render(_verdict(findings))
    assert len(lines) <= 20
    assert lines[0].startswith("revgate: 3 blocking, 5 advisory printed (+5 counted)")
    assert lines[0].endswith("task T8  ef45ab6  round 1")
    assert (
        sum(1 for line in lines if line.startswith("src/a.py:") and "protected_path" in line) == 3
    )
    assert sum(1 for line in lines if line.startswith("advisory ")) == 5
    assert not any("contract.optional_unguarded" in line for line in lines)
    assert any(line.startswith("details: /state/runs/T8/") for line in lines)
    assert lines[-1].startswith("fix blocking findings")
    assert any(
        line.startswith("    evidence: evidence 0   verify: revgate recheck ") for line in lines
    )


def test_summary_overflow_keeps_twenty_lines() -> None:
    findings = [
        _f("policy.protected_path", i, Tier.BLOCKING, Grade.E1_EXACT, IMPL) for i in range(25)
    ]
    lines = _render(_verdict(findings))
    assert len(lines) == 20
    details = next(i for i, line in enumerate(lines) if line.startswith("details: "))
    assert lines[details - 1] == "… and 9 more blocking"
    assert sum(1 for line in lines if line.startswith("src/a.py:")) == 16
    assert lines[0].startswith("revgate: 25 blocking, 0 advisory printed")


def test_clean_summary_is_one_line() -> None:
    assert _render(_verdict([])) == ["revgate: clean  task T8  ef45ab6  (41 checks)"]
    shadow = _f(
        "contract.optional_unguarded", 1, Tier.SHADOW, Grade.E2_STRUCTURAL, frozenset({"log"})
    )
    assert _render(_verdict([shadow])) == ["revgate: clean  task T8  ef45ab6  (41 checks)"]


def test_status_line_marks_focus_and_provisional() -> None:
    adv = _f("plan.symbol_missing", 1, Tier.ADVISORY, Grade.E1_EXACT, IMPL)
    lines = _render(_verdict([adv], focus=("risk: high",)), provisional=True)
    assert lines[0] == (
        "revgate: 0 blocking, 1 advisory printed, focus (risk: high)  task T8  ef45ab6"
        "  round 1  provisional"
    )


def test_exit_code_follows_blocking_findings() -> None:
    blocking = _f("policy.protected_path", 1, Tier.BLOCKING, Grade.E1_EXACT, IMPL)
    adv = _f("plan.symbol_missing", 2, Tier.ADVISORY, Grade.E1_EXACT, IMPL)
    assert exit_code_for(_verdict([blocking, adv])) == 1
    assert exit_code_for(_verdict([adv])) == 0
    explained = dataclasses.replace(blocking, status="explained", audience=frozenset({"log"}))
    assert exit_code_for(_verdict([explained])) == 0
