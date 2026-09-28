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

Python 3 stdlib only. The authoritative (Chinese) design notes are the module
docstring of `pi-rpc-wrap.py`; this file is the entry point for a reader who found
the repo first.

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
| `DISPATCH_HEARTBEAT` | `1` = heartbeat-driven session: exempt from the recursion guard |
| `AGENTD_WRAP_*` | per-run overrides: `PI_BIN`, `INIT_OK`, `RECV_ARMED`, `INIT_TIMEOUT`, `ARM_TIMEOUT`, `SETTLE_WINDOW`, `EXIT_GRACE` |
| `AGENTD_DIR` | where the sibling `agentd` repo lives (default: `../agentd`) |

## Assembly face

For a task/resident session the wrapper assembles the initial prompt from the
workspace's persona assets: the profile named by `DISPATCH_PROFILE` (an ordered
list of capabilities), each capability's `prompt.md`, and — when the profile
declares `knowledge` — a rendered knowledge list resolved by name through
`bots/kb_index.py`. Extensions are passed to `pi` with `-e`. An optional
`prompt.md` in the participant directory is the initial nudge for a blank new
generation (absent ⇒ bare start, which is a supported path).

## Sibling repo dependency

`proto.py` — the single source of the path/envelope/exit-code contract, mirrored by
the TypeScript side of the supervisor — lives in the **`agentd`** repo. This repo
imports it from `AGENTD_DIR` (default `../agentd`), so the two repos must be
checked out side by side (or `AGENTD_DIR` must point at `agentd/`). Nothing is
duplicated: two copies of the protocol would drift.

## Tests

```bash
python3 test_wrap.py          # 444 checks, ~90 s; no network, no real pi
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
| `pi-rpc-wrap.py` | the wrapper: spawn, socket passthrough, readiness handshake, convergence, model-error gate, resident mode |
| `fakepi_rpc.py` | scriptable `pi --mode rpc` double for the tests |
| `test_wrap.py` | the verification matrix (T1…T46: argv/env, injection, handshake, convergence, exit-code semantics, cross-file pins) |
