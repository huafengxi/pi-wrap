"""One PIPE owner and one replaceable local socket client; no agent lifecycle.

The explicitly located, fixed agentd tool modules supply protocol identity and
in-memory DialogRequests only. No agent registration, mailbox or scheduler runs.
"""
import atexit
from collections import deque
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
import socket
import stat
import subprocess
import threading

from agentd_dependency import require_agentd
require_agentd('proto', 'wait_state')
import proto
from wait_state import DialogRequests, DIALOG_METHODS

RING_MAX = 200
STDERR_TAIL_MAX = 8192
DIAG_STDERR_LIMIT = 2048


def log(fmt, *args):
    print('[pi-host] ' + (fmt % args if args else fmt), file=__import__('sys').stderr, flush=True)


@dataclass(frozen=True)
class LaunchConfig:
    session_file: str
    cwd: str
    session_name: str
    pi_binary: str
    extension_argv: tuple
    socket_path: str
    env: dict

    def __post_init__(self):
        for name in ('session_file', 'cwd', 'socket_path'):
            value = getattr(self, name)
            if not isinstance(value, str) or not os.path.isabs(value):
                raise ValueError(name + ' must be an explicit absolute path')
        if not self.session_name or not self.pi_binary:
            raise ValueError('session_name and pi_binary are required')
        if not os.path.isdir(self.cwd):
            raise ValueError('cwd must exist')
        if not isinstance(self.env, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                                for k, v in self.env.items()):
            raise ValueError('env must be an explicit string map')
        if len(self.extension_argv) % 2 or any(self.extension_argv[i] != '-e'
                                              for i in range(0, len(self.extension_argv), 2)):
            raise ValueError('extension_argv accepts only explicit -e path pairs')

    def argv(self):
        return [self.pi_binary, '--mode', 'rpc', '--session', self.session_file,
                '-n', self.session_name, *self.extension_argv]


