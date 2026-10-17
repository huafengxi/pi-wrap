"""Fixed S manual Service + ordinary P host, cooperative foreground fakepi only.
RUND_SOURCE must explicitly locate the read-only fixed S export. No supervisor is
implemented here; only the harness calls the existing Runtime APIs.
"""
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

import test_generic as harness
until = harness.until
sys.path.insert(0, os.environ['RUND_SOURCE'])
from runtime.lifecycle import Runtime
from runtime.kernel import read_json


class RuntimeHostTests(harness.Case):
    # Inherit fixture/harness helpers, not the ordinary standalone test cases.
    test_01_no_agent_pump_replacement_and_dialog = None
    test_02_double_host_and_explicit_recovery = None
    test_03_host_crash_is_not_child_wait = None
    test_04_actual_shared_method_imports = None
    test_06_unknown_socket_and_config_refusal = None

    def test_05_manual_group_lease_natural_exit_and_explicit_restart(self):
        source = self.area/'sourceGit'
        source.mkdir()
        p_root = Path(__file__).resolve().parent
        for name in ('pi_host.py', 'transport.py', 'agentd_dependency.py'):
            shutil.copyfile(p_root/name, source/name)
        from agentd_dependency import require_agentd
        tools = Path(require_agentd('proto', 'wait_state'))
        (source/'tools').mkdir()
        # Exactly the original fixed tool blobs, not agent lifecycle modules.
        for name in ('proto.py', 'wait_state.py'):
            shutil.copyfile(tools/name, source/'tools'/name)
        shutil.copyfile(self.config['piBinary'], source/'fakepi')
        (source/'fakepi').chmod(0o700)
        # This one-shot exec shim only materializes explicit paths after prepare.
        # It neither spawns nor waits nor supervises anything.
        (source/'entry.py').write_text('''import json,os,sys
from pathlib import Path
code=Path(__file__).resolve().parent
area=Path(os.environ['H1_AREA'])
cfg=dict(sessionFile=str(area/'history.jsonl'),cwd=os.getcwd(),sessionName='runtime-ordinary-h1',piBinary=str(code/'fakepi'),socketPath=str(area/'local/s'),extensionArgv=[],env={k:os.environ[k] for k in ('HOME','TMPDIR','PATH','H1_AREA')})
p=Path('config.json');p.write_text(json.dumps(cfg))
os.execv(sys.executable,[sys.executable,'-B',str(code/'pi_host.py'),str(p.resolve())])
''')
        def git(*args):
            result = subprocess.run(['git', '-C', str(source), *args], env=self.env, capture_output=True, text=True)
            self.record(git=list(args), rc=result.returncode, stdout=result.stdout, stderr=result.stderr)
            self.assertEqual(result.returncode, 0)
            return result.stdout.strip()
        git('init', '-q', '--template=')
        paths = ['entry.py', 'fakepi', 'pi_host.py', 'transport.py', 'agentd_dependency.py', 'tools']
        git('add', '--', *paths)
        message = self.area/'fixture-message'; message.write_text('8pm0lt synthetic fixed P/A foreground fixture\n')
        git('-c', 'user.name=H1 fixture', '-c', 'user.email=h1@example.invalid', 'commit', '-q', '-F', str(message), '--', *paths)
        pin = git('rev-parse', 'HEAD')
        d = dict(apiVersion='rund/v1alpha1', kind='Service', metadata=dict(id='host', version=1),
                 spec=dict(node='node-h1', sources=[dict(alias='main', repository=source.as_uri(), revision=pin, requires=[])],
                           prepare=[], toolchain=dict(python=sys.executable), cwd='${work}', credentialRefs=[],
                           argv=[sys.executable, '${code.main}/entry.py'],
                           env=dict(AGENTD_DIR='${code.main}/tools', H1_AREA=str(self.area), PYTHONDONTWRITEBYTECODE='1'),
                           desired='online', restart=dict(policy='manual', initialSeconds=.1, maxSeconds=.2)))
        self.record(declaration=d, S=__import__('runtime.kernel', fromlist=['proc']).__file__)
        r = Runtime(self.area/'node', 'node-h1')
        try:
            s = r.submit_service(d, 'explicit-start')
            run = r._service_current(s)
            p = r.kernel.processes[r.kernel.key(run)]
            self.children.append(p)
            ready = self.ready()
            self.assertEqual(ready['pgid'], run['pgid'])
            self.assertNotEqual(ready['pid'], run['pid'])  # S host and P pi are distinct
            lease = Path(r.kernel.key(run))/'lease.lock'
            st = lease.stat()
            self.assertEqual(ready['lease'], [st.st_dev, st.st_ino])
            conn = self.connect()
            self.command(conn, 'descendant')
            until(lambda: (self.area/'descendant.json').exists())
            descendant = json.loads((self.area/'descendant.json').read_text())
            self.known.append(descendant)
            self.assertEqual(descendant['pgid'], run['pgid'])
            self.assertEqual(descendant['lease'], ready['lease'])
            self.command(conn, 'exit', code=3)
            self.assertEqual(p.wait(timeout=6), 3)
            observation = r.kernel.observe(run)
            self.record(host_wait=p.returncode, run=run, observation=observation)
            self.assertTrue(observation['waitObserved'])
            self.assertTrue(observation['leaseBusy'], 'descendant must retain the real lease after host wait')
            self.assertEqual(observation['status'], 'unknown')
            for _ in range(3):
                state = r.tick_service('host')
                self.assertEqual(state['generation'], 1)
            (self.area/'release-descendant').touch()
            os.waitpid(descendant['pid'], 0)  # own subreaper, not a fabricated runtime wait
            s = until(lambda: (v if (v:=r.tick_service('host'))['phase']=='exited' else None))
            self.assertEqual(s['generation'], 1)
            for _ in range(3):
                self.assertEqual(r.tick_service('host')['generation'], 1)
            self.record(natural_exit=s, result=read_json(Path(r.kernel.key(run))/'result.json'))
            # Only S creates the next generation, from an explicit control.
            control = dict(apiVersion='rund-control/v1alpha1', requestId='explicit-restart', action='restart',
                           kind='Service', id='host', attemptId='service', expectedGeneration=1)
            s = r.control(control)
            self.assertEqual(s['generation'], 2)
            newrun = r._service_current(s)
            newp = r.kernel.processes[r.kernel.key(newrun)]
            self.children.append(newp)
            new = self.ready()
            self.assertNotEqual(new['pid'], ready['pid'])
            self.assertEqual(self.dialogs(self.command(self.connect())), [])
            control.update(requestId='explicit-stop', action='stop', expectedGeneration=2)
            stopped = r.control(control)
            self.assertEqual(stopped['phase'], 'stopped')
            self.assertEqual(r.tick_service('host')['generation'], 2)
            self.record(control=control, stopped=stopped)
        finally:
            # Candidate is not trusted to clean up a failed test. Each Popen is
            # independently reaped and saved fake/descendant identities cleaned.
            for p in self.children:
                if p.poll() is None:
                    p.kill()
                p.wait(timeout=5)
            r.kernel.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
