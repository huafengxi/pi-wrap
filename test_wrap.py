#!/usr/bin/env python3
"""test_wrap.py — pi-rpc-wrap.py 单元测试（验证组 U）。

在 /tmp 临时树搭 agents/task/<id>/，以 fakepi_rpc.py 冒充 `pi --mode rpc`，
逐场景断言。仅标准库。用法：python3 pi-wrap/test_wrap.py

射程分工：**人格面的内容**（caps 展开序 / 回落 / 工具面并集 / knowledge 三档 / model 派生 /
压缩策略归一 / 降级矩阵）不在这里断言 —— 那是解析层的输出契约，归 `test_persona.py`（P 系列）。
本文件对人格面只断言 wrap 的两个动作（T48）：把注入层扩展 `-e` 进去、把输入 env 透传下去。
"""
import glob
import json
import os
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WRAP = os.path.join(HERE, "pi-rpc-wrap.py")
FAKEPI = os.path.join(HERE, "fakepi_rpc.py")
# proto.py 住在兄弟仓 agentd/（协议单点，与 TS 侧 core.ts 同源）；AGENTD_DIR 可改指
AGENTD_DIR = os.environ.get("AGENTD_DIR") or os.path.join(
    os.path.dirname(HERE), "agentd")
for _p in (HERE, AGENTD_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import proto  # noqa: E402  就绪标记路径单点

PASS = 0
FAIL = 0


def ok(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("PASS  %s" % name)
    else:
        FAIL += 1
        print("FAIL  %s  %s" % (name, detail))


class Client:
    """unix socket 客户端（收集行 → 解析事件/响应）。"""

    def __init__(self, sock_path):
        self.c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.c.connect(sock_path)
        self.lines = []
        self.lock = threading.Lock()
        self.alive = True
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        buf = b""
        while self.alive:
            try:
                d = self.c.recv(262144)
            except OSError:
                break
            if not d:
                break
            buf += d
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                with self.lock:
                    self.lines.append(obj)
        self.alive = False

    def send(self, obj):
        try:
            self.c.sendall((json.dumps(obj) + "\n").encode())
            return True
        except OSError:
            self.alive = False
            return False

    def wait_resp(self, rid, timeout=10):
        dl = time.time() + timeout
        while time.time() < dl:
            with self.lock:
                for o in self.lines:
                    if o.get("type") == "response" and o.get("id") == rid:
                        return o
            time.sleep(0.05)
        return None

    def wait_event(self, etype, timeout=10):
        dl = time.time() + timeout
        while time.time() < dl:
            with self.lock:
                for o in self.lines:
                    if o.get("type") == etype:
                        return o
            time.sleep(0.05)
        return None

    def closed(self, timeout=5):
        dl = time.time() + timeout
        while time.time() < dl:
            if not self.alive:
                return True
            time.sleep(0.05)
        return not self.alive

    def close(self):
        self.alive = False
        try:
            self.c.close()
        except OSError:
            pass


class Env:
    """一个任务目录 + wrap 进程环境。"""

    def __init__(self, name, fake_mode="ok", extra_env=None, preseed_user=False,
                 with_prompt=True):
        self.root = tempfile.mkdtemp(prefix="wraptest-%s-" % name)
        self.name = "t-" + name            # 二段 name（目录/端点用）
        self.task_id = "task/" + self.name  # 路径式 id（AGENT_SELF，任务）
        self.home = os.path.join(self.root, "agents", "task", self.name)
        os.makedirs(os.path.join(self.home, "session"))
        os.makedirs(os.path.join(self.home, "inbox"))
        if with_prompt:
            with open(os.path.join(self.home, "prompt.md"), "w") as f:
                f.write("# fake prompt\n\ndo the thing\n")
        if preseed_user:
            with open(os.path.join(self.home, "session",
                                   "session.jsonl"), "w") as f:
                f.write(json.dumps({"type": "session", "id": "e0"}) + "\n")
                f.write(json.dumps({"type": "message", "id": "e1",
                                    "parentId": "e0",
                                    "message": {"role": "user",
                                                "content": "prior"}}) + "\n")
        self.flags = os.path.join(self.home, "flags")
        os.makedirs(self.flags)
        self.env = {
            "AGENT_HOME": self.home, "AGENT_ROOT": self.root,
            "AGENT_SELF": self.task_id,
            "AGENTD_WRAP_PI_BIN": FAKEPI,
            "AGENTD_WRAP_INIT_TIMEOUT": "10",
            "AGENTD_WRAP_EXIT_GRACE": "5",
            "FAKE_MODE": fake_mode, "FAKE_FLAG_DIR": self.flags,
            "PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp"),
        }
        if extra_env:
            self.env.update(extra_env)
        self.sock = os.path.join(self.root, "run", "agentd",
                                 self.name + ".sock")

    def start_wrap(self):
        os.chmod(FAKEPI, 0o755)
        # start_new_session：测试进程与 wrap 分组隔离（清理时组杀不波及自身；
        # 与 runner spawn 的形态一致）
        return subprocess.Popen([sys.executable, WRAP], env=self.env,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                start_new_session=True)

    def wait_sock(self, timeout=10):
        dl = time.time() + timeout
        while time.time() < dl:
            if os.path.exists(self.sock):
                return True
            time.sleep(0.05)
        return False

    def wait_argv(self, timeout=15):
        """事件驱动等「fake pi 已启动并落下 argv 快照」（就绪标记 = <flags>/argv，
        fakepi_rpc.py 开机即写）。两类计时敏感断言一律改等本标记，不用 sleep 硬等：

        ① **首个请求类断言**（T1 get_entries / T2 get_state建议修 3 的
        历史瞬态失败）：wrap 的 run() 顺序 = setup_socket（sock 文件在场）→ accept 线程
        → spawn_pi，故「sock 就位」≠「self.pi 已赋值」；观测连接落在该窗口内发请求 →
        pi_send 判 `self.pi is None` 返回 False → _conn_relay 直接断连（观测面既有语义，
        非缺陷）→ 客户端永等不到响应。等 argv 快照在场再连即关闭该窗口（子进程解释器
        启动耗时 ≫ 父进程 Popen 返回后的赋值），且仍远在收敛关 stdin 之前。
        ② **resident 形态 argv 断言**（T11/T13c/T21b/T27/T28f）：resident 不收敛、无退出
        点可等，旧实现 `time.sleep(1.5~2)` 硬等 = 机器负载高时快照未落 → 假失败。

        返回 bool（超时 False，交由紧随其后的既有断言带 detail 报错，不改断言数）。
        """
        dl = time.time() + timeout
        while time.time() < dl:
            if self.read_argv() is not None:
                return True
            time.sleep(0.02)
        return False

    def wait_flag(self, name, timeout=15):
        """事件驱动等指定 flag 文件可读（JSON 可解析）。形态同 wait_argv，
        用于 wait_argv 之后仍需等后续 flag 落盘的场景（fakepi_rpc.py 写序：
        argv → gate_env → persona_env，wait_argv 只保证第一个）。绝不无条件等。
        返回 bool（超时 False）。"""
        dl = time.time() + timeout
        while time.time() < dl:
            if self._read_json(name) is not None:
                return True
            time.sleep(0.02)
        return False

    def read_diag(self):
        p = os.path.join(self.home, "diagnosis.md")
        if not os.path.exists(p):
            return None
        with open(p) as f:
            return f.read()

    def read_argv(self):
        """fake 启动时落的 argv 快照（：resident argv 断言）。"""
        return self._read_json("argv")

    def read_persona_env(self):
        """fake 启动时落的**人格输入 env** 快照（人格装配不住 argv ⇒ wrap 的职责只剩
        「-e 注入层 + 透传输入 env」，本快照钉后半句；缺键 = None）。"""
        return self._read_json("persona_env")

    def _read_json(self, name):
        p = os.path.join(self.flags, name)
        if not os.path.exists(p):
            return None
        try:
            with open(p) as f:
                return json.loads(f.read())
        except ValueError:
            return None

    def cleanup(self, procs):
        for p in procs:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
        shutil.rmtree(self.root, ignore_errors=True)


def t1_normal():
    """T1 全链路：投递→透传 get_entries→settled→收敛→exit 0→sock 清理、无诊断。"""
    e = Env("t1")
    p = e.start_wrap()
    try:
        ok("T1 sock 就位", e.wait_sock(), e.sock)
        e.wait_argv()          # 等 pi 已 spawn（否则首个请求落在 pre-spawn 窗口被断连）
        c = Client(e.sock)
        c.send({"id": "GE1", "type": "get_entries"})
        r = c.wait_resp("GE1")
        ok("T1 get_entries 经 socket 配对",
           r is not None and r.get("success") is True
           and len(r.get("data", {}).get("entries", [])) == 2, repr(r))
        c.wait_event("agent_settled", 15)
        rc = p.wait(timeout=20)
        ok("T1 收敛退出码 0", rc == 0, "rc=%s" % rc)
        diag = e.read_diag()
        ok("T1 无诊断文件", diag is None, (diag or "")[:200])
        ok("T1 sock 已清理", not os.path.exists(e.sock))
        ok("T1 .pid 已清理", not os.path.exists(e.sock + ".pid"))
        ok("T1 fake 收到过 prompt",
           os.path.exists(os.path.join(e.flags, "prompt")))
        argv = e.read_argv()
        ok("T1 任务形态 argv（-n 任务名；人格面不在 argv —— 工具面/正文/模型全归注入层）",
           argv is not None and "-n" in argv
           and argv[argv.index("-n") + 1] == "[task %s]" % e.name
           and not [f for f in PERSONA_FLAGS if f in argv],
           repr(argv))
        c.close()
    finally:
        e.cleanup([p])


def t2_relay_replace():
    """T2 透传与连接替换：新连接替换旧连接；小环回放（hang_settle 长活不收敛）。"""
    e = Env("t2", fake_mode="hang_settle")
    p = e.start_wrap()
    try:
        ok("T2 sock 就位", e.wait_sock())
        e.wait_argv()          # 同 T1：等 pi 已 spawn 再发首个请求
        c1 = Client(e.sock)
        c1.send({"id": "A1", "type": "get_state"})
        ok("T2 c1 get_state", (c1.wait_resp("A1") or {}).get("success"))
        c2 = Client(e.sock)                    # 新连接替换旧连接
        ok("T2 旧连接被断开", c1.closed(5))
        ok("T2 新连接收到环回放（agent_start 等）",
           c2.wait_event("agent_start", 10) is not None
           or c2.wait_event("agent_settled", 10) is not None)
        c2.send({"id": "A2", "type": "get_state"})
        ok("T2 c2 get_state", (c2.wait_resp("A2") or {}).get("success"))
        c2.close()
        c1.close()
    finally:
        e.cleanup([p])


def t3_crash():
    """T3 启动即炸：pi exit 3 → 诊断 stage=pi_died_before_ack + stderr 尾。"""
    e = Env("t3", fake_mode="crash")
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        ok("T3 透传 pi 退出码", rc == 3, "rc=%s" % rc)
        diag = e.read_diag() or ""
        ok("T3 诊断在场且阶段正确", "pi_died_before_ack" in diag, diag[:300])
        ok("T3 stderr 尾入诊断", "boom" in diag, diag[:300])
        ok("T3 sock 已清理", not os.path.exists(e.sock))
    finally:
        e.cleanup([p])


def t4_reject():
    """T4 初始投递被拒：诊断 stage=prompt_rejected、exit 1。"""
    e = Env("t4", fake_mode="reject")
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        ok("T4 exit 1", rc == 1, "rc=%s" % rc)
        diag = e.read_diag() or ""
        ok("T4 诊断阶段", "prompt_rejected" in diag, diag[:300])
        ok("T4 拒绝原因透传", "fake rejected" in diag, diag[:300])
    finally:
        e.cleanup([p])


def t5_idempotent():
    """T5 幂等：会话已有 user 消息 → 跳过投递；idle_settle 收敛。"""
    e = Env("t5", fake_mode="idle_settle", preseed_user=True)
    p = e.start_wrap()
    try:
        ok("T5 sock 就位", e.wait_sock())
        rc = p.wait(timeout=20)
        ok("T5 收敛退出码 0", rc == 0, "rc=%s" % rc)
        ok("T5 fake 未收到 prompt（跳过投递）",
           not os.path.exists(os.path.join(e.flags, "prompt")))
        ok("T5 无诊断", e.read_diag() is None)
    finally:
        e.cleanup([p])


def t6_window_cancel():
    """T6 竞态窗取消：首个 settled 后窗内注入 follow_up → 不收敛、续跑再收敛。"""
    e = Env("t6", extra_env={"AGENTD_WRAP_SETTLE_WINDOW": "1.5"})
    p = e.start_wrap()
    try:
        ok("T6 sock 就位", e.wait_sock())
        c = Client(e.sock)
        ok("T6 等首个 settled", c.wait_event("agent_settled", 15) is not None)
        c.send({"id": "FU1", "type": "follow_up", "message": "injected"})
        r = c.wait_resp("FU1")
        ok("T6 follow_up 受理", (r or {}).get("success") is True, repr(r))
        ok("T6 窗内注入后仍收敛（第二轮 settled）", p.wait(timeout=25) == 0)
        ok("T6 fake 收到 follow_up",
           os.path.exists(os.path.join(e.flags, "follow_up")))
        c.close()
    finally:
        e.cleanup([p])


def t6b_steer_injection():
    """T6b steer 注入：干预面投递方式——steer 命令被受理、
    落 pi 的 steering 队列（非 followUp）、同样不提前收敛。"""
    e = Env("t6b", extra_env={"AGENTD_WRAP_SETTLE_WINDOW": "1.5"})
    p = e.start_wrap()
    try:
        ok("T6b sock 就位", e.wait_sock())
        c = Client(e.sock)
        ok("T6b 等首个 settled", c.wait_event("agent_settled", 15) is not None)
        c.send({"id": "ST1", "type": "steer", "message": "立即改向"})
        r = c.wait_resp("ST1")
        ok("T6b steer 受理", (r or {}).get("success") is True, repr(r))

        def steering_seen(timeout=5):
            dl = time.time() + timeout
            while time.time() < dl:
                with c.lock:
                    for o in c.lines:
                        if o.get("type") == "queue_update" and o.get("steering"):
                            return o
                time.sleep(0.05)
            return None

        qu = steering_seen()
        ok("T6b steering 队列非空（steer 进 steering、不进 followUp）",
           qu is not None and not qu.get("followUp"), repr(qu))
        ok("T6b fake 收到 steer",
           os.path.exists(os.path.join(e.flags, "steer")))
        ok("T6b 注入后仍收敛（第二轮 settled）", p.wait(timeout=25) == 0)
        c.close()
    finally:
        e.cleanup([p])


def t7_stale_takeover():
    """T7 双宿主拒启 + 死节点接管。"""
    e = Env("t7", fake_mode="idle_settle")
    p1 = e.start_wrap()
    try:
        ok("T7 sock 就位", e.wait_sock())
        p2 = e.start_wrap()                       # 同端点第二实例 → 拒启
        rc2 = p2.wait(timeout=15)
        ok("T7 双宿主拒启（exit 1）", rc2 == 1, "rc=%s" % rc2)
        err2 = p2.stderr.read().decode("utf-8", "replace")
        ok("T7 拒启原因", "拒启" in err2 or "双宿主" in err2, err2[:200])
        os.killpg(os.getpgid(p1.pid), signal.SIGKILL)   # 杀出死节点（留陈旧档）
        p1.wait(timeout=10)
        ok("T7 陈旧 sock 残留（待接管）", os.path.exists(e.sock))
        p3 = e.start_wrap()                       # 身份已死 → 接管重建
        ok("T7 接管成功", e.wait_sock() and p3.poll() is None)
        os.killpg(os.getpgid(p3.pid), signal.SIGKILL)
        p3.wait(timeout=10)
    finally:
        e.cleanup([p1])


def t8_resident_no_converge():
    """T8 resident 无完成收敛：agent_settled 后不关 stdin、不退出；
    argv 会话名钉死 AGENTD_SESSION_NAME、不注入子端扩展、不屏蔽 ask_user；
    follow_up 注入链路保留。"""
    e = Env("t8", extra_env={"AGENTD_RESIDENT": "1",
                             "AGENTD_SESSION_NAME": "bot/zz-resident-t8"})
    p = e.start_wrap()
    try:
        ok("T8 sock 就位", e.wait_sock())
        c = Client(e.sock)
        ok("T8 等首个 settled", c.wait_event("agent_settled", 15) is not None)
        time.sleep(1.5)  # 任务形态在收敛窗（0.5s）后已关 stdin；resident 必须仍活
        ok("T8 settled 后不收敛（进程存活）", p.poll() is None,
           "rc=%s" % p.poll())
        c.send({"id": "FU1", "type": "follow_up", "message": "still here"})
        r = c.wait_resp("FU1")
        ok("T8 follow_up 受理（注入/ask 链路保留）",
           (r or {}).get("success") is True, repr(r))
        argv = e.read_argv()
        ok("T8 argv 会话名钉死",
           argv is not None and "-n" in argv
           and argv[argv.index("-n") + 1] == "bot/zz-resident-t8", repr(argv))
        ok("T8 argv 无子端扩展/不屏蔽 ask_user",
           argv is not None and "-e" not in argv and "-xt" not in argv,
           repr(argv))
        ok("T8 无诊断", e.read_diag() is None)
        c.close()
    finally:
        e.cleanup([p])


def t9_resident_exit_passthrough():
    """T9 resident 退出码语义：pi 非预期退出（7）透传记账，
    不写诊断不误报（收敛语义不适用）；sock/.pid 照常清理。"""
    e = Env("t9", fake_mode="selfkill", preseed_user=True,
            extra_env={"AGENTD_RESIDENT": "1"})
    p = e.start_wrap()
    try:
        ok("T9 sock 就位", e.wait_sock())
        rc = p.wait(timeout=20)
        ok("T9 pi 退出码 7 透传", rc == 7, "rc=%s" % rc)
        ok("T9 无诊断（非收敛退出不误报）", e.read_diag() is None,
           (e.read_diag() or "")[:200])
        ok("T9 sock 已清理", not os.path.exists(e.sock))
        ok("T9 .pid 已清理", not os.path.exists(e.sock + ".pid"))
    finally:
        e.cleanup([p])


def t10_resident_no_prompt():
    """T10 resident 初始引导可选（设计决策 6）：无 prompt.md 裸启动，
    不诊断不退出（任务形态同场景会走 prompt_read_failed 诊断）。"""
    e = Env("t10", fake_mode="hang_settle", with_prompt=False,
            extra_env={"AGENTD_RESIDENT": "1"})
    p = e.start_wrap()
    try:
        ok("T10 sock 就位", e.wait_sock())
        time.sleep(1.0)
        ok("T10 裸启动存活", p.poll() is None, "rc=%s" % p.poll())
        ok("T10 无诊断", e.read_diag() is None, (e.read_diag() or "")[:200])
        ok("T10 fake 未收到 prompt",
           not os.path.exists(os.path.join(e.flags, "prompt")))
    finally:
        e.cleanup([p])


def t11_probe_ext():
    """T11 探针扩展注入（任务/常驻同款）：探针在场时 argv 带 -e probe.ts，
    使 spec.json?v=chat 观测面 /inspect 可用；缺失时静默跳过（其余测试已覆盖）。"""
    for mode, extra in (("task", {}),
                        ("resident", {"AGENTD_RESIDENT": "1",
                                      "AGENTD_SESSION_NAME":
                                          "bot/zz-probe-t11"})):
        e = Env("t11-" + mode, extra_env=extra)
        probe = os.path.join(e.root, "w", "ext", "sessiond", "probe.ts")
        os.makedirs(os.path.dirname(probe))
        with open(probe, "w") as f:
            f.write("export default function () {}\n")
        p = e.start_wrap()
        try:
            ok("T11 %s sock 就位" % mode, e.wait_sock(), e.sock)
            if mode == "task":
                p.wait(timeout=20)
            else:
                e.wait_argv()      # resident 不收敛：事件驱动等 argv 快照（旧 sleep(2)）
            argv = e.read_argv()
            ok("T11 %s 探针注入（-e probe.ts）" % mode,
               argv is not None and "-e" in argv and probe in argv,
               repr(argv))
        finally:
            e.cleanup([p])


def t12_sock_bind_failed():
    """T12 bind 失败专属诊断（ 阶段、 判定修复）：注入深 AGENT_ROOT
    使端点超内核 sun_path 上限（Linux 108 / macOS 104 字节）→ CPython unix_bind 预检
    抛 OSError('AF_UNIX path too long')（errno=None，旧 e.errno 判定是死分支）→
    断言 diagnosis.md stage=sock_bind_failed 且平台限提示文案在场。"""
    e = Env("t12")
    # 不改 AGENT_HOME（home 侧文件操作不受影响），只把 AGENT_ROOT 换成深树：
    # 端点 = <root>/run/agentd/<name>.sock 必超平台限（macOS 104 更严，同样命中）。
    deep = tempfile.mkdtemp(prefix="wraptest-t12deep-")
    deep = os.path.join(deep, *(["d" * 16] * 8))
    os.makedirs(deep)
    e.env["AGENT_ROOT"] = deep
    e.sock = os.path.join(deep, "run", "agentd", e.name + ".sock")
    assert len(e.sock.encode()) > 108, "深路径用例前提不成立: %r" % e.sock
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        ok("T12 bind 失败 exit 1", rc == 1, "rc=%s" % rc)
        diag = e.read_diag() or ""
        ok("T12 诊断阶段 sock_bind_failed", "sock_bind_failed" in diag,
           diag[:300])
        ok("T12 平台限提示文案在场",
           "sun_path" in diag and "换更浅的 root" in diag, diag[:400])
    finally:
        e.cleanup([p])
        base = deep
        for _ in range(8):
            base = os.path.dirname(base)
        shutil.rmtree(base, ignore_errors=True)


def _w(path, text="export default function () {}\n"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


EXEC_PROFILE_MODEL = "llm-router/executor"      # 生产 bots/profiles/executor.json 的 model 现值
PLANNER_MODEL = "llm-router/planner"            # 生产常驻 profile（dispatcher/*-lead）的 model 现值


def t29_child_exts():
    """T29 子端扩展注入面：CHILD_EXTS 两文件在场 → 任务形态 argv 按序
    注入两个 -e（含 receiver-child = 子任务自家信箱的推送收件面）；文件缺失 → 跳过不拖垮
    会话（既有容错口径）；resident 形态一律不注入（主端 index.ts 由 workdir 的 .pi 自动发现，
    其 receiver 已覆盖自家信箱，再注入只会白占一份 watch/poll）。"""
    rel_dir = os.path.join("assistant", ".pi", "extensions", "agentd")
    names = ("ask-user-child.ts", "receiver-child.ts")

    def _mk_exts(e):
        d = os.path.join(e.root, rel_dir)
        os.makedirs(d, exist_ok=True)
        for n in names:
            with open(os.path.join(d, n), "w") as f:
                f.write("export default function () {}\n")
        return [os.path.join(e.root, rel_dir, n) for n in names]

    # ① 两文件在场 → 任务形态按 CHILD_EXTS 顺序注入（探针扩展不在场 → 不多不少两个）
    e = Env("t29a")
    want = _mk_exts(e)
    p = e.start_wrap()
    try:
        ok("T29a sock 就位", e.wait_sock())
        e.wait_argv()
        argv = e.read_argv()
        got = [argv[i + 1] for i, x in enumerate(argv or []) if x == "-e"]
        ok("T29a 任务形态注入子端扩展×2（含 receiver-child，按 CHILD_EXTS 顺序）",
           got == want, repr(got))
        ok("T29a 注入路径均在场（缺失会被 wrap 静默跳过 = 收件面凭空失效）",
           bool(got) and all(os.path.exists(g) for g in got), repr(got))
        ok("T29a 任务形态仍是 --mode rpc（非 print → steer/followUp 两队列均可用）",
           argv is not None and "--mode" in argv
           and argv[argv.index("--mode") + 1] == "rpc", repr(argv))
    finally:
        e.cleanup([p])

    # ② 文件缺失 → 静默跳过，会话照常起（不拖垮）
    e2 = Env("t29b")
    p2 = e2.start_wrap()
    try:
        ok("T29b sock 就位", e2.wait_sock())
        e2.wait_argv()
        argv2 = e2.read_argv()
        ok("T29b 子端扩展缺失 → 零 -e 且会话照常起",
           argv2 is not None and "-e" not in argv2, repr(argv2))
    finally:
        e2.cleanup([p2])

    # ③ resident 形态：文件在场也不注入 CHILD_EXTS（不与主端 receiver 并存）
    e3 = Env("t29c", extra_env={"AGENTD_RESIDENT": "1",
                               "AGENTD_SESSION_NAME": "bot/zz-res-t29"})
    _mk_exts(e3)
    p3 = e3.start_wrap()
    try:
        ok("T29c sock 就位", e3.wait_sock())
        c3 = Client(e3.sock)
        ok("T29c 等首个 settled", c3.wait_event("agent_settled", 15) is not None)
        argv3 = e3.read_argv()
        ok("T29c resident 不注入子端扩展（主端 index.ts receiver 已覆盖自家信箱）",
           argv3 is not None and "-e" not in argv3, repr(argv3))
        c3.close()
    finally:
        e3.cleanup([p3])


def _mk_child_exts(e, names=("ask-user-child.ts", "receiver-child.ts")):
    """在临时树里造子端扩展文件（wrap 只判存在性 → 桦文件即可）。"""
    d = os.path.join(e.root, "assistant", ".pi", "extensions", "agentd")
    os.makedirs(d, exist_ok=True)
    for n in names:
        with open(os.path.join(d, n), "w") as f:
            f.write("export default function () {}\n")


def t30_ready_handshake():
    """T30 就绪握手（ 🔴0，P0 spawn 竞态）：wrap 与子端收件扩展之间的两枚标记
    （单点 proto.task_ready_path）——`init-ok`（wrap 写：初始投递已收口 → 子端据此开就绪门
    才开始 drain 自家 inbox）与 `recv-armed`（子端写：首次补扫完成 → wrap **有界**等它之后才进
    收敛监督）。缺这道握手 = 子端在 session_start 抢跑注入，两种现网形态：① prompt 被拒
    （stage=prompt_rejected、exit 1 秒死，有现网实证）；② 注入轮先跑完 → agent_settled
    被当任务收敛（exit 0 假成功、session.jsonl 永不落盘，实证）。
      a) 任务形态 + receiver-child 在场 → 两条路径经 env 传给 pi；标记在 prompt 被接受后才落盘
         （fake 侧 gate_open 晚于 prompt）；退出即清；session.jsonl 在场；
      b) 陈旧标记 spawn 前必清（否则骗开本代就绪门 → 复现抢跑）；
      c) 失败路径（prompt 被拒）不写 init-ok；
      d) 幂等跳过路径（resume 代）也写 init-ok（why 自证）；
      e) resident 形态不传 env、不等 arm（主端 receiver 行为逐字不变）；
      f) 子端永不回写 arm → wrap 有界等待后照常收敛（不假活）+ stderr WARN；
      g) receiver-child 缺失（不注入）→ 不传 env、不等 arm（存量形态零回归）。"""
    def gate_env(e):
        """有界重试读 gate_env flag：写入方 = fakepi_rpc.py（测试夹具）。
        写序 argv → gate_env → persona_env，wait_argv 不保证 gate_env 已落盘；
        文件存在但内容为空（写入方 open("w") 创建后尚未 write）= 撕裂读。
        修法：有界重试直到 JSON 可解析，超时返 None（由后续断言带 detail 报错）。"""
        p = os.path.join(e.flags, "gate_env")
        dl = time.time() + 10
        while time.time() < dl:
            if not os.path.exists(p):
                time.sleep(0.02)
                continue
            try:
                with open(p) as f:
                    return json.loads(f.read())
            except ValueError:
                time.sleep(0.02)
        return None

    def flag_mtime(e, name):
        p = os.path.join(e.flags, name)
        return os.stat(p).st_mtime if os.path.exists(p) else None

    def init_ok_doc(e):
        p = os.path.join(e.flags, "init_ok_doc")
        return open(p, encoding="utf-8").read() if os.path.exists(p) else ""

    def marks(e):
        return [proto.task_ready_path(e.root, e.name, k)
                for k in proto.TASK_READY_KINDS]

    # ---- a)+b) 健康路径（并预置陈旧标记验证 spawn 前必清）----
    e = Env("t30a", fake_mode="child_race",
            extra_env={"AGENTD_WRAP_ARM_TIMEOUT": "3"})
    _mk_child_exts(e)
    stale_ok, stale_arm = marks(e)
    os.makedirs(os.path.dirname(stale_ok), exist_ok=True)
    with open(stale_ok, "w") as f:
        f.write('{"why": "STALE-上一代残留"}\n')      # 骗门诱饶：不得被本代尊重
    with open(stale_arm, "w") as f:
        f.write('{"why": "STALE"}\n')
    t0 = time.time()
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=40)
        err = p.stderr.read().decode("utf-8", "replace")
        ge = gate_env(e)
        ok("T30a env 传两枚标记路径（单点 proto.task_ready_path）",
           ge is not None and ge["init_ok"] == stale_ok
           and ge["recv_armed"] == stale_arm, repr(ge))
        ok("T30a 收敛 exit 0（健康路径）", rc == 0, "rc=%s" % rc)
        ok("T30a session.jsonl 在场（必要非充分判据：exit 0 不足以证明执行过，硬判据 = 会话树含初始 prompt 的 user 事件，见 e2e.py S53）",
           os.path.exists(os.path.join(e.home, "session", "session.jsonl")))
        ok("T30a 时序：prompt 被接受早于子端开门（陈旧标记未骗开本代门）",
           flag_mtime(e, "prompt") is not None
           and flag_mtime(e, "gate_open") is not None
           and flag_mtime(e, "prompt") <= flag_mtime(e, "gate_open")
           and flag_mtime(e, "gate_bypassed") is None,
           repr([(n, flag_mtime(e, n)) for n in
                 ("prompt", "gate_open", "gate_bypassed")]))
        ok("T30a init-ok 由本次投递写入（why=prompt-accepted，非陈旧内容）",
           "prompt-accepted" in init_ok_doc(e)
           and "STALE" not in init_ok_doc(e), repr(init_ok_doc(e)[:200]))
        ok("T30a 子端已回写 arm（wrap 等到了才进收敛）",
           "recv-armed 在场" in err, err[-300:])
        ok("T30a 退出即清两枚标记（不留残留骗下一代）",
           not any(os.path.exists(m) for m in (stale_ok, stale_arm)),
           repr([m for m in (stale_ok, stale_arm) if os.path.exists(m)]))
        ok("T30a 全程有界（无假活）", time.time() - t0 < 30, "%.1fs" % (time.time() - t0))
        ok("T30a 健康路径不写诊断", not os.path.exists(
            os.path.join(e.home, "diagnosis.md")))
    finally:
        e.cleanup([p])

    # ---- c) 失败路径（prompt 被拒）不写 init-ok；陈旧标记仍被清 ----
    e2 = Env("t30c", fake_mode="reject")
    _mk_child_exts(e2)
    m_ok, m_arm = marks(e2)
    os.makedirs(os.path.dirname(m_ok), exist_ok=True)
    with open(m_ok, "w") as f:
        f.write('{"why": "STALE"}\n')
    p2 = e2.start_wrap()
    try:
        rc2 = p2.wait(timeout=30)
        ok("T30c prompt 被拒 → exit 1", rc2 == 1, "rc=%s" % rc2)
        ok("T30c 失败路径不写 init-ok（且陈旧标记已被清）",
           not os.path.exists(m_ok) and not os.path.exists(m_arm))
        diag = os.path.join(e2.home, "diagnosis.md")
        ok("T30c 诊断落盘 stage=prompt_rejected", os.path.exists(diag)
           and "prompt_rejected" in open(diag, encoding="utf-8").read())
    finally:
        e2.cleanup([p2])

    # ---- d) 幂等跳过路径（resume 代）也写 init-ok ----
    e3 = Env("t30d", fake_mode="child_race", preseed_user=True,
             extra_env={"AGENTD_WRAP_ARM_TIMEOUT": "3"})
    _mk_child_exts(e3)
    p3 = e3.start_wrap()
    try:
        # resume 代无初始 prompt → fake 不会自发起轮/收敛（真形态由子端注入起轮，非本组断言面）：
        # 只等有界地等标记落盘与子端开门，不等待退出。
        dl3 = time.time() + 20
        while time.time() < dl3 and not init_ok_doc(e3):
            time.sleep(0.05)
        ok("T30d 幂等跳过路径也写 init-ok（why 自证），子端门照样开",
           "resume-idempotent-skip" in init_ok_doc(e3)
           and flag_mtime(e3, "gate_open") is not None, repr(init_ok_doc(e3)[:200]))
        ok("T30d resume 代不写诊断（跳过投递不是失败）",
           not os.path.exists(os.path.join(e3.home, "diagnosis.md")))
    finally:
        e3.cleanup([p3])

    # ---- e) resident 形态：不传 env、不等 arm ----
    e4 = Env("t30e", fake_mode="idle_settle",
             extra_env={"AGENTD_RESIDENT": "1",
                        "AGENTD_SESSION_NAME": "bot/zz-t30",
                        "AGENTD_WRAP_ARM_TIMEOUT": "1"})
    _mk_child_exts(e4)
    p4 = e4.start_wrap()
    try:
        ok("T30e sock 就位", e4.wait_sock())
        e4.wait_argv()
        ge4 = gate_env(e4)
        ok("T30e resident 不传就绪门 env（主端 index.ts receiver 行为逐字不变）",
           ge4 is not None and ge4["init_ok"] == "" and ge4["recv_armed"] == "",
           repr(ge4))
        time.sleep(1.5)
        ok("T30e resident 不等 arm、不收敛（无子端握手方，pi 仍存活）",
           p4.poll() is None, "pi 应仍存活")
        ok("T30e resident 不写就绪标记（不参与握手）",
           not any(os.path.exists(m) for m in marks(e4)))
    finally:
        e4.cleanup([p4])

    # ---- f) 子端永不回写 arm → 有界等待后照常收敛（不假活）----
    e5 = Env("t30f", fake_mode="child_race",
             extra_env={"FAKE_CHILD_NO_ARM": "1", "AGENTD_WRAP_ARM_TIMEOUT": "1"})
    _mk_child_exts(e5)
    t5 = time.time()
    p5 = e5.start_wrap()
    try:
        rc5 = p5.wait(timeout=40)
        err5 = p5.stderr.read().decode("utf-8", "replace")
        ok("T30f 无 arm 仍收敛 exit 0（有界等待不拖死任务）", rc5 == 0, "rc=%s" % rc5)
        ok("T30f 超时留 WARN（不静默）", "未收到子端 recv-armed" in err5, err5[-300:])
        ok("T30f 等待有界（≤ arm_timeout + 宽松）",
           time.time() - t5 < 20, "%.1fs" % (time.time() - t5))
        ok("T30f session.jsonl 仍在场（收敛前注入已落树）",
           os.path.exists(os.path.join(e5.home, "session", "session.jsonl")))
    finally:
        e5.cleanup([p5])

    # ---- g) receiver-child 缺失 → 不传 env、不等 arm（存量形态零回归）----
    e6 = Env("t30g", fake_mode="child_race",
             extra_env={"FAKE_CHILD_NO_ARM": "1", "AGENTD_WRAP_ARM_TIMEOUT": "1"})
    _mk_child_exts(e6, names=("ask-user-child.ts",))  # 故意缺 receiver
    p6 = e6.start_wrap()
    try:
        rc6 = p6.wait(timeout=40)
        err6 = p6.stderr.read().decode("utf-8", "replace")
        ge6 = gate_env(e6)
        ok("T30g receiver-child 缺失 → 不传就绪门 env", 
           ge6 is not None and ge6["init_ok"] == "", repr(ge6))
        ok("T30g 不注入即不等 arm（无 WARN、无白等）",
           "未收到子端 recv-armed" not in err6, err6[-300:])
        ok("T30g 会话照常收口", rc6 in (0, 1), "rc=%s" % rc6)
    finally:
        e6.cleanup([p6])


def t31_ready_env_scrub():
    """T31 就绪门信号 env 的洗刷面（🔴1 后果①）：
    `AGENTD_WRAP_INIT_OK`/`AGENTD_WRAP_RECV_ARMED` 装的是 wrap 给**本次会话**的两枚标记绝对路径
    （身份/信号类，与 AGENTD_RESIDENT/DISPATCH_PROFILE 同族），必须只来自写者自身、不得继承：
    未进洗刷名单时它们被任务内每个孙进程继承 → 经 bash 工具 → `make` → `serviced/serviced.py` 带进被启动的
    服务，且嵌套 receiver 会拿外层任务的标记开门。
      a) 名单单点含两枚 + scrub_env 真洗掉（其余键保留）；
      d) 跳文件同源：三个调用方都 import 同一份名单（不另立副本）；
      e) 握手不受影响：spawn_pi() 在洗刷之后**显式**赋值两枚（源码顺序断言；端到端证据 = T30a）。"""
    import envscrub
    two = ("AGENTD_WRAP_INIT_OK", "AGENTD_WRAP_RECV_ARMED")
    # ---- a) 名单单点 + 洗刷行为 ----
    ok("T31a ENV_SCRUB_EXACT 含就绪门两枚信号 env（名单单一事实源）",
       set(two) <= envscrub.ENV_SCRUB_EXACT, sorted(envscrub.ENV_SCRUB_EXACT))
    polluted = {"AGENTD_WRAP_INIT_OK": "/tmp/synthetic/outer.init-ok",
                "AGENTD_WRAP_RECV_ARMED": "/tmp/synthetic/outer.recv-armed",
                "AGENT_SELF": "task/outer", "PATH": "/usr/bin", "HOME": "/h",
                "SOME_UNRELATED": "keep-me"}
    scrubbed = envscrub.scrub_env(base=polluted)
    ok("T31a scrub_env 洗掉两枚（继承链断开）",
       not any(k in scrubbed for k in two), sorted(scrubbed))
    ok("T31a 非身份键逐字保留（不过杀）",
       scrubbed.get("PATH") == "/usr/bin" and scrubbed.get("SOME_UNRELATED") == "keep-me",
       sorted(scrubbed))
    ok("T31a runner spawn 口径（strip_third_party=True）同样洗掉两枚",
       not any(k in envscrub.scrub_env(base=polluted, strip_third_party=True) for k in two))

    # ---- d) 跳文件同源（三个调用方 import 同一份名单，不另立副本）----
    ws = os.path.normpath(os.path.join(HERE, ".."))
    for rel in ("serviced/serviced.py", "agentd/runner.py",
                # 第三个调用方在 gitignored 的 w/ 整树里（`git ls-files w` = 0）→ 快照隔离跑时
                # 不在场；在场（真工作区）则照断，不在场显式记一条跳过（不静默、不当失败）。
                "w/ext/sessiond/proc.py"):
        p = os.path.join(ws, rel)
        if not os.path.isfile(p):
            ok("T31d %s 不在本快照内（w/ 整树 gitignored）→ 跳过该调用方同源断言" % rel, True)
            continue
        src = open(p, encoding="utf-8").read()
        ok("T31d %s import 名单单点 envscrub（不复制名单）" % rel,
           "import envscrub" in src and "ENV_SCRUB_EXACT = {" not in src, rel)

    # ---- e) 握手不受影响：spawn_pi() 在洗刷之后显式赋值 ----
    wrap_src = open(WRAP, encoding="utf-8").read()
    fn_at = wrap_src.index("    def spawn_pi(self):")
    fn_src = wrap_src[fn_at:fn_at + 2000]
    i_env = fn_src.index("env = dict(os.environ")
    i_ok = fn_src.index('env["AGENTD_WRAP_INIT_OK"]')
    i_arm = fn_src.index('env["AGENTD_WRAP_RECV_ARMED"]')
    ok("T31e spawn_pi() 先组 env 再**显式**赋两枚（不依赖继承 → 洗刷不影响握手）",
       0 <= i_env < i_ok and i_env < i_arm, (i_env, i_ok, i_arm))
    ok("T31e 两枚只在 child_recv_injected（任务形态）分支赋值（resident 不参与）",
       "if self.child_recv_injected:" in fn_src
       and fn_src.index("if self.child_recv_injected:") < i_ok, fn_src[:400])


def t_fake_settled_inflight():
    """T_FAKE_SETTLED 假 settled 免疫：agent_start 后发假 agent_settled
    （inflight>0，无配对 agent_end），竞态窗（0.5s）过期后 wrap 不得收敛；
    等 agent_end（inflight→0）+ 真 agent_settled 后才收敛 exit 0。
    修前红：假 settled 后 0.5s 竞态窗过期 → 收敛 → 关 stdin → fakepi EOF → wrap 早退；
    修后绿：inflight>0 → 跳过收敛判定 → 等真 settled（1.0s 后）→ 收敛。"""
    e = Env("t_fake_settled", fake_mode="fake_settled_inflight")
    p = e.start_wrap()
    try:
        ok("T_FAKE_SETTLED sock 就位", e.wait_sock())
        # 等假 settled 标记（fakepi 发完假 settled 后落此标记）
        fake_flag = os.path.join(e.flags, "fake_settled_emitted")
        real_flag = os.path.join(e.flags, "real_settled_emitted")
        dl = time.time() + 15
        while time.time() < dl and not os.path.exists(fake_flag):
            time.sleep(0.05)
        ok("T_FAKE_SETTLED 假 settled 已发出", os.path.exists(fake_flag))
        t_fake = time.time()
        # 关键断言：假 settled 后 0.8s（> SETTLE_WINDOW 0.5s，< 真 settled 1.0s），
        # wrap 仍存活（未收敛）。修前：竞态窗过期 → 收敛 → 关 stdin → fakepi EOF → wrap 退出。
        time.sleep(0.8)
        alive_at_08 = p.poll() is None
        ok("T_FAKE_SETTLED 假 settled 后 0.8s wrap 仍存活（inflight>0 免疫生效）",
           alive_at_08,
           "rc=%s（修前红：假 settled 触发收敛→早退于真 settled 之前）" % p.poll())
        # 等 wrap 退出（真 settled 后收敛）
        rc = p.wait(timeout=20)
        t_exit = time.time()
        ok("T_FAKE_SETTLED 真 settled 后收敛 exit 0", rc == 0, "rc=%s" % rc)
        ok("T_FAKE_SETTLED 无诊断", e.read_diag() is None,
           (e.read_diag() or "")[:200])
        ok("T_FAKE_SETTLED 真 settled 标记在场", os.path.exists(real_flag))
        # 修后绿：wrap 退出时间 ≥ 假 settled 后 1.0s（等真 settled）
        elapsed = t_exit - t_fake
        ok("T_FAKE_SETTLED wrap 退出耗时 ≥ 0.9s（等真 settled，非早退）",
           elapsed >= 0.9, "elapsed=%.2fs（修前红：< 0.8s 早退）" % elapsed)
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()
        shutil.rmtree(e.root, ignore_errors=True)


# ---------- 末轮模型错误档（：exit 0 假成功收口） ----------
#
# 驱动方式 = **预置 session.jsonl + 既有 fake 模式 `idle_settle`**（boot 0.3s 后自发
# agent_settled ⇒ 收敛路径走真流程；预置首条 user 使 deliver_prompt 走幂等跳过，且
# idle_settle 不调 fakepi 的 race_persist ⇒ 预置文件不会被覆写）。**不改 fakepi_rpc.py**。
# 条目形状照现场取证（agents/task/uhqkbf/session/session.jsonl 末条）与 pi 本体声明
# （pi-ai/dist/types.d.ts 的 StopReason 值域）构造，不凭记忆手写。

TS0 = "2026-09-15T05:31:18.122Z"     # 会话头/首条 user
TS1 = "2026-09-15T05:59:01.062Z"     # 中间轮（toolUse + toolResult）
TS2 = "2026-09-15T06:04:13.513Z"     # 末条（ 现场同值）

USAGE_ZERO = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0,
              "totalTokens": 0,
              "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0,
                       "total": 0}}