class Transport:
    def init_transport(self):
        self.pi = None
        self.ring = deque(maxlen=RING_MAX)
        self.lock = threading.Lock()
        self.send_lock = threading.Lock()
        self.conn = None
        self.stdin_open = True
        self.stderr_tail = deque()
        self.stderr_len = 0
        self.warned_oversize = self.warned_badjson = False
        self.srv = None
        self.dialog_requests = DialogRequests()
        self._socket_locks = []
        self._socket_identity = None
        self._pump_threads = []

    def setup_socket(self):
        """Locks stay local beside the socket; never unlink a lock inode.

        Both endpoint and canonical session key are locked, before examining or
        unlinking stale endpoints. Unknown existing endpoints fail closed. The
        local runtime directory is part of the stable session declaration.
        """
        d = os.path.dirname(self.sock_path)
        os.makedirs(d, mode=0o700, exist_ok=True)
        session = os.path.realpath(self.session_file)
        session_key = hashlib.sha256(os.fsencode(session)).hexdigest()[:24]
        try:
            for path in sorted({self.sock_path + '.lock', os.path.join(d, session_key + '.session.lock')}):
                fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                self._socket_locks.append(fd)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            doc = proto.read_json(self.pid_file)
            if isinstance(doc, dict) and doc.get('pid'):
                if proto.pid_identity_ok(doc['pid'], doc.get('procStart')):
                    raise RuntimeError('live host owns socket; refusing second host')
            if os.path.lexists(self.sock_path):
                if not isinstance(doc, dict) or not doc.get('pid'):
                    raise RuntimeError('unknown existing socket; refusing takeover')
                if not stat.S_ISSOCK(os.lstat(self.sock_path).st_mode):
                    raise RuntimeError('endpoint is not a socket')
                os.unlink(self.sock_path)
            self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.srv.bind(self.sock_path)
            st = os.lstat(self.sock_path)
            self._socket_identity = (st.st_dev, st.st_ino)
            os.chmod(self.sock_path, 0o600)
            self.srv.listen(4)
            proto.atomic_write_json(self.pid_file, {
                'pid': os.getpid(), 'procStart': proto.proc_starttime(os.getpid()),
                'startedAt': proto.now_ts(), **self.socket_metadata()})
            atexit.register(self.cleanup_socket)
        except BaseException as e:
            # Preserve the agent adapter's precise bind diagnostic contract.
            if isinstance(e, OSError) and not isinstance(e, BlockingIOError) and hasattr(self, 'diagnose'):
                extra = 'bind 失败: %r' % e
                if 'path too long' in str(e).lower():
                    extra += '；端点路径超平台 sun_path 上限，需换更浅的 root'
                self.diagnose('sock_bind_failed', 1, extra=extra)
            Transport.cleanup_socket(self)
            if isinstance(e, BlockingIOError):
                raise RuntimeError('拒启双宿主: local session/socket lock is held') from e
            raise
        log('socket ready: %s', self.sock_path)

    def socket_metadata(self):
        return dict(sessionFile=os.path.realpath(self.session_file), sessionName=self.session_name)

    def owns_socket(self):
        if self._socket_identity is None:
            return False
        try:
            st = os.lstat(self.sock_path)
            return (st.st_dev, st.st_ino) == self._socket_identity
        except FileNotFoundError:
            return False

    def cleanup_socket(self):
        if self.owns_socket():
            os.unlink(self.sock_path)
            try:
                os.unlink(self.pid_file)
            except FileNotFoundError:
                pass
        self._socket_identity = None
        if self.srv is not None:
            self.srv.close()
            self.srv = None
        for fd in self._socket_locks:
            os.close(fd)
        self._socket_locks = []

    def launch(self, config):
        if self.pi is not None:
            raise RuntimeError('one pi child per host invocation; explicit new host required')
        # S owns the lease. Propagate its actual open descriptor to the entire
        # cooperative foreground tree; never synthesize a lease or a generation.
        lease = config.env.get('RUND_LEASE_FD')
        pass_fds = ()
        if lease is not None:
            fd = int(lease)
            if fd < 3:
                raise ValueError('invalid RUND_LEASE_FD')
            os.fstat(fd)
            pass_fds = (fd,)
        self.pi = subprocess.Popen(config.argv(), cwd=config.cwd, env=config.env,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, pass_fds=pass_fds)
        for name, target in [('stdout', self._read_stdout), ('stderr', self._read_stderr)]:
            t = threading.Thread(target=target, daemon=True, name='wrap-' + name)
            self._pump_threads.append(t)
            t.start()
        log('pi spawned pid=%d session=%s', self.pi.pid, config.session_file)

    def _on_event(self, obj):
        self.dialog_requests.event(obj)

    def finish_pumps(self):
        # Real wait belongs to caller. A descendant retaining PIPEs may outlive
        # the leader; do not claim it has exited or invent a second supervisor.
        for t in self._pump_threads:
            t.join(timeout=1)
        if all(not t.is_alive() for t in self._pump_threads):
            for stream in (self.pi.stdin, self.pi.stdout, self.pi.stderr):
                stream.close()

    def pi_send(self, raw_line, expected_conn=None):
        """单写者串行写入 pi stdin（与透传上行、自身命令复用同一把锁）。"""
        with self.lock:
            if expected_conn is not None and self.conn is not expected_conn:
                return False
            if not self.stdin_open or self.pi is None or self.pi.stdin is None:
                return False
            try:
                self.pi.stdin.write(
                    raw_line if raw_line.endswith(b"\n") else raw_line + b"\n")
                self.pi.stdin.flush()
                try:
                    self.dialog_requests.sent(json.loads(raw_line))
                except (ValueError, TypeError):
                    pass
                return True
            except (OSError, ValueError):
                self.stdin_open = False
                return False


    def close_pi_stdin(self):
        with self.lock:
            if not self.stdin_open:
                return
            self.stdin_open = False
            try:
                self.pi.stdin.close()
            except (OSError, AttributeError):
                pass


    def _read_stdout(self):
        """持续吸走 pi stdout（无论有无人观测）：入环 + 转发当前连接 + 收敛解析。"""
        try:
            for raw in iter(lambda: self.pi.stdout.readline(), b""):
                if len(raw) > 16 * 1024 * 1024:
                    # 静默丢事件 ⇒ 可能吞掉一枚 agent_end ⇒ inflight 永久泄漏 ⇒ 收敛不成立。
                    # WARN 每进程只打一次，且文案不含每轮变化的可变量（否则去重失效刷屏）。
                    if not self.warned_oversize:
                        self.warned_oversize = True
                        log("WARN 单行 > 16MB 上限，本次丢弃的是一行 pi stdout 事件；"
                            "若其中含 agent_end，inflight 会泄漏、收敛改由 hang 看门狗"
                            "收口。本告警每进程只打一次")
                    continue                     # 单行上限（对齐 sessiond 口径）
                try:
                    obj = json.loads(raw)
                except ValueError:
                    # 同上：非 JSON 行里若夹带 agent_end 同样泄漏 inflight。
                    if not self.warned_badjson:
                        self.warned_badjson = True
                        log("WARN json.loads 失败，本次丢弃的是一行 pi stdout 事件；"
                            "若其中含 agent_end，inflight 会泄漏、收敛改由 hang 看门狗"
                            "收口。本告警每进程只打一次")
                    continue
                # Serialize live delivery with the in-memory dialog baseline
                # for attach replay, even when no browser/socket is connected.
                with self.send_lock:
                    if isinstance(obj, dict):
                        self._on_event(obj)
                    self.ring.append(raw)
                    with self.lock:
                        c = self.conn
                    if c is not None:
                        try:
                            c.sendall(raw)
                        except OSError:
                            self._drop_conn(c)
        except (OSError, ValueError):
            pass


    def _read_stderr(self):
        """pi stderr 泵：**到达即写**（不等 EOF）。

        硬要求 = 小额 stderr 在子进程**存活期**就落 `run/agentd/<name>.stderr.log`：
        `BufferedReader.read(n)` 攒满 n ∨ EOF 才返回 ⇒ 注入层的装配摘要行与全部 fail-soft
        WARN（都远小于 n）在活会话期恒不可见；而换代/kill 路径上读端（本进程）与写端（pi）
        同死 ⇒ 卡在管道缓冲里的内容无人读、永久丢失。⇒ 任何依赖 EOF flush 的形态（等退出
        再落盘 / 只调 flush 时机 / 加大缓冲）在那条路上恒失效，必须用 `read1(n)`（至多一次
        底层 read，有数据即返回；EOF 仍回 b""）∨ 对 raw fd `os.read`。内存尾（stderr_tail /
        STDERR_TAIL_MAX）与诊断尾同源，语义不变。
        """
        try:
            with open(self.stderr_log, "ab") as lf:
                while True:
                    d = self.pi.stderr.read1(4096)
                    if not d:
                        break
                    lf.write(d)
                    lf.flush()
                    self.stderr_tail.append(d)
                    self.stderr_len += len(d)
                    while self.stderr_len > STDERR_TAIL_MAX:
                        drop = self.stderr_tail.popleft()
                        self.stderr_len -= len(drop)
        except (OSError, ValueError):
            pass


    def stderr_tail_text(self):
        tail = b"".join(self.stderr_tail)[-DIAG_STDERR_LIMIT:]
        return tail.decode("utf-8", "replace")


    def accept_loop(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            # A non-reading observer can delay the pump only for this bounded
            # send timeout, then gets dropped. Idle readers are not disconnected.
            conn.settimeout(0.2)
            old = None
            try:
                with self.send_lock:
                    with self.lock:
                        old = self.conn
                        self.conn = conn
                    if old is not None:
                        self._shutdown_close(old)
                    for raw in self.replay_lines():
                        conn.sendall(raw)
            except OSError:
                self._drop_conn(conn)
                continue
            log("观测连接就位（替换=%s）", old is not None)
            threading.Thread(target=self._conn_relay, args=(conn,),
                             daemon=True, name="wrap-relay").start()


    def replay_lines(self):
        # Raw event history is not a dialog baseline. Resolved/expired requests
        # must not be resurrected; live requests survive ring eviction.
        for raw in list(self.ring):
            try:
                obj = json.loads(raw)
            except ValueError:
                continue
            if (isinstance(obj, dict) and obj.get("type") == "extension_ui_request"
                    and obj.get("method") in DIALOG_METHODS):
                continue
            yield raw
        for request in self.dialog_requests.dialogs():
            yield (json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8")


    def _conn_relay(self, conn):
        buf = b""
        while True:
            with self.lock:
                if self.conn is not conn:
                    return                       # 已被新连接替换
            try:
                d = conn.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            if not d:
                break
            buf += d
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                if not self.pi_send(line, expected_conn=conn):
                    self._drop_conn(conn)
                    return
        self._drop_conn(conn)


    @staticmethod
    def _shutdown_close(conn):
        try:
            conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            conn.close()
        except OSError:
            pass


    def _drop_conn(self, conn):
        with self.lock:
            if self.conn is conn:
                self.conn = None
        self._shutdown_close(conn)

