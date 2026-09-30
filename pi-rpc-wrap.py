#!/usr/bin/env python3
"""pi-rpc-wrap.py — 任务会话封装脚本（plan v3 B 路线）。

定位 = 「带脑子的 socat」：把任务会话以 `pi --mode rpc` 形态拉起，持有其
stdio，并向 ~/m/run/agentd/<taskId>.sock 透传 pi rpc 原生字节流——观测/交互
的全部智能（get_entries 基线/事件环/注入/自愈）由消费方（web sessiond
SocketSupervisor）经透传链路驱动，本脚本不实现任何观测语义。

对 runner 而言本脚本 = 普通命令 + 退出码（核心不变量）：
  - 完成收敛后以 pi 的退出码退出（正常路径 = stdin EOF 优雅退出 → 0）；
  - 进程级失败（启动即炸/投递被拒/异常退出/收敛超时）→ 写 $AGENT_HOME/diagnosis.md
    后非 0 退出。result.md 已退役（inform 2026-09-02-19-32-57-dispatcher-x8hp）：
    正常完成证据 = session.jsonl 过程 + report.md 结论。

职责：
  1. 拉起 `pi --mode rpc`（--session/-n/-e 子端扩展〔枚数 = CHILD_EXTS 现场计数，现 2 枚：
     ask-user-child + receiver-child〕 + sessiond 探针扩展；**人格面零 flag**：只 `-e` 注入层
     扩展 + 透传它的输入 env，见 `_persona_ext_argv`；
     探针注入使观测面 /inspect 在任务/常驻会话可用；
     argv 依 runner 注入的 AGENT_HOME/AGENT_ROOT/AGENT_SELF 组装，跨机可移植）；
  2. prompt.md 幂等投递：会话 jsonl 已有 user 消息（复活/重放场景）则跳过；
  3. stdio↔unix socket 透传：同时刻一条活跃连接，新连接替换旧连接（支持 web
     重启重连）；新连接先冲刷小环（近 RING_MAX 行）再续直播；无论有无人观测
     持续吸走 pi stdout（发送失败断连接，绝不阻塞管道）；
  4. 完成收敛：观察 agent_settled（pi finally 保证）∧ 无在飞轮（agent_start/
     agent_end 配对计数 inflight）∧ 最近 queue_update 队列空 → SETTLE_WINDOW 竞态窗
     （窗内出现新轮次/入队即取消本轮）→ 关 pi stdin → pi 优雅退出 → **末轮模型错误闸**
     （见 model_error_gate）→ 以 pi 退出码退出；
  5. 失败诊断：$AGENT_HOME/diagnosis.md（阶段 + exitcode + stderr 尾）。

末轮模型错误闸（exit 0 假成功收口；实证）：上游模型请求失败时 pi 会落一条
role=assistant ∧ stopReason="error" ∧ usage 全零（往往只有 thinking 块）的条目后结束该轮，此后无排队
⇒ 收敛判据全满足 ⇒ 旧行为 exit 0 + notified.json complete:true，而 report.md 零落盘（退出码与
complete 都不反映「交付物没写」，旧兜底只有通知文案层的 warn=no_report）。修法 = pi 退出后有界尾读
会话 jsonl（SESSION_TAIL_BYTES，绝不整文件进内存）取末条 assistant 的 stopReason，两档：
(i) "error" ∧ report.md 非空不在场 → diagnosis stage=model_error_stopreason + 退出码 1；
(ii) "error" ∧ report.md 非空在场 → 透传 pi 退出码 + WARN + 信息性 diagnosis
stage=model_error_stopreason_delivered（不参与完成判定；task-layout.md 的 diagnosis.md 行已记该唯一例外）。
拿不到证据（jsonl 缺失/不可解析/无 assistant 条目/stopReason 缺失或非 "error"）⇒ 行为与旧版逐字一致；
resident 形态不经此闸（无 report.md 交付语义）。

socket 生命周期：端点单点 = proto.task_sock_path（<root>/run/agentd/<id>.sock）；
chmod 0600；.pid 伴生档记本进程 {pid, procStart, startedAt}——启动时发现陈旧
节点按 (pid,procStart) 身份校验：存活 → 拒启（防双宿主）；死 → unlink 接管；
退出清理（atexit；SIGKILL 留陈旧由下次启动接管兜底）。

就绪握手（P0 spawn 竞态）：子端收件扩展（receiver-child.ts）在 pi 的
`session_start` 就会 drain 自家 `agents/task/<id>/inbox`，而 `sendUserMessage` 在会话空闲时
**直接起轮**（pi `agent-session.js::sendUserMessage` JSDoc：Always triggers a turn）——注入因此
抢在初始 prompt 之前，两种现网形态（均有现网实证）：① pi 正在 streaming → 初始 prompt
被拒（`Agent is already processing` → stage=prompt_rejected、exit 1 秒死）；② 注入轮先跑完（且因
pi session-manager 的 no-assistant guard 未落盘）→ `agent_settled` 被当任务收敛 → **exit 0 假成功**
（零执行、`session/session.jsonl` 不存在、却报 task_done）。修法 = 两个标记文件的双向握手
（单点 `proto.task_ready_path`，与 sock 同目录的宿主本地运行时件）：本脚本在初始投递收口后写
`init-ok`（子端据此开「就绪门」才开始认领），子端首次补扫完成后回写 `recv-armed`（本脚本**有界**
等它之后才进收敛监督，防首轮瞬时结束时排队中的注入被收敛吞掉）。两侧都有超时退路（宁可晚投
不可不投）；resident 形态不参与（不注入子端扩展，其主端 receiver 行为逐字不变）。
排障判据沉淀（订正🟡3）：**exit 0 不足以证明任务执行过**，且
「`session/session.jsonl` 在场（含 user+assistant 事件）」**也不足以**证明——真 pi 反例：抢跑形态下
注入轮先起，session.jsonl 在场且含 user+assistant，但**初始 prompt 从未进会话树**（首条 user 是注入的
信封而非 prompt）、干预信封部分丢失、仍 exit 0 + `task_done`。硬判据 = **会话树含初始 prompt 的
 user 事件**（或 `report.md` 在场）；e2e S53 已按此断言（`"e2e S53 live prompt" in txt`）。

resident 模式（env AGENTD_RESIDENT=1，设计 §2.2）：常驻会话
（bot 族进程型参与方）与任务形态的差异全部关在本脚本内——不触发完成收敛（不关 pi
stdin，主线程只等 pi 退出）、argv 会话名钉死 $AGENTD_SESSION_NAME（不注入子端扩展；
工具面的排除归 profile/能力声明，本脚本不预置任何形态基线）、prompt.md 可选（不存在则裸启动）、
pi 任何退出 = 代终止事实透传退出码，
不写诊断不误报（崩溃自愈/换代归属 = runner restartPolicy=auto / control 三动作）。
其余（socket 生命周期/.pid 伴生档/透传/小环）逐字复用。

人格装配**不在本文件**（两层，均住别处）：解析层 `persona.py`（同目录；profile/caps →
结构化注入面，合并语义与全部 fail-soft 降级分支的单一实现）+ 注入层 pi 扩展
`pi-core/agent/extensions/profile-loader.ts`（= 常量 PROFILE_LOADER_EXT_REL；会话内把那份结构
映射到 pi 的 API：系统提示追加 /
skill 路径 / 活动工具集 / 模型 / 压缩策略）。本文件对人格面只做两件事（`_persona_ext_argv`）：
① 把注入层扩展 `-e` 进去（同时钉住它在 pi 扩展装载序里的位置 = 先于自动发现的全局扩展，
故人格正文落在其它扩展的追加之前）；② 透传它需要的那一枚人格输入 env（`DISPATCH_PROFILE` =
profile 名，来自 spec.command，逐字不改；会话形态**不住 env**，它是 profile 清单的 `form`
字段，由解析层读出）。机制口径权威 = dispatch/DISPATCH.md §3，
装配面逐格全文 = bots/docs/profile-assembly.md，资产形态与字段规范 = bots/README.md
「人格资产」/「知识库规范」节，解析层的输入输出契约 = persona.py 模块头。
**排障面的位置变更**：逐能力注入日志（`人格装配（会话内注入）：…`）与解析层告警现在写在 **pi 的
stderr** ⇒ 落 `run/agentd/<name>.stderr.log`（与诊断的 stderr 尾同源），不再在 wrap 自己的日志里；
会话内取证 = `/persona`。压缩策略 env 的写者也是注入层 ⇒ `spawn_pi` 恒洗掉从宿主继承的同名 env
（「无策略 = env 不在场」是硬语义，不靠调用方环境干净）。协议层扩展（ask-user-child/
receiver-child/探针）仍归调度层注入，与人格装配无关。

失败域：观测面（accept/转发）异常只断观测不伤收敛主线；未捕获异常兜底写
诊断后退出 1。本文件只用 python3 标准库（解析层的 pyyaml 依赖与其降级分支见 persona.py）。
"""
import atexit
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import deque