USAGE_OK = {"input": 742, "output": 1381, "cacheRead": 197632, "cacheWrite": 0,
            "reasoning": 10, "totalTokens": 199755,
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0,
                     "total": 0}}


def _sess_entry(eid, parent, role, blocks=None, ts=TS0, **msg_extra):
    """一条会话 jsonl 条目（外层 type/id/parentId/timestamp + message）。"""
    msg = {"role": role,
           "content": [] if blocks is None else blocks,
           "timestamp": ts}
    msg.update(msg_extra)
    return {"type": "message", "id": eid, "parentId": parent,
            "timestamp": ts, "message": msg}


def _seed_session(home, entries):
    p = os.path.join(home, "session", "session.jsonl")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    return p


def _error_tail_entries(last_stop="error"):
    """ 现场形态：user → assistant(toolUse) → toolResult → assistant(stopReason=<last_stop>、
    usage 全零、只有 thinking、带 errorMessage)。首条 user 使 wrap 跳过初始投递。"""
    return [
        {"type": "session", "id": "e0", "timestamp": TS0},
        _sess_entry("e1", "e0", "user", [{"type": "text", "text": "# fake prompt"}],
                    ts=TS0),
        _sess_entry("e2", "e1", "assistant",
                    [{"type": "thinking", "thinking": "let me work"},
                     {"type": "toolCall", "id": "c1", "name": "write"}],
                    ts=TS1, stopReason="toolUse", usage=USAGE_OK, rawStopReason="tool_calls",
                    provider="llm-router", api="openai-completions", model="executor"),
        _sess_entry("e3", "e2", "toolResult", [{"type": "text", "text": "ok"}], ts=TS1,
                    toolCallId="c1", toolName="write", isError=False),
        _sess_entry("e4", "e3", "assistant",
                    [{"type": "thinking",
                      "thinking": "Now write the report. It must be thorough with "
                                  "evidence. Let me draft it carefully."}],
                    ts=TS2, stopReason=last_stop, usage=USAGE_ZERO,
                    errorMessage="upstream stream interrupted",
                    provider="llm-router", api="openai-completions", model="executor"),
    ]


