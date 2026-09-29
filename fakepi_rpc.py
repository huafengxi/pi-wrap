#!/usr/bin/env python3
"""fakepi_rpc.py — pi --mode rpc 最小协议模拟（test_wrap.py 专用）。

模式（环境变量 FAKE_MODE）：
  ok           prompt → 回执 + 轮次事件 + agent_settled；follow_up / steer → 回执 +
               queue_update 非空（分别落 followUp / steering 队列）+ 第二轮次 + 再 settled
               （收敛竞态窗取消路径；steer = 立即介入当前轮的注入面）
  crash        启动即炸（写一行 stderr 后 exit 3）
  reject       prompt 回执 success:false
  idle_settle  启动 0.3s 后自发 agent_settled（幂等跳过投递后的收敛路径）
  hang_settle  prompt 受理论正常轮次但永不 agent_settled（长活会话，透传测试用）
  selfkill     自发 agent_settled 后 0.5s exit 7（resident 退出码透传断言）
  fake_settled_inflight  复现假 settled 场景：prompt 受理后发 agent_start →
               假 agent_settled（inflight>0，无配对 agent_end）→ 真轮次事件 → agent_end →
               真 agent_settled；修前 wrap 在假 settled 后收敛（早退），修后等真 settled
  child_race   子端收件扩展 + 就绪门**健康路径**（🔴0）：boot 起一个模拟 receiver-child
               的线程，等 wrap 的 init-ok 标记（env AGENTD_WRAP_INIT_OK）后才 drain 自家 inbox；
               prompt 正常起轮并落 session.jsonl（user+assistant），门开后排队的注入也落树
  child_race_reject       **形态① exit 1 秒死**：无视就绪门（boot 即注入起轮、占住 streaming）→
               prompt 回执 success:false + pi 逐字错误串（agent-session.js:833）→ wrap 判
               stage=prompt_rejected，session.jsonl 不落盘
  child_race_fakesuccess  **形态② exit 0 假成功**：无视就绪门，注入轮只有 user 事件无 assistant →
               pi 的 no-assistant guard 使 session.jsonl 永不落盘，但 agent_settled 照发 →
               prompt 被接受却零轮次产出 → wrap 误判收敛 exit 0
环境变量 FAKE_FLAG_DIR：收到 prompt / follow_up / steer 时落标记文件（断言用）；
启动时把 argv 写入 <FLAG_DIR>/argv（resident argv 断言）。
child_race 族另落时序标记（gate_open / gate_bypassed / child_inject_<N> /
prompt_rejected_race / prompt_accepted_no_turn）供「注入 vs prompt」先后断言；
FAKE_CHILD_HONOR_GATE=0 强制无视就绪门（A/B 对照），FAKE_CHILD_NO_ARM=1 不回写 recv-armed
（wrap 的有界等待断言），FAKE_CHILD_GATE_TIMEOUT 调门等待上界（秒，缺省 30）。
仅标准库；行协议 = pi rpc 子集。
"""
import json
import os
import sys
import threading
import time

MODE = os.environ.get("FAKE_MODE", "ok")
FLAG_DIR = os.environ.get("FAKE_FLAG_DIR", "")

sys.stderr.write("fake-pi boot (mode=%s)\n" % MODE)
sys.stderr.flush()

if MODE == "crash":
    sys.stderr.write("fake-pi FATAL boom\n")
    sys.stderr.flush()
    sys.exit(3)


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def flag(name):
    if FLAG_DIR:
        try:
            with open(os.path.join(FLAG_DIR, name), "a") as f:
                f.write("1\n")
        except OSError:
            pass


if FLAG_DIR:
    try:
        with open(os.path.join(FLAG_DIR, "argv"), "w") as f:
            f.write(json.dumps(sys.argv) + "\n")
    except OSError:
        pass
    # 就绪门 env 快照：断言 wrap 是否把两枚标记路径传给了 pi 侧
    # （任务形态传 / resident 不传 / receiver-child 缺失不传）。
    try:
        with open(os.path.join(FLAG_DIR, "gate_env"), "w") as f:
            f.write(json.dumps({
                "init_ok": os.environ.get("AGENTD_WRAP_INIT_OK", ""),
                "recv_armed": os.environ.get("AGENTD_WRAP_RECV_ARMED", ""),
                "mode": MODE}) + "\n")
    except OSError:
        pass


def turn(rid, command, settle=True):
    emit({"id": rid, "type": "response", "command": command, "success": True})
    emit({"type": "agent_start"})
    emit({"type": "message_start", "message": {"role": "user"}})
    emit({"type": "message_start", "message": {"role": "assistant"}})
    emit({"type": "agent_end"})
    if settle:
        emit({"type": "agent_settled"})


