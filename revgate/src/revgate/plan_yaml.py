"""A parser for the fixed YAML subset that a plan's `plan-waves` block uses.

`revgate` takes no runtime dependencies, so it can't use PyYAML. The subset: block
mappings (`key: value`, or `key:` followed by an indented block or a sequence at the same
indentation), block sequences (`- item`, and `- key: value` starting a mapping inside a
sequence item), flow sequences `[a, b]` and flow mappings `{k: v}` on one line and
nestable, plain scalars (`-?\\d+` as `int`, `true`/`false` as `bool`, `null`/`~` as
`None`, anything else as `str`), single- and double-quoted strings, `#` comments at the
start of a line or after whitespace outside quotes, and blank lines. Anything else raises
`PlanYamlError` with the 1-based line it found the problem on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_INT_RE = re.compile(r"-?\d+")
_RESERVED_START = frozenset("|>&*!%@`")
_DQ_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "n": "\n", "t": "\t", "r": "\r", "0": "\0"}


class PlanYamlError(Exception):
    """The text isn't in the supported YAML subset."""

    def __init__(self, line: int, message: str) -> None:
        super().__init__(f"line {line}: {message}")
        self.line = line
        self.message = message


@dataclass
class _Line:
    number: int  # 1-based
    indent: int
    text: str  # without indentation, comment, or trailing whitespace


def parse_yaml_subset(text: str) -> object:
    """Parse `text`; an empty or comment-only document gives `None`."""
    lines = _logical_lines(text)
    if not lines:
        return None
    parser = _BlockParser(lines)
    value = parser.node(lines[0].indent)
    if parser.pos < len(lines):
        extra = lines[parser.pos]
        raise PlanYamlError(extra.number, "unexpected content after the document")
    return value


# --- lines --------------------------------------------------------------------------------


def _logical_lines(text: str) -> list[_Line]:
    out: list[_Line] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.lstrip(" \t")
        leading = raw[: len(raw) - len(stripped)]
        body = _strip_comment(stripped, number).rstrip()
        if not body:
            continue
        if "\t" in leading:
            raise PlanYamlError(number, "a tab in indentation")
        out.append(_Line(number, len(leading), body))
    return out


def _strip_comment(text: str, number: int) -> str:
    quote: str | None = None
    i = 0
    while i < len(text):
        ch = text[i]
        if quote is not None:
            if quote == '"' and ch == "\\":
                i += 2
                continue
            if ch == quote:
                if quote == "'" and text[i + 1 : i + 2] == "'":
                    i += 2
                    continue
                quote = None
        elif ch in "\"'" and (i == 0 or text[i - 1] in " \t[{,:"):
            quote = ch
        elif ch == "#" and (i == 0 or text[i - 1] in " \t"):
            return text[:i]
        i += 1
    return text


# --- block structure ----------------------------------------------------------------------


def _is_seq_item(text: str) -> bool:
    return text == "-" or text.startswith("- ")


def _split_key(text: str, number: int) -> tuple[str, str] | None:
    """`key: rest` at the top level of `text`, or None when the line isn't a mapping entry."""
    if text[0] in "\"'":
        key, end = _quoted(text, 0, number)
        rest = text[end:]
        if rest == ":" or rest.startswith(": "):
            return key, rest[1:].strip()
        return None
    if text[0] in "[{":
        return None
    for i, ch in enumerate(text):
        if ch == ":" and (i + 1 == len(text) or text[i + 1] == " "):
            key = text[:i].strip()
            if not key:
                raise PlanYamlError(number, "an empty mapping key")
            return key, text[i + 1 :].strip()
    return None


class _BlockParser:
    def __init__(self, lines: list[_Line]) -> None:
        self.lines = lines
        self.pos = 0

    def _peek(self) -> _Line | None:
        return self.lines[self.pos] if self.pos < len(self.lines) else None

    def node(self, indent: int) -> object:
        line = self._peek()
        assert line is not None and line.indent == indent
        if _is_seq_item(line.text):
            return self.sequence(indent)
        if _split_key(line.text, line.number) is not None:
            return self.mapping(indent)
        value = _inline_value(line.text, line.number)
        self.pos += 1
        nxt = self._peek()
        if nxt is not None and nxt.indent >= indent:
            raise PlanYamlError(nxt.number, "a scalar can't be followed by more content")
        return value

    def _child(self, parent: _Line, *, allow_same_indent_seq: bool) -> object:
        nxt = self._peek()
        if nxt is None:
            return None
        if nxt.indent > parent.indent:
            return self.node(nxt.indent)
        if allow_same_indent_seq and nxt.indent == parent.indent and _is_seq_item(nxt.text):
            return self.sequence(nxt.indent)
        return None

    def sequence(self, indent: int) -> list[object]:
        items: list[object] = []
        while (line := self._peek()) is not None and line.indent == indent:
            if not _is_seq_item(line.text):
                break  # a same-indentation sequence under a key ends at the next key
            rest = line.text[1:].lstrip(" ")
            if not rest:
                self.pos += 1
                nxt = self._peek()
                if nxt is not None and nxt.indent > indent:
                    items.append(self.node(nxt.indent))
                else:
                    items.append(None)
                continue
            # Re-read the item's text as a node starting at its own column.
            column = indent + len(line.text) - len(rest)
            self.lines[self.pos] = _Line(line.number, column, rest)
            items.append(self.node(column))
        self._no_deeper(indent)
        return items

    def mapping(self, indent: int) -> dict[str, object]:
        out: dict[str, object] = {}
        while (line := self._peek()) is not None and line.indent == indent:
            if _is_seq_item(line.text):
                raise PlanYamlError(line.number, "a sequence item inside a mapping")
            split = _split_key(line.text, line.number)
            if split is None:
                raise PlanYamlError(line.number, "expected `key: value`")
            key, rest = split
            if key in out:
                raise PlanYamlError(line.number, f"duplicate key {key!r}")
            self.pos += 1
            if rest:
                out[key] = _inline_value(rest, line.number)
            else:
                out[key] = self._child(line, allow_same_indent_seq=True)
        self._no_deeper(indent)
        return out

    def _no_deeper(self, indent: int) -> None:
        line = self._peek()
        if line is not None and line.indent > indent:
            raise PlanYamlError(line.number, "unexpected indentation")


