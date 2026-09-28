from __future__ import annotations

import dataclasses
import json

from revgate.model import (
    Anchor,
    Evidence,
    Finding,
    Grade,
    Obligation,
    Pin,
    Provenance,
    RunFile,
    Source,
    TaskScope,
    Tier,
    Unverified,
    Verdict,
    Witness,
    finding_from_dict,
    finding_to_dict,
    new_finding,
    obligation_from_dict,
    obligation_to_dict,
    run_file_from_json,
    run_file_to_json,
)


def make(**kw: object) -> Finding:
    args: dict[str, object] = {
        "file": "src/pkg/mod.py",
        "line": 10,
        "message": "m",
        "evidence": "e",
        "grade": Grade.E1_EXACT,
        "source": Source.GENERIC,
    }
    args.update(kw)
    return new_finding("wiring.unwired_new_symbol", **args)  # type: ignore[arg-type]


def test_enums_have_the_spec_values() -> None:
    assert [g.value for g in Grade] == [0, 1, 2, 3, 4]
    assert Grade.E2_STRUCTURAL > Grade.E1_EXACT
    assert Source("statistical") is Source.STATISTICAL
    assert [t.value for t in Tier] == ["blocking", "advisory", "shadow"]
    assert Provenance.EDITED_PLANNED.value == "edited_planned"


def test_anchor_uses_a_bucket_only_without_a_symbol() -> None:
    assert Anchor.of("a.py", None, 60) == Anchor("a.py", None, 2)
    assert Anchor.of("a.py", "m.f", 60) == Anchor("a.py", "m.f", None)
    assert Anchor.of("a.py", "m.f", 60).key() == "a.py::m.f"
    assert Anchor.of("a.py", None, 60).key() != Anchor.of("a.py", None, 80).key()


def test_new_finding_truncates_message_evidence_and_fix() -> None:
    f = make(message="m" * 200, evidence="e" * 400, fix="f" * 150)
    assert len(f.message) == 110 and f.message.endswith("…")
    assert len(f.evidence) == 220 and f.evidence.endswith("…")
    assert f.fix is not None and len(f.fix) == 100 and f.fix.endswith("…")
    short = make(message="short", evidence="fact", fix="do it")
    assert (short.message, short.evidence, short.fix) == ("short", "fact", "do it")


def test_new_finding_defaults() -> None:
    f = make()
    assert f.tier is Tier.SHADOW
    assert f.review is False
    assert f.status == "open"
    assert f.audience == frozenset({"log"})
    assert f.verify == f"revgate recheck {f.id}"
    assert f.end_line == 10
    assert f.round == 1
    assert f.also == ()
    assert f.kind == "defect" and f.impact == "important"


def test_same_inputs_give_the_same_id_and_cluster_key() -> None:
    a, b = make(evidence="x"), make(evidence="x")
    assert a.id == b.id and len(a.id) == 8
    assert a.cluster_key == b.cluster_key
    assert make(evidence="y").id != a.id


def test_cluster_key_is_line_free_when_a_symbol_is_set() -> None:
    a = make(line=10, evidence="x at :12", symbol="pkg.mod.f")
    b = make(line=40, evidence="x at :57", symbol="pkg.mod.f")
    assert a.cluster_key == b.cluster_key
    assert not any(ch.isdigit() for ch in a.cluster_key.split("|")[-1])


def test_evidence_key_overrides_evidence_for_identity() -> None:
    a = make(evidence="one", evidence_key="k")
    b = make(evidence="two", evidence_key="k")
    assert a.id == b.id and a.cluster_key == b.cluster_key


def test_finding_round_trips_through_a_dict() -> None:
    f = dataclasses.replace(
        make(symbol="pkg.mod.f", fix="fix it", witness_digest="abc"),
        tier=Tier.BLOCKING,
        audience=frozenset({"wave", "implementer", "log"}),
        also=("plan.call_edge_missing",),
    )
    d = finding_to_dict(f)
    assert d["grade"] == 1
    assert d["tier"] == "blocking"
    assert d["audience"] == ["implementer", "log", "wave"]
    assert d["also"] == ["plan.call_edge_missing"]
    assert finding_from_dict(json.loads(json.dumps(d))) == f


