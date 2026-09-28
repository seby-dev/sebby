"""`PlanFidelityRule` and `PlanFilesRule`: did the task do what its brief said."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from conftest import MakeRepo, RepoFixture

from revgate.config import Config, parse_config
from revgate.model import Finding, Grade, RuleOutput, Source, TaskScope, Tier, Unverified
from revgate.plan import task_from_plan
from revgate.rules.registry import StaticRule, load_tier_table
from revgate.spi.cache import BlobCache
from revgate.static.ctx import StaticCtx, build_static_ctx
from revgate.static.plan_rules import PlanFidelityRule, PlanFilesRule
from revgate.verdict.route import route

PLAN_PATH = "docs/plans/plan.md"


def nondefault_cfg() -> Config:
    return parse_config(b"[paths]\n")


def build(repo: RepoFixture, tmp_path: Path, plan_md: str | None, task_id: str = "1") -> StaticCtx:
    plan = task_from_plan(plan_md, PLAN_PATH, task_id) if plan_md is not None else None
    owned = frozenset(plan.owns.all()) if plan is not None else frozenset()
    return build_static_ctx(
        repo.path,
        repo.base,
        repo.head,
        cfg=nondefault_cfg(),
        plan=plan,
        report=None,
        scope=TaskScope(task_id=task_id, owns=owned, runs=frozenset()),
        cache=BlobCache(tmp_path / "cache", "test"),
    )


def run_fidelity(ctx: StaticCtx) -> list[RuleOutput]:
    rule = PlanFidelityRule()
    assert isinstance(rule, StaticRule)
    return list(rule.check(ctx))


def run_files(ctx: StaticCtx) -> list[RuleOutput]:
    rule = PlanFilesRule()
    assert isinstance(rule, StaticRule)
    return list(rule.check(ctx))


def by_rule(out: list[RuleOutput], rule: str) -> list[Finding]:
    return [o for o in out if isinstance(o, Finding) and o.rule == rule]


def plan_md(code: str, interfaces: str = "", files: str = "- Modify: `pkg/cells.py`") -> str:
    iface = f"**Interfaces:**\n- Produces: {interfaces}\n\n" if interfaces else ""
    return (
        "# Plan\n\n### Task 1: Cells\n\n**Files:**\n"
        f"{files}\n\n{iface}- [ ] **Step 1: Implement**\n\n```python\n{code}```\n"
    )


def test_ids() -> None:
    assert PlanFidelityRule().ids == frozenset(
        {
            "plan.symbol_missing",
            "plan.signature_mismatch",
            "plan.test_missing",
            "plan.call_edge_missing",
        }
    )
    assert PlanFilesRule().ids == frozenset({"plan.file_missing", "plan.file_extra"})


# --- call edges -----------------------------------------------------------------------------

CELLS_BASE = "def quarter_cell_steps(x):\n    return x * 4\n\n\ndef bar_cells(x):\n    return [x]\n"
CELLS_HEAD = (
    "def quarter_cell_steps(x):\n    return x * 4\n\n\ndef bar_cells(x):\n    return [x, x]\n"
)
OTHER = (
    "from pkg.cells import quarter_cell_steps\n\n\ndef use(x):\n    return quarter_cell_steps(x)\n"
)
EDGE_CODE = "def bar_cells(x):\n    return quarter_cell_steps(x)\n"


def edge_repo(make_repo: MakeRepo, base_extra: Mapping[str, str] | None = None) -> RepoFixture:
    base = {"pkg/__init__.py": "", "pkg/cells.py": CELLS_BASE, "pkg/other.py": OTHER}
    base.update(base_extra or {})
    return make_repo(base, {"pkg/cells.py": CELLS_HEAD}, plan=plan_md(EDGE_CODE))


def test_call_edge_existing_callers_is_question(make_repo: MakeRepo, tmp_path: Path) -> None:
    ctx = build(edge_repo(make_repo), tmp_path, plan_md(EDGE_CODE))
    found = by_rule(run_fidelity(ctx), "plan.call_edge_missing")
    assert len(found) == 1
    f = found[0]
    assert (f.file, f.symbol, f.grade) == ("pkg/cells.py", "bar_cells", Grade.E2_STRUCTURAL)
    assert "quarter_cell_steps" in f.message
    routed = route(f, load_tier_table(), ctx.cfg, ctx.scope)
    assert routed.tier is Tier.ADVISORY
    assert "implementer" not in routed.audience


def test_call_edge_present_is_silent(make_repo: MakeRepo, tmp_path: Path) -> None:
    head = CELLS_BASE.replace("return [x]", "return [quarter_cell_steps(x)]")
    repo = make_repo(
        {"pkg/__init__.py": "", "pkg/cells.py": CELLS_BASE},
        {"pkg/cells.py": head},
        plan=plan_md(EDGE_CODE),
    )
    assert (
        by_rule(run_fidelity(build(repo, tmp_path, plan_md(EDGE_CODE))), "plan.call_edge_missing")
        == []
    )


def test_call_edge_ambiguous_unverified(make_repo: MakeRepo, tmp_path: Path) -> None:
    dup = {"pkg/dup.py": "def quarter_cell_steps(x):\n    return x\n"}
    out = run_fidelity(build(edge_repo(make_repo, dup), tmp_path, plan_md(EDGE_CODE)))
    assert by_rule(out, "plan.call_edge_missing") == []
    notes = [o for o in out if isinstance(o, Unverified) and o.rule == "plan.call_edge_missing"]
    assert len(notes) == 1
    assert "ambiguous" in notes[0].note and "quarter_cell_steps" in notes[0].note


# --- signatures and symbols -----------------------------------------------------------------


def test_signature_mismatch(make_repo: MakeRepo, tmp_path: Path) -> None:
    md = plan_md("x = 1\n", interfaces="`def f(a, b)`")
    repo = make_repo(
        {"pkg/__init__.py": "", "pkg/cells.py": "X = 0\n"},
        {"pkg/cells.py": "def f(b, a):\n    return a - b\n"},
        plan=md,
    )
    found = by_rule(run_fidelity(build(repo, tmp_path, md)), "plan.signature_mismatch")
    assert len(found) == 1
    f = found[0]
    assert (f.grade, f.source, f.file, f.symbol) == (
        Grade.E1_EXACT,
        Source.DECLARED,
        "pkg/cells.py",
        "f",
    )
    assert "(a, b)" in f.message and "(b, a)" in f.message


def test_signature_match_is_silent(make_repo: MakeRepo, tmp_path: Path) -> None:
    md = plan_md("x = 1\n", interfaces="`def f(a, b=2) -> int`")
    repo = make_repo(
        {"pkg/__init__.py": "", "pkg/cells.py": "X = 0\n"},
        {"pkg/cells.py": "def f(a: int, b: int = 2) -> int:\n    return a - b\n"},
        plan=md,
    )
    assert run_fidelity(build(repo, tmp_path, md)) == []


def test_signature_default_mismatch(make_repo: MakeRepo, tmp_path: Path) -> None:
    md = plan_md("x = 1\n", interfaces="`def f(a, b=2)`")
    repo = make_repo(
        {"pkg/__init__.py": "", "pkg/cells.py": "X = 0\n"},
        {"pkg/cells.py": "def f(a, b=3):\n    return a - b\n"},
        plan=md,
    )
    found = by_rule(run_fidelity(build(repo, tmp_path, md)), "plan.signature_mismatch")
    assert len(found) == 1


def test_symbol_missing(make_repo: MakeRepo, tmp_path: Path) -> None:
    md = plan_md("def g(x):\n    return x\n")
    repo = make_repo(
        {"pkg/__init__.py": "", "pkg/cells.py": "X = 0\n"},
        {"pkg/cells.py": "X = 1\n"},
        plan=md,
    )
    found = by_rule(run_fidelity(build(repo, tmp_path, md)), "plan.symbol_missing")
    assert len(found) == 1
    f = found[0]
    assert (f.grade, f.source, f.file) == (Grade.E1_EXACT, Source.DECLARED, "pkg/cells.py")
    assert "g" in f.message


def test_symbol_present_is_silent(make_repo: MakeRepo, tmp_path: Path) -> None:
    md = plan_md("def g(x):\n    return x\n")
    repo = make_repo(
        {"pkg/__init__.py": "", "pkg/cells.py": "X = 0\n"},
        {"pkg/cells.py": "def g(x):\n    return x\n"},
        plan=md,
    )
    assert by_rule(run_fidelity(build(repo, tmp_path, md)), "plan.symbol_missing") == []


# --- tests ----------------------------------------------------------------------------------

BRIEF_TEST = (
    "def test_relocates_marks():\n"
    "    cells = bar_cells(3)\n"
    "    assert cells == [3, 3]\n"
    "    assert len(cells) == 2\n"
)
TEST_FILES = "- Modify: `pkg/cells.py`\n- Test: `tests/test_cells.py`"


def test_missing_test_fires(make_repo: MakeRepo, tmp_path: Path) -> None:
    md = plan_md(BRIEF_TEST, files=TEST_FILES)
    repo = make_repo(
        {"pkg/__init__.py": "", "pkg/cells.py": CELLS_BASE},
        {"pkg/cells.py": CELLS_HEAD, "tests/test_cells.py": "def test_other():\n    assert 1\n"},
        plan=md,
    )
    found = by_rule(run_fidelity(build(repo, tmp_path, md)), "plan.test_missing")
    assert len(found) == 1
    assert "test_relocates_marks" in found[0].message
    assert found[0].file == "tests/test_cells.py"


def test_renamed_test_with_same_body_is_silent(make_repo: MakeRepo, tmp_path: Path) -> None:
    md = plan_md(BRIEF_TEST, files=TEST_FILES)
    renamed = BRIEF_TEST.replace("test_relocates_marks", "test_bar_cells_doubles")
    repo = make_repo(
        {"pkg/__init__.py": "", "pkg/cells.py": CELLS_BASE},
        {"pkg/cells.py": CELLS_HEAD, "tests/test_cells.py": renamed},
        plan=md,
    )
    assert by_rule(run_fidelity(build(repo, tmp_path, md)), "plan.test_missing") == []


def test_present_test_is_silent(make_repo: MakeRepo, tmp_path: Path) -> None:
    md = plan_md(BRIEF_TEST, files=TEST_FILES)
    repo = make_repo(
        {"pkg/__init__.py": "", "pkg/cells.py": CELLS_BASE},
        {"pkg/cells.py": CELLS_HEAD, "tests/test_cells.py": BRIEF_TEST},
        plan=md,
    )
    assert by_rule(run_fidelity(build(repo, tmp_path, md)), "plan.test_missing") == []


# --- files ----------------------------------------------------------------------------------

FILES_PLAN = """# Plan

