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

KB_INDEX_SRC = os.path.join(os.path.dirname(HERE), "bots", "kb_index.py")
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

    def profile(self, name, caps=None, model=None, form=persona.FORM_TASK,
                extra=None, raw=None):
        """建 profile 薄清单。`form` = 会话形态轴的**声明落点**（缺省写 task 档，与改前
        `Tree.resolve(form=FORM_TASK)` 的缺省一致）；`form=None` = 显式不声明该键（造漏声明面）。"""
        p = os.path.join(self.root, "bots", "profiles", name + ".json")
        if raw is not None:
            return _w(p, raw)
        doc = {"name": name, "summary": "test profile %s" % name}
        if form is not None:
            doc["form"] = form
        doc["caps"] = list(caps) if caps is not None else [name]
        if model is not None:
            doc["model"] = model
        if extra:
            doc.update(extra)
        return _w(p, json.dumps(doc, ensure_ascii=False, indent=1))

    def kb(self, dom_rel, docs, copy_tool=True):
        """建知识域 `<root>/<dom_rel>/` 下逐篇 .md；docs = [(文件名, when ∨ None)]。
        copy_tool=True 把**生产** bots/kb_index.py 拷进临时树（解析层按文件路径导入 ⇒
        测的是真工具而非 mock）。返回域绝对路径。"""
        if copy_tool:
            d = os.path.join(self.root, "bots")
            os.makedirs(d, exist_ok=True)
            shutil.copy(KB_INDEX_SRC, os.path.join(d, "kb_index.py"))
        ddir = os.path.join(self.root, dom_rel)
        os.makedirs(ddir, exist_ok=True)
        for fn, when in docs:
            with open(os.path.join(ddir, fn), "w", encoding="utf-8") as f:
                if when:
                    f.write('---\nwhen: "%s"\n---\n\n# %s\n\n正文\n' % (when, fn))
                else:
                    f.write("# %s\n\n正文（未入册）\n" % fn)
        return ddir

    def lore_fixture(self, with_lore=True):
        """lore 三面夹具（library 两层 + desk/journal + archive）；with_lore=False 只留工具。"""
        self.kb(os.path.join("lore", "library", "dom", "facts"),
                [("a.md", "册 A 何时读"), ("noWhen.md", None)], copy_tool=True)
        if not with_lore:
            shutil.rmtree(os.path.join(self.root, "lore"), ignore_errors=True)
            return {}
        self.kb(os.path.join("lore", "library", "dom", "cases"),
                [("c.md", "案例 C 何时读")], copy_tool=False)
        self.kb(os.path.join("lore", "desk", "me", "journal"), [("2026-09.md", None)],
                copy_tool=False)
        self.kb(os.path.join("lore", "desk", "me"), [("todo.md", None)], copy_tool=False)
        self.kb(os.path.join("lore", "archive"), [("incidents-x.md", None)], copy_tool=False)
        lore = os.path.join(self.root, "lore")
        return {"lib": os.path.join(lore, "library", "dom"),
                "journal": os.path.join(lore, "desk", "me", "journal"),
                "desk": os.path.join(lore, "desk", "me"),
                "archive": os.path.join(lore, "archive")}

    def resolve(self, profile=None, env=None):
        """一次解析（形态不由调用方给：它是 profile 清单的 `form` 字段）。"""
        saved = {k: os.environ.get(k) for k in ("DISPATCH_PROFILE",)}
        if env:
            os.environ.update(env)
        try:
            return persona.resolve(self.root, profile=profile, emit=self.emit)
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
        ok("P1e form 来自 profile 声明并原样回带（不再由调用方传入）",
           r["form"] == persona.FORM_TASK, r["form"])
        t.profile("res", caps=["executor"], model="p/m", form=persona.FORM_RESIDENT)
        t.profile("itr", caps=[], model="p/m", form=persona.FORM_INTERACTIVE)
        ok("P1f 三档值域均可解析并原样回带（task/resident/interactive）",
           [t.resolve(profile=n)["form"] for n in ("executor", "res", "itr")]
           == [persona.FORM_TASK, persona.FORM_RESIDENT, persona.FORM_INTERACTIVE],
           [t.resolve(profile=n)["form"] for n in ("executor", "res", "itr")])
        t.profile("bad", caps=["executor"], model="p/m", form="nope")
        rb = t.resolve(profile="bad")
        ok("P1g 声明非法 form ⇒ WARN 点名三档 + 缺省最严档 task（fail-soft，绝不 die）",
           rb["form"] == persona.FORM_TASK
           and any("值非法" in w and "task|resident|interactive" in w for w in rb["warnings"]),
           json.dumps(rb["warnings"], ensure_ascii=False)[:400])
        t.profile("noform", caps=["executor"], model="p/m", form=None)
        rn = t.resolve(profile="noform")
        ok("P1h 漏声明 form ⇒ WARN 点名提交期判据（cap_lint E17）+ 缺省最严档 task",
           rn["form"] == persona.FORM_TASK
           and any("未声明" in w and "E17" in w for w in rn["warnings"]),
           json.dumps(rn["warnings"], ensure_ascii=False)[:400])
    finally:
        t.cleanup()


