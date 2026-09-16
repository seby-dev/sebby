from __future__ import annotations

from pydantic_settings import SettingsConfigDict

from sebby.config import Settings, get_settings, resolve_dotenv_path


def test_resolve_dotenv_path_picks_env_specific_file(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "staging")
    (tmp_path / ".env.staging").write_text("X=1\n")
    (tmp_path / ".env").write_text("X=2\n")

    assert resolve_dotenv_path(base_dir=tmp_path) == tmp_path / ".env.staging"


def test_resolve_dotenv_path_falls_back_to_plain_env(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "staging")
    (tmp_path / ".env").write_text("X=2\n")

    assert resolve_dotenv_path(base_dir=tmp_path) == tmp_path / ".env"


def test_resolve_dotenv_path_returns_none_when_neither_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "staging")

    assert resolve_dotenv_path(base_dir=tmp_path) is None


def test_resolve_dotenv_path_defaults_to_development_when_app_env_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("APP_ENV", raising=False)
    (tmp_path / ".env.development").write_text("X=1\n")

    assert resolve_dotenv_path(base_dir=tmp_path) == tmp_path / ".env.development"


def test_settings_subclass_loads_values_from_dotenv(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    (tmp_path / ".env.test").write_text("NAME=from-dotenv\n")

    class MySettings(Settings):
        model_config = SettingsConfigDict(
            env_file=resolve_dotenv_path(base_dir=tmp_path), extra="ignore"
        )
        name: str = "default"

    settings = MySettings()
    assert settings.name == "from-dotenv"


def test_settings_subclass_ignores_ambient_os_environ(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("NAME", "from-ambient-env")
    (tmp_path / ".env.test").write_text("NAME=from-dotenv\n")

    class MySettings(Settings):
        model_config = SettingsConfigDict(
            env_file=resolve_dotenv_path(base_dir=tmp_path), extra="ignore"
        )
        name: str = "default"

    settings = MySettings()
    assert settings.name == "from-dotenv"


def test_settings_subclass_accepts_init_kwargs_override(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")

    class MySettings(Settings):
        model_config = SettingsConfigDict(env_file=None, extra="ignore")
        name: str = "default"

    settings = MySettings(name="explicit")
    assert settings.name == "explicit"


def test_get_settings_returns_same_instance_each_call(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")

    class MySettings(Settings):
        model_config = SettingsConfigDict(env_file=None, extra="ignore")
        name: str = "default"

    first = get_settings(MySettings)
    second = get_settings(MySettings)
    assert first is second
