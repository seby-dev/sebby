"""`revgate task`: preflight, inputs, the run cache, gates, the static phase, and outputs."""

from __future__ import annotations

import io
import json
import sys
from collections.abc import Iterable
from pathlib import Path

import pytest
from conftest import MakeRepo, RepoFixture, git

import revgate.static.all_rules as all_rules
from revgate.gitio import common_dir
from revgate.model import RuleOutput, run_file_from_json
from revgate.store import findings_log_path, ledger_entries, read_jsonl, run_file_path
from revgate.task_cmd import TaskArgs, run_task, source_hash

SHAPES = "class Base:\n    pass\n\n\nclass Sub(Base):\n    pass\n"
USE_BASE = 'from pkg.shapes import Base, Sub\n\n\ndef kind(x):\n    return "none"\n'
SHADOWED = (
    "from pkg.shapes import Base, Sub\n"
    "\n"
    "\n"
    "def kind(x):\n"
    "    if isinstance(x, Base):\n"
    '        return "base"\n'
    "    elif isinstance(x, Sub):\n"
    '        return "sub"\n'
    '    return "none"\n'
)
BASE_FILES = {"pkg/__init__.py": "", "pkg/shapes.py": SHAPES, "pkg/use.py": USE_BASE}
PLAN_PATH = "docs/plans/plan.md"
PLAN = (
    "# Plan\n\n"
    "### Task T1: dispatch on shapes\n\n"
    "**Files:**\n"
    "- Modify: `pkg/use.py`\n\n"
    "- [ ] **Step 1:** edit `kind`.\n"
)
REVIEW_TOML = '[policy]\nrevgate_source_repo = "/nonexistent/revgate-source"\n'


def _state(repo: Path) -> Path:
    return common_dir(repo) / "revgate"


def _args(fx: RepoFixture, **kw: object) -> TaskArgs:
    params: dict[str, object] = {
        "repo": fx.path,
        "base": fx.base,
        "head": fx.head,
        "role": "implementer",
    }
    params.update(kw)
    return TaskArgs(**params)  # type: ignore[arg-type]


def _run(fx: RepoFixture, **kw: object) -> tuple[int, str]:
    out = io.StringIO()
    code = run_task(_args(fx, **kw), out=out)
    return code, out.getvalue()


def _run_file(fx: RepoFixture, task: str | None = "T1", role: str = "implementer") -> Path:
    return run_file_path(_state(fx.path), task, fx.head, role)