def t38_model_error_no_report():
    """T38 档 (i)（ 核心形态 =）：末条 assistant stopReason=error + usage 全零 +
    只有 thinking，report.md 不在场 ⇒ 收敛判据虽全满足，**不算完成**：exit 1 +
    diagnosis stage=model_error_stopreason + 证据小节（末条 ts/stopReason/usage 全零/
    只有 thinking/交付物自查）+ WARN 落 wrap stderr。修前：exit 0 + 无 diagnosis（假成功）。"""
    e = Env("t38", fake_mode="idle_settle")
    sess = _seed_session(e.home, _error_tail_entries())
    p = e.start_wrap()
    try:
        ok("T38 sock 就位", e.wait_sock())
        rc = p.wait(timeout=20)
        err = p.stderr.read().decode("utf-8", "replace")
        ok("T38 档 (i) 退出码 1（不再 exit 0 假成功）", rc == 1, "rc=%s" % rc)
        diag = e.read_diag() or ""
        ok("T38 diagnosis 在场且 stage=model_error_stopreason",
           "stage: model_error_stopreason" in diag, diag[:400])
        ok("T38 diagnosis 记 exitcode 1", "- exitcode: 1" in diag, diag[:400])
        ok("T38 证据含末条事件 ts", TS2 in diag, diag[-900:])
        ok("T38 证据含 stopReason: error", "stopReason: error" in diag, diag[-900:])
        ok("T38 证据含 usage 全零事实（逐键）",
           "usage 全零: True" in diag and "input=0" in diag
           and "totalTokens=0" in diag, diag[-900:])
        ok("T38 证据含「只有 thinking 而无 text/toolCall」事实",
           "只有 thinking 而无 text/toolCall: True" in diag, diag[-900:])
        ok("T38 证据含 errorMessage（上游失败原因）",
           "upstream stream interrupted" in diag, diag[-900:])
        ok("T38 证据含交付物自查（report.md 缺位）",
           "交付物自查: report.md 不在场" in diag, diag[-900:])
        ok("T38 证据含尾读窗口（有界读自证）", "尾读窗口: 末 " in diag, diag[-900:])
        ok("T38 WARN 落 wrap stderr", "末轮模型错误" in err and "退出码 1" in err,
           err[-500:])
        ok("T38 不是异常退出档（收敛仍成立，只是不算完成）",
           "pi_exited_unexpected" not in diag
           and "任务收敛完成（exit 0）" not in err, diag[:300] + err[-300:])
        ok("T38 预置会话文件未被改写（fake 不覆写）", os.path.getsize(sess) > 0)
        ok("T38 report.md 仍未在场（新档不代写交付物）",
           not os.path.exists(os.path.join(e.home, "report.md")))
    finally:
        e.cleanup([p])


