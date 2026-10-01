# toolface-harness —— 人格注入层「工具面」的取证 harness（需活模型，不进 CI）

耐久回归面：复现并核验**人格注入层**（`pi-core/agent/extensions/profile-loader.ts`）对 pi 工具面的
两类动作 —— ① 四个施加点（`session_start` / `resources_discover` / `input` / `turn_start`）按
profile 装载的能力声明（`cap.yml` 的 `tools` 白名单 ∪ / `excludeTools` 排除）重设活动工具集；
② 两处与施加序无关的强制（`before_provider_request` 过滤真正发给模型的 `payload.tools`、
`tool_call` 拦下期望面之外的调用）。附带可观测面：注入层的装配摘要行与全部 WARN 走 pi 的 stderr，
harness 的读端**到达即写**（`read1` 语义）⇒ 顺带核验会话存活期即可读到这些行。

`test_wrap.py` / `test_persona.py` 是**离线**矩阵（假 pi、无网络）；本 harness 是**活会话**取证面
（真 pi + 真模型槽），二者不互相替代：

- **需活模型槽 ⇒ 不进 CI、不被 `test_wrap.py` 调用**（也不被任何服务的 spawn 装配链读取）。
- 跑一次 = 若干次真实模型请求；每枚 case 都自带假活上界（`--settle-timeout` / `--timeout` /
  驱动 `finally` 段的优雅收口 → 30s 宽限 → 组杀），绝不无条件 `wait`。

## 工作根红线（不得为了跑通而弱化）

- 工作根 = **专用临时根**（缺省基目录 `<HARNESS_WS>/run/temp/toolface-harness/r-<时间戳>`），
  生产资产只经**软链**挂进去（`setup.sh` 建；`rm -rf` 不跟随软链 ⇒ 清理面对生产资产零删除动作）。
- 三处同一套 **root 身份断言**（`setup.sh` / `clean.sh` / `drive.py:guard_root()`）：
  realpath 前缀判定（工作根必须在临时基目录内）+ 拒「等于生产根」+ 拒「生产根在工作根内部
  （工作根是生产根的祖先）」；不满足即 `REFUSE` 退出 2、不建不删。
- 不吞错：全仓零 `ignore_errors=True`、零 `2>/dev/null`（唯一命中是「不用它」的注释行）。
- 判活/判残留一律 `ps -eo pid,ppid,lstart,command | grep …`（不用 `pgrep`：它给不出启动时刻与父进程）。

## 公开仓收录判据

仓里**不存机器专有值**：无用户名 / 主机名 / 家目录绝对路径 / 仓名单常量。部署面一律
「现场发现 ∨ 调用方注入」：`pi` 的可执行路径运行时 `command -v pi` 取；wrap 档追加探针用的
shim **运行时生成、不落仓**；生产根清单缺省 = 现场发现（工作区根 + 其下每个含 `.git` 的一级子目录），
也可 `HARNESS_PROD_ROOTS` 注入。工作区根缺省值 `~/m` 是**可覆盖的约定值**（`HARNESS_WS`），
不是硬依赖。

## 布局

| 文件 | 角色 |
|---|---|
| `setup.sh` | 建工作根：临时根 + 顶层软链生产资产（真目录三名 `bots/`·`agents/`·`run/` 自建）+ 拷入 harness 自建的 profile/能力/探针。只创建、绝不删除 |
| `drive.py` | 驱动器：两档拉起（`direct` 直拉 `pi --mode rpc` ∨ `wrap` 拉 `pi-rpc-wrap.py` 生产形态）、投递 prompt、取样 stderr size、有界收口、写 `out/<case>/` |
| `clean.sh` | 清理面：过完三条 root 身份断言后 `rm -rf` 工作根本身（先逐条打印软链身份留证） |
| `probe/toolface-probe.ts` | 探针扩展：在 `session_start` / `resources_discover` / `input` / `before_agent_start` / `turn_start` / `before_provider_request` / `tool_call` 各时点打名集与 payload 读数（全走 stderr） |
| `assets/bots/profiles/*.json` | harness 自建 profile：`harness-min`（excludeTools 档）、`harness-wl`（白名单档）、`harness-call`（对抗档：窄白名单 + 正文要求照用户提示发调用） |
| `assets/bots/caps/harness-*/` | 上述 profile 装载的能力声明（`cap.yml`）与最小正文（`prompt.md`） |

