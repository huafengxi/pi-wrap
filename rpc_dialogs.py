"""In-memory RPC dialog baseline for pi-host socket attach only.

This is not a task-tab wait detector. Native internal signal settlement is not
observable on the unmodified RPC stream; no state file or session entry is
written here. Explicit responses, deadlines and tool/turn completion retain
existing replay semantics without inventing a persistent observation protocol.
"""
import math
import threading
import time

DIALOG_METHODS = {"select", "confirm", "input", "editor"}


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


class DialogRequests:
    def __init__(self):
        self.lock = threading.RLock()
        self.tools = []
        self.turns = 0
        self.waits = {}

    def event(self, obj):
        with self.lock:
            t, call = obj.get("type"), obj.get("toolCallId")
            if t == "agent_start":
                self.turns += 1
            elif t == "tool_execution_start" and call:
                self.tools.append(call)
            elif t == "extension_ui_request" and obj.get("method") in DIALOG_METHODS and obj.get("id"):
                now = time.time()
                timeout = obj.get("timeout")
                self.waits["dialog:" + obj["id"]] = {
                    "request": dict(obj),
                    "deadline": now + timeout / 1000 if number(timeout) and timeout > 0 else None,
                    "toolCallId": self.tools[-1] if self.tools else None}
            elif t == "tool_execution_end":
                self.tools = [c for c in self.tools if c != call]
                self.waits = {k: w for k, w in self.waits.items() if w.get("toolCallId") != call}
            elif t == "agent_end":
                self.turns = max(0, self.turns - 1)
                if self.turns == 0:
                    self.tools.clear()
                    self.waits.clear()
            elif t == "agent_settled" and self.turns == 0 and not self.tools:
                self.waits.clear()

    def sent(self, obj):
        """Only called after a successful write to pi stdin."""
        with self.lock:
            t = obj.get("type")
            if t == "extension_ui_response":
                self.waits.pop("dialog:" + str(obj.get("id")), None)
            else:
                # A bare abort need not resolve ctx.ui or waitForReply promises.
                # sessiond sends explicit cancelled dialog responses first;
                # otherwise only the real tool end/deadline clears the wait.
                return

    def dialogs(self):
        """Live requests for socket attach, even if the raw ring has wrapped.

        Do not replay old/resolved dialogs or restart their timeout on attach.
        """
        with self.lock:
            now = time.time()
            out = []
            for w in self.waits.values():
                deadline = w.get("deadline")
                if number(deadline) and deadline <= now:
                    continue
                request = dict(w["request"])
                if number(deadline):
                    request["timeout"] = max(1, (deadline - now) * 1000)
                out.append(request)
            return out
