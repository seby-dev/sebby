# Sebby Core Utilities Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add five small, independent `sebby` modules (`storage`, `cache`, `cli`, `config`, `logging`) plus shared lint/CI config artifacts, continuing the spec's phase 2 rollout.

**Architecture:** Each module is a single file directly under `src/sebby/`, stdlib-only except where noted, independently importable, with any real dependency isolated to an optional extra (matching the `llm` extra precedent from the previous plan). Shared config artifacts are plain files (`configs/ruff.toml`, `configs/mypy.ini`) other repos point their own tooling at, plus a `workflow_call`-enabled GitHub Actions workflow other repos can invoke with `uses:`.

**Tech Stack:** Python 3.11+, `uv`, `pydantic-settings` (via the new `config` extra), `structlog` (via the new `logging` extra), `pytest`, `ruff`, `mypy` (strict).

**Spec:** `docs/superpowers/specs/2026-09-16-shared-toolkit-design.md`

**Note on scope:** This is plan 2 of several (plan 1 shipped `sebby.retry` and `sebby.llm`, already merged to `main`). It covers only the small, self-contained modules from the spec's phase 2 step. `sebby.http` and `sebby.notify` are deliberately excluded — they're meaningfully more complex (FastAPI, async) and get their own plan. The Claude Code plugin, the `organist_bot` pilot migration, and the CLAUDE.md de-duplication each get their own plan later still.

## Global Constraints

