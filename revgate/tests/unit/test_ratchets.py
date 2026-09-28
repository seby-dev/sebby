"""G11 policy ratchets: fork-to-tip checks on everything a gamer would loosen."""

from __future__ import annotations

from collections.abc import Mapping

from conftest import MakeRepo, RepoFixture

from revgate.config import parse_config
from revgate.model import Grade, Source
from revgate.static.ratchets import (
    PROTECTED_GLOBS,
    RatchetResult,
    count_assertions_py,
    count_assertions_ts,
    ratchet_findings,
    raw_signal_counts,
)

OWNED = frozenset({"tests/test_a.py", "web/src/a.test.ts"})
UNOWNED: frozenset[str] = frozenset()

BASE_TEST = """\
from pkg.a import f


def test_one():
    assert f(1) == 1
    assert f(0) == 0


def test_two():
    assert f(3) == 3
"""


def _run(
    r: RepoFixture,
    owns: frozenset[str] = UNOWNED,
    plan_text: str = "",
    plan_path: str | None = None,
) -> RatchetResult:
    return ratchet_findings(
        r.path,
        r.base,
        r.head,
        cfg=parse_config(None),
        plan_owns=owns,
        plan_text=plan_text,
        plan_path=plan_path,
    )


def _rules(result: RatchetResult) -> list[str]:
    return sorted(f.rule for f in result.findings)


def _repo(make_repo: MakeRepo, head: Mapping[str, str | None]) -> RepoFixture:
    return make_repo({"pkg/a.py": "def f(x):\n    return x\n", "tests/test_a.py": BASE_TEST}, head)


def test_ratchets_owned_unowned(make_repo: MakeRepo) -> None:
    skipped = BASE_TEST.replace(
        "from pkg.a import f\n", "import pytest\n\nfrom pkg.a import f\n"
    ).replace("def test_two", "@pytest.mark.skip\ndef test_two")
    r = _repo(make_repo, {"tests/test_a.py": skipped})
    assert _rules(_run(r, OWNED)) == ["policy.test_disabled.owned"]
    assert _rules(_run(r, UNOWNED)) == ["policy.test_disabled.unowned"]

    deleted = BASE_TEST.split("\n\ndef test_two")[0] + "\n"
    r = _repo(make_repo, {"tests/test_a.py": deleted})
    assert _rules(_run(r, OWNED)) == ["policy.test_deleted.owned"]
    assert _rules(_run(r, UNOWNED)) == ["policy.test_deleted.unowned"]

    weakened = BASE_TEST.replace("    assert f(0) == 0\n", "")
    r = _repo(make_repo, {"tests/test_a.py": weakened})
    assert _rules(_run(r, OWNED)) == ["policy.test_weakened.owned"]
    assert _rules(_run(r, UNOWNED)) == ["policy.test_weakened.unowned"]

    r = make_repo(
        {".github/workflows/ci.yml": "on: push\n"}, {".github/workflows/ci.yml": "on: [push]\n"}
    )
    assert _rules(_run(r)) == ["policy.protected_path"]
    assert _rules(_run(r, frozenset({".github/workflows/ci.yml"}))) == []

    pyproject = '[project]\nname = "x"\nversion = "1"\n\n[tool.ruff]\nline-length = 100\n'
    r = make_repo(
        {"pyproject.toml": pyproject}, {"pyproject.toml": pyproject.replace('"1"', '"2"')}
    )
    assert _rules(_run(r)) == []
    r = make_repo(
        {"pyproject.toml": pyproject}, {"pyproject.toml": pyproject.replace("100", "120")}
    )
    assert _rules(_run(r)) == ["policy.protected_path"]


def test_every_finding_is_e1_policy(make_repo: MakeRepo) -> None:
    r = make_repo({"Makefile": "check:\n\ttrue\n"}, {"Makefile": "check:\n\tfalse\n"})
    (finding,) = _run(r).findings
    assert finding.grade is Grade.E1_EXACT
    assert finding.source is Source.POLICY
    assert finding.file == "Makefile"


def test_plan_path_and_review_toml_are_protected(make_repo: MakeRepo) -> None:
    r = make_repo(
        {".review.toml": "", "docs/plan.md": "# Plan\n"},
        {".review.toml": "[rules]\n", "docs/plan.md": "# Plan, edited\n"},
    )
    result = _run(r, plan_path="docs/plan.md")
    assert sorted(f.file for f in result.findings) == [".review.toml", "docs/plan.md"]
    assert result.review_toml_edited is True
    assert ".review.toml" in PROTECTED_GLOBS


def test_assertion_count_not_lines(make_repo: MakeRepo) -> None:
    r = _repo(make_repo, {"tests/test_a.py": BASE_TEST.replace("f(3) == 3", "f(3) == 3 and f(4)")})
    assert _rules(_run(r)) == []

    r = _repo(make_repo, {"tests/test_a.py": BASE_TEST.replace("    assert f(1) == 1\n", "")})
    assert _rules(_run(r)) == ["policy.test_weakened.unowned"]


