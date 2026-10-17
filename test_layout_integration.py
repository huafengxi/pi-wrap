"""Real candidate processes in the runner's sibling and explicit layouts.

No model/config discovery; lifecycle uses repository fakepi_rpc.py, dialog
RPC uses recovery/fresh-rpc-fixture.py. No protocol implementation is copied.
"""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import unittest

from fixture_support import fixture_dir
from agentd_dependency import require_agentd
DEP = Path(require_agentd('proto', 'wait_state', 'task_state', 'envscrub', 'safe_delete'))
import proto
HERE = Path(__file__).resolve().parent

class LayoutIntegration(unittest.TestCase):
    def setUp(self):
        self.base = Path(fixture_dir(prefix='live-'))
        self.root = self.base / 'ws'
        self.home = self.root / 'agents/task/live'
        (self.home / 'inbox').mkdir(parents=True)
        (self.home / 'session').mkdir()
        (self.root / 'agents/queue/dispatcher/inbox').mkdir(parents=True)
        (self.root / 'env').mkdir()
        (self.root / 'env/host-id').write_text('{}\n')
        (self.home / 'prompt.md').write_text('isolated fixture prompt')
        self.env = dict(HOME=os.environ['HOME'], PATH=os.environ['PATH'],
            AGENT_ROOT=str(self.root), AGENT_HOME=str(self.home), AGENT_SELF='task/live',
            AGENTD_WRAP_PI_BIN=str(HERE / 'fakepi_rpc.py'), FAKE_FLAG_DIR=str(self.home),
            AGENTD_WRAP_SETTLE_WINDOW='0.1', AGENTD_WRAP_ARM_TIMEOUT='0.05',
            AGENTD_WRAP_EXIT_GRACE='2', PYTHONDONTWRITEBYTECODE='1')
        self.sibling = os.environ.get('TEST_LAYOUT')=='sibling'
        if not self.sibling: self.env['AGENTD_DIR'] = str(DEP)
        proto.atomic_write_json(str(self.home/'spec.json'), dict(host='fixture-host', resources=[], reaper='queue/dispatcher'))
        self.clients=[]

    def start(self, entry=None, mode=None):
        if mode:
            fake=self.base/'fakepi-wait'
            fake.write_text('#!'+sys.executable+'\n'+(HERE/'recovery/fresh-rpc-fixture.py').read_text())
            fake.chmod(0o700)
            self.env.update(AGENTD_WRAP_PI_BIN=str(fake), WAIT_MODE=mode, FIXTURE_AGENTD=str(DEP))
        # Keep stderr from every launched process, not only the last loop case.
        self.log=self.base/('wrapper-%d.log'%len(list(self.base.glob('wrapper-*.log'))))
        err=self.log.open('w')
        self.addCleanup(err.close)
        p=subprocess.Popen([sys.executable, '-B', str(entry or HERE/'pi-rpc-wrap.py')],
            env=self.env, cwd=self.base, stdout=subprocess.DEVNULL, stderr=err)
        def stop():
            if p.poll() is None: p.terminate()
            try: p.wait(timeout=5)
            except subprocess.TimeoutExpired: p.kill(); p.wait()
        self.addCleanup(stop)
        self.pid=dict(pid=p.pid, procStart=proto.proc_starttime(p.pid), gen=1,
            status='running', final=False, lastAliveAt=proto.now_ts())
        proto.atomic_write_json(str(self.home/'pid.json'), self.pid)
        return p

    def until(self, predicate):
        deadline=time.monotonic()+12
        while time.monotonic()<deadline:
            result=predicate()
            if result: return result
            time.sleep(.02)
        self.fail('condition timed out: '+self.log.read_text())

    def connect(self):
        c=socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.settimeout(5)
        c.connect(proto.task_sock_path(str(self.root), 'live'))
        self.addCleanup(c.close)
        f=c.makefile('rb')
        self.addCleanup(f.close)
        return c,f

    def barrier(self, c, f, rid):
        c.sendall((json.dumps(dict(type='get_state',id=rid))+'\n').encode())
        events=[]
        while True:
            row=json.loads(f.readline())
            if row.get('type')=='response' and row.get('id')==rid: return events
            events.append(row)

    def page(self):
        # Execute the actual pinned task-tab report, not a local reimplementation.
        env=dict(self.env, AGENTD_DIR=str(DEP))
        r=subprocess.run([sys.executable,'-B',str(DEP/'report.py'),'--root',str(self.root),'-s','active'],
            env=env,cwd=self.base,capture_output=True,text=True,timeout=10)
        self.assertEqual(r.returncode,0,r.stderr)
        return r.stdout

    def test_standard_fakepi_real_relative_and_symlink_lifecycle(self):
        # Full lifecycle/model-error/message/persona/resident suite runs alongside
        # this test in EACH layout. Here startup discovery is deliberately not
        # repaired by injecting AGENTD_DIR in sibling mode.
        for kind in ('real','relative','symlink'):
            with self.subTest(entry=kind):
                entry=HERE/'pi-rpc-wrap.py'
                if kind=='relative': entry=Path(os.path.relpath(entry,self.base))
                if kind=='symlink':
                    entry=self.base/'entry with spaces.py'
                    entry.symlink_to(HERE/'pi-rpc-wrap.py')
                p=self.start(entry)
                self.assertEqual(p.wait(timeout=15),0,self.log.read_text())
                snapshot=json.loads((self.home/'persona_env').read_text())
                self.assertEqual(snapshot['W_WORKSPACE'],str(self.root))
                self.assertEqual(snapshot['AGENT_SELF'],'task/live')
                self.assertFalse((self.home/'diagnosis.md').exists())
                print('actual lifecycle:',os.environ.get('TEST_LAYOUT'),kind,flush=True)

    def test_file_request_exact_reply_without_wait_observation(self):
        old, _ = proto.write_message(str(self.root/'agents/queue/dispatcher/inbox'),
            dict(type='ask', **{'from':'task/live'}, via='send_message', body='older nonblocking ask'))
        p=self.start(mode='ask')
        asks=self.until(lambda: len(proto.list_task_asks(str(self.root), 'task/live')) == 2)
        current=next(a for a in proto.list_task_asks(str(self.root), 'task/live') if a['id'] != old)
        ref=current['id']
        page=self.page()
        self.assertNotIn('待答复（协调者）',page)
        self.assertNotIn('--ref '+ref,page)
        answer=self.base/'answer with spaces.txt'
        answer.write_text('fixture answer')
        r=subprocess.run([sys.executable, '-B', str(DEP/'agentctl.py'), '--root', str(self.root),
            'send', 'task/live', '--type', 'reply', '--ref', ref, '--body-file', str(answer)],
            env=dict(self.env, AGENT_SELF='queue/dispatcher'),cwd=self.base,text=True,capture_output=True,timeout=10)
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(p.wait(timeout=10),0,self.log.read_text())
        self.assertEqual(proto.find_pending_ask(str(self.root), 'task/live')['id'], old)
        self.assertFalse((self.home/'wait-state.json').exists())
        self.assertEqual([m['ref'] for m in proto.list_messages(str(self.home/'inbox'))],[ref])
        print('actual file producer/exact-ref reply (no wait report):',ref,flush=True)

    def test_all_dialog_methods_and_observer_attach_replay(self):
        for method in ('select','confirm','input','editor'):
            with self.subTest(method=method):
                # Resident keeps the real observer socket open after resolution.
                self.env['AGENTD_RESIDENT']='1'
                p=self.start(mode=method)
                self.until(lambda: Path(proto.task_sock_path(str(self.root), 'live')).exists())
                self.assertNotIn('待用户答复',self.page())
                # Prompt acceptance precedes the producer's burst; an explicit
                # fixture barrier ensures ring eviction before the first attach.
                self.until(lambda: (self.home/'dialog-emitted').exists())
                c,f=self.connect()
                events=self.barrier(c,f,'first')
                dialogs=[x for x in events if x.get('type')=='extension_ui_request' and x.get('method')==method]
                self.assertEqual(len(dialogs),1,events)
                self.assertEqual(dialogs[0]['id'],'live-dialog')
                self.assertGreater(dialogs[0]['timeout'],0)
                self.assertLess(dialogs[0]['timeout'],30000)
                c2,f2=self.connect()
                events2=self.barrier(c2,f2,'second')
                self.assertEqual(len([x for x in events2 if x.get('id')=='live-dialog']),1)
                ticks=[x['index'] for x in events2 if x.get('type')=='fixture_tick']
                self.assertTrue(ticks)
                self.assertGreater(min(ticks),0, 'raw ring must have evicted the original dialog')
                c2.sendall(b'{"type":"extension_ui_response","id":"live-dialog","value":"yes"}\n')
                self.barrier(c2,f2,'answered')
                self.assertNotIn('**❓',self.page())
                self.assertFalse((self.home/'wait-state.json').exists())
                c3,f3=self.connect()
                resolved=self.barrier(c3,f3,'third')
                self.assertFalse(any(x.get('id')=='live-dialog' for x in resolved),resolved)
                print('actual dialog/replay/resolution:',method,flush=True)
                p.terminate(); p.wait(timeout=5)
                for conn,stream in ((c,f),(c2,f2),(c3,f3)):
                    stream.close(); conn.close()

if __name__=='__main__':
    unittest.main(verbosity=2)
