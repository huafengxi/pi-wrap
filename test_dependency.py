"""Pinned imports/report predicate and actionable failure of the new wait interface.

A rejection is a passing negative test, NOT a passing wrapper integration test.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

from fixture_support import fixture_dir

HERE = Path(__file__).resolve().parent
PIN = "9625bdc15f853e49a6312d20dc429e23330e3d89"


class DependencyTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(fixture_dir(prefix="dep-"))
        self.home = self.base / "empty home"
        self.home.mkdir()
        self.source = Path(os.environ["AGENTD_DIR"]).resolve()
        self.assertEqual(subprocess.check_output(["git", "-C", str(self.source), "rev-parse", "HEAD"], text=True).strip(), PIN)

    def run_python(self, entry, code=None, explicit=None, cwd=None):
        env = {"PATH": os.environ["PATH"], "HOME": str(self.home), "PYTHONDONTWRITEBYTECODE": "1",
               "AGENTD_WRAP_PI_BIN": str(HERE / "fakepi_rpc.py")}
        if explicit is not None:
            env["AGENTD_DIR"] = str(explicit)
        command = [sys.executable, "-B", str(entry)] if code is None else [sys.executable, "-B", "-c", code, str(entry)]
        return subprocess.run(command, env=env, cwd=cwd or self.base, text=True, capture_output=True)

    def test_layouts_real_symlink_spaces_imports_and_report_singlepoint(self):
        for sibling in (True, False):
            parent = self.base / ("sibling with spaces" if sibling else "arbitrary layout")
            repo = parent / "wrap checkout"
            repo.mkdir(parents=True)
            for name in ("agentd_dependency.py", "pi-rpc-wrap.py", "persona.py"):
                shutil.copy(HERE / name, repo)
            dep = parent / "agentd" if sibling else self.base / "elsewhere" / "pinned dependency"
            shutil.copytree(self.source, dep, ignore=shutil.ignore_patterns(".git", "__pycache__"))
            link = self.base / ("alias sibling.py" if sibling else "alias explicit.py")
            link.symlink_to(repo / "pi-rpc-wrap.py")
            code = '''import json, os, pathlib, sys
sys.path.insert(0, str(pathlib.Path(sys.argv[1]).resolve().parent))
from agentd_dependency import require_agentd
p = require_agentd("proto", "task_state", "envscrub")
import proto, task_state, envscrub
assert pathlib.Path(task_state.__file__).resolve().parent == pathlib.Path(p)
r=pathlib.Path("report.md")
assert not task_state.report_nonempty(r)
r.write_bytes(b" \\n\\t")
assert not task_state.report_nonempty(r)
r.write_bytes(b" " * 9000 + b"delivered")
assert task_state.report_nonempty(r)
r.write_bytes(b"")
assert not task_state.report_nonempty(r)
assert not task_state.report_nonempty(pathlib.Path("."))
x=envscrub.scrub_env(base={"AGENT_SELF":"task/stale", "AGENT_ROOT":"stale", "KEEP":"ok"})
assert "AGENT_SELF" not in x and "AGENT_ROOT" not in x and x["KEEP"]=="ok"
print(json.dumps({"proto":proto.__file__, "report_nonempty":task_state.__file__, "envscrub":envscrub.__file__}))
'''
            for entry in (repo / "pi-rpc-wrap.py", link):
                explicit = None if sibling else dep
                result = self.run_python(entry, code, explicit)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                # Full source retained, pin unchanged: startup must reject wait_state.
                reject = self.run_python(entry, explicit=explicit)
                self.assertNotEqual(reject.returncode, 0)
                self.assertIn("wait_state.py", reject.stderr)
                self.assertIn("Set AGENTD_DIR", reject.stderr)
                self.assertNotIn("fake-pi boot", reject.stderr)
                print(json.dumps({"layout": "sibling" if sibling else "explicit non-sibling", "entry": str(entry), "imports": result.stdout.strip(), "wrapper": "BLOCKED: wait_state missing at fixed pin"}))
            # Explicit invalid configuration wins even when a valid sibling exists.
            bad = self.run_python(repo / "pi-rpc-wrap.py", explicit=self.base / "missing dependency")
            self.assertNotEqual(bad.returncode, 0)
            self.assertIn("explicit AGENTD_DIR", bad.stderr)
            self.assertIn("proto.py", bad.stderr)

    def test_nonsibling_missing_dependency_does_not_search_unified_repo(self):
        repo = self.base / "detached with spaces"
        repo.mkdir()
        for name in ("pi-rpc-wrap.py", "agentd_dependency.py"):
            shutil.copy(HERE / name, repo)
        result = self.run_python(repo / "pi-rpc-wrap.py")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("development sibling", result.stderr)
        self.assertIn("No unified-repo fallback", result.stderr)
        # Existing but incomplete explicit directory is not repaired from elsewhere.
        bad = self.base / "partial dependency"
        bad.mkdir()
        shutil.copy(self.source / "proto.py", bad)
        result = self.run_python(repo / "pi-rpc-wrap.py", explicit=bad)
        self.assertIn("task_state.py", result.stderr)
        self.assertIn("wait_state.py", result.stderr)


if __name__ == "__main__":
    unittest.main()
