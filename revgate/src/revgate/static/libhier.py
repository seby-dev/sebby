"""G6's hierarchy rules: `isinstance` shadowing and library subclasses a dispatch misses.

Both rules read the `isinstance` chains the index records for each changed function.

- `lib.isinstance_shadowing`: a later branch tests a class that subclasses a class an
  earlier branch already tested, so the later branch never runs. Project classes resolve
  through the index's method resolution order; library classes through the library's
  own source (`libsrc`), never by importing it.
- `lib.subclass_unhandled` and `lib.subclass_unlisted`: for a branch over a class from a
  package in `[semantic].library_hierarchies`, every strict subclass the function neither
  dispatches nor mentions (a `not isinstance` guard mentions it) falls into the parent's
  branch. A subclass on `[semantic].curated_distinct_subclasses` is `unhandled`; any other
  is `unlisted`, in shadow.

Every finding is differenced against the same check on the base version of the function,
so a shadowing that was already there doesn't fire on a task that only touched it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from revgate.config import Config, is_test_path
from revgate.model import Finding, Grade, RuleOutput, Source, Unverified, new_finding
from revgate.spi import libsrc
from revgate.spi.facts import IsinstanceChain, PyFileFacts
from revgate.spi.link import TreeIndex
from revgate.static.ctx import StaticCtx
from revgate.verdict.triage import baseline_difference

SHADOWING = "lib.isinstance_shadowing"
UNHANDLED = "lib.subclass_unhandled"
UNLISTED = "lib.subclass_unlisted"


def _short(dotted: str) -> str:
    return dotted.rsplit(".", 1)[-1]


@dataclass
class _Libraries:
    """Library hierarchies for the packages the configuration names, read once per check."""

    repo: Path
    cfg: Config
    _site: Path | None = None
    _site_read: bool = False
    _hier: dict[str, dict[str, tuple[str, ...]]] = field(default_factory=dict)
    missing: set[str] = field(default_factory=set)

    def package(self, dotted: str) -> str | None:
        pkg = dotted.split(".", 1)[0]
        return pkg if pkg in self.cfg.library_hierarchies else None

    def site(self) -> Path | None:
        if not self._site_read:
            self._site = libsrc.find_site_packages(self.repo)
            self._site_read = True
        return self._site

    def hierarchy(self, pkg: str) -> Mapping[str, tuple[str, ...]] | None:
        """The package's class hierarchy, or None (recorded as missing) when it can't be read."""
        if pkg not in self._hier:
            site = self.site()
            self._hier[pkg] = libsrc.library_hierarchy(site, pkg) if site is not None else {}
        if not self._hier[pkg]:
            self.missing.add(pkg)
            return None
        return self._hier[pkg]

    def canonical(self, dotted: str) -> str:
        pkg = self.package(dotted)
        site = self.site()
        if pkg is None or site is None or self.hierarchy(pkg) is None:
            return dotted
        return libsrc.canonical_class(site, pkg, dotted)

    def subclasses(self, dotted: str) -> tuple[str, ...] | None:
        pkg = self.package(dotted)
        hier = self.hierarchy(pkg) if pkg is not None else None
        if hier is None:
            return None
        return libsrc.strict_subclasses(hier, self.canonical(dotted))


@dataclass(frozen=True)
class _Branch:
    line: int
    negated: bool
    classes: tuple[str, ...]  # resolved, canonical for library classes
    guarded: bool = False  # an `isinstance(...) and ...` test: it shadows no later branch


def _resolve(index: TreeIndex, libs: _Libraries, module: str, text: str) -> str | None:
    found = index.resolve(text, module)
    if not found:
        return None
    target = found[0]
    return target if target in index.classes else libs.canonical(target)


def _is_library(index: TreeIndex, libs: _Libraries, dotted: str) -> bool:
    return dotted not in index.classes and libs.package(dotted) is not None


def _is_subclass(index: TreeIndex, libs: _Libraries, child: str, parent: str) -> bool:
    if child == parent:
        return False
    if child in index.classes:
        ancestors = index.mro(child)[1:]
        if parent in ancestors:
            return True
        # A project class whose external base is the library class, or one of its subclasses.
        for base in (a for a in ancestors if a not in index.classes):
            canon = libs.canonical(base)
            if canon == parent or _is_subclass(index, libs, canon, parent):
                return True
        return False
    if _is_library(index, libs, parent) and _is_library(index, libs, child):
        subs = libs.subclasses(parent)
        return subs is not None and child in subs
    return False


@dataclass(frozen=True)
class _Target:
    """Where a function's findings go: its head file and qualname, whichever side is read."""

    path: str
    symbol: str


def _branches(
    index: TreeIndex, libs: _Libraries, module: str, chain: IsinstanceChain
) -> list[_Branch]:
    out: list[_Branch] = []
    for b in chain.branches:
        resolved = (_resolve(index, libs, module, text) for text in b.classes)
        out.append(
            _Branch(b.line, b.negated, tuple(r for r in resolved if r is not None), b.guarded)
        )
    return out


