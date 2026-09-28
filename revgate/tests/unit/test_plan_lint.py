"""`plan-lint` and `plan brief`: one test per error code, each on an otherwise valid plan."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from conftest import MakeRepo

from revgate.config import parse_config
from revgate.plan_lint import LintReport, lint_plan, plan_brief, render_lint

FENCE = "```"
PLAN_PATH = "docs/plans/plan.md"


@dataclass
class T:
    id: str
    risk: str | None = "low"
    deps: list[str] = field(default_factory=list)
    create: list[str] = field(default_factory=list)
    modify: list[str] = field(default_factory=list)
    test: list[str] = field(default_factory=list)
    est: int | None = 30
    extra: list[str] = field(default_factory=list)  # extra YAML lines under the task
    prose: list[str] | None = None  # the Files list, as (kind, path) bullets; None mirrors owns
    section: bool = True


Wave = tuple[str, Sequence[tuple[str, Sequence[T]]]]


def _flow(items: Sequence[str]) -> str:
    return "[" + ", ".join(items) + "]"


def _walls(waves: Sequence[Wave], cap: int = 5) -> dict[str, int]:
    """An independent hand computation of each wave's wall-clock, for the helper's table."""
    out: dict[str, int] = {}
    for wid, subs in waves:
        tasks = [t for _sid, ts in subs for t in ts]
        est = {t.id: t.est or 0 for t in tasks}
        deps = {t.id: [d for d in t.deps if d in est] for t in tasks}
        memo: dict[str, int] = {}

        def path(
            tid: str,
            est: dict[str, int] = est,
            deps: dict[str, list[str]] = deps,
            memo: dict[str, int] = memo,
        ) -> int:
            if tid not in memo:
                memo[tid] = est[tid] + max((path(d) for d in deps[tid]), default=0)
            return memo[tid]

        critical = max((path(t.id) for t in tasks), default=0)
        review = 20 if any(t.risk == "high" for t in tasks) else 0
        out[wid] = max(critical, math.ceil(sum(est.values()) / cap)) + 10 + review
    return out


def build_plan(
    waves: Sequence[Wave],
    *,
    forbidden: Sequence[str] = (),
    table: Mapping[str, int] | None = None,
    block: str | None = None,
) -> str:
    lines = [f"{FENCE}yaml plan-waves"]
    if forbidden:
        lines.append(f"forbidden: {_flow(forbidden)}")
    lines.append("waves:")
    for wid, subs in waves:
        lines += [f"  - id: {wid}", "    subwaves:"]
        for sid, tasks in subs:
            lines += [f"      - id: {sid}", "        tasks:"]
            for t in tasks:
                lines.append(f"          - id: {t.id}")
                if t.risk is not None:
                    lines.append(f"            risk: {t.risk}")
                lines.append(f"            depends_on: {_flow(t.deps)}")
                if t.est is not None:
                    lines.append(f"            estimate_min: {t.est}")
                owns = f"create: {_flow(t.create)}, modify: {_flow(t.modify)}"
                lines.append(f"            owns: {{{owns}, test: {_flow(t.test)}}}")
                lines += [f"            {x}" for x in t.extra]
    lines.append(FENCE)
    walls = dict(_walls(waves)) if table is None else dict(table)
    out = ["# Demo plan", "", "## Estimated duration", ""]
    out += ["| Wave | Tasks | Wave wall-clock |", "|---|---|---|"]
    out += [f"| {wid} | x | {minutes} min |" for wid, minutes in walls.items()]
    out += ["", "## Plan block", "", block if block is not None else "\n".join(lines), ""]
    for _wid, subs in waves:
        for _sid, tasks in subs:
            for t in tasks:
                if not t.section:
                    continue
                out += [f"### Task {t.id}: do {t.id}", "", "**Files:**"]
                if t.prose is not None:
                    out += t.prose
                else:
                    out += [f"- Create: `{p}`" for p in t.create]
                    out += [f"- Modify: `{p}`" for p in t.modify]
                    out += [f"- Test: `{p}`" for p in t.test if p not in t.create]
                out += ["", f"- [ ] **Step 1:** write `test_{t.id.lower()}_works`.", ""]
    return "\n".join(out) + "\n"


def one(tid: str, **kw: object) -> T:
    """A task that owns its own module and test."""
    n = tid.lower()
    base: dict[str, object] = {
        "create": [f"src/{n}.py", f"tests/test_{n}.py"],
        "test": [f"tests/test_{n}.py"],
    }
    base.update(kw)
    return T(tid, **base)  # type: ignore[arg-type]


def codes(report: LintReport, level: str = "error") -> set[str]:
    return {m.code for m in report.messages if m.level == level}


def lint(md: str, **kw: object) -> LintReport:
    return lint_plan(md, plan_path=PLAN_PATH, **kw)  # type: ignore[arg-type]


def valid_waves() -> list[Wave]:
    return [
        (
            "W1",
            [("W1a", [one("T1", est=60), one("T3", est=45)]), ("W1b", [one("T2", deps=["T1"])])],
        ),
        ("W2", [("W2a", [one("T4", deps=["T2"], modify=["src/t1.py"])])]),
    ]


# --- the valid plan and its estimates --------------------------------------------------------


def test_valid_plan_has_no_errors_and_hand_computed_estimates() -> None:
    report = lint(build_plan(valid_waves()))
    assert report.errors() == ()
    w1 = report.estimates[0]
    assert (w1.wave, w1.tasks_sum, w1.critical_path, w1.throughput) == ("W1", 135, 90, 27)
    assert (w1.gate, w1.review, w1.wall) == (10, 0, 90 + 10)
    assert report.estimates[1].wall == 30 + 10
    assert report.total_min == 100 + 40


def test_valid_plan_against_a_base_commit(make_repo: MakeRepo) -> None:
    fx = make_repo({"src/app.py": "x = 1\n"}, {})
    waves: list[Wave] = [("W1", [("W1a", [one("T1", modify=["src/app.py"])])])]
    report = lint(build_plan(waves), repo=fx.path, base=fx.base)
    assert report.errors() == ()


def test_risk_high_adds_the_review_minutes() -> None:
    report = lint(build_plan([("W1", [("W1a", [one("T1", risk="high")])])]))
    assert report.errors() == ()
    assert report.estimates[0].review == 20
    assert report.estimates[0].wall == 30 + 10 + 20


def test_render_lint_prints_errors_warnings_and_a_pasteable_table() -> None:
    waves = valid_waves()
    waves[0][1][1][1][0].deps = ["T1", "T9"]  # dep-order error
    text = render_lint(lint(build_plan(waves)))
    err = text.index("error")
    table = text.index("| Wave |")
    assert err < table
    assert "Wave wall-clock" in text
    assert "| W1 | W1a: T1 60, T3 45; W1b: T2 30 | 135 | 90 | 27 | 10 | — | 100 min |" in text
    assert "| W2 | T4 30 |" in text
    assert (
        "| **Plan total** | 4 tasks, 165 task-minutes | | | | | | **140 min (2 h 20 min)** |"
        in text
    )


# --- errors ----------------------------------------------------------------------------------


def test_parse_error_on_a_malformed_block() -> None:
    bad = f"{FENCE}yaml plan-waves\nwaves:\n  - id: W1\n    subwaves: [unclosed\n{FENCE}"
    report = lint(build_plan([], block=bad, table={}))
    assert codes(report) == {"parse"}


def test_no_block() -> None:
    report = lint("# A plan\n\n### Task T1: x\n\n- Create: `a.py`\n")
    assert codes(report) == {"no-block"}


def test_duplicate_task_id() -> None:
    waves: list[Wave] = [("W1", [("W1a", [one("T1")]), ("W1b", [one("T1", create=["src/z.py"])])])]
    assert "dup-id" in codes(lint(build_plan(waves)))


def test_risk_value_must_be_low_or_high() -> None:
    waves: list[Wave] = [("W1", [("W1a", [one("T1", risk="medium")])])]
    assert codes(lint(build_plan(waves))) == {"risk"}


def test_missing_estimate_is_a_missing_field() -> None:
    waves: list[Wave] = [("W1", [("W1a", [one("T1", est=None)])])]
    assert codes(lint(build_plan(waves, table={"W1": 10}))) == {"missing-field"}


def test_missing_risk_is_a_missing_field() -> None:
    waves: list[Wave] = [("W1", [("W1a", [one("T1", risk=None)])])]
    assert codes(lint(build_plan(waves))) == {"missing-field"}


def test_dependency_on_a_later_task() -> None:
    waves: list[Wave] = [("W1", [("W1a", [one("T1", deps=["T2"])]), ("W1b", [one("T2")])])]
    report = lint(build_plan(waves, table={"W1": 70}))
    assert codes(report) == {"dep-order"}


def test_dependency_in_the_same_subwave_is_out_of_order() -> None:
    waves: list[Wave] = [("W1", [("W1a", [one("T1"), one("T2", deps=["T1"])])])]
    assert codes(lint(build_plan(waves, table={"W1": 70}))) == {"dep-order"}


def test_same_wave_dependency_on_a_risk_high_task() -> None:
    waves: list[Wave] = [
        ("W1", [("W1a", [one("T1", risk="high")]), ("W1b", [one("T2", deps=["T1"])])])
    ]
    report = lint(build_plan(waves))
    assert codes(report) == {"risky-dep-same-wave"}
    assert report.errors()[0].task == "T2"


def test_same_wave_dependency_on_a_risk_path_owner() -> None:
    cfg = parse_config(b'[risk.paths]\n"src/pitch*.py" = "wave"\n')
    waves: list[Wave] = [
        ("W1", [("W1a", [one("T1", create=["src/pitch.py", "tests/test_t1.py"])])]),
        ("W2", [("W2a", [one("T3")]), ("W2b", [one("T2", deps=["T3"])])]),
    ]
    assert codes(lint(build_plan(waves), cfg=cfg)) == set()
    waves[1][1][1][1][0].deps = ["T1", "T3"]  # T1 is in W1: fine
    assert codes(lint(build_plan(waves), cfg=cfg)) == set()
    same: list[Wave] = [
        (
            "W1",
            [
                ("W1a", [one("T1", create=["src/pitch.py", "tests/test_t1.py"])]),
                ("W1b", [one("T2", deps=["T1"])]),
            ],
        )
    ]
    assert codes(lint(build_plan(same), cfg=cfg)) == {"risky-dep-same-wave"}


def test_two_tasks_in_one_subwave_share_a_file() -> None:
    waves: list[Wave] = [
        ("W1", [("W1a", [one("T1", modify=["src/app.py"]), one("T2", modify=["src/app.py"])])])
    ]
    assert codes(lint(build_plan(waves))) == {"subwave-share"}


def test_two_tasks_in_one_wave_share_a_file_without_a_dependency_path() -> None:
    waves: list[Wave] = [
        (
            "W1",
            [
                ("W1a", [one("T1", modify=["src/app.py"]), one("T2")]),
                ("W1b", [one("T3", deps=["T2"], modify=["src/app.py"])]),
            ],
        )
    ]
    assert codes(lint(build_plan(waves))) == {"wave-share"}


def test_a_transitive_dependency_path_allows_a_shared_file() -> None:
    waves: list[Wave] = [
        (
            "W1",
            [
                ("W1a", [one("T1", modify=["src/app.py"])]),
                ("W1b", [one("T2", deps=["T1"])]),
                ("W1c", [one("T3", deps=["T2"], modify=["src/app.py"])]),
            ],
        )
    ]
    assert codes(lint(build_plan(waves))) == set()


def test_forbidden_plan_level_task_level_and_the_plan_itself() -> None:
    plan_level: list[Wave] = [("W1", [("W1a", [one("T1", modify=["tests/frozen.py"])])])]
    report = lint(build_plan(plan_level, forbidden=["tests/frozen.py"]))
    assert codes(report) == {"forbidden"}
    task_level: list[Wave] = [
        ("W1", [("W1a", [one("T1", modify=["src/a.py"], extra=["forbidden: [src/a.py]"])])])
    ]
    assert codes(lint(build_plan(task_level))) == {"forbidden"}
    directory: list[Wave] = [("W1", [("W1a", [one("T1", modify=["client/app.ts"])])])]
    assert codes(lint(build_plan(directory, forbidden=["client/"]))) == {"forbidden"}
    itself: list[Wave] = [("W1", [("W1a", [one("T1", modify=[PLAN_PATH])])])]
    assert codes(lint(build_plan(itself))) == {"forbidden"}


def test_modify_path_missing_at_base(make_repo: MakeRepo) -> None:
    fx = make_repo({"src/app.py": "x = 1\n"}, {})
    waves: list[Wave] = [
        ("W1", [("W1a", [one("T1", modify=["src/nope.py"])])]),
    ]
    assert codes(lint(build_plan(waves), repo=fx.path, base=fx.base)) == {"modify-missing"}
    # A path a dependency creates is fine.
    ok: list[Wave] = [
        ("W1", [("W1a", [one("T1", create=["src/new.py", "tests/test_t1.py"])])]),
        ("W2", [("W2a", [one("T2", deps=["T1"], modify=["src/new.py"])])]),
    ]
    assert codes(lint(build_plan(ok), repo=fx.path, base=fx.base)) == set()


def test_create_path_exists_at_base(make_repo: MakeRepo) -> None:
    fx = make_repo({"src/t1.py": "x = 1\n"}, {})
    waves: list[Wave] = [("W1", [("W1a", [one("T1")])])]
    assert codes(lint(build_plan(waves), repo=fx.path, base=fx.base)) == {"create-exists"}


def test_a_task_with_no_tests() -> None:
    waves: list[Wave] = [("W1", [("W1a", [one("T1", create=["src/t1.py"], test=[])])])]
    assert codes(lint(build_plan(waves))) == {"no-tests"}
    reason: list[Wave] = [
        (
            "W1",
            [("W1a", [one("T1", create=["src/t1.py"], test=[], extra=['tests: "none: docs"'])])],
        )
    ]
    assert codes(lint(build_plan(reason))) == set()
    no_reason: list[Wave] = [
        ("W1", [("W1a", [one("T1", create=["src/t1.py"], test=[], extra=["tests: none"])])])
    ]
    assert codes(lint(build_plan(no_reason))) == {"no-tests"}


def test_context_anchors_resolve_at_base(make_repo: MakeRepo) -> None:
    fx = make_repo(
        {
            "src/app.py": "class Box:\n    def open(self) -> None:\n        pass\n\n\nLIMIT = 3\n",
            "docs/spec.md": "# Spec\n\n## The `rhythm` rules\n\ntext\n",
            "web/grid.ts": "export function paint(): void {}\n",
        },
        {},
    )
    good = [
        "src/app.py::Box.open",
        "src/app.py::LIMIT",
        '"docs/spec.md#The `rhythm` rules"',
        "docs/spec.md#the-rhythm-rules",
        "web/grid.ts::paint",
        "src/app.py",
    ]
    waves: list[Wave] = [("W1", [("W1a", [one("T1", extra=[f"context: {_flow(good)}"])])])]
    assert codes(lint(build_plan(waves), repo=fx.path, base=fx.base)) == set()
    for bad in ("src/app.py::Box.shut", "docs/spec.md#Melody", "src/gone.py", "web/grid.ts::x"):
        waves = [("W1", [("W1a", [one("T1", extra=[f'context: ["{bad}"]'])])])]
        report = lint(build_plan(waves), repo=fx.path, base=fx.base)
        assert codes(report) == {"context"}, bad


def test_risk_high_task_needs_a_reference_context_entry() -> None:
    cfg = parse_config(b'[plan]\nrisk_high_context_prefix = "docs/reference/"\n')
    waves: list[Wave] = [("W1", [("W1a", [one("T1", risk="high")])])]
    assert codes(lint(build_plan(waves), cfg=cfg)) == {"risk-context"}
    ok: list[Wave] = [
        (
            "W1",
            [("W1a", [one("T1", risk="high", extra=['context: ["docs/reference/a.md#B"]'])])],
        )
    ]
    assert codes(lint(build_plan(ok), cfg=cfg)) == set()


def test_prose_files_list_must_match_the_block() -> None:
    extra_in_prose = one(
        "T1",
        prose=["- Create: `src/t1.py`", "- Test: `tests/test_t1.py`", "- Modify: `src/other.py`"],
    )
    assert codes(lint(build_plan([("W1", [("W1a", [extra_in_prose])])]))) == {"prose-mismatch"}
    missing_in_prose = one("T1", prose=["- Create: `src/t1.py`"])
    assert codes(lint(build_plan([("W1", [("W1a", [missing_in_prose])])]))) == {"prose-mismatch"}
    no_section = one("T1", section=False)
    assert codes(lint(build_plan([("W1", [("W1a", [no_section])])]))) == {"prose-mismatch"}


def test_duration_table_more_than_ten_percent_off() -> None:
    waves: list[Wave] = [("W1", [("W1a", [one("T1")])])]  # computed 40
    assert codes(lint(build_plan(waves, table={"W1": 44}))) == set()
    assert codes(lint(build_plan(waves, table={"W1": 45}))) == {"duration-table"}
    assert codes(lint(build_plan(waves, table={}))) == {"duration-table"}


def test_duration_table_reads_hours_and_minutes() -> None:
    waves: list[Wave] = [("W1", [("W1a", [one("T1", est=110)])])]  # computed 120
    md = build_plan(waves).replace("| 120 min |", "| 2 h 0 min |")
    assert codes(lint(md)) == set()
    assert codes(lint(build_plan(waves).replace("| 120 min |", "| 120 |"))) == set()


# --- warnings --------------------------------------------------------------------------------


def test_warnings() -> None:
    waves: list[Wave] = [
        ("W1", [("W1a", [one(f"T{i}") for i in range(1, 7)])]),
        (
            "W2",
            [
                (
                    "W2a",
                    [
                        one(
                            "T7",
                            risk="high",
                            create=["src/t7.py"],
                            test=[],
                            extra=['tests: "none: data"', 'context: ["src/t1.py:10-20"]'],
                        )
                    ],
                )
            ],
        ),
    ]
    report = lint(build_plan(waves))
    assert codes(report) == set()
    assert codes(report, "warning") == {
        "one-task-wave",
        "wide-subwave",
        "context-lines",
        "risk-high-no-test",
    }


# --- plan brief ------------------------------------------------------------------------------


def test_plan_brief_prints_the_block_entry_and_section(make_repo: MakeRepo) -> None:
    fx = make_repo({"src/app.py": "def run() -> None:\n    pass\n"}, {})
    waves: list[Wave] = [
        (
            "W1",
            [("W1a", [one("T1", extra=['context: ["src/app.py::run", "src/app.py::gone"]'])])],
        )
    ]
    md = build_plan(waves)
    brief = plan_brief(md, "T1", repo=fx.path, head=fx.head)
    assert "id: T1" in brief
    assert "risk: low" in brief
    assert "tests/test_t1.py" in brief
    assert "### Task T1: do T1" in brief
    assert any(line.rstrip().endswith("src/app.py::run") for line in brief.splitlines())
    assert "src/app.py::gone (unresolved)" in brief
    assert "src/app.py::run (unresolved)" not in brief
