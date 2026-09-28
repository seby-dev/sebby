"""`revgate map`: the branch review's map of changed areas, each deep or cleared.

An area is a changed Python function, or a 25-line bucket of changed lines outside any
changed function (algorithm spec "Area coverage for `revgate map`"). The MVP has no
pinning and no fail-first, so no area can meet the clearing conditions: every area is
`deep`, and each carries the reasons it can't be cleared yet.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from revgate import gitio
from revgate.change import ChangeModel
from revgate.config import Config, ConfigError, glob_match, is_prod_path, is_test_path, load_config
from revgate.gitio import GitError, SafetyError
from revgate.model import BUCKET_LINES, TaskScope
from revgate.plan import parse_plan_block
from revgate.plan_yaml import PlanYamlError
from revgate.static.ctx import build_static_ctx
from revgate.store import atomic_write_text, state_dir
from revgate.task_cmd import blob_cache

MAX_LINES = 200
CALIBRATION = "no .review.toml: calibration"
NEVER = "never cleared"
UNPINNED = "pinning not built (Stage 2)"
NO_FAIL_FIRST = "fail-first not built (Stage 2)"
NO_FIXTURES = "fixture contracts not built (Stage 2)"
FALLBACK = "clearing not built (MVP)"
CONFIG_GLOBS: tuple[str, ...] = (
    ".github/**",
    "Makefile",
    "**/Makefile",
    ".review.toml",
    "pyproject.toml",
    "**/pyproject.toml",
    "**/package.json",
    "**/tsconfig*.json",
    "**/eslint.config.*",
    "**/.eslintrc*",
    "**/.prettierrc*",
    "**/vite.config.*",
    "**/vitest.config.*",
    "**/playwright.config.*",
    "**/*.cfg",
    "**/*.ini",
    "ruff.toml",
    ".pre-commit-config.yaml",
    "**/uv.lock",
    "uv.lock",
    "**/package-lock.json",
)


class MapAbort(Exception):
    """Exit 2 with one `revgate: <reason>` line."""


@dataclass(frozen=True)
class Area:
    path: str
    symbol: str | None
    lines: tuple[int, int] | None
    status: str
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "symbol": self.symbol,
            "lines": list(self.lines) if self.lines is not None else None,
            "status": self.status,
            "reasons": list(self.reasons),
        }


def _reasons(path: str, cfg: Config, risky_tasks: Mapping[str, str], deleted: bool) -> list[str]:
    out: list[str] = []
    if cfg.is_default:
        out.append(CALIBRATION)
    if cfg.risk_paths and glob_match(path, tuple(cfg.risk_paths)):
        out.append("risk path")
    if path in risky_tasks:
        out.append(f"risk: high task {risky_tasks[path]}")
    if deleted:
        out.append("deleted")
    if glob_match(path, cfg.prompt_globs) or glob_match(path, CONFIG_GLOBS):
        out.append(NEVER)
    elif path.endswith(".py"):
        if is_test_path(path, cfg):
            out.append(NO_FAIL_FIRST)
        elif is_prod_path(path, cfg):
            out.append(UNPINNED)
    elif path.endswith((".ts", ".tsx")):
        out.append(NO_FAIL_FIRST)
        if path.endswith(".tsx") and not is_test_path(path, cfg):
            out.append(NEVER)
    elif path.endswith(".json"):
        out.append(NO_FIXTURES)
    if not out:
        out.append(FALLBACK)
    return out


def _buckets(lines: frozenset[int]) -> list[tuple[int, int]]:
    starts = sorted({line // BUCKET_LINES for line in lines})
    return [(max(1, b * BUCKET_LINES), b * BUCKET_LINES + BUCKET_LINES - 1) for b in starts]


def build_areas(change: ChangeModel, cfg: Config, risky_tasks: Mapping[str, str]) -> list[Area]:
    areas: list[Area] = []
    deleted = {path for letter, path, _old in change.status if letter == "D"}
    heads = {path for letter, path, _old in change.status if letter != "D"}
    for path in sorted(deleted):
        areas.append(Area(path, None, None, "deep", tuple(_reasons(path, cfg, risky_tasks, True))))
    for path in sorted(heads):
        reasons = tuple(_reasons(path, cfg, risky_tasks, False))
        changed = change.added_lines(path)
        covered: set[int] = set()
        funcs = sorted(
            (fc for fc in change.functions.values() if fc.path == path and fc.head is not None),
            key=lambda fc: (fc.head.lineno if fc.head is not None else 0, fc.qualname),
        )
        for fc in funcs:
            assert fc.head is not None
            span = (fc.head.lineno, fc.head.end_lineno)
            covered.update(range(span[0], span[1] + 1))
            areas.append(Area(path, fc.qualname, span, "deep", reasons))
        rest = frozenset(changed - covered)
        if rest:
            areas.extend(Area(path, None, b, "deep", reasons) for b in _buckets(rest))
        elif not funcs:
            areas.append(Area(path, None, None, "deep", reasons))
    return areas


def _risky_tasks(top: Path, base: str, plan_path: str | None) -> dict[str, str]:
    """Each file a `risk: high` task of the plan owns, mapped to that task's id."""
    if plan_path is None:
        return {}
    try:
        data = gitio.show(top, base, plan_path)
    except GitError as exc:
        raise MapAbort(f"can't read {plan_path} at {base[:7]}: {exc}") from exc
    if data is None:
        raise MapAbort(f"{plan_path} doesn't exist at {base[:7]}")
    try:
        block = parse_plan_block(data.decode("utf-8", errors="replace"))
    except PlanYamlError as exc:
        raise MapAbort(f"{plan_path}: {exc}") from exc
    out: dict[str, str] = {}
    if block is None:
        return out
    for _w, _s, task in block.ordered_tasks():
        if task.risk == "high":
            for path in sorted(task.owns.all()):
                out.setdefault(path, task.id)
    return out


