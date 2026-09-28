"""G3's cardinality row: a function that now returns several items, and its old readers.

For every changed function whose cardinality went from one value (or one value or None) to
several, each production call site outside the task's added lines that reads only one
result (`f(...)[0]`, a fixed unpacking, or an attribute of the result) is a finding. The
other contract rows of G3 arrive with later stages.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from revgate.change import FuncChange
from revgate.config import is_test_path
from revgate.model import Finding, Grade, RuleOutput, Source, new_finding
from revgate.spi.facts import CallSite
from revgate.static.ctx import StaticCtx

RULE = "contract.cardinality_consumer"
_SINGLE = frozenset({"one", "optional"})
_READS_ONE = {
    "index0": "reads only [0]",
    "unpacked": "unpacks it as one value",
    "attr": "reads an attribute of one result",
}


def _widened(fc: FuncChange) -> bool:
    return (
        fc.base is not None
        and fc.head is not None
        and fc.status in ("modified", "moved")
        and fc.base.cardinality in _SINGLE
        and fc.head.cardinality == "many"
    )


def _consumers(ctx: StaticCtx, qualname: str) -> Iterator[CallSite]:
    for site in ctx.head.callers(qualname):
        if not site.path.endswith(".py") or is_test_path(site.path, ctx.cfg):
            continue
        if site.line in ctx.change.added_lines(site.path):
            continue  # the task wrote this call against the new contract
        if site.use in _READS_ONE:
            yield site


class CardinalityRule:
    """`contract.cardinality_consumer`: E2, generic, advisory with `review`."""

    ids = frozenset({RULE})
    languages = frozenset({"py"})

    def check(self, ctx: StaticCtx) -> Iterable[RuleOutput]:
        out: list[Finding] = []
        for qualname, fc in ctx.change.functions.items():
            if not _widened(fc):
                continue
            name = qualname.rsplit(".", 1)[-1]
            for site in _consumers(ctx, qualname):
                how = _READS_ONE[site.use]
                out.append(
                    new_finding(
                        RULE,
                        file=site.path,
                        line=site.line,
                        message=f"{name}() can now return several items; this caller {how}",
                        evidence=(
                            f"{qualname} cardinality {fc.base.cardinality if fc.base else '?'}"
                            f" -> many; call in {site.caller} ({site.use})"
                        ),
                        evidence_key=f"{qualname}|{site.caller}|{site.use}",
                        grade=Grade.E2_STRUCTURAL,
                        source=Source.GENERIC,
                        kind="integration",
                        impact="critical",
                        fix=f"handle every item {name}() returns, or take the first on purpose",
                    )
                )
        return sorted(out, key=lambda f: (f.file, f.line, f.id))
