"""The plan model: the `plan-waves` block plus the prose task sections."""

from __future__ import annotations

import pytest
from conftest import MakeRepo

from revgate.plan import (
    Owns,
    PlanEdge,
    load_plan_task,
    parse_plan_block,
    parse_task_sections,
    plan_slug,
    plan_task_hash,
    task_from_plan,
)
from revgate.plan_yaml import PlanYamlError

FENCE = "```"

PLAN = f"""\
# Demo plan

**Goal:** Move the items to the end of each row.

{FENCE}yaml plan-waves
forbidden: [tests/frozen.py]
waves:
  - id: W1
    subwaves:
      - id: W1a
        tasks:
          - id: T1
            risk: low
            depends_on: []
            estimate_min: 30
            owns:
              create: [src/pkg/marks.py, tests/test_marks.py]
              modify: [src/pkg/sheet.py]
              test: [tests/test_marks.py]
            context: ["docs/spec.md#Marks"]
          - id: T2
            risk: high
            depends_on: []
            kind: refactor
            owns: {{modify: [src/pkg/pdf.py], test: [tests/test_pdf.py]}}
            runs: [tests/test_api.py]
            forbidden: [src/pkg/schema.py]
  - id: W2
    subwaves:
      - id: W2a
        tasks:
          - id: T3
            risk: low
            depends_on: [T1]
            owns: {{modify: [src/pkg/api.py, src/pkg/sheet.py], test: [tests/test_api.py]}}
            tests: "none: covered by T1's tests"
{FENCE}

## Tasks

### Task T1: Marks module

**Files:**
- Create: `src/pkg/marks.py`
- Modify: `src/pkg/sheet.py:10-40`
- Test: `tests/test_marks.py`

**Interfaces:**
- Produces: `def cell_steps(cell: Cell, index: int) -> int` and
  `marks.move_item(x: Row, *, strict: bool = False) -> Row`.
- Consumes `callers("pkg.a.f")` from the index.

**Context:** read `sheet.py`.

- [ ] **Step 1: Test.**

{FENCE}python
def test_moves_items():
    assert move_item(1) == 1
{FENCE}

Replace `build_rows`' body:

{FENCE}python
cells = move_item(x)
return cells
{FENCE}

### Task T2: PDF

**Files:**
- Modify: `src/pkg/pdf.py`
- Test: `tests/test_pdf.py`

{FENCE}python
def render(bar):
    return draw_marks(bar)
{FENCE}

{FENCE}ts
it("draws the marks", () => {{}});
test('keeps the grid', () => {{}});
{FENCE}

### Task T3: API

**Files:**
- Modify: `src/pkg/api.py`

{FENCE}python
def handle(req):
    return cell_steps(req, 0) + finalize(req)
{FENCE}

Then in the prose, with no owner named:

{FENCE}python
x = orphan_call()
{FENCE}

## Follow-up work
"""

PROSE_ONLY = f"""\
# Old plan

### Task 1: First

**Files:**
- Create: `src/a.py`
- Modify: `src/b.py`
- Test: `tests/test_a.py`

### Task 2: Second

**Files:**
- Modify: `src/c.py`

{FENCE}python
def c():
    return a_helper()
{FENCE}
"""


def test_parse_plan_block_and_ordered_tasks() -> None:
    block = parse_plan_block(PLAN)
    assert block is not None
    assert block.forbidden == ("tests/frozen.py",)
    ordered = block.ordered_tasks()
    assert [(w, s, t.id) for w, s, t in ordered] == [(0, 0, "T1"), (0, 0, "T2"), (1, 0, "T3")]
    t1, t2, t3 = (t for _w, _s, t in ordered)
    assert t1.owns == Owns(
        create=("src/pkg/marks.py", "tests/test_marks.py"),
        modify=("src/pkg/sheet.py",),
        test=("tests/test_marks.py",),
    )
    assert t1.owns.all() == frozenset(
        {"src/pkg/marks.py", "tests/test_marks.py", "src/pkg/sheet.py"}
    )
    assert t1.estimate_min == 30 and t1.kind == "feat" and t1.context == ("docs/spec.md#Marks",)
    assert t2.kind == "refactor" and t2.risk == "high" and t2.runs == ("tests/test_api.py",)
    assert t3.depends_on == ("T1",) and t3.tests_none == "covered by T1's tests"
    assert t1.tests_none is None


