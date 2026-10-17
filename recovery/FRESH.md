# F-P current offline recovery entry — task/516ryp

This is the **current** recovery declaration for the F-P candidate. It supersedes
only the selection of a P consumer dependency, not historical facts, verdicts or
other consumers. No deployment values/activation are supplied. Not F-C/P3, a
complete six-repository gate, a generic interaction host or a production release.

## Fixed inputs and source projection

- Previous P: `f89188fda24f803985fa1809cd40da5484ed1b10` (retained ancestor).
- Current A: **`b40e0d027b1ebbe072b0edc0929e0411da3c37af`** in `agentd.git`.
- Previous P dependency: `ca35c53c6bd9c437615058126c5bcd2f97de2801`.
- Platform source base: `1f650e88357b29908526245f6b7ccae2d5540464`.
- Platform source end: `1156870f3bf23d00bd8ced0717fc63331e08e54b`.
- Caller test input: `fc5d66a08ff05122a0fa01e781903f1eed54a9a5`, containing
  `967cf7d53eb5cca9af645813535a9661b1b3e5d9` producer deletion.

`fresh-dependencies.json` is authoritative for this candidate's full pin,
required interfaces, exact module hashes and caller inputs. Historical
`dependencies.json`/`continuation-dependencies.json` are immutable declarations
of older candidates, **not mutable aliases for the new selection**. The helper
and new test entry point explicitly refer to the fresh declaration. Runtime
checks interfaces and actual import origins (not Git metadata); fixed-object
export tests additionally check exact module hashes. A branch name never selects
A in the test runner. S→A0 and all other consumers are unchanged.

`fresh-source-map.json` records per-source paths and before/after objects:

| Source | P projection |
| --- | --- |
| `1090df79741716fac97c8f9e1b75e361d6e3f9c0` | no pi-wrap paths; actual no-op |
| `4dbd3ca779aae0ae9e7fb98182d77f826d3be436` | `b4357000c5d4553445418aadc2614154fde5a3f0`, README and pi-rpc-wrap.py only |
| `1156870f3bf23d00bd8ced0717fc63331e08e54b` | no pi-wrap paths; actual no-op |

The projected runtime delta is the same six targeted edits as platform source;
independent imports, roots, protocol/report predicates and lifecycle code were
not replaced with the platform whole file. Subsequent dependency/helper, tests,
README and new recovery files are independent-repository adaptations, not source
projections. Their commit message is marked separately with this taskId.

## Communication contract, not a wait observer

`DialogRequests()` is an in-memory socket attach baseline. The wrapper sends
successful stdin writes to `sent`, RPC events to `event`, and replays `dialogs`
after filtering historical dialog requests out of its raw ring. It does not
read/write wait-state JSON, consume file-ask tool updates, infer waits from
unpaired tools, or import report.current_tools. No CurrentWaits fallback,
waitToken/native fingerprint, launcher/w bridge, second runtime or native patch.

Dialog answers/cancel/deadlines/tool end/turn settlement keep their existing
semantics. An unmodified native internal signal settlement may not emit a wire
event: a dialog baseline is **not proof of a current blocking wait**. The mock
native test exposes this boundary, it does not fix the native protocol. A bare
abort must not be guessed to settle a promise.

Task and resident stdio/socket lifecycle, one-client replacement, prompt
idempotence, init-ok/recv-armed, settle_loop, model_error_gate and message
contracts stay in the original wrapper. Ordinary sessions cannot impersonate a
task; no general host/dual entry extraction is part of this candidate.

## Tests, old assertions and limits

Use `fresh-run-tests.py` from a clean fixed P export (after push), with explicit
actual bare A and caller object sources, an available Bash3.2 binary and a NEW
short output root. See README for command syntax. The runner retains logs,
commands, full minimal child environments, return codes, module/input hashes and
skip lines in results.json. It exports the actual old A too and requires real
startup refusal; a missing/bad explicit path cannot fall back to a valid sibling.

Both layouts contain real exported A directories: sibling `agentd` without
AGENTD_DIR, and explicit non-sibling A with spaces at unrelated cwd. Every P
`test_*.py` runs in both. No installed runtime is preloaded to rescue imports.
Separate import probes report all module __file__ paths and wrapper object
identity. Supplemental fixed-A tests execute the exported new P plus the real
TS producer against synthetic mailboxes, preserving precise ref/via/nonblocking
ask separation and timeout. A's current_tools/report tests test read-only
10-column semantics, not a P observation writer.

The old three integration test shapes are retained, not mass-deleted:

- The file-wait/report-command assertion is replaced by real ask/exact-ref CLI
  reply, old nonblocking ask remains pending, and no report wait hint or JSON.
  The report no longer fabricates a wait cell/reply command.
- The four dialog methods still exercise two attaches after ring eviction,
  answer and a third attach with no resurrection. Synchronization uses fixture
  completion and wire barriers, not current_waits reads or report wait hints.
- Real/relative/symlink fakepi lifecycle remains. All inherited test_wrap.py
  lifecycle assertions (historical 228 counter) remain byte-identical; no wait
  behavior is resurrected to preserve that number.

New test_wait_removal.py independently guards legacy JSON open calls, absent/
forged/stale/invalid files, ignored tool updates, in-memory replay after eviction,
settle non-resurrection, successful-write-only answer/cancel, deadline and mock
native abort boundary. test_dependency.py checks new module hashes and interfaces,
old/missing/bad paths and foreign cached imports. The old rpc-wait-fixture.py is
frozen history; the current fresh-rpc-fixture.py emits no ask update observation.

P14 kb-index needs genuine caller input and remains a disclosed skip; no fake
same-name/static count closes it. Optional native helper/actual marked renderer
inputs are explicit read-only copies with hashes; missing inputs remain skips,
not PASS. No installs/downloads or real pi/model/browser/macOS/SSH/production
socket/service are used. Linux Bash3.2 is not macOS evidence. Toolface live-model
driver and shell clean's destructive body are excluded; safe fixture cleanup is
confined to per-invocation newly created roots, which the outer runner retains.

## Historical recovery is intentionally frozen

All prior recovery files (including failed test results, continuation PASS
records, old source mapping, dependencies, run-tests.py and verify.py) retain
original blobs. `verify.py` and old run-tests.py have old frozen candidate/pin
scope; they are not the new candidate verifier and are not weakened to accept
this delta. Old P at old pin remains recoverable. New provenance/object audit
and fresh fixed-export runs, not inherited PASS counts, support this candidate.
Independent review/queue acceptance follows implementation; no token is PASS.
