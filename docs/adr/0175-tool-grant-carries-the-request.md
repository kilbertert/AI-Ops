# 175. 工具授权装请求目标，不装订单号

日期：2026-10-30
状态：已采纳（设计定型，尚未实施）

## 决定

节点拿到的 Grant 从「订单号 + 基址」改为「**这次要发的请求** + 输入 + 基址」：

```text
Grant = { tenant_id, site_ids, request:{method,path,query}, inputs, base_url, bearer, expires_at }
```

三处随之改变：

1. 节点**不再拼 CPMS 路径**——`CpmsReadTool.java:95-96` 里
   `"/chOrderInfo/getOrderInfoDetail/" + grant.orderNo()` 这种拼装消失，节点只执行授权
   给它的那个请求。
2. 网关代理比对**完整请求目标**，不再是 `path == 前缀 + run.order_no`
   （`operator_cpms.py:408`、`operator_cpms.py:446`）。
3. 工具名 → 请求路径的映射从 Java 常量搬到声明里，与 Python 侧的 `PATHS`
   （`operator_cpms.py:27`）合成一处。

**这条决定的收益只有一句话：新增一个 CPMS 只读工具不再需要改 Java、不再需要节点发版。**

## 依据

ADR-0165 说「固定接口、白名单字段和参数足够」，并把「任意 HTTP/SQL 工具」列为被否方案。
在那个时点这句话是对的：受控接口确实比开放工具安全。但它有一个当时没有被计价的成本——

路径拼装在 Java 里（`CpmsReadTool.java:95-96`），工具名在 `CpmsReadTool.NAMES`
（`CpmsReadTool.java:23`）与 `GovernedToolRuntime.resolvedTools()` 的 `if/else`
（`GovernedToolRuntime.java:87-96`）里，未声明的工具直接抛
`ExecConfigViolation("TOOLS", "发布清单包含未实现的工具")`。同一个工具名在仓库里写了五遍：
`chat_execution.py:73`、`operator_cpms.py:27`、`CpmsReadTool.java:23`、`ops_provider.py:14`、
`node_publication.py:176`。

于是「受控接口」的实际成本是**每个接口一次节点构建 + 一次部署窗口 + 一次双端契约核对**。
这不是安全裕度，是变更税；它高到足以让「这个能力值不值得做成工具」的决定被成本主导，
而不是被需求主导——站点查询就是这样一个被成本挡住的例子。

## 被否掉的替代形态

**网关侧通用代理：节点只拿响应，请求由网关发出。** 最强的地方是它完全不动 Java——
一次网关发版就能支持任意数量的新工具，比本决定还省一次节点维护窗口。它输在两件事：
公司令牌会从「节点持有 180 秒、绑定单次 run 的 Grant」变成「网关进程长期持有、代所有 run
发请求」，而 41 上已验收的边界正是「节点凭自身凭据领授权、自己发请求、网关只做代理与逐行
核对」（`docs/agents/operator-cpms/README.md`「范围约束」）；同时超时、节流、取消与
「读取期间撤权只停止后续交付」这些语义都长在节点那侧（`CpmsReadTool.java:121-137` 的
总时限与 93/103/108 三处 `requireLive` 复核），搬进网关是一个进程内的并发问题，
不是一次重构。

**保留 `orderNo`，为站点加一个专用字段。** 最强的地方是改动最小、站点能最快跑通：
Grant 多一个 `site_query` 字段，代理多一条前缀规则。它输在形状——这就是「订单是特例，
其他能力各自加字段」的同一个问题再犯一次。第三个能力（设备故障记录按站点 + 时间过滤）
会需要第四个字段，第四个能力需要第五个。

**让节点从执行清单里自取路径。** 最强的地方是路径活在声明里、不用改授权载荷，
发布期的漂移检查可以顺带覆盖它。它输在生命周期不匹配：清单是发布期产出、跨 run 不变的，
而路径里的订单号是 run 级的。让 run 级参数进入发布期清单，等于让清单随 run 变化，
而清单的身份就是它原始字节的 sha256（`execution_manifest.py` 头部约束 1）。

## 后果与验证

- **这是一次破坏性的双端变更，没有灰度窗口。** 证据字段集合现在是精确相等比对
  （`operator_cpms.py:463-467` 要求恰好 `{tool, order_no, source, observed_at, data}`，
  `CpmsReadTool.java:100` 生产的正是这五项）。改字段意味着「旧节点 + 新网关」与
  「新节点 + 旧网关」都必须**被明确拒绝**，而不是碰巧失败。部署顺序与回滚点要在任务里
  单独写清，不能沿用「先网关后节点」的默认顺序。
- `invoke()`（`operator_cpms.py:433-447`）里的路径构造消失，`/v1/operator-tools/{tool}`
  的语义变成「按授权执行」，不再是「按名字拼路径」。
- `ops_provider.py:14-55` 的「provider 已存在且 schema 不同就抛错」会变成常态而不是异常：
  新增工具必然改变 provider schema。需要一条显式迁移命令，而不是让人手工删重建。
- `GovernedToolRuntime.resolvedTools()` 的 `if/else` 换成一个「按名字从声明装配」的通用
  构造，未在声明里的工具名仍然拒绝——**拒绝的判据不放松，只是从编译器常量变成数据**。
- 验收：`tools/experiments/m2_close/m3node/NodeLinkAcceptance.java` 覆盖单工具 HTTP 链路；
  `tools/experiments/m2_close/m3node/verify_operator_cpms.py` 覆盖正常、供应商失败、末轮截断、
  草稿试用与发布全关。本决定另需两条混跑拒绝证据（见上），以及一条「新增一个工具不改
  `services/execution-node` 任何文件」的可机械判定的证据——那是这条决定唯一真正的验收物。

方案与分期见 [工具架构](../agents/operator-cpms/tool-architecture.md)。
