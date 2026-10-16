"""One file, one `hol lsp --lsp-check-proofs` process.

The server binds to the first script it is given and refuses any other,
so a session is a (file, process) pair.  Every operation first re-reads
the file from disk and syncs it to the server.
"""

from __future__ import annotations

import contextlib
import os
import pathlib
import re
import threading
import time
from typing import Any

from . import format as fmt
from .cheats import find_cheats, mask
from .client import LspClient, LspError, RequestTimeout, ServerDied, make_env, uri_key
from .config import Config
from .memory import footprint_mb
from .positions import UTF16, from_lsp, to_lsp

LANGUAGE_ID = "holsml"
# A compile that was interrupted and not restarted for this long is
# taken as over (otherwise `diagnostics` would always use its full `wait`).
INTERRUPTED_GRACE = 30.0
CHANGED = object()  # `_wait`: another call synced a newer text meanwhile

PROOF_HEAD = re.compile(r"^(?:Theorem|Triviality|Lemma)\b", re.M)
PROOF = re.compile(r"^Proof\b", re.M)
QED = re.compile(r"^QED\b", re.M)


class Superseded(Exception):
    """The server was stopped or replaced while a call waited without the
    session's lock.  The caller starts over."""


def read_text(path: str) -> str:
    with open(path, "rb") as f:
        return f.read().decode("utf-8", "replace")


@contextlib.contextmanager
def released(lock: threading.RLock):
    """Let go of `lock` (held once by this thread) for the duration."""
    lock.release()
    try:
        yield
    finally:
        lock.acquire()


def proof_edit_start(old: str, new: str) -> int | None:
    """Where the declaration starts, if the change from `old` to `new`
    starts inside a `Proof ... QED` body and runs past that body's `QED`.

    `hol lsp` decides that an edit is confined to one proof from where the
    edit starts.  It then cancels just that proof's check and reuses, by
    name, the checks still running on the proofs below, even if the same
    edit changed their tactics.  A changed proof below then shows the
    result of its old text, or `checking` for good if the old text looped.
    `sync` avoids this by touching the declaration's start first, which
    the server treats as a statement edit: it cancels everything below."""
    pre = 0
    n = min(len(old), len(new))
    while pre < n and old[pre] == new[pre]:
        pre += 1
    suf = 0
    while suf < n - pre and old[-1 - suf] == new[-1 - suf]:
        suf += 1
    end = len(new) - suf  # the changed region of `new` is [pre, end)
    clean = mask(new)
    head = None
    for head in PROOF_HEAD.finditer(clean, 0, pre):
        pass
    if head is None:
        return None
    body = PROOF.search(clean, head.end(), pre)
    if body is None:
        return None
    qed = QED.search(clean, body.end())
    if qed is not None and qed.start() < pre:
        return None  # the change starts after this proof
    if qed is None or end > qed.start():
        return head.start()
    return None


