"""Local helper contract v1; real Linux Bash3.2, no model or cleanup harness."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

from fixture_support import fixture_dir
from platform_paths import workspace_root

HERE = Path(__file__).resolve().parent


class RootTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(fixture_dir(prefix="root-"))
        self.ws = self.base / "caller with spaces"
        self.repo = self.ws / "renamed repo"
        self.repo.mkdir(parents=True)
        self.entry = self.repo / "entry.py"
        self.entry.touch()
        self.link = self.base / "alias entry.py"
        self.link.symlink_to(self.entry)

    def test_default_real_relative_and_symlink(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(workspace_root(self.entry), str(self.ws))
            self.assertEqual(workspace_root(self.link), str(self.ws))
            self.assertEqual(workspace_root(os.path.relpath(self.link)), str(self.ws))
        with patch.dict(os.environ, {"PLATFORM_WORKSPACE": ""}, clear=True):
            self.assertEqual(workspace_root(self.link), str(self.ws))

    def test_override_expanded_absolute_independent_of_source(self):
        with patch.dict(os.environ, {"HOME": str(self.base), "PLATFORM_WORKSPACE": "~/other root"}, clear=True):
            self.assertEqual(workspace_root(self.link), str(self.base / "other root"))
        with patch.dict(os.environ, {"PLATFORM_WORKSPACE": "relative override"}, clear=True):
            self.assertEqual(workspace_root(self.entry), str(Path("relative override").resolve()))

    def test_cli_from_other_cwd(self):
        env = dict(os.environ, PLATFORM_WORKSPACE="")
        out = subprocess.check_output([sys.executable, str(HERE / "platform_paths.py"), str(self.link)], cwd=self.base, env=env, text=True)
        self.assertEqual(out.strip(), str(self.ws))

    def test_bash32_real_relative_symlink_and_nested_entry(self):
        bash = os.environ.get("BASH32")
        self.assertTrue(bash, "Set BASH32 to an existing Linux Bash3.2 binary; no download")
        for p in ("bots/profiles", "bots/caps"):
            (self.ws / p).mkdir(parents=True)
        shutil.copy(HERE / "platform_paths.py", self.repo)
        (self.repo / "pi-rpc-wrap.py").touch()
        harness = self.repo / "test/toolface-harness"
        shutil.copytree(HERE / "test/toolface-harness", harness)
        # Only the real clean script's root resolution prefix is executable here.
        # No delete command, no production roots, no refusal experiment.
        prefix = (harness / "clean.sh").read_text().split('rr=$(realpath "$R")', 1)[0]
        clean = harness / "clean-prefix.sh"
        clean.write_text(prefix + '\nprintf "ROOT=%s\\n" "$WS"\n')
        link = self.base / "shell link with spaces"
        link.symlink_to(harness / "setup.sh")
        clean_link = self.base / "clean link"
        clean_link.symlink_to(clean)
        version_log = self.base / "versions.log"
        guard = self.base / "bash-env"
        guard.write_text('case "$BASH_VERSION" in 3.2.*) ;; *) exit 93;; esac\nprintf "%s\\n" "$BASH_VERSION" >> "$VERSION_LOG"\n')
        cases = []
        for kind, entry in (("real", harness / "setup.sh"), ("relative", Path(os.path.relpath(harness / "setup.sh", self.base))), ("symlink", link)):
            for override in (None, "", str(self.ws), "~/caller with spaces", os.path.relpath(self.ws, self.base)):
                for module_override in (False, True):
                    env = {"PATH": os.environ["PATH"], "HOME": str(self.base), "PYTHON": sys.executable,
                           "BASH_ENV": str(guard), "VERSION_LOG": str(version_log),
                           "HARNESS_BASE": str(self.base / ("outputs-%d" % len(cases))),
                           "HARNESS_ROOT": str(self.base / "unused root")}
                    if override is not None:
                        env["PLATFORM_WORKSPACE"] = override
                    if module_override:
                        env["PLATFORM_WORKSPACE"] = str(self.base / "not a workspace")
                        env["HARNESS_WS"] = str(self.ws)
                    command = [bash, "-c", 'exec "$BASH" "$@"', "nested", str(entry)]
                    run = subprocess.run(command, env=env, cwd=self.base, text=True, capture_output=True)
                    self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                    chosen_clean = clean_link if kind == "symlink" else clean
                    check = subprocess.run([bash, "-c", 'exec "$BASH" "$@"', "nested", str(chosen_clean)], env=env, cwd=self.base, text=True, capture_output=True)
                    self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
                    self.assertIn("ROOT=" + (str(self.ws) if module_override else workspace_expected(override, self.ws, self.base)), check.stdout)
                    cases.append({"entry": kind, "override": override, "module_override": module_override, "setup": run.returncode, "clean_prefix": check.returncode})
        versions = version_log.read_text().splitlines()
        self.assertEqual(len(versions), 4 * len(cases))
        self.assertTrue(all(v.startswith("3.2.") for v in versions))
        for shell in (harness / "setup.sh", harness / "clean.sh"):
            subprocess.run([bash, "-n", str(shell)], check=True)
        print(json.dumps({"bash32_cases": cases, "actual_shell_executions": len(versions), "fixture": str(self.base)}, indent=2))


def workspace_expected(value, ws, cwd):
    if not value:
        return str(ws)
    if value.startswith("~/"):
        return str(cwd / value[2:])
    return str((cwd / value).resolve())


if __name__ == "__main__":
    unittest.main()
