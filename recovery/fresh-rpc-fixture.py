#!/usr/bin/env python3
"""No-model RPC fixture for dependency continuation, not a runtime component.

Unlike fakepi_rpc.py (used by the full lifecycle suite), this fixture emits
dialog/file-request events and waits for real socket/file responses.
"""
import json
import os
from pathlib import Path
import sys
import threading
import time
sys.path.insert(0, os.environ['FIXTURE_AGENTD'])
import proto
lock = threading.Lock()
def emit(obj):
    with lock:
        print(json.dumps(obj), flush=True)
def finish(call):
    emit(dict(type='tool_execution_end', toolCallId=call))
    emit(dict(type='agent_end'))
    emit(dict(type='agent_settled'))
def ask():
    root, who = os.environ['AGENT_ROOT'], os.environ['AGENT_SELF']
    msg = proto.write_message(str(Path(root) / 'agents/queue/dispatcher/inbox'),
        dict(type='ask', **{'from': who}, body='Which fixture option?'))
    ref = msg[0]  # proto.write_message returns (actual generated id, path).
    emit(dict(type='tool_execution_start', toolCallId='file-tool', toolName='ask_dispatcher'))
    end = time.monotonic()+30
    while time.monotonic()<end:
        replies = proto.list_messages(str(Path(os.environ['AGENT_HOME'])/'inbox'))
        if any(m.get('type')=='reply' and m.get('ref')==ref and m.get('body')=='fixture answer' for m in replies):
            finish('file-tool')
            return
        time.sleep(.02)
    os._exit(3)
for line in sys.stdin:
    cmd=json.loads(line)
    t=cmd.get('type')
    if t=='prompt':
        emit(dict(type='response', command=t, id=cmd['id'], success=True))
        emit(dict(type='agent_start'))
        if os.environ['WAIT_MODE']=='ask':
            threading.Thread(target=ask, daemon=True).start()
        else:
            emit(dict(type='tool_execution_start', toolCallId='dialog-tool', toolName='ask_user'))
            emit(dict(type='extension_ui_request', method=os.environ['WAIT_MODE'],
                id='live-dialog', title='Fixture choice', options=['yes','no'], timeout=30000))
            # Overflow the real wrapper ring before the first observer attaches.
            for i in range(250): emit(dict(type='fixture_tick', index=i))
            emit(dict(type='extension_ui_request', method='notify', id='notice', message='not a wait'))
            (Path(os.environ['AGENT_HOME'])/'dialog-emitted').write_text('ready')
    elif t=='extension_ui_response':
        if cmd.get('id')=='live-dialog': finish('dialog-tool')
    else:
        emit(dict(type='response', command=t, id=cmd.get('id'), success=True))
