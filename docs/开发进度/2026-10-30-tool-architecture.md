# 工具架构解耦方案与四条 ADR

分支 `docs/tool-architecture`，基线 `83ba786a`。本轮**只有文档，没有代码改动**。

## 目标

回应「查站点已可用，为什么没有封装成 `cpms_order_detail` 那样的工具、也没有用户端 CPMS
只读 API」。结论是：不是那条能力不值得，而是工具机制把「这一次 run 有没有订单」当成了
「这一次 run 是什么任务」。

## 已交付

| 文件 | 内容 |
| --- | --- |
| `docs/agents/operator-cpms/tool-architecture.md` | 现状八层调用链、9 项耦合（C1–C9，逐条带 `file:line`）、根因、目标形态、P0–P3 分期与每期等价证明、风险 |
| `docs/adr/0174-capability-is-the-run-type.md` | 能力是 run 的类型，`order_no` 可空并降级为输入 |
| `docs/adr/0175-tool-grant-carries-the-request.md` | 授权装请求目标而非订单号；做完后新增 CPMS 只读工具不需要改 Java |
| `docs/adr/0176-tool-arguments-gated-against-the-question.md` | 工具可带参数，参数由网关对照提问原文放行 |
| `docs/adr/0177-site-query-has-one-implementation-three-entries.md` | 站点查询一份实现、三个入口（对话/工具/`GET /v1/sites`） |

四条决定分别对应用户在 2026-10-30 确认的四个选择：先出方案不动代码、模型可给参数、
用户端两种形态都要、接受一次 Java 节点配套发版。

## 四条 ADR 的关系

- 0174 与 0175 是同一个机制的两半：前者补类型，后者换载荷。都是 P0/P1。
- 0176 是工具化的前置条件：没有参数放行，站点工具跑不起来。
- 0177 是业务交付，依赖前三条。

## 已知缺口

1. **`GET /v1/sites` 的 C 端可见范围未决**（ADR-0177 §后果）。C 端调用者 `DataScope` 是
   `self`，`resolve_query_scope` 对 `self` 返回 `site_ids=None`（`identity/query_scope.py:273-281`），
   而 `search_sites` 里 `None` 表示不过滤。直接复用等于把整租户站点名单交给任何 C 端用户。
   三个候选已列在 ADR-0177 里，需要单独一条决定，实现不得先于它。
2. **0176 的多轮组合面未验证**：单轮「参数是提问原文子串」成立，不代表模型连续多轮换词
   仍然安全。
3. **P1 没有灰度窗口**：证据字段集是精确相等比对，两端必须同版部署，部署顺序与回滚点
   需要在 P1 任务里单独写清。

## 下一步

用户确认四条 ADR 的方向后，按 `/to-spec` → `/to-tickets` 拆 P0/P1 两个可独立交付的切片。
P0 不需要 Java 发版，P1 需要。C 端口径决定应在 P3 之前落定。