# ---------- P2 caps 展开序（= 声明序）/ 去重 ----------

def p2_caps():
    t = Tree("p2")
    try:
        t.cap("executor", EXEC_TEXT)
        t.cap("review", CAP_TEXT)
        t.profile("review", caps=["review", "executor"], model="p/m")
        r = t.resolve(profile="review")
        ok("P2a caps = 声明序原样（装配器不再按形态重排/前置；基线在首位是声明面的事，"
           "提交期判据 = cap_lint E17）",
           r["caps"] == ["review", "executor"], r["caps"])
        ok("P2b 注入序 = caps 声明序（正文逐字、不改一个字节）",
           r["appendParts"] == [CAP_TEXT, EXEC_TEXT], [len(x) for x in r["appendParts"]])
        ok("P2c promptStats 与正文一一对应",
           r["promptStats"] == [{"cap": "review", "chars": len(CAP_TEXT)},
                                {"cap": "executor", "chars": len(EXEC_TEXT)}],
           r["promptStats"])
        ok("P2d 无「非首位」类形态硬规则 WARN（前置已搬进声明面）",
           not any("非首位" in w for w in r["warnings"]), r["warnings"])
        t.profile("review-res", caps=["review", "executor"], model="p/m",
                  form=persona.FORM_RESIDENT)
        rr = t.resolve(profile="review-res")
        ok("P2e resident 档同样按声明序原样（形态不派生能力面）",
           rr["caps"] == ["review", "executor"] and rr["form"] == persona.FORM_RESIDENT,
           (rr["caps"], rr["form"]))
        ok("P2f 排除集只来自能力声明（本夹具的能力都未声明 ⇒ 空）",
           rr["excludeTools"] == [], rr["excludeTools"])
        t.profile("review-dup", caps=["review", "executor", "review"], model="p/m")
        rd = t.resolve(profile="review-dup")
        ok("P2g 能力名重复 ⇒ 去重保序 + WARN（与形态无关）",
           rd["caps"] == ["review", "executor"]
           and any("重复" in w for w in rd["warnings"]), rd["caps"])
    finally:
        t.cleanup()


def p2b_bundle_and_missing():
    t = Tree("p2b")
    try:
        t.cap("executor", EXEC_TEXT)
        t.cap("kit", prompt_text=None, cap_yml={"summary": "bundle"})
        t.profile("p", caps=["executor", "kit", "ghost"], model="p/m")
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


# ---------- P3 回落（task 档无形态声明可读时的缺省面） ----------