def obligation() -> Obligation:
    return Obligation(
        oid="a17c30e9d2",
        kind="contract.consumer_compat",
        rule="wiring.unwired_new_symbol",
        anchor=Anchor.of("src/pkg/mod.py", "pkg.mod.f", 3),
        source="plan:T3:code-block:2",
        owner="T3",
        params={"callee": "f"},
        engines=("static",),
        impact="critical",
        question="q1",
        status="deferred",
    )


def test_obligation_round_trips_through_a_dict() -> None:
    ob = obligation()
    assert obligation_from_dict(json.loads(json.dumps(obligation_to_dict(ob)))) == ob


def run_file() -> RunFile:
    return RunFile(
        task="T1",
        role="controller",
        base="ab12cd3",
        head="ef45ab6",
        plan="docs/plans/plan.md",
        plan_task_hash="h",
        revgate_version="0.2.0",
        source_hash="s",
        config_hash="c",
        focus=True,
        focus_reasons=("risk.paths:src/pkg/mod.py",),
        findings=(make(), make(evidence="other")),
        obligations=(obligation(),),
        unverified=("parse-error:x.py",),
        incomplete=(),
        exit_code=1,
    )


def test_run_file_round_trips_and_is_canonical() -> None:
    rf = run_file()
    text = run_file_to_json(rf)
    assert run_file_from_json(text) == rf
    assert ": " not in text and ", " not in text
    data = json.loads(text)
    assert list(data) == sorted(data)
    assert data["revgate"] == {"source_hash": "s", "version": "0.2.0"}
    assert text == json.dumps(data, sort_keys=True, separators=(",", ":"))
    assert not {"timings", "duration", "cache_hit"} & set(data)
    assert run_file_to_json(run_file()) == text


def test_verdict_blocking_needs_blocking_tier_live_status_and_implementer() -> None:
    base = make()
    impl = frozenset({"implementer", "log"})
    keep_open = dataclasses.replace(base, tier=Tier.BLOCKING, audience=impl)
    keep_ack = dataclasses.replace(keep_open, id="2", status="acknowledged")
    explained = dataclasses.replace(keep_open, id="3", status="explained")
    controller = dataclasses.replace(keep_open, id="4", audience=frozenset({"controller"}))
    advisory = dataclasses.replace(keep_open, id="5", tier=Tier.ADVISORY)
    v = Verdict(
        findings=(keep_open, keep_ack, explained, controller, advisory),
        focus=False,
        focus_reasons=(),
        obligations=(),
        unverified=(),
    )
    assert v.blocking() == (keep_open, keep_ack)


def test_task_scope_covers_owns_runs_and_ledger_assignments() -> None:
    scope = TaskScope("T1", frozenset({"a.py"}), frozenset({"tests/t.py"}), frozenset({"b.py"}))
    assert scope.covers("a.py") and scope.covers("tests/t.py") and scope.covers("b.py")
    assert not scope.covers("c.py")


def test_evidence_witness_pin_and_unverified_are_frozen_values() -> None:
    w = Witness("command", "s", None, None, None, "pytest -q; exit 1")
    ev = Evidence("oid", "static", "violated", Grade.E0_EXECUTED, "c", w, 1)
    pin = Pin("py:a.py::f", "added", True, "n/a:added", "killed", (3, 4), "none", True)
    u = Unverified("plan.symbol_missing", "no plan block")
    for obj in (w, ev, pin, u):
        assert dataclasses.is_dataclass(obj)
        try:
            obj.__setattr__("summary", "x")
        except dataclasses.FrozenInstanceError:
            pass
        else:  # pragma: no cover
            raise AssertionError(f"{type(obj).__name__} is mutable")
