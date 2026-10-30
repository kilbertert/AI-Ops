# C 端站点接口的回归自检（#1192）

日期：2026-10-30。分支 `feat/consumer-site-api`，基线 `7b5d62f3`。

本片**新增对外行为**（一个只读接口），所以除了「现有断言仍绿」，还要证明新契约本身。

## 已执行的检查

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 新模块 | `uv run pytest tests/consumer/test_consumer_sites.py` | 10 passed |
| 相关面 | `tests/operations/` + `tests/consumer/` + `tests/gateway/` | 780 passed, 2 skipped |
| 全量 | `uv run pytest` | 见 PR |
| 静态/格式/语法 | `ruff check`、`ruff format --check`、`compileall` | 通过 |
| 公网规范 | `tests/gateway/test_public_openapi.py` | 通过（19 → 20 条） |
| 镜像上下文摘要 | `deploy/m2-live-03/context_digest.py --check` | match |

## 新契约被证明的九件事

| 判据 | 断言方式 |
| --- | --- |
| 范围只含本人订单站点 | 订单里是 A、租户站点列表里有 A 和 B，响应只有 A |
| 订单请求永远带服务端推导的 `user-id` | 抓 `RequestSpec.headers`，断言 `user-id`/`tenant-id`，并断言**没有** `Authorization` |
| 空身份直接拒绝且不发任何请求 | 403 + 记录到出站调用数为 0 |
| 空身份不伪装成空列表 | 直接调推导函数，断言抛 `CONSUMER_SCOPE_REQUIRED` |
| 跨租户的行被拒 | 订单行 `tenantId` 不同 → `SITES_SCOPE_MISMATCH` |
| 截断要标注 | 订单 `total` 远大于一页 → `scope_truncated` 与 `limited` 都为真 |
| `limit` 超上限被拒 | `limit=11` → 400 `SITES_LIMIT_REJECTED`；`limit=3` → 200 |
| 空结果两种原因可区分 | 无订单 → `none_visible`；有站点但关键词无命中 → `no_match` |
| 范围推导只有一处 | 接口返回的集合 == 直接调 `own_site_scope()` 的结果 |

## `outside_scope` 是这一片唯一改到管家代码的地方

`query_sites` 多了一个关键字参数，默认 `reject`——**管家的行为一字未变**。这一点由既有的
`test_site_search_rejects_bad_evidence[site]` 继续覆盖：上游返回范围外的站点仍然整份拒绝。

C 端用 `skip` 的理由写在 ADR-0178：上游按租户返回，范围外的行是预期内的；按违约处理会让
「本人只有一个站点」的用户永远查不到自己的站点。租户不符在两种模式下都是硬拒绝。

## 未完成业务验收

- 没有真实 CPMS、没有真实账号、没有真实页面。上游形状来自
  `iot/cloud-charging-pile:release` 的源码只读核对，不是实测响应。
- **#1192 有一条验收标准只满足一半**：「与 C 端对话回合共用同一份范围推导」——C 端对话今天
  没有站点查询，那是 #1193 的范围。本片做到的是范围推导为一个函数、接口不自建第二份，
  并由一致性断言证明。这条不记为已满足。
- 不能引用本记录说明「C 端站点已上线」：没有部署、没有真实租户数据。
