# H1 — explicit ordinary-session host (offline, not activated)

Task `8pm0lt`, based on P `afec43d015c7955687c1d1c614af353d52caeedb`.
Only fakepi/Linux/local sockets have been exercised. No web/receiver/reaper
routing, model, deployment or new session family is provided. This is **not H2**.

## Entries, configuration and dependency

- `pi_host.py /absolute/local/declaration.json`: ordinary session, no agent
  registration, `AGENT_SELF`, `AGENTD_RESIDENT`, spec/pid/enable or agent runtime
  tree. Does not create task/bot/queue mailboxes, infer an address from
  `PI_SESSION_ID`, inject a prompt, or evaluate report/agent_settled completion.
- `pi-rpc-wrap.py`: existing agent adapter, still requires its original agent
  environment. Prompt idempotence, ready/armed handshake, persona/extension argv
  and env, settle_loop, model_error_gate and diagnosis stay here. Existing task
  socket/ready paths are unchanged; task metadata is retained in the endpoint
  owner document. It uses the same `LaunchConfig` and `Transport.launch`.
- Both inherit **the identical method objects** in `transport.Transport` for
  socket setup, one Popen, stdin serialization, stdout/stderr pumps, bounded ring,
  single-client replacement, dialog replay and relay. There is no second pump,
  dialog state machine, broker, supervisor or restart-pi loop.

Example **synthetic declaration**, all paths explicitly provisioned by caller:

```json
{
  "sessionFile": "/tmp/example-h1/session.jsonl",
  "cwd": "/tmp/example-h1/work",
  "sessionName": "ordinary-example",
  "piBinary": "/tmp/example-h1/fakepi",
  "extensionArgv": [],
  "socketPath": "/tmp/example-h1/local/s",
  "env": {
    "HOME": "/tmp/example-h1/home",
    "TMPDIR": "/tmp/example-h1/tmp",
    "PATH": "/usr/bin:/bin"
  }
}
```

All fields are required, unknown fields rejected. `extensionArgv` is only a list
of `-e`, explicit path pairs, not a second arbitrary CLI. `env` is the complete
child environment (no implicit full host environment fallback); cwd/session/
socket must be absolute. Caller chooses the stable session name separately from
any presentation title. No automatic renaming or mailbox migration occurs.

A **tool dependency remains**: A `b40e0d027b1ebbe072b0edc0929e0411da3c37af`,
`proto.py` (identity/atomic IO only) and `wait_state.py` (memory-only
DialogRequests). Ordinary host does not import agent task/report/runner modules.
The agent adapter additionally uses the original `task_state.report_nonempty`.
`agentd_dependency.py` uses explicit `AGENTD_DIR` or real-source sibling `agentd`,
checks origins/interfaces, never searches `~/m` or a platform fallback. The
runtime locator does not itself enforce a Git SHA: deployment/export must select
the pin. The H1 runner exports that exact A and verifies the five original tool
hashes from frozen `fresh-dependencies.json`. Nothing is vendored to claim zero
static dependency. `h1-dependencies.json` adds H1's test-only S and A0 pins;
pre-H1 recovery history/dependency maps/runners remain unchanged.

## PIPE, socket and process ownership

```text
standalone caller OR fixed S Runtime Service(manual)
    | (S alone owns host generation, stop, pid/group identity and lease)
    v
P Host / agent Wrap
    +-- sole pi Popen, stdin PIPE, stdout PIPE, stderr PIPE, real pi wait
    +-- Transport: stdout continuously -> ring + current client
    +-- Transport: current client -> serialized pi stdin
    +-- one replaceable AF_UNIX client, never an independent pi owner
         (web/client implementation is outside H1)
```

Connection count is not a runtime generation. Disconnect/replacement only shuts
down that socket connection, not pi stdin; it never replays user commands.
A slow/non-reading client is dropped after a bounded 0.2s socket write timeout.
With no client, stdout is still drained and the 200-line ring retained. Full
history/state remains a client-issued native RPC query, not a new persistence
protocol. A relay command is accepted only while its connection is current.
Accepted stdin writes have normal PIPE ambiguity; no exactly-once guarantee or
crash-durable command queue is claimed.

