#!/usr/bin/env python3
"""harness driver —— 两档拉起形态，都带假活上界。

工作根红线：本脚本的 root 一律取 env `HARNESS_ROOT`（= 专用临时根，由 `setup.sh` 建），
绝不指向生产树（工作区根 ∨ 任何仓根）；生产资产只经软链挂进该根。`guard_root()` 的三条
断言（临时基目录前缀判定 / 拒「等于生产根」/ 拒「生产根在工作根内部」）在动手前跑，
不满足即 REFUSE 退出 2；全程不用 `ignore_errors` 类吞错。

档 1（--mode direct）：直接拉 `pi --mode rpc`，装载序由本脚本定：
  缺省 `-e 探针 -e 注入层` ⇒ 探针的 handler 先跑，读到的是注入层施加**之前**的面（机制定位用）；
  `--probe-last` ⇒ `-e 注入层 -e 探针`，读到施加/过滤**之后**的面（验收用，也是对抗档的前提）。
  prompt 由本脚本经 stdin 的 rpc `prompt` 请求投递（`--prompts N` 投 N 轮，每轮等 agent_settled），
  `--delay` 用来把「迟注册」排在首轮之前。

档 2（--mode wrap）：拉 `pi-rpc-wrap.py`（生产形态：CHILD_EXTS + sessiond 探针 + 注入层），
  另用 `AGENTD_WRAP_PI_BIN` 钩子在 argv 末尾追加本 harness 的探针扩展（运行时生成的 shim，
  不落仓 ⇒ 里面不存任何绝对路径常量）。注意 wrap 档下 pi 的 stdout 被 wrap 透传进它自己的
  unix sock，本驱动数不到 `agent_settled`（meta.json 的 `settled` 恒 0），收敛信号 = wrap 进程退出。

对抗档（`--adversary-tool <名>`，见探针头注）：模拟「第三方 package 每轮重置活动集 + 把被挡的
工具补回 payload」，用来强制触发注入层的执行面兜底（`tool_call` 拦截）。需 `--probe-last`
∨ `--mode wrap`（探针必须装载在注入层**之后**）。

输出：$HARNESS_ROOT/out/<case>/{stderr.log,stdout.jsonl,meta.json,sizes.txt}
（wrap 档的 pi stderr 由 wrap 自己写 $HARNESS_ROOT/run/agentd/<case>.stderr.log，meta.json 的
`stderr_log` 字段给出该取哪一份）。
"""
import json
import os
import subprocess
import sys
import threading
import time

R = os.environ.get("HARNESS_ROOT", "")
# 工作区根（约定值，可覆盖）：软链源、注入层与生产根发现的基准。
WS = os.environ.get("HARNESS_WS", "~/m")
# 临时基目录（可覆盖）：工作根必须在它内部。
BASE_ENV = os.environ.get("HARNESS_BASE", "")
# 注入层扩展相对工作区根的位置（= pi-rpc-wrap.py 的 PROFILE_LOADER_EXT_REL 同值；可覆盖）。
LOADER_REL = os.environ.get("HARNESS_LOADER_REL", "pi-core/agent/extensions/profile-loader.ts")
# 被测 wrap 脚本（缺省 = 本 harness 所在 checkout 的 pi-rpc-wrap.py；可覆盖）。
HARNESS_DIR = os.path.dirname(os.path.abspath(__file__))
WRAP = os.environ.get("HARNESS_WRAP", os.path.join(os.path.dirname(os.path.dirname(HARNESS_DIR)), "pi-rpc-wrap.py"))


def ws_root():
    return os.path.realpath(os.path.expanduser(WS))


def base_dir():
    if BASE_ENV:
        return os.path.realpath(os.path.expanduser(BASE_ENV))
    return os.path.realpath(os.path.join(ws_root(), "run", "temp", "toolface-harness"))


def prod_roots():
    """生产根清单：调用方注入（`HARNESS_PROD_ROOTS`，冒号分隔）∨ 现场发现
    （工作区根 + 其下每个含 `.git` 的一级子目录）。不落任何仓名单常量。"""
    injected = os.environ.get("HARNESS_PROD_ROOTS", "").strip()
    if injected:
        return [p for p in (x.strip() for x in injected.split(":")) if p]
    ws = ws_root()
    found = [ws]
    try:
        entries = sorted(os.listdir(ws))
    except OSError as e:
        print("WARN: 生产根现场发现失败 %r ⇒ 只按工作区根判定" % (e,))
        return found
    for e in entries:
        d = os.path.join(ws, e)
        if os.path.isdir(os.path.join(d, ".git")):
            found.append(os.path.realpath(d))
    return found


