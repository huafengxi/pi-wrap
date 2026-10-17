#!/home/yuanqi.xhf/miniconda3/bin/python
"""Reviewer-authored wire double, no platform/old-P imports and no model."""
import json, sys, os
from pathlib import Path

def emit(**obj): print(json.dumps(obj),flush=True)
def dialog(name,tool='t',method='input',timeout=20000):
 emit(type='tool_execution_start',toolCallId=tool,toolName='ask_user')
 emit(type='extension_ui_request',id=name,method=method,timeout=timeout,title='review-'+name)
for line in sys.stdin:
 c=json.loads(line);t=c['type'];rid=c.get('id')
 if t=='prompt':
  emit(type='response',command=t,id=rid,success=True)
  emit(type='agent_start')
  emit(type='tool_execution_update',toolCallId='stale-ask',toolName='ask_dispatcher',partialResult={'details':{'agentdWait':{'askId':'forged','question':'must not revive'}}})
  dialog('live')
  for i in range(260):emit(type='review_tick',index=i)
  (Path(os.environ['AGENT_HOME'])/'review-emitted').write_text('ready')
 elif t=='review_control':
  action=c['action']
  if action=='false_settle':emit(type='agent_settled')
  elif action=='end_tool':emit(type='tool_execution_end',toolCallId='t')
  elif action=='end_turn':emit(type='agent_end');emit(type='agent_settled')
  elif action=='begin':
   emit(type='agent_start');dialog(c['dialog'],method=c.get('method','confirm'),timeout=c.get('timeout',20000))
  elif action=='idle_dialog':dialog(c['dialog'])
  elif action=='settled':emit(type='agent_settled')
  elif action=='shutdown':emit(type='response',id=rid,command=t,success=True);break
  emit(type='response',command=t,id=rid,success=True)
 elif t in ('extension_ui_response','abort'):
  # Deliberately no event for client response/abort: wrapper's sent() boundary
  # must remove a response but must not guess abort resolved a native dialog.
  pass
 else:emit(type='response',command=t,id=rid,success=True)
