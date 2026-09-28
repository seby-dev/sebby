"""Every Stage 1a static rule, in the order `revgate task` runs them: cheapest first.

The ratchets, the plan rules, and the dangling-reference rule read the diff and a few
files; the wiring, contract, and library rules query the whole index, so they run last
and are the first to be skipped when the rules budget runs out.
"""

from __future__ import annotations

from collections.abc import Iterable

from revgate.model import RuleOutput
from revgate.rules.registry import StaticRule
from revgate.static.contracts import CardinalityRule
from revgate.static.ctx import StaticCtx
from revgate.static.docs_refs import DanglingCodeRefRule
from revgate.static.libhier import LibHierarchyRule
from revgate.static.plan_rules import PlanFidelityRule, PlanFilesRule
from revgate.static.ratchets import RatchetResult, ratchet_findings
from revgate.static.wiring import WiringRule

RATCHET_IDS = frozenset(
    {
        "policy.protected_path",
        "policy.unjustified_suppression",
        "policy.test_disabled.owned",
        "policy.test_disabled.unowned",
        "policy.test_deleted.owned",
        "policy.test_deleted.unowned",
        "policy.test_weakened.owned",
        "policy.test_weakened.unowned",
        "policy.expected_value_chased",
    }
)


class RatchetRule:
    """The G11 ratchets as a static rule. `ratchet` also returns the flags (a suppression
    added, `.review.toml` edited) that the verdict's focus conditions read."""

    ids = RATCHET_IDS
    languages = frozenset({"py", "ts"})

    def ratchet(self, ctx: StaticCtx) -> RatchetResult:
        plan = ctx.plan
        return ratchet_findings(
            ctx.repo,
            ctx.fork,
            ctx.tip,
            cfg=ctx.cfg,
            # The task scope's owns is the plan's in every command; a bench replay with no
            # plan gives a scope of its own, and the ratchets read ownership from it too.
            plan_owns=(plan.owns.all() if plan is not None else frozenset()) | ctx.scope.owns,
            plan_text=plan.section_text if plan is not None else "",
            plan_path=plan.plan_path if plan is not None else None,
        )

    def check(self, ctx: StaticCtx) -> Iterable[RuleOutput]:
        return self.ratchet(ctx).findings


def rule_name(rule: StaticRule) -> str:
    """A rule's name in `budget:`, `skipped: budget:`, and `internal:` notes: its first id."""
    return min(rule.ids)


ALL_RULES: tuple[StaticRule, ...] = (
    RatchetRule(),
    PlanFilesRule(),
    PlanFidelityRule(),
    DanglingCodeRefRule(),
    WiringRule(),
    CardinalityRule(),
    LibHierarchyRule(),
)
