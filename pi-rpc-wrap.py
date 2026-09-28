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
  1. 拉起 `pi --mode rpc`（--session/-n/-e 子端扩展×3 + sessiond 探针扩展/-xt ask_user；
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
stdin，主线程只等 pi 退出）、argv 会话名钉死 $AGENTD_SESSION_NAME（不注入子端扩展、
不屏蔽 ask_user）、prompt.md 可选（不存在则裸启动）、pi 任何退出 = 代终止事实透传退出码，
不写诊断不误报（崩溃自愈/换代归属 = runner restartPolicy=auto / control 三动作）。
其余（socket 生命周期/.pid 伴生档/透传/小环）逐字复用。

人格装配（两层模型 = 原子能力 CAP + profile；机制口径权威 = assistant/DISPATCH.md §3）：
env DISPATCH_PROFILE=<profile 名>（**单值**；链式组合已退役不留兼容，能力组合住 profile 的 caps
列表）→ 读薄清单 $AGENT_ROOT/bots/profiles/<名>.json（纯只读，一份资产可被任意多会话引用；字段
只有 name/summary/notes/model/caps/contextCompaction，**不直挂**捆绑资产）→ 按 `caps` 列表序逐个展开原子能力
$AGENT_ROOT/bots/caps/<能力>/：
  - prompt.md → 一个 --append-system-prompt（**装配器不碰正文一个字节**：无 frontmatter 剥离、无改写；
    多能力 = 多次追加，pi 原生追加语义；正文缺失 = bundle 能力，合法形态）；
  - cap.yml（YAML，`yaml.safe_load`）= 装配声明：skills → 共享库 bots/skills/<名>/ 一级解析
    （**不回落全局**）各一个 --skill；extensions → bots/extensions/<名>/（含 index.ts 则恰一个 -e，
    否则直属每个 .ts 按名排序各一个 -e；依赖一律 .ts，jiti 刷不掉 .mjs ESM 缓存）；
    knowledge（**lore 仓根下的名**，首段 = 层标识 library/desk/archive；工作区路径声明仍可解析
    = legacy 档）跨能力并集去重保序；tools/excludeTools 见下。
  - **任务形态恒前置 `executor` 能力**（装配器硬规则，防漏列）；resident 形态不前置。
  - **任务形态未设 profile（∨ 名字非法 = 按未设处置）时回落 `executor` profile**（`model` 只住 profile ⇒
    回落面就是任务形态的缺省模型角色档）；回落复用同一条解析路径，注入面与只前置基线能力逐字一致。
    resident 形态不回落（argv 逐字不变）；显式设了合法 profile 名（哪怕清单缺失）也不回落。
工具面并集语义：-xt = ∪(声明者 excludeTools) ∪ 形态基线（任务形态 = ask_user，resident 为空）；
-t = ∪(声明者 tools)；未声明者不参与合并。pi 的 excludeTools 在 tools 白名单**之后**生效 ⇒
最终工具面 = （∪tools）−（∪excludeTools ∪ 基线）；被排除掉的白名单项 = WARN 不阻断。出参中
-xt 至多一个（排除集非空时恰一个）、-t 至多一个——pi 两者都是赋值语义（重复出现后者覆盖前者），
拆开拼会解除基线屏蔽；-e/--skill 才是累加。model **只住 profile**（能力层无此字段：复用单元
不该决定运行环境）→ 至多一个 --model，**并由其 provider 段派生至多一个 --provider**（形如
<provider>/<id> 才派生；缺失/无斜杠/畸形 = 不注入 --provider、落回 settings 默认，判定单点
provider_of_model；注入序固定 = --provider 在 --model 之前，两形态一致）；任务形态未设 profile
时回落 `executor` profile ⇒ 任务缺省即带它的 model（回落面解析不到 model = WARN + 不注入，
落回 settings 默认）。
knowledge 名 → 装配时调 $AGENT_ROOT/bots/kb_index.py 按名解析并渲染「知识清单」块，以一个额外的
--append-system-prompt 追加在**全部能力正文之后**（与 skill 清单注入同构：清单进 prompt、内容按需
read，消费纪律由块自带）；**注入档由名的首段决定**（library = 逐册 when 表、desk = journal 一行
检索入口、archive = 一行检索入口）；名按 lore 根一级解析、**不回落**，解析不到 = 一行降级说明进块
+ WARN（不静默）；**字段缺失 = argv 逐字不变**；kb_index 不可导入/渲染异常/lore 仓未克隆 = WARN
跳过该块（不拖垮会话）。声明与注入形态的规范权威 = bots/README.md「知识库规范」，机制口径 =
assistant/DISPATCH.md §3，lore 内容组织 = the workspace knowledge-base README §5。
上下文压缩策略（profile 的 `contextCompaction`，与 `model` 同类的**运行环境/策略**字段，不是资产
逃生口）：`model` 决定用哪个模型，本字段决定「上下文长到多少就触发 compaction」。pi 的 compaction
阈值只住 settings（全局 `~/.pi/agent/settings.json` ∨ 项目级 `<cwd>/.pi/settings.json`），**无
per-session 覆盖通道**（无 CLI flag、无 `PI_*` env 覆盖项、`ExtensionContext` 不暴露 settings），而
项目级 settings = workdir 共享（多个 profile 共用同一 workdir）⇒ 做不到 per-profile。故装配面 =
校验/归一该字段后注入 ① env `AGENTD_CONTEXT_COMPACTION=<紧凑 JSON>` ② 一个
`-e $AGENT_ROOT/bots/extensions/context-compaction/index.ts`（扩展单元自建触发 + 对 pi 内建
`threshold` 触发的 cancel，把触发点后移到本策略阈值；自建触发在 agent 空闲时兜现——`ctx.compact()`
内部先 `abort()`，轮内调用会中止在飞 run）。**task 与 resident 两形态同等装配**（策略住
profile，与形态无关），注入位在能力（caps）之后。字段缺失 = env 与 `-e` 两者都不注入（argv/env
逐字不变）；字段非法 ∨ 扩展文件缺失 = WARN + 不注入（与 profile 缺失同口径的告警降级，不硬失败）。
只控「何时触发/是否触发」，**不控「保留多少」**：pi 的 `keepRecentTokens`/`reserveTokens` 切点在
`prepareCompaction(pathEntries, settings)` 内算定，扩展事件里改不动 ⇒ 本字段不支持这两个键（规范
正文与理由 = bots/README.md「人格资产」节）。
降级总原则（沿现状风格）：未设该 env → 任务形态回落 `executor` profile（注入面 = 基线能力，另取其
model；回落面不可用则只注入基线能力、不注入 --model）、resident argv 逐字不变；profile 名
非法（含 / \\ , 或以 . 开头）/清单缺失或损坏/能力目录缺失/cap.yml 不可解析/pyyaml 不可导入/
contextCompaction 非法 = 告警跳过该层，不硬失败不拖垮会话；全无可注入 = 裸启动。不做继承（无
extends）、能力不引用能力。
协议层扩展（ask-user-child/message-child/探针）仍归调度层注入，与人格装配无关。

