#!/usr/bin/env python3
"""persona.py — 人格装配的**解析层**：profile/caps → 结构化注入面（单一实现，两个消费者）。

消费者（两者共用这一份解析 ⇒ 合并语义不存在第二份实现）：
  ① `pi-rpc-wrap.py`（**发射层**：把结构拼成 pi 的 argv / env）；
  ② `bots/extensions/profile-loader/index.ts`（**pi 侧注入适配器**：经本文件 CLI 取同一个结构，
     在会话内注入 —— 手工调用 `pi --profile <名>` 走这条，无需 wrap）。

两层模型（原子能力 CAP + profile 薄清单）与字段规范权威 = `bots/README.md`「人格资产」节；
机制口径 = `dispatch/DISPATCH.md` §3；装配面逐格全文 = `bots/docs/profile-assembly.md`。

CLI（扩展侧 `execFileSync` 调用；**stdout = 一个 JSON 对象**，告警/摘要进 stderr）：

    python3 persona.py resolve --root <ws> --form task|resident [--profile <名>]

`--profile` 缺省 = 读 env `DISPATCH_PROFILE`（两个输入口等价，flag 优先）。
**退出码恒 0**（fail-soft 是硬要求）：解析面任何缺失/损坏/非法一律降级为 `warnings[]` 项，
绝不 die——硬失败会自锁（连「修这条路径」的修复会话都起不来）。只有用法错误 ∨ 内部 bug
（不该发生）才非 0。

输出 schema（`schemaVersion` 递增即契约变更；跨仓契约的两侧 pin = `pi-wrap/test_persona.py`
与 `bots/extensions/profile-loader/`）：

    schemaVersion   int    本 schema 版本
    form            str    "task" ∨ "resident"
    profile         str?   实际使用的 profile 名（回落时 = 回落面）；未设/非法 = null
    fallback        bool   是否走了任务形态的缺省回落
    caps            [str]  展开后的有序能力名（已前置基线 / 去重保序）
    appendParts     [str]  **按注入序**的追加正文（逐能力 prompt.md 全文 + 末位知识清单块）
    appendJoiner    str    多段追加正文的拼接符（= pi 自身的语义：它对多段追加正文正是
                           `join("\n\n")`，dist/core/agent-session.js；单点在此，
                           注入层不硬编码）
    promptStats     [{cap,chars}]  逐能力正文字符数（自证/观测面）
    skillPaths      [str]  能力捆绑 skill 的绝对路径（按声明序，已核在场）
    extensionPaths  [str]  能力捆绑扩展的绝对 .ts 路径（**注入层兑现不了**：pi 无运行期装载
                           扩展的 API ⇒ 有值即一条 warning，处置 = 退役该字段）
    tools           [str]  ∪(声明者 tools)；空 = 未声明（不发白名单）
    excludeTools    [str]  ∪(声明者 excludeTools) ∪ 形态基线
    model           str?   profile 的 `model`（能力层无此字段）
    provider        str?   由 model 的 provider 段派生（`<provider>/<id>` 才派生）
    contextCompaction obj? 归一化策略（字段缺失/非法 = null）
    contextCompactionExt str? 执行体绝对路径（策略合法 ∧ 文件在场才给；两者同进同退）
    stats           obj    {promptChars,promptCount,skills,exts,knowledgeChars}
    warnings        [str]  降级项全文（与 stderr 告警同源，供会话内 `/persona` 自证）
"""
import argparse
import json
import os
import sys

SCHEMA_VERSION = 1

FORM_TASK = "task"
FORM_RESIDENT = "resident"

# 追加正文的拼接符 = pi 自身的语义（多个 --append-system-prompt 按序 join("\n\n")，
# 整段再以 "\n\n" 接在基础提示之后：dist/core/agent-session.js 与 core/system-prompt.js）。
# 单点在此 ⇒ 两个消费者（argv 发射 / 会话内注入）产出逐字相同的系统提示。
APPEND_JOINER = "\n\n"

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
                                            # ⇒ 注入面与回落前逐字一致）。fail-soft 见 resolve_caps
CAP_ALLOWED_FIELDS = frozenset({"summary", "skills", "extensions", "knowledge",
                                "tools", "excludeTools"})   # cap.yml 合法键闭合集（禁 caps/model）
PROFILE_BANNED_FIELDS = ("skills", "extensions", "knowledge", "tools",
                         "excludeTools")    # profile 只列 caps，不给逃生口 ⇒ 直挂即 WARN 忽略。
                                            # `contextCompaction` 属运行环境/策略类字段（与 model
                                            # 同类），不在本名单里、也不构成资产直挂的逃生口
