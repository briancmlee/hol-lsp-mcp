import unittest

from hol_lsp_mcp.cheats import find_cheats, mask
from hol_lsp_mcp.format import format_diagnostics

SRC = '''Theory t

(* cheat in a comment (* nested cheat *) still comment cheat *)
val s = "cheat \\" cheat"
val cheater = 1
val my_cheat = 2

Theorem foo:
  T
Proof
  strip_tac >> cheat
QED

Theorem bar[simp]:
  F
Proof
  cheat
QED

val baz = store_thm("baz", ``T``, cheat)

val loose = cheat
'''


class CheatsTest(unittest.TestCase):
    def test_mask_keeps_offsets(self):
        m = mask(SRC)
        self.assertEqual(len(m), len(SRC))
        self.assertEqual(m.count("\n"), SRC.count("\n"))
        self.assertNotIn("nested", m)

    def test_find(self):
        self.assertEqual(find_cheats(SRC),
                         [("foo", 11), ("bar", 17), ("baz", 20), (None, 22)])

    def test_none(self):
        self.assertEqual(find_cheats("Theorem a: T\nProof\n  simp[] (* cheat *)\nQED\n"), [])

    def test_verdict_not_ok(self):
        pos = lambda uri, p: (p.get("line", 0) + 1, p.get("character", 0) + 1)
        r = {"status": "settled", "items": [], "why": "",
             "proofs": {"foo": {"name": "foo", "status": "proved"},
                        "bar": {"name": "bar", "status": "proved"}},
             "cheats": [("foo", 11), ("bar", 17)]}
        out = format_diagnostics(r, pos)
        first = out.split("\n")[0]
        self.assertFalse(first.startswith("OK"), out)
        self.assertTrue(first.startswith("CHEATS: 2 proofs use cheat (foo, bar)"), out)
        self.assertIn("  foo (line 11)", out)
        self.assertIn("  bar (line 17)", out)
        r["items"] = [{"severity": 1, "message": "boom",
                       "range": {"start": {"line": 0, "character": 0}}}]
        first = format_diagnostics(r, pos).split("\n")[0]
        self.assertTrue(first.startswith("PROBLEMS: 1 error"), first)
        self.assertIn("2 proofs use cheat (foo, bar)", first)


if __name__ == "__main__":
    unittest.main()
