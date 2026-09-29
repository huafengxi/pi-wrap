# pi-wrap — supervised RPC session wrapper

`pi-rpc-wrap.py` launches one agent session as `pi --mode rpc`, holds its stdio,
and passes the **raw pi-RPC byte stream through** to a unix socket
(`<root>/run/agentd/<taskId>.sock`) so an observer/interactor can drive the live
session. It is, by design, "a socat with judgement": every observation/interaction
intelligence (entry baselines, event rings, injection, self-healing) belongs to the
consumer; this wrapper owns only the **lifecycle** semantics — and the exit code it
hands back to its supervisor.

To the supervisor (the `agentd` runner) the wrapper is an ordinary command plus an
exit code. That single invariant is what makes the whole system debuggable: no
side channel, no state machine to query — read the exit code and the session's
`report.md`.

Python 3 stdlib only, except `PyYAML` for reading capability declarations (absent
⇒ the documented degrade path, never a hard failure). The authoritative (Chinese)
design notes are the module docstrings of `pi-rpc-wrap.py` (lifecycle + argv
emission) and `persona.py` (persona assembly); this file is the entry point for a
reader who found the repo first.

## Why a wrapper at all

The failure modes it closes are all forms of **false success** and **spawn races**:

- `pi` can exit 0 having done nothing (a prompt rejected before any turn, an empty
  run) — so completion is judged on evidence, not on the exit code alone.
- The upstream model request can fail on the last turn while `pi` still exits 0
  (`stopReason=error`, usage all zero, no report written). The **model-error gate**
  classifies that: no non-empty `report.md` ⇒ a real failure (diagnosis
  `stage=model_error_stopreason`, exit **1**); a delivered report ⇒ pass the exit
  code through with a WARN plus an informational diagnosis
  (`model_error_stopreason_delivered`).
- A child receiver extension can miss envelopes that arrived *before* the session
  started, or inject before the initial prompt was accepted. The **readiness
  handshake** closes that with two marker files and a bounded wait.
- A resident (never-converging) session must not be treated as a task: any `pi`
  exit is a termination fact and its exit code is passed through untouched.

## Exit-code contract

| Situation | Exit |
|---|---|
| converged, `pi` exited gracefully (stdin EOF) | `pi`'s code (normally 0) |
| model-error gate (i): `stopReason=error` ∧ no non-empty `report.md` | **1** + a diagnosis file |
| model-error gate (ii): error ∧ report delivered | `pi`'s code + WARN + informational diagnosis |
| prompt rejected / hard local failure (e.g. cannot bind the socket) | 1 (2 for a usage error) |
| resident mode: any `pi` exit | passed through (e.g. 7) |

Convergence grace: after closing stdin the wrapper waits `EXIT_GRACE` (15 s) for
`pi` to exit, then SIGTERM.

## Readiness handshake

Two markers, both under `<root>/run/agentd/` (single source = `proto.task_ready_path`
in the sibling `agentd` repo):

- `init-ok` — written by this wrapper once the initial prompt has been accepted by
  `pi` (so nothing can be injected before the session exists);
- `recv-armed` — written by the child receiver extension once its inbox sweep is
  armed (so envelopes that arrived before spawn are not lost).

The wrapper waits for `recv-armed` up to `CHILD_ARM_TIMEOUT` (3 s) before entering
convergence supervision; a missing marker degrades (with a log line), never hangs.
Stale markers from a previous generation are removed before spawn.

## Environment contract

Injected by the supervisor (and scrubbed of inherited scheduler identity first):

| Variable | Meaning |
|---|---|
| `AGENT_ROOT` | workspace root (all paths derive from it) |
| `AGENT_HOME` | this participant's directory (`agents/task/<id>/`, `agents/bot/<name>/`) |
| `AGENT_SELF` | path-style participant id (`task/<id>`, `bot/<name>`, `topic/<id>`) |
| `AGENTD_TASK` | recursion guard: set for task-shaped sessions, absent for resident ones |
| `AGENTD_RESIDENT` | `1` = resident session (no completion convergence) |
| `AGENTD_SESSION_NAME` | pinned session name for resident spawns |
| `DISPATCH_PROFILE` | thin profile to assemble (ordered capability list) |
| `AGENTD_WRAP_*` | per-run overrides: `PI_BIN`, `INIT_OK`, `RECV_ARMED`, `INIT_TIMEOUT`, `ARM_TIMEOUT`, `SETTLE_WINDOW`, `EXIT_GRACE` |
| `AGENTD_DIR` | where the sibling `agentd` repo lives (default: `../agentd`) |

## Assembly face

For a task/resident session the persona comes from the workspace's persona assets:
the profile named by `DISPATCH_PROFILE` (an ordered list of capabilities), each
capability's `prompt.md`, the skills it bundles, and — when a capability declares
`knowledge` — a rendered knowledge list resolved by name through `bots/kb_index.py`.
An optional `prompt.md` in the participant directory is the initial nudge for a
blank new generation (absent ⇒ bare start, which is a supported path).

Assembly is **two layers**, and the merge semantics live in exactly one of them:

