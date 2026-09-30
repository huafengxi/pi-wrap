/**
 * toolface-probe.ts — 工具面取证探针扩展（harness 专用；只在临时工作根里被 `-e` 装载）。
 *
 * 用途：在多个时点打印 pi 的工具面名集与 provider payload 读数，判定人格注入层的
 * excludeTools/tools 施加是否生效、「迟注册工具」是否逃过施加、以及 payload 过滤是否落到
 * 真正发给模型的 tools 上。全部输出走 stderr（与注入层同一管道 ⇒ 顺带验 stderr 泵在会话
 * 存活期即落盘）。
 *
 * 三个 env 开关（都缺省关闭 ⇒ 不设就是纯观测探针）：
 *
 * 1. `HARNESS_LATE_REG_MS` > 0：在 session_start 之后（= 扩展绑定完成、工具 registry 已建）
 *    延时 registerTool 一枚名为 harness_late_tool 的工具 —— pi 的 registerTool 会调
 *    runtime.refreshTools() → AgentSession._refreshToolRegistry()（无 options）⇒ 命中
 *    「registry 新增项自动进活动集」分支，用来验注入层对迟注册工具的处置。
 *
 * 2. `HARNESS_ADVERSARY_TOOL` = <工具名>：**对抗档**，把「强制触发注入层执行面兜底
 *    （`tool_call` 拦截）」变成不依赖模型配合的确定性 case。机制面（两条都是实测事实）：
 *    ① pi 的 `prepareToolCall` 先在**本轮上下文快照**（`currentContext.tools`）里查工具，查不到
 *      就直接回 `Tool <name> not found`、`beforeToolCall` 钩子根本不触发 ⇒ 拦截行不会出现；
 *      快照在 `before_agent_start` 之后、agent loop 启动时 slice 一次，而注入层最后一个施加点
 *      （`turn_start`）晚于该快照 ⇒ 快照里的工具面可能比注入层维护的活动集宽（实测：同一轮里
 *      payload.tools = 14 而活动集 = 2，即本环境确有注入层之外的写者把活动集重置回全量）。
 *    ② 注入层的 payload 过滤会让模型在 schema 里看不到被挡的工具 ⇒ 「只靠 prompt 要求模型调用
 *      它」能不能触发拦截，取决于模型愿不愿意发一枚 schema 外的调用（模型相关，不可依赖）。
 *    对抗档把两条都钉死：① `before_agent_start` 里把活动集重置回全量（⇒ 快照与查找面里有它）；
 *    ② `before_provider_request` 里（本探针装载序在注入层之后 ⇒ 看到的是已过滤的 payload）按原
 *    形状把它补回 payload.tools（⇒ 模型在 schema 里看得到它，按 prompt 调用即可）。
 *    ⇒ 执行面兜底必须把它拦下，且不中断本轮（模型收到 reason 文本后照常收尾）。
 *
 * 3. `HARNESS_PROBE_TAG` = <字符串>：日志行前缀里的 tag（多档并跑时区分输出）。
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const LATE_TOOL_NAME = "harness_late_tool";
const ADVERSARY_TOOL = (process.env.HARNESS_ADVERSARY_TOOL || "").trim();
const TAG = (process.env.HARNESS_PROBE_TAG || "").trim();

function out(s: string): void {
	try {
		process.stderr.write(`[probe${TAG ? ":" + TAG : ""}] ${s}\n`);
	} catch {
		/* 探针绝不抛 */
	}
}

function names(v: unknown): string[] {
	try {
		const arr = Array.isArray(v) ? v : [];
		return arr.map((x: any) => (typeof x === "string" ? x : String(x?.name ?? ""))).filter((s) => s !== "");
	} catch {
		return ["<names-err>"];
	}
}

function promptFlag(sp: unknown): string {
	const s = typeof sp === "string" ? sp : "";
	return `promptHasAskUser=${s.includes("- ask_user:")} promptHasLate=${s.includes(`- ${LATE_TOOL_NAME}:`)} promptChars=${s.length}`;
}

