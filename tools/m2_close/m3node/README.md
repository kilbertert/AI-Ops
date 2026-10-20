# M3 执行配置守卫验收（节点侧 + Gateway 侧）

本目录是这一片的验收工具。节点侧证明的是**真实节点装配**在模型调用前拒绝配置漂移 ——
不是某个测试专用工厂，也不是只校验早期 DTO。Gateway 侧证明签发的期望、领取的对照
与记录终态，契约见 `docs/agents/m3-gateway-exec-config.md`。

## Gateway 侧（`verify_gateway_exec_config.py` / `verify_migration_rollback.py` / `manifest_parity.py`）

```bash
# 真实 HTTP：一致性/缺失/错摘要/兄弟节点，四种组合各一个一次性数据根
uv run python tools/m2_close/m3node/verify_gateway_exec_config.py --out /tmp/exec-config-http.json

# 迁移与回滚：旧库副本、重复启动、用升级前的代码读升级后的库
uv run python tools/m2_close/m3node/verify_migration_rollback.py --out /tmp/migration.json

# 跨语言清单对照：独立声明的期望 + 两端真实加载器
M3NODE_DEPS=… M3NODE_JDK_IMAGE=… uv run python tools/m2_close/m3node/manifest_parity.py
```

`manifest_samples_expected.json` 是**期望的唯一来源**：脚本不用任何一侧的结果当正确答案。
语义样本会被复制到一次性目录并显式设 0600（Git 不保存 0600，检出通常 0644，而权限本身
就是判据），权限与符号链接负例另行构造。**必须至少有一个正例被两端接受** ——
「两端全部拒绝」是空转一致，不是通过。

三者互不替代：单测证判据、真实 HTTP 证进程与路由、迁移脚本证部署影响。
`manifest-samples/` 是两端**共用**的样本；加样本要同时跑 Java 一侧，否则对照会遮住新分歧。

## 端到端：整轮完成与工具领取的分界（`verify_node_link.py`）

```bash
M3NODE_DEPS=… M3NODE_JDK_IMAGE=… \
  uv run python tools/m2_close/m3node/verify_node_link.py --out /tmp/node-link.json
```

真 Gateway 进程（`create_gateway_app`，一次性数据根）+ **真 Java 节点**（编译自
`tools/m2_close/node/src`）+ 合成模型/CPMS。平台派发入口是替身，但**领取与完成通知
都由节点自己的类发出**；平台替身按**同步**语义起节点（真实平台也是同步接口），
否则「派发返回时记录已 succeeded」这条既有语义会被替身自己的异步性破坏。

39 条判据（LINK-01..10）：正常两轮、第二轮 500、末轮截断、三种坏回执、受控 claim 被拒（身份不一致
与「已领取未写快照」两种）、取消、领取重试、撤销边界、两 run 归属、未启用兼容。
节点容器用 `--network=host` 才能打到宿主 loopback 上的真 Gateway —— 这是本机验收的
已知取舍，不是隔离证明。

## 同 JVM 并发（`m3link/run.sh`）

```bash
M3NODE_DEPS=… M3NODE_JDK_IMAGE=… bash tools/m2_close/m3node/m3link/run.sh
```

`verify_node_link.py` 每次派发只起一个节点进程，覆盖不到「同一个 JVM 里两个 run」。
这一层用 `ConcurrentRunAcceptance`（自带只回答领取/完成的**替身** Gateway，故意对任何 run
都回 200 —— 这正是「能领到 ≠ 拥有这条 run」的最坏情形）跑四个模式：

| 模式 | 判据 |
|---|---|
| `distinct` | 两个不同 run 都完成；model=4、claims=2、各一条 completed |
| `early` | 相同 run 在 A 构建 ChatClient 之前重入 ⇒ B 本地被拒；model=2、claims=1 |
| `late` | 相同 run 在 A 已领取、末轮请求仍挂在上游时重入 ⇒ B 本地被拒；model=2、claims=1 |
| `first-error` | 首轮失败：claims=0，只发一次 failed |

协调全部用可观察事件（customizer 到达 / 末轮请求到达上游），**没有 sleep**。
它不替代端到端：真 Gateway 的 39 条判据仍在 `verify_node_link.py`。

