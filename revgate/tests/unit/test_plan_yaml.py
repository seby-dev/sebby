"""The `plan-waves` YAML subset parser."""

from __future__ import annotations

import pytest

from revgate.plan_yaml import PlanYamlError, parse_yaml_subset

APPENDIX_H_BLOCK = """\
forbidden: [tests/frozen_cross_validate_base.py]   # plan-level; optional
waves:
  - id: W1
    subwaves:
      - id: W1a
        tasks:
          - id: T1
            risk: low
            depends_on: []
            owns:
              create: [src/pkg/new_module.py, tests/test_new_module.py]
              modify: [src/pkg/schema.py]
              test: [tests/test_new_module.py]
          - id: T2
            risk: high
            depends_on: []
            owns: {modify: [web/src/review/grid.ts], test: [web/src/review/grid.test.ts]}
            context: [docs/reference/grammar.md#rhythm]
      - id: W1b
        tasks:
          - id: T3
            risk: low
            depends_on: [T1]
            owns: {modify: [src/pkg/api.py, src/pkg/new_module.py], test: [tests/test_api.py]}
            runs: [tests/test_api_extract.py]      # tests run but not owned
            forbidden: [src/pkg/schema.py]
            context: [src/pkg/api.py::parse_request]
"""


def _tasks(doc: object) -> list[dict[str, object]]:
    assert isinstance(doc, dict)
    out: list[dict[str, object]] = []
    for wave in doc["waves"]:
        for sub in wave["subwaves"]:
            out.extend(sub["tasks"])
    return out


def test_appendix_h_block_parses() -> None:
    doc = parse_yaml_subset(APPENDIX_H_BLOCK)
    assert isinstance(doc, dict)
    assert doc["forbidden"] == ["tests/frozen_cross_validate_base.py"]
    tasks = _tasks(doc)
    assert [t["id"] for t in tasks] == ["T1", "T2", "T3"]
    assert [t["risk"] for t in tasks] == ["low", "high", "low"]
    assert tasks[0]["depends_on"] == []
    assert tasks[0]["owns"] == {
        "create": ["src/pkg/new_module.py", "tests/test_new_module.py"],
        "modify": ["src/pkg/schema.py"],
        "test": ["tests/test_new_module.py"],
    }
    assert tasks[1]["owns"] == {
        "modify": ["web/src/review/grid.ts"],
        "test": ["web/src/review/grid.test.ts"],
    }
    assert tasks[1]["context"] == ["docs/reference/grammar.md#rhythm"]
    # The trailing comment is dropped, not kept as part of the value.
    assert tasks[2]["runs"] == ["tests/test_api_extract.py"]
    assert tasks[2]["depends_on"] == ["T1"]
    assert tasks[2]["context"] == ["src/pkg/api.py::parse_request"]


def test_scalars() -> None:
    doc = parse_yaml_subset(
        "a: 50\nb: -3\nc: true\nd: false\ne: null\nf: ~\ng: plain text\nh: '07'\n"
    )
    assert doc == {
        "a": 50,
        "b": -3,
        "c": True,
        "d": False,
        "e": None,
        "f": None,
        "g": "plain text",
        "h": "07",
    }
    assert isinstance(doc, dict) and isinstance(doc["a"], int)


def test_quoted_strings_keep_hash() -> None:
    doc = parse_yaml_subset(
        'a: "x # not a comment"  # a comment\n'
        "b: 'it''s # here'\n"
        "c: [\"spec#Heading one\", 'q, r']\n"
    )
    assert doc == {"a": "x # not a comment", "b": "it's # here", "c": ["spec#Heading one", "q, r"]}


def test_nested_flow_and_block_sequences() -> None:
    doc = parse_yaml_subset(
        "x: {a: [1, 2, {b: c}], d: {}}\n"
        "y:\n"
        "- one\n"
        "- - two\n"
        "  - three\n"
        "z:\n"
        "  -\n"
        "    k: v\n"
        "# a full-line comment\n"
        "\n"
        "w: [ ]\n"
    )
    assert doc == {
        "x": {"a": [1, 2, {"b": "c"}], "d": {}},
        "y": ["one", ["two", "three"]],
        "z": [{"k": "v"}],
        "w": [],
    }


def test_empty_text_is_none() -> None:
    assert parse_yaml_subset("# only a comment\n\n") is None


def test_tab_in_indentation_raises_with_line() -> None:
    with pytest.raises(PlanYamlError) as err:
        parse_yaml_subset("a:\n  b: 1\n\tc: 2\n")
    assert err.value.line == 3


def test_unclosed_flow_sequence_raises() -> None:
    with pytest.raises(PlanYamlError) as err:
        parse_yaml_subset("a: 1\nb: [x, y\n")
    assert err.value.line == 2


@pytest.mark.parametrize(
    "text, line",
    [
        ("a: 1\na: 2\n", 2),  # duplicate key
        ("a: |\n  text\n", 1),  # block scalar
        ("a: &anchor 1\n", 1),  # anchor
        ("a: 1\n   b: 2\n", 2),  # stray indentation
        ("just a scalar line\nb: 1\n", 2),  # mixed node kinds
        ("a: [x] trailing\n", 1),  # text after a flow collection
        ('a: "unterminated\n', 1),
        ("a: {b c}\n", 1),  # flow mapping entry without a colon
    ],
)
def test_outside_the_subset_raises(text: str, line: int) -> None:
    with pytest.raises(PlanYamlError) as err:
        parse_yaml_subset(text)
    assert err.value.line == line
