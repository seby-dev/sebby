"""`revgate map`: every changed area is deep in the MVP, each with its reasons."""

from __future__ import annotations

import io
import json

from conftest import MakeRepo

from revgate.gitio import common_dir
from revgate.map_cmd import run_map

CODE_BASE = "def a():\n    return 1\n\n\ndef b():\n    return 2\n"
CODE_HEAD = "def a():\n    return 10\n\n\ndef b():\n    return 2\n"
REVIEW_TOML = (
    '[policy]\nrevgate_source_repo = "/nonexistent/revgate-source"\n'
    '\n[paths]\nprompts = ["prompts/**"]\n'
    '\n[risk.paths]\n"pkg/risky.py" = "wave"\n'
)


def _areas(text: str) -> list[dict[str, object]]:
    data = json.loads(text)
    assert isinstance(data, dict)
    areas = data["areas"]
    assert isinstance(areas, list)
    return areas


def test_every_area_is_deep_with_reasons(make_repo: MakeRepo) -> None:
    fx = make_repo(
        {"pkg/__init__.py": "", "pkg/code.py": CODE_BASE, "pkg/risky.py": "X = 1\n"},
        {
            "pkg/code.py": CODE_HEAD,
            "pkg/risky.py": "X = 2\n",
            ".github/workflows/ci.yml": "on: push\n",
            "prompts/read.txt": "Read the page.\n",
            "docs/notes.md": "# Notes\n",
        },
        review_toml=REVIEW_TOML,
    )
    out = io.StringIO()
    assert run_map(fx.path, fx.base, fx.head, None, as_json=True, out=out) == 0
    areas = _areas(out.getvalue())
    assert areas
    for area in areas:
        assert area["status"] == "deep"
        assert area["reasons"]
        assert set(area) >= {"path", "symbol", "lines", "status", "reasons"}
    by_path = {(a["path"], a["symbol"]): a for a in areas}
    code = by_path[("pkg/code.py", "pkg.code.a")]
    assert "pinning not built (Stage 2)" in code["reasons"]  # type: ignore[operator]
    assert code["lines"] == [1, 2]
    assert ("pkg/code.py", "pkg.code.b") not in by_path
    risky = [a for a in areas if a["path"] == "pkg/risky.py"]
    assert risky and all("risk path" in a["reasons"] for a in risky)  # type: ignore[operator]
    ci = [a for a in areas if a["path"] == ".github/workflows/ci.yml"]
    assert ci and "never cleared" in ci[0]["reasons"]  # type: ignore[operator]
    prompt = [a for a in areas if a["path"] == "prompts/read.txt"]
    assert prompt and "never cleared" in prompt[0]["reasons"]  # type: ignore[operator]
    assert (common_dir(fx.path) / "revgate" / "maps" / f"{fx.head}.json").is_file()


def test_no_review_toml_is_calibration(make_repo: MakeRepo) -> None:
    fx = make_repo({"pkg/code.py": CODE_BASE}, {"pkg/code.py": CODE_HEAD})
    out = io.StringIO()
    assert run_map(fx.path, fx.base, fx.head, None, as_json=True, out=out) == 0
    for area in _areas(out.getvalue()):
        assert "no .review.toml: calibration" in area["reasons"]  # type: ignore[operator]


def test_markdown_summary(make_repo: MakeRepo) -> None:
    fx = make_repo({"pkg/code.py": CODE_BASE}, {"pkg/code.py": CODE_HEAD})
    out = io.StringIO()
    assert run_map(fx.path, fx.base, fx.head, None, as_json=False, out=out) == 0
    text = out.getvalue()
    assert "deep: 1" in text
    assert "pkg/code.py" in text
    assert len(text.splitlines()) <= 200