def t39_model_error_with_report():
    """T39 档 (ii)（假阳性面）：末条 stopReason=error 但 report.md **非空在场** ⇒ 仍 exit 0
    （以 pi 退出码为准）+ WARN + 信息性 diagnosis（stage=model_error_stopreason_delivered、
    exitcode 0、自证「不改判定」）——已交付的任务不被标成失败。"""
    e = Env("t39", fake_mode="idle_settle")
    _seed_session(e.home, _error_tail_entries())
    report = os.path.join(e.home, "report.md")
    with open(report, "w", encoding="utf-8") as f:
        f.write("# report\n\n交付物在场（末轮遭遇模型错误）。\n")
    p = e.start_wrap()
    try:
        ok("T39 sock 就位", e.wait_sock())
        rc = p.wait(timeout=20)
        err = p.stderr.read().decode("utf-8", "replace")
        ok("T39 档 (ii) 退出码 0（交付物在场 ⇒ 不改判定）", rc == 0, "rc=%s" % rc)
        ok("T39 WARN 落 wrap stderr（点名 exit 0）",
           "末轮模型错误" in err and "exit 0" in err, err[-500:])
        diag = e.read_diag() or ""
        ok("T39 信息性 diagnosis 在场且 stage=model_error_stopreason_delivered",
           "stage: model_error_stopreason_delivered" in diag, diag[:400])
        ok("T39 diagnosis 记 exitcode 0", "- exitcode: 0" in diag, diag[:400])
        ok("T39 diagnosis 自证「信息性、不改判定」",
           "信息性记录、不改判定" in diag, diag[:600])
        ok("T39 证据含交付物自查（report.md 非空在场）",
           "交付物自查: report.md 非空在场" in diag, diag[-900:])
        ok("T39 证据仍记 stopReason: error 与 usage 全零",
           "stopReason: error" in diag and "usage 全零: True" in diag, diag[-900:])
        ok("T39 report.md 内容未被改写",
           "交付物在场" in open(report, encoding="utf-8").read())
    finally:
        e.cleanup([p])


