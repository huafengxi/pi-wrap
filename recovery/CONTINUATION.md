# Fixed dependency continuation — rd6-p-cont-191456

This layer succeeds the old **BLOCKED** candidate
`eb213ea103416c82bcbe57003047aa608dbf7443`. It does not rewrite its verdict,
source projection, adaptation records or failed tests. Source remains frozen at
`1f650e88357b29908526245f6b7ccae2d5540464`. No new Source-Commit is claimed.

Old pin: `9625bdc15f853e49a6312d20dc429e23330e3d89` (no wait_state).
New reviewed pin: `ca35c53c6bd9c437615058126c5bcd2f97de2801`.
See `continuation-dependencies.json` for required interfaces and explicit caller
static input revision. No runtime wrapper/persona/helper/protocol changes.

## Exact delta and history

`continuation-net-paths.json` lists every non-recovery path with old/new
blob/mode/deletion and explanation. Only README and dependency tests change;
`test_layout_integration.py` is new. All other runtime/old tests/helper/shell
files retain identical blobs/modes. Historical source-refs/source-map/net-paths,
adaptations/ADAPTATIONS, dependencies and test-results are immutable. The new
verifier validates historical net content at the old candidate (not at today's
HEAD), then validates the new exact path map and unchanged historical records.
It still checks all source projection trees, original refs/objects and ancestry.

`run-tests.py` now consumes the new pin and runs every test_*.py in two real
copies, with a sibling agentd link or arbitrary non-sibling + explicit AGENTD_DIR.
The real integration startup omits AGENTD_DIR in sibling mode. Space paths,
unrelated cwd, real/relative/symlink entrances and missing/bad dependency tests
are retained. There is no unified checkout or production fallback.

Two existing static cross-file tests (T43/T45) require caller-owned tasks/run.py
and core.ts. They are supplied through an explicit synthetic read-only input
root at the recorded caller revision. They are read as text, not executed. No
caller assets are installed or activated; P14 kb-index is intentionally absent.

The small `rpc-wait-fixture.py` is a test-only no-model JSON-lines fake pi,
not a copy of wait_state or a wrapper behavior change. It emits native RPC
requests, creates a real fixture file ask through pinned proto, and blocks until
an actual file/socket answer. The full inherited lifecycle suite still uses
repository fakepi_rpc.py unchanged. Its real subprocess environment checks
cover message/readiness, resident, report/model-error, persona and W_WORKSPACE.
Native dialog tests cover all four methods, multiple observer attaches, ring
eviction and non-resurrection after response; report uses the pinned agentd.

## Results and boundaries

See `continuation-test-results.json` for all seven scripts in both layouts.
Inherited test_wrap reports 228 passed / 0 failed per layout. Persona reports
86 legacy PASS counters, but **one is P14 skip**: 85 executed assertions + one
skip per layout; no kb-index integration PASS. All other scripts execute with
no skip. Real Linux Bash3.2 (not macOS) runs shell root tests; clean only executes
its root prefix and full syntax check, never its deletion body.

The initial unmodified wrapper suite reached 212 passes but failed two missing
static caller inputs. The first new wait fixture used the wrong return shape
for proto.write_message; that test-only tuple handling was corrected, with old
logs preserved and the complete suite rerun. Neither was fixed by changing the
runtime or downgrading tracking/dialog/replay.

Fixtures use empty HOME and minimal environment, no real models/configuration,
production ask/socket, service start or caller preactivation. Existing tests
may clean their private fixture/socket lifecycle only; outer validation roots
are retained. No GC/root deletion experiments. This is a bounded source
candidate result, not production authorization or six-repository integration.
D5 stays closed, D6 adds no guard, P1/P6 and macOS/multihost remain unaccepted.
