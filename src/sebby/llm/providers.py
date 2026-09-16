from __future__ import annotations

from dataclasses import dataclass


class UnknownProviderError(Exception):
    pass


class UnknownEffortError(Exception):
    pass


VALID_EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high")

PROVIDER_API_KEY_ENV: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
}


@dataclass(frozen=True)
class ProviderConfig:
    """One provider's model choice and effort level.

    Model IDs are supplied by the caller rather than hardcoded here, since
    they go stale independently of this library's release cycle.
    """

    provider: str
    model: str
    effort: str | None = None

    def __post_init__(self) -> None:
        if self.provider not in PROVIDER_API_KEY_ENV:
            raise UnknownProviderError(
                f"unknown provider: {self.provider!r}; "
                f"expected one of {tuple(PROVIDER_API_KEY_ENV)}"
            )
        if self.effort is not None and self.effort not in VALID_EFFORT_LEVELS:
            raise UnknownEffortError(
                f"invalid effort {self.effort!r}; expected one of {VALID_EFFORT_LEVELS}"
            )

    def api_key_env_var(self) -> str:
        return PROVIDER_API_KEY_ENV[self.provider]

    def litellm_model_string(self) -> str:
        return f"{self.provider}/{self.model}"