def test_expected_value_chased(make_repo: MakeRepo) -> None:
    base = "def test_double():\n    assert f(2) == 4\n"
    r = make_repo({"tests/test_a.py": base}, {"tests/test_a.py": base.replace("4", "5")})
    result = _run(r, plan_text="Make the widget faster.")
    (finding,) = result.findings
    assert finding.rule == "policy.expected_value_chased"
    assert "«4»" in finding.evidence
    assert "«5»" in finding.evidence
    assert _run(r, plan_text="Update test_double for the new rule.").findings == ()
    assert _run(r, plan_text="The doubled value is now 5.").findings == ()


def test_expected_value_chased_in_typescript(make_repo: MakeRepo) -> None:
    base = 'it("doubles", () => {\n  expect(double(2)).toBe(4);\n});\n'
    r = make_repo(
        {"web/src/a.test.ts": base}, {"web/src/a.test.ts": base.replace("toBe(4)", "toBe(5)")}
    )
    assert _rules(_run(r)) == ["policy.expected_value_chased"]


def test_suppression_reason(make_repo: MakeRepo) -> None:
    base = {"src/a.py": "x = 1\n"}
    cases = {
        "x = y  # noqa: BLE001\n": ["policy.unjustified_suppression"],
        "x = y  # noqa: BLE001 -- the test double raises anything\n": [],
        "x = y  # type: ignore[attr-defined] -- mypy can't see the plugin\n": [],
    }
    for line, expected in cases.items():
        r = make_repo(base, {"src/a.py": line})
        result = _run(r)
        assert _rules(result) == expected, line
        assert result.suppression_added is True


def test_suppression_already_present_is_not_new(make_repo: MakeRepo) -> None:
    base = {"src/a.py": "import os  # noqa: F401\n"}
    r = make_repo(base, {"src/a.py": "import os  # noqa: F401\nx = 1\n"})
    result = _run(r)
    assert result.findings == ()
    assert result.suppression_added is False


def test_typescript_suppression_markers(make_repo: MakeRepo) -> None:
    r = make_repo(
        {"web/src/a.ts": "export const a = 1;\n"},
        {"web/src/a.ts": "export const a = b as any;\n// @ts-ignore -- the typings lag\n"},
    )
    result = _run(r)
    assert _rules(result) == ["policy.unjustified_suppression"]
    assert result.findings[0].line == 1


def test_environment_guards_are_allowed(make_repo: MakeRepo) -> None:
    head = BASE_TEST.replace(
        "from pkg.a import f\n",
        "import sys\n\nimport pytest\n\nfrom pkg.a import f\n\n"
        "fitz = pytest.importorskip('fitz')\n",
    ).replace(
        "def test_two",
        '@pytest.mark.skipif(sys.platform == "win32", reason="posix only")\ndef test_two',
    )
    r = _repo(make_repo, {"tests/test_a.py": head})
    assert _rules(_run(r)) == []

    guarded = BASE_TEST.replace(
        "def test_two():\n",
        "def test_two():\n    if sys.platform == 'win32':\n        pytest.skip('posix only')\n",
    )
    r = _repo(make_repo, {"tests/test_a.py": guarded})
    assert _rules(_run(r)) == []

    unguarded = BASE_TEST.replace(
        "def test_two():\n", "def test_two():\n    pytest.skip('later')\n"
    )
    r = _repo(make_repo, {"tests/test_a.py": unguarded})
    assert _rules(_run(r)) == ["policy.test_disabled.unowned"]


def test_xfail_conditions(make_repo: MakeRepo) -> None:
    for decorator, expected in (
        ('@pytest.mark.xfail(reason="flaky")', ["policy.test_disabled.unowned"]),
        ("@pytest.mark.xfail", ["policy.test_disabled.unowned"]),
        ('@pytest.mark.xfail(sys.platform == "win32", reason="x")', []),
        ('@pytest.mark.xfail(condition=IS_CI, reason="x")', []),
        ('@unittest.skip("later")', ["policy.test_disabled.unowned"]),
    ):
        head = BASE_TEST.replace("def test_two", f"{decorator}\ndef test_two")
        r = _repo(make_repo, {"tests/test_a.py": head})
        assert _rules(_run(r)) == expected, decorator


def test_typescript_only_is_disabling(make_repo: MakeRepo) -> None:
    base = 'it("a", () => {\n  expect(a()).toBe(1);\n});\n'
    r = make_repo(
        {"web/src/a.test.ts": base}, {"web/src/a.test.ts": base.replace("it(", "it.only(")}
    )
    assert _rules(_run(r, OWNED)) == ["policy.test_disabled.owned"]
    assert _rules(_run(r, UNOWNED)) == ["policy.test_disabled.unowned"]