def guard_root():
    """root 身份断言：工作根必须在专用临时基目录内，且不得等于生产根、不得是生产根的祖先。
    （工作根**在**生产根内部 = 临时根的必然形态，放行；反向 = 拒绝。全程不用 ignore_errors 类吞错。）"""
    if not R:
        print("REFUSE: HARNESS_ROOT 未设")
        sys.exit(2)
    rr = os.path.realpath(R)
    base = base_dir()
    if not (rr == base or rr.startswith(base + os.sep)):
        print("REFUSE: HARNESS_ROOT=%s 不在临时基目录 %s 内" % (rr, base))
        sys.exit(2)
    for prod in prod_roots():
        p = os.path.realpath(os.path.expanduser(prod))
        if rr == p:
            print("REFUSE: 工作根 %s 等于生产根 %s" % (rr, p))
            sys.exit(2)
        if p.startswith(rr + os.sep):
            print("REFUSE: 生产根 %s 在工作根 %s 内部（工作根是生产根的祖先）" % (p, rr))
            sys.exit(2)
    return rr


def pump(stream, path, sizes=None, tag=""):
    """到达即写（read1 语义）：本 harness 的读端不得自己复刻被验证的缺陷。"""
    with open(path, "ab") as f:
        while True:
            try:
                d = stream.read1(65536) if hasattr(stream, "read1") else stream.read(65536)
            except (OSError, ValueError):
                break
            if not d:
                break
            f.write(d)
            f.flush()
            if sizes is not None:
                sizes.append((time.time(), tag, len(d)))


