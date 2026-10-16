"""Footprint measurement, default limits and the soft-limit notices."""

import os
import unittest

from hol_lsp_mcp import format as fmt
from hol_lsp_mcp.memory import footprint_mb


class MemoryTest(unittest.TestCase):
    def test_footprint_of_self(self):
        mb = footprint_mb(os.getpid())
        self.assertIsNotNone(mb)
        self.assertGreater(mb, 1)

    def test_footprint_of_missing_process(self):
        self.assertIsNone(footprint_mb(2 ** 22 + 12345))


def pos(_uri, p):
    return (p["line"] + 1, p["character"] + 1)


def result(**kw):
    r = {"status": "incomplete", "why": "still checking", "waited": 25, "items": [],
         "proofs": {}, "cheats": [], "checking": {}, "baseline": None,
         "footprint_mb": 900,
         "limits": {"heap_mb": 6000, "soft_mb": 2000, "soft_secs": 180, "soft_factor": 10}}
    r.update(kw)
    return r


class NoticeTest(unittest.TestCase):
    def test_quiet_when_within_limits(self):
        r = result(proofs={"a": {"name": "a", "status": "checking"}}, checking={"a": 30})
        self.assertNotIn("LOOPING", fmt.format_diagnostics(r, pos))

    def test_long_proof(self):
        r = result(proofs={"a": {"name": "a", "status": "checking",
                                 "pos": {"line": 9, "character": 0}}},
                   checking={"a": 200}, footprint_mb=2500)
        out = fmt.format_diagnostics(r, pos)
        self.assertIn("PROBABLY LOOPING: proof a (line 10) has been checking for 3m20s; "
                      "server footprint 2.4 GB. Cancel it or restart?", out)
        self.assertIn("capped at 6000 MB", out)

    def test_far_slower_than_neighbours(self):
        r = result(proofs={"a": {"name": "a", "status": "checking"},
                           "b": {"name": "b", "status": "proved"}},
                   checking={"a": 70}, baseline=3)
        self.assertIn("PROBABLY LOOPING: proof a has been checking for 1m10s, "
                      "finished proofs here took ≤3s", fmt.format_diagnostics(r, pos))

    def test_large_server_without_slow_proof(self):
        r = result(status="settled", why="", footprint_mb=13000,
                   proofs={"a": {"name": "a", "status": "proved"}})
        out = fmt.format_diagnostics(r, pos)
        self.assertIn("SERVER LARGE: server footprint 12.7 GB, over the soft limit of 2.0 GB", out)
        self.assertTrue(out.startswith("OK"), out)

    def test_interrupt_failure_mentions_cap(self):
        r = result(status="settled", why="",
                   proofs={"a": {"name": "a", "status": "failed", "detail": "Interrupt"}})
        self.assertIn("[Interrupt: perhaps the 6000 MB heap cap]", fmt.format_diagnostics(r, pos))


if __name__ == "__main__":
    unittest.main()
