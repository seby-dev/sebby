from __future__ import annotations

from pathlib import Path

import pytest

from revgate import labels
from revgate.model import Finding, Grade, RunFile, Source, new_finding
from revgate.store import (
    append_jsonl_locked,
    findings_log_path,
    ledger_entries,
    ledger_rulings,
    read_jsonl,
)


def finding(rule: str = "plan.symbol_missing", line: int = 3) -> Finding:
    return new_finding(
        rule,
        file="a.py",
        line=line,
        message="m",
        evidence=f"e{line}",
        grade=Grade.E1_EXACT,
        source=Source.DECLARED,
    )


def run_file(*findings: Finding, task: str | None = "T3", head: str = "ef45ab6") -> RunFile:
    return RunFile(
        task=task,
        role="implementer",
        base="ab12cd3",
        head=head,
        plan=None,
        plan_task_hash=None,
        revgate_version="0.2.0",
        source_hash="s",
        config_hash="",
        focus=False,
        focus_reasons=(),
        findings=tuple(findings),
        obligations=(),
        unverified=(),
        incomplete=(),
        exit_code=0,
    )


def test_log_findings_writes_one_row_per_finding_with_run_context(tmp_path: Path) -> None:
    a, b = finding(line=3), finding(line=9)
    labels.log_findings(tmp_path, run_file(a, b))
    rows = read_jsonl(findings_log_path(tmp_path))
    assert [r["type"] for r in rows] == ["finding", "finding"]
    assert [r["id"] for r in rows] == [a.id, b.id]
    assert rows[0]["task"] == "T3"
    assert rows[0]["head"] == "ef45ab6"
    assert rows[0]["role"] == "implementer"
    assert rows[0]["rule"] == "plan.symbol_missing"


def test_find_finding_returns_the_latest_row(tmp_path: Path) -> None:
    f = finding()
    labels.log_findings(tmp_path, run_file(f, head="aaa1111"))
    labels.log_findings(tmp_path, run_file(f, head="bbb2222"))
    row = labels.find_finding(tmp_path, f.id)
    assert row is not None
    assert row["head"] == "bbb2222"
    assert labels.find_finding(tmp_path, "nope") is None


def test_mark_appends_a_label_row(tmp_path: Path) -> None:
    f = finding()
    labels.log_findings(tmp_path, run_file(f))
    row = labels.mark(tmp_path, f.id, "tp", labeler="controller", note="real")
    assert row["type"] == "label"
    assert row["id"] == f.id
    assert row["label"] == "tp"
    assert row["rule"] == f.rule
    assert row["labeler"] == "controller"
    assert row["strength"] == "explicit"
    assert read_jsonl(findings_log_path(tmp_path))[-1] == row


def test_mark_fp_ruling_appends_an_explained_ledger_ruling(tmp_path: Path) -> None:
    f = finding()
    labels.log_findings(tmp_path, run_file(f))
    labels.mark(tmp_path, f.id, "fp", labeler="adjudicator", ruling=True, plan_slug="p1")
    assert ledger_rulings(tmp_path, "p1", "T3") == frozenset(
        {(f.rule, f.cluster_key, f.witness_digest)}
    )
    (entry,) = ledger_entries(tmp_path, "p1")
    assert entry["kind"] == "ruling"
    assert entry["verdict"] == "explained"


def test_mark_tp_ruling_appends_an_upheld_ledger_ruling(tmp_path: Path) -> None:
    f = finding()
    labels.log_findings(tmp_path, run_file(f))
    labels.mark(tmp_path, f.id, "tp", labeler="adjudicator", ruling=True, plan_slug="p1")
    (entry,) = ledger_entries(tmp_path, "p1")
    assert entry["verdict"] == "upheld"
    assert ledger_rulings(tmp_path, "p1", "T3") == frozenset()


def test_mark_ruling_without_a_plan_slug_is_refused(tmp_path: Path) -> None:
    f = finding()
    labels.log_findings(tmp_path, run_file(f))
    with pytest.raises(ValueError):
        labels.mark(tmp_path, f.id, "fp", labeler="x", ruling=True)
    assert [r["type"] for r in read_jsonl(findings_log_path(tmp_path))] == ["finding"]


def test_mark_on_an_unknown_id_raises_key_error(tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        labels.mark(tmp_path, "missing", "tp", labeler="x")


def test_unlabeled_excludes_marked_findings(tmp_path: Path) -> None:
    a, b, c = finding(line=1), finding(line=2), finding(line=3)
    labels.log_findings(tmp_path, run_file(a, b, c))
    labels.log_findings(tmp_path, run_file(a, head="bbb2222"))
    labels.mark(tmp_path, b.id, "fp", labeler="x")
    assert [r["id"] for r in labels.unlabeled(tmp_path, 10)] == [a.id, c.id]
    assert [r["id"] for r in labels.unlabeled(tmp_path, 1)] == [a.id]


def test_rule_stats_counts_only_explicit_labels(tmp_path: Path) -> None:
    a, b, c = finding(line=1), finding(line=2), finding("wiring.dead", line=3)
    labels.log_findings(tmp_path, run_file(a, b, c))
    labels.mark(tmp_path, a.id, "tp", labeler="x")
    labels.mark(tmp_path, b.id, "fp", labeler="x")
    labels.mark(tmp_path, c.id, "tp", labeler="x")
    # A weak label from another source never counts toward precision.
    append_jsonl_locked(
        findings_log_path(tmp_path),
        {"type": "label", "id": c.id, "rule": c.rule, "label": "fp", "strength": "weak"},
    )
    assert labels.rule_stats(tmp_path) == {"plan.symbol_missing": (1, 1), "wiring.dead": (1, 0)}


def test_rule_stats_keeps_the_latest_label_per_finding_and_the_last_n(tmp_path: Path) -> None:
    fs = [finding(line=i) for i in range(1, 6)]
    labels.log_findings(tmp_path, run_file(*fs))
    labels.mark(tmp_path, fs[0].id, "fp", labeler="x")
    for f in fs:
        labels.mark(tmp_path, f.id, "tp", labeler="x")
    assert labels.rule_stats(tmp_path) == {"plan.symbol_missing": (5, 0)}
    assert labels.rule_stats(tmp_path, last=2) == {"plan.symbol_missing": (2, 0)}


def test_wilson_lower_bound_matches_the_spec_thresholds() -> None:
    assert 0.71 < labels.wilson_lower_bound(7, 7) < 0.73
    assert labels.wilson_lower_bound(6, 6) < 0.70
    assert labels.wilson_lower_bound(45, 46) >= 0.90
    assert labels.wilson_lower_bound(44, 46) < 0.90
    assert labels.wilson_lower_bound(0, 0) == 0.0
