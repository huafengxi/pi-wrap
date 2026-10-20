"""Actual parent TERM/KILL with a child ignoring both stdin EOF and TERM."""
import ctypes
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

import local_process as L

P = Path(__file__).resolve().parent


def until(fn):
    end = time.monotonic() + 8
    while time.monotonic() < end:
        value = fn()
        if value:
            return value
        time.sleep(.02)
    raise TimeoutError('condition not reached')


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux kernel parent-death contract')
class ParentExit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) == 0

    def test_parent_term_and_kill_reap_uncooperative_child(self):
        for sig in (signal.SIGTERM, signal.SIGKILL):
            with self.subTest(sig=sig):
                root = Path(tempfile.mkdtemp(prefix='p-parent-')).resolve()
                fake = root/'fakepi'
                fake.write_text('#!'+sys.executable+'\n'+'''import json,os,signal,time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path('child-ready').write_text(str(os.getpid()))
while True: time.sleep(1)
''')
                fake.chmod(0o700)
                config = dict(sessionFile=str(root/'history.jsonl'), cwd=str(root),
                              sessionName='parent-exit', piBinary=str(fake), socketPath=str(root/'s'),
                              extensionArgv=[], env=dict(PATH=os.defpath, HOME=str(root)))
                with (root/'host.log').open('wb') as log:
                    p = subprocess.Popen([sys.executable, '-B', str(P/'pi_host.py'), '-'],
                                         stdin=subprocess.PIPE, stdout=log, stderr=log, start_new_session=True)
                    child = None
                    try:
                        p.stdin.write(json.dumps(config).encode()); p.stdin.close()
                        until(lambda: (root/'child-ready').exists())
                        child = L.identity(int((root/'child-ready').read_text()))
                        self.assertEqual(child['ppid'], p.pid)
                        self.assertFalse((root/'host.json').exists())
                        # A different process in another session must be untouched.
                        with subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(60)'],
                                              start_new_session=True) as unrelated:
                            try:
                                p.send_signal(sig); self.assertEqual(p.wait(timeout=8), -sig)
                                until(lambda: not L.identity(child['pid']) or L.identity(child['pid'])['state'] == 'Z')
                                self.assertEqual(os.waitpid(child['pid'], 0)[1], signal.SIGKILL)
                                self.assertIsNone(unrelated.poll())
                                self.assertFalse((root/'host.json').exists())
                            finally:
                                unrelated.kill(); unrelated.wait(timeout=5)
                    finally:
                        if p.poll() is None: p.kill(); p.wait(timeout=5)
                        if child:
                            now = L.identity(child['pid'])
                            if now and now['start'] == child['start']:
                                os.kill(child['pid'], signal.SIGKILL)
                                try: os.waitpid(child['pid'], 0)
                                except ChildProcessError: pass
                print('P_PARENT_ROOT='+str(root), flush=True)

    def test_parent_already_gone_refuses_to_exec(self):
        root = Path(tempfile.mkdtemp(prefix='p-parent-race-'))
        result = subprocess.run([sys.executable, str(P/'child_exec.py'), '0', sys.executable,
                                 '-c', 'from pathlib import Path;Path("ran").touch()'], cwd=root)
        self.assertEqual(result.returncode, 125)
        self.assertFalse((root/'ran').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