def t39b_model_error_zero_byte_report():
    """T39b 档 (i) 的「非空」判据回归网（前鉴形态 = exitcode=0 而 report.md **零字节**）：
    零字节与纯空白两种 report.md 都视同缺报告 ⇒ 仍走档 (i) exit 1。若判据退化成「存在性」则本组红。"""
    for tag, body in (("zero", ""), ("blank", "   \n\n  \t\n")):
        e = Env("t39b-" + tag, fake_mode="idle_settle")
        _seed_session(e.home, _error_tail_entries())
        with open(os.path.join(e.home, "report.md"), "w", encoding="utf-8") as f:
            f.write(body)
        p = e.start_wrap()
        try:
            ok("T39b(%s) sock 就位" % tag, e.wait_sock())
            rc = p.wait(timeout=20)
            diag = e.read_diag() or ""
            ok("T39b(%s) 零字节/纯空白 report ⇒ 仍 exit 1（非空判据生效）" % tag,
               rc == 1, "rc=%s" % rc)
            ok("T39b(%s) stage=model_error_stopreason（不是 _delivered）" % tag,
               "stage: model_error_stopreason\n" in diag
               and "model_error_stopreason_delivered" not in diag, diag[:400])
            ok("T39b(%s) 交付物自查点名「零字节/纯空白视同缺报告」" % tag,
               "零字节/纯空白" in diag, diag[-900:])
        finally:
            e.cleanup([p])


def t40_normal_stop_no_report():
    """T40 验收 4 的 (c)：末条**正常**（stopReason 为现值 stop ∨ 末条是 toolResult 而其前
    assistant 为 toolUse）+ 无 report.md ⇒ **行为与现行逐字一致**：exit 0 且无 diagnosis。
    即「exit 0 但无 report」形态未被改判（那是通知文案层 warn=no_report 的事，本批不扩射程）。"""
    normal_stop = _error_tail_entries(last_stop="stop")
    normal_stop[-1]["message"]["usage"] = USAGE_OK          # 正常完成轮：usage 非零、无 errorMessage
    normal_stop[-1]["message"].pop("errorMessage", None)
    normal_stop[-1]["message"]["content"] = [{"type": "text", "text": "done"}]
    for tag, entries in (("stop", normal_stop),
                         ("tooluse_tail", _error_tail_entries()[:4])):
        e = Env("t40-" + tag, fake_mode="idle_settle")
        _seed_session(e.home, entries)
        p = e.start_wrap()
        try:
            ok("T40(%s) sock 就位" % tag, e.wait_sock())
            rc = p.wait(timeout=20)
            err = p.stderr.read().decode("utf-8", "replace")
            ok("T40(%s) 末条正常 + 无 report ⇒ 仍 exit 0（未被改判）" % tag,
               rc == 0, "rc=%s" % rc)
            ok("T40(%s) 无 diagnosis（与现行行为逐字一致）" % tag,
               e.read_diag() is None, (e.read_diag() or "")[:300])
            ok("T40(%s) 无模型错误 WARN（未误触发）" % tag,
               "末轮模型错误" not in err, err[-400:])
            ok("T40(%s) 收敛路径逐字不变（日志仍是「任务收敛完成（exit 0）」）" % tag,
               "任务收敛完成（exit 0）" in err, err[-400:])
        finally:
            e.cleanup([p])


