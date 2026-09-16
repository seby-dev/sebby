from __future__ import annotations

from typing import Any

MAX_CACHE_BREAKPOINTS = 4
_CACHE_CONTROL = {"type": "ephemeral"}


class TooManyCacheBreakpointsError(Exception):
    pass


def _as_content_blocks(content: str | list[dict[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return list(content)


def mark_cache_breakpoint(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a shallow copy of `messages` with a cache_control breakpoint on
    the last message only. Never mutates the input list or its entries.

    Anthropic allows at most `MAX_CACHE_BREAKPOINTS` cache_control
    breakpoints per request; this function only ever adds one, so callers
    may combine its result with breakpoints added elsewhere (e.g. on tools)
    up to that cap.
    """
    if not messages:
        return []

    *head, last = messages
    blocks = _as_content_blocks(last["content"])
    if not blocks:
        return [*head, last]

    marked_blocks = [*blocks[:-1], {**blocks[-1], "cache_control": dict(_CACHE_CONTROL)}]
    marked_last = {**last, "content": marked_blocks}
    return [*head, marked_last]


def mark_cache_breakpoint_on_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a shallow copy of `tools` with a cache_control breakpoint on
    the last tool definition only. Never mutates the input.
    """
    if not tools:
        return []

    *head, last = tools
    marked_last = {**last, "cache_control": dict(_CACHE_CONTROL)}
    return [*head, marked_last]


def count_cache_breakpoints(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]]
) -> int:
    """Count cache_control breakpoints already present, so callers can check
    against Anthropic's per-request cap before adding more.
    """
    count = 0
    for message in messages:
        for block in _as_content_blocks(message.get("content", "")):
            if "cache_control" in block:
                count += 1
    for tool in tools:
        if "cache_control" in tool:
            count += 1
    return count


def assert_within_breakpoint_cap(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]]
) -> None:
    count = count_cache_breakpoints(messages, tools)
    if count > MAX_CACHE_BREAKPOINTS:
        raise TooManyCacheBreakpointsError(
            f"{count} cache_control breakpoints exceeds Anthropic's cap of "
            f"{MAX_CACHE_BREAKPOINTS}"
        )