CONTEXT_COMPACTION_ENV = "AGENTD_CONTEXT_COMPACTION"   # 归一化策略的注入通道（紧凑 JSON），消费方 =
                                            # bots/extensions/context-compaction/index.ts
CONTEXT_COMPACTION_EXT_REL = "bots/extensions/context-compaction/index.ts"   # 执行体（扩展单元）；
                                            # 文件缺失 → WARN + 不装配（照探针扩展口径）
CC_FIELDS = ("enabled", "triggerTokens", "triggerRatio",
             "customInstructions")         # `contextCompaction` 键白名单（白名单外一律非法：拼错的
                                            # 键被静默忽略会改变语义）。**不含** keepRecentTokens/
                                            # reserveTokens —— 不可达面（切点在 pi 的
                                            # prepareCompaction 内算定），见 cc_policy
CC_INSTRUCTIONS_MAX = 2000                 # customInstructions 字符数上界（与 policy.ts 同口径）

# 形态基线排除集（安全面单调收紧、与 caps 序无关）：任务形态恒屏蔽 ask_user（反问走调度协议），
# resident 形态为空（主端 ask_user 链路保留）。
FORM_XT_BASELINE = {FORM_TASK: ("ask_user",), FORM_RESIDENT: ()}


def cc_policy(raw):
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
    """profile 的 `model` 值 → 其 provider 段 ∨ None（= 不派生）。

    声明源只有既有的 profile `model` 字段（**不新增 profile 字段、不硬编码任何 provider 名**）：
    形如 `<provider>/<id>`（含且只含一个 `/`、且 provider 段非空）时返回 provider 段；
    其余一律返回 None ⇒ 调用方不指定 provider，pi 落回 `settings.json` 的默认 provider。

    **fail-soft 是硬要求**（与 `model` 的降级口径同源）：本函数处在所有任务 spawn 的
    公共路径上，任何畸形值都只降级、**绝不 die**（硬失败会自锁——连「修这条路径」的修复任务
    都起不来）。畸形判定逐条：
      - None / 非字符串 / 空白串 → None（无 model 声明，provider 无从派生）；
      - 不含 `/`（如 `qwen3.8-max`）→ None（裸模型 id：pi 自己按 settings 默认 provider 解析）；
      - `/` 前段为空（如 `/planner`）→ None（provider 段空 = 注入空值反而覆盖掉默认）；
      - 含 ≥2 个 `/`（如 `a/b/c`）→ None（两段式不成立，**不猜切分点**：`model` 仍原样交出去，
        由 pi 自行解析，装配器不做二次判断）。
    返回的 provider 段已 strip（与 `model` 取值处的 strip 同口径）。

    **写侧约束（只记边界、不加代码校验）**：profile 的 `model` 前段必须是 pi 已配置的 provider 名。
    pi 的 `resolveCliModel`（`dist/core/model-resolver.js`）对显式 `--provider <未知>` 是**硬失败**
    （返回 `Unknown provider "<x>"` 错误），而单独一个 `--model <未知>/<id>` 仍可能经 model id 字面
    精确匹配解析成功 ⇒ 指定 provider 会**窄化**该容错面。现网无实例：各 profile 的 model 前段均为
    已配置 provider。**不做写侧校验**：profile 是跳机同步面，校验会在同步时刻差上误拒，与本函数
    「只降级不 die」的口径相左。"""
    if not isinstance(model, str):
        return None
    m = model.strip()
    if not m or "/" not in m:
        return None
    if m.count("/") != 1:
        return None                        # 畸形：多段，不猜切分点
    prov = m.split("/", 1)[0].strip()
    return prov or None                    # `/id` 形态：provider 段空 ⇒ 不派生


def _stderr_emit(fmt, *args):
    sys.stderr.write(("[persona] " + fmt + "\n") % args)
    sys.stderr.flush()