## 节点侧重跑

本目录是这一片的验收工具。它证明的是**真实节点装配**在模型调用前拒绝配置漂移 ——
不是某个测试专用工厂，也不是只校验早期 DTO。

## 被测方（都是真的）

| 环节 | 真实类 |
|---|---|
| 装配 | Spring 上下文按节点的方式扫描 `com.trendpower.aiops.node`；`GuardedChatModelFactory` 与 `GovernedToolRuntime` 的 `@Primary` 接管是真的接管（工具执行走框架默认的管理器与解析器，本片未覆盖它） |
| 模型 | `OpenAiCompatibleChatModelAdapter` → `OpenAiChatModel`（锁定依赖里的真实适配器） |
| 工具 | `DefaultToolCallingManager` + `ToolCallingAdvisor`，执行到 `ControlledChargingStatusTool` |
| 驱动入口 | `ChatSessionRuntime.execute`（节点收到一次派发后真正走的那条路，流式） |
| 上游 | **唯一的替身**：本机 loopback 上自写的合成 HTTP 服务（`SyntheticUpstream`），记录真实收到的 `model`/`tools`/选项/路径/消息 |

不接真实供应商、不访问 41、不读生产凭据、容器 `--network=none`。

## 每个「期望」锚在哪一面（实测结论，不是设想）

| 面 | 能看到什么 | 谁用它 |
|---|---|---|
| 构造出来的模型 | 模型标识、有效选项（baseUrl 与 key 在闭包里读不到） | `GuardedChatModelFactory.verifyModelOptions` |
| 解析出来的工具集合 | name + description + schema | `GovernedToolRuntime.resolve` → `requireResolvedTools` |
| 最终 `Prompt` | **完整**的 system 消息集合、消息形状；模型与工具回调（客户端合并后）| `GuardedChatModel.ExecGuard` |
| 上游请求体 | 真正发出去的东西 | 验收断言 |

`Prompt.getOptions()` 在**没有** `PromptFactory` 的路径上实测为 `null`；本片诊断也确认
框架只调用 `builder.defaultAdvisors/defaultTools/defaultToolContext`，从不调用 `defaultOptions`。
所以「只看 Prompt 就宣称冻结了模型与工具」是不成立的。

## 复跑

```bash
export M3NODE_JDK_IMAGE='eclipse-temurin:21-jdk@sha256:<pinned>'
export M3NODE_DEPS=/path/to/artifacts  # 其中 lib/ 放锁定依赖
tools/m2_close/m3node/run.sh
```

缺 `M3NODE_JDK_IMAGE` 或 `M3NODE_DEPS` 一律**非零退出**：不跳过后报通过。
运行产物写在 `tools/m2_close/m3node/build/`（已被 gitignore），含 `guarded.log`、
`counter-proof.log` 与 `logs.sha256`。

## 场景

启用守卫（`guarded.log`）：

- `exact` — 流式工具循环正例：上游收到受控工具定义与清单声明的模型与选项；受控工具执行
  **1 次**；模型调用 **2 次**（一次真实工具循环，明确计数，不预设为 1）；业务领取替身 1 次。
- `system-drift` / `model-drift` / `endpoint-drift` / `options-drift` — 分别改系统提示词、
  模型标识、endpoint、有效选项，一律显式失败，**上游调用 0、业务调用 0**。
- `tool-description-drift` / `tool-schema-drift` — 工具名不变，只改描述或输入 schema，
  同样拒绝（同名漂移必须被拒，早先「只比工具名集合」的口径已作废）。
- `history-drift` / `media-drift` / `memory-drift` — 固定无历史模式下显式拒绝。
- `mcp-drift` — 动态 MCP 服务器在固定受控工具模式下拒绝。
- `advisor-<drift>-call` / `advisor-<drift>-stream`（`drift` = `model` / `temperature` /
  `tool-description` / `tool-schema`）— 构造之后、最终调用之前改写**有效消费对象**：
  两条路径都必须在首个上游请求前拒绝，且 `chatCalls=0`、`claimCalls=0`、工具执行 0。
  这四条 ×2 就是 Codex 复现的那批绕过的回归。