## env

| 变量 | 缺省 | 含义 |
|---|---|---|
| `HARNESS_ROOT` | 无（`drive.py`/`clean.sh` 必需） | 工作根；必须在临时基目录内 |
| `HARNESS_WS` | `~/m` | 工作区根：软链源、注入层位置与生产根发现的基准 |
| `HARNESS_BASE` | `<HARNESS_WS>/run/temp/toolface-harness` | 临时基目录（工作根的允许范围） |
| `HARNESS_PROD_ROOTS` | 现场发现 | 冒号分隔的生产根清单（断言用） |
| `HARNESS_WRAP` | 本 harness 所在 checkout 的 `pi-rpc-wrap.py` | `wrap` 档的被测脚本 |
| `HARNESS_LOADER_REL` | `pi-core/agent/extensions/profile-loader.ts` | 注入层相对工作区根的位置（= `pi-rpc-wrap.py` 的 `PROFILE_LOADER_EXT_REL` 同值） |
| `HARNESS_LATE_REG_MS` | `0`（由 `--late-ms` 写） | >0 ⇒ 探针在 `session_start` 后 N ms 迟注册 `harness_late_tool` |
| `HARNESS_ADVERSARY_TOOL` | 空（由 `--adversary-tool` 写） | 对抗档目标工具名（见下） |
| `HARNESS_PROBE_TAG` | 空（由 `--tag` 写） | 探针日志行前缀 tag（多档并跑时区分输出） |

## 跑法

```bash
cd <本目录>
R=$(./setup.sh | tail -1)                       # 建根；末行 = 工作根路径
HARNESS_ROOT=$R python3 drive.py excl-res-late --mode direct --form resident \
  --profile harness-min --late-ms 3000 --delay 8 --probe-last --settle-timeout 150
HARNESS_ROOT=$R python3 drive.py wl-task-late --mode direct --form task \
  --profile harness-wl --late-ms 3000 --delay 8 --probe-last --settle-timeout 150
HARNESS_ROOT=$R python3 drive.py adv-block-hostinfo --mode direct --form task \
  --profile harness-call --probe-last --adversary-tool host_info --settle-timeout 150 \
  --prompt '请调用 host_info 工具一次（它不需要参数）。拿到结果后，把结果原样贴一行，再另起一行写 done。'
HARNESS_ROOT=$R python3 drive.py wrap-task-excl --mode wrap --form task \
  --profile harness-min --settle-timeout 120
HARNESS_ROOT=$R ./clean.sh                      # 收尾：过断言后删工作根
```

`HARNESS_ROOT` 的取值纪律：**只取 `setup.sh` 打出的那个根**（∨ 临时基目录下的自建子目录），
绝不指向工作区根、任何仓根或生产 `run/`。三处断言会拒，但别拿它们当唯一防线。

读数落点：

- `direct` 档 ⇒ `$R/out/<case>/{stderr.log,stdout.jsonl,meta.json,sizes.txt}`（`stderr.log` =
  注入层日志 + 探针读数；`meta.json` = 本 case 的完整 argv/env/stage/rc/elapsed）。
- `wrap` 档 ⇒ pi 的 stderr 由 wrap 自己写 `$R/run/agentd/<case>.stderr.log`（`meta.json` 的
  `stderr_log` 字段指明该取哪一份），`$R/out/<case>/stderr.log` 只有 wrap 自己的行；
  且 pi 的 stdout 被 wrap 透传进它自己的 unix sock（harness 不连该 sock）⇒ `meta.json` 的
  `settled` 恒 0，收敛信号 = wrap 进程退出（`rc`）。
- `sizes.txt` = 活会话期每 `--sample-every` 秒一行 `t/alive/size`，用来判「小额 stderr 是否在
  子进程存活期就落盘」（不是退出后的 EOF flush）。

## case 清单与期望读数