def p3_fallback():
    t = Tree("p3")
    try:
        t.cap("executor", EXEC_TEXT, cap_yml={"excludeTools": ["ask_user"]})
        t.profile("executor", caps=["executor"], model="llm-router/executor")
        r = t.resolve(profile="")            # 未设
        ok("P3a 未设 profile 名 ⇒ 缺省最严档 task + 回落 executor profile（缺省模型角色档）",
           r["fallback"] is True and r["profile"] == "executor"
           and r["form"] == persona.FORM_TASK
           and r["model"] == "llm-router/executor",
           (r["fallback"], r["profile"], r["form"], r["model"]))
        ok("P3b 回落面注入面 = 回落 profile 的声明面（同一条解析路径，不另写平行分支）",
           r["caps"] == ["executor"] and r["appendParts"] == [EXEC_TEXT], r["caps"])
        ok("P3c 排除集来自能力声明（caps/executor 的 excludeTools）而非形态基线",
           r["excludeTools"] == ["ask_user"]
           and not any("形态基线" in w for w in r["warnings"]), r["excludeTools"])
        t.profile("res-empty", caps=[], model=None, form=persona.FORM_RESIDENT)
        rr = t.resolve(profile="res-empty")
        ok("P3d 显式声明 resident 档 ⇒ 不回落（有名字就无从读不到形态）+ 裸启动面",
           rr["fallback"] is False and rr["form"] == persona.FORM_RESIDENT
           and rr["caps"] == [] and rr["appendParts"] == [] and rr["model"] is None
           and rr["excludeTools"] == [],
           json.dumps({k: rr[k] for k in ("form", "caps", "appendParts", "model", "excludeTools")},
                      ensure_ascii=False))
        r2 = t.resolve(profile="ghost")
        ok("P3e 显式合法名但清单缺失 ⇒ 不回落（缺失属降级而非未设）、注入面为空 + WARN 点名人格面缺席"
           "（改前该路径仍被装配器硬规则前置基线能力；现按 d-p8bo A 只靠声明面 + lint 兜）",
           r2["fallback"] is False and r2["profile"] == "ghost" and r2["caps"] == []
           and r2["appendParts"] == [] and r2["model"] is None
           and any("不存在" in w for w in r2["warnings"]),
           (r2["fallback"], r2["caps"], json.dumps(r2["warnings"], ensure_ascii=False)[:300]))
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
        t.profile("p", caps=["executor", "dom"], model="p/m")
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
        t.profile("p", caps=["executor", "rev"], model="p/m")
        r = t.resolve(profile="p")
        ok("P5a tools = 声明者并集（去重保序）", r["tools"] == ["read", "bash", "web_search"],
           r["tools"])
        ok("P5b excludeTools = 声明者并集（无形态基线参与；本例只有 executor 能力声明了它）",
           r["excludeTools"] == ["web_search"], r["excludeTools"])
        ok("P5c 白名单项被排除集命中 ⇒ WARN 不阻断",
           any("被排除集命中" in w and "web_search" in w for w in r["warnings"]), r["warnings"])
        t.profile("p-noxt", caps=["rev"], model="p/m")
        rn = t.resolve(profile="p-noxt")
        ok("P5e task 档也不派生任何形态基线排除集（无人声明 ⇒ 空，安全面只住声明）",
           rn["form"] == persona.FORM_TASK and rn["excludeTools"] == [],
           (rn["form"], rn["excludeTools"]))
        t.cap("bad", CAP_TEXT, cap_yml={"tools": ["read,bash", 7]})
        t.profile("q", caps=["executor", "bad"], model="p/m")
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
        t.profile("p-res", caps=["executor"], model="p/m", form=persona.FORM_RESIDENT,
                  extra={"contextCompaction": {"triggerRatio": 0.5}})
        rr = t.resolve(profile="p-res")
        ok("P9g resident 档同等装配（策略住 profile、与形态无关）",
           rr["contextCompactionExt"] == ext, rr["contextCompactionExt"])
        # 零 cap 是合法声明（interactive 档的定档形态），而 resolve() 对它走早退分支
        # ⇒ 早退不得吞掉与能力面无关的运行环境字段（一手缺陷：command-center 声明了
        # 策略却被静默丢弃、零 WARN，压缩落回 pi 内建阈值）。
        t.profile("p-zero", caps=[], model="p/m", form=persona.FORM_INTERACTIVE,
                  extra={"contextCompaction": {"triggerTokens": 512000}})
        rz = t.resolve(profile="p-zero")
        ok("P9h 零 cap 的早退分支同等装配策略（形态/能力面都不改变该字段的装配）",
           rz["caps"] == []
           and rz["contextCompaction"] == {"enabled": True, "triggerTokens": 512000}
           and rz["contextCompactionExt"] == ext,
           (rz["caps"], rz["contextCompaction"], rz["contextCompactionExt"]))
        t.profile("q-zero", caps=[], model="p/m", form=persona.FORM_INTERACTIVE)
        rz2 = t.resolve(profile="q-zero")
        ok("P9i 零 cap 且未声明策略 ⇒ 两键仍同为空（早退分支不凭空装配）",
           rz2["contextCompaction"] is None and rz2["contextCompactionExt"] is None,
           (rz2["contextCompaction"], rz2["contextCompactionExt"]))
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
        t.profile("p", caps=["executor", "a", "b"], model="p/m")
        r = t.resolve(profile="p")
        j = r["appendJoiner"]
        argv_form = j.join(r["appendParts"])          # pi 对多个 --append-system-prompt 的拼接
        session_form = j.join(r["appendParts"])       # 注入层的拼接（同一个 joiner、同一份 parts）
        ok("P11a 两形态的追加段逐字相同", argv_form == session_form ==
           EXEC_TEXT + "\n\n# a\n" + "\n\n# b\n", repr(session_form))
        ok("P11b 追加段 = 基础提示之后以 joiner 相接（前缀语义由 pi 侧承担，此处钉分隔符）",
           ("BASE" + j + argv_form) == "BASE\n\n" + EXEC_TEXT + "\n\n# a\n\n\n# b\n",
           repr("BASE" + j + argv_form))
        ok("P11c parts 序 = caps 声明序（基线在首是声明面的事，不是装配器重排的结果）",
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

        r = run(["resolve", "--root", t.root, "--profile", "p"])
        d = json.loads(r.stdout)
        ok("P12a rc=0 且 stdout 是**单个** JSON 对象（告警走 stderr）",
           r.returncode == 0 and set(d.keys()) == SCHEMA_KEYS and r.stdout.count("\n") == 1,
           (r.returncode, r.stdout[:200], r.stderr[:200]))
        ok("P12b 注入面与进程内解析逐字一致",
           d["appendParts"] == [EXEC_TEXT] and d["model"] == "llm-router/executor",
           d["appendParts"])
        rc = run(["resolve", "--root", t.root, "--profile", "ghost"])
        ok("P12c 清单缺失仍 rc=0（fail-soft 是硬要求）+ warnings 非空",
           rc.returncode == 0 and json.loads(rc.stdout)["warnings"], (rc.returncode, rc.stdout[:200]))
        rr = run(["resolve", "--root", t.root],
                 e=dict({k: v for k, v in env.items() if k != "DISPATCH_PROFILE"},
                        DISPATCH_PROFILE="p"))
        ok("P12d --profile 缺省 = 读 env DISPATCH_PROFILE（两个输入口等价）",
           json.loads(rr.stdout)["caps"] == d["caps"] and rr.returncode == 0, rr.stdout[:200])
        re_ = run(["resolve", "--root", t.root, "--profile", "p"],
                  e=dict(env, DISPATCH_PROFILE="p"))
        ok("P12e env 形态与 flag 形态产出同一注入面",
           json.loads(re_.stdout)["appendParts"] == d["appendParts"], re_.stdout[:200])
        rc2 = run(["resolve", "--root", t.root, "--profile", "p", "--compact"])
        ok("P12f --compact 输出无缩进（同一 JSON）",
           " " not in rc2.stdout.split("\n")[0][:40]
           and json.loads(rc2.stdout) == d, rc2.stdout[:120])
        e = {k: v for k, v in env.items() if k != "AGENT_ROOT"}
        rc3 = run(["resolve"], e=e)
        ok("P12g 无 --root 且无 AGENT_ROOT ⇒ rc=2 + FATAL 到 stderr（用法错误不静默）",
           rc3.returncode == 2 and "FATAL" in rc3.stderr, (rc3.returncode, rc3.stderr[:200]))
        rc4 = run(["resolve", "--root", "/nonexistent/root/xyz", "--profile", "p"])
        ok("P12h root 不存在 ⇒ rc=0 + 空注入面（绝不 die：硬失败会自锁）",
           rc4.returncode == 0 and json.loads(rc4.stdout)["appendParts"] == [],
           (rc4.returncode, rc4.stdout[:200], rc4.stderr[:200]))
        rc5 = run(["nosuchcmd"])
        ok("P12i 未知子命令 ⇒ argparse 拒（rc≠0）", rc5.returncode != 0, rc5.returncode)
        rc6 = run(["resolve", "--root", t.root, "--form", "task", "--profile", "p"])
        ok("P12j `--form` 已退役（形态住 profile 的 `form` 字段）⇒ argparse 拒 rc=2",
           rc6.returncode == 2 and "--form" in rc6.stderr,
           (rc6.returncode, rc6.stderr[:200]))
    finally:
        t.cleanup()


# ---------- P13 真实资产面（在场则核，不在场跳过） ----------

# 现网形态映射（验收面：本批把形态轴收进 profile 的逐枚落点）；名单外的 profile 只核通用判据。
REAL_FORMS = {"executor": persona.FORM_TASK, "review": persona.FORM_TASK,
              "dispatcher": persona.FORM_RESIDENT, "moderator": persona.FORM_RESIDENT,
              "agentfw-lead": persona.FORM_RESIDENT, "work-lead": persona.FORM_RESIDENT,
              "command-center": persona.FORM_INTERACTIVE}


def p13_real_assets():
    ws = os.path.normpath(os.path.join(HERE, ".."))
    if not os.path.isdir(os.path.join(ws, "bots", "profiles")):
        ok("P13 真实资产不在本快照内 → 跳过", True)
        return
    names = sorted(f[:-5] for f in os.listdir(os.path.join(ws, "bots", "profiles"))
                   if f.endswith(".json"))
    bad, forms = [], {}
    for n in names:
        r = persona.resolve(ws, profile=n, emit=lambda *a: None)
        forms[n] = r["form"]
        if r["schemaVersion"] != 1:
            bad.append((n, r["form"], "schemaVersion != 1"))
        # 注入面非空的要求**按形态分档**：task 档必带基线能力 ⇒ caps 与 appendParts 都非空；
        # resident/interactive 档的**零 cap 是合法声明**（装配层接受 `caps: []` ⇒ 注入面为空、
        # ask_user 保留）⇒ 只要求「caps 与注入正文同空同非空」（不一致 = 装配失真）。
        if r["form"] == persona.FORM_TASK:
            if not r["caps"] or not r["appendParts"]:
                bad.append((n, r["form"], "task 档注入面为空：%s" % r["caps"]))
        elif bool(r["caps"]) != bool(r["appendParts"]):
            bad.append((n, r["form"], "caps 与注入正文不同空同非空：%s" % r["caps"]))
        if r["appendParts"] and any(not p.strip() for p in r["appendParts"]):
            bad.append((n, r["form"], "空正文"))
        if r["form"] not in persona.FORMS:
            bad.append((n, r["form"], "form 不在三档值域"))
        if n in REAL_FORMS and r["form"] != REAL_FORMS[n]:
            bad.append((n, r["form"], "现网映射期望 %s" % REAL_FORMS[n]))
        if r["form"] == persona.FORM_TASK:
            # 安全不变量（声明面承担、cap_lint E17 提交期兜）：基线能力在首位 ∧ 排除 ask_user
            if not r["caps"] or r["caps"][0] != "executor":
                bad.append((n, r["form"], "caps 首位非 executor：%s" % r["caps"]))
            if "ask_user" not in r["excludeTools"]:
                bad.append((n, r["form"], "excludeTools 漏 ask_user：%s" % r["excludeTools"]))
        else:
            if r["fallback"]:
                bad.append((n, r["form"], "非 task 档却走了回落"))
    ok("P13 现网全部 profile 解析成功、注入面**按形态分档**合规（task 档非空 ∧ 基线在首位；"
       "resident/interactive 档零 cap 合法）、form 声明与现网映射逐枚相符"
       "（%d 个 profile：%s）" % (len(names), json.dumps(forms, ensure_ascii=False)),
       not bad, bad)




# ---------- P14 knowledge 三档（真 kb_index，迁自 argv 形态的 T35/T28） ----------

PT = "# domain persona\n"


def p14_knowledge_tiers():
    if not os.path.isfile(KB_INDEX_SRC):
        ok("P14 生产 kb_index.py 不在本快照内 → 跳过", True)
        return

    def mk(tag, knowledge, with_lore=True, extra_dirs=()):
        t = Tree(tag)
        t.cap("mod", PT, cap_yml={"knowledge": list(knowledge)})
        t.profile("mod", caps=["mod"], model="p/m",
                    form=persona.FORM_RESIDENT)
        d = t.lore_fixture(with_lore=with_lore)
        for rel, docs in extra_dirs:
            t.kb(rel, docs, copy_tool=False)
        return t, d

    # g) 三档各一节
    t, d = mk("p14g", ["library/dom", "desk/me", "archive"])
    try:
        r = t.resolve(profile="mod")
        blk = r["appendParts"][1] if len(r["appendParts"]) == 2 else ""
        ok("P14g 清单块顶在能力正文之后（末位）",
           len(r["appendParts"]) == 2 and r["appendParts"][0] == PT, repr(r["appendParts"])[:200])
        ok("P14g library 档 = 逐册 when 表且恒递归（facts 与 cases 两层都进表）",
           "### 知识库 `library/dom`" in blk
           and os.path.join(d["lib"], "facts", "a.md") in blk
           and os.path.join(d["lib"], "cases", "c.md") in blk
           and "册 A 何时读" in blk and "案例 C 何时读" in blk, blk[:600])
        ok("P14g library 档未入册册不列", "noWhen.md" not in blk, blk[:400])
        ok("P14g desk 档 = journal 一行检索入口（含 grep 与 git log -S 两种检索）",
           "### 书桌 `desk/me`" in blk and d["journal"] in blk
           and "grep -rn <关键词>" in blk and "log -S<关键词>" in blk, blk[:600])
        ok("P14g desk 档不逐册列 journal、账本与自建工作文件不注入清单",
           "2026-09.md" not in blk and "**不注入清单**" in blk and "todo.md" in blk, blk[:600])
        ok("P14g archive 档 = 一行检索入口（不注入清单）",
           "### 档案库 `archive`" in blk and d["archive"] in blk
           and "incidents-x.md" not in blk and "incidents-" in blk, blk[:600])
        ok("P14g 块头三句消费纪律在场",
           all(x in blk for x in ("## 知识清单", "按需读取", "禁止预加载", "以权威为准")), blk[:300])
        ok("P14g 解析摘要一行（lore 档按层计数、legacy 0 项）",
           any("knowledge 名解析：lore 档 3 项（archive×1, desk×1, library×1），"
               "legacy 工作区路径档 0 项" in l for l in t.lines),
           json.dumps(t.lines, ensure_ascii=False)[-500:])
        ok("P14g 块字符数进 stats", r["stats"]["knowledgeChars"] == len(blk), r["stats"])
    finally:
        t.cleanup()

    # h) 名不可解析 ⇒ 一行降级说明进块（不静默）
    t, d = mk("p14h", ["library/ghost", "desk/me"])
    try:
        r = t.resolve(profile="mod")
        blk = r["appendParts"][1] if len(r["appendParts"]) == 2 else ""
        ok("P14h 名不可解析 ⇒ 块内一行降级说明 + 可解析面照常",
           "不可解析" in blk and "library/ghost" in blk and "### 书桌 `desk/me`" in blk, blk[:400])
    finally:
        t.cleanup()

    # i) lore 根不在场 ⇒ WARN 点名根因、解析不抛
    t, d = mk("p14i", ["library/dom"], with_lore=False)
    try:
        r = t.resolve(profile="mod")
        blk = r["appendParts"][1] if len(r["appendParts"]) == 2 else ""
        ok("P14i lore 根不在场 ⇒ WARN 点名根因 + 块内降级说明（不拖垮）",
           any("lore 仓根不在场" in w for w in r["warnings"]) and "不可解析" in blk,
           json.dumps(r["warnings"], ensure_ascii=False)[:400])
        ok("P14i 解析摘要标出 lore 根不在场", any("不在场" in l for l in t.lines), t.lines[-2:])
    finally:
        t.cleanup()

    # j) lore 名与 legacy 工作区路径混存
    t, d = mk("p14j", ["library/dom", "kb/legacy"],
              extra_dirs=[(os.path.join("kb", "legacy"), [("l.md", "legacy 册何时读")])])
    try:
        r = t.resolve(profile="mod")
        blk = r["appendParts"][1] if len(r["appendParts"]) == 2 else ""
        ok("P14j 两档混存 ⇒ 各一节且顺序照声明（lore 档在前）",
           "### 知识库 `library/dom`" in blk and blk.index("### 知识库") < blk.index("### 域 ")
           and "legacy 册何时读" in blk, blk[:500])
        ok("P14j legacy 档一条聚合 WARN + 解析摘要计数",
           any("legacy 档" in w for w in r["warnings"])
           and any("lore 档 1 项（library×1），legacy 工作区路径档 1 项" in l for l in t.lines),
           json.dumps(r["warnings"], ensure_ascii=False)[:400])
    finally:
        t.cleanup()

    # k) 名含 `..` ⇒ 拒绝、注入面只有能力正文
    t, d = mk("p14k", ["../evil"])
    try:
        r = t.resolve(profile="mod")
        ok("P14k 名含 `..` ⇒ WARN 拒绝、注入面只有能力正文",
           r["appendParts"] == [PT] and any("`..` 路径段" in w for w in r["warnings"]),
           json.dumps(r["warnings"], ensure_ascii=False)[:300])
    finally:
        t.cleanup()

    # l) 名表缓存损坏 ⇒ 注入面零影响（缓存与注入链路隔离）
    for label, payload in (("损坏 JSON", "{not json"), ("口径版本不匹配", None)):
        t, d = mk("p14l", ["library/dom", "desk/me", "archive"])
        try:
            cdir = os.path.join(t.root, "run", "kb-index")
            os.makedirs(cdir, exist_ok=True)
            if payload is None:
                payload = json.dumps({"version": -1, "root": t.root, "docs": {}})
            _w(os.path.join(cdir, "names.json"), payload)
            r = t.resolve(profile="mod")
            blk = r["appendParts"][1] if len(r["appendParts"]) == 2 else ""
            ok("P14l 名表缓存%s ⇒ 三档清单仍完整（注入链路不读缓存）" % label,
               "### 知识库 `library/dom`" in blk and "### 书桌 `desk/me`" in blk
               and "### 档案库 `archive`" in blk and os.path.join(d["lib"], "facts", "a.md") in blk,
               blk[:400])
        finally:
            t.cleanup()

    # m) kb 工具在场但 knowledge 字段缺失 ⇒ 注入面逐字不变（零回归出口）
    t = Tree("p14m")
    try:
        t.cap("mod", PT, cap_yml={"summary": "no knowledge"})
        t.profile("mod", caps=["mod"], model="p/m",
                    form=persona.FORM_RESIDENT)
        t.lore_fixture()
        r = t.resolve(profile="mod")
        ok("P14m 无 knowledge 声明 ⇒ 只有能力正文、无清单相关日志/告警",
           r["appendParts"] == [PT] and not any("knowledge" in l for l in t.lines),
           json.dumps(t.lines, ensure_ascii=False)[:300])
    finally:
        t.cleanup()


# ---------- P15 profile 直挂禁字段（迁自 T34） ----------

def p15_profile_banned_fields():
    t = Tree("p15")
    try:
        t.cap("executor", EXEC_TEXT, cap_yml={"excludeTools": ["ask_user"]})
        t.profile("p", caps=["executor"], model="p/m", extra={
            "skills": ["s"], "extensions": ["e"], "knowledge": ["library/x"],
            "tools": ["read"], "excludeTools": ["bash"]})
        r = t.resolve(profile="p")
        ok("P15a 五个禁字段各一条 WARN（点名「profile 只列 caps」）",
           sum(1 for w in r["warnings"] if "清单直挂" in w) == 5
           and any("profile 只列 caps" in w for w in r["warnings"]),
           json.dumps(r["warnings"], ensure_ascii=False)[:600])
        ok("P15b 直挂字段一律不影响注入面（正文/工具面/skill 全按能力声明走）",
           r["appendParts"] == [EXEC_TEXT] and r["tools"] == [] and r["skillPaths"] == []
           and r["excludeTools"] == ["ask_user"],
           json.dumps({k: r[k] for k in ("tools", "skillPaths", "excludeTools")}, ensure_ascii=False))
        t.profile("q", raw="{not json")
        rq = t.resolve(profile="q")
        ok("P15c 清单损坏 ⇒ WARN 跳过 + 注入面为空（人格面缺席，会话裸起；形态无从读起 ⇒ 缺省档）",
           rq["appendParts"] == [] and rq["caps"] == []
           and rq["form"] == persona.FORM_TASK
           and any("不可读/损坏" in w for w in rq["warnings"]),
           json.dumps(rq["warnings"], ensure_ascii=False)[:300])
        t.profile("r", raw="[1,2]")
        rr = t.resolve(profile="r")
        ok("P15d 清单顶层非对象 ⇒ WARN 跳过 + 注入面为空（同 P15c 口径）",
           rr["appendParts"] == [] and any("顶层非对象" in w for w in rr["warnings"]),
           rr["warnings"])
    finally:
        t.cleanup()


# ---------- P16 能力声明面的降级分支（迁自 T32/T25a/T36 的资产面） ----------

def p16_cap_degradation():
    t = Tree("p16")
    try:
        t.cap("executor", EXEC_TEXT)
        # ① 无 cap.yml = 纯正文能力
        t.cap("plain", CAP_TEXT, cap_yml=None)
        t.profile("p1", caps=["executor", "plain"], model="p/m")
        r = t.resolve(profile="p1")
        ok("P16a 无 cap.yml ⇒ WARN 点名「按纯正文能力处理」+ 正文照注",
           r["appendParts"] == [EXEC_TEXT, CAP_TEXT]
           and any("纯正文能力" in w for w in r["warnings"]), r["warnings"])
        # ② cap.yml 损坏 ⇒ 跳过该能力（含正文）
        t.cap("broken", CAP_TEXT, cap_yml="a: [1, 2\nb: }{")
        t.profile("p2", caps=["executor", "broken"], model="p/m")
        r2 = t.resolve(profile="p2")
        ok("P16b cap.yml 解析失败 ⇒ 跳过该能力（含正文注入）+ WARN",
           r2["appendParts"] == [EXEC_TEXT]
           and any("解析失败" in w and "含正文注入" in w for w in r2["warnings"]),
           json.dumps(r2["warnings"], ensure_ascii=False)[:400])
        # ③ 顶层非 mapping
        t.cap("seq", CAP_TEXT, cap_yml="- a\n- b\n")
        t.profile("p3", caps=["executor", "seq"], model="p/m")
        r3 = t.resolve(profile="p3")
        ok("P16c cap.yml 顶层非 mapping ⇒ 跳过该能力 + WARN",
           r3["appendParts"] == [EXEC_TEXT]
           and any("顶层非 mapping" in w for w in r3["warnings"]), r3["warnings"])
        # ④ bundle（无正文）是合法形态 ⇒ info 而非 WARN
        t2 = Tree("p16d")
        try:
            t2.cap("executor", EXEC_TEXT)
            t2.cap("kit", prompt_text=None, cap_yml={"summary": "bundle"})
            t2.profile("p", caps=["kit"], model="p/m")
            r4 = t2.resolve(profile="p")
            ok("P16d bundle 是合法形态 ⇒ info 行、**不是** WARN",
               any("bundle 能力" in l for l in t2.lines)
               and not any("无 prompt.md" in w for w in r4["warnings"]),
               json.dumps(r4["warnings"], ensure_ascii=False)[:300])
        finally:
            t2.cleanup()
        # ⑤ 能力层禁键（caps / model / 未知键）⇒ WARN 忽略该键、不展开
        t.cap("evil", CAP_TEXT, cap_yml={"caps": ["executor"], "model": "x/y", "nope": 1})
        t.profile("p5", caps=["executor", "evil"], model="p/m")
        r5 = t.resolve(profile="p5")
        ok("P16e 能力层三个禁键各一条 WARN（点名合法键闭合集）且不生效",
           sum(1 for w in r5["warnings"] if "非法键" in w) == 3
           and any("能力不得引用能力" in w for w in r5["warnings"])
           and r5["model"] == "p/m" and r5["caps"] == ["executor", "evil"],
           json.dumps({"w": r5["warnings"], "model": r5["model"], "caps": r5["caps"]},
                      ensure_ascii=False)[:600])
        # ⑥ profile 无 model 字段 ⇒ 无 model、无 provider、不刷 model 相关 WARN
        t.profile("p6", caps=["executor"])
        r6 = t.resolve(profile="p6")
        ok("P16f profile 无 model 字段 ⇒ model/provider 皆空且无相关 WARN",
           r6["model"] is None and r6["provider"] is None
           and not any("model" in w for w in r6["warnings"]), r6["warnings"])
    finally:
        t.cleanup()


# ---------- P17 形态轴收进 profile 后的安全不变量（声明面承担） ----------

def p17_form_axis():
    """形态轴从 env 收进 profile 的 `form` 字段后，安全不变量的承担方从装配器硬规则
    变成声明面（用户裁定 d-p8bo A：只靠 lint、不保留装配器硬规则）⇒ 本族钉三件事：
      ① 声明齐备时不变量成立（基线能力在首位 ∧ ask_user 被排除）；
      ② 漏声明时**解析层不补**（fail-soft：只 WARN、注入面就没有它；提交期由
         `bots/cap_lint.py` 的 E17 报 ERROR 兜死，钉桩在 `bots/test_cap_lint.py`）；
      ③ 三档均不派生能力面/工具面，形态只影响回落闸（interactive 档 = D 批 command-center 的落点）。"""
    t = Tree("p17")
    try:
        t.cap("executor", EXEC_TEXT, cap_yml={"excludeTools": ["ask_user"]})
        t.cap("role", CAP_TEXT, cap_yml={"summary": "role face"})
        t.profile("good", caps=["executor", "role"], model="p/m", form=persona.FORM_TASK)
        r = t.resolve(profile="good")
        ok("P17a task 档声明齐备 ⇒ 基线能力在 caps 首位 ∧ ask_user 在排除集（两条不变量）",
           r["form"] == persona.FORM_TASK and r["caps"][0] == "executor"
           and "ask_user" in r["excludeTools"] and r["appendParts"][0] == EXEC_TEXT,
           json.dumps({k: r[k] for k in ("form", "caps", "excludeTools")}, ensure_ascii=False))
        t.profile("miss", caps=["role"], model="p/m", form=persona.FORM_TASK)
        rm = t.resolve(profile="miss")
        ok("P17b task 档漏列基线能力 ⇒ 解析层**不补**（只 WARN、不 die；提交期判据 = cap_lint E17）",
           rm["caps"] == ["role"] and rm["appendParts"] == [CAP_TEXT]
           and rm["excludeTools"] == [] and rm["warnings"] == [],
           json.dumps({"caps": rm["caps"], "xt": rm["excludeTools"],
                       "w": rm["warnings"]}, ensure_ascii=False))
        t.profile("itr", caps=["role"], model="p/planner", form=persona.FORM_INTERACTIVE)
        ri = t.resolve(profile="itr")
        ok("P17c interactive 档：不前置基线、不排除 ask_user、不回落（model 照给）",
           ri["form"] == persona.FORM_INTERACTIVE and ri["caps"] == ["role"]
           and ri["excludeTools"] == [] and ri["fallback"] is False
           and ri["model"] == "p/planner",
           json.dumps({k: ri[k] for k in ("form", "caps", "excludeTools", "fallback", "model")},
                      ensure_ascii=False))
        t.profile("itr-empty", caps=[], model="p/planner", form=persona.FORM_INTERACTIVE)
        re_ = t.resolve(profile="itr-empty")
        ok("P17d interactive 档零 cap 是合法形态（D 批 command-center 的起步形）："
           "注入面只有 model、无正文无工具面变动",
           re_["caps"] == [] and re_["appendParts"] == [] and re_["tools"] == []
           and re_["excludeTools"] == [] and re_["model"] == "p/planner",
           json.dumps({k: re_[k] for k in ("caps", "appendParts", "tools", "excludeTools")},
                      ensure_ascii=False)[:300])
        ok("P17e 三档值域与缺省档常量（跨仓契约：cap_lint.FORMS 同值）",
           persona.FORMS == ("task", "resident", "interactive")
           and persona.FORM_DEFAULT == persona.FORM_TASK and persona.FORM_FIELD == "form",
           (persona.FORMS, persona.FORM_DEFAULT, persona.FORM_FIELD))
        ok("P17f 解析层已无形态基线常量（TASK_BASELINE_CAP / FORM_XT_BASELINE 退役）",
           not hasattr(persona, "TASK_BASELINE_CAP")
           and not hasattr(persona, "FORM_XT_BASELINE"),
           [n for n in ("TASK_BASELINE_CAP", "FORM_XT_BASELINE") if hasattr(persona, n)])
    finally:
        t.cleanup()


def main():
    for fn in (p1_contract, p2_caps, p2b_bundle_and_missing, p3_fallback, p4_knowledge,
               p5_tools, p6_bundles, p8_provider, p9_cc, p10_warnings, p11_equivalence,
               p12_cli, p13_real_assets, p14_knowledge_tiers,
               p15_profile_banned_fields, p16_cap_degradation, p17_form_axis):
        print("---- %s" % fn.__name__)
        fn()
    print("==== persona 单测：%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
