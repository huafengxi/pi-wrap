# Recovery adaptation boundaries

Task rd6-p-175748. Original main is retained as ancestry; source projection and
adaptation commits are separate. `source-map.json` records all 14 first-parent
source steps (7 projected, 7 no-op), modes/blobs/deletions and full source SHAs.
`source-refs.json` records original archive/retired refs and reachable object
identities. `net-paths.json` compares every non-recovery path, not just a count.

## Ownership

All `pi-wrap/` paths map to repository root, plus owned copies of
`platform_paths.py` and `test_platform_paths.py`. Platform-wide README deployment
and cross-module installation guidance remains caller-owned; the independent
root/helper/dependency mechanism is documented in this README instead. Other
modules, including `agentd/wait_state.py`, are deliberately not copied. Source
persona/thinking/task-tab changes remain intact. No uncommitted source is used.

## Adapted paths

- `agentd_dependency.py`, wrapper import prefix: real-source location, explicit
  AGENTD_DIR priority, named required-file validation with clear refusal. Existing
  wait tracking/attach replay remains required; no fallback implementation or
  capability downgrade. No model execution is possible past the fixed-pin
  dependency failure. Wrapper docstring replaces a deployment-root example.
- `platform_paths.py`, `test_platform_paths.py`: local helper contract v1,
  nonempty expanded/absolute override then real source repository parent, no
  marker/MODULES; real/relative/symlink/space paths and actual Linux Bash3.2.
  The helper adaptation has the same contract as agentd's owned helper; no runtime
  cross-repo helper import. Test root/module overrides are synthetic.
- Toolface `setup.sh`, `clean.sh`, `drive.py`, README: resolve own checkout via
  realpath and local helper; HARNESS_WS retains priority. Setup uses the same
  helper through its Python CLI, including nested Bash3.2 entry. Clean validation
  executes only its source root-resolution prefix and `-n`; its destructive body
  and real-model drive are NOT run. No deletion or guard-policy redesign.
- `fixture_support.py`, `test_persona.py`, `test_wrap.py`: imports are local;
  cleanup uses pinned agentd safe_delete, authorized only for a newly created
  short fixture base. That exact base is allowlisted because TMPDIR may differ;
  no production-root deletion experiment. Empty fixture HOME/environment.
  `test_wrap.py` explicitly passes AGENTD_DIR into fakepi wrapper children and
  fails immediately at missing required wait_state; no false integration PASS.
  Model strings in its profile fixtures are synthetic, never real config reads.
- `test_thinking.py`: existing safe_delete import honors AGENTD_DIR, not an
  unconditionally injected sibling path. Persona runtime itself is unchanged
  from frozen source, including form/model/thinking fail-soft semantics.
- `test_dependency.py`, `test_fakepi.py`: isolated fixed-pin import/report/envscrub
  and negative-layout coverage, and direct fakepi JSON-lines tests. Negative
  startup success is NOT wrapper integration success.
- README and recovery metadata: standalone dependency pin/interface, transparent
  BLOCKED compatibility and exact test boundary; no actual deployment values.

## Explicitly unresolved

`wait_state.CurrentWaits` (`sent`, `event`, `dialogs`) and `DIALOG_METHODS` are
required by source `1f650e88357b29908526245f6b7ccae2d5540464` but absent from the
fixed agentd `9625bdc15f853e49a6312d20dc429e23330e3d89`. Dispatcher explicitly
rejected capability downgrade and authorized partial/blocked delivery instead.
The pin remains unchanged. Lifecycle/resident/message/replay full integration
must be rerun only after a separately reviewed, explicitly authorized new pin.
This candidate is not deployed, not a production gate, not P1/P6 completion.