class FileSession:
    def __init__(self, path: str, config: Config):
        self.path = path
        self.dir = os.path.dirname(path)
        self.uri = pathlib.Path(path).as_uri()
        self.key = uri_key(self.uri)
        self.config = config
        self.lock = threading.RLock()
        self.client: LspClient | None = None
        self.encoding = UTF16
        self.last_used = time.monotonic()
        self.waiters = 0  # calls waiting without `lock`; guarded by `lock`
        # --maxheap (MB) for this file's server, set by `restart`; None
        # means the config's.  `heap_cap` is what the running server got.
        self.max_heap_mb: int | None = None
        self.heap_cap = 0
        self._reset_doc()

    def _reset_doc(self) -> None:
        self.opened = False
        self.version = 0
        self.text: str | None = None
        self.sync_seq = 0
        self.settled_version: int | None = None
        self.changed_at = time.monotonic()  # last didOpen/didChange
        self.msg_cursor = 0
        self._other_text: dict[str, tuple[float, str | None]] = {}

    # --------------------------------------------------------- lifecycle

    def running(self) -> bool:
        return self.client is not None and self.client.alive()

    def start(self) -> None:
        cap = self.config.max_heap_mb if self.max_heap_mb is None else self.max_heap_mb
        # Poly/ML runtime options go before the subcommand.
        argv = ([self.config.hol_bin] + (["--maxheap", str(cap)] if cap > 0 else [])
                + ["lsp", "--lsp-check-proofs"])
        if not os.path.exists(self.config.hol_bin):
            raise RuntimeError(f"HOL binary not found: {self.config.hol_bin} "
                               "(set HOL_BIN or HOLDIR)")
        self._reset_doc()
        client = LspClient(argv, cwd=self.dir, env=make_env(self.config.extra_path,
                                                                self.config.goalstate_budget_ms))
        caps = {
            "general": {"positionEncodings": ["utf-8"]},
            "textDocument": {"documentSymbol": {"hierarchicalDocumentSymbolSupport": True}},
        }
        try:
            res = client.request("initialize", {
                "processId": os.getpid(),
                "rootUri": pathlib.Path(self.dir).as_uri(),
                "capabilities": caps,
            }, timeout=self.config.init_timeout)
            client.notify("initialized", {})
        except (ServerDied, RequestTimeout, LspError) as e:
            tail = client.stderr_tail()
            client.close(grace=2)
            raise RuntimeError(f"hol lsp failed to start for {self.path}: {e}\n"
                               f"stderr tail:\n{tail}") from e
        client.initialize_result = res
        self.heap_cap = cap
        self.encoding = ((res or {}).get("capabilities") or {}).get("positionEncoding") or UTF16
        self.client = client

    def stop(self) -> None:
        c, self.client = self.client, None
        if c is not None:
            c.close()
        self._reset_doc()

    def _c(self) -> LspClient:
        if self.client is None:
            raise ServerDied("server not running")
        return self.client

    # -------------------------------------------------------------- sync

    def sync(self) -> None:
        """Bring the server's copy of the file up to date with the disk."""
        try:
            text = read_text(self.path)
        except FileNotFoundError:
            raise FileNotFoundError(f"no such file: {self.path}") from None
        c = self._c()
        if not self.opened:
            self.version = 1
            self.sync_seq = c.notify("textDocument/didOpen", {"textDocument": {
                "uri": self.uri, "languageId": LANGUAGE_ID,
                "version": self.version, "text": text}})
            self.opened = True
        elif text != self.text:
            at = proof_edit_start(self.text or "", text)
            if at is not None:
                # See `proof_edit_start`: a blank line before the
                # declaration, at once superseded by the real text.
                self.version += 1
                c.notify("textDocument/didChange", {
                    "textDocument": {"uri": self.uri, "version": self.version},
                    "contentChanges": [{"text": text[:at] + "\n" + text[at:]}]})
            self.version += 1
            self.sync_seq = c.notify("textDocument/didChange", {
                "textDocument": {"uri": self.uri, "version": self.version},
                "contentChanges": [{"text": text}]})
        else:
            return
        self.text = text
        self.settled_version = None
        self.changed_at = time.monotonic()

    def new_server_messages(self) -> list[str]:
        """window/showMessage lines received since the last call."""
        if self.client is None:
            return []
        out = []
        with self.client.cond:
            for seq, method, _typ, text in self.client.state.messages:
                if seq > self.msg_cursor and method == "window/showMessage":
                    out.append(text)
            self.msg_cursor = self.client.seq
        return out

    # --------------------------------------------------------- positions

    def _text_of(self, uri: str | None) -> str | None:
        if uri is None or uri_key(uri) == self.key:
            return self.text
        path = uri_key(uri)
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            return None
        cached = self._other_text.get(path)
        if cached and cached[0] == mtime:
            return cached[1]
        try:
            text = read_text(path)
        except OSError:
            text = None
        if len(self._other_text) > 50:
            self._other_text.clear()
        self._other_text[path] = (mtime, text)
        return text

    def pos_out(self, uri: str | None, pos: dict) -> tuple[int, int]:
        return from_lsp(self._text_of(uri), pos, self.encoding)

    def loc_str(self, uri: str, rng: dict) -> str:
        line, col = self.pos_out(uri, rng.get("start") or {})
        return f"{uri_key(uri)}:{line}:{col}"

    def pos_in(self, line: int, column: int) -> dict:
        return to_lsp(self.text or "", line, column, self.encoding)

    def text_doc_pos(self, line: int, column: int) -> dict:
        return {"textDocument": {"uri": self.uri}, "position": self.pos_in(line, column)}

    # ------------------------------------------------------- compile state

    def _compile_done(self, version: int | None = None, sync_seq: int | None = None) -> Any:
        """The compile event that ends the compile of `version` (sent at
        `sync_seq`; default: the current ones), if it has arrived.  Called
        with the client's cond held."""
        version = self.version if version is None else version
        sync_seq = self.sync_seq if sync_seq is None else sync_seq
        c = self._c()
        st = c.state
        for ev in reversed(st.compile_events):
            if ev.seq <= sync_seq:
                break
            if uri_key(ev.params.get("uri", "")) != self.key:
                continue
            if ev.kind == "blocked":
                return ev
            if ev.kind == "completed" and st.diag_version.get(self.key, -1) >= version:
                return ev
            if (ev.kind == "interrupted" and not st.progress_active
                    and st.progress_seq < ev.seq
                    and time.monotonic() - ev.t > INTERRUPTED_GRACE):
                return ev
        return None

    def last_blocked(self) -> dict | None:
        """Params of $/compileBlocked if that's the latest compile outcome."""
        c = self._c()
        with c.cond:
            for ev in reversed(c.state.compile_events):
                if uri_key(ev.params.get("uri", "")) == self.key:
                    return ev.params if ev.kind == "blocked" else None
        return None

    def wait_compiled(self, timeout: float) -> Any:
        return self._c().wait_until(self._compile_done, timeout)

    # ----------------------------------------------------------- methods

    def _wait(self, c: LspClient, mark: tuple[int, int], pred, deadline: float,
              poll: float = 0.25) -> Any:
        """`c.wait_until(pred)` until `deadline`, without holding `lock`, so
        that other calls on this file are not stuck behind the wait.
        Returns pred's value, None at the deadline, or CHANGED if another
        call synced a newer text (or sent retryCompile) meanwhile.  Raises
        Superseded if the server was stopped or replaced meanwhile.
        Called with `lock` held once."""
        def p() -> Any:
            return CHANGED if (self.version, self.sync_seq) != mark else pred()

        died = None
        self.waiters += 1
        try:
            with released(self.lock):
                try:
                    r = c.wait_until(p, max(0.0, deadline - time.monotonic()), poll)
                except ServerDied as e:
                    died = e
        finally:
            self.waiters -= 1
        if self.client is not c:
            raise Superseded()
        if died is not None:
            raise died
        return CHANGED if (self.version, self.sync_seq) != mark else r

    def diagnostics(self, deadline: float, start: float | None = None) -> dict:
        """Wait, until `deadline` at most, for the compile of the current
        text and for the proof pool to settle.  Returns {status, why,
        waited, items, proofs, cheats, checking, baseline, blocked}, where
        status is settled, blocked or incomplete.  Returning early stops
        nothing: the server carries on checking.  `start` is when the
        caller started waiting, for reporting."""
        start = time.monotonic() if start is None else start
        while True:
            c = self._c()
            mark = version, sync_seq = self.version, self.sync_seq
            result: dict[str, Any] = {"status": "settled", "why": ""}

            def snapshot() -> dict:
                now = time.monotonic()
                with c.cond:
                    d = c.state.diagnostics.get(self.key) or {}
                    result["items"] = list(d.get("items") or [])
                    result["proofs"] = self._live_proofs(c.state.proofs.get(self.key) or {})
                    # Counted from the last edit at the latest: what ran
                    # before it was checking a different text.
                    since = c.state.checking_since.get(self.key) or {}
                    result["checking"] = {n: now - max(t, self.changed_at)
                                          for n, t in since.items()}
                    secs = (c.state.check_secs.get(self.key) or {}).values()
                    result["baseline"] = max(secs) if secs else None
                result["cheats"] = find_cheats(self.text or "")
                result["waited"] = now - start
                result["footprint_mb"] = footprint_mb(c.proc.pid)
                result["limits"] = {"heap_mb": self.heap_cap, "soft_mb": self.config.soft_mb,
                                    "soft_secs": self.config.soft_secs,
                                    "soft_factor": self.config.soft_factor}
                return result

            if self.settled_version == version:
                snapshot()
                result["blocked"] = self.last_blocked()
                if result["blocked"]:
                    result["status"] = "blocked"
                return result

            ev = self._wait(c, mark, lambda: self._compile_done(version, sync_seq), deadline)
            if ev is CHANGED:
                continue
            if ev is None:
                result.update(status="incomplete", why="still compiling")
                return snapshot()
            if ev.kind == "blocked":
                result.update(status="blocked", blocked=ev.params)
                # Give the load-failure diagnostics a moment to arrive.
                c.wait_until(lambda: False, 0.3)
                self.settled_version = version
                return snapshot()
            if ev.kind == "interrupted":
                result.update(status="incomplete",
                              why="the compile was interrupted and did not restart")
                return snapshot()

            done_at = time.monotonic()
            quiet = self.config.quiet_period

            def settled() -> bool:
                st = c.state
                if st.progress_active:
                    return False
                if any(s.get("status") == "checking"
                       for s in (st.proofs.get(self.key) or {}).values()):
                    return False
                last = max(done_at, st.activity.get(self.key, (0, 0.0))[1])
                return time.monotonic() - last >= quiet

            ok = self._wait(c, mark, settled, deadline, poll=0.2)
            if ok is CHANGED:
                continue
            if not ok:
                result.update(status="incomplete", why="still checking")
                return snapshot()
            self.settled_version = version
            return snapshot()

    def _live_proofs(self, proofs: dict[str, dict]) -> dict[str, dict]:
        """The accumulated proof states, minus `cheated` entries for names no
        longer in the file.  The server announces a dropped proof as
        `cheated` and never retracts it, so a deleted or renamed theorem
        would otherwise stay in the tally for good."""
        text = self.text or ""
        out = {}
        for name, st in proofs.items():
            if st.get("status") == "cheated" and not re.search(
                    r"(?<![\w'])" + re.escape(name) + r"(?![\w'])", text):
                continue
            out[name] = st
        return out

    def goal_state(self, line: int, column: int) -> Any:
        c = self._c()
        params = self.text_doc_pos(line, column)
        res = c.request("$/hol/goalState", params, timeout=self.config.request_timeout)
        # "pending" means "ask again when the compile is done".
        for _ in range(3):
            if not (isinstance(res, dict) and res.get("status") == "pending"):
                break
            self.wait_compiled(self.config.request_timeout)
            c.wait_until(lambda: False, 0.5)
            res = c.request("$/hol/goalState", params, timeout=self.config.request_timeout)
        return res

    def eval(self, code: str, timeout: float) -> tuple[Any, str]:
        """The report list, plus whatever the server wrote to stderr meanwhile
        (where `print` output goes; the reports only carry PolyML.print-style
        toplevel output)."""
        c = self._c()
        mark = c.stderr_mark()
        res = c.request("$/eval", {"uri": self.uri, "code": code}, timeout=timeout)
        c.wait_until(lambda: False, 0.2)  # let the stderr reader catch up
        return res, c.stderr_since(mark)

    def request_at(self, method: str, line: int, column: int, extra: dict | None = None) -> Any:
        params = self.text_doc_pos(line, column)
        if extra:
            params.update(extra)
        return self._c().request(method, params, timeout=self.config.request_timeout)

    def document_symbol(self) -> Any:
        return self._c().request("textDocument/documentSymbol",
                                 {"textDocument": {"uri": self.uri}},
                                 timeout=self.config.request_timeout)

    def workspace_symbol(self, query: str) -> Any:
        return self._c().request("workspace/symbol", {"query": query},
                                 timeout=self.config.request_timeout)

    def retry_compile(self) -> None:
        self.sync_seq = self._c().notify("$/hol/retryCompile",
                                       {"textDocument": {"uri": self.uri}})
        self.settled_version = None

    # ------------------------------------------------------- formatting

    def format_diagnostics(self, r: dict) -> str:
        return fmt.format_diagnostics(r, self.pos_out)