def _shadowing(
    index: TreeIndex, libs: _Libraries, branches: list[_Branch], target: _Target
) -> list[Finding]:
    out: list[Finding] = []
    earlier: list[str] = []
    for branch in branches:
        if branch.negated:
            continue
        for cls in branch.classes:
            parent = next((p for p in earlier if _is_subclass(index, libs, cls, p)), None)
            if parent is None:
                continue
            sub, sup = _short(cls), _short(parent)
            out.append(
                new_finding(
                    SHADOWING,
                    file=target.path,
                    line=branch.line,
                    symbol=target.symbol,
                    message=(
                        f"the isinstance branch for {sub} never runs: {sub} subclasses "
                        f"{sup}, tested earlier"
                    ),
                    evidence=f"{cls} subclasses {parent}; {parent} is tested first",
                    evidence_key=f"{cls} after {parent}",
                    grade=Grade.E1_EXACT,
                    source=Source.GENERIC,
                    fix=f"test {sub} before {sup}",
                )
            )
        if not branch.guarded:
            earlier.extend(branch.classes)
    return out


def _handled(
    index: TreeIndex,
    libs: _Libraries,
    module: str,
    chain: IsinstanceChain,
    branches: list[_Branch],
    candidates: set[str],
) -> set[str]:
    """Classes the function dispatches on or otherwise mentions (a `not isinstance` guard)."""
    handled = {c for b in branches for c in b.classes}
    shorts = {_short(c) for c in candidates}
    for name in chain.mentioned:
        if _short(name) in shorts:
            resolved = _resolve(index, libs, module, name)
            if resolved is not None:
                handled.add(resolved)
    return handled


def _subclass_findings(
    index: TreeIndex,
    libs: _Libraries,
    module: str,
    chain: IsinstanceChain,
    branches: list[_Branch],
    target: _Target,
) -> list[Finding]:
    dispatched: list[tuple[_Branch, str, tuple[str, ...]]] = []
    for branch in branches:
        if branch.negated:
            continue
        for cls in branch.classes:
            if not _is_library(index, libs, cls):
                continue
            subs = libs.subclasses(cls)
            if subs is not None:
                dispatched.append((branch, cls, subs))
    if not dispatched:
        return []
    candidates = {s for _b, _c, subs in dispatched for s in subs}
    handled = _handled(index, libs, module, chain, branches, candidates)
    curated = {libs.canonical(c) for c in libs.cfg.curated_distinct_subclasses}
    curated |= set(libs.cfg.curated_distinct_subclasses)
    out: list[Finding] = []
    func = _short(target.symbol)
    for branch, cls, subs in dispatched:
        # A subclass of a handled class between it and `cls` takes that class's branch.
        middle = [h for h in handled if h in subs]
        covered = {s for h in middle for s in (libs.subclasses(h) or ())}
        for sub in subs:
            if sub in handled or sub in covered:
                continue
            is_curated = sub in curated
            rule = UNHANDLED if is_curated else UNLISTED
            short_sub, short_cls = _short(sub), _short(cls)
            if is_curated:
                message = (
                    f"{short_sub} subclasses {short_cls}, but {func}() neither dispatches "
                    f"nor excludes it"
                )
            else:
                message = f"{short_sub} takes the {short_cls} branch in {func}()"
            out.append(
                new_finding(
                    rule,
                    file=target.path,
                    line=branch.line,
                    symbol=target.symbol,
                    message=message,
                    evidence=f"{sub} is a strict subclass of {cls} in the library source",
                    evidence_key=f"{sub} under {cls}",
                    grade=Grade.E1_EXACT if is_curated else Grade.E3_HEURISTIC,
                    source=Source.DECLARED if is_curated else Source.GENERIC,
                    fix=(
                        f"add a branch for {short_sub} before {short_cls}, or exclude it"
                        if is_curated
                        else None
                    ),
                )
            )
    return out


def _detect(
    index: TreeIndex, libs: _Libraries, facts: PyFileFacts, func: str, target: _Target
) -> list[Finding]:
    out: list[Finding] = []
    for chain in facts.isinstance_chains:
        if chain.func != func:
            continue
        branches = _branches(index, libs, facts.module, chain)
        out.extend(_shadowing(index, libs, branches, target))
        out.extend(_subclass_findings(index, libs, facts.module, chain, branches, target))
    return out


def _dedupe(findings: Iterable[Finding]) -> list[Finding]:
    seen: dict[str, Finding] = {}
    for f in findings:
        seen.setdefault(f.id, f)
    return sorted(seen.values(), key=lambda f: (f.file, f.line, f.rule, f.id))


class LibHierarchyRule:
    """`lib.isinstance_shadowing` and `lib.subclass_unhandled` (E1, blocking), and
    `lib.subclass_unlisted` (E3, shadow)."""

    ids = frozenset({SHADOWING, UNHANDLED, UNLISTED})
    languages = frozenset({"py"})

    def check(self, ctx: StaticCtx) -> Iterable[RuleOutput]:
        libs = _Libraries(ctx.repo, ctx.cfg)
        found: list[Finding] = []
        for qualname, fc in ctx.change.functions.items():
            head_fn = fc.head
            if head_fn is None or is_test_path(head_fn.path, ctx.cfg):
                continue
            head_facts = ctx.head.py.get(head_fn.path)
            if head_facts is None or head_facts.parse_error is not None:
                continue
            target = _Target(head_fn.path, qualname)
            at_head = _detect(ctx.head, libs, head_facts, head_fn.qualname, target)
            if not at_head:
                continue
            at_base: list[Finding] = []
            base_fn = fc.base
            base_facts = ctx.base.py.get(base_fn.path) if base_fn is not None else None
            if base_fn is not None and base_facts is not None:
                at_base = _detect(ctx.base, libs, base_facts, base_fn.qualname, target)
            found.extend(baseline_difference(at_head, at_base))
        outputs: list[RuleOutput] = list(_dedupe(found))
        outputs.extend(Unverified(UNHANDLED, f"library-missing:{p}") for p in sorted(libs.missing))
        return outputs
