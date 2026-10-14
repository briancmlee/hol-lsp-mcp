"""A tiny stand-in for `hol lsp`, enough to exercise the wrapper's protocol
handling without a HOL build.  Stdlib only.

Behaviour on each didOpen/didChange ("compile"):
  - asks the client `window/workDoneProgress/create` (must be answered)
  - `$/progress` begin, publishDiagnostics(version), `$/progress` end,
    `$/compileCompleted`
  - every word `BAD` gives an error diagnostic at its position (utf-8 bytes)
  - `Ancestors missing` gives `$/compileBlocked` instead
  - every `Theorem NAME` is announced `checking`, then after 0.3s `proved`,
    or `failed` if its proof contains `FAIL`
  - `DIE` in the text makes the process exit
"""

import json
import os
import re
import sys
import threading
import time

out_lock = threading.Lock()
ENC = "utf-8"
bound_uri = None
texts = {}
versions = {}
gen = 0


def send(msg):
    body = json.dumps(msg).encode()
    with out_lock:
        sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
        sys.stdout.buffer.flush()


def notify(method, params):
    send({"jsonrpc": "2.0", "method": method, "params": params})


def read_msg():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        line = line.rstrip(b"\r\n")
        if not line:
            break
        k, _, v = line.decode().partition(":")
        headers[k.strip().lower()] = v.strip()
    n = int(headers["content-length"])
    return json.loads(sys.stdin.buffer.read(n))


def pos_of(text, byte_off):
    b = text.encode()[:byte_off]
    line = b.count(b"\n")
    col = byte_off - (b.rfind(b"\n") + 1)
    if ENC == "utf-16":
        line_start = b.rfind(b"\n") + 1
        col = len(b[line_start:].decode().encode("utf-16-le")) // 2
    return {"line": line, "character": col}


def compile_(uri, text, version, my_gen):
    send({"jsonrpc": "2.0", "id": f"srv{version}", "method": "window/workDoneProgress/create",
          "params": {"token": str(version)}})
    if "DIE" in text:
        sys.stderr.write("fake server: dying as asked\n")
        sys.stderr.flush()
        os._exit(3)
    if "Ancestors missing" in text:
        i = text.encode().index(b"missing")
        notify("textDocument/publishDiagnostics", {"uri": uri, "version": version, "diagnostics": [
            {"range": {"start": pos_of(text, i), "end": pos_of(text, i + 7)}, "severity": 1,
             "message": "cannot load missingTheory"}]})
        notify("$/compileBlocked", {"uri": uri, "modules": ["missing"],
                                    "message": "cannot load missingTheory: Cannot find file"})
        return
    notify("$/progress", {"token": str(version), "value": {"kind": "begin", "title": "compile"}})
    time.sleep(0.2)
    if my_gen != gen:
        notify("$/progress", {"token": str(version), "value": {"kind": "end"}})
        notify("$/compileInterrupted", {"uri": uri})
        return
    diags = []
    for m in re.finditer(rb"BAD", text.encode()):
        diags.append({"range": {"start": pos_of(text, m.start()), "end": pos_of(text, m.end())},
                      "severity": 1, "message": "Type error:\n  BAD is bad"})
    notify("textDocument/publishDiagnostics", {"uri": uri, "version": version, "diagnostics": diags})
    notify("$/progress", {"token": str(version), "value": {"kind": "end"}})
    thms = [(m.group(1), m.start(), text.find("QED", m.start()))
            for m in re.finditer(r"Theorem (\w+)", text)]
    for name, start, _ in thms:
        notify("$/proofStates", {"uri": uri, "states": [
            {"pos": pos_of(text, len(text[:start].encode())), "name": name, "status": "checking"}]})
    notify("$/compileCompleted", {"uri": uri})

    def finish():
        time.sleep(0.3)
        for name, start, end in thms:
            body = text[start:end]
            st = {"pos": pos_of(text, len(text[:start].encode())), "name": name,
                  "status": "failed" if "FAIL" in body else "proved"}
            if "FAIL" in body:
                st["detail"] = "tactic FAIL failed"
            notify("$/proofStates", {"uri": uri, "states": [st]})
    threading.Thread(target=finish, daemon=True).start()


def main():
    global ENC, bound_uri, gen
    while True:
        msg = read_msg()
        if msg is None:
            return
        method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if method is None:
            continue  # a reply to our request
        if method == "initialize":
            offered = ((params.get("capabilities") or {}).get("general") or {}).get("positionEncodings") or []
            ENC = "utf-8" if "utf-8" in offered and not os.environ.get("FAKE_UTF16") else "utf-16"
            send({"jsonrpc": "2.0", "id": mid, "result": {"capabilities": {
                "positionEncoding": ENC, "textDocumentSync": 1, "hoverProvider": True}}})
            notify("window/logMessage", {"type": 3, "message": "started"})
        elif method == "initialized":
            notify("window/showMessage", {"type": 2, "message": "fake heap warning"})
        elif method in ("textDocument/didOpen", "textDocument/didChange"):
            td = params["textDocument"]
            uri = td["uri"]
            if bound_uri is None:
                bound_uri = uri
            text = td["text"] if method.endswith("didOpen") else params["contentChanges"][0]["text"]
            texts[uri] = text
            versions[uri] = td["version"]
            gen += 1
            threading.Thread(target=compile_, args=(uri, text, td["version"], gen), daemon=True).start()
        elif method == "$/hol/retryCompile":
            gen += 1
            uri = params["uri"]
            threading.Thread(target=compile_, args=(uri, texts[uri].replace("Ancestors missing", ""), versions[uri], gen),
                             daemon=True).start()
        elif method == "textDocument/hover":
            p = params["position"]
            send({"jsonrpc": "2.0", "id": mid, "result": {"contents": {
                "kind": "markdown", "value": f"```\nhover at {p['line']}:{p['character']} \x1b[1mbold\x1b[0m\n```"}}})
        elif method == "$/hol/goalState":
            p = params["position"]
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "theorem": "foo", "step": p["character"], "goals": [], "status": "ok",
                "pretty": "\x1b[34mx\x1b[0m = x", "context": ["branch 1 of 2 of THENL"],
                "note": None, "error": None}})
        elif method == "textDocument/definition":
            send({"jsonrpc": "2.0", "id": mid, "result": [{"uri": params["textDocument"]["uri"], "range": {
                "start": {"line": 1, "character": 5}, "end": {"line": 1, "character": 6}}}]})
        elif method == "$/eval":
            send({"jsonrpc": "2.0", "id": mid, "result": [
                {"kind": "toplevelOut", "pos": {"line": 0, "character": 0}, "body": "val it = 3: int\n"},
                {"kind": "error", "hard": False, "pos": {"start": {"line": 0, "character": 0},
                                                         "end": {"line": 0, "character": 2}}, "msg": "warn"},
                {"kind": "compileCompleted"}]})
        elif method == "shutdown":
            send({"jsonrpc": "2.0", "id": mid, "result": None})
        elif method == "exit":
            return
        elif mid is not None:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}})


if __name__ == "__main__":
    main()