失败域：观测面（accept/转发）异常只断观测不伤收敛主线；未捕获异常兜底写
诊断后退出 1。除 pyyaml（cap.yml 解析，缺失即按上述降级分支处置）外仅使用 python3 标准库。
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
# shared with the TypeScript side (assistant/.pi/extensions/agentd/core.ts) and
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
CHILD_EXTS = (            # 子端扩展（相对 $AGENT_ROOT，与 core.ts 的 *_REL 常量同源）
    "assistant/.pi/extensions/agentd/ask-user-child.ts",
    "assistant/.pi/extensions/agentd/message-child.ts",
    # 子任务自家信箱的推送收件面：只收 agents/task/<本任务 id>/inbox（收件面
    # 锁死单点 = core.taskSelfMailbox），按信封 deliver 选 steer/followUp 注入。只进任务形态
    # 分支：resident（bot 型常驻会话）由 workdir 的 .pi 自动发现主端 index.ts，其 receiver 已
    # 覆盖自家信箱，再注入本扩展只会白占一份 watch/poll。
    "assistant/.pi/extensions/agentd/receiver-child.ts",
)
PROBE_EXT_REL = "w/ext/sessiond/probe.ts"   # sessiond 探针：任务/常驻会话
                                            # 也注入，使 spec.json?v=chat 观测面 /inspect 可用
KB_INDEX_REL = "bots/kb_index.py"           # lore 资产清单 + 全局名字索引工具：能力 cap.yml 的
                                            # knowledge 名 → 「知识清单」注入块（三档渲染）
CAPS_REL = "bots/caps"                      # 原子能力库：<名>/{cap.yml,prompt.md}（prompt.md 可缺省
                                            # = bundle 能力，只有捆绑声明）
PROFILES_REL = "bots/profiles"              # profile 薄清单：<名>.json（字段只有 name/summary/notes/
                                            # model/caps/contextCompaction，不直挂捆绑资产）
SKILLS_REL = "bots/skills"                  # skill 共享库：cap.yml 按名捆绑，一级解析、不回落全局
EXTS_REL = "bots/extensions"                # 扩展共享库：一个名字 = 一个扩展单元，一律 .ts
TASK_BASELINE_CAP = "executor"              # 任务形态恒前置的能力（装配器硬规则）；resident 不前置
TASK_FALLBACK_PROFILE = "executor"          # 任务形态未设 DISPATCH_PROFILE（∨ 名字非法 = 按未设处置）时
                                            # 回落的缺省 profile：`model` 只住 profile ⇒ 回落面即任务形态的
                                            # 缺省模型角色档（resident 形态不回落；其 caps 就是基线能力本身
                                            # ⇒ 注入面与回落前逐字一致）。fail-soft 见 _resolve_caps
CAP_ALLOWED_FIELDS = frozenset({"summary", "skills", "extensions", "knowledge",
                                "tools", "excludeTools"})   # cap.yml 合法键闭合集（禁 caps/model）
PROFILE_BANNED_FIELDS = ("skills", "extensions", "knowledge", "tools",
                         "excludeTools")    # profile 只列 caps，不给逃生口 ⇒ 直挂即 WARN 忽略。
                                            # `contextCompaction` 属运行环境/策略类字段（与 model
                                            # 同类），不在本名单里、也不构成资产直挂的逃生口
CONTEXT_COMPACTION_ENV = "AGENTD_CONTEXT_COMPACTION"   # 归一化策略的注入通道（紧凑 JSON），消费方 =
                                            # bots/extensions/context-compaction/index.ts
CONTEXT_COMPACTION_EXT_REL = "bots/extensions/context-compaction/index.ts"   # 执行体（扩展单元）；
                                            # 文件缺失 → WARN + 不注入（照 PROBE_EXT_REL 口径）
CC_FIELDS = ("enabled", "triggerTokens", "triggerRatio",
             "customInstructions")         # `contextCompaction` 键白名单（白名单外一律非法：拼错的
                                            # 键被静默忽略会改变语义）。**不含** keepRecentTokens/
                                            # reserveTokens —— 不可达面（切点在 pi 的
                                            # prepareCompaction 内算定），见 _cc_policy
CC_INSTRUCTIONS_MAX = 2000                 # customInstructions 字符数上界（与 policy.ts 同口径）


