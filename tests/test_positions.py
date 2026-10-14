import unittest

from hol_lsp_mcp.positions import (UTF8, UTF16, UTF32, chars_to_units, from_lsp,
                                   to_lsp, units_to_chars)

TEXT = "Theorem foo:\n  ∀x. x ⇒ ‘y’\nProof\n  𝔸 rw[]\nQED\n"


class PositionsTest(unittest.TestCase):
    def test_ascii_identity(self):
        for enc in (UTF8, UTF16, UTF32):
            self.assertEqual(to_lsp(TEXT, 1, 9, enc), {"line": 0, "character": 8})
            self.assertEqual(from_lsp(TEXT, {"line": 0, "character": 8}, enc), (1, 9))

    def test_utf8_multibyte(self):
        # line 2 is "  ∀x. x ⇒ ‘y’"; column of "x" after "∀" is 4 (1-based).
        p = to_lsp(TEXT, 2, 4, UTF8)
        self.assertEqual(p, {"line": 1, "character": 2 + 3})
        self.assertEqual(from_lsp(TEXT, p, UTF8), (2, 4))
        # 'y' inside ‘y’: chars "  ∀x. x ⇒ ‘" = 11 chars -> column 12
        line = "  ∀x. x ⇒ ‘y’"
        col = line.index("y") + 1
        p = to_lsp(TEXT, 2, col, UTF8)
        self.assertEqual(p["character"], len(line[:col - 1].encode()))
        self.assertEqual(from_lsp(TEXT, p, UTF8), (2, col))

    def test_utf16_astral(self):
        # line 4 is "  𝔸 rw[]": 𝔸 is outside the BMP, two utf-16 units.
        line = "  𝔸 rw[]"
        col = line.index("r") + 1
        p = to_lsp(TEXT, 4, col, UTF16)
        self.assertEqual(p["character"], col - 1 + 1)
        self.assertEqual(from_lsp(TEXT, p, UTF16), (4, col))
        self.assertEqual(to_lsp(TEXT, 4, col, UTF32)["character"], col - 1)
        self.assertEqual(to_lsp(TEXT, 4, col, UTF8)["character"], col - 1 + 3)

    def test_roundtrip_every_char(self):
        lines = TEXT.split("\n")
        for enc in (UTF8, UTF16, UTF32):
            for li, line in enumerate(lines, 1):
                for col in range(1, len(line) + 2):
                    self.assertEqual(from_lsp(TEXT, to_lsp(TEXT, li, col, enc), enc), (li, col),
                                     (enc, li, col))

    def test_clamping_and_mid_char(self):
        line = "a∀b"
        self.assertEqual(units_to_chars(line, 2, UTF8), 1)  # inside ∀
        self.assertEqual(units_to_chars(line, 4, UTF8), 2)
        self.assertEqual(units_to_chars(line, 99, UTF8), 3)
        self.assertEqual(chars_to_units(line, 99, UTF8), 5)
        self.assertEqual(to_lsp(TEXT, 99, 5, UTF8), {"line": 98, "character": 0})
        self.assertEqual(to_lsp(TEXT, 0, 0, UTF8), {"line": 0, "character": 0})

    def test_crlf(self):
        text = "a\r\n‘b’\r\n"
        p = to_lsp(text, 2, 3, UTF8)
        self.assertEqual(p, {"line": 1, "character": 4})
        self.assertEqual(from_lsp(text, p, UTF8), (2, 3))

    def test_no_text(self):
        self.assertEqual(from_lsp(None, {"line": 3, "character": 7}, UTF8), (4, 8))


if __name__ == "__main__":
    unittest.main()
