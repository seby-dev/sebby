from __future__ import annotations

import logging
import logging.handlers
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

import structlog


def _scrub_processor(
    pattern: re.Pattern[str], replacement: str
) -> Callable[[Any, str, dict[str, Any]], dict[str, Any]]:
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
    might otherwise end up in logs. This applies to events logged through
    structlog AND to "foreign" records from stdlib `logging` (which is what
    third-party libraries such as litellm and httpx use) — both paths run
    the same shared processors, including the scrub step, before
    rendering.

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
        handler.close()
        root_logger.removeHandler(handler)

    console_handler = logging.StreamHandler(console_stream)
    console_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=structlog.dev.ConsoleRenderer(),
            foreign_pre_chain=shared_processors,
        )
    )
    root_logger.addHandler(console_handler)

    if json_file is not None:
        file_path = Path(json_file)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            file_path, maxBytes=10_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                processor=structlog.processors.JSONRenderer(),
                foreign_pre_chain=shared_processors,
            )
        )
        root_logger.addHandler(file_handler)

    if sentry_dsn is not None:
        try:
            import sentry_sdk  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ImportError(
                "setup_logging(sentry_dsn=...) requires sentry-sdk: "
                "install with `uv add sentry-sdk`."
            ) from exc
        sentry_sdk.init(dsn=sentry_dsn)
