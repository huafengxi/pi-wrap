#!/usr/bin/env python3
"""Enumerate ALL repository test_*.py. No live toolface/model harness.

Requires an independently cloned agentd at the exact metadata pin, plus an
existing Bash3.2 binary. Does not fetch/download/build/install/clean anything.
A nonzero suite result is retained, never translated to integration PASS.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

REPO = Path(__file__).resolve().parents[1]
p = argparse.ArgumentParser()
p.add_argument("--agentd", required=True)
p.add_argument("--bash32", required=True)
p.add_argument("--output", required=True)
a = p.parse_args()
dep = Path(a.agentd).expanduser().resolve()
bash = Path(a.bash32).expanduser().resolve()
output = Path(a.output).expanduser().resolve()
output.mkdir(parents=True, exist_ok=False)
(output / "home").mkdir()
(output / "tmp").mkdir()
pin = json.loads((REPO / "recovery/dependencies.json").read_text())["agentd"]["revision"]
actual = subprocess.check_output(["git", "-C", str(dep), "rev-parse", "HEAD"], text=True).strip()
if actual != pin:
    raise SystemExit("agentd revision mismatch: " + actual)
if subprocess.check_output(["git", "-C", str(dep), "status", "--porcelain"], text=True).strip():
    raise SystemExit("agentd dependency must be clean")
version = subprocess.check_output([str(bash), "--version"], text=True).splitlines()[0]
if "version 3.2." not in version:
    raise SystemExit("BASH32 is not Bash3.2: " + version)
env = {"HOME": str(output / "home"), "TMPDIR": str(output / "tmp"),
       "PATH": str(Path(sys.executable).parent) + ":/usr/bin:/bin",
       "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
       "AGENTD_DIR": str(dep), "BASH32": str(bash)}
results = []
for i, test in enumerate(sorted(REPO.rglob("test_*.py"))):
    if ".git" in test.parts:
        continue
    log = output / ("%02d-%s.log" % (i, test.name))
    command = [sys.executable, "-B", str(test)]
    with log.open("w") as f:
        try:
            rc = subprocess.run(command, env=env, cwd=output, stdout=f, stderr=subprocess.STDOUT, timeout=300).returncode
        except subprocess.TimeoutExpired:
            rc = 124
    results.append({"test": str(test.relative_to(REPO)), "command": command, "returncode": rc, "log": str(log)})
    print(test.name, "rc=" + str(rc), flush=True)
summary = {"agentd_revision": actual, "bash32": version, "tests": results,
           "excluded": ["test/toolface-harness/drive.py (real-model harness, not a test_*.py; never run)", "clean.sh destructive body (not executed; root prefix + Bash3.2 syntax tested)"],
           "status": "PASS" if all(x["returncode"] == 0 for x in results) else "BLOCKED/FAIL (inspect logs)"}
(output / "results.json").write_text(json.dumps(summary, indent=2) + "\n")
print(json.dumps(summary, indent=2))
sys.exit(0 if summary["status"] == "PASS" else 1)