def main():
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("case")
    ap.add_argument("--mode", choices=["direct", "wrap"], default="direct")
    ap.add_argument("--form", choices=["task", "resident"], default="task")
    ap.add_argument("--profile", default="")
    ap.add_argument("--delay", type=float, default=0.0, help="投递 prompt 前的等待秒数（迟注册排在首轮之前用）")
    ap.add_argument("--late-ms", type=int, default=0, help=">0 ⇒ 探针在 session_start 后 N ms 注册 harness_late_tool")
    ap.add_argument("--adversary-tool", default="", help="对抗档：探针每轮重置活动集并把该工具补回 payload（强制触发注入层的 tool_call 拦截；需探针装载在注入层之后）")
    ap.add_argument("--tag", default="", help="探针日志行前缀 tag（多档并跑时区分输出）")
    ap.add_argument("--prompt", default="回复一行 ok。不要调用任何工具。")
    ap.add_argument("--prompts", type=int, default=1, help="投递几轮 prompt（每轮等 agent_settled；>1 用于复现「第二轮基础提示被重建」面）")
    ap.add_argument("--agent-dir", default="", help="非空 ⇒ 设 PI_CODING_AGENT_DIR（对照实验：换一个 pi agent dir，例如去掉某个 package 的 settings 副本）")
    ap.add_argument("--bare", action="store_true", help="洗掉身份/人格 env ⇒ 验注入层的零行为规则（用户裸起 pi 的面）")
    ap.add_argument("--loader", default="", help="非空 ⇒ 用该文件当注入层（修前对照用）")
    ap.add_argument("--probe-last", action="store_true", help="直驱档把探针排在注入层之后（⇒ 探针读到过滤后的 payload）")
    ap.add_argument("--timeout", type=float, default=180.0, help="整轮上界（假活防线）")
    ap.add_argument("--settle-timeout", type=float, default=120.0, help="等 agent_settled 的上界")
    ap.add_argument("--sample-every", type=float, default=2.0)
    args = ap.parse_args()

    root = guard_root()
    if args.adversary_tool and not (args.probe_last or args.mode == "wrap"):
        print("REFUSE: --adversary-tool 需要探针装载在注入层之后（--probe-last ∨ --mode wrap）")
        sys.exit(2)
    outdir = os.path.join(root, "out", args.case)
    os.makedirs(outdir, exist_ok=True)
    home = os.path.join(root, "agents", "task", args.case)
    os.makedirs(os.path.join(home, "session"), exist_ok=True)
    os.makedirs(os.path.join(home, "inbox"), exist_ok=True)
    with open(os.path.join(home, "prompt.md"), "w", encoding="utf-8") as f:
        f.write(args.prompt + "\n")
    session_file = os.path.join(home, "session", "session.jsonl")
    if os.path.exists(session_file):
        os.unlink(session_file)

    env = dict(os.environ)
    env["AGENT_ROOT"] = root
    env["AGENT_HOME"] = home
    env["AGENT_SELF"] = "task/" + args.case
    if args.bare:
        # 零行为档：无 profile ∧ 非 agentd 监督（判据 = AGENT_SELF/AGENTD_RESIDENT+SESSION_NAME）
        for k in ("AGENT_SELF", "AGENTD_RESIDENT", "AGENTD_SESSION_NAME", "DISPATCH_PROFILE"):
            env.pop(k, None)
    env.pop("DISPATCH_PROFILE", None)
    if args.profile:
        env["DISPATCH_PROFILE"] = args.profile
    env.pop("AGENTD_RESIDENT", None)
    env.pop("AGENTD_SESSION_NAME", None)
    if args.form == "resident":
        env["AGENTD_RESIDENT"] = "1"
        env["AGENTD_SESSION_NAME"] = "hz-" + args.case
    env["HARNESS_LATE_REG_MS"] = str(args.late_ms)
    env["HARNESS_ADVERSARY_TOOL"] = args.adversary_tool
    env["HARNESS_PROBE_TAG"] = args.tag
    if args.agent_dir:
        env["PI_CODING_AGENT_DIR"] = args.agent_dir
    else:
        env.pop("PI_CODING_AGENT_DIR", None)
    env.pop("AGENTD_CONTEXT_COMPACTION", None)

    probe = os.path.join(root, "probe", "toolface-probe.ts")
    loader = os.path.join(root, LOADER_REL)
    if args.loader:
        loader = args.loader
    for need in (probe, loader):
        if not os.path.exists(need):
            print("REFUSE: 缺件 %s（工作根没建好？跑 setup.sh）" % need)
            sys.exit(2)
    pi_bin = subprocess.run(["bash", "-lc", "command -v pi"], capture_output=True, text=True).stdout.strip()
    if not pi_bin:
        print("REFUSE: PATH 上找不到 pi")
        sys.exit(2)

    if args.mode == "direct":
        # 装载序 = handler 执行序：探针在前 ⇒ 读到注入层施加**之前**的面（机制定位用）；
        # --probe-last ⇒ 读到施加/过滤**之后**的面（修后验收 ∨ 对抗档用）。
        exts = ["-e", loader, "-e", probe] if args.probe_last else ["-e", probe, "-e", loader]
        argv = [pi_bin, "--mode", "rpc", "--session", session_file, "-n", args.case] + exts
    else:
        if not os.path.exists(WRAP):
            print("REFUSE: 找不到被测 wrap 脚本 %s（设 HARNESS_WRAP 覆盖）" % WRAP)
            sys.exit(2)
        # shim 运行时生成（不落仓）：pi 的可执行路径现场 `command -v` 取，探针路径按工作根拼
        # ⇒ 仓里不存任何机器专有的绝对路径。
        shim = os.path.join(root, "pi-shim.sh")
        with open(shim, "w", encoding="utf-8") as f:
            f.write("#!/usr/bin/env bash\n# harness pi shim：在 wrap 组好的 argv 末尾追加探针扩展\nexec %s \"$@\" -e %s\n" % (pi_bin, probe))
        os.chmod(shim, 0o755)
        env["AGENTD_WRAP_PI_BIN"] = shim
        argv = ["python3", WRAP]

    sizes = []
    sizes_lock = threading.Lock()
    t0 = time.time()
    err_path = os.path.join(outdir, "stderr.log")
    open(err_path, "wb").close()
    p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, env=env, cwd=root,
                         start_new_session=True)
    threading.Thread(target=pump, args=(p.stderr, err_path), kwargs={"sizes": sizes, "tag": "stderr"}, daemon=True).start()

    stop = threading.Event()
    state = {"settled": 0}
    out_f = open(os.path.join(outdir, "stdout.jsonl"), "wb")

    def read_stdout():
        while True:
            try:
                d = p.stdout.read1(65536) if hasattr(p.stdout, "read1") else p.stdout.read(65536)
            except (OSError, ValueError):
                break
            if not d:
                break
            out_f.write(d)
            out_f.flush()
            state["settled"] += d.count(b'"agent_settled"')

    threading.Thread(target=read_stdout, daemon=True).start()

    def sampler():
        # 活会话期取样：pi stderr 日志（wrap 档 = run/agentd/<case>.stderr.log；直驱档 = out/<case>/stderr.log）
        target = os.path.join(root, "run", "agentd", args.case + ".stderr.log") if args.mode == "wrap" else err_path
        with open(os.path.join(outdir, "sizes.txt"), "w", encoding="utf-8") as f:
            while not stop.is_set():
                try:
                    st = os.stat(target)
                    alive = p.poll() is None
                    f.write("t=%.2f alive=%s size=%d target=%s\n" % (time.time() - t0, alive, st.st_size, target))
                    f.flush()
                except OSError as e:
                    f.write("t=%.2f stat-err=%r\n" % (time.time() - t0, e))
                    f.flush()
                stop.wait(args.sample_every)

    threading.Thread(target=sampler, daemon=True).start()

    stage = "start"
    try:
        if args.delay > 0:
            stage = "delay"
            deadline = time.time() + args.delay
            while time.time() < deadline:
                if p.poll() is not None:
                    break
                time.sleep(0.2)
        for i in range(args.prompts):
            if p.poll() is not None:
                stage = "pi-exited-early"
                break
            if args.mode == "direct":
                stage = "prompt-%d" % (i + 1)
                p.stdin.write((json.dumps({"id": "p%d" % (i + 1), "type": "prompt", "message": args.prompt}, ensure_ascii=False) + "\n").encode("utf-8"))
                p.stdin.flush()
            base = state["settled"]
            stage = "wait-settled-%d" % (i + 1)
            deadline = time.time() + args.settle_timeout
            while time.time() < deadline and state["settled"] <= base:
                if p.poll() is not None:
                    break
                time.sleep(0.25)
            if state["settled"] <= base:
                stage = "settle-timeout-%d" % (i + 1)
                break
        stage = "drain"
        time.sleep(1.5)   # 让末轮事件与 stderr 走完
    finally:
        stop.set()
        stage = "shutdown"
        # 优雅退出（kill 路径会永久丢缓冲 ⇒ 先走优雅档）
        if p.poll() is None:
            if args.mode == "wrap":
                p.terminate()          # wrap 的 SIGTERM handler 关 pi stdin ⇒ pi EOF 优雅退出
            else:
                try:
                    p.stdin.close()
                except OSError:
                    pass
        grace = time.time() + 30
        while time.time() < grace and p.poll() is None:
            time.sleep(0.3)
        if p.poll() is None:
            stage = "kill-group"
            try:
                os.killpg(os.getpgid(p.pid), 15)
            except OSError:
                pass
            time.sleep(3)
            if p.poll() is None:
                try:
                    os.killpg(os.getpgid(p.pid), 9)
                except OSError:
                    pass
            p.wait(timeout=10)
        out_f.close()

    meta = {
        "case": args.case, "mode": args.mode, "form": args.form, "profile": args.profile,
        "delay": args.delay, "late_ms": args.late_ms, "root": root, "argv": argv,
        "stage": stage, "rc": p.returncode, "settled": state["settled"], "prompts": args.prompts,
        "agent_dir": args.agent_dir, "probe_last": args.probe_last, "loader": loader,
        "adversary_tool": args.adversary_tool, "wrap": WRAP if args.mode == "wrap" else "",
        "elapsed": round(time.time() - t0, 2),
        "env": {k: env.get(k) for k in ("AGENT_ROOT", "AGENT_HOME", "AGENT_SELF", "DISPATCH_PROFILE",
                                        "AGENTD_RESIDENT", "AGENTD_SESSION_NAME", "HARNESS_LATE_REG_MS",
                                        "HARNESS_ADVERSARY_TOOL", "HARNESS_PROBE_TAG", "AGENTD_WRAP_PI_BIN")},
        "stderr_log": os.path.join(root, "run", "agentd", args.case + ".stderr.log") if args.mode == "wrap" else err_path,
    }
    with open(os.path.join(outdir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    print(json.dumps(meta, ensure_ascii=False))


if __name__ == "__main__":
    main()
