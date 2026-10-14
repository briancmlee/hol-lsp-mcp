"""Turn server results into concise text for an agent."""

from __future__ import annotations

import re
from typing import Any, Callable

ANSI = re.compile(r"\x1B\[[0-9;]*m")

SEVERITY = {1: "error", 2: "warning", 3: "info", 4: "hint"}

SYMBOL_KIND = {
    1: "file", 2: "module", 3: "namespace", 4: "package", 5: "class",
    6: "method", 7: "property", 8: "field", 9: "constructor", 10: "enum",
    11: "interface", 12: "function", 13: "variable", 14: "constant",
    15: "string", 16: "number", 17: "boolean", 18: "array", 19: "object",
    20: "key", 21: "null", 22: "enum-member", 23: "struct", 24: "event",
    25: "operator", 26: "type-parameter",
}

COMPLETION_KIND = {
    1: "text", 2: "method", 3: "function", 4: "constructor", 5: "field",
    6: "variable", 7: "class", 8: "interface", 9: "module", 10: "property",
    11: "unit", 12: "value", 13: "enum", 14: "keyword", 15: "snippet",
    16: "color", 17: "file", 18: "reference", 19: "folder", 20: "enum-member",
    21: "constant", 22: "struct", 23: "event", 24: "operator",
    25: "type-parameter",
}

PROOF_ORDER = ["proved", "failed", "suspended", "diverged", "cheated",
               "checking", "unseen"]

MAX_DIAGNOSTICS = 100
MAX_MESSAGE = 4000
MAX_COMPLETIONS = 50

# (uri-or-None, LSP position) -> (line, column), 1-based code points
PosFn = Callable[[str | None, dict], tuple[int, int]]


def strip_ansi(s: str) -> str:
    return ANSI.sub("", s)


def _clip(s: str, n: int = MAX_MESSAGE) -> str:
    s = s.rstrip()
    return s if len(s) <= n else s[:n] + f"... [{len(s) - n} more chars]"


def _indent(s: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + line if line else line for line in s.split("\n"))


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


# ------------------------------------------------------------- diagnostics

def proof_tally(proofs: dict[str, dict]) -> dict[str, list[dict]]:
    by: dict[str, list[dict]] = {}
    for st in proofs.values():
        by.setdefault(st.get("status", "?"), []).append(st)
    return by


def format_diagnostics(r: dict, pos: PosFn) -> str:
    """`r` is what `FileSession.diagnostics` returns."""
    items = r["items"]
    proofs = r["proofs"]
    errors = [d for d in items if d.get("severity", 1) == 1]
    warnings = [d for d in items if d.get("severity", 1) == 2]
    by = proof_tally(proofs)
    total = len(proofs)
    proved = len(by.get("proved", []))
    bad = {k: v for k, v in by.items() if k not in ("proved",)}

    # One-line verdict.
    status = r["status"]
    if status == "blocked":
        b = r.get("blocked") or {}
        verdict = ("BLOCKED: not compiled; declared dependencies unavailable: "
                   + (b.get("message") or "?").strip().split("\n")[0])
    else:
        parts = []
        if errors:
            parts.append(_plural(len(errors), "error"))
        if warnings:
            parts.append(_plural(len(warnings), "warning"))
        for k in PROOF_ORDER:
            if k != "proved" and by.get(k):
                parts.append(f"{len(by[k])} {k}")
        for k in sorted(set(by) - set(PROOF_ORDER)):
            parts.append(f"{len(by[k])} {k}")
        proofs_txt = f"{proved}/{total} proofs proved"
        if status == "settled":
            if not errors and not bad:
                verdict = f"OK: compiled with no errors; {proofs_txt}"
                if warnings:
                    verdict += f"; {_plural(len(warnings), 'warning')}"
            elif not errors and set(bad) <= {"cheated"} and not warnings:
                verdict = f"OK (with cheats): no errors; {proofs_txt}, {len(by['cheated'])} cheated"
            else:
                verdict = "PROBLEMS: " + ", ".join(parts) + f"; {proofs_txt}"
        else:
            why = r.get("why", "")
            verdict = (f"INCOMPLETE ({why}): so far " +
                       (", ".join(parts) + "; " if parts else "") + proofs_txt)
    out = [verdict]

    if total:
        counts = ", ".join(f"{len(by[k])} {k}" for k in PROOF_ORDER if by.get(k))
        others = sorted(set(by) - set(PROOF_ORDER))
        if others:
            counts += ", " + ", ".join(f"{len(by[k])} {k}" for k in others)
        out.append(f"Proofs: {counts} ({total} total)")
        for k in PROOF_ORDER[1:] + others:
            for st in sorted(by.get(k, []), key=lambda s: (s.get("pos") or {}).get("line", 0)):
                where = ""
                if st.get("pos"):
                    where = f" (line {pos(None, st['pos'])[0]})"
                line = f"  {k}: {st.get('name')}{where}"
                det = st.get("detail")
                if det:
                    det = _clip(str(det), 1500)
                    line += ":\n" + _indent(det, "      ") if "\n" in det else f": {det}"
                out.append(line)

    if status == "blocked":
        b = r.get("blocked") or {}
        if b.get("modules"):
            out.append("Declared dependencies: " + ", ".join(b["modules"]))
        msg = (b.get("message") or "").strip()
        if "\n" in msg:
            out.append(_indent(msg, "  "))
        out.append("Fix the header, or build the dependency and call retryCompile "
                   "(or restart if it was already loaded).")

    if items:
        out.append(f"Diagnostics ({len(items)}):")
        for d in sorted(items, key=lambda d: ((d.get("range") or {}).get("start") or {}).get("line", 0))[:MAX_DIAGNOSTICS]:
            out.append(format_diagnostic(d, pos))
        if len(items) > MAX_DIAGNOSTICS:
            out.append(f"  ... {len(items) - MAX_DIAGNOSTICS} more not shown")
    elif status != "blocked":
        out.append("Diagnostics: none")
    return "\n".join(out)