def test_parse_plan_block_absent_and_invalid() -> None:
    assert parse_plan_block(PROSE_ONLY) is None
    bad = f"# P\n\n{FENCE}yaml plan-waves\nwaves: [\n{FENCE}\n"
    with pytest.raises(PlanYamlError) as err:
        parse_plan_block(bad)
    assert err.value.line == 4  # the plan file's line, not the block's
    wrong_shape = f"{FENCE}yaml plan-waves\nwaves:\n  - id: W1\n    subwaves: 3\n{FENCE}\n"
    with pytest.raises(PlanYamlError):
        parse_plan_block(wrong_shape)


def test_block_inside_a_longer_fence_is_not_the_plan_block() -> None:
    md = f"````markdown\n{FENCE}yaml plan-waves\nwaves: []\n{FENCE}\n````\n"
    assert parse_plan_block(md) is None


def test_parse_task_sections() -> None:
    sections = parse_task_sections(PLAN)
    assert list(sections) == ["T1", "T2", "T3"]
    t1 = sections["T1"]
    assert t1.title == "Marks module"
    assert t1.files_create == ("src/pkg/marks.py",)
    assert t1.files_modify == ("src/pkg/sheet.py",)
    assert t1.files_test == ("tests/test_marks.py",)
    assert "cell_steps" in t1.interfaces_text
    assert "**Context:**" not in t1.interfaces_text
    assert [b.lang for b in t1.code_blocks] == ["python", "python"]
    assert t1.code_blocks[1].ref == "plan:T1:code-block:2"
    assert "Replace `build_rows`' body:" in t1.code_blocks[1].preceding_prose
    # T3's section stops at the next `## ` heading.
    assert "Follow-up" not in sections["T3"].section_text


def test_task_from_plan_with_block() -> None:
    task = task_from_plan(PLAN, "docs/plans/demo.md", "T1")
    assert task is not None
    assert task.block_present
    assert task.owns.modify == ("src/pkg/sheet.py",)
    assert task.forbidden == ("tests/frozen.py", "docs/plans/demo.md")
    assert task.goal == "Move the items to the end of each row."
    assert task.risk == "low" and task.estimate_min == 30
    assert PlanEdge("build_rows", "move_item", "plan:T1:code-block:2") in task.call_edges
    assert PlanEdge("test_moves_items", "move_item", "plan:T1:code-block:1") in task.call_edges
    assert "test_moves_items" in task.tests
    sigs = {s.name: s for s in task.signatures}
    steps = sigs["cell_steps"]
    assert steps.params == ("cell", "index")
    assert steps.defaults == (None, None)
    assert steps.returns == "int"
    move_item = sigs["marks.move_item"]
    assert move_item.params == ("x", "strict")
    assert move_item.defaults == (None, "False")
    assert "callers" not in sigs  # a call in prose isn't a signature
    # Ownership across the plan.
    assert task.owners["src/pkg/sheet.py"] == ("T1", "T3")
    # T3's own files are later; sheet.py is T1's own file too, so it isn't "later".
    assert task.later_owners == {"src/pkg/api.py": "T3", "tests/test_api.py": "T3"}
    # T3's code calls finalize and cell_steps; T1's code calls neither.
    assert task.later_edges["finalize"] == "T3"
    assert "move_item" not in task.later_edges
    # T2 is concurrent (same sub-wave), not later: its callee isn't a later edge.
    assert "draw_marks" not in task.later_edges


def test_task_from_plan_other_fields() -> None:
    t2 = task_from_plan(PLAN, "docs/plans/demo.md", "T2")
    assert t2 is not None
    assert t2.kind == "refactor"
    assert t2.forbidden == ("tests/frozen.py", "src/pkg/schema.py", "docs/plans/demo.md")
    assert t2.tests == ("draws the marks", "keeps the grid")
    assert t2.depends_on == ()
    t3 = task_from_plan(PLAN, "docs/plans/demo.md", "T3")
    assert t3 is not None
    # A bare snippet with no backticked owner before it: the edge is skipped and noted.
    assert all(e.callee != "orphan_call" for e in t3.call_edges)
    assert any(n.startswith("plan-snippet-owner:plan:T3:code-block:2") for n in t3.unverified)
    assert task_from_plan(PLAN, "docs/plans/demo.md", "T9") is None


