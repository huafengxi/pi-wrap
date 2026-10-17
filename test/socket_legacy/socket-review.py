#!/usr/bin/env python3
"""Independent subprocess+real AF_UNIX reconnection oracles against pinned P/A."""
import os,json,socket,subprocess as sp,sys,time,hashlib,traceback
from pathlib import Path
# Original eight wire oracles: task/0uzhg3. Only paths/interpreter/evidence
# locations are parameterized for H1; no expectation has been weakened.
import tempfile
SOURCE=Path(__file__).resolve().parent
BASE=Path(tempfile.mkdtemp(prefix='h1sock-',dir='/tmp'));T=BASE
(T/'evidence').mkdir()
PY=sys.executable; fake=T/'review-fakepi.py'
fake.write_text('#!'+PY+'\n'+(SOURCE/'review-fakepi.py').read_text());fake.chmod(0o700)
layouts=json.loads(Path(sys.argv[1]).read_text())
print('socket evidence:',BASE,flush=True)
results=[]
for layout in ('sibling','explicit'):
 P=Path(layouts[layout]['P'])
 A=Path(layouts[layout]['A'])
 for variant,payload in [('absent',None),('forged',b'{"gen":1,"waits":[{"kind":"dialog","request":{"id":"forged","method":"input"}}]}'),('stale',b'{"gen":0,"waits":[{"kind":"ask","askId":"stale"}]}'),('invalid',b'{broken')]:
  label=layout+'-'+variant;root=BASE/label;home=root/'agents/task/review';(home/'session').mkdir(parents=True);(home/'inbox').mkdir()
  (home/'prompt.md').write_text('review synthetic prompt');(home/'spec.json').write_text('{}')
  legacy=home/'wait-state.json'
  if payload is not None:legacy.write_bytes(payload)
  before=legacy.stat() if legacy.exists() else None
  (root/'home').mkdir();(root/'tmp').mkdir();(root/'other cwd').mkdir()
  env=dict(HOME=str(root/'home'),TMPDIR=str(root/'tmp'),PATH='/home/yuanqi.xhf/miniconda3/bin:/usr/bin:/bin',LANG='C.UTF-8',PYTHONDONTWRITEBYTECODE='1',AGENT_HOME=str(home),AGENT_ROOT=str(root),AGENT_SELF='task/review',AGENTD_RESIDENT='1',AGENTD_WRAP_PI_BIN=str(fake))
  if layout=='explicit':env['AGENTD_DIR']=str(A)
  command=[PY,'-B',str(SOURCE/'guard-launch.py'),str(P/'pi-rpc-wrap.py')]
  log=(T/'evidence'/('socket-'+label+'.stderr')).open('w');wire=(T/'evidence'/('socket-'+label+'.jsonl')).open('w');clients=[];proc=None;seq=0
  def record(**obj):wire.write(json.dumps(obj)+'\n');wire.flush()
  record(command=command,env=env,cwd=str(root/'other cwd'),P=str(P),A='b40e0d027b1ebbe072b0edc0929e0411da3c37af')
  def connect():
   c=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);c.settimeout(6);c.connect(str(root/'run/agentd/review.sock'));f=c.makefile('rb');clients.append((c,f));return c,f
  def barrier(conn,action=None,**kw):
   global seq
   seq+=1;rid='review-'+str(seq);c,f=conn
   msg=dict(type='review_control',id=rid,action=action,**kw) if action else dict(type='get_state',id=rid)
   c.sendall((json.dumps(msg)+'\n').encode());record(sent=msg)
   events=[]
   while True:
    raw=f.readline();assert raw, 'socket EOF';o=json.loads(raw);record(received=o,barrier=rid);events.append(o)
    if o.get('type')=='response' and o.get('id')==rid:return events
  def attached(expected):
   conn=connect();events=barrier(conn);dialogs=[o for o in events if o.get('type')=='extension_ui_request' and o.get('method') in ('select','confirm','input','editor')]
   assert [x['id'] for x in dialogs]==expected,dialogs
   record(assertion='attach dialog baseline',expected=expected,actual=dialogs)
   return conn,events,dialogs
  try:
   proc=sp.Popen(command,env=env,cwd=root/'other cwd',stdout=sp.DEVNULL,stderr=log)
   deadline=time.monotonic()+10
   while not (home/'review-emitted').exists():
    assert proc.poll() is None,'wrapper died';assert time.monotonic()<deadline;time.sleep(.01)
   # First barrier serializes with prompt/event burst and proves ring overflow.
   conn=connect();barrier(conn);conn,events,dialogs=attached(['live'])
   ticks=[o['index'] for o in events if o.get('type')=='review_tick'];assert ticks and min(ticks)>0
   first_timeout=dialogs[0]['timeout'];time.sleep(.04)
   conn,_,dialogs=attached(['live']);assert 0<dialogs[0]['timeout']<first_timeout<20000
   # False settle while a tool+turn is live must not clear the baseline.
   barrier(conn,'false_settle');conn,_,_=attached(['live'])
   # Successful cancellation reaches stdin; no server event is emitted for it.
   conn[0].sendall(b'{"type":"extension_ui_response","id":"live","cancelled":true}\n');barrier(conn)
   conn,_,_=attached([])
   barrier(conn,'end_tool');barrier(conn,'end_turn');conn,_,_=attached([])
   # Raw ring retains this dialog; tool completion without a client answer must
   # clear memory, so reconnect cannot replay the historical wire request.
   barrier(conn,'begin',dialog='tool-finished',method='editor');conn,_,_=attached(['tool-finished'])
   barrier(conn,'end_tool');conn,_,_=attached([]);barrier(conn,'end_turn')
   # Bare abort is not a settlement signal. Actual agent_end is.
   barrier(conn,'begin',dialog='turn-finished',method='select');conn,_,_=attached(['turn-finished'])
   conn[0].sendall(b'{"type":"abort"}\n');barrier(conn);conn,_,_=attached(['turn-finished'])
   barrier(conn,'end_turn');conn,_,_=attached([])
   # Deadline must not be restarted by reconnect, and response without emitted
   # events must clear immediately after successful stdin write.
   barrier(conn,'begin',dialog='expires',timeout=80);time.sleep(.12);conn,_,_=attached([]);barrier(conn,'end_turn')
   barrier(conn,'begin',dialog='answer');conn,_,_=attached(['answer'])
   conn[0].sendall(b'{"type":"extension_ui_response","id":"answer","value":"ok"}\n');barrier(conn);conn,_,_=attached([])
   barrier(conn,'end_turn');barrier(conn,'settled');conn,_,_=attached([])
   barrier(conn,'shutdown');rc=proc.wait(timeout=6);assert rc==0,rc
   if payload is None:assert not legacy.exists()
   else:
    after=legacy.stat();assert legacy.read_bytes()==payload;assert (before.st_mtime_ns,before.st_atime_ns)==(after.st_mtime_ns,after.st_atime_ns), 'legacy touched'
   log.flush();text=(T/'evidence'/('socket-'+label+'.stderr')).read_text();assert 'review IO tripwire active' in text and 'FORBIDDEN' not in text
   results.append(dict(case=label,returncode=rc,status='PASS',legacy='never opened (audit hook); absent or same bytes and pre-read mtime/atime',reconnects=len(clients),wire=str(wire.name),stderr=str(log.name)))
   print(label,'PASS',len(clients),'real socket connections',flush=True)
  except Exception:
   results.append(dict(case=label,status='HARNESS_OR_PRODUCT_FAILURE',error=traceback.format_exc()));raise
  finally:
   for c,f in clients:f.close();c.close()
   if proc is not None and proc.poll() is None:
    proc.terminate()
    try:proc.wait(timeout=5)
    except sp.TimeoutExpired:proc.kill();proc.wait()
   wire.close();log.close();(T/'evidence/socket-results.json').write_text(json.dumps(results,indent=2)+'\n')
print('PASS: 8 independent wire cases, both fixed layouts; no real model or external socket')
