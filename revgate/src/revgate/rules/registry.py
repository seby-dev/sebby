"""Rule metadata: the starting tier table and the protocol every static rule implements."""

from __future__ import annotations

import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from importlib.resources import files
from typing import Any, Protocol, runtime_checkable

from revgate.model import Grade, RuleOutput, Source, Tier

TIER_TABLE_RESOURCE = "starting_tiers.toml"


@dataclass(frozen=True)
class RuleSpec:
    id: str
    stage: str
    langs: tuple[str, ...]
    grade: Grade
    source: Source
    tier: Tier
    review: bool
    proven: str | None
    context: bool = False


TierTable = Mapping[str, RuleSpec]


def _spec_from_row(row: Mapping[str, Any]) -> RuleSpec:
    try:
        return RuleSpec(
            id=str(row["id"]),
            stage=str(row["stage"]),
            langs=tuple(str(lang) for lang in row["langs"]),
            grade=Grade(int(row["grade"])),
            source=Source(str(row["source"])),
            tier=Tier(str(row["tier"])),
            review=bool(row["review"]),
            proven=str(row["proven"]) if "proven" in row else None,
            context=bool(row.get("context", False)),
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise ValueError(f"bad rule row {dict(row)!r}: {exc}") from exc


def load_tier_table() -> dict[str, RuleSpec]:
    """Appendix A's starting tiers, in the appendix's order."""
    text = files("revgate.rules").joinpath(TIER_TABLE_RESOURCE).read_text(encoding="utf-8")
    table: dict[str, RuleSpec] = {}
    for row in tomllib.loads(text).get("rule", []):
        spec = _spec_from_row(row)
        if spec.id in table:
            raise ValueError(f"rule {spec.id!r} appears twice in {TIER_TABLE_RESOURCE}")
        table[spec.id] = spec
    return table


def spec_for(rule_id: str, table: TierTable) -> RuleSpec:
    """The rule's own entry, else the nearest templated family entry.

    `lesson.x` falls back to `lesson.<id>`, and `family.a.b` tries `family.a.<id>` before
    `family.<id>`. Raises KeyError when nothing matches.
    """
    if rule_id in table:
        return table[rule_id]
    parts = rule_id.split(".")
    for i in range(len(parts) - 1, 0, -1):
        candidate = ".".join(parts[:i]) + ".<id>"
        if candidate in table:
            return table[candidate]
    raise KeyError(rule_id)


@runtime_checkable
class StaticRule(Protocol):
    """A static detector. One detector can own several split-tier rule ids.

    `ctx` is the static context (`StaticCtx`, which arrives with the change model).
    """

    ids: frozenset[str]
    languages: frozenset[str]

    def check(self, ctx: Any) -> Iterable[RuleOutput]: ...