def _meta(fx: RepoFixture, task: str | None = "T1", role: str = "implementer") -> dict[str, object]:
    path = _run_file(fx, task, role).with_suffix(".meta.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _blocking_repo(make_repo: MakeRepo, review_toml: str | None = REVIEW_TOML) -> RepoFixture:
    return make_repo(
        BASE_FILES,
        {"pkg/use.py": SHADOWED},
        plan=PLAN,
        plan_path=PLAN_PATH,
        review_toml=review_toml,
    )


# --- preflight ---------------------------------------------------------------------------------


def test_safety_home_root(make_repo: MakeRepo, monkeypatch: pytest.MonkeyPatch) -> None:
    fx = _blocking_repo(make_repo)
    monkeypatch.setenv("HOME", str(fx.path))
    code, text = _run(fx, plan=PLAN_PATH, task="T1")
    assert code == 2
    assert text.startswith("revgate: ")
    assert not _state(fx.path).exists()


def test_dirty_definition(make_repo: MakeRepo) -> None:
    fx = _blocking_repo(make_repo)
    (fx.path / "pkg" / "use.py").write_text(USE_BASE, encoding="utf-8")
    code, text = _run(fx, plan=PLAN_PATH, task="T1")
    assert code == 2
    assert "pkg/use.py" in text
    git(fx.path, "checkout", "--", "pkg/use.py")

    (fx.path / ".coverage").write_text("coverage data", encoding="utf-8")
    code, _ = _run(fx, plan=PLAN_PATH, task="T1")
    assert code != 2
    assert ".coverage" in _meta(fx)["untracked"]  # type: ignore[operator]

    (fx.path / "src").mkdir()
    (fx.path / "src" / "new.py").write_text("x = 1\n", encoding="utf-8")
    code, text = _run(fx, plan=PLAN_PATH, task="T1")
    assert code == 2
    assert "src/new.py" in text
    (fx.path / "src" / "new.py").unlink()

    code, text = _run(fx, head=fx.base, plan=PLAN_PATH, task="T1")
    assert code == 2
    assert "HEAD" in text


# --- inputs ------------------------------------------------------------------------------------

SIG_PLAN_BASE = (
    "# Plan\n\n"
    "### Task T1: add pair\n\n"
    "**Files:**\n"
    "- Create: `pkg/pair.py`\n\n"
    "**Interfaces:**\n"
    "- Produces: `def pair(a, b)`\n"
)
SIG_PLAN_HEAD = SIG_PLAN_BASE.replace("pair(a, b)", "pair(b, a)")
PAIR = "def pair(a: int, b: int) -> int:\n    return a + b\n"


def _comparable(fx: RepoFixture) -> list[tuple[str, str, int, str]]:
    rf = run_file_from_json(_run_file(fx).read_text(encoding="utf-8"))
    return sorted(
        (f.rule, f.file, f.line, f.message)
        for f in rf.findings
        if f.rule != "policy.protected_path" and f.file != PLAN_PATH
    )


def test_plan_read_from_wave_base(make_repo: MakeRepo) -> None:
    base = {"pkg/__init__.py": ""}
    plain = make_repo(
        base,
        {"pkg/pair.py": PAIR},
        plan=SIG_PLAN_BASE,
        plan_path=PLAN_PATH,
        review_toml=REVIEW_TOML,
    )
    edited = make_repo(
        base,
        {"pkg/pair.py": PAIR, PLAN_PATH: SIG_PLAN_HEAD},
        plan=SIG_PLAN_BASE,
        plan_path=PLAN_PATH,
        review_toml=REVIEW_TOML,
    )
    _run(plain, plan=PLAN_PATH, task="T1")
    _run(edited, plan=PLAN_PATH, task="T1")
    assert _comparable(plain) == _comparable(edited)
    assert not any(rule == "plan.signature_mismatch" for rule, *_ in _comparable(edited))
    # The control: the edited plan, committed at the base, does change the findings.
    control = make_repo(
        base,
        {"pkg/pair.py": PAIR},
        plan=SIG_PLAN_HEAD,
        plan_path=PLAN_PATH,
        review_toml=REVIEW_TOML,
    )
    _run(control, plan=PLAN_PATH, task="T1")
    assert any(rule == "plan.signature_mismatch" for rule, *_ in _comparable(control))


def test_report_glob_ambiguous(make_repo: MakeRepo) -> None:
    toml = REVIEW_TOML + '\n[plan]\nreport_glob = "reports/{plan}/*.md"\n'
    fx = _blocking_repo(make_repo, review_toml=toml)
    (fx.path / "reports" / "plan").mkdir(parents=True)
    (fx.path / "reports" / "plan" / "a.md").write_text("# a\n", encoding="utf-8")
    (fx.path / "reports" / "plan" / "b.md").write_text("# b\n", encoding="utf-8")
    code, text = _run(fx, plan=PLAN_PATH, task="T1")
    assert code == 2
    assert "matches 2 files" in text


# --- the run file, the cache, and the outputs -----------------------------------------------


def test_run_file_deterministic(make_repo: MakeRepo) -> None:
    fx = _blocking_repo(make_repo)
    _run(fx, plan=PLAN_PATH, task="T1")
    first = _run_file(fx).read_bytes()
    assert _meta(fx)["run_cache"] == "miss"
    _run(fx, plan=PLAN_PATH, task="T1")
    assert _run_file(fx).read_bytes() == first
    assert _meta(fx)["run_cache"] == "hit"


def test_an_untracked_file_misses_the_run_cache(make_repo: MakeRepo) -> None:
    # An untracked root config file (outside the prod and test globs) can change what the
    # gates check, so a run in a tree that holds one can't share another tree's result.
    fx = _blocking_repo(make_repo)
    _run(fx, plan=PLAN_PATH, task="T1")
    (fx.path / "pytest.ini").write_text("[pytest]\naddopts = -k nothing\n", encoding="utf-8")
    _run(fx, plan=PLAN_PATH, task="T1")
    assert _meta(fx)["run_cache"] == "miss"
    _run(fx, plan=PLAN_PATH, task="T1")
    assert _meta(fx)["run_cache"] == "hit"


def test_a_different_report_misses_the_run_cache(make_repo: MakeRepo, tmp_path: Path) -> None:
    fx = _blocking_repo(make_repo)
    first, second = tmp_path / "a.md", tmp_path / "b.md"
    first.write_text("# Report\n", encoding="utf-8")
    second.write_text("# Report\n\nDisclosed: `kind`\n", encoding="utf-8")
    _run(fx, plan=PLAN_PATH, task="T1", report=first)
    _run(fx, plan=PLAN_PATH, task="T1", report=second)
    assert _meta(fx)["run_cache"] == "miss"
    _run(fx, plan=PLAN_PATH, task="T1", report=second)
    assert _meta(fx)["run_cache"] == "hit"


def test_blocking_finding_end_to_end(make_repo: MakeRepo) -> None:
    fx = _blocking_repo(make_repo)
    code, text = _run(fx, plan=PLAN_PATH, task="T1", role="controller")
    assert code == 1
    assert "pkg/use.py:7 lib.isinstance_shadowing " in text
    assert len(text.splitlines()) <= 20
    rf = run_file_from_json(_run_file(fx, role="controller").read_text(encoding="utf-8"))
    assert rf.exit_code == 1
    assert rf.role == "controller"
    assert rf.source_hash == source_hash()
    assert _meta(fx, role="controller")["timings"]
    state = _state(fx.path)
    rows = [r for r in read_jsonl(findings_log_path(state)) if r.get("type") == "finding"]
    assert len(rows) == len(rf.findings)
    runs = [e for e in ledger_entries(state, "plan") if e.get("kind") == "run"]
    assert len(runs) == 1
    assert runs[0]["task"] == "T1"
    assert runs[0]["head"] == fx.head


def test_no_review_toml_is_all_shadow(make_repo: MakeRepo) -> None:
    fx = _blocking_repo(make_repo, review_toml=None)
    code, text = _run(fx, plan=PLAN_PATH, task="T1")
    assert code == 0
    assert "lib.isinstance_shadowing" not in text
    rf = run_file_from_json(_run_file(fx).read_text(encoding="utf-8"))
    assert any(f.rule == "lib.isinstance_shadowing" for f in rf.findings)


def _gate_toml(command: str, timeout: int = 600) -> str:
    return (
        REVIEW_TOML
        + f"\n[budget]\ngate_timeout_seconds = {timeout}\n"
        + f"\n[gates.task]\ncommands = [{json.dumps(command)}]\n"
    )


def test_failing_gate_stops_before_static_and_is_not_cached(
    make_repo: MakeRepo, tmp_path: Path
) -> None:
    counter = tmp_path / "gate-count"
    code_text = f"open({str(counter)!r}, 'a').write('x'); raise SystemExit(3)"
    command = f"{sys.executable} -c {json.dumps(code_text)}"
    fx = _blocking_repo(make_repo, review_toml=_gate_toml(command))
    code, text = _run(fx, plan=PLAN_PATH, task="T1")
    assert code == 1
    assert "gate.failed" in text
    assert "lib.isinstance_shadowing" not in text
    assert _meta(fx)["rules"] == {}
    code, _ = _run(fx, plan=PLAN_PATH, task="T1")
    assert code == 1
    assert counter.read_text() == "xx"


def test_gate_timeout_exits_2_and_is_not_cached(make_repo: MakeRepo, tmp_path: Path) -> None:
    counter = tmp_path / "gate-count"
    code_text = f"import time; open({str(counter)!r}, 'a').write('x'); time.sleep(30)"
    command = f"{sys.executable} -c {json.dumps(code_text)}"
    fx = _blocking_repo(make_repo, review_toml=_gate_toml(command, timeout=1))
    code, _ = _run(fx, plan=PLAN_PATH, task="T1")
    assert code == 2
    rf = run_file_from_json(_run_file(fx).read_text(encoding="utf-8"))
    assert rf.incomplete and rf.incomplete[0].startswith("gate:")
    code, _ = _run(fx, plan=PLAN_PATH, task="T1")
    assert code == 2
    assert counter.read_text() == "xx"


def test_rules_budget(make_repo: MakeRepo) -> None:
    toml = REVIEW_TOML + "\n[budget]\nrules_seconds = 0\n"
    fx = _blocking_repo(make_repo, review_toml=toml)
    code, _ = _run(fx, plan=PLAN_PATH, task="T1")
    assert code != 2
    rf = run_file_from_json(_run_file(fx).read_text(encoding="utf-8"))
    for rule in all_rules.ALL_RULES[1:]:
        assert f"skipped: budget:{all_rules.rule_name(rule)}" in rf.unverified
    assert rf.provisional


class _Boom:
    ids = frozenset({"x.boom"})
    languages = frozenset({"any"})

    def check(self, ctx: object) -> Iterable[RuleOutput]:
        raise RuntimeError("boom")


def test_incomplete_sets_focus(make_repo: MakeRepo, monkeypatch: pytest.MonkeyPatch) -> None:
    fx = make_repo(BASE_FILES, {"pkg/use.py": USE_BASE + "\n"}, review_toml=REVIEW_TOML)
    monkeypatch.setattr(all_rules, "ALL_RULES", (_Boom(), *all_rules.ALL_RULES))
    code, text = _run(fx)
    assert code == 0
    rf = run_file_from_json(_run_file(fx, task=None).read_text(encoding="utf-8"))
    assert "internal:x.boom" in rf.incomplete
    assert rf.focus
    assert "incomplete:x.boom" in rf.focus_reasons
    assert "incomplete:x.boom" in text
    # An incomplete run is never cached.
    _run(fx)
    assert _meta(fx, task=None)["run_cache"] == "miss"


def test_all_rules_run_cheapest_first() -> None:
    names = [all_rules.rule_name(r) for r in all_rules.ALL_RULES]
    assert len(names) == 7
    assert names[0].startswith("policy.")
    assert isinstance(all_rules.ALL_RULES[0], all_rules.RatchetRule)
