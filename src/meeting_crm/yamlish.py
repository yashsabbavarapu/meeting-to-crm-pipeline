"""A small, strict YAML subset parser so config stays YAML with zero runtime deps.

Supported: nested block mappings, block lists (of scalars or mappings), inline
flow lists like ``[a, "b, c"]``, quoted and plain scalars, ints, floats,
booleans, null, and ``#`` comments. Anchors, multi-line strings, and flow
mappings are not supported and raise ``YamlError`` rather than misparsing.
"""
from __future__ import annotations

import re
from typing import Any


class YamlError(ValueError):
    pass


_INT = re.compile(r"^[-+]?\d+$")
_FLOAT = re.compile(r"^[-+]?(\d+\.\d*|\.\d+|\d+\.?\d*[eE][-+]?\d+)$")


def _strip_comment(line: str) -> str:
    quote: str | None = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i]
    return line


def _split_flow(body: str, line_no: int) -> list[str]:
    items: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    for ch in body:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
            buf.append(ch)
        elif ch == ",":
            items.append("".join(buf).strip())
            buf = []
        elif ch in "[]{}":
            raise YamlError(f"line {line_no}: nested flow collections are not supported")
        else:
            buf.append(ch)
    if quote:
        raise YamlError(f"line {line_no}: unterminated quote")
    tail = "".join(buf).strip()
    if tail or items:
        items.append(tail)
    return items


def parse_scalar(raw: str, line_no: int = 0) -> Any:
    text = raw.strip()
    if text.startswith("["):
        if not text.endswith("]"):
            raise YamlError(f"line {line_no}: unterminated flow list")
        return [parse_scalar(item, line_no) for item in _split_flow(text[1:-1], line_no)]
    if text.startswith("{"):
        raise YamlError(f"line {line_no}: flow mappings are not supported")
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        inner = text[1:-1]
        if text[0] == '"':
            inner = inner.replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")
        else:
            inner = inner.replace("''", "'")
        return inner
    if text in ("", "~", "null", "Null", "NULL"):
        return None
    if text in ("true", "True", "TRUE"):
        return True
    if text in ("false", "False", "FALSE"):
        return False
    if _INT.match(text):
        return int(text)
    if _FLOAT.match(text):
        return float(text)
    if text[0] in "&*!|>":
        raise YamlError(f"line {line_no}: unsupported YAML syntax {text[0]!r}")
    return text


def _split_key(content: str, line_no: int) -> tuple[str, str] | None:
    """Split ``key: value``. Returns None when the content is not a mapping entry."""
    if content[0] in "'\"[{":
        return None
    match = re.match(r"^([^:]+?):(?:\s+(.*))?$", content)
    if not match:
        return None
    key = match.group(1).strip()
    if not key:
        raise YamlError(f"line {line_no}: empty key")
    return key, (match.group(2) or "")


class _Parser:
    def __init__(self, text: str) -> None:
        self.lines: list[tuple[int, str, int]] = []  # (indent, content, line_no)
        for no, raw in enumerate(text.splitlines(), start=1):
            if "\t" in raw[: len(raw) - len(raw.lstrip())]:
                raise YamlError(f"line {no}: tabs are not allowed for indentation")
            stripped = _strip_comment(raw).rstrip()
            if not stripped.strip():
                continue
            indent = len(stripped) - len(stripped.lstrip(" "))
            self.lines.append((indent, stripped.strip(), no))
        self.pos = 0

    def parse(self) -> Any:
        if not self.lines:
            return {}
        value = self._block(self.lines[0][0])
        if self.pos < len(self.lines):
            _, _, no = self.lines[self.pos]
            raise YamlError(f"line {no}: unexpected indentation")
        return value

    def _is_list_item(self, content: str) -> bool:
        return content == "-" or content.startswith("- ")

    def _block(self, indent: int) -> Any:
        _, content, _ = self.lines[self.pos]
        if self._is_list_item(content):
            return self._list(indent)
        return self._mapping(indent)

    def _child(self, parent_indent: int, line_no: int) -> Any:
        """Parse the nested block after a ``key:`` or ``-`` line with no inline value."""
        if self.pos >= len(self.lines):
            return None
        indent, content, _ = self.lines[self.pos]
        if indent > parent_indent:
            return self._block(indent)
        if indent == parent_indent and self._is_list_item(content):
            return self._list(indent)  # "key:\n- item" at the same indent is valid YAML
        return None

    def _mapping(self, indent: int) -> dict[str, Any]:
        result: dict[str, Any] = {}
        while self.pos < len(self.lines):
            line_indent, content, no = self.lines[self.pos]
            if line_indent < indent:
                break
            if line_indent > indent:
                raise YamlError(f"line {no}: unexpected indentation")
            if self._is_list_item(content):
                break
            split = _split_key(content, no)
            if split is None:
                raise YamlError(f"line {no}: expected 'key: value'")
            key, raw_value = split
            if key in result:
                raise YamlError(f"line {no}: duplicate key {key!r}")
            self.pos += 1
            if raw_value.strip():
                result[key] = parse_scalar(raw_value, no)
            else:
                result[key] = self._child(indent, no)
        return result

    def _list(self, indent: int) -> list[Any]:
        result: list[Any] = []
        while self.pos < len(self.lines):
            line_indent, content, no = self.lines[self.pos]
            if line_indent < indent:
                break
            if line_indent > indent:
                raise YamlError(f"line {no}: unexpected indentation")
            if not self._is_list_item(content):
                break
            rest = content[1:].lstrip()
            if not rest:
                self.pos += 1
                result.append(self._child(indent, no))
                continue
            if _split_key(rest, no) is not None:
                # "- key: value" starts an inline mapping; re-anchor it at the item's column.
                offset = len(content) - len(rest)
                self.lines[self.pos] = (indent + offset, rest, no)
                result.append(self._mapping(indent + offset))
                continue
            self.pos += 1
            result.append(parse_scalar(rest, no))
        return result


def loads(text: str) -> Any:
    return _Parser(text).parse()
