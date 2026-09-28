"""`.review.toml` loading. This module starts with the gate cache's section only."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

_AGE_RE = re.compile(r"^(\d+)([smhd])$")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


class ConfigError(Exception):
    """.review.toml is malformed."""


@dataclass(frozen=True)
class GateCacheConfig:
    env_allowlist: tuple[str, ...] = ()
    max_age_s: int = 86_400


def parse_age(text: str) -> int:
    match = _AGE_RE.match(text.strip())
    if match is None:
        raise ConfigError(f"bad age {text!r}; use a number followed by s, m, h, or d")
    return int(match.group(1)) * _UNITS[match.group(2)]


def load_gate_cache_config(top: Path) -> GateCacheConfig:
    path = top / ".review.toml"
    if not path.exists():
        return GateCacheConfig()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    section = data.get("gates", {}).get("cache", {})
    allow = tuple(str(v) for v in section.get("env_allowlist", ()))
    max_age = parse_age(str(section["max_age"])) if "max_age" in section else 86_400
    return GateCacheConfig(env_allowlist=allow, max_age_s=max_age)
