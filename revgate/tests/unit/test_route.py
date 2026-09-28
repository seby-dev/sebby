"""Routing: tiers from the table, audiences, focus, and ordering."""

from __future__ import annotations

import dataclasses

from conftest import MakeRepo

from revgate.config import Config, load_config, parse_config
from revgate.model import Finding, Grade, Impact, Source, TaskScope, Tier, new_finding
from revgate.rules.registry import load_tier_table
from revgate.verdict.route import (
    PRIOR,
    FocusInputs,
    audience_of,
    focus_flags,
    in_task_scope,
    route,
    sort_key,
    tier_of,
)

TEMPLATED = ("lesson.<id>", "distilled.<id>", "family.<id>")
SCOPE = TaskScope("T1", owns=frozenset({"src/a.py"}), runs=frozenset())


def _finding(
    rule: str,
    *,
    grade: Grade = Grade.E1_EXACT,
    source: Source = Source.GENERIC,
    file: str = "src/a.py",
    impact: Impact = "important",
) -> Finding:
    return new_finding(
        rule,
        file=file,
        line=1,
        message="m",
        evidence="e",
        grade=grade,
        source=source,
        impact=impact,
    )


def _configured(make_repo: MakeRepo) -> Config:
    repo = make_repo({"src/a.py": "x = 1\n"}, {}, review_toml="[routing]\nmax_lines = 400\n")
    cfg = load_config(repo.path, repo.head)
    assert not cfg.is_default
    return cfg


def test_tier_of_matches_appendix_a(make_repo: MakeRepo) -> None:
    table = load_tier_table()
    cfg = _configured(make_repo)
    default = parse_config(None)
    for spec in table.values():
        if spec.id in TEMPLATED:
            continue
        f = _finding(spec.id, grade=spec.grade, source=spec.source)
        assert tier_of(f, table, cfg, SCOPE) is spec.tier, spec.id
        expected_default = Tier.BLOCKING if spec.id == "gate.failed" else Tier.SHADOW
        assert tier_of(f, table, default, SCOPE) is expected_default, spec.id


def test_blocking_outside_scope_goes_to_the_controller(make_repo: MakeRepo) -> None:
    table = load_tier_table()
    cfg = _configured(make_repo)
    f = _finding("policy.protected_path", source=Source.POLICY, file="src/other.py")
    routed = route(f, table, cfg, SCOPE)
    assert routed.tier is Tier.ADVISORY
    assert routed.audience == frozenset({"controller", "log"})


def test_audiences_by_grade_and_review(make_repo: MakeRepo) -> None:
    table = load_tier_table()
    cfg = _configured(make_repo)
    e2 = route(_finding("plan.call_edge_missing", grade=Grade.E2_STRUCTURAL), table, cfg, SCOPE)
    assert e2.tier is Tier.ADVISORY
    assert "implementer" not in e2.audience
    e1 = route(_finding("wiring.unwired_new_symbol"), table, cfg, SCOPE)
    assert e1.tier is Tier.ADVISORY and e1.review
    assert {"implementer", "wave"} <= e1.audience
    shadow = route(_finding("contract.optional_unguarded"), table, cfg, SCOPE)
    assert shadow.tier is Tier.SHADOW and shadow.audience == frozenset({"log"})
    blocking = route(_finding("policy.protected_path", source=Source.POLICY), table, cfg, SCOPE)
    assert blocking.audience == frozenset({"implementer", "wave", "log"})
    assert "controller" in audience_of(blocking, SCOPE, disputed=True)


def test_in_task_scope_and_downgrades(make_repo: MakeRepo) -> None:
    table = load_tier_table()
    cfg = _configured(make_repo)
    assert in_task_scope(_finding("gate.failed", file="anything"), SCOPE)
    assert in_task_scope(_finding("wave.integrity", file="elsewhere.py"), SCOPE)
    assert not in_task_scope(_finding("docs.dangling_code_ref", file="b.py"), SCOPE)
    minor = _finding("policy.protected_path", source=Source.POLICY, impact="minor")
    assert tier_of(minor, table, cfg, SCOPE) is Tier.ADVISORY
    weak = _finding("policy.protected_path", source=Source.POLICY, grade=Grade.E2_STRUCTURAL)
    assert tier_of(weak, table, cfg, SCOPE) is Tier.ADVISORY
    stat = _finding("policy.protected_path", source=Source.STATISTICAL)
    assert tier_of(stat, table, cfg, SCOPE) is Tier.ADVISORY
    deferred = dataclasses.replace(_finding("policy.protected_path"), status="deferred")
    assert tier_of(deferred, table, cfg, SCOPE) is Tier.ADVISORY
    unknown = _finding("no.such_rule")
    assert tier_of(unknown, table, cfg, SCOPE) is Tier.SHADOW


def test_rule_override_replaces_the_table_tier() -> None:
    table = load_tier_table()
    cfg = parse_config(b'[rules]\n"docs.dangling_prose_ref" = { tier = "blocking" }\n')
    f = _finding("docs.dangling_prose_ref")
    assert tier_of(f, table, cfg, SCOPE) is Tier.BLOCKING


def test_focus_flags_report_every_condition() -> None:
    table = load_tier_table()
    cfg = parse_config(b'[risk.paths]\n"src/pitch.py" = "wave"\n"web/src/**" = "wave"\n')
    review_f = route(_finding("wiring.unwired_new_symbol"), table, cfg, SCOPE)
    inputs = FocusInputs(
        risk="high",
        changed_paths=("src/a.py", "src/pitch.py", "web/src/x.ts"),
        nontest_changed_lines=512,
        suppression_added=True,
        review_toml_edited=True,
    )
    focus, reasons = focus_flags([review_f], inputs, cfg)
    assert focus
    assert reasons == (
        "risk: high",
        "risk.paths:src/pitch.py",
        "risk.paths:web/src/x.ts",
        "review rule wiring.unwired_new_symbol",
        "suppression added",
        ".review.toml edited",
        "diff 512 > max_lines 400",
    )
    quiet = FocusInputs(None, ("src/a.py",), 10, False, False)
    shadow = route(_finding("contract.optional_unguarded"), table, cfg, SCOPE)
    assert focus_flags([shadow], quiet, cfg) == (False, ())


def test_sort_key_orders_by_tier_impact_and_prior() -> None:
    assert PRIOR[Grade.E0_EXECUTED] == 0.95 and PRIOR[Grade.E4_CONTEXT] == 0.0
    a = dataclasses.replace(_finding("r.a", grade=Grade.E2_STRUCTURAL), tier=Tier.ADVISORY)
    b = dataclasses.replace(_finding("r.b", grade=Grade.E0_EXECUTED), tier=Tier.ADVISORY)
    c = dataclasses.replace(_finding("r.c", impact="critical"), tier=Tier.ADVISORY)
    d = dataclasses.replace(_finding("r.d"), tier=Tier.BLOCKING, impact="minor")
    assert [f.rule for f in sorted([a, b, c, d], key=sort_key)] == ["r.d", "r.c", "r.b", "r.a"]
