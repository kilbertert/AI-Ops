# 试运行：同一条执行路径的另一个入口

日期：2026-10-14
状态：已采纳
影响版本：PRD #772 F（子票 #781）；`dify_trial.py`、`POST /v1/dify/trial`、`standard_diagnoses.kind='trial'`

## 背景

运营在 Dify 里改完方法论与工具勾选之后，**没有办法在真实订单上试一下**再发布。Dify 的
「调试与预览」跑的是 Dify 自己的智能体循环，碰不到我们的执行器、证据日志与校验器 ——
就算它碰到了数据，它的行为也不是生产上会发生的行为。

PRD #772 F 要在 Dify 编辑页加一颗「在 AI-Ops 试运行」按钮：拿**当前草稿**（方法论提示词
+ 工具白名单），对一张订单走一次 AI-Ops 真实 harness，结果以诊断报告 + 证据展示。

## 决定

**试运行是 `run_agent_diagnosis` 的另一个入口，不是第二个执行器。** 具体到代码：

1. `dify_trial.run_trial` 调用 `run_agent_diagnosis` —— 也就是 `aiops diagnose` CLI 走的
   **同一个函数** —— 只把两样东西换成草稿值：`methodology`（提示词）与 `allowed_tools`
   （工具白名单）。`fixture=None`，所以打的是**真数据源**。
2. **护栏全部继承，一条都不重写**：白名单在 `DiagnosticToolExecutor.execute` 里
   fail-closed 强制（ADR-0019 不变）、证据日志、语言守卫、`AgentResultValidator`、
   结构契约。
3. **预算同一份数字**：`max_turns` / `max_tool_calls` 来自同一份 `Settings`
   （`config.py` 的 `AIOPS_AGENT_MAX_TURNS=8` / `AIOPS_AGENT_MAX_TOOL_CALLS=16`）。
   "试运行与生产同一预算"因此是构造出来的，不是承诺出来的 —— 没有第二份可以漂走的数字。
4. **草稿只能收窄工具集合**：白名单里的名字要过 `ToolName` 枚举，七个之外直接拒。
   与 `allowed_tools` 在发布门上的检查同一形状。
5. **不产生智能体版本、不写注册表、不改任何生产路由。** 它写进 store 的只有一行
   `kind='trial'` 的作业行（状态与结果，复用 `standard_diagnoses` 的作业机械）与运行
   目录里的证据；没有任何东西会读它去做路由决策。
6. **顺序改掉了 Dify 的对话面。** 网关的 `start_standard_diagnosis` 带着会话轮次、
   turn 注册、interrupt RPC、conversation store —— 那些是**对话**需要的，试运行一个都
   不要。硬把草稿塞进那条路径会退化成"复制一份执行链"，正是本节要避免的。所以试运行
   直接走 CLI 那个入口，短路掉对话机制。

## 范围来源（本票：固定调试租户）

试运行要真的查一张订单，所以它需要一个**订单范围**。本票的语义是：

* 范围由服务端配置 `AIOPS_GATEWAY_DIFY_TRIAL_DEBUG_TENANT` 决定，**不来自请求**。
  请求体里出现 `tenant_id` / `site_ids` / `user_id` 一律 **400**（`extra="forbid"`），
  不是安静忽略 —— 忽略会让运营以为它起了作用。
* 那个租户的范围按**顶层账号口径**（`DataScope(type="all")` = 整租户、不受站点维度
  约束）。**不是** `agent_manifest.admin_context`：那个合成上下文是 `type="self"` 且
  主体没有 C 端用户，`resolve_query_scope` 对它直接抛 `SCOPE_ERROR_EMPTY_SCOPE` ——
  它是给"不碰订单数据"的发布/对账用的。
* 订单可见性用与生产**同一条判据**：路由拿这个范围问 `order_authorizer`（
  `_order_authorization`），越权与不存在同为 404。**路由问的范围与 worker 执行的范围
  是同一个对象**，所以不存在"问了 A、跑了 B"。
* 钥匙决定**能不能调**（`AIOPS_GATEWAY_DIFY_TRIAL_API_KEY`，第六把独立的 Dify 钥匙）；
  配置的租户决定**能看什么**。两件事分开，所以这条路由不去猜发起人是谁。
  `X-AIOps-Initiator` 只进审计，**不是**授权依据。

**#782 接管的方向**：把执行范围从"固定调试租户"换成发起人的公司身份（`operator` 口径）。
届时只改 `GatewayRuntime.trial_run_context()` 一处，路由与 worker 不动 —— 因为
"路由问的 == worker 跑的"这条已经由单一构造点保证了。

## 入口（本票：AI-Ops 侧）

本票只交付 **AI-Ops 侧的运行模式与 HTTP 面**（`POST /v1/dify/trial` 提交、
`GET /v1/dify/trial/{id}` 读结果）。Dify 编辑页那颗按钮（桥转发、草稿读取）属于部署侧，
依赖 #773 的草稿结论与 #783 的宿主工作；**草稿可读性的结论是：Dify 1.17.1 的 chat 应用
没有草稿层，只能读到"最近一次保存"的配置**（#773 结论），所以按钮必须写明「以最近一次
保存的配置为准」—— 那是既成事实，不是设计选择。

## 被否掉的替代形态

- **让 Dify 自己的循环执行七个工具。** 那要我们的执行器暴露给 Dify 的循环，等于把
  白名单、预算、证据纪律交给 Dify 的编排 —— 而 PRD #772 的 Out of Scope 正是这一条。
- **试运行做成"第二套执行链"**（自己造 executor / 自己的预算常量）。两套实现必然漂：
  试运行通过而生产表现不同，正是要防的那件事。
- **让请求体带租户/站点**。那会让"试运行"成为一条按请求查单的通道；#782 的按人范围
  才是它该有的形状，而那是**身份**给的，不是请求给的。
- **复用 `admin_context` 当执行范围。** 它是 `self` 范围且无 C 端用户，连一张订单都
  解析不出来（本次实现中真的踩到：第一次跑起来就是 `SCOPE_ERROR_EMPTY_SCOPE`）。

## 后果

- 运营要按真实数据验证草稿，只有这一条路；Dify 预览返回的仍是固定说明（#774）。
- 试运行的作业与生产诊断**同一张表**，所以状态词表、认领守卫、过期清理、重启收敛
  全部复用；多出来的只有 `kind='trial'` 与两列审计（发起人、草稿摘要哈希）。
- 审计记的是**草稿摘要哈希**而不是提示词本身：审计要回答"是不是同一份草稿"，不需要
  把运营的文字抄进第二个库。
- 并发闸本票是 1（串行）：试运行打的是真数据源与真模型，这是它对自己可能压垮生产
  只读库的唯一防线。按人频率上限随 #782 一起做（那时才有"人"这个维度）。