- `advisor-<drift>-*` 还覆盖 R2 的四条：`model-with-namespace`、`temperature-roundoff`、
  `extra-body-model`、`extra-body-messages`（前两条是「有损比较」的产物，后两条走请求扩展覆盖层；
  上游实测确实收到被改写的值）。
- `cross-run` — 两个不同合法 `run_id`；交错点钉在**上游记录到 A 的第一次 chat 请求**时才放行 B，
  所以顺序是确定的而不是靠注释宣称。A `chatCalls+2`、工具 `+1`、业务 `+1` 且拿到 final；
  B 在首个上游请求前被拒、贡献 0。
- `mid-loop-drift` — 工具循环**中途**的漂移：第一轮真实发生（上游 `chatCalls=1`、工具执行 1 次），
  第二轮在最终边界被拒（仍是 `chatCalls=1`）。已发生的调用不被抹去，阻止的是下一次。
- `reject-missing` / `reject-loosened` / `reject-empty` / `reject-schema` / `reject-history` /
  `reject-not-json` — 启用后清单缺失、权限过宽、为空、schema 不受支持、`history_policy` 非 `none`、
  非法 JSON，全部在装配阶段拒绝，**不降级**。

反向控制（`counter-proof.log`）：同一份夹具、同一批漂移，只是**不指向清单** ⇒
守卫不介入，改动**确实到达上游**（`EVIL-SYSTEM`、`evil-model`、`temperature=0.9`、
多出来的用户消息）。没有这一段，上面的「拒绝」可能只是夹具没跑起来造成的假绿。

## 三条实测结论（都改变了实现，见 ADR 0051）

1. **`Prompt` 面上有没有模型与工具，取决于路径**：框架只调用 `builder.defaultAdvisors/
   defaultTools/defaultToolContext`，**从不** `defaultOptions`，所以**没有 `PromptFactory`**
   的路径上 `prompt.getOptions()` 实测为 `null`；但本节点的真实路径**经过** `PromptFactory`，
   最终 `Prompt` 携带的有效选项是**完整**的（model/temperature/topP/maxTokens/baseUrl/
   apiKey/tools/toolContext 实测非空）。⇒ 最终边界**必须每轮复查**有效模型、选项与工具定义；
   构造期与解析期的检查保留为更早的失败点，但**不是**最终保证。
   （上一版把「无 PromptFactory 路径的 `null`」推广成「最终对象上没有」，那是个绕过。）
2. **「交付的工具不可执行」是误判，已撤回**：`claimCalls=0` 的根因是夹具用了 9 个字符的
   `run_id`（契约形状 `run_[A-Za-z0-9_-]{20,64}`），工具按设计拒绝。改用合法编号后**未改任何
   装配**，工具即执行 —— 因此没有引入工具调用管理器覆盖。
3. **中途漂移只有一条可达通道**：改派发体无效（`Prompt` 一次构建、多轮复用）；
   `SnailAiInterceptor` 只看到第一轮（`ToolCallingAdvisor` 在自己那个位置重新进入链）；
   只有 order **晚于** `ToolCallingAdvisor` 的 advisor 才会每轮执行。而且必须是
   **`BaseAdvisor`** —— 只实现 `CallAdvisor` 的 advisor 在流式路径上根本不会应用。

## 失败传播自检

```bash
M3NODE_SELFTEST=1 tools/m2_close/m3node/run.sh   # 期望 SELFTEST=PASS
```

自检**递归调用本脚本**并注入故意失败（`M3NODE_FORCE_PHASE_FAILURE=guarded|reverse|both`），
断言顶层非零退出、日志里有**阶段自身**的失败标记、且不以整体成功收尾。
（旧脚本 `java … | tee log` 的退出码是 `tee` 的 0；更早一版自检另写了一套 docker 检查，
所以把 `run_phase` 改成 `return 0` 它照样报 PASS —— 现在这个变异会让自检**转红**。）

## 未覆盖

Gateway 签发与持久化、`agent_version` 回执、真实供应商与真实模型、真实订单与真实节点身份、
生产节点装配（本片只在本地合成环境验证）。本目录的结果不证明 M3 已验收。
