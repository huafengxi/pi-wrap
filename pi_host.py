#!/usr/bin/env python3
"""Explicit ordinary-session host. No agent identity, prompt or settle policy.

Usage: python pi_host.py /absolute/local/declaration.json
See HOST.md. A new invocation is an explicit recovery, never a transparent retry.
"""
import json
import os
from pathlib import Path
import sys
import threading

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
from transport import LaunchConfig, Transport, proto


def load_config(path):
    obj = json.loads(Path(path).read_text())
    required = {'sessionFile', 'cwd', 'sessionName', 'piBinary', 'socketPath', 'env', 'extensionArgv'}
    if type(obj) is not dict or set(obj) != required:
        raise ValueError('declaration must contain exactly ' + ', '.join(sorted(required)))
    if not isinstance(obj['extensionArgv'], list) or any(not isinstance(x, str) for x in obj['extensionArgv']):
        raise ValueError('extensionArgv must be an argv list')
    env = dict(obj['env'])
    # The runtime's real lease is authority, not a declaration-supplied fd.
    if 'RUND_LEASE_FD' in env:
        raise ValueError('RUND_LEASE_FD is runtime-owned, not configurable')
    if 'RUND_LEASE_FD' in os.environ:
        env['RUND_LEASE_FD'] = os.environ['RUND_LEASE_FD']
    return LaunchConfig(obj['sessionFile'], obj['cwd'], obj['sessionName'], obj['piBinary'],
                        tuple(obj['extensionArgv']), obj['socketPath'], env)


def fact(event, **values):
    print(json.dumps(dict(event=event, at=proto.now_ts(), **values)), flush=True)


class Host(Transport):
    def __init__(self, config):
        self.config = config
        self.session_file = config.session_file
        self.session_name = config.session_name
        self.sock_path = config.socket_path
        self.pid_file = self.sock_path + '.pid'
        self.stderr_log = self.sock_path + '.stderr.log'
        self.init_transport()

    def run(self):
        self.setup_socket()
        try:
            self.launch(self.config)
            # Do not expose a usable relay before its stdin owner exists.
            threading.Thread(target=self.accept_loop, daemon=True, name='host-accept').start()
            fact('host_started', hostPid=os.getpid(), hostProcStart=proto.proc_starttime(os.getpid()),
                 childPid=self.pi.pid, childProcStart=proto.proc_starttime(self.pi.pid),
                 sessionFile=self.session_file, sessionName=self.session_name, socketPath=self.sock_path)
            rc = self.pi.wait()  # sole pi wait, no restart loop or report predicate
            self.finish_pumps()
            fact('child_wait', childPid=self.pi.pid, returncode=rc)
            return rc if rc >= 0 else 128 - rc
        finally:
            self.cleanup_socket()


def main():
    try:
        if len(sys.argv) != 2:
            raise ValueError(__doc__)
        return Host(load_config(sys.argv[1])).run()
    except Exception as e:
        fact('host_error', error=repr(e))
        return 1


if __name__ == '__main__':
    sys.exit(main())
