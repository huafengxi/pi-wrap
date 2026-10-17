#!/usr/bin/env python3
"""Test-only IO tripwire, run unmodified fixed candidate via its actual file."""
import os,sys,runpy
legacy=os.path.realpath(os.path.join(os.environ['AGENT_HOME'],'wait-state.json'))
def audit(event,args):
 if event=='open' and isinstance(args[0],(str,bytes)):
  if os.path.realpath(os.fsdecode(args[0]))==legacy:
   print('FORBIDDEN legacy JSON access:',args, file=sys.stderr,flush=True)
   raise AssertionError('legacy JSON must never be opened by P or imported A')
sys.addaudithook(audit)
print('review IO tripwire active:',legacy,file=sys.stderr,flush=True)
runpy.run_path(sys.argv[1],run_name='__main__')
