"""Direct fakepi protocol smoke tests; no real executable/model/config lookup."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from fixture_support import fixture_dir

HERE = Path(__file__).resolve().parent


class FakePiTests(unittest.TestCase):
    def run_fake(self, mode, commands):
        root = Path(fixture_dir(prefix="fake-"))
        env = {"PATH": os.environ["PATH"], "HOME": str(root), "FAKE_MODE": mode,
               "FAKE_FLAG_DIR": str(root), "W_WORKSPACE": str(root),
               "AGENT_SELF": "task/fixture", "DISPATCH_PROFILE": "synthetic"}
        p = subprocess.run([sys.executable, "-B", str(HERE / "fakepi_rpc.py"), "--mode", "rpc"],
                           input="".join(json.dumps(c) + "\n" for c in commands),
                           env=env, cwd=root, capture_output=True, text=True, timeout=10)
        return p, root

    def test_prompt_message_queues_snapshots_and_eof(self):
        cmds = [{"id": str(i), "type": t, "message": "fixture"} for i, t in enumerate(("prompt", "steer", "follow_up", "get_entries", "get_state"))]
        p, root = self.run_fake("ok", cmds)
        self.assertEqual(p.returncode, 0, p.stderr)
        events = [json.loads(line) for line in p.stdout.splitlines()]
        responses = [x for x in events if x["type"] == "response"]
        self.assertEqual([x["command"] for x in responses], [x["type"] for x in cmds])
        self.assertTrue(all(x["success"] for x in responses))
        self.assertTrue(any(x["type"] == "agent_settled" for x in events))
        snap = json.loads((root / "persona_env").read_text())
        self.assertEqual(snap["AGENT_SELF"], "task/fixture")
        self.assertEqual(snap["W_WORKSPACE"], str(root))

    def test_crash_and_prompt_rejection(self):
        crash, _ = self.run_fake("crash", [])
        self.assertEqual(crash.returncode, 3)
        reject, _ = self.run_fake("reject", [{"type": "prompt", "id": "p", "message": "test"}])
        self.assertEqual(reject.returncode, 0)
        self.assertFalse(json.loads(reject.stdout.splitlines()[0])["success"])


if __name__ == "__main__":
    unittest.main()
