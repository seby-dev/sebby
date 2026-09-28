"""Routing: a finding's tier, its audience, the task's focus flag, and the reading order.

Follows the algorithm spec's "Tiers and routing" and "Focus" sections. Every rule, policy
rules included, takes its tier from the table (or a `.review.toml` override); routing only
ever lowers a tier, never raises one.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from revgate.config import Config, ConfigError, glob_match
from revgate.model import Finding, Grade, Source, TaskScope, Tier
from revgate.rules.registry import RuleSpec, TierTable, spec_for

PRIOR: Mapping[Grade, float] = MappingProxyType(
    {
        Grade.E0_EXECUTED: 0.95,
        Grade.E1_EXACT: 0.90,
        Grade.E2_STRUCTURAL: 0.60,
        Grade.E3_HEURISTIC: 0.25,
        Grade.E4_CONTEXT: 0.0,
    }
)

_TIER_ORDER = {Tier.BLOCKING: 0, Tier.ADVISORY: 1, Tier.SHADOW: 2}
_IMPACT_ORDER = {"critical": 0, "important": 1, "minor": 2}


def sort_key(f: Finding) -> tuple[object, ...]:
    """Tier, impact, descending prior by grade, then file, line, rule, and id."""
    return (
        _TIER_ORDER[f.tier],
        _IMPACT_ORDER[f.impact],
        -PRIOR[f.grade],
        f.file,
        f.line,
        f.rule,
        f.id,
    )


def in_task_scope(f: Finding, scope: TaskScope) -> bool:
    """A failed gate and every wave rule belong to whoever runs them; otherwise the file must
    be owned, run, or ledger-assigned to the task."""
    if f.rule == "gate.failed" or f.rule.startswith("wave."):
        return True
    return scope.covers(f.file)


def _spec(rule: str, tiers: TierTable) -> RuleSpec | None:
    try:
        return spec_for(rule, tiers)
    except KeyError:
        return None


def _override(f: Finding, cfg: Config, key: str) -> str | None:
    return cfg.rule_overrides.get(f.rule, {}).get(key)


def tier_of(f: Finding, tiers: TierTable, cfg: Config, scope: TaskScope) -> Tier:
    """The spec's `tier_of`. A rule the table doesn't know starts in shadow."""
    if cfg.is_default and f.rule != "gate.failed":
        return Tier.SHADOW  # no .review.toml: calibration mode
    override = _override(f, cfg, "tier")
    if override is not None:
        try:
            t = Tier(override)
        except ValueError as exc:
            raise ConfigError(f"[rules] {f.rule!r} has an unknown tier {override!r}") from exc
    else:
        spec = _spec(f.rule, tiers)
        t = spec.tier if spec is not None else Tier.SHADOW
    if t is Tier.BLOCKING and f.grade > Grade.E1_EXACT:
        t = Tier.ADVISORY  # unconfirmed structural evidence never blocks
    if t is Tier.BLOCKING and (
        f.impact == "minor" or not in_task_scope(f, scope) or f.status == "deferred"
    ):
        t = Tier.ADVISORY
    if f.source is Source.STATISTICAL and t is Tier.BLOCKING:
        t = Tier.ADVISORY  # statistical rules stop at advisory
    return t


def audience_of(f: Finding, scope: TaskScope, *, disputed: bool = False) -> frozenset[str]:
    """The spec's `audience_of`, for an already tiered finding.

    An explained finding is hidden from everyone but the log. A disputed finding also goes
    to the controller, who adjudicates it; it keeps every other reader.
    """
    a = {"log"}
    if f.status == "explained" or f.tier is Tier.SHADOW:
        return frozenset(a)
    if not in_task_scope(f, scope) or f.status == "deferred":
        return frozenset(a | {"controller"})  # never this implementer
    if f.tier is Tier.BLOCKING or f.grade <= Grade.E1_EXACT:
        a.add("implementer")  # blocking, or the advisory channel
    if f.review or f.status in ("acknowledged", "suppressed") or f.tier is Tier.BLOCKING:
        a.add("wave")
    if disputed:
        a.add("controller")
    return frozenset(a)


def _review_of(f: Finding, tiers: TierTable, cfg: Config) -> bool:
    override = _override(f, cfg, "review")
    if override is not None:
        if override.lower() not in ("true", "false"):
            raise ConfigError(f"[rules] {f.rule!r} review must be true or false")
        return override.lower() == "true"
    spec = _spec(f.rule, tiers)
    return spec.review if spec is not None else False


def route(
    f: Finding, tiers: TierTable, cfg: Config, scope: TaskScope, *, disputed: bool = False
) -> Finding:
    """Set the finding's tier, its `review` attribute from the table, and its audience."""
    tiered = dataclasses.replace(
        f, tier=tier_of(f, tiers, cfg, scope), review=_review_of(f, tiers, cfg)
    )
    return dataclasses.replace(tiered, audience=audience_of(tiered, scope, disputed=disputed))


@dataclass(frozen=True)
class FocusInputs:
    """The facts about a task's diff that the focus conditions read."""

    risk: str | None
    changed_paths: tuple[str, ...]
    nontest_changed_lines: int
    suppression_added: bool
    review_toml_edited: bool


def focus_flags(
    findings: Sequence[Finding], inputs: FocusInputs, cfg: Config
) -> tuple[bool, tuple[str, ...]]:
    """The parent's focus conditions, each as a reason string, in a fixed order."""
    reasons: list[str] = []
    if inputs.risk == "high":
        reasons.append("risk: high")
    risk_globs = tuple(cfg.risk_paths)
    reasons.extend(
        f"risk.paths:{path}"
        for path in sorted(set(inputs.changed_paths))
        if risk_globs and glob_match(path, risk_globs)
    )
    review_rules = sorted(
        {
            f.rule
            for f in findings
            if f.review and f.tier is Tier.ADVISORY and f.status in ("open", "acknowledged")
        }
    )
    reasons.extend(f"review rule {rule}" for rule in review_rules)
    if inputs.suppression_added:
        reasons.append("suppression added")
    if inputs.review_toml_edited:
        reasons.append(".review.toml edited")
    if inputs.nontest_changed_lines > cfg.max_lines:
        reasons.append(f"diff {inputs.nontest_changed_lines} > max_lines {cfg.max_lines}")
    return bool(reasons), tuple(reasons)
