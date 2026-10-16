#!/usr/bin/env python3
"""Read-only provenance validation against an independent platform source clone.

Usage: python -B recovery/verify.py --source /path/to/source [--target /path/to/origin]
No fetch, checkout, index update, production harness or model invocation.
"""
import argparse
import json
from pathlib import Path
import subprocess

REPO = Path(__file__).resolve().parents[1]
p = argparse.ArgumentParser()
p.add_argument("--source", required=True)
p.add_argument("--target", default=str(REPO))
a = p.parse_args()
S, T = Path(a.source), Path(a.target)

def g(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args]).decode().strip()

def tree(repo, sha, owned=False):
    result = {}
    for row in g(repo, "ls-tree", "-r", sha).splitlines():
        meta, path = row.split("\t")
        mode, kind, oid = meta.split()
        if owned:
            if path.startswith("pi-wrap/"):
                dest = path[len("pi-wrap/"):]
            elif path in ("platform_paths.py", "test_platform_paths.py"):
                dest = path
            else:
                continue
            result[dest] = {"source": path, "mode": mode, "oid": oid}
        elif not path.startswith("recovery/"):
            result[path] = {"mode": mode, "oid": oid}
    return result

def plain(info):
    return {k: info[k] for k in ("mode", "oid")} if info else None

mapping = json.loads((REPO / "recovery/source-map.json").read_text())
refs = json.loads((REPO / "recovery/source-refs.json").read_text())
net = json.loads((REPO / "recovery/net-paths.json").read_text())
source_steps = g(S, "rev-list", "--first-parent", "--reverse", mapping["source_baseline"] + ".." + mapping["source_frozen"]).splitlines()
assert source_steps == [r["source_commit"] for r in mapping["commits"]]
base = tree(S, mapping["source_baseline"], True)
original = tree(T, mapping["original_main"])
assert {p: plain(v) for p, v in base.items() if v["source"].startswith("pi-wrap/")} == original
expected = dict(original)
parent = mapping["original_main"]
projected = 0
for r in mapping["commits"]:
    sha = r["source_commit"]
    before, after = tree(S, sha + "^", True), tree(S, sha, True)
    changes = [{"source_path": (after.get(path) or before[path])["source"], "target_path": path,
                "before": before.get(path), "after": after.get(path), "deleted": path not in after}
               for path in sorted(before.keys() | after.keys()) if before.get(path) != after.get(path)]
    assert r["changes"] == changes, sha
    assert r["target_parent"] == parent, sha
    for c in changes:
        if c["after"]:
            expected[c["target_path"]] = plain(c["after"])
        else:
            expected.pop(c["target_path"], None)
    if changes:
        commit = r["target_commit"]
        assert g(T, "rev-parse", commit + "^") == parent
        assert tree(T, commit) == expected, sha
        message = g(T, "show", "-s", "--format=%B", commit)
        for token in ("Source-Repo: agent-platform.git", "Source-Commit: " + sha, mapping["taskId"], "Source-Paths:", "Target-Paths:", "Changes:"):
            assert token in message, (sha, token)
        parent = commit
        projected += 1
    else:
        assert r["target_commit"] is None
assert parent == mapping["projection_tip"]
final = tree(T, "HEAD")
assert sorted(final.keys() | expected.keys()) == sorted(row["path"] for row in net["paths"])
for row in net["paths"]:
    path = row["path"]
    assert row["original"] == original.get(path)
    assert row["projected"] == expected.get(path)
    assert row["delivered"] == final.get(path)
    if row["projected"] != row["delivered"]:
        assert row["adaptation"]
subprocess.run(["git", "-C", str(T), "merge-base", "--is-ancestor", mapping["original_main"], "HEAD"], check=True)
closure = set()
for r in refs["refs"]:
    if r["source_ref"] == "refs/heads/main":
        subprocess.run(["git", "-C", str(T), "merge-base", "--is-ancestor", r["oid"], r["target_ref"]], check=True)
    else:
        assert g(T, "rev-parse", r["target_ref"]) == r["oid"]
    actual = g(T, "rev-list", "--objects", r["oid"]).splitlines()
    assert sorted(actual) == r["objects"]
    target_objects = {line.split()[0] for line in g(T, "rev-list", "--objects", r["target_ref"]).splitlines()}
    ids = {line.split()[0] for line in actual}
    assert ids <= target_objects
    closure |= ids
for oid in sorted(closure):
    subprocess.run(["git", "-C", str(T), "cat-file", "-e", oid], check=True)
subprocess.run(["git", "-C", str(T), "fsck", "--full", "--strict"], check=True)
print(json.dumps({"status": "provenance PASS (runtime remains BLOCKED)", "source_frozen": mapping["source_frozen"],
                  "target_head": g(T, "rev-parse", "HEAD"), "source_steps": len(source_steps), "projected": projected,
                  "no_op": len(source_steps) - projected, "non_recovery_paths": len(final),
                  "original_ref_mappings": len(refs["refs"]), "original_closure_objects": len(closure)}, indent=2))