# ---------- 子端收件扩展模拟（🔴0：P0 spawn 竞态的就绪门与两形态） ----------

RACE_MODES = ("child_race", "child_race_reject", "child_race_fakesuccess")
RACE = MODE in RACE_MODES
RACE_LOCK = threading.Lock()
# 就绪门开关在**模块载入时**算定（不放到注入线程里算：否则 prompt 可能先于线程到达，
# 读到未初始化的 bypassed → A/B 不可重现）。缺省：child_race = 尊重就绪门（修后形态）；
# 两个形态模式 = 绕过门（复现 v41 的抢跑）。显式 FAKE_CHILD_HONOR_GATE 覆盖缺省
# = 变异法对照（同一模式下只差「是否尊重门」，其余逐字相同）。
RACE_HONOR = os.environ.get(
    "FAKE_CHILD_HONOR_GATE", "1" if MODE == "child_race" else "0") != "0"
RACE_BYPASSED = not (os.environ.get("AGENTD_WRAP_INIT_OK", "") and RACE_HONOR)
RACE_STATE = {"streaming": False, "queue": [], "entries": [],
              "bypassed": RACE_BYPASSED}


def _session_arg():
    """从 argv 取 --session 路径（同真 pi：wrap 的 build_argv 传它）。"""
    for i, a in enumerate(sys.argv):
        if a == "--session" and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return ""


def race_persist():
    """按 pi session-manager 的 **no-assistant guard** 落盘（session-manager.js::_persist）：
    只有 user 事件时不写文件（实证：exit 0 假成功且 session/ 是空目录）。"""
    entries = RACE_STATE["entries"]
    if not any((e.get("message") or {}).get("role") == "assistant" for e in entries):
        return False
    p = _session_arg()
    if not p:
        return False
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


def race_child_receiver():
    """模拟 receiver-child.ts：session_start 即 drain 自家 inbox（认领 = 扁平 ack）。
    就绪门（env AGENTD_WRAP_INIT_OK 在场且 FAKE_CHILD_HONOR_GATE != 0）→ 等 wrap 写完 init-ok
    才注入；否则 boot 即注入 = 复现 P0 竞态（pi sendUserMessage 空闲时直接起轮）。
    类型白名单同真扩展：reply 不认领（🔴2）。所有等待均有界（假活防线）。"""
    home = os.environ.get("AGENT_HOME", "")
    inbox = os.path.join(home, "inbox")
    ackdir = os.path.join(inbox, "ack")
    gate = os.environ.get("AGENTD_WRAP_INIT_OK", "")
    bypassed = RACE_BYPASSED
    if not bypassed:
        try:
            bound = float(os.environ.get("FAKE_CHILD_GATE_TIMEOUT", "30"))
        except ValueError:
            bound = 30.0
        deadline = time.time() + bound
        while time.time() < deadline and not os.path.exists(gate):
            time.sleep(0.02)
        flag("gate_open")
        # 标记内容快照（断言 wrap 的三条成功路各自自证 why）
        try:
            with open(gate, encoding="utf-8") as gf:
                doc = gf.read()
        except OSError:
            doc = ""
        if FLAG_DIR:
            try:
                with open(os.path.join(FLAG_DIR, "init_ok_doc"), "w",
                          encoding="utf-8") as f:
                    f.write(doc)
            except OSError:
                pass
    else:
        flag("gate_bypassed")
    texts = []
    try:
        names = sorted(f for f in os.listdir(inbox) if f.endswith(".msg"))
    except OSError:
        names = []
    for fn in names:
        mid = fn[:-4]
        try:
            with open(os.path.join(inbox, fn), encoding="utf-8") as mf:
                doc = json.load(mf)
        except (OSError, ValueError):
            continue
        # 🔴2 类型白名单**在认领之前**（同真扩展 core.drainInboxDir 的 opts.types）：reply 的
        # 权威消费者 = ask-user-child 的 waitForReply+writeAck → 零注入、零 ack、原件留存。
        if not isinstance(doc, dict) or doc.get("type") not in ("inform", "ask"):
            continue
        if os.path.exists(os.path.join(ackdir, mid)):
            continue                        # 已送达
        try:
            os.makedirs(ackdir, exist_ok=True)
            with open(os.path.join(ackdir, mid), "x", encoding="utf-8") as af:
                af.write(json.dumps({"id": mid}) + "\n")
        except OSError:
            continue                        # 并发认领失败（O_EXCL 语义）
        body = doc.get("body")
        texts.append(body if isinstance(body, str) else json.dumps(body, ensure_ascii=False))
    flag("child_inject_%d" % len(texts))
    armed = os.environ.get("AGENTD_WRAP_RECV_ARMED", "")
    if armed and os.environ.get("FAKE_CHILD_NO_ARM", "0") != "1":
        try:
            os.makedirs(os.path.dirname(armed), exist_ok=True)
            with open(armed, "w", encoding="utf-8") as f:
                f.write(json.dumps({"pid": os.getpid()}) + "\n")
            flag("recv_armed_written")
        except OSError:
            pass
    with RACE_LOCK:
        RACE_STATE["queue"].extend(texts)
        if bypassed:
            # 抢跑形态：注入轮先起（只有 user 事件 → no-assistant guard 不落盘）
            RACE_STATE["streaming"] = MODE == "child_race_reject"
            RACE_STATE["entries"].append(
                {"type": "message",
                 "message": {"role": "user", "content": texts[:1]}})
    if not bypassed:
        pass                                # 门生效：注入进队列，由 prompt 轮吸收后落树
    elif MODE == "child_race_fakesuccess":
        race_persist()                      # 恒 False（无 assistant）= session.jsonl 不落盘
        emit({"type": "agent_start"})
        emit({"type": "message_start", "message": {"role": "user"}})
        emit({"type": "agent_end"})
        emit({"type": "agent_settled"})     # → wrap 误判任务收敛（exit 0 假成功）
    elif MODE == "child_race_reject":
        def _end_turn():
            time.sleep(1.5)                 # 注入轮占住 streaming，覆盖 prompt 到达时刻
            with RACE_LOCK:
                RACE_STATE["streaming"] = False
            emit({"type": "agent_end"})
            emit({"type": "agent_settled"})
        threading.Thread(target=_end_turn, daemon=True).start()


