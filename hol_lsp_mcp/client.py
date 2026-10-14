"""A minimal LSP client for one `hol lsp` process.

A reader thread parses the server's output, routes responses to waiting
requests by id, answers server->client requests with `null`, and folds
notifications into `ServerState` instead of keeping every message.
"""

from __future__ import annotations

import collections
import logging
import os
import subprocess
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable

from .framing import Decoder, encode

log = logging.getLogger(__name__)

STDERR_TAIL_BYTES = 64 * 1024
COMPILE_EVENTS = {
    "$/compileCompleted": "completed",
    "$/compileInterrupted": "interrupted",
    "$/compileBlocked": "blocked",
}


class LspError(Exception):
    """The server answered a request with an error."""

    def __init__(self, method: str, error: dict):
        self.code = error.get("code")
        self.message = error.get("message", "")
        self.data = error.get("data")
        super().__init__(f"{method}: server error {self.code}: {self.message}")


class ServerDied(Exception):
    """The server process exited (or closed its stdout)."""


class RequestTimeout(Exception):
    pass


def uri_key(uri: str) -> str:
    """Normalise a file URI to a path, so `%`-encoding differences between
    what we send and what the server echoes don't matter."""
    p = urllib.parse.urlparse(uri)
    if p.scheme == "file":
        return urllib.parse.unquote(p.path)
    return uri


@dataclass
class CompileEvent:
    seq: int
    kind: str  # completed | interrupted | blocked
    params: dict
    t: float = field(default_factory=time.monotonic)


@dataclass
class ServerState:
    """What the reader derives from notifications.  Guarded by
    `LspClient.cond`; every field update bumps `LspClient.seq`."""

    # uri key -> {"version": int|None, "items": [...], "seq": int}
    diagnostics: dict[str, dict] = field(default_factory=dict)
    # uri key -> highest publishDiagnostics version seen
    diag_version: dict[str, int] = field(default_factory=dict)
    # uri key -> {proof name -> state dict (pos, name, status, detail)}
    proofs: dict[str, dict[str, dict]] = field(default_factory=dict)
    # uri key -> {proof name -> monotonic time we first saw it `checking`}
    checking_since: dict[str, dict[str, float]] = field(default_factory=dict)
    # uri key -> {proof name -> seconds its last finished check took}
    check_secs: dict[str, dict[str, float]] = field(default_factory=dict)
    # uri key -> (seq, monotonic time) of the last proofStates/diagnostics
    activity: dict[str, tuple[int, float]] = field(default_factory=dict)
    compile_events: collections.deque = field(
        default_factory=lambda: collections.deque(maxlen=200))
    progress_active: set = field(default_factory=set)
    progress_seq: int = 0  # seq of the last $/progress or $/compileProgress
    # (seq, method, type, text) for window/showMessage and logMessage
    messages: collections.deque = field(
        default_factory=lambda: collections.deque(maxlen=200))


