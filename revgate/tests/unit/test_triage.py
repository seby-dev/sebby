"""Triage: explanation, acknowledgment, baseline differencing, clustering, and `decide`."""

from __future__ import annotations

import dataclasses

from revgate.config import Config, parse_config
from revgate.model import Finding, Grade, Impact, Response, Source, TaskScope, Tier, new_finding
from revgate.rules.registry import load_tier_table
from revgate.verdict.route import FocusInputs
from revgate.verdict.triage import baseline_difference, cluster, decide, explain_or_acknowledge

SCOPE = TaskScope("T1", owns=frozenset({"src/a.py"}), runs=frozenset())
QUIET = FocusInputs(None, ("src/a.py",), 10, False, False)
NO_RULINGS: frozenset[tuple[str, str, str | None]] = frozenset()


def _cfg() -> Config:
    return parse_config(b"[routing]\nmax_lines = 400\n")


def _finding(
    rule: str,
    *,
    symbol: str | None = None,
    line: int = 1,
    grade: Grade = Grade.E1_EXACT,
    impact: Impact = "important",
    evidence: str = "e",
    witness_digest: str | None = None,
) -> Finding:
    return new_finding(
        rule,
        file="src/a.py",
        line=line,
        message="m",
        evidence=evidence,
        grade=grade,
        source=Source.DECLARED,
        impact=impact,
        symbol=symbol,
        witness_digest=witness_digest,
    )


def _decide(
    findings: list[Finding],
    *,
    disclosed: frozenset[str] = frozenset(),
    responses: dict[str, Response] | None = None,
    rulings: frozenset[tuple[str, str, str | None]] = NO_RULINGS,
) -> tuple[Finding, ...]:
    v = decide(
        findings,
        scope=SCOPE,
        tiers=load_tier_table(),
        cfg=_cfg(),
        disclosed=disclosed,
        responses=responses or {},
        rulings=rulings,
        focus_inputs=QUIET,
    )
    return v.findings


def test_disclosure_acknowledges() -> None:
    f = _finding("plan.symbol_missing", symbol="relocate")
    status = explain_or_acknowledge(
        f, disclosed=frozenset({"relocate"}), responses={}, rulings=NO_RULINGS
    )
    assert status == "acknowledged"
    (out,) = _decide([f], disclosed=frozenset({"relocate"}))
    assert out.status == "acknowledged"
    assert out.tier is Tier.ADVISORY
    assert "wave" in out.audience

    ruled = frozenset({(f.rule, f.cluster_key, f.witness_digest)})
    (explained,) = _decide([f], rulings=ruled)
    assert explained.status == "explained"
    assert explained.audience == frozenset({"log"})

    intentional = {f.id: Response(f.id, "intentional", "kept on purpose")}
    (ack,) = _decide([f], responses=intentional)
    assert ack.status == "acknowledged"


def test_a_ruling_needs_the_same_witness() -> None:
    f = _finding("plan.symbol_missing", symbol="relocate", witness_digest="aaa")
    other = frozenset({(f.rule, f.cluster_key, "bbb")})
    assert explain_or_acknowledge(f, disclosed=frozenset(), responses={}, rulings=other) == "open"


def test_disclosure_applies_only_to_plan_rules() -> None:
    f = _finding("docs.dangling_prose_ref", symbol="relocate")
    status = explain_or_acknowledge(
        f, disclosed=frozenset({"relocate"}), responses={}, rulings=NO_RULINGS
    )
    assert status == "open"


def test_a_dispute_reaches_the_controller_and_still_blocks() -> None:
    f = new_finding(
        "policy.protected_path",
        file="src/a.py",
        line=3,
        message="m",
        evidence="e",
        grade=Grade.E1_EXACT,
        source=Source.POLICY,
    )
    (out,) = _decide([f], responses={f.id: Response(f.id, "dispute", "not ours")})
    assert out.tier is Tier.BLOCKING and out.status == "open"
    assert {"implementer", "controller", "wave"} <= out.audience


def test_baseline_difference_drops_findings_present_at_base() -> None:
    head = [
        _finding("plan.symbol_missing", symbol="a"),
        _finding("plan.symbol_missing", symbol="b"),
    ]
    base = [_finding("plan.symbol_missing", symbol="a")]
    assert [f.symbol for f in baseline_difference(head, base)] == ["b"]


