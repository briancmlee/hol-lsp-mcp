"""Convert between agent positions and LSP positions.

Agents see files as text: 1-based lines, 1-based columns counted in
Unicode code points.  LSP positions are 0-based lines and 0-based
`character` offsets counted in the negotiated `positionEncoding`
(`utf-8` bytes, `utf-16` code units or `utf-32` code points).
"""

from __future__ import annotations

UTF8, UTF16, UTF32 = "utf-8", "utf-16", "utf-32"


def _lines(text: str) -> list[str]:
    # LSP also treats a lone "\r" as a line break, but HOL counts "\n";
    # a "\r" before "\n" just sits at the end of its line.
    return text.split("\n")


def _line(text: str, line0: int) -> str:
    lines = _lines(text)
    if 0 <= line0 < len(lines):
        return lines[line0]
    return ""


def _units(s: str, encoding: str) -> int:
    """Length of `s` in units of `encoding`."""
    if encoding == UTF8:
        return len(s.encode("utf-8", "surrogatepass"))
    if encoding == UTF16:
        return sum(2 if ord(ch) > 0xFFFF else 1 for ch in s)
    return len(s)


def units_to_chars(line_text: str, units: int, encoding: str) -> int:
    """Code points covered by the first `units` units of `line_text`.

    An offset that falls inside a character counts that character as not
    yet reached; an offset past the end of the line clamps to its length.
    """
    if units <= 0:
        return 0
    if encoding == UTF32:
        return min(units, len(line_text))
    n = 0
    for i, ch in enumerate(line_text):
        n += _units(ch, encoding)
        if n > units:
            return i
        if n == units:
            return i + 1
    return len(line_text)


def chars_to_units(line_text: str, chars: int, encoding: str) -> int:
    chars = max(0, min(chars, len(line_text)))
    return _units(line_text[:chars], encoding)


def to_lsp(text: str, line: int, column: int, encoding: str) -> dict[str, int]:
    """1-based (line, column in code points) -> LSP Position."""
    line0 = max(0, line - 1)
    return {
        "line": line0,
        "character": chars_to_units(_line(text, line0), max(0, column - 1), encoding),
    }


def from_lsp(text: str | None, pos: dict, encoding: str) -> tuple[int, int]:
    """LSP Position -> 1-based (line, column in code points).

    Without the text (e.g. a file that can't be read) the offset is
    reported as is, which is exact for ASCII lines.
    """
    line0 = int(pos.get("line", 0))
    units = int(pos.get("character", 0))
    if text is None:
        return line0 + 1, units + 1
    return line0 + 1, units_to_chars(_line(text, line0), units, encoding) + 1
