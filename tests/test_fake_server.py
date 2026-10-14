"""Client/session/manager behaviour against tests/fake_hol_lsp.py."""

import os
import shutil
import stat
import sys
import tempfile
import time
import unittest

from hol_lsp_mcp.config import Config
from hol_lsp_mcp.manager import Manager

HERE = os.path.dirname(os.path.abspath(__file__))


class FakeServerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="hol_lsp_mcp_fake_")
        self.bin = os.path.join(self.dir, "fake-hol")
        with open(self.bin, "w") as f:
            f.write(f"#!/bin/sh\nexec '{sys.executable}' '{HERE}/fake_hol_lsp.py' \"$@\"\n")
        os.chmod(self.bin, os.stat(self.bin).st_mode | stat.S_IEXEC)
        self.m = Manager(Config(hol_bin=self.bin, extra_path=None, max_servers=2,
                                init_timeout=20, request_timeout=20, quiet_period=0.3))

    def tearDown(self):
        self.m.shutdown()
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, name, text):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def test_clean_then_edit_then_cached(self):
        f = self.write("aScript.sml", "Theory a\nTheorem foo:\n  ∀x. x\nProof\n  rw[]\nQED\n")
        out = self.m.diagnostics(f, timeout=20)
        self.assertIn("OK: compiled with no errors; 1/1 proofs proved", out)
        self.assertIn("fake heap warning", out)  # showMessage surfaced once
        # Edit: BAD after a multibyte char -> column in code points.
        self.write("aScript.sml", "Theory a\nval ‘x’ = BAD\nTheorem foo:\n  T\nProof\n  FAIL\nQED\n")
        out = self.m.diagnostics(f, timeout=20)
        self.assertTrue(out.startswith("PROBLEMS: 1 error, 1 failed"), out)
        self.assertIn("2:11-14 error:", out)
        self.assertIn("failed: foo (line 3): tactic FAIL failed", out)
        self.assertNotIn("fake heap warning", out)
        t0 = time.monotonic()
        again = self.m.diagnostics(f, timeout=20)
        self.assertLess(time.monotonic() - t0, 0.5)
        self.assertEqual(again, out)

    def test_positions_in_and_out(self):
        f = self.write("bScript.sml", "Theory b\n  ‘∀x’ y\n")
        self.m.diagnostics(f, timeout=20)
        # column 6 (1-based, code points) is the closing quote, after
        # "  ‘∀x": 2 + 3 + 3 + 1 = 9 bytes.
        self.assertIn("hover at 1:9", self.m.hover(f, 2, 6))
        self.assertNotIn("\x1b", self.m.hover(f, 2, 6))
        gs = self.m.goal_state(f, 2, 6)
        self.assertIn("Theorem foo, step 9", gs)
        self.assertIn("[branch 1 of 2 of THENL]", gs)
        self.assertIn("x = x", gs)
        self.assertNotIn("\x1b", gs)
        # The fake answers line 1 (0-based), byte 5: just past "  ‘", so
        # the ∀ at column 4.
        self.assertTrue(self.m.definition(f, 1, 1).endswith("bScript.sml:2:4"))
        self.assertIn("unknown method", self.m.references(f, 1, 1))
        ev = self.m.eval(f, "1 + 2", 10)
        self.assertIn("val it = 3: int", ev)
        self.assertIn("warning at 1:1-1:3: warn", ev)

    def test_utf16_negotiated(self):
        os.environ["FAKE_UTF16"] = "1"
        try:
            f = self.write("cScript.sml", "Theory c\n𝔸 BAD\n")
            out = self.m.diagnostics(f, timeout=20)
        finally:
            del os.environ["FAKE_UTF16"]
        self.assertIn("2:3-6 error:", out)

    def test_blocked_and_retry(self):
        f = self.write("dScript.sml", "Theory d\nAncestors missing\n")
        out = self.m.diagnostics(f, timeout=20)
        self.assertTrue(out.startswith("BLOCKED:"), out)
        self.assertIn("cannot load missingTheory", out)
        self.assertIn("Sent $/hol/retryCompile", self.m.retry_compile(f))
        out = self.m.diagnostics(f, timeout=20)
        self.assertTrue(out.startswith("OK"), out)

    def test_lru_cap(self):
        files = [self.write(f"f{i}Script.sml", f"Theory f{i}\n") for i in range(3)]
        for f in files:
            self.m.diagnostics(f, timeout=20)
        running = self.m.running_files()
        self.assertEqual(len(running), 2)
        self.assertNotIn(files[0], running)

    def test_dead_server_restarts(self):
        f = self.write("eScript.sml", "Theory e\nDIE\n")
        out = self.m.diagnostics(f, timeout=20)
        self.assertIn("server died", out)
        self.write("eScript.sml", "Theory e\n")
        out = self.m.diagnostics(f, timeout=20)
        self.assertIn("had exited (code 3)", out)
        self.assertIn("dying as asked", out)
        self.assertIn("OK: compiled", out)

    def test_restart_and_timeout(self):
        f = self.write("gScript.sml", "Theory g\n")
        self.assertIn("Server running", self.m.restart(f))
        self.assertIn("OK", self.m.diagnostics(f, timeout=20))
        self.write("gScript.sml", "Theory g\nTheorem t:\n T\nProof\nQED\n")
        out = self.m.diagnostics(f, timeout=0.05)
        self.assertTrue(out.startswith("INCOMPLETE"), out)

    def test_missing_file(self):
        self.assertIn("no such file", self.m.diagnostics(os.path.join(self.dir, "nope.sml")))


if __name__ == "__main__":
    unittest.main()
