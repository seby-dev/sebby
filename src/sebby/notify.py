from __future__ import annotations

import re
from typing import Any, Protocol

_MARKDOWN_V2_SPECIAL_CHARS = r"_*[]()~`>#+-=|{}.!"


def escape_markdown_v2(text: str) -> str:
    """Escape a string for Telegram's MarkdownV2 parse mode.

    Escapes every character MarkdownV2 treats as special
    (`_*[]()~`>#+-=|{}.!`), per Telegram's Bot API documentation.
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
) -> None:
    """Send `message` to a Telegram chat via the Bot API's `sendMessage`
    endpoint, MarkdownV2-escaped by default.

    `post_fn` is injected rather than this module importing an HTTP client
    directly — pass e.g. `functools.partial(httpx.post, timeout=10)`. This
    keeps `sebby.notify` dependency-free and network-call-free in tests.
    """
    text = escape_markdown_v2(message) if escape else message
    post_fn(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "MarkdownV2"},
    )