def format_range(rng: dict, pos: PosFn, uri: str | None = None) -> str:
    s = pos(uri, rng.get("start") or {})
    e = pos(uri, rng.get("end") or rng.get("start") or {})
    if s == e:
        return f"{s[0]}:{s[1]}"
    if s[0] == e[0]:
        return f"{s[0]}:{s[1]}-{e[1]}"
    return f"{s[0]}:{s[1]}-{e[0]}:{e[1]}"


def format_diagnostic(d: dict, pos: PosFn) -> str:
    sev = SEVERITY.get(d.get("severity", 1), "?")
    where = format_range(d.get("range") or {}, pos)
    msg = _clip(str(d.get("message", "")))
    if "\n" in msg:
        return f"  {where} {sev}:\n" + _indent(msg, "      ")
    return f"  {where} {sev}: {msg}"


# --------------------------------------------------------------- goalState

def format_goal_state(res: Any, blocked: dict | None = None) -> str:
    if res is None:
        if blocked:
            return ("null: the file's compile is blocked (" +
                    (blocked.get("message") or "dependencies unavailable").split("\n")[0] +
                    "), so there is no goal state.")
        return "null: the position is not inside a Proof ... QED block (or its statement does not parse)."
    status = res.get("status")
    if status == "pending" and not res.get("theorem") and not res.get("goals") and not res.get("pretty"):
        return "pending: the server is not ready (compile still running); ask again once diagnostics has finished."
    head = f"Theorem {res.get('theorem')}, step {res.get('step')}"
    if status and status != "ok":
        head += f" [status: {status}" + (": provisional, the compile has not finished" if status == "pending" else "") + "]"
    out = [head]
    ctx = res.get("context") or []
    if ctx:
        out.append(" ".join(f"[{c}]" for c in ctx))
    if res.get("note"):
        out.append(str(res["note"]))
    if res.get("error"):
        out.append("Error: " + str(res["error"]).rstrip())
    pretty = strip_ansi(res.get("pretty") or "").rstrip()
    if pretty:
        out.append(pretty)
    elif not res.get("error"):
        goals = res.get("goals")
        out.append("(no goals)" if not goals else "\n".join(
            "\n".join(f"  {a}" for a in g.get("asms", [])) + "\n  ----\n  " + g.get("goal", "")
            for g in goals))
    return "\n".join(out)


# -------------------------------------------------------------------- eval