The **local runtime directory (socket parent) is part of the stable declaration**
for a session: all callers for that canonical session must use that same local
directory. Keep it private and outside protocol/sync trees; choose a short path
for platform sun_path limits. A socket-path flock plus a canonical session-key
flock reject concurrent hosts (also for a different socket basename in the same
declared local directory). Locks remain as private files and are never unlinked,
preventing inode replacement/ABA. Socket is 0600. Owner metadata records real
pid/procStart, sessionFile/name, not agent registration or fabricated generation.
Cleanup removes only its own bound socket inode and owner document. Unknown
existing endpoints or a live owner fail closed; proven dead owner permits stale
socket takeover. The mechanism does not arbitrate deliberately conflicting
local-directory declarations, cross-host filesystems, or hostile same-uid writers.

Dialog replay uses only this process's fixed A `DialogRequests` and actual wire
signals/successful stdin responses. Reattach replays current unsettled requests;
answer, tool/turn completion or deadline settles them. Historical ring dialogs
are filtered, so they cannot resurrect. A new host starts with empty dialog
memory. An unpaired historical JSONL tool call is **not** evidence of waiting.
No extra wait JSON, native fingerprint, waitToken or waiting observation is read
or written. The pre-existing limitation of native internal signal settlement
without a wire event remains; mock-native tests do not close the real-pi gate.

`host_started` is a fact about the actual child and declared session; `child_wait`
is emitted only following real Popen.wait and contains its raw returncode. A
host crash/disconnection with no wait fact is **not a child exit fact**. Session
history is retained; recovery is an explicit new invocation (or S's explicit new
manual generation), not transparent continuation or lossless memory recovery.
One host invocation never launches a second pi. Ordinary host does not kill a
process group or write runtime state. SIGKILL may orphan a child temporarily;
only the runtime or explicitly owning harness cleans its known group/identities.

When hosted, `RUND_LEASE_FD` comes from S, cannot be overridden in ordinary JSON,
and is checked as a real open descriptor then passed using Popen.pass_fds.
Pi inherits the host's group/session; cooperative foreground descendants must
also retain that same lease until they exit. Host does not invent a lease,
generation, task pid or liveness bridge. Unknown group/lease/starting conservative
gates in S are not changed. Noncooperative daemonization is not supported here.

## Reproduce without a model

From a clean export of the pushed P object, with existing local tools only:

```sh
/absolute/miniconda3/bin/python -B recovery/h1-run-tests.py \
  --agentd-bare /absolute/agentd.git \
  --serviced-bare /absolute/serviced.git \
  --caller-repo /absolute/read-only-caller-repo \
  --bash32 /absolute/existing/bash-3.2 \
  --native-rpc-source /absolute/installed/rpc-mode.js \
  --output /tmp/NEW-SHORT-H1-ROOT
```

The runner exports fixed inputs into new roots, checks harmless guard bait only,
runs eight existing P test files plus new ordinary/runtime tests in sibling and
explicit non-sibling layouts, the four existing A supplemental files, the
original eight socket scenarios, and fixed S prepare/lifecycle. The socket
oracles originate in task/0uzhg3; only input/evidence/interpreter paths changed.
Generic fakepi records real PID/stdio inodes, argv identity, commands/session,
wire response/events and effects; >24 MB no-client output and a connected
non-reader exercise pump ownership. Tests retain roots/logs and independently
reap only their own Popen/pidfd identities, never use production as a rejection
fixture. S integration supplies only fixed original proto/wait_state tool blobs
and proves group/lease retention across host exit, conservative blocking, manual
no-restart, explicit new generation and stop. It is a synthetic fixture, not an
actual model or receiver confirmation.

`results.json` records command/environment/cwd/rc and all skip text; individual
logs preserve raw output and retained artifact roots. Original persona P14 may
print “passed” while internally skipping missing kb-index; count that as skip.
Without actual marked input two supplemental A HTML tests skip. Existing S58⑤
cross-language skip and older unknowns are not discharged by H1. True pi/model,
TS receiver strong injection, browser, macOS/SSH, live toolface driver,
destructive clean.sh, production sockets/services/sync/GC and H2/Y1/full DAG
remain untested/unapproved.
