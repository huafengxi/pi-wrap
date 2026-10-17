"""P-specific removal/replay oracles; no persistence is a communication contract."""
import builtins
import importlib.util
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fixture_support import fixture_dir
HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('removal_wrapper', HERE/'pi-rpc-wrap.py')
wrapper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wrapper)  # No protocol/runtime module preloading to repair imports.


class RemovalTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(fixture_dir('removal-'))
        self.home = self.root/'agents/task/removal'
        self.home.mkdir(parents=True)
        self.env = dict(AGENT_HOME=str(self.home), AGENT_ROOT=str(self.root), AGENT_SELF='task/removal')
        self.legacy = self.home/'wait-state.json'

    def make(self):
        with patch.dict(os.environ, self.env):
            return wrapper.Wrap()

    def event(self, w, **obj):
        w._on_event(obj)
        w.ring.append((json.dumps(obj)+'\n').encode())

    def replay(self, w):
        return [json.loads(b) for b in w.replay_lines() if json.loads(b).get('type')=='extension_ui_request']

    def test_absent_forged_stale_invalid_legacy_is_never_opened_or_replayed(self):
        for payload in (None, b'{invalid', b'{"waits":[{"kind":"dialog","request":{"id":"forged","method":"input"}}]}',
                        b'{"gen":0,"waits":[{"kind":"ask","askId":"stale"}]}'):
            with self.subTest(payload=payload):
                if payload is not None: self.legacy.write_bytes(payload)
                original_open, original_path_open = builtins.open, Path.open
                def guarded_open(path, *args, **kwargs):
                    if not isinstance(path, int) and Path(path).resolve() == self.legacy:
                        raise AssertionError('wrapper touched legacy JSON')
                    return original_open(path, *args, **kwargs)
                def guarded_path_open(path, *args, **kwargs):
                    if path.resolve() == self.legacy: raise AssertionError('wrapper touched legacy JSON')
                    return original_path_open(path, *args, **kwargs)
                with patch('builtins.open', guarded_open), patch.object(Path, 'open', guarded_path_open):
                    w = self.make()
                    self.event(w, type='tool_execution_update', toolCallId='old', toolName='ask_dispatcher',
                               partialResult={'details':{'agentdWait':{'askId':'old','question':'ignored'}}})
                    self.event(w, type='agent_settled')
                    self.assertEqual(self.replay(w), [])
                    self.assertEqual(w.dialog_requests.waits, {})
                if payload is None: self.assertFalse(self.legacy.exists())
                else: self.assertEqual(self.legacy.read_bytes(), payload)

    def test_memory_replay_after_ring_eviction_and_no_resurrection_after_settle(self):
        w = self.make()
        self.event(w, type='agent_start')
        self.event(w, type='tool_execution_start', toolCallId='t')
        self.event(w, type='extension_ui_request', id='live', method='input', timeout=30000)
        for i in range(wrapper.RING_MAX+20): self.event(w, type='tick', index=i)
        first = self.replay(w)
        self.assertEqual([o['id'] for o in first], ['live'])
        self.assertGreater(first[0]['timeout'], 0)
        self.assertLess(first[0]['timeout'], 30000)
        self.assertEqual([o['id'] for o in self.replay(w)], ['live'])
        self.event(w, type='agent_settled')  # Not settled while tool/turn still live.
        self.assertEqual([o['id'] for o in self.replay(w)], ['live'])
        self.event(w, type='tool_execution_end', toolCallId='t')
        self.event(w, type='agent_end')
        self.event(w, type='agent_settled')
        self.assertEqual(self.replay(w), [])
        fresh = self.make()  # A new wrapper must not recover old persistent waits.
        self.assertEqual(self.replay(fresh), [])
        self.assertFalse(self.legacy.exists())

    def test_answer_cancel_and_failed_send_preserve_successful_write_boundary(self):
        for cancelled in (False, True):
            w = self.make()
            self.event(w, type='extension_ui_request', id='d', method='confirm')
            response = json.dumps(dict(type='extension_ui_response', id='d', cancelled=cancelled)).encode()
            self.assertFalse(w.pi_send(response))
            self.assertEqual([o['id'] for o in self.replay(w)], ['d'])
            w.pi = SimpleNamespace(stdin=io.BytesIO())
            self.assertTrue(w.pi_send(response))
            self.assertEqual(self.replay(w), [])
            self.assertTrue(w.pi.stdin.getvalue().endswith(b'\n'))
        self.assertFalse(self.legacy.exists())

    def test_deadline_and_mock_native_signal_boundary_are_not_wait_proof(self):
        w = self.make()
        with patch('wait_state.time.time', return_value=100):
            self.event(w, type='extension_ui_request', id='deadline', method='editor', timeout=1000)
        with patch('wait_state.time.time', return_value=102):
            self.assertEqual(self.replay(w), [])
        self.event(w, type='extension_ui_request', id='native', method='input')
        w.pi = SimpleNamespace(stdin=io.BytesIO())
        self.assertTrue(w.pi_send(b'{"type":"abort"}'))
        # Unmodified native internal Promise settlement emits no RPC event.
        # An abort is NOT evidence of settlement; disclose this baseline limit.
        self.assertEqual([o['id'] for o in self.replay(w)], ['native'])
        self.event(w, type='agent_end')
        self.assertEqual(self.replay(w), [])
        self.assertFalse(self.legacy.exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
