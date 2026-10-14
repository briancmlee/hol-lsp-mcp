# Upstream tracker: HOL4 (`hol lsp`, Holmake) and CakeML

Changes we may want to request from, or contribute to, upstream HOL4
(<https://github.com/HOL-Theorem-Prover/HOL>), mainly its language server
(`bin/hol lsp`, `tools-poly/lsp`). **Nothing here has been filed.** The
user decides what to send. The draft texts are written for HOL's
maintainers.

Baselines:

- **local**: `~/HOL`, branch `lsp-walker-budget` = 652d1f0aa (2026-09-27)
  plus three local commits. `bin/hol` was rebuilt 2026-10-15 02:27.
- **upstream**: `origin/develop` 166fd15df (2026-10-14), HOL's default
  branch. `origin/master` is still 652d1f0aa. develop has 104 commits since
  then, many of them in `tools-poly/lsp`, so every item below was re-checked
  against develop (by reading the source; no develop build was made).

Status values: `open` (not reported), `local patch` (fixed in the user's
HOL, not upstream), `drafted`, `reported`, `fixed upstream`,
`wontfix/obsolete`.

## Summary

| # | Title | Status | Component | Severity for agents |
|---|---|---|---|---|
| 1 | An edit spanning two proofs leaves the second one's old check running | local patch | lsp server | blocker |
| 2 | The pool reports `cheat` proofs as `proved` | open | lsp server | blocker |
| 3 | The goal-state walker's 1500 ms budget is fixed | local patch | lsp server | slows iteration |
| 4 | Holmake's project scan walks large untracked trees; no local way to exclude them | open | Holmake | slows iteration |
| 5 | A committed `[projects.<id>].path` is checked before `holproject.local.toml` can override it | open | Holmake | slows iteration |
| 6 | `bin/build` does not notice that `bin/hol` is stale after an LSP source edit | open | build / docs | slows iteration |
| 7 | `$/hol/retryCompile` params differ from `lsp-server.md` | open | lsp server / docs | slows iteration |
| 8 | goalState with the cursor inside a tactic shows the state before it | open (docs) | lsp docs | cosmetic |
| 9 | Hover returns null on a line with a non-ASCII string literal | open | lsp server | cosmetic |
| 10 | `$/eval` runs in global state; holdep logs `Cannot find file PolyML.ui` | fixed upstream (main part) | lsp server | slows iteration |
| 11 | tools-poly/lsp/README.md says `referencesProvider` is advertised | fixed upstream | lsp docs | cosmetic |
| 12 | README's printf handshake doesn't show `started` or the shutdown reply | wontfix/obsolete | lsp docs | cosmetic |
| 13 | `CoInductive` silently makes a free variable a fixed parameter | open (unconfirmed) | IndDef / docs | cosmetic |
| 14 | First Holmake run writes make-deps caches into an external project | open | Holmake | cosmetic |

Merged into other entries: "diagnostics hung >10 min showing `checking`"
→ #1; "goalState times out after CF's `xmatch`" → #3; "a looping tactic
hangs the LSP silently" → #1 (develop now has a per-proof timeout).

---

## 1. An edit spanning two proofs leaves the second one's old check running

- **Status:** local patch. `~/HOL` 53f27a3ce and `~/src/HOL-lspfix`
  0c6958153 (branch `lsp-failing-proof-fix`), neither pushed. The patch's
  test passes against the installed `~/HOL/bin/hol` (2026-10-15).
  **Still present on develop:** `startCompile`'s `editedProofBody` still
  decides from `proofBodyAt (#text text) editOff`, i.e. from where the
  edit *starts* (server.ML ~l.3043 on 166fd15df). The patch no longer
  applies cleanly to develop (conflict in `server.ML`), so it needs a
  rebase before it can become a PR.
- **Component:** lsp server (`tools-poly/lsp/server.ML`, `deferred_proofs.ML`)
- **Severity for agents:** blocker. An agent typically rewrites a file
  with one save that touches several proofs. When this bug hits, the agent
  waits indefinitely on a proof that has already failed.
- **Sources:** hol-lsp-mcp session (previous version of this file);
  `~/Programming/cakeml-worktrees/async-semantics/async/design/HANDOFF.md`
  §9 "Tool feedback". There `diagnostics` hung for more than 10 min with a
  proof `checking` that Holmake failed in under 1 s, and the worker
  abandoned the LSP for Holmake plus a dumped heap. With the patch, the same
  case (a 50-proof theory where one write changed two proofs) reports the
  failure 2 s after the edit.
- **Upstream since:** 805dadc88 added a per-proof timeout
  (`HOL_LSP_PROOF_TIMEOUT_MS`, `$/setConfig proofTimeoutMs`, default
  120 s). That bounds a hang from a looping *old* tactic, but nothing in
  the code suggests that the *new* tactic then runs. Not tested on a
  develop build.

**Evidence and minimal repro.**

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

1. Open it with `hol lsp --lsp-check-proofs`. Wait until `early` is
   `proved` and `later` is `checking`.
2. Send one full-text `didChange` that changes `early`'s tactic to
   `ACCEPT_TAC  TRUTH` (one extra space) and `later`'s tactic to `NO_TAC`.
3. `early` is cancelled and re-checked. `later` is never announced again
   and stays `checking`. Expected: `later` is reported `failed`.

Cause: `editOff` is where the edit starts, so an edit that starts in
`early`'s body counts as confined to `early`, and only `early` is
cancelled (`cancelProofAt`). The pass then re-elaborates and re-enqueues
`later`. `DeferredProofs.check` matches it by name to the entry still
running the old tactic, keeps that entry, and the new tactic never runs.
`tacticOnlyEdit` already brackets the change from both ends (common prefix
and suffix against `compiledText`), but the invalidation does not use it.

**Draft PR text.**

> lsp: re-check every proof an edit reaches, not just the first
>
> `startCompile` decides whether an edit is confined to one proof body
> from where the edit starts. A single `didChange` that starts in one
> proof and ends in the next therefore cancels only the first. The second
> is re-enqueued, matched by name to its still-running old check, and
> never re-run, so it keeps reporting the old tactic's state (forever, if
> that tactic loops).
>
> This change treats an edit as confined to one body only when both the
> edit mark and the two-ended `tacticOnlyEdit` bracket put the whole
> change inside that body. Anything else takes the existing conservative
> path (`cancelProofsAtOrAfter` from the resume point). A retraction, which
> marks a declaration's start, behaves as before. Adds
> `test_edit_spanning_two_proofs_rechecks_both`, which fails without the
> change.

