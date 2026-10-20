# Local process facts

`local_process.py` provides local process identity and AF_UNIX peer facts for
`pi_host.Host` and its explicitly configured Web client. It does not register
agents or run a lifecycle owner. Generic Transport uses these local facts by
default. The agent adapter in A overrides identity hooks with its own proto
contract; generic P neither imports nor discovers A.

- Linux: start remains the integer `/proc/<pid>/stat` field 22 tick count.
- Darwin: `proc_pidinfo(PROC_PIDTBSDINFO)` returns start seconds/microseconds;
  start is encoded as integer Unix microseconds. `host_started` and socket owner
  `procStart` use that same integer. W compares its string representation.
- Identity additionally includes boot, uid, process group, session, parent and
  state. Darwin uses `kern.boottime`, BSD info and `getsid`; its start and group
  are rechecked after `getsid`. Missing is distinct from denied/incomplete data.
- An owner metadata record without start cannot authorize replacing a live PID.
  Linux and Darwin start units are platform-local, not transferable tokens.
- Peer facts: Linux SO_PEERCRED; Darwin LOCAL_PEERPID plus getpeereid. Neither
  side replaces kernel peer credentials with a caller-provided PID.
- Darwin group enumeration is limited to the current uid's cooperative local
  foreground tree. It is a repeated snapshot, not a guarantee against hostile
  daemonization or UID changes.

The module provides observations, not atomic signal handles. A caller may use
Darwin check-identity-then-kill with an explicitly accepted PID-reuse race. A
failed read or identity mismatch is not authorization to signal. Linux callers
can continue to use pidfd. No new stop RPC, worker, service or dependency on a
runtime generation is introduced.

Run `python3 -B test_local_process.py` for real
own-process/child/peer checks and deterministic Darwin error-path tests. The
Linux-specific token oracle skips on Darwin; that skip is not a Darwin failure
or evidence of Linux behavior on Darwin. Full ordinary-host and Web lifecycle
integration has separate test roots and evidence.
