"""Real isolated fakepi/socket subprocess tests; evidence is retained, not deleted."""
import ctypes
import importlib.util
import json
import os
import platform
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

P = Path(__file__).resolve().parent
BASE = Path(tempfile.mkdtemp(prefix='h1-', dir='/tmp'))
# Explicit harness ownership of orphans after intentional host SIGKILL.
libc = ctypes.CDLL(None, use_errno=True)
assert libc.prctl(36, 1, 0, 0, 0) == 0
assert platform.machine() in ('x86_64', 'aarch64'), 'pidfd ABI not verified'
libc.syscall.restype = ctypes.c_long


def pidfd_open(pid):
    fd = libc.syscall(434, pid, 0)
    if fd < 0:
        raise OSError(ctypes.get_errno(), 'pidfd_open')
    return fd


def pidfd_signal(fd, sig):
    if libc.syscall(424, fd, sig, ctypes.c_void_p(0), 0) != 0:
        raise OSError(ctypes.get_errno(), 'pidfd_send_signal')


_preflight = pidfd_open(os.getpid())
pidfd_signal(_preflight, 0)
os.close(_preflight)
print('H1 evidence:', BASE, flush=True)


def until(fn, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            value = fn()
        except FileNotFoundError:
            value = None
        if value:
            return value
        time.sleep(.01)
    raise AssertionError('timed out: ' + repr(fn))


def identity(pid):
    try:
        fields = Path('/proc', str(pid), 'stat').read_text().split(') ')[1].split()
        return fields[19]
    except FileNotFoundError:
        return None


class Case(unittest.TestCase):
    def setUp(self):
        self.area = BASE/self._testMethodName
        self.area.mkdir()
        for d in ('home', 'tmp', 'cwd', 'local'):
            (self.area/d).mkdir()
        self.children = []
        self.known = []
        self.clients = []
        self.logs = []
        self.events = (self.area/'wire.jsonl').open('w')
        self.session = self.area/'history.jsonl'
        self.session.write_text('{"type":"session","id":"historical-original"}\n')
        with self.session.open('a') as f:
            f.write(json.dumps(dict(type='message', message=dict(role='assistant', content=[
                dict(type='toolCall', id='old-unpaired', name='ask_user', arguments=dict(question='not a live dialog'))])))+'\n')
        # A fake executable with an absolute Python interpreter, no PATH/model lookup.
        fake = self.area/'fakepi'
        fake.write_text('#!'+sys.executable+'\n'+(P/'generic_fakepi.py').read_text())
        fake.chmod(0o700)
        self.config = dict(sessionFile=str(self.session), cwd=str(self.area/'cwd'), sessionName='ordinary-h1',
                           piBinary=str(fake), socketPath=str(self.area/'local/s'), extensionArgv=[],
                           env=dict(HOME=str(self.area/'home'), TMPDIR=str(self.area/'tmp'), PATH=os.defpath,
                                    H1_AREA=str(self.area), H1_BURST='0'))
        self.env = dict(HOME=str(self.area/'home'), TMPDIR=str(self.area/'tmp'), PATH=os.defpath,
                        PYTHONDONTWRITEBYTECODE='1')
        if 'AGENTD_DIR' in os.environ:
            self.env['AGENTD_DIR'] = os.environ['AGENTD_DIR']

    def record(self, **obj):
        self.events.write(json.dumps(dict(at=time.time(), **obj))+'\n')
        self.events.flush()

    def start(self, config=None):
        n = len(self.children)
        config = config or self.config
        declaration = self.area/('config%d.json' % n)
        declaration.write_text(json.dumps(config, indent=2))
        log = (self.area/('host%d.log' % n)).open('w')
        err = (self.area/('host%d.stderr' % n)).open('w')
        self.logs += [log, err]
        argv = [sys.executable, '-B', str(P/'pi_host.py'), str(declaration)]
        p = subprocess.Popen(argv, env=self.env, cwd=self.area/'cwd', stdout=log, stderr=err)
        self.children.append(p)
        self.record(spawn=argv, env=self.env, pid=p.pid, procStart=identity(p.pid), declaration=config)
        return p

    def ready(self):
        def fresh():
            doc = json.loads((self.area/'ready.json').read_text())
            return doc if doc['pid'] not in {d['pid'] for d in self.known} else None
        doc = until(fresh)
        self.known.append(doc)
        return doc

    def connect(self):
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.settimeout(8)
        c.connect(self.config['socketPath'])
        f = c.makefile('rb')
        self.clients.append((c, f))
        return c, f

    def command(self, conn, type='get_state', **kw):
        rid = 'command-'+str(time.monotonic_ns())
        obj = dict(type=type, id=rid, **kw)
        conn[0].sendall((json.dumps(obj)+'\n').encode())
        self.record(sent=obj)
        seen = []
        while True:
            raw = conn[1].readline()
            assert raw, 'unexpected socket EOF'
            obj = json.loads(raw)
            self.record(received=obj)
            seen.append(obj)
            if obj.get('type') == 'response' and obj.get('id') == rid:
                return seen

    def dialogs(self, events):
        return [e['id'] for e in events if e.get('type') == 'extension_ui_request']

    def kill_known(self, pid, start):
        if identity(pid) is not None:
            assert identity(pid) == start, 'refuse changed process identity'
            fd = pidfd_open(pid)
            try:
                assert identity(pid) == start
                pidfd_signal(fd, signal.SIGKILL)
            finally:
                os.close(fd)
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass

    def tearDown(self):
        for c, f in self.clients:
            f.close(); c.close()
        for p in self.children:
            if p.poll() is None:
                p.kill()
            p.wait(timeout=5)
        for name in ('ready.json', 'descendant.json'):
            path = self.area/name
            if path.exists():
                self.known.append(json.loads(path.read_text()))
        for doc in self.known:
            self.kill_known(doc['pid'], doc['start'])
        for f in self.logs:
            f.close()
        self.record(cleanup='only own Popen and recorded pidfd identities', children=[p.returncode for p in self.children])
        self.events.close()

    def test_01_no_agent_pump_replacement_and_dialog(self):
        self.config['env']['H1_BURST'] = '6000'  # >24 MB with NO client
        p = self.start(); before = self.ready()
        until(lambda: (self.area/'burst-done').read_text() == '6000')
        self.assertIsNone(p.poll())
        self.assertEqual(os.stat(self.config['socketPath']).st_mode & 0o777, 0o600)
        conn = self.connect()
        first = self.command(conn)
        self.assertEqual(first[-1]['data'], before)
        self.assertEqual(self.dialogs(first), [], 'historical unpaired tool call is not a live wait')
        self.assertGreater(min(x['index'] for x in first if x.get('type') == 'tick'), 0)
        self.command(conn, 'effect')
        self.command(conn, 'dialog')
        conn2 = self.connect()
        self.assertEqual(self.dialogs(self.command(conn2)), ['live'])
        self.assertEqual(self.command(conn2)[-1]['data'], before)
        self.assertEqual(conn[1].read(), b'', 'new client replaces old without closing stdin')
        conn2[0].sendall(b'{"type":"extension_ui_response","id":"live","value":"yes"}\n')
        self.command(conn2)
        conn3 = self.connect()
        self.assertEqual(self.dialogs(self.command(conn3)), [])
        self.command(conn3, 'dialog'); self.command(conn3, 'end_turn')
        self.assertEqual(self.dialogs(self.command(self.connect())), [])
        # No agent_settled/report predicate in ordinary host.
        conn4 = self.clients[-1]
        self.command(conn4, 'settled'); time.sleep(.6)
        self.assertIsNone(p.poll())
        # Explicit disconnect also leaves child/stdin unchanged.
        conn4[1].close(); conn4[0].close()
        conn5 = self.connect()
        self.assertEqual(self.command(conn5)[-1]['data'], before)
        # A connected observer that never reads must not stop stdout draining.
        conn5[0].sendall(b'{"type":"burst","id":"slow-reader"}\n')
        until(lambda: (self.area/'burst-done').read_text() == '5000')
        conn6 = self.connect()
        self.assertEqual(self.command(conn6)[-1]['data'], before)
        self.command(conn6, 'exit', code=7)
        self.assertEqual(p.wait(timeout=6), 7)
        self.assertEqual(len((self.area/'effects').read_text().splitlines()), 1)
        self.assertTrue(self.session.read_text().startswith('{"type":"session","id":"historical-original"}\n'))
        self.assertFalse((self.area/'agents').exists())
        self.assertFalse((self.area/'report.md').exists())
        facts = [json.loads(x) for x in (self.area/'host0.log').read_text().splitlines()]
        self.assertEqual(facts[-1]['event'], 'child_wait')
        self.assertEqual(facts[-1]['returncode'], 7)
        self.assertEqual(len([x for x in facts if x['event']=='host_started']), 1)

    def test_02_double_host_and_explicit_recovery(self):
        p = self.start(); old = self.ready(); conn = self.connect()
        self.command(conn, 'dialog')
        sock = Path(self.config['socketPath']); inode = sock.stat().st_ino
        duplicate = self.start()
        self.assertEqual(duplicate.wait(timeout=6), 1)
        self.assertEqual(sock.stat().st_ino, inode)
        # Same canonical session, even a different endpoint in the declared runtime dir.
        cfg = dict(self.config, socketPath=str(sock.parent/'other'))
        duplicate2 = self.start(cfg)
        self.assertEqual(duplicate2.wait(timeout=6), 1)
        self.assertFalse(Path(cfg['socketPath']).exists())
        self.assertIsNone(p.poll())
        self.assertEqual(self.command(conn)[-1]['data'], old)
        self.command(conn, 'exit')
        self.assertEqual(p.wait(timeout=6), 0)
        saved = self.session.read_bytes()
        again = self.start()
        until(lambda: (self.area/'host3.log').stat().st_size > 0)
        new = self.ready()
        self.assertNotEqual(old['pid'], new['pid'])
        self.assertEqual(self.dialogs(self.command(self.connect())), [])
        self.assertTrue(self.session.read_bytes().startswith(saved))
        self.command(self.clients[-1], 'exit')
        self.assertEqual(again.wait(timeout=6), 0)

    def test_03_host_crash_is_not_child_wait(self):
        p = self.start(); old = self.ready(); conn = self.connect()
        self.command(conn, 'dialog')
        saved = self.session.read_bytes()
        p.kill(); self.assertEqual(p.wait(timeout=6), -9)
        facts = [json.loads(x) for x in (self.area/'host0.log').read_text().splitlines()]
        self.assertFalse(any(x['event']=='child_wait' for x in facts))
        self.record(host_crash='harness SIGKILL', child_still_alive=identity(old['pid']) == old['start'])
        # Child may see PIPE EOF and exit itself; host did not wait for it.
        # Explicit harness cleanup of its orphan, not a hidden host retry.
        self.kill_known(old['pid'], old['start'])
        again = self.start()
        until(lambda: (self.area/'host1.log').stat().st_size > 0)
        new = self.ready(); self.assertNotEqual(old['pid'], new['pid'])
        self.assertEqual(self.dialogs(self.command(self.connect())), [])
        self.assertTrue(self.session.read_bytes().startswith(saved))
        self.command(self.clients[-1], 'exit')
        self.assertEqual(again.wait(timeout=6), 0)

    def test_04_actual_shared_method_imports(self):
        import pi_host, transport
        spec = importlib.util.spec_from_file_location('h1_agent', P/'pi-rpc-wrap.py')
        wrapper = importlib.util.module_from_spec(spec); spec.loader.exec_module(wrapper)
        self.assertIs(pi_host.Host.__mro__[1], transport.Transport)
        self.assertIs(wrapper.Wrap.__mro__[1], transport.Transport)
        for name in ('pi_send', 'launch', '_read_stdout', 'accept_loop', 'replay_lines', '_conn_relay', 'setup_socket'):
            self.assertIs(getattr(wrapper.Wrap, name), getattr(pi_host.Host, name), name)
        self.record(imports=dict(transport=transport.__file__, proto=transport.proto.__file__,
                                 DialogRequests=__import__('wait_state').__file__), shared='actual identical method objects')

    def test_06_unknown_socket_and_config_refusal(self):
        bait = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        bait.bind(self.config['socketPath']); bait.listen(1)
        try:
            inode = Path(self.config['socketPath']).stat().st_ino
            p = self.start()
            self.assertEqual(p.wait(timeout=6), 1)
            self.assertEqual(Path(self.config['socketPath']).stat().st_ino, inode)
            self.assertFalse((self.area/'ready.json').exists())
            cfg = dict(self.config, extensionArgv=['--arbitrary-option'])
            p = self.start(cfg)
            self.assertEqual(p.wait(timeout=6), 1)
            self.assertEqual(Path(self.config['socketPath']).stat().st_ino, inode)
            self.assertFalse((self.area/'ready.json').exists())
        finally:
            bait.close()  # own harmless socket; do not delete the evidence


if __name__ == '__main__':
    unittest.main(verbosity=2)