def test_prose_only_plan_falls_back() -> None:
    task = task_from_plan(PROSE_ONLY, "docs/plans/old.md", "1")
    assert task is not None
    assert task.task_id == "1"
    assert not task.block_present
    assert task.owns == Owns(create=("src/a.py",), modify=("src/b.py",), test=("tests/test_a.py",))
    assert "plan-block-missing" in task.unverified
    assert task.risk is None and task.depends_on == () and task.runs == ()
    assert task.forbidden == ("docs/plans/old.md",)
    assert task.kind == "feat"
    assert task.later_owners == {"src/c.py": "2"}
    assert task.later_edges == {"a_helper": "2"}


def test_plan_slug_and_hash() -> None:
    assert plan_slug("docs/superpowers/plans/2026-09-28-demo.md") == "2026-09-28-demo"
    a = task_from_plan(PLAN, "p.md", "T1")
    b = task_from_plan(PLAN, "p.md", "T1")
    c = task_from_plan(PLAN.replace("estimate_min: 30", "estimate_min: 31"), "p.md", "T1")
    assert a is not None and b is not None and c is not None
    assert plan_task_hash(a) == plan_task_hash(b)
    assert plan_task_hash(a) != plan_task_hash(c)
    assert len(plan_task_hash(a)) == 64


def test_load_plan_task_reads_the_wave_base(make_repo: MakeRepo) -> None:
    path = "docs/plans/demo.md"
    changed = PLAN.replace("estimate_min: 30", "estimate_min: 99")
    repo = make_repo({}, {path: changed}, plan=PLAN, plan_path=path)
    (repo.path / path).write_text(PLAN.replace("estimate_min: 30", "estimate_min: 7"))
    at_base = load_plan_task(repo.path, repo.base, path, "T1")
    assert at_base is not None and at_base.estimate_min == 30
    at_head = load_plan_task(repo.path, repo.head, path, "T1")
    assert at_head is not None and at_head.estimate_min == 99
    assert load_plan_task(repo.path, repo.base, "docs/plans/missing.md", "T1") is None


def test_owns_all_is_a_frozenset() -> None:
    assert Owns((), (), ()).all() == frozenset()


INDENTED_BODY_PLAN = f"""\
# Old plan

### Task 1: Wire the helper

**Files:**
- Modify: `src/pkg/sheet.py`

Replace `layout_cells`' body (everything after its docstring) with:

{FENCE}python
    grid: list[Cell] = []
    for n in bar.notes:
        grid.extend([FILLER] * (cell_steps(n) - 1))
    relocate_marks(grid)
    return grid
{FENCE}
"""


def test_an_indented_body_snippet_gives_its_owners_edges() -> None:
    # A body fragment indented two levels (a method's or a nested block's) still parses:
    # the snippet is dedented before it's wrapped, so the owner named in the prose gets
    # its edges instead of a `plan-snippet-unparsed` note (bench case C2).
    task = task_from_plan(INDENTED_BODY_PLAN, "docs/plans/old.md", "1")
    assert task is not None
    callees = {(e.caller, e.callee) for e in task.call_edges}
    assert ("layout_cells", "relocate_marks") in callees
    assert ("layout_cells", "cell_steps") in callees
    assert not any(n.startswith("plan-snippet-unparsed") for n in task.unverified)


LEVEL_TWO_PLAN = f"""\
# Older plan

## Global constraints

Keep it small.

## Task 1: The store

**Files:**
- Create: `src/pkg/store.py`

### Steps

{FENCE}python
def save(page):
    write_page(page)
{FENCE}

## Task 2: The route

**Files:**
- Modify: `src/pkg/app.py`
"""


def test_level_two_task_headings_are_task_sections() -> None:
    # Some historical plans head each task `## Task N:` with `###` subsections inside it
    # (bench case C29); the section runs to the next heading of its own level.
    sections = parse_task_sections(LEVEL_TWO_PLAN)
    assert list(sections) == ["1", "2"]
    assert sections["1"].files_create == ("src/pkg/store.py",)
    assert [b.ref for b in sections["1"].code_blocks] == ["plan:1:code-block:1"]
    task = task_from_plan(LEVEL_TWO_PLAN, "docs/plans/older.md", "2")
    assert task is not None
    assert task.owns.modify == ("src/pkg/app.py",)
