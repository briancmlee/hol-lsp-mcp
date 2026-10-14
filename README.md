# hol-lsp-mcp

> **Status: experimental.** This tool was written largely by an AI coding agent (Claude) and has **not yet been reviewed** by a human. Expect rough edges; use at your own risk.
>
> Reviewed up to commit: _none yet_.

A small [MCP](https://modelcontextprotocol.io) server that lets coding agents
such as Claude Code use HOL4's built-in language server, `bin/hol lsp`.

The tools mirror the server's LSP methods one-to-one, with the same names and
meanings, so if you know `hol lsp` there is nothing new to learn. The wrapper
only adds what an agent can't easily do itself:

- **Server lifecycle.** It runs one `hol lsp --lsp-check-proofs` process per
  file, because the server serves only one script. Each process starts in the
  file's directory, so it picks up that directory's `Holmakefile` and
  `HOLHEAP`. Servers stay alive between calls. At most
  `HOL_LSP_MCP_MAX_SERVERS` run at once, and the least recently used idle one
  is shut down to make room. A server that has died is restarted on the next
  call, and the response includes its stderr tail. All servers are shut down
  when the MCP server exits.
- **File sync from disk.** The agent edits files with its normal tools. Every
  call re-reads the file and sends `didOpen` or a full-text `didChange` if the
  file has changed.
- **Waiting for results.** `diagnostics` waits for the compile of the current
  text to finish, then until no proof is still `checking` and the server has
  been quiet for about a second. If the file hasn't changed since the last
  such answer, it returns immediately.
- **Positions.** All lines and columns are 1-based, and columns count Unicode
  characters, which is how an agent sees a file full of `∀`, `⇒` and `‘’`. The
  wrapper negotiates `utf-8` with the server and converts in both directions.
  It handles `utf-16` too.
- **Flagging `cheat`.** The server's pool checks hypotheses, not oracle tags,
  so it reports a proof that uses `cheat` as `proved`. `diagnostics` therefore
  also scans the file text for `cheat` as a whole word, skipping comments and
  string literals. If it finds any, the verdict is never `OK`: it reads
  `CHEATS: 2 proofs use cheat (foo, bar)`, and each use is listed with its
  enclosing theorem and line.
- **Text output.** Results are concise text written for an agent's context,
  not raw JSON.

## Tools

| tool | server method | returns |
|---|---|---|
| `diagnostics(file, timeout=900)` | `textDocument/publishDiagnostics` + `$/proofStates` | One-line verdict (never `OK` if the text uses `cheat`), proof tally (`proved`/`failed`/`suspended`/`diverged`/`cheated`/`checking`) with the non-proved ones named, then the diagnostics. Waits until the compile has finished and the proof pool has settled. On timeout, returns what it has, marked `INCOMPLETE`. |
| `goalState(file, line, column)` | `$/hol/goalState` | theorem, step, context tags, note, error, goals (`pretty` without ANSI escapes); says so plainly when the result is `null` |
| `eval(file, code, timeout=120)` | `$/eval` | errors and warnings with positions in `code`, `compilerOut`/`toplevelOut`, plus any stderr the server wrote meanwhile (see below) |
| `hover(file, line, column)` | `textDocument/hover` | the hover text |
| `definition(file, line, column)` | `textDocument/definition` | `path:line:column` |
| `references(file, line, column)` | `textDocument/references` | `path:line:column`. Current HOL does not provide this, and the tool says so. |
| `documentSymbol(file)` | `textDocument/documentSymbol` | outline: line, name, kind, detail |
| `workspaceSymbol(file, query)` | `workspace/symbol` (in that file's server) | name, kind, theory, location |
| `completion(file, line, column)` | `textDocument/completion` | labels with details, first 50 |
| `retryCompile(file)` | `$/hol/retryCompile` | sends the notification; call `diagnostics` next |
| `restart(file)` | (wrapper) | kills and restarts that file's server, e.g. after its dependencies were rebuilt |

## Setup

You need Python 3.10 or later and a built HOL (`bin/hol` and `bin/hol.state`).

```sh
cd hol-lsp-mcp
python3 -m venv .venv
.venv/bin/pip install -e .
```

Environment variables:

| variable | default | meaning |
|---|---|---|
| `HOL_BIN` | `$HOLDIR/bin/hol`, else `~/HOL/bin/hol` | the `hol` executable |
| `HOL_LSP_EXTRA_PATH` | unset | prepended to `PATH` for the server processes (e.g. `~/.local/polyml/bin`) |
| `HOL_LSP_MCP_MAX_SERVERS` | `3` | how many servers may run at once. Each one holds a HOL heap. |

Register it with Claude Code at local scope (the default scope):

```sh
claude mcp add hol-lsp \
  -e HOL_BIN=$HOME/HOL/bin/hol \
  -e HOL_LSP_EXTRA_PATH=$HOME/.local/polyml/bin \
  -- $HOME/Programming/hol-lsp-mcp/.venv/bin/hol-lsp-mcp serve
```

### Trying it without an agent

The CLI uses the same core code. Each run starts a fresh server, so the
position-based commands first wait for the compile to finish (`--no-wait`
skips this).

```sh
.venv/bin/hol-lsp-mcp diagnostics path/to/fooScript.sml
.venv/bin/hol-lsp-mcp goalState path/to/fooScript.sml 42 5
.venv/bin/hol-lsp-mcp eval path/to/fooScript.sml 'PolyML.print (DB.find "ADD_COMM");'
.venv/bin/hol-lsp-mcp hover path/to/fooScript.sml 42 5
.venv/bin/hol-lsp-mcp workspaceSymbol path/to/fooScript.sml ADD_COMM
```

## Relation to HOL's language server

`hol lsp` is HOL's own LSP server (see `tools-poly/lsp/README.md` and
`Manual/Developers/lsp-server.md` in the HOL tree). This project is a client
for it, like HOL's eglot and VS Code clients. As both of those do, it turns
proof checking on (`--lsp-check-proofs`), so the server's worker pool really
runs every proof after the fast elaboration pass.

Some server behaviour worth knowing:

- **One server per script.** A server is pinned to the dependencies it loaded
  first. After you rebuild a dependency with `Holmake`, call `restart`. A
  dependency that was *missing* is re-read on `retryCompile`.
- **`cheat` is tallied as `proved`.** The pool's verdict compares hypotheses,
  not oracle tags, so replaying `cheat` succeeds. The wrapper's text scan,
  described above, makes up for this. In the tally, `cheated` means "not being
  checked": the proof was dropped by an edit, or its tactic didn't compile and
  it is taken on trust.
- **The tally is accumulated from transitions.** The server never retracts a
  name, so the wrapper hides `cheated` entries whose name no longer appears in
  the file. Otherwise deleted or renamed theorems would stay in the tally.
- **`eval`** runs in the server process, not in the file's namespace: it sees
  loaded theories and libraries, but not the file's own `val`s. It does not
  echo bindings. `PolyML.print` output comes back in the reply, while `print`
  output goes to the server's stderr, which the wrapper captures and appends.
  The server's automatic `holdep` preloading may log harmless lines such as
  `Cannot find file PolyML.ui`.
- **Hover returns nothing on a line that contains a string literal with
  non-ASCII characters**, e.g. `val p = ("∀", foo)`. This happens at every
  offset on that line, so it isn't a position-conversion problem.

## Tests

```sh
.venv/bin/python -m unittest discover -s tests       # unit tests (no HOL needed)
.venv/bin/python tests/e2e_hol_lsp.py -v              # against a real hol lsp
```

The unit tests cover framing, position conversion, and the session and
lifecycle logic against a fake server (`tests/fake_hol_lsp.py`). The
end-to-end script skips cleanly if HOL isn't built. Set
`HOL_LSP_E2E_VERBOSE=1` to see every tool's output.

## License

BSD-3-Clause; see `LICENSE`.
