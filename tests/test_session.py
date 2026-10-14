"""FileSession pieces that need no server."""

import os
import tempfile
import unittest

from hol_lsp_mcp.config import Config
from hol_lsp_mcp.session import FileSession, proof_edit_start

THY = """Theory t

Theorem a:
  T
Proof
  simp[]
QED

Theorem b:
  T
Proof
  metis_tac[]
QED

Definition d:
  d = 1
End
"""


def edit(text, old, new):
    assert old in text, old
    return text.replace(old, new, 1)


class ProofEditStartTest(unittest.TestCase):
    def test_inside_one_proof(self):
        self.assertIsNone(proof_edit_start(THY, edit(THY, "simp[]", "rw[]")))

    def test_two_proofs(self):
        new = edit(edit(THY, "simp[]", "rw[]"), "metis_tac[]", "fs[]")
        self.assertEqual(proof_edit_start(THY, new), THY.index("Theorem a"))

    def test_proof_and_definition_below(self):
        new = edit(edit(THY, "metis_tac[]", "fs[]"), "d = 1", "d = 2")
        self.assertEqual(proof_edit_start(THY, new), THY.index("Theorem b"))

    def test_starts_outside_a_proof(self):
        new = edit(edit(THY, "Theorem a:\n  T", "Theorem a:\n  T ∧ T"), "metis_tac[]", "fs[]")
        self.assertIsNone(proof_edit_start(THY, new))
        self.assertIsNone(proof_edit_start(THY, edit(THY, "d = 1", "d = 2")))

    def test_qed_in_a_comment_does_not_end_the_proof(self):
        old = edit(THY, "simp[]", "simp[] (* not the\nQED of a *)\n  >> rw[]")
        new = edit(edit(old, "simp[]", "gs[]"), "rw[]", "fs[]")
        self.assertIsNone(proof_edit_start(old, new))


class StubClient:
    def __init__(self):
        self.sent = []

    def notify(self, method, params):
        self.sent.append((method, params))
        return len(self.sent)


class SyncTest(unittest.TestCase):
    def test_edit_across_proofs_touches_the_declaration_first(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "tScript.sml")
            with open(path, "w", encoding="utf-8") as f:
                f.write(THY)
            s = FileSession(path, Config(hol_bin="hol", extra_path=None, max_servers=1))
            s.client = StubClient()
            s.sync()
            new = edit(edit(THY, "simp[]", "rw[]"), "metis_tac[]", "fs[]")
            with open(path, "w", encoding="utf-8") as f:
                f.write(new)
            s.sync()
            sent = s.client.sent
            self.assertEqual([m for m, _ in sent],
                             ["textDocument/didOpen"] + ["textDocument/didChange"] * 2)
            at = THY.index("Theorem a")
            self.assertEqual(sent[1][1]["contentChanges"][0]["text"], new[:at] + "\n" + new[at:])
            self.assertEqual(sent[2][1]["contentChanges"][0]["text"], new)
            self.assertEqual([p["textDocument"]["version"] for _, p in sent[1:]], [2, 3])
            self.assertEqual(s.version, 3)
            self.assertEqual(s.sync_seq, 3)
            # A tactic-only edit is sent as is.
            with open(path, "w", encoding="utf-8") as f:
                f.write(edit(new, "fs[]", "gs[]"))
            s.sync()
            self.assertEqual(len(sent), 4)


if __name__ == "__main__":
    unittest.main()
