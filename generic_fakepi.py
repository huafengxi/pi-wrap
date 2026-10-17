#!/usr/bin/env python3
"""Synthetic RPC fixture, never a model. Used only by H1 isolated tests."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def identity():
    lease = os.environ.get('RUND_LEASE_FD')
    return dict(pid=os.getpid(), pgid=os.getpgrp(), start=Path('/proc/self/stat').read_text().split(') ')[1].split()[19],
                stdin=os.fstat(0).st_ino, stdout=os.fstat(1).st_ino,
                lease=None if lease is None else [os.fstat(int(lease)).st_dev, os.fstat(int(lease)).st_ino],
                name=sys.argv[sys.argv.index('-n') + 1])


def emit(obj):
    print(json.dumps(obj), flush=True)


def main():
    session = Path(sys.argv[sys.argv.index('--session') + 1])
    area = Path(os.environ['H1_AREA'])
    area.mkdir(exist_ok=True)
    (area/'ready.json').write_text(json.dumps(identity()))
    def burst(count):
        for i in range(count):
            emit(dict(type='tick', index=i, payload='x'*4096))
        (area/'burst-done').write_text(str(count))
    burst(int(os.environ.get('H1_BURST', '0')))
    for line in sys.stdin:
        obj = json.loads(line)
        with (area/'commands.jsonl').open('a') as f:
            f.write(json.dumps(obj)+'\n')
        with session.open('a') as f:
            f.write(json.dumps(dict(type='message', message=dict(role='user', content=obj)))+'\n')
        t = obj.get('type')
        if t == 'dialog':
            emit(dict(type='agent_start'))
            emit(dict(type='tool_execution_start', toolCallId='native'))
            emit(dict(type='extension_ui_request', id='live', method='input', timeout=30000, title='synthetic question'))
        if t == 'end_turn':
            emit(dict(type='tool_execution_end', toolCallId='native'))
            emit(dict(type='agent_end'))
        if t == 'settled':
            emit(dict(type='agent_settled'))
        if t == 'burst':
            burst(5000)
        if t == 'descendant':
            fd = int(os.environ['RUND_LEASE_FD'])
            # A cooperative child retains the actual lease even after its
            # foreground parent exits. Runtime must keep the group conservative.
            code = '''import os,json,time,sys
from pathlib import Path
area=Path(sys.argv[1]);fd=int(os.environ['RUND_LEASE_FD'])
s=os.fstat(fd)
(area/'descendant.json').write_text(json.dumps(dict(pid=os.getpid(),pgid=os.getpgrp(),lease=[s.st_dev,s.st_ino],start=Path('/proc/self/stat').read_text().split(') ')[1].split()[19])))
while not (area/'release-descendant').exists(): time.sleep(.01)
'''
            subprocess.Popen([sys.executable, '-B', '-c', code, str(area)], pass_fds=(fd,),
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        emit(dict(type='response', id=obj.get('id'), command=t, success=True, data=identity()))
        if t == 'effect':
            with (area/'effects').open('a') as f:
                f.write(obj['id']+'\n')
        if t == 'exit':
            return obj.get('code', 0)
    return 0


if __name__ == '__main__':
    sys.exit(main())