class LspClient:
    def __init__(self, argv: list[str], cwd: str, env: dict[str, str] | None = None):
        self.argv = argv
        self.cwd = cwd
        self.proc = subprocess.Popen(
            argv, cwd=cwd, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.cond = threading.Condition()
        self.seq = 0
        self.state = ServerState()
        self.reader_done = False
        self._pending: dict[int, dict] = {}
        self._next_id = 1
        self._write_lock = threading.Lock()
        self._stderr = bytearray()
        self._stderr_total = 0  # bytes ever received, for `stderr_since`
        self._stderr_lock = threading.Lock()
        self.protocol_errors = 0
        self.initialize_result: dict | None = None
        threading.Thread(target=self._read_stdout, daemon=True,
                         name=f"lsp-reader-{self.proc.pid}").start()
        threading.Thread(target=self._read_stderr, daemon=True,
                         name=f"lsp-stderr-{self.proc.pid}").start()

    # ------------------------------------------------------------ status

    def alive(self) -> bool:
        return self.proc.poll() is None and not self.reader_done

    def stderr_tail(self, nbytes: int = 2000) -> str:
        with self._stderr_lock:
            return bytes(self._stderr[-nbytes:]).decode("utf-8", "replace")

    def stderr_mark(self) -> int:
        with self._stderr_lock:
            return self._stderr_total

    def stderr_since(self, mark: int) -> str:
        """What the server wrote to stderr since `stderr_mark()` returned
        `mark` (as far as the retained tail reaches)."""
        with self._stderr_lock:
            n = min(self._stderr_total - mark, len(self._stderr))
            return bytes(self._stderr[len(self._stderr) - n:]).decode("utf-8", "replace") if n > 0 else ""

    # ----------------------------------------------------------- reading

    def _read_stderr(self) -> None:
        stream = self.proc.stderr
        assert stream is not None
        while True:
            try:
                chunk = stream.read1(65536)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            with self._stderr_lock:
                self._stderr.extend(chunk)
                self._stderr_total += len(chunk)
                if len(self._stderr) > 2 * STDERR_TAIL_BYTES:
                    del self._stderr[:-STDERR_TAIL_BYTES]

    def _read_stdout(self) -> None:
        stream = self.proc.stdout
        assert stream is not None
        dec = Decoder()
        try:
            while True:
                try:
                    chunk = stream.read1(65536)
                except (OSError, ValueError):
                    break
                if not chunk:
                    break
                for msg in dec.feed(chunk):
                    try:
                        self._dispatch(msg)
                    except Exception:  # never let one bad message kill the reader
                        log.exception("error handling server message")
                self.protocol_errors = dec.errors
        finally:
            with self.cond:
                self.reader_done = True
                for slot in self._pending.values():
                    slot.setdefault("dead", True)
                self.seq += 1
                self.cond.notify_all()

    def _dispatch(self, msg: Any) -> None:
        if not isinstance(msg, dict):
            return
        method = msg.get("method")
        if method is None:
            # A response.
            rid = msg.get("id")
            with self.cond:
                slot = self._pending.get(rid)
                if slot is not None:
                    slot["reply"] = msg
                self.seq += 1
                self.cond.notify_all()
            return
        if "id" in msg:
            # A request from the server (workDoneProgress/create,
            # workspace/configuration, ...): answer so it never blocks.
            self._send({"jsonrpc": "2.0", "id": msg["id"], "result": None})
            return
        params = msg.get("params") or {}
        with self.cond:
            self.seq += 1
            self._note(method, params)
            self.cond.notify_all()

    def _note(self, method: str, params: dict) -> None:
        """Fold one notification into `state`.  Called with `cond` held."""
        st = self.state
        seq = self.seq
        if method == "textDocument/publishDiagnostics":
            key = uri_key(params.get("uri", ""))
            ver = params.get("version")
            st.diagnostics[key] = {"version": ver,
                                   "items": params.get("diagnostics") or [],
                                   "seq": seq}
            if isinstance(ver, int):
                st.diag_version[key] = max(ver, st.diag_version.get(key, ver))
            st.activity[key] = (seq, time.monotonic())
        elif method == "$/proofStates":
            key = uri_key(params.get("uri", ""))
            table = st.proofs.setdefault(key, {})
            since = st.checking_since.setdefault(key, {})
            now = time.monotonic()
            for s in params.get("states") or []:
                name = s.get("name")
                if name is None:
                    continue
                table[name] = s
                # The server re-announces a proof that an edit moved, so
                # only a proof not already checking starts the clock.
                status = s.get("status")
                if status == "checking":
                    since.setdefault(name, now)
                elif name in since:
                    t0 = since.pop(name)
                    if status != "cheated":  # cheated = dropped, not finished
                        st.check_secs.setdefault(key, {})[name] = now - t0
            st.activity[key] = (seq, now)
        elif method in COMPILE_EVENTS:
            st.compile_events.append(CompileEvent(seq, COMPILE_EVENTS[method], params))
        elif method == "$/compileProgress":
            st.progress_seq = seq
        elif method == "$/progress":
            token = params.get("token")
            kind = (params.get("value") or {}).get("kind")
            if kind == "begin":
                st.progress_active.add(token)
            elif kind == "end":
                st.progress_active.discard(token)
            st.progress_seq = seq
        elif method in ("window/showMessage", "window/logMessage"):
            st.messages.append((seq, method, params.get("type"),
                                str(params.get("message", ""))))

    # ----------------------------------------------------------- writing

    def _send(self, msg: dict) -> None:
        data = encode(msg)
        with self._write_lock:
            stdin = self.proc.stdin
            if stdin is None:
                raise ServerDied("server stdin is closed")
            try:
                stdin.write(data)
                stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as e:
                raise ServerDied(f"cannot write to server: {e}") from e

    def notify(self, method: str, params: Any) -> int:
        """Send a notification; return the state sequence number at the time,
        so callers can wait for what the server says *after* it."""
        with self.cond:
            seq = self.seq
        self._send({"jsonrpc": "2.0", "method": method, "params": params})
        return seq

    def request(self, method: str, params: Any, timeout: float = 60.0) -> Any:
        with self.cond:
            rid = self._next_id
            self._next_id += 1
            slot: dict = {}
            self._pending[rid] = slot
        try:
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            deadline = time.monotonic() + timeout
            with self.cond:
                while "reply" not in slot:
                    if self.reader_done or slot.get("dead"):
                        raise ServerDied(f"server exited while waiting for {method}")
                    left = deadline - time.monotonic()
                    if left <= 0:
                        break
                    self.cond.wait(min(left, 1.0))
            if "reply" not in slot:
                try:
                    self.notify("$/cancelRequest", {"id": rid})
                except ServerDied:
                    pass
                raise RequestTimeout(f"{method}: no reply within {timeout:g}s")
            reply = slot["reply"]
            if "error" in reply and reply["error"] is not None:
                raise LspError(method, reply["error"])
            return reply.get("result")
        finally:
            with self.cond:
                self._pending.pop(rid, None)

    # ----------------------------------------------------------- waiting

    def wait_until(self, pred: Callable[[], Any], timeout: float,
                   poll: float = 0.25) -> Any:
        """Wait (holding `cond` while evaluating) until `pred()` is truthy.
        Returns its value, or None on timeout.  Raises ServerDied if the
        server goes away first."""
        deadline = time.monotonic() + timeout
        with self.cond:
            while True:
                r = pred()
                if r:
                    return r
                if self.reader_done:
                    raise ServerDied("server exited")
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                self.cond.wait(min(left, poll))

    # ---------------------------------------------------------- shutdown

    def close(self, grace: float = 10.0) -> None:
        """shutdown + exit, then kill if it hasn't gone."""
        if self.proc.poll() is None and not self.reader_done:
            try:
                self.request("shutdown", None, timeout=grace)
                self.notify("exit", None)
            except (ServerDied, RequestTimeout, LspError):
                pass
            try:
                self.proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                pass
        if self.proc.poll() is None:
            try:
                self.proc.kill()
                self.proc.wait(timeout=5)
            except Exception:
                pass
        for s in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                if s is not None:
                    s.close()
            except Exception:
                pass


def make_env(extra_path: str | None,
             goalstate_budget_ms: str | None = None) -> dict[str, str]:
    env = dict(os.environ)
    if extra_path:
        env["PATH"] = extra_path + os.pathsep + env.get("PATH", "")
    if goalstate_budget_ms:
        env["HOL_LSP_GOALSTATE_BUDGET_MS"] = goalstate_budget_ms
    return env
