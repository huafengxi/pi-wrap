#!/usr/bin/env python3
"""test_wrap.py — pi-rpc-wrap.py 单元测试（验证组 U）。

在 /tmp 临时树搭 agents/task/<id>/，以 fakepi_rpc.py 冒充 `pi --mode rpc`，
逐场景断言。仅标准库。用法：python3 pi-wrap/test_wrap.py
"""
import glob
import json
import os
import re
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

    def read_diag(self):
        p = os.path.join(self.home, "diagnosis.md")
        if not os.path.exists(p):
            return None
        with open(p) as f:
            return f.read()

    def read_argv(self):
        """fake 启动时落的 argv 快照（：resident argv 断言）。"""
        p = os.path.join(self.flags, "argv")
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
        ok("T1 任务形态 argv（-n 任务名 + -xt ask_user）",
           argv is not None and "-xt" in argv
           and argv[argv.index("-xt") + 1] == "ask_user"
           and "-n" in argv
           and argv[argv.index("-n") + 1] == "[task %s]" % e.name,
           repr(argv))
        parsed = _pi_parsed(argv)
        ok("T1 pi parseArgs 生效集合 = {ask_user}（基线屏蔽，无 profile 零回归）",
           parsed is not None and parsed["excludeTools"] == ["ask_user"],
           repr(parsed))
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


EXEC_TEXT = "# executor baseline\n\ntask persona\n"
CAP_TEXT = "# reviewer persona\n\nbe strict\n"


def _yml_val(x):
    """极简 YAML 标量序列化（测试夹具只用「标量键 + 流式数组」两种形态，故 test_wrap 保持
    仅标准库、不 import yaml）：字符串一律双引号（json.dumps 产出的是合法 YAML 双引号标量，
    且能安全承载内嵌逗号/冒号）；None → null；bool → true/false；其余 str()。"""
    if isinstance(x, str):
        return json.dumps(x, ensure_ascii=False)
    if x is None:
        return "null"
    if isinstance(x, bool):
        return "true" if x else "false"
    return str(x)


def _yml(doc):
    """dict → YAML 文本（嵌套 mapping 不支持：夹具不需要，用到即测试自身写错）。"""
    lines = []
    for k, v in doc.items():
        if isinstance(v, dict):
            raise AssertionError("测试夹具不用嵌套 mapping：%r" % k)
        if isinstance(v, list):
            lines.append("%s: [%s]" % (k, ", ".join(_yml_val(i) for i in v)))
        else:
            lines.append("%s: %s" % (k, _yml_val(v)))
    return "\n".join(lines) + "\n"