- `persona.py` (this repo) — the *resolution* layer: profile manifest →
  capabilities → structured injection face (ordered prompt parts, skill paths,
  tool allow/deny sets, model + derived provider, normalized compaction policy,
  warnings). Every merge rule and every fail-soft degrade branch is here, and
  nowhere else. It is also a CLI (`persona.py resolve --root … --form
  task|resident [--profile …]` → one JSON object on stdout) so an out-of-process
  consumer resolves the very same structure instead of re-implementing it.
- `pi-core/agent/extensions/profile-loader.ts` (main workspace repo) — the
  *injection* layer: a pi extension that calls that CLI once per session and maps
  the structure onto pi's in-session API — system-prompt append
  (`before_agent_start`), skill paths (`resources_discover`), active tool set
  (`setActiveTools`), model (`setModel`), compaction policy (env + loading the
  `context-compaction` unit). It is auto-discovered from `~/.pi/agent/extensions/`,
  so a human can start a persona session with just `pi --persona <profile>`.
  The **form** still comes from `AGENTD_RESIDENT` (a fact about the session, not
  part of the persona declaration), so a resident-shaped profile needs
  `AGENTD_RESIDENT=1 pi --persona <profile>`; without it the task form applies
  (executor baseline prepended, `ask_user` excluded).

**The wrapper therefore puts no persona data in argv**: the prompt text, knowledge
list, skill paths, tool sets, model and compaction policy are all injected
in-session (the emission contract is pinned by `test_wrap.py` T48). Its whole job
for the persona face is two things (`_persona_ext_argv`):

1. pass `-e <profile-loader.ts>`. The file is auto-discovered anyway; the explicit
   `-e` **pins load order**, because pi loads CLI extensions before discovered ones
   (`mergePaths(cliEnabledExtensions, enabledExtensions)` in
   `dist/core/resource-loader.js`) and `before_agent_start` handlers run in load
   order. Pinning it first keeps the persona text ahead of other global extensions'
   appends (e.g. host-info's identity lines). pi de-duplicates by realpath, so the
   same file arriving twice is loaded once — no duplicate flag registration.
2. pass the two input env vars through untouched (`DISPATCH_PROFILE` = profile
   name, `AGENTD_RESIDENT` = form), plus `AGENT_ROOT` so the extension can find
   `persona.py`. The wrapper always *removes* an inherited
   `AGENTD_CONTEXT_COMPACTION`: its writer is now the extension, and "no policy ⇒
   env absent" is a hard semantic that must not depend on a clean caller
   environment.

Position note: pi builds its system prompt with a dedicated append slot
(`appendSystemPrompt` in `BuildSystemPromptOptions`) placed *before*
`<project_context>` and the skills section, whereas `before_agent_start` only ever
sees the **finished** prompt — so the persona now lands at its end. The appended
bytes are identical (`appendJoiner` in `persona.py` is pi's own joiner for that
slot); only the position differs. Restoring the old slot would need pi's private
`buildSystemPrompt` (not in the package's public `exports`), so it is deliberately
not done.

Observability moved with it: the per-capability assembly log and the resolution
warnings are now written by the extension to **pi's stderr** ⇒
`run/agentd/<name>.stderr.log` (the same tail the diagnosis file quotes), not to
the wrapper's own log. In-session evidence is the read-only `/persona` command.

## Sibling repo dependency

`proto.py` — the single source of the path/envelope/exit-code contract, mirrored by
the TypeScript side of the supervisor — lives in the **`agentd`** repo. This repo
imports it from `AGENTD_DIR` (default `../agentd`), so the two repos must be
checked out side by side (or `AGENTD_DIR` must point at `agentd/`). Nothing is
duplicated: two copies of the protocol would drift.

## Tests

```bash
python3 test_persona.py       # resolution layer: output contract, merge semantics,
                             # degrade matrix, CLI, cross-consumer equivalence
python3 test_wrap.py          # emission + lifecycle: argv/env, handshake, convergence,
                             # exit codes; no network, no real pi
```

`fakepi_rpc.py` is the `pi --mode rpc` double: it speaks the same JSON-lines
protocol, can script failures (prompt rejection, false `agent_settled`, stream
interrupts, self-kill with a chosen exit code) and writes an argv/env snapshot so
the assertions can check *what the wrapper actually passed to pi*. All fixtures are
synthetic trees under `/tmp`; the suite never touches a production workspace, and
the cross-file pins (the TypeScript extension, the service manager, the persona
assets) skip gracefully when those repos are absent.

Run it with `AGENTD_DIR` pointing at an `agentd` checkout (the default `../agentd`
works when both repos sit in the same parent directory).

## Files

| File | Role |
|---|---|
| `pi-rpc-wrap.py` | the wrapper: spawn, socket passthrough, readiness handshake, convergence, model-error gate, resident mode; loads the persona injection extension and passes its input env through |
| `persona.py` | persona assembly (resolution layer): profile/capabilities → structured injection face; also a CLI for out-of-process consumers (the injection extension) |
| `fakepi_rpc.py` | scriptable `pi --mode rpc` double for the tests |
| `test_persona.py` | resolution-layer matrix (P1…P16: output contract, capability expansion, fallback, tool sets, bundles, model/provider, compaction policy, knowledge tiers against the real `kb_index.py`, warnings, CLI) |
| `test_wrap.py` | the verification matrix (T1…T48: argv/env, handshake, convergence, exit-code semantics, persona emission contract, cross-file pins) |
