"""`StaticCtx`: everything a static rule reads, built once per review.

`build_static_ctx` indexes the fork and the tip (with `overrides` applied at the tip, for a
bench's seeded replay), keeps one TypeScript process open across both index builds, and
computes the change model between them.
"""

from __future__ import annotations

import difflib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from revgate import gitio
from revgate.change import ChangeModel, StatusEntry, change_model
from revgate.config import Config, is_test_path
from revgate.model import TaskScope
from revgate.plan import PlanTask
from revgate.report import ReportModel
from revgate.spi import ts_bridge
from revgate.spi.cache import BlobCache
from revgate.spi.link import TreeIndex, TsIndexerFn

Numstat = list[tuple[int | None, int | None, str]]


@dataclass(frozen=True)
class StaticCtx:
    base: TreeIndex
    head: TreeIndex
    change: ChangeModel
    plan: PlanTask | None
    report: ReportModel | None
    cfg: Config
    fork: str
    tip: str
    repo: Path
    scope: TaskScope
    unverified: tuple[str, ...]


def _line_count(data: bytes) -> int:
    return len(data.splitlines())


def _difflib_hunks(before: bytes, after: bytes) -> tuple[tuple[tuple[int, int], ...], int, int]:
    """Head-side `(start, count)` hunks in `git diff -U0`'s convention, plus the added and
    deleted line counts."""
    a, b = before.splitlines(keepends=True), after.splitlines(keepends=True)
    hunks: list[tuple[int, int]] = []
    added = deleted = 0
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        count = j2 - j1
        # git names the line before a pure deletion as the hunk's start.
        hunks.append((j1 + 1 if count else j1, count))
        added += count
        deleted += i2 - i1
    return tuple(hunks), added, deleted


def _apply_overrides(
    repo: Path,
    fork: str,
    status: Sequence[StatusEntry],
    hunks: Mapping[str, tuple[tuple[int, int], ...]],
    numstat: Sequence[tuple[int | None, int | None, str]],
    overrides: Mapping[str, bytes | None],
) -> tuple[list[StatusEntry], dict[str, tuple[tuple[int, int], ...]], Numstat]:
    """Each overridden path joins the diff as `A`, `M`, or `D` against the fork, the same
    side every other path's hunks are measured from."""
    entries = list(status)
    new_hunks = dict(hunks)
    stats: Numstat = list(numstat)
    for path in sorted(overrides):
        data = overrides[path]
        kept: list[StatusEntry] = []
        for letter, p, old in entries:
            if p != path:
                kept.append((letter, p, old))
            elif letter == "R" and old is not None:
                kept.append(("D", old, None))  # the rename's source is still gone
        entries = kept
        new_hunks.pop(path, None)
        stats = [s for s in stats if s[2] != path]
        before = gitio.show(repo, fork, path)
        if data is None:
            if before is not None:
                entries.append(("D", path, None))
                stats.append((0, _line_count(before), path))
            continue
        if before == data:
            continue
        ranges, added, deleted = _difflib_hunks(before or b"", data)
        entries.append(("A" if before is None else "M", path, None))
        if ranges:
            new_hunks[path] = ranges
        stats.append((added, deleted, path))
    entries.sort(key=lambda e: (e[1], e[0]))
    return entries, new_hunks, stats


def build_static_ctx(
    repo: Path,
    fork: str,
    tip: str,
    *,
    cfg: Config,
    plan: PlanTask | None,
    report: ReportModel | None,
    scope: TaskScope,
    cache: BlobCache,
    overrides: Mapping[str, bytes | None] | None = None,
) -> StaticCtx:
    status: Sequence[StatusEntry] = gitio.diff_name_status(repo, fork, tip)
    hunks: Mapping[str, tuple[tuple[int, int], ...]] = gitio.diff_hunks(repo, fork, tip)
    numstat: Sequence[tuple[int | None, int | None, str]] = gitio.diff_numstat(repo, fork, tip)
    if overrides:
        status, hunks, numstat = _apply_overrides(repo, fork, status, hunks, numstat, overrides)

    indexer, close = ts_bridge.make_ts_indexer(repo, lambda p: is_test_path(p, cfg))
    live: list[TsIndexerFn | None] = [indexer]

    def build(rev: str, over: Mapping[str, bytes | None] | None) -> TreeIndex:
        if live[0] is not None:
            try:
                return TreeIndex.build(repo, rev, cache, cfg, ts_indexer=live[0], overrides=over)
            except ts_bridge.TsUnavailable:
                live[0] = None  # the process died: both sides go without TypeScript facts
        return TreeIndex.build(repo, rev, cache, cfg, ts_indexer=None, overrides=over)

    try:
        base = build(fork, None)
        head = build(tip, overrides)
    finally:
        close()
    if live[0] is None and indexer is not None:
        # The base may have been built with TypeScript facts before the process died; a
        # comparison against a head without them would read every export as removed.
        base = TreeIndex.build(repo, fork, cache, cfg, ts_indexer=None)

    change = change_model(
        base,
        head,
        status,
        cfg,
        hunks=hunks,
        numstat=numstat,
        plan_kind=plan.kind if plan is not None else None,
    )
    unverified = tuple(sorted(set(base.unverified) | set(head.unverified)))
    return StaticCtx(
        base=base,
        head=head,
        change=change,
        plan=plan,
        report=report,
        cfg=cfg,
        fork=fork,
        tip=tip,
        repo=repo,
        scope=scope,
        unverified=unverified,
    )
