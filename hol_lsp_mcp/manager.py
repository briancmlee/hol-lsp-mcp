"""Server pool plus the operations the MCP tools and the CLI share.

One `FileSession` per file, at most `max_servers` running; the least
recently used idle one is shut down to make room.  Each operation takes
the session's lock (so calls on one file serialise), restarts a dead
server, re-syncs the file from disk, and returns text.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import OrderedDict
from typing import Callable

from . import format as fmt
from .client import LspError, RequestTimeout, ServerDied
from .config import Config
from .positions import from_lsp
from .session import FileSession

log = logging.getLogger(__name__)


class Manager:
    def __init__(self, config: Config | None = None):
        self.config = config or Config.from_env()
        self._lock = threading.Lock()
        self._sessions: "OrderedDict[str, FileSession]" = OrderedDict()
        self._closed = False

    # --------------------------------------------------------------- pool

    def _session(self, path: str) -> FileSession:
        evict: list[FileSession] = []
        with self._lock:
            if self._closed:
                raise RuntimeError("shutting down")
            s = self._sessions.get(path)
            if s is None:
                s = FileSession(path, self.config)
                self._sessions[path] = s
            self._sessions.move_to_end(path)
            s.last_used = time.monotonic()
            running = [x for x in self._sessions.values() if x is not s and x.client is not None]
            excess = len(running) + 1 - self.config.max_servers
            for victim in running:  # oldest first
                if excess <= 0:
                    break
                if victim.lock.acquire(blocking=False):
                    del self._sessions[victim.path]
                    evict.append(victim)
                    excess -= 1
        for victim in evict:
            try:
                log.info("shutting down idle server for %s", victim.path)
                victim.stop()
            finally:
                victim.lock.release()
        return s

    def _run(self, file: str, op: Callable[[FileSession], str], *, sync: bool = True,
             restart: bool = False) -> str:
        path = os.path.abspath(os.path.expanduser(file))
        while True:
            s = self._session(path)
            with s.lock:
                with self._lock:
                    if self._sessions.get(path) is not s:
                        continue  # evicted between lookup and lock; try again
                notes: list[str] = []
                try:
                    if restart and s.client is not None:
                        s.stop()
                        notes.append(f"Stopped the server for {path}.")
                    if s.client is not None and not s.client.alive():
                        c = s.client
                        rc = c.proc.poll()
                        notes.append(f"Note: the server for this file had exited (code {rc}); "
                                     f"restarted it. Its stderr tail:\n{c.stderr_tail(1500).rstrip()}")
                        s.stop()
                    if s.client is None:
                        if not os.path.isfile(path):
                            return f"Error: no such file: {path}"
                        s.start()
                        if restart:
                            notes.append("Started a new server.")
                    if sync:
                        s.sync()
                    out = op(s)
                except FileNotFoundError as e:
                    out = f"Error: {e}"
                except ServerDied as e:
                    tail = s.client.stderr_tail(1500).rstrip() if s.client else ""
                    out = (f"Error: the server died ({e}); it will be restarted on the next call."
                           + (f"\nstderr tail:\n{tail}" if tail else ""))
                except RequestTimeout as e:
                    out = f"Error: request timed out: {e}"
                except LspError as e:
                    out = f"Error from server: {e.message} (code {e.code})"
                except RuntimeError as e:
                    out = f"Error: {e}"
                msgs = s.new_server_messages()
                if msgs:
                    notes.append("Server messages:\n" + "\n".join("  " + m for m in msgs))
                s.last_used = time.monotonic()
                return "\n\n".join([out] + notes) if notes else out

    def shutdown(self) -> None:
        """Shut every server down (shutdown + exit, then kill), in parallel."""
        with self._lock:
            self._closed = True
            sessions = list(self._sessions.values())
            self._sessions.clear()
        threads = [threading.Thread(target=s.stop, daemon=True) for s in sessions]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)

    def running_files(self) -> list[str]:
        with self._lock:
            return [p for p, s in self._sessions.items() if s.client is not None]

    # ---------------------------------------------------------- operations

    def diagnostics(self, file: str, timeout: float = 900) -> str:
        return self._run(file, lambda s: s.format_diagnostics(s.diagnostics(timeout)))

    def goal_state(self, file: str, line: int, column: int) -> str:
        def op(s: FileSession) -> str:
            res = s.goal_state(line, column)
            return fmt.format_goal_state(res, s.last_blocked() if res is None else None)
        return self._run(file, op)

    def eval(self, file: str, code: str, timeout: float = 120) -> str:
        def op(s: FileSession) -> str:
            res = s.eval(code, timeout)
            return fmt.format_eval(res, lambda p: from_lsp(code, p, s.encoding))
        return self._run(file, op)

    def hover(self, file: str, line: int, column: int) -> str:
        return self._run(file, lambda s: fmt.hover_text(
            s.request_at("textDocument/hover", line, column)))

    def definition(self, file: str, line: int, column: int) -> str:
        return self._run(file, lambda s: fmt.format_locations(
            s.request_at("textDocument/definition", line, column), s.loc_str, "definition"))

    def references(self, file: str, line: int, column: int) -> str:
        return self._run(file, lambda s: fmt.format_locations(
            s.request_at("textDocument/references", line, column,
                         {"context": {"includeDeclaration": True}}),
            s.loc_str, "references"))

    def document_symbol(self, file: str) -> str:
        return self._run(file, lambda s: fmt.format_document_symbols(
            s.document_symbol(), s.pos_out))

    def workspace_symbol(self, file: str, query: str) -> str:
        return self._run(file, lambda s: fmt.format_workspace_symbols(
            s.workspace_symbol(query), s.loc_str))

    def completion(self, file: str, line: int, column: int) -> str:
        return self._run(file, lambda s: fmt.format_completion(
            s.request_at("textDocument/completion", line, column)))

    def retry_compile(self, file: str) -> str:
        def op(s: FileSession) -> str:
            s.retry_compile()
            return ("Sent $/hol/retryCompile (a notification; the server does not reply). "
                    "Call diagnostics to see the result.")
        return self._run(file, op)

    def restart(self, file: str) -> str:
        def op(s: FileSession) -> str:
            return f"Server running (pid {s.client.proc.pid}); file opened at version {s.version}."
        return self._run(file, op, restart=True)

    def wait_compiled(self, file: str, timeout: float = 600) -> str:
        """CLI helper: wait until the current text has compiled."""
        def op(s: FileSession) -> str:
            ev = s.wait_compiled(timeout)
            return "compiled" if ev and ev.kind == "completed" else (ev.kind if ev else "timeout")
        return self._run(file, op)