def t41_tail_unreadable_failsoft():
    """T41 验收 3 的 fail-soft：拿不到证据（jsonl 不存在 / 全是不可解析垃圾行 / 末行半截 JSON
    且窗内无 assistant）⇒ **不得因此把正常任务判失败**：exit 0 + 无 diagnosis + 一行 WARN。"""
    garbage = ["NOT-JSON-AT-ALL\n", "{\"type\": \"message\", \"message\":\n",
               "\x00\x01binary-junk\n"]
    forms = {
        "absent": None,                       # jsonl 不存在
        "garbage": garbage,                   # 全不可解析
        "halfline": garbage + ['{"type": "message", "message": {"role": "assis'],
    }
    for tag, entries in forms.items():
        e = Env("t41-" + tag, fake_mode="idle_settle")
        if entries is not None:
            p_ = os.path.join(e.home, "session", "session.jsonl")
            with open(p_, "w", encoding="utf-8") as f:
                f.write("".join(x if x.endswith("\n") else x + "\n" for x in entries))
        p = e.start_wrap()
        try:
            ok("T41(%s) sock 就位" % tag, e.wait_sock())
            rc = p.wait(timeout=20)
            err = p.stderr.read().decode("utf-8", "replace")
            ok("T41(%s) fail-soft ⇒ exit 0（不判失败）" % tag, rc == 0, "rc=%s" % rc)
            ok("T41(%s) 无 diagnosis" % tag, e.read_diag() is None,
               (e.read_diag() or "")[:300])
            ok("T41(%s) 有一行 WARN（不静默）" % tag, "WARN" in err, err[-400:])
        finally:
            e.cleanup([p])


def t41b_tail_window_truncated():
    """T41b 有界读的尾窗边界：文件 > SESSION_TAIL_BYTES（首部填一条巨型合法条目）⇒ 尾窗首行被
    上界截断而丢弃，仍能命中末条 assistant ⇒ 档 (i) exit 1（证明有界读不靠整文件、也不因截断漏判）。"""
    e = Env("t41b", fake_mode="idle_settle")
    entries = _error_tail_entries()
    pad = {"type": "message", "id": "pad", "parentId": "e0", "timestamp": TS0,
           "message": {"role": "user", "content": "P" * (300 * 1024)}}
    _seed_session(e.home, [entries[0], pad] + entries[1:])
    size = os.path.getsize(os.path.join(e.home, "session", "session.jsonl"))
    p = e.start_wrap()
    try:
        ok("T41b 文件确 > 尾读上界（%dB > 262144B）" % size, size > 262144, str(size))
        rc = p.wait(timeout=20)
        diag = e.read_diag() or ""
        ok("T41b 尾窗截断下仍命中末条 ⇒ exit 1", rc == 1, "rc=%s" % rc)
        ok("T41b stage=model_error_stopreason",
           "stage: model_error_stopreason" in diag, diag[:300])
        ok("T41b 证据记尾读窗口 = 上界值（有界读自证）",
           "尾读窗口: 末 262144B / 文件 %dB" % size in diag, diag[-900:])
    finally:
        e.cleanup([p])


def t42_resident_model_error_untouched():
    """T42 resident 形态不适用新档：常驻会话无 report.md 交付语义，末条 stopReason=error 也
    只透传 pi 退出码、不写 diagnosis（run() 里 resident 分支在收敛分支之前返回）。"""
    e = Env("t42", fake_mode="selfkill",
            extra_env={"AGENTD_RESIDENT": "1",
                       "AGENTD_SESSION_NAME": "bot/zz-resident-t42"})
    _seed_session(e.home, _error_tail_entries())
    p = e.start_wrap()
    try:
        ok("T42 sock 就位", e.wait_sock())
        rc = p.wait(timeout=20)
        err = p.stderr.read().decode("utf-8", "replace")
        ok("T42 resident 退出码透传（7，不受新档影响）", rc == 7, "rc=%s" % rc)
        ok("T42 无 diagnosis（resident 不经模型错误闸）", e.read_diag() is None,
           (e.read_diag() or "")[:300])
        ok("T42 无模型错误 WARN", "末轮模型错误" not in err, err[-400:])
    finally:
        e.cleanup([p])


def t43_heartbeat_prompt_anchor_lines():
    """T43 定时触发面 prompt.md 的两个锚定行与 core.ts::buildPromptMd **同源**（防漂断言）。

    定时触发面（`heartbeats/` 注册表 → 同目录 `register.py`）登记的任务，其 prompt.md
    由 registrar 自写、不经 `buildPromptMd`（agentctl 无 prompt 渲染动词），而 `caps/executor` 的
    「本次任务参数」锚定句（下文所有「任务目录」「本任务 taskId」均指该行）与「分级门禁」标记都要求
    这两行在 prompt **开头** ⇒ 缺行会让基线那两处引用对该形态悬空。
    镜像允许（python 重实现 = 第三份副本、node 跑 TS = 给脚本加运行时依赖），但**没有断言的镜像**
    才是错 —— 漂移会在「executor 基线改了锚定句措辞」那天静默发生。本用例钉四面：① token 集同源
    （任一侧改名即红）；② 位置在开头（两行先于「## 需求描述」）；③ 路径按 portablePath 同款口径
    渲染（$HOME 内 → 波浪号）；④ 参数行点名执行机 env。
    """
    reg_path = os.path.join(HERE, "..", "heartbeats", "register.py")
    core_path = os.path.join(HERE, "..", "assistant", ".pi", "extensions",
                             "agentd", "core.ts")
    with open(reg_path, encoding="utf-8") as f:
        reg = f.read()
    with open(core_path, encoding="utf-8") as f:
        core = f.read()
    tokens = ["任务分级", "【本次任务参数】", "taskId", "任务目录", "report.md"]

    # core.ts 侧：buildPromptMd 的函数体区间（两行模板都在其中）
    i = core.index("export function buildPromptMd")
    seg = core[i:i + 6000]
    for t in tokens:
        ok("T43 core.ts::buildPromptMd 含 token %s" % t, t in seg,
           "改名/删除 ⇒ 同批改 heartbeats/register.py 与本清单（否则该形态的锚定行会静默漂）")

    # register.py 侧：build_prompt 的函数体区间（parts 列表即 prompt 落盘顺序）
    j = reg.index("def build_prompt(")
    k = reg.index("\ndef ", j + 1)
    block = reg[j:k]
    for t in tokens:
        ok("T43 register.py 的 build_prompt 含 token %s（与 buildPromptMd 同源）" % t,
           t in block, "函数体内缺该 token ⇒ 该形态任务的 prompt.md 少一个锚定字段")

    need = block.index('"## 需求描述"')
    ok("T43 分级行在需求描述之前（= prompt 开头）",
       block.index("任务分级") < need, "caps/executor 的分级门禁按「任务头部」标记对号")
    ok("T43 参数行在需求描述之前（= prompt 开头）",
       block.index("【本次任务参数】") < need,
       "caps/executor 逐字依赖「任务 prompt **开头**的『本次任务参数』行」")
    ok("T43 路径按 portablePath 同款口径（$HOME 内 → 波浪号形态）",
       "portable(" in block and "def portable(" in reg,
       "跨机可移植面：host≠登记机的件（如 host=mac 的产线件）拿到登记机绝对路径会不存在")
    ok("T43 参数行点名执行机 env $AGENT_HOME", "`$AGENT_HOME`" in block,
       "缺括注 ⇒ 执行者拿到波浪号路径却不知道权威现值在哪")


def t45_retired_marker_zero_reflow():
    """T45 已退役的 `DISPATCH_HEARTBEAT` 标记零回流（提交期钉桩，纯读文件、不起子进程）。

    定时触发面改为「一枚 timer 一件事」后，它登记的任务是叶子（探针件只读、产线件只产自己那份产物），
    该标记的全部消费面同批退役 = 递归守卫豁免（core.ts recursionGuardReason）、主端装配豁免
    （index.ts task-gate）、`send_message` 让位判定、洗刷名单（agentd/envscrub.py）、协议惯例条
    （agentd/agent-file-protocol.md）、人格例外句（caps/executor）。
      a) 洗刷名单不含该枚（名单是枚名制、只收在产标记：死键留着会让「名单 = 在产身份标记」失真）；
      b) 机制面零命中（core.ts / index.ts / envscrub.py / agent-file-protocol.md）；
      c) 注册表与登记层零命中（heartbeats/**）；
      d) 人格面零命中（bots/caps/*/prompt.md）；
      e) registrar 的 spec.command 与 core.ts buildSpawnCommand 同形态（无 env 前缀）。
    豁免**行为**（残留 env 副本不放行、不装配主端）由 ext 套件 `agentd-ext.test.mjs` 的
    recursionGuardReason / task-gate / dispatch guard 三组钉住；本组只钉「文本零回流」。
    """
    import envscrub
    ws = os.path.normpath(os.path.join(HERE, ".."))
    key = "DISPATCH_HEARTBEAT"

    def _read(*rel):
        with open(os.path.join(ws, *rel), encoding="utf-8") as f:
            return f.read()

    # ---- a) 洗刷名单：只收在产标记 ----
    ok("T45a ENV_SCRUB_EXACT 不含已退役标记（名单 = 在产身份标记）",
       key not in envscrub.ENV_SCRUB_EXACT, sorted(envscrub.ENV_SCRUB_EXACT))
    ok("T45a 在产的同族前缀标记仍在名单（不误杀）",
       "AGENTD_RESIDENT" in envscrub.ENV_SCRUB_EXACT,
       sorted(envscrub.ENV_SCRUB_EXACT))

    # ---- b) 机制面 ----
    for rel in (("assistant", ".pi", "extensions", "agentd", "core.ts"),
                ("assistant", ".pi", "extensions", "agentd", "index.ts"),
                ("agentd", "envscrub.py"),
                ("agentd", "agent-file-protocol.md")):
        txt = _read(*rel)
        ok("T45b 机制面零命中 %s" % "/".join(rel), key not in txt,
           "命中 ⇒ 豁免面被加回（须同批改回叶子语义并扩 ext 套件断言）")

    # ---- c) 登记层与注册表 ----
    for pat in (("heartbeats",),):
        for root, _dirs, files in os.walk(os.path.join(ws, *pat)):
            for fn in sorted(files):
                if not fn.endswith((".py", ".md", ".json")):
                    continue
                fp = os.path.join(root, fn)
                with open(fp, encoding="utf-8") as f:
                    txt = f.read()
                ok("T45c 登记层零命中 %s" % os.path.relpath(fp, ws), key not in txt,
                   "命中 ⇒ 登记层重新声明了已退役的形态标记")

    # ---- d) 人格面 ----
    caps = os.path.join(ws, "bots", "caps")
    for name in sorted(os.listdir(caps)):
        pp = os.path.join(caps, name, "prompt.md")
        if not os.path.isfile(pp):
            continue
        with open(pp, encoding="utf-8") as f:
            txt = f.read()
        ok("T45d 人格面零命中 caps/%s" % name, key not in txt,
           "命中 ⇒ 人格面留了机制面已不成立的例外句（照条文办事的模型会去派任务并被 guard 拦）")

    # ---- e) spec.command 形态同源（无 env 前缀）----
    reg = _read("heartbeats", "register.py")
    core = _read("assistant", ".pi", "extensions", "agentd", "core.ts")
    m = (re.search(r"^COMMAND = '(.+)'", reg, re.M)
         or re.search(r'^COMMAND = "(.+)"', reg, re.M))
    ok("T45e registrar 的 COMMAND 常量可解析", m is not None, reg[:200])
    if m:
        cmd = m.group(1)
        ok("T45e spec.command 与 buildSpawnCommand 同形态（无 env 前缀）",
           cmd in core and not re.match(r"^[A-Z][A-Z0-9_]*=", cmd), repr(cmd))