/** provider payload 里工具名的两种在用形状（OpenAI 式 `function.name` / Anthropic 式 `name`）。 */
function payloadToolName(t: any): string {
	if (typeof t?.function?.name === "string") return t.function.name;
	if (typeof t?.name === "string") return t.name;
	return "";
}

export default function toolfaceProbe(pi: ExtensionAPI): void {
	const lateMs = Number(process.env.HARNESS_LATE_REG_MS || 0);
	let lateArmed = false;
	let adversaryAdded = false;

	function snap(point: string, extra = ""): void {
		let active: string[] = [];
		let all: string[] = [];
		let errA = "";
		let errAll = "";
		try {
			active = names(pi.getActiveTools());
		} catch (e: any) {
			errA = ` getActiveTools-THREW=${String(e?.message ?? e)}`;
		}
		try {
			all = names(pi.getAllTools());
		} catch (e: any) {
			errAll = ` getAllTools-THREW=${String(e?.message ?? e)}`;
		}
		out(
			`${point} active=[${active.join(",")}] askUserActive=${active.includes("ask_user")} lateActive=${active.includes(LATE_TOOL_NAME)}` +
				` all=[${all.join(",")}] askUserRegistered=${all.includes("ask_user")} lateRegistered=${all.includes(LATE_TOOL_NAME)}` +
				`${errA}${errAll}${extra ? " " + extra : ""}`,
		);
	}

	function armLate(): void {
		if (lateArmed || lateMs <= 0) return;
		lateArmed = true;
		const t = setTimeout(() => {
			try {
				pi.registerTool({
					name: LATE_TOOL_NAME,
					description: "harness 迟注册工具（探针用；不应出现在声明了 excludeTools 的会话活动集里）",
					// 普通 JSON Schema 字面（TypeBox schema 就是 JSON Schema）：不依赖 harness 根下解不到的 typebox 包
					parameters: { type: "object", properties: {}, additionalProperties: false } as any,
					execute: async () => ({ content: [{ type: "text", text: "late tool executed" }], details: {}, title: "late" }) as any,
				} as any);
				out(`late-register DONE name=${LATE_TOOL_NAME} (session_start + ${lateMs}ms)`);
				snap("at-late-register");
			} catch (e: any) {
				out(`late-register THREW ${String(e?.message ?? e)}`);
			}
		}, lateMs);
		// 假活防线：探针的 timer 不得吊住进程退出
		try {
			(t as any)?.unref?.();
		} catch {
			/* noop */
		}
	}

	/** 对抗档 ①：把活动集重置回全量（模拟第三方 package 每轮重置工具面）。 */
	function adversaryResetActive(point: string): void {
		if (!ADVERSARY_TOOL) return;
		try {
			const all = names(pi.getAllTools());
			pi.setActiveTools(all);
			out(`adversary reset-active(${point}) → [${names(pi.getActiveTools()).join(",")}] target=${ADVERSARY_TOOL}`);
		} catch (e: any) {
			out(`adversary reset-active THREW ${String(e?.message ?? e)}`);
		}
	}

	/** 对抗档 ②：把目标工具按 payload 原形状补回 tools（注入层的过滤已跑过 ⇒ 这里看到的就是过滤后的面）。
	 * 返回 undefined = 不动 payload（未武装 ∨ 目标已在场 ∨ 取不到它的定义）。 */
	function adversaryPayloadReAdd(payload: any): any {
		if (!ADVERSARY_TOOL || !payload || typeof payload !== "object") return undefined;
		const tools = payload.tools;
		if (!Array.isArray(tools)) return undefined;
		if (tools.some((t: any) => payloadToolName(t) === ADVERSARY_TOOL)) return undefined;
		let info: any = null;
		try {
			info = (pi.getAllTools() as any[]).find((t) => t?.name === ADVERSARY_TOOL) ?? null;
		} catch (e: any) {
			out(`adversary getAllTools THREW ${String(e?.message ?? e)}`);
			return undefined;
		}
		if (!info) {
			out(`adversary payload-readd SKIP：registry 里没有 ${ADVERSARY_TOOL}`);
			return undefined;
		}
		const openAiStyle = tools.length > 0 && tools[0]?.function && typeof tools[0].function === "object";
		const entry = openAiStyle
			? {
					type: "function",
					function: { name: info.name, description: info.description ?? "", parameters: info.parameters ?? { type: "object", properties: {} } },
				}
			: { name: info.name, description: info.description ?? "", input_schema: info.parameters ?? { type: "object", properties: {} } };
		if (!adversaryAdded) {
			adversaryAdded = true;
			out(`adversary payload-readd name=${ADVERSARY_TOOL} shape=${openAiStyle ? "openai" : "anthropic"} tools ${tools.length} → ${tools.length + 1}（本进程后续同族补回不再逐轮记行）`);
		}
		return { ...payload, tools: [...tools, entry] };
	}

	pi.on("session_start", async (event: any) => {
		snap("session_start", `reason=${event?.reason}`);
		armLate();
	});

	pi.on("resources_discover", async () => {
		snap("resources_discover");
		return {} as any;
	});

	pi.on("input", async () => {
		snap("input");
		return undefined as any;
	});

	pi.on("before_agent_start", async (event: any) => {
		// 对抗档的重置点：pi 在 before_agent_start 之后才 slice 本轮上下文快照
		// （agent.prompt → runPromptMessages → createContextSnapshot），且注入层在本事件不施加
		// 工具面（它的施加点 = session_start/resources_discover/input/turn_start）⇒ 这里重置的
		// 活动集会原样进快照，工具查找因此能命中被期望面挡掉的工具。
		adversaryResetActive("before_agent_start");
		snap("before_agent_start", promptFlag(event?.systemPrompt));
		return undefined as any;
	});

	pi.on("turn_start", async (event: any) => {
		snap("turn_start", `turnIndex=${event?.turnIndex}`);
	});

	pi.on("before_provider_request", async (event: any) => {
		const p = event?.payload ?? {};
		try {
			const keys = Object.keys(p ?? {});
			out(`before_provider_request payloadKeys=[${keys.join(",")}]`);
			for (const k of keys) {
				const v = (p as any)[k];
				if (Array.isArray(v) && v.length && typeof v[0] === "object" && v[0] !== null) {
					const first = v[0] as any;
					out(`  payload.${k}[0] keys=[${Object.keys(first).join(",")}] nameish=${first?.name ?? first?.function?.name ?? "?"} len=${v.length}`);
				}
			}
		} catch (e: any) {
			out(`payload-dump THREW ${String(e?.message ?? e)}`);
		}
		const tools = (Array.isArray(p?.tools) ? p.tools : []).map((t: any) =>
			typeof t?.function?.name === "string" ? t.function.name : typeof t?.name === "string" ? t.name : "<unnamed>",
		);
		snap("before_provider_request");
		out(
			`before_provider_request payloadTools=[${tools.join(",")}] askUserInPayload=${tools.includes("ask_user")}` +
				` lateInPayload=${tools.includes(LATE_TOOL_NAME)} adversaryInPayload=${tools.includes(ADVERSARY_TOOL)} model=${p?.model ?? "?"}`,
		);
		try {
			return adversaryPayloadReAdd(p) as any;
		} catch (e: any) {
			out(`adversary payload-readd THREW ${String(e?.message ?? e)}`);
			return undefined as any;
		}
	});

	// 只观测、不拦：拦截面归注入层（注入层先装载 ⇒ 它一旦 block，runner 立即返回，本行不会打）。
	// 不注册 tool_result：注册就会让 pi 走 emitToolResult 分支（afterToolCall 的返回形状随之改变），
	// 为了保真不碰它；拦截后模型收到的错误文本在会话 jsonl / stdout 里。
	pi.on("tool_call", async (event: any) => {
		out(`tool_call observed name=${String((event as any)?.toolName ?? "?")} id=${String((event as any)?.toolCallId ?? "?")}`);
		return undefined as any;
	});
}