def test_cluster_keeps_the_strongest_and_lists_the_rest() -> None:
    weak = _finding("plan.call_edge_missing", symbol="f", grade=Grade.E2_STRUCTURAL)
    strong = _finding("wiring.unwired_planned_here", symbol="f", impact="minor")
    stronger = _finding("plan.symbol_missing", symbol="f", impact="critical")
    elsewhere = _finding("plan.symbol_missing", symbol="g")
    heads, absorbed = cluster([weak, strong, stronger, elsewhere])
    assert len(heads) == 2
    head_f = next(h for h in heads if h.symbol == "f")
    assert head_f.rule == "plan.symbol_missing" and head_f.impact == "critical"
    assert head_f.also == ("plan.call_edge_missing", "wiring.unwired_planned_here")
    assert sorted(f.rule for f in absorbed) == [
        "plan.call_edge_missing",
        "wiring.unwired_planned_here",
    ]
    assert {f.status for f in absorbed} == {"clustered"}
    assert {f.absorbed_by for f in absorbed} == {head_f.id}


def test_cluster_never_merges_two_findings_of_one_rule() -> None:
    first = _finding("wiring.unwired_planned_here", symbol="f", evidence="a")
    second = _finding("wiring.unwired_planned_here", symbol="f", evidence="b")
    heads, absorbed = cluster([first, second])
    assert sorted(h.id for h in heads) == sorted([first.id, second.id])
    assert absorbed == []


def _suppression(line: int) -> Finding:
    return new_finding(
        "policy.unjustified_suppression",
        file="src/a.py",
        line=line,
        message="m",
        evidence=f"line {line}",
        grade=Grade.E1_EXACT,
        source=Source.POLICY,
    )


def test_decide_keeps_every_blocking_finding_near_one_anchor() -> None:
    # Three bare suppressions within 25 lines share an anchor; each is its own fix round,
    # so each stays a blocking finding of its own.
    v = decide(
        [_suppression(5), _suppression(6), _suppression(7)],
        scope=SCOPE,
        tiers=load_tier_table(),
        cfg=_cfg(),
        disclosed=frozenset(),
        responses={},
        rulings=NO_RULINGS,
        focus_inputs=QUIET,
    )
    assert [f.line for f in v.blocking()] == [5, 6, 7]


def test_decide_keeps_an_absorbed_finding_with_a_pointer() -> None:
    weak = _finding("plan.call_edge_missing", symbol="f", grade=Grade.E2_STRUCTURAL)
    strong = _finding("plan.symbol_missing", symbol="f", impact="critical")
    out = _decide([weak, strong])
    absorbed = [f for f in out if f.status == "clustered"]
    assert [f.rule for f in absorbed] == ["plan.call_edge_missing"]
    assert absorbed[0].absorbed_by == next(f.id for f in out if f.status != "clustered")


def test_decide_never_hides_a_blocking_finding_behind_a_stronger_one() -> None:
    blocking = new_finding(
        "policy.protected_path",
        file="src/a.py",
        line=2,
        message="m",
        evidence="e",
        grade=Grade.E1_EXACT,
        source=Source.POLICY,
    )
    stronger = dataclasses.replace(
        _finding("behave.amplify_crash", line=3, grade=Grade.E0_EXECUTED), impact="critical"
    )
    out = _decide([stronger, blocking])
    assert [f.rule for f in out if f.tier is Tier.BLOCKING] == ["policy.protected_path"]
    assert out[0].rule == "policy.protected_path"


def test_decide_sorts_and_records_unverified() -> None:
    v = decide(
        [
            _finding("plan.symbol_missing", symbol="z", line=9),
            _finding("plan.symbol_missing", symbol="a", line=2),
        ],
        scope=SCOPE,
        tiers=load_tier_table(),
        cfg=_cfg(),
        disclosed=frozenset(),
        responses={},
        rulings=NO_RULINGS,
        focus_inputs=QUIET,
        unverified=("ts-unavailable", "parse-error:x.py", "ts-unavailable"),
    )
    assert [f.symbol for f in v.findings] == ["a", "z"]
    assert v.unverified == ("parse-error:x.py", "ts-unavailable")
    assert not v.focus