### Task 1: Files

**Files:**
- Create: `pkg/new.py`
- Modify: `pkg/a.py`
- Modify: `web/src/view.ts`
- Modify: `pkg/sub/`
- Test: `tests/test_a.py`

- [ ] **Step 1**
"""


def test_plan_files(make_repo: MakeRepo, tmp_path: Path) -> None:
    repo = make_repo(
        {
            "pkg/__init__.py": "",
            "pkg/a.py": "A = 1\n",
            "tests/test_a.py": "def test_a():\n    assert 1\n",
            "web/src/view.ts": "export const v = 1;\n",
            "uv.lock": "x\n",
        },
        {
            "pkg/new.py": "N = 1\n",
            "pkg/z.py": "Z = 1\n",
            "pkg/sub/deep.py": "D = 1\n",
            "web/src/view.ts": None,
            "web/src/view.tsx": "export const v = 2;\n",
            "uv.lock": "y\n",
            "web/package-lock.json": "{}\n",
            ".superpowers/sdd/plan/task-1-report.md": "# Report\n",
        },
        plan=FILES_PLAN,
    )
    out = run_files(build(repo, tmp_path, FILES_PLAN))
    missing = by_rule(out, "plan.file_missing")
    extra = by_rule(out, "plan.file_extra")
    assert [f.file for f in missing] == ["pkg/a.py"]
    assert [f.file for f in extra] == ["pkg/z.py"]
    f = missing[0]
    assert (f.grade, f.source) == (Grade.E1_EXACT, Source.DECLARED)
    routed = route(f, load_tier_table(), nondefault_cfg(), TaskScope("1", frozenset(), frozenset()))
    assert routed.tier is Tier.ADVISORY


def test_plan_files_signature_change_exempts_test(make_repo: MakeRepo, tmp_path: Path) -> None:
    md = plan_md("x = 1\n")
    test_src = "from pkg.cells import bar_cells\n\n\ndef test_b():\n    assert bar_cells(1, 2)\n"
    repo = make_repo(
        {"pkg/__init__.py": "", "pkg/cells.py": CELLS_BASE, "tests/test_b.py": "B = 1\n"},
        {
            "pkg/cells.py": CELLS_BASE.replace("bar_cells(x)", "bar_cells(x, y)"),
            "tests/test_b.py": test_src,
        },
        plan=md,
    )
    assert run_files(build(repo, tmp_path, md)) == []


def test_planless_context_yields_nothing(make_repo: MakeRepo, tmp_path: Path) -> None:
    repo = edge_repo(make_repo)
    ctx = build(repo, tmp_path, None)
    assert run_fidelity(ctx) == []
    assert run_files(ctx) == []