def _stderr_drain_until(p, needle, timeout=10):
    """kill **前**在活进程的 stderr 上有界等 needle 出现，返回这期间读到的文本累积。

    为什么需要它（T46a 的顺序竞态）：wrap 的 `初始 prompt 已投递并被接受` 写在
    `init_waiter.wait()` 返回之后（= 收到子端回执），而测试原先只等 fake pi 的 prompt
    flag（= 子端**收到** prompt 的更早时刻）⇒ 负载下组杀可早于该行写出，杀后读拿不到它。
    修在测试面：把观测点从「杀后读尾文本」挪到「杀前已等到期望行」，杀后读保留作兜底。

    只用 `os.read(fd)`（不经 p.stderr 的缓冲读）⇒ 随后 `_stderr_after_kill` 的
    `p.stderr.read()` 仍能读到管道里剩余部分：两段拼接无丢失、无重复。
    needle 出现 ∨ EOF（写端已关）∨ 超时即返回；有界，绝不无条件等。"""
    buf = b""
    fd = p.stderr.fileno()
    dl = time.time() + timeout
    while needle not in buf.decode("utf-8", "replace"):
        left = dl - time.time()
        if left <= 0:
            break
        r, _, _ = select.select([fd], [], [], min(left, 0.2))
        if not r:
            continue
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break
        if not chunk:                     # EOF：进程已死、写端关闭
            break
        buf += chunk
    return buf.decode("utf-8", "replace")


def _stderr_after_kill(p, timeout=10):
    """resident 形态（不收敛）读 wrap 自身 stderr 的唯一安全形态：先杀完进程组再 read，
    否则 read() 无限阻塞 = 假活（同 T36d 的既有处置）。返回文本（不可读时返回原因串）。
    断言的行可能写在「测试已观测到子端标记」之后（见 `_stderr_drain_until`）⇒ 调用方须先
    在杀前等到期望行，本函数只作兜底（两段文本合并后判定）。"""
    try:
        p.wait(timeout=timeout)
        return p.stderr.read().decode("utf-8", "replace")
    except Exception as ex:
        return "(stderr 不可读: %r)" % (ex,)


def t46_resident_prompt_delivery():
    """T46 resident 初始引导两态：blank 新代 spawn 时，声明源带 prompt.md ⇒
    真投递（会话型常驻 bot 不再停摆等信唤醒）；无 prompt.md ⇒ 既有裸启动路径不变（设计
 决策 6「初始引导可选」）。

    断言取**日志文本**而不是 init-ok 标记：`write_init_ok()` 首行即 `if self.resident: return`
    ⇒ resident 形态两态都不落 init-ok（不参与子端就绪握手），reason 串只在 wrap 自身 stderr
    （生产 = run/logs/agentd.log）可见。"""
    # ---- a) 有 prompt.md ⇒ 初始投递发生，reason ≠ resident-bare-start ----
    e = Env("t46a", fake_mode="hang_settle",
            extra_env={"AGENTD_RESIDENT": "1",
                       "AGENTD_SESSION_NAME": "bot/zz-resident-t46a"})
    p = e.start_wrap()
    live = ""
    try:
        ok("T46a sock 就位", e.wait_sock())
        ok("T46a pi 子进程已启动（argv 快照在场）", e.wait_argv())
        flag = os.path.join(e.flags, "prompt")
        dl = time.time() + 15          # 有界等投递标记（绝不无条件等）
        while time.time() < dl and not os.path.exists(flag):
            time.sleep(0.05)
        ok("T46a 初始引导已投递（fake 收到 prompt）", os.path.exists(flag), flag)
        ok("T46a 无诊断（投递成功不误报）", e.read_diag() is None,
           (e.read_diag() or "")[:200])
        iok = proto.task_ready_path(e.root, e.name, "init-ok")
        ok("T46a resident 不落 init-ok 标记（reason 只在日志面）",
           not os.path.exists(iok), iok)
        # 期望的日志行晚于上面那枚 flag（wrap 要收到子端回执才写）⇒ 杀前先在活进程上等到它
        live = _stderr_drain_until(p, "初始 prompt 已投递并被接受")
    finally:
        e.cleanup([p])
    err = live + _stderr_after_kill(p)   # 杀前累积 + 杀后兜底（两段拼接，无丢失无重复）
    ok("T46a 日志 reason = 已投递并被接受（不是 resident-bare-start）",
       "初始 prompt 已投递并被接受" in err
       and "resident-bare-start" not in err and "裸启动" not in err,
       err[-400:])

    # ---- b) 无 prompt.md ⇒ 既有裸启动断言仍绿 ----
    e = Env("t46b", fake_mode="hang_settle", with_prompt=False,
            extra_env={"AGENTD_RESIDENT": "1",
                       "AGENTD_SESSION_NAME": "bot/zz-resident-t46b"})
    p = e.start_wrap()
    try:
        ok("T46b sock 就位", e.wait_sock())
        ok("T46b pi 子进程已启动（argv 快照在场）", e.wait_argv())
        time.sleep(1.0)                 # 同 T10：投递分支在 spawn 后即刻走过，短等即可判否
        ok("T46b 裸启动存活（不诊断不退出）", p.poll() is None, "rc=%s" % p.poll())
        ok("T46b fake 未收到 prompt", not os.path.exists(
            os.path.join(e.flags, "prompt")))
        ok("T46b 无诊断", e.read_diag() is None, (e.read_diag() or "")[:200])
        iok = proto.task_ready_path(e.root, e.name, "init-ok")
        ok("T46b resident 裸启动同样不落 init-ok 标记", not os.path.exists(iok), iok)
    finally:
        e.cleanup([p])
    err = _stderr_after_kill(p)
    ok("T46b 日志 reason = 裸启动（初始引导可选）、无投递行",
       "resident：无 prompt.md，裸启动（初始引导可选）" in err
       and "初始 prompt 已投递并被接受" not in err, err[-400:])


# spec.command 的 env 前缀是身份标记进入会话的**唯一合法通道**（runner 先洗刷、再由 bash -c
# 执行命令串 ⇒ 前缀注入发生在洗刷之后）。因此每一枚前缀键都必须在洗刷名单里：漏列 ⇒ 它从该
# 会话的每个孙进程继承下去（bash 工具 → make → serviced/serviced.py → 被启动的服务），形态 =
# 身份标记事故形态（若被启动的是 agentd，该标记的语义对它 spawn 的每个任务全网生效、无告警）。
# 本组 = **提交期钉桩**
# （零运行时守卫：名单是枚名制、同前缀族里住着配置旋钮 ⇒ 不能按前缀洗，见 envscrub.py）。
_CMD_PREFIX_SOURCES = (
    # (相对工作区根的 glob, 说明)。不在场 = 该源不在本快照内（w/ 整树 gitignored、pi-wrap
    # 单独 checkout）⇒ 显式记一条跳过，不静默、不当失败。
    ("bots/daemon/*/spec.json", "守护型/常驻 bot 的被追踪声明源"),
    ("heartbeats/register.py", "定时触发面的登记脚本（spec.command 常量）"),
    ("w/ext/sessiond/proc.py", "create_bot 的 spec.command 模板"),
)
# `KEY=VAL KEY=VAL … exec|python3|bash` 形态的前缀键（值可含引号/`$`/`{}`）
_CMD_ENV_PREFIX_RE = re.compile(r"((?:\b[A-Z][A-Z0-9_]*=\S+[ \t]+)+)(?:exec|python3|bash)\b")


def _cmd_prefix_keys(text):
    """从命令/脚本文本里取全部 `KEY=VAL … exec` 形态的 env 前缀键名。"""
    out = []
    for m in _CMD_ENV_PREFIX_RE.finditer(text):
        for kv in m.group(1).split():
            out.append(kv.split("=", 1)[0])
    return out


def t47_spec_command_env_scrub():
    """T47 `spec.command` env 前缀键 ⊆ 洗刷名单（提交期钉桩）：
      a) 扫到的前缀键集合含已知四枚（防正则失配 ⇒ 扫到 0 枚的假绿）；
      b) 逐枚断言 scrub_env 真洗掉（漏列 ⇒ 红在提交前，不红在事故里）。"""
    import envscrub
    ws = os.path.normpath(os.path.join(HERE, ".."))
    found = {}
    for pat, why in _CMD_PREFIX_SOURCES:
        hits = sorted(glob.glob(os.path.join(ws, pat)))
        if not hits:
            ok("T47a %s 不在本快照内（%s）→ 跳过该源" % (pat, why), True)
            continue
        for h in hits:
            with open(h, encoding="utf-8") as f:
                for k in _cmd_prefix_keys(f.read()):
                    found.setdefault(k, set()).add(os.path.relpath(h, ws))
    if not found:
        ok("T47 三个声明源均不在本快照内 → 本组跳过（无断言可跑）", True)
        return
    known = {"DISPATCH_PROFILE", "AGENTD_RESIDENT", "AGENTD_SESSION_NAME"}
    ok("T47a 扫到已知三枚前缀键（正则未失配、非假绿）", known <= set(found),
       sorted(found))
    for k in sorted(found):
        scrubbed = envscrub.scrub_env(base={k: "1", "PATH": "/usr/bin",
                                            "SOME_UNRELATED": "keep-me"})
        ok("T47b 前缀键 %s 被洗刷名单覆盖（源 %s）"
           % (k, ",".join(sorted(found[k]))),
           k not in scrubbed and scrubbed.get("SOME_UNRELATED") == "keep-me",
           sorted(scrubbed))


