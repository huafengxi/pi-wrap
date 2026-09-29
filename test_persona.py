#!/usr/bin/env python3
"""test_persona.py — 人格装配**解析层**（persona.py）的离线单测。

射程分工（不与 test_wrap.py 重复）：本文件钉**解析层的输出契约**（结构化注入面的键集、
合并语义、降级矩阵、CLI 形态、以及「会话内注入 == argv 发射」的等价性锚）；
test_wrap.py 钉**发射层与生命周期**（pi argv 逐格、就绪握手、收敛与诊断）。
两者共用同一份解析 ⇒ 解析层回归由本文件先抓到，发射层回归由 test_wrap 抓。

仅标准库（夹具的 cap.yml 用「标量键 + 流式数组」两种形态，与 test_wrap 同款极简序列化）。
跑法：`python3 pi-wrap/test_persona.py`（退出码 0 = 全绿）。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import persona                                                  # noqa: E402

PASS = 0
FAIL = 0


def ok(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("PASS  %s" % name)
    else:
        FAIL += 1
        print("FAIL  %s\n      %s" % (name, detail))


# ---------- 夹具 ----------

EXEC_TEXT = "# executor baseline\n\ntask persona\n"
CAP_TEXT = "# reviewer persona\n\nbe strict\n"


def _yml_val(x):
    if isinstance(x, str):
        return json.dumps(x, ensure_ascii=False)
    if x is None:
        return "null"
    if isinstance(x, bool):
        return "true" if x else "false"
    return str(x)


def _yml(doc):
    lines = []
    for k, v in doc.items():
        if isinstance(v, list):
            lines.append("%s: [%s]" % (k, ", ".join(_yml_val(i) for i in v)))
        else:
            lines.append("%s: %s" % (k, _yml_val(v)))
    return "\n".join(lines) + "\n"


def _w(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


class Tree:
    """临时工作区树 + 一个把告警收进列表的 emit（断言 warnings 用）。"""

    def __init__(self, tag):
        self.root = tempfile.mkdtemp(prefix="persona-%s-" % tag)
        self.lines = []

    def emit(self, fmt, *args):
        self.lines.append(fmt % args if args else fmt)

    def cap(self, name, prompt_text=CAP_TEXT, cap_yml=None):
        cdir = os.path.join(self.root, "bots", "caps", name)
        os.makedirs(cdir, exist_ok=True)
        if prompt_text is not None:
            _w(os.path.join(cdir, "prompt.md"), prompt_text)
        if cap_yml is not None:
            _w(os.path.join(cdir, "cap.yml"),
               cap_yml if isinstance(cap_yml, str) else _yml(cap_yml))
        return cdir

    def profile(self, name, caps=None, model=None, extra=None, raw=None):
        p = os.path.join(self.root, "bots", "profiles", name + ".json")
        if raw is not None:
            return _w(p, raw)
        doc = {"name": name, "summary": "test profile %s" % name,
               "caps": list(caps) if caps is not None else [name]}
        if model is not None:
            doc["model"] = model
        if extra:
            doc.update(extra)
        return _w(p, json.dumps(doc, ensure_ascii=False, indent=1))

    def resolve(self, form=persona.FORM_TASK, profile=None, env=None):
        saved = {k: os.environ.get(k) for k in ("DISPATCH_PROFILE",)}
        if env:
            os.environ.update(env)
        try:
            return persona.resolve(self.root, form=form, profile=profile,
                                   emit=self.emit)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


SCHEMA_KEYS = {
    "schemaVersion", "form", "profile", "fallback", "caps", "appendParts",
    "appendJoiner", "promptStats", "skillPaths", "extensionPaths", "tools",
    "excludeTools", "model", "provider", "contextCompaction",
    "contextCompactionExt", "stats", "warnings",
}


# ---------- P1 输出契约 ----------

def p1_contract():
    t = Tree("p1")
    try:
        t.cap("executor", EXEC_TEXT)
        t.profile("executor", caps=["executor"], model="llm-router/executor")
        r = t.resolve(profile="executor")
        ok("P1a 键集与模块头 schema 逐字一致（跨仓契约 pin）",
           set(r.keys()) == SCHEMA_KEYS, sorted(set(r.keys()) ^ SCHEMA_KEYS))
        ok("P1b schemaVersion = 1", r["schemaVersion"] == 1, r["schemaVersion"])
        ok("P1c appendJoiner = pi 的多值拼接符 \\n\\n",
           r["appendJoiner"] == "\n\n", repr(r["appendJoiner"]))
        ok("P1d JSON 可序列化（扩展侧读的就是这个）",
           json.loads(json.dumps(r, ensure_ascii=False))["profile"] == "executor")
        ok("P1e form 原样回带", r["form"] == persona.FORM_TASK, r["form"])
        ok("P1f 非法 form 是编程错误 ⇒ ValueError（不静默降级）",
           _raises_value_error(t))
    finally:
        t.cleanup()


def _raises_value_error(t):
    try:
        persona.Resolver(t.root, form="nope")
        return False
    except ValueError:
        return True


# ---------- P2 caps 展开序 / 基线前置 / 去重 ----------

def p2_caps():
    t = Tree("p2")
    try:
        t.cap("executor", EXEC_TEXT)
        t.cap("review", CAP_TEXT)
        t.profile("review", caps=["review", "executor"], model="p/m")
        r = t.resolve(profile="review")
        ok("P2a 任务形态：基线能力被提到首位并去重",
           r["caps"] == ["executor", "review"], r["caps"])
        ok("P2b 注入序 = caps 序（正文逐字、不改一个字节）",
           r["appendParts"] == [EXEC_TEXT, CAP_TEXT], [len(x) for x in r["appendParts"]])
        ok("P2c promptStats 与正文一一对应",
           r["promptStats"] == [{"cap": "executor", "chars": len(EXEC_TEXT)},
                                {"cap": "review", "chars": len(CAP_TEXT)}],
           r["promptStats"])
        ok("P2d 非首位声明有 WARN（作者意图与「基线恒首」不一致）",
           any("非首位" in w for w in r["warnings"]), r["warnings"])
        rr = t.resolve(form=persona.FORM_RESIDENT, profile="review")
        ok("P2e resident 形态不前置基线、按 caps 序原样",
           rr["caps"] == ["review", "executor"], rr["caps"])
        ok("P2f resident 形态无基线排除集", rr["excludeTools"] == [], rr["excludeTools"])
    finally:
        t.cleanup()


def p2b_bundle_and_missing():
    t = Tree("p2b")
    try:
        t.cap("executor", EXEC_TEXT)
        t.cap("kit", prompt_text=None, cap_yml={"summary": "bundle"})
        t.profile("p", caps=["kit", "ghost"], model="p/m")
        r = t.resolve(profile="p")
        ok("P2g bundle 能力（无正文）合法 ⇒ 不进 appendParts、不刷 WARN",
           r["appendParts"] == [EXEC_TEXT]
           and not any("无 prompt.md" in w for w in r["warnings"]),
           json.dumps(r["warnings"], ensure_ascii=False))
        ok("P2h 缺失能力 WARN 跳过、其余照常（不拖垮）",
           any("ghost" in w and "不存在" in w for w in r["warnings"]), r["warnings"])
        ok("P2i stats.promptChars = 各正文之和",
           r["stats"]["promptChars"] == len(EXEC_TEXT)
           and r["stats"]["promptCount"] == 1, r["stats"])
    finally:
        t.cleanup()


# ---------- P3 回落与形态基线 ----------

def p3_fallback():
    t = Tree("p3")
    try:
        t.cap("executor", EXEC_TEXT)
        t.profile("executor", caps=["executor"], model="llm-router/executor")
        r = t.resolve(profile="")            # 未设
        ok("P3a 任务形态未设 profile ⇒ 回落 executor profile（缺省模型角色档）",
           r["fallback"] is True and r["profile"] == "executor"
           and r["model"] == "llm-router/executor", (r["fallback"], r["profile"], r["model"]))
        ok("P3b 回落面 caps 与「只前置基线」逐字一致",
           r["caps"] == ["executor"] and r["appendParts"] == [EXEC_TEXT], r["caps"])
        ok("P3c 任务形态排除集恒含形态基线 ask_user",
           r["excludeTools"] == ["ask_user"], r["excludeTools"])
        rr = t.resolve(form=persona.FORM_RESIDENT, profile="")
        ok("P3d resident 形态不回落 ⇒ 裸启动面（caps 空、注入面空）",
           rr["fallback"] is False and rr["caps"] == [] and rr["appendParts"] == []
           and rr["model"] is None and rr["excludeTools"] == [],
           json.dumps({k: rr[k] for k in ("caps", "appendParts", "model", "excludeTools")},
                      ensure_ascii=False))
        r2 = t.resolve(profile="ghost")
        ok("P3e 显式合法名但清单缺失 ⇒ 不回落（缺失属降级而非未设）+ 仍前置基线",
           r2["fallback"] is False and r2["profile"] == "ghost" and r2["caps"] == ["executor"]
           and r2["model"] is None, (r2["fallback"], r2["caps"], r2["model"]))
        r3 = t.resolve(profile="a,b")
        ok("P3f 已退役链式写法 ⇒ WARN 点名成因 + 按未设处置（走回落）",
           any("已退役的链式写法" in w for w in r3["warnings"]) and r3["fallback"] is True,
           r3["warnings"])
        r4 = t.resolve(profile="../evil")
        ok("P3g 非法名（穿越段）⇒ WARN 拒绝",
           any("名字非法" in w for w in r4["warnings"]), r4["warnings"])
    finally:
        t.cleanup()


# ---------- P4 knowledge 清单（末位追加 + 降级） ----------

def p4_knowledge():
    t = Tree("p4")
    try:
        t.cap("executor", EXEC_TEXT)
        t.cap("dom", CAP_TEXT, cap_yml={"knowledge": ["library/x", "desk/y"]})
        t.profile("p", caps=["dom"], model="p/m")
        r = t.resolve(profile="p")
        ok("P4a kb 工具缺失 ⇒ 降级：块不注入、WARN 点名根因、正文照常",
           r["appendParts"] == [EXEC_TEXT, CAP_TEXT]
           and any("kb 索引工具缺失" in w for w in r["warnings"]),
           json.dumps(r["warnings"], ensure_ascii=False))
        ok("P4b 块缺席时 stats.knowledgeChars = 0", r["stats"]["knowledgeChars"] == 0, r["stats"])
        # 造一个假的 kb_index（同签名）验证「块追加在全部能力正文之后」
        _w(os.path.join(t.root, "bots", "kb_index.py"),
           "def knowledge_block(entries, root=None, warnings=None):\n"
           "    return '## 知识清单\\n' + '\\n'.join(entries)\n"
           "def lore_root(root):\n    return root + '/lore'\n"
           "def normalize_domains(entries, root, x, lore):\n    return []\n")
        t2 = Tree("p4b")
        try:
            shutil.copytree(os.path.join(t.root, "bots"), os.path.join(t2.root, "bots"))
            r2 = t2.resolve(profile="p")
            ok("P4c 知识清单块追加在全部能力正文之后（末位）",
               r2["appendParts"] == [EXEC_TEXT, CAP_TEXT, "## 知识清单\nlibrary/x\ndesk/y"],
               json.dumps(r2["appendParts"], ensure_ascii=False)[:300])
            ok("P4d 块字符数进 stats", r2["stats"]["knowledgeChars"] == len(r2["appendParts"][-1]),
               r2["stats"])
        finally:
            t2.cleanup()
    finally:
        t.cleanup()


# ---------- P5 工具面并集 ----------

def p5_tools():
    t = Tree("p5")
    try:
        t.cap("executor", EXEC_TEXT, cap_yml={"excludeTools": ["web_search"]})
        t.cap("rev", CAP_TEXT, cap_yml={"tools": ["read", "bash", "web_search"]})
        t.profile("p", caps=["rev"], model="p/m")
        r = t.resolve(profile="p")
        ok("P5a tools = 声明者并集（去重保序）", r["tools"] == ["read", "bash", "web_search"],
           r["tools"])
        ok("P5b excludeTools = 形态基线 ∪ 声明者（基线恒在）",
           r["excludeTools"] == ["ask_user", "web_search"], r["excludeTools"])
        ok("P5c 白名单项被排除集命中 ⇒ WARN 不阻断",
           any("被排除集命中" in w and "web_search" in w for w in r["warnings"]), r["warnings"])
        t.cap("bad", CAP_TEXT, cap_yml={"tools": ["read,bash", 7]})
        t.profile("q", caps=["bad"], model="p/m")
        rq = t.resolve(profile="q")
        ok("P5d 内嵌逗号元素拒绝 + 非字符串元素跳过（两条 WARN）",
           any("内嵌逗号" in w for w in rq["warnings"])
           and any("非字符串" in w for w in rq["warnings"]) and rq["tools"] == [],
           json.dumps(rq["warnings"], ensure_ascii=False))
    finally:
        t.cleanup()


# ---------- P6/P7 捆绑资产路径 ----------

def p6_bundles():
    t = Tree("p6")
    try:
        t.cap("executor", EXEC_TEXT)
        os.makedirs(os.path.join(t.root, "bots", "skills", "s1"))
        _w(os.path.join(t.root, "bots", "skills", "s1", "SKILL.md"), "---\nname: s1\n---\n")
        _w(os.path.join(t.root, "bots", "extensions", "e1", "index.ts"), "// e1\n")
        _w(os.path.join(t.root, "bots", "extensions", "e2", "b.ts"), "// b\n")
        _w(os.path.join(t.root, "bots", "extensions", "e2", "a.ts"), "// a\n")
        _w(os.path.join(t.root, "bots", "extensions", "e2", "note.md"), "// 非 ts\n")
        t.cap("kit", CAP_TEXT, cap_yml={"skills": ["s1", "ghost"],
                                        "extensions": ["e1", "e2", "nope"]})
        t.profile("p", caps=["kit"], model="p/m")
        r = t.resolve(profile="p")
        ok("P6a skillPaths = 一级解析的绝对路径（缺失项跳过 + WARN 点名不回落全局）",
           r["skillPaths"] == [os.path.join(t.root, "bots", "skills", "s1")]
           and any("不回落全局" in w for w in r["warnings"]), r["skillPaths"])
        ok("P6b 扩展单元：index.ts 恰一项；flat 形态直属 .ts 按名排序",
           r["extensionPaths"] == [
               os.path.join(t.root, "bots", "extensions", "e1", "index.ts"),
               os.path.join(t.root, "bots", "extensions", "e2", "a.ts"),
               os.path.join(t.root, "bots", "extensions", "e2", "b.ts")],
           r["extensionPaths"])
        ok("P6c 非 .ts 文件 WARN 跳过、捆绑名不存在 WARN 跳过",
           any("非 .ts" in w for w in r["warnings"])
           and any("nope" in w and "不存在" in w for w in r["warnings"]),
           json.dumps(r["warnings"], ensure_ascii=False))
        ok("P6d stats.skills/exts 计数与路径数一致",
           r["stats"]["skills"] == 1 and r["stats"]["exts"] == 3, r["stats"])
    finally:
        t.cleanup()


# ---------- P8 model → provider 派生 ----------

def p8_provider():
    cases = [
        ("llm-router/planner", "llm-router"),   # 合法两段式
        ("qwen3.8-max", None),                  # 裸 id：pi 按 settings 默认 provider 解析
        ("/planner", None),                     # provider 段空
        ("a/b/c", None),                        # 多段：不猜切分点
        ("", None), ("   ", None), (None, None), (7, None), ("  p/m  ", "p"),
    ]
    bad = [(m, persona.provider_of_model(m), want) for m, want in cases
           if persona.provider_of_model(m) != want]
    ok("P8a provider_of_model 九态逐条（含四种畸形一律 None = fail-soft 不 die）",
       not bad, bad)
    t = Tree("p8")
    try:
        t.cap("executor", EXEC_TEXT)
        t.profile("p", caps=["executor"], model="  llm-router/planner  ")
        r = t.resolve(profile="p")
        ok("P8b model strip 后原样带出 + provider 派生",
           r["model"] == "llm-router/planner" and r["provider"] == "llm-router",
           (r["model"], r["provider"]))
        t.profile("q", caps=["executor"], extra={"model": 7})
        rq = t.resolve(profile="q")
        ok("P8c model 类型非法 ⇒ WARN + 不给 model（不硬失败）",
           rq["model"] is None and rq["provider"] is None
           and any("model 字段非非空字符串" in w for w in rq["warnings"]), rq["warnings"])
    finally:
        t.cleanup()


# ---------- P9 contextCompaction 判据表（与 lint E16 / policy.ts 同判） ----------

def p9_cc():
    good = [{"triggerTokens": 1000}, {"triggerRatio": 0.5},
            {"triggerTokens": 1000, "triggerRatio": 0.5},
            {"enabled": False}, {"enabled": True, "triggerRatio": 1},
            {"triggerTokens": 1, "customInstructions": "压叙述"}]
    bad = [{}, {"triggerTokens": 0}, {"triggerTokens": -3}, {"triggerTokens": 1.5},
           {"triggerTokens": True}, {"triggerRatio": 0}, {"triggerRatio": 1.5},
           {"triggerRatio": "0.5"}, {"enabled": "yes"}, {"enabled": True},
           {"customInstructions": "   "}, {"customInstructions": "x" * 2001},
           {"keepRecentTokens": 1}, {"triggerTokens": 10, "unknownKey": 1},
           "not-a-dict", None, 7, []]
    bad_g = [g for g in good if persona.cc_policy(g)[0] is None]
    bad_b = [b for b in bad if persona.cc_policy(b)[0] is not None]
    ok("P9a 六个合法形态全部通过（归一化恒含 enabled）",
       not bad_g and all(persona.cc_policy(g)[0].get("enabled") in (True, False) for g in good),
       bad_g)
    ok("P9b 十七个非法形态全部拒绝且给出原因（白名单外键整块丢弃、不「忽略未知键」）",
       not bad_b and all(persona.cc_policy(b)[1] for b in bad), bad_b)
    pol, err = persona.cc_policy({"triggerRatio": 0.75})
    ok("P9c 归一化只含实际声明的键 + 恒含 enabled",
       pol == {"enabled": True, "triggerRatio": 0.75} and err is None, (pol, err))

    t = Tree("p9")
    try:
        t.cap("executor", EXEC_TEXT)
        ext = os.path.join(t.root, persona.CONTEXT_COMPACTION_EXT_REL)
        t.profile("p", caps=["executor"], model="p/m",
                  extra={"contextCompaction": {"triggerRatio": 0.5}})
        r = t.resolve(profile="p")
        ok("P9d 执行体缺失 ⇒ 策略与路径同进同退（都为空）+ WARN 点名",
           r["contextCompaction"] is None and r["contextCompactionExt"] is None
           and any("执行体缺失" in w for w in r["warnings"]), r["warnings"])
        _w(ext, "export default function () {}\n")
        r2 = t.resolve(profile="p")
        ok("P9e 执行体在场 ⇒ 归一化策略 + 绝对路径同时给出",
           r2["contextCompaction"] == {"enabled": True, "triggerRatio": 0.5}
           and r2["contextCompactionExt"] == ext,
           (r2["contextCompaction"], r2["contextCompactionExt"]))
        t.profile("q", caps=["executor"], model="p/m",
                  extra={"contextCompaction": {"keepRecentTokens": 1}})
        r3 = t.resolve(profile="q")
        ok("P9f 策略非法 ⇒ WARN 点名不可达面 + 不装配（会话照起）",
           r3["contextCompaction"] is None
           and any("keepRecentTokens" in w for w in r3["warnings"]), r3["warnings"])
        rr = t.resolve(form=persona.FORM_RESIDENT, profile="p")
        ok("P9g resident 形态同等装配（策略住 profile、与形态无关）",
           rr["contextCompactionExt"] == ext, rr["contextCompactionExt"])
    finally:
        t.cleanup()


# ---------- P10 告警收集与 sink 同源 ----------

def p10_warnings():
    t = Tree("p10")
    try:
        t.cap("executor", EXEC_TEXT)
        t.profile("p", caps=["executor", "ghost"], model="p/m")
        r = t.resolve(profile="p")
        ok("P10a warnings[] 与 emit sink 同源（WARN 行进两者、info 行只进 sink）",
           r["warnings"] == [l for l in t.lines if l.startswith("WARN")]
           and any("展开完成" in l for l in t.lines)
           and not any("展开完成" in w for w in r["warnings"]),
           json.dumps({"warn": r["warnings"], "lines": t.lines}, ensure_ascii=False)[:400])
        ok("P10b warnings 是独立副本（后续 emit 不改已返回的结果）",
           (r["warnings"].append("x"), "x" not in persona.resolve(
               t.root, emit=t.emit)["warnings"])[1])
    finally:
        t.cleanup()


# ---------- P11 等价性锚：会话内注入 == argv 发射 ----------

def p11_equivalence():
    """两个消费者必须产出逐字相同的系统提示追加段：
      argv 形态 = N 个 `--append-system-prompt`，pi 侧 `join("\\n\\n")`
                  （dist/core/agent-session.js）后整段以 `\\n\\n` 接在基础提示之后
                  （dist/core/system-prompt.js）；
      会话内形态 = `before_agent_start` 返回 `event.systemPrompt + joiner + joiner.join(parts)`。
    本条把 joiner 与 parts 序钉成契约（改任一侧都要过这条）。"""
    t = Tree("p11")
    try:
        t.cap("executor", EXEC_TEXT)
        t.cap("a", "# a\n")
        t.cap("b", "# b\n")
        t.profile("p", caps=["a", "b"], model="p/m")
        r = t.resolve(profile="p")
        j = r["appendJoiner"]
        argv_form = j.join(r["appendParts"])          # pi 对多个 --append-system-prompt 的拼接
        session_form = j.join(r["appendParts"])       # 注入层的拼接（同一个 joiner、同一份 parts）
        ok("P11a 两形态的追加段逐字相同", argv_form == session_form ==
           EXEC_TEXT + "\n\n# a\n" + "\n\n# b\n", repr(session_form))
        ok("P11b 追加段 = 基础提示之后以 joiner 相接（前缀语义由 pi 侧承担，此处钉分隔符）",
           ("BASE" + j + argv_form) == "BASE\n\n" + EXEC_TEXT + "\n\n# a\n\n\n# b\n",
           repr("BASE" + j + argv_form))
        ok("P11c parts 序 = caps 展开序（基线在首）",
           r["caps"] == ["executor", "a", "b"], r["caps"])
    finally:
        t.cleanup()


# ---------- P12 CLI 契约（扩展侧 execFileSync 消费的就是这个） ----------

def p12_cli():
    t = Tree("p12")
    script = os.path.join(HERE, "persona.py")
    try:
        t.cap("executor", EXEC_TEXT)
        t.profile("p", caps=["executor"], model="llm-router/executor")
        env = dict(os.environ, AGENT_ROOT=t.root)

        def run(args, e=None):
            return subprocess.run([sys.executable, script] + args,
                                  capture_output=True, text=True,
                                  env=e if e is not None else env, timeout=60)

        r = run(["resolve", "--root", t.root, "--form", "task", "--profile", "p"])
        d = json.loads(r.stdout)
        ok("P12a rc=0 且 stdout 是**单个** JSON 对象（告警走 stderr）",
           r.returncode == 0 and set(d.keys()) == SCHEMA_KEYS and r.stdout.count("\n") == 1,
           (r.returncode, r.stdout[:200], r.stderr[:200]))
        ok("P12b 注入面与进程内解析逐字一致",
           d["appendParts"] == [EXEC_TEXT] and d["model"] == "llm-router/executor",
           d["appendParts"])
        rc = run(["resolve", "--root", t.root, "--form", "task", "--profile", "ghost"])
        ok("P12c 清单缺失仍 rc=0（fail-soft 是硬要求）+ warnings 非空",
           rc.returncode == 0 and json.loads(rc.stdout)["warnings"], (rc.returncode, rc.stdout[:200]))
        rr = run(["resolve", "--root", t.root, "--form", "resident"],
                 e={k: v for k, v in env.items() if k != "DISPATCH_PROFILE"})
        ok("P12d --profile 缺省 = 读 env DISPATCH_PROFILE（两个输入口等价）",
           json.loads(rr.stdout)["caps"] == [] and rr.returncode == 0, rr.stdout[:200])
        re_ = run(["resolve", "--root", t.root, "--profile", "p"],
                  e=dict(env, DISPATCH_PROFILE="p"))
        ok("P12e env 形态与 flag 形态产出同一注入面",
           json.loads(re_.stdout)["appendParts"] == d["appendParts"], re_.stdout[:200])
        rc2 = run(["resolve", "--root", t.root, "--profile", "p", "--compact"])
        ok("P12f --compact 输出无缩进（同一 JSON）",
           " " not in rc2.stdout.split("\n")[0][:40]
           and json.loads(rc2.stdout) == d, rc2.stdout[:120])
        e = {k: v for k, v in env.items() if k != "AGENT_ROOT"}
        rc3 = run(["resolve", "--form", "task"], e=e)
        ok("P12g 无 --root 且无 AGENT_ROOT ⇒ rc=2 + FATAL 到 stderr（用法错误不静默）",
           rc3.returncode == 2 and "FATAL" in rc3.stderr, (rc3.returncode, rc3.stderr[:200]))
        rc4 = run(["resolve", "--root", "/nonexistent/root/xyz", "--profile", "p"])
        ok("P12h root 不存在 ⇒ rc=0 + 空注入面（绝不 die：硬失败会自锁）",
           rc4.returncode == 0 and json.loads(rc4.stdout)["appendParts"] == [],
           (rc4.returncode, rc4.stdout[:200], rc4.stderr[:200]))
        rc5 = run(["nosuchcmd"])
        ok("P12i 未知子命令 ⇒ argparse 拒（rc≠0）", rc5.returncode != 0, rc5.returncode)
    finally:
        t.cleanup()


# ---------- P13 真实资产面（在场则核，不在场跳过） ----------

def p13_real_assets():
    ws = os.path.normpath(os.path.join(HERE, ".."))
    if not os.path.isdir(os.path.join(ws, "bots", "profiles")):
        ok("P13 真实资产不在本快照内 → 跳过", True)
        return
    names = sorted(f[:-5] for f in os.listdir(os.path.join(ws, "bots", "profiles"))
                   if f.endswith(".json"))
    bad = []
    for n in names:
        for form in (persona.FORM_TASK, persona.FORM_RESIDENT):
            r = persona.resolve(ws, form=form, profile=n, emit=lambda *a: None)
            if r["schemaVersion"] != 1 or not r["caps"] or not r["appendParts"]:
                bad.append((n, form, r["caps"]))
            if r["appendParts"] and any(not p.strip() for p in r["appendParts"]):
                bad.append((n, form, "空正文"))
    ok("P13 现网全部 profile × 两形态解析成功且注入面非空（%d 个 profile）" % len(names),
       not bad, bad)


def main():
    for fn in (p1_contract, p2_caps, p2b_bundle_and_missing, p3_fallback, p4_knowledge,
               p5_tools, p6_bundles, p8_provider, p9_cc, p10_warnings, p11_equivalence,
               p12_cli, p13_real_assets):
        print("---- %s" % fn.__name__)
        fn()
    print("==== persona 单测：%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