def test_deleted_test_with_a_replacement_is_allowed(make_repo: MakeRepo) -> None:
    head = BASE_TEST.replace("def test_two():\n    assert f(3) == 3\n", "")
    head += "\n\ndef test_two_renamed():\n    assert f(3) == 3\n    assert f(4) == 4\n"
    r = _repo(make_repo, {"tests/test_a.py": head})
    assert _rules(_run(r)) == []

    weaker = BASE_TEST.replace("def test_two():\n    assert f(3) == 3\n", "")
    weaker += "\n\ndef test_other():\n    assert g(3) == 3\n"
    r = _repo(make_repo, {"tests/test_a.py": weaker})
    assert _rules(_run(r)) == ["policy.test_deleted.unowned"]


def test_plan_naming_a_deleted_test_exempts_it(make_repo: MakeRepo) -> None:
    deleted = BASE_TEST.split("\n\ndef test_two")[0] + "\n"
    r = _repo(make_repo, {"tests/test_a.py": deleted})
    assert _rules(_run(r, plan_text="Delete `test_two`; it tests a removed path.")) == []


def test_deleted_test_file_counts_each_test(make_repo: MakeRepo) -> None:
    r = _repo(make_repo, {"tests/test_a.py": None})
    assert _rules(_run(r)) == ["policy.test_deleted.unowned"] * 2


def test_count_assertions() -> None:
    py = (
        "import pytest\n\n"
        "def test_a():\n    assert x\n    m.assert_called_once()\n"
        "    with pytest.raises(ValueError):\n        f()\n\n"
        "class TestB:\n    def test_c(self):\n        self.assertEqual(1, 1)\n\n"
        "def helper():\n    assert y\n"
    )
    assert count_assertions_py(py) == {"test_a": 3, "TestB.test_c": 1}
    assert count_assertions_py("def (:\n") == {}
    ts = (
        'describe("d", () => {\n'
        '  it("one", () => {\n    expect(a).toBe(1);\n    expect(b).toBe(2);\n  });\n'
        "  test('two', () => {\n    expect(c).toBe(3);\n  });\n"
        "});\n"
    )
    assert count_assertions_ts(ts) == {"one": 2, "two": 1}


def test_findings_are_deterministic(make_repo: MakeRepo) -> None:
    r = make_repo(
        {"Makefile": "a\n", "ruff.toml": "a\n", "src/a.py": "x = 1\n"},
        {"Makefile": "b\n", "ruff.toml": "b\n", "src/a.py": "x = 1  # noqa\n"},
    )
    first, second = _run(r), _run(r)
    assert first == second
    assert [f.file for f in first.findings] == ["Makefile", "ruff.toml", "src/a.py"]


def test_raw_signal_counts_match_the_prototype(make_repo: MakeRepo) -> None:
    base_tests = (
        "import pytest\n\n\n"
        "def test_w():\n    assert f(1) == 1\n    assert f(2) == 2\n\n\n"
        "def test_e():\n    assert f(2) == 4\n\n\n"
        "def test_s():\n    assert f(0) == 0\n"
    )
    head_tests = (
        base_tests.replace("    assert f(2) == 2\n", "")
        .replace("f(2) == 4", "f(2) == 5")
        .replace(
            "def test_s", '@pytest.mark.skipif(sys.platform == "win32", reason="x")\ndef test_s'
        )
        .replace("import pytest\n", "import pytest\nimport sys  # type: ignore\n")
    )
    r = make_repo(
        {
            "src/a.py": "x = 1\n",
            "tests/test_a.py": base_tests,
            "tests/conftest.py": "",
            "pyproject.toml": '[project]\nname = "x"\n',
        },
        {
            "src/a.py": "x = cast(Any, y)\nz = 1  # noqa: E501 - long URL\n",
            "tests/test_a.py": head_tests,
            "tests/conftest.py": "import pytest\n",
            "pyproject.toml": '[project]\nname = "y"\n',
        },
    )
    counts = raw_signal_counts(r.path, r.base, r.head)
    assert counts == {
        "suppress_new": 2,
        "suppress_no_reason": 1,
        "test_disabled": 1,
        "test_deleted": 0,
        "test_weakened": 1,
        "assert_edited": 1,
        "config_touched": 2,
    }
    assert dict(_run(r).raw_counts) == counts


def test_a_marker_in_prose_is_not_a_suppression(make_repo: MakeRepo) -> None:
    # `as any` counts in code only (the spec reads it by AST), and a Python marker only in
    # a comment: prose in a TypeScript comment or a Python docstring suppresses nothing
    # (bench case C11's hand judgment found both).
    r = make_repo(
        {"web/src/a.ts": "export const a = 1;\n", "src/a.py": "x = 1\n"},
        {
            "web/src/a.ts": (
                "// dropped, same as any other version mismatch\n"
                'export const a = "cast as any";\n'
                "export const b = c as any; // as any\n"
            ),
            "src/a.py": (
                'def f():\n    """A `# type: ignore` here would be flagged."""\n    return 1\n'
            ),
        },
    )
    result = _run(r)
    assert [(f.rule, f.file, f.line) for f in result.findings] == [
        ("policy.unjustified_suppression", "web/src/a.ts", 3)
    ]