def _w(path, text="export default function () {}\n"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def _mk_cap(root, name, prompt_text=CAP_TEXT, cap_yml=None):
    """在临时树建一个原子能力 `bots/caps/<名>/{cap.yml,prompt.md}`（两层模型的复用单元）。
      prompt_text=None ⇒ **bundle 能力**（无正文，只有捆绑声明，合法形态）；
      cap_yml=None     ⇒ 不写 cap.yml（**纯正文能力**，装配器 WARN 后只注入正文）；
      cap_yml=dict     ⇒ _yml() 序列化；cap_yml=str ⇒ 原样写（损坏 YAML / 顶层非 mapping 用例）。
    返回 (能力目录, 正文文本 ∨ None)。"""
    cdir = os.path.join(root, "bots", "caps", name)
    os.makedirs(cdir, exist_ok=True)
    if prompt_text is not None:
        _w(os.path.join(cdir, "prompt.md"), prompt_text)
    if cap_yml is not None:
        _w(os.path.join(cdir, "cap.yml"),
           cap_yml if isinstance(cap_yml, str) else _yml(cap_yml))
    return cdir, prompt_text


def _mk_manifest(root, name, caps=None, model=None, notes=None, extra=None,
                 raw=None):
    """在临时树建 profile 薄清单 `bots/profiles/<名>.json`（字段只有 name/summary/notes/
    model/caps/contextCompaction；捆绑资产一律住能力 cap.yml，`contextCompaction` 是运行环境/
    策略类字段、不是资产逃生口）。
      caps 缺省 = [name]（同名单能力，最常见形态）；
      extra = 追加字段（直挂禁字段与 contextCompaction 用例）；raw = 原样写（损坏 JSON /
      顶层非对象用例）。
    返回清单路径。"""
    p = os.path.join(root, "bots", "profiles", name + ".json")
    if raw is not None:
        return _w(p, raw)
    doc = {"name": name, "summary": "test profile %s" % name,
           "caps": list(caps) if caps is not None else [name]}
    if model is not None:
        doc["model"] = model
    if notes is not None:
        doc["notes"] = list(notes)
    if extra:
        doc.update(extra)
    return _w(p, json.dumps(doc, ensure_ascii=False))


def _mk_persona(root, name, prompt_text=CAP_TEXT, cap_yml=None, **manifest):
    """一站式：能力 `<name>` + 同名薄清单（caps:[name]）。返回 (能力目录, 正文文本)。"""
    cdir, text = _mk_cap(root, name, prompt_text=prompt_text, cap_yml=cap_yml)
    _mk_manifest(root, name, **manifest)
    return cdir, text


def _mk_executor(root, text=EXEC_TEXT):
    """建任务形态基线能力 `executor`（装配器对任务形态恒前置它，故夹具要么建它、
    要么显式接受「WARN 跳过」并只断言被测能力）。返回 (能力目录, 正文文本)。"""
    return _mk_cap(root, "executor", prompt_text=text, cap_yml={"summary": "baseline"})


def _mk_skill(root, name, with_skill_md=True):
    """建共享库 skill `bots/skills/<名>/`（cap.yml 的 skills 按名捆绑；一级解析、不回落全局）。"""
    sd = os.path.join(root, "bots", "skills", name)
    os.makedirs(sd, exist_ok=True)
    if with_skill_md:
        _w(os.path.join(sd, "SKILL.md"), "# %s\n" % name)
    return sd


def _mk_ext_unit(root, name, form="index"):
    """建共享库扩展单元 `bots/extensions/<名>/`（一律 .ts）。form：
      index = 含 index.ts（恰一个 -e）；flat = 直属多个 .ts（按名排序各一个 -e）；
      mixed = 直属 .ts + 非 .ts + dot 开头（非 .ts WARN 跳过、dot 静默跳过）；
      nots  = 只有非 .ts（无可注入）；empty = 空目录。返回单元目录。"""
    ed = os.path.join(root, "bots", "extensions", name)
    os.makedirs(ed, exist_ok=True)
    if form == "index":
        _w(os.path.join(ed, "index.ts"))
    elif form == "flat":
        for fn in ("b.ts", "a.ts"):      # 故意逆序建，断言注入按名排序
            _w(os.path.join(ed, fn))
    elif form == "mixed":
        _w(os.path.join(ed, "b.ts"))
        _w(os.path.join(ed, "a.ts"))
        _w(os.path.join(ed, "readme.md"), "# not ts\n")
        _w(os.path.join(ed, ".hidden.ts"))
    elif form == "nots":
        _w(os.path.join(ed, "readme.md"), "# not ts\n")
    return ed


def _cap_argv_checks(name, argv, prompt_text, skills=(), model=None):
    """能力注入的公共断言（--append-system-prompt / --skill / --model）。
    skills = 期望的共享库 skill 名**按声明序**（能力化后不再按目录名排序：声明序即注入序，
    作者可控）。模型只住 profile ⇒ model 断言的是薄清单的值。"""
    prompts = _argv_flag_pairs(argv, "--append-system-prompt")
    ok("%s argv 的 --append-system-prompt 恰一份且 = 能力 prompt.md 文本"
       "（装配器不碰正文一个字节；基线能力在本夹具故意缺席 ⇒ 注入面只剩被测能力）" % name,
       prompts == [prompt_text], repr(prompts)[:300])
    got_skills = _argv_flag_pairs(argv, "--skill")
    ok("%s argv 含每个捆绑 skill 的 --skill（bots/skills/ 一级解析，序 = 声明序）" % name,
       [os.path.basename(x) for x in got_skills] == list(skills),
       "got=%r want=%r" % (got_skills, list(skills)))
    ok("%s --skill 一律是 bots/skills/ 下的绝对路径（不回落全局）" % name,
       all(x.startswith(os.sep) and "bots/skills/" in x.replace(os.sep, "/")
           for x in got_skills), repr(got_skills))
    if model is not None:
        ok("%s argv 含 --model %s（model 只住 profile）" % (name, model),
           _argv_flag_pairs(argv, "--model") == [model], repr(argv))
    else:
        ok("%s argv 不含 --model（薄清单未声明 model）" % name,
           argv is not None and "--model" not in argv, repr(argv))


def t13_profile():
    """T13 profile 在场（两层模型）：任务/常驻两形态 argv 均注入；`model` 住薄清单 → --model；
    薄清单无 model → 不拼。被测能力是 caps 里唯一在场的能力（基线 executor 能力故意不建 ⇒
    装配器 WARN 跳过，注入面只剩被测能力，断言不受基线干扰）。"""
    # ① 任务形态 + 薄清单带 model + 能力捆绑两个 skill（声明序 = 注入序）
    e = Env("t13a", extra_env={"DISPATCH_PROFILE": "review"})
    _mk_skill(e.root, "b-skill")
    _mk_skill(e.root, "a-skill")
    _mk_persona(e.root, "review",
                cap_yml={"summary": "reviewer", "skills": ["b-skill", "a-skill"]},
                model="bailian/qwen3-test")
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        argv = e.read_argv()
        ok("T13a 任务形态收敛退出 0", rc == 0, "rc=%s" % rc)
        _cap_argv_checks("T13a", argv, CAP_TEXT, skills=("b-skill", "a-skill"),
                         model="bailian/qwen3-test")
        ok("T13a 既有参数不变（-xt ask_user 仍在场、恰一个）",
           _argv_flag_pairs(argv, "-xt") == ["ask_user"], repr(argv))
    finally:
        e.cleanup([p])
    # ② 任务形态 + 薄清单无 model → 不拼 --model（两态覆盖）
    e = Env("t13b", extra_env={"DISPATCH_PROFILE": "review"})
    _mk_skill(e.root, "only-skill")
    _mk_persona(e.root, "review", cap_yml={"skills": ["only-skill"]})
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        argv = e.read_argv()
        ok("T13b 退出 0", rc == 0, "rc=%s" % rc)
        _cap_argv_checks("T13b", argv, CAP_TEXT, skills=("only-skill",), model=None)
    finally:
        e.cleanup([p])
    # ③ resident 形态 + profile（与任务形态同款注入，但不前置基线能力）
    e = Env("t13c", fake_mode="hang_settle",
            extra_env={"AGENTD_RESIDENT": "1",
                       "AGENTD_SESSION_NAME": "bot/zz-profile-t13",
                       "DISPATCH_PROFILE": "review"})
    _mk_skill(e.root, "s1")
    _mk_persona(e.root, "review", cap_yml={"skills": ["s1"]})
    p = e.start_wrap()
    try:
        ok("T13c sock 就位", e.wait_sock())
        e.wait_argv()        # resident 不收敛：事件驱动等 argv 快照（旧 sleep(1.5)）
        argv = e.read_argv()
        _cap_argv_checks("T13c-resident", argv, CAP_TEXT, skills=("s1",), model=None)
        ok("T13c resident 无基线 -xt（形态基线为空 ∧ 能力未声明排除）",
           _argv_flag_pairs(argv, "-xt") == [], repr(argv))
        ok("T13c 无诊断", e.read_diag() is None)
    finally:
        e.cleanup([p])


def t14_profile_missing():
    """T14 profile 薄清单不存在：WARN 跳过 + **任务形态仍前置基线能力**（装配器硬规则，
    防漏列）⇒ 不再等价裸启动；无诊断、退出 0。"""
    e = Env("t14", extra_env={"DISPATCH_PROFILE": "ghost"})
    _mk_executor(e.root)
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        ok("T14 正常收敛退出 0（不硬失败）", rc == 0, "rc=%s" % rc)
        err = p.stderr.read().decode("utf-8", "replace")
        ok("T14 stderr 告警在场（点名 profile 不存在）",
           "ghost" in err and "不存在" in err, err[-400:])
        argv = e.read_argv()
        prompts = _argv_flag_pairs(argv, "--append-system-prompt")
        ok("T14 任务形态仍注入基线能力（executor 正文恰一份）",
           prompts == [EXEC_TEXT], repr(prompts)[:300])
        ok("T14 argv 无 --skill/--model",
           argv is not None and "--skill" not in argv and "--model" not in argv,
           repr(argv))
        ok("T14 无诊断", e.read_diag() is None)
    finally:
        e.cleanup([p])
    # ② resident 形态 + 薄清单缺失 = 真裸启动（不前置基线能力）
    e = Env("t14b", fake_mode="hang_settle",
            extra_env={"AGENTD_RESIDENT": "1",
                       "AGENTD_SESSION_NAME": "bot/zz-ghost-t14",
                       "DISPATCH_PROFILE": "ghost"})
    _mk_executor(e.root)
    p = e.start_wrap()
    try:
        ok("T14b sock 就位", e.wait_sock())
        e.wait_argv()
        argv = e.read_argv()
        ok("T14b resident 薄清单缺失 → 裸启动（无任何人格注入、无 -xt）",
           _argv_flag_pairs(argv, "--append-system-prompt") == []
           and _argv_flag_pairs(argv, "-xt") == [], repr(argv))
    finally:
        e.cleanup([p])


def t15_profile_unset():
    """T15 未设 DISPATCH_PROFILE（两形态分档）：**任务形态仍注入基线能力**（装配器硬规则
    承担，不再靠 core.ts 拼串）；**resident 形态 argv 逐字不变**（零回归出口移到这一档）——
    即使薄清单与能力资产在场也绝不注入。
    注：本夹具不建 `executor` **profile 清单**（只建同名能力）⇒ 任务形态的缺省回落走
    fail-soft 分支（WARN + 不注入 --model），断言与回落接入前逐字一致；回落正面四态 = T36。"""
    # ① 任务形态：只注入 executor 基线能力，其余资产在场也不注入
    e = Env("t15a")
    _mk_executor(e.root)
    _mk_skill(e.root, "s1")
    _mk_persona(e.root, "review", cap_yml={"skills": ["s1"]},
                model="bailian/qwen3-test")
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        argv = e.read_argv()
        ok("T15a 收敛退出 0", rc == 0, "rc=%s" % rc)
        ok("T15a 任务形态注入基线能力且只有它（review 资产在场也不注入）",
           _argv_flag_pairs(argv, "--append-system-prompt") == [EXEC_TEXT]
           and _argv_flag_pairs(argv, "--skill") == []
           and "--model" not in (argv or []), repr(argv)[:300])
        ok("T15a 基线 -xt ask_user 仍在场",
           _argv_flag_pairs(argv, "-xt") == ["ask_user"], repr(argv))
    finally:
        e.cleanup([p])
    # ② resident 形态：argv 逐字不变（零回归出口）
    e = Env("t15b", fake_mode="hang_settle",
            extra_env={"AGENTD_RESIDENT": "1",
                       "AGENTD_SESSION_NAME": "bot/zz-unset-t15"})
    _mk_executor(e.root)
    _mk_persona(e.root, "review", cap_yml={"skills": ["s1"]},
                model="bailian/qwen3-test")
    _mk_skill(e.root, "s1")
    p = e.start_wrap()
    try:
        ok("T15b sock 就位", e.wait_sock())
        e.wait_argv()
        argv = e.read_argv()
        ok("T15b resident 未设 env → 无任何人格注入（现状逐字不变）",
           _argv_flag_pairs(argv, "--append-system-prompt") == []
           and _argv_flag_pairs(argv, "--skill") == []
           and _argv_flag_pairs(argv, "-xt") == []
           and "--model" not in (argv or []), repr(argv))
    finally:
        e.cleanup([p])


def _argv_flag_pairs(argv, flag):
    """收集 argv 中所有 <flag, 值> 对（多 -xt/-e 叠加断言用）。"""
    out = []
    if argv:
        for i, a in enumerate(argv):
            if a == flag and i + 1 < len(argv):
                out.append(argv[i + 1])
    return out


def _pi_parsed(argv):
    """pi CLI 生效集合级断言基建：用 node 调 pi 的 parseArgs 解析 wrap 产出 argv（去掉首个
    元素 = 可执行名），返回 {excludeTools, tools}。argv 级断言拦不住 pi 实际语义问题（如重复
    -xt 后者覆盖），故断言必须到解析后的生效集合层。node/args.js 不在场 → None（用例按断言
    失败处理）。"""
    if argv is None:
        return None
    cands = sorted(glob.glob(os.path.expanduser(
        "~/.nvm/versions/node/*/lib/node_modules/@earendil-works/"
        "pi-coding-agent/dist/cli/args.js")))
    if not cands:
        return None
    script = ("import(%s).then(m=>{const r=m.parseArgs(%s);"
              "process.stdout.write(JSON.stringify({"
              "excludeTools:r.excludeTools||[],tools:r.tools||[]}))})"
              % (json.dumps(cands[-1]), json.dumps(argv[1:])))
    try:
        out = subprocess.run(["node", "-e", script], capture_output=True,
                             text=True, timeout=30)
        if out.returncode != 0:
            return None
        return json.loads(out.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def t16_tools_whitelist():
    """T16 工具面白名单：cap.yml 的 tools → -t 逗号连接；清单 = review 试点的只读面；
    任务形态基线 -xt ask_user 不变（白名单路径下排除集仍以单个 -xt 前置）。"""
    e = Env("t16", extra_env={"DISPATCH_PROFILE": "review"})
    _mk_persona(e.root, "review",
                cap_yml={"tools": ["read", "grep", "find", "ls"]})
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        argv = e.read_argv()
        ok("T16 收敛退出 0", rc == 0, "rc=%s" % rc)
        ok("T16 argv 含 -t 只读白名单（逗号连接、恰一个）",
           _argv_flag_pairs(argv, "-t") == ["read,grep,find,ls"], repr(argv))
        ok("T16 既有 -xt ask_user 不变（基线屏蔽保留、恰一个）",
           _argv_flag_pairs(argv, "-xt") == ["ask_user"], repr(argv))
        parsed = _pi_parsed(argv)
        ok("T16 pi parseArgs 生效：白名单 tools 与基线 excludeTools 均生效",
           parsed is not None
           and parsed["tools"] == ["read", "grep", "find", "ls"]
           and parsed["excludeTools"] == ["ask_user"], repr(parsed))
    finally:
        e.cleanup([p])


def t17_tools_blacklist():
    """T17 工具面黑名单：cap.yml 的 excludeTools 与形态基线 ask_user **并集**为单个 -xt——
    pi 的 -xt 是赋值（重复出现后者覆盖前者），拆成两个 -xt 会解除任务形态对 ask_user 的屏蔽。
    断言到 pi parseArgs 生效集合级（含 ask_user ∧ 黑名单元素）。"""
    e = Env("t17", extra_env={"DISPATCH_PROFILE": "review"})
    _mk_persona(e.root, "review",
                cap_yml={"excludeTools": ["web_search", "bash"]})
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        argv = e.read_argv()
        ok("T17 收敛退出 0", rc == 0, "rc=%s" % rc)
        ok("T17 argv 单个 -xt = 基线 ∪ 能力黑名单（基线项在前）",
           _argv_flag_pairs(argv, "-xt") == ["ask_user,web_search,bash"], repr(argv))
        ok("T17 argv 无 -t", argv is not None and "-t" not in argv, repr(argv))
        parsed = _pi_parsed(argv)
        ok("T17 pi parseArgs 生效集合含 ask_user 与黑名单元素",
           parsed is not None
           and set(parsed["excludeTools"])
           == {"ask_user", "web_search", "bash"}, repr(parsed))
    finally:
        e.cleanup([p])


def t18_tools_edge():
    """T18 工具面边界：① 同一能力同时声明 tools 与 excludeTools ⇒ **两面均生效**（并集语义；
    旧「互斥、白名单优先」已退休），被排除掉的白名单项 = WARN 不阻断；② 空数组/缺省 = 不拼
    （未声明者不参与合并）；③ 非字符串元素 WARN 跳过；④ 全非法元素 → 不拼 -t。"""
    # ① 并集语义：-t 与 -xt 并存，白名单里被排除的项 WARN
    e = Env("t18a", extra_env={"DISPATCH_PROFILE": "review"})
    _mk_persona(e.root, "review",
                cap_yml={"tools": ["read", "bash"], "excludeTools": ["bash"]})
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        argv = e.read_argv()
        err = p.stderr.read().decode("utf-8", "replace")
        ok("T18a 收敛退出 0", rc == 0, "rc=%s" % rc)
        ok("T18a -t 与 -xt 并存（并集语义，不再互斥）",
           _argv_flag_pairs(argv, "-t") == ["read,bash"]
           and _argv_flag_pairs(argv, "-xt") == ["ask_user,bash"], repr(argv))
        parsed = _pi_parsed(argv)
        ok("T18a pi parseArgs 生效集合：bash 最终被排除（excludeTools 在 tools 之后生效）",
           parsed is not None and parsed["tools"] == ["read", "bash"]
           and set(parsed["excludeTools"]) == {"ask_user", "bash"}, repr(parsed))
        ok("T18a 被排除的白名单项 WARN 在场（可见即可、不阻断）",
           "被排除集命中" in err and "bash" in err, err[-400:])
    finally:
        e.cleanup([p])
    # ② 空数组 = 缺省：工具面相关 argv 逐字不变（只剩既有 -xt ask_user）
    e = Env("t18b", extra_env={"DISPATCH_PROFILE": "review"})
    _mk_persona(e.root, "review", cap_yml={"tools": [], "excludeTools": []})
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        argv = e.read_argv()
        ok("T18b 空数组 = 未声明（无 -t，-xt 仅 ask_user）",
           rc == 0 and argv is not None and "-t" not in argv
           and _argv_flag_pairs(argv, "-xt") == ["ask_user"], repr(argv))
    finally:
        e.cleanup([p])
    # ③ 非字符串元素跳过，其余生效 + 告警
    e = Env("t18c", extra_env={"DISPATCH_PROFILE": "review"})
    _mk_persona(e.root, "review",
                cap_yml={"excludeTools": ["bash", 42, None, "  "]})
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        argv = e.read_argv()
        err = p.stderr.read().decode("utf-8", "replace")
        ok("T18c 非法元素跳过后剩余黑名单生效（单个合并 -xt）",
           rc == 0 and _argv_flag_pairs(argv, "-xt")
           == ["ask_user,bash"], repr(argv))
        ok("T18c 非法元素告警在场", "非字符串" in err, err[-400:])
    finally:
        e.cleanup([p])
    # ④ 全非法元素 → 过滤后为空 = 未声明不拼
    e = Env("t18d", extra_env={"DISPATCH_PROFILE": "review"})
    _mk_persona(e.root, "review", cap_yml={"tools": [42, None]})
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        argv = e.read_argv()
        ok("T18d 全非法元素 → 不拼 -t",
           rc == 0 and argv is not None and "-t" not in argv, repr(argv))
    finally:
        e.cleanup([p])


def t19_profile_extensions():
    """T19 能力捆绑扩展（共享库 `bots/extensions/<名>/`，一律 .ts）：index.ts 形态 → 恰一个 -e；
    flat 形态 → 直属每个 .ts 按名排序各一个 -e；非 .ts → WARN 跳过、dot 开头静默跳过；
    与协议层 -e 并存；多单元按声明序。"""
    e = Env("t19", extra_env={"DISPATCH_PROFILE": "review"})
    ed_idx = _mk_ext_unit(e.root, "unit-idx", "index")
    ed_flat = _mk_ext_unit(e.root, "unit-flat", "flat")
    ed_mix = _mk_ext_unit(e.root, "unit-mixed", "mixed")
    _mk_persona(e.root, "review",
                cap_yml={"extensions": ["unit-idx", "unit-flat", "unit-mixed"]})
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        argv = e.read_argv()
        err = p.stderr.read().decode("utf-8", "replace")
        ok("T19 收敛退出 0", rc == 0, "rc=%s" % rc)
        es = _argv_flag_pairs(argv, "-e")
        got = [x for x in es if "bots/extensions" in x.replace(os.sep, "/")]
        want = [os.path.join(ed_idx, "index.ts"),
                os.path.join(ed_flat, "a.ts"), os.path.join(ed_flat, "b.ts"),
                os.path.join(ed_mix, "a.ts"), os.path.join(ed_mix, "b.ts")]
        ok("T19 扩展 -e 序确定（单元按声明序；flat 单元内按名排序）",
           got == want, "got=%r want=%r" % (got, want))
        ok("T19 非 .ts / dot 开头条目未注入",
           not any("readme.md" in x or ".hidden.ts" in x for x in es), repr(es))
        ok("T19 跳过告警在场", "非 .ts" in err, err[-500:])
        ok("T19 协议层 -e 与既有 -xt ask_user 不受影响",
           "ask_user" in ",".join(_argv_flag_pairs(argv, "-xt")), repr(argv))
    finally:
        e.cleanup([p])


def t20_profile_extensions_empty():
    """T20 扩展单元降级：① 空目录 / 只有非 .ts ⇒ 无该单元 -e + WARN；② 捆绑名不存在 ⇒ WARN
    跳过；③ resident 未设 DISPATCH_PROFILE ⇒ 能力资产在场也绝不注入。"""
    e = Env("t20a", extra_env={"DISPATCH_PROFILE": "review"})
    ed_empty = _mk_ext_unit(e.root, "unit-empty", "empty")
    ed_nots = _mk_ext_unit(e.root, "unit-nots", "nots")
    _mk_persona(e.root, "review",
                cap_yml={"extensions": ["unit-empty", "unit-nots", "unit-ghost"]})
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        argv = e.read_argv()
        err = p.stderr.read().decode("utf-8", "replace")
        es = _argv_flag_pairs(argv, "-e")
        ok("T20a 空/无可注入 .ts/不存在的单元 → 无 capability 侧 -e",
           rc == 0 and not any(x.startswith((ed_empty, ed_nots)) for x in es)
           and not any("unit-ghost" in x for x in es), repr(argv))
        ok("T20a 两类降级告警在场（无可注入 .ts / 单元不存在）",
           "无可注入 .ts" in err and "不存在" in err, err[-500:])
    finally:
        e.cleanup([p])
    # ② resident 未设 env：能力资产（扩展 + 工具面）在场也绝不注入
    e = Env("t20b", fake_mode="hang_settle",
            extra_env={"AGENTD_RESIDENT": "1",
                       "AGENTD_SESSION_NAME": "bot/zz-noenv-t20"})
    ed = _mk_ext_unit(e.root, "unit-idx", "index")
    _mk_persona(e.root, "review",
                cap_yml={"tools": ["read"], "extensions": ["unit-idx"]})
    p = e.start_wrap()
    try:
        ok("T20b sock 就位", e.wait_sock())
        e.wait_argv()
        argv = e.read_argv()
        ok("T20b resident 未设 env → extensions/tools 在场也绝不注入",
           "-t" not in (argv or [])
           and not any(x.startswith(ed) for x in _argv_flag_pairs(argv, "-e")),
           repr(argv))
    finally:
        e.cleanup([p])


def t21_xt_merge_edges():
    """T21 -xt 合并边界：① 黑名单含 ask_user 本身 → 并集去重（任务形态基线在前）；
    ② resident 形态无基线 → 黑名单独立单个 -xt（不含 ask_user）。两态均断言 pi parseArgs
    生效集合。"""
    e = Env("t21a", extra_env={"DISPATCH_PROFILE": "review"})
    _mk_persona(e.root, "review",
                cap_yml={"excludeTools": ["ask_user", "web_search"]})
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        argv = e.read_argv()
        ok("T21a 并集去重（单个 -xt，无重复 ask_user）",
           rc == 0 and _argv_flag_pairs(argv, "-xt")
           == ["ask_user,web_search"], repr(argv))
        parsed = _pi_parsed(argv)
        ok("T21a pi parseArgs 生效集合 = {ask_user, web_search}",
           parsed is not None
           and set(parsed["excludeTools"]) == {"ask_user", "web_search"},
           repr(parsed))
    finally:
        e.cleanup([p])
    e = Env("t21b", fake_mode="hang_settle",
            extra_env={"AGENTD_RESIDENT": "1",
                       "AGENTD_SESSION_NAME": "bot/zz-xt-t21",
                       "DISPATCH_PROFILE": "review"})
    _mk_persona(e.root, "review",
                cap_yml={"excludeTools": ["web_search", "bash"]})
    p = e.start_wrap()
    try:
        ok("T21b sock 就位", e.wait_sock())
        e.wait_argv()
        argv = e.read_argv()
        ok("T21b resident 单个 -xt 无基线项（行为不变）",
           _argv_flag_pairs(argv, "-xt") == ["web_search,bash"], repr(argv))
        parsed = _pi_parsed(argv)
        ok("T21b pi parseArgs 生效集合 = 黑名单（无 ask_user）",
           parsed is not None
           and set(parsed["excludeTools"]) == {"web_search", "bash"},
           repr(parsed))
    finally:
        e.cleanup([p])


def t22_tools_field_edges():
    """T22 工具面字段边界：① 含内嵌逗号元素 → WARN 拒绝该元素（pi 按逗号拆工具名，防白名单/
    黑名单被撑大）；② 字段非数组 → WARN 跳过、工具面零变化（基线 -xt 仍兜底在场）。"""
    e = Env("t22a", extra_env={"DISPATCH_PROFILE": "review"})
    _mk_persona(e.root, "review",
                cap_yml={"excludeTools": ["read,bash", "grep"]})
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        argv = e.read_argv()
        err = p.stderr.read().decode("utf-8", "replace")
        ok("T22a 含逗号元素被拒绝，剩余黑名单与基线合并",
           rc == 0 and _argv_flag_pairs(argv, "-xt")
           == ["ask_user,grep"], repr(argv))
        ok("T22a 逗号拒绝告警在场", "内嵌逗号" in err, err[-500:])
    finally:
        e.cleanup([p])
    e = Env("t22b", extra_env={"DISPATCH_PROFILE": "review"})
    _mk_persona(e.root, "review", cap_yml={"tools": ["a,b", "read"]})
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        argv = e.read_argv()
        err = p.stderr.read().decode("utf-8", "replace")
        ok("T22b 白名单含逗号元素被拒绝，剩余生效",
           rc == 0 and _argv_flag_pairs(argv, "-t") == ["read"], repr(argv))
        ok("T22b 逗号拒绝告警在场", "内嵌逗号" in err, err[-500:])
    finally:
        e.cleanup([p])
    e = Env("t22c", extra_env={"DISPATCH_PROFILE": "review"})
    _mk_persona(e.root, "review", cap_yml={"tools": "read"})
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        argv = e.read_argv()
        err = p.stderr.read().decode("utf-8", "replace")
        ok("T22c 非数组字段跳过（无 -t，基线 -xt ask_user 兜底在场）",
           rc == 0 and argv is not None and "-t" not in argv
           and _argv_flag_pairs(argv, "-xt") == ["ask_user"], repr(argv))
        ok("T22c 非数组告警在场", "非数组" in err, err[-500:])
    finally:
        e.cleanup([p])


def t23_multi_cap_order():
    """T23 多能力注入序（薄清单 caps 列表序 = 注入序）：多个 --append-system-prompt（追加语义）、
    各能力的 skills/extensions 依次注入；任务形态基线单个 -xt ask_user 保留。"""
    e = Env("t23", extra_env={"DISPATCH_PROFILE": "combo"})
    _mk_skill(e.root, "s-base")
    _mk_skill(e.root, "s-p1")
    _mk_skill(e.root, "s-p2")
    ed_a = _mk_ext_unit(e.root, "unit-a", "index")
    ed_b = _mk_ext_unit(e.root, "unit-b", "index")
    _mk_cap(e.root, "base", prompt_text="# base persona\n",
            cap_yml={"skills": ["s-base"], "extensions": ["unit-a"]})
    _mk_cap(e.root, "persona", prompt_text="# persona overlay\n",
            cap_yml={"skills": ["s-p1", "s-p2"], "extensions": ["unit-b"]})
    _mk_manifest(e.root, "combo", caps=["base", "persona"])
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        argv = e.read_argv()
        ok("T23 收敛退出 0", rc == 0, "rc=%s" % rc)
        prompts = _argv_flag_pairs(argv, "--append-system-prompt")
        ok("T23 多个 --append-system-prompt 顺序 = caps 列表序",
           prompts == ["# base persona\n", "# persona overlay\n"], repr(prompts))
        skills = [os.path.basename(x) for x in _argv_flag_pairs(argv, "--skill")]
        ok("T23 skills 按 caps 序注入（能力内按声明序）",
           skills == ["s-base", "s-p1", "s-p2"], repr(skills))
        es = _argv_flag_pairs(argv, "-e")
        got = [x for x in es if "bots/extensions" in x.replace(os.sep, "/")]
        ok("T23 extensions 按 caps 序注入",
           got == [os.path.join(ed_a, "index.ts"), os.path.join(ed_b, "index.ts")],
           repr(got))
        ok("T23 基线单个 -xt ask_user 保留",
           _argv_flag_pairs(argv, "-xt") == ["ask_user"], repr(argv))
    finally:
        e.cleanup([p])


def t24_cap_partial_missing():
    """T24 caps 内单能力缺失：WARN 跳过该能力、其余照常注入，不硬失败不拖垮会话（无诊断）；
    全部能力缺失 + 任务形态 ⇒ 只剩形态基线 -xt（基线能力也不在场时 = 等价裸启动）。"""
    # ① ghost 在前：其余照常注入
    e = Env("t24a", extra_env={"DISPATCH_PROFILE": "combo"})
    _mk_skill(e.root, "s1")
    _mk_cap(e.root, "review", cap_yml={"skills": ["s1"]})
    _mk_manifest(e.root, "combo", caps=["ghost", "review"])
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        err = p.stderr.read().decode("utf-8", "replace")
        argv = e.read_argv()
        ok("T24a 收敛退出 0（不硬失败）", rc == 0, "rc=%s" % rc)
        ok("T24a 缺失能力告警在场", "ghost" in err and "不存在" in err, err[-400:])
        _cap_argv_checks("T24a", argv, CAP_TEXT, skills=("s1",))
        ok("T24a 无诊断", e.read_diag() is None)
    finally:
        e.cleanup([p])
    # ② 全部能力缺失（含基线）= 等价裸启动（无任何人格参数，基线 -xt 兜底）
    e = Env("t24b", extra_env={"DISPATCH_PROFILE": "combo"})
    _mk_manifest(e.root, "combo", caps=["ghost1", "ghost2"])
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        argv = e.read_argv()
        ok("T24b 全能力缺失 → 等价裸启动（退出 0、无人格参数、基线 -xt 仍在）",
           rc == 0
           and _argv_flag_pairs(argv, "--append-system-prompt") == []
           and _argv_flag_pairs(argv, "--skill") == []
           and "--model" not in (argv or [])
           and _argv_flag_pairs(argv, "-xt") == ["ask_user"], repr(argv))
    finally:
        e.cleanup([p])


def t25_model_and_toolface_merge():
    """T25 声明面归属与并集：① `model` **只住 profile**（薄清单的值生效为单个 --model；
    能力 cap.yml 声明 model = 非法键 ⇒ WARN 忽略，旧「跨链后者覆盖」退休）；② 工具面跨能力
    **并集**（两能力分别声明 tools 与 excludeTools ⇒ -t 与 -xt 同时在场，旧「后者覆盖」退休）。"""
    # ① model 只住 profile
    e = Env("t25a", extra_env={"DISPATCH_PROFILE": "who"})
    _mk_cap(e.root, "base", prompt_text="# base\n",
            cap_yml={"model": "bailian/cap-should-be-ignored"})
    _mk_manifest(e.root, "who", caps=["base"], model="bailian/profile-model")
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        argv = e.read_argv()
        err = p.stderr.read().decode("utf-8", "replace")
        ok("T25a --model 恰一个且 = 薄清单的值（能力层声明被忽略）",
           rc == 0 and _argv_flag_pairs(argv, "--model") == ["bailian/profile-model"],
           repr(argv))
        ok("T25a 能力层非法键 model 的 WARN 在场",
           "非法键" in err and "model" in err, err[-400:])
    finally:
        e.cleanup([p])
    # ①' profile 未声明 model ⇒ 能力层声明也不生效（无 --model）
    e = Env("t25a2", extra_env={"DISPATCH_PROFILE": "who"})
    _mk_cap(e.root, "base", prompt_text="# base\n",
            cap_yml={"model": "bailian/cap-only"})
    _mk_manifest(e.root, "who", caps=["base"])
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        argv = e.read_argv()
        ok("T25a2 profile 无 model ⇒ 不拼 --model（能力层无此字段）",
           rc == 0 and "--model" not in (argv or []), repr(argv))
    finally:
        e.cleanup([p])
    # ② 工具面跨能力并集（白名单 ∪ 白名单、黑名单 ∪ 黑名单 ∪ 基线）
    e = Env("t25b", extra_env={"DISPATCH_PROFILE": "combo"})
    _mk_cap(e.root, "base", prompt_text="# base\n",
            cap_yml={"tools": ["read", "grep"]})
    _mk_cap(e.root, "persona", prompt_text="# persona\n",
            cap_yml={"tools": ["grep", "ls"], "excludeTools": ["web_search"]})
    _mk_manifest(e.root, "combo", caps=["base", "persona"])
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        argv = e.read_argv()
        err = p.stderr.read().decode("utf-8", "replace")
        ok("T25b 工具面并集：-t = 两能力白名单去重保序、-xt = 基线 ∪ 黑名单",
           rc == 0 and _argv_flag_pairs(argv, "-t") == ["read,grep,ls"]
           and _argv_flag_pairs(argv, "-xt") == ["ask_user,web_search"],
           repr(argv))
        parsed = _pi_parsed(argv)
        ok("T25b pi parseArgs 生效集合 = 并集（排除在 whitelist 之后生效）",
           parsed is not None and parsed["tools"] == ["read", "grep", "ls"]
           and set(parsed["excludeTools"]) == {"ask_user", "web_search"},
           repr(parsed))
        ok("T25b 无「后者覆盖」类告警（并集语义不是冲突）",
           "覆盖" not in err, err[-400:])
    finally:
        e.cleanup([p])


def t26_single_value_defense():
    """T26 单值文法防护（链式已退役）：① 空值/全空白 = 未设；② **含逗号 = 已退役的链式写法**
    ⇒ WARN 文案点名成因、按未设处置；③ 非法名（`../evil`、前导 `.`）⇒ WARN 拒绝；
    ④ caps 数组内重复能力名 ⇒ 去重保序 + WARN。任务形态四态均仍注入基线能力。"""
    # ① 空值/全空白 = 未设（任务形态只剩基线能力）
    e = Env("t26a", extra_env={"DISPATCH_PROFILE": "   "})
    _mk_executor(e.root)
    _mk_persona(e.root, "pb", prompt_text="# pb\n")
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        prompts = _argv_flag_pairs(e.read_argv(), "--append-system-prompt")
        ok("T26a 全空白 = 未设 → 只注入基线能力",
           rc == 0 and prompts == [EXEC_TEXT], repr(prompts)[:300])
    finally:
        e.cleanup([p])
    # ② 含逗号 = 已退役链式写法：WARN 点名成因 + 按未设处置（不硬失败）
    e = Env("t26b", extra_env={"DISPATCH_PROFILE": "executor,pb"})
    _mk_executor(e.root)
    _mk_persona(e.root, "pb", prompt_text="# pb\n")
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        err = p.stderr.read().decode("utf-8", "replace")
        prompts = _argv_flag_pairs(e.read_argv(), "--append-system-prompt")
        ok("T26b 链式写法按未设处置（只剩基线能力，pb 不注入）",
           rc == 0 and prompts == [EXEC_TEXT], repr(prompts)[:300])
        ok("T26b WARN 文案点名「已退役的链式写法」（诊断可达）",
           "含逗号" in err and "已退役的链式写法" in err, err[-400:])
    finally:
        e.cleanup([p])
    # ③ 非法名（穿越段）→ WARN 拒绝，任务形态仍前置基线能力
    e = Env("t26c", extra_env={"DISPATCH_PROFILE": "../evil"})
    _mk_executor(e.root)
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        err = p.stderr.read().decode("utf-8", "replace")
        prompts = _argv_flag_pairs(e.read_argv(), "--append-system-prompt")
        ok("T26c 非法名拒绝、基线能力照常注入",
           rc == 0 and prompts == [EXEC_TEXT], repr(prompts)[:300])
        ok("T26c 非法告警在场", "名字非法" in err, err[-400:])
    finally:
        e.cleanup([p])
    # ④ caps 内重复能力名 → 去重保序（正文恰注入一次）+ WARN
    e = Env("t26d", extra_env={"DISPATCH_PROFILE": "combo"})
    _mk_cap(e.root, "pa", prompt_text="# pa\n")
    _mk_cap(e.root, "pb", prompt_text="# pb\n")
    _mk_manifest(e.root, "combo", caps=["pa", "pb", "pa"])
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        err = p.stderr.read().decode("utf-8", "replace")
        prompts = _argv_flag_pairs(e.read_argv(), "--append-system-prompt")
        ok("T26d caps 重复名去重保序（pa 恰一次、序不变）",
           rc == 0 and prompts == ["# pa\n", "# pb\n"], repr(prompts))
        ok("T26d 重复告警在场", "重复" in err, err[-400:])
    finally:
        e.cleanup([p])


def t27_resident_multi_cap():
    """T27 resident 形态 + 多能力薄清单：装配两形态共用，resident 只按自身 spec.command 声明的
    单值 DISPATCH_PROFILE 装载、**不前置基线能力**（任务形态的前置由装配器硬规则承担）；
    无基线 -xt（行为不变）。"""
    e = Env("t27", fake_mode="hang_settle",
            extra_env={"AGENTD_RESIDENT": "1",
                       "AGENTD_SESSION_NAME": "bot/zz-combo-t27",
                       "DISPATCH_PROFILE": "combo"})
    _mk_executor(e.root)          # 基线能力在场也不该被 resident 装载
    _mk_cap(e.root, "base", prompt_text="# base persona\n")
    _mk_cap(e.root, "persona", prompt_text="# persona overlay\n")
    _mk_manifest(e.root, "combo", caps=["base", "persona"])
    p = e.start_wrap()
    try:
        ok("T27 sock 就位", e.wait_sock())
        e.wait_argv()
        argv = e.read_argv()
        prompts = _argv_flag_pairs(argv, "--append-system-prompt")
        ok("T27 resident 多能力按 caps 序注入、**不前置 executor**",
           prompts == ["# base persona\n", "# persona overlay\n"], repr(prompts))
        ok("T27 resident 无基线 -xt（无 ask_user）",
           _argv_flag_pairs(argv, "-xt") == [], repr(argv))
        ok("T27 无诊断", e.read_diag() is None)
    finally:
        e.cleanup([p])


KB_INDEX_SRC = os.path.join(HERE, os.pardir, "bots", "kb_index.py")


def _mk_kb(root, dom_rel, docs, copy_tool=True):
    """在临时树建知识域：<root>/<dom_rel>/ 下逐篇写 .md。
    docs = [(文件名, when 或 None)]；when 非空 → 写 frontmatter（入册），None → 不写（未入册）。
    copy_tool=True 把生产 bots/kb_index.py 拷进临时树（装配链路按文件路径导入，测的是真工具
    而非 mock）。返回域绝对路径。"""
    if copy_tool:
        d = os.path.join(root, "bots")
        os.makedirs(d, exist_ok=True)
        shutil.copy(KB_INDEX_SRC, os.path.join(d, "kb_index.py"))
    ddir = os.path.join(root, dom_rel)
    os.makedirs(ddir, exist_ok=True)
    for fn, when in docs:
        with open(os.path.join(ddir, fn), "w") as f:
            if when:
                f.write('---\nwhen: "%s"\n---\n\n# %s\n\n正文\n' % (when, fn))
            else:
                f.write("# %s\n\n正文（未入册）\n" % fn)
    return ddir


def t28_knowledge():
    """T28 knowledge 注入挂接：cap.yml 的 `knowledge` = **纯路径列表**（域级用途字段已退休）→
    装配时调 bots/kb_index.py 聚合「知识清单」块，以一个额外 --append-system-prompt 追加在
    **全部能力正文之后**；字段缺失 = 注入面逐字不变；跨能力并集去重保序；工具缺失/域不存在/
    字段非法/对象形态一律 WARN 降级不拖垮会话。"""
    # a) 正常注入：入册文档进清单（绝对路径）、未入册不进、域标题**不带描述**、顶在能力正文之后
    e = Env("t28a", extra_env={"DISPATCH_PROFILE": "mod"})
    ptext = "# moderator persona\n"
    _mk_persona(e.root, "mod", prompt_text=ptext,
                cap_yml={"knowledge": ["kb/dom"]})
    ddir = _mk_kb(e.root, os.path.join("kb", "dom"),
                  [("in.md", "要在做 X 时读"), ("out.md", None)])
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        argv = e.read_argv()
        prompts = _argv_flag_pairs(argv, "--append-system-prompt")
        ok("T28a 收敛退出 0", rc == 0, "rc=%s" % rc)
        ok("T28a 能力正文仍是第一个 --append-system-prompt（清单块在其后）",
           len(prompts) == 2 and prompts[0] == ptext, repr(prompts)[:300])
        blk = prompts[1] if len(prompts) == 2 else ""
        ok("T28a 知识清单块在场（标题 + 消费纪律）",
           "知识清单" in blk and "禁止预加载" in blk, blk[:300])
        ok("T28a 入册文档以绝对路径列出、文档级 when 照常、未入册不列",
           os.path.join(ddir, "in.md") in blk and "要在做 X 时读" in blk
           and os.path.join(ddir, "out.md") not in blk, blk[:400])
        dom_heads = [l for l in blk.split("\n") if l.startswith("### 域 ")]
        ok("T28a 域标题恰一个且不带描述（域级用途字段已退休）",
           len(dom_heads) == 1 and "——" not in dom_heads[0], repr(dom_heads))
        ok("T28a 无诊断", e.read_diag() is None)
    finally:
        e.cleanup([p])
    # b) 字段缺失 = 注入面逐字不变（只有一个 --append-system-prompt）
    e = Env("t28b", extra_env={"DISPATCH_PROFILE": "mod"})
    _mk_persona(e.root, "mod", prompt_text=ptext, cap_yml={"summary": "m"})
    _mk_kb(e.root, os.path.join("kb", "dom"), [("in.md", "w")])
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        prompts = _argv_flag_pairs(e.read_argv(), "--append-system-prompt")
        err = p.stderr.read().decode("utf-8", "replace")
        ok("T28b 无 knowledge 字段 → 注入面逐字不变（仅能力正文）",
           rc == 0 and prompts == [ptext], repr(prompts)[:300])
        ok("T28b 无 knowledge 日志/告警噪声", "knowledge" not in err, err[-300:])
    finally:
        e.cleanup([p])
    # c) kb 工具缺失 → WARN 降级，不注入、不拖垮
    e = Env("t28c", extra_env={"DISPATCH_PROFILE": "mod"})
    _mk_persona(e.root, "mod", prompt_text=ptext,
                cap_yml={"knowledge": ["kb/dom"]})
    _mk_kb(e.root, os.path.join("kb", "dom"), [("in.md", "w")], copy_tool=False)
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        prompts = _argv_flag_pairs(e.read_argv(), "--append-system-prompt")
        err = p.stderr.read().decode("utf-8", "replace")
        ok("T28c kb_index 缺失 → 会话照常（仅能力正文）",
           rc == 0 and prompts == [ptext], repr(prompts)[:300])
        ok("T28c 降级告警在场", "kb 索引工具缺失" in err, err[-300:])
        ok("T28c 无诊断", e.read_diag() is None)
    finally:
        e.cleanup([p])
    # d) 域不存在 → 块内可见降级说明（不静默消失、不报错退出）
    e = Env("t28d", extra_env={"DISPATCH_PROFILE": "mod"})
    _mk_persona(e.root, "mod", prompt_text=ptext,
                cap_yml={"knowledge": ["kb/ghost"]})
    _mk_kb(e.root, os.path.join("kb", "dom"), [("in.md", "w")])
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        prompts = _argv_flag_pairs(e.read_argv(), "--append-system-prompt")
        blk = prompts[1] if len(prompts) == 2 else ""
        ok("T28d 域不存在 → 块内可见降级说明",
           rc == 0 and "目录不存在" in blk and "kb/ghost" in blk.replace(os.sep, "/"),
           blk[:300])
    finally:
        e.cleanup([p])
    # e) 字段非数组 / 对象形态（域级用途写法已退休）→ WARN 跳过，注入面不变
    e = Env("t28e", extra_env={"DISPATCH_PROFILE": "mod"})
    _mk_persona(e.root, "mod", prompt_text=ptext, cap_yml={"knowledge": "kb/dom"})
    _mk_kb(e.root, os.path.join("kb", "dom"), [("in.md", "w")])
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        prompts = _argv_flag_pairs(e.read_argv(), "--append-system-prompt")
        err = p.stderr.read().decode("utf-8", "replace")
        ok("T28e knowledge 非数组 → WARN 跳过，注入面不变",
           rc == 0 and prompts == [ptext], repr(prompts)[:300])
        ok("T28e 非数组告警在场", "非数组" in err, err[-300:])
    finally:
        e.cleanup([p])
    e = Env("t28e2", extra_env={"DISPATCH_PROFILE": "mod"})
    _mk_persona(e.root, "mod", prompt_text=ptext,
                cap_yml={"knowledge": [{"path": "kb/dom", "when": "旧形态"}]})
    _mk_kb(e.root, os.path.join("kb", "dom"), [("in.md", "w")])
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        prompts = _argv_flag_pairs(e.read_argv(), "--append-system-prompt")
        err = p.stderr.read().decode("utf-8", "replace")
        ok("T28e2 对象形态（域级用途已退休）→ WARN 跳过，注入面不变",
           rc == 0 and prompts == [ptext], repr(prompts)[:300])
        ok("T28e2 退休告警在场", "已退休" in err, err[-400:])
    finally:
        e.cleanup([p])
    # f) 跨能力并集去重（同域只列一次）+ resident 形态同款注入
    e = Env("t28f", fake_mode="hang_settle",
            extra_env={"AGENTD_RESIDENT": "1",
                       "AGENTD_SESSION_NAME": "bot/zz-kb-t28",
                       "DISPATCH_PROFILE": "combo"})
    _mk_cap(e.root, "base", prompt_text="# base\n",
            cap_yml={"knowledge": ["kb/dom"]})
    _mk_cap(e.root, "mod", prompt_text=ptext,
            cap_yml={"knowledge": ["kb/dom", "kb/dom2"]})
    _mk_manifest(e.root, "combo", caps=["base", "mod"])
    _mk_kb(e.root, os.path.join("kb", "dom"), [("a.md", "wa")])
    _mk_kb(e.root, os.path.join("kb", "dom2"), [("b.md", "wb")])
    p = e.start_wrap()
    try:
        ok("T28f sock 就位", e.wait_sock())
        e.wait_argv()
        prompts = _argv_flag_pairs(e.read_argv(), "--append-system-prompt")
        blk = prompts[-1] if len(prompts) == 3 else ""
        ok("T28f resident：能力正文按 caps 序 + 末尾一个知识清单块",
           prompts[:2] == ["# base\n", ptext] and "知识清单" in blk,
           repr(prompts)[:300])
        dom_heads = [l for l in blk.split("\n") if l.startswith("### 域 ")]
        ok("T28f 跨能力同域去重（域标题 = 2 个不同域、同域只一次）且文档级 when 照常",
           len(dom_heads) == 2 and len(set(dom_heads)) == 2
           and "wa" in blk and "wb" in blk, repr(dom_heads) + blk[:400])
        ok("T28f 无诊断", e.read_diag() is None)
    finally:
        e.cleanup([p])


def t35_knowledge_tiers():
    """T35 knowledge 按名解析 + 注入模式三档（lore 引用模型批）：
    g) 三档各一节（library 逐册 when 恒递归两层 / desk journal 一行检索入口、账本与自建工作文件
       不注入 / archive 一行检索入口）；
    h) 名不可解析 ⇒ 一行降级说明进块（不静默消失）；
    i) lore 根不在场 ⇒ 一条点名根因的 WARN，会话照常起（降级不拖垮）；
    j) lore 名与 legacy 工作区路径混存 ⇒ 两档各一节 + 一行解析摘要日志；
    k) 名含 `..` ⇒ 拒绝（WARN），注入面不变；
    l) 名表缓存损坏 ⇒ 注入面零影响（缓存与注入链路隔离）。"""
    ptext = "# domain persona\n"

    def _lore_fixture(root, with_lore=True):
        """建 lore 三面夹具（library 两层 + desk/journal + archive）；with_lore=False 只放工具。"""
        _mk_kb(root, os.path.join("lore", "library", "dom", "facts"),
               [("a.md", "册 A 何时读"), ("noWhen.md", None)],
               copy_tool=True)                       # 顺带把生产 kb_index.py 拷进临时树
        if not with_lore:
            shutil.rmtree(os.path.join(root, "lore"), ignore_errors=True)
            return {}
        _mk_kb(root, os.path.join("lore", "library", "dom", "cases"),
               [("c.md", "案例 C 何时读")], copy_tool=False)
        _mk_kb(root, os.path.join("lore", "desk", "me", "journal"),
               [("2026-09.md", None)], copy_tool=False)
        _mk_kb(root, os.path.join("lore", "desk", "me"),
               [("todo.md", None)], copy_tool=False)
        _mk_kb(root, os.path.join("lore", "archive"),
               [("incidents-x.md", None)], copy_tool=False)
        lore = os.path.join(root, "lore")
        return {"lib": os.path.join(lore, "library", "dom"),
                "journal": os.path.join(lore, "desk", "me", "journal"),
                "desk": os.path.join(lore, "desk", "me"),
                "archive": os.path.join(lore, "archive")}

    # g) 三档各一节
    e = Env("t35g", extra_env={"DISPATCH_PROFILE": "mod"})
    _mk_persona(e.root, "mod", prompt_text=ptext,
                cap_yml={"knowledge": ["library/dom", "desk/me", "archive"]})
    d = _lore_fixture(e.root)
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        err = p.stderr.read().decode("utf-8", "replace")
        prompts = _argv_flag_pairs(e.read_argv(), "--append-system-prompt")
        blk = prompts[1] if len(prompts) == 2 else ""
        ok("T35g 收敛退出 0 且清单块顶在能力正文之后",
           rc == 0 and len(prompts) == 2 and prompts[0] == ptext, repr(prompts)[:200])
        ok("T35g library 档 = 逐册 when 表且恒递归（facts 与 cases 两层都进表）",
           "### 知识库 `library/dom`" in blk
           and os.path.join(d["lib"], "facts", "a.md") in blk
           and os.path.join(d["lib"], "cases", "c.md") in blk
           and "册 A 何时读" in blk and "案例 C 何时读" in blk, blk[:600])
        ok("T35g library 档未入册册不列", "noWhen.md" not in blk, blk[:400])
        ok("T35g desk 档 = journal 一行检索入口（含 grep 与 git log -S 两种检索）",
           "### 书桌 `desk/me`" in blk and d["journal"] in blk
           and "grep -rn <关键词>" in blk and "log -S<关键词>" in blk, blk[:600])
        ok("T35g desk 档不逐册列 journal、账本与自建工作文件不注入清单",
           "2026-09.md" not in blk and "**不注入清单**" in blk and "todo.md" in blk,
           blk[:600])
        ok("T35g archive 档 = 一行检索入口（不注入清单）",
           "### 档案库 `archive`" in blk and d["archive"] in blk
           and "incidents-x.md" not in blk and "incidents-" in blk, blk[:600])
        ok("T35g 块头三句消费纪律在场",
           all(x in blk for x in ("## 知识清单", "按需读取", "禁止预加载", "以权威为准")),
           blk[:300])
        ok("T35g 解析摘要日志一行（lore 档按层计数、legacy 0 项）",
           "knowledge 名解析：lore 档 3 项（archive×1, desk×1, library×1），"
           "legacy 工作区路径档 0 项" in err, err[-500:])
        ok("T35g 无诊断", e.read_diag() is None)
    finally:
        e.cleanup([p])

    # h) 名不可解析 ⇒ 一行降级说明进块
    e = Env("t35h", extra_env={"DISPATCH_PROFILE": "mod"})
    _mk_persona(e.root, "mod", prompt_text=ptext,
                cap_yml={"knowledge": ["library/ghost", "desk/me"]})
    d = _lore_fixture(e.root)
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        prompts = _argv_flag_pairs(e.read_argv(), "--append-system-prompt")
        blk = prompts[1] if len(prompts) == 2 else ""
        ok("T35h 名不可解析 ⇒ 块内一行降级说明（不静默）+ 可解析面照常",
           rc == 0 and "不可解析" in blk and "library/ghost" in blk
           and "### 书桌 `desk/me`" in blk, blk[:400])
    finally:
        e.cleanup([p])

    # i) lore 根不在场 ⇒ WARN 点名根因、会话照常
    e = Env("t35i", extra_env={"DISPATCH_PROFILE": "mod"})
    _mk_persona(e.root, "mod", prompt_text=ptext,
                cap_yml={"knowledge": ["library/dom"]})
    _lore_fixture(e.root, with_lore=False)
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        err = p.stderr.read().decode("utf-8", "replace")
        prompts = _argv_flag_pairs(e.read_argv(), "--append-system-prompt")
        blk = prompts[1] if len(prompts) == 2 else ""
        ok("T35i lore 根不在场 ⇒ 会话照常起 + WARN 点名根因 + 块内降级说明",
           rc == 0 and "lore 仓根不在场" in err and "不可解析" in blk
           and e.read_diag() is None, err[-400:] + blk[:200])
        ok("T35i 解析摘要标出 lore 根不在场", "不在场" in err, err[-400:])
    finally:
        e.cleanup([p])

    # j) lore 名与 legacy 工作区路径混存
    e = Env("t35j", extra_env={"DISPATCH_PROFILE": "mod"})
    _mk_persona(e.root, "mod", prompt_text=ptext,
                cap_yml={"knowledge": ["library/dom", "kb/legacy"]})
    d = _lore_fixture(e.root)
    _mk_kb(e.root, os.path.join("kb", "legacy"), [("l.md", "legacy 册何时读")],
           copy_tool=False)
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        err = p.stderr.read().decode("utf-8", "replace")
        prompts = _argv_flag_pairs(e.read_argv(), "--append-system-prompt")
        blk = prompts[1] if len(prompts) == 2 else ""
        ok("T35j 两档混存 ⇒ 各一节且顺序照声明（lore 档在前）",
           rc == 0 and "### 知识库 `library/dom`" in blk
           and blk.index("### 知识库") < blk.index("### 域 ")
           and "legacy 册何时读" in blk, blk[:500])
        ok("T35j legacy 档一条聚合 WARN + 解析摘要计数",
           "legacy 档" in err and "lore 档 1 项（library×1），legacy 工作区路径档 1 项" in err,
           err[-500:])
    finally:
        e.cleanup([p])

    # k) 名含 `..` ⇒ 拒绝
    e = Env("t35k", extra_env={"DISPATCH_PROFILE": "mod"})
    _mk_persona(e.root, "mod", prompt_text=ptext, cap_yml={"knowledge": ["../evil"]})
    _lore_fixture(e.root)
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        err = p.stderr.read().decode("utf-8", "replace")
        prompts = _argv_flag_pairs(e.read_argv(), "--append-system-prompt")
        ok("T35k 名含 `..` ⇒ WARN 拒绝、注入面只有能力正文",
           rc == 0 and prompts == [ptext] and "`..` 路径段" in err, repr(prompts)[:200] + err[-300:])
    finally:
        e.cleanup([p])

    # l) 名表缓存损坏 ⇒ 注入面零影响（缓存与注入链路隔离）
    for label, payload in (("损坏 JSON", "{not json"), ("口径版本不匹配", None)):
        e = Env("t35l", extra_env={"DISPATCH_PROFILE": "mod"})
        _mk_persona(e.root, "mod", prompt_text=ptext,
                    cap_yml={"knowledge": ["library/dom", "desk/me", "archive"]})
        d = _lore_fixture(e.root)
        cdir = os.path.join(e.root, "run", "kb-index")
        os.makedirs(cdir, exist_ok=True)
        if payload is None:
            payload = json.dumps({"version": -1, "root": e.root, "docs": {}})
        with open(os.path.join(cdir, "names.json"), "w") as f:
            f.write(payload)
        p = e.start_wrap()
        try:
            rc = p.wait(timeout=20)
            prompts = _argv_flag_pairs(e.read_argv(), "--append-system-prompt")
            blk = prompts[1] if len(prompts) == 2 else ""
            ok("T35l 名表缓存%s ⇒ 三档清单仍完整（注入链路不读缓存）" % label,
               rc == 0 and "### 知识库 `library/dom`" in blk
               and "### 书桌 `desk/me`" in blk and "### 档案库 `archive`" in blk
               and os.path.join(d["lib"], "facts", "a.md") in blk
               and e.read_diag() is None, blk[:400])
        finally:
            e.cleanup([p])


def t32_cap_degradation():
    """T32 能力装配降级分支专项（资产面异常只降级不硬失败）：
    ① 无 cap.yml = 纯正文能力（WARN + 只注入正文）；② cap.yml 损坏/顶层非 mapping ⇒ 跳过
    该能力（含正文）；③ bundle 能力（无 prompt.md）合法 ⇒ info 日志、**不是 WARN**；
    ④ 捆绑 skill 缺失 ⇒ WARN 跳过且**不回落全局**；⑤ cap.yml 禁键（caps）⇒ WARN 忽略该键。"""
    # ① 纯正文能力（无 cap.yml）
    e = Env("t32a", extra_env={"DISPATCH_PROFILE": "plain"})
    _mk_cap(e.root, "plain", prompt_text="# plain cap\n", cap_yml=None)
    _mk_manifest(e.root, "plain")
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        err = p.stderr.read().decode("utf-8", "replace")
        ok("T32a 无 cap.yml → 正文照常注入（纯正文能力）",
           rc == 0 and _argv_flag_pairs(e.read_argv(), "--append-system-prompt")
           == ["# plain cap\n"], repr(err)[-300:])
        ok("T32a WARN 点名「按纯正文能力处理」", "纯正文能力" in err, err[-400:])
    finally:
        e.cleanup([p])
    # ② cap.yml 损坏（YAML 语法错）⇒ 跳过该能力（含正文）
    e = Env("t32b", extra_env={"DISPATCH_PROFILE": "combo"})
    _mk_cap(e.root, "bad", prompt_text="# bad cap\n",
            cap_yml="summary: [未闭合\n\ttools: {")
    _mk_cap(e.root, "good", prompt_text="# good cap\n", cap_yml={"summary": "g"})
    _mk_manifest(e.root, "combo", caps=["bad", "good"])
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        err = p.stderr.read().decode("utf-8", "replace")
        prompts = _argv_flag_pairs(e.read_argv(), "--append-system-prompt")
        ok("T32b cap.yml 损坏 → 跳过该能力（正文也不注入），其余能力照常",
           rc == 0 and prompts == ["# good cap\n"], repr(prompts)[:300])
        ok("T32b 损坏告警点名「跳过该能力（含正文注入）」",
           "解析失败" in err and "含正文注入" in err, err[-400:])
        ok("T32b 无诊断", e.read_diag() is None)
    finally:
        e.cleanup([p])
    # ②' cap.yml 顶层非 mapping（YAML 合法但不是声明对象）
    e = Env("t32b2", extra_env={"DISPATCH_PROFILE": "scalar"})
    _mk_cap(e.root, "scalar", prompt_text="# s\n", cap_yml="- a\n- b\n")
    _mk_manifest(e.root, "scalar")
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        err = p.stderr.read().decode("utf-8", "replace")
        ok("T32b2 顶层非 mapping → 跳过该能力 + WARN",
           rc == 0 and _argv_flag_pairs(e.read_argv(), "--append-system-prompt") == []
           and "顶层非 mapping" in err, err[-400:])
    finally:
        e.cleanup([p])
    # ③ bundle 能力（无 prompt.md、有捆绑声明）= 合法形态
    e = Env("t32c", extra_env={"DISPATCH_PROFILE": "bundle"})
    _mk_skill(e.root, "kit-skill")
    _mk_cap(e.root, "kit", prompt_text=None, cap_yml={"skills": ["kit-skill"]})
    _mk_manifest(e.root, "bundle", caps=["kit"])
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        err = p.stderr.read().decode("utf-8", "replace")
        argv = e.read_argv()
        ok("T32c bundle 能力：无正文注入、捆绑 skill 照常",
           rc == 0 and _argv_flag_pairs(argv, "--append-system-prompt") == []
           and [os.path.basename(x) for x in _argv_flag_pairs(argv, "--skill")]
           == ["kit-skill"], repr(argv))
        ok("T32c bundle 是合法形态 ⇒ 不刷 WARN（只有 info 行）",
           "bundle 能力" in err and "WARN: 能力 'kit' 无 prompt.md" not in err,
           err[-400:])
    finally:
        e.cleanup([p])
    # ④ 捆绑 skill 缺失 ⇒ WARN 跳过、不回落全局（全局 skills/ 有同名目录也不注入）
    e = Env("t32d", extra_env={"DISPATCH_PROFILE": "review"})
    decoy = os.path.join(e.root, "skills", "ghost-skill")   # 全局层诱饵（同名，不得被回落命中）
    os.makedirs(decoy, exist_ok=True)
    _w(os.path.join(decoy, "SKILL.md"), "# decoy\n")
    _mk_persona(e.root, "review", cap_yml={"skills": ["ghost-skill"]})
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        err = p.stderr.read().decode("utf-8", "replace")
        argv = e.read_argv()
        ok("T32d 缺失 skill 被跳过且未回落全局（无 --skill 指向诱饵目录）",
           rc == 0 and _argv_flag_pairs(argv, "--skill") == [], repr(argv))
        ok("T32d WARN 点名「只解析 bots/skills/ 一级、不回落全局」",
           "不回落全局" in err, err[-400:])
    finally:
        e.cleanup([p])
    # ⑤ cap.yml 禁键 caps（能力不得引用能力）⇒ WARN 忽略、不展开
    e = Env("t32e", extra_env={"DISPATCH_PROFILE": "outer"})
    _mk_cap(e.root, "selfref", prompt_text="# selfref\n",
            cap_yml={"caps": ["nested"], "summary": "s"})
    _mk_cap(e.root, "nested", prompt_text="# nested\n")
    _mk_manifest(e.root, "outer", caps=["selfref"])
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        err = p.stderr.read().decode("utf-8", "replace")
        prompts = _argv_flag_pairs(e.read_argv(), "--append-system-prompt")
        ok("T32e 能力引用能力被拒（nested 正文未注入）",
           rc == 0 and prompts == ["# selfref\n"], repr(prompts)[:300])
        ok("T32e 非法键 WARN 在场（点名合法键闭合集）",
           "非法键" in err and "caps" in err, err[-400:])
    finally:
        e.cleanup([p])


def t33_injection_order_equivalence():
    """T33 注入序等价性（链退役后由装配器硬规则承担 executor 前置）：
    ① 任务形态：caps 里**未列** executor ⇒ 仍恒首注入；② caps 里**非首位列** executor ⇒
    提到首位并去重（恰一份，WARN 点名）；②' caps 首位就是 executor（如 `executor` profile 自身）⇒
    静默去重不刷 WARN；③ resident 形态：即使 caps 列了 executor 也照常按 caps 序（resident 不前置基线，
    但显式声明合法）；④ 显式人格恒在基线之后。"""
    # ① 任务形态 + 未列 executor
    e = Env("t33a", extra_env={"DISPATCH_PROFILE": "review"})
    _mk_executor(e.root)
    _mk_persona(e.root, "review", prompt_text="# reviewer\n")
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        prompts = _argv_flag_pairs(e.read_argv(), "--append-system-prompt")
        ok("T33a 任务形态 executor 正文恒首、显式人格在后",
           rc == 0 and prompts == [EXEC_TEXT, "# reviewer\n"], repr(prompts)[:300])
    finally:
        e.cleanup([p])
    # ② 任务形态 + caps 显式列 executor（冗余声明）
    e = Env("t33b", extra_env={"DISPATCH_PROFILE": "combo"})
    _mk_executor(e.root)
    _mk_cap(e.root, "review", prompt_text="# reviewer\n")
    _mk_manifest(e.root, "combo", caps=["review", "executor"])
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        err = p.stderr.read().decode("utf-8", "replace")
        prompts = _argv_flag_pairs(e.read_argv(), "--append-system-prompt")
        ok("T33b 列在非首位的去重：executor 恰一份且被提到首（不因 caps 序而后移）",
           rc == 0 and prompts == [EXEC_TEXT, "# reviewer\n"], repr(prompts)[:300])
        ok("T33b 非首位声明 WARN 在场（作者意图与「基线恒首」不一致）",
           "非首位" in err and "提到首位" in err, err[-400:])
    finally:
        e.cleanup([p])
    # ③ resident 形态 + caps 显式列 executor（合法，按 caps 序、不额外前置）
    e = Env("t33c", fake_mode="hang_settle",
            extra_env={"AGENTD_RESIDENT": "1",
                       "AGENTD_SESSION_NAME": "bot/zz-order-t33",
                       "DISPATCH_PROFILE": "combo"})
    _mk_executor(e.root)
    _mk_cap(e.root, "review", prompt_text="# reviewer\n")
    _mk_manifest(e.root, "combo", caps=["review", "executor"])
    p = e.start_wrap()
    try:
        ok("T33c sock 就位", e.wait_sock())
        e.wait_argv()
        prompts = _argv_flag_pairs(e.read_argv(), "--append-system-prompt")
        ok("T33c resident 按 caps 序（review 在前）、不额外前置基线",
           prompts == ["# reviewer\n", EXEC_TEXT], repr(prompts)[:300])
    finally:
        e.cleanup([p])
    # ④ 任务形态 + profile 无 caps 字段 ⇒ 只剩基线能力（WARN 点名）
    e = Env("t33d", extra_env={"DISPATCH_PROFILE": "nocaps"})
    _mk_executor(e.root)
    _mk_manifest(e.root, "nocaps", raw=json.dumps({"name": "nocaps"}))
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        err = p.stderr.read().decode("utf-8", "replace")
        prompts = _argv_flag_pairs(e.read_argv(), "--append-system-prompt")
        ok("T33d 薄清单无 caps → 只注入基线能力 + WARN",
           rc == 0 and prompts == [EXEC_TEXT] and "无 caps 字段" in err,
           repr(prompts)[:200] + err[-300:])
    finally:
        e.cleanup([p])
    # ②' caps 首位就是基线能力（`executor` profile 自身的形态，也是存量 spec.command
    # `DISPATCH_PROFILE=executor` 的展开结果）⇒ 静默去重、零 WARN 噪声
    e = Env("t33e", extra_env={"DISPATCH_PROFILE": "executor"})
    _mk_executor(e.root)
    _mk_manifest(e.root, "executor", caps=["executor"])
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        err = p.stderr.read().decode("utf-8", "replace")
        prompts = _argv_flag_pairs(e.read_argv(), "--append-system-prompt")
        warns = [l for l in err.split("\n") if "WARN" in l]
        ok("T33e caps 首位即基线 ⇒ 恰一份正文且不刷基线去重 WARN（存量 "
           "DISPATCH_PROFILE=executor 形态无噪声；其余 WARN 如探针扩展缺失不属本判据）",
           rc == 0 and prompts == [EXEC_TEXT]
           and not [l for l in warns if "caps" in l or "非首位" in l or "executor" in l],
           repr(prompts)[:200] + json.dumps(warns, ensure_ascii=False)[:400])
    finally:
        e.cleanup([p])


def t34_profile_banned_fields():
    """T34 profile 薄清单的直挂禁字段（profile 只列 caps，不给逃生口）：直挂 skills/tools/
    knowledge ⇒ WARN 忽略该字段（注入面不受影响）；损坏 JSON / 顶层非对象 ⇒ WARN 跳过。"""
    e = Env("t34a", extra_env={"DISPATCH_PROFILE": "leaky"})
    _mk_skill(e.root, "should-not-load")
    _mk_cap(e.root, "cap-ok", prompt_text="# cap ok\n", cap_yml={"summary": "s"})
    _mk_manifest(e.root, "leaky", caps=["cap-ok"],
                 extra={"skills": ["should-not-load"], "tools": ["read"],
                        "knowledge": ["kb/dom"], "excludeTools": ["bash"]})
    _mk_kb(e.root, os.path.join("kb", "dom"), [("in.md", "w")])
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        err = p.stderr.read().decode("utf-8", "replace")
        argv = e.read_argv()
        ok("T34a 直挂禁字段一律被忽略（无 --skill/-t/知识清单块，-xt 只有基线）",
           rc == 0 and _argv_flag_pairs(argv, "--skill") == []
           and "-t" not in (argv or [])
           and _argv_flag_pairs(argv, "--append-system-prompt") == ["# cap ok\n"]
           and _argv_flag_pairs(argv, "-xt") == ["ask_user"], repr(argv)[:300])
        ok("T34a 四个禁字段各有 WARN（点名「profile 只列 caps」）",
           err.count("清单直挂") == 4 and "profile 只列 caps" in err, err[-600:])
    finally:
        e.cleanup([p])
    # ② 损坏 JSON / 顶层非对象
    e = Env("t34b", extra_env={"DISPATCH_PROFILE": "broken"})
    _mk_executor(e.root)
    _mk_manifest(e.root, "broken", raw="{ 不是 JSON")
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        err = p.stderr.read().decode("utf-8", "replace")
        ok("T34b 清单损坏 → WARN 跳过、任务形态仍注入基线能力",
           rc == 0 and _argv_flag_pairs(e.read_argv(), "--append-system-prompt")
           == [EXEC_TEXT] and "不可读/损坏" in err, err[-300:])
    finally:
        e.cleanup([p])
    e = Env("t34c", extra_env={"DISPATCH_PROFILE": "arr"})
    _mk_executor(e.root)
    _mk_manifest(e.root, "arr", raw='["not", "an", "object"]')
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        err = p.stderr.read().decode("utf-8", "replace")
        ok("T34c 清单顶层非对象 → WARN 跳过、基线能力照常",
           rc == 0 and _argv_flag_pairs(e.read_argv(), "--append-system-prompt")
           == [EXEC_TEXT] and "顶层非对象" in err, err[-300:])
    finally:
        e.cleanup([p])


EXEC_PROFILE_MODEL = "llm-router/executor"      # 生产 bots/profiles/executor.json 的 model 现值
PLANNER_MODEL = "llm-router/planner"            # 生产常驻 profile（dispatcher/*-lead）的 model 现值


def t36_task_model_fallback():
    """T36 任务形态缺省回落 `executor` profile（模型角色档接入面）：`model` 只住 profile ⇒
    未设 DISPATCH_PROFILE 的任务形态回落缺省 profile 拿它的 model。四态：
    ① 非 resident ∧ 无 profile ⇒ --model = 回落面的值，且 caps 与回落前逐字一致（executor 首位、无重复）；
    ② 非 resident ∧ 显式合法 profile ⇒ model 来自该 profile、**不打回落日志**；
    ③ resident ∧ 显式 profile ⇒ 该 profile 的 model；resident ∧ 无 profile ⇒ **不回落**、无 --model；
    ④ fail-soft（硬要求）：回落面缺席 / 无 model 字段 / model 非字符串 ⇒ 无 --model、wrap 不失败、WARN 在场。
    夹具全在临时树（`Env.root`），**不动生产 `bots/profiles/`**。"""
    # ① 非 resident ∧ 无 DISPATCH_PROFILE ⇒ 回落 executor profile
    e = Env("t36a")
    _mk_executor(e.root)
    _mk_manifest(e.root, "executor", caps=["executor"], model=EXEC_PROFILE_MODEL)
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        argv = e.read_argv()
        err = p.stderr.read().decode("utf-8", "replace")
        ok("T36a 任务形态无 profile ⇒ --model 恰一个且 = 回落面的值", 
           rc == 0 and _argv_flag_pairs(argv, "--model") == [EXEC_PROFILE_MODEL],
           "rc=%s argv=%r" % (rc, argv))
        ok("T36a caps 与回落前逐字一致（基线能力首位、无重复：正文恰一份）",
           _argv_flag_pairs(argv, "--append-system-prompt") == [EXEC_TEXT], repr(argv)[:300])
        ok("T36a 基线 -xt ask_user 仍在场（回落不改变工具面基线）",
           _argv_flag_pairs(argv, "-xt") == ["ask_user"], repr(argv))
        ok("T36a 回落日志在场（点名未设 DISPATCH_PROFILE + 回落面 + 最终 model 值）",
           "未设 DISPATCH_PROFILE" in err and "回落" in err and EXEC_PROFILE_MODEL in err,
           err[-400:])
        ok("T36a 无诊断（回落不是异常）", e.read_diag() is None)
    finally:
        e.cleanup([p])
    # ② 非 resident ∧ 显式合法 profile ⇒ model 来自该 profile、不打回落日志
    e = Env("t36b", extra_env={"DISPATCH_PROFILE": "review"})
    _mk_executor(e.root)
    _mk_persona(e.root, "review", prompt_text="# review persona\n",
                model=EXEC_PROFILE_MODEL)
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        argv = e.read_argv()
        err = p.stderr.read().decode("utf-8", "replace")
        ok("T36b 显式 review profile ⇒ --model 来自 review.json",
           rc == 0 and _argv_flag_pairs(argv, "--model") == [EXEC_PROFILE_MODEL],
           "rc=%s argv=%r" % (rc, argv))
        ok("T36b 基线仍恒首 + 人格叠加（序不变）",
           _argv_flag_pairs(argv, "--append-system-prompt") == [EXEC_TEXT, "# review persona\n"],
           repr(argv)[:300])
        ok("T36b 不打回落日志（显式指定 = 作者有意图）", "未设 DISPATCH_PROFILE" not in err,
           err[-400:])
    finally:
        e.cleanup([p])
    # ③ resident 形态：显式 profile 拿它的 model；无 profile **不回落**
    e = Env("t36c", fake_mode="hang_settle",
            extra_env={"AGENTD_RESIDENT": "1",
                       "AGENTD_SESSION_NAME": "bot/zz-lead-t36",
                       "DISPATCH_PROFILE": "agentfw-lead"})
    _mk_executor(e.root)          # 基线能力在场也不该被 resident 装载
    _mk_cap(e.root, "lead", prompt_text="# lead persona\n")
    _mk_manifest(e.root, "agentfw-lead", caps=["lead"], model=PLANNER_MODEL)
    p = e.start_wrap()
    try:
        ok("T36c sock 就位", e.wait_sock())
        e.wait_argv()
        argv = e.read_argv()
        ok("T36c resident + 常驻 profile ⇒ --model = planner 角色档",
           _argv_flag_pairs(argv, "--model") == [PLANNER_MODEL], repr(argv))
        ok("T36c resident 不前置基线能力（行为不变）",
           _argv_flag_pairs(argv, "--append-system-prompt") == ["# lead persona\n"],
           repr(argv)[:300])
    finally:
        e.cleanup([p])
    e = Env("t36d", fake_mode="hang_settle",
            extra_env={"AGENTD_RESIDENT": "1",
                       "AGENTD_SESSION_NAME": "bot/zz-noprofile-t36"})
    _mk_executor(e.root)
    _mk_manifest(e.root, "executor", caps=["executor"], model=EXEC_PROFILE_MODEL)
    p = e.start_wrap()
    try:
        ok("T36d sock 就位", e.wait_sock())
        e.wait_argv()
        argv = e.read_argv()
        ok("T36d resident 无 profile ⇒ **不回落**（无 --model、argv 逐字不变）",
           "--model" not in (argv or [])
           and _argv_flag_pairs(argv, "--append-system-prompt") == []
           and _argv_flag_pairs(argv, "-xt") == [], repr(argv))
    finally:
        e.cleanup([p])
    # resident 不收敛（hang_settle）⇒ stderr 只能在杀完进程组后读（否则 read() 无限阻塞 = 假活）
    err = ""
    try:
        p.wait(timeout=10)
        err = p.stderr.read().decode("utf-8", "replace")
    except Exception as ex:
        err = "(stderr 不可读: %r)" % (ex,)
    ok("T36d resident 不打回落日志（回落只属任务形态）",
       "未设 DISPATCH_PROFILE" not in err, err[-400:])
    # ④ fail-soft 三子态（夹具里做，不动生产 executor.json）
    # ④-1 回落面缺席
    e = Env("t36e")
    _mk_executor(e.root)          # 只有 executor **能力**，无 executor **profile 清单**
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        argv = e.read_argv()
        err = p.stderr.read().decode("utf-8", "replace")
        ok("T36e 回落面缺席 ⇒ 不 die（收敛退出 0）且不注入 --model",
           rc == 0 and "--model" not in (argv or []), "rc=%s argv=%r" % (rc, argv))
        ok("T36e WARN 在场（点名回落 + 不注入 --model 的成因）",
           "未设 DISPATCH_PROFILE" in err and "不注入 --model" in err, err[-400:])
        ok("T36e 基线能力照常注入（fail-soft 不伤人格面）",
           _argv_flag_pairs(argv, "--append-system-prompt") == [EXEC_TEXT], repr(argv)[:300])
        ok("T36e 无诊断（不属未捕获异常兜底）", e.read_diag() is None)
    finally:
        e.cleanup([p])
    # ④-2 回落面在场但无 `model` 字段
    e = Env("t36f")
    _mk_executor(e.root)
    _mk_manifest(e.root, "executor", caps=["executor"])
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        argv = e.read_argv()
        err = p.stderr.read().decode("utf-8", "replace")
        ok("T36f 回落面无 model 字段 ⇒ 不 die + 不注入 --model",
           rc == 0 and "--model" not in (argv or []), "rc=%s argv=%r" % (rc, argv))
        ok("T36f WARN 在场（fail-soft 分支可归因）",
           "未设 DISPATCH_PROFILE" in err and "不注入 --model" in err, err[-400:])
    finally:
        e.cleanup([p])
    # ④-3 回落面 `model` 非非空字符串
    e = Env("t36g")
    _mk_executor(e.root)
    _mk_manifest(e.root, "executor", caps=["executor"], model=123)
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        argv = e.read_argv()
        err = p.stderr.read().decode("utf-8", "replace")
        ok("T36g model 类型非法 ⇒ 不 die + 不注入 --model",
           rc == 0 and "--model" not in (argv or []), "rc=%s argv=%r" % (rc, argv))
        ok("T36g 两条 WARN：类型告警 + 回落 fail-soft 告警",
           "model 字段非非空字符串" in err and "不注入 --model" in err, err[-400:])
        ok("T36g 基线能力照常注入",
           _argv_flag_pairs(argv, "--append-system-prompt") == [EXEC_TEXT], repr(argv)[:300])
    finally:
        e.cleanup([p])


CC_EXT_REL = os.path.join("bots", "extensions", "context-compaction", "index.ts")


def _mk_cc_ext(root, present=True):
    """建（present=False 则不建）profile `contextCompaction` 的执行体扩展单元（桩文件即可：
    本组断言的是装配面，不跑真 pi）。返回其绝对路径。"""
    p = os.path.join(root, CC_EXT_REL)
    if present:
        _w(p, "// stub extension\nexport default function () {}\n")
    return p


def _mk_pi_env_shim(e, keys=("AGENTD_CONTEXT_COMPACTION",)):
    """把 Env 的 pi 可执行换成一个壳：先把关心的 env 落 `<flags>/cc_env`（JSON，缺失 = null），
    再 exec 真 fakepi。断言「wrap 到底给 pi 传了什么 env」需要子进程侧的取证面；fakepi_rpc.py
    的既有快照只含就绪门两枚变量，故用壳补一层（不改被测件、也不改 fake 的既有语义）。
    返回 cc_env 文件路径。"""
    dumper = os.path.join(e.root, "env-dump.py")
    _w(dumper, "import json, os, sys\n"
               "out = os.path.join(os.environ['FAKE_FLAG_DIR'], 'cc_env')\n"
               "with open(out, 'w') as g:\n"
               "    json.dump({k: os.environ.get(k) for k in sys.argv[1:]}, g)\n")
    shim = os.path.join(e.root, "pi-shim.sh")
    _w(shim, "#!/bin/bash\npython3 '%s' %s\nexec python3 '%s' \"$@\"\n"
       % (dumper, " ".join(keys), FAKEPI))
    os.chmod(shim, 0o755)
    e.env["AGENTD_WRAP_PI_BIN"] = shim
    return os.path.join(e.flags, "cc_env")


def _read_cc_env(e):
    """壳落的 env 快照 → dict ∨ None（未落盘）。值 = 该 env 在 pi 子进程里的真值（缺失 = None）。"""
    p = os.path.join(e.flags, "cc_env")
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def t37_cc_assembly():
    """T37 profile 的 `contextCompaction`（每 profile 的上下文压缩策略）装配面。

    四种装配形态 + resident 同等 + 注入位：
      ① 合法策略 ⇒ env（归一化紧凑 JSON）+ `-e` 执行体同时在场，且 `-e` 位在能力注入之后；
      ② 无策略 ⇒ 两者都不在场（执行体在场也不注：开关是策略不是文件），且从宿主继承的陈旧
         同名 env 被显式洗掉（「无策略 = env 不在场」是硬语义）；
      ③ 非法策略（类型错 / 白名单外的键 / 无触发点）⇒ WARN + 不注入，会话照起（rc=0）；
      ④ 策略合法但执行体文件缺失 ⇒ WARN + env/`-e` 两者都不注（同进同退）；
      ⑤ resident 形态同等装配（策略住 profile，与形态无关）。"""
    # ① 合法策略
    e = Env("t37a", extra_env={"DISPATCH_PROFILE": "cc"})
    _mk_executor(e.root)
    _mk_cap(e.root, "cc", prompt_text="# cc cap\n", cap_yml={"summary": "s"})
    _mk_manifest(e.root, "cc", caps=["executor", "cc"],
                 extra={"contextCompaction": {"triggerRatio": 0.8,
                                              "triggerTokens": 120000,
                                              "customInstructions": "侧重代码"}})
    ext = _mk_cc_ext(e.root)
    _mk_pi_env_shim(e)
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        argv = e.read_argv()
        err = p.stderr.read().decode("utf-8", "replace")
        envdoc = _read_cc_env(e)
        raw = (envdoc or {}).get("AGENTD_CONTEXT_COMPACTION")
        ok("T37a 收敛退出 0", rc == 0, "rc=%s" % rc)
        ok("T37a `-e` 执行体恰一份且指向 <root>/" + CC_EXT_REL,
           _argv_flag_pairs(argv, "-e").count(ext) == 1 and ext.endswith(CC_EXT_REL),
           repr(argv)[:400])
        ok("T37a 注入位在能力之后（`-e` 下标 > 最后一个 --append-system-prompt 下标）",
           argv is not None and argv.index(ext)
           > max(i for i, a in enumerate(argv) if a == "--append-system-prompt"),
           repr(argv)[:400])
        ok("T37a env 在场且 = 归一化紧凑 JSON（enabled 缺省物化为 true，无空格）",
           raw is not None and " " not in raw
           and json.loads(raw) == {"enabled": True, "triggerRatio": 0.8,
                                   "triggerTokens": 120000,
                                   "customInstructions": "侧重代码"},
           repr(raw))
        ok("T37a 装配日志可取证（trigger/ratio/enabled/ext 四件齐）",
           "contextCompaction 装配：trigger=120000 ratio=0.8 enabled=True ext=" in err
           and ext in err, err[-600:])
        ok("T37a 无 WARN（合法策略不刷噪声）", "contextCompaction" not in
           "".join(l for l in err.splitlines(True) if "WARN" in l), err[-600:])
    finally:
        e.cleanup([p])

    # ② 无策略（执行体在场也不注）+ 宿主陈旧 env 被洗掉
    e = Env("t37b", extra_env={"DISPATCH_PROFILE": "executor",
                               "AGENTD_CONTEXT_COMPACTION": '{"enabled":false}'})
    _mk_executor(e.root)
    _mk_manifest(e.root, "executor", caps=["executor"], model=EXEC_PROFILE_MODEL)
    ext = _mk_cc_ext(e.root)
    _mk_pi_env_shim(e)
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        argv = e.read_argv()
        err = p.stderr.read().decode("utf-8", "replace")
        envdoc = _read_cc_env(e)
        ok("T37b 无策略 ⇒ 不注 `-e`、不传 env（宿主陈旧值也被显式洗掉）",
           rc == 0 and envdoc is not None
           and envdoc.get("AGENTD_CONTEXT_COMPACTION") is None
           and ext not in _argv_flag_pairs(argv, "-e"),
           "rc=%s env=%r argv=%r" % (rc, envdoc, argv)[:400])
        ok("T37b 不打装配日志（零行为变更：现役 profile 一律不声明该字段）",
           "contextCompaction" not in err, err[-400:])
    finally:
        e.cleanup([p])

    # ③ 非法策略：类型错 / 白名单外的键（含不可达面）/ 无触发点
    for tag, bad, needle in (
            ("t37c1", {"triggerTokens": "abc"}, "triggerTokens 非正整数"),
            ("t37c2", {"triggerToken": 100}, "白名单外的键"),
            ("t37c3", {"keepRecentTokens": 5, "triggerTokens": 100}, "keepRecentTokens"),
            ("t37c4", {}, "既无 triggerTokens 也无 triggerRatio"),
            ("t37c5", {"triggerRatio": 1.5}, "triggerRatio 非 0<r<=1"),
            ("t37c6", {"triggerTokens": 100, "customInstructions": "x" * 2001},
             "customInstructions 超 2000 字符"),
            ("t37c7", ["not", "an", "object"], "顶层非对象")):
        e = Env(tag, extra_env={"DISPATCH_PROFILE": "cc"})
        _mk_executor(e.root)
        _mk_manifest(e.root, "cc", caps=["executor"],
                     extra={"contextCompaction": bad})
        ext = _mk_cc_ext(e.root)
        _mk_pi_env_shim(e)
        p = e.start_wrap()
        try:
            rc = p.wait(timeout=20)
            argv = e.read_argv()
            err = p.stderr.read().decode("utf-8", "replace")
            envdoc = _read_cc_env(e)
            ok("T37c %s 非法策略 ⇒ WARN（%s）+ env/`-e` 都不注 + 会话照起"
               % (tag, needle),
               rc == 0 and "contextCompaction 非法" in err and needle in err
               and "不装配" in err
               and (envdoc or {}).get("AGENTD_CONTEXT_COMPACTION") is None
               and ext not in _argv_flag_pairs(argv, "-e"),
               "rc=%s env=%r err=%r" % (rc, envdoc, err[-400:]))
            ok("T37c %s 非法策略不影响其余装配面（基线能力 + --model 照常）"
               % tag,
               _argv_flag_pairs(argv, "--append-system-prompt") == [EXEC_TEXT],
               repr(argv)[:300])
        finally:
            e.cleanup([p])

    # ④ 策略合法但执行体缺失
    e = Env("t37d", extra_env={"DISPATCH_PROFILE": "cc"})
    _mk_executor(e.root)
    _mk_manifest(e.root, "cc", caps=["executor"],
                 extra={"contextCompaction": {"enabled": False}})
    ext = _mk_cc_ext(e.root, present=False)
    _mk_pi_env_shim(e)
    p = e.start_wrap()
    try:
        rc = p.wait(timeout=20)
        argv = e.read_argv()
        err = p.stderr.read().decode("utf-8", "replace")
        envdoc = _read_cc_env(e)
        ok("T37d 执行体缺失 ⇒ WARN + env/`-e` 同进同退都不注 + 会话照起",
           rc == 0 and "contextCompaction 执行体缺失" in err and ext in err
           and (envdoc or {}).get("AGENTD_CONTEXT_COMPACTION") is None
           and ext not in _argv_flag_pairs(argv, "-e"),
           "rc=%s env=%r err=%r" % (rc, envdoc, err[-400:]))
    finally:
        e.cleanup([p])

    # ⑤ resident 形态同等装配
    e = Env("t37e", extra_env={"DISPATCH_PROFILE": "cc", "AGENTD_RESIDENT": "1",
                               "AGENTD_SESSION_NAME": "bot/t37e"},
            with_prompt=False, fake_mode="hang_settle")
    _mk_manifest(e.root, "cc", caps=["cc"],
                 extra={"contextCompaction": {"triggerTokens": 1}})
    _mk_cap(e.root, "cc", prompt_text="# cc cap\n", cap_yml={"summary": "s"})
    ext = _mk_cc_ext(e.root)
    _mk_pi_env_shim(e)
    p = e.start_wrap()
    try:
        ok("T37e resident 等到 argv 快照", e.wait_argv(), e.sock)
        argv = e.read_argv()
        envdoc = _read_cc_env(e)
        raw = (envdoc or {}).get("AGENTD_CONTEXT_COMPACTION")
        ok("T37e resident 形态同等装配（env + `-e` 都在场，resident 不前置基线能力）",
           raw is not None and json.loads(raw) == {"enabled": True, "triggerTokens": 1}
           and _argv_flag_pairs(argv, "-e").count(ext) == 1
           and _argv_flag_pairs(argv, "--append-system-prompt") == ["# cc cap\n"],
           "env=%r argv=%r" % (raw, argv)[:400])
    finally:
        e.cleanup([p])


def t29_child_exts():
    """T29 子端扩展注入面：CHILD_EXTS 三文件在场 → 任务形态 argv 按序
    注入三个 -e（含 receiver-child = 子任务自家信箱的推送收件面）；文件缺失 → 跳过不拖垮
    会话（既有容错口径）；resident 形态一律不注入（主端 index.ts 由 workdir 的 .pi 自动发现，
    其 receiver 已覆盖自家信箱，再注入只会白占一份 watch/poll）。"""
    rel_dir = os.path.join("assistant", ".pi", "extensions", "agentd")
    names = ("ask-user-child.ts", "message-child.ts", "receiver-child.ts")

    def _mk_exts(e):
        d = os.path.join(e.root, rel_dir)
        os.makedirs(d, exist_ok=True)
        for n in names:
            with open(os.path.join(d, n), "w") as f:
                f.write("export default function () {}\n")
        return [os.path.join(e.root, rel_dir, n) for n in names]

    # ① 三文件在场 → 任务形态按 CHILD_EXTS 顺序注入（探针扩展不在场 → 不多不少三个）
    e = Env("t29a")
    want = _mk_exts(e)
    p = e.start_wrap()
    try:
        ok("T29a sock 就位", e.wait_sock())
        e.wait_argv()
        argv = e.read_argv()
        got = [argv[i + 1] for i, x in enumerate(argv or []) if x == "-e"]
        ok("T29a 任务形态注入子端扩展×3（含 receiver-child，按 CHILD_EXTS 顺序）",
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


def _mk_child_exts(e, names=("ask-user-child.ts", "message-child.ts",
                             "receiver-child.ts")):
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
        p = os.path.join(e.flags, "gate_env")
        if not os.path.exists(p):
            return None
        with open(p) as f:
            return json.loads(f.read())

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
    _mk_child_exts(e6, names=("ask-user-child.ts", "message-child.ts"))  # 故意缺 receiver
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
    """T43 心跳 prompt.md 的两个锚定行与 core.ts::buildPromptMd **同源**（防漂断言）。

    心跳的 prompt.md 由 `assistant/heartbeat.sh` 自写、不经 `buildPromptMd`，而 `caps/executor`
    的「本次任务参数」锚定句（下文所有「任务目录」「本任务 taskId」均指该行）与「分级门禁」标记
    都要求这两行在 prompt **开头** ⇒ 缺行会让基线那两处引用对心跳形态悬空。
    镜像允许（bash 侧无薄渲染入口：agentctl 无 prompt 渲染动词、python 重实现 = 第三份副本、
    node 跑 TS = 给脚本加运行时依赖），但**没有断言的镜像**才是错 —— 漂移会在「executor 基线改了
    锚定句措辞」那天静默发生。本用例钉三面：① token 集同源（任一侧改名即红）；② 位置在开头
    （两行先于 REQUIREMENT 的 printf）；③ 路径按 portablePath 同款口径渲染（$HOME 内 → 波浪号）。
    """
    hb_path = os.path.join(HERE, "..", "assistant", "heartbeat.sh")
    core_path = os.path.join(HERE, "..", "assistant", ".pi", "extensions",
                             "agentd", "core.ts")
    with open(hb_path, encoding="utf-8") as f:
        hb = f.read()
    with open(core_path, encoding="utf-8") as f:
        core = f.read()
    tokens = ["任务分级", "【本次任务参数】", "taskId", "任务目录", "report.md"]

    # core.ts 侧：buildPromptMd 的函数体区间（两行模板都在其中）
    i = core.index("export function buildPromptMd")
    seg = core[i:i + 6000]
    for t in tokens:
        ok("T43 core.ts::buildPromptMd 含 token %s" % t, t in seg,
           "改名/删除 ⇒ 同批改 heartbeat.sh 与本清单（否则心跳形态的锚定行会静默漂）")

    # heartbeat.sh 侧：prompt 落盘块（从 mkdir session/ 到重定向收尾）
    j = hb.index('mkdir -p "$AGENT_DIR/session"')
    k = hb.index('> "$AGENT_DIR/prompt.md"', j)
    block = hb[j:k]
    for t in tokens:
        ok("T43 heartbeat.sh 的 prompt 块含 token %s（与 buildPromptMd 同源）" % t,
           t in block, "块内缺该 token ⇒ 心跳任务的 prompt.md 少一个锚定字段")

    req = block.index('"$REQUIREMENT"')
    ok("T43 分级行在 REQUIREMENT 之前（= prompt 开头）",
       block.index("任务分级：S") < req, "caps/executor 的分级门禁按「任务头部」标记对号")
    ok("T43 参数行在 REQUIREMENT 之前（= prompt 开头）",
       block.index("【本次任务参数】") < req,
       "caps/executor 逐字依赖「任务 prompt **开头**的『本次任务参数』行」")
    ok("T43 路径按 portablePath 同款口径（$HOME 内 → 波浪号形态）",
       'AGENT_DIR_DISP="~${AGENT_DIR#"$HOME"}"' in block,
       "跨机可移植面：心跳任务也可能 host≠登记机，绝对路径会给出不存在的登记机 home 前缀")
    ok("T43 参数行点名执行机 env $AGENT_HOME", "`$AGENT_HOME`" in block,
       "缺括注 ⇒ 执行者拿到波浪号路径却不知道权威现值在哪")


def t44_provider_injection():
    """T44 `--provider` 注入（`d-isqn` = B）：声明源只有既有的 profile `model` 字段，
    provider 段由其派生（**不新增 profile 字段、不硬编码 provider 名**）。四态：
    ① `<provider>/<id>` 形式 ⇒ argv 含 `--provider <provider 段>` 与 `--model <原值>`，且 provider 在 model 之前；
    ② profile 无 model ∨ 解析不到 ⇒ **不含** `--provider`（落回 settings.json 默认）；
    ③ model 不含 `/`（裸模型 id）⇒ **不含** `--provider`，`--model` 照旧注入；
    ④ 畸形值（`/planner`、`a/b/c`）⇒ **不 die**（rc=0）且 **不含** `--provider`（fail-soft 是硬要求：
    本路径影响所有任务 spawn）。夹具全在临时树（`Env.root`），**不动生产 `bots/profiles/`**。"""
    def _argv_task(tag, model):
        """任务形态跑一轮，返回 (rc, argv, Env, proc)。调用方负责 cleanup。"""
        e = Env(tag)
        _mk_executor(e.root)
        _mk_manifest(e.root, "executor", caps=["executor"], model=model)
        p = e.start_wrap()
        rc = p.wait(timeout=20)
        return rc, e.read_argv(), e, p

    # ① 现网形态：model = llm-router/<角色>
    rc, argv, e, p = _argv_task("t44a", "llm-router/planner")
    try:
        ok("T44a 任务形态收敛退出 0", rc == 0, "rc=%s" % rc)
        ok("T44a argv 含 --provider llm-router（恰一个，由 model 的 provider 段派生）",
           _argv_flag_pairs(argv, "--provider") == ["llm-router"], repr(argv))
        ok("T44a argv 含 --model llm-router/planner（既有注入不变，恰一个）",
           _argv_flag_pairs(argv, "--model") == ["llm-router/planner"], repr(argv))
        ok("T44a 注入序 = --provider 在 --model 之前（两形态一致的固定序）",
           argv is not None and argv.index("--provider") < argv.index("--model"),
           repr(argv))
        ok("T44a 基线 -xt ask_user 仍在场（provider 注入不改变工具面）",
           _argv_flag_pairs(argv, "-xt") == ["ask_user"], repr(argv))
    finally:
        e.cleanup([p])
    # ② profile 无 model ⇒ 不拼 --provider（也不拼 --model）
    rc, argv, e, p = _argv_task("t44b", None)
    try:
        ok("T44b profile 无 model ⇒ 退出 0 且 argv 不含 --provider",
           rc == 0 and "--provider" not in (argv or []), "rc=%s argv=%r" % (rc, argv))
        ok("T44b 同时不含 --model（model 只住 profile，两者同源缺席）",
           "--model" not in (argv or []), repr(argv))
    finally:
        e.cleanup([p])
    # ②' profile 解析不到（resident + 清单缺失）⇒ 不拼 --provider
    e = Env("t44b2", fake_mode="hang_settle",
            extra_env={"AGENTD_RESIDENT": "1",
                       "AGENTD_SESSION_NAME": "bot/zz-ghost-t44",
                       "DISPATCH_PROFILE": "ghost"})
    p = e.start_wrap()
    try:
        ok("T44b2 sock 就位", e.wait_sock())
        e.wait_argv()
        argv = e.read_argv()
        ok("T44b2 resident + profile 清单缺失 ⇒ argv 不含 --provider（解析不到 = 不注入）",
           argv is not None and "--provider" not in argv, repr(argv))
    finally:
        e.cleanup([p])
    # ③ model 不含 `/`（裸模型 id）⇒ --model 照旧、无 --provider
    rc, argv, e, p = _argv_task("t44c", "qwen3.8-max")
    try:
        ok("T44c model 无斜杠 ⇒ --model 原样注入",
           rc == 0 and _argv_flag_pairs(argv, "--model") == ["qwen3.8-max"],
           "rc=%s argv=%r" % (rc, argv))
        ok("T44c model 无斜杠 ⇒ argv 不含 --provider（provider 无从派生）",
           "--provider" not in (argv or []), repr(argv))
    finally:
        e.cleanup([p])
    # ④ 畸形值 fail-soft（两子态：provider 段空 / 多段）
    for tag, bad in (("t44d1", "/planner"), ("t44d2", "a/b/c")):
        rc, argv, e, p = _argv_task(tag, bad)
        try:
            ok("T44d(%s) 畸形 model ⇒ 不 die（rc=0、无诊断）" % bad,
               rc == 0 and e.read_diag() is None, "rc=%s diag=%r" % (rc, e.read_diag()))
            ok("T44d(%s) 畸形 model ⇒ argv 不含 --provider（不猜切分点）" % bad,
               "--provider" not in (argv or []), repr(argv))
            ok("T44d(%s) 畸形 model ⇒ --model 仍原样注入（装配器不二次判断 model 值）" % bad,
               _argv_flag_pairs(argv, "--model") == [bad], repr(argv))
        finally:
            e.cleanup([p])


def t45_heartbeat_env_scrub():
    """T45 `DISPATCH_HEARTBEAT` 的洗刷面：心跳标记与 AGENTD_RESIDENT 同族
    （只应来自 spec.command 前缀，登记方 = assistant/heartbeat.sh），未进洗刷名单时它被心跳
    会话内每个孙进程继承 → 经 bash 工具 → `make` → `serviced/serviced.py` 带进被启动的服务；若那是
    agentd，它 spawn 的每个任务都带上递归守卫豁免（守卫全网静默失效）。
      a) 名单单点含该枚 + scrub_env 真洗掉（其余键逐字保留，不过杀）；
      d) 心跳豁免不回归：按 spec.command 前缀形态（bash -c 'DISPATCH_HEARTBEAT=1 exec …'）
         起进程 ⇒ pi 子进程 environ 里该标记仍在场（洗刷只断继承路径、不断显式声明路径）。"""
    import envscrub
    key = "DISPATCH_HEARTBEAT"
    # ---- a) 名单单点 + 洗刷行为 ----
    ok("T45a ENV_SCRUB_EXACT 含 DISPATCH_HEARTBEAT（名单单一事实源）",
       key in envscrub.ENV_SCRUB_EXACT, sorted(envscrub.ENV_SCRUB_EXACT))
    polluted = {key: "1", "AGENTD_RESIDENT": "1", "DISPATCH_PROFILE": "executor",
                "AGENT_SELF": "task/outer", "PATH": "/usr/bin", "HOME": "/h",
                "SOME_UNRELATED": "keep-me"}
    scrubbed = envscrub.scrub_env(base=polluted)
    ok("T45a scrub_env 洗掉该枚（继承链断开）", key not in scrubbed, sorted(scrubbed))
    ok("T45a 非身份键逐字保留（不过杀）",
       scrubbed.get("PATH") == "/usr/bin" and scrubbed.get("SOME_UNRELATED") == "keep-me",
       sorted(scrubbed))
    ok("T45a runner spawn 口径（strip_third_party=True）同样洗掉该枚",
       key not in envscrub.scrub_env(base=polluted, strip_third_party=True))

    # ---- d) 心跳豁免不回归：spec.command 前缀形态起进程 ⇒ pi 子进程 environ 仍带该标记 ----
    e = Env("t45hb")
    e.env.pop(key, None)          # 父 env 不带该枚（洗刷后形态）：只能由命令前缀注入
    ok("T45d 夹具父 env 不含该枚（标记只能来自命令前缀，不靠继承）",
       key not in e.env, sorted(k for k in e.env if k.startswith("DISPATCH")))
    # 与 runner.spawn 同构：subprocess.Popen(["bash", "-c", spec.command], env=…)，
    # spec.command = assistant/heartbeat.sh 登记的 `DISPATCH_HEARTBEAT=1 exec python3 <wrap>` 形态。
    os.chmod(FAKEPI, 0o755)
    spec_command = "%s=1 exec %s %s" % (key, sys.executable, WRAP)
    p = subprocess.Popen(["bash", "-c", spec_command], env=e.env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         start_new_session=True)
    try:
        ok("T45d sock 就位", e.wait_sock())
        ok("T45d pi 子进程已启动（argv 快照在场）", e.wait_argv())
        ge = None
        gp = os.path.join(e.flags, "gate_env")
        dl = time.time() + 15
        while time.time() < dl and ge is None:
            if os.path.exists(gp):
                try:
                    with open(gp) as f:
                        ge = json.loads(f.read())
                except ValueError:
                    ge = None
            time.sleep(0.05)
        ok("T45d pi 子进程 environ 仍含 DISPATCH_HEARTBEAT=1（豁免路径不回归）",
           ge is not None and ge.get("dispatch_heartbeat") == "1", repr(ge))
    finally:
        e.cleanup([p])


def _stderr_after_kill(p, timeout=10):
    """resident 形态（不收敛）读 wrap 自身 stderr 的唯一安全形态：先杀完进程组再 read，
    否则 read() 无限阻塞 = 假活（同 T36d 的既有处置）。返回文本（不可读时返回原因串）。"""
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
    finally:
        e.cleanup([p])
    err = _stderr_after_kill(p)
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
# DISPATCH_HEARTBEAT 事故（若被启动的是 agentd，递归守卫全网静默失效）。本组 = **提交期钉桩**
# （零运行时守卫：名单是枚名制、同前缀族里住着配置旋钮 ⇒ 不能按前缀洗，见 envscrub.py）。
_CMD_PREFIX_SOURCES = (
    # (相对工作区根的 glob, 说明)。不在场 = 该源不在本快照内（w/ 整树 gitignored、pi-wrap
    # 单独 checkout）⇒ 显式记一条跳过，不静默、不当失败。
    ("bots/daemon/*/spec.json", "守护型/常驻 bot 的被追踪声明源"),
    ("assistant/heartbeat.sh", "心跳任务的登记脚本（spec.command heredoc）"),
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
    known = {"DISPATCH_PROFILE", "AGENTD_RESIDENT", "AGENTD_SESSION_NAME",
             "DISPATCH_HEARTBEAT"}
    ok("T47a 扫到已知四枚前缀键（正则未失配、非假绿）", known <= set(found),
       sorted(found))
    for k in sorted(found):
        scrubbed = envscrub.scrub_env(base={k: "1", "PATH": "/usr/bin",
                                            "SOME_UNRELATED": "keep-me"})
        ok("T47b 前缀键 %s 被洗刷名单覆盖（源 %s）"
           % (k, ",".join(sorted(found[k]))),
           k not in scrubbed and scrubbed.get("SOME_UNRELATED") == "keep-me",
           sorted(scrubbed))


def main():
    global PASS, FAIL
    os.chmod(FAKEPI, 0o755)
    for fn in (t1_normal, t2_relay_replace, t3_crash, t4_reject,
               t5_idempotent, t6_window_cancel, t6b_steer_injection,
               t7_stale_takeover,
               t8_resident_no_converge, t9_resident_exit_passthrough,
               t10_resident_no_prompt, t11_probe_ext, t12_sock_bind_failed,
               t13_profile, t14_profile_missing, t15_profile_unset,
               t16_tools_whitelist, t17_tools_blacklist, t18_tools_edge,
               t19_profile_extensions, t20_profile_extensions_empty,
               t21_xt_merge_edges, t22_tools_field_edges,
               t23_multi_cap_order, t24_cap_partial_missing,
               t25_model_and_toolface_merge, t26_single_value_defense,
               t27_resident_multi_cap, t28_knowledge,
               t35_knowledge_tiers, t29_child_exts,
               t30_ready_handshake, t31_ready_env_scrub,
               t32_cap_degradation, t33_injection_order_equivalence,
               t34_profile_banned_fields, t36_task_model_fallback,
               t37_cc_assembly,
               t_fake_settled_inflight,
               t38_model_error_no_report, t39_model_error_with_report,
               t39b_model_error_zero_byte_report, t40_normal_stop_no_report,
               t41_tail_unreadable_failsoft, t41b_tail_window_truncated,
               t42_resident_model_error_untouched,
               t43_heartbeat_prompt_anchor_lines,
               t44_provider_injection, t45_heartbeat_env_scrub,
               t46_resident_prompt_delivery,
               t47_spec_command_env_scrub):
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
