"""Server pool plus the operations the MCP tools and the CLI share.

One `FileSession` per file, at most `max_servers` running; the least
recently used idle one is shut down to make room.  A server with no tool
calls for `idle_secs` is stopped too (its next call starts a fresh one),
unless a call is waiting on it or a proof is still checking.  Each operation takes
the session's lock (so calls on one file serialise), restarts a dead
server, re-syncs the file from disk, and returns text.  `diagnostics`
lets go of the lock while it waits for the server, so other calls on the
file are not stuck behind it.
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
from .session import FileSession, Superseded

log = logging.getLogger(__name__)


class Manager:
    def __init__(self, config: Config | None = None):
        self.config = config or Config.from_env()
        self._lock = threading.Lock()
        self._sessions: "OrderedDict[str, FileSession]" = OrderedDict()
        self._closed = False
        self._stop_reaper = threading.Event()
        self.reaper: threading.Thread | None = None
        if self.config.idle_secs > 0:
            self.reaper = threading.Thread(target=self._reaper, name="hol-lsp-idle", daemon=True)
            self.reaper.start()

    # ------------------------------------------------------- idle shutdown

    def _reaper(self) -> None:
        interval = max(0.05, min(self.config.idle_check_secs, self.config.idle_secs / 4))
        while not self._stop_reaper.wait(interval):
            try:
                self.reap_idle()
            except Exception:  # never let the reaper die
                log.exception("idle check failed")

    def reap_idle(self, now: float | None = None) -> list[str]:
        """Stop the servers idle for `idle_secs`; returns their paths.  A
        server is skipped while a call holds or waits on it, or while
        `FileSession.busy` says it is compiling or checking a proof."""
        now = time.monotonic() if now is None else now
        with self._lock:
            candidates = [s for s in self._sessions.values()
                          if s.client is not None and now - s.last_used >= self.config.idle_secs]
        stopped = []
        for s in candidates:
            if not s.lock.acquire(blocking=False):
                continue  # a call is using it
            try:
                if s.client is None or s.waiters or now - s.last_used < self.config.idle_secs:
                    continue
                if s.busy():
                    continue
                idle = now - s.last_used
                log.info("stopping server for %s after %s idle", s.path, fmt.duration(idle))
                s.stop()
                s.idle_stopped = fmt.duration(idle)
                stopped.append(s.path)
            finally:
                s.lock.release()
        return stopped

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
                    if victim.waiters:  # a diagnostics call is waiting on it
                        victim.lock.release()
                        continue
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
             restart: bool = False, max_heap_mb: int | None = None) -> str:
        path = os.path.abspath(os.path.expanduser(file))
        while True:
            s = self._session(path)
            with s.lock:
                with self._lock:
                    if self._sessions.get(path) is not s:
                        continue  # evicted between lookup and lock; try again
                notes: list[str] = []
                if s.idle_stopped is not None:
                    notes.append(f"Note: the server for {path} was stopped after "
                                 f"{s.idle_stopped} idle; restarted it.")
                    s.idle_stopped = None
                try:
                    if max_heap_mb is not None:
                        s.max_heap_mb = max(0, max_heap_mb)
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
        self._stop_reaper.set()
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

    def diagnostics(self, file: str, wait: float = 25) -> str:
        start = time.monotonic()
        deadline = start + wait
        while True:
            try:
                return self._run(file, lambda s: s.format_diagnostics(s.diagnostics(deadline, start)))
            except Superseded:
                continue  # restarted while we waited: wait on the new server

    def goal_state(self, file: str, line: int, column: int) -> str:
        def op(s: FileSession) -> str:
            res = s.goal_state(line, column)
            return fmt.format_goal_state(res, s.last_blocked() if res is None else None)
        return self._run(file, op)

    def eval(self, file: str, code: str, timeout: float = 120) -> str:
        def op(s: FileSession) -> str:
            res, stderr = s.eval(code, timeout)
            out = fmt.format_eval(res, lambda p: from_lsp(code, p, s.encoding))
            if stderr.strip():
                out += ("\nServer stderr while it ran (print output; may include unrelated lines):\n"
                        + fmt.clip(stderr.rstrip(), 20000))
            return out
        return self._run(file, op)

    def hover(self, file: str, line: int, column: int) -> str:
        return self._run(file, lambda s: fmt.hover_text(
            s.request_at("textDocument/hover", line, column)))

    def definition(self, file: str, line: int, column: int) -> str:
        return self._run(file, lambda s: fmt.format_locations(
            s.request_at("textDocument/definition", line, column), s.loc_str, "definition"))

    def references(self, file: str, line: int, column: int) -> str:
        def op(s: FileSession) -> str:
            caps = (s.client.initialize_result or {}).get("capabilities") or {}
            if not caps.get("referencesProvider"):
                return ("Not supported by this server: it does not advertise "
                        "referencesProvider (HOL has no index of theorem uses).")
            return fmt.format_locations(
                s.request_at("textDocument/references", line, column,
                             {"context": {"includeDeclaration": True}}),
                s.loc_str, "references")
        return self._run(file, op)

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

    def restart(self, file: str, max_heap_mb: int | None = None) -> str:
        def op(s: FileSession) -> str:
            cap = f"heap capped at {s.heap_cap} MB" if s.heap_cap else "no heap cap"
            return (f"Server running (pid {s.client.proc.pid}, {cap}); "
                    f"file opened at version {s.version}.")
        return self._run(file, op, restart=True, max_heap_mb=max_heap_mb)

    def wait_compiled(self, file: str, timeout: float = 600) -> str:
        """CLI helper: wait until the current text has compiled."""
        def op(s: FileSession) -> str:
            ev = s.wait_compiled(timeout)
            return "compiled" if ev and ev.kind == "completed" else (ev.kind if ev else "timeout")
        return self._run(file, op)
