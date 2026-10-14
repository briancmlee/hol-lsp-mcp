"""Command line front end on the same core, for trying things without an agent.

    python -m hol_lsp_mcp serve                      # MCP server on stdio
    python -m hol_lsp_mcp diagnostics FILE [--timeout S]
    python -m hol_lsp_mcp goalState FILE LINE COLUMN
    python -m hol_lsp_mcp eval FILE CODE
    python -m hol_lsp_mcp hover|definition|references|completion FILE LINE COLUMN
    python -m hol_lsp_mcp documentSymbol FILE
    python -m hol_lsp_mcp workspaceSymbol FILE QUERY

Each CLI run starts a fresh server, so position-based commands first wait
for the file's compile to finish (use --no-wait to skip that).
"""

from __future__ import annotations

import argparse
import sys

from .manager import Manager

POSITIONAL = {
    "goalState": "goal_state",
    "hover": "hover",
    "definition": "definition",
    "references": "references",
    "completion": "completion",
}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    ap = argparse.ArgumentParser(prog="hol-lsp-mcp", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="run the MCP server over stdio")
    p = sub.add_parser("diagnostics")
    p.add_argument("file")
    p.add_argument("--timeout", type=float, default=900)
    for name in POSITIONAL:
        p = sub.add_parser(name)
        p.add_argument("file")
        p.add_argument("line", type=int)
        p.add_argument("column", type=int)
        p.add_argument("--no-wait", action="store_true")
    p = sub.add_parser("eval")
    p.add_argument("file")
    p.add_argument("code")
    p.add_argument("--timeout", type=float, default=120)
    p.add_argument("--no-wait", action="store_true")
    p = sub.add_parser("documentSymbol")
    p.add_argument("file")
    p = sub.add_parser("workspaceSymbol")
    p.add_argument("file")
    p.add_argument("query")
    p.add_argument("--no-wait", action="store_true")
    p = sub.add_parser("retryCompile")
    p.add_argument("file")
    args = ap.parse_args(argv)

    if args.cmd == "serve":
        from .server import main as serve
        serve()
        return 0

    m = Manager()
    try:
        if getattr(args, "no_wait", True) is False:
            m.wait_compiled(args.file)
        if args.cmd == "diagnostics":
            out = m.diagnostics(args.file, args.timeout)
        elif args.cmd in POSITIONAL:
            out = getattr(m, POSITIONAL[args.cmd])(args.file, args.line, args.column)
        elif args.cmd == "eval":
            out = m.eval(args.file, args.code, args.timeout)
        elif args.cmd == "documentSymbol":
            out = m.document_symbol(args.file)
        elif args.cmd == "workspaceSymbol":
            out = m.workspace_symbol(args.file, args.query)
        elif args.cmd == "retryCompile":
            out = m.retry_compile(args.file)
        else:  # pragma: no cover
            ap.error(f"unknown command {args.cmd}")
        print(out)
    finally:
        m.shutdown()
    return 0
