#!/usr/bin/env python3
"""All repository test_*.py in TWO real isolated layouts, no live/model harness.

Caller inputs are explicit read-only static test dependencies (T43/T45), not a
runtime fallback. No fetching, installing or root cleanup; retain output roots.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

REPO = Path(__file__).resolve().parents[1]
p = argparse.ArgumentParser()
p.add_argument('--agentd', required=True)
p.add_argument('--bash32', required=True)
p.add_argument('--caller-inputs', required=True, help='synthetic root with the two frozen static caller inputs')
p.add_argument('--output', required=True)
a = p.parse_args()
dep = Path(a.agentd).expanduser().resolve()
bash = Path(a.bash32).expanduser().resolve()
caller = Path(a.caller_inputs).expanduser().resolve()
output = Path(a.output).expanduser().resolve()
metadata = json.loads((REPO/'recovery/continuation-dependencies.json').read_text())
pin = metadata['agentd']['revision']
actual = subprocess.check_output(['git','-C',str(dep),'rev-parse','HEAD'],text=True).strip()
if actual != pin:
    raise SystemExit('agentd revision mismatch: '+actual)
if subprocess.check_output(['git','-C',str(dep),'status','--porcelain'],text=True).strip():
    raise SystemExit('agentd dependency must be clean')
version = subprocess.check_output([str(bash),'--version'],text=True).splitlines()[0]
if 'version 3.2.' not in version:
    raise SystemExit('BASH32 is not Bash3.2: '+version)
inputs={name:hashlib.sha256((caller/name).read_bytes()).hexdigest()
        for name in metadata['caller_inputs']['files']}
output.mkdir(parents=True, exist_ok=False)
(output/'home').mkdir()
(output/'tmp').mkdir()
results=[]
tests=[f.relative_to(REPO) for f in sorted(REPO.rglob('test_*.py')) if '.git' not in f.parts]
for layout in ('sibling','explicit'):
    parent=output/(layout+' layout with spaces')
    checkout=parent/'wrap checkout' if layout=='sibling' else parent/'arbitrary'/'nested wrap'
    shutil.copytree(REPO,checkout,ignore=shutil.ignore_patterns('.git','__pycache__'))
    if layout=='sibling':
        (parent/'agentd').symlink_to(dep,target_is_directory=True)
    cwd=output/(layout+' different cwd')
    cwd.mkdir()
    env={'HOME':str(output/'home'),'TMPDIR':str(output/'tmp'),
         'PATH':str(Path(sys.executable).parent)+':/usr/bin:/bin','LANG':'C.UTF-8',
         'PYTHONDONTWRITEBYTECODE':'1','BASH32':str(bash),
         'PLATFORM_WORKSPACE':str(caller),'TEST_LAYOUT':layout}
    if layout=='explicit': env['AGENTD_DIR']=str(dep)
    # test_dependency.py needs a pin source, but the wrapper integration test in
    # the sibling layout deliberately starts its child with AGENTD_DIR absent.
    for i,test in enumerate(tests):
        log=output/('%s-%02d-%s.log'%(layout,i,test.name))
        command=[sys.executable,'-B',str(checkout/test)]
        testenv=dict(env)
        if test.name=='test_dependency.py': testenv['AGENTD_DIR']=str(dep)
        with log.open('w') as f:
            try:
                rc=subprocess.run(command,env=testenv,cwd=cwd,stdout=f,stderr=subprocess.STDOUT,timeout=360).returncode
            except subprocess.TimeoutExpired:
                rc=124
        text=log.read_text()
        skips=[line for line in text.splitlines() if 'skip' in line.lower() or '→ 跳过' in line]
        results.append(dict(layout=layout,test=str(test),command=command,returncode=rc,log=str(log),skip_lines=skips))
        print(layout,test,'rc='+str(rc),flush=True)
summary=dict(agentd_revision=actual,bash32=version,caller_input_sha256=inputs,
    test_enumeration=[str(t) for t in tests],tests=results,
    excluded=['live toolface/model driver','clean.sh deletion body','production/macOS/multihost integration'],
    status='PASS' if all(r['returncode']==0 for r in results) else 'BLOCKED/FAIL (inspect logs)')
(output/'results.json').write_text(json.dumps(summary,indent=2)+'\n')
print(json.dumps(summary,indent=2))
sys.exit(0 if summary['status']=='PASS' else 1)
