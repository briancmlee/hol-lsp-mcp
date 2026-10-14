# Issues to report to HOL4 (`hol lsp`)

Notes for HOL's language server (`tools-poly/lsp`), found while writing
hol-lsp-mcp. Nothing here has been filed yet. All were seen at HOL commit
652d1f0aa (`develop`, 2026-09-27).

## 1. An edit that runs from one proof body into the next leaves the second proof's old check running

**Issue text.**

With `--lsp-check-proofs`, change two proofs in one `didChange`, e.g. a
full-text sync after a save that touched both. The second proof is never
re-checked. Its old check keeps running, and the server reports that old
check's state for the new text. If the old tactic loops, the proof shows
`checking` forever, even when its new tactic fails at once (under Holmake
the new text fails in under 3 s).

Steps to reproduce:

```sml
Theory spanedit

Theorem early:
  T
Proof
  ACCEPT_TAC TRUTH
QED

Theorem later:
  T /\ T
Proof
  (fn g => let fun loop () = (OS.Process.sleep (Time.fromMilliseconds 50); loop ()) in loop () end)
QED
```

1. Open it with `hol lsp --lsp-check-proofs` and wait for `early` to be
   `proved` while `later` is `checking`.
2. Send one full-text `didChange` that changes `early`'s tactic to
   `ACCEPT_TAC  TRUTH` (one extra space) and `later`'s tactic to `NO_TAC`.
3. `early` is cancelled and re-checked. `later` is never announced again
   and stays `checking`. Expected: `later` is reported `failed`.

Cause. `startCompile` passes
`inProofBody = proofBodyAt (#text text) editOff` to `invalidateProofs`.
`editOff` is where the edit *starts*, so an edit that starts in `early`'s
body counts as confined to `early`, and only `early` is cancelled
(`cancelProofAt`). The pass then re-elaborates and re-enqueues `later`.
`DeferredProofs.check` matches it by name to the entry still running the
old tactic and keeps that entry, so the new tactic never runs. The
`tacticOnlyEdit` computation a few lines below already brackets the change
from both ends (common prefix and suffix against `compiledText`), but
`invalidateProofs` does not use it.

**Patch** (branch `lsp-failing-proof-fix` in a local HOL clone, commit
0c6958153, not pushed): in `startCompile`, move the `tacticOnlyEdit` binding
above the `invalidateProofs` calls. Then pass

```sml
val editedProofBody =
  case (proofBodyAt (#text text) editOff, tacticOnlyEdit) of
      (SOME d, SOME {decStart, ...}) =>
        if d = decStart then SOME d else NONE
    | _ => NONE
```

as `inProofBody`. A proof is cancelled alone only when both the edit mark
and the two-ended bracket put the whole change inside that one body.
Anything else takes the existing conservative branch (`cancelProofsAtOrAfter`
from the resume point). Requiring both keeps the existing behaviour for a
retraction, which marks a declaration's start, outside any body. Adds
`test_edit_spanning_two_proofs_rechecks_both` to
`tools-poly/lsp/tests/lsp_tests.py`. That test fails before the patch.
With the patch, the whole `--requires none` suite passes (153 tests). With
the patch, the original case (a 50-proof theory where one write changed two
proofs) reports the failure 2 s after the edit instead of never.

Installing it is not a drop-in. `server.ML` is compiled into `bin/hol`
(`cd tools-poly && polyc -o ../bin/hol hol.ML`, as configure does), unlike
the `*_init.ML` files, which are loaded at run time. Poly/ML then refuses
every heap saved by the old executable ("Saved state was exported from a
different executable"). Also, `src/proofman/Holmakefile` lists `bin/hol` as
a dependency of `hol.state0`. So every heap has to be rebuilt: HOL's own
heaps, and every heap a project such as CakeML built on them.

Remaining gap, not addressed by the patch: `tacticOnlyEdit` compares against
`compiledText`, the text of the last *completed* pass. Pool entries come
from the last pass that reached `checkDeferred`. Usually these are the same
pass. A `didChange` that lands between `checkDeferred` and the
`compiledText` update could in principle let a change be judged against the
wrong text. Recording the text alongside the pool's entries would close
this.

**Client workaround** (in hol-lsp-mcp, `session.proof_edit_start`): when a
change starts inside a `Proof ... QED` body and runs past that `QED`, first
send the new text with a blank line inserted before that declaration, then
the real text. The edit mark is then the declaration's start, and the
server takes the conservative branch.

## 2. `$/hol/retryCompile` parameters differ from `lsp-server.md`

`Manual/Developers/lsp-server.md` documents the notification's params as
`{ uri: URI }`. The server decodes them with `textDocumentId`, i.e.
`{ textDocument: { uri: URI } }`. A client following the document sends a
notification that the server silently ignores. Either the document or the
decoder should change. Accepting both shapes would keep existing clients
working.

## 3. Hover returns null on any line containing a string literal with non-ASCII characters

On a line such as

```sml
val pz = ("∀", plus_zero)
```

`textDocument/hover` returns `null` at every offset on the line, including
on `plus_zero`. With the multibyte characters in a comment instead, as in
`val (*‘’‘’‘’*) pz = plus_zero`, hover on `plus_zero` works at the same
kind of column, so the client's conversion of positions is not the problem.
Seen with the `utf-8` position encoding. The hover path seems to give up on
(or mismeasure) a line with a multibyte character inside a string token.
