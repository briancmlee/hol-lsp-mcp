import json
import unittest

from hol_lsp_mcp.framing import Decoder, encode


class FramingTest(unittest.TestCase):
    def test_roundtrip(self):
        msg = {"jsonrpc": "2.0", "id": 1, "method": "x", "params": {"t": "∀x. x ⇒ ‘y’"}}
        data = encode(msg)
        self.assertTrue(data.startswith(b"Content-Length: "))
        header, body = data.split(b"\r\n\r\n", 1)
        self.assertEqual(int(header.split(b":")[1]), len(body))
        self.assertEqual(Decoder().feed(data), [msg])

    def test_byte_by_byte_and_multiple(self):
        msgs = [{"id": i, "result": "é" * i} for i in range(5)]
        data = b"".join(encode(m) for m in msgs)
        d = Decoder()
        out = []
        for i in range(len(data)):
            out.extend(d.feed(data[i:i + 1]))
        self.assertEqual(out, msgs)
        self.assertEqual(d.pending, 0)

    def test_split_inside_multibyte_char(self):
        data = encode({"r": "⊢"})
        d = Decoder()
        cut = data.index("⊢".encode()) + 1
        self.assertEqual(d.feed(data[:cut]), [])
        self.assertEqual(d.feed(data[cut:]), [{"r": "⊢"}])

    def test_extra_headers_and_case(self):
        body = json.dumps({"a": 1}).encode()
        data = (b"content-type: application/vscode-jsonrpc; charset=utf-8\r\n"
                b"CONTENT-LENGTH: %d\r\n\r\n" % len(body)) + body
        self.assertEqual(Decoder().feed(data), [{"a": 1}])

    def test_bad_header_and_bad_json_are_skipped(self):
        d = Decoder()
        data = b"X-Junk: 1\r\n\r\n" + b"Content-Length: 3\r\n\r\n{x}" + encode({"ok": True})
        self.assertEqual(d.feed(data), [{"ok": True}])
        self.assertEqual(d.errors, 2)


if __name__ == "__main__":
    unittest.main()
