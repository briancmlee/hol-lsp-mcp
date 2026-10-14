"""The MCP server: one tool per `hol lsp` method."""

from __future__ import annotations

import atexit
import contextlib
import logging
import signal
import sys
from functools import partial

import anyio
from mcp.server import MCPServer

from .manager import Manager

INSTRUCTIONS = """\
Tools mirroring HOL4's language server (`hol lsp --lsp-check-proofs`), one
server per theory script.  Lines and columns are 1-based; columns count
Unicode characters.  Edit files with your normal tools: every call re-reads
the file from disk first.  `diagnostics` waits for the compile and the
proof checks to finish."""

manager: Manager | None = None


def _mgr() -> Manager:
    assert manager is not None
    return manager


async def _call(fn, *args) -> str:
    return await anyio.to_thread.run_sync(partial(fn, *args))


@contextlib.asynccontextmanager
async def _lifespan(_app):
    try:
        yield {}
    finally:
        await anyio.to_thread.run_sync(_mgr().shutdown)


mcp = MCPServer("hol-lsp", instructions=INSTRUCTIONS, lifespan=_lifespan)


@mcp.tool(structured_output=False)
async def diagnostics(file: str, timeout: float = 900) -> str:
    """textDocument/publishDiagnostics + $/proofStates: the file's latest
    diagnostics and proof tally, once the compile has finished and no proof
    is still being checked.  Starts with a one-line verdict."""
    return await _call(_mgr().diagnostics, file, timeout)


@mcp.tool(structured_output=False)
async def goalState(file: str, line: int, column: int) -> str:
    """$/hol/goalState: the goal state at a position inside Proof ... QED."""
    return await _call(_mgr().goal_state, file, line, column)


@mcp.tool(structured_output=False)
async def eval(file: str, code: str, timeout: float = 120) -> str:
    """$/eval: compile and run SML in the file's server process.  Bindings are
    not echoed: use print or PolyML.print.  Sees loaded theories/libraries,
    not the file's own declarations; changes persist in the process."""
    return await _call(_mgr().eval, file, code, timeout)


@mcp.tool(structured_output=False)
async def hover(file: str, line: int, column: int) -> str:
    """textDocument/hover: type, value and documentation of the identifier."""
    return await _call(_mgr().hover, file, line, column)


@mcp.tool(structured_output=False)
async def definition(file: str, line: int, column: int) -> str:
    """textDocument/definition: where the identifier is defined (path:line:column)."""
    return await _call(_mgr().definition, file, line, column)


@mcp.tool(structured_output=False)
async def references(file: str, line: int, column: int) -> str:
    """textDocument/references (says so if the server does not support it; current HOL does not)."""
    return await _call(_mgr().references, file, line, column)


@mcp.tool(structured_output=False)
async def documentSymbol(file: str) -> str:
    """textDocument/documentSymbol: the file's outline (works even if it does not compile)."""
    return await _call(_mgr().document_symbol, file)


@mcp.tool(structured_output=False)
async def workspaceSymbol(file: str, query: str) -> str:
    """workspace/symbol in this file's server: stored theorems matching a substring (>= 2 chars)."""
    return await _call(_mgr().workspace_symbol, file, query)


@mcp.tool(structured_output=False)
async def completion(file: str, line: int, column: int) -> str:
    """textDocument/completion: names in scope and theorem names (first 50)."""
    return await _call(_mgr().completion, file, line, column)


@mcp.tool(structured_output=False)
async def retryCompile(file: str) -> str:
    """$/hol/retryCompile: compile a file blocked on a missing dependency again."""
    return await _call(_mgr().retry_compile, file)


@mcp.tool(structured_output=False)
async def restart(file: str) -> str:
    """Restart this file's server (e.g. after its dependencies were rebuilt)."""
    return await _call(_mgr().restart, file)


def main() -> None:
    global manager
    logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                        format="hol-lsp-mcp: %(message)s")
    manager = Manager()
    atexit.register(manager.shutdown)

    def on_term(signum, _frame):
        manager.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, on_term)
    mcp.run()