Patch shape (at 652d1f0aa): move the `tacticOnlyEdit` binding above the
`invalidateProofs` calls and pass

```sml
val editedProofBody =
  case (proofBodyAt (#text text) editOff, tacticOnlyEdit) of
      (SOME d, SOME {decStart, ...}) =>
        if d = decStart then SOME d else NONE
    | _ => NONE
```

as `inProofBody`. On develop, `editedProofBody ()` also falls back to
`tacticBodyDecl editOff`. The rebase must keep that fallback and AND it
with the bracket in the same way.

Remaining gap, not addressed: `tacticOnlyEdit` compares against
`compiledText`, the text of the last *completed* pass, while pool entries
come from the last pass that reached `checkDeferred`. A `didChange`
landing between the two could be judged against the wrong text. Recording
the text next to the pool's entries would close this.

Installing the patch is not a drop-in. `server.ML` is compiled into
`bin/hol`, so every heap must be rebuilt (see #6).

Client workaround (hol-lsp-mcp, `session.proof_edit_start`): when a change
starts inside a proof body and runs past its `QED`, first send the text
with a blank line inserted before that declaration, then the real text.

## 2. The pool reports `cheat` proofs as `proved`

- **Status:** open. **Still present on develop:** `DeferredProofs.verdict`
  (deferred_proofs.ML l.103 on 166fd15df) compares only the hypotheses of
  the replayed theorem with the goal's assumptions. 755ca966a ("do not
  report a cheated-past proof as proved") is about the goal-state pane's
  "Initial goal proved." text, not the pool verdict.
- **Component:** lsp server (`tools-poly/lsp/deferred_proofs.ML`)
- **Severity for agents:** blocker. An agent reads `proved` as done, so a
  leftover `cheat` passes every check the LSP offers. hol-lsp-mcp works
  around this with a text scan.
- **Sources:** hol-lsp-mcp README ("`cheat` is tallied as `proved`") and
  commit 65e45f2. Reproduced 2026-10-15 on local `~/HOL`.

**Evidence and minimal repro.**
`~/.cache/upstream-tracker/repros/hover/hoverScript.sml` contains

```sml
Theorem cheaty:
  !n. n * 0 = 0
Proof
  cheat
QED
```

`hol lsp --lsp-check-proofs` reports `cheaty` as `proved` in
`$/proofStates`. hol-lsp-mcp shows `3/3 proofs proved`, plus its own
`CHEATS` line.

**Draft issue text.**

> **lsp: a proof that uses `cheat` is reported as `proved`**
>
> With `--lsp-check-proofs`, a `Proof cheat QED` block is reported
> `proved` in `$/proofStates`. `DeferredProofs.verdict` compares the
> replayed theorem's hypotheses with the goal's assumptions and does not
> look at its oracle tags. `cheat` (`mk_oracle_thm "cheat"`) adds no
> hypotheses, so the comparison succeeds.
>
> Repro: a theory with `Theorem t: !n. n * 0 = 0  Proof cheat QED`.
> Expected: a distinct state (e.g. `cheated`, or `proved` plus an
> `oracles` field), so that a client can tell a finished proof from a
> placeholder. The check would be whether the replayed theorem's tag
> carries the `cheat` oracle. The elaboration placeholder's `fast_proof`
> tag is not on the replayed theorem, so it should not interfere.

## 3. The goal-state walker's 1500 ms budget is fixed

- **Status:** local patch. `~/HOL` 708ff2ef4 (code) + 7328afed5 (test);
  `~/src/HOL-lspfix` eb4162aca + ec9be9532. Not pushed. The test passes
  against the installed `~/HOL/bin/hol` (2026-10-15).
  **Still present on develop** (`val walkerBudgetMs = 1500`,
  goal_state_init.ML l.51). The code commit **applies cleanly to
  develop**. The test commit conflicts, but only because develop's test
  `Client` already has an `env` argument. Drop that hunk and keep the new
  test.
- **Component:** lsp server (`tools-poly/lsp/goal_state_init.ML`)
- **Severity for agents:** slows iteration. In CakeML CF proofs, every
  position after `xmatch` answers `walker timed out (>1500ms)`, so agents
  write those steps blind.
- **Sources:** `~/Programming/cakeml-worktrees/cf-spike/async/design/HANDOFF-cf.md`
  (gotchas, final report); `02-cf-feasibility.md` "What failed, and
  friction"; hol-lsp-mcp 93527d2 (sets the variable to 10000).

**Evidence and minimal repro.** The test
`goalState_budget_from_environment`: a tactic step that sleeps 3 s times
out under the default and completes with
`HOL_LSP_GOALSTATE_BUDGET_MS=10000`. Run it with
`HOL_LSP_TEST_REPO=~/HOL python3 tools-poly/lsp/tests/lsp_tests.py goalState_budget_from_environment`.

**Draft PR text.**

> lsp: let HOL_LSP_GOALSTATE_BUDGET_MS set the goal-state walker's budget
>
> `$/hol/goalState` walks the proof's tactic under a hard-coded 1500 ms
> budget. A single slower step, e.g. CakeML's CF `xmatch` or a large
> `EVAL`, makes every later position answer `walker timed out`, with no
> way to raise the limit. Read the budget from
> `HOL_LSP_GOALSTATE_BUDGET_MS` once, when `goal_state_init.ML` loads, and
> keep 1500 when it is unset or not a positive integer. Adds
> `test_goalState_budget_from_environment`.

Before sending, consider matching develop's newer knob,
`HOL_LSP_PROOF_TIMEOUT_MS`, which a client can also set through
`$/setConfig proofTimeoutMs`. A `goalStateBudgetMs` key in `$/setConfig`
would be the consistent addition. The patch is a drop-in:
`goal_state_init.ML` loads from source at server start, so no heap is
rebuilt.

## 4. Holmake's project scan walks large untracked trees; no local way to exclude them

- **Status:** open. **Still present on develop.** `HMProject.discover_under`
  walks every subdirectory and skips only a fixed list (`skip_dirs = ["..",
  ".", ".git", ".hg", ".svn", ".hol", ".claude"]`) plus the committed
  `holproject.toml`'s `exclude`. `holproject.local.toml` accepts only
  `[projects.<id>]` (`recognised_local_keys = ["projects"]`), so a
  developer cannot exclude a directory of the root project without
  editing the tracked file.
- **Component:** Holmake (`tools/Holmake/core/HMProject.sml`)
- **Severity for agents:** slows iteration (badly in the worst case).
- **Sources:** `~/.cache/cakeml-build/chain.log` ("misc stage killed (scan
  of _opam)") and `STATUS.md`. An untracked opam switch in the CakeML
  checkout (about 41k directories) made `Holmake` scan for more than 75 min,
  with swap growing, before it was killed. After moving untracked trees out
  of the way, `Holmake -n` in `misc/` took 11 s. `shared-base-build.md`
  (Pitfalls) measures a fixed overhead of about 6.5 s per run, even with
  nothing to build, from scanning 322 directories. `HANDOFF.md` §8 counts
  about 314 directories per run.

**Evidence and minimal repro (not run).** A project whose committed
`holproject.toml` has no `exclude`, plus a large untracked directory:

```sh
mkdir -p big && (cd big && for i in $(seq 1 20000); do mkdir -p d$i/x; done)
time Holmake -n     # scan time grows with the size of big/
```

**Draft issue text.**

> **Holmake: let a developer exclude directories from the project scan
> without editing holproject.toml**
>
> Holmake's project discovery walks every directory under the project
> root except a fixed list (`.git`, `.hol`, …) and the committed
> `holproject.toml`'s `exclude`. A large untracked tree inside a checkout
> (a local opam switch, a venv, `node_modules`, another tool's build
> output) is scanned on every run. For us, a 41k-directory opam switch made
> a no-op Holmake run take over 75 minutes. `holproject.local.toml` only
> accepts `[projects.<id>]`, so the only fix is to change a tracked file.
>
> Suggestions, any of which would help: accept a top-level `exclude` in
> `holproject.local.toml` (unioned with the committed one); skip
> directories that contain a `CACHEDIR.TAG` or a marker file such as
> `.holignore`; or report (e.g. after 10 s) which directories the scan is
> spending its time in.

## 5. A committed `[projects.<id>].path` is checked before `holproject.local.toml` can override it

- **Status:** open. **Still present on develop.** `HMProject.load` calls
  `externals_from_table` on the committed and the local table separately,
  and deduplicates by id only afterwards (HMProject.sml l.247–320 on
  166fd15df).
- **Component:** Holmake (`tools/Holmake/core/HMProject.sml`)
- **Severity for agents:** slows iteration (there is a workaround).
- **Sources:** `~/Programming/cakeml-worktrees/cf-spike/async/design/shared-base-build.md`
  (Pitfalls), `HANDOFF-cf.md` (Dead ends). Confirmed by reading
  HMProject.sml.

**Evidence and minimal repro.**

```
sub/holproject.toml:        [projects.cakeml]
                            path = "../does-not-exist"
sub/holproject.local.toml:  [projects.cakeml]
                            path = "/real/cakeml"
cd sub && Holmake   →  Fail "no holproject.toml at …/does-not-exist"
```

`externals_from_table` is not lazy. For each entry it calls
`excludes_declared_at <path>`, which raises when the path has no
`holproject.toml`. A malformed `holproject.toml` at the committed path
fails in the same way.

**Draft issue text.**

> **Holmake: holproject.local.toml cannot override a committed external
> whose path doesn't exist**
>
> `holproject.local.toml` is meant to let a developer re-point a committed
> `[projects.<id>]` entry. But `HMProject.load` resolves both tables'
> externals (including reading each external's own `holproject.toml` for
> its excludes) before deduplicating by id. If the committed path does
> not exist on this machine, `load` fails with `no holproject.toml at
> <path>` before the override is consulted. Suggested fix: deduplicate the
> raw `(id, table)` pairs (local first), then resolve paths and read
> excludes only for the survivors. Workaround: make the committed path
> exist (e.g. a symlink).

## 6. `bin/build` does not notice that `bin/hol` is stale after an LSP source edit

- **Status:** open. On develop, `tools-poly/build.sml` (l.195–220) checks
  `bin/hol` against `tools-poly/hol.ML`, the Holmake sources and
  `tools/parsing`, but not against the LSP sources that polyc compiles into
  it: `tools-poly/lsp/{server,lsp_namespace,holide}.ML` and
  `LSPExtension.{sig,sml}`. tools-poly/lsp/README.md says so ("no build
  step notices otherwise").
- **Component:** build (`tools-poly/build.sml`), docs
- **Severity for agents:** slows iteration. An agent that patches
  `server.ML` and runs `bin/build` silently keeps the old server. Once it
  re-runs configure, Poly/ML refuses every heap saved by the old executable
  ("Saved state was exported from a different executable"), so all heaps,
  including CakeML's, must be rebuilt. `src/proofman/Holmakefile` also
  lists `bin/hol` as a dependency of `hol.state0`.
- **Sources:** previous version of this file (#1 "Installing it is not a
  drop-in"); `~/.cache/cakeml-build/STATUS.md` 02:39 (base rebuilt after
  the HOL rebuild with the LSP fix).

**Draft issue text.**

> **build: check bin/hol against the LSP sources compiled into it**
>
> `build.sml` warns when `bin/hol` is older than `tools-poly/hol.ML` or
> the Holmake sources, but not when it is older than
> `tools-poly/lsp/server.ML`, `lsp_namespace.ML`, `holide.ML` or
> `LSPExtension.{sig,sml}`, which polyc also compiles into it. After
> editing `server.ML`, `bin/build` succeeds and the LSP keeps running
> the old code. Adding those files to the `check_against hol` list would
> make the existing staleness check cover them.

## 7. `$/hol/retryCompile` params differ from `lsp-server.md`

- **Status:** open. **Still present on develop.**
  `Manual/Developers/lsp-server.md` (l.246–249) documents
  `{ uri: URI }`. `server.ML` (l.5741) decodes `textDocumentId`, i.e.
  `{ textDocument: { uri } }`.
- **Component:** lsp server / docs
- **Severity for agents:** slows iteration. A client that follows the doc
  is silently ignored, and the file stays blocked.
- **Sources:** previous version of this file. hol-lsp-mcp sends the
  `textDocument` shape.

**Draft issue text.**

> **lsp: `$/hol/retryCompile` params don't match lsp-server.md**
>
> lsp-server.md documents the notification's params as `{ uri: URI }`,
> but the server decodes `{ textDocument: { uri: URI } }`. A client that
> follows the document sends a notification that is silently ignored
> (there is no response, because it is a notification). Either fix the
> doc, or accept both shapes in the decoder so that existing clients keep
> working.

## 8. goalState with the cursor inside a tactic shows the state before it

- **Status:** open (docs). The behaviour looks deliberate: a tactic counts
  as applied once the cursor is past its last character. The README's
  goalState section does not say so.
- **Component:** lsp docs (`tools-poly/lsp/README.md`, goalState section)
- **Severity for agents:** cosmetic. An agent that puts the cursor on a
  tactic's first column expects the state after the tactic and gets the
  state before it.
- **Sources:** `async-semantics/async/design/HANDOFF.md` §8 ("a position
  inside a tactic sometimes showed the state before that tactic"). Reproduced
  2026-10-15 on local `~/HOL`.

**Evidence and minimal repro.**

```sml
Theorem two:
  !n. n + 0 = n /\ 0 + n = n
Proof
  gen_tac >>
  conj_tac >>
  simp[]
QED
```

On the `conj_tac` line (1-based columns): columns 3 and 6 (inside
`conj_tac`) give step 1, `n + 0 = n ∧ 0 + n = n`. Column 11 (just past
it) gives step 2, two subgoals.

**Draft issue text.**

> **lsp docs: say which goal state goalState reports for a cursor inside a
> tactic**
>
> With the cursor anywhere inside `conj_tac`, `$/hol/goalState` returns
> the state before `conj_tac`; just past its last character, it returns
> the state after. That is a sensible rule, but the README's description
> of `step` ("0-based tactic-step index at the cursor") doesn't say it.
> One sentence would do: "A tactic counts as applied once the cursor is
> past its end."

## 9. Hover returns null on a line with a non-ASCII string literal

- **Status:** open. Reproduced 2026-10-15 on local `~/HOL` (652d1f0aa +
  patches). Not tested on a develop build, and no develop commit mentions
  it.
- **Component:** lsp server (hover)
- **Severity for agents:** cosmetic.
- **Sources:** previous version of this file; hol-lsp-mcp README.

**Evidence and minimal repro.** In
`~/.cache/upstream-tracker/repros/hover/hoverScript.sml`:

```sml
val pz = ("∀", plus_zero)   (* hover on plus_zero: null *)
val pa = ("A", plus_zero)   (* hover on plus_zero: ⊢ ∀n. n + 0 = n *)
```

With the multibyte characters in a comment instead (`val (*‘’*) pz =
plus_zero`), hover works, so the client's position conversion is not the
cause. This was seen with the `utf-8` position encoding.

**Draft issue text.**

> **lsp: hover returns null on any line containing a non-ASCII string
> literal**
>
> On `val pz = ("∀", plus_zero)`, `textDocument/hover` returns `null` at
> every offset on the line, including on `plus_zero`. On
> `val pa = ("A", plus_zero)` it works. Multibyte characters in a comment
> on the same line do not trigger it. Seen with `positionEncoding`
> `utf-8`. It looks as if the hover path mismeasures, or gives up on, a
> line whose string token contains multibyte characters.

## 10. `$/eval` runs in global state; holdep logs `Cannot find file PolyML.ui`

- **Status:** fixed upstream (main part). 957a91fd8 ("evaluate a chunk
  where the cursor is, and say what it bound", 2026-10-07) adds a
  `position` parameter. The chunk runs against the compile state at that
  declaration. Without it, it runs after the file's last compiled
  declaration, so the file's own `val`s are in scope. Bindings persist
  between chunks evaluated at the same position.
  The `Cannot find file PolyML.ui` lines are by design: server.ML
  (l.2880) says holdep "reports every module the text mentions, basis
  structures included, and 'Cannot find file' for one of those is routine
  and harmless". That part is wontfix and not worth raising.
- **Component:** lsp server
- **Severity for agents:** slows iteration (on 652d1f0aa).
- **Sources:** hol-lsp-mcp README ("`eval` runs in the server process…").
- **Follow-up (ours, not upstream):** once the local HOL is on develop,
  hol-lsp-mcp's `eval` should accept a line and send `position`, and the
  README caveat should go.

## 11. tools-poly/lsp/README.md says `referencesProvider` is advertised

- **Status:** fixed upstream. At 652d1f0aa the README (l.22, l.67) listed
  `referencesProvider`. On develop it says "Not `referencesProvider`: see
  `Manual/Developers/lsp-server.md`" (l.38), and lsp-server.md (l.1410)
  explains why there is no `textDocument/references`.
- **Component:** lsp docs
- **Severity for agents:** cosmetic.
- **Sources:** hol-lsp-mcp `references` tool ("Current HOL does not provide
  this").

## 12. README's printf handshake doesn't show `started` or the shutdown reply

- **Status:** wontfix/obsolete. Could not reproduce. Both the 652d1f0aa
  and the develop README list the `initialize` response, a
  `window/logMessage "started"` and `{"id":2,"result":null}`. Running the
  README's `printf … | bin/hol lsp` against local `~/HOL` on 2026-10-15
  printed exactly those three messages and exited 0
  (`~/.cache/upstream-tracker/repros/handshake.out`). The original report
  was probably made against an older build or README.
- **Component:** lsp docs
- **Severity for agents:** cosmetic.

## 13. `CoInductive` silently makes a free variable a fixed parameter

- **Status:** open (unconfirmed). It could not be reproduced in a small
  case. HOL's (co)inductive definitions take "schematic variables": the
  same variables following the constant in every rule. This is documented
  in `Manual/Description/definitions.smd` (around l.2174), and HOL prints
  `<<HOL message: Treating "a" as schematic variable>>` when it applies
  it. In repros (`~/.cache/upstream-tracker/repros/coind/coind{,2,3}.sml`),
  a free `m` that varies in a recursive occurrence was generalised with a
  "Generalising variable" message, not made schematic. The worker's
  original failing definition was not committed. The committed fix
  (`asyncCfProgScript.sml` `STEP`) quantifies `m` in each rule, with
  `p A WORLD` intentionally schematic.
- **Component:** IndDef (`CoIndDefLib`) / docs / possibly lsp
- **Severity for agents:** cosmetic for now. The cost was a nonsense
  coinduction principle that went unnoticed for a while.
- **Sources:** `cf-spike/async/design/HANDOFF-cf.md` (Dead ends),
  `02-cf-feasibility.md` "What failed, and friction".
- **Possible request, if a repro turns up:** the "Treating … as schematic
  variable" message is easy to miss in a Holmake log. Ask whether `hol lsp`
  shows HOL_MESSAGEs raised while elaborating a declaration as
  information-level diagnostics on that declaration. **Next step:** ask the
  cf-spike worker for the exact pre-fix `CoInductive STEP` text.

## 14. First Holmake run writes make-deps caches into an external project

- **Status:** open (low priority).
- **Component:** Holmake
- **Severity for agents:** cosmetic. It matters only if a shared base is
  ever made read-only.
- **Sources:** `cf-spike/async/design/shared-base-build.md` (Pitfalls). A
  first build in an experiment that uses the base as `[projects.cakeml]`
  created 65 `.hol/make-deps/*Theory.{sml,sig}.d` files in the base. No
  build products were changed, and later runs wrote nothing.
- **Note:** before reporting, check whether Holmake tolerates failing to
  write them (e.g. a read-only base). If it does, this is not worth an
  issue.

---

## Not upstream (for the record)

- Rosetta: the agents' Bash tool runs under x86_64 emulation, so every
  HOL/Holmake invocation needs `arch -arm64`. This is local environment, not
  HOL.
- `goalState` queued behind a waiting `diagnostics` call: a hol-lsp-mcp
  problem, fixed in 561dbcf.
- `pkill -f Holmake` by one worker killed another's build: a process
  rule for workers, already in their conventions.

## Local patches ready to become upstream PRs

| Patch | Local commits | Applies to develop? | Ready? |
|---|---|---|---|
| Walker budget env var (#3) | 708ff2ef4, test 7328afed5 | code: clean; test: drop the `Client(env=…)` hunk (already upstream) | Yes. Optionally add a `$/setConfig` key first |
| Re-check every proof an edit reaches (#1) | 53f27a3ce (`~/HOL`), 0c6958153 (`~/src/HOL-lspfix`) | conflicts in `server.ML` (develop changed `editedProofBody`) | Needs a rebase onto develop and a run of the `--requires none` suite |
