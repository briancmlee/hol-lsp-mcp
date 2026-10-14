"""LSP base-protocol framing: `Content-Length` headers, CRLF separated."""

from __future__ import annotations

import json
from typing import Any


def encode(msg: dict[str, Any]) -> bytes:
    """Frame one JSON-RPC message."""
    body = json.dumps(msg, ensure_ascii=False).encode("utf-8")
    return b"Content-Length: %d\r\n\r\n" % len(body) + body


class Decoder:
    """Incremental decoder: feed it bytes as they arrive, get messages back.

    Headers are separated from the body by CRLF CRLF; only
    `Content-Length` is used.  A header block without a usable
    `Content-Length`, or a body that isn't JSON, is dropped and counted in
    `errors` rather than wedging the stream.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self.errors = 0

    def feed(self, data: bytes) -> list[Any]:
        self._buf.extend(data)
        out: list[Any] = []
        while True:
            end = self._buf.find(b"\r\n\r\n")
            if end < 0:
                break
            header = bytes(self._buf[:end]).decode("ascii", "replace")
            length = None
            for line in header.split("\r\n"):
                name, sep, value = line.partition(":")
                if sep and name.strip().lower() == "content-length":
                    try:
                        length = int(value.strip())
                    except ValueError:
                        length = None
            if length is None or length < 0:
                self.errors += 1
                del self._buf[: end + 4]
                continue
            start = end + 4
            if len(self._buf) < start + length:
                break
            body = bytes(self._buf[start : start + length])
            del self._buf[: start + length]
            try:
                out.append(json.loads(body.decode("utf-8", "replace")))
            except ValueError:
                self.errors += 1
        return out

    @property
    def pending(self) -> int:
        """Bytes buffered but not yet forming a complete message."""
        return len(self._buf)
