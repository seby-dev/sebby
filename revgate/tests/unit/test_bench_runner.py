"""`revgate bench`: the case runner, on synthetic repositories only."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from conftest import MakeRepo, RepoFixture

from revgate.bench.runner import (
    CaseResult,
    bench_data_dir,
    load_cases,
    result_line,
    run_bench,
    run_case,
)
from revgate.spi.cache import BlobCache

UTIL_BASE = "def helper():\n    return 1\n\n\ndef other():\n    return 2\n"
UTIL_HEAD = "def other():\n    return 2\n"
SCRIPT = "from pkg.util import helper\n\nhelper()\n"
BASE = {"pkg/__init__.py": "", "pkg/util.py": UTIL_BASE, "scripts/x.py": SCRIPT}

FENCE = "```"
PLAN = f"""\
# Marks plan

### Task 1: The marks module

**Files:**
- Create: `pkg/marks.py`
- Test: `tests/test_marks.py`

### Task 2: Wire the marks

**Files:**
- Modify: `pkg/sheet.py`

In `pkg/sheet.py`:

{FENCE}python
def draw(grid):
    finalize(grid)
{FENCE}
"""
MARKS = "def finalize(grid):\n    return grid\n"
MARKS_TEST = "from pkg.marks import finalize\n\n\ndef test_finalize():\n    assert finalize(1)\n"


def _dangling_repo(make_repo: MakeRepo) -> RepoFixture:
    return make_repo(BASE, {"pkg/util.py": UTIL_HEAD})


def _case(directory: Path, name: str, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.toml"
    path.write_text(body, encoding="utf-8")
    return path


def _task_case(repo: RepoFixture, tables: str, extra: str = "") -> str:
    return (
        f'id = "T1"\nstage = "1a"\ntier = "fast"\nkind = "task"\nrepo = "{repo.path}"\n'
        f'base = "{repo.base}"\nhead = "{repo.head}"\n{extra}\n{tables}'
    )


def _run(directory: Path, tmp_path: Path) -> CaseResult:
    [case] = load_cases(directory, "1a")
    return run_case(case, cache=BlobCache(tmp_path / "cache", "test"))


def test_an_expected_rule_that_fires_passes(make_repo: MakeRepo, tmp_path: Path) -> None:
    repo = _dangling_repo(make_repo)
    expect = '[[expect]]\nrule = "docs.dangling_code_ref"\nfile = "scripts/x.py"\nline = 1\n'
    _case(tmp_path / "cases", "T1", _task_case(repo, expect))
    result = _run(tmp_path / "cases", tmp_path)
    assert result.passed, result.details
    assert result.case_id == "T1"


def test_an_expected_rule_that_does_not_fire_fails(make_repo: MakeRepo, tmp_path: Path) -> None:
    repo = _dangling_repo(make_repo)
    expect = '[[expect]]\nrule = "wiring.unwired_planned_here"\nfile = "pkg/util.py"\n'
    _case(tmp_path / "cases", "T1", _task_case(repo, expect))
    result = _run(tmp_path / "cases", tmp_path)
    assert not result.passed
    assert any("missing wiring.unwired_planned_here" in d for d in result.details)
    assert result_line(result).startswith("FAIL T1: main: missing wiring.unwired_planned_here")


def test_a_silent_rule_that_fires_fails(make_repo: MakeRepo, tmp_path: Path) -> None:
    repo = _dangling_repo(make_repo)
    silent = '[[silent]]\nrule = "docs.*"\ncontains = "helper"\n'
    _case(tmp_path / "cases", "T1", _task_case(repo, silent))
    result = _run(tmp_path / "cases", tmp_path)
    assert not result.passed
    assert any("not silent" in d and "scripts/x.py:1" in d for d in result.details)


def test_the_tier_and_printed_fields_read_the_routed_finding(
    make_repo: MakeRepo, tmp_path: Path
) -> None:
    # With a .review.toml at base, an owned dangling reference blocks and is printed.
    repo = make_repo(BASE, {"pkg/util.py": UTIL_HEAD}, review_toml="")
    expect = (
        '[[expect]]\nrule = "docs.dangling_code_ref"\nfile = "scripts/x.py"\n'
        'tier = "blocking"\nprinted = true\ngrade = 1\n'
    )
    _case(tmp_path / "cases", "T1", _task_case(repo, expect, 'owns = ["scripts/x.py"]'))
    assert _run(tmp_path / "cases", tmp_path).passed
    unowned = expect.replace('"blocking"', '"advisory"').replace("true", "false")
    _case(tmp_path / "cases", "T1", _task_case(repo, unowned))
    assert _run(tmp_path / "cases", tmp_path).passed


def _plan_repo(make_repo: MakeRepo) -> RepoFixture:
    return make_repo(
        {"pkg/__init__.py": ""},
        {"pkg/marks.py": MARKS, "tests/test_marks.py": MARKS_TEST},
        plan=PLAN,
        plan_path="docs/plans/marks.md",
    )


def test_deferred_obligations_and_their_count(make_repo: MakeRepo, tmp_path: Path) -> None:
    repo = _plan_repo(make_repo)
    plan = 'plan = "docs/plans/marks.md"\ntask = "1"'
    ok = '[[deferred]]\nsymbol = "finalize"\nowner = "2"\n'
    _case(tmp_path / "cases", "T1", _task_case(repo, ok + "deferred_count = 1\n", plan))
    assert _run(tmp_path / "cases", tmp_path).passed

    wrong_owner = ok.replace('"2"', '"3"')
    _case(tmp_path / "cases", "T1", _task_case(repo, wrong_owner, plan))
    result = _run(tmp_path / "cases", tmp_path)
    assert not result.passed
    assert any("no deferred obligation for finalize owned by Task 3" in d for d in result.details)

    _case(tmp_path / "cases", "T1", _task_case(repo, "deferred_count = 2\n", plan))
    result = _run(tmp_path / "cases", tmp_path)
    assert not result.passed
    assert any("1 deferred obligations, expected 2" in d for d in result.details)


def test_overrides_replace_a_path_at_head(make_repo: MakeRepo, tmp_path: Path) -> None:
    # Head is identical to base; the seed file removes `helper`, so the reference dangles.
    repo = make_repo(BASE, {})
    cases = tmp_path / "cases"
    (cases / "seeds").mkdir(parents=True)
    (cases / "seeds" / "util.py").write_text(UTIL_HEAD, encoding="utf-8")
    body = (
        '[overrides]\n"pkg/util.py" = "seeds/util.py"\n\n'
        '[[expect]]\nrule = "docs.dangling_code_ref"\nfile = "scripts/x.py"\n'
    )
    _case(cases, "T1", _task_case(repo, body))
    assert _run(cases, tmp_path).passed
    _case(cases, "T1", _task_case(repo, body.replace("[overrides]", "[unused]")))
    assert not _run(cases, tmp_path).passed


def test_bench_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = tmp_path / "data"
    (data / "inputs").mkdir(parents=True)
    monkeypatch.setenv("REVGATE_BENCH_DATA", str(data))
    assert bench_data_dir() == data
    assert bench_data_dir(data) == data
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(FileNotFoundError, match="empty"):
        bench_data_dir(empty)
    monkeypatch.setenv("REVGATE_BENCH_DATA", str(empty))
    with pytest.raises(FileNotFoundError):
        bench_data_dir()


def test_run_bench_prints_one_line_per_case_and_exits_1_on_a_failure(
    make_repo: MakeRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("REVGATE_BENCH_DATA", raising=False)
    monkeypatch.setattr("revgate.bench.runner.DEFAULT_DATA", tmp_path / "no-data")
    repo = _dangling_repo(make_repo)
    cases = tmp_path / "cases"
    good = '[[expect]]\nrule = "docs.dangling_code_ref"\nfile = "scripts/x.py"\n'
    bad = '[[expect]]\nrule = "contract.cardinality_consumer"\nfile = "scripts/x.py"\n'
    _case(cases, "C1", _task_case(repo, good).replace('id = "T1"', 'id = "C1"'))
    _case(cases, "C2", _task_case(repo, bad).replace('id = "T1"', 'id = "C2"'))
    slow = _task_case(repo, bad).replace('id = "T1"', 'id = "C3"').replace('"fast"', '"full"')
    _case(cases, "C3", slow)

    out = io.StringIO()
    code = run_bench("1a", "fast", None, write_baseline=False, cases=cases, repo=repo.path, out=out)
    lines = out.getvalue().splitlines()
    assert code == 1
    assert lines[0].startswith("PASS C1 ") and lines[0].endswith(" s")
    assert lines[1].startswith("FAIL C2: main: missing contract.cardinality_consumer")
    assert not any("C3" in line for line in lines)  # a full-tier case needs --tier full

    out = io.StringIO()
    code = run_bench(
        "1a", "full", ["C1"], write_baseline=True, cases=cases, repo=repo.path, out=out
    )
    assert code == 0
    baseline = tmp_path / "baselines" / "stage-1a.json"
    assert baseline.is_file()
    assert '"id": "C1"' in baseline.read_text(encoding="utf-8")


def test_a_case_that_needs_missing_data_fails_with_the_reason(
    make_repo: MakeRepo, tmp_path: Path
) -> None:
    repo = _dangling_repo(make_repo)
    body = (
        f'id = "C10"\nstage = "1a"\ntier = "fast"\nkind = "corpus"\nrepo = "{repo.path}"\n'
        'commits_file = "inputs/commits.txt"\nlimit = 3\n'
    )
    _case(tmp_path / "cases", "C10", body)
    [case] = load_cases(tmp_path / "cases", "1a")
    result = run_case(case, cache=BlobCache(tmp_path / "cache", "test"))
    assert not result.passed
    assert "no benchmark data directory" in result_line(result)


def test_a_corpus_case_checks_silence_per_commit(make_repo: MakeRepo, tmp_path: Path) -> None:
    repo = _dangling_repo(make_repo)
    data = tmp_path / "data"
    (data / "inputs").mkdir(parents=True)
    (data / "inputs" / "commits.txt").write_text(f"{repo.head}\n{repo.base}\n")
    body = (
        f'id = "C10"\nstage = "1a"\ntier = "fast"\nkind = "corpus"\nrepo = "{repo.path}"\n'
        'commits_file = "inputs/commits.txt"\nlimit = 5\n'
        '[[silent]]\nrule = "docs.dangling_code_ref"\n'
    )
    _case(tmp_path / "cases", "C10", body)
    [case] = load_cases(tmp_path / "cases", "1a")
    result = run_case(case, cache=BlobCache(tmp_path / "cache", "test"), data=data)
    assert not result.passed  # the head commit removes `helper`; the root commit is skipped
    assert result.details[0] == "commits 2, blocking 0"
    assert any(repo.head[:9] in d and "not silent" in d for d in result.details)


def _data(tmp_path: Path, files: dict[str, str]) -> Path:
    data = tmp_path / "data"
    (data / "inputs").mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (data / "inputs" / name).write_text(text, encoding="utf-8")
    return data


def _one(tmp_path: Path, body: str, data: Path) -> CaseResult:
    _case(tmp_path / "cases", "X", body)
    [case] = load_cases(tmp_path / "cases", "1a")
    return run_case(case, cache=BlobCache(tmp_path / "cache", "test"), data=data)


def test_a_ratchet_corpus_compares_raw_counts(make_repo: MakeRepo, tmp_path: Path) -> None:
    test_base = "def test_a():\n    assert 1\n\n\ndef test_b():\n    assert 2\n"
    repo = make_repo(
        {"tests/test_x.py": test_base}, {"tests/test_x.py": "def test_a():\n    assert 1\n"}
    )
    units = f'{{"units": [{{"fork": "{repo.base}", "tip": "{repo.head}", "files": []}}]}}'
    head = (
        f'id = "C11"\nstage = "1a"\ntier = "fast"\nkind = "ratchet_corpus"\n'
        f'repo = "{repo.path}"\nunits_file = "inputs/units.json"\n'
        'signals_file = "inputs/rows.json"\nsignals = ["test_deleted", "suppress_new"]\n'
    )
    ok = _one(tmp_path, head, _data(tmp_path, {"units.json": units, "rows.json": "[{}]"}))
    assert ok.passed, ok.details  # 1 deleted test against a sparse row's 0: within 1
    far = _data(tmp_path, {"rows.json": '[{"test_deleted": 3}]'})
    result = _one(tmp_path, head, far)
    assert not result.passed
    assert "test_deleted: 1 vs r5 3" in result_line(result)
    accepted = head + '[[accepted_mismatches]]\nunit = 0\nsignal = "test_deleted"\nreason = "x"\n'
    assert _one(tmp_path, accepted, far).passed


def test_a_perf_case_times_cold_and_warm_runs(make_repo: MakeRepo, tmp_path: Path) -> None:
    repo = _dangling_repo(make_repo)
    data = _data(tmp_path, {"commits.txt": f"{repo.head}\n"})
    body = (
        f'id = "C30"\nstage = "1a"\ntier = "full"\nkind = "perf"\nrepo = "{repo.path}"\n'
        'commits_file = "inputs/commits.txt"\n'
    )
    result = _one(tmp_path, body, data)
    assert result.passed, result.details
    assert result.details[0].startswith("cold ") or result.details[0].startswith("SKIP")


def test_delivery_matches_by_file_symbol_and_category(make_repo: MakeRepo, tmp_path: Path) -> None:
    repo = make_repo(BASE, {"pkg/util.py": UTIL_HEAD}, review_toml="")
    rows = (
        '[{"id": "D#1", "base": null, "head": null, "file": "scripts/x.py", "symbol": null,'
        ' "lines": [1, 1], "category": "docs", "severity": "critical"},'
        ' {"id": "D#2", "base": null, "head": null, "file": "scripts/x.py", "symbol": null,'
        ' "lines": [1, 1], "category": "ui-state", "severity": "minor"}]'
    )
    body = (
        f'id = "delivery"\nstage = "1a"\ntier = "fast"\nkind = "delivery"\n'
        f'repo = "{repo.path}"\nfindings_file = "inputs/d.json"\n'
        'must_reach_implementer = ["D#1"]\n'
        f'[[range]]\nid = "D#1"\nbase = "{repo.base}"\nhead = "{repo.head}"\n'
        f'[[range]]\nid = "D#2"\nbase = "{repo.base}"\nhead = "{repo.head}"\n'
    )
    result = _one(tmp_path, body, _data(tmp_path, {"d.json": rows}))
    # D#1 (critical, 3) matches; D#2 (minor, 0.5) has the wrong category. With no plan the
    # range owns only what it changed (pkg/util.py), so the finding in scripts/x.py goes
    # to the controller, not the implementer.
    assert not result.passed
    assert result.details[0].startswith(
        "FRR_delivered_task 0.000 (none); FRR_delivered_any 0.857 (D#1)"
    )
    assert result_line(result) == "FAIL delivery: D#1 didn't reach the implementer"
