"""One file, one `hol lsp --lsp-check-proofs` process.

The server binds to the first script it is given and refuses any other,
so a session is a (file, process) pair.  Every operation first re-reads
the file from disk and syncs it to the server.
"""

from __future__ import annotations

import os
import pathlib
import re
import threading
import time
from typing import Any

from . import format as fmt
from .client import LspClient, LspError, RequestTimeout, ServerDied, make_env, uri_key
from .config import Config
from .positions import UTF16, from_lsp, to_lsp

LANGUAGE_ID = "holsml"
# A compile that was interrupted and not restarted for this long is
# taken as over (otherwise `diagnostics` would wait for the full timeout).
INTERRUPTED_GRACE = 30.0


def read_text(path: str) -> str:
    with open(path, "rb") as f:
        return f.read().decode("utf-8", "replace")


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
        self._reset_doc()

    def _reset_doc(self) -> None:
        self.opened = False
        self.version = 0
        self.text: str | None = None
        self.sync_seq = 0
        self.settled_version: int | None = None
        self.msg_cursor = 0
        self._other_text: dict[str, tuple[float, str | None]] = {}

    # --------------------------------------------------------- lifecycle

    def running(self) -> bool:
        return self.client is not None and self.client.alive()

    def start(self) -> None:
        argv = [self.config.hol_bin, "lsp", "--lsp-check-proofs"]
        if not os.path.exists(self.config.hol_bin):
            raise RuntimeError(f"HOL binary not found: {self.config.hol_bin} "
                               "(set HOL_BIN or HOLDIR)")
        self._reset_doc()
        client = LspClient(argv, cwd=self.dir, env=make_env(self.config.extra_path))
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
            self.version += 1
            self.sync_seq = c.notify("textDocument/didChange", {
                "textDocument": {"uri": self.uri, "version": self.version},
                "contentChanges": [{"text": text}]})
        else:
            return
        self.text = text
        self.settled_version = None

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

    def _compile_done(self) -> Any:
        """The compile event that ends the compile of the current version, if
        it has arrived.  Called with the client's cond held."""
        c = self._c()
        st = c.state
        for ev in reversed(st.compile_events):
            if ev.seq <= self.sync_seq:
                break
            if uri_key(ev.params.get("uri", "")) != self.key:
                continue
            if ev.kind == "blocked":
                return ev
            if ev.kind == "completed" and st.diag_version.get(self.key, -1) >= self.version:
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

    def diagnostics(self, timeout: float = 900.0) -> dict:
        """Wait for the compile of the current text and for the proof pool
        to settle; return {status, items, proofs, blocked, why}."""
        c = self._c()
        start = time.monotonic()
        deadline = start + timeout
        result: dict[str, Any] = {"status": "settled", "why": ""}

        def snapshot() -> None:
            with c.cond:
                d = c.state.diagnostics.get(self.key) or {}
                result["items"] = list(d.get("items") or [])
                result["proofs"] = self._live_proofs(c.state.proofs.get(self.key) or {})

        if self.settled_version == self.version:
            snapshot()
            result["blocked"] = self.last_blocked()
            if result["blocked"]:
                result["status"] = "blocked"
            return result

        ev = c.wait_until(self._compile_done, max(0.0, deadline - time.monotonic()))
        if ev is None:
            result.update(status="timeout",
                          why=f"timed out after {timeout:g}s; the compile has not finished")
            snapshot()
            return result
        if ev.kind == "blocked":
            result.update(status="blocked", blocked=ev.params)
            # Give the load-failure diagnostics a moment to arrive.
            c.wait_until(lambda: False, 0.3)
            snapshot()
            self.settled_version = self.version
            return result
        if ev.kind == "interrupted":
            result.update(status="timeout", why="the compile was interrupted and did not restart")
            snapshot()
            return result

        done_at = time.monotonic()
        quiet = self.config.quiet_period

        def settled() -> bool:
            st = c.state
            if st.progress_active:
                return False
            if any(s.get("status") == "checking" for s in (st.proofs.get(self.key) or {}).values()):
                return False
            last = max(done_at, st.activity.get(self.key, (0, 0.0))[1])
            return time.monotonic() - last >= quiet

        ok = c.wait_until(settled, max(0.0, deadline - time.monotonic()), poll=0.2)
        snapshot()
        if not ok:
            n = sum(1 for s in result["proofs"].values() if s.get("status") == "checking")
            result.update(status="timeout",
                          why=f"timed out after {timeout:g}s; {n} proofs still checking")
            return result
        self.settled_version = self.version
        return result

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
