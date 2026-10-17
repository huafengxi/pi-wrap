#!/usr/bin/env python3
"""F-P isolated fixed-A runner. Never deploys or discovers a production root.

Run this from a clean fixed P export after push. All exports/fixtures are new;
A and caller are read with git archive/show at full metadata pins, never main.
No cleanup of the output root, no installs, no model, no inherited agent identity.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

REPO = Path(__file__).resolve().parents[1]
META = json.loads((REPO/'recovery/fresh-dependencies.json').read_text())
p = argparse.ArgumentParser()
p.add_argument('--agentd-bare', required=True)
p.add_argument('--caller-repo', required=True)
p.add_argument('--bash32', required=True)
p.add_argument('--output', required=True, help='new short absolute root, e.g. /tmp/fp-unique')
p.add_argument('--native-rpc-source', help='optional read-only installed native helper; mock driver only')
p.add_argument('--marked-js', help='optional actual renderer input; absence is a disclosed skip')
a = p.parse_args()
output = Path(a.output).resolve()
output.mkdir(parents=True, exist_ok=False)
(output/'home').mkdir()
(output/'tmp').mkdir()
caller = output/'caller inputs'
caller.mkdir()
records = []

def digest(data):
    return hashlib.sha256(data).hexdigest()

def git(*args):
    return subprocess.check_output(['git', *map(str, args)])

# Tar input is from the explicit trusted bare commit; only regular files/dirs
# and relative in-tree symlinks from that fixed Git tree are extracted.
def export_a(destination, revision=None):
    archive = git('--git-dir='+str(Path(a.agentd_bare).resolve()), 'archive', revision or META['agentd']['revision'])
    destination.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(destination, filter='data')
    if revision is None:
        for name, expected in META['agentd']['module_sha256'].items():
            assert digest((destination/name).read_bytes()) == expected, name

caller_hashes = {}
for name in META['caller_inputs']['files']:
    data = git('-C', Path(a.caller_repo).resolve(), 'show', META['caller_inputs']['revision']+':'+name)
    target = caller/name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    target.chmod(0o444)
    caller_hashes[name] = digest(data)
# Validate the source relation, not a branch's current value.
subprocess.run(['git','-C',a.caller_repo,'merge-base','--is-ancestor',
    META['caller_inputs']['producer_deletion'], META['caller_inputs']['revision']],check=True)

bash = output/'tools/bash32'
bash.parent.mkdir()
shutil.copyfile(Path(a.bash32).resolve(), bash)
bash.chmod(0o700)
version = subprocess.check_output([str(bash),'--version'],text=True).splitlines()[0]
assert 'version 3.2.' in version, version
optional = {}
for flag, name in ((a.native_rpc_source,'native-rpc.js'), (a.marked_js,'marked.js')):
    if flag:
        source = Path(flag).resolve()
        dest = output/'tools'/name
        dest.write_bytes(source.read_bytes())
        dest.chmod(0o444)
        optional[name] = dict(source=str(source), export=str(dest), sha256=digest(dest.read_bytes()))
node = shutil.which('node')
assert node, 'node is required for actual TS producer tests; never silently skip it'
baseenv = dict(HOME=str(output/'home'), TMPDIR=str(output/'tmp'),
    PATH=os.pathsep.join([str(Path(sys.executable).parent), str(Path(node).parent), '/usr/bin','/bin']),
    LANG='C.UTF-8', PYTHONDONTWRITEBYTECODE='1', BASH32=str(bash), PLATFORM_WORKSPACE=str(caller))
# These are explicitly supplied caller test fixtures, not runtime import paths.
if 'native-rpc.js' in optional: baseenv['NATIVE_RPC_SOURCE'] = optional['native-rpc.js']['export']
if 'marked.js' in optional: baseenv['TASK_MARKED_JS'] = optional['marked.js']['export']
tests = [f.relative_to(REPO) for f in sorted(REPO.rglob('test_*.py')) if '.git' not in f.parts]


def run(label, command, env, cwd, expected_error=None):
    logfile = output/(label+'.log')
    with logfile.open('w') as log:
        log.write(json.dumps(dict(command=command, env=env, cwd=str(cwd)))+'\n')
        log.flush()
        try:
            rc = subprocess.run(command,env=env,cwd=cwd,stdout=log,stderr=subprocess.STDOUT,timeout=420).returncode
        except subprocess.TimeoutExpired:
            rc = 124
    text = logfile.read_text(errors='replace')
    accepted = (rc == 0) if expected_error is None else (rc == 1 and expected_error in text)
    record = dict(label=label, command=command, env=env, cwd=str(cwd), returncode=rc, log=str(logfile),
                  expected_error=expected_error, accepted=accepted,
                  skip_lines=[s for s in text.splitlines()[1:] if 'skip' in s.lower() or '→ 跳过' in s])
    records.append(record)
    (output/'results.json').write_text(json.dumps(dict(metadata=META, bash32=version,
        bash_sha256=digest(bash.read_bytes()), caller_sha256=caller_hashes, optional=optional,
        test_enumeration=list(map(str,tests)), results=records),indent=2)+'\n')
    print(label, 'rc='+str(rc), 'accepted='+str(accepted), flush=True)

# This is the actual prior reviewed A, not only a synthetic missing-class stub.
old = output/'actual old A'
export_a(old, META['agentd']['previous_revision'])
run('negative-actual-old-A', [sys.executable,'-B',str(REPO/'pi-rpc-wrap.py')],
    dict(baseenv, AGENTD_DIR=str(old)), output,
    expected_error='requires agentd wait_state.DialogRequests')

for layout in ('sibling','explicit'):
    parent = output/(layout+' layout with spaces')
    checkout = parent/'pi-wrap' if layout=='sibling' else parent/'arbitrary'/'nested wrap'
    shutil.copytree(REPO,checkout,ignore=shutil.ignore_patterns('.git','__pycache__'))
    dep = parent/'agentd' if layout=='sibling' else output/'non sibling A with spaces'
    export_a(dep)  # True directory sibling, no symlink or AGENTD_DIR workaround.
    cwd = output/(layout+' different cwd')
    cwd.mkdir()
    env = dict(baseenv, TEST_LAYOUT=layout)
    if layout=='explicit': env['AGENTD_DIR'] = str(dep)
    for test in tests:
        run(layout+'-'+test.stem, [sys.executable,'-B',str(checkout/test)], env, cwd)
    # Supplemental A tests exercise THIS exported P, not the old platform wrapper.
    extraenv = dict(env, AGENTD_TEST_WRAP=str(checkout/'pi-rpc-wrap.py'),
        ASK_WAIT_DRIVER=str(caller/'pi-core/agent/extensions/agentd/tests/ask-wait.test.mjs'))
    for name in ('test_current_tools.py','test_report_slots.py','test_web_report.py','test_wait_state.py'):
        run(layout+'-A-'+Path(name).stem,
            [sys.executable,'-B',str(dep/'recovery/isolated-test.py'),str(dep/name)], extraenv, cwd)

sys.exit(0 if all(r['accepted'] for r in records) else 1)