# --- inline values ------------------------------------------------------------------------


def _inline_value(text: str, number: int) -> object:
    if text[0] in "[{":
        value, end = _flow(text, 0, number)
        if text[end:].strip():
            raise PlanYamlError(number, "text after a flow collection")
        return value
    if text[0] in "\"'":
        value_s, end = _quoted(text, 0, number)
        if text[end:].strip():
            raise PlanYamlError(number, "text after a quoted string")
        return value_s
    return _plain(text, number)


def _plain(text: str, number: int) -> object:
    if text[0] in _RESERVED_START:
        raise PlanYamlError(number, f"{text[0]!r} isn't supported")
    if _INT_RE.fullmatch(text):
        return int(text)
    if text in ("true", "false"):
        return text == "true"
    if text in ("null", "~"):
        return None
    return text


def _quoted(text: str, start: int, number: int) -> tuple[str, int]:
    quote = text[start]
    out: list[str] = []
    i = start + 1
    while i < len(text):
        ch = text[i]
        if quote == '"' and ch == "\\":
            esc = text[i + 1 : i + 2]
            if esc not in _DQ_ESCAPES:
                raise PlanYamlError(number, f"unsupported escape \\{esc}")
            out.append(_DQ_ESCAPES[esc])
            i += 2
            continue
        if ch == quote:
            if quote == "'" and text[i + 1 : i + 2] == "'":
                out.append("'")
                i += 2
                continue
            return "".join(out), i + 1
        out.append(ch)
        i += 1
    raise PlanYamlError(number, "an unterminated quoted string")


def _skip_spaces(text: str, i: int) -> int:
    while i < len(text) and text[i] == " ":
        i += 1
    return i


def _flow(text: str, i: int, number: int) -> tuple[object, int]:
    """A flow collection starting at `text[i]`; returns it and the index after it."""
    closing = "]" if text[i] == "[" else "}"
    items: list[object] = []
    mapping: dict[str, object] = {}
    i = _skip_spaces(text, i + 1)
    if text[i : i + 1] == closing:
        return (items if closing == "]" else mapping), i + 1
    while True:
        if i >= len(text):
            raise PlanYamlError(number, f"an unclosed flow collection (missing {closing!r})")
        if closing == "]":
            value, i = _flow_item(text, i, number)
            items.append(value)
        else:
            key, i = _flow_key(text, i, number)
            if key in mapping:
                raise PlanYamlError(number, f"duplicate key {key!r}")
            i = _skip_spaces(text, i)
            value, i = _flow_item(text, i, number)
            mapping[key] = value
        i = _skip_spaces(text, i)
        if i >= len(text):
            raise PlanYamlError(number, f"an unclosed flow collection (missing {closing!r})")
        if text[i] == ",":
            i = _skip_spaces(text, i + 1)
            continue
        if text[i] == closing:
            return (items if closing == "]" else mapping), i + 1
        raise PlanYamlError(number, f"unexpected {text[i]!r} in a flow collection")


def _flow_key(text: str, i: int, number: int) -> tuple[str, int]:
    if text[i : i + 1] in ("'", '"'):
        key, i = _quoted(text, i, number)
        i = _skip_spaces(text, i)
        if text[i : i + 1] != ":":
            raise PlanYamlError(number, "expected ':' after a flow mapping key")
        return key, i + 1
    j = i
    while j < len(text) and text[j] not in ",{}[]":
        if text[j] == ":" and (j + 1 == len(text) or text[j + 1] in " ,}[{"):
            key = text[i:j].strip()
            if not key:
                raise PlanYamlError(number, "an empty flow mapping key")
            return key, j + 1
        j += 1
    raise PlanYamlError(number, "a flow mapping entry without ':'")


def _flow_item(text: str, i: int, number: int) -> tuple[object, int]:
    if i >= len(text):
        raise PlanYamlError(number, "an unclosed flow collection")
    ch = text[i]
    if ch in "[{":
        return _flow(text, i, number)
    if ch in "\"'":
        return _quoted(text, i, number)
    j = i
    while j < len(text) and text[j] not in ",]}":
        if text[j] in "[{":
            raise PlanYamlError(number, f"unexpected {text[j]!r} inside a plain scalar")
        j += 1
    raw = text[i:j].strip()
    if not raw:
        raise PlanYamlError(number, "an empty flow item")
    return _plain(raw, number), j