class Resolver:
    """一次人格装配的解析（无副作用：不写盘、不起进程；只读资产 + 记告警）。

    `emit(fmt, *args)` = 告警/摘要 sink（缺省写 stderr）。凡 fmt 以 `WARN` 开头者同时进
    `self.warnings`（⇒ JSON 的 `warnings[]` 与 stderr 告警同源，不两处维护）。
    """

    def __init__(self, root, form=FORM_TASK, emit=None):
        if form not in FORM_XT_BASELINE:
            raise ValueError("form 必须是 %s ∨ %s（实得 %r）"
                             % (FORM_TASK, FORM_RESIDENT, form))
        self.root = root or ""
        self.form = form
        self.resident = (form == FORM_RESIDENT)
        self._emit = emit or _stderr_emit
        self.warnings = []

    # ---------- sink ----------

    def emit(self, fmt, *args):
        text = fmt % args if args else fmt
        if text.startswith("WARN"):
            self.warnings.append(text)
        self._emit(fmt, *args)

    # ---------- 解析入口 ----------

    def resolve(self, profile_raw=None):
        """profile 名（∨ None = 读 env `DISPATCH_PROFILE`）→ 结构化注入面 dict（schema 见模块头）。

        顺序即日志序（与 argv 发射形态逐字同序）：caps 展开 → 逐能力正文/捆绑 → 工具面并集 →
        knowledge 清单 → 展开摘要 → contextCompaction 装配。
        """
        if profile_raw is None:
            profile_raw = os.environ.get("DISPATCH_PROFILE", "")
        out = {
            "schemaVersion": SCHEMA_VERSION,
            "form": self.form,
            "profile": None,
            "fallback": False,
            "caps": [],
            "appendParts": [],
            "appendJoiner": APPEND_JOINER,
            "promptStats": [],
            "skillPaths": [],
            "extensionPaths": [],
            "tools": [],
            "excludeTools": list(FORM_XT_BASELINE[self.form]),
            "model": None,
            "provider": None,
            "contextCompaction": None,
            "contextCompactionExt": None,
            "stats": {"promptChars": 0, "promptCount": 0, "skills": 0,
                      "exts": 0, "knowledgeChars": 0},
            "warnings": self.warnings,
        }
        caps, model, pname, cc, fallback = self.resolve_caps(profile_raw)
        out["profile"], out["fallback"] = pname, fallback
        out["caps"] = caps
        out["model"] = model
        out["provider"] = provider_of_model(model)
        if not caps:
            out["excludeTools"] = self._dedup(out["excludeTools"])
            return out

        kb_entries, t_list, xt_decl = [], [], []
        for name in caps:
            unit = self.cap_unit(name)
            if unit is None:
                continue                   # 该能力被跳过（目录缺失/cap.yml 不可信），其余照常
            if unit["text"] is not None:
                out["appendParts"].append(unit["text"])
                out["promptStats"].append({"cap": name, "chars": unit["chars"]})
                out["stats"]["promptChars"] += unit["chars"]
                out["stats"]["promptCount"] += 1
            out["skillPaths"] += unit["skills"]
            out["extensionPaths"] += unit["exts"]
            out["stats"]["skills"] += len(unit["skills"])
            out["stats"]["exts"] += len(unit["exts"])
            kb_entries += unit["knowledge"]
            t_list += unit["tools"]
            xt_decl += unit["excludeTools"]
            self.emit("能力 %r 注入：prompt=%d 字符，skills=%d，extensions=%d",
                      name, unit["chars"], len(unit["skills"]), len(unit["exts"]))

        # 工具面并集（去重保序）；排除集 = 声明者并集 ∪ 形态基线（安全面单调收紧、与 caps 序无关）
        out["tools"] = self._dedup(t_list)
        out["excludeTools"] = self._dedup(list(FORM_XT_BASELINE[self.form]) + xt_decl)
        killed = [t for t in out["tools"] if t in set(out["excludeTools"])]
        if killed:
            self.emit("WARN: 工具面白名单项 %s 被排除集命中（∪excludeTools ∪ 形态基线）⇒ 最终不生效"
                      "（pi 的 excludeTools 在 tools 白名单之后生效）；可见即可，不阻断",
                      ",".join(killed))

        # knowledge 清单：追加在全部能力正文之后（拼接序即注入序）
        block, kb_chars = self.knowledge_block(kb_entries)
        if block:
            out["appendParts"].append(block)
            out["stats"]["knowledgeChars"] = kb_chars
            self.emit("knowledge 清单注入：%d 项声明，%d 字符", len(kb_entries), kb_chars)

        self.emit("profile %s 展开完成：能力=%s，model=%s，provider=%s，工具面=%s",
                  repr(pname) if pname else "（未设）", ",".join(caps), model or "（缺省）",
                  out["provider"] or "（缺省）",
                  ("白名单 %d 项" % len(out["tools"])) if out["tools"]
                  else (("黑名单 %d 项" % len(out["excludeTools"]))
                        if out["excludeTools"] else "（缺省）"))

        # contextCompaction（profile 级运行环境字段；两形态同等，策略住 profile、与形态无关）
        if cc:
            ext = os.path.join(self.root, CONTEXT_COMPACTION_EXT_REL)
            if not os.path.isfile(ext):
                self.emit("WARN: contextCompaction 执行体缺失，跳过装配（策略已声明但装配不上；"
                          "会话照起，压缩行为落回 pi 内建 settings 阈值）: %s", ext)
            else:
                out["contextCompaction"] = cc
                out["contextCompactionExt"] = ext
                self.emit("contextCompaction 装配：trigger=%s ratio=%s enabled=%s ext=%s",
                          cc.get("triggerTokens", "（未设）"), cc.get("triggerRatio", "（未设）"),
                          cc.get("enabled", True), ext)
        out["warnings"] = list(self.warnings)
        return out

    # ---------- profile 薄清单 ----------

    def parse_profile_name(self, raw):
        """`DISPATCH_PROFILE` = **单值 profile 名**（链式组合已退役、不留兼容：能力组合住 profile 的
        `caps` 列表，注入序 = 列表序）。文法白名单：拒 `/`、`\\` 与前导 `.`（可能走出 profiles/；
        主闸 = 登记侧 core.ts 白名单）；**含逗号 = 已退役的链式写法**（如旧 `executor,review`）⇒
        WARN 点名成因后按未设处置（诊断价值：现网存量 spec.command 里可能还有旧链）。
        返回名字 ∨ None。"""
        n = (raw or "").strip()
        if not n:
            return None
        if "," in n:
            self.emit("WARN: DISPATCH_PROFILE %r 含逗号 = 已退役的链式写法（现为单值 profile 名，"
                      "能力组合住 profile 清单的 caps 列表）→ 按未设处置", n)
            return None
        if "/" in n or "\\" in n or n.startswith("."):
            self.emit("WARN: DISPATCH_PROFILE 名字非法 %r（含 / 或 \\ 或以 . 开头），跳过", n)
            return None
        return n

    def load_profile_doc(self, name):
        """读 profile 薄清单 `bots/profiles/<名>.json` → dict ∨ None。
        缺失 / 不可读 / JSON 损坏 / 顶层非对象 = WARN + None（任务形态仍前置基线能力，
        resident = 裸启动）。直挂捆绑字段（profile 只列 caps，不给逃生口）= WARN 忽略该字段。"""
        pf = os.path.join(self.root, PROFILES_REL, name + ".json")
        if not os.path.isfile(pf):
            self.emit("WARN: profile %r 不存在（%s），跳过（任务形态仍会前置基线能力；"
                      "resident 形态 = 裸启动）", name, pf)
            return None
        try:
            with open(pf, encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, ValueError) as e:
            self.emit("WARN: profile %r 清单不可读/损坏 %r，跳过", name, e)
            return None
        if not isinstance(doc, dict):
            self.emit("WARN: profile %r 清单顶层非对象（%s），跳过", name, type(doc).__name__)
            return None
        for banned in PROFILE_BANNED_FIELDS:
            if banned in doc:
                self.emit("WARN: profile %r 清单直挂 %r 字段（profile 只列 caps；捆绑资产住能力 cap.yml，"
                          "要额外装就建一个 bundle 能力）→ 忽略该字段", name, banned)
        return doc

    def resolve_caps(self, profile_raw):
        """profile 名 → (能力名有序列表, model ∨ None, profile 名 ∨ None, cc 策略 ∨ None, 是否回落)。
        注入序 = profile 的 `caps` 列表序（平铺，能力不引用能力）；**任务形态恒前置 `executor` 能力**
        （装配器硬规则承担，防漏列；resident 形态不前置）。`model` 只住 profile（能力层无此字段：
        复用单元不该决定运行环境）。降级：caps 缺失/非数组/元素非法 → WARN 逐项跳过。
        **任务形态未设 profile（∨ 名字非法 = 按未设处置）⇒ 回落 `TASK_FALLBACK_PROFILE`**：回落复用
        **同一条**解析路径（清单读取 / caps 校验 / model 取值 / 基线前置去重全部照旧，不另写平行分支），
        故回落后的 caps 与「只前置基线能力」逐字一致（回落 profile 的 caps 就是基线能力本身），
        差别只是拿到它的 `model`。resident 形态**不回落**（注入面逐字不变）；显式设了合法 profile 名
        （哪怕清单缺失）**也不回落**（名字合法 = 作者有指定意图，缺失属降级而非未设）。"""
        name = self.parse_profile_name(profile_raw)
        fallback = False
        if name is None and not self.resident:
            # 任务形态的缺省模型角色档：`model` 只住 profile，未设 profile 就拿不到 ⇒ 回落
            # `TASK_FALLBACK_PROFILE`。**fail-soft 是硬要求**：这条路径影响所有任务 spawn，回落面
            # 缺失/不可解析/无 model/类型非法一律 WARN + 不给 model（落回 settings 默认），
            # **绝不 die**——硬失败会自锁（连「修这条路径」的修复任务都起不来）。降级全靠下面
            # 既有的 `load_profile_doc` / model 类型分支承担，本处不重复实现。
            name, fallback = TASK_FALLBACK_PROFILE, True
        caps, model, cc = [], None, None
        if name:
            doc = self.load_profile_doc(name)
            if doc is not None:
                raw = doc.get("caps")
                if raw is None:
                    self.emit("WARN: profile %r 无 caps 字段（profile = 能力的有序声明列表）→ 无可注入能力",
                              name)
                elif not isinstance(raw, list):
                    self.emit("WARN: profile %r 的 caps 非数组 %r，跳过", name, raw)
                else:
                    for item in raw:
                        if not isinstance(item, str) or not item.strip():
                            self.emit("WARN: profile %r 的 caps 含非字符串/空元素 %r，跳过", name, item)
                            continue
                        c = item.strip()
                        if c in caps:
                            self.emit("WARN: profile %r 的 caps 能力名重复 %r，去重保序", name, c)
                            continue
                        caps.append(c)
                m = doc.get("model")
                if isinstance(m, str) and m.strip():
                    model = m.strip()
                elif m is not None:
                    self.emit("WARN: profile %r 的 model 字段非非空字符串，跳过 model 注入", name)
                if "contextCompaction" in doc:
                    pol, err = cc_policy(doc.get("contextCompaction"))
                    if pol is None:
                        # 与「profile 缺失 = 告警降级不硬失败」同口径：会话照起，只是策略不生效
                        #（压缩行为落回 pi 内建的 settings 阈值）。
                        self.emit("WARN: profile %r 的 contextCompaction 非法（%s）⇒ 不装配"
                                  "（不注入 %s 与 -e；会话照起，压缩行为落回 pi 内建 settings 阈值）",
                                  name, err, CONTEXT_COMPACTION_ENV)
                    else:
                        cc = pol
        if not self.resident:
            if TASK_BASELINE_CAP in caps:
                # 已列在首位 = 声明与硬规则一致（如 `executor` profile 自身），静默去重；
                # 列在非首位 = 作者意图与「基线恒首」不一致（装配器会把它提到首位），值得告警。
                if caps[0] != TASK_BASELINE_CAP:
                    self.emit("WARN: profile %r 的 caps 把 %r 列在非首位——任务形态由装配器恒前置该能力，"
                              "已提到首位并去重", name, TASK_BASELINE_CAP)
                caps = [c for c in caps if c != TASK_BASELINE_CAP]
            caps = [TASK_BASELINE_CAP] + caps
        if fallback:
            # 回落一条日志（事后可从 run/logs/* 归因「这个任务的 model 从哪来」）；拿不到 model
            # 时升为 WARN（fail-soft 分支：不注入 model、不硬失败）。
            if model:
                self.emit("任务形态未设 DISPATCH_PROFILE → 回落 %r profile（缺省模型角色档）："
                          "model=%s，caps=%s", name, model, ",".join(caps))
            else:
                self.emit("WARN: 任务形态未设 DISPATCH_PROFILE → 回落 %r profile，但解析不到可用 model"
                          "（清单缺失/损坏/无 model 字段/类型非法，成因见上方告警）⇒ 不切模型，"
                          "落回 settings 默认（fail-soft：本路径影响所有任务 spawn，硬失败会自锁）", name)
        return caps, model, name, cc, fallback

    # ---------- 能力单元 ----------

    def _yaml_module(self):
        """惰性导入 `yaml`（cap.yml 解析；本层唯一的第三方依赖，四机实测可用且仓内已依赖）。
        不可导入 → 每进程 WARN 一次并返回 None：调用方按「cap.yml 不可解析」处置（跳过该能力），
        无 cap.yml 的纯正文能力照常注入 ⇒ 依赖缺失不拖垮会话。"""
        if hasattr(self, "_yaml_mod"):
            return self._yaml_mod
        try:
            import yaml
            self._yaml_mod = yaml
        except Exception as e:                # ImportError 及任何导入期异常
            self._yaml_mod = None
            self.emit("WARN: pyyaml 不可导入 %r ⇒ 在场的能力声明 cap.yml 一律不可解析（对应能力被跳过；"
                      "无 cap.yml 的纯正文能力照常注入）", e)
        return self._yaml_mod

    def load_cap_yml(self, cdir, name):
        """读能力声明 `bots/caps/<名>/cap.yml` → (声明 dict, 是否跳过该能力)。
        - 文件不存在 ⇒ ({}, False)：**纯正文能力**（只注入 prompt.md、无捆绑声明）+ WARN
          （规范形态是 cap.yml 与 prompt.md 两文件在场）；
        - 不可读 / YAML 解析失败 / 顶层非 mapping / yaml 不可用 ⇒ (None, True)：**跳过该能力（含正文）**
          ——声明面不可信时注入半份资产更危险（工具面与捆绑都无法判定）；
        - 非法键（`caps`/`model`/未知键）⇒ WARN 忽略该键（能力不得引用能力；model 只住 profile）。"""
        yf = os.path.join(cdir, "cap.yml")
        if not os.path.isfile(yf):
            self.emit("WARN: 能力 %r 无 cap.yml（%s）→ 按纯正文能力处理（无捆绑声明）", name, yf)
            return {}, False
        yaml = self._yaml_module()
        if yaml is None:
            return None, True
        try:
            with open(yf, encoding="utf-8") as f:
                doc = yaml.safe_load(f)
        except Exception as e:               # OSError / yaml.YAMLError 及解析期任何异常
            self.emit("WARN: 能力 %r cap.yml 不可读/解析失败 %r → 跳过该能力（含正文注入）", name, e)
            return None, True
        if doc is None:
            doc = {}                         # 空文件 = 空声明（合法）
        if not isinstance(doc, dict):
            self.emit("WARN: 能力 %r cap.yml 顶层非 mapping（%s）→ 跳过该能力", name, type(doc).__name__)
            return None, True
        for k in list(doc.keys()):
            if k not in CAP_ALLOWED_FIELDS:
                self.emit("WARN: 能力 %r cap.yml 含非法键 %r（合法键 = %s；能力不得引用能力、model 只住 "
                          "profile）→ 忽略该键", name, k, "/".join(sorted(CAP_ALLOWED_FIELDS)))
                doc.pop(k)
        return doc, False

    def cap_unit(self, name):
        """单个原子能力 → 注入面 dict ∨ None（= 该能力被跳过：目录缺失 ∨ cap.yml 不可信）。

        dict 字段：`text`（prompt.md 全文 ∨ None = bundle 能力；**解析层不碰正文一个字节**：无
        frontmatter 剥离、无改写）、`chars`、`skills`/`exts`（绝对路径列表）、`knowledge`（名列表）、
        `tools`/`excludeTools`（清洗后的工具名列表）。"""
        cdir = os.path.join(self.root, CAPS_REL, name)
        if not os.path.isdir(cdir):
            self.emit("WARN: 能力 %r 不存在（%s），跳过该能力（其余照常注入，不拖垮会话）", name, cdir)
            return None
        decl, skip = self.load_cap_yml(cdir, name)
        if skip:
            return None
        unit = {"name": name, "text": None, "chars": 0, "skills": [], "exts": [],
                "knowledge": [], "tools": [], "excludeTools": []}
        pf = os.path.join(cdir, "prompt.md")
        if os.path.isfile(pf):
            try:
                with open(pf, encoding="utf-8") as f:
                    text = f.read()
                unit["text"] = text
                unit["chars"] = len(text)
            except OSError as e:
                self.emit("WARN: 能力 %r prompt.md 不可读 %r，跳过正文注入", name, e)
        else:
            self.emit("能力 %r 无 prompt.md = bundle 能力（只捆绑资产、无注入正文）", name)
        unit["skills"] = self.skill_paths(decl.get("skills"), name)
        unit["exts"] = self.extension_paths(decl.get("extensions"), name)
        unit["knowledge"] = self.name_list(decl.get("knowledge"), "knowledge", name)
        unit["tools"] = self.tool_list(decl, "tools", name)
        unit["excludeTools"] = self.tool_list(decl, "excludeTools", name)
        return unit

    def name_list(self, v, field, cap):
        """cap.yml 名单字段清洗（`skills`/`extensions`/`knowledge` 共用）：非数组 → WARN + 空；
        非字符串/空白元素 → WARN 跳过；`skills`/`extensions` 的名另拒路径分隔与前导点
        （防走出共享库一级）；`knowledge` 是 **lore 仓根下的名**（`library/<域>` ∨ `desk/<岗位>` ∨
        `archive`，首段即层标识）∨ 工作区路径（legacy 档），故允许 `/`（`..` 段由 kb_index 拒）。"""
        if v is None:
            return []
        if not isinstance(v, list):
            self.emit("WARN: 能力 %r cap.yml 的 %s 字段非数组 %r，跳过", cap, field, v)
            return []
        out = []
        for item in v:
            if field == "knowledge" and isinstance(item, dict):
                self.emit("WARN: 能力 %r cap.yml 的 knowledge 项为对象形态 %r：域级用途字段已退休，"
                          "声明只收路径字符串（如 dispatch/docs）→ 跳过该项", cap, item)
                continue
            if not isinstance(item, str) or not item.strip():
                self.emit("WARN: 能力 %r cap.yml 的 %s 含非字符串/空元素 %r，跳过", cap, field, item)
                continue
            n = item.strip()
            if field != "knowledge" and ("/" in n or "\\" in n or n.startswith(".")):
                self.emit("WARN: 能力 %r cap.yml 的 %s 名 %r 非法（含 / 或 \\ 或以 . 开头），跳过",
                          cap, field, n)
                continue
            out.append(n)
        return out

    def tool_list(self, doc, field, name):
        """cap.yml 工具面字段解析：取字符串数组，非字符串/空白元素 WARN 跳过；含内嵌逗号的元素
        （如 "read,bash"）WARN 拒绝——pi 按逗号拆工具名，原样拼入会撑大白名单/黑名单；
        非数组/缺省 → 空列表（= 未声明，不参与并集合并，调用方不拼参数）。"""
        v = doc.get(field)
        if v is None:
            return []
        if not isinstance(v, list):
            self.emit("WARN: 能力 %r cap.yml 的 %s 字段非数组 %r，跳过", name, field, v)
            return []
        out = []
        for item in v:
            if isinstance(item, str) and item.strip():
                t = item.strip()
                if "," in t:
                    self.emit("WARN: 能力 %r cap.yml 的 %s 元素 %r 含内嵌逗号，拒绝"
                              "（pi 按逗号拆工具名，防白名单/黑名单被撑大）", name, field, item)
                    continue
                out.append(t)
            else:
                self.emit("WARN: 能力 %r cap.yml 的 %s 含非字符串/空元素 %r，跳过", name, field, item)
        return out

    def skill_paths(self, names, cap):
        """cap.yml 的 `skills` → 共享库 `bots/skills/<名>/` **一级解析（不回落全局**：全局层本来就
        必装，回落无意义），按声明序返回绝对路径（与全局 skills 叠加，pi 原生累加语义）；
        目录缺失 → WARN 跳过该项（找不到 = 告警跳过，不硬失败）。"""
        out = []
        for item in self.name_list(names, "skills", cap):
            sd = os.path.join(self.root, SKILLS_REL, item)
            if not os.path.isdir(sd):
                self.emit("WARN: 能力 %r 捆绑的 skill %r 不存在（%s），跳过（只解析 bots/skills/ 一级、"
                          "不回落全局）", cap, item, sd)
                continue
            if not os.path.isfile(os.path.join(sd, "SKILL.md")):
                self.emit("WARN: 能力 %r 捆绑的 skill %r 缺 SKILL.md（%s），仍按目录注入 --skill"
                          "（pi 侧自行忽略）", cap, item, sd)
            out.append(sd)
        return out

    def extension_paths(self, names, cap):
        """cap.yml 的 `extensions` → 共享库 `bots/extensions/<名>/`（一个名字 = 一个扩展单元），
        按声明序返回绝对 .ts 路径；目录缺失/无可注入 .ts → WARN 跳过该项。
        **消费面注意**：pi 无运行期装载扩展的 API ⇒ 会话内注入层兑现不了本字段（有值即告警），
        只有 argv 发射层能拼 `-e`。"""
        out = []
        for item in self.name_list(names, "extensions", cap):
            ed = os.path.join(self.root, EXTS_REL, item)
            if not os.path.isdir(ed):
                self.emit("WARN: 能力 %r 捆绑的扩展 %r 不存在（%s），跳过", cap, item, ed)
                continue
            unit = self.ext_unit_paths(ed, item)
            if not unit:
                self.emit("WARN: 能力 %r 捆绑的扩展 %r 无可注入 .ts（%s），跳过", cap, item, ed)
            out += unit
        return out

    def ext_unit_paths(self, edir, name):
        """共享库扩展单元 `bots/extensions/<名>/` → 绝对 .ts 路径列表：含 `index.ts` → 恰一项；
        否则直属每个 `.ts`（按名排序保确定性）；非 .ts 文件 → WARN 跳过（dot 开头静默跳过、
        子目录不递归）。依赖一律 `.ts`（jiti 刷不掉 .mjs ESM 缓存）。"""
        idx = os.path.join(edir, "index.ts")
        if os.path.isfile(idx):
            return [idx]
        out = []
        try:
            entries = sorted(os.listdir(edir))
        except OSError:
            return []
        for entry in entries:
            if entry.startswith("."):
                continue
            ep = os.path.join(edir, entry)
            if not os.path.isfile(ep):
                continue                     # 子目录不递归（单元形态 = index.ts ∨ 直属 .ts）
            if entry.endswith(".ts"):
                out.append(ep)
            else:
                self.emit("WARN: 扩展 %r 内非 .ts 文件，跳过: %s", name, ep)
        return out

    # ---------- knowledge 清单 ----------

    def _kb_module(self):
        """按文件路径导入 $AGENT_ROOT/bots/kb_index.py（模块名带点/不在 sys.path，走 importlib）。
        结果缓存在实例上；不可导入（文件缺失/语法错/依赖缺失）→ WARN + None（知识清单不注入，
        会话照常起——清单是增强面，不是启动必需）。"""
        if hasattr(self, "_kb_mod"):
            return self._kb_mod
        self._kb_mod = None
        p = os.path.join(self.root, KB_INDEX_REL)
        if not os.path.isfile(p):
            self.emit("WARN: kb 索引工具缺失（%s），跳过 knowledge 清单注入", p)
            return None
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location("kb_index", p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            self._kb_mod = mod
        except Exception as e:            # 导入面任何异常都不该拖垮会话装配
            self.emit("WARN: kb 索引工具导入失败（%s）%r，跳过 knowledge 清单注入", p, e)
        return self._kb_mod

    def knowledge_block(self, entries):
        """knowledge 名列表 → (渲染好的「知识清单」块 ∨ None, 块字符数)。
        entries = 跨能力并集后的声明原样列表（**lore 仓根下的名** ∨ 工作区路径 = legacy 档），
        名解析/分档/去重/清洗/渲染全部交 kb_index（判定单点，与 CLI/巡检同一套口径）；本层只多记
        一行解析摘要（lore 档按层计数 / legacy 档计数 / lore 根在场性），便于排障「清单为何少了某面」。
        无有效名/渲染为空/异常 → (None, 0)：注入面逐字不变（字段缺失零回归的同一出口）。"""
        if not entries:
            return None, 0
        mod = self._kb_module()
        if mod is None:
            return None, 0
        try:
            warns = []
            block = mod.knowledge_block(entries, root=self.root, warnings=warns)
            for w in warns:
                self.emit("WARN: knowledge %s", w)
            self._log_knowledge_resolve(mod, entries)
        except Exception as e:
            self.emit("WARN: knowledge 清单渲染异常 %r，跳过注入（会话照常起）", e)
            return None, 0
        if not block.strip():
            self.emit("WARN: knowledge 声明 %d 项但无有效名（全部被拒/为空），跳过清单注入",
                      len(entries))
            return None, 0
        return block, len(block)

    def _log_knowledge_resolve(self, mod, entries):
        """一行解析摘要（只进日志、不影响注入面）：lore 档按层计数 + legacy 档计数 + lore 根在场性。
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
            self.emit("knowledge 名解析：lore 档 %d 项%s，legacy 工作区路径档 %d 项（lore 根 %s）",
                      sum(tiers.values()),
                      "（%s）" % ", ".join("%s×%d" % (k, v) for k, v in sorted(tiers.items()))
                      if tiers else "",
                      legacy, lore if os.path.isdir(lore) else "%s 不在场" % lore)
        except Exception as e:
            self.emit("WARN: knowledge 解析摘要计算异常 %r（不影响清单渲染）", e)

    # ---------- 小工具 ----------

    @staticmethod
    def _dedup(seq):
        return list(dict.fromkeys(seq))


def resolve(root, form=FORM_TASK, profile=None, emit=None):
    """便捷入口：一次解析（`Resolver.resolve` 的函数形态）。"""
    return Resolver(root, form=form, emit=emit).resolve(profile)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="persona.py",
        description="人格装配解析层：profile/caps → 结构化注入面 JSON（stdout）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("resolve", help="解析一个 profile → JSON")
    r.add_argument("--root", default=os.environ.get("AGENT_ROOT", ""),
                   help="工作区根（缺省 = env AGENT_ROOT）")
    r.add_argument("--form", choices=(FORM_TASK, FORM_RESIDENT), default=FORM_TASK,
                   help="会话形态（task = 前置基线能力 + 回落 + 屏蔽 ask_user）")
    r.add_argument("--profile", default=None,
                   help="profile 名（缺省 = 读 env DISPATCH_PROFILE）")
    r.add_argument("--compact", action="store_true", help="紧凑 JSON（无缩进）")
    a = ap.parse_args(argv)
    if a.cmd != "resolve":                 # argparse 已限制值域，此支为将来子命令留位
        return 2
    if not a.root:
        sys.stderr.write("[persona] FATAL: 需要 --root ∨ env AGENT_ROOT\n")
        return 2
    out = resolve(a.root, form=a.form, profile=a.profile)
    sys.stdout.write(json.dumps(out, ensure_ascii=False,
                                separators=(",", ":") if a.compact else None) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