def render_markdown(areas: list[Area], head: str, base: str, json_path: Path) -> str:
    deep = sum(1 for a in areas if a.status == "deep")
    lines = [
        f"# revgate map {head[:7]} (base {base[:7]})",
        "",
        f"deep: {deep}, cleared: {len(areas) - deep}",
    ]
    current: str | None = None
    for a in areas:
        if a.status != "deep":
            continue
        if a.path != current:
            current = a.path
            lines += ["", f"## {a.path}", ""]
        where = f"`{a.symbol}` " if a.symbol is not None else ""
        span = f"lines {a.lines[0]}-{a.lines[1]}" if a.lines is not None else "whole file"
        lines.append(f"- {where}{span}: {'; '.join(a.reasons)}")
    tail = f"JSON: {json_path}"
    if len(lines) + 1 > MAX_LINES:
        kept = lines[: MAX_LINES - 2]
        more = len(lines) - len(kept)
        return "\n".join([*kept, f"… {more} more lines; see the JSON", tail])
    return "\n".join([*lines, "", tail][:MAX_LINES])


def run_map(
    repo: Path, base: str, head: str, plan_path: str | None, *, as_json: bool, out: TextIO
) -> int:
    try:
        return _run_map(repo, base, head, plan_path, as_json, out)
    except MapAbort as exc:
        print(f"revgate: {exc}", file=out)
        return 2


def _run_map(
    repo: Path, base_rev: str, head_rev: str, plan_path: str | None, as_json: bool, out: TextIO
) -> int:
    try:
        top = gitio.safe_toplevel(repo, expected=repo)
    except SafetyError as exc:
        raise MapAbort(str(exc)) from exc
    try:
        base = gitio.rev_parse(top, base_rev)
        head = gitio.rev_parse(top, head_rev)
    except GitError as exc:
        raise MapAbort(f"--base or --head doesn't name a commit: {exc}") from exc
    try:
        cfg = load_config(top, rev=base)
    except ConfigError as exc:
        raise MapAbort(str(exc)) from exc
    risky = _risky_tasks(top, base, plan_path)
    state = state_dir(top)
    try:
        ctx = build_static_ctx(
            top,
            base,
            head,
            cfg=cfg,
            plan=None,
            report=None,
            scope=TaskScope(None, frozenset(), frozenset()),
            cache=blob_cache(state),
        )
    except Exception as exc:  # the index or the change model crashed: exit 2 (A6)
        raise MapAbort(f"the index or change model failed: {exc!r}") from exc
    areas = build_areas(ctx.change, cfg, risky)
    deep = sum(1 for a in areas if a.status == "deep")
    doc = {
        "base": base,
        "head": head,
        "plan": plan_path,
        "counts": {"deep": deep, "cleared": len(areas) - deep},
        "unverified": list(ctx.unverified),
        "areas": [a.to_dict() for a in areas],
    }
    text = json.dumps(doc, sort_keys=True, separators=(",", ":"))
    json_path = state / "maps" / f"{head}.json"
    atomic_write_text(json_path, text)
    print(text if as_json else render_markdown(areas, head, base, json_path), file=out)
    return 0
