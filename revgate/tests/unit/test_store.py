from __future__ import annotations

import json
from pathlib import Path

import pytest

from revgate.model import Grade, RunFile, Source, new_finding
from revgate.store import (
    canonical_json,
    findings_log_path,
    latest_controller_runs,
    ledger_append,
    ledger_assigned,
    ledger_entries,
    ledger_rulings,
    read_run_file,
    run_cache_get,
    run_cache_put,
    run_file_path,
    write_meta,
    write_run_file,
)


def run_file(task: str | None = "T3", head: str = "ef45ab6") -> RunFile:
    f = new_finding(
        "plan.symbol_missing",
        file="a.py",
        line=3,
        message="m",
        evidence="e",
        grade=Grade.E1_EXACT,
        source=Source.DECLARED,
    )
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
        findings=(f,),
        obligations=(),
        unverified=(),
        incomplete=(),
        exit_code=0,
        provisional=True,
    )


def test_canonical_json_sorts_keys_without_spaces() -> None:
    assert canonical_json({"b": 1, "a": [1, {"d": 2, "c": 3}]}) == ('{"a":[1,{"c":3,"d":2}],"b":1}')


def test_run_file_round_trips_at_its_path(tmp_path: Path) -> None:
    rf = run_file()
    path = write_run_file(tmp_path, rf)
    assert path == tmp_path / "runs" / "T3" / "ef45ab6.implementer.json"
    assert path == run_file_path(tmp_path, "T3", "ef45ab6", "implementer")
    assert read_run_file(path) == rf
    assert write_run_file(tmp_path, rf).read_bytes() == path.read_bytes()


def test_adhoc_run_file_path(tmp_path: Path) -> None:
    assert write_run_file(tmp_path, run_file(task=None)) == (
        tmp_path / "runs" / "adhoc" / "ef45ab6.implementer.json"
    )


@pytest.mark.parametrize(
    ("task", "head", "role"),
    [("../x", "h", "r"), ("T1", "a/b", "r"), ("T1", "h", ".."), ("", "h", "r")],
)
def test_run_file_path_rejects_path_components(
    tmp_path: Path, task: str, head: str, role: str
) -> None:
    with pytest.raises(ValueError):
        run_file_path(tmp_path, task, head, role)


def test_meta_sidecar_holds_timings(tmp_path: Path) -> None:
    path = write_meta(tmp_path, "T3", "ef45ab6", "implementer", {"timings": {"static": 2.5}})
    assert path == tmp_path / "runs" / "T3" / "ef45ab6.implementer.meta.json"
    assert json.loads(path.read_text()) == {"timings": {"static": 2.5}}


def test_ledger_appends_in_order(tmp_path: Path) -> None:
    ledger_append(tmp_path, "plan-a", {"kind": "run", "task": "T1", "role": "implementer"})
    ledger_append(tmp_path, "plan-a", {"kind": "run", "task": "T1", "role": "controller"})
    rows = ledger_entries(tmp_path, "plan-a")
    assert [r["role"] for r in rows] == ["implementer", "controller"]
    assert ledger_entries(tmp_path, "other-plan") == []


def test_ledger_rulings_are_explained_rulings_for_the_task(tmp_path: Path) -> None:
    def ruling(task: str, rule: str, verdict: str, digest: str | None) -> dict[str, object]:
        return {
            "kind": "ruling",
            "task": task,
            "rule": rule,
            "cluster_key": f"{rule}|f|x",
            "witness_digest": digest,
            "verdict": verdict,
            "labeler": "controller",
            "note": "n",
        }

    ledger_append(tmp_path, "p", ruling("T1", "a.one", "explained", None))
    ledger_append(tmp_path, "p", ruling("T1", "a.two", "explained", "d2"))
    ledger_append(tmp_path, "p", ruling("T1", "a.three", "rejected", None))
    ledger_append(tmp_path, "p", ruling("T2", "a.four", "explained", None))
    ledger_append(tmp_path, "p", {"kind": "run", "task": "T1", "rule": "a.five"})
    assert ledger_rulings(tmp_path, "p", "T1") == frozenset(
        {("a.one", "a.one|f|x", None), ("a.two", "a.two|f|x", "d2")}
    )
    assert ledger_rulings(tmp_path, "p", "T9") == frozenset()


def test_ledger_assigned_paths(tmp_path: Path) -> None:
    ledger_append(tmp_path, "p", {"kind": "assign", "task": "T1", "path": "x.py", "reason": "r"})
    ledger_append(tmp_path, "p", {"kind": "assign", "task": "T2", "path": "y.py", "reason": "r"})
    assert ledger_assigned(tmp_path, "p", "T1") == frozenset({"x.py"})


def test_latest_controller_runs(tmp_path: Path) -> None:
    for task, role, head in [
        ("T1", "controller", "h1"),
        ("T1", "implementer", "h2"),
        ("T1", "controller", "h3"),
        ("T2", "implementer", "h4"),
        ("T3", "controller", "h5"),
    ]:
        ledger_append(tmp_path, "p", {"kind": "run", "task": task, "role": role, "head": head})
    latest = latest_controller_runs(tmp_path, "p")
    assert set(latest) == {"T1", "T3"}
    assert latest["T1"]["head"] == "h3"


def test_run_cache_round_trips(tmp_path: Path) -> None:
    assert run_cache_get(tmp_path, "k1") is None
    run_cache_put(tmp_path, "k1", '{"x":1}')
    run_cache_put(tmp_path, "k/../2", "two")
    assert run_cache_get(tmp_path, "k1") == '{"x":1}'
    assert run_cache_get(tmp_path, "k/../2") == "two"


def test_findings_log_path(tmp_path: Path) -> None:
    assert findings_log_path(tmp_path) == tmp_path / "findings.jsonl"