def _cc_policy(raw):
    """profile 的 `contextCompaction` 字段 → (归一化 dict ∨ None, 错误原因 ∨ None)。

    判据表（单一事实源 = bots/README.md「人格资产」节；同口径的另两份实现 = lint 的 E16 与
    扩展侧 `bots/extensions/context-compaction/policy.ts:parsePolicy`，三者必须同判：lint 通过的
    资产一定被本装配器装配，lint 报 E16 的一定在这里 WARN 丢弃）：
      - 顶层非对象 / 含白名单外的键 ⇒ 非法（整块丢弃，不「忽略未知键」）；
      - `enabled` 非 bool（缺省 true）、`triggerTokens` 非正整数、`triggerRatio` 非 0<r<=1 的数、
        `customInstructions` 非非空字符串 ∨ 超 CC_INSTRUCTIONS_MAX 字符 ⇒ 非法；
      - `triggerTokens` 与 `triggerRatio` **至少给一个**，除非 `enabled` 为 false（关掉自动压缩时
        允许只留 enabled:false；两者都给 = OR 语义 = 先到者触发，判定在扩展侧）。
    归一化输出只含实际声明的键 + 恒含 `enabled`（紧凑 JSON 进 env，扩展侧再解析一次）。
    """
    if not isinstance(raw, dict):
        return None, "顶层非对象（%s）" % type(raw).__name__
    unknown = [k for k in raw if k not in CC_FIELDS]
    if unknown:
        return None, ("含白名单外的键 %s（合法键 = %s；keepRecentTokens/reserveTokens 属不可达面，"
                      "不支持）" % (",".join(sorted(map(str, unknown))), "/".join(CC_FIELDS)))
    out = {"enabled": True}
    if "enabled" in raw:
        if not isinstance(raw["enabled"], bool):
            return None, "enabled 非布尔（%.40r）" % (raw["enabled"],)
        out["enabled"] = raw["enabled"]
    if "triggerTokens" in raw:
        v = raw["triggerTokens"]
        if isinstance(v, bool) or not isinstance(v, int) or v < 1:
            return None, "triggerTokens 非正整数（%.40r）" % (v,)
        out["triggerTokens"] = v
    if "triggerRatio" in raw:
        v = raw["triggerRatio"]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 < v <= 1:
            return None, "triggerRatio 非 0<r<=1 的有限数（%.40r）" % (v,)
        out["triggerRatio"] = v
    if "customInstructions" in raw:
        v = raw["customInstructions"]
        if not isinstance(v, str) or not v.strip():
            return None, "customInstructions 非非空字符串（%.40r）" % (v,)
        if len(v) > CC_INSTRUCTIONS_MAX:
            return None, ("customInstructions 超 %d 字符（实得 %d）"
                          % (CC_INSTRUCTIONS_MAX, len(v)))
        out["customInstructions"] = v
    if out["enabled"] is not False and "triggerTokens" not in out \
            and "triggerRatio" not in out:
        return None, ("既无 triggerTokens 也无 triggerRatio（enabled 非 false 时至少给一个，"
                      "否则策略无触发点）")
    return out, None


def provider_of_model(model):
    """profile 的 `model` 值 → 其 provider 段（供 `--provider` 注入）∨ None（= 不注入）。

    声明源只有既有的 profile `model` 字段（**不新增 profile 字段、不硬编码任何 provider 名**）：
    形如 `<provider>/<id>`（含且只含一个 `/`、且 provider 段非空）时返回 provider 段；
    其余一律返回 None ⇒ 调用方不拼 `--provider`，pi 落回 `settings.json` 的默认 provider。

    **fail-soft 是硬要求**（与既有 `--model` 的降级口径同源）：本函数处在所有任务 spawn 的
    公共路径上，任何畸形值都只降级、**绝不 die**（硬失败会自锁——连「修这条路径」的修复任务
    都起不来）。畸形判定逐条：
      - None / 非字符串 / 空白串 → None（无 model 声明，provider 无从派生）；
      - 不含 `/`（如 `qwen3.8-max`）→ None（裸模型 id：pi 自己按 settings 默认 provider 解析）；
      - `/` 前段为空（如 `/planner`）→ None（provider 段空 = 注入空值反而覆盖掉默认）；
      - 含 ≥2 个 `/`（如 `a/b/c`）→ None（两段式不成立，**不猜切分点**：`--model` 仍原样注入，
        由 pi 自行解析，装配器不做二次判断）。
    返回的 provider 段已 strip（与 `model` 取值处的 strip 同口径）。

    **写侧约束（只记边界、不加代码校验）**：profile 的 `model` 前段必须是 pi 已配置的 provider 名。
    pi 的 `resolveCliModel`（`dist/core/model-resolver.js`）对显式 `--provider <未知>` 是**硬失败**
    （返回 `Unknown provider "<x>"` 错误），而单独一个 `--model <未知>/<id>` 仍可能经 model id 字面
    精确匹配解析成功 ⇒ 注入 `--provider` 会**窄化**该容错面（两者都给时 pi 剥掉 model 的 provider
    前缀，不双重前缀）。现网无实例：各 profile 的 model 前段均为已配置 provider。**不做写侧校验**：
    profile 是跳机同步面，校验会在同步时刻差上误拒，与本函数「只降级不 die」的口径相左。"""
    if not isinstance(model, str):
        return None
    m = model.strip()
    if not m or "/" not in m:
        return None
    if m.count("/") != 1:
        return None                        # 畸形：多段，不猜切分点
    prov = m.split("/", 1)[0].strip()
    return prov or None                    # `/id` 形态：provider 段空 ⇒ 不注入


def log(fmt, *args):
    sys.stderr.write(("[pi-rpc-wrap] " + fmt + "\n") % args)
    sys.stderr.flush()


