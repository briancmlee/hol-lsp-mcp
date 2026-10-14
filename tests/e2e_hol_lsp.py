"""End-to-end tests against a real `hol lsp`.

    python tests/e2e_hol_lsp.py [-v] [TestCase.test_name ...]

Skips cleanly when HOL isn't built (no `bin/hol.state`, or `bin/hol lsp`
doesn't complete a handshake).  Uses HOL_BIN / HOLDIR like the server; if
HOL_LSP_EXTRA_PATH is unset and ~/.local/polyml/bin exists, uses that.
Set HOL_LSP_E2E_VERBOSE=1 to print every tool's output.

The theories are small and self-contained, modelled on HOL's own
tools-poly/lsp/tests/lsp_tests.py.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hol_lsp_mcp.client import make_env  # noqa: E402
from hol_lsp_mcp.config import Config  # noqa: E402
from hol_lsp_mcp.framing import Decoder, encode  # noqa: E402
from hol_lsp_mcp.manager import Manager  # noqa: E402

VERBOSE = bool(os.environ.get("HOL_LSP_E2E_VERBOSE"))
POLY_BIN = os.path.expanduser("~/.local/polyml/bin")
if not os.environ.get("HOL_LSP_EXTRA_PATH") and os.path.isdir(POLY_BIN):
    os.environ["HOL_LSP_EXTRA_PATH"] = POLY_BIN

M: Manager | None = None
TMP: str | None = None


def hol_ready(cfg: Config) -> str | None:
    """None if `hol lsp` is usable, else why not."""
    hol = cfg.hol_bin
    state = os.path.join(os.path.dirname(hol), "hol.state")
    if not os.path.exists(hol):
        return f"{hol} does not exist"
    if not os.path.exists(state):
        return f"{state} does not exist (HOL not built yet)"
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"capabilities": {}, "rootUri": "file:///tmp", "processId": None}},
        {"jsonrpc": "2.0", "method": "initialized", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "shutdown", "params": None},
        {"jsonrpc": "2.0", "method": "exit", "params": None},
    ]
    try:
        p = subprocess.run([hol, "lsp"], input=b"".join(encode(m) for m in msgs),
                           capture_output=True, timeout=120, cwd="/tmp",
                           env=make_env(cfg.extra_path, cfg.goalstate_budget_ms))
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"`hol lsp` handshake failed: {e}"
    replies = Decoder().feed(p.stdout)
    if not any(r.get("id") == 1 and "result" in r for r in replies):
        return (f"`hol lsp` did not answer initialize (rc {p.returncode}); stderr: "
                + p.stderr.decode(errors="replace")[-500:])
    return None


def setUpModule():
    global M, TMP
    cfg = Config.from_env()
    why = hol_ready(cfg)
    if why:
        raise unittest.SkipTest(why)
    cfg.max_servers = 2
    M = Manager(cfg)
    TMP = tempfile.mkdtemp(prefix="hol_lsp_mcp_e2e_")


def tearDownModule():
    if M is not None:
        M.shutdown()
    if TMP:
        shutil.rmtree(TMP, ignore_errors=True)


def write(name: str, text: str) -> str:
    assert TMP is not None
    path = os.path.join(TMP, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def show(label: str, out: str) -> str:
    if VERBOSE:
        print(f"\n----- {label}\n{out}", flush=True)
    return out


CLEAN = ("Theory e2eclean\n"
         "Ancestors arithmetic\n\n"
         "Theorem truth:\n"
         "  T\n"
         "Proof\n"
         "  ACCEPT_TAC TRUTH\n"
         "QED\n\n"
         "Theorem plus_zero:\n"
         "  ∀n:num. n + 0 = n\n"
         "Proof\n"
         "  rw[]\n"
         "QED\n\n"
         "val (*‘’‘’‘’*) pz = plus_zero\n")


class E2E(unittest.TestCase):
    def test_clean_theory_and_queries(self):
        f = write("e2ecleanScript.sml", CLEAN)
        lines = CLEAN.split("\n")
        t0 = time.monotonic()
        out = show("diagnostics clean", M.diagnostics(f, wait=180))
        self.assertTrue(out.startswith("OK: compiled with no errors; 2/2 proofs proved"), out)
        # Unchanged file: answered from the settled state.
        t1 = time.monotonic()
        self.assertEqual(M.diagnostics(f).split("\n")[0], out.split("\n")[0])
        self.assertLess(time.monotonic() - t1, 1.0)
        if VERBOSE:
            print(f"(first diagnostics took {t1 - t0:.1f}s)")

        # goalState inside plus_zero's proof (line of "  rw[]").
        ln = lines.index("  rw[]") + 1
        gs = show("goalState", M.goal_state(f, ln, 3))
        self.assertIn("Theorem plus_zero", gs)
        self.assertIn("n + 0 = n", gs)
        self.assertNotIn("\x1b", gs)
        gs0 = show("goalState outside", M.goal_state(f, 1, 1))
        self.assertTrue(gs0.startswith("null"), gs0)

        # hover on `rw`.
        hv = show("hover rw", M.hover(f, ln, 4))
        self.assertIn("val rw", hv)
        self.assertIn("tactic", hv)

        # hover on plus_zero after multibyte characters on the same line:
        # only lands on it if columns are converted from code points to
        # bytes (12 bytes off otherwise).  In a comment, because the server
        # answers no hover at all on a line with a multibyte *string
        # literal*, e.g. `val pz = ("∀", plus_zero)`.
        pl = lines.index("val (*‘’‘’‘’*) pz = plus_zero")
        col = lines[pl].index("plus_zero") + 3
        hv2 = show("hover plus_zero", M.hover(f, pl + 1, col))
        self.assertIn("val plus_zero", hv2)
        self.assertIn("n + 0 = n", hv2)

        ev = show("eval", M.eval(f, 'PolyML.print (DB.find "ADD_COMM");', 120))
        self.assertIn("ADD_COMM", ev)
        self.assertTrue(ev.startswith("eval: ok"), ev)
        ev2 = show("eval print", M.eval(f, 'val _ = print "hello from eval\\n";', 120))
        self.assertIn("hello from eval", ev2)
        ev3 = show("eval error", M.eval(f, "val _ = 3 + true;", 120))
        self.assertTrue(ev3.startswith("eval: 1 error"), ev3)
        self.assertIn("error at 1:9", ev3)

        ds = show("documentSymbol", M.document_symbol(f))
        for name in ("truth", "plus_zero", "pz"):
            self.assertIn(name, ds)
        self.assertRegex(ds, r"(?m)^%d: plus_zero" % (lines.index("Theorem plus_zero:") + 1))

        ws = show("workspaceSymbol", M.workspace_symbol(f, "ADD_COMM"))
        self.assertIn("ADD_COMM", ws)
        self.assertIn("arithmeticScript.sml", ws)

        cp = show("completion", M.completion(f, pl + 1, col))
        self.assertIn("plus_zero", cp)

        df = show("definition", M.definition(f, pl + 1, col))
        self.assertTrue(df.startswith("No definition") or ":" in df, df)

        rf = show("references", M.references(f, pl + 1, col))
        self.assertTrue(rf.startswith("Not supported") or rf.startswith("No references"), rf)

        # An edit is picked up from disk.
        write("e2ecleanScript.sml", CLEAN + "\nval bad = 3 + true\n")
        out2 = show("diagnostics after edit", M.diagnostics(f, wait=180))
        self.assertTrue(out2.startswith("PROBLEMS: 1 error"), out2)
        write("e2ecleanScript.sml", CLEAN)
        out3 = show("diagnostics after revert", M.diagnostics(f, wait=180))
        self.assertTrue(out3.startswith("OK"), out3)

    def test_type_error(self):
        src = ("Theory e2ebad\n\n"
               "val x = (\"‘’\", 3 + true)\n")
        f = write("e2ebadScript.sml", src)
        out = show("diagnostics type error", M.diagnostics(f, wait=180))
        self.assertTrue(out.startswith("PROBLEMS:"), out)
        self.assertRegex(out, r"Type error|Can't unify")
        m = re.search(r"(?m)^  3:(\d+)", out)
        self.assertIsNotNone(m, out)
        self.assertLessEqual(int(m.group(1)), len(src.split("\n")[2]) + 1)

    def test_failing_tactic(self):
        src = ("Theory e2efail\n"
               "Ancestors bool\n\n"
               "Theorem bar: !p:bool. p /\\ q ==> r\n"
               "Proof\n"
               "  ACCEPT_TAC TRUTH\n"
               "QED\n")
        f = write("e2efailScript.sml", src)
        out = show("diagnostics failing tactic", M.diagnostics(f, wait=180))
        self.assertTrue(out.startswith("PROBLEMS:"), out)
        self.assertIn("failed: bar", out)
        gs = show("goalState failing tactic", M.goal_state(f, 6, 19))
        self.assertIn("Theorem bar", gs)
        self.assertIn("Error:", gs)

    def test_cheat(self):
        src = ("Theory e2echeat\n\n"
               "Theorem fine:\n"
               "  T\n"
               "Proof\n"
               "  ACCEPT_TAC TRUTH\n"
               "QED\n\n"
               "Theorem cheaty:\n"
               "  F\n"
               "Proof\n"
               "  cheat\n"
               "QED\n")
        f = write("e2echeatScript.sml", src)
        out = show("diagnostics cheat", M.diagnostics(f, wait=180))
        # The pool replays `cheat` successfully: its verdict compares
        # hypotheses, not oracle tags, so a cheated proof is reported as
        # `proved`.  Only assert it isn't a failure.
        self.assertNotIn("failed:", out)
        self.assertIn("Proofs:", out)
        # ... so the wrapper's text scan must keep the verdict from being OK.
        self.assertFalse(out.startswith("OK"), out)
        self.assertTrue(out.startswith("CHEATS: 1 proof uses cheat (cheaty)"), out)
        self.assertIn("cheaty (line 12)", out)

    def test_blocked_ancestor(self):
        src = ("Theory e2eblk\nAncestors nosuchtheory\n\n"
               "Theorem x:\n  T\nProof\n  ACCEPT_TAC TRUTH\nQED\n")
        f = write("e2eblkScript.sml", src)
        out = show("diagnostics blocked", M.diagnostics(f, wait=180))
        self.assertTrue(out.startswith("BLOCKED:"), out)
        self.assertIn("nosuchtheory", out)
        self.assertIn("compile is blocked", M.goal_state(f, 7, 3))
        self.assertIn("x (constant)", M.document_symbol(f))
        self.assertIn("retryCompile", M.retry_compile(f))
        self.assertTrue(M.diagnostics(f, wait=180).startswith("BLOCKED:"))

    def test_deleted_theorem_leaves_tally(self):
        def thm(n):
            return f"Theorem {n}:\n  T\nProof\n  ACCEPT_TAC TRUTH\nQED\n\n"
        f = write("e2edelScript.sml", "Theory e2edel\n\n" + thm("one") + thm("two"))
        self.assertIn("2/2 proofs proved", M.diagnostics(f, wait=180))
        write("e2edelScript.sml", "Theory e2edel\n\n" + thm("one"))
        out = show("diagnostics after deleting a theorem", M.diagnostics(f, wait=180))
        self.assertTrue(out.startswith("OK: compiled with no errors; 1/1 proofs proved"), out)

    def test_restart(self):
        f = write("e2erstScript.sml", "Theory e2erst\n\nval x = 1\n")
        self.assertTrue(M.diagnostics(f, wait=180).startswith("OK"))
        self.assertIn("Started a new server", M.restart(f))
        self.assertTrue(M.diagnostics(f, wait=180).startswith("OK"))

    def test_looping_proof_then_edit_across_proofs(self):
        def src(early, later):
            return ("Theory e2eloop\n\n"
                    f"Theorem early:\n  T\nProof\n  {early}\nQED\n\n"
                    f"Theorem later:\n  T\nProof\n  {later}\nQED\n")
        loop = ("(fn g => let fun loop () = (OS.Process.sleep "
                "(Time.fromMilliseconds 50); loop ()) in loop () end)")
        f = write("e2eloopScript.sml", src("ACCEPT_TAC TRUTH", loop))
        out = show("diagnostics looping", M.diagnostics(f, wait=20))
        self.assertTrue(out.startswith("INCOMPLETE after 20s: 1/2 proved, 1 still checking"), out)
        self.assertRegex(out, r"later \(line 9\): checking for \d+s \(finished proofs here took")
        # Other calls answer while diagnostics waits.
        bg = {}
        t = threading.Thread(target=lambda: bg.update(out=M.diagnostics(f, wait=10)))
        t.start()
        time.sleep(1)
        t0 = time.monotonic()
        self.assertIn("Theorem early", M.goal_state(f, 6, 3))
        self.assertLess(time.monotonic() - t0, 5)
        t.join(30)
        self.assertTrue(bg["out"].startswith("INCOMPLETE"), bg["out"])
        # One write that changes both proofs.  The server reuses the check
        # still running on `later` unless the wrapper steps around it (see
        # session.proof_edit_start), so this would stay `checking`.
        write("e2eloopScript.sml", src("ACCEPT_TAC  TRUTH", "NO_TAC"))
        out = show("diagnostics after edit across proofs", M.diagnostics(f, wait=120))
        self.assertTrue(out.startswith("PROBLEMS:"), out)
        self.assertIn("failed: later", out)


if __name__ == "__main__":
    unittest.main()