if RACE and RACE_BYPASSED:
    # 绕过就绪门 = 复现真形态的抢跑：真 pi 的扩展 session_start 在 boot 阶段就 fires，而 wrap 的
    # prompt 行要等 pi 开始读 stdin 才被处理 → 注入**恒赢**。故同步跑完再进 stdin 循环（确定性，
    # 不靠线程调度抛胜算）。
    race_child_receiver()
elif RACE:
    threading.Thread(target=race_child_receiver, daemon=True).start()

if MODE in ("idle_settle", "selfkill"):
    def _settle():
        time.sleep(0.3)
        emit({"type": "agent_settled"})
        if MODE == "selfkill":
            time.sleep(0.5)
            os._exit(7)   # 非预期退出（：resident 退出码透传断言）
    threading.Thread(target=_settle, daemon=True).start()

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        cmd = json.loads(line)
    except ValueError:
        continue
    t = cmd.get("type")
    rid = cmd.get("id")
    if t == "prompt":
        flag("prompt")
        if MODE == "reject":
            emit({"id": rid, "type": "response", "command": "prompt",
                  "success": False, "error": "fake rejected"})
            continue
        if RACE:
            with RACE_LOCK:
                streaming = RACE_STATE["streaming"]
            if streaming:
                # 形态①：pi 逐字错误串（agent-session.js:833）→ wrap 判 stage=prompt_rejected
                emit({"id": rid, "type": "response", "command": "prompt",
                      "success": False,
                      "error": "Agent is already processing. Specify streamingBehavior "
                               "('steer' or 'followUp') to queue the message."})
                flag("prompt_rejected_race")
                continue
            with RACE_LOCK:
                bypassed = RACE_STATE["bypassed"]
            if MODE == "child_race_fakesuccess" and bypassed:
                # 形态②：接受但永不起轮（现网 日志形态：初始 prompt 已投递并被接受 →
                # 完成收敛 agent_settled ∧ 队列空 → exit 0，session.jsonl 不存在）
                emit({"id": rid, "type": "response", "command": "prompt",
                      "success": True})
                flag("prompt_accepted_no_turn")
                continue
            # 健康路径：起轮 → 落 user+assistant → 吸收就绪门开后排队的注入（也落树）
            emit({"id": rid, "type": "response", "command": "prompt",
                  "success": True})
            emit({"type": "agent_start"})
            emit({"type": "message_start", "message": {"role": "user"}})
            with RACE_LOCK:
                RACE_STATE["streaming"] = True
                RACE_STATE["entries"].append(
                    {"type": "message",
                     "message": {"role": "user",
                                 "content": [cmd.get("message", "")]}})
            time.sleep(0.4)                 # 模型往返（此窗口内子端应已开门并排队注入）
            deadline = time.time() + 3.0    # 有界等注入排队（假活防线）
            while time.time() < deadline:
                with RACE_LOCK:
                    if RACE_STATE["queue"]:
                        break
                time.sleep(0.05)
            with RACE_LOCK:
                q = list(RACE_STATE["queue"])
                RACE_STATE["queue"] = []
                RACE_STATE["entries"].append(
                    {"type": "message",
                     "message": {"role": "assistant",
                                 "content": ["fake assistant reply"]}})
                for text in q:
                    RACE_STATE["entries"].append(
                        {"type": "message",
                         "message": {"role": "user", "content": [text]}})
            for _t in q:
                emit({"type": "message_start", "message": {"role": "user"}})
            race_persist()
            flag("race_landed_%d" % len(q))
            with RACE_LOCK:
                RACE_STATE["streaming"] = False
            emit({"type": "agent_end"})
            emit({"type": "agent_settled"})
            continue
        if MODE == "fake_settled_inflight":
            #：复现证据面事件序（fake-idle-late-*/rpc.jsonl）
            # 关键场景：假 settled 在所有 activity 事件之后发出，后 > SETTLE_WINDOW（0.5s）
            # 才发 agent_end。修前：竞态窗过期（无 activity）→ 收敛（早退）；
            # 修后：inflight>0 → 跳过收敛判定 → 等 agent_end → 真 settled 后收敛。
            # 用线程延迟发真 settled，主线程继续读 stdin（可检测 EOF）。
            emit({"id": rid, "type": "response", "command": "prompt", "success": True})
            emit({"type": "agent_start"})            # inflight → 1
            emit({"type": "turn_start"})
            emit({"type": "message_start", "message": {"role": "user"}})  # activity
            emit({"type": "message_end"})
            emit({"type": "message_start", "message": {"role": "assistant"}})
            emit({"type": "message_update"})         # assistant 正在工作
            # 假 settled：在 activity 事件之后发出（竞态窗内无新 activity）
            emit({"type": "agent_settled"})          # 假 settled（inflight=1，无配对 agent_end）
            flag("fake_settled_emitted")
            def _delayed_real_settled():
                time.sleep(1.0)                      # > SETTLE_WINDOW(0.5s)
                emit({"type": "message_update"})
                emit({"type": "message_end"})
                emit({"type": "turn_end"})
                emit({"type": "agent_end"})          # inflight → 0
                emit({"type": "agent_settled"})      # 真 settled（inflight=0）
                flag("real_settled_emitted")
            threading.Thread(target=_delayed_real_settled, daemon=True).start()
            # 主线程继续读 stdin：修前 wrap 关 stdin → EOF → 退出；修后等真 settled
            continue
        turn(rid, "prompt", settle=MODE != "hang_settle")
    elif t == "follow_up":
        flag("follow_up")
        emit({"id": rid, "type": "response", "command": "follow_up",
              "success": True})
        emit({"type": "queue_update", "steering": [],
              "followUp": [cmd.get("message", "x")]})
        time.sleep(0.1)
        emit({"type": "queue_update", "steering": [], "followUp": []})
        emit({"type": "message_start", "message": {"role": "user"}})
        emit({"type": "agent_end"})
        emit({"type": "agent_settled"})
    elif t == "steer":
        # 与 follow_up 同形，只换队列：steer 进 steering 队列（真实 pi 在当前轮下一个工具
        # 调用间隙注入），wrap 侧 queue_busy/竞态窗判定口径一致（steering 或 followUp 非空即忙）。
        flag("steer")
        emit({"id": rid, "type": "response", "command": "steer",
              "success": True})
        emit({"type": "queue_update", "steering": [cmd.get("message", "x")],
              "followUp": []})
        time.sleep(0.1)
        emit({"type": "queue_update", "steering": [], "followUp": []})
        emit({"type": "message_start", "message": {"role": "user"}})
        emit({"type": "agent_end"})
        emit({"type": "agent_settled"})
    elif t == "get_entries":
        emit({"id": rid, "type": "response", "command": "get_entries",
              "success": True,
              "data": {"entries": [
                  {"id": "e1", "type": "session"},
                  {"id": "e2", "parentId": "e1", "type": "message",
                   "message": {"role": "user"}}], "leafId": "e2"}})
    elif t == "get_state":
        emit({"id": rid, "type": "response", "command": "get_state",
              "success": True, "data": {}})
    else:
        emit({"id": rid, "type": "response", "command": t, "success": True})
# stdin EOF → 优雅退出（对齐 pi 语义）
sys.exit(0)
