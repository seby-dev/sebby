from __future__ import annotations

import re
from typing import Any, Protocol

_MARKDOWN_V2_SPECIAL_CHARS = "\\" + r"_*[]()~`>#+-=|{}.!"


def escape_markdown_v2(text: str) -> str:
    """Escape a string for Telegram's MarkdownV2 parse mode.

    Escapes every character MarkdownV2 treats as special, including the
    backslash itself (`\\_*[]()~`>#+-=|{}.!`), per Telegram's Bot API
    documentation.
    """
    return re.sub(f"([{re.escape(_MARKDOWN_V2_SPECIAL_CHARS)}])", r"\\\1", text)


class HttpPostFn(Protocol):
    def __call__(self, url: str, *, json: dict[str, Any]) -> Any: ...


def send_telegram_alert(
    message: str,
    *,
    bot_token: str,
    chat_id: str,
    post_fn: HttpPostFn,
    escape: bool = True,
) -> Any:
    """Send `message` to a Telegram chat via the Bot API's `sendMessage`
    endpoint, MarkdownV2-escaped by default. Returns whatever `post_fn`
    returns, so the caller can check/raise on failure.

    `post_fn` is injected rather than this module importing an HTTP client
    directly — pass e.g. a wrapper that raises on non-2xx:

        def post_and_raise(url, *, json):
            response = httpx.post(url, json=json, timeout=10)
            response.raise_for_status()
            return response

    This keeps `sebby.notify` dependency-free and network-call-free in
    tests. Note: the bot token appears in the request URL — if your
    `post_fn` logs request URLs, redact it there.
    """
    text = escape_markdown_v2(message) if escape else message
    return post_fn(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "MarkdownV2"},
    )
