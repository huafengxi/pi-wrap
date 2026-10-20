#!/usr/bin/env python3
"""Independent ordinary pi host. Launch with JSON on stdin (argument '-').

A caller may also supply an explicit JSON input file. Runtime output consists
of the socket and optional diagnostics; host.json belongs to the Web caller.
"""
import json
import os
from pathlib import Path
import sys
import threading

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
from transport import LaunchConfig, Transport
import host_io
import local_process


def load_config(path):
    obj = json.load(sys.stdin) if path == '-' else json.loads(Path(path).read_text())
    required = {'sessionFile', 'cwd', 'sessionName', 'piBinary', 'socketPath', 'env', 'extensionArgv'}
    if type(obj) is not dict or set(obj) != required:
        raise ValueError('declaration must contain exactly ' + ', '.join(sorted(required)))
    if not isinstance(obj['extensionArgv'], list) or any(not isinstance(x, str) for x in obj['extensionArgv']):
        raise ValueError('extensionArgv must be an argv list')
    env = dict(obj['env'])
    return LaunchConfig(obj['sessionFile'], obj['cwd'], obj['sessionName'], obj['piBinary'],
                        tuple(obj['extensionArgv']), obj['socketPath'], env)


def fact(event, **values):
    # Optional diagnostics for standalone callers. Runtime readers use host.json,
    # never parse this log as a second source of process truth.
    print(json.dumps(dict(event=event, at=host_io.now_ts(), **values)), flush=True)


class Host(Transport):
    def __init__(self, config):
        self.config = config
        self.session_file = config.session_file
        self.session_name = config.session_name
        self.sock_path = config.socket_path
        self.state_file = str(Path(self.sock_path).with_name('host.json'))
        self.stderr_log = str(Path(self.sock_path).with_name('host.log'))
        self.init_transport()

    def read_socket_owner(self):
        previous = host_io.read_owner(self.state_file)
        if previous is None:
            return None
        if not isinstance(previous, dict) or not {'pid', 'start'} <= previous.keys():
            raise RuntimeError('missing host identity')
        return dict(pid=previous['pid'], procStart=previous['start'])

    def publish_socket_owner(self, owner):
        # Web publishes its verified P identity after real peer/get_state.
        pass

    def launch_argv(self, config):
        if sys.platform.startswith('linux'):
            return [sys.executable, '-B', str(Path(_HERE) / 'child_exec.py'),
                    str(os.getpid()), *config.argv()]
        # Native pi RPC exits on stdin EOF when P closes or loses its PIPEs.
        return config.argv()

    def remove_socket_owner(self):
        # Web owns host.json, including retirement of old identities.
        pass

    def run(self):
        self.setup_socket()
        try:
            self.launch(self.config)
            child = local_process.identity(self.pi.pid)
            if child is None:
                raise RuntimeError('child start identity unavailable')
            threading.Thread(target=self.accept_loop, daemon=True, name='host-accept').start()
            fact('host_started', hostPid=os.getpid(), hostProcStart=local_process.starttime(os.getpid()),
                 childPid=self.pi.pid, childProcStart=child['start'],
                 sessionFile=self.session_file, sessionName=self.session_name, socketPath=self.sock_path)
            rc = self.pi.wait()  # sole pi wait, no restart or report predicate
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