LOADER_REL = os.path.join("pi-core", "agent", "extensions", "profile-loader.ts")
PERSONA_FLAGS = ("--append-system-prompt", "--skill", "-t", "-xt", "--model", "--provider")


def _mk_loader(root, text="export default function () {}\n"):
    """在临时树建人格注入层扩展（`pi-core/agent/extensions/profile-loader.ts` = 自动发现面，
    生产由 wrap 另拼一个 `-e` 钉装载序）。返回其绝对路径。"""
    p = os.path.join(root, LOADER_REL)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    return p


def t48_persona_emission():
    """T48 人格面的发射契约：**wrap 不装配人格**（解析在 persona.py、注入在 profile-loader 扩展），
    它对人格面只有两个动作 —— ① 把注入层扩展 `-e` 进去；② 把注入层要读的输入 env 透传下去。
      a) 注入层在场 ⇒ 恰一个 `-e <loader>`，且**零**人格 flag（--append-system-prompt/--skill/
         -t/-xt/--model/--provider 全部不发）、也不发 context-compaction 的 `-e`（压缩策略的
         env 与执行体装载都归注入层）；
      b) 输入 env 透传：DISPATCH_PROFILE / AGENTD_RESIDENT / AGENT_ROOT / AGENT_SELF 原样到子进程；
      c) 陈旧的 AGENTD_CONTEXT_COMPACTION 被洗掉（「无策略 = env 不在场」是硬语义，写者是注入层）；
      d) 注入层缺失 ⇒ WARN 点名 + 不注入 + **会话照常收敛 exit 0**（fail-soft：绝不 die，
         硬失败会自锁）、无诊断；
      e) resident 形态同样注入注入层（策略/人格住 profile，与形态无关），且不注入 CHILD_EXTS。
    人格面的**内容**断言（caps 展开序 / 回落 / 工具面并集 / knowledge / model 派生 / 压缩策略归一 /
    降级矩阵）已全部迁到解析层单测 `test_persona.py`（P 系列）—— 同一份解析、两处断言即漂移。"""
    # ① 注入层在场：恰一个 -e，零人格 flag
    e = Env("t48a", extra_env={"DISPATCH_PROFILE": "review"})
    loader = _mk_loader(e.root)
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        argv = e.read_argv() or []
        err = p.stderr.read().decode("utf-8", "replace")
        ok("T48a 恰一个 -e 指向注入层（人格面唯一入口）",
           argv.count(loader) == 1 and loader in argv, repr(argv)[:400])
        leaked = [f for f in PERSONA_FLAGS if f in argv]
        ok("T48a 零人格 flag（--append-system-prompt/--skill/-t/-xt/--model/--provider 全不发）",
           not leaked, leaked)
        ok("T48a 不发 context-compaction 的 -e（压缩策略归注入层）",
           not [a for a in argv if "context-compaction" in a], repr(argv)[:300])
        ok("T48a 装载行在场（点名 profile 与形态，排障可从 agentd.log 归因）",
           "人格注入层装载" in err and "profile=review" in err and "form=task" in err,
           err[-400:])
        ok("T48a 照常收敛 exit 0、无诊断", rc == 0 and e.read_diag() is None, "rc=%s" % rc)
        # ② 输入 env 透传
        pe = e.read_persona_env() or {}
        ok("T48b 输入 env 原样透传（DISPATCH_PROFILE/AGENT_ROOT/AGENT_SELF）",
           pe.get("DISPATCH_PROFILE") == "review" and pe.get("AGENT_ROOT") == e.root
           and pe.get("AGENT_SELF") == e.task_id, json.dumps(pe, ensure_ascii=False))
        ok("T48b 任务形态不带 AGENTD_RESIDENT（注入层据此判形态）",
           pe.get("AGENTD_RESIDENT") is None, json.dumps(pe, ensure_ascii=False))
    finally:
        e.cleanup([p])

    # ③ 陈旧压缩策略 env 被洗掉
    e = Env("t48c", extra_env={"DISPATCH_PROFILE": "review",
                               "AGENTD_CONTEXT_COMPACTION": '{"enabled":true,"triggerTokens":1}'})
    _mk_loader(e.root)
    p = e.start_wrap()
    try:
        p.wait(timeout=20)
        pe = e.read_persona_env() or {}
        ok("T48c 宿主带来的陈旧 AGENTD_CONTEXT_COMPACTION 被洗掉（写者只能是注入层）",
           "AGENTD_CONTEXT_COMPACTION" not in pe or pe.get("AGENTD_CONTEXT_COMPACTION") is None,
           json.dumps(pe, ensure_ascii=False))
    finally:
        e.cleanup([p])

    # ④ 注入层缺失：WARN + 不注入 + 会话照常
    e = Env("t48d", extra_env={"DISPATCH_PROFILE": "review"})
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        argv = e.read_argv() or []
        err = p.stderr.read().decode("utf-8", "replace")
        ok("T48d 注入层缺失 ⇒ WARN 点名「人格面缺席」+ 不拼 -e",
           "人格注入层扩展缺失" in err and "人格面缺席" in err
           and not [a for a in argv if "profile-loader" in a], err[-400:])
        ok("T48d 会话照常收敛 exit 0、无诊断（fail-soft：硬失败会自锁）",
           rc == 0 and e.read_diag() is None, "rc=%s diag=%r" % (rc, (e.read_diag() or "")[:200]))
    finally:
        e.cleanup([p])

    # ⑤ resident 形态：注入层在场、CHILD_EXTS 不在场
    e = Env("t48e", extra_env={"DISPATCH_PROFILE": "dispatcher", "AGENTD_RESIDENT": "1",
                               "AGENTD_SESSION_NAME": "bot/t48e"})
    loader = _mk_loader(e.root)
    rel_dir = os.path.join("assistant", ".pi", "extensions", "agentd")
    for n in ("ask-user-child.ts", "receiver-child.ts"):
        fp = os.path.join(e.root, rel_dir, n)
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, "w", encoding="utf-8") as f:
            f.write("export default function () {}\n")
    p = e.start_wrap()
    try:
        e.wait_argv(15)
        argv = e.read_argv() or []
        e.wait_flag("persona_env")   # fakepi_rpc.py 写序：argv → gate_env → persona_env；wait_argv 不保证后者已落盘
        pe = e.read_persona_env() or {}
        ok("T48e resident 形态同样注入注入层（人格住 profile、与形态无关）",
           argv.count(loader) == 1, repr(argv)[:300])
        ok("T48e resident 不注入 CHILD_EXTS（主端扩展由 workdir 的 .pi 自动发现）",
           not [a for a in argv if "child.ts" in a], repr(argv)[:300])
        ok("T48e 形态输入透传（AGENTD_RESIDENT=1 + 会话名 ⇒ 注入层不前置基线、不回落）",
           pe.get("AGENTD_RESIDENT") == "1" and pe.get("AGENTD_SESSION_NAME") == "bot/t48e"
           and pe.get("DISPATCH_PROFILE") == "dispatcher", json.dumps(pe, ensure_ascii=False))
        ok("T48e 零人格 flag（同任务形态）",
           not [f for f in PERSONA_FLAGS if f in argv],
           [f for f in PERSONA_FLAGS if f in argv])
    finally:
        e.cleanup([p])


def t49_stderr_pump_live():
    """T49 stderr 泵「到达即写」：小额 stderr（远小于 4096B）在 pi **存活期**就落
    `run/agentd/<name>.stderr.log`，不等 EOF。

    判据钉在「存活期」而不是「退出后」：换代/kill 路径下读端（wrap）与写端（pi）同死 ⇒
    卡在管道缓冲里的内容永久丢失 ⇒ 任何依赖 EOF flush 的修法在那条路上恒失效（只能验到
    优雅退出这一档）。fake pi 开机就写一行（~30B）且 `hang_settle` 永不收敛 ⇒ 子进程恒存活，
    「落盘时子进程仍存活」可断言。生产面的同源内容 = 注入层的装配摘要行与 fail-soft WARN。
    """
    e = Env("t49", fake_mode="hang_settle")
    p = e.start_wrap()
    try:
        ok("T49 sock 就位", e.wait_sock())
        ok("T49 fake pi 已启动（argv 快照在场）", e.wait_argv(15))
        slog = os.path.join(e.root, "run", "agentd", e.name + ".stderr.log")
        text, alive = "", False
        dl = time.time() + 10          # 有界轮询（假活防线）
        while time.time() < dl:
            alive = p.poll() is None
            try:
                with open(slog, encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError:
                text = ""
            if "fake-pi boot" in text:
                break
            time.sleep(0.05)
        ok("T49 小额 stderr 存活期即落盘（不等 EOF）", "fake-pi boot" in text,
           "alive=%s log=%s size=%d head=%r" % (alive, slog, len(text), text[:120]))
        ok("T49 落盘时子进程仍存活（不是退出后的 EOF flush）", alive,
           "poll=%s" % p.poll())
        ok("T49 落盘量远小于 read 的 4096 门槛（钉「攒满 n 才返回」那一格）",
           0 < len(text) < 4096, "size=%d" % len(text))
    finally:
        e.cleanup([p])


def main():
    global PASS, FAIL
    os.chmod(FAKEPI, 0o755)
    for fn in (t1_normal,
               t2_relay_replace,
               t3_crash,
               t4_reject,
               t5_idempotent,
               t6_window_cancel,
               t6b_steer_injection,
               t7_stale_takeover,
               t8_resident_no_converge,
               t9_resident_exit_passthrough,
               t10_resident_no_prompt,
               t11_probe_ext,
               t12_sock_bind_failed,
               t29_child_exts,
               t30_ready_handshake,
               t31_ready_env_scrub,
               t_fake_settled_inflight,
               t38_model_error_no_report,
               t39_model_error_with_report,
               t39b_model_error_zero_byte_report,
               t40_normal_stop_no_report,
               t41_tail_unreadable_failsoft,
               t41b_tail_window_truncated,
               t42_resident_model_error_untouched,
               t43_heartbeat_prompt_anchor_lines,
               t45_retired_marker_zero_reflow,
               t46_resident_prompt_delivery,
               t47_spec_command_env_scrub,
               t48_persona_emission,
               t49_stderr_pump_live):
        print("---- %s" % fn.__name__)
        try:
            fn()
        except Exception as ex:
            FAIL += 1
            import traceback
            print("FAIL  %s 异常: %s\n%s" % (fn.__name__, ex,
                                              traceback.format_exc()))
    print("==== wrap 单测：%d passed, %d failed" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
