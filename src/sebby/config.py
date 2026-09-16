from __future__ import annotations

import os
from pathlib import Path
from typing import Any, TypeVar

from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

T = TypeVar("T", bound="Settings")

_APP_ENV_VAR = "APP_ENV"
_DEFAULT_APP_ENV = "development"


def resolve_dotenv_path(
    *,
    app_env_var: str = _APP_ENV_VAR,
    default_env: str = _DEFAULT_APP_ENV,
    base_dir: Path | None = None,
) -> Path | None:
    """Pick a dotenv file based on the `APP_ENV` environment variable.

    Looks for `.env.<APP_ENV>` (default env name: "development") in
    `base_dir` (defaults to the current working directory), falling back to
    a plain `.env`. Returns None if neither exists.
    """
    base = base_dir or Path.cwd()
    env_name = os.environ.get(app_env_var, default_env)
    candidate = base / f".env.{env_name}"
    if candidate.exists():
        return candidate
    fallback = base / ".env"
    if fallback.exists():
        return fallback
    return None


class Settings(BaseSettings):
    """Base class for environment-driven configuration.

    Subclass this and declare fields as usual for `pydantic-settings`. The
    dotenv file is selected once, at class-definition time, via
    `resolve_dotenv_path()` — set `APP_ENV` (and create `.env.<APP_ENV>`)
    before importing your subclass, not after. Deliberately excludes raw
    OS environment variables as a settings source (only `init` kwargs and
    the dotenv file are read) so ambient shell variables can't silently
    override configuration — only `APP_ENV` itself is read directly from
    `os.environ`, to pick which dotenv file to load.
    """

    model_config = SettingsConfigDict(env_file=resolve_dotenv_path(), extra="ignore")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings, dotenv_settings)


_singletons: dict[type[Any], Any] = {}


def get_settings(settings_cls: type[T]) -> T:
    """Return a process-wide singleton instance of `settings_cls`.

    Keyed by class object, so distinct `Settings` subclasses each get their
    own singleton; the same subclass always returns the same instance.
    """
    if settings_cls not in _singletons:
        _singletons[settings_cls] = settings_cls()
    instance: T = _singletons[settings_cls]
    return instance