- Package is importable as `sebby`; every submodule must also be independently importable, and a module's own runtime dependency (e.g. `structlog` for logging, `pydantic-settings` for config) must not become a hard dependency for the whole package — use an optional extra, the way `litellm` was moved to an `llm` extra in the previous plan.
- Every constructor/function that has a natural seam for testability (a clock, a stream, an output sink) must expose it as a parameter rather than hardcoding it — this is what let the previous plan's tests avoid real sleeps and real I/O where possible.
- All tests use pytest; file-based tests use pytest's `tmp_path` fixture (real local file I/O is fine — it's *network* calls and *sleeps* that tests must avoid); no real network calls, no real Sentry/API calls.
- Base `dependencies` in `pyproject.toml` stays `[]`; each optional capability lives in its own extra, and `dev` depends on every extra it needs to run the full test suite (self-referential extras, e.g. `"sebby[config]"`, following the `llm` extra's precedent).
- New public classes/dataclasses get a docstring noting any non-obvious semantics (not what the code does — why it's built that way, or a gotcha a caller needs to know).

---

### Task 1: `sebby.storage`

**Files:**
- Create: `src/sebby/storage.py`
- Create: `tests/test_storage.py`

**Interfaces:**
- Consumes: nothing (stdlib only: `json`, `os`, `tempfile`, `pathlib`, `fcntl` where available)
- Produces: `AtomicJSONStore(path: str | Path)` with `.read(default: Any = None) -> Any` and `.write(data: Any) -> None`

- [ ] **Step 1: Write the failing tests**

`tests/test_storage.py`:

```python
from __future__ import annotations

import pytest

from sebby.storage import AtomicJSONStore


def test_write_then_read_roundtrips_data(tmp_path):
    store = AtomicJSONStore(tmp_path / "data.json")
    store.write({"a": 1, "b": [1, 2, 3]})
    assert store.read() == {"a": 1, "b": [1, 2, 3]}


def test_read_returns_default_when_file_missing(tmp_path):
    store = AtomicJSONStore(tmp_path / "missing.json")
    assert store.read(default={"empty": True}) == {"empty": True}


def test_read_returns_none_default_when_file_missing_and_no_default_given(tmp_path):
    store = AtomicJSONStore(tmp_path / "missing.json")
    assert store.read() is None


def test_write_creates_parent_directories(tmp_path):
    store = AtomicJSONStore(tmp_path / "nested" / "dir" / "data.json")
    store.write({"x": 1})
    assert store.read() == {"x": 1}


def test_write_does_not_corrupt_existing_file_on_serialization_error(tmp_path):
    path = tmp_path / "data.json"
    store = AtomicJSONStore(path)
    store.write({"good": "data"})

    class Unserializable:
        pass

    with pytest.raises(TypeError):
        store.write({"bad": Unserializable()})

    assert store.read() == {"good": "data"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/sebby/Developer/sebby && uv run pytest tests/test_storage.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'sebby.storage'`

- [ ] **Step 3: Implement `src/sebby/storage.py`**

```python
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no fcntl
    fcntl = None  # type: ignore[assignment]


class AtomicJSONStore:
    """A file-backed JSON store with atomic, file-locked writes.

    Writes go to a temp file in the same directory, then `os.replace` swaps
    it into place — a reader never observes a partially-written file. On
    POSIX, writes and reads also take an `fcntl` file lock so concurrent
    processes don't interleave; on platforms without `fcntl` (e.g. Windows),
    the atomic-rename guarantee alone still prevents torn reads.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def read(self, default: Any = None) -> Any:
        if not self.path.exists():
            return default
        with self.path.open("r", encoding="utf-8") as f:
            if fcntl is not None:
                fcntl.flock(f.fileno(), fcntl.LOCK_SH)
            try:
                return json.load(f)
            finally:
                if fcntl is not None:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    def write(self, data: Any) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                if fcntl is not None:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, self.path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/sebby/Developer/sebby && uv run pytest tests/test_storage.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Run lint and type checks**

Run: `cd /Users/sebby/Developer/sebby && uv run ruff check . && uv run mypy src`
Expected: both clean

- [ ] **Step 6: Commit**

```bash
git add src/sebby/storage.py tests/test_storage.py
git commit -m "feat: add sebby.storage.AtomicJSONStore"
```

---

### Task 2: `sebby.cache`

**Files:**
- Create: `src/sebby/cache.py`
- Create: `tests/test_cache.py`

**Interfaces:**
- Consumes: nothing (stdlib only)
- Produces: `TTLCache(ttl_seconds: float, *, clock: Callable[[], float] = time.monotonic)` with `.get(key: str) -> T | None`, `.set(key: str, value: T) -> None`, `.clear() -> None`, `__len__`

Note: this is unrelated to `sebby.llm.cache` (the Anthropic prompt-cache breakpoint logic from the previous plan) — same word, different module, different purpose. Don't confuse the two.

- [ ] **Step 1: Write the failing tests**

`tests/test_cache.py`:

```python
from __future__ import annotations

from sebby.cache import TTLCache


def test_get_returns_none_for_missing_key():
    cache: TTLCache[str] = TTLCache(ttl_seconds=60)
    assert cache.get("missing") is None


def test_set_then_get_returns_value_before_expiry():
    clock = {"t": 0.0}
    cache: TTLCache[str] = TTLCache(ttl_seconds=10, clock=lambda: clock["t"])
    cache.set("key", "value")
    clock["t"] = 5.0
    assert cache.get("key") == "value"


def test_get_returns_none_after_expiry():
    clock = {"t": 0.0}
    cache: TTLCache[str] = TTLCache(ttl_seconds=10, clock=lambda: clock["t"])
    cache.set("key", "value")
    clock["t"] = 10.0
    assert cache.get("key") is None


def test_expired_entry_is_evicted_from_storage():
    clock = {"t": 0.0}
    cache: TTLCache[str] = TTLCache(ttl_seconds=10, clock=lambda: clock["t"])
    cache.set("key", "value")
    clock["t"] = 10.0
    cache.get("key")
    assert len(cache) == 0


def test_clear_removes_all_entries():
    cache: TTLCache[str] = TTLCache(ttl_seconds=60)
    cache.set("a", "1")
    cache.set("b", "2")
    cache.clear()
    assert cache.get("a") is None
    assert cache.get("b") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/sebby/Developer/sebby && uv run pytest tests/test_cache.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'sebby.cache'`

- [ ] **Step 3: Implement `src/sebby/cache.py`**

```python
from __future__ import annotations

import time
from typing import Callable, Generic, TypeVar

T = TypeVar("T")


class TTLCache(Generic[T]):
    """A trivial in-memory cache where each entry expires after a fixed TTL.

    `clock` defaults to `time.monotonic` but is injectable so tests can
    advance time deterministically instead of sleeping for real.
    """

    def __init__(self, ttl_seconds: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._entries: dict[str, tuple[float, T]] = {}

    def get(self, key: str) -> T | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if self._clock() >= expires_at:
            del self._entries[key]
            return None
        return value

    def set(self, key: str, value: T) -> None:
        self._entries[key] = (self._clock() + self._ttl, value)

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/sebby/Developer/sebby && uv run pytest tests/test_cache.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Run lint and type checks**

Run: `cd /Users/sebby/Developer/sebby && uv run ruff check . && uv run mypy src`
Expected: both clean

- [ ] **Step 6: Commit**

```bash
git add src/sebby/cache.py tests/test_cache.py
git commit -m "feat: add sebby.cache.TTLCache"
```

---

### Task 3: `sebby.cli`

**Files:**
- Create: `src/sebby/cli.py`
- Create: `tests/test_cli.py`

**Interfaces:**
- Consumes: nothing (stdlib only)
- Produces: `run_main(body: Callable[[], int | None], *, catch: tuple[type[Exception], ...] = (Exception,), stream: TextIO = sys.stderr) -> int`

- [ ] **Step 1: Write the failing tests**

`tests/test_cli.py`:

```python
from __future__ import annotations

import io

import pytest

from sebby.cli import run_main


def test_run_main_returns_zero_on_success_with_no_return_value():
    assert run_main(lambda: None) == 0


def test_run_main_returns_bodys_int_result():
    assert run_main(lambda: 42) == 42


def test_run_main_catches_listed_exception_prints_and_returns_one():
    stream = io.StringIO()

    def body() -> int | None:
        raise ValueError("bad input")

    result = run_main(body, catch=(ValueError,), stream=stream)

    assert result == 1
    assert "bad input" in stream.getvalue()


def test_run_main_lets_unlisted_exception_propagate():
    def body() -> int | None:
        raise KeyError("oops")

    with pytest.raises(KeyError):
        run_main(body, catch=(ValueError,), stream=io.StringIO())


def test_run_main_default_catch_is_broad_exception():
    stream = io.StringIO()

    def body() -> int | None:
        raise RuntimeError("boom")

    assert run_main(body, stream=stream) == 1
    assert "boom" in stream.getvalue()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/sebby/Developer/sebby && uv run pytest tests/test_cli.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'sebby.cli'`

- [ ] **Step 3: Implement `src/sebby/cli.py`**

```python
from __future__ import annotations

import sys
from typing import Callable, TextIO


def run_main(
    body: Callable[[], int | None],
    *,
    catch: tuple[type[Exception], ...] = (Exception,),
    stream: TextIO = sys.stderr,
) -> int:
    """Run `body`, catching exceptions in `catch`, printing them to `stream`.

    Returns `body`'s result (0 if it returns None) on success, or 1 if a
    caught exception was raised. Use as `sys.exit(run_main(main))` at a
    script's entry point — each stage should raise a domain-specific
    exception rather than calling `sys.exit` itself, so `run_main` is the
    single place that turns an error into a process exit code.
    """
    try:
        result = body()
    except catch as exc:
        print(f"error: {exc}", file=stream)
        return 1
    return result if result is not None else 0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/sebby/Developer/sebby && uv run pytest tests/test_cli.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Run lint and type checks**

Run: `cd /Users/sebby/Developer/sebby && uv run ruff check . && uv run mypy src`
Expected: both clean

- [ ] **Step 6: Commit**

```bash
git add src/sebby/cli.py tests/test_cli.py
git commit -m "feat: add sebby.cli.run_main"
```

---

### Task 4: `sebby.config`

**Files:**
- Modify: `pyproject.toml` (add a `config` optional-dependency group, add it to `dev`)
- Create: `src/sebby/config.py`
- Create: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing from earlier tasks in this plan
- Produces: `resolve_dotenv_path(*, app_env_var: str = "APP_ENV", default_env: str = "development", base_dir: Path | None = None) -> Path | None`, `Settings` (a `pydantic_settings.BaseSettings` subclass), `get_settings(settings_cls: type[T]) -> T`

- [ ] **Step 1: Add the `config` extra to `pyproject.toml`**

Edit the `[project.optional-dependencies]` table to add a `config` group and include it in `dev`:

```toml
[project.optional-dependencies]
llm = [
    "litellm>=1.50.0",
]
config = [
    "pydantic-settings>=2.0.0",
]
dev = [
    "sebby[llm]",
    "sebby[config]",
    "pytest>=8.0.0",
    "ruff>=0.6.0",
    "mypy>=1.11.0",
]
```

- [ ] **Step 2: Sync the environment**

Run: `cd /Users/sebby/Developer/sebby && uv sync --extra dev`
Expected: installs `pydantic-settings` and its dependencies with no errors

- [ ] **Step 3: Write the failing tests**

`tests/test_config.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `cd /Users/sebby/Developer/sebby && uv run pytest tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'sebby.config'`

- [ ] **Step 5: Implement `src/sebby/config.py`**

```python
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
    return _singletons[settings_cls]
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd /Users/sebby/Developer/sebby && uv run pytest tests/test_config.py -v`
Expected: PASS (8 passed)

- [ ] **Step 7: Run lint and type checks**

Run: `cd /Users/sebby/Developer/sebby && uv run ruff check . && uv run mypy src`
Expected: both clean

- [ ] **Step 8: Run the full suite to confirm no regressions**

Run: `cd /Users/sebby/Developer/sebby && uv run pytest`
Expected: all previous tests plus these 8 pass

- [ ] **Step 9: Commit**

```bash
git add pyproject.toml uv.lock src/sebby/config.py tests/test_config.py
git commit -m "feat: add sebby.config.Settings and get_settings"
```

---

### Task 5: `sebby.logging`

**Files:**
- Modify: `pyproject.toml` (add a `logging` optional-dependency group, add it to `dev`)
- Create: `src/sebby/logging.py`
- Create: `tests/test_logging.py`

**Interfaces:**
- Consumes: nothing from earlier tasks in this plan
- Produces: `setup_logging(*, level: int = logging.INFO, console_stream: TextIO = sys.stderr, json_file: str | Path | None = None, scrub_pattern: re.Pattern[str] | None = None, scrub_replacement: str = "***", sentry_dsn: str | None = None) -> None`

Note: naming this file `logging.py` inside the `sebby` package is intentional and safe — `import logging` from *within* `src/sebby/logging.py` resolves to the stdlib `logging` package via Python 3's absolute-import semantics, not to itself. Sentry support is deliberately NOT bundled into the `logging` extra — `sentry-sdk` stays a fully separate, undeclared dependency a consumer installs themselves only if they want it; this also means `test_setup_logging_raises_clear_error_when_sentry_requested_but_not_installed` below is a real test against a real absence, not a mock.

- [ ] **Step 1: Add the `logging` extra to `pyproject.toml`**

Edit `[project.optional-dependencies]`:

```toml
[project.optional-dependencies]
llm = [
    "litellm>=1.50.0",
]
config = [
    "pydantic-settings>=2.0.0",
]
logging = [
    "structlog>=24.0.0",
]
dev = [
    "sebby[llm]",
    "sebby[config]",
    "sebby[logging]",
    "pytest>=8.0.0",
    "ruff>=0.6.0",
    "mypy>=1.11.0",
]
```

- [ ] **Step 2: Sync the environment**

Run: `cd /Users/sebby/Developer/sebby && uv sync --extra dev`
Expected: installs `structlog` with no errors

- [ ] **Step 3: Write the failing tests**

`tests/test_logging.py`:

```python
from __future__ import annotations

import io
import json
import logging
import re

import pytest
import structlog

from sebby.logging import setup_logging


def test_setup_logging_writes_readable_message_to_console_stream():
    stream = io.StringIO()
    setup_logging(console_stream=stream)

    structlog.get_logger().info("hello world", extra_field=1)

    output = stream.getvalue()
    assert "hello world" in output


def test_setup_logging_writes_json_lines_to_file(tmp_path):
    log_file = tmp_path / "app.log"
    setup_logging(console_stream=io.StringIO(), json_file=log_file)

    structlog.get_logger().info("json test", user_id=42)

    lines = log_file.read_text().strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event"] == "json test"
    assert record["user_id"] == 42
    assert "timestamp" in record


def test_setup_logging_scrubs_matching_secrets():
    stream = io.StringIO()
    setup_logging(
        console_stream=stream,
        scrub_pattern=re.compile(r"sk-[A-Za-z0-9]+"),
        scrub_replacement="***REDACTED***",
    )

    structlog.get_logger().info("using key sk-abc123XYZ")

    output = stream.getvalue()
    assert "sk-abc123XYZ" not in output
    assert "***REDACTED***" in output


def test_setup_logging_is_idempotent_does_not_stack_handlers():
    setup_logging(console_stream=io.StringIO())
    setup_logging(console_stream=io.StringIO())

    root = logging.getLogger()
    assert len(root.handlers) == 1


def test_setup_logging_raises_clear_error_when_sentry_requested_but_not_installed():
    with pytest.raises(ImportError, match="sentry-sdk"):
        setup_logging(console_stream=io.StringIO(), sentry_dsn="https://fake@sentry.example/1")
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `cd /Users/sebby/Developer/sebby && uv run pytest tests/test_logging.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'sebby.logging'`

- [ ] **Step 5: Implement `src/sebby/logging.py`**

```python
from __future__ import annotations

import logging
import logging.handlers
import re
import sys
from pathlib import Path
from typing import Any, TextIO

import structlog


def _scrub_processor(pattern: re.Pattern[str], replacement: str):
    def processor(logger: Any, method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        for key, value in list(event_dict.items()):
            if isinstance(value, str):
                event_dict[key] = pattern.sub(replacement, value)
        return event_dict

    return processor


def setup_logging(
    *,
    level: int = logging.INFO,
    console_stream: TextIO = sys.stderr,
    json_file: str | Path | None = None,
    scrub_pattern: re.Pattern[str] | None = None,
    scrub_replacement: str = "***",
    sentry_dsn: str | None = None,
) -> None:
    """Configure structlog + stdlib logging: a human-readable console
    stream and, optionally, a rotating JSON-lines file. Safe to call more
    than once — each call replaces the previous configuration (clears
    existing root-logger handlers) rather than stacking handlers.

    `scrub_pattern`, if given, is applied to every string field in every
    log event before it's rendered, replacing matches with
    `scrub_replacement` — use it to redact secrets (API keys, tokens) that
    might otherwise end up in logs.

    `sentry_dsn`, if given, initializes Sentry error tracking. Requires the
    `sentry-sdk` package (not bundled with sebby's `logging` extra — a
    consumer who wants Sentry installs it themselves) — raises `ImportError`
    with an actionable message if it isn't installed.
    """
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
    ]
    if scrub_pattern is not None:
        shared_processors.append(_scrub_processor(scrub_pattern, scrub_replacement))

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)

    console_handler = logging.StreamHandler(console_stream)
    console_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(processor=structlog.dev.ConsoleRenderer())
    )
    root_logger.addHandler(console_handler)

    if json_file is not None:
        file_path = Path(json_file)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            file_path, maxBytes=10_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(
            structlog.stdlib.ProcessorFormatter(processor=structlog.processors.JSONRenderer())
        )
        root_logger.addHandler(file_handler)

    if sentry_dsn is not None:
        try:
            import sentry_sdk
        except ImportError as exc:
            raise ImportError(
                "setup_logging(sentry_dsn=...) requires sentry-sdk: "
                "install with `uv add sentry-sdk`."
            ) from exc
        sentry_sdk.init(dsn=sentry_dsn)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd /Users/sebby/Developer/sebby && uv run pytest tests/test_logging.py -v`
Expected: PASS (5 passed)

If a test fails because of an unexpected `structlog` API mismatch (e.g. a renamed processor or config parameter), check the installed `structlog` version's docs for its stdlib-integration recipe and adapt the implementation to match — the intent above (structlog processors feeding stdlib logging handlers, one JSON-rendering, one console-rendering) is authoritative, not the exact API surface, since `structlog`'s API can shift between versions.

- [ ] **Step 7: Run lint and type checks**

Run: `cd /Users/sebby/Developer/sebby && uv run ruff check . && uv run mypy src`
Expected: both clean. If mypy complains about missing type stubs for `structlog`, add `[[tool.mypy.overrides]] module = "structlog.*" ignore_missing_imports = true` to `pyproject.toml`.

- [ ] **Step 8: Run the full suite to confirm no regressions**

Run: `cd /Users/sebby/Developer/sebby && uv run pytest`
Expected: all previous tests plus these 5 pass

- [ ] **Step 9: Commit**

```bash
git add pyproject.toml uv.lock src/sebby/logging.py tests/test_logging.py
git commit -m "feat: add sebby.logging.setup_logging"
```

---

### Task 6: Shared lint/CI config artifacts

**Files:**
- Create: `configs/ruff.toml`
- Create: `configs/mypy.ini`
- Modify: `.github/workflows/ci.yml` (add a `workflow_call` trigger so other repos can invoke this workflow with `uses:`)
- Modify: `README.md` (document how to use the shared configs and the reusable workflow)

**Interfaces:**
- Consumes: nothing
- Produces: nothing importable — these are files other repos point their own tooling at, not Python code

- [ ] **Step 1: Write `configs/ruff.toml`**

A standalone `ruff.toml` (not embedded in `pyproject.toml`) uses top-level table names without the `tool.ruff.` prefix:

```toml
line-length = 100
target-version = "py311"

[lint]
select = ["E", "F", "W", "I", "UP", "B"]
```

- [ ] **Step 2: Write `configs/mypy.ini`**

```ini
[mypy]
python_version = 3.11
strict = True
```

- [ ] **Step 3: Verify both configs are syntactically valid and actually usable**

Run: `cd /Users/sebby/Developer/sebby && uv run ruff check --config configs/ruff.toml . && uv run mypy --config-file configs/mypy.ini src`
Expected: both commands run without a config-parsing error (they should report the same clean result as the project's own `pyproject.toml`-embedded config, since the rules are identical)

- [ ] **Step 4: Add a `workflow_call` trigger to `.github/workflows/ci.yml`**

Replace the file's contents with:

```yaml
name: CI
on:
  push:
    branches: [main]
  pull_request:
  workflow_call:
    inputs:
      python-version:
        type: string
        default: "3.11"
      mypy-target:
        type: string
        default: "src"
      extra:
        type: string
        default: "dev"

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
      - run: uv sync --extra ${{ inputs.extra || 'dev' }}
      - run: uv run ruff check .
      - run: uv run ruff format --check .
      - run: uv run mypy ${{ inputs.mypy-target || 'src' }}
      - run: uv run pytest
```

This keeps CI running for pushes/PRs to this repo (using `dev`/`src` defaults, which match this repo's own layout) while also being callable from another repo's workflow via:

```yaml
jobs:
  ci:
    uses: seby-dev/sebby/.github/workflows/python-ci.yml@main
    with:
      mypy-target: organist_bot  # or whatever that repo's package directory is named
```

(When called this way, `actions/checkout@v4` checks out the *calling* repository by default, not `sebby` — so the job runs against the caller's own `pyproject.toml`, `dev` extra, and source tree.)

- [ ] **Step 5: Validate the workflow YAML**

Run: `cd /Users/sebby/Developer/sebby && uv run python -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))" 2>&1 || python3 -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))"`

If neither `yaml` module is available, visually double check the indentation and structure against the block above instead — don't add `pyyaml` as a new dependency just for this one-off check.

Expected: no parse error. Note in your task report that a *live* test of another repo actually calling this workflow via `uses:` is out of scope for this plan — flag it as a manual follow-up for the human to verify once a second repo tries it.

- [ ] **Step 6: Document the shared configs and reusable workflow in `README.md`**

Add a new section after the existing `## Modules` table:

```markdown
## Shared config

- `configs/ruff.toml` — the ruff config this repo uses, usable standalone via `ruff check --config path/to/sebby/configs/ruff.toml .` in another project, or copy the `[lint] select = [...]` block into that project's own `pyproject.toml`.
- `configs/mypy.ini` — likewise, via `mypy --config-file path/to/sebby/configs/mypy.ini src`, or copy the `[mypy]` block into that project's own config.
- `.github/workflows/ci.yml` is a reusable workflow other repos can call directly:

      jobs:
        ci:
          uses: seby-dev/sebby/.github/workflows/python-ci.yml@main
          with:
            mypy-target: your_package_dir
```

- [ ] **Step 7: Run the full suite one more time to confirm nothing broke**

Run: `cd /Users/sebby/Developer/sebby && make pre-push`
Expected: all steps pass

- [ ] **Step 8: Commit**

```bash
git add configs/ruff.toml configs/mypy.ini .github/workflows/ci.yml README.md
git commit -m "feat: add shared ruff/mypy config and reusable CI workflow"
```
