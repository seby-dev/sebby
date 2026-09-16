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


def test_setup_logging_scrubs_and_timestamps_stdlib_originated_records(tmp_path):
    """Records from plain stdlib `logging` (as used by third-party libraries
    like litellm/httpx) are "foreign" to structlog and bypass
    `structlog.configure(processors=[...])` entirely — they only get scrubbed
    and timestamped if `foreign_pre_chain` runs the shared processors on
    them too. This proves that wiring, using `logging.getLogger(...)`
    directly rather than `structlog.get_logger()`.
    """
    stream = io.StringIO()
    log_file = tmp_path / "app.log"
    setup_logging(
        console_stream=stream,
        json_file=log_file,
        scrub_pattern=re.compile(r"sk-[A-Za-z0-9]+"),
        scrub_replacement="***REDACTED***",
    )

    logging.getLogger("some.third.party.lib").warning("using key sk-abc123XYZ")

    console_output = stream.getvalue()
    assert "sk-abc123XYZ" not in console_output
    assert "***REDACTED***" in console_output

    lines = log_file.read_text().strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert "sk-abc123XYZ" not in record["event"]
    assert "***REDACTED***" in record["event"]
    assert "timestamp" in record
    assert record["level"] == "warning"
