from __future__ import annotations

import pytest

from sebby.llm.cache import (
    MAX_CACHE_BREAKPOINTS,
    TooManyCacheBreakpointsError,
    assert_within_breakpoint_cap,
    count_cache_breakpoints,
    mark_cache_breakpoint,
    mark_cache_breakpoint_on_tools,
)


def test_mark_cache_breakpoint_adds_cache_control_to_last_message_only() -> None:
    messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
        {"role": "user", "content": "third"},
    ]

    result = mark_cache_breakpoint(messages)

    assert result[0]["content"] == "first"
    assert result[1]["content"] == "second"
    assert result[2]["content"] == [
        {"type": "text", "text": "third", "cache_control": {"type": "ephemeral"}}
    ]


def test_mark_cache_breakpoint_does_not_mutate_input() -> None:
    messages = [{"role": "user", "content": "only message"}]

    mark_cache_breakpoint(messages)

    assert messages == [{"role": "user", "content": "only message"}]


def test_mark_cache_breakpoint_handles_empty_list() -> None:
    assert mark_cache_breakpoint([]) == []


def test_mark_cache_breakpoint_on_tools_marks_last_tool_only() -> None:
    tools = [{"name": "search"}, {"name": "fetch"}]

    result = mark_cache_breakpoint_on_tools(tools)

    assert result[0] == {"name": "search"}
    assert result[1] == {"name": "fetch", "cache_control": {"type": "ephemeral"}}


def test_mark_cache_breakpoint_on_tools_does_not_mutate_input() -> None:
    tools = [{"name": "search"}]

    mark_cache_breakpoint_on_tools(tools)

    assert tools == [{"name": "search"}]


def test_mark_cache_breakpoint_on_tools_handles_empty_list() -> None:
    assert mark_cache_breakpoint_on_tools([]) == []


def test_count_cache_breakpoints_counts_messages_and_tools() -> None:
    messages = [
        {
            "role": "user",
            "content": [{"type": "text", "text": "a", "cache_control": {"type": "ephemeral"}}],
        },
    ]
    tools = [{"name": "search", "cache_control": {"type": "ephemeral"}}]

    assert count_cache_breakpoints(messages, tools) == 2


def test_assert_within_breakpoint_cap_raises_when_exceeded() -> None:
    block = {"type": "text", "text": "x", "cache_control": {"type": "ephemeral"}}
    messages = [{"role": "user", "content": [block]} for _ in range(MAX_CACHE_BREAKPOINTS + 1)]

    with pytest.raises(TooManyCacheBreakpointsError):
        assert_within_breakpoint_cap(messages, [])


def test_assert_within_breakpoint_cap_passes_at_exactly_the_cap() -> None:
    block = {"type": "text", "text": "x", "cache_control": {"type": "ephemeral"}}
    messages = [{"role": "user", "content": [block]} for _ in range(MAX_CACHE_BREAKPOINTS)]

    assert_within_breakpoint_cap(messages, [])  # must not raise


def test_mark_cache_breakpoint_handles_none_content_without_crashing() -> None:
    # Standard shape for an assistant turn that only makes tool calls.
    messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
    ]

    result = mark_cache_breakpoint(messages)

    assert result[0]["content"] == "first"
    # Nothing to mark on a None-content message: it comes back unchanged,
    # not crashed, and not mutated in place.
    assert result[1] == {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]}
    assert result[1] is not messages[1]


def test_mark_cache_breakpoint_handles_missing_content_key_without_crashing() -> None:
    messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "tool_calls": [{"id": "1"}]},
    ]

    result = mark_cache_breakpoint(messages)

    assert result[0]["content"] == "first"
    assert result[1] == {"role": "assistant", "tool_calls": [{"id": "1"}]}
    assert "content" not in result[1]


def test_count_cache_breakpoints_handles_none_content() -> None:
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
    ]

    assert count_cache_breakpoints(messages, []) == 0
