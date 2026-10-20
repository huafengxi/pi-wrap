"""Local process platform facts. Real tests touch only this test's children."""
import ctypes
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import local_process as O


class ProcessTests(unittest.TestCase):
    def test_own_start_is_stable(self):
        a, b = O.identity(os.getpid()), O.identity(os.getpid())
        self.assertEqual(a['start'], b['start'])
        self.assertEqual(a['uid'], os.getuid())
        self.assertEqual(a['sid'], os.getsid(0))
        self.assertEqual(O.starttime(os.getpid()), int(a['start']))
        self.assertTrue(O.owner_alive(os.getpid(), O.starttime(os.getpid())))
        self.assertFalse(O.owner_alive(os.getpid(), 0))
        self.assertTrue(O.owner_alive(os.getpid(), None))

    def test_linux_token_unchanged(self):
        if not sys.platform.startswith('linux'):
            self.skipTest('Linux-only /proc token oracle')
        raw = Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()
        self.assertEqual(O.identity(os.getpid())['start'], raw[19])

    def test_invalid_pids(self):
        for pid in (0, -1, True, '1'):
            with self.assertRaises(ValueError):
                O.identity(pid)

    def test_own_child_group_and_absence(self):
        p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True)
        try:
            expected = O.identity(p.pid)
            self.assertEqual(expected['pgid'], p.pid)
            self.assertEqual(expected['sid'], p.pid)
            self.assertIn(p.pid, [i['pid'] for i in O.group_members(expected)])
        finally:
            p.terminate()
            p.wait(timeout=5)
        self.assertIsNone(O.identity(p.pid))

    def test_real_socket_peer(self):
        root = tempfile.mkdtemp(prefix='op-peer-')
        server = socket.socket(socket.AF_UNIX)
        client = socket.socket(socket.AF_UNIX)
        try:
            server.bind(root + '/s'); server.listen(1)
            client.connect(root + '/s')
            accepted, _ = server.accept()
            try:
                self.assertEqual(O.peer_credentials(client)[:2], (os.getpid(), os.geteuid()))
                self.assertEqual(O.peer_credentials(accepted)[:2], (os.getpid(), os.geteuid()))
            finally:
                accepted.close()
        finally:
            client.close(); server.close()

    def test_darwin_token_and_state(self):
        b = O.BSDInfo(pid=42, ppid=7, uid=os.getuid(), pgid=42,
                      status=4, start_sec=12345, start_usec=678)
        with patch.object(O.sys, 'platform', 'darwin'), patch.object(O, '_bsd_info', return_value=b), \
                patch.object(O.os, 'getsid', return_value=42), patch.object(O, 'boot_id', return_value='boot'):
            a = O.identity(42)
        self.assertEqual(a['start'], '12345000678')
        self.assertEqual(a['state'], 'T')

    def test_darwin_read_denial_is_not_absence(self):
        with patch.object(O.sys, 'platform', 'darwin'), \
                patch.object(O, '_bsd_info', side_effect=PermissionError('denied')):
            with self.assertRaises(PermissionError):
                O.identity(42)
            with self.assertRaises(PermissionError):
                O.owner_alive(42, None)

    def test_darwin_mid_read_reuse_is_error(self):
        a = O.BSDInfo(pid=42, status=2, start_sec=123, start_usec=1)
        b = O.BSDInfo(pid=42, status=2, start_sec=123, start_usec=2)
        with patch.object(O.sys, 'platform', 'darwin'), patch.object(O, '_bsd_info', side_effect=[a, b]), \
                patch.object(O.os, 'getsid', return_value=42):
            with self.assertRaisesRegex(RuntimeError, 'changed'):
                O.identity(42)

    def test_darwin_truncated_read_fails(self):
        class Lib:
            @staticmethod
            def proc_pidinfo(*args):
                ctypes.set_errno(0)
                return 8
        with patch.object(O, '_darwin', return_value=Lib()):
            with self.assertRaises(OSError):
                O._bsd_info(42)

    def test_darwin_esrch_is_absence(self):
        class Lib:
            @staticmethod
            def proc_pidinfo(*args):
                import errno
                ctypes.set_errno(errno.ESRCH)
                return 0
        with patch.object(O, '_darwin', return_value=Lib()):
            self.assertIsNone(O._bsd_info(42))

    def test_transport_has_no_file_locks(self):
        from transport import Transport
        root = Path(tempfile.mkdtemp(prefix='p-no-locks-')).resolve()
        t = Transport()
        t.session_file = str(root / 'history.jsonl')
        t.session_name = 'standalone'
        t.sock_path = str(root / 's')
        t.pid_file = t.sock_path + '.pid'
        t.init_transport()
        try:
            t.setup_socket()
            self.assertEqual(list(root.glob('*.lock')), [])
        finally:
            t.cleanup_socket()

    def test_transport_identity_is_local(self):
        from transport import Transport
        t = Transport()
        with patch.object(O, 'starttime', return_value=111) as start, \
                patch.object(O, 'owner_alive', return_value=True) as alive:
            self.assertEqual(t.process_starttime(42), 111)
            self.assertTrue(t.process_owner_alive(42, 111))
            start.assert_called_once_with(42)
            alive.assert_called_once_with(42, 111)


if __name__ == '__main__':
    unittest.main(verbosity=2)