class Wrap:
    def __init__(self):
        self.home = os.environ.get("AGENT_HOME", "")
        self.root = os.environ.get("AGENT_ROOT", "")
        self.task_id = os.environ.get("AGENT_SELF", "")
        # resident 模式（设计 §2.2）：标记与心跳 DISPATCH_HEARTBEAT=1
        # 同款 = spec.command 内嵌 env 前缀，经 bash -c 天然注入，不依赖 runner 继承。
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
        # profile 的 `contextCompaction` 装配态（_resolve_caps 解析 → _compaction_argv 注入）：
        # 前者 = 归一化策略 dict ∨ None（字段缺失/非法都是 None），后者 = 是否真拼了 `-e`
        #（执行体缺失时 = False ⇒ spawn_pi 也不传 env，两者同进同退）。
        self.cc_policy = None
        self.cc_injected = False
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
            # 不注入子端扩展（主端全套扩展由 workdir 的 .pi 自动发现）、不屏蔽 ask_user
            #（主端 ask_user 链路保留）。
            argv = [self.pi_bin, "--mode", "rpc", "--session", self.session_file,
                    "-n", self.session_name or "[resident %s]" % self.name]
            argv += self._probe_ext_argv()
            argv += self._profile_argv()
            argv += self._compaction_argv()
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
        # 基线屏蔽项不单独拼 -xt：与能力声明的 excludeTools 并集为单个 -xt（pi 的 -xt 是
        # 赋值、后者覆盖，拆开拼会解除基线屏蔽）；无声明时 _profile_argv 原样兜底拼回。
        argv += self._profile_argv(xt_baseline=("ask_user",))
        argv += self._compaction_argv()
        return argv

    # ---------- knowledge 知识清单注入（规范 bots/README.md「知识库规范」） ----------

    def _kb_module(self):
        """按文件路径导入 $AGENT_ROOT/bots/kb_index.py（模块名带点/不在 sys.path，走 importlib）。
        结果缓存在实例上；不可导入（文件缺失/语法错/依赖缺失）→ WARN + None（知识清单不注入，
        会话照常起——清单是增强面，不是启动必需）。"""
        if hasattr(self, "_kb_mod"):
            return self._kb_mod
        self._kb_mod = None
        p = os.path.join(self.root, KB_INDEX_REL)
        if not os.path.isfile(p):
            log("WARN: kb 索引工具缺失（%s），跳过 knowledge 清单注入", p)
            return None
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location("kb_index", p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            self._kb_mod = mod
        except Exception as e:            # 导入面任何异常都不该拖垮会话装配
            log("WARN: kb 索引工具导入失败（%s）%r，跳过 knowledge 清单注入", p, e)
        return self._kb_mod

    def _knowledge_argv(self, entries):
        """knowledge 名列表 → 一个 --append-system-prompt（渲染好的「知识清单」块）。
        entries = 跨能力并集后的声明原样列表（**lore 仓根下的名** ∨ 工作区路径 = legacy 档），
        名解析/分档/去重/清洗/渲染全部交 kb_index（判定单点，与 CLI/巡检同一套口径）；本函数只多记
        一行解析摘要（lore 档按层计数 / legacy 档计数 / lore 根在场性），便于排障「清单为何少了某面」。
        无有效名/渲染为空/异常 → ([], 0)：argv 逐字不变（字段缺失零回归的同一出口）。
        返回 (argv, 块字符数)。"""
        if not entries:
            return [], 0
        mod = self._kb_module()
        if mod is None:
            return [], 0
        try:
            warns = []
            block = mod.knowledge_block(entries, root=self.root, warnings=warns)
            for w in warns:
                log("WARN: knowledge %s", w)
            self._log_knowledge_resolve(mod, entries)
        except Exception as e:
            log("WARN: knowledge 清单渲染异常 %r，跳过注入（会话照常起）", e)
            return [], 0
        if not block.strip():
            log("WARN: knowledge 声明 %d 项但无有效名（全部被拒/为空），跳过清单注入",
                len(entries))
            return [], 0
        return ["--append-system-prompt", block], len(block)

    def _log_knowledge_resolve(self, mod, entries):
        """一行解析摘要（只进日志、不影响 argv）：lore 档按层计数 + legacy 档计数 + lore 根在场性。
        本函数全程 try 包裹——日志面任何异常都不得伤装配主线。"""
        try:
            lore = mod.lore_root(self.root)
            got = mod.normalize_domains(entries, self.root, None, lore)
            tiers, legacy = {}, 0
            for e in got:
                if e.get("kind") == "lore":
                    tiers[e.get("tier")] = tiers.get(e.get("tier"), 0) + 1
                else:
                    legacy += 1
            log("knowledge 名解析：lore 档 %d 项%s，legacy 工作区路径档 %d 项（lore 根 %s）",
                sum(tiers.values()),
                "（%s）" % ", ".join("%s×%d" % (k, v) for k, v in sorted(tiers.items()))
                if tiers else "",
                legacy, lore if os.path.isdir(lore) else "%s 不在场" % lore)
        except Exception as e:
            log("WARN: knowledge 解析摘要计算异常 %r（不影响清单渲染）", e)

    def _probe_ext_argv(self):
        """sessiond 探针扩展注入（观测面 /inspect 支持）：文件缺失只告警不拖垮会话，
        口径与 proc.py:_spawn 同款；此时 /inspect 回「探针未加载」而非 403。"""
        p = os.path.join(self.root, PROBE_EXT_REL)
        if os.path.exists(p):
            return ["-e", p]
        log("WARN: 探针扩展缺失，跳过注入（/inspect 不可用）: %s", p)
        return []

    # ---------- 人格装配（两层模型：profile = 原子能力声明列表；机制口径 DISPATCH.md §3） ----------

    @staticmethod
    def _profile_name(raw):
        """`DISPATCH_PROFILE` = **单值 profile 名**（链式组合已退役、不留兼容：能力组合住 profile 的
        `caps` 列表，注入序 = 列表序）。文法白名单：拒 `/`、`\\` 与前导 `.`（可能走出 profiles/；
        主闸 = 登记侧 core.ts 白名单）；**含逗号 = 已退役的链式写法**（如旧 `executor,review`）⇒
        WARN 点名成因后按未设处置（诊断价值：现网存量 spec.command 里可能还有旧链）。
        返回名字 ∨ None。"""
        n = (raw or "").strip()
        if not n:
            return None
        if "," in n:
            log("WARN: DISPATCH_PROFILE %r 含逗号 = 已退役的链式写法（现为单值 profile 名，"
                "能力组合住 profile 清单的 caps 列表）→ 按未设处置", n)
            return None
        if "/" in n or "\\" in n or n.startswith("."):
            log("WARN: DISPATCH_PROFILE 名字非法 %r（含 / 或 \\ 或以 . 开头），跳过", n)
            return None
        return n

    def _load_profile_doc(self, name):
        """读 profile 薄清单 `bots/profiles/<名>.json` → dict ∨ None。
        缺失 / 不可读 / JSON 损坏 / 顶层非对象 = WARN + None（任务形态仍前置基线能力，
        resident = 裸启动）。直挂捆绑字段（profile 只列 caps，不给逃生口）= WARN 忽略该字段。"""
        pf = os.path.join(self.root, PROFILES_REL, name + ".json")
        if not os.path.isfile(pf):
            log("WARN: profile %r 不存在（%s），跳过（任务形态仍会前置基线能力；"
                "resident 形态 = 裸启动）", name, pf)
            return None
        try:
            with open(pf, encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, ValueError) as e:
            log("WARN: profile %r 清单不可读/损坏 %r，跳过", name, e)
            return None
        if not isinstance(doc, dict):
            log("WARN: profile %r 清单顶层非对象（%s），跳过", name, type(doc).__name__)
            return None
        for banned in PROFILE_BANNED_FIELDS:
            if banned in doc:
                log("WARN: profile %r 清单直挂 %r 字段（profile 只列 caps；捆绑资产住能力 cap.yml，"
                    "要额外装就建一个 bundle 能力）→ 忽略该字段", name, banned)
        return doc

    def _resolve_caps(self):
        """`DISPATCH_PROFILE` 单值 → (能力名有序列表, model ∨ None, profile 名 ∨ None)。
        注入序 = profile 的 `caps` 列表序（平铺，能力不引用能力）；**任务形态恒前置 `executor` 能力**
        （装配器硬规则承担，防漏列；resident 形态不前置）。`model` 只住 profile（能力层无此字段：
        复用单元不该决定运行环境）。降级：caps 缺失/非数组/元素非法 → WARN 逐项跳过。
        **任务形态未设 profile（∨ 名字非法 = 按未设处置）⇒ 回落 `TASK_FALLBACK_PROFILE`**：回落复用
        **同一条**解析路径（清单读取 / caps 校验 / model 取值 / 基线前置去重全部照旧，不另写平行分支），
        故回落后的 caps 与「只前置基线能力」逐字一致（回落 profile 的 caps 就是基线能力本身），
        差别只是拿到它的 `model`。resident 形态**不回落**（argv 逐字不变）；显式设了合法 profile 名
        （哪怕清单缺失）**也不回落**（名字合法 = 作者有指定意图，缺失属降级而非未设）。
        返回 (能力名有序列表, model ∨ None, profile 名 ∨ None)——第三项 = 实际使用的 profile 名
        （回落时即回落面），供调用方记日志，不重复走 `_profile_name`（否则同一枚非法值会刷两条 WARN）。"""
        name = self._profile_name(os.environ.get("DISPATCH_PROFILE", ""))
        fallback = False
        if name is None and not self.resident:
            # 任务形态的缺省模型角色档：`model` 只住 profile，未设 profile 就拿不到 ⇒ 回落
            # `TASK_FALLBACK_PROFILE`。**fail-soft 是硬要求**：这条路径影响所有任务 spawn，回落面
            # 缺失/不可解析/无 model/类型非法一律 WARN + 不注入 --model（落回 settings 默认），
            # **绝不 die**——硬失败会自锁（连「修这条路径」的修复任务都起不来）。降级全靠下面
            # 既有的 `_load_profile_doc` / model 类型分支承担，本处不重复实现。
            name, fallback = TASK_FALLBACK_PROFILE, True
        caps, model = [], None
        if name:
            doc = self._load_profile_doc(name)
            if doc is not None:
                raw = doc.get("caps")
                if raw is None:
                    log("WARN: profile %r 无 caps 字段（profile = 能力的有序声明列表）→ 无可注入能力",
                        name)
                elif not isinstance(raw, list):
                    log("WARN: profile %r 的 caps 非数组 %r，跳过", name, raw)
                else:
                    for item in raw:
                        if not isinstance(item, str) or not item.strip():
                            log("WARN: profile %r 的 caps 含非字符串/空元素 %r，跳过", name, item)
                            continue
                        c = item.strip()
                        if c in caps:
                            log("WARN: profile %r 的 caps 能力名重复 %r，去重保序", name, c)
                            continue
                        caps.append(c)
                m = doc.get("model")
                if isinstance(m, str) and m.strip():
                    model = m.strip()
                elif m is not None:
                    log("WARN: profile %r 的 model 字段非非空字符串，跳过 --model", name)
                if "contextCompaction" in doc:
                    pol, err = _cc_policy(doc.get("contextCompaction"))
                    if pol is None:
                        # 与「profile 缺失 = 告警降级不硬失败」同口径：会话照起，只是策略不生效
                        #（压缩行为落回 pi 内建的 settings 阈值）。
                        log("WARN: profile %r 的 contextCompaction 非法（%s）⇒ 不装配"
                            "（不注入 %s 与 -e；会话照起，压缩行为落回 pi 内建 settings 阈值）",
                            name, err, CONTEXT_COMPACTION_ENV)
                    else:
                        self.cc_policy = pol
        if not self.resident:
            if TASK_BASELINE_CAP in caps:
                # 已列在首位 = 声明与硬规则一致（如 `executor` profile 自身），静默去重；
                # 列在非首位 = 作者意图与「基线恒首」不一致（装配器会把它提到首位），值得告警。
                if caps[0] != TASK_BASELINE_CAP:
                    log("WARN: profile %r 的 caps 把 %r 列在非首位——任务形态由装配器恒前置该能力，"
                        "已提到首位并去重", name, TASK_BASELINE_CAP)
                caps = [c for c in caps if c != TASK_BASELINE_CAP]
            caps = [TASK_BASELINE_CAP] + caps
        if fallback:
            # 回落一条日志（事后可从 run/logs/* 归因「这个任务的 --model 从哪来」）；拿不到 model
            # 时升为 WARN（fail-soft 分支：不注入 --model、不硬失败）。
            if model:
                log("任务形态未设 DISPATCH_PROFILE → 回落 %r profile（缺省模型角色档）："
                    "model=%s，caps=%s", name, model, ",".join(caps))
            else:
                log("WARN: 任务形态未设 DISPATCH_PROFILE → 回落 %r profile，但解析不到可用 model"
                    "（清单缺失/损坏/无 model 字段/类型非法，成因见上方告警）⇒ 不注入 --model，"
                    "落回 settings 默认（fail-soft：本路径影响所有任务 spawn，硬失败会自锁）", name)
        return caps, model, name

    def _yaml_module(self):
        """惰性导入 `yaml`（cap.yml 解析；本脚本唯一的第三方依赖，四机实测可用且仓内已依赖）。
        不可导入 → 每进程 WARN 一次并返回 None：调用方按「cap.yml 不可解析」处置（跳过该能力），
        无 cap.yml 的纯正文能力照常注入 ⇒ 依赖缺失不拖垮会话。"""
        if hasattr(self, "_yaml_mod"):
            return self._yaml_mod
        try:
            import yaml
            self._yaml_mod = yaml
        except Exception as e:                # ImportError 及任何导入期异常
            self._yaml_mod = None
            log("WARN: pyyaml 不可导入 %r ⇒ 在场的能力声明 cap.yml 一律不可解析（对应能力被跳过；"
                "无 cap.yml 的纯正文能力照常注入）", e)
        return self._yaml_mod

    def _load_cap_yml(self, cdir, name):
        """读能力声明 `bots/caps/<名>/cap.yml` → (声明 dict, 是否跳过该能力)。
        - 文件不存在 ⇒ ({}, False)：**纯正文能力**（只注入 prompt.md、无捆绑声明）+ WARN
          （规范形态是 cap.yml 与 prompt.md 两文件在场）；
        - 不可读 / YAML 解析失败 / 顶层非 mapping / yaml 不可用 ⇒ (None, True)：**跳过该能力（含正文）**
          ——声明面不可信时注入半份资产更危险（工具面与捆绑都无法判定）；
        - 非法键（`caps`/`model`/未知键）⇒ WARN 忽略该键（能力不得引用能力；model 只住 profile）。"""
        yf = os.path.join(cdir, "cap.yml")
        if not os.path.isfile(yf):
            log("WARN: 能力 %r 无 cap.yml（%s）→ 按纯正文能力处理（无捆绑声明）", name, yf)
            return {}, False
        yaml = self._yaml_module()
        if yaml is None:
            return None, True
        try:
            with open(yf, encoding="utf-8") as f:
                doc = yaml.safe_load(f)
        except Exception as e:               # OSError / yaml.YAMLError 及解析期任何异常
            log("WARN: 能力 %r cap.yml 不可读/解析失败 %r → 跳过该能力（含正文注入）", name, e)
            return None, True
        if doc is None:
            doc = {}                         # 空文件 = 空声明（合法）
        if not isinstance(doc, dict):
            log("WARN: 能力 %r cap.yml 顶层非 mapping（%s）→ 跳过该能力", name, type(doc).__name__)
            return None, True
        for k in list(doc.keys()):
            if k not in CAP_ALLOWED_FIELDS:
                log("WARN: 能力 %r cap.yml 含非法键 %r（合法键 = %s；能力不得引用能力、model 只住 "
                    "profile）→ 忽略该键", name, k, "/".join(sorted(CAP_ALLOWED_FIELDS)))
                doc.pop(k)
        return doc, False

    @staticmethod
    def _name_list(v, field, cap):
        """cap.yml 名单字段清洗（`skills`/`extensions`/`knowledge` 共用）：非数组 → WARN + 空；
        非字符串/空白元素 → WARN 跳过；`skills`/`extensions` 的名另拒路径分隔与前导点
        （防走出共享库一级）；`knowledge` 是 **lore 仓根下的名**（`library/<域>` ∨ `desk/<岗位>` ∨
        `archive`，首段即层标识）∨ 工作区路径（legacy 档），故允许 `/`（`..` 段由 kb_index 拒）。"""
        if v is None:
            return []
        if not isinstance(v, list):
            log("WARN: 能力 %r cap.yml 的 %s 字段非数组 %r，跳过", cap, field, v)
            return []
        out = []
        for item in v:
            if field == "knowledge" and isinstance(item, dict):
                log("WARN: 能力 %r cap.yml 的 knowledge 项为对象形态 %r：域级用途字段已退休，"
                    "声明只收路径字符串（如 assistant/docs）→ 跳过该项", cap, item)
                continue
            if not isinstance(item, str) or not item.strip():
                log("WARN: 能力 %r cap.yml 的 %s 含非字符串/空元素 %r，跳过", cap, field, item)
                continue
            n = item.strip()
            if field != "knowledge" and ("/" in n or "\\" in n or n.startswith(".")):
                log("WARN: 能力 %r cap.yml 的 %s 名 %r 非法（含 / 或 \\ 或以 . 开头），跳过",
                    cap, field, n)
                continue
            out.append(n)
        return out

    def _skill_bundle_argv(self, names, cap, stats):
        """cap.yml 的 `skills` → 共享库 `bots/skills/<名>/` **一级解析（不回落全局**：全局层本来就
        必装，回落无意义），按声明序各一个 `--skill <绝对路径>`（与全局 skills 叠加，pi 原生累加语义）；
        目录缺失 → WARN 跳过该项（找不到 = 告警跳过，不硬失败）。"""
        argv = []
        for item in self._name_list(names, "skills", cap):
            sd = os.path.join(self.root, SKILLS_REL, item)
            if not os.path.isdir(sd):
                log("WARN: 能力 %r 捆绑的 skill %r 不存在（%s），跳过（只解析 bots/skills/ 一级、"
                    "不回落全局）", cap, item, sd)
                continue
            if not os.path.isfile(os.path.join(sd, "SKILL.md")):
                log("WARN: 能力 %r 捆绑的 skill %r 缺 SKILL.md（%s），仍按目录注入 --skill"
                    "（pi 侧自行忽略）", cap, item, sd)
            argv += ["--skill", sd]
            stats["skills"] += 1
        return argv

    def _ext_bundle_argv(self, names, cap, stats):
        """cap.yml 的 `extensions` → 共享库 `bots/extensions/<名>/`（一个名字 = 一个扩展单元），
        按声明序注入；单元解析见 `_ext_unit_argv`；目录缺失/无可注入 .ts → WARN 跳过该项。"""
        argv = []
        for item in self._name_list(names, "extensions", cap):
            ed = os.path.join(self.root, EXTS_REL, item)
            if not os.path.isdir(ed):
                log("WARN: 能力 %r 捆绑的扩展 %r 不存在（%s），跳过", cap, item, ed)
                continue
            unit, n = self._ext_unit_argv(ed, item)
            if not n:
                log("WARN: 能力 %r 捆绑的扩展 %r 无可注入 .ts（%s），跳过", cap, item, ed)
            argv += unit
            stats["exts"] += n
        return argv

    @staticmethod
    def _ext_unit_argv(edir, name):
        """共享库扩展单元 `bots/extensions/<名>/` → `-e`：含 `index.ts` → 恰一个 `-e <dir>/index.ts`；
        否则直属每个 `.ts`（按名排序保确定性）各一个 `-e`；非 .ts 文件 → WARN 跳过（dot 开头静默跳过、
        子目录不递归）。依赖一律 `.ts`（jiti 刷不掉 .mjs ESM 缓存）。返回 (argv, 计数)。"""
        idx = os.path.join(edir, "index.ts")
        if os.path.isfile(idx):
            return ["-e", idx], 1
        argv = []
        try:
            entries = sorted(os.listdir(edir))
        except OSError:
            return [], 0
        for entry in entries:
            if entry.startswith("."):
                continue
            ep = os.path.join(edir, entry)
            if not os.path.isfile(ep):
                continue                     # 子目录不递归（单元形态 = index.ts ∨ 直属 .ts）
            if entry.endswith(".ts"):
                argv += ["-e", ep]
            else:
                log("WARN: 扩展 %r 内非 .ts 文件，跳过: %s", name, ep)
        return argv, len(argv) // 2

    def _cap_argv(self, name):
        """单个原子能力的注入（`caps` 列表序逐个调用）：
          - `prompt.md` → 一个 `--append-system-prompt`（**装配器不碰正文一个字节**：无 frontmatter
            剥离、无改写；多能力 = 多次追加，pi 原生追加语义；超 120KB 告警 = 内核单参数上限
            MAX_ARG_STRLEN，能力化后该约束退化为「单能力正文上界」）；正文缺失 = **bundle 能力**
            （只有捆绑声明，合法形态、非缺陷 ⇒ info 日志不 WARN）；
          - `cap.yml` 的 skills/extensions → 共享库一级解析（`_skill_bundle_argv`/`_ext_bundle_argv`）。
        返回 (argv, 统计 dict, 声明 dict)；声明为 None = 该能力被跳过（目录缺失/cap.yml 不可信）。"""
        cdir = os.path.join(self.root, CAPS_REL, name)
        if not os.path.isdir(cdir):
            log("WARN: 能力 %r 不存在（%s），跳过该能力（其余照常注入，不拖垮会话）", name, cdir)
            return [], None, None
        decl, skip = self._load_cap_yml(cdir, name)
        if skip:
            return [], None, None
        argv = []
        stats = {"prompt_chars": 0, "skills": 0, "exts": 0}
        pf = os.path.join(cdir, "prompt.md")
        if os.path.isfile(pf):
            try:
                with open(pf, encoding="utf-8") as f:
                    text = f.read()
                if len(text.encode("utf-8")) > 120 * 1024:
                    log("WARN: 能力 %r prompt.md 超 120KB，可能触内核单参数上限（MAX_ARG_STRLEN），"
                        "pi 可能启动失败", name)
                argv += ["--append-system-prompt", text]
                stats["prompt_chars"] = len(text)
            except OSError as e:
                log("WARN: 能力 %r prompt.md 不可读 %r，跳过正文注入", name, e)
        else:
            log("能力 %r 无 prompt.md = bundle 能力（只捆绑资产、无注入正文）", name)
        argv += self._skill_bundle_argv(decl.get("skills"), name, stats)
        argv += self._ext_bundle_argv(decl.get("extensions"), name, stats)
        return argv, stats, decl

    def _compaction_argv(self):
        """profile `contextCompaction` 的执行体注入（**task 与 resident 两形态同等**：策略住 profile，
        与形态无关）；注入位在能力（caps）之后（`-e` 是累加语义，顺序无副作用，但日志能看出来源）。

        两个前置缺一就不注入（与「profile 缺失 = 告警降级不硬失败」同口径）：
          ① `_resolve_caps` 解到合法策略（字段缺失/非法 ⇒ `self.cc_policy` 为 None）；
          ② 执行体文件在场（缺失 ⇒ WARN + 不注入，照 `PROBE_EXT_REL` 口径；pi 对 `-e` 的加载错误
             是致命的，绝不指向不存在的路径）。
        注入时置 `cc_injected` ⇒ `spawn_pi` 才传 env `AGENTD_CONTEXT_COMPACTION`
        （env 与 `-e` 同进同退：只传 env 无执行体 = 无人消费，只注执行体无 env = 扩展静默不启用）。
        返回 `[]` ∨ `["-e", <绝对路径>]`。"""
        pol = self.cc_policy
        if not pol:
            return []
        p = os.path.join(self.root, CONTEXT_COMPACTION_EXT_REL)
        if not os.path.isfile(p):
            log("WARN: contextCompaction 执行体缺失，跳过注入（策略已声明但装配不上；"
                "会话照起，压缩行为落回 pi 内建 settings 阈值）: %s", p)
            return []
        self.cc_injected = True
        log("contextCompaction 装配：trigger=%s ratio=%s enabled=%s ext=%s",
            pol.get("triggerTokens", "（未设）"), pol.get("triggerRatio", "（未设）"),
            pol.get("enabled", True), p)
        return ["-e", p]

    def _profile_argv(self, xt_baseline=()):
        """人格装配注入单点（任务/常驻两形态共用；机制口径 = `assistant/DISPATCH.md` §3）：
        `DISPATCH_PROFILE=<profile 名>`（**单值**）→ 薄清单 `bots/profiles/<名>.json` 的 `caps`
        → 逐能力读 `bots/caps/<能力>/{cap.yml,prompt.md}`（`_resolve_caps`/`_cap_argv`）：
          - 注入序 = `caps` 列表序；**任务形态恒前置 `executor` 能力**（装配器硬规则），resident 不前置；
          - `knowledge`（lore 仓根下的名，首段 = 层标识）跨能力**并集去重保序** → 一个
            `--append-system-prompt` 的「知识清单」块，追加在**全部能力正文之后**（名解析与三档渲染交
            `bots/kb_index.py`，判定单点；工作区路径声明 = legacy 档，渲染形态不变）；
          - `model` **只来自 profile** → 至多一个 `--model`，**并由其 provider 段派生至多一个
            `--provider`**（`<provider>/<id>` 形式才派生；缺失/无斜杠/畸形 = 不注入 `--provider`、
            落回 settings 默认，判定与 fail-soft 口径见 `provider_of_model`）。注入序固定 =
            **`--provider` 在 `--model` 之前**（两形态一致）；**任务形态未设 profile 时回落 `executor`
            profile**（⇒ 任务缺省即带它的 model；回落面解析不到 model = WARN + 不注入，绝不硬失败）；
          - **工具面并集语义**：`-xt` = ∪(声明者 excludeTools) ∪ 形态基线；`-t` = ∪(声明者 tools)；
            未声明者不参与合并（无声明者 = 不发 `-t`）。pi 的 excludeTools 在 tools 白名单**之后**生效
            ⇒ 最终工具面 = （∪tools）−（∪excludeTools ∪ 基线）；被排除掉的白名单项 = WARN 不阻断。
            出参中 `-xt` 至多一个（排除集非空时**恰一个**）、`-t` 至多一个——pi 两者都是赋值语义
            （重复出现后者覆盖前者），拆开拼会解除基线屏蔽；`-e`/`--skill` 才是累加。
        xt_baseline = 形态基线排除集（任务形态 = ("ask_user",)，resident 形态传空）。
        未设 env → 任务形态回落 `executor` profile（注入面 = 基线能力，另取其 model）、resident argv
        逐字不变（零回归出口）；profile/能力缺失或损坏 → 告警跳过（降级分支全集见各
        `_load_*`/`_cap_argv` docstring），全无可注入 = 裸启动，
        绝不硬失败。责任口径（白名单模式需自行列全，含调度协议工具）见 DISPATCH.md §3。"""
        xt = list(dict.fromkeys(xt_baseline))
        caps, model_used, pname = self._resolve_caps()
        if not caps:
            return ["-xt", ",".join(xt)] if xt else []
        argv = []
        kb_entries = []      # knowledge 名跨能力累积（并集；去重保序、名解析与分档渲染交 kb_index）
        t_list, xt_decl = [], []
        for name in caps:
            c_argv, stats, decl = self._cap_argv(name)
            if decl is None:
                continue                     # 该能力被跳过（目录缺失/cap.yml 不可信），其余照常
            argv += c_argv
            kb_entries += self._name_list(decl.get("knowledge"), "knowledge", name)
            t_list += self._tool_list(decl, "tools", name)
            xt_decl += self._tool_list(decl, "excludeTools", name)
            log("能力 %r 注入：prompt=%d 字符，skills=%d，extensions=%d",
                name, stats["prompt_chars"], stats["skills"], stats["exts"])
        # 工具面并集（去重保序）；排除集 = 声明者并集 ∪ 形态基线（安全面单调收紧、与 caps 序无关）
        t_list = list(dict.fromkeys(t_list))
        xt = list(dict.fromkeys(xt + xt_decl))
        killed = [t for t in t_list if t in set(xt)]
        if killed:
            log("WARN: 工具面白名单项 %s 被排除集命中（∪excludeTools ∪ 形态基线）⇒ 最终不生效"
                "（pi 的 excludeTools 在 tools 白名单之后生效）；可见即可，不阻断", ",".join(killed))
        # knowledge 清单：追加在全部能力正文之后（--append-system-prompt 顺序即拼接顺序）
        kb_argv, kb_chars = self._knowledge_argv(kb_entries)
        argv += kb_argv
        if kb_argv:
            log("knowledge 清单注入：%d 项声明，%d 字符", len(kb_entries), kb_chars)
        if model_used is not None:
            # provider 段派生（声明源 = 同一个 profile `model` 字段，不另设字段）：畸形/无斜杠
            # ⇒ provider 为 None ⇒ 只注入 --model（fail-soft，绝不 die；判定见 provider_of_model）
            prov = provider_of_model(model_used)
            if prov is not None:
                argv += ["--provider", prov]
            argv += ["--model", model_used]
        if t_list:
            # 白名单路径：排除集仍以单个 -xt 前置（赋值语义，拆开拼会解除基线屏蔽），再拼 -t
            argv += (["-xt", ",".join(xt)] if xt else []) + ["-t", ",".join(t_list)]
        elif xt:
            argv += ["-xt", ",".join(xt)]
        log("profile %s 展开完成：能力=%s，model=%s，provider=%s，工具面=%s",
            repr(pname) if pname else "（未设）", ",".join(caps), model_used or "（缺省）",
            provider_of_model(model_used) or "（缺省）",
            ("白名单 %d 项" % len(t_list)) if t_list
            else (("黑名单 %d 项" % len(xt)) if xt else "（缺省）"))
        return argv

    @staticmethod
    def _tool_list(doc, field, name):
        """cap.yml 工具面字段解析：取字符串数组，非字符串/空白元素 WARN 跳过；含内嵌逗号的元素
        （如 "read,bash"）WARN 拒绝——pi 按逗号拆工具名，原样拼入会撑大白名单/黑名单；
        非数组/缺省 → 空列表（= 未声明，不参与并集合并，调用方不拼参数）。"""
        v = doc.get(field)
        if v is None:
            return []
        if not isinstance(v, list):
            log("WARN: 能力 %r cap.yml 的 %s 字段非数组 %r，跳过", name, field, v)
            return []
        out = []
        for item in v:
            if isinstance(item, str) and item.strip():
                t = item.strip()
                if "," in t:
                    log("WARN: 能力 %r cap.yml 的 %s 元素 %r 含内嵌逗号，拒绝"
                        "（pi 按逗号拆工具名，防白名单/黑名单被撑大）", name, field, item)
                    continue
                out.append(t)
            else:
                log("WARN: 能力 %r cap.yml 的 %s 含非字符串/空元素 %r，跳过", name, field, item)
        return out

    def spawn_pi(self):
        self.clear_ready_marks()       # 陈旧标记不得骗开本代就绪门（先于 pi 启动）
        argv = self.build_argv()
        env = dict(os.environ, SESSIOND_SESSION_FILE=self.session_file)
        if self.cc_injected:
            # 归一化策略进 env（紧凑 JSON）；消费方 = bots/extensions/context-compaction/index.ts。
            env[CONTEXT_COMPACTION_ENV] = json.dumps(
                self.cc_policy, ensure_ascii=False, separators=(",", ":"))
        else:
            # 未注入执行体 ⇒ 显式洗掉可能从宿主继承的同名 env（否则一个陈旧值会让别处装载的
            # 执行体误启用）：「无策略 = env 不在场」是硬语义，不靠调用方环境干净。
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
        assistant/DISPATCH.md「完成判定」与 agentd/report.py 的 has_report。
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
        心跳任务**适用**此闸（它带 DISPATCH_HEARTBEAT=1 而不带 AGENTD_RESIDENT=1 ⇒ 走收敛分支；
        登记方裁定为期望行为：心跳同样以 report.md 为完成要件，且 restartPolicy 非 auto 不会重启成风暴）。
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
        try:
            with open(self.stderr_log, "ab") as lf:
                while True:
                    d = self.pi.stderr.read(4096)
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
