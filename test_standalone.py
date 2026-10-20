"""Copy only P to an unrelated root; poison A discovery and exercise real RPC."""
import ast
import importlib.util
import json
import os
from pathlib import Path
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

import local_process as O
from rpc_dialogs import DialogRequests

P = Path(__file__).resolve().parent
RUNTIME = ('pi_host.py', 'transport.py', 'host_io.py', 'local_process.py', 'rpc_dialogs.py', 'child_exec.py')


class Standalone(unittest.TestCase):
    def test_runtime_imports_are_stdlib_or_local(self):
        local = {Path(n).stem for n in RUNTIME}
        for file in RUNTIME:
            for node in ast.walk(ast.parse((P / file).read_text())):
                names = ([a.name for a in node.names] if isinstance(node, ast.Import) else
                         [node.module or ''] if isinstance(node, ast.ImportFrom) else [])
                for name in names:
                    self.assertIn(name.split('.')[0], sys.stdlib_module_names | local, (file, name))
        self.assertFalse((P / 'pi-rpc-wrap.py').exists())
        self.assertFalse((P / 'persona.py').exists())

    def test_copied_directory_real_rpc_without_agentd(self):
        root = Path(tempfile.mkdtemp(prefix='p-solo-', dir='/tmp')).resolve()
        print('P_STANDALONE_ROOT=' + str(root), flush=True)
        code = root / 'only-p'
        shutil.copytree(P, code, ignore=shutil.ignore_patterns('__pycache__', '.git'))
        poison = root / 'poison'; poison.mkdir()
        for name in ('proto', 'task_state', 'wait_state', 'persona', 'agentd_dependency'):
            (poison / (name + '.py')).write_text("raise RuntimeError('agent dependency used')\n")
        home = root / 'home'; home.mkdir()
        fake = root / 'fakepi'
        fake.write_text('#!' + sys.executable + '\n' + '''import json,sys
for line in sys.stdin:
 q=json.loads(line)
 print(json.dumps(dict(type='response',id=q['id'],command=q['type'],success=True,data={})),flush=True)
 if q['type']=='exit': sys.exit(7)
''')
        fake.chmod(0o700)
        env = dict(HOME=str(home), PATH=os.defpath, AGENTD_DIR=str(poison), PYTHONPATH=str(poison),
                   PYTHONDONTWRITEBYTECODE='1')
        cfg = dict(sessionFile=str(root / 'history.jsonl'), cwd=str(root), sessionName='standalone',
                   piBinary=str(fake), socketPath=str(root / 's'), extensionArgv=[], env=env)
        # Reject even an attempted read/import of A, instead of merely checking
        # that a convenient installed checkout was not needed in this test.
        launcher = '''import os,sys,runpy
forbidden={'proto','task_state','wait_state','persona','agentd_dependency'}
poison=sys.argv.pop(1); entry=sys.argv.pop(1)
def audit(event,args):
 if event=='import' and args[0].split('.')[0] in forbidden: raise RuntimeError('forbidden agent import')
 if event=='open' and isinstance(args[0],str) and os.path.realpath(args[0]).startswith(poison+os.sep):
  raise RuntimeError('forbidden agent read')
sys.addaudithook(audit)
runpy.run_path(entry,run_name='__main__')
'''
        out = (root / 'facts.jsonl').open('w'); err = (root / 'stderr').open('w')
        p = subprocess.Popen([sys.executable, '-I', '-B', '-c', launcher, str(poison),
                              str(code / 'pi_host.py'), '-'],
                             env=env, cwd=home, stdin=subprocess.PIPE, stdout=out, stderr=err)
        p.stdin.write(json.dumps(cfg).encode()); p.stdin.close()
        facts = []; peer = None; conn = socket.socket(socket.AF_UNIX); conn.settimeout(5)
        try:
            end = time.monotonic() + 8
            while time.monotonic() < end:
                facts = [json.loads(s) for s in (root / 'facts.jsonl').read_text().splitlines()]
                if any(f['event'] == 'host_started' for f in facts): break
                self.assertIsNone(p.poll(), (root / 'stderr').read_text())
                time.sleep(.02)
            start = next(f for f in facts if f['event'] == 'host_started')
            self.assertEqual(start['hostPid'], p.pid)
            self.assertFalse((root / 'host.json').exists(), 'P must not publish caller metadata')
            caller_record = json.dumps({k: O.identity(p.pid)[k] for k in ('pid', 'start', 'boot')})
            (root / 'host.json').write_text(caller_record)
            conn.connect(cfg['socketPath']); peer = O.peer_credentials(conn)
            self.assertEqual(peer[:2], (p.pid, os.getuid()))
            with conn.makefile('rb') as stream:
                for command in ('get_state', 'exit'):
                    request = dict(type=command, id='solo-' + command)
                    conn.sendall(json.dumps(request).encode() + b'\n')
                    while True:
                        response = json.loads(stream.readline())
                        if response.get('id') == request['id']: break
                    self.assertIs(response['success'], True)
                    self.assertEqual(response['command'], command)
            self.assertEqual(p.wait(timeout=8), 7)
            facts = [json.loads(s) for s in (root / 'facts.jsonl').read_text().splitlines()]
            self.assertEqual(facts[-1]['event'], 'child_wait')
            self.assertEqual(facts[-1]['returncode'], 7)
            self.assertEqual(list(root.rglob('*.lock')), [])
            self.assertEqual((root / 'host.json').read_text(), caller_record)
            state = json.loads(caller_record)
            self.assertEqual(state['pid'], p.pid)
            self.assertNotIn('childWait', state)
            self.assertNotIn('instance', state)
            self.assertNotIn('child', state)
            for name in ('s.pid', 's.stderr.log', 'config.json', 'current.json', 'binding.json'):
                self.assertFalse((root / name).exists(), name)
            self.assertFalse((root / 's').exists())
        finally:
            conn.close()
            if p.poll() is None: p.terminate(); p.wait(timeout=8)
            for f in facts:
                if f.get('event') == 'host_started' and O.owner_alive(f['childPid'], f['childProcStart']):
                    os.kill(f['childPid'], signal.SIGKILL)
            out.close(); err.close()
        (root / 'result.json').write_text(json.dumps(dict(peer=peer, facts=facts, exitcode=p.returncode)))

    def test_native_dialog_baseline_not_agent_policy(self):
        d = DialogRequests()
        d.event(dict(type='agent_start'))
        d.event(dict(type='tool_execution_start', toolCallId='native'))
        d.event(dict(type='extension_ui_request', method='input', id='one', timeout=30000))
        self.assertEqual([r['id'] for r in d.dialogs()], ['one'])
        d.sent(dict(type='extension_ui_response', id='one', value='ok'))
        self.assertEqual(d.dialogs(), [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
