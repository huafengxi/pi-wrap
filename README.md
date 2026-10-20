# pi-host — independent local pi RPC host

P is a Python-standard-library-only process/transport mechanism. Copy this
whole directory anywhere and run it without agentd, a workspace, persona assets,
`AGENTD_DIR` or `PYTHONPATH`. The platform repository is its distribution
container, not a runtime dependency on another module.

## Responsibility

P starts one `pi --mode rpc`, uniquely owns its stdin/stdout/stderr PIPEs, drains
output without an observer, serves one replaceable Unix-socket client, replays
a bounded raw event ring plus live native UI requests, and waits for its child.
It never restarts pi, replays inbound commands, prompts a model on its own,
interprets task reports or participates in scheduling/registration/mailboxes.

The caller supplies cwd, executable, history/socket paths, explicit `-e` pairs
and child environment. P does not discover workspace configuration or scrub
agent identity policy. A runtime's actual inherited `RUND_LEASE_FD` is propagated
as an open fd, not synthesized from configuration.

## Launch input

```sh
python3 pi_host.py - < /caller/supplied-input.json
```

`-` reads one JSON object through stdin until EOF. A caller can instead pass an
explicit input filename. The Web caller uses a one-shot pipe and does not keep
a launch configuration/environment file. Input has exactly these keys:

```json
{
  "sessionFile": "/absolute/private/history.jsonl",
  "cwd": "/absolute/existing/directory",
  "sessionName": "example",
  "piBinary": "/absolute/bin/pi",
  "socketPath": "/absolute/private/s",
  "extensionArgv": [],
  "env": {"HOME": "/absolute/private/home", "PATH": "/usr/bin:/bin"}
}
```

## Runtime output

Use one agreed endpoint per session in a short, caller-owned private directory.
Runtime files beside that endpoint have explicit writers:

| Output | Writer and meaning |
|---|---|
| `host.json` | Web caller alone: accepted native P identity fields at the top level, without a `host` wrapper; P reads it for previous-owner checks |
| socket (normally `s`) | P's Unix stream endpoint |
| `host.log` | pi stderr; the caller may also redirect P stdout/stderr here for one diagnostic log |

There is no random instance token, per-instance directory, separate pidfile,
launch-intent marker or persisted copy of the input environment. `host.json` is
published by Web only after verifying the real socket peer and get_state; it
remains after socket cleanup. P never writes or removes it.
Pi's PID is held in P's subprocess handle, not in persistent lifecycle records.
A stale P record is never a current liveness oracle.

The flat `host.json` contains exactly `pid`, `start` and `boot`. `start` is a
string token in the local platform's units; `boot` distinguishes boots. Web
expects its own uid and pgid/sid equal to P's pid because it launches P as a
session/group leader. Other process facts are sampled from the kernel rather
than persisted. Binding, launch input, pi identity, child waits and errors are
not fields in this record.

Pi cannot continue independently after P. On Linux, `child_exec.py` arms
PR_SET_PDEATHSIG(SIGKILL) in a fresh interpreter and then execs pi without an
extra resident process. Parent checks before and after arming close the startup
race. Other supported hosts use native pi RPC's shutdown on stdin EOF; P is
the sole writer. Linux's kernel guard also covers pi ignoring EOF/TERM and
uncatchable P death. It applies only to the ordinary Host, leaving the agent
adapter's launch policy under its own control. Tool descendants are separately
checked for closure before restart, history mutation or deletion.

The host waits for pi in memory and exits with its code (signals map to
128+signal). Host JSON has no child exit result or error field. Stdout
`host_started`/`child_wait` lines remain
optional standalone diagnostics; ordinary Web readers do not parse logs to make
lifecycle decisions. Stderr is drained/flushed promptly, with a bounded in-memory
tail. Slow observers are dropped after a bounded send timeout, not allowed to
block pi indefinitely.

## Ownership and acceptance

P has no cross-process file lock, lease or simultaneous-start uniqueness promise.
Live-owner/unknown-endpoint checks and bind errors conservatively reject unsafe
reuse. Existing owner checks read the Web-accepted P identity in `host.json`.
Web uses unique temporary files and atomic replacement. P cleans up only the socket
inode it bound, never an endpoint installed by another owner.

After launch, the W caller checks kernel peer PID/uid and process start identity
against THIS invocation's Popen, then sends one uniquely identified `get_state`.
Only the matching successful response accepts startup. No RPC is sent to a
mismatched peer. Web does not persist an intent before Popen: an interrupted
launch may need manual investigation. This does not promise automatic crash
recovery or atomic contention handling. Darwin signals use accepted
check-identity-then-kill semantics; see [LOCAL-PROCESS.md](LOCAL-PROCESS.md).

## Agent adapter boundary

[agentd's adapter](../agentd/RPC-ADAPTER.md) imports generic `Transport` and adds
its own file locks, identity hooks, delivery/persona and completion policies.
The dependency is **agentd → P**, never the reverse. Its existing pidfile/log
protocol is preserved by Transport's default owner hooks; the minimal ordinary
Host overrides those hooks without changing the agent protocol.

`rpc_dialogs.py` is the sole native pi RPC in-memory UI baseline, not persistent
task wait detection. `host_io.py` implements only small atomic local records.

## Tests

```sh
python3 -B test_standalone.py
python3 -B test_local_process.py
python3 -B test_generic.py  # Linux-only pidfd/orphan scenarios
python3 -B test_parent_exit.py  # Linux parent TERM/KILL and pre-exec race
```

The standalone suite copies only P, poisons agent discovery and rejects attempted
A imports/reads. It exercises stdin configuration, real peer/get_state, actual
exit 7 and minimal runtime files. Process tests cover native Linux/Darwin facts
(with explicit platform skips). Agent policy tests remain in agentd. Tests use
isolated mutable roots, not production services or user/model acceptance.