# proto.py lives in the sibling agentd repo: it is the protocol's single source,
# shared with the TypeScript side (the agentd extension's core.ts) and
# with every agentd module. AGENTD_DIR overrides the location for a layout where
# the two repos are not siblings.
_HERE = os.path.dirname(os.path.abspath(__file__))
AGENTD_DIR = os.environ.get("AGENTD_DIR") or os.path.join(
    os.path.dirname(_HERE), "agentd")
for _p in (_HERE, AGENTD_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import proto  # noqa: E402

RING_MAX = 200            # 小环容量（行）：新连接短窗回放，完整基线走 get_entries
STDERR_TAIL_MAX = 8192    # stderr 内存尾上限（字节）
DIAG_STDERR_LIMIT = 2048  # diagnosis.md 内 stderr 尾展示上限（字节）
SETTLE_WINDOW = 0.5       # 收敛竞态窗（秒）：吸收临末注入
INIT_PROMPT_TIMEOUT = 60  # 初始投递回执超时（秒）
EXIT_GRACE = 15           # 关 stdin 后等 pi 退出的宽限（秒），超时 SIGTERM
CHILD_ARM_TIMEOUT = 3     # 进收敛监督前等子端收件扩展回写 arm 标记的上界（秒）
# ---- 末轮模型错误档----
SESSION_TAIL_BYTES = 256 * 1024   # 会话 jsonl 尾读上界（字节）：取值理由见 last_assistant_message
MODEL_ERROR_STOPREASON = "error"  # pi 的 StopReason 值域中唯一的「模型请求失败」档
MODEL_ERROR_STAGE = "model_error_stopreason"                    # 档 (i)：真失败（退出码 1）
MODEL_ERROR_STAGE_DELIVERED = "model_error_stopreason_delivered"  # 档 (ii)：信息性（退出码 0）


def _env_float(name, default, cap=None):
    """环境钩（测试/应急用，仅可向下调）：读 float，非法/超限回落缺省。"""
    try:
        v = float(os.environ.get(name) or default)
    except ValueError:
        return default
    if cap is not None:
        v = min(v, cap)
    return v if v > 0 else default
# 扩展根（相对 $AGENT_ROOT）：agentd 扩展住调用方的**全局装载面**，本仓不逐处钉它的目录
# ⇒ 单点在此，可由 AGENTD_EXT_REL 注入（部署面变化时零改码）；与 TS 侧 core.ts 的
# ASK_USER_CHILD_REL / RECEIVER_CHILD_REL 同源（跳语言 pin = 扩展自测的「注入面同源」组）。
EXT_DIR_REL = os.environ.get("AGENTD_EXT_REL") or "pi-core/agent/extensions/agentd"
CHILD_EXTS = (            # 子端扩展（相对 $AGENT_ROOT，由 EXT_DIR_REL 单点拼）
    EXT_DIR_REL + "/ask-user-child.ts",
    # 子任务自家信箱的推送收件面：只收 agents/task/<本任务 id>/inbox（收件面
    # 锁死单点 = core.taskSelfMailbox），按信封 deliver 选 steer/followUp 注入。只进任务形态
    # 分支：resident（bot 型常驻会话）的主端 index.ts 由**全局装载面**发现（cwd 无关），其
    # receiver 已覆盖自家信箱，再注入本扩展只会白占一份 watch/poll。
    EXT_DIR_REL + "/receiver-child.ts",
)
PROBE_EXT_REL = "w/ext/sessiond/probe.ts"   # sessiond 探针：任务/常驻会话
                                            # 也注入，使 spec.json?v=chat 观测面 /inspect 可用
# 人格装配不住在本文件：解析层 = 同目录 `persona.py`（合并语义与降级分支的单一实现），
# 注入层 = pi 侧扩展 PROFILE_LOADER_EXT_REL（本文件只负责把它 `-e` 进去）。
# 本文件对人格面只做两件事：① 注入那一个扩展；② 透传人格输入 env（DISPATCH_PROFILE，
# 来自 spec.command，逐字不改；形态轴住 profile 的 `form` 字段 ⇒ 不经 env）。
# 常量从解析层取用（不在此复制，复制即漂移）。
import persona                                                # noqa: E402
CONTEXT_COMPACTION_ENV = persona.CONTEXT_COMPACTION_ENV       # 归一化策略的注入通道（紧凑 JSON）；
                                                              # 写者 = profile-loader 扩展（会话内），
                                                              # 消费方 = bots/extensions/context-compaction/index.ts
PROFILE_LOADER_EXT_REL = "pi-core/agent/extensions/profile-loader.ts"   # 人格注入层（= ~/.pi/agent/
                                            # extensions/ 的自动发现面，被追踪 ⇒ 四机同步）；
                                            # 文件缺失 → WARN + 不注入（会话裸起、人格面缺席，
                                            # 口径同 PROBE_EXT_REL；绝不 die——硬失败会自锁）


def log(fmt, *args):
    sys.stderr.write(("[pi-rpc-wrap] " + fmt + "\n") % args)
    sys.stderr.flush()


class Wrap:
    def __init__(self):
        self.home = os.environ.get("AGENT_HOME", "")
        self.root = os.environ.get("AGENT_ROOT", "")
        self.task_id = os.environ.get("AGENT_SELF", "")
        # resident 模式（设计 §2.2）：标记 = spec.command 内嵌 env 前缀，
        # 经 bash -c 天然注入，不依赖 runner 继承（惯例 = agentd/agent-file-protocol.md）。
        self.resident = os.environ.get("AGENTD_RESIDENT") == "1"
        self.session_name = os.environ.get("AGENTD_SESSION_NAME", "")
        if not self.home or not self.root or not self.task_id \
                or not os.path.isdir(self.home):
            log("FATAL: 缺少 AGENT_HOME/AGENT_ROOT/AGENT_SELF 或目录不存在"
                "（本脚本只能由 agentd runner 拉起）")
            sys.exit(2)
        # AGENT_SELF = 路径式 id（task/<id>）：文件名/端点/显示名用二段 name；
        # 历史档案字段（诊断等）仍记完整路径式 id。
        self.name = self.task_id.split("/", 1)[1] if "/" in self.task_id \
            else self.task_id
        self.session_file = os.path.join(self.home, "session", "session.jsonl")
        self.prompt_file = os.path.join(self.home, "prompt.md")
        self.sock_path = proto.task_sock_path(self.root, self.name)
        self.pid_file = self.sock_path + ".pid"
        # 就绪握手标记（单点 = proto.task_ready_path）：
        #   init-ok    本脚本写（初始 prompt 已被接受/幂等跳过/裸启动）→ 子端收件扩展
        #              据此开「就绪门」才开始 drain 自家 inbox；
        #   recv-armed 子端收件扩展写（门开 + 首次补扫完成）→ 本脚本有界等待它之后才进
        #              收敛监督（防首轮瞬时结束时排队中的注入被收敛吞掉）。
        # 缺这道握手 = 子端在 session_start 抢跑注入，两种现网形态（均有现网实证，
        #）：① pi 正在 streaming → 初始 prompt 被拒（stage=prompt_rejected、exit 1 秒死）；
        # ② 注入轮先跑完 → agent_settled 被当任务收敛（exit 0 假成功、session.jsonl 永不落盘）。
        self.init_ok_file = proto.task_ready_path(self.root, self.name, "init-ok")
        self.recv_armed_file = proto.task_ready_path(self.root, self.name, "recv-armed")
        self.child_recv_injected = False   # build_argv 是否真注入了 receiver-child.ts
        # 人格注入层（profile-loader 扩展）是否真拼了 `-e`：False = 文件缺失（WARN 已在
        # _persona_ext_argv 记），此时会话照起但人格面缺席 ⇒ 排障先看这条。
        self.loader_injected = False
        self.stderr_log = os.path.join(
            os.path.dirname(self.sock_path), self.name + ".stderr.log")
        # 参数钩（仅可向下调；测试/应急用，生产不设）
        self.pi_bin = os.environ.get("AGENTD_WRAP_PI_BIN") or "pi"
        self.init_timeout = _env_float(
            "AGENTD_WRAP_INIT_TIMEOUT", INIT_PROMPT_TIMEOUT, INIT_PROMPT_TIMEOUT)
        self.settle_window = _env_float(
            "AGENTD_WRAP_SETTLE_WINDOW", SETTLE_WINDOW, 2.0)
        self.exit_grace = _env_float(
            "AGENTD_WRAP_EXIT_GRACE", EXIT_GRACE, EXIT_GRACE)
        self.arm_timeout = _env_float(
            "AGENTD_WRAP_ARM_TIMEOUT", CHILD_ARM_TIMEOUT, CHILD_ARM_TIMEOUT)

        self.pi = None                 # pi 子进程句柄
        self.ring = deque(maxlen=RING_MAX)
        self.lock = threading.Lock()   # 守护 conn / stdin 写
        self.send_lock = threading.Lock()  # 对当前连接的全部写（泵转发/环回放）串行化，
                                           # 防两线程 sendall 交错擕裂 JSON 行
        self.conn = None               # 当前活跃观测连接（唯一）
        self.stdin_open = True
        self.stderr_tail = deque()     # bytes 片段，合计 ≤ STDERR_TAIL_MAX
        self.stderr_len = 0
        # 收敛判定状态
        self.settled = False           # 观察到 agent_settled（待决）
        self.inflight = 0              # 在飞轮计数（agent_start/agent_end 配对）
        self.activity = False          # 竞态窗内的新轮次/入队活动
        self.queue_busy = False        # 最近 queue_update：steering/followUp 非空
        self.converging = False        # 收敛已决（关闭/正在关闭 pi stdin）
        self.init_waiter = threading.Event()
        self.init_resp = None
        self.diag_written = False
        self.converge_timeout = False   # 收敛宽限看门狗动过手（诊断阶段判定用）
        self.warned_oversize = False   # 丢弃事件的 WARN 去重（每进程只打一次，见 _read_stdout）
        self.warned_badjson = False    # 同上：非 JSON 行分支
        self.srv = None

    # ---------- socket 生命周期 ----------

    def setup_socket(self):
        d = os.path.dirname(self.sock_path)
        os.makedirs(d, exist_ok=True)
        # 陈旧节点接管：.pid 伴生档身份校验（杀纪律口径：(pid,procStart) 二元组）
        doc = proto.read_json(self.pid_file)
        if isinstance(doc, dict) and doc.get("pid"):
            if proto.pid_identity_ok(doc.get("pid"), doc.get("procStart")):
                log("FATAL: socket %s 已被存活的封装进程持有（pid=%s），拒启防双宿主",
                    self.sock_path, doc.get("pid"))
                sys.exit(1)
            log("陈旧观测端点（持锁者已死），接管: %s", self.sock_path)
            for p in (self.sock_path, self.pid_file):
                try:
                    os.unlink(p)
                except OSError:
                    pass
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.srv.bind(self.sock_path)
        except OSError as e:
            # 端点是 AF_UNIX 路径，受内核 sun_path 上限约束（Linux 108 / macOS
            # 104 字节）：root 过深（如 mac TMPDIR 下的临时树）会超长。
            # 专属诊断阶段点名——不再混入通用 wrap_error 兑底。
            # 判定口径：CPython unix_bind 进内核前自预检长度，超长抛
            # OSError('AF_UNIX path too long') 且 errno=None；内核侧真错才带 errno。
            # 两条信号取并集，「超平台限换更浅 root」提示才真实可达。
            import errno as _errno
            extra = "bind 失败: %r" % e
            if e.errno == _errno.ENAMETOOLONG \
                    or "path too long" in str(e).lower():
                extra += "；端点路径超平台 sun_path 上限（Linux 108 / macOS 104 字节），需换更浅的 root"
            log("FATAL: 观测端点 bind 失败: %s (%r)", self.sock_path, e)
            self.srv.close()
            self.diagnose("sock_bind_failed", 1, extra=extra)
            sys.exit(1)
        os.chmod(self.sock_path, 0o600)
        self.srv.listen(4)
        proto.atomic_write_json(self.pid_file, {
            "pid": os.getpid(), "procStart": proto.proc_starttime(os.getpid()),
            "startedAt": proto.now_ts(), "task": self.task_id})
        atexit.register(self.cleanup_socket)
        log("观测端点就绪: %s", self.sock_path)

    def cleanup_socket(self):
        for p in (self.sock_path, self.pid_file,
                  self.init_ok_file, self.recv_armed_file):
            try:
                os.unlink(p)
            except OSError:
                pass

    # ---------- 就绪握手（P0 spawn 竞态） ----------

    def clear_ready_marks(self):
        """spawn 前清上一代残留标记：陈旧 init-ok 会**骗开本代就绪门**——形态①/② 的失败代
        恰好留下「session.jsonl 无 user 消息」的重试代，若沿用旧标记则本代子端照旧抢跑。"""
        for p in (self.init_ok_file, self.recv_armed_file):
            try:
                os.unlink(p)
            except OSError:
                pass

    def write_init_ok(self, why):
        """初始投递收口（成功三路径共用）→ 落 init-ok 标记，开子端就绪门。best-effort：
        写失败只 WARN（子端另有「会话树已有 user 消息」等价证据 + 有界超时后照旧投递）。
        resident 形态不参与握手（不注入子端扩展，主端 index.ts receiver 行为逐字不变）→ 不落标记。"""
        if self.resident:
            return
        try:
            os.makedirs(os.path.dirname(self.init_ok_file), exist_ok=True)
            proto.atomic_write_json(self.init_ok_file, {
                "task": self.task_id, "pid": os.getpid(),
                "procStart": proto.proc_starttime(os.getpid()),
                "ts": proto.now_ts(), "why": why})
            log("就绪标记已落盘（%s）: %s", why, self.init_ok_file)
        except OSError as e:
            log("WARN: 就绪标记落盘失败（子端将回落到会话树证据/超时开门）: %r", e)

    def await_child_arm(self):
        """有界等待子端收件扩展回写 recv-armed（假活防线：绝不无条件等）。
        只在真注入了 receiver-child.ts 时等；超时 → WARN 后照常进收敛监督（扩展未加载/
        身份不可解析/门超时都不该拖住任务收口）。"""
        if not self.child_recv_injected:
            return
        deadline = time.monotonic() + self.arm_timeout
        while time.monotonic() < deadline:
            if os.path.exists(self.recv_armed_file):
                log("子端收件面已就绪（recv-armed 在场）→ 进收敛监督")
                return
            if self.pi is not None and self.pi.poll() is not None:
                return                      # pi 已先退出：无可等
            time.sleep(0.05)
        log("WARN: %.1fs 内未收到子端 recv-armed（扩展未加载/身份不可解析/就绪门超时）"
            "→ 照常进收敛监督", self.arm_timeout)

    # ---------- pi 拉起与初始投递 ----------

    def build_argv(self):
        if self.resident:
            # resident 形态：会话名钉死 $AGENTD_SESSION_NAME（缺省回退）；
            # 不注入子端扩展（主端全套扩展由 workdir 的 .pi 自动发现）；工具面的排除只来自
            # profile 装载的能力声明（cap.yml 的 excludeTools/tools）——解析层无形态基线，
            # 本脚本也不预置任何工具面。
            argv = [self.pi_bin, "--mode", "rpc", "--session", self.session_file,
                    "-n", self.session_name or "[resident %s]" % self.name]
            argv += self._probe_ext_argv()
            argv += self._persona_ext_argv()
            return argv
        argv = [self.pi_bin, "--mode", "rpc", "--session", self.session_file,
                "-n", "[task %s]" % self.name]
        for rel in CHILD_EXTS:
            p = os.path.join(self.root, rel)
            if os.path.exists(p):      # 文件缺失不拖垮会话（对齐 sessiond 口径）
                argv += ["-e", p]
                if os.path.basename(p) == "receiver-child.ts":
                    self.child_recv_injected = True   # 就绪握手对象在场（await_child_arm）
            else:
                log("WARN: 子端扩展缺失，跳过注入: %s", p)
        argv += self._probe_ext_argv()
        argv += self._persona_ext_argv()
        return argv

    # ---------- knowledge 知识清单注入（规范 bots/README.md「知识库规范」） ----------

    def _probe_ext_argv(self):
        """sessiond 探针扩展注入（观测面 /inspect 支持）：文件缺失只告警不拖垮会话，
        口径与 proc.py:_spawn 同款；此时 /inspect 回「探针未加载」而非 403。"""
        p = os.path.join(self.root, PROBE_EXT_REL)
        if os.path.exists(p):
            return ["-e", p]
        log("WARN: 探针扩展缺失，跳过注入（/inspect 不可用）: %s", p)
        return []

    # ---------- 人格注入层的装载（解析层 = persona.py，注入层 = profile-loader 扩展） ----------

    def _persona_ext_argv(self):
        """把人格注入层扩展 `-e` 进 pi（任务/常驻两形态同等）。

        **本文件不装配人格**：profile → caps → 注入面的解析在 `persona.py`（合并语义与全部
        fail-soft 降级分支的单一实现），会话内注入在 `PROFILE_LOADER_EXT_REL` 指的 pi 扩展
        （系统提示追加 / skill 路径 / 活动工具集 / 模型 / 压缩策略）。这里只负责让那个扩展在场，
        以及把它需要的那一个输入透传下去（在 `os.environ` 里，`spawn_pi` 原样带过去）：
          - `DISPATCH_PROFILE` = profile 名（单值；来自 spec.command 的 env 前缀，逐字不改）。
        **会话形态不是本层/注入层的输入**：它住 profile 清单的 `form` 字段（三档
        task|resident|interactive），由解析层读出并回写进它输出的 `form`。`AGENTD_RESIDENT`
        仍在环境里且照旧透传，但只作「是否 agentd 监督会话」的判据（注入层的零行为规则）
        与本脚本的 resident 行为（完成收敛闸 / 会话名 / 不注入子端扩展 / 裸启动）。
        profile 名缺省时注入层按解析层的缺省档处置（回落 `executor` profile），
        判据在解析层，不在此重复。

        `-e` 的注入位还有第二重作用：pi 的扩展装载序是 **CLI `-e` 先于自动发现**
        （dist/core/resource-loader.js 的 `mergePaths(cliEnabledExtensions, enabledExtensions)`）
        ⇒ 注入层的 `before_agent_start` 先跑，人格正文落在其它全局扩展（如 host-info 的身份行）
        的追加之前。本文件同时也在自动发现目录里（`~/.pi/agent/extensions/`，手工/交互会话免
        `-e`），pi 按 realpath 去重 ⇒ 只装载一次（不会撞 flag 名）。

        文件缺失 → WARN + 不注入（会话裸起、人格面缺席；口径同 `PROBE_EXT_REL`）。**绝不 die**：
        本路径在所有会话 spawn 的公共路上，硬失败会自锁（连「修这条路径」的修复会话都起不来）。
        返回 `[]` ∨ `["-e", <绝对路径>]`。"""
        p = os.path.join(self.root, PROFILE_LOADER_EXT_REL)
        if not os.path.isfile(p):
            log("WARN: 人格注入层扩展缺失，跳过注入（会话照起但**人格面缺席**：无系统提示追加、"
                "无能力捆绑 skill、工具面不收窄、model 不切换；取证 = 会话内 /persona）: %s", p)
            return []
        self.loader_injected = True
        # 形态不在本行：它住 profile 的 `form` 字段，由解析层读出后写在注入层自己的
        # 装配摘要行里（`人格装配（会话内注入）：… form=<档>`，同落 pi 的 stderr）。
        log("人格注入层装载：%s（profile=%s）", p,
            os.environ.get("DISPATCH_PROFILE") or "（未设 → 解析层按缺省档回落）")
        return ["-e", p]

    def spawn_pi(self):
        self.clear_ready_marks()       # 陈旧标记不得骗开本代就绪门（先于 pi 启动）
        argv = self.build_argv()
        env = dict(os.environ, SESSIOND_SESSION_FILE=self.session_file)
        # 压缩策略的写者是人格注入层（它在会话内按 profile 的 `contextCompaction` 设这枚 env 并
        # 装载执行体）⇒ 本层恒洗掉可能从宿主继承的陈旧值：「无策略 = env 不在场」是硬语义，
        # 不靠调用方环境干净（否则一个陈旧值会让注入层装载的执行体误启用旧策略）。
        env.pop(CONTEXT_COMPACTION_ENV, None)
        if self.child_recv_injected:
            # 就绪门信号通道（只任务形态：resident 不注入子端扩展，其主端 receiver 行为不变）。
            env["AGENTD_WRAP_INIT_OK"] = self.init_ok_file
            env["AGENTD_WRAP_RECV_ARMED"] = self.recv_armed_file
        self.pi = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,    # 继承本进程进程组：杀纪律组杀覆盖（§5.5）
            env=env)
        threading.Thread(target=self._read_stdout, daemon=True,
                         name="wrap-stdout").start()
        threading.Thread(target=self._read_stderr, daemon=True,
                         name="wrap-stderr").start()
        log("pi spawned pid=%d session=%s", self.pi.pid, self.session_file)

    def session_has_user_message(self):
        """幂等判定：会话 jsonl 已有 user 消息（复活/重放场景）→ 跳过初始投递。"""
        try:
            with open(self.session_file, encoding="utf-8") as f:
                for line in f:
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(e, dict) and e.get("type") == "message":
                        m = e.get("message")
                        if isinstance(m, dict) and m.get("role") == "user":
                            return True
        except OSError:
            pass
        return False

    # ---------- 末轮模型错误档（：exit 0 假成功收口） ----------

    def last_assistant_message(self):
        """有界尾读会话 jsonl：末条 role=assistant 的 (entry, message)；拿不到证据 = (None, None)。

        上界 SESSION_TAIL_BYTES（从文件尾 seek 读，绝不整文件进内存）：取值理由 =
        对「现网最大单行」保留 ≥2× 余量。**复核法**（数字会漂，引用前重测）= 逐个量
        `agents/*/*/session/session.jsonl` 的最大单行，与 SESSION_TAIL_BYTES 相除；2026-09-15
        按**全量 310 个文件**口径的现值 = 114886B ⇒ 262144/114886 = 2.28×。**量时必须点名口径**：
        按文件大小取样的 top-N 会给出偏小的最大值（同一批数据的 top8 = 106097B/2.47×，曾被
        当成全量最大值写进本文）。余量不足的方向是安全的：末条 assistant 落在窗外 ⇒ 取不到
        证据 ⇒ fail-soft 不判失败（不会造假失败）；而会话文件可达数十 MB ⇒ 整文件读不可接受，
        上界必须存在。尾窗 < 文件大小时首行可能被上界截断 ⇒ 丢弃首行。
        向前扫描时末条不一定是 assistant（正常形态下 toolResult 在 assistant 之后）⇒ 跳过非
        assistant 条目继续往前，找到即返回（只解析尾窗内的少数几行）。
        fail-soft（任务书要求：拿不到证据不得把正常任务判失败）：文件不存在/不可读、尾窗内
        无可解析的 assistant 条目（坏行逐行跳过）⇒ (None, None) + 一行 WARN。
        """
        try:
            with open(self.session_file, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                n = min(size, SESSION_TAIL_BYTES)
                f.seek(size - n)
                blob = f.read(n)
        except OSError as e:
            log("WARN 会话尾读不可用（fail-soft，按拿不到证据处置）: %r", e)
            return None, None
        lines = blob.split(b"\n")
        if n < size:
            lines = lines[1:]              # 首行被上界截断 → 不可解析，丢弃
        for raw in reversed(lines):
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except ValueError:
                continue                   # 坏行/半截行 → 继续往前找
            if not isinstance(entry, dict) or entry.get("type") != "message":
                continue
            msg = entry.get("message")
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                return entry, msg
        log("WARN 会话尾窗（末 %dB / 文件 %dB）内无 assistant 条目"
            "（fail-soft，按拿不到证据处置）", n, size)
        return None, None

    def report_delivered(self):
        """report.md 是否**非空在场**（判据单点 = scheduler.report_nonempty，惰性 import）。

        为何必须「非空」而不是「存在」：此前两例的形态就是「exitcode=0 而 report.md
        **零字节**」——按存在性判会让零字节报告算成已交付，前鉴形态一点没治。口径同源 =
        dispatch/DISPATCH.md「完成判定」与 agentd/report.py 的 has_report。
        惰性 import（不在模块顶部）：wrap 在每次 spawn 的关键路径上，顶部 import 面越小越好；
        导入失败 ⇒ 退化为本地最小实现（getsize>0 ∧ 有非空白字节）+ WARN，不抛。
        """
        path = os.path.join(self.home, "report.md")
        try:
            import scheduler               # 同目录；判据单点，不另写一份
            return bool(scheduler.report_nonempty(path))
        except Exception as e:
            log("WARN report_nonempty 单点不可导入（%r）→ 本地最小判定", e)
        try:
            if os.path.getsize(path) <= 0:
                return False
            with open(path, "rb") as f:
                return bool(f.read().strip())
        except OSError:
            return False

    def _model_error_detail(self, entry, msg, delivered):
        """诊断的证据小节：末条 ts / stopReason / usage 全零事实 / 是否只有 thinking /
        交付物自查 / 尾读窗口（验收 2 的五个事实全在内）。只读已解析对象，不报错。"""
        usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else {}
        cost = usage.get("cost") if isinstance(usage.get("cost"), dict) else {}
        keys = ("input", "output", "cacheRead", "cacheWrite", "totalTokens")
        all_zero = all(not usage.get(k) for k in keys) and not cost.get("total")
        content = msg.get("content")
        blocks = [b.get("type") for b in content if isinstance(b, dict)] \
            if isinstance(content, list) else []
        try:
            fsize = os.path.getsize(self.session_file)
        except OSError:
            fsize = -1
        return [
            "末条 assistant 事件 ts: %s"
            % (entry.get("timestamp") or msg.get("timestamp") or "?"),
            "stopReason: %s（rawStopReason: %s）"
            % (msg.get("stopReason"), msg.get("rawStopReason") or "无"),
            "errorMessage: %s" % (msg.get("errorMessage") or "（无）"),
            "usage 全零: %s（%s；cost.total=%s）"
            % (all_zero, " ".join("%s=%s" % (k, usage.get(k)) for k in keys),
               cost.get("total")),
            "该轮 content 块: %s ⇒ 只有 thinking 而无 text/toolCall: %s"
            % (blocks or "（无）", bool(blocks) and set(blocks) <= {"thinking"}),
            "provider/api/model: %s"
            % "/".join(str(msg.get(k) or "?") for k in ("provider", "api", "model")),
            "交付物自查: %s"
            % ("report.md 非空在场" if delivered else
               "report.md 不在场或零字节/纯空白（视同缺报告，口径同完成判定）"),
            "尾读窗口: 末 %dB / 文件 %dB（上界 SESSION_TAIL_BYTES=%dB）"
            % (min(max(fsize, 0), SESSION_TAIL_BYTES), fsize, SESSION_TAIL_BYTES),
        ]

    def model_error_gate(self):
        """收敛成功（pi 优雅退出 0）后的末轮模型错误档：返回本进程该用的退出码（0 ∨ 1）。

        根因（实证，2026-09-15）：上游模型请求失败时 pi 落一条 role=assistant ∧
        stopReason="error" ∧ usage 全零（只有 thinking 块）的条目后结束该轮，此后无排队 ⇒
        收敛判据（agent_settled ∧ 无在飞轮 ∧ 队列空）全满足 ⇒ 旧行为 exit 0 +
        notified.json complete:true，而 report.md 零落盘 = 假成功档。
        两档语义（登记方拍板）：
          (i)  error ∧ report.md 非空**不**在场 ⇒ 诊断 stage=model_error_stopreason + 退出码 1
               （与既有 wrap 层失败档 prompt_rejected/prompt_ack_timeout/wrap_error 同码，不新造语义）；
          (ii) error ∧ report.md 非空在场 ⇒ 透传 pi 退出码（0）+ WARN + 信息性诊断
               stage=model_error_stopreason_delivered（不参与完成判定）。
        只把 "error" 当失败档：pi 的 StopReason 值域 =
        pending|stop|length|toolUse|error|aborted|deferred（pi-ai/dist/types.d.ts 本体声明），
        "aborted"（取消/compaction 中止）纳入会造出取消场景的假阳性。
        resident 形态不经此闸（run() 里 resident 分支先返回：常驻会话无 report.md 交付语义）；
        定时触发面登记的任务**适用**此闸（它不带 AGENTD_RESIDENT=1 ⇒ 走收敛分支；
        期望行为：它同样以 report.md 为完成要件，且 restartPolicy 非 auto 不会重启成风暴）。
        任何拿不到证据的形态（jsonl 缺失/不可解析/无 assistant 条目/stopReason 缺失或不是 error）
        ⇒ 行为与旧版逐字一致（返回 0）；整个闸外层 try/except ⇒ 本档自身出 bug 不得把正常任务判失败。
        """
        try:
            entry, msg = self.last_assistant_message()
            if msg is None:
                return 0                   # fail-soft：无证据不改判定
            if msg.get("stopReason") != MODEL_ERROR_STOPREASON:
                return 0
            delivered = self.report_delivered()
            detail = self._model_error_detail(entry, msg, delivered)
            if delivered:
                log("WARN 末轮模型错误（stopReason=error）但交付物在场（report.md 非空）"
                    "→ 按 pi 退出码 exit 0；信息性记录已落 diagnosis.md（stage=%s，"
                    "不参与完成判定）", MODEL_ERROR_STAGE_DELIVERED)
                self.diagnose(MODEL_ERROR_STAGE_DELIVERED, 0,
                              extra="信息性记录、不改判定：交付物 report.md 非空在场、"
                                    "末轮模型请求失败（stopReason=error）",
                              detail=detail)
                return 0
            log("WARN 末轮模型错误（stopReason=error）且交付物缺位（report.md 非空不在场）"
                "→ 不视为正常完成：diagnosis stage=%s + 退出码 1", MODEL_ERROR_STAGE)
            self.diagnose(MODEL_ERROR_STAGE, 1,
                          extra="模型请求失败收尾（stopReason=error）且 report.md 非空不在场"
                                " ⇒ 收敛判据成立也不等于任务完成",
                          detail=detail)
            return 1
        except Exception as e:             # 新档自身异常 ⇒ 退回现行行为（不误判失败）
            log("WARN 模型错误档判定异常（fail-soft，退回现行行为）: %r", e)
            return 0

    def deliver_prompt(self):
        """初始投递。成功返回 None；失败返回诊断阶段串（pi 早死场景的退出码由调用方定）。
        幂等：会话 jsonl 已有 user 消息（复活/重放场景）→ 跳过。"""
        if self.session_has_user_message():
            log("会话已有 user 消息，跳过初始投递（幂等）")
            self.write_init_ok("resume-idempotent-skip")
            return None
        if self.resident and not os.path.exists(self.prompt_file):
            # resident 形态：初始引导可选（设计 决策 6）——无 prompt.md 裸启动，
            # 靠会话历史续跑；任务形态仍必达（缺失走下方诊断路径）。
            log("resident：无 prompt.md，裸启动（初始引导可选）")
            self.write_init_ok("resident-bare-start")
            return None
        try:
            with open(self.prompt_file, encoding="utf-8") as f:
                text = f.read()
        except OSError as e:
            self.diagnose("prompt_read_failed", 1, extra="prompt.md 不可读: %r" % e)
            return "prompt_read_failed"
        if not self.pi_send(json.dumps(
                {"id": "init-%d" % os.getpid(), "type": "prompt",
                 "message": text}, ensure_ascii=False).encode("utf-8")):
            self.diagnose("prompt_send_failed", 1,
                          extra="pi stdin 不可写（启动即炸？见 stderr 尾）")
            return "prompt_send_failed"
        # 等回执：分段等待 + 早死探测（启动即炸时不等满超时）
        deadline = time.monotonic() + self.init_timeout
        while not self.init_waiter.wait(0.5):
            if self.pi.poll() is not None:
                return "pi_died_before_ack"   # 退出码由调用方透传记账（诊断在 run）
            if time.monotonic() >= deadline:
                self.diagnose("prompt_ack_timeout", 1,
                              extra="初始投递 %.0fs 无回执" % self.init_timeout)
                return "prompt_ack_timeout"
        resp = self.init_resp or {}
        if not resp.get("success"):
            self.diagnose("prompt_rejected", 1,
                          extra="初始投递被拒: %s" % (resp.get("error") or "?"))
            return "prompt_rejected"
        log("初始 prompt 已投递并被接受")
        self.write_init_ok("prompt-accepted")   # 就绪门开：子端可以开始认领自家 inbox
        return None

    # ---------- pi stdin 单写者 ----------

    def pi_send(self, raw_line):
        """单写者串行写入 pi stdin（与透传上行、自身命令复用同一把锁）。"""
        with self.lock:
            if not self.stdin_open or self.pi is None or self.pi.stdin is None:
                return False
            try:
                self.pi.stdin.write(
                    raw_line if raw_line.endswith(b"\n") else raw_line + b"\n")
                self.pi.stdin.flush()
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

    # ---------- stdout/stderr 泵 ----------

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
                self.ring.append(raw)
                with self.lock:
                    c = self.conn
                if c is not None:
                    try:
                        with self.send_lock:
                            c.sendall(raw)
                    except OSError:
                        self._drop_conn(c)
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
                if isinstance(obj, dict):
                    self._on_event(obj)
        except (OSError, ValueError):
            pass

    def _on_event(self, obj):
        t = obj.get("type")
        if t == "response" and obj.get("id", "").startswith("init-"):
            self.init_resp = obj
            self.init_waiter.set()
        if t == "response" and obj.get("success") is True \
                and obj.get("command") in ("prompt", "follow_up", "steer"):
            self._turn_activity()               # 注入被受理 = 竞态窗活动
        elif t == "queue_update":
            busy = bool(obj.get("steering")) or bool(obj.get("followUp"))
            self.queue_busy = busy
            if busy:
                self._turn_activity()
        elif t == "message_start":
            m = obj.get("message") or {}
            if m.get("role") == "user":
                self._turn_activity()           # 新轮次开始
        elif t == "agent_start":
            self.inflight += 1                  # 在飞轮开始
        elif t == "agent_end":
            self.inflight = max(0, self.inflight - 1)  # 在飞轮结束（钳位防负）
        elif t == "agent_settled":
            self.settled = True

    def _turn_activity(self):
        self.activity = True

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

    # ---------- 观测连接（透传） ----------

    def accept_loop(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            with self.lock:
                old = self.conn
                self.conn = conn
            if old is not None:
                # shutdown 先：close 不会唤醒对端/本端阻塞中的 recv（经典坑），
                # 半开连接会一直挂着；shutdown 双向关闭才能把 EOF 送达两端。
                self._shutdown_close(old)
            try:
                with self.send_lock:
                    for raw in list(self.ring):  # 小环回放（短窗补缺，基线走 get_entries）
                        conn.sendall(raw)
            except OSError:
                self._drop_conn(conn)
                continue
            log("观测连接就位（替换=%s）", old is not None)
            threading.Thread(target=self._conn_relay, args=(conn,),
                             daemon=True, name="wrap-relay").start()

    def _conn_relay(self, conn):
        buf = b""
        while True:
            with self.lock:
                if self.conn is not conn:
                    return                       # 已被新连接替换
            try:
                d = conn.recv(65536)
            except OSError:
                break
            if not d:
                break
            buf += d
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                if not self.pi_send(line):
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

    # ---------- 完成收敛 ----------

    def settle_loop(self):
        """收敛监督：agent_settled ∧ 无在飞轮 ∧ 队列空 → 竞态窗 → 关 pi stdin。"""
        while self.pi.poll() is None and not self.converging:
            if not self.settled:
                time.sleep(0.1)
                continue
            # 假 settled 免疫：在飞轮未结束（inflight > 0）→ 不进入收敛判定。
            # pi 的 _runAgentPrompt finally 无条件发 agent_settled；同 tick 并发注入时后到者
            # 被 activeRun throw 丢弃 → 走 finally → 假 settled（inflight 仍 > 0）。
            # 只有 agent_end 把 inflight 归零后，真 settled 才触发收敛。
            if self.inflight > 0:
                time.sleep(0.1)
                continue
            # 竞态窗：窗内出现新轮次/入队活动 → 取消本轮，等下一次 settled
            self.settled = False
            self.activity = False
            deadline = time.monotonic() + self.settle_window
            while time.monotonic() < deadline:
                if self.activity or self.queue_busy:
                    break
                time.sleep(0.05)
            if self.activity or self.queue_busy:
                self.activity = False
                log("收敛取消：竞态窗内出现新活动，等待下一次 agent_settled")
                continue
            self.converging = True
            log("完成收敛：agent_settled ∧ 队列空 → 关闭 pi stdin（EOF 优雅退出）")
            self.close_pi_stdin()
            threading.Thread(target=self._exit_watchdog, daemon=True,
                             name="wrap-exit-wd").start()
            return
        # 循环退出 = pi 已先退出（异常路径，主线程记账）

    def _exit_watchdog(self):
        """收敛后宽限：EOF 退出实测亚秒级；超时未退 → SIGTERM→3s→强杀（诊断记账）。"""
        deadline = time.monotonic() + self.exit_grace
        while time.monotonic() < deadline:
            if self.pi.poll() is not None:
                return
            time.sleep(0.1)
        if self.pi.poll() is None:
            log("收敛后 %.0fs 未退出，SIGTERM", self.exit_grace)
            self.converge_timeout = True
            try:
                self.pi.terminate()
            except OSError:
                pass
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and self.pi.poll() is None:
                time.sleep(0.1)
            if self.pi.poll() is None:
                self._kill_pi_hard()

    # ---------- 诊断 ----------

    def diagnose(self, stage, exitcode, extra="", detail=None):
        """进程级失败诊断（result.md 退役口径）：$AGENT_HOME/diagnosis.md。
        detail = 可选证据行列表（渲染成一个 `## 证据（<stage>）` 小节）。
        注意：stage=model_error_stopreason_delivered 是**信息性**记录（exitcode 0、交付物在场），
        不参与完成判定——task-layout.md 的 diagnosis.md 行已记该唯一例外。"""
        if self.diag_written:
            return
        self.diag_written = True
        lines = [
            "# task rpc 诊断（diagnosis.md）",
            "",
            "> 任务进程级失败诊断（pi-rpc-wrap.py 写；result.md 已退役——",
            "> 正常完成证据 = session.jsonl 过程 + report.md 结论）。",
            "",
            "- task: %s" % self.task_id,
            "- stage: %s" % stage,
            "- exitcode: %s" % exitcode,
            "- time: %s" % proto.now_ts(),
            "- session: %s" % self.session_file,
        ]
        if extra:
            lines += ["- note: %s" % extra]
        if detail:
            lines += ["", "## 证据（%s）" % stage, ""]
            lines += ["- %s" % d for d in detail]
        tail = self.stderr_tail_text()
        if tail.strip():
            lines += ["", "## pi stderr 尾（≤%dB）" % DIAG_STDERR_LIMIT,
                      "", "```", tail.rstrip(), "```"]
        else:
            lines += ["", "## pi stderr 尾", "", "（空）"]
        try:
            proto.atomic_write(os.path.join(self.home, "diagnosis.md"),
                               "\n".join(lines) + "\n")
            log("诊断已落盘: %s/diagnosis.md (stage=%s)", self.home, stage)
        except OSError as e:
            log("WARN: 诊断落盘失败: %r", e)

    # ---------- 主流程 ----------

    def run(self):
        self.setup_socket()
        threading.Thread(target=self.accept_loop, daemon=True,
                         name="wrap-accept").start()
        self.spawn_pi()
        stage = self.deliver_prompt()
        if stage is not None:
            if self.pi.poll() is None:
                self._kill_pi_hard()       # wrap 层失败：自行清理 pi（退出码不代表任务事实）
            if stage == "pi_died_before_ack":
                # pi 自身早死（启动即炸）：退出码透传记账（runner 侧死因保真）
                rc = self.pi.returncode if self.pi.returncode is not None else -9
                code = rc if rc >= 0 else 128 + (-rc)
                self.diagnose(stage, code,
                              extra="pi 在初始投递回执前退出 rc=%s（启动即炸）" % rc)
                return code if code != 0 else 1
            return 1
        if not self.resident:
            self.await_child_arm()   # 有界等子端首次补扫落定，再进收敛监督
            threading.Thread(target=self.settle_loop, daemon=True,
                             name="wrap-settle").start()
        # 等待 pi 退出：正常路径 = 收敛关 stdin 后 EOF 优雅退出（实测亚秒级）；
        # 收敛后宽限看门狗在 settle_loop 内（防 wait 先于收敛进入无限等）。
        rc = self.pi.wait()
        code = rc if rc >= 0 else 128 + (-rc)
        if self.resident:
            # resident 形态：无完成收敛语义——pi 任何退出都是代终止事实
            #（崩溃 → runner auto 重启续跑；stop/restart/clear → 组杀），退出码透传记账，
            # 不写诊断不误报。
            log("resident: pi 退出 rc=%s → wrap 退出码 %d 透传"
                "（崩溃自愈/换代归属 runner）", rc, code)
            return code
        if self.converging and rc == 0:
            # 末轮模型错误档：读点在 **pi 退出后** = 登记方放行的位置
            #（pi 的全部写已 flush ⇒ 无落盘竞态；任务书原写「收敛前」已作废，别按字面改回去）。
            gate = self.model_error_gate()
            if gate != 0:
                return gate
            log("任务收敛完成（exit 0）")
            return 0
        if not self.converging:
            # pi 先于收敛退出 = 异常（崩溃/被外部组杀外的信号等）
            self.diagnose("pi_exited_unexpected", code)
        elif self.converge_timeout:
            self.diagnose("converge_timeout_killed", code,
                          extra="关 stdin 后 %.0fs 未退出，宽限看门狗动手"
                          % self.exit_grace)
        elif rc != 0:
            self.diagnose("converge_nonzero_exit", code)
        return code

    def _kill_pi_hard(self):
        try:
            self.pi.kill()
            self.pi.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass


def main():
    w = Wrap()

    def _on_term(signum, _frame):
        # 优雅停机：关 stdin 让 pi EOF 退出（正常路径由 runner 组杀覆盖，此为兜底）
        w.close_pi_stdin()
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, _on_term)
    try:
        code = w.run()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    except Exception:
        import traceback
        tb = traceback.format_exc()
        log("WRAP ERROR:\n%s", tb)
        w.diagnose("wrap_error", 1, extra=tb.splitlines()[-1] if tb else "?")
        code = 1
    sys.exit(code)


if __name__ == "__main__":
    main()