| case | 档 | 形态 | profile | 期望的关键行（原文族） |
|---|---|---|---|---|
| `excl-res-late` | direct（`--probe-last`） | resident | `harness-min` | 装配摘要行 `人格装配（会话内注入）：profile=harness-min form=resident … xt=ask_user,harness_late_tool …`；`at-late-register … lateActive=true`（pi 自己把迟注册项并进活动集）而**下一个施加点**（`input`）后 `lateActive=false`；`工具面过滤（before_provider_request）：payload.tools 15 → 13`；`askUserInPayload=false lateInPayload=false` |
| `wl-task-late` | direct（`--probe-last`） | task | `harness-wl` | `tools=3`；`payload.tools 15 → 3`；`payloadTools=[read,ls,harness_late_tool] lateInPayload=true`（白名单**不误伤**：白名单内的迟注册工具照常给） |
| `adv-block-hostinfo` | direct（`--probe-last`，对抗档） | task | `harness-call` | `adversary reset-active(before_agent_start) → […host_info…]`；`工具面过滤（before_provider_request）：payload.tools 14 → 2`；`adversary payload-readd name=host_info … tools 2 → 3`；**`工具面拦截：host_info 不在本 profile 的 tools 白名单内 ⇒ 阻止执行（不中断本轮）`**；会话 jsonl 里该调用的 toolResult 文本 = `人格工具面：host_info … 不要重试 …`，且本轮不中断（模型照常收尾） |
| `wrap-task-excl` | wrap（生产形态） | task | `harness-min` | **两份 stderr 分开取**：`$R/out/<case>/stderr.log` = wrap 自己的行（`观测端点就绪: …` / `人格注入层装载：…（profile=harness-min）`——**不含形态字段**：形态轴住 profile 的 `form`，由解析层定档后写在注入层自己的装配摘要行里 / `任务收敛完成（exit 0）`）；`$R/run/agentd/<case>.stderr.log` = pi 的 stderr（装配摘要行 + `工具面过滤（before_provider_request）：payload.tools 15 → 14` + 探针读数）；`sizes.txt` 显示 `alive=True` 时 size 即 >0 |

对抗档为什么存在（机制细节 = `probe/toolface-probe.ts` 头注）：pi 的 `prepareToolCall` 先在**本轮
上下文快照**里查工具，查不到就直接回 `Tool <name> not found`、`beforeToolCall` 钩子不触发 ⇒ 拦截行
不会出现；而注入层的 payload 过滤会让模型在 schema 里看不到被挡的工具 ⇒ 「只在 prompt 里要求模型
调用它」能否触发拦截取决于模型愿不愿发一枚 schema 外的调用（实测：本环境的模型会发，且因为快照的
工具面比注入层维护的活动集宽，查找能命中 ⇒ 拦截行确实出现；但这条依赖模型配合，不可当回归判据）。
对抗档把两件事钉死（探针在 `before_agent_start` 重置活动集 + 在 `before_provider_request` 把目标工具
按原形状补回 payload）⇒ 触发变成确定性的。**对抗档要求探针装载在注入层之后**（`--probe-last` ∨
`--mode wrap`），否则 `drive.py` 直接 REFUSE。

## 已知边界

- `--form <档>` 有**两个协调效果**（人格形态轴已从 env 收进 profile 的 `form` 字段）：① 写常驻标记
  env（`AGENTD_RESIDENT` + `AGENTD_SESSION_NAME`）——它们仍是 wrap 的完成收敛形态与注入层「是否
  agentd 监督会话」判据的输入；② 把工作根里那份 profile **副本**的 `form` 字段写成同值（只写
  harness 自建的副本：软链/跳出工作根一律不写，判据 = `drive.py:sync_profile_form` 的三重身份断言）。
- 工具面的排除**只来自能力声明**（`cap.yml` 的 `excludeTools`）：解析层已无形态基线排除集，本 harness
  的三枚自建 profile 也不列生产的 `executor` 能力 ⇒ 两档的期望读数只差在常驻标记驱动的 wrap 行为上。
- `--form resident` 的会话**永不收敛**（wrap 档不关 pi stdin）⇒ 驱动按 `--settle-timeout` 上界发
  SIGTERM（wrap 的 handler 关 pi stdin ⇒ pi 优雅退出），`meta.json` 的 `rc=143` 是设计路径、不是失败。
- 换 pi 的 agent dir 做对照实验（例如去掉某个第三方 package）用 `--agent-dir <目录>`：那个目录要
  自己准备（本 harness 不落任何 settings 副本 —— 它们是生成物，落仓即制造陈旧副本）。
- 修前对照用 `--loader <文件>`：那份修前注入层同样**不落仓**，现场
  `git show <sha>:pi-core/agent/extensions/profile-loader.ts > <临时文件>` 生成后传进来。
