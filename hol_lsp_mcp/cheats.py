"""Find `cheat` in a script's text.

The server's proof pool compares hypotheses, not oracle tags, so replaying
`cheat` succeeds and the proof is reported as `proved`.  This plain text
scan is how `diagnostics` avoids calling such a file OK.
"""

from __future__ import annotations

import re

CHEAT = re.compile(r"(?<![\w'])cheat(?![\w'])")
DECL = re.compile(r"^(?:Theorem|Triviality|Lemma|Definition)\s+([\w']+)", re.M)
STORE = re.compile(r"""store_thm\s*\(\s*"([^"]+)\"""")
END = re.compile(r"^(?:QED|End)\b", re.M)
# A top-level SML declaration starts outside any proof (a `store_thm` on
# the same line comes later in it, so it still wins).
TOPLEVEL = re.compile(r"^(?:val|fun|local|open|structure)\b", re.M)


def mask(text: str) -> str:
    """`text` with SML comments (nested) and string literals blanked out,
    newlines kept, so offsets and line numbers are unchanged."""
    out = list(text)
    i, n = 0, len(text)
    while i < n:
        if text.startswith("(*", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if text.startswith("(*", j):
                    depth, j = depth + 1, j + 2
                elif text.startswith("*)", j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            end = j
        elif text[i] == '"':
            j = i + 1
            while j < n and text[j] != '"':
                if text[j] == "\\":
                    j += 1
                j += 1
            end = min(j + 1, n)
        else:
            i += 1
            continue
        for k in range(i, end):
            if out[k] != "\n":
                out[k] = " "
        i = end
    return "".join(out)


def find_cheats(text: str) -> list[tuple[str | None, int]]:
    """[(enclosing theorem name or None, 1-based line)] for each `cheat`."""
    clean = mask(text)
    # Declaration starts and ends, in order.
    marks: list[tuple[int, str | None]] = []
    for m in DECL.finditer(clean):
        marks.append((m.start(), m.group(1)))
    for m in STORE.finditer(text):
        if clean[m.start()] != " ":  # not inside a comment
            marks.append((m.start(), m.group(1)))
    for m in END.finditer(clean):
        marks.append((m.end(), None))
    for m in TOPLEVEL.finditer(clean):
        marks.append((m.start(), None))
    marks.sort(key=lambda mk: mk[0])
    out = []
    for m in CHEAT.finditer(clean):
        name = None
        for pos, nm in marks:
            if pos > m.start():
                break
            name = nm
        out.append((name, clean.count("\n", 0, m.start()) + 1))
    return out