def format_eval(res: Any, pos_in_code: Callable[[dict], tuple[int, int]]) -> str:
    if res is None:
        return "null"
    if not isinstance(res, list):
        return repr(res)
    out = []
    errs = warns = 0
    for rep in res:
        kind = rep.get("kind")
        if kind == "error":
            hard = rep.get("hard", True)
            errs += hard
            warns += not hard
            p = rep.get("pos") or {}
            s = pos_in_code(p.get("start") or {})
            e = pos_in_code(p.get("end") or p.get("start") or {})
            where = f"{s[0]}:{s[1]}" + (f"-{e[0]}:{e[1]}" if e != s else "")
            msg = _clip(str(rep.get("msg", "")))
            label = "error" if hard else "warning"
            out.append(f"{label} at {where}:" + ("\n" + _indent(msg) if "\n" in msg else f" {msg}"))
        elif kind in ("compilerOut", "toplevelOut"):
            body = _clip(str(rep.get("body", "")), 20000)
            if body:
                out.append(body)
        elif kind == "interrupted":
            out.append("(interrupted)")
        elif kind in ("compileCompleted", "compileProgress"):
            pass
        else:
            out.append(repr(rep))
    head = "eval: " + ("ok" if not errs else _plural(errs, "error"))
    if warns:
        head += ", " + _plural(warns, "warning")
    return "\n".join([head] + out) if out else head + " (no output)"


# --------------------------------------------------------------- the rest

def hover_text(res: Any) -> str:
    if not res:
        return "null: nothing to show at this position."
    c = res.get("contents") if isinstance(res, dict) else res

    def one(x: Any) -> str:
        if isinstance(x, str):
            return x
        if isinstance(x, dict):
            return str(x.get("value", ""))
        return str(x)

    text = "\n\n".join(one(x) for x in c) if isinstance(c, list) else one(c)
    return strip_ansi(text).strip() or "(empty hover)"


def locations(res: Any) -> list[tuple[str, dict]]:
    """Location | Location[] | LocationLink[] | null -> [(uri, range)]."""
    if not res:
        return []
    if isinstance(res, dict):
        res = [res]
    out = []
    for loc in res:
        if "targetUri" in loc:
            out.append((loc["targetUri"], loc.get("targetSelectionRange") or loc.get("targetRange") or {}))
        elif "uri" in loc:
            out.append((loc["uri"], loc.get("range") or {}))
    return out


def format_locations(res: Any, loc_str: Callable[[str, dict], str], what: str) -> str:
    locs = locations(res)
    if not locs:
        return f"No {what} found."
    return "\n".join(loc_str(u, r) for u, r in locs)


def format_document_symbols(res: Any, pos: PosFn) -> str:
    if not res:
        return "No symbols."
    out: list[str] = []

    def walk(syms: list, depth: int) -> None:
        for s in syms:
            kind = SYMBOL_KIND.get(s.get("kind"), str(s.get("kind")))
            rng = s.get("selectionRange") or s.get("range") or (s.get("location") or {}).get("range") or {}
            line = pos(None, rng.get("start") or {})[0] if rng else "?"
            detail = f"  {s['detail']}" if s.get("detail") else ""
            out.append(f"{'  ' * depth}{line}: {s.get('name')} ({kind}){detail}")
            if s.get("children"):
                walk(s["children"], depth + 1)

    walk(res, 0)
    return "\n".join(out)


def format_workspace_symbols(res: Any, loc_str: Callable[[str, dict], str],
                             limit: int = 100) -> str:
    if not res:
        return "No symbols found (queries shorter than two characters answer nothing)."
    out = []
    for s in res[:limit]:
        kind = SYMBOL_KIND.get(s.get("kind"), str(s.get("kind")))
        loc = s.get("location") or {}
        where = loc_str(loc["uri"], loc.get("range") or {}) if loc.get("uri") else "?"
        cont = f" [{s['containerName']}]" if s.get("containerName") else ""
        out.append(f"{s.get('name')} ({kind}){cont} {where}")
    if len(res) > limit:
        out.append(f"... {len(res) - limit} more")
    return "\n".join(out)


def format_completion(res: Any, limit: int = MAX_COMPLETIONS) -> str:
    if res is None:
        return "No completions."
    if isinstance(res, list):
        items, incomplete = res, False
    else:
        items, incomplete = res.get("items") or [], bool(res.get("isIncomplete"))
    if not items:
        return "No completions" + (" (incomplete: empty prefix?)" if incomplete else "") + "."
    out = []
    for it in items[:limit]:
        kind = COMPLETION_KIND.get(it.get("kind"), "")
        det = it.get("detail")
        out.append(it.get("label", "?") + (f"  ({kind})" if kind else "") + (f"  {det}" if det else ""))
    more = len(items) - limit
    tail = []
    if more > 0:
        tail.append(f"... {more} more")
    if incomplete:
        tail.append("(list is incomplete)")
    return "\n".join(out + tail)
