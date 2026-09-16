from __future__ import annotations

from typing import Any

from sebby.notify import escape_markdown_v2, send_telegram_alert


def test_escape_markdown_v2_escapes_all_special_chars():
    result = escape_markdown_v2(r"a_b*c[d](e)~f`g>h#i+j-k=l|m{n}o.p!q\r")

    assert result == r"a\_b\*c\[d\]\(e\)\~f\`g\>h\#i\+j\-k\=l\|m\{n\}o\.p\!q\\r"


def test_escape_markdown_v2_leaves_plain_text_unchanged():
    assert escape_markdown_v2("hello world 123") == "hello world 123"


def test_send_telegram_alert_posts_to_correct_url_with_escaped_text():
    calls: list[dict[str, Any]] = []

    def fake_post(url: str, *, json: dict[str, Any]) -> None:
        calls.append({"url": url, "json": json})

    send_telegram_alert(
        "price is $5 (was $10)",
        bot_token="TOKEN123",
        chat_id="chat-1",
        post_fn=fake_post,
    )

    assert len(calls) == 1
    assert calls[0]["url"] == "https://api.telegram.org/botTOKEN123/sendMessage"
    assert calls[0]["json"]["chat_id"] == "chat-1"
    assert calls[0]["json"]["parse_mode"] == "MarkdownV2"
    assert calls[0]["json"]["text"] == escape_markdown_v2("price is $5 (was $10)")


def test_send_telegram_alert_skips_escaping_when_escape_false():
    calls: list[dict[str, Any]] = []

    def fake_post(url: str, *, json: dict[str, Any]) -> None:
        calls.append({"url": url, "json": json})

    send_telegram_alert(
        "raw *text*",
        bot_token="TOKEN123",
        chat_id="chat-1",
        post_fn=fake_post,
        escape=False,
    )

    assert calls[0]["json"]["text"] == "raw *text*"


def test_send_telegram_alert_returns_post_fn_result():
    def fake_post(url: str, *, json: dict[str, Any]) -> str:
        return "fake-response"

    result = send_telegram_alert("hi", bot_token="T", chat_id="c", post_fn=fake_post)

    assert result == "fake-response"
